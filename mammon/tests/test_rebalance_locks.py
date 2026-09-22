"""Locked target weights and the always-100 edit (SRD 5.8f).

The Target & Drift editor used to let a mix add up to anything; these tests pin
the rule that replaced that: an edit moves the OTHER unlocked classes so the
column totals exactly 100, and a locked class is never touched. All synthetic
data -- one brokerage, two funds, no real institution anywhere.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest
from PyQt5.QtWidgets import QApplication, QCheckBox

from mammon import db, investments, ledger, portfolio, rebalance
from mammon.ui.rebalance_dialog import RebalanceDialog
from mammon.tests import fresh_db, skip_under_xdist

AS_OF = "2026-06-30"
HUNDRED = Decimal("100")

MIX = {"domestic_stock": Decimal("70"), "bond": Decimal("20"),
       "cash": Decimal("10")}


def _total(lines: dict) -> Decimal:
    return sum(lines.values(), Decimal("0"))


# ---------------------------------------------------------------------------
# the pure redistribution rule
# ---------------------------------------------------------------------------
def test_raising_one_class_lowers_the_other_unlocked_ones():
    out = rebalance.apply_target_edit(MIX, set(), "domestic_stock", 80)
    assert out["domestic_stock"] == Decimal("80")
    assert out["bond"] < MIX["bond"] and out["cash"] < MIX["cash"]
    assert _total(out) == HUNDRED


def test_lowering_one_class_raises_the_other_unlocked_ones():
    out = rebalance.apply_target_edit(MIX, set(), "domestic_stock", 40)
    assert out["domestic_stock"] == Decimal("40")
    assert out["bond"] > MIX["bond"] and out["cash"] > MIX["cash"]
    assert _total(out) == HUNDRED


def test_the_remainder_of_an_uneven_split_still_lands_on_exactly_100():
    # 100 split over 90 in the ratio 70:20 does not divide evenly; the leftover
    # hundredth has to go somewhere rather than vanish from the total.
    out = rebalance.apply_target_edit(MIX, set(), "cash", 0)
    assert out["cash"] == Decimal("0")
    assert _total(out) == HUNDRED


def test_a_locked_class_keeps_its_exact_value_across_several_edits():
    lines = dict(MIX)
    locked = {"cash"}
    for pct in (55, 62, 31, 44):
        lines = rebalance.apply_target_edit(lines, locked, "domestic_stock", pct)
        assert lines["cash"] == Decimal("10")
        assert _total(lines) == HUNDRED
    assert lines["domestic_stock"] == Decimal("44")
    assert lines["bond"] == Decimal("46")


def test_the_users_workflow_set_one_lock_it_move_on():
    """All unlocked -> set A and lock it -> set B and lock it -> set C."""
    lines, locked = dict(MIX), set()

    lines = rebalance.apply_target_edit(lines, locked, "domestic_stock", 60)
    locked.add("domestic_stock")
    assert _total(lines) == HUNDRED

    lines = rebalance.apply_target_edit(lines, locked, "bond", 25)
    locked.add("bond")
    assert lines["domestic_stock"] == Decimal("60")
    assert _total(lines) == HUNDRED

    lines = rebalance.apply_target_edit(lines, locked, "cash", 15)
    assert lines["domestic_stock"] == Decimal("60")
    assert lines["bond"] == Decimal("25")
    assert lines["cash"] == Decimal("15")
    assert _total(lines) == HUNDRED


def test_a_recipient_clamped_to_zero_never_goes_negative():
    lines = {"domestic_stock": Decimal("50"), "bond": Decimal("30"),
             "cash": Decimal("20")}
    out = rebalance.apply_target_edit(lines, {"cash"}, "domestic_stock", 80)
    assert out["domestic_stock"] == Decimal("80")     # 100 - 20 locked cash
    assert out["bond"] == Decimal("0")
    assert all(v >= 0 for v in out.values())
    assert _total(out) == HUNDRED


def test_an_edit_beyond_what_the_others_can_absorb_is_clamped():
    lines = {"domestic_stock": Decimal("50"), "bond": Decimal("30"),
             "cash": Decimal("20")}
    out = rebalance.apply_target_edit(lines, {"cash"}, "domestic_stock", 95)
    assert out["domestic_stock"] == Decimal("80")     # not 95: cash is locked
    assert out["cash"] == Decimal("20")
    assert _total(out) == HUNDRED


def test_an_edit_with_no_unlocked_recipient_is_clamped_not_honoured():
    lines = {"domestic_stock": Decimal("60"), "bond": Decimal("40")}
    out = rebalance.apply_target_edit(lines, {"domestic_stock"}, "bond", 10)
    assert out["domestic_stock"] == Decimal("60")
    assert out["bond"] == Decimal("40")               # the only free weight
    assert _total(out) == HUNDRED


def test_recipients_all_at_zero_share_what_comes_back_evenly():
    lines = {"domestic_stock": Decimal("100"), "bond": Decimal("0"),
             "cash": Decimal("0")}
    out = rebalance.apply_target_edit(lines, set(), "domestic_stock", 50)
    assert out["bond"] == Decimal("25") and out["cash"] == Decimal("25")
    assert _total(out) == HUNDRED


def test_a_negative_or_oversized_entry_is_clamped_into_range():
    assert rebalance.apply_target_edit(MIX, set(), "bond", -5)["bond"] == 0
    out = rebalance.apply_target_edit(MIX, set(), "bond", 140)
    assert out["bond"] == HUNDRED and _total(out) == HUNDRED


# ---------------------------------------------------------------------------
# the stored lock
# ---------------------------------------------------------------------------
@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "locks.db")
    yield c
    c.close()


def test_lock_state_round_trips_through_the_database(tmp_path):
    path = tmp_path / "roundtrip.db"
    c = fresh_db(path)
    tid = rebalance.create_target(c, "Mix", lines=dict(MIX), active=True)
    rebalance.set_locked(c, tid, "cash", True)
    assert rebalance.locked_classes(c, tid) == {"cash"}
    c.close()

    c2 = fresh_db(path)
    assert rebalance.locked_classes(c2, tid) == {"cash"}
    assert rebalance.is_locked(c2, tid, "cash")
    assert not rebalance.is_locked(c2, tid, "bond")
    rebalance.set_locked(c2, tid, "cash", False)
    assert rebalance.locked_classes(c2, tid) == set()
    c2.close()


def test_set_line_balanced_honours_the_stored_locks(conn):
    tid = rebalance.create_target(conn, "Mix", lines=dict(MIX), active=True)
    rebalance.set_locked(conn, tid, "cash", True)
    rebalance.set_line_balanced(conn, tid, "domestic_stock", 50)
    lines = rebalance.target_lines(conn, tid)
    assert lines["cash"] == Decimal("10")
    assert lines["domestic_stock"] == Decimal("50")
    assert rebalance.target_total(conn, tid) == HUNDRED


def test_a_locked_line_driven_to_zero_keeps_its_row_and_its_lock(conn):
    tid = rebalance.create_target(conn, "Mix", lines=dict(MIX), active=True)
    rebalance.set_locked(conn, tid, "cash", True)
    rebalance.set_line(conn, tid, "cash", 0)
    assert rebalance.target_lines(conn, tid)["cash"] == Decimal("0")
    assert rebalance.is_locked(conn, tid, "cash")


# ---------------------------------------------------------------------------
# the dialog, offscreen
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking",
                                opening_balance=120_000_00)
    inv = ledger.create_account(conn, "Brokerage", "investment",
                                opening_balance=0)
    ledger.create_transfer(conn, chk, inv, "2026-01-02", 100_000_00, payee="Fund")
    investments.record_investment(conn, inv, "2026-01-03", "Buy", symbol="VTI",
                                  quantity="700", price="100", amount=-70_000_00)
    investments.record_investment(conn, inv, "2026-01-03", "Buy", symbol="BND",
                                  quantity="200", price="100", amount=-20_000_00)
    for sym in ("VTI", "BND"):
        investments.record_price(conn, sym, "2026-06-30", "100")
    investments.rebuild_holdings(conn, inv)
    portfolio.set_security(conn, "VTI", asset_class="domestic_stock")
    portfolio.set_security(conn, "BND", asset_class="bond")
    return {"chk": chk, "inv": inv}


def _row_for(dlg, label: str):
    """The class row named ``label``, as a QTreeWidgetItem."""
    return next(dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())
                if dlg.tree.topLevelItem(i).text(dlg.CLASS) == label)


@skip_under_xdist(
    "drives a modeless Qt dialog through processEvents while its widgets are rebuilt; crashes a worker under -n auto, passes serially")
def test_the_dialog_locks_a_class_and_keeps_the_column_at_100(qapp, conn, world):
    dlg = RebalanceDialog(conn, as_of=AS_OF)
    dlg._ask_name = lambda: "Mix"
    tid = dlg.new_target()
    assert rebalance.target_total(conn, tid) == HUNDRED

    # Lock cash through the checkbox the user actually clicks.
    cash_row = _row_for(dlg, "Cash")
    box = dlg.tree.itemWidget(cash_row, dlg.LOCK).findChild(QCheckBox)
    assert box is not None and not box.isChecked()
    box.setChecked(True)
    qapp.processEvents()
    assert rebalance.locked_classes(conn, tid) == {"cash"}

    # A locked weight is not editable in place, and the unlocked one is.
    cash_row = _row_for(dlg, "Cash")
    assert not dlg.tree.itemWidget(cash_row, dlg.TARGET).isEnabled()
    stock_row = _row_for(dlg, "Domestic stock")
    spin = dlg.tree.itemWidget(stock_row, dlg.TARGET)
    assert spin.isEnabled()
    spin.setValue(50.0)
    qapp.processEvents()

    lines = rebalance.target_lines(conn, tid)
    assert lines["cash"] == Decimal("10")             # locked, untouched
    assert lines["domestic_stock"] == Decimal("50")
    assert lines["bond"] == Decimal("40")
    assert rebalance.target_total(conn, tid) == HUNDRED
    # And the redrawn table shows the recomputed numbers, not the stale ones.
    assert dlg.tree.itemWidget(_row_for(dlg, "Bonds"), dlg.TARGET).value() == \
        pytest.approx(40.0)
    dlg.deleteLater()
