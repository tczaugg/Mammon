"""The Asset Allocation report (``mammon/ui/asset_allocation.py``).

All data here is SYNTHETIC: ZZ-prefixed tickers, invented account names, round
numbers. Nothing touches a real ledger.

What is worth asserting is mostly structural -- the tree's shape, which rows
carry the warning triangle, what a bar is made of -- plus the one thing the
report exists for: that a mix can be SET here, which nothing in the app could
do before.
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt                                   # noqa: E402
from PyQt5.QtWidgets import QApplication, QDialog             # noqa: E402

from mammon import investments, ledger, portfolio, security_mix   # noqa: E402
from mammon.ui import asset_allocation as aa                  # noqa: E402
from mammon.tests import fresh_db                             # noqa: E402

AS_OF = "2026-06-30"
OPEN_DATE = "2024-01-01"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "asset_allocation.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """Two investment accounts, four holdings, deliberately in three states.

    ZZSTK has a single class. ZZMIX has a real mixture. ZZNONE has NEITHER --
    it is the unallocated holding the triangles are about. The 401(k) also
    holds ZZFUND, which stands for a plan fund with no public ticker: nothing
    could describe it before this window existed.
    """
    taxable = ledger.create_account(conn, "ZZ Taxable", "investment",
                                    opening_balance=20_000_00,
                                    opening_date=OPEN_DATE)
    plan = ledger.create_account(conn, "ZZ 401k", "investment",
                                 opening_balance=30_000_00,
                                 opening_date=OPEN_DATE)

    def buy(acct, sym, qty, price):
        investments.record_investment(
            conn, acct, OPEN_DATE, "Buy", symbol=sym, quantity=Decimal(qty),
            price=Decimal(price), amount=int(Decimal(qty) * Decimal(price) * 100))
        investments.rebuild_holdings(conn, acct)
        investments.record_price(conn, sym, AS_OF, Decimal(price))

    buy(taxable, "ZZSTK", "100", "50.00")     # $5,000
    buy(taxable, "ZZNONE", "100", "20.00")    # $2,000, no class and no mixture
    buy(plan, "ZZMIX", "100", "60.00")        # $6,000
    buy(plan, "ZZFUND", "100", "40.00")       # $4,000, the untickered plan fund

    portfolio.set_security(conn, "ZZSTK", asset_class="domestic_stock")
    security_mix.set_mixture(conn, "ZZMIX",
                             {"domestic_stock": 60, "bond": 40}, source="test")
    return {"taxable": taxable, "plan": plan}


def _window(conn, **kw):
    return aa.AssetAllocationWindow(conn, as_of=AS_OF, **kw)


# --- colors -----------------------------------------------------------------
def test_every_class_has_a_color_and_they_are_all_distinct():
    """One definition, so a class is the same color in every bar. If two shared
    one, two accounts' bars could not be compared, which is the whole point."""
    colors = aa.class_colors()
    assert set(colors) == set(portfolio.ASSET_CLASSES)
    assert len(set(colors.values())) == len(portfolio.ASSET_CLASSES)
    # Unallocated is NOT a class color: it must not look like an answer.
    assert aa.unclassified_color() not in colors.values()


def test_crypto_is_among_the_colored_classes():
    """It became assignable on 2026-09-21, so the report has to be able to draw
    it."""
    assert "crypto" in aa.class_colors()


# --- the bar ----------------------------------------------------------------
def test_a_bar_orders_its_segments_by_class_not_by_size(qapp):
    """Bonds must sit in the same place in every account's bar, or the bars
    cannot be read against each other."""
    bar = aa.ClassBar({"bond": 10, "domestic_stock": 90})
    assert bar.order() == ["domestic_stock", "bond"]
    flipped = aa.ClassBar({"domestic_stock": 10, "bond": 90})
    assert flipped.order() == ["domestic_stock", "bond"]


def test_an_unallocated_slice_sorts_last_and_is_named_as_a_gap(qapp):
    bar = aa.ClassBar({aa.UNCLASSIFIED: 50, "bond": 50})
    assert bar.order() == ["bond", aa.UNCLASSIFIED]
    assert "Unallocated" in bar.describe()
    assert "50.0%" in bar.describe()


def test_an_empty_bar_says_nothing_rather_than_dividing_by_zero(qapp):
    bar = aa.ClassBar({})
    assert bar.order() == []
    assert bar.describe() == ""


# --- the tree ---------------------------------------------------------------
def test_accounts_are_rows_and_their_holdings_are_children(conn, world, qapp):
    win = _window(conn)
    try:
        tree = win.tree
        names = [tree.topLevelItem(i).text(aa.COL_NAME)
                 for i in range(tree.topLevelItemCount())]
        assert set(names) == {"ZZ Taxable", "ZZ 401k"}
        kids = {}
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            kids[top.text(aa.COL_NAME)] = {
                top.child(j).text(aa.COL_NAME) for j in range(top.childCount())}
        assert {"ZZSTK", "ZZNONE"} <= kids["ZZ Taxable"]
        assert {"ZZMIX", "ZZFUND"} <= kids["ZZ 401k"]
    finally:
        win.deleteLater()


def test_property_is_not_in_this_report(conn, world, qapp):
    """Reported: "I'm not sure property still has a place on it ... I'd say
    leave it out". It has no (mu, sigma), so it can never join a projection,
    and it was only ever a wedge here."""
    house = ledger.create_account(conn, "ZZ House", "asset",
                                  opening_balance=400_000_00,
                                  opening_date=OPEN_DATE)
    portfolio.set_account_asset_class(conn, house, "real_estate")
    win = _window(conn)
    try:
        names = [win.tree.topLevelItem(i).text(aa.COL_NAME)
                 for i in range(win.tree.topLevelItemCount())]
        assert "ZZ House" not in names
        assert "real_estate" not in win.total_bar.weights()
    finally:
        win.deleteLater()


# --- the triangles ----------------------------------------------------------
def test_an_undefined_holding_is_marked_and_so_is_the_account_above_it(
        conn, world, qapp):
    """Reported: "put up amber triangles where the mix is not defined and on any
    rollups that are affected by unallocated asset classes". The old view showed
    one `unclassified` wedge and left the user to work out which holding caused
    it."""
    win = _window(conn)
    try:
        tree = win.tree
        marked = set()
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            if not top.icon(aa.COL_NAME).isNull():
                marked.add(top.text(aa.COL_NAME))
            for j in range(top.childCount()):
                kid = top.child(j)
                if not kid.icon(aa.COL_NAME).isNull():
                    marked.add(kid.text(aa.COL_NAME))
        # ZZNONE has neither a class nor a mixture; its account inherits the mark.
        assert "ZZNONE" in marked
        assert "ZZ Taxable" in marked
        # ZZ 401k's holdings are both allocated... except ZZFUND, which is not.
        assert "ZZFUND" in marked
        # ZZSTK has a class and ZZMIX a mixture: neither is marked.
        assert "ZZSTK" not in marked and "ZZMIX" not in marked
        assert win.has_unallocated() is True
    finally:
        win.deleteLater()


def test_once_everything_is_defined_no_triangle_remains(conn, world, qapp):
    security_mix.set_mixture(conn, "ZZNONE", {"bond": 100})
    security_mix.set_mixture(conn, "ZZFUND", {"intl_stock": 100})
    win = _window(conn)
    try:
        assert win.has_unallocated() is False
        assert aa.UNCLASSIFIED not in win.total_bar.weights()
        for i in range(win.tree.topLevelItemCount()):
            top = win.tree.topLevelItem(i)
            assert top.icon(aa.COL_NAME).isNull()
    finally:
        win.deleteLater()


# --- editing, which is the point --------------------------------------------
def test_a_plan_fund_with_no_ticker_can_finally_be_given_a_mix(
        conn, world, qapp, monkeypatch):
    """THE capability this report exists for. security_mix.set_mixture had no
    caller but the yfinance fetch, and a 401(k) fund has no public ticker, so
    such a fund could not be described at all."""
    win = _window(conn)
    try:
        assert security_mix.get_mixture(conn, "ZZFUND") == {}
        item = _find(win.tree, "ZZFUND")
        monkeypatch.setattr(win, "_run_editor", lambda ed: QDialog.Accepted)
        monkeypatch.setattr(
            win, "_make_editor",
            lambda subject, weights: aa.MixEditor(
                subject, {"domestic_stock": 70, "bond": 30}, parent=win))
        win.edit_item(item)
        assert security_mix.get_mixture(conn, "ZZFUND") == {
            "domestic_stock": Decimal("70.00"), "bond": Decimal("30.00")}
        # ...and the report is redrawn on it: the mark is gone from that row.
        assert _find(win.tree, "ZZFUND").icon(aa.COL_NAME).isNull()
    finally:
        win.deleteLater()


def test_an_account_can_be_given_a_conservative_mix(conn, world, qapp,
                                                    monkeypatch):
    """Reported: "I need to be able to assign one account to a conservative mix
    and another to the crypto class. I can't do either." An account carried one
    class, and for an INVESTMENT account allocation never even read it."""
    win = _window(conn)
    try:
        item = _find(win.tree, "ZZ 401k")
        monkeypatch.setattr(win, "_run_editor", lambda ed: QDialog.Accepted)
        monkeypatch.setattr(
            win, "_make_editor",
            lambda subject, weights: aa.MixEditor(
                subject, {"bond": 70, "domestic_stock": 30}, parent=win))
        win.edit_item(item)
        stored = security_mix.get_account_mixture(conn, world["plan"])
        assert stored == {"bond": Decimal("70.00"),
                          "domestic_stock": Decimal("30.00")}
    finally:
        win.deleteLater()


def test_an_account_can_be_given_the_crypto_class(conn, world, qapp, monkeypatch):
    win = _window(conn)
    try:
        item = _find(win.tree, "ZZ Taxable")
        monkeypatch.setattr(win, "_run_editor", lambda ed: QDialog.Accepted)
        monkeypatch.setattr(
            win, "_make_editor",
            lambda subject, weights: aa.MixEditor(subject, {"crypto": 100},
                                                  parent=win))
        win.edit_item(item)
        assert security_mix.get_account_mixture(conn, world["taxable"]) == {
            "crypto": Decimal("100.00")}
    finally:
        win.deleteLater()


def test_a_cancelled_edit_changes_nothing(conn, world, qapp, monkeypatch):
    win = _window(conn)
    try:
        monkeypatch.setattr(win, "_run_editor", lambda ed: QDialog.Rejected)
        win.edit_item(_find(win.tree, "ZZFUND"))
        assert security_mix.get_mixture(conn, "ZZFUND") == {}
    finally:
        win.deleteLater()


def test_clearing_a_mix_puts_the_holding_back_on_its_single_class(
        conn, world, qapp):
    win = _window(conn)
    try:
        item = _find(win.tree, "ZZMIX")
        win.tree.setCurrentItem(item)
        assert security_mix.get_mixture(conn, "ZZMIX") != {}
        win.clear_selected()
        assert security_mix.get_mixture(conn, "ZZMIX") == {}
    finally:
        win.deleteLater()


def test_the_editor_scales_what_was_typed_to_a_hundred(qapp):
    """60/30/5 is plainly a ratio; refusing it for summing to 95 would be
    pedantry, and storing it unscaled would understate the holding."""
    editor = aa.MixEditor("ZZX", {"domestic_stock": 60, "bond": 30, "cash": 5})
    try:
        weights = editor.weights()
        assert sum(weights.values()) == Decimal("95")
        security_mix_normalized = security_mix.normalize(weights)
        assert sum(security_mix_normalized.values()) == Decimal("100.00")
    finally:
        editor.deleteLater()


def test_an_all_zero_editor_clears_the_mix(qapp):
    editor = aa.MixEditor("ZZX", {})
    try:
        assert editor.weights() == {}
    finally:
        editor.deleteLater()


# --- rollup -----------------------------------------------------------------
def test_the_total_bar_is_the_sum_of_the_accounts(conn, world, qapp):
    win = _window(conn)
    try:
        total = sum(win.total_bar.weights().values())
        alloc = portfolio.allocation(conn, as_of=AS_OF, scope="investments")
        assert total == alloc.total
    finally:
        win.deleteLater()


def test_an_accounts_cash_appears_as_its_own_row(conn, world, qapp):
    """Cash is most of a fresh account, and a composition that silently omitted
    it would not add up."""
    win = _window(conn)
    try:
        top = _find(win.tree, "ZZ Taxable")
        kids = [top.child(i).text(aa.COL_NAME) for i in range(top.childCount())]
        assert "Cash" in kids
    finally:
        win.deleteLater()


def _find(tree, name):
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        if top.text(aa.COL_NAME) == name:
            return top
        for j in range(top.childCount()):
            kid = top.child(j)
            if kid.text(aa.COL_NAME) == name:
                return kid
    raise AssertionError(f"no row named {name!r}")
