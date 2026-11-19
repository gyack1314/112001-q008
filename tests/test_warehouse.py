import json
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import Warehouse, build_report, diff_publication, explain, parse_period, publish
from src.model import FactError, PeriodError
from src.report import MODE_AS_KNOWN

RECORDS = [
    # 合同 C-1 支付 500,000，跨两个区域；C-2 支付 300,000 没有分摊证据。
    {"fact_kind": "payment", "payment_id": "P-1", "contract_id": "C-1",
     "amount": 500000, "currency": "CNY", "confirmed_at": "2026-03-10"},
    {"fact_kind": "payment", "payment_id": "P-2", "contract_id": "C-1",
     "amount": 100000, "currency": "CNY", "confirmed_at": "2026-08-01"},
    {"fact_kind": "payment", "payment_id": "P-3", "contract_id": "C-2",
     "amount": 300000, "currency": "CNY", "confirmed_at": "2026-05-01"},
    {"fact_kind": "payment", "payment_id": "P-4", "contract_id": "C-3",
     "amount": 90000, "currency": "CNY", "status": "pending"},
    # 显式分摊：C-1 按 60/40 分给 north 的 PK-1 与 south 的 PK-2。
    {"fact_kind": "allocation", "allocation_id": "A-1", "contract_id": "C-1",
     "package_id": "PK-1", "ratio": 0.6, "evidence": "联合采购协议 2026-01 第 4 条",
     "effective_at": "2026-02-01"},
    {"fact_kind": "allocation", "allocation_id": "A-2", "contract_id": "C-1",
     "package_id": "PK-2", "ratio": 0.4, "evidence": "联合采购协议 2026-01 第 4 条",
     "effective_at": "2026-02-01"},
    # 一个工程包多个设施：PK-1 有 3 项设施，PK-2 有 1 项。
    {"fact_kind": "package", "package_id": "PK-1", "project_id": "PR-1",
     "region_id": "north", "status": "completed", "completed_at": "2026-04-20"},
    {"fact_kind": "package", "package_id": "PK-2", "project_id": "PR-2",
     "region_id": "south", "status": "in_progress"},
    {"fact_kind": "asset_acceptance", "asset_id": "AS-1", "package_id": "PK-1",
     "status": "accepted", "accepted_at": "2026-04-30"},
    {"fact_kind": "asset_acceptance", "asset_id": "AS-2", "package_id": "PK-1",
     "status": "accepted", "accepted_at": "2026-05-05"},
    {"fact_kind": "asset_acceptance", "asset_id": "AS-3", "package_id": "PK-1",
     "status": "pending"},
    # PK-2 未完工，但其中一项设施已通过资产验收：两者互不替代。
    {"fact_kind": "asset_acceptance", "asset_id": "AS-4", "package_id": "PK-2",
     "status": "accepted", "accepted_at": "2026-06-01"},
    {"fact_kind": "asset_acceptance", "asset_id": "AS-5", "package_id": "PK-2",
     "status": "pending"},
]


class WarehouseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "warehouse.sqlite3")
        self.warehouse = Warehouse(self.db)
        self.addCleanup(self.warehouse.close)
        self.load_result = self.warehouse.load_records(RECORDS)

    def report(self, period="2026", **kwargs):
        return build_report(self.warehouse, parse_period(period), **kwargs)


class IngestionTest(WarehouseTestCase):
    def test_fact_kinds_are_stored_at_source_grain(self):
        counts = self.warehouse.table_counts()
        self.assertEqual(counts["payments"], 4)
        self.assertEqual(counts["packages"], 2)
        self.assertEqual(counts["facilities"], 5)
        self.assertEqual(counts["allocations"], 2)

    def test_payment_amount_is_not_multiplied_by_facilities(self):
        report = self.report()
        totals = report.payload["confirmed_investment"]["by_currency"]["CNY"]
        # 2026 年内确认 P-1、P-2、P-3；P-4 仍是 pending。
        self.assertEqual(totals["confirmed_payments"], "900000.00")
        self.assertEqual(totals["allocated"], "600000.00")
        self.assertEqual(totals["unallocated"], "300000.00")

    def test_regions_are_regions_not_currency_codes(self):
        report = self.report()
        names = {region["region"] for region in report.payload["delivered_facilities"]["by_region"]}
        self.assertEqual(names, {"north", "south"})

    def test_unallocated_has_reason_and_is_not_spread_over_facilities(self):
        report = self.report()
        reasons = report.payload["attribution"]["unassigned_amount_by_currency"]["CNY"]
        self.assertEqual(reasons["no_allocation_evidence"], "300000.00")
        north = next(r for r in report.payload["delivered_facilities"]["by_region"]
                     if r["region"] == "north")
        # north 只承接 A-1 对应的合同额（600,000 的 60%），不因 PK-1 有 3 项设施而变大。
        self.assertEqual(north["allocated_by_currency"]["CNY"], {"allocated": "360000.00"})
        self.assertEqual(north["accepted_facilities"], 2)

    def test_ratios_use_contract_confirmed_base_not_period_amount(self):
        contract = self.report("2026-Q1").payload
        # 比例按合同已确认总额 600,000 折算，而不是按本期 500,000 重算比例。
        self.assertEqual(contract["confirmed_investment"]["by_currency"]["CNY"]["confirmed_payments"],
                         "500000.00")
        self.assertEqual(contract["confirmed_investment"]["by_currency"]["CNY"]["allocated"],
                         "500000.00")
        mapping = {m["allocation_id"]: m for m in contract["attribution"]["mappings"]}
        self.assertEqual(mapping["A-1"]["amount"], "360000.00")
        self.assertEqual(mapping["A-1"]["amount_in_period"], "300000.00")
        self.assertEqual(mapping["A-2"]["amount_in_period"], "200000.00")

    def test_source_without_payment_id_gets_stable_identity(self):
        with self.warehouse.transaction() as conn:
            conn.execute("DELETE FROM payments")
        record = {"fact_kind": "payment", "contract_id": "C-9", "amount": 10,
                  "confirmed_at": "2026-01-01"}
        warehouse = Warehouse(os.path.join(self.tmp.name, "auto.sqlite3"))
        self.addCleanup(warehouse.close)
        first = warehouse.load_records([record])
        second = warehouse.load_records([record])
        self.assertEqual(first["data_hash"], second["data_hash"])
        self.assertEqual(len(warehouse.payments()), 1)

    def test_rejects_allocation_without_evidence(self):
        warehouse = Warehouse(os.path.join(self.tmp.name, "bad.sqlite3"))
        self.addCleanup(warehouse.close)
        result = warehouse.load_records([
            {"fact_kind": "payment", "payment_id": "P-1", "contract_id": "C-1",
             "amount": 1, "confirmed_at": "2026-01-01"},
            {"fact_kind": "allocation", "allocation_id": "A-1", "contract_id": "C-1",
             "package_id": "PK-1", "ratio": 1},
            {"fact_kind": "package", "package_id": "PK-1", "region_id": "north"},
        ])
        self.assertEqual(result["records_rejected"], 1)
        self.assertEqual(result["records_loaded"], 2)
        self.assertEqual(warehouse.allocations(), [])
        report = build_report(warehouse, parse_period("2026"))
        self.assertEqual(report.payload["confirmed_investment"]["by_currency"]["CNY"]["unallocated"],
                         "1.00")

    def test_ratio_out_of_range_is_rejected(self):
        warehouse = Warehouse(os.path.join(self.tmp.name, "bad2.sqlite3"))
        self.addCleanup(warehouse.close)
        result = warehouse.load_records([
            {"fact_kind": "payment", "payment_id": "P-1", "contract_id": "C-1",
             "amount": 1, "confirmed_at": "2026-01-01"},
            {"fact_kind": "allocation", "allocation_id": "A-1", "contract_id": "C-1",
             "package_id": "PK-1", "ratio": 1.5, "evidence": "x"},
            {"fact_kind": "package", "package_id": "PK-1", "region_id": "north"},
        ])
        self.assertEqual(result["records_rejected"], 1)
        self.assertEqual(warehouse.allocations(), [])

    def test_over_allocation_is_reported_not_clipped(self):
        extra = RECORDS + [
            {"fact_kind": "allocation", "allocation_id": "A-3", "contract_id": "C-1",
             "project_id": "PR-9", "amount": 500000, "evidence": "补充协议"},
        ]
        warehouse = Warehouse(os.path.join(self.tmp.name, "over.sqlite3"))
        self.addCleanup(warehouse.close)
        warehouse.load_records(extra)
        report = build_report(warehouse, parse_period("2026"))
        totals = report.payload["confirmed_investment"]["by_currency"]["CNY"]
        self.assertEqual(totals["allocation_overrun"], "500000.00")
        codes = {issue["code"] for issue in warehouse.issues()}
        self.assertIn("allocation_exceeds_contract", codes)


class StatusIndependenceTest(WarehouseTestCase):
    def test_completion_acceptance_and_payment_are_separate(self):
        retrieved = explain(self.warehouse, parse_period("2026"), package_id="PK-2")
        package = retrieved["package"]
        self.assertEqual(package["status"], "in_progress")
        self.assertEqual(package["accepted_facility_count"], 1)
        self.assertFalse(package["completed_in_period"])

        asset = explain(self.warehouse, parse_period("2026"), asset_id="AS-4")["asset"]
        self.assertTrue(asset["accepted_in_period"])
        self.assertEqual(asset["region_id"], "south")
        self.assertEqual(asset["package_completion_note"], "package_not_completed")

        report = self.report()
        self.assertEqual(report.payload["payment_confirmation_states"], {"confirmed": 3, "pending": 1})
        self.assertEqual(report.payload["package_completion_states"],
                         {"completed": 1, "in_progress": 1})
        self.assertEqual(report.payload["facility_acceptance_states"],
                         {"accepted": 3, "pending": 2})

    def test_completion_without_acceptance_does_not_create_delivery(self):
        records = [
            {"fact_kind": "package", "package_id": "PK-1", "region_id": "north",
             "status": "completed", "completed_at": "2026-04-20"},
            {"fact_kind": "asset_acceptance", "asset_id": "AS-1", "package_id": "PK-1",
             "status": "pending"},
        ]
        warehouse = Warehouse(os.path.join(self.tmp.name, "noaccept.sqlite3"))
        self.addCleanup(warehouse.close)
        warehouse.load_records(records)
        report = build_report(warehouse, parse_period("2026"))
        self.assertEqual(report.payload["delivered_facilities"]["accepted_in_period"], 0)
        self.assertEqual(report.payload["delivered_facilities"]["completed_packages_in_period"], 1)

    def test_payment_confirmed_without_date_cannot_enter_period(self):
        records = [
            {"fact_kind": "payment", "payment_id": "P-1", "contract_id": "C-1",
             "amount": 100, "status": "confirmed"},
        ]
        warehouse = Warehouse(os.path.join(self.tmp.name, "nodate.sqlite3"))
        self.addCleanup(warehouse.close)
        warehouse.load_records(records)
        report = build_report(warehouse, parse_period("2026"))
        self.assertEqual(report.payload["confirmed_investment"]["by_currency"], {})
        self.assertEqual(report.payment_states["confirmed_missing_date"], 1)
        detail = explain(warehouse, parse_period("2026"), payment_id="P-1")["payment"]
        self.assertEqual(detail["reason"], "payment_confirmed_missing_date")


class PeriodTest(WarehouseTestCase):
    def test_period_filters_payments_independently_of_assets(self):
        quarter = self.report("2026-Q2")
        # P-3 于 5 月确认；P-1 在 3 月（Q1），P-2 在 8 月（Q3）。
        self.assertEqual(quarter.payload["confirmed_investment"]["by_currency"]["CNY"]
                         ["confirmed_payments"], "300000.00")
        # AS-1(4/30)、AS-2(5/5)、AS-4(6/1) 在本季度验收。
        self.assertEqual(quarter.payload["delivered_facilities"]["accepted_in_period"], 3)

    def test_year_and_month_and_range(self):
        self.assertEqual(parse_period("2026-02").end.isoformat(), "2026-02-28")
        self.assertEqual(parse_period("2026-Q3").start.isoformat(), "2026-07-01")
        ranged = parse_period("2026-04-01..2026-04-30")
        self.assertEqual(ranged.label, "2026-04-01..2026-04-30")

    def test_invalid_period_raises(self):
        with self.assertRaises(PeriodError):
            parse_period("2026-13")
        with self.assertRaises(PeriodError):
            parse_period("not-a-period")

    def test_as_known_mode_includes_confirmed_without_date(self):
        records = [
            {"fact_kind": "payment", "payment_id": "P-1", "contract_id": "C-1",
             "amount": 100, "confirmed_at": "2027-01-05"},
        ]
        warehouse = Warehouse(os.path.join(self.tmp.name, "asknown.sqlite3"))
        self.addCleanup(warehouse.close)
        warehouse.load_records(records)
        confirmed = build_report(warehouse, parse_period("2026"))
        as_known = build_report(warehouse, parse_period("2026"), mode=MODE_AS_KNOWN)
        self.assertEqual(confirmed.payload["confirmed_investment"]["by_currency"], {})
        self.assertEqual(as_known.payload["confirmed_investment"]["by_currency"]["CNY"]
                         ["confirmed_payments"], "100.00")


class ExplainTest(WarehouseTestCase):
    def test_contract_explains_evidence_and_remainder(self):
        result = explain(self.warehouse, parse_period("2026"), contract_id="C-1")["contract"]
        self.assertEqual(result["confirmed_total"], "600000.00")
        self.assertEqual(result["confirmed_in_period"], "600000.00")
        by_id = {e["allocation_id"]: e for e in result["evaluations"]}
        self.assertEqual(by_id["A-1"]["amount"], "360000.00")
        self.assertEqual(by_id["A-1"]["evidence"], "联合采购协议 2026-01 第 4 条")

    def test_unallocated_payment_explains_missing_evidence(self):
        result = explain(self.warehouse, parse_period("2026"), payment_id="P-3")["payment"]
        self.assertTrue(result["in_period"])
        self.assertEqual(result["contract"]["unallocated_in_period"], "300000.00")
        self.assertEqual(result["contract"]["unallocated_reasons"],
                         {"no_allocation_evidence": "300000.00"})
        reasons = explain(self.warehouse, parse_period("2026"), contract_id="C-2")
        self.assertEqual(reasons["contract"]["evaluations"], [])

    def test_asset_with_missing_package_explains_reason(self):
        warehouse = Warehouse(os.path.join(self.tmp.name, "orphan.sqlite3"))
        self.addCleanup(warehouse.close)
        warehouse.load_records([
            {"fact_kind": "asset_acceptance", "asset_id": "AS-X", "status": "accepted",
             "accepted_at": "2026-07-01"},
        ])
        result = explain(warehouse, parse_period("2026"), asset_id="AS-X")["asset"]
        self.assertEqual(result["reason"], "asset_missing_package")
        report = build_report(warehouse, parse_period("2026"))
        self.assertEqual(report.payload["delivered_facilities"]["accepted_in_period"], 0)
        self.assertEqual(report.payload["delivered_facilities"]["unassigned"]["region"]
                         ["asset_missing_package"], 1)


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "warehouse.sqlite3")
        self.warehouse = Warehouse(self.db)
        self.addCleanup(self.warehouse.close)
        self.warehouse.load_records(RECORDS)

    def test_publication_is_frozen_when_mapping_is_added_later(self):
        report = build_report(self.warehouse, parse_period("2026"))
        published = publish(self.warehouse, report)
        stored_before = self.warehouse.get_publication(published["publication_id"])
        total_before = stored_before["payload"]["confirmed_investment"]["by_currency"]["CNY"]

        # 后续补充分摊证据：C-2 的 300,000 归到 east。
        extra = RECORDS + [
            {"fact_kind": "allocation", "allocation_id": "A-9", "contract_id": "C-2",
             "project_id": "PR-9", "region_id": "east", "ratio": 1,
             "evidence": "补充归属确认单 2026-09"},
        ]
        self.warehouse.load_records(extra)

        current = build_report(self.warehouse, parse_period("2026"))
        self.assertEqual(current.payload["confirmed_investment"]["by_currency"]["CNY"]
                         ["unallocated"], "0.00")
        self.assertEqual(current.payload["confirmed_investment"]["by_currency"]["CNY"]
                         ["allocated"], "900000.00")

        stored_after = self.warehouse.get_publication(published["publication_id"])
        self.assertEqual(stored_before["payload_hash"], stored_after["payload_hash"])
        self.assertEqual(stored_after["payload"]["confirmed_investment"]["by_currency"]["CNY"],
                         total_before)
        diff = diff_publication(self.warehouse, published["publication_id"])
        self.assertEqual(diff["mapping_added"], ["A-9"])
        self.assertEqual(diff["totals_published"]["CNY"]["unallocated"], "300000.00")
        self.assertEqual(diff["totals_current"]["CNY"]["unallocated"], "0.00")

    def test_publication_tables_reject_updates(self):
        report = build_report(self.warehouse, parse_period("2026"))
        published = publish(self.warehouse, report)
        with self.assertRaises(sqlite3.DatabaseError):
            self.warehouse.conn.execute(
                "UPDATE publications SET payload = '{}' WHERE publication_id = ?",
                (published["publication_id"],),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.warehouse.conn.execute(
                "DELETE FROM publication_mappings WHERE publication_id = ?",
                (published["publication_id"],),
            )

    def test_publication_records_mapping_and_states_at_publish_time(self):
        report = build_report(self.warehouse, parse_period("2026"))
        published = publish(self.warehouse, report)
        stored = self.warehouse.get_publication(published["publication_id"])
        mappings = stored["payload"]["attribution"]["mappings"]
        self.assertEqual({m["allocation_id"] for m in mappings}, {"A-1", "A-2"})
        self.assertEqual(stored["payload"]["payment_confirmation_states"],
                         {"confirmed": 3, "pending": 1})
        rows = self.warehouse._rows(
            "SELECT fact_kind, COUNT(*) AS n FROM publication_statuses "
            "WHERE publication_id = ? GROUP BY fact_kind",
            (published["publication_id"],),
        )
        by_kind = Counter({row["fact_kind"]: row["n"] for row in rows})
        self.assertEqual(by_kind, Counter({"payment": 4, "facility": 3, "package": 1}))
        totals = self.warehouse._rows(
            "SELECT metric, value FROM publication_totals WHERE publication_id = ? "
            "AND currency = 'CNY'", (published["publication_id"],),
        )
        self.assertEqual(
            {row["metric"]: row["value"] for row in totals
             if row["metric"] in {"confirmed_payments", "allocated", "unallocated"}},
            {"confirmed_payments": "900000.00", "allocated": "600000.00",
             "unallocated": "300000.00"},
        )

    def test_duplicate_publish_of_same_state_is_rejected(self):
        from datetime import datetime, timezone
        fixed = datetime(2026, 9, 1, tzinfo=timezone.utc)
        report = build_report(self.warehouse, parse_period("2026"))
        publish(self.warehouse, report, published_at=fixed)
        with self.assertRaises(RuntimeError):
            publish(self.warehouse, report, published_at=fixed)


if __name__ == "__main__":
    unittest.main()
