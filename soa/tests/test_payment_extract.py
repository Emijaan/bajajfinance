"""Tests for payment classification and Excel helpers."""

from __future__ import annotations

import io
from datetime import date

from django.test import SimpleTestCase
from openpyxl import Workbook

from soa.excel_io import find_loan_column_index, read_loan_numbers_from_xlsx
from soa.payment_extract import (
    classify_payment_type,
    extract_payment_rows_from_json,
    parse_dd_mon_yyyy,
    _heuristic_amount_from_segment,
)


class PdfAmountHeuristicTests(SimpleTestCase):
    def test_credit_not_total_row(self) -> None:
        segment = (
            "14-May-2026 Payment Received vide ONLINE payment No:\n"
            "BD016134BALAAAJZ61K\n"
            "X\n"
            "- 0.00 8,400.00 0.00 0.00 0.00 0.00 0 11,474.00\n"
            "- Total - 37,613.00 31,495.00 3,000.00"
        )
        self.assertEqual(_heuristic_amount_from_segment(segment), 8400.0)

    def test_truncates_at_total(self) -> None:
        segment = "Payment text\n- 0.00 500.00 0.00\nTotal 99,999.00"
        self.assertEqual(_heuristic_amount_from_segment(segment), 500.0)


class PaymentClassifyTests(SimpleTestCase):
    def test_parse_dd_mon_yyyy(self) -> None:
        self.assertEqual(parse_dd_mon_yyyy("08-Apr-2025"), date(2025, 4, 8))
        self.assertEqual(parse_dd_mon_yyyy("x 21-Apr-2025 y"), date(2025, 4, 21))
        self.assertIsNone(parse_dd_mon_yyyy("nope"))

    def test_classify_online(self) -> None:
        t = (
            "21-Apr-2025 Payment Received ONLINE vide Reference "
            "No: 3770491840 for Advance"
        )
        self.assertEqual(classify_payment_type(t), "online")

    def test_classify_online_vide_payment_no(self) -> None:
        t = "Payment Received vide\nONLINE payment No:\nPP115141BB31V0S7G906"
        self.assertEqual(classify_payment_type(t), "online")

    def test_classify_cash(self) -> None:
        self.assertEqual(
            classify_payment_type("Payment Received CASH at branch"),
            "cash",
        )

    def test_classify_cash_vide_payment_no(self) -> None:
        t = "Payment Received vide\nCASH payment No:\nABC123"
        self.assertEqual(classify_payment_type(t), "cash")

    def test_classify_unknown_payment(self) -> None:
        self.assertEqual(
            classify_payment_type("Payment Received at counter"),
            "unknown",
        )

    def test_classify_not_payment(self) -> None:
        self.assertIsNone(classify_payment_type("CONVENIENCE FEES"))


class JsonExtractTests(SimpleTestCase):
    def test_extracts_from_nested_dict(self) -> None:
        payload = {
            "data": [
                {
                    "Date": "21-Apr-2025",
                    "Particulars": (
                        "Payment Received vide ONLINE payment No: PP115141BB31V0S7G906"
                    ),
                    "Credit": 890.0,
                }
            ]
        }
        rows = extract_payment_rows_from_json(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payment_type"], "online")
        self.assertEqual(rows[0]["amount"], 890.0)


class ExcelReadTests(SimpleTestCase):
    def _xlsx_bytes(self, headers: tuple, rows: list) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.append(list(headers))
        for r in rows:
            ws.append(list(r))
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_find_loan_column(self) -> None:
        self.assertEqual(
            find_loan_column_index(("Loan No", "Name")),
            0,
        )
        self.assertEqual(
            find_loan_column_index(("x", "AGREEMENT NO")),
            1,
        )

    def test_read_loans(self) -> None:
        raw = self._xlsx_bytes(
            ("Loan No", "Note"),
            (("P6D9PRR66028313", "a"), ("p6d9prr66028313", "dup")),
        )
        loans = read_loan_numbers_from_xlsx(raw)
        self.assertEqual(loans, ["P6D9PRR66028313"])
