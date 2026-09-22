"""The per-fund rebalancing window (``mammon/ui/fund_target_window.py``).

All data is SYNTHETIC. The interaction under test is the reported one:
"any account we click on expands to the fund/securities in it ... the targets
are ignored for un-expanded accounts. So when the report starts and all the
accounts are un-expanded, the two bars are identical. Then as they are opened,
the targets take effect, changing the second bar."
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QDoubleSpinBox   # noqa: E402

from mammon import investments, ledger, portfolio, rebalance, security_mix  # noqa: E402
from mammon.ui import fund_target_window as ftw            # noqa: E402
from mammon.ui.asset_allocation import ClassBar            # noqa: E402
from mammon.tests import fresh_db                          # noqa: E402

AS_OF = "2026-06-30"
OPEN = "2020-01-01"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "fund_window.db")
    yield c
    c.close()


def _buy(conn, acct, sym, qty, price):
    investments.record_investment(conn, acct, "2020-01-02", "Buy", symbol=sym,
                                  quantity=Decimal(qty), price=Decimal(price),
                                  amount=int(Decimal(qty) * Decimal(price) * 100))
    investments.rebuild_holdings(conn, acct)
    investments.record_price(conn, sym, AS_OF, Decimal(price))


@pytest.fixture
def world(conn):
    plan = ledger.create_account(conn, "ZZ 401k", "investment",
                                 opening_balance=100_000_00, opening_date=OPEN)
    _buy(conn, plan, "ZZBAL", "700", "100.00")
    _buy(conn, plan, "ZZIDX", "300", "100.00")
    security_mix.set_mixture(conn, "ZZBAL",
                             {"domestic_stock": 70, "bond": 25, "cash": 5})
    portfolio.set_security(conn, "ZZIDX", asset_class="domestic_stock")

    taxable = ledger.create_account(conn, "ZZ Taxable", "investment",
                                    opening_balance=50_000_00, opening_date=OPEN)
    _buy(conn, taxable, "ZZBND", "500", "100.00")
    portfolio.set_security(conn, "ZZBND", asset_class="bond")
    return {"plan": plan, "taxable": taxable}


def _window(conn, qapp):
    win = ftw.FundTargetWindow(conn, as_of=AS_OF)
    qapp.processEvents()
    return win


# --- the list ---------------------------------------------------------------
def test_every_investment_account_is_listed_with_its_own_composition(conn, world,
                                                                     qapp):
    win = _window(conn, qapp)
    try:
        names = [win.tree.topLevelItem(i).text(ftw.COL_NAME)
                 for i in range(win.tree.topLevelItemCount())]
        assert set(names) == {"ZZ 401k", "ZZ Taxable"}
        bar = win.account_bar(world["plan"])
        assert isinstance(bar, ClassBar)
        # $70k of 70/25/5 plus $30k pure domestic: 79 / 17.5 / 3.5.
        weights = bar.weights()
        assert weights["domestic_stock"] == 79_000_00
        assert weights["bond"] == 17_500_00
        assert weights["cash"] == 3_500_00
        assert "79.0%" in bar.describe()
    finally:
        win.deleteLater()


def test_an_account_expands_into_the_funds_it_holds(conn, world, qapp):
    win = _window(conn, qapp)
    try:
        win.expand_account(world["plan"])
        qapp.processEvents()
        top = next(win.tree.topLevelItem(i)
                   for i in range(win.tree.topLevelItemCount())
                   if win.tree.topLevelItem(i).text(ftw.COL_NAME) == "ZZ 401k")
        symbols = {top.child(i).text(ftw.COL_NAME) for i in range(top.childCount())}
        assert symbols == {"ZZBAL", "ZZIDX"}
        # ...each with a target spin box, which is how a weight is entered.
        spin = win.tree.itemWidget(top.child(0), ftw.COL_TARGET)
        assert isinstance(spin, QDoubleSpinBox)
    finally:
        win.deleteLater()


# --- the two bars, which is the reported mechanic ---------------------------
def test_with_everything_closed_the_two_bars_are_identical(conn, world, qapp):
    """Reported: "when the report starts and all the accounts are un-expanded,
    the two bars are identical"."""
    win = _window(conn, qapp)
    try:
        assert win.expanded_accounts() == []
        assert win.current_bar.weights() == win.target_bar.weights()
        assert "none open" in win.target_label.text()
    finally:
        win.deleteLater()


def test_opening_an_account_puts_its_target_into_the_lower_bar(conn, world, qapp):
    """"Then as they are opened, the targets take effect, changing the second
    bar." """
    win = _window(conn, qapp)
    plan = world["plan"]
    try:
        win.expand_account(plan)
        win.set_fund_pct(plan, "ZZBAL", 60)
        win.set_fund_pct(plan, "ZZIDX", 40)
        qapp.processEvents()

        before = win.report().pct(win.current_bar.weights())
        after = win.report().pct(win.target_bar.weights())
        assert win.current_bar.weights() != win.target_bar.weights()
        assert float(after["domestic_stock"]) > float(before["domestic_stock"])
        assert float(after["bond"]) < float(before["bond"])
        # The upper bar never moves: it is what you own today.
        assert sum(win.current_bar.weights().values()) == 150_000_00
        assert sum(win.target_bar.weights().values()) == 150_000_00
        assert "ZZ 401k" in win.target_label.text()
    finally:
        win.deleteLater()


def test_closing_the_account_again_takes_its_target_back_out(conn, world, qapp):
    win = _window(conn, qapp)
    plan = world["plan"]
    try:
        win.expand_account(plan)
        win.set_fund_pct(plan, "ZZBAL", 60)
        win.set_fund_pct(plan, "ZZIDX", 40)
        qapp.processEvents()
        assert win.current_bar.weights() != win.target_bar.weights()

        win.expand_account(plan, False)
        qapp.processEvents()
        assert win.expanded_accounts() == []
        assert win.current_bar.weights() == win.target_bar.weights()
    finally:
        win.deleteLater()


def test_an_account_left_closed_is_not_touched_by_another_accounts_target(
        conn, world, qapp):
    """The taxable account's bonds are in both bars at full size -- it is not
    being rebalanced, and proposing trades there would be a different thing."""
    win = _window(conn, qapp)
    plan = world["plan"]
    try:
        win.expand_account(plan)
        win.set_fund_pct(plan, "ZZBAL", 100)
        qapp.processEvents()
        # ZZBAL at 100% of the 401(k) is 25% bonds of $100k = $25,000, plus the
        # untouched taxable $50,000.
        assert win.target_bar.weights()["bond"] == 25_000_00 + 50_000_00
    finally:
        win.deleteLater()


# --- the fund columns -------------------------------------------------------
def test_a_fund_row_states_current_target_drift_and_the_trade(conn, world, qapp):
    win = _window(conn, qapp)
    plan = world["plan"]
    try:
        win.expand_account(plan)
        win.set_fund_pct(plan, "ZZBAL", 60)
        win.set_fund_pct(plan, "ZZIDX", 40)
        qapp.processEvents()
        current, drift, move = win.fund_row_text(plan, "ZZBAL")
        assert current == "70.0%"
        assert drift == "+10.0 pp"
        assert move == "Sell $10,000.00"
        current, drift, move = win.fund_row_text(plan, "ZZIDX")
        assert current == "30.0%"
        assert drift == "-10.0 pp"
        assert move == "Buy $10,000.00"
    finally:
        win.deleteLater()


def test_a_closed_accounts_funds_show_no_trade(conn, world, qapp):
    """Its target is not in play, so a drift or a buy/sell figure there would
    describe a projection the lower bar is not making."""
    win = _window(conn, qapp)
    plan = world["plan"]
    try:
        win.expand_account(plan)
        win.set_fund_pct(plan, "ZZBAL", 60)
        win.expand_account(plan, False)
        qapp.processEvents()
        _current, drift, move = win.fund_row_text(plan, "ZZBAL")
        assert drift == ""
        assert move == ""
    finally:
        win.deleteLater()


def test_editing_a_weight_updates_the_bar_without_rebuilding_the_tree(
        conn, world, qapp):
    """The spin box must survive its own valueChanged: rebuilding calls
    setItemWidget, which deletes it under a live signal frame -- the
    heap-corruption pattern CLAUDE.md documents."""
    win = _window(conn, qapp)
    plan = world["plan"]
    try:
        win.expand_account(plan)
        qapp.processEvents()
        top = next(win.tree.topLevelItem(i)
                   for i in range(win.tree.topLevelItemCount())
                   if win.tree.topLevelItem(i).text(ftw.COL_NAME) == "ZZ 401k")
        kid = top.child(0)
        spin = win.tree.itemWidget(kid, ftw.COL_TARGET)
        before = dict(win.target_bar.weights())
        spin.setValue(55.0)
        qapp.processEvents()
        # Same widget object, still in the tree.
        assert win.tree.itemWidget(kid, ftw.COL_TARGET) is spin
        assert win.target_bar.weights() != before
    finally:
        win.deleteLater()


# --- the target the window edits -------------------------------------------
def test_a_second_window_edits_the_same_target(conn, world, qapp):
    """Caught by rendering it: create_target does not activate what it makes, so
    looking up the ACTIVE target made a fresh empty one on every open and the
    weights typed last time were silently gone -- the lower bar read 67% cash."""
    first = _window(conn, qapp)
    plan = world["plan"]
    try:
        first.expand_account(plan)
        first.set_fund_pct(plan, "ZZBAL", 60)
        first.set_fund_pct(plan, "ZZIDX", 40)
        tid = first.target_id()
    finally:
        first.deleteLater()

    second = _window(conn, qapp)
    try:
        assert second.target_id() == tid
        assert rebalance.fund_lines(conn, tid) == {
            (plan, "ZZBAL"): Decimal("60"), (plan, "ZZIDX"): Decimal("40")}
        second.expand_account(plan)
        qapp.processEvents()
        assert second.fund_row_text(plan, "ZZBAL")[2] == "Sell $10,000.00"
    finally:
        second.deleteLater()


def test_weights_short_of_a_hundred_are_called_out(conn, world, qapp):
    win = _window(conn, qapp)
    plan = world["plan"]
    try:
        win.expand_account(plan)
        win.set_fund_pct(plan, "ZZBAL", 50)
        win.set_fund_pct(plan, "ZZIDX", 30)
        qapp.processEvents()
        assert "do not add to 100%" in win.note.text()
        assert "ZZ 401k" in win.note.text()
    finally:
        win.deleteLater()


# --- the bars' own labels ---------------------------------------------------
def test_a_segment_is_labelled_only_when_its_label_fits(qapp):
    """Reported: "with the percentages in the bars or via tooltip for small
    bars". A percentage clipped to "4" is worse than none, and the tooltip
    carries every figure regardless."""
    bar = ClassBar({"domestic_stock": 97, "cash": 3}, show_labels=True)
    bar.resize(400, 30)
    qapp.processEvents()
    assert "97.0%" in bar.describe() and "3.0%" in bar.describe()
    assert bar._show_labels is True
    # The narrow slice gets no room: at 400px wide its segment is 12px.
    plain = ClassBar({"domestic_stock": 97, "cash": 3})
    assert plain._show_labels is False
