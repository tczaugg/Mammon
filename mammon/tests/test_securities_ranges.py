"""Held date ranges, and the overlap that disqualifies a proposed rename.

The user's ask, verbatim: "What would be helpful for the securities table is if
it showed the date ranges when it was owned (long or short). Then you could
disqualify a name change proposition if the time ranges overlap."

The reasoning the tests pin down: a rename is a SUCCESSION -- the old symbol
stops being held, the new one starts -- so a single day on which both had an
open position proves they are two securities and the merge would destroy one of
them. Two exemptions have to survive: a case-only twin (one security stored
twice) overlaps itself totally and is still the merge it always was, and a
target identity that exists nowhere else in the file has no ranges to overlap.

Synthetic tickers only (SRD 5.8e-2f).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest

from PyQt5.QtWidgets import QApplication

from mammon import db, investments, ledger, securities
from mammon.tests import fresh_db


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "ranges.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Zz Brokerage", "investment")


_MONEY_IN = {"sell", "shtsell"}


def _txn(conn, account, date, action, symbol, qty, price="10.00"):
    """One quantity-changing row, with a plausibly signed cash amount."""
    cash = int(Decimal(str(qty)) * Decimal(price) * 100)
    if action.strip().lower() not in _MONEY_IN:
        cash = -cash
    investments.record_investment(
        conn, account, date, action, symbol=symbol, quantity=str(qty),
        price=price, amount=cash)


def _split_for(splits, stored):
    for s in splits:
        if s.old == stored:
            return s
    raise AssertionError(f"no split for {stored!r} in "
                         f"{[s.old for s in splits]}")


# ---- the ranges themselves ----------------------------------------------

def test_buy_to_zero_sell_is_one_closed_long_range(conn, account):
    _txn(conn, account, "2004-03-12", "Buy", "ZZTA", 10)
    _txn(conn, account, "2011-07-01", "Sell", "ZZTA", 10)
    ranges = investments.held_ranges(conn, "ZZTA")
    assert [(r.start, r.end, r.direction) for r in ranges] == [
        ("2004-03-12", "2011-07-01", "long")]
    assert investments.format_held_ranges(ranges) == "2004-03-12 - 2011-07-01"


def test_partial_sales_do_not_close_the_range(conn, account):
    _txn(conn, account, "2004-03-12", "Buy", "ZZTA", 10)
    _txn(conn, account, "2006-01-05", "Sell", "ZZTA", 4)
    _txn(conn, account, "2008-09-09", "Buy", "ZZTA", 2)
    _txn(conn, account, "2011-07-01", "Sell", "ZZTA", 8)
    ranges = investments.held_ranges(conn, "ZZTA")
    assert [(r.start, r.end) for r in ranges] == [
        ("2004-03-12", "2011-07-01")]


def test_a_short_position_is_marked_short(conn, account):
    _txn(conn, account, "2015-02-02", "ShtSell", "ZZTB", 5)
    _txn(conn, account, "2015-08-08", "CvrShrt", "ZZTB", 5)
    ranges = investments.held_ranges(conn, "ZZTB")
    assert [(r.start, r.end, r.direction) for r in ranges] == [
        ("2015-02-02", "2015-08-08", "short")]
    assert investments.format_held_ranges(ranges) == \
        "2015-02-02 - 2015-08-08 (short)"


def test_an_open_position_has_no_end(conn, account):
    _txn(conn, account, "2019-05-02", "Buy", "ZZTC", 7)
    ranges = investments.held_ranges(conn, "ZZTC")
    assert ranges[0].end is None
    assert investments.format_held_ranges(ranges) == "2019-05-02 - present"


def test_a_symbol_with_no_quantity_rows_has_no_ranges(conn, account):
    investments.record_investment(conn, account, "2020-04-04", "Div",
                                  symbol="ZZTD", amount=1200)
    assert investments.held_ranges(conn, "ZZTD") == []
    assert investments.format_held_ranges([]) == ""


def test_only_three_ranges_are_spelled_out(conn, account):
    for year in (2001, 2002, 2003, 2004):
        _txn(conn, account, f"{year}-01-05", "Buy", "ZZTE", 3)
        _txn(conn, account, f"{year}-06-05", "Sell", "ZZTE", 3)
    ranges = investments.held_ranges(conn, "ZZTE")
    assert len(ranges) == 4
    text = investments.format_held_ranges(ranges, 3)
    assert text.endswith("+1 more")
    assert "2004-01-05" not in text
    # The tooltip form keeps everything.
    assert "2004-01-05" in investments.format_held_ranges(ranges)


def test_overlap_is_inclusive_on_the_handover_day():
    a = investments.HeldRange("2001-01-01", "2004-06-30", "long")
    b = investments.HeldRange("2004-06-30", None, "long")
    assert investments.held_overlap(a, b) == ("2004-06-30", "2004-06-30")
    c = investments.HeldRange("2004-07-01", None, "long")
    assert investments.held_overlap(a, c) is None


# ---- the disqualification ------------------------------------------------

def test_overlapping_pair_is_not_proposed_and_says_why(conn, account):
    # "ZZTA ZZ ALPHA CORP" would be merged into the bare "ZZTA" -- but both
    # were held through 2006, so they are two securities, not one renamed.
    _txn(conn, account, "2005-01-03", "Buy", "ZZTA ZZ ALPHA CORP", 10)
    _txn(conn, account, "2007-02-02", "Sell", "ZZTA ZZ ALPHA CORP", 10)
    _txn(conn, account, "2006-05-05", "Buy", "ZZTA", 4)
    splits = securities.suggest_all(conn)
    s = _split_for(splits, "ZZTA ZZ ALPHA CORP")
    assert s.symbol == s.old
    assert s.name is None
    assert not s.changes_key
    assert not s.actionable
    assert s.refused
    assert "ZZTA" in s.reason
    assert "2006-05-05" in s.reason and "2007-02-02" in s.reason


def test_non_overlapping_pair_is_still_proposed(conn, account):
    # Sold out in 2003, the successor bought in 2004: a rename stays plausible.
    _txn(conn, account, "2001-03-03", "Buy", "ZZTB ZZ BETA CORP", 10)
    _txn(conn, account, "2003-03-03", "Sell", "ZZTB ZZ BETA CORP", 10)
    _txn(conn, account, "2004-04-04", "Buy", "ZZTB", 10)
    s = _split_for(securities.suggest_all(conn), "ZZTB ZZ BETA CORP")
    assert s.symbol == "ZZTB"
    assert s.changes_key
    assert s.actionable
    assert s.reason is None


def test_target_identity_absent_from_the_file_cannot_overlap(conn, account):
    # Nothing else is stored as "ZZTF", so there is nothing to collide with.
    _txn(conn, account, "2005-01-03", "Buy", "ZZTF ZZ BALANCED FUND", 10)
    s = _split_for(securities.suggest_all(conn), "ZZTF ZZ BALANCED FUND")
    assert s.symbol == "ZZTF"
    assert s.actionable


def test_case_only_twin_merge_survives_total_overlap(conn, account):
    # One security stored twice, held simultaneously by construction. The
    # overlap is evidence FOR the merge and must not disqualify it.
    _txn(conn, account, "2005-01-03", "Buy", "zzta", 10)
    _txn(conn, account, "2005-01-03", "Buy", "ZZTA", 10)
    s = _split_for(securities.suggest_all(conn), "zzta")
    assert s.case_merge
    assert s.symbol == "ZZTA"
    assert s.changes_key
    assert s.actionable
    assert s.reason == securities.CASE_MERGE_REASON


def test_a_dividend_only_target_cannot_disqualify(conn, account):
    # The bare spelling exists but never held anything, so the ordinary
    # merge-into-the-ticker proposal is untouched.
    _txn(conn, account, "2005-01-03", "Buy", "ZZTG ZZ GAMMA CORP", 10)
    investments.record_investment(conn, account, "2006-06-06", "Div",
                                  symbol="ZZTG", amount=900)
    s = _split_for(securities.suggest_all(conn), "ZZTG ZZ GAMMA CORP")
    assert s.symbol == "ZZTG"
    assert s.actionable


# ---- the dialog ----------------------------------------------------------

def test_dialog_shows_the_held_text(conn, account):
    from mammon.ui.securities_dialog import HELD, HEADERS, SecuritiesDialog

    _txn(conn, account, "2004-03-12", "Buy", "ZZTA", 10)
    _txn(conn, account, "2011-07-01", "Sell", "ZZTA", 10)
    _txn(conn, account, "2019-05-02", "Buy", "ZZTC", 7)
    investments.record_investment(conn, account, "2020-04-04", "Div",
                                  symbol="ZZTD", amount=1200)
    dlg = SecuritiesDialog(conn)
    try:
        assert HEADERS[HELD] == "Held"
        cells = {s.old: dlg.table.item(row, HELD)
                 for row, s in enumerate(dlg._splits)}
        assert cells["ZZTA"].text() == "2004-03-12 - 2011-07-01"
        assert cells["ZZTA"].toolTip() == "2004-03-12 - 2011-07-01"
        assert cells["ZZTC"].text() == "2019-05-02 - present"
        # Nothing was ever held under the dividend-only spelling.
        assert cells["ZZTD"].text() == ""
    finally:
        dlg.deleteLater()
