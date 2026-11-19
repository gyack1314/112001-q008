"""发布：把某个报告期的结果冻结为不可变快照。

发布内容包含当时的映射（使用的分摊证据及其折算金额）和确认状态
（付款确认、工程包完工、设施验收）。后续补充的关联会进入新的当前视图和
新的发布，但不会修改既有发布的载荷、明细或合计。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from typing import Any

from .model import Period, canonical_json
from .report import Report
from .warehouse import Warehouse


def _publication_id(report: Report, published_at: datetime) -> str:
    from .model import _hash

    return "PUB-" + _hash({
        "period": report.period.as_dict(),
        "as_of": report.as_of.isoformat() if report.as_of else None,
        "mode": report.mode,
        "data_hash": report.data_hash,
        "payload_hash": report.payload_hash,
        "published_at": published_at.isoformat(timespec="seconds"),
    })[:16]


def publish(
    warehouse: Warehouse,
    report: Report,
    published_at: datetime | None = None,
) -> dict[str, Any]:
    published_at = published_at or datetime.now(timezone.utc)
    publication_id = _publication_id(report, published_at)
    payload = report.payload
    payload_json = canonical_json(payload)

    with warehouse.transaction() as conn:
        existing = conn.execute(
            "SELECT publication_id FROM publications WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        if existing is not None:
            raise RuntimeError(f"发布 {publication_id} 已存在，历史结果不可覆盖")

        conn.execute(
            "INSERT INTO publications(publication_id, published_at, period_label, as_of, "
            "data_hash, payload, payload_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (publication_id, published_at.isoformat(timespec="seconds"),
             report.period.label, report.as_of.isoformat() if report.as_of else None,
             report.data_hash or "", payload_json, report.payload_hash),
        )
        for mapping in payload["attribution"]["mappings"]:
            conn.execute(
                "INSERT OR IGNORE INTO publication_mappings(publication_id, allocation_id, "
                "contract_id, payment_id, package_id, project_id, region_id, amount, ratio, "
                "evidence, effective_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (publication_id, mapping["allocation_id"], mapping["contract_id"],
                 mapping["payment_id"], mapping["package_id"], mapping["project_id"],
                 mapping["region_id"], mapping["amount"], mapping["ratio"],
                 mapping["evidence"], mapping["effective_at"]),
            )
        for payment in payload["confirmed_investment"]["payments"]:
            conn.execute(
                "INSERT OR REPLACE INTO publication_statuses(publication_id, fact_kind, "
                "fact_id, status, effective_date, detail) VALUES (?, 'payment', ?, ?, ?, ?)",
                (publication_id, payment["payment_id"], payment["status"],
                 payment["confirmed_at"], canonical_json({
                     "in_period": payment["in_period"], "note": payment["note"]})),
            )
        for region in payload["delivered_facilities"]["by_region"]:
            for project, entry in region["by_project"].items():
                for facility in entry["assets"]:
                    conn.execute(
                        "INSERT OR REPLACE INTO publication_statuses(publication_id, fact_kind, "
                        "fact_id, status, effective_date, detail) VALUES (?, 'facility', ?, "
                        "'accepted', ?, ?)",
                        (publication_id, facility["asset_id"], facility["accepted_at"],
                         canonical_json({"region": region["region"], "project": project,
                                         "package_id": facility["package_id"]})),
                    )
                for package in entry["packages"]:
                    conn.execute(
                        "INSERT OR REPLACE INTO publication_statuses(publication_id, fact_kind, "
                        "fact_id, status, effective_date, detail) VALUES (?, 'package', ?, "
                        "'completed', ?, ?)",
                        (publication_id, package["package_id"], package["completed_at"],
                         canonical_json({"region": region["region"], "project": project})),
                    )
        for currency, totals in payload["confirmed_investment"]["by_currency"].items():
            for metric, value in totals.items():
                conn.execute(
                    "INSERT OR REPLACE INTO publication_totals(publication_id, metric, "
                    "currency, value) VALUES (?, ?, ?, ?)",
                    (publication_id, metric, currency, value),
                )
    return {
        "publication_id": publication_id,
        "published_at": published_at.isoformat(timespec="seconds"),
        "period": report.period.label,
        "data_hash": report.data_hash,
        "payload_hash": report.payload_hash,
    }


def diff_publication(warehouse: Warehouse, publication_id: str) -> dict[str, Any]:
    """比较某份历史发布与当前事实视图，说明后续关联带来的变化。"""

    stored = warehouse.get_publication(publication_id)
    if stored is None:
        raise KeyError(f"发布不存在: {publication_id}")
    previous = stored["payload"]
    report_period = Period(
        label=previous["report"]["period"]["label"],
        kind=previous["report"]["period"]["kind"],
        start=date.fromisoformat(previous["report"]["period"]["start"]) if previous["report"]["period"]["start"] else None,
        end=date.fromisoformat(previous["report"]["period"]["end"]) if previous["report"]["period"]["end"] else None,
    )
    from .report import build_report

    # 当前视图不收窄到发布时点：后续补充的归属证据应体现为新增映射。
    current = build_report(warehouse, report_period, mode=previous["report"]["mode"])
    current_payload = current.payload

    old_mappings = {m["allocation_id"]: m for m in previous["attribution"]["mappings"]}
    new_mappings = {m["allocation_id"]: m for m in current_payload["attribution"]["mappings"]}

    def totals(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
        return {
            currency: dict(values)
            for currency, values in payload["confirmed_investment"]["by_currency"].items()
        }

    return {
        "publication_id": publication_id,
        "published_at": stored["published_at"],
        "publication_as_of": previous["report"]["as_of"],
        "publication_data_hash": stored["data_hash"],
        "current_data_hash": current.data_hash,
        "mapping_added": sorted(set(new_mappings) - set(old_mappings)),
        "mapping_removed": sorted(set(old_mappings) - set(new_mappings)),
        "mapping_changed": sorted(
            key for key in set(old_mappings) & set(new_mappings)
            if old_mappings[key] != new_mappings[key]
        ),
        "totals_published": totals(previous),
        "totals_current": totals(current_payload),
        "accepted_facilities_published": previous["delivered_facilities"]["accepted_in_period"],
        "accepted_facilities_current": current_payload["delivered_facilities"]["accepted_in_period"],
        "states_published": {
            "payment": previous["payment_confirmation_states"],
            "package": previous["package_completion_states"],
            "facility": previous["facility_acceptance_states"],
        },
        "states_current": {
            "payment": current_payload["payment_confirmation_states"],
            "package": current_payload["package_completion_states"],
            "facility": current_payload["facility_acceptance_states"],
        },
        "note": "历史发布内容保持不变；以上差异是当前视图相对发布时点的变化。",
    }
