"""The Target & Drift window (SRD 5.8f): editing a target in place and reading
the drift off it. Modals go through the overridable seams, so nothing here can
block under the offscreen platform."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest
from PyQt5.QtWidgets import QApplication

from mammon import db, investments, ledger, portfolio, rebalance
from mammon.ui.rebalance_dialog import RebalanceDialog

AS_OF = "2026-06-30"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "rebal_ui.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=120_000_00)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
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
    house = ledger.create_account(conn, "House", "asset", opening_balance=300_000_00)
    portfolio.set_account_asset_class(conn, house, "real_estate")
    return {"chk": chk, "inv": inv, "house": house}


def _named(dlg, name):
    """The RebalanceDialog patched to answer its name prompt without a modal."""
    dlg._ask_name = lambda: name
    return dlg


def test_window_with_no_target_explains_itself(qapp, conn, world):
    dlg = RebalanceDialog(conn, as_of=AS_OF)
    assert dlg.report is None and dlg.table.rowCount() == 0
    assert "No target yet" in dlg.status.text()
    assert not dlg.delete_btn.isEnabled()
    dlg.deleteLater()


def test_new_target_seeds_from_today_and_reads_on_target(qapp, conn, world):
    dlg = _named(RebalanceDialog(conn, as_of=AS_OF), "As held")
    tid = dlg.new_target()
    assert tid is not None
    assert rebalance.target_lines(conn, tid) == {
        "domestic_stock": Decimal("70"), "bond": Decimal("20"), "cash": Decimal("10")}
    # Seeded from what is held, so nothing is out of band on day one.
    assert dlg.report is not None and not dlg.report.needs_rebalance
    assert "On target" in dlg.status.text()
    # The house is named as context, and explicitly not part of the mix.
    assert "Real estate" in dlg.fixed_label.text()
    assert "nothing here can be rebalanced" in dlg.fixed_label.text()
    assert "real_estate" not in [r.asset_class for r in dlg.report.rows]
    dlg.deleteLater()


def test_editing_a_target_percent_redraws_the_drift(qapp, conn, world):
    dlg = _named(RebalanceDialog(conn, as_of=AS_OF), "Mix")
    dlg.new_target()
    # Cash is settled, so lock it: pulling equities down to 50 then lands
    # entirely on bonds (40), the column still totals 100, and equities read 20
    # points overweight, so the row says sell.
    dlg.set_locked("cash", True)
    dlg.set_target_pct("domestic_stock", 50)
    # The numbers are correct at once; the TABLE is rebuilt off the signal
    # stack (see set_target_pct), so the redraw lands on the next turn.
    qapp.processEvents()
    by_class = {r.asset_class: r for r in dlg.report.rows}
    assert by_class["domestic_stock"].drift_pct == Decimal("20")
    assert by_class["domestic_stock"].move_cents == -20_000_00
    row = next(i for i in range(dlg.table.rowCount())
               if dlg.table.item(i, dlg.CLASS).text() == "Domestic stock")
    assert dlg.table.item(row, dlg.DRIFT).text() == "+20.0"
    assert dlg.table.item(row, dlg.MOVE).text() == "Sell $20,000.00"
    bond_row = next(i for i in range(dlg.table.rowCount())
                    if dlg.table.item(i, dlg.CLASS).text() == "Bonds")
    assert dlg.table.item(bond_row, dlg.MOVE).text() == "Buy $20,000.00"
    assert "out of band" in dlg.status.text()
    # The spin box in the Target column carries the stored weight.
    assert dlg.table.cellWidget(row, dlg.TARGET).value() == pytest.approx(50.0)
    dlg.deleteLater()


def test_a_target_that_does_not_total_100_is_shown_not_normalized(qapp, conn, world):
    dlg = _named(RebalanceDialog(conn, as_of=AS_OF), "Partial")
    tid = dlg.new_target()
    # The EDITOR can no longer build a total like this -- it rebalances the
    # unlocked classes -- but a line written straight through the domain (or by
    # a build that predates the locks) still can, and it is SHOWN, not quietly
    # normalized into a plausible, wrong target.
    rebalance.set_line(conn, tid, "cash", 0)         # 70 + 20, totals 90
    dlg.refresh()
    qapp.processEvents()
    assert rebalance.target_total(conn, tid) == Decimal("90")
    assert "adds up to 90.0%" in dlg.status.text()
    assert not dlg.report.target_is_complete
    dlg.deleteLater()


def test_zeroing_a_class_in_the_editor_keeps_the_total_at_100(qapp, conn, world):
    """Zeroing a class drops its target line -- the case that makes the deferred
    rebuild load-bearing rather than theoretical -- and what it gave up is
    spread over the other unlocked classes rather than lost from the total."""
    dlg = _named(RebalanceDialog(conn, as_of=AS_OF), "No cash")
    tid = dlg.new_target()
    dlg.set_target_pct("cash", 0)
    qapp.processEvents()
    lines = rebalance.target_lines(conn, tid)
    assert "cash" not in lines
    assert rebalance.target_total(conn, tid) == Decimal("100")
    dlg.deleteLater()


def test_bands_and_sleeve_are_saved_on_the_target(qapp, conn, world):
    dlg = _named(RebalanceDialog(conn, as_of=AS_OF), "Bands")
    tid = dlg.new_target()
    assert dlg.band_abs.value() == pytest.approx(5.0)
    assert dlg.band_rel.value() == pytest.approx(25.0)
    dlg.band_abs.setValue(2.0)
    stored = rebalance.get_target(conn, tid)
    assert Decimal(stored["band_abs_pct"]) == Decimal("2")
    # A tighter band finds drift the default missed.
    dlg.set_target_pct("domestic_stock", 67)
    dlg.set_target_pct("bond", 23)
    qapp.processEvents()
    assert dlg.report.needs_rebalance

    dlg.sleeve_combo.setCurrentIndex(dlg.sleeve_combo.findData("with_cash"))
    assert rebalance.get_target(conn, tid)["sleeve"] == "with_cash"
    assert dlg.report.sleeve_total == 120_000_00     # the chequing account joins
    dlg.deleteLater()


def test_choosing_a_target_makes_it_the_active_one(qapp, conn, world):
    a = rebalance.create_target(conn, "A", lines={"domestic_stock": 100}, active=True)
    b = rebalance.create_target(conn, "B", lines={"bond": 100})
    dlg = RebalanceDialog(conn, as_of=AS_OF)
    assert dlg.current_target_id() == a
    dlg.target_combo.setCurrentIndex(dlg.target_combo.findData(b))
    assert rebalance.active_target(conn)["id"] == b
    assert dlg.report.target_name == "B"
    dlg.deleteLater()


def test_delete_asks_first_and_promotes_another_target(qapp, conn, world):
    a = rebalance.create_target(conn, "A", lines={"domestic_stock": 100}, active=True)
    rebalance.create_target(conn, "B", lines={"bond": 100})
    dlg = RebalanceDialog(conn, as_of=AS_OF)
    dlg._confirm = lambda *a, **k: False
    dlg.delete_target()
    assert len(rebalance.list_targets(conn)) == 2       # declined: nothing gone
    dlg._confirm = lambda *a, **k: True
    dlg.delete_target()
    remaining = rebalance.list_targets(conn)
    assert len(remaining) == 1 and remaining[0]["name"] == "B"
    # Something is still active, so the window never lands on "no target".
    assert rebalance.active_target(conn)["name"] == "B"
    dlg.deleteLater()


def test_nothing_classified_reports_the_next_step_instead_of_failing(qapp, conn):
    """A brokerage whose securities have no asset class yet: the window says
    what to do rather than raising."""
    chk = ledger.create_account(conn, "C", "checking", opening_balance=50_000_00)
    inv = ledger.create_account(conn, "B", "investment", opening_balance=0)
    ledger.create_transfer(conn, chk, inv, "2026-01-02", 10_000_00, payee="Fund")
    investments.record_investment(conn, inv, "2026-01-03", "Buy", symbol="ZZZ",
                                  quantity="100", price="100", amount=-10_000_00)
    investments.record_price(conn, "ZZZ", "2026-06-30", "100")
    investments.rebuild_holdings(conn, inv)
    dlg = _named(RebalanceDialog(conn, as_of=AS_OF), "Nope")
    warnings = []
    dlg._warn = lambda title, text: warnings.append(text)
    # 'unclassified' is not a settable asset class, so a target cannot be seeded.
    assert dlg.new_target() is None
    assert warnings and "asset class" in warnings[0]
    assert rebalance.list_targets(conn) == []
    dlg.deleteLater()
