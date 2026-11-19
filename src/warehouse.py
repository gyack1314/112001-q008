"""SQLite 数仓：按来源粒度存储事实，保留历史发布快照。

三类来源记录进入三张独立事实表：

* ``payments``    一条合同付款一行，金额的唯一事实粒度；
* ``packages``    一条施工台账工程包一行；
* ``facilities``  一条资产系统设施一行。

事实表之间不做宽表连接。付款与设施在报表层分别聚合，因此一个工程包下的
多个设施不会放大付款金额。合同、工程包、区域、设施之间的关系由
``attributions``（派生，带业务依据）显式表达。

``publications`` 及其明细表只插入、不更新、不删除，由触发器保护。重新加载
当前事实只会改变“当前视图”，不会改写既有发布。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator

from .model import (
    Allocation,
    FactError,
    Facility,
    Package,
    ParsedRecord,
    Payment,
    canonical_json,
    money,
    parse_record,
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_records (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    warnings TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    confirmed_at TEXT,
    declared_project_id TEXT,
    declared_region_id TEXT,
    source_line INTEGER NOT NULL,
    fact_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY,
    project_id TEXT,
    region_id TEXT,
    status TEXT NOT NULL,
    completed_at TEXT,
    source_line INTEGER NOT NULL,
    fact_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facilities (
    asset_id TEXT PRIMARY KEY,
    package_id TEXT,
    status TEXT NOT NULL,
    accepted_at TEXT,
    source_line INTEGER NOT NULL,
    fact_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    payment_id TEXT,
    package_id TEXT,
    project_id TEXT,
    region_id TEXT,
    amount TEXT,
    ratio TEXT,
    evidence TEXT NOT NULL,
    effective_at TEXT,
    source_line INTEGER NOT NULL,
    fact_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    fact_kind TEXT,
    fact_id TEXT,
    message TEXT NOT NULL,
    details TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'ingest'
);

CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    published_at TEXT NOT NULL,
    period_label TEXT NOT NULL,
    as_of TEXT,
    data_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_mappings (
    publication_id TEXT NOT NULL,
    allocation_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    payment_id TEXT,
    package_id TEXT,
    project_id TEXT,
    region_id TEXT,
    amount TEXT,
    ratio TEXT,
    evidence TEXT NOT NULL,
    effective_at TEXT,
    PRIMARY KEY (publication_id, allocation_id)
);

CREATE TABLE IF NOT EXISTS publication_statuses (
    publication_id TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    status TEXT NOT NULL,
    effective_date TEXT,
    detail TEXT NOT NULL,
    PRIMARY KEY (publication_id, fact_kind, fact_id)
);

CREATE TABLE IF NOT EXISTS publication_totals (
    publication_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    currency TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (publication_id, metric, currency)
);

-- 发布结果不可改写。
CREATE TRIGGER IF NOT EXISTS publications_no_update
BEFORE UPDATE ON publications
BEGIN
    SELECT RAISE(ABORT, 'published results are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publications_no_delete
BEFORE DELETE ON publications
BEGIN
    SELECT RAISE(ABORT, 'published results are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publication_mappings_no_update
BEFORE UPDATE ON publication_mappings
BEGIN
    SELECT RAISE(ABORT, 'published mappings are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publication_mappings_no_delete
BEFORE DELETE ON publication_mappings
BEGIN
    SELECT RAISE(ABORT, 'published mappings are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publication_statuses_no_update
BEFORE UPDATE ON publication_statuses
BEGIN
    SELECT RAISE(ABORT, 'published statuses are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publication_statuses_no_delete
BEFORE DELETE ON publication_statuses
BEGIN
    SELECT RAISE(ABORT, 'published statuses are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publication_totals_no_update
BEFORE UPDATE ON publication_totals
BEGIN
    SELECT RAISE(ABORT, 'published totals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publication_totals_no_delete
BEFORE DELETE ON publication_totals
BEGIN
    SELECT RAISE(ABORT, 'published totals are immutable');
END;
"""


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _json_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Warehouse:
    """一个 SQLite 文件对应一个本地数仓。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------
    # 连接和基础查询

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)))

    # ------------------------------------------------------------------
    # 装载：替换当前事实，保留发布

    def load_records(self, records: list[Any]) -> dict[str, Any]:
        parsed: list[ParsedRecord] = []
        parse_errors: list[dict[str, Any]] = []
        for index, record in enumerate(records, start=1):
            try:
                parsed.append(parse_record(record, ordinal=index))
            except FactError as exc:
                parse_errors.append(
                    {"code": "invalid_record", "severity": "error", "source_line": index,
                     "message": str(exc)}
                )
        if parse_errors and not parsed:
            raise FactError("没有可装载的有效记录；" + parse_errors[0]["message"])

        data_hash = _json_hash(canonical_json(records))
        issues: list[dict[str, Any]] = list(parse_errors)
        with self.transaction() as conn:
            conn.execute("DELETE FROM payments")
            conn.execute("DELETE FROM packages")
            conn.execute("DELETE FROM facilities")
            conn.execute("DELETE FROM allocations")
            conn.execute("DELETE FROM raw_records")
            conn.execute("DELETE FROM issues WHERE scope = 'ingest'")
            conn.execute("DELETE FROM meta")

            seen: dict[str, str] = {}
            for parsed_record in parsed:
                record = parsed_record.fact
                key = self._business_key(record)
                if key in seen:
                    issues.append(self._issue(
                        "duplicate_identity", "error", parsed_record.kind, parsed_record.fact_id,
                        f"{parsed_record.kind} 标识 {key} 重复出现，保留首次记录，忽略后续记录。",
                        {"identity": key, "kept_fact_id": seen[key]},
                    ))
                    continue
                seen[key] = parsed_record.fact_id
                conn.execute(
                    "INSERT INTO raw_records(fact_id, kind, payload, warnings) VALUES (?, ?, ?, ?)",
                    (parsed_record.fact_id, parsed_record.kind,
                     canonical_json({"fact_kind": parsed_record.kind, **record.__dict__}),
                     canonical_json(list(parsed_record.warnings))),
                )
                self._insert_fact(conn, parsed_record)
                for warning in parsed_record.warnings:
                    issues.append(self._issue(
                        "source_warning", "warning", parsed_record.kind, parsed_record.fact_id,
                        warning, {},
                    ))

            issues.extend(self._structural_issues())
            for issue in issues:
                self._insert_issue(conn, issue)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('data_hash', ?), ('loaded_at', ?)",
                (data_hash, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        return {
            "data_hash": data_hash,
            "records_loaded": len(seen),
            "records_rejected": len(parse_errors),
            "issues": issues,
        }

    @staticmethod
    def _business_key(fact: Any) -> str:
        if isinstance(fact, Payment):
            return f"payment:{fact.payment_id}"
        if isinstance(fact, Package):
            return f"package:{fact.package_id}"
        if isinstance(fact, Facility):
            return f"asset:{fact.asset_id}"
        if isinstance(fact, Allocation):
            return f"allocation:{fact.allocation_id}"
        raise TypeError(type(fact))

    def _insert_fact(self, conn: sqlite3.Connection, record: ParsedRecord) -> None:
        fact = record.fact
        if isinstance(fact, Payment):
            conn.execute(
                "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fact.payment_id, fact.contract_id, money(fact.amount), fact.currency,
                 fact.status, _iso(fact.confirmed_at), fact.declared_project_id,
                 fact.declared_region_id, fact.source_line, fact.fact_id),
            )
        elif isinstance(fact, Package):
            conn.execute(
                "INSERT INTO packages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fact.package_id, fact.project_id, fact.region_id, fact.status,
                 _iso(fact.completed_at), fact.source_line, fact.fact_id),
            )
        elif isinstance(fact, Facility):
            conn.execute(
                "INSERT INTO facilities VALUES (?, ?, ?, ?, ?, ?)",
                (fact.asset_id, fact.package_id, fact.status, _iso(fact.accepted_at),
                 fact.source_line, fact.fact_id),
            )
        else:
            conn.execute(
                "INSERT INTO allocations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fact.allocation_id, fact.contract_id, fact.payment_id, fact.package_id,
                 fact.project_id, fact.region_id, money(fact.amount) if fact.amount is not None else None,
                 str(fact.ratio) if fact.ratio is not None else None, fact.evidence,
                 _iso(fact.effective_at), fact.source_line, fact.fact_id),
            )

    def _structural_issues(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        payments = {row["payment_id"]: row for row in self._rows("SELECT * FROM payments")}
        contracts = {row["contract_id"] for row in payments.values()}
        packages = {row["package_id"]: row for row in self._rows("SELECT * FROM packages")}

        for payment in payments.values():
            if payment["status"] == "confirmed" and payment["confirmed_at"] is None:
                issues.append(self._issue(
                    "confirmed_payment_missing_date", "warning", "payment", payment["fact_id"],
                    f"付款 {payment['payment_id']} 已确认但缺少 confirmed_at，无法进入任何报告期。",
                    {"payment_id": payment["payment_id"]},
                ))
        for package in packages.values():
            if not package["project_id"]:
                issues.append(self._issue(
                    "package_missing_project", "warning", "package", package["fact_id"],
                    f"工程包 {package['package_id']} 未登记项目，相关金额/设施只能归入未归属项目。",
                    {"package_id": package["package_id"]},
                ))
            if not package["region_id"]:
                issues.append(self._issue(
                    "package_missing_region", "warning", "package", package["fact_id"],
                    f"工程包 {package['package_id']} 未登记区域，相关金额/设施只能归入未归属区域。",
                    {"package_id": package["package_id"]},
                ))
        for row in self._rows("SELECT * FROM facilities"):
            if not row["package_id"]:
                issues.append(self._issue(
                    "facility_missing_package", "warning", "asset_acceptance", row["fact_id"],
                    f"设施 {row['asset_id']} 未登记工程包，无法关联项目或区域。",
                    {"asset_id": row["asset_id"]},
                ))
            elif row["package_id"] not in packages:
                issues.append(self._issue(
                    "unknown_package_reference", "error", "asset_acceptance", row["fact_id"],
                    f"设施 {row['asset_id']} 引用的工程包 {row['package_id']} 不存在。",
                    {"asset_id": row["asset_id"], "package_id": row["package_id"]},
                ))

        contract_totals: dict[str, Decimal] = {}
        for payment in payments.values():
            contract_totals[payment["contract_id"]] = (
                contract_totals.get(payment["contract_id"], Decimal(0))
                + Decimal(payment["amount"])
            )
        allocated_by_contract: dict[str, Decimal] = {}
        ratio_by_contract: dict[str, Decimal] = {}
        for alloc in self._rows("SELECT * FROM allocations"):
            if alloc["contract_id"] not in contracts:
                issues.append(self._issue(
                    "allocation_unknown_contract", "error", "allocation", alloc["fact_id"],
                    f"分摊 {alloc['allocation_id']} 引用的合同 {alloc['contract_id']} 没有付款记录。",
                    {"allocation_id": alloc["allocation_id"]},
                ))
            if alloc["payment_id"]:
                payment = payments.get(alloc["payment_id"])
                if payment is None:
                    issues.append(self._issue(
                        "allocation_unknown_payment", "error", "allocation", alloc["fact_id"],
                        f"分摊 {alloc['allocation_id']} 引用的付款 {alloc['payment_id']} 不存在，"
                        "该分摊不会计入。",
                        {"allocation_id": alloc["allocation_id"]},
                    ))
                elif payment["contract_id"] != alloc["contract_id"]:
                    issues.append(self._issue(
                        "allocation_payment_contract_mismatch", "error", "allocation",
                        alloc["fact_id"],
                        f"分摊 {alloc['allocation_id']} 标注合同 {alloc['contract_id']}，"
                        f"但付款 {alloc['payment_id']} 属于合同 {payment['contract_id']}。",
                        {"allocation_id": alloc["allocation_id"]},
                    ))
            if alloc["package_id"] and alloc["package_id"] not in packages:
                issues.append(self._issue(
                    "allocation_unknown_package", "error", "allocation", alloc["fact_id"],
                    f"分摊 {alloc['allocation_id']} 引用的工程包 {alloc['package_id']} 不存在；"
                    "该金额不会归入任何区域。",
                    {"allocation_id": alloc["allocation_id"]},
                ))
            if alloc["amount"] is not None:
                allocated_by_contract[alloc["contract_id"]] = (
                    allocated_by_contract.get(alloc["contract_id"], Decimal(0))
                    + Decimal(alloc["amount"])
                )
            else:
                ratio_by_contract[alloc["contract_id"]] = (
                    ratio_by_contract.get(alloc["contract_id"], Decimal(0))
                    + Decimal(alloc["ratio"])
                )
        for contract_id in sorted(set(allocated_by_contract) | set(ratio_by_contract)):
            total = contract_totals.get(contract_id, Decimal(0))
            fixed = allocated_by_contract.get(contract_id, Decimal(0))
            ratio = ratio_by_contract.get(contract_id, Decimal(0))
            if ratio > 1:
                issues.append(self._issue(
                    "allocation_ratio_exceeds_contract", "error", "allocation", None,
                    f"合同 {contract_id} 的分摊比例合计为 {ratio}，超过 100%。",
                    {"contract_id": contract_id, "ratio": str(ratio)},
                ))
            if fixed + total * ratio > total:
                issues.append(self._issue(
                    "allocation_exceeds_contract", "error", "allocation", None,
                    f"合同 {contract_id} 的明确分摊金额与比例折算合计超过其付款总额；"
                    "报表会单独列示超额部分，不做静默裁剪。",
                    {"contract_id": contract_id, "confirmed_base": money(total),
                     "fixed": money(fixed), "ratio": str(ratio)},
                ))
        return issues

    @staticmethod
    def _issue(
        code: str, severity: str, fact_kind: str | None, fact_id: str | None,
        message: str, details: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "code": code,
            "severity": severity,
            "fact_kind": fact_kind,
            "fact_id": fact_id,
            "message": message,
            "details": details,
        }

    def _insert_issue(self, conn: sqlite3.Connection, issue: dict[str, Any], scope: str = "ingest") -> None:
        conn.execute(
            "INSERT INTO issues(code, severity, fact_kind, fact_id, message, details, scope) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (issue["code"], issue["severity"], issue.get("fact_kind"), issue.get("fact_id"),
             issue["message"], canonical_json(issue.get("details", {})), scope),
        )

    # ------------------------------------------------------------------
    # 事实读取

    def data_hash(self) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'data_hash'").fetchone()
        return row["value"] if row else None

    def loaded_at(self) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'loaded_at'").fetchone()
        return row["value"] if row else None

    def payments(self) -> list[Payment]:
        return [
            Payment(
                payment_id=row["payment_id"],
                contract_id=row["contract_id"],
                amount=Decimal(row["amount"]),
                currency=row["currency"],
                status=row["status"],
                confirmed_at=_date(row["confirmed_at"]),
                declared_project_id=row["declared_project_id"],
                declared_region_id=row["declared_region_id"],
                source_line=row["source_line"],
                fact_id=row["fact_id"],
            )
            for row in self._rows("SELECT * FROM payments ORDER BY payment_id")
        ]

    def packages(self) -> list[Package]:
        return [
            Package(
                package_id=row["package_id"],
                project_id=row["project_id"],
                region_id=row["region_id"],
                status=row["status"],
                completed_at=_date(row["completed_at"]),
                source_line=row["source_line"],
                fact_id=row["fact_id"],
            )
            for row in self._rows("SELECT * FROM packages ORDER BY package_id")
        ]

    def facilities(self) -> list[Facility]:
        return [
            Facility(
                asset_id=row["asset_id"],
                package_id=row["package_id"],
                status=row["status"],
                accepted_at=_date(row["accepted_at"]),
                source_line=row["source_line"],
                fact_id=row["fact_id"],
            )
            for row in self._rows("SELECT * FROM facilities ORDER BY asset_id")
        ]

    def allocations(self) -> list[Allocation]:
        return [
            Allocation(
                allocation_id=row["allocation_id"],
                contract_id=row["contract_id"],
                payment_id=row["payment_id"],
                package_id=row["package_id"],
                project_id=row["project_id"],
                region_id=row["region_id"],
                amount=Decimal(row["amount"]) if row["amount"] is not None else None,
                ratio=Decimal(row["ratio"]) if row["ratio"] is not None else None,
                evidence=row["evidence"],
                effective_at=_date(row["effective_at"]),
                source_line=row["source_line"],
                fact_id=row["fact_id"],
            )
            for row in self._rows("SELECT * FROM allocations ORDER BY allocation_id")
        ]

    def issues(self) -> list[dict[str, Any]]:
        return [
            {
                "code": row["code"],
                "severity": row["severity"],
                "fact_kind": row["fact_kind"],
                "fact_id": row["fact_id"],
                "message": row["message"],
                "details": json.loads(row["details"]),
                "scope": row["scope"],
            }
            for row in self._rows("SELECT * FROM issues ORDER BY issue_id")
        ]

    def table_counts(self) -> dict[str, int]:
        return {
            table: self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("payments", "packages", "facilities", "allocations",
                          "issues", "publications")
        }

    # ------------------------------------------------------------------
    # 发布

    def get_publication(self, publication_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM publications WHERE publication_id = ?", (publication_id,)
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        if _json_hash(row["payload"]) != row["payload_hash"]:
            raise RuntimeError(f"发布 {publication_id} 的内容校验失败，快照可能已损坏")
        return {
            "publication_id": row["publication_id"],
            "published_at": row["published_at"],
            "period_label": row["period_label"],
            "as_of": row["as_of"],
            "data_hash": row["data_hash"],
            "payload": payload,
            "payload_hash": row["payload_hash"],
        }

    def list_publications(self) -> list[dict[str, Any]]:
        return [
            {
                "publication_id": row["publication_id"],
                "published_at": row["published_at"],
                "period_label": row["period_label"],
                "as_of": row["as_of"],
                "data_hash": row["data_hash"],
            }
            for row in self._rows(
                "SELECT publication_id, published_at, period_label, as_of, data_hash "
                "FROM publications ORDER BY published_at, publication_id"
            )
        ]
