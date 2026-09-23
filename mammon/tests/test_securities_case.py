"""Letter case alone is not a change the user has to approve (SRD 5.8e-2f).

``investments.ticker_of`` upper-cases what it derives, so a file that stored
"zzta" produced a proposal to "rename" it to "ZZTA": a real re-key across
price_history, holdings and every transaction, for the same security, and one
table row per lower-cased symbol waiting for approval. The user's words:
"requiring approval to change case is annoying and stupid ... this just clutters
the table."

The exception this file pins just as hard is the case where a case difference IS
the change: two DISTINCT stored rows whose spellings fold together are one
security stored twice, and merging them is the repair.

Every ticker here is invented (ZZT*, "ZZ ..."); nothing in this file comes from
a real ledger.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from mammon import db, investments, ledger, securities
from mammon.ui.securities_dialog import (
    SecuritiesDialog, DESCRIPTION, INCLUDE, STATUS,
)
from mammon.tests import fresh_db


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "seccase.db")
    yield c
    c.close()


def _buy(conn, account, symbol, date="2021-05-03", qty=10, price="100.00"):
    investments.record_investment(
        conn, account, date, "Buy", symbol=symbol, quantity=str(qty),
        price=str(price), amount=-int(Decimal(qty) * Decimal(price) * 100))
    investments.rebuild_holdings(conn, account)


def _security(conn, symbol, ticker=None, name=None):
    """A securities-master row the way an import would leave one. ``ticker=''``
    is the recorded absence a QIF with no ``S`` field produces."""
    conn.execute("INSERT INTO securities (symbol, ticker, name) VALUES (?,?,?)",
                 (symbol, ticker, name))
    conn.commit()


def _split_for(splits, stored):
    for s in splits:
        if s.old == stored:
            return s
    raise AssertionError(f"{stored!r} not proposed at all")


def _row_for(dlg, stored):
    for r, s in enumerate(dlg._splits):
        if s.old == stored:
            return r
    raise AssertionError(f"{stored!r} not listed")


def _snapshot(conn, symbol):
    """Every stored row of one security, exactly as spelled."""
    out = {}
    for table in ("price_history", "holdings", "investment_transactions"):
        out[table] = [tuple(r) for r in conn.execute(
            f"SELECT * FROM {table} WHERE symbol=? ORDER BY rowid", (symbol,))]
    return out


# --- (a) a stored spelling that differs only in case ------------------------
def test_a_lower_case_symbol_is_not_a_proposed_change(conn):
    acct = ledger.create_account(conn, "Brokerage", "investment")
    _buy(conn, acct, "zzta")
    s = _split_for(securities.suggest_all(conn), "zzta")
    assert s.symbol == "ZZTA"          # the derived identity still upper-cases
    assert s.changes_key is False      # ... but nothing would be re-keyed
    assert s.actionable is False
    assert s.name is None


def test_a_case_only_row_is_listed_but_not_tickable(conn):
    """The listing is kept -- the user still wants to see every security -- but
    the row is unticked, non-checkable and counted as left alone."""
    acct = ledger.create_account(conn, "Brokerage", "investment")
    _buy(conn, acct, "zzta")
    dlg = SecuritiesDialog(conn)
    row = _row_for(dlg, "zzta")
    tick = dlg.table.item(row, INCLUDE)
    assert tick.checkState() == Qt.Unchecked
    assert not (tick.flags() & Qt.ItemIsUserCheckable)
    assert dlg.table.item(row, STATUS).text() == "unchanged"
    assert "0 proposed changes" in dlg.tally.text()
    assert dlg.chosen() == []


# --- (b) a description that differs only in case ---------------------------
def test_a_description_differing_only_in_case_is_not_proposed(conn):
    acct = ledger.create_account(conn, "401k", "investment")
    _buy(conn, acct, "Zz Balanced Fund")
    _security(conn, "Zz Balanced Fund", ticker="", name="ZZ BALANCED FUND")
    s = _split_for(securities.suggest_all(conn), "Zz Balanced Fund")
    assert s.name is None
    assert s.changes_key is False
    assert s.actionable is False
    dlg = SecuritiesDialog(conn)
    row = _row_for(dlg, "Zz Balanced Fund")
    assert dlg.table.item(row, DESCRIPTION).text() == ""
    assert dlg.table.item(row, STATUS).text() == "unchanged"


def test_a_description_not_yet_recorded_is_still_proposed(conn):
    """The suppression is narrow: only a case-only difference from what is
    already recorded. A row with nothing recorded still gets its description."""
    acct = ledger.create_account(conn, "401k", "investment")
    _buy(conn, acct, "Zz Growth Fund")
    _security(conn, "Zz Growth Fund", ticker="")
    s = _split_for(securities.suggest_all(conn), "Zz Growth Fund")
    assert s.name == "Zz Growth Fund"
    assert s.actionable is True


# --- (c) a real rename is untouched by any of this -------------------------
def test_a_real_ticker_split_is_still_proposed(conn):
    acct = ledger.create_account(conn, "Brokerage", "investment")
    _buy(conn, acct, "ZZTB ZAPHOD TECH FUND")
    s = _split_for(securities.suggest_all(conn), "ZZTB ZAPHOD TECH FUND")
    assert s.symbol == "ZZTB"
    assert s.changes_key is True
    assert s.actionable is True
    dlg = SecuritiesDialog(conn)
    tick = dlg.table.item(_row_for(dlg, "ZZTB ZAPHOD TECH FUND"), INCLUDE)
    assert tick.checkState() == Qt.Checked
    assert tick.flags() & Qt.ItemIsUserCheckable


# --- (d) THE exception: two stored rows that fold together -----------------
def test_two_rows_differing_only_in_case_stay_a_proposed_merge(conn):
    """Detected from the actual set of stored symbols, not guessed: "zztc" is
    only a merge because "ZZTC" is really there too."""
    a = ledger.create_account(conn, "Brokerage", "investment")
    b = ledger.create_account(conn, "IRA", "investment")
    _buy(conn, a, "zztc")
    _buy(conn, b, "ZZTC")
    splits = securities.suggest_all(conn)
    twin = _split_for(splits, "zztc")
    assert twin.case_merge is True
    assert twin.changes_key is True
    assert twin.actionable is True
    assert twin.refused is False
    assert twin.reason and "case" in twin.reason.lower()
    # The row that is already spelled the way it will be stays a no-op: it must
    # never be re-keyed onto itself (_rekey deletes the old spelling).
    kept = _split_for(splits, "ZZTC")
    assert kept.changes_key is False
    assert kept.actionable is False


def test_the_case_merge_row_reads_as_a_merge_and_applies_as_one(conn,
                                                                monkeypatch):
    a = ledger.create_account(conn, "Brokerage", "investment")
    b = ledger.create_account(conn, "IRA", "investment")
    _buy(conn, a, "zztc")
    _buy(conn, b, "ZZTC")
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: 0))
    dlg = SecuritiesDialog(conn)
    status = dlg.table.item(_row_for(dlg, "zztc"), STATUS).text()
    assert "merges with" in status and "case" in status.lower()
    assert "1 proposed change" in dlg.tally.text()
    dlg.on_apply()
    left = sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions"))
    assert left == ["ZZTC"]


# --- (e) applying everything leaves a case-only row byte-identical ----------
def test_applying_every_suggestion_leaves_case_only_rows_untouched(conn,
                                                                   monkeypatch):
    """The end of the whole point: no silent bulk re-casing pass. A case-only
    row is identical in every table _rekey touches after apply_splits has run
    over the full suggest_all list."""
    a = ledger.create_account(conn, "Brokerage", "investment")
    b = ledger.create_account(conn, "IRA", "investment")
    _buy(conn, a, "ZZTB ZAPHOD TECH FUND")      # a genuine rename, applied
    _buy(conn, b, "zzta")                       # case only, must not move
    investments.record_price(conn, "zzta", "2021-05-04", "101.25")
    _security(conn, "Zz Balanced Fund", ticker="", name="ZZ BALANCED FUND")

    before = _snapshot(conn, "zzta")
    assert any(before[t] for t in before)

    securities.apply_splits(conn, securities.suggest_all(conn))

    assert _snapshot(conn, "zzta") == before
    assert conn.execute("SELECT COUNT(*) FROM investment_transactions "
                        "WHERE symbol='ZZTA'").fetchone()[0] == 0
    # The rename that WAS proposed still happened, so this is not "apply did
    # nothing at all".
    assert conn.execute("SELECT COUNT(*) FROM investment_transactions "
                        "WHERE symbol='ZZTB'").fetchone()[0] == 1
    # And no securities row was re-cased behind the user's back.
    assert securities.name_of(conn, "Zz Balanced Fund") == "ZZ BALANCED FUND"
