"""BUG 4: clearing a split leg's amount yields the exact integer-cents remainder.

A leg's amount is a QDoubleSpinBox. Backspacing its text empty leaves a plain
spin box returning the LAST accepted number from ``value()`` and firing no
``valueChanged`` -- so the split's live Remainder (and the uncategorized slot it
lands in on save) stayed off by that stale amount, often ~$4. SplitAmountSpinBox
reads a cleared box as 0 and SplitDialog recomputes on the line edit's textChanged,
so the remainder is exact integer cents with no residual and no float drift.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.ui.models import RegisterModel
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "d.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


# ---------------------------------------------------------------------------
# the spin box in isolation
# ---------------------------------------------------------------------------
def test_split_amount_spinbox_reads_cleared_box_as_zero(qapp):
    from mammon.ui.delegates import SplitAmountSpinBox
    sb = SplitAmountSpinBox()
    sb.setRange(-1_000_000, 1_000_000)
    sb.setDecimals(2)
    sb.setValue(-4.17)
    assert sb.value() == pytest.approx(-4.17)
    sb.lineEdit().clear()                       # backspaced empty
    assert sb.value() == 0.0                     # NOT the stale -4.17
    sb.lineEdit().setText("-")                   # a lone sign is not a number
    assert sb.value() == 0.0
    sb.setValue(12.5)                            # a real number again
    assert sb.value() == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# the dialog: clearing a leg gives an exact remainder
# ---------------------------------------------------------------------------
def test_clearing_a_leg_amount_yields_exact_remainder(qapp, conn, accounts):
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    ledger.resolve_category(conn, "Auto:Fuel")
    tid = ledger.add_transaction(conn, chk, "2026-02-01", -100_00, payee="Store")
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(tid))
    try:
        # Two legs summing to the -$100.00 total: remainder starts at 0.
        dlg._lines[0]["cat"].setCurrentText("Auto:Fuel")
        dlg._lines[0]["amount"].setValue(-95.83)
        dlg._lines[1]["amount"].setValue(-4.17)     # leave leg 2's category blank
        assert dlg.remainder_cents() == 0

        # Backspace leg 2's amount empty. It must count as 0, so the remainder is
        # exactly leg 1's shortfall (-$4.17) -- not stale at 0 by counting -4.17.
        dlg._lines[1]["amount"].lineEdit().clear()
        assert dlg._line_amounts() == [-95_83]      # the cleared leg drops out
        assert dlg.remainder_cents() == -4_17       # exact integer cents
        assert "4.17" in dlg.remainder_label.text()  # live label recomputed
    finally:
        dlg.deleteLater()


def test_clearing_a_leg_with_a_category_counts_it_as_zero(qapp, conn, accounts):
    """A cleared leg that still names a category counts as 0 cents (it is kept as a
    zero line), so the remainder is total minus the other legs -- no stale amount."""
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    ledger.resolve_category(conn, "Auto:Fuel")
    ledger.resolve_category(conn, "Groceries")
    tid = ledger.add_transaction(conn, chk, "2026-02-01", -50_00, payee="Store")
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(tid))
    try:
        dlg._lines[0]["cat"].setCurrentText("Auto:Fuel")
        dlg._lines[0]["amount"].setValue(-30.00)
        dlg._lines[1]["cat"].setCurrentText("Groceries")
        dlg._lines[1]["amount"].setValue(-20.00)
        assert dlg.remainder_cents() == 0

        dlg._lines[1]["amount"].lineEdit().clear()
        # Leg 2 kept (it has a category) but counts as 0 cents.
        assert dlg._line_amounts() == [-30_00, 0]
        assert dlg.remainder_cents() == -20_00
    finally:
        dlg.deleteLater()
