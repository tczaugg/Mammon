"""The classification review, end to end: audit, confirm, write, and nothing else.

This is the life-cycle test for the safe half of the instrument backfill. A
synthetic ledger carries stocks and option contracts with real positions, lots
and transactions behind them; the audit proposes; a person confirms some rows in
bulk and corrects one by hand; the write lands.

The assertion that matters is the negative one. Recording what a security IS
must not be able to change WHICH security it is, so every identity column and
every holding, lot assignment and transaction row is snapshotted before the
apply and compared after it. The destructive neighbour -- the old merge that
rekeys a symbol and deletes the losing row -- is exactly what this screen must
not become, and a classification that quietly rewrote a symbol would take a
40-year history's cost basis with it.

Reclassification is also reversible: a row set back to "(not classified)" clears
its kind and its terms, which is why the screen can be used without ceremony.

No modal is ever shown: the confirmation goes through ``QMessageBox.question``
and the write through the ``_apply`` seam. All data here is synthetic.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from mammon import db, investments, ledger
from mammon.ui.securities_dialog import (
    SecurityKindDialog, K_INCLUDE, K_KIND, K_SYMBOL, K_TERMS, UNCLASSIFIED,
)
from mammon.tests import fresh_db

CALL = "XYZ 260117C00150000 XYZ 17JAN26 150 C"
PUT = "XYZ 260117P00120000 XYZ 17JAN26 120 P"


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "kindreview.db")
    yield c
    c.close()


def _sec(conn, symbol, name=None, sec_type=None, ticker=None):
    """A securities row as a pre-classification import left it: kind NULL.

    Inserted directly rather than through ``record_master``, which classifies
    option rows itself -- a file that arrives already classified has nothing to
    review, and the review is what is under test.
    """
    conn.execute(
        "INSERT INTO securities(symbol, name, sec_type, ticker) VALUES (?,?,?,?)",
        (symbol, name, sec_type, ticker))
    conn.commit()


@pytest.fixture
def world(conn):
    """One brokerage account holding stock, two contracts on that stock, a sweep
    fund the classifier reads as a share, and one row already fused."""
    acct = ledger.create_account(conn, "Brokerage", "investment",
                                 opening_balance=0)
    _sec(conn, "XYZ", "XYZ INC", "Stock")
    # A contract filed under its underlying's ticker: the shape the old merge
    # would have absorbed into the stock.
    _sec(conn, CALL, None, "Option", ticker="XYZ")
    _sec(conn, PUT, None, "Option")
    _sec(conn, "MMKT", "CASH RESERVES", "Stock")
    _sec(conn, "VGT VANGUARD INFO TECH ETF", "VANGUARD INFO TECH ETF", "ETF",
         ticker="VGT")
    # Already fused: the identity is a stock, the description is a contract.
    _sec(conn, "QRS", CALL, "Stock")

    b1 = investments.record_investment(conn, acct, "2024-01-05", "Buy",
                                       symbol="XYZ", quantity="10",
                                       price="100.00", amount=-1000_00)
    b2 = investments.record_investment(conn, acct, "2024-06-05", "Buy",
                                       symbol="XYZ", quantity="10",
                                       price="120.00", amount=-1200_00)
    sale = investments.record_investment(conn, acct, "2025-03-05", "Sell",
                                         symbol="XYZ", quantity="5",
                                         price="130.00", amount=650_00)
    investments.assign_lots(conn, sale, [(b2, "3"), (b1, "2")])
    investments.record_investment(conn, acct, "2025-04-01", "Buy", symbol=CALL,
                                  quantity="2", price="3.40", amount=-680_00)
    investments.record_investment(conn, acct, "2025-04-01", "Buy", symbol=PUT,
                                  quantity="1", price="2.10", amount=-210_00)
    investments.record_investment(conn, acct, "2025-04-02", "Buy", symbol="MMKT",
                                  quantity="500", price="1.00", amount=-500_00)
    investments.rebuild_holdings(conn, acct)
    return acct


def _yes(monkeypatch, seen=None):
    def question(*a, **k):
        if seen is not None:
            seen.append(a[2])
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: 0))


def _row_for(dlg, symbol):
    for r, row in enumerate(dlg._rows):
        if row.symbol == symbol:
            return r
    raise AssertionError(f"{symbol!r} not listed")


def _pick(dlg, symbol, kind):
    """The individual exception: correct one row in its own drop-down."""
    row = _row_for(dlg, symbol)
    combo = dlg._combos[row]
    index = combo.findData(kind)
    assert index >= 0, f"no {kind!r} in the drop-down"
    combo.setCurrentIndex(index)
    return row


def _ticked(dlg, symbol):
    return dlg.table.item(_row_for(dlg, symbol),
                          K_INCLUDE).checkState() == Qt.Checked


def _kinds(conn):
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT symbol, kind, kind_source FROM securities")}


def _terms(conn, symbol):
    return tuple(conn.execute(
        "SELECT multiplier, underlying, expiration, strike, option_right "
        "FROM securities WHERE symbol=?", (symbol,)).fetchone())


IDENTITY = ("SELECT symbol, name, sec_type, asset_class, ticker FROM securities"
            " ORDER BY symbol")


def _snapshot(conn):
    """Everything a classification must not be able to touch."""
    return {
        "identity": [tuple(r) for r in conn.execute(IDENTITY)],
        "holdings": [tuple(r) for r in conn.execute(
            "SELECT * FROM holdings ORDER BY account_id, symbol")],
        "lot_assignments": [tuple(r) for r in conn.execute(
            "SELECT * FROM lot_assignments ORDER BY id")],
        "investment_transactions": [tuple(r) for r in conn.execute(
            "SELECT * FROM investment_transactions ORDER BY id")],
        "transactions": [tuple(r) for r in conn.execute(
            "SELECT * FROM transactions ORDER BY id")],
    }


# ---------------------------------------------------------------------------
# the life cycle
# ---------------------------------------------------------------------------
def test_a_bulk_confirmation_and_one_exception_classify_the_file(
        conn, world, monkeypatch):
    """The whole workflow in one pass: tick every contract by pattern, correct
    the sweep fund by hand, apply once."""
    seen = []
    _yes(monkeypatch, seen)
    dlg = SecurityKindDialog(conn)
    dlg._set_all(False)                 # "Tick none", then choose deliberately

    # Bulk, by rule: "option" names both contracts and nothing else.
    dlg.pattern.setText("option")
    assert dlg.tick_matching() == 2
    assert _ticked(dlg, CALL) and _ticked(dlg, PUT)
    assert not _ticked(dlg, "XYZ")

    # Exception, by hand: the classifier reads MMKT/CASH RESERVES as a share.
    row = _pick(dlg, "MMKT", "money_market")
    assert dlg.table.item(row, K_INCLUDE).checkState() == Qt.Checked, \
        "correcting a row is a decision; it should not need a second tick"

    before = _snapshot(conn)
    dlg.on_apply()

    kinds = _kinds(conn)
    assert kinds[CALL] == ("option", "user")
    assert kinds[PUT] == ("option", "user")
    assert kinds["MMKT"] == ("money_market", "user")
    # Untouched rows stay unclassified -- nothing is applied by being listed.
    assert kinds["XYZ"] == (None, None)
    assert kinds["QRS"] == (None, None)
    assert kinds["VGT VANGUARD INFO TECH ETF"] == (None, None)

    assert _terms(conn, CALL) == ("100", "XYZ", "2026-01-17", "150", "C")
    assert _terms(conn, PUT) == ("100", "XYZ", "2026-01-17", "120", "P")
    assert _terms(conn, "MMKT") == (None,) * 5

    assert _snapshot(conn) == before
    assert seen, "the write went ahead without asking"


def test_no_identity_holding_lot_or_transaction_moves(conn, world, monkeypatch):
    """Said separately from the happy path because it is the point: this screen
    is not allowed to become the merge that rekeys a symbol."""
    _yes(monkeypatch)
    dlg = SecurityKindDialog(conn)
    dlg.pattern.setText("*")            # everything the screen can reach
    dlg.tick_matching()
    before = _snapshot(conn)

    dlg.on_apply()

    after = _snapshot(conn)
    for table, rows in before.items():
        assert after[table] == rows, f"{table} changed"
    # And every security still exists: a classification never removes a row.
    assert conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0] == 6
    # the two hand-specified lots behind the sale are still assigned
    assert conn.execute(
        "SELECT COUNT(*) FROM lot_assignments").fetchone()[0] == 2


def test_the_contract_filed_under_the_stock_keeps_its_ticker(conn, world,
                                                             monkeypatch):
    """Classifying the flagged contract records what it IS and leaves the wrong
    ticker exactly where it was -- un-fusing is a separate, later operation."""
    _yes(monkeypatch)
    dlg = SecurityKindDialog(conn)
    dlg.pattern.setText("option")
    dlg.tick_matching()
    dlg.on_apply()
    assert conn.execute("SELECT ticker FROM securities WHERE symbol=?",
                        (CALL,)).fetchone()[0] == "XYZ"


# ---------------------------------------------------------------------------
# what the screen refuses to do on its own
# ---------------------------------------------------------------------------
def test_a_proposal_arrives_ticked_but_a_flagged_row_does_not(conn, world):
    """The screen opens on its own proposals -- 900 securities do not get ticked
    one at a time -- but never pre-ticks a row the audit calls suspect."""
    dlg = SecurityKindDialog(conn)
    assert _ticked(dlg, "XYZ") and _ticked(dlg, "MMKT")
    assert not _ticked(dlg, CALL) and not _ticked(dlg, "QRS")


def test_suspect_rows_are_not_ticked_by_the_blanket_button(conn, world):
    dlg = SecurityKindDialog(conn)
    dlg._set_all(True)
    assert _ticked(dlg, "XYZ")
    assert not _ticked(dlg, "QRS"), "a fused row is a question, not a proposal"
    assert not _ticked(dlg, CALL), "a contract under its stock's ticker needs a look"


def test_a_fused_row_proposes_nothing_and_stays_as_it_is(conn, world,
                                                         monkeypatch):
    """One row that is two instruments is not fixed by relabelling it."""
    _yes(monkeypatch)
    dlg = SecurityKindDialog(conn)
    assert dlg.audit.by_symbol("QRS").fused
    dlg._set_all(True)
    dlg.on_apply()
    assert _kinds(conn)["QRS"] == (None, None)


def test_the_confirmation_names_the_flagged_rows(conn, world, monkeypatch):
    seen = []
    _yes(monkeypatch, seen)
    dlg = SecurityKindDialog(conn)
    dlg.pattern.setText("option")
    dlg.tick_matching()
    dlg.on_apply()
    [text] = seen
    assert CALL in text
    assert "No symbol, ticker, holding or transaction is changed." in text


def test_nothing_selected_writes_nothing(conn, world, monkeypatch):
    _yes(monkeypatch)
    dlg = SecurityKindDialog(conn)
    dlg._set_all(False)
    before = _snapshot(conn)
    dlg.on_apply()
    assert _snapshot(conn) == before
    assert all(k == (None, None) for k in _kinds(conn).values())


def test_every_write_goes_through_the_one_seam(conn, world, monkeypatch):
    """If the dialog ever wrote around ``securities.set_kinds``, this passes a
    no-op seam and still finds the database changed."""
    _yes(monkeypatch)

    class Deaf(SecurityKindDialog):
        def _apply(self, updates):
            self.seen = list(updates)
            return 0

    dlg = Deaf(conn)
    dlg._set_all(False)
    dlg.pattern.setText("option")
    dlg.tick_matching()
    dlg.on_apply()
    assert sorted(u.symbol for u in dlg.seen) == sorted([CALL, PUT])
    assert all(k == (None, None) for k in _kinds(conn).values())


# ---------------------------------------------------------------------------
# reversibility
# ---------------------------------------------------------------------------
def test_setting_a_row_back_to_unclassified_clears_its_terms(conn, world,
                                                             monkeypatch):
    """Additive and reversible: the undo for a wrong confirmation is the screen
    itself, and a term must not outlive the kind it described."""
    _yes(monkeypatch)
    dlg = SecurityKindDialog(conn)
    dlg.pattern.setText("option")
    dlg.tick_matching()
    dlg.on_apply()
    assert _terms(conn, PUT)[3] == "120"

    dlg = SecurityKindDialog(conn)
    _pick(dlg, PUT, None)
    dlg.on_apply()
    assert _kinds(conn)[PUT] == (None, None)
    assert _terms(conn, PUT) == (None,) * 5


def test_overriding_a_contract_to_a_share_leaves_no_strike_behind(conn, world,
                                                                  monkeypatch):
    """A strike on something called an equity is how a stock ends up with an
    expiration date. Terms follow the kind the report parsed, or not at all."""
    _yes(monkeypatch)
    dlg = SecurityKindDialog(conn)
    _pick(dlg, PUT, "equity")
    dlg.on_apply()
    assert _kinds(conn)[PUT] == ("equity", "user")
    assert _terms(conn, PUT) == (None,) * 5


# ---------------------------------------------------------------------------
# what the screen shows
# ---------------------------------------------------------------------------
def test_an_unclassified_row_reads_as_unclassified_not_as_a_share(conn, world):
    from mammon.ui.securities_dialog import K_CURRENT
    dlg = SecurityKindDialog(conn)
    assert dlg.table.item(_row_for(dlg, "XYZ"), K_CURRENT).text() == UNCLASSIFIED


def test_the_contract_terms_are_shown_before_they_are_confirmed(conn, world):
    dlg = SecurityKindDialog(conn)
    text = dlg.table.item(_row_for(dlg, CALL), K_TERMS).text()
    assert "2026-01-17" in text and "150" in text and "x100" in text
    assert dlg.table.item(_row_for(dlg, "XYZ"), K_TERMS).text() == ""


def test_the_symbol_column_is_not_editable(conn, world):
    """The identity is displayed, never offered for editing -- there is no path
    from this screen to a symbol."""
    dlg = SecurityKindDialog(conn)
    item = dlg.table.item(_row_for(dlg, CALL), K_SYMBOL)
    assert not item.flags() & Qt.ItemIsEditable
    assert dlg.table.cellWidget(_row_for(dlg, CALL), K_KIND) is not None


def _wheel_event(delta=-120):
    """A synthetic one-notch vertical wheel event (down when delta<0)."""
    from PyQt5.QtGui import QWheelEvent
    from PyQt5.QtCore import QPoint, QPointF
    return QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0),
                       QPoint(0, delta), Qt.NoButton, Qt.NoModifier,
                       Qt.NoScrollPhase, False)


def test_the_is_a_combo_ignores_the_mouse_wheel(conn, world):
    """USER UX BUG (2026-09-14): the wheel over an 'Is a' cell silently
    RECLASSIFIED whatever security was under the pointer while the user was
    only scrolling the table. The cell combo must ignore the collapsed wheel
    event so the event propagates and the table scrolls instead."""
    from PyQt5.QtWidgets import QComboBox
    from mammon.ui.delegates import NoWheelComboBox

    dlg = SecurityKindDialog(conn)
    row = _row_for(dlg, CALL)
    combo = dlg.table.cellWidget(row, K_KIND)
    assert isinstance(combo, NoWheelComboBox)
    assert combo.count() >= 3          # Unclassified + the real kinds

    # Baseline: the SAME event moves a stock combo and is accepted -- proving
    # the event is live and the hazard is real.
    plain = QComboBox()
    plain.addItems(["a", "b", "c", "d"])
    plain.setCurrentIndex(1)
    base_ev = _wheel_event(-120)
    plain.wheelEvent(base_ev)
    assert plain.currentIndex() == 2 and base_ev.isAccepted()

    # The dialog's combo is inert in both directions, and the row's proposed
    # kind is untouched -- no silent reclassification.
    before_index, before_kind = combo.currentIndex(), combo.currentData()
    for delta in (-120, 120):
        ev = _wheel_event(delta)
        combo.wheelEvent(ev)
        assert combo.currentIndex() == before_index
        assert combo.currentData() == before_kind
        assert not ev.isAccepted()


def test_a_pattern_never_unticks_what_an_earlier_one_chose(conn, world):
    dlg = SecurityKindDialog(conn)
    dlg._set_all(False)
    dlg.pattern.setText("option")
    dlg.tick_matching()
    dlg.pattern.setText("MMKT")
    dlg.tick_matching()
    assert _ticked(dlg, CALL) and _ticked(dlg, PUT) and _ticked(dlg, "MMKT")
