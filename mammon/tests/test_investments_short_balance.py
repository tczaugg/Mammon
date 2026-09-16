"""Regression tests for the investment register's Share Bal column on SHORT legs.

``register_rows`` used to carry its OWN copy of the share-moving rule
(``a in _ADD_ACTIONS or a in _REMOVE_ACTIONS``), which omitted the two short sets
-- ``_SHORT_OPEN_ACTIONS`` (ShtSell) and ``_SHORT_COVER_ACTIONS`` (CvrShrt) are
separate sets in ``mammon.investments``. A short leg therefore fell through to the
cash-only branch, ``share_bal`` stayed ``None``, and ``ui.models.fmt_qty`` rendered
it as an empty cell: the user shorts shares, the balance is legitimately negative,
and the register shows nothing at all.

The fix is ONE classification for the module: ``is_quantity_action`` /
``share_qty_delta``. These tests cover that end to end -- the domain rows AND the
Qt model cell the user actually reads -- plus the deliberate Quicken-parity
behavior that a cash-only row (Div) still leaves Share Bal blank.

Synthetic data only.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, investments, ledger

SYM = "ZZTS"  # synthetic ticker, matches no real security


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "short.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _short_sell(conn, a, date, qty, price, amount):
    investments.record_investment(conn, a, date, "ShtSell", symbol=SYM,
                                  quantity=qty, price=price, amount=amount)


def _cover(conn, a, date, qty, price, amount):
    investments.record_investment(conn, a, date, "CvrShrt", symbol=SYM,
                                  quantity=qty, price=price, amount=amount)


def test_share_bal_goes_negative_on_a_short_and_rises_when_covered(conn, acct):
    """The reported bug: shorting shares must show a NEGATIVE running Share Bal,
    not a blank cell -- deepening on a second ShtSell and rising toward zero on a
    partial CvrShrt."""
    _short_sell(conn, acct, "2026-01-05", "10", "50", 500_00)
    _short_sell(conn, acct, "2026-02-05", "5", "60", 300_00)
    _cover(conn, acct, "2026-03-05", "4", "40", -160_00)

    rows = investments.register_rows(conn, acct)
    assert [r["share_bal"] for r in rows] == ["-10", "-15", "-11"]


def test_a_cash_only_row_between_short_legs_still_leaves_share_bal_blank(conn, acct):
    """Quicken parity: Div/IntInc/RtrnCap move cash, not shares, so their Share Bal
    stays blank -- and they must not disturb the running short balance either."""
    _short_sell(conn, acct, "2026-01-05", "10", "50", 500_00)
    investments.record_investment(conn, acct, "2026-02-01", "Div", symbol=SYM,
                                  amount=12_00)
    _cover(conn, acct, "2026-03-05", "3", "40", -120_00)

    rows = investments.register_rows(conn, acct)
    assert [r["share_bal"] for r in rows] == ["-10", None, "-7"]


def test_a_short_crossed_back_through_zero_becomes_a_long_balance(conn, acct):
    """Crossing zero is the case a sign-blind classification hides: -10 shares plus
    a 25-share buy is +15 long, which must appear as a plain positive balance."""
    _short_sell(conn, acct, "2026-01-05", "10", "50", 500_00)
    investments.record_investment(conn, acct, "2026-04-01", "Buy", symbol=SYM,
                                  quantity="25", price="40", amount=-1_000_00)

    rows = investments.register_rows(conn, acct)
    assert [r["share_bal"] for r in rows] == ["-10", "15"]


def test_a_fully_covered_short_reads_flat_zero_not_blank(conn, acct):
    """A closed short is FLAT, which is a fact worth showing; blank would read as
    'no information' and is what the bug produced."""
    _short_sell(conn, acct, "2026-01-05", "8", "50", 400_00)
    _cover(conn, acct, "2026-02-05", "8", "45", -360_00)

    rows = investments.register_rows(conn, acct)
    assert [r["share_bal"] for r in rows] == ["-8", "0"]


def test_short_share_bal_reaches_the_register_model_cell(qapp, conn, acct):
    """End of the pipe: the Qt model's Share Bal cell must render the negative
    string. ui.models.fmt_qty maps None -> "", so a regression here is invisible in
    the domain layer but blank on screen."""
    from PyQt5.QtCore import Qt

    from mammon.ui.models import InvestmentRegisterModel as M

    _short_sell(conn, acct, "2026-01-05", "10", "50", 500_00)
    _cover(conn, acct, "2026-02-05", "4", "45", -180_00)

    m = M(conn, acct)

    def cell(r, c):
        return m.data(m.index(r, c), Qt.DisplayRole)

    assert cell(0, M.SHARE_BAL) == "-10"
    assert cell(0, M.SHARE_BAL) != ""
    assert cell(1, M.SHARE_BAL) == "-6"


def test_the_register_sorts_a_negative_share_bal_below_zero(qapp, conn, acct):
    """The sort key helper coerces blank to Decimal(0); a negative must still order
    BELOW a flat/long row rather than being swallowed by that fallback."""
    from PyQt5.QtCore import Qt

    from mammon.ui.models import InvestmentRegisterModel as M

    _short_sell(conn, acct, "2026-01-05", "10", "50", 500_00)
    investments.record_investment(conn, acct, "2026-02-01", "Div", symbol=SYM,
                                  amount=12_00)
    investments.record_investment(conn, acct, "2026-03-01", "Buy", symbol=SYM,
                                  quantity="25", price="40", amount=-1_000_00)

    m = M(conn, acct)
    m.sort(M.SHARE_BAL, Qt.AscendingOrder)

    def cell(r, c):
        return m.data(m.index(r, c), Qt.DisplayRole)

    order = [cell(r, M.SHARE_BAL) for r in range(m.rowCount())]
    assert order.index("-10") < order.index("15")
