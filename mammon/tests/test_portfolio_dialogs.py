"""The portfolio windows (roadmap item 7): Lots, Capital Gains, Performance,
Allocation and Specify Lots, plus the investment register's entries to them."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from mammon import db, investments, ledger, portfolio
from mammon.ui import prefs
from mammon.ui.portfolio_dialogs import (AllocationDialog, CapitalGainsDialog,
                                         LotsDialog, PerformanceDialog,
                                         SpecifyLotsDialog)
from mammon.ui.widgets import InvestmentRegisterWidget
from mammon.tests import fresh_db


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    """The Allocation window remembers its scope in QSettings; keep the tests
    out of the real one."""
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "pdialogs.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=50000_00)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ledger.create_transfer(conn, chk, inv, "2024-01-02", 20000_00, payee="Fund brokerage")
    investments.set_lot_method(conn, inv, "fifo")
    b1 = investments.record_investment(conn, inv, "2024-01-05", "Buy", symbol="AAPL",
                                       quantity="10", price="100.00", amount=-1000_00)
    b2 = investments.record_investment(conn, inv, "2024-06-05", "Buy", symbol="AAPL",
                                       quantity="10", price="120.00", amount=-1200_00)
    s = investments.record_investment(conn, inv, "2025-03-05", "Sell", symbol="AAPL",
                                      quantity="5", price="130.00", amount=650_00)
    investments.record_investment(conn, inv, "2025-06-01", "Div", symbol="AAPL", amount=40_00)
    investments.record_price(conn, "AAPL", "2025-12-31", "140.00")
    investments.rebuild_holdings(conn, inv)
    return {"chk": chk, "inv": inv, "b1": b1, "b2": b2, "sale": s}


def test_lots_window_lists_open_lots_and_filters_by_security(qapp, conn, world):
    dlg = LotsDialog(conn, world["inv"], as_of="2025-12-31")
    assert "First in, first out" in dlg.method_label.text()
    t = dlg.table
    assert t.rowCount() == 2
    assert [t.item(r, 2).text() for r in range(2)] == ["5", "10"]          # FIFO left 5 + 10
    assert [t.item(r, 7).text() for r in range(2)] == ["Long", "Long"]
    assert t.item(0, 6).text() == "$200.00" and t.item(1, 6).text() == "$200.00"
    assert "2 lots" in dlg.footer.text() and "$400.00" in dlg.footer.text()
    dlg.symbol.setCurrentIndex(dlg.symbol.findData("AAPL"))
    assert dlg.table.rowCount() == 2
    dlg.deleteLater()


def test_capital_gains_window_follows_the_year(qapp, conn, world):
    dlg = CapitalGainsDialog(conn, world["inv"], year=2025)
    t = dlg.table
    assert t.rowCount() == 1
    assert [t.item(0, c).text() for c in (0, 3, 4, 5, 6, 7)] == \
        ["AAPL", "5", "$650.00", "$500.00", "$150.00", "Long"]
    assert "Long-term $150.00 (1 lot)" in dlg.footer.text()
    dlg.year.setValue(2024)
    assert dlg.table.rowCount() == 0 and "Total proceeds $0.00" in dlg.footer.text()
    dlg.deleteLater()


def test_performance_window_reports_the_rate(qapp, conn, world):
    dlg = PerformanceDialog(conn, world["inv"], start="2025-01-01", end="2025-12-31")
    p = dlg.performance
    # Start: 20 shares valued at ... no price before 2025-12-31 -> unpriced start.
    assert p is not None and p.money_in == 0 and p.income == 40_00
    assert "per year" in dlg.labels["irr"].text() or "n/a" in dlg.labels["irr"].text()
    dlg.symbol.setCurrentIndex(dlg.symbol.findData("AAPL"))
    assert dlg.performance.symbol == "AAPL"
    assert dlg.labels["money_out"].text() == "$690.00"        # sale proceeds + cash dividend
    dlg.deleteLater()


def test_allocation_window_classifies_in_place(qapp, conn, world):
    prefs.set_allocation_scope("investments")
    dlg = AllocationDialog(conn, as_of="2025-12-31")
    ct = dlg.class_table
    labels = [ct.item(r, 0).text() for r in range(ct.rowCount())]
    assert labels == ["Cash", "Unclassified"]                   # 18,450 cash vs 2,100 AAPL
    st = dlg.sec_table
    assert st.item(0, 0).text() == "AAPL"
    combo = st.cellWidget(0, 1)
    combo.setCurrentIndex(combo.findData("domestic_stock"))
    assert portfolio.get_security(conn, "AAPL")["asset_class"] == "domestic_stock"
    labels = [ct.item(r, 0).text() for r in range(ct.rowCount())]
    assert labels == ["Cash", "Domestic stock"]
    assert "Total" in dlg.footer.text()
    dlg.deleteLater()


def test_allocation_window_scope_covers_property_and_remembers_it(qapp, conn, world):
    """The scope picker reaches the house, the house is classified on the By
    account tab, and the choice survives into the next window."""
    house = ledger.create_account(conn, "House", "asset", opening_balance=300000_00)
    ledger.create_account(conn, "Mortgage", "liability", opening_balance=-200000_00)
    prefs.set_allocation_scope("investments")
    dlg = AllocationDialog(conn, as_of="2025-12-31")
    assert dlg.scope_row.isVisibleTo(dlg)
    assert [dlg.acct_table.item(r, 0).text() for r in range(dlg.acct_table.rowCount())] \
        == ["Brokerage"]
    assert dlg.acct_table.item(0, 1).text() == dlg.BY_SECURITY_NOTE   # classed by security

    dlg.scope_combo.setCurrentIndex(dlg.scope_combo.findData("everything"))
    names = [dlg.acct_table.item(r, 0).text() for r in range(dlg.acct_table.rowCount())]
    assert names == ["House", "Checking", "Brokerage"]           # largest first, no Mortgage
    assert "Unclassified" in [dlg.class_table.item(r, 0).text()
                              for r in range(dlg.class_table.rowCount())]
    combo = dlg.acct_table.cellWidget(0, 1)                      # the House row
    combo.setCurrentIndex(combo.findData("real_estate"))
    assert ledger.get_account(conn, house)["asset_class"] == "real_estate"
    classes = {dlg.class_table.item(r, 0).text(): dlg.class_table.item(r, 1).text()
               for r in range(dlg.class_table.rowCount())}
    # The house is now real estate; only the unclassified SECURITY is left over.
    assert classes["Real estate"] == "$300,000.00" and classes["Unclassified"] == "$2,100.00"
    dlg.deleteLater()
    # Remembered: the next window opens on the same scope.
    again = AllocationDialog(conn, as_of="2025-12-31")
    assert again.scope == "everything" and prefs.allocation_scope() == "everything"
    # A caller that names the accounts owns the scope, so the picker is hidden.
    named = AllocationDialog(conn, as_of="2025-12-31", account_ids=[world["inv"]])
    assert not named.scope_row.isVisibleTo(named)
    assert [s.label for s in named.allocation.by_account] == ["Brokerage"]
    again.deleteLater()
    named.deleteLater()


def test_allocation_window_expands_other_and_offers_a_way_back(qapp, conn, world):
    """The Back button appears only while the pie is drilled into Other."""
    ledger.create_account(conn, "House", "asset", opening_balance=300000_00)
    gold = ledger.create_account(conn, "Gold bar", "asset", opening_balance=1000_00)
    portfolio.set_account_asset_class(conn, gold, "other")
    prefs.set_allocation_scope("everything")
    portfolio.set_security(conn, "AAPL", asset_class="domestic_stock")
    dlg = AllocationDialog(conn, as_of="2025-12-31")
    if dlg._chart is None:                       # matplotlib absent: nothing to test
        pytest.skip("matplotlib not installed")
    assert not dlg.back_btn.isVisibleTo(dlg)
    # The house dwarfs everything, so the lowest classes that together reach 10%
    # of the total -- Cash, Domestic stock and the "Other" class -- all roll into
    # one Other wedge (drawn once, not twice under the same name, even though a
    # real class is literally called "Other").
    assert dlg._chart.has_group()
    assert [lab for lab, _ in dlg._chart.grouped_members()] == \
        ["Cash", "Domestic stock", "Other"]                 # largest first
    assert [lab for lab, _ in dlg._chart.drawn_slices()].count("Other") == 1
    dlg._chart.drill_into_other()
    assert dlg.back_btn.isVisibleTo(dlg)
    dlg.back_btn.click()
    assert not dlg.back_btn.isVisibleTo(dlg) and dlg._chart.zoom_path() == []
    # Switching tabs draws a fresh chart, which is always at the top level.
    dlg._chart.drill_into_other()
    dlg.tabs.setCurrentIndex(1)
    assert dlg._chart.zoom_path() == [] and not dlg.back_btn.isVisibleTo(dlg)
    dlg.deleteLater()


def test_allocation_pie_follows_the_tab(qapp, conn, world):
    """One chart, three groupings: the pie answers whatever the table in front
    of it is answering."""
    prefs.set_allocation_scope("investments")
    dlg = AllocationDialog(conn, as_of="2025-12-31")
    portfolio.set_security(conn, "AAPL", asset_class="domestic_stock")
    dlg.reload()
    assert dlg.chart_slices() == [("Cash", 18490_00), ("Domestic stock", 2100_00)]
    dlg.tabs.setCurrentIndex(1)
    assert dlg.chart_slices() == [("AAPL", 2100_00)]             # cash is not a security
    dlg.tabs.setCurrentIndex(2)
    assert dlg.chart_slices() == [("Brokerage", 20590_00)]
    if dlg._chart is not None:                                   # matplotlib present
        assert dlg._chart is not None
    dlg.deleteLater()


def test_specify_lots_saves_through_the_domain_and_reports_bad_input(qapp, conn, world):
    inv, sale, b1, b2 = world["inv"], world["sale"], world["b1"], world["b2"]
    dlg = SpecifyLotsDialog(conn, sale)
    assert dlg.table.rowCount() == 2
    assert "5 shares" in dlg.footer.text() or "of 5" in dlg.footer.text()
    dlg.edits[1].setText("5")                                    # all from the June lot
    assert "Assigned 5 of 5" in dlg.footer.text()
    dlg.accept()
    assert dlg.result() == dlg.Accepted
    assert investments.lot_assignments_for(conn, sale) == [(b2, "5")]
    investments.rebuild_holdings(conn, inv)
    [g] = portfolio.capital_gains(conn, inv)
    assert (g.acquired, g.basis, g.term) == ("2024-06-05", 600_00, "short")
    # Too many shares: refused through the seam, nothing changed.
    dlg2 = SpecifyLotsDialog(conn, sale)
    dlg2.edits[0].setText("9")
    warned = []
    dlg2._warn = warned.append
    dlg2.accept()
    assert warned and "more shares" in warned[0]
    assert investments.lot_assignments_for(conn, sale) == [(b2, "5")]
    dlg.deleteLater()
    dlg2.deleteLater()


def test_investment_register_offers_the_portfolio_windows(qapp, conn, world):
    w = InvestmentRegisterWidget(conn, world["inv"])
    texts = [a.text() for a in w.gear_menu.actions()]
    for name in ("Lots…", "Capital Gains…", "Performance…", "Allocation…"):
        assert name in texts
    w.deleteLater()


def test_account_details_offers_the_cost_basis_method_for_investment_accounts(
        qapp, conn, world):
    from mammon.ui.widgets import AccountDetailsDialog
    inv = ledger.get_account(conn, world["inv"])
    dlg = AccountDetailsDialog(inv)
    assert dlg.lot_method.currentData() == "fifo" and not dlg.lot_method.isHidden()
    dlg.lot_method.setCurrentIndex(dlg.lot_method.findData("lifo"))
    assert dlg.values()["lot_method"] == "lifo"
    chk = ledger.get_account(conn, world["chk"])
    dlg2 = AccountDetailsDialog(chk)
    assert dlg2.lot_method.isHidden() and dlg2.values()["lot_method"] == "average"
    dlg.deleteLater()
    dlg2.deleteLater()


# ---------------------------------------------------------------------------
# fund mixtures (SRD 5.8g)
# ---------------------------------------------------------------------------
class _FakeMixSource:
    """Canned fund compositions, keyed by symbol."""
    source_name = "fake"

    def __init__(self, by_symbol):
        self.by_symbol = dict(by_symbol)

    def get_mixtures(self, symbols):
        from mammon import security_mix
        out = []
        for sym in symbols:
            entry = self.by_symbol.get(sym)
            if entry is not None:
                positions, category = entry
                out.append(security_mix.SecurityMixture(
                    symbol=sym, weights=positions, source=self.source_name,
                    as_of="2025-12-31", category=category))
        return out


def test_allocation_window_fetches_and_shows_a_fund_mixture(qapp, conn, world):
    """A fund's value is split across what it actually holds, and the window
    says so in the Mixture column instead of pretending it is one class."""
    from mammon import security_mix

    prefs.set_allocation_scope("investments")
    portfolio.set_security(conn, "AAPL", asset_class="domestic_stock")
    dlg = AllocationDialog(conn, as_of="2025-12-31")
    messages = []
    dlg._warn = lambda title, text: messages.append(text)
    dlg._mixture_source = lambda: _FakeMixSource(
        {"AAPL": ({"stockPosition": 0.60, "bondPosition": 0.38,
                   "cashPosition": 0.02}, "Target-Date 2030")})
    dlg.fetch_mixtures()

    assert security_mix.get_mixture(conn, "AAPL")["bond"] == Decimal("38")
    st = dlg.sec_table
    assert st.item(0, 0).text() == "AAPL"
    assert "38% Bonds" in st.item(0, 2).text()          # the new Mixture column
    # The value is now split across classes, and nothing was created or lost.
    classes = {dlg.class_table.item(r, 0).text(): dlg.class_table.item(r, 1).text()
               for r in range(dlg.class_table.rowCount())}
    assert classes["Bonds"] == "$798.00"                # 38% of the 2,100 holding
    assert sum(s.value for s in dlg.allocation.by_class) == dlg.allocation.total
    # By security is unaffected: a mixture splits what a holding is made OF.
    assert st.item(0, 3).text() == "$2,100.00"
    assert "1 fund(s) split" in messages[0]
    dlg.deleteLater()


def test_a_fund_with_no_equity_class_is_named_not_guessed(qapp, conn, world):
    """The domestic/international split is the one thing the data cannot say,
    so an unplaced equity slice is reported for the user to decide."""
    prefs.set_allocation_scope("investments")
    dlg = AllocationDialog(conn, as_of="2025-12-31")     # AAPL unclassified
    messages = []
    dlg._warn = lambda title, text: messages.append(text)
    dlg._mixture_source = lambda: _FakeMixSource(
        {"AAPL": ({"stockPosition": 0.97, "cashPosition": 0.03},
                  "Foreign Large Blend")})
    dlg.fetch_mixtures()
    assert "AAPL (looks like International stock)" in messages[0]
    assert "Unclassified" in messages[0]
    dlg.deleteLater()


def test_mixture_fetch_reports_a_backend_failure_instead_of_raising(qapp, conn, world):
    prefs.set_allocation_scope("investments")
    dlg = AllocationDialog(conn, as_of="2025-12-31")
    messages = []
    dlg._warn = lambda title, text: messages.append(text)

    class Exploding:
        def get_mixtures(self, symbols):
            raise RuntimeError("yfinance is not installed")

    dlg._mixture_source = lambda: Exploding()
    dlg.fetch_mixtures()
    assert "yfinance is not installed" in messages[0]
    dlg.deleteLater()
