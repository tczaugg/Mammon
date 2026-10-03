"""Unit tests for the printable-register HTML builder (mammon.ui.printing).

register_html is pure -- it turns register-row dicts into an HTML document --
so these run without a QApplication. The PDF render path (which needs Qt) is
covered in test_ui.py.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mammon.ui.printing import register_html


def _rows():
    return [
        {"date": "2026-01-05", "num": "1001", "payee": "Safeway",
         "category_label": "Groceries", "tag": None, "memo": "weekly",
         "amount": -125_00, "cleared": 1, "reconciled": 0, "balance": 9_875_00},
        {"date": "2026-01-10", "num": None, "payee": "Paycheck",
         "category_label": "Salary", "tag": None, "memo": None,
         "amount": 2_500_00, "cleared": 0, "reconciled": 1, "balance": 12_375_00},
    ]


def test_register_html_has_account_headers_and_totals():
    html = register_html("Second Credit Union Checking", _rows(), 12_375_00,
                         subtitle="2 transactions")
    assert "Second Credit Union Checking" in html
    assert "2 transactions" in html
    for h in ["Date", "Num", "Payee", "Category", "Memo",
              "Payment", "Clr", "Deposit", "Balance"]:
        assert f">{h}<" in html
    # thousands-separated ending balance with a dollar sign
    assert "Ending Balance: $12,375.00" in html


def test_register_html_fans_amounts_and_marks_clr():
    html = register_html("Checking", _rows(), 12_375_00)
    # a debit shows in Payment (positive, comma-formatted)
    assert "125.00" in html
    # a credit shows in Deposit
    assert "2,500.00" in html
    # cleared 'c' and reconciled 'R' glyphs both present as their own cells
    assert ">c<" in html
    assert ">R<" in html


def test_register_html_escapes_and_tolerates_missing_fields():
    rows = [{"date": "2026-02-01", "num": None, "payee": "A & B <Co>",
             "category_label": "", "tag": None, "memo": None,
             "amount": -10_00, "cleared": 0, "reconciled": 0, "balance": -10_00}]
    html = register_html("Cash", rows, -10_00)
    assert "A &amp; B &lt;Co&gt;" in html      # HTML-escaped payee
    assert "Ending Balance: -$10.00" in html    # negative balance keeps its sign
