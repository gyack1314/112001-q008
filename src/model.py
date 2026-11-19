"""领域模型、来源记录解析和报告期工具。

本模块刻意不把付款、工程包和设施拼成一张宽表。三类来源记录分别落在
:class:`Payment`、:class:`Package`、:class:`Facility` 中，金额与设施数量
只在报告层按键聚合，避免“一个工程包多个设施”导致付款金额重复计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Any


CENT = Decimal("0.01")
ZERO = Decimal("0")
FACT_KINDS = {"payment", "package", "asset_acceptance", "allocation"}


class FactError(ValueError):
    """来源记录不满足契约。"""


class PeriodError(ValueError):
    """报告期表达式无法识别。"""


# ---------------------------------------------------------------------------
# 金额、日期和状态规范化


def parse_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise FactError(f"{field_name} 不能是布尔值")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        raw = value.strip().replace(",", "").replace("，", "")
        if not raw:
            raise FactError(f"{field_name} 不能为空")
        try:
            result = Decimal(raw)
        except InvalidOperation as exc:
            raise FactError(f"{field_name} 不是有效金额: {value!r}") from exc
    else:
        raise FactError(f"{field_name} 不是有效金额: {value!r}")
    if not result.is_finite():
        raise FactError(f"{field_name} 必须是有限金额")
    return result


def parse_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise FactError(f"{field_name} 不是有效日期: {value!r}")
    raw = value.strip()
    # 允许 ISO 日期时间，但报告按日期粒度。
    raw = raw[:10].replace("/", "-")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise FactError(f"{field_name} 不是 ISO 日期: {value!r}") from exc


def optional_date(record: dict[str, Any], *keys: str) -> date | None:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return parse_date(value, key)
    return None


def optional_text(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            raise FactError(f"{keys[0]} 必须是文本")
        text = str(value).strip()
        if text:
            return text
    return None


def required_text(record: dict[str, Any], *keys: str) -> str:
    value = optional_text(record, *keys)
    if value is None:
        raise FactError(f"缺少必填字段 {keys[0]}")
    return value


_STATUS_ALIASES = {
    "confirmed": "confirmed",
    "confirm": "confirmed",
    "已确认": "confirmed",
    "确认": "confirmed",
    "pending": "pending",
    "unconfirmed": "pending",
    "待确认": "pending",
    "未确认": "pending",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "已取消": "cancelled",
    "reversed": "reversed",
    "已冲销": "reversed",
    "completed": "completed",
    "complete": "completed",
    "完工": "completed",
    "已完工": "completed",
    "in_progress": "in_progress",
    "ongoing": "in_progress",
    "在建": "in_progress",
    "未完工": "in_progress",
    "not_started": "not_started",
    "未开工": "not_started",
    "accepted": "accepted",
    "acceptance": "accepted",
    "已验收": "accepted",
    "验收": "accepted",
    "rejected": "rejected",
    "不合格": "rejected",
    "未通过": "rejected",
    "待验收": "pending",
}


def normalize_status(value: Any, field_name: str = "status") -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise FactError(f"{field_name} 必须是文本")
    normalized = value.strip().lower()
    result = _STATUS_ALIASES.get(normalized, _STATUS_ALIASES.get(value.strip()))
    if result is None:
        raise FactError(f"{field_name} 不支持: {value!r}")
    return result


# ---------------------------------------------------------------------------
# 三类粒度事实和显式分摊证据


@dataclass(frozen=True)
class Payment:
    """合同付款事实。金额粒度是付款记录，不是设施。"""

    payment_id: str
    contract_id: str
    amount: Decimal
    currency: str
    status: str
    confirmed_at: date | None
    declared_project_id: str | None
    declared_region_id: str | None
    source_line: int
    fact_id: str

    @property
    def confirmed_status(self) -> str:
        if self.status != "confirmed":
            return self.status
        if self.confirmed_at is None:
            return "confirmed_missing_date"
        return "confirmed"


@dataclass(frozen=True)
class Package:
    """施工台账中的工程包事实。完工状态只描述施工台账。"""

    package_id: str
    project_id: str | None
    region_id: str | None
    status: str
    completed_at: date | None
    source_line: int
    fact_id: str

    @property
    def completion_status(self) -> str:
        if self.status != "completed":
            return self.status
        if self.completed_at is None:
            return "completed_missing_date"
        return "completed"


@dataclass(frozen=True)
class Facility:
    """资产系统中的设施验收事实。验收状态只描述资产验收。"""

    asset_id: str
    package_id: str | None
    status: str
    accepted_at: date | None
    source_line: int
    fact_id: str

    @property
    def acceptance_status(self) -> str:
        if self.status != "accepted":
            return self.status
        if self.accepted_at is None:
            return "accepted_missing_date"
        return "accepted"


@dataclass(frozen=True)
class Allocation:
    """合同金额到工程包/项目/区域的显式业务归属证据。

    ``evidence`` 是必须存在的业务依据文本（协议、审批单、分摊说明等）。
    ``amount`` 与 ``ratio`` 二选一；比例按合同已确认付款总额折算。
    """

    allocation_id: str
    contract_id: str
    payment_id: str | None
    package_id: str | None
    project_id: str | None
    region_id: str | None
    amount: Decimal | None
    ratio: Decimal | None
    evidence: str
    effective_at: date | None
    source_line: int
    fact_id: str


ParsedFact = Payment | Package | Facility | Allocation


@dataclass(frozen=True)
class ParsedRecord:
    fact_id: str
    kind: str
    fact: ParsedFact
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _canonical_fact_id(kind: str, record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{kind}:auto:{digest}"


def _fact_id(kind: str, explicit: str | None, record: dict[str, Any]) -> str:
    if explicit:
        return f"{kind}:{explicit}"
    return _canonical_fact_id(kind, record)


def parse_record(record: Any, ordinal: int = 0) -> ParsedRecord:
    """把一条 JSON 记录解析成对应粒度的领域对象。

    ``ordinal`` 仅用于错误信息，不参与事实标识生成。
    """

    if not isinstance(record, dict):
        raise FactError(f"第 {ordinal} 条记录必须是 JSON 对象")
    raw_kind = record.get("fact_kind")
    if not isinstance(raw_kind, str) or not raw_kind.strip():
        raise FactError(f"第 {ordinal} 条记录缺少 fact_kind")
    kind = raw_kind.strip()
    aliases = {"asset": "asset_acceptance", "facility": "asset_acceptance",
               "contract_allocation": "allocation", "package_fact": "package"}
    kind = aliases.get(kind, kind)
    if kind not in FACT_KINDS:
        raise FactError(f"第 {ordinal} 条记录 fact_kind 不支持: {raw_kind!r}")

    warnings: list[str] = []
    try:
        if kind == "payment":
            fact: ParsedFact = _parse_payment(record, ordinal, warnings)
        elif kind == "package":
            fact = _parse_package(record, ordinal, warnings)
        elif kind == "asset_acceptance":
            fact = _parse_facility(record, ordinal, warnings)
        else:
            fact = _parse_allocation(record, ordinal, warnings)
    except FactError as exc:
        raise FactError(f"第 {ordinal} 条 {kind} 记录无效: {exc}") from exc

    explicit = {
        "payment": optional_text(record, "payment_id", "payment"),
        "package": optional_text(record, "package_id", "package"),
        "asset_acceptance": optional_text(record, "asset_id", "asset"),
        "allocation": optional_text(record, "allocation_id", "allocation"),
    }[kind]
    return ParsedRecord(
        fact_id=_fact_id(kind, explicit, record),
        kind=kind,
        fact=fact,
        warnings=tuple(warnings),
    )


def _parse_payment(
    record: dict[str, Any], ordinal: int, warnings: list[str]
) -> Payment:
    # 付款来源可以不带付款编号；此时用记录内容的稳定摘要作为标识。
    # 若金额等字段被修订，摘要会变化，视为一笔新事实而不是原记录改版。
    payment_id = optional_text(record, "payment_id", "payment")
    if payment_id is None:
        payment_id = "auto:" + _hash(record)[:16]
        warnings.append("付款记录缺少 payment_id，已按记录内容生成稳定标识；修订内容会生成新付款。")
    contract_id = required_text(record, "contract_id", "contract")
    amount = parse_decimal(record.get("amount"), "amount")
    currency = optional_text(record, "currency") or "CNY"
    confirmed_at = optional_date(record, "confirmed_at", "confirmed_date")
    status = normalize_status(record.get("status"))
    if status is None:
        status = "confirmed" if confirmed_at is not None else "pending"
    if status == "confirmed" and confirmed_at is None:
        warnings.append("付款状态为 confirmed 但缺少 confirmed_at，不能计入任何报告期。")
    if status != "confirmed" and confirmed_at is not None:
        warnings.append("付款带 confirmed_at 但状态不是 confirmed，仍按状态视为未确认。")
    project_id = optional_text(record, "project_id", "project")
    region_id = optional_text(record, "region_id", "region")
    return Payment(
        payment_id=payment_id,
        contract_id=contract_id,
        amount=amount,
        currency=currency,
        status=status,
        confirmed_at=confirmed_at,
        declared_project_id=project_id,
        declared_region_id=region_id,
        source_line=ordinal,
        fact_id=_fact_id("payment", None if payment_id.startswith("auto:") else payment_id, record),
    )


def _parse_package(
    record: dict[str, Any], ordinal: int, warnings: list[str]
) -> Package:
    package_id = required_text(record, "package_id", "package")
    completed_at = optional_date(record, "completed_at", "completion_date")
    status = normalize_status(record.get("status"))
    if status is None:
        status = "completed" if completed_at is not None else "in_progress"
    if status == "completed" and completed_at is None:
        warnings.append("工程包状态为 completed 但缺少 completed_at，不能计入完工报告期。")
    if status != "completed" and completed_at is not None:
        warnings.append("工程包带 completed_at 但状态不是 completed，仍按状态视为未完工。")
    return Package(
        package_id=package_id,
        project_id=optional_text(record, "project_id", "project"),
        region_id=optional_text(record, "region_id", "region"),
        status=status,
        completed_at=completed_at,
        source_line=ordinal,
        fact_id=_fact_id("package", package_id, record),
    )


def _parse_facility(
    record: dict[str, Any], ordinal: int, warnings: list[str]
) -> Facility:
    asset_id = required_text(record, "asset_id", "asset")
    accepted_at = optional_date(record, "accepted_at", "acceptance_date")
    status = normalize_status(record.get("status"))
    if status is None:
        status = "accepted" if accepted_at is not None else "pending"
    if status == "accepted" and accepted_at is None:
        warnings.append("设施状态为 accepted 但缺少 accepted_at，不能计入交付报告期。")
    if status != "accepted" and accepted_at is not None:
        warnings.append("设施带 accepted_at 但状态不是 accepted，仍按状态视为未验收。")
    return Facility(
        asset_id=asset_id,
        package_id=optional_text(record, "package_id", "package"),
        status=status,
        accepted_at=accepted_at,
        source_line=ordinal,
        fact_id=_fact_id("asset_acceptance", asset_id, record),
    )


def _parse_allocation(
    record: dict[str, Any], ordinal: int, warnings: list[str]
) -> Allocation:
    allocation_id = required_text(record, "allocation_id", "allocation")
    contract_id = required_text(record, "contract_id", "contract")
    evidence = optional_text(record, "evidence", "basis", "evidence_note")
    if evidence is None:
        raise FactError("分摊记录必须提供 evidence/basis 业务依据，不能无依据分摊")
    amount_raw = record.get("amount")
    amount = None if amount_raw is None or amount_raw == "" else parse_decimal(amount_raw, "amount")
    ratio_raw = record.get("ratio")
    if ratio_raw is None or ratio_raw == "":
        percentage = record.get("percentage")
        if percentage is not None and percentage != "":
            ratio = parse_decimal(percentage, "percentage") / Decimal(100)
        else:
            ratio = None
    else:
        ratio = parse_decimal(ratio_raw, "ratio")
    if amount is not None and ratio is not None:
        raise FactError("分摊记录 amount 与 ratio 只能填写一个")
    if amount is None and ratio is None:
        raise FactError("分摊记录必须提供 amount 或 ratio")
    if amount is not None and amount < 0:
        raise FactError("分摊金额不能为负")
    if ratio is not None and (ratio <= 0 or ratio > 1):
        raise FactError("分摊比例必须大于 0 且不超过 1")
    package_id = optional_text(record, "package_id", "package")
    project_id = optional_text(record, "project_id", "project")
    region_id = optional_text(record, "region_id", "region")
    if not any((package_id, project_id, region_id)):
        raise FactError("分摊记录必须指向 package、project 或 region 中至少一个")
    effective_at = optional_date(record, "effective_at", "effective_date")
    if effective_at is None:
        warnings.append("分摊记录缺少 effective_at；历史发布将把它视为长期有效证据。")
    return Allocation(
        allocation_id=allocation_id,
        contract_id=contract_id,
        payment_id=optional_text(record, "payment_id"),
        package_id=package_id,
        project_id=project_id,
        region_id=region_id,
        amount=amount,
        ratio=ratio,
        evidence=evidence,
        effective_at=effective_at,
        source_line=ordinal,
        fact_id=_fact_id("allocation", allocation_id, record),
    )


# ---------------------------------------------------------------------------
# 报告期


@dataclass(frozen=True)
class Period:
    label: str
    kind: str
    start: date | None
    end: date | None

    def contains(self, value: date | None) -> bool:
        if value is None:
            return False
        if self.start is not None and value < self.start:
            return False
        if self.end is not None and value > self.end:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
        }


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def parse_period(value: str | None) -> Period:
    """解析 ``all``、``2026``、``2026-Q2``、``2026-05`` 或日期区间。"""

    if value is None:
        return Period(label="all", kind="all", start=None, end=None)
    text = str(value).strip()
    if not text or text.lower() == "all":
        return Period(label="all", kind="all", start=None, end=None)

    match = re.fullmatch(r"(\d{4})", text)
    if match:
        year = int(match.group(1))
        return Period(label=f"{year}", kind="year", start=date(year, 1, 1), end=date(year, 12, 31))

    match = re.fullmatch(r"(\d{4})[-_ ]?[Qq]([1-4])", text)
    if match:
        year, quarter = int(match.group(1)), int(match.group(2))
        start_month = 3 * (quarter - 1) + 1
        end_month = start_month + 2
        return Period(
            label=f"{year}-Q{quarter}",
            kind="quarter",
            start=date(year, start_month, 1),
            end=_month_end(year, end_month),
        )

    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise PeriodError(f"报告期月份无效: {value!r}")
        return Period(
            label=f"{year}-{month:02d}",
            kind="month",
            start=date(year, month, 1),
            end=_month_end(year, month),
        )

    separator = ".." if ".." in text else (":" if ":" in text else None)
    if separator:
        left, right = text.split(separator, 1)
        try:
            start = parse_date(left, "period.start")
            end = parse_date(right, "period.end")
        except FactError as exc:
            raise PeriodError(str(exc)) from exc
        if start > end:
            raise PeriodError("报告期开始日期不能晚于结束日期")
        return Period(label=text, kind="range", start=start, end=end)

    try:
        single = parse_date(text, "period")
    except FactError as exc:
        raise PeriodError(
            f"无法识别报告期 {value!r}；支持 all、YYYY、YYYY-Qn、YYYY-MM、YYYY-MM-DD 或 start..end"
        ) from exc
    return Period(label=single.isoformat(), kind="date", start=single, end=single)


# ---------------------------------------------------------------------------
# 输出格式


def money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
