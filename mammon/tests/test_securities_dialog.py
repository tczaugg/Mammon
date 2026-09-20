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
    SecuritiesDialog, DESCRIPTION, IDENTITY, INCLUDE, ROWS, STATUS,
)
from mammon.tests import fresh_db


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "secui.db")
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


def test_the_blurb_names_both_ways_to_keep_a_stored_name(conn, world):
    """User-reported: "clear the Identity cell back to the stored name to say
    so" read as an instruction to type the stored name back in. The label has
    to name the checkbox route (in the user's own terms) and the empty-cell
    route, in words that stand on their own."""
    dlg = SecuritiesDialog(conn)
    text = dlg.blurb.text()
    assert "to say so" not in text
    assert "checkbox" in text                      # the route the user expected
    assert "keep the stored name as the identity" in text
    assert "leave the Identity cell empty" in text  # the other route
    assert "keeps the stored name" in text
    assert "IDENTITY" in text and "DESCRIPTION" in text


def test_emptying_the_identity_cell_keeps_the_stored_name_with_its_description(
        conn):
    """The blurb's second route, pinned to chosen(): an empty Identity cell
    falls back to the stored name, and the row still carries its Description
    edit because it is still ticked."""
    acct = ledger.create_account(conn, "401k", "investment")
    _buy(conn, acct, "INTL EQUITY INDEX")
    dlg = SecuritiesDialog(conn)
    row = _row_for(dlg, "INTL EQUITY INDEX")
    dlg.table.item(row, IDENTITY).setText("")
    dlg.table.item(row, DESCRIPTION).setText("International Equity Index")
    picked = [s for s in dlg.chosen() if s.old == "INTL EQUITY INDEX"]
    assert len(picked) == 1
    assert picked[0].symbol == "INTL EQUITY INDEX"
    assert picked[0].name == "International Equity Index"


def test_unticking_a_row_yields_nothing_at_all_from_chosen(conn):
    """The blurb's first route: an unticked row is skipped whole -- its
    Description edit does not travel either."""
    acct = ledger.create_account(conn, "401k", "investment")
    _buy(conn, acct, "INTL EQUITY INDEX")
    dlg = SecuritiesDialog(conn)
    row = _row_for(dlg, "INTL EQUITY INDEX")
    dlg.table.item(row, DESCRIPTION).setText("International Equity Index")
    dlg.table.item(row, INCLUDE).setCheckState(Qt.Unchecked)
    assert [s for s in dlg.chosen() if s.old == "INTL EQUITY INDEX"] == []


def test_select_none_clears_every_tick(conn, world):
    dlg = SecuritiesDialog(conn)
    dlg._set_all(False)
    for row in range(dlg.table.rowCount()):
        assert dlg.table.item(row, INCLUDE).checkState() != Qt.Checked
    assert dlg.chosen() == []


# ---------------------------------------------------------------------------
# a proposal that would affect nothing
# ---------------------------------------------------------------------------
def _true_rows(conn, symbol) -> int:
    """The number of stored rows carrying `symbol`, counted independently of
    securities.usage_counts so the Rows column is checked against the file
    rather than against the function that fills it."""
    total = 0
    for table in securities.SYMBOL_TABLES:
        total += int(conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE symbol=?", (symbol,)).fetchone()[0])
    return total


def test_a_catalog_only_symbol_is_not_offered_as_a_change(conn, world):
    """The user's report: "I'm seeing a row that says 0 description recorded.
    Why would you propose something if it affects 0 items?"

    A securities catalog row that outlived its transactions has no rows in any
    SYMBOL_TABLES table, so applying anything for it would write a catalog name
    and move nothing. It may be listed -- it is part of what the file knows --
    but never ticked, never tickable, and never described as a change."""
    conn.execute("INSERT INTO securities(symbol, name) VALUES(?,?)",
                 ("ZZORPHAN ZZ ORPHAN CORP", None))
    conn.commit()
    assert _true_rows(conn, "ZZORPHAN ZZ ORPHAN CORP") == 0

    dlg = SecuritiesDialog(conn)
    row = _row_for(dlg, "ZZORPHAN ZZ ORPHAN CORP")
    split = dlg._splits[row]

    assert not split.actionable
    assert split.symbol == split.old and split.name is None   # nothing to apply
    assert dlg.table.item(row, ROWS).text() == "0"
    tick = dlg.table.item(row, INCLUDE)
    assert tick.checkState() == Qt.Unchecked
    assert not tick.flags() & Qt.ItemIsUserCheckable
    status = dlg.table.item(row, STATUS).text()
    assert "catalog" in status and "nothing to change" in status
    # ... and it does not travel through chosen(), nor get counted as an option
    assert [s for s in dlg.chosen() if s.old == "ZZORPHAN ZZ ORPHAN CORP"] == []
    assert "option contract" not in dlg.tally.text()


def test_every_zero_row_symbol_is_left_alone(conn, world):
    """The invariant behind the fix, over the whole list: the Rows column and
    the tick agree, because both read the same counts."""
    conn.execute("INSERT INTO securities(symbol, name) VALUES(?,?)",
                 ("ZZORPHAN ZZ ORPHAN CORP", None))
    conn.commit()
    dlg = SecuritiesDialog(conn)
    for row, split in enumerate(dlg._splits):
        if dlg.table.item(row, ROWS).text() == "0":
            assert not split.actionable, f"{split.old!r} proposed but moves nothing"
            assert dlg.table.item(row, INCLUDE).checkState() == Qt.Unchecked


def test_the_rows_column_counts_the_rows_the_symbol_actually_has(conn, world):
    """Rows must be the TRUE number of stored rows carrying the stored spelling
    -- the set apply_splits would move -- for a row that re-keys and for one
    whose identity differs from its spelling only by letter case. A count read
    under any other key (the proposed identity, a folded spelling) reads 0 or
    reads another security's rows, and the number the user approves is a
    fiction."""
    _buy(conn, world["ib"], "zzta", date="2020-02-02")
    _buy(conn, world["ib"], "ZZTA", date="2020-03-03")
    investments.rebuild_holdings(conn, world["ib"])
    dlg = SecuritiesDialog(conn)

    rekeyed = _row_for(dlg, "VGT VANGUARD INFO TECH ETF")
    assert dlg._splits[rekeyed].changes_key                  # it does re-key
    assert dlg.table.item(rekeyed, ROWS).text() == \
        f"{_true_rows(conn, 'VGT VANGUARD INFO TECH ETF'):,}"

    for spelling in ("zzta", "ZZTA"):
        row = _row_for(dlg, spelling)
        truth = _true_rows(conn, spelling)
        assert truth > 0
        assert dlg.table.item(row, ROWS).text() == f"{truth:,}"
