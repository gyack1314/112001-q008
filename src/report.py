"""报告层：按报告期汇总已确认投入和已验收设施，并解释未归属原因。

关键约束：

* 付款金额只从 ``payments`` 聚合一次；设施数量只从 ``facilities`` 聚合一次。
  两者不在同一行计算，所以付款金额不会因工程包下的设施数量而膨胀。
* 只有带业务依据的 ``allocation`` 记录才把合同金额指向工程包、项目或区域。
  没有依据的余额进入 ``unallocated``，不按设施数量或工程包个数均摊。
* 工程完工、资产验收、付款确认三种状态各自独立计算，互不替代。
* 分摊按合同整体表达。某一报告期只包含合同的部分付款时，已归属金额按
  该期已确认付款占合同已确认总额的比例折算；伪造不出新的归属关系。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from .model import Allocation, Facility, Package, Payment, Period, ZERO, money
from .warehouse import Warehouse


UNASSIGNED = "(未归属)"
MODE_CONFIRMED = "confirmed"
MODE_AS_KNOWN = "as-known"
CENT = Decimal("0.01")

# 未分配金额原因的稳定代码，便于机器消费。
REASON_NO_ALLOCATION = "no_allocation_evidence"
REASON_PARTIAL_ALLOCATION = "partial_allocation_evidence"
REASON_ALLOCATION_UNKNOWN_PAYMENT = "allocation_unknown_payment"
REASON_ALLOCATION_UNKNOWN_PACKAGE = "allocation_unknown_package"
REASON_ALLOCATION_CONTRACT_MISMATCH = "allocation_payment_contract_mismatch"
REASON_ALLOCATION_MISSING_TARGET = "allocation_missing_target"
REASON_PAYMENT_UNCONFIRMED = "payment_unconfirmed"
REASON_PAYMENT_NO_DATE = "payment_confirmed_missing_date"
REASON_PAYMENT_CURRENCY_MIXED = "contract_currency_mixed"
REASON_ALLOCATION_NOT_YET_EFFECTIVE = "allocation_effective_after_as_of"

# 未归属设施原因的稳定代码。
REASON_ACCEPTANCE_PENDING = "asset_acceptance_pending"
REASON_ACCEPTANCE_REJECTED = "asset_acceptance_rejected"
REASON_ACCEPTANCE_NO_DATE = "asset_accepted_missing_date"
REASON_ASSET_NO_PACKAGE = "asset_missing_package"
REASON_ASSET_UNKNOWN_PACKAGE = "asset_unknown_package"
REASON_ASSET_PACKAGE_NO_PROJECT = "package_missing_project"
REASON_ASSET_PACKAGE_NO_REGION = "package_missing_region"

REASON_PACKAGE_NOT_COMPLETED = "package_not_completed"


def _add(bucket: dict[str, Decimal], key: str, value: Decimal) -> None:
    bucket[key] = bucket.get(key, ZERO) + value


def _money_bucket(bucket: dict[str, Decimal]) -> dict[str, str]:
    return {key: money(value) for key, value in sorted(bucket.items())}


def _to_cents(value: Decimal) -> int:
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _from_cents(value: int) -> Decimal:
    return (Decimal(value) / 100).quantize(CENT)


def _distribute(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """把 ``total`` 按权重拆成精确到分的若干份，保证各份之和等于总额。"""

    if not weights:
        return []
    cents = _to_cents(total)
    weight_sum = sum(weights)
    if cents <= 0 or weight_sum <= 0:
        return [ZERO for _ in weights]
    raw = [Decimal(cents) * weight / weight_sum for weight in weights]
    floors = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in raw]
    remainder = cents - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - floors[i], -i), reverse=True)
    for index in order[:remainder]:
        floors[index] += 1
    return [_from_cents(value) for value in floors]


@dataclass
class AllocationEval:
    """一条分摊证据在某份合同当前付款基础上的折算结果。"""

    allocation: Allocation
    amount: Decimal
    kind: str
    status: str
    reason: str | None
    payment_id: str | None
    package_id: str | None
    project_id: str | None
    region_id: str | None
    currency: str
    amount_in_period: Decimal = ZERO

    def as_mapping(self) -> dict[str, Any]:
        return {
            "allocation_id": self.allocation.allocation_id,
            "contract_id": self.allocation.contract_id,
            "payment_id": self.payment_id,
            "package_id": self.package_id,
            "project_id": self.project_id,
            "region_id": self.region_id,
            "amount": money(self.amount),
            "amount_in_period": money(self.amount_in_period),
            "basis": self.kind,
            "ratio": str(self.allocation.ratio) if self.allocation.ratio is not None else None,
            "evidence": self.allocation.evidence,
            "effective_at": (
                self.allocation.effective_at.isoformat() if self.allocation.effective_at else None
            ),
            "status": self.status,
            "reason": self.reason,
            "currency": self.currency,
        }


@dataclass
class Report:
    period: Period
    as_of: date | None
    mode: str
    warehouse: Warehouse
    data_hash: str | None

    def __post_init__(self) -> None:
        self.payments = self.warehouse.payments()
        self.packages = {p.package_id: p for p in self.warehouse.packages()}
        self.facilities = self.warehouse.facilities()
        self.allocations = self.warehouse.allocations()
        self.allocation_cache: dict[str, list[AllocationEval]] = {}
        self._build()
        self.payload = self._payload()
        self.payload_hash = _hash_payload(self.payload)

    # ------------------------------------------------------------------
    # 期间与归属证据

    def _payment_in_scope(self, payment: Payment) -> bool:
        if self.mode == MODE_AS_KNOWN:
            # as-known：付款只要已确认就计入，不论确认日期是否落在报告期。
            return payment.status == "confirmed"
        return payment.status == "confirmed" and self.period.contains(payment.confirmed_at)

    def _confirmed_base(self, contract_id: str) -> tuple[Decimal, str]:
        """比例分摊的基数：该合同已确认付款总额，与报告期无关。"""

        total = ZERO
        currencies: set[str] = set()
        for payment in self.payments:
            if payment.contract_id == contract_id and payment.status == "confirmed":
                total += payment.amount
                currencies.add(payment.currency)
        if len(currencies) > 1:
            return total, "mixed"
        return total, next(iter(currencies), "CNY")

    def allocations_for(self, contract_id: str) -> list[AllocationEval]:
        cached = self.allocation_cache.get(contract_id)
        if cached is not None:
            return cached
        total, currency = self._confirmed_base(contract_id)
        payments_by_id = {p.payment_id: p for p in self.payments}
        packages = self.packages
        result: list[AllocationEval] = []
        for alloc in self.allocations:
            if alloc.contract_id != contract_id:
                continue
            status, reason = "effective", None
            if currency == "mixed":
                status, reason = "invalid", REASON_PAYMENT_CURRENCY_MIXED
            if alloc.payment_id is not None:
                payment = payments_by_id.get(alloc.payment_id)
                if payment is None:
                    status, reason = "invalid", REASON_ALLOCATION_UNKNOWN_PAYMENT
                elif payment.contract_id != contract_id:
                    status, reason = "invalid", REASON_ALLOCATION_CONTRACT_MISMATCH
                elif payment.status != "confirmed":
                    status, reason = "invalid", REASON_PAYMENT_UNCONFIRMED
            if alloc.package_id is not None and alloc.package_id not in packages:
                status, reason = "invalid", REASON_ALLOCATION_UNKNOWN_PACKAGE
            if (
                status == "effective"
                and self.as_of is not None
                and alloc.effective_at is not None
                and alloc.effective_at > self.as_of
            ):
                # as-of 复算：当时尚未补充的归属证据不改变当时视图。
                status, reason = "not_yet_effective", REASON_ALLOCATION_NOT_YET_EFFECTIVE
            amount = alloc.amount if alloc.amount is not None else (total * alloc.ratio).quantize(CENT)
            target = self._resolve_target(alloc)
            if status == "effective" and target is None:
                status, reason = "invalid", REASON_ALLOCATION_MISSING_TARGET
            result.append(AllocationEval(
                allocation=alloc,
                amount=amount,
                kind="fixed" if alloc.amount is not None else "ratio",
                status=status,
                reason=reason,
                payment_id=alloc.payment_id,
                package_id=alloc.package_id,
                project_id=target[0] if target else alloc.project_id,
                region_id=target[1] if target else alloc.region_id,
                currency="MIXED" if currency == "mixed" else currency,
            ))
        self.allocation_cache[contract_id] = result
        return result

    def _resolve_target(self, alloc: Allocation) -> tuple[str | None, str | None] | None:
        """解析分摊目标为 (project, region)：显式证据优先，工程包登记其次。"""

        if alloc.package_id is not None:
            package = self.packages.get(alloc.package_id)
            if package is None:
                return None
            return (alloc.project_id or package.project_id, alloc.region_id or package.region_id)
        return (alloc.project_id, alloc.region_id)

    def allocation_summary(self, contract_id: str) -> dict[str, Any]:
        evaluations = self.allocations_for(contract_id)
        effective = [e for e in evaluations if e.status == "effective"]
        fixed = sum((e.amount for e in effective if e.kind == "fixed"), ZERO)
        ratio = sum((e.amount for e in effective if e.kind == "ratio"), ZERO)
        total, currency = self._confirmed_base(contract_id)
        return {
            "contract_id": contract_id,
            "confirmed_base": total,
            "effective_fixed": fixed,
            "effective_ratio": ratio,
            "effective_total": fixed + ratio,
            "currency": currency,
            "evaluations": evaluations,
        }

    def contract_breakdown(self, contract_id: str) -> dict[str, Any]:
        """合同在当前报告期内的已确认付款与按证据折算的已归属金额。"""

        in_scope = [p for p in self.payments
                    if p.contract_id == contract_id and self._payment_in_scope(p)]
        period_total = sum((p.amount for p in in_scope), ZERO)
        confirmed_total, currency = self._confirmed_base(contract_id)
        evaluations = self.allocations_for(contract_id)

        effective = [e for e in evaluations if e.status == "effective"]
        invalid = [e for e in evaluations if e.status != "effective"]
        effective_total = sum((e.amount for e in effective), ZERO)

        if period_total <= 0 or confirmed_total <= 0 or currency == "mixed":
            period_allocated = ZERO
            overrun = ZERO
            allocations_in_period: list[AllocationEval] = []
        else:
            usable = min(effective_total, confirmed_total)
            overrun = max(effective_total - confirmed_total, ZERO)
            period_share = period_total / confirmed_total
            period_allocated = (usable * period_share).quantize(CENT)
            parts = _distribute(period_allocated, [e.amount for e in effective])
            allocations_in_period = []
            for evaluation, part in zip(effective, parts):
                evaluation.amount_in_period = part
                allocations_in_period.append(evaluation)

        period_unallocated = period_total - period_allocated
        if period_unallocated < 0:
            # 分摊超过本期付款（例如合同后续付款尚未确认）：超出部分仍单独披露。
            overrun += -period_unallocated
            period_unallocated = ZERO

        # 未归属金额按“有无可用证据”直接定性；无效证据只单独列示金额，
        # 不用它去均摊未归属金额，否则等于伪造归属。
        reasons: dict[str, Decimal] = {}
        if period_unallocated > 0:
            key = REASON_PARTIAL_ALLOCATION if effective_total > 0 else REASON_NO_ALLOCATION
            reasons[key] = period_unallocated
        invalid_evidence: dict[str, Decimal] = {}
        for evaluation in invalid:
            key = evaluation.reason or REASON_ALLOCATION_MISSING_TARGET
            invalid_evidence[key] = invalid_evidence.get(key, ZERO) + evaluation.amount

        return {
            "contract_id": contract_id,
            "currency": "MIXED" if currency == "mixed" else currency,
            "confirmed_in_period": period_total,
            "confirmed_total": confirmed_total,
            "allocated_in_period": period_allocated,
            "unallocated_in_period": period_unallocated,
            "allocation_overrun": overrun,
            "reasons": dict(reasons),
            "invalid_evidence": dict(invalid_evidence),
            "evaluations": evaluations,
            "effective_in_period": allocations_in_period,
            "payments_in_period": in_scope,
        }

    # ------------------------------------------------------------------
    # 汇总

    def _build(self) -> None:
        self.currency_totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        self.region_amounts: dict[str, dict[str, dict[str, Decimal]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(Decimal))
        )
        self.project_amounts: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        self.contracts: list[dict[str, Any]] = []
        self.unallocated_by_contract: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        self.payment_details: list[dict[str, Any]] = []
        self.region_asset_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.region_completed_packages: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.asset_status_counts: dict[str, int] = defaultdict(int)
        self.package_states: dict[str, int] = defaultdict(int)
        self.payment_states: dict[str, int] = defaultdict(int)
        self.package_completed_in_period: set[str] = set()
        self.region_assets: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.region_packages: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.unassigned_assets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        contract_ids = sorted({payment.contract_id for payment in self.payments})
        for contract_id in contract_ids:
            breakdown = self.contract_breakdown(contract_id)
            currency = breakdown["currency"]
            self.contracts.append(breakdown)
            if breakdown["confirmed_in_period"] > 0 and currency != "MIXED":
                _add(self.currency_totals[currency], "confirmed_payments", breakdown["confirmed_in_period"])
                _add(self.currency_totals[currency], "allocated", breakdown["allocated_in_period"])
                _add(self.currency_totals[currency], "unallocated", breakdown["unallocated_in_period"])
                _add(self.currency_totals[currency], "allocation_overrun", breakdown["allocation_overrun"])
                for reason, value in breakdown["reasons"].items():
                    _add(self.unallocated_by_contract[currency], reason, value)
                for evaluation in breakdown["effective_in_period"]:
                    region = evaluation.region_id or UNASSIGNED
                    _add(self.region_amounts[currency][region], "allocated", evaluation.amount_in_period)
                    _add(self.project_amounts[currency], evaluation.project_id or UNASSIGNED,
                         evaluation.amount_in_period)

        for payment in self.payments:
            self.payment_states[payment.confirmed_status] += 1
            in_period = self._payment_in_scope(payment)
            self.payment_details.append({
                "payment_id": payment.payment_id,
                "contract_id": payment.contract_id,
                "amount": money(payment.amount),
                "currency": payment.currency,
                "status": payment.status,
                "confirmed_at": payment.confirmed_at.isoformat() if payment.confirmed_at else None,
                "in_period": in_period,
                "note": None if in_period else self._out_of_period_note(payment),
            })

        for package in self.packages.values():
            self.package_states[package.completion_status] += 1
            if package.status != "completed" or not self.period.contains(package.completed_at):
                continue
            region = package.region_id or UNASSIGNED
            project = package.project_id or UNASSIGNED
            self.package_completed_in_period.add(package.package_id)
            self.region_completed_packages[region][project] += 1
            self.region_packages[region][project].append({
                "package_id": package.package_id,
                "project_id": package.project_id,
                "region_id": package.region_id,
                "completed_at": package.completed_at.isoformat(),
                "status": package.status,
            })

        for facility in self.facilities:
            self.asset_status_counts[facility.acceptance_status] += 1
            package = self.packages.get(facility.package_id) if facility.package_id else None
            region, project, reason = self._facility_scope(facility, package)
            accepted_in_period = (
                facility.status == "accepted" and self.period.contains(facility.accepted_at)
            )
            if accepted_in_period and region is not None:
                self.region_asset_counts[region][project or UNASSIGNED] += 1
                self.region_assets[region][project or UNASSIGNED].append({
                    "asset_id": facility.asset_id,
                    "package_id": facility.package_id,
                    "accepted_at": facility.accepted_at.isoformat(),
                    "status": facility.status,
                })
            elif accepted_in_period:
                self.unassigned_assets["region"][reason or REASON_ASSET_PACKAGE_NO_REGION] += 1
                if project is None:
                    self.unassigned_assets["project"][
                        reason or REASON_ASSET_PACKAGE_NO_PROJECT] += 1
            else:
                state = self._facility_out_of_period_reason(facility, package)
                if state != "outside_period":
                    self.unassigned_assets["acceptance_state"][state] += 1

    def _out_of_period_note(self, payment: Payment) -> str:
        if payment.status != "confirmed":
            return REASON_PAYMENT_UNCONFIRMED
        if payment.confirmed_at is None:
            return REASON_PAYMENT_NO_DATE
        return "outside_period"

    def _facility_scope(
        self, facility: Facility, package: Package | None
    ) -> tuple[str | None, str | None, str | None]:
        if package is None:
            if facility.package_id is None:
                return None, None, REASON_ASSET_NO_PACKAGE
            return None, None, REASON_ASSET_UNKNOWN_PACKAGE
        region = package.region_id or None
        project = package.project_id or None
        if region is None:
            return None, project, REASON_ASSET_PACKAGE_NO_REGION
        return region, project, None

    def _facility_out_of_period_reason(self, facility: Facility, package: Package | None) -> str:
        if facility.status == "rejected":
            return REASON_ACCEPTANCE_REJECTED
        if facility.status == "accepted" and facility.accepted_at is None:
            return REASON_ACCEPTANCE_NO_DATE
        if facility.status == "accepted":
            return "outside_period"
        if package is None:
            return REASON_ASSET_NO_PACKAGE if facility.package_id is None else REASON_ASSET_UNKNOWN_PACKAGE
        if not package.project_id:
            return REASON_ASSET_PACKAGE_NO_PROJECT
        return REASON_ACCEPTANCE_PENDING

    # ------------------------------------------------------------------
    # 输出

    def _payload(self) -> dict[str, Any]:
        by_region = []
        regions = sorted(
            {region for per_currency in self.region_amounts.values() for region in per_currency}
            | set(self.region_asset_counts)
            | set(self.region_completed_packages)
        )
        for region in regions:
            per_project: dict[str, dict[str, Any]] = {}
            for currency in sorted(self.region_amounts):
                for project, value in self.region_amounts[currency].get(region, {}).items():
                    entry = per_project.setdefault(project, {"allocated_by_currency": {}})
                    entry["allocated_by_currency"][currency] = money(value)
            for project, count in self.region_asset_counts.get(region, {}).items():
                per_project.setdefault(project, {"allocated_by_currency": {}})["accepted_facilities"] = count
            for project, count in self.region_completed_packages.get(region, {}).items():
                per_project.setdefault(project, {"allocated_by_currency": {}})["completed_packages"] = count
            for project, entry in per_project.items():
                entry["assets"] = self.region_assets.get(region, {}).get(project, [])
                entry["packages"] = self.region_packages.get(region, {}).get(project, [])
            by_region.append({
                "region": region,
                "allocated_by_currency": {
                    currency: _money_bucket(self.region_amounts[currency].get(region, {}))
                    for currency in sorted(self.region_amounts)
                },
                "accepted_facilities": sum(self.region_asset_counts.get(region, {}).values()),
                "completed_packages": sum(self.region_completed_packages.get(region, {}).values()),
                "by_project": dict(sorted(per_project.items())),
            })

        totals_by_currency: dict[str, dict[str, str]] = {}
        for currency, buckets in sorted(self.currency_totals.items()):
            totals_by_currency[currency] = {
                "confirmed_payments": money(buckets.get("confirmed_payments", ZERO)),
                "allocated": money(buckets.get("allocated", ZERO)),
                "unallocated": money(buckets.get("unallocated", ZERO)),
                "allocation_overrun": money(buckets.get("allocation_overrun", ZERO)),
            }

        by_contract = []
        for breakdown in self.contracts:
            if breakdown["confirmed_in_period"] <= 0:
                continue
            by_contract.append({
                "contract_id": breakdown["contract_id"],
                "currency": breakdown["currency"],
                "confirmed_in_period": money(breakdown["confirmed_in_period"]),
                "confirmed_total": money(breakdown["confirmed_total"]),
                "allocated_in_period": money(breakdown["allocated_in_period"]),
                "unallocated_in_period": money(breakdown["unallocated_in_period"]),
                "allocation_overrun": money(breakdown["allocation_overrun"]),
                "unallocated_reasons": _money_bucket(breakdown["reasons"]),
                "invalid_evidence_amount": _money_bucket(breakdown["invalid_evidence"]),
                "mappings_in_period": [e.as_mapping() for e in breakdown["effective_in_period"]],
            })

        by_project = [
            {"project": project, "currency": currency, "allocated": money(value)}
            for currency, projects in sorted(self.project_amounts.items())
            for project, value in sorted(projects.items())
        ]

        ordered_mappings = [
            evaluation.as_mapping()
            for contract_id in sorted(self.allocation_cache)
            for evaluation in self.allocation_cache[contract_id]
        ]
        ordered_mappings.sort(key=lambda item: item["allocation_id"])

        return {
            "report": {
                "period": self.period.as_dict(),
                "as_of": self.as_of.isoformat() if self.as_of else None,
                "mode": self.mode,
                "data_hash": self.data_hash,
            },
            "confirmed_investment": {
                "by_currency": totals_by_currency,
                "by_contract": by_contract,
                "payments": sorted(
                    self.payment_details, key=lambda item: (item["contract_id"], item["payment_id"])),
            },
            "delivered_facilities": {
                "accepted_in_period": sum(
                    sum(counts.values()) for counts in self.region_asset_counts.values()),
                "completed_packages_in_period": len(self.package_completed_in_period),
                "by_region": by_region,
                "unassigned": {
                    scope: dict(sorted(reasons.items()))
                    for scope, reasons in sorted(self.unassigned_assets.items())
                },
            },
            "facility_acceptance_states": dict(sorted(self.asset_status_counts.items())),
            "package_completion_states": dict(sorted(self.package_states.items())),
            "payment_confirmation_states": dict(sorted(self.payment_states.items())),
            "attribution": {
                "mappings": ordered_mappings,
                "by_project": by_project,
                "unassigned_amount_by_currency": {
                    currency: _money_bucket(reasons)
                    for currency, reasons in sorted(self.unallocated_by_contract.items())
                },
                "unassigned_assets": {
                    scope: dict(sorted(reasons.items()))
                    for scope, reasons in sorted(self.unassigned_assets.items())
                },
            },
            "issues": self.warehouse.issues(),
        }

    def render_text(self) -> str:
        payload = self.payload
        lines: list[str] = []
        period = payload["report"]["period"]
        mode_label = "已确认（按确认日期）" if self.mode == MODE_CONFIRMED else "当时已知（as-of）"
        lines.append(f"报告期: {period['label']}  [{mode_label}]")
        if payload["report"]["as_of"]:
            lines.append(f"时点: {payload['report']['as_of']}")
        lines.append("")
        lines.append("已确认投入（按合同付款，金额不随设施数量放大）")
        if not payload["confirmed_investment"]["by_currency"]:
            lines.append("  本期无已确认付款。")
        for currency, totals in payload["confirmed_investment"]["by_currency"].items():
            extra = ""
            if Decimal(totals["allocation_overrun"]) > 0:
                extra = f" | 分摊超额 {totals['allocation_overrun']}（未裁剪）"
            lines.append(
                f"  {currency}: 已确认 {totals['confirmed_payments']} | "
                f"已归属 {totals['allocated']} | 未分配 {totals['unallocated']}{extra}"
            )
        lines.append("")
        lines.append("已验收设施（按资产登记，独立于付款与完工状态）")
        delivered = payload["delivered_facilities"]
        lines.append(f"  本期已验收设施 {delivered['accepted_in_period']} 项；"
                     f"本期完工工程包 {delivered['completed_packages_in_period']} 个。")
        for region in delivered["by_region"]:
            amounts = ", ".join(
                f"{currency} {entry['allocated']}"
                for currency, entry in region["allocated_by_currency"].items()
                if Decimal(entry.get("allocated", "0")) > 0
            )
            lines.append(
                f"  - {region['region']}: 设施 {region['accepted_facilities']} 项，"
                f"完工工程包 {region['completed_packages']} 个"
                + (f"，已归属投入 {amounts}" if amounts else "")
            )
        for scope, reasons in delivered["unassigned"].items():
            lines.append("  未归属设施[" + scope + "]: "
                         + ", ".join(f"{key}={value}" for key, value in reasons.items()))
        lines.append("")
        lines.append("状态口径（三者互不替代）")
        lines.append("  付款确认: " + (", ".join(
            f"{k}={v}" for k, v in payload["payment_confirmation_states"].items()) or "无"))
        lines.append("  工程完工: " + (", ".join(
            f"{k}={v}" for k, v in payload["package_completion_states"].items()) or "无"))
        lines.append("  资产验收: " + (", ".join(
            f"{k}={v}" for k, v in payload["facility_acceptance_states"].items()) or "无"))
        lines.append("")
        reasons = payload["attribution"]["unassigned_amount_by_currency"]
        if reasons:
            lines.append("未分配金额原因")
            for currency, values in reasons.items():
                for code, value in values.items():
                    if Decimal(value) != 0:
                        lines.append(f"  {currency} {code}: {value}")
            lines.append("")
        issues = payload["issues"]
        if issues:
            lines.append("数据质量问题")
            for issue in issues:
                lines.append(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
        return "\n".join(lines).rstrip() + "\n"


def build_report(
    warehouse: Warehouse,
    period: Period,
    as_of: date | None = None,
    mode: str = MODE_CONFIRMED,
) -> Report:
    if mode not in {MODE_CONFIRMED, MODE_AS_KNOWN}:
        raise ValueError(f"不支持的报表模式: {mode}")
    return Report(period=period, as_of=as_of, mode=mode, warehouse=warehouse,
                  data_hash=warehouse.data_hash())


def _hash_payload(payload: dict[str, Any]) -> str:
    from .model import _hash

    return _hash(payload)


def _explain_contract(breakdown: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_id": breakdown["contract_id"],
        "currency": breakdown["currency"],
        "confirmed_total": money(breakdown["confirmed_total"]),
        "confirmed_in_period": money(breakdown["confirmed_in_period"]),
        "allocated_in_period": money(breakdown["allocated_in_period"]),
        "unallocated_in_period": money(breakdown["unallocated_in_period"]),
        "unallocated_reasons": _money_bucket(breakdown["reasons"]),
        "invalid_evidence_amount": _money_bucket(breakdown["invalid_evidence"]),
        "allocation_overrun": money(breakdown["allocation_overrun"]),
        "evaluations": [e.as_mapping() for e in breakdown["evaluations"]],
    }


def explain(
    warehouse: Warehouse,
    period: Period,
    payment_id: str | None = None,
    contract_id: str | None = None,
    asset_id: str | None = None,
    package_id: str | None = None,
) -> dict[str, Any]:
    """解释某笔金额或设施为何（未）归属，以及各状态来源。"""

    if not any((payment_id, contract_id, asset_id, package_id)):
        raise ValueError("请至少指定 payment、contract、asset 或 package 之一")
    report = build_report(warehouse, period)
    result: dict[str, Any] = {
        "period": period.as_dict(),
        "query": {"payment_id": payment_id, "contract_id": contract_id,
                  "asset_id": asset_id, "package_id": package_id},
    }
    if contract_id:
        result["contract"] = _explain_contract(report.contract_breakdown(contract_id))
    if payment_id:
        payment = next((p for p in warehouse.payments() if payment_id in
                        {p.payment_id, p.fact_id, p.fact_id.split(":", 1)[-1]}), None)
        if payment is None:
            result["payment"] = {"payment_id": payment_id, "found": False}
        else:
            in_period = report._payment_in_scope(payment)
            result["payment"] = {
                "payment_id": payment.payment_id,
                "found": True,
                "contract_id": payment.contract_id,
                "amount": money(payment.amount),
                "currency": payment.currency,
                "status": payment.status,
                "confirmed_at": payment.confirmed_at.isoformat() if payment.confirmed_at else None,
                "in_period": in_period,
                "reason": None if in_period else report._out_of_period_note(payment),
                "contract": _explain_contract(report.contract_breakdown(payment.contract_id))
                if in_period else None,
                "note": "付款本身不携带区域；区域归属只来自带业务依据的分摊记录。",
            }
    if asset_id:
        facility = next((f for f in warehouse.facilities() if asset_id in
                         {f.asset_id, f.fact_id, f.fact_id.split(":", 1)[-1]}), None)
        if facility is None:
            result["asset"] = {"asset_id": asset_id, "found": False}
        else:
            package = report.packages.get(facility.package_id) if facility.package_id else None
            region, project, reason = report._facility_scope(facility, package)
            accepted_in_period = (
                facility.status == "accepted" and period.contains(facility.accepted_at)
            )
            result["asset"] = {
                "asset_id": facility.asset_id,
                "found": True,
                "package_id": facility.package_id,
                "package": None if package is None else {
                    "project_id": package.project_id,
                    "region_id": package.region_id,
                    "status": package.status,
                    "completed_at": package.completed_at.isoformat() if package.completed_at else None,
                    "completion_status": package.completion_status,
                },
                "acceptance_status": facility.acceptance_status,
                "accepted_at": facility.accepted_at.isoformat() if facility.accepted_at else None,
                "accepted_in_period": accepted_in_period,
                "project_id": project,
                "region_id": region,
                "reason": reason if not accepted_in_period or region is None else None,
                "status_reason": None if accepted_in_period else report._facility_out_of_period_reason(
                    facility, package),
                "package_completion_note": (
                    None if package is not None and package.status == "completed"
                    else REASON_PACKAGE_NOT_COMPLETED
                ),
                "note": "设施验收状态独立于工程包完工与付款确认，不被任何一方自动替代。",
            }
    if package_id:
        package = report.packages.get(package_id)
        if package is None:
            result["package"] = {"package_id": package_id, "found": False}
        else:
            facilities = [f for f in warehouse.facilities() if f.package_id == package_id]
            completed_in_period = (
                package.status == "completed" and period.contains(package.completed_at)
            )
            result["package"] = {
                "package_id": package.package_id,
                "found": True,
                "project_id": package.project_id,
                "region_id": package.region_id,
                "status": package.status,
                "completed_at": package.completed_at.isoformat() if package.completed_at else None,
                "completed_in_period": completed_in_period,
                "facility_count": len(facilities),
                "accepted_facility_count": sum(
                    1 for f in facilities
                    if f.status == "accepted" and period.contains(f.accepted_at)
                ),
                "acceptance_note": "设施验收状态独立于工程包完工状态，不因完工自动视为已验收。",
            }
    return result
