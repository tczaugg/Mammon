"""The securities manager: the confirmation step that guards the ticker split.

The invariant worth holding down is that NOTHING is derived-and-applied. The
dialog may propose that "FID BALANCED K6" is ticker ``FID``, but a proposal the
user does not tick must never reach the database -- that derivation is wrong for
plan funds, and ``INTL EQUITY INDEX`` -> ``INTL`` would file a real listed
company's prices against a retirement fund.

No modal is ever shown: the confirmation goes through ``QMessageBox.question``
and the write through the ``_apply`` seam.
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
    SecuritiesDialog, DESCRIPTION, IDENTITY, INCLUDE, STATUS,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "secui.db")
    yield c
    c.close()


def _buy(conn, account, symbol, date="2021-05-03", qty=10, price="100.00"):
    investments.record_investment(
        conn, account, date, "Buy", symbol=symbol, quantity=str(qty),
        price=str(price), amount=-int(Decimal(qty) * Decimal(price) * 100))
    investments.rebuild_holdings(conn, account)


@pytest.fixture
def world(conn):
    """The real shape of the problem: one ETF under two spellings, a plan fund
    with no ticker, and a company that changed its name."""
    a = ledger.create_account(conn, "IB IRA", "investment")
    b = ledger.create_account(conn, "401k", "investment")
    _buy(conn, a, "VGT VANGUARD INFO TECH ETF")
    investments.record_investment(conn, a, "2026-03-26", "Div", symbol="VGT",
                                  amount=4200)
    _buy(conn, a, "PWE PENN WEST ENERGY TRUST ORD SHR", date="2009-05-07")
    _buy(conn, a, "PWE PENN WEST PETROLEUM LTD", date="2011-01-14")
    _buy(conn, b, "DOMESTIC BOND INDEX")
    investments.rebuild_holdings(conn, a)
    return {"ib": a, "plan": b}


def _row_for(dlg, stored):
    for r, s in enumerate(dlg._splits):
        if s.old == stored:
            return r
    raise AssertionError(f"{stored!r} not listed")


def _yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: 0))


# ---------------------------------------------------------------------------
def test_a_plan_fund_is_listed_as_unchanged(conn, world):
    dlg = SecuritiesDialog(conn)
    row = _row_for(dlg, "DOMESTIC BOND INDEX")
    assert dlg.table.item(row, IDENTITY).text() == "DOMESTIC BOND INDEX"
    assert "unchanged" in dlg.table.item(row, STATUS).text() or \
        "description" in dlg.table.item(row, STATUS).text()


def test_two_spellings_of_one_holding_are_flagged_as_a_merge(conn, world):
    dlg = SecuritiesDialog(conn)
    row = _row_for(dlg, "VGT VANGUARD INFO TECH ETF")
    assert dlg.table.item(row, IDENTITY).text() == "VGT"
    assert "merges with" in dlg.table.item(row, STATUS).text()
    assert "VGT" in dlg.table.item(row, STATUS).text()


def test_the_renamed_company_merges_into_one_security(conn, world, monkeypatch):
    """PWE PENN WEST ENERGY TRUST and PWE PENN WEST PETROLEUM are one company
    across a rename, so they belong together under one identity."""
    _yes(monkeypatch)
    dlg = SecuritiesDialog(conn)
    dlg.on_apply()
    left = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions WHERE symbol LIKE 'PWE%'")]
    assert left == ["PWE"]
    held = [h["symbol"] for h in investments.list_holdings(conn, world["ib"])]
    assert "PWE" in held and not any(h.startswith("PWE ") for h in held)


def test_an_unticked_proposal_never_reaches_the_database(conn, monkeypatch):
    """The guard the whole design rests on: 'FID BALANCED K6' proposes ticker
    FID, and leaving it unticked must leave the security exactly as it was."""
    acct = ledger.create_account(conn, "401k", "investment")
    _buy(conn, acct, "FID BALANCED K6")
    _yes(monkeypatch)
    dlg = SecuritiesDialog(conn)
    dlg._set_all(False)
    assert dlg.chosen() == []
    dlg.on_apply()
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions")]
    assert rows == ["FID BALANCED K6"]


def test_editing_the_identity_cell_overrides_the_suggestion(conn, monkeypatch):
    """A wrong guess is corrected in place, not worked around."""
    acct = ledger.create_account(conn, "401k", "investment")
    _buy(conn, acct, "INTL EQUITY INDEX")
    _yes(monkeypatch)
    dlg = SecuritiesDialog(conn)
    row = _row_for(dlg, "INTL EQUITY INDEX")
    assert dlg.table.item(row, IDENTITY).text() == "INTL"      # the bad guess
    dlg.table.item(row, IDENTITY).setText("INTL EQUITY INDEX")  # user corrects
    dlg.table.item(row, DESCRIPTION).setText("International Equity Index")
    dlg.on_apply()
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions")]
    assert rows == ["INTL EQUITY INDEX"]
    assert securities.name_of(conn, "INTL EQUITY INDEX") == "International Equity Index"


def test_apply_is_refused_without_confirmation(conn, world, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: 0))
    dlg = SecuritiesDialog(conn)
    dlg.on_apply()
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions "
        "WHERE symbol='VGT VANGUARD INFO TECH ETF'").fetchone()[0] > 0


def test_the_confirmation_spells_out_every_merge(conn, world, monkeypatch):
    """A merge is the one change here that re-running cannot undo, so it is
    named rather than counted."""
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: (asked.append(a[2]),
                                                      QMessageBox.No)[1]))
    dlg = SecuritiesDialog(conn)
    dlg.on_apply()
    assert asked and "MERGED" in asked[0]
    assert "VGT" in asked[0] and "PWE" in asked[0]
    assert "cannot be undone" in asked[0]


def test_applying_records_descriptions_and_survives_a_rebuild(conn, world,
                                                              monkeypatch):
    """holdings.name is derived and replaced wholesale, so it has to be re-read
    from `securities` on every rebuild or it vanishes at the next import."""
    _yes(monkeypatch)
    dlg = SecuritiesDialog(conn)
    dlg.on_apply()
    assert securities.name_of(conn, "VGT") == "VANGUARD INFO TECH ETF"
    investments.rebuild_holdings(conn, world["ib"])
    row = conn.execute("SELECT name FROM holdings WHERE symbol='VGT'").fetchone()
    assert row["name"] == "VANGUARD INFO TECH ETF"


def test_the_summary_counts_rows_that_will_move(conn, world):
    dlg = SecuritiesDialog(conn)
    text = dlg.summary.text()
    assert "selected" in text
    assert "re-keyed" in text
    assert "merge" in text


def test_select_none_clears_every_tick(conn, world):
    dlg = SecuritiesDialog(conn)
    dlg._set_all(False)
    for row in range(dlg.table.rowCount()):
        assert dlg.table.item(row, INCLUDE).checkState() != Qt.Checked
    assert dlg.chosen() == []
