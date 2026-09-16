"""What an option contract LOOKS like -- the holdings window and the register's
action list (SRD 5.8e-8).

Item 7 of the instrument taxonomy: the domain layer already knows a contract is
not a share (item 5a valued it, item 5b gave it endings); this file proves the
two places a user MEETS one behave accordingly.

  * :class:`HoldingsDialog` lists a contract indented under its underlying but
    counts it entirely separately -- contracts are never folded into the
    underlying's share count -- and shows the terms that make it identifiable
    (expiration, strike, right) beside its premium and market value.
  * A WRITTEN contract reads as the liability it is: a negative quantity, a
    negative market value, painted with ``style.negative_color()`` so it follows
    the theme into dark mode rather than a hardcoded red.
  * An expiring or expired contract carries a cue, and that cue comes from
    ``investments.option_position_problems`` -- no expiry rule is restated in
    the widget layer.
  * The investment register offers the seven option verbs (the four open/close
    pairs plus exercise, assignment and expiration) for a contract, and only for
    a contract.

Every assertion above has a NULL-KIND TWIN: a second account holding securities
of exactly the same SHAPE -- including one whose symbol is an OSI contract
string -- that nobody ever classified. UNCLASSIFIED is not "option", so that
account must get today's UI unchanged: eight columns with their original
titles, no indent, no cue, no extra verbs. A forty-year ledger nobody has
classified is that account, everywhere.

All data is synthetic: two invented issuers, invented contracts, invented
account names.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest

from mammon import db, instruments, investments, ledger, securities
from mammon.ui import style
from mammon.ui.models import fmt_date
from mammon.ui.widgets import (HoldingsDialog, InvestmentTransactionDialog,
                               _INV_ACTION_CHOICES)

from PyQt5.QtCore import Qt


# --------------------------------------------------------------------------
# synthetic data: one classified issuer, one identical-in-shape twin
# --------------------------------------------------------------------------
STOCK = "ACME"
PAST = "ACME  251201C00055000"      # strike 55 call, expiration already past
CALL = "ACME  251210C00050000"      # strike 50 call, expires within the week
PUT = "ACME  260116P00045000"       # strike 45 put, WRITTEN (short), far out

# The twin: same shapes, same OSI-looking symbol, never classified.
TWIN_STOCK = "ACMX"
TWIN_CALL = "ACMX  251210C00050000"

BUY = "2025-11-03"
AS_OF = "2025-12-05"                # the only price date, so valuation_as_of is this


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "options_ui.db")
    yield c
    c.close()


def _security(conn, symbol, name=None):
    conn.execute("INSERT OR IGNORE INTO securities(symbol, name) VALUES (?,?)",
                 (symbol, name or symbol))
    conn.commit()


def _option(conn, symbol, *, strike, right, expiration, underlying=STOCK,
            multiplier="100"):
    """One CLASSIFIED contract, through the single writer of the kind columns."""
    _security(conn, symbol)
    securities.set_kinds(conn, [dict(
        symbol=symbol, kind=instruments.Kind.OPTION.value, kind_source="user",
        multiplier=multiplier, underlying=underlying, expiration=expiration,
        strike=strike, option_right=right)])


def _leg(conn, account_id, action, symbol, quantity, amount, date=BUY):
    investments.record_investment(conn, account_id, date, action,
                                  symbol=symbol, quantity=quantity,
                                  amount=amount)


@pytest.fixture
def classified(conn):
    """An account holding 100 shares, two long calls (one of them past its
    expiration, one expiring within the week) and one WRITTEN put."""
    acct = ledger.create_account(conn, "Brokerage One", "investment",
                                 opening_balance=0)
    _security(conn, STOCK, "Acme Industries")
    _option(conn, PAST, strike="55", right="C", expiration="2025-12-01")
    _option(conn, CALL, strike="50", right="C", expiration="2025-12-10")
    _option(conn, PUT, strike="45", right="P", expiration="2026-01-16")

    _leg(conn, acct, "Buy", STOCK, "100", -50_00 * 100)
    _leg(conn, acct, investments.OPTION_BUY_TO_OPEN, PAST, "1", -150_00)
    _leg(conn, acct, investments.OPTION_BUY_TO_OPEN, CALL, "2", -400_00)
    _leg(conn, acct, investments.OPTION_SELL_TO_OPEN, PUT, "1", 300_00)

    for symbol, close in ((STOCK, "52.00"), (PAST, "0.25"), (CALL, "3.00"),
                          (PUT, "2.50")):
        investments.record_price(conn, symbol, AS_OF, close)
    investments.rebuild_holdings(conn, acct)
    return acct


@pytest.fixture
def unclassified(conn):
    """The TWIN. Same shapes, same quantities, one symbol that reads exactly
    like an option contract -- and not one classification anywhere."""
    acct = ledger.create_account(conn, "Brokerage Two", "investment",
                                 opening_balance=0)
    _security(conn, TWIN_STOCK, "Acmex Industries")
    _security(conn, TWIN_CALL)
    _leg(conn, acct, "Buy", TWIN_STOCK, "100", -50_00 * 100)
    _leg(conn, acct, "Buy", TWIN_CALL, "2", -400_00)
    for symbol, close in ((TWIN_STOCK, "52.00"), (TWIN_CALL, "3.00")):
        investments.record_price(conn, symbol, AS_OF, close)
    investments.rebuild_holdings(conn, acct)
    return acct


def _rows(dlg):
    """``{true symbol: row}`` for the Currently Held table (cash row excluded)."""
    out = {}
    for row in range(len(dlg._held)):
        item = dlg.table.item(row, HoldingsDialog.SYMBOL)
        out[item.data(Qt.UserRole)] = row
    return out


def _text(dlg, row, col):
    item = dlg.table.item(row, col)
    return item.text() if item is not None else None


def _colour(dlg, row, col):
    from PyQt5.QtGui import QColor
    item = dlg.table.item(row, col)
    return QColor(item.foreground().color()).name()


def _colour_name(spec):
    from PyQt5.QtGui import QColor
    return QColor(spec).name()


def _num(dlg, row, col):
    """A displayed quantity back as a Decimal, so the assertion is about the
    NUMBER and not about how many trailing zeros the store happened to keep."""
    return Decimal(_text(dlg, row, col).replace(",", ""))


# --------------------------------------------------------------------------
# 1. Grouping: a contract sits under its underlying, counted separately
# --------------------------------------------------------------------------
def test_contracts_group_under_the_underlying_and_are_counted_separately(
        qapp, conn, classified):
    dlg = HoldingsDialog(conn, classified)

    # The shares come first, then the contracts written on them, ordered by
    # expiration and then strike -- one visual block per underlying.
    order = [dlg.table.item(r, HoldingsDialog.SYMBOL).data(Qt.UserRole)
             for r in range(len(dlg._held))]
    assert order == [STOCK, PAST, CALL, PUT]

    rows = _rows(dlg)
    # Grouping is presentation only. The stock row still says 100 SHARES: the
    # three contracts controlling another 400 are nowhere in that number.
    assert _num(dlg, rows[STOCK], HoldingsDialog.SHARES) == Decimal(100)
    assert _num(dlg, rows[CALL], HoldingsDialog.SHARES) == Decimal(2)
    assert _num(dlg, rows[PAST], HoldingsDialog.SHARES) == Decimal(1)

    # The contract rows are indented; the stock they hang under is not. The
    # TRUE symbol stays in Qt.UserRole, so charting an indented row still
    # charts that contract and not the stock.
    assert _text(dlg, rows[STOCK], HoldingsDialog.SYMBOL) == STOCK
    assert _text(dlg, rows[CALL], HoldingsDialog.SYMBOL).startswith(" ")
    assert _text(dlg, rows[CALL], HoldingsDialog.SYMBOL).strip() == CALL
    assert (dlg.table.item(rows[CALL], HoldingsDialog.SYMBOL)
            .data(HoldingsDialog.GROUP_ROLE)) == STOCK


def test_null_kind_holdings_are_todays_table_unchanged(qapp, conn, unclassified):
    """The TWIN of the test above. An OSI-shaped symbol nobody classified is
    just a security: eight columns with their original titles, symbol order,
    no indent, no cue."""
    dlg = HoldingsDialog(conn, unclassified)

    assert dlg.table.columnCount() == len(HoldingsDialog.HEADERS)
    headers = [dlg.table.horizontalHeaderItem(c).text()
               for c in range(dlg.table.columnCount())]
    assert headers == HoldingsDialog.HEADERS

    order = [dlg.table.item(r, HoldingsDialog.SYMBOL).data(Qt.UserRole)
             for r in range(len(dlg._held))]
    assert order == sorted(order) == [TWIN_STOCK, TWIN_CALL]
    for row in range(len(dlg._held)):
        item = dlg.table.item(row, HoldingsDialog.SYMBOL)
        assert item.text() == item.data(Qt.UserRole)          # never indented
        assert item.data(HoldingsDialog.CUE_ROLE) is None


# --------------------------------------------------------------------------
# 2. The terms, the premium and the market value
# --------------------------------------------------------------------------
def test_option_row_shows_premium_terms_and_its_own_market_value(
        qapp, conn, classified):
    dlg = HoldingsDialog(conn, classified)
    rows = _rows(dlg)

    headers = [dlg.table.horizontalHeaderItem(c).text()
               for c in range(dlg.table.columnCount())]
    assert headers == HoldingsDialog.OPTION_HEADERS

    row = rows[CALL]
    assert _text(dlg, row, HoldingsDialog.EXPIRES) == fmt_date("2025-12-10")
    assert _num(dlg, row, HoldingsDialog.STRIKE) == Decimal(50)
    assert _num(dlg, rows[PUT], HoldingsDialog.STRIKE) == Decimal(45)
    assert _text(dlg, row, HoldingsDialog.RIGHT) == "Call"
    assert _text(dlg, rows[PUT], HoldingsDialog.RIGHT) == "Put"
    # The premium is per SHARE; the market value is premium x contracts x 100.
    assert _num(dlg, row, HoldingsDialog.PRICE) == Decimal("3.00")
    assert _num(dlg, row, HoldingsDialog.MARKET) == Decimal("600.00")
    # ... and the underlying's own value is untouched by any of it.
    assert _num(dlg, rows[STOCK], HoldingsDialog.MARKET) == Decimal("5200.00")

    # A share row in the same table simply has no terms to show.
    for col in (HoldingsDialog.EXPIRES, HoldingsDialog.STRIKE,
                HoldingsDialog.RIGHT):
        assert _text(dlg, rows[STOCK], col) == ""


def test_written_contract_reads_as_a_liability(qapp, conn, classified):
    dlg = HoldingsDialog(conn, classified)
    row = _rows(dlg)[PUT]

    assert _num(dlg, row, HoldingsDialog.SHARES) == Decimal(-1)
    assert _num(dlg, row, HoldingsDialog.MARKET) == Decimal("-250.00")
    negative = _colour_name(style.negative_color())
    assert _colour(dlg, row, HoldingsDialog.SHARES) == negative
    assert _colour(dlg, row, HoldingsDialog.MARKET) == negative


def test_null_kind_row_shows_no_terms_and_no_liability_colour(
        qapp, conn, unclassified):
    """The TWIN. The unclassified contract-shaped security has no terms columns
    to fill at all, and its long position is painted like any other holding."""
    dlg = HoldingsDialog(conn, unclassified)
    assert dlg.table.columnCount() == HoldingsDialog.EXPIRES   # no ninth column
    row = _rows(dlg)[TWIN_CALL]
    assert _num(dlg, row, HoldingsDialog.SHARES) == Decimal(2)
    # Valued as 2 SHARES at 3.00 -- no multiplier, because nothing said option.
    assert _num(dlg, row, HoldingsDialog.MARKET) == Decimal("6.00")
    assert _colour(dlg, row, HoldingsDialog.SHARES) != _colour_name(
        style.negative_color())


# --------------------------------------------------------------------------
# 3. The expiration cue, sourced from option_position_problems
# --------------------------------------------------------------------------
def test_expiring_and_expired_contracts_are_visually_distinguishable(
        qapp, conn, classified):
    dlg = HoldingsDialog(conn, classified)
    rows = _rows(dlg)

    def cue(symbol):
        return (dlg.table.item(rows[symbol], HoldingsDialog.SYMBOL)
                .data(HoldingsDialog.CUE_ROLE))

    assert cue(PAST) == investments.OPTION_CUE_EXPIRED
    assert cue(CALL) == investments.OPTION_CUE_EXPIRING
    assert cue(PUT) is None
    assert cue(STOCK) is None

    # The cue is the DOMAIN's answer, not a rule restated in the widget.
    problems = investments.option_position_problems(conn, classified, AS_OF)
    assert {p.symbol for p in problems
            if p.problem == investments.OPTION_PROBLEM_EXPIRED} == {PAST}

    # Expired and merely-expiring read differently, and both come from the
    # theme rather than a literal.
    expired = _colour(dlg, rows[PAST], HoldingsDialog.SYMBOL)
    expiring = _colour(dlg, rows[CALL], HoldingsDialog.SYMBOL)
    assert expired == _colour_name(style.negative_color())
    assert expiring == _colour_name(style.accent_color())
    assert expired != expiring
    # An expired contract explains itself rather than just changing colour.
    assert dlg.table.item(rows[PAST], HoldingsDialog.SYMBOL).toolTip()


def test_null_kind_never_gets_an_expiration_cue(qapp, conn, unclassified):
    """The TWIN. ``ACMX  251210C00050000`` reads as a contract expiring in five
    days to a human, and to this code it is a security with a funny name. An
    unclassified ledger must not sprout warnings nobody asked for."""
    dlg = HoldingsDialog(conn, unclassified)
    assert investments.option_position_problems(conn, unclassified, AS_OF) == []
    for row in range(len(dlg._held)):
        item = dlg.table.item(row, HoldingsDialog.SYMBOL)
        assert item.data(HoldingsDialog.CUE_ROLE) is None
        assert not item.toolTip()


# --------------------------------------------------------------------------
# 4. The register's action vocabulary
# --------------------------------------------------------------------------
_BASE_CODES = [code for _label, code in _INV_ACTION_CHOICES]
_OPTION_CODES = [
    investments.OPTION_BUY_TO_OPEN, investments.OPTION_SELL_TO_CLOSE,
    investments.OPTION_SELL_TO_OPEN, investments.OPTION_BUY_TO_CLOSE,
    investments.OPTION_EXERCISE, investments.OPTION_ASSIGN,
    investments.OPTION_EXPIRE,
]


def test_option_row_offers_the_seven_option_verbs(qapp, conn, classified):
    dlg = InvestmentTransactionDialog(conn, classified)
    assert dlg.action_codes() == _BASE_CODES          # nothing until a contract

    dlg.security.setEditText(CALL)
    assert dlg.action_codes() == _BASE_CODES + _OPTION_CODES

    # Choosing a plain share again takes them away, so "Sell to Close" never
    # stands beside "Sell" for something that has neither.
    dlg.security.setEditText(STOCK)
    assert dlg.action_codes() == _BASE_CODES


def test_null_kind_security_offers_exactly_todays_actions(
        qapp, conn, unclassified):
    """The TWIN. The verbs key off ``kind == 'option'`` and nothing else --
    not the shape of the symbol, not the quantity, not the account."""
    dlg = InvestmentTransactionDialog(conn, unclassified)
    assert dlg.action_codes() == _BASE_CODES

    dlg.security.setEditText(TWIN_CALL)
    assert dlg.action_codes() == _BASE_CODES
    dlg.security.setEditText(TWIN_STOCK)
    assert dlg.action_codes() == _BASE_CODES
