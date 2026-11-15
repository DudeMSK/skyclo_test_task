from datetime import date
from decimal import Decimal
from pathlib import Path
import unittest

from report import calculate_report, load_data, money_text


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts, cls.works, cls.payments = load_data(DATA_DIR)

    def calculate(self, report_date: date):
        return calculate_report(self.contracts, self.works, self.payments, report_date)

    def test_required_report_date(self) -> None:
        work_rows, contract_rows = self.calculate(date(2026, 11, 10))
        works = {row.work_id: row for row in work_rows}
        contracts = {row.contract_id: row for row in contract_rows}

        self.assertEqual(works["W-11"].paid, Decimal("100000.00"))
        self.assertEqual(works["W-12"].paid, Decimal("100000.00"))
        self.assertEqual(works["W-13"].status_as_of, "cancelled")
        self.assertEqual(works["W-13"].remaining, Decimal("0.00"))
        self.assertEqual(works["W-14"].paid, Decimal("100000.00"))
        self.assertEqual(works["W-15"].paid, Decimal("15000.00"))
        self.assertEqual(works["W-16"].remaining, Decimal("100000.00"))

        self.assertEqual(contracts["D-1"].paid, Decimal("315000.00"))
        self.assertEqual(contracts["D-1"].remaining, Decimal("185000.00"))
        self.assertEqual(contracts["D-1"].cancelled_works_total, Decimal("100000.00"))
        self.assertEqual(contracts["D-2"].paid, Decimal("75000.00"))
        self.assertEqual(contracts["D-2"].remaining, Decimal("15000.00"))

    def test_future_payment_and_completion_are_excluded(self) -> None:
        work_rows, contract_rows = self.calculate(date(2026, 11, 10))
        works = {row.work_id: row for row in work_rows}
        contracts = {row.contract_id: row for row in contract_rows}

        self.assertEqual(works["W-23"].status_as_of, "planned")
        self.assertEqual(works["W-23"].paid, Decimal("25000.00"))
        self.assertEqual(contracts["D-2"].paid, Decimal("75000.00"))

        later_works, later_contracts = self.calculate(date(2026, 11, 12))
        later_work_map = {row.work_id: row for row in later_works}
        later_contract_map = {row.contract_id: row for row in later_contracts}
        self.assertEqual(later_work_map["W-23"].status_as_of, "done")
        self.assertEqual(later_contract_map["D-2"].paid, Decimal("95000.00"))
        self.assertEqual(later_contract_map["D-2"].overpaid, Decimal("5000.00"))

    def test_refund_has_effect_only_before_refund_date(self) -> None:
        _, before_refund = self.calculate(date(2026, 9, 5))
        _, after_refund = self.calculate(date(2026, 9, 10))
        before = {row.contract_id: row for row in before_refund}
        after = {row.contract_id: row for row in after_refund}

        self.assertEqual(before["D-1"].paid, Decimal("350000.00"))
        self.assertEqual(after["D-1"].paid, Decimal("300000.00"))
        self.assertNotIn("D-2", before)

    def test_money_is_always_formatted_to_kopecks(self) -> None:
        self.assertEqual(money_text(Decimal("12")), "12.00")
        self.assertEqual(money_text(Decimal("12.5")), "12.50")

    def test_allocated_and_unallocated_money_matches_contract_payments(self) -> None:
        work_rows, contract_rows = self.calculate(date(2026, 11, 10))
        allocated = {}
        for row in work_rows:
            allocated[row.contract_id] = allocated.get(row.contract_id, Decimal("0.00")) + row.paid

        for contract in contract_rows:
            self.assertEqual(
                allocated.get(contract.contract_id, Decimal("0.00")) + contract.unallocated_paid,
                contract.paid,
            )


if __name__ == "__main__":
    unittest.main()
