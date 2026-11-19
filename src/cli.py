"""命令行入口：装载、报表、解释与发布。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

from .model import FactError, PeriodError, parse_date, parse_period
from .publication import diff_publication, publish
from .report import MODE_AS_KNOWN, MODE_CONFIRMED, build_report, explain
from .warehouse import Warehouse


DEFAULT_DB = "warehouse.sqlite3"


def _read_records(path: str) -> list[Any]:
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if isinstance(data, dict) and "records" in data:
        data = data["records"]
    if not isinstance(data, list):
        raise FactError("输入文件必须是 JSON 数组，或含 records 数组的对象")
    return data


def _warehouse(args: argparse.Namespace) -> Warehouse:
    return Warehouse(args.db)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def cmd_load(args: argparse.Namespace) -> int:
    records = _read_records(args.input)
    with _warehouse(args) as warehouse:
        result = warehouse.load_records(records)
    _print_json({
        "data_hash": result["data_hash"],
        "records_loaded": result["records_loaded"],
        "records_rejected": result["records_rejected"],
        "issues": result["issues"],
    })
    return 0 if result["records_rejected"] == 0 else 2


def cmd_report(args: argparse.Namespace) -> int:
    period = parse_period(args.period)
    as_of = parse_date(args.as_of, "as_of") if args.as_of else None
    with _warehouse(args) as warehouse:
        report = build_report(warehouse, period, as_of=as_of, mode=args.mode)
        if args.format == "text":
            print(report.render_text(), end="")
        else:
            _print_json(report.payload)
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    period = parse_period(args.period)
    with _warehouse(args) as warehouse:
        result = explain(
            warehouse,
            period,
            payment_id=args.payment,
            contract_id=args.contract,
            asset_id=args.asset,
            package_id=args.package,
        )
    _print_json(result)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    period = parse_period(args.period)
    as_of = parse_date(args.as_of, "as_of") if args.as_of else None
    with _warehouse(args) as warehouse:
        report = build_report(warehouse, period, as_of=as_of, mode=args.mode)
        published_at = None
        if args.published_at:
            published_at = datetime.fromisoformat(args.published_at)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        result = publish(warehouse, report, published_at=published_at)
    _print_json(result)
    return 0


def cmd_publications(args: argparse.Namespace) -> int:
    with _warehouse(args) as warehouse:
        if args.diff:
            _print_json(diff_publication(warehouse, args.diff))
        else:
            _print_json({"publications": warehouse.list_publications()})
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with _warehouse(args) as warehouse:
        published = warehouse.get_publication(args.publication)
    if published is None:
        print(f"发布不存在: {args.publication}", file=sys.stderr)
        return 1
    _print_json(published)
    return 0


def cmd_issues(args: argparse.Namespace) -> int:
    with _warehouse(args) as warehouse:
        _print_json({"issues": warehouse.issues()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-warehouse",
        description="工程投入与设施交付数仓（付款/工程包/设施分粒度事实）",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 文件路径（默认 {DEFAULT_DB}）")
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="装载三类来源记录（替换当前事实，保留发布）")
    load.add_argument("--input", required=True, help="facts.json 路径")
    load.set_defaults(func=cmd_load)

    report = sub.add_parser("report", help="按报告期输出已确认投入与已验收设施")
    report.add_argument("--period", default="all", help="all、2026、2026-Q2、2026-05、2026-05-01..2026-06-30")
    report.add_argument("--format", choices=("json", "text"), default="text")
    report.add_argument("--mode", choices=(MODE_CONFIRMED, MODE_AS_KNOWN), default=MODE_CONFIRMED,
                        help="confirmed=按确认日期落报告期；as-known=只要已确认即计入")
    report.add_argument("--as-of", help="按该日期当时可用的归属证据复算（晚于该日的分摊不计）")
    report.set_defaults(func=cmd_report)

    explain_cmd = sub.add_parser("explain", help="解释金额或设施未归属/未计入的原因")
    explain_cmd.add_argument("--period", default="all")
    explain_cmd.add_argument("--payment")
    explain_cmd.add_argument("--contract")
    explain_cmd.add_argument("--asset")
    explain_cmd.add_argument("--package")
    explain_cmd.set_defaults(func=cmd_explain)

    publisher = sub.add_parser("publish", help="冻结当前报告期为不可变发布")
    publisher.add_argument("--period", default="all")
    publisher.add_argument("--mode", choices=(MODE_CONFIRMED, MODE_AS_KNOWN), default=MODE_CONFIRMED)
    publisher.add_argument("--published-at", help="ISO 时间；默认当前 UTC 时间")
    publisher.add_argument("--as-of", help="按该日期当时可用的归属证据冻结结果")
    publisher.set_defaults(func=cmd_publish)

    publications = sub.add_parser("publications", help="列出发布，或与当前视图比较")
    publications.add_argument("--diff", help="发布 ID：比较该发布与当前视图")
    publications.set_defaults(func=cmd_publications)

    show = sub.add_parser("show", help="查看某份发布的冻结内容")
    show.add_argument("publication")
    show.set_defaults(func=cmd_show)

    issues = sub.add_parser("issues", help="列出来源与关联质量问题")
    issues.set_defaults(func=cmd_issues)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FactError, PeriodError) as exc:
        print(f"输入无效: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取输入失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
