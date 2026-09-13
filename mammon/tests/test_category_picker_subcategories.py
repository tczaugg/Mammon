"""Regression: the ONE report category picker is a checkable TREE, and what it
hands the reports is category IDS (SRD 5.9c).

The user's ask: "Upgrade the category picker to be able to expand and select
subcategories, not just top level categories. This will be needed for tax
reporting." A tax report is the case that breaks a top-level-only picker:
``Taxes:Federal`` is deductible and ``Taxes:Property`` is a different line
entirely, so "the Taxes category" is not a filter anyone can use.

Four things are pinned here, because each is a separate way the upgrade could
rot back:

* the SHAPE -- a parent expands to its children, to any depth, and every row is
  individually checkable;
* the SCOPE -- the expense/income/both kind each report already passes still
  decides which top levels (and therefore which subtrees) are offered at all;
* the PROPAGATION -- ticking a parent takes its whole subtree, and unticking one
  child leaves the parent PARTIALLY checked, meaning "this parent's own postings
  and the children still ticked";
* the FILTER -- the report body and its rolled-up totals really drop the
  unticked sub-category, and a selection is carried as IDS, so
  `ledger.rename_category` (which keeps the id) cannot silently change what a
  saved filter set resolves to.

Synthetic data only: invented payees and a three-level category tree, no PII.
Money stays signed integer cents.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt

from mammon import db, ledger
from mammon.ui.report_filters import (
    CATEGORY_KIND_BOTH,
    CATEGORY_KIND_EXPENSE,
    CATEGORY_KIND_INCOME,
    ReportFilterBar,
    category_picker_tree,
)
from mammon.ui.report_saved_filters import apply_filter_state, filter_state_to_dict
from mammon.ui.report_window import ITEMIZE_SPEC, TRANSACTIONS_SPEC, ReportWindow

JAN = ("2026-01-01", "2026-01-31")


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "subcats.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    """A THREE-level expense tree with money at every level, plus one income
    top level so the kind scope has something to exclude::

        Taxes                 -50.00   (its own posting)
          Federal            -100.00
            Withholding      -220.00
          Property           -400.00
        Groceries            -140.00
        Salary             +3,000.00   (income)

    Every level holds transactions of its own, which is what makes "ticked
    parent, unticked child" a distinguishable answer: Taxes without Property is
    -370.00, not -770.00 and not -320.00.
    """
    checking = ledger.create_account(conn, "ANON Checking", "checking")
    cat = {
        "taxes": ledger.resolve_category(conn, "Taxes"),
        "federal": ledger.resolve_category(conn, "Taxes:Federal"),
        "withholding": ledger.resolve_category(conn, "Taxes:Federal:Withholding"),
        "property": ledger.resolve_category(conn, "Taxes:Property"),
        "groceries": ledger.resolve_category(conn, "Groceries"),
        "salary": ledger.resolve_category(conn, "Salary"),
    }
    ledger.add_transaction(conn, checking, "2026-01-04", -50_00,
                           category_id=cat["taxes"], payee="ANON Tax Prep")
    ledger.add_transaction(conn, checking, "2026-01-06", -100_00,
                           category_id=cat["federal"], payee="ANON Treasury")
    ledger.add_transaction(conn, checking, "2026-01-08", -220_00,
                           category_id=cat["withholding"], payee="ANON Payroll Tax")
    ledger.add_transaction(conn, checking, "2026-01-15", -400_00,
                           category_id=cat["property"], payee="ANON County")
    ledger.add_transaction(conn, checking, "2026-01-20", -140_00,
                           category_id=cat["groceries"], payee="ANON Market")
    ledger.add_transaction(conn, checking, "2026-01-28", 3000_00,
                           category_id=cat["salary"], payee="ANON Employer")
    cat["checking"] = checking
    return cat


# -- helpers ------------------------------------------------------------------

def _top(tree, name):
    """The top-level row named ``name`` (fails loudly if the picker lost it)."""
    for i in range(tree.count()):
        if tree.item(i).text() == name:
            return tree.item(i)
    raise AssertionError(
        "%r is not offered; the picker lists %s"
        % (name, [tree.item(i).text() for i in range(tree.count())]))


def _kids(item):
    return [item.child(i) for i in range(item.childCount())]


def _node(tree, path):
    """The row at a ``Parent:Child:Grandchild`` path -- the picker driven the way
    a user drives it, expanding one level at a time."""
    parts = path.split(":")
    item = _top(tree, parts[0])
    for part in parts[1:]:
        match = [k for k in _kids(item) if k.text() == part]
        assert match, "%r has no child %r (it has %s)" % (
            item.text(), part, [k.text() for k in _kids(item)])
        item = match[0]
    return item


def _labels(item):
    return [k.text() for k in _kids(item)]


def _bar(conn, kind=CATEGORY_KIND_EXPENSE):
    return ReportFilterBar(conn, *JAN, category_kind=kind)


def _pick_taxes_without_property(bar):
    """The tax-reporting case, driven through the widget: clear everything, tick
    Taxes (which takes its subtree), then untick Property alone."""
    bar.clear_categories()
    _top(bar.category_list, "Taxes").setCheckState(Qt.Checked)
    _node(bar.category_list, "Taxes:Property").setCheckState(Qt.Unchecked)


# -- (a) the shape: parents expand to their children --------------------------

def test_picker_exposes_children_under_a_parent(qapp, conn, seeded):
    """The defect in one assertion: the picker used to be a flat list of top
    levels, so Taxes had nothing under it to tick."""
    bar = _bar(conn)
    try:
        taxes = _top(bar.category_list, "Taxes")
        assert _labels(taxes) == ["Federal", "Property"]
        federal = _node(bar.category_list, "Taxes:Federal")
        assert _labels(federal) == ["Withholding"], \
            "the tree must go deeper than one level"
        # Three levels really are three rows, not a flattened 'Taxes:Federal'
        # label pretending to be one.
        assert bar.category_list.topLevelItemCount() >= 2
        assert len(list(bar.category_list.iter_items())) >= 5
    finally:
        bar.deleteLater()


def test_every_row_is_individually_checkable_and_carries_its_id(qapp, conn,
                                                                seeded):
    """A subtree row is a first-class pick: user-checkable, and keyed by the
    ledger's own category id rather than by its display name."""
    bar = _bar(conn)
    try:
        for path, key in (("Taxes", "taxes"),
                          ("Taxes:Federal", "federal"),
                          ("Taxes:Federal:Withholding", "withholding"),
                          ("Taxes:Property", "property")):
            item = _node(bar.category_list, path)
            assert item.flags() & Qt.ItemIsUserCheckable
            assert item.category_id == seeded[key]
    finally:
        bar.deleteLater()


def test_the_tree_comes_from_the_domain_layer_not_from_the_widget(conn, seeded):
    """`ui` holds no SQL: the hierarchy is `category_types.category_forest`,
    projected by the one shared picker helper."""
    forest = category_picker_tree(conn, CATEGORY_KIND_EXPENSE)
    taxes = next(n for n in forest if n["name"] == "Taxes")
    assert taxes["id"] == seeded["taxes"]
    assert [c["name"] for c in taxes["children"]] == ["Federal", "Property"]
    federal = taxes["children"][0]
    assert [c["name"] for c in federal["children"]] == ["Withholding"]
    assert federal["children"][0]["id"] == seeded["withholding"]


# -- (b) the scope: expense / income / both still decides what is offered ------

def test_kind_scope_still_decides_which_subtrees_are_offered(qapp, conn, seeded):
    """A subtree is offered only under a top level of the requested kind -- the
    kind-awareness the picker already had must survive the tree."""
    expense = _bar(conn, CATEGORY_KIND_EXPENSE)
    income = _bar(conn, CATEGORY_KIND_INCOME)
    both = _bar(conn, CATEGORY_KIND_BOTH)
    try:
        listed = [expense.category_list.item(i).text()
                  for i in range(expense.category_list.count())]
        assert "Taxes" in listed and "Salary" not in listed
        assert _node(expense.category_list, "Taxes:Federal:Withholding") is not None

        income_names = [income.category_list.item(i).text()
                        for i in range(income.category_list.count())]
        assert income_names == ["Salary"]
        # No Taxes top level in the income scope means no Taxes SUBTREE either.
        for it in income.category_list.iter_items():
            assert it.text() not in ("Federal", "Property", "Withholding")

        assert _node(both.category_list, "Taxes:Property") is not None
        assert _top(both.category_list, "Salary") is not None
    finally:
        for bar in (expense, income, both):
            bar.deleteLater()


# -- (c) propagation: parent implies subtree, one untick makes it partial ------

def test_checking_a_parent_selects_its_whole_subtree(qapp, conn, seeded):
    bar = _bar(conn)
    try:
        bar.clear_categories()
        assert bar.selected_category_ids() == []
        _top(bar.category_list, "Taxes").setCheckState(Qt.Checked)

        for path in ("Taxes:Federal", "Taxes:Federal:Withholding",
                     "Taxes:Property"):
            assert _node(bar.category_list, path).checkState() == Qt.Checked, path
        assert bar.selected_category_ids() == sorted(
            [seeded["taxes"], seeded["federal"], seeded["withholding"],
             seeded["property"]])
        assert _top(bar.category_list, "Groceries").checkState() == Qt.Unchecked
    finally:
        bar.deleteLater()


def test_unchecking_one_child_leaves_the_parent_partially_checked(qapp, conn,
                                                                  seeded):
    """The state the whole feature exists for: Taxes ticked EXCEPT Property.
    The parent stays in the selection (its own postings and its other children
    still count) while the one unticked child drops out."""
    bar = _bar(conn)
    try:
        _pick_taxes_without_property(bar)

        taxes = _top(bar.category_list, "Taxes")
        assert taxes.checkState() == Qt.PartiallyChecked
        assert _node(bar.category_list, "Taxes:Federal").checkState() == Qt.Checked
        assert _node(bar.category_list,
                     "Taxes:Federal:Withholding").checkState() == Qt.Checked
        assert _node(bar.category_list,
                     "Taxes:Property").checkState() == Qt.Unchecked

        ids = bar.selected_category_ids()
        assert seeded["property"] not in ids
        assert ids == sorted([seeded["taxes"], seeded["federal"],
                              seeded["withholding"]])
        # The name-level getter still answers for the callers that group by top
        # level -- a partially checked parent counts as chosen.
        assert bar.selected_categories() == {"Taxes"}
    finally:
        bar.deleteLater()


def test_unchecking_a_grandchild_bubbles_partial_all_the_way_up(qapp, conn,
                                                                seeded):
    bar = _bar(conn)
    try:
        bar.clear_categories()
        _top(bar.category_list, "Taxes").setCheckState(Qt.Checked)
        _node(bar.category_list,
              "Taxes:Federal:Withholding").setCheckState(Qt.Unchecked)

        assert _node(bar.category_list,
                     "Taxes:Federal").checkState() == Qt.PartiallyChecked
        assert _top(bar.category_list, "Taxes").checkState() == Qt.PartiallyChecked
        assert seeded["withholding"] not in bar.selected_category_ids()
    finally:
        bar.deleteLater()


def test_everything_ticked_is_still_no_filter(qapp, conn, seeded):
    """All-checked has always meant "do not filter", and an unticked GRANDCHILD
    has to be enough to make it a real filter again."""
    bar = _bar(conn)
    try:
        assert bar.selected_category_ids() is None
        _node(bar.category_list,
              "Taxes:Federal:Withholding").setCheckState(Qt.Unchecked)
        assert bar.selected_category_ids() is not None
        bar.mark_categories()
        assert bar.selected_category_ids() is None
    finally:
        bar.deleteLater()


# -- (d) the filter: the report really drops the unticked sub-category --------

def _tree_amount(win, label, depth):
    rows = [r for r in win._rows if r.depth == depth and r.cells[0] == label]
    assert rows, "no %r row at depth %d in %s" % (
        label, depth, [(r.depth, r.cells[0]) for r in win._rows])
    return rows[0].amount


def test_itemize_filters_on_the_chosen_subcategories(qapp, conn, seeded):
    """End to end through the window: tick Taxes, untick Property, and the
    report shows Federal (and its child) but not Property -- in the BODY and in
    the parent's rolled-up total."""
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        win.filters.set_range(*JAN)
        _pick_taxes_without_property(win.filters)
        win.filters.apply_button.click()

        labels = [r.cells[0] for r in win._rows if r.cells[0]]
        assert "Taxes" in labels and "Federal" in labels
        assert "Withholding" in labels, \
            "a ticked grandchild must still reach the report body"
        assert "Property" not in labels
        assert "Groceries" not in labels and "Salary" not in labels

        payees = [r.cells[2] for r in win._rows if r.kind == "txn"]
        assert "ANON County" not in payees, \
            "the unticked sub-category's transactions must be gone"
        assert {"ANON Treasury", "ANON Payroll Tax", "ANON Tax Prep"} <= set(payees)

        # -50 (Taxes' own) + -100 (Federal) + -220 (Withholding); the -400 of
        # Property is out of the SUM as well as out of the body.
        assert _tree_amount(win, "Taxes", 1) == -370_00
        assert _tree_amount(win, "Federal", 2) == -320_00
    finally:
        win.close()


def test_transactions_listing_does_not_re_expand_an_unticked_child(qapp, conn,
                                                                   seeded):
    """The listing report expands a category to its subtree by default. Driven
    from the tree it must NOT: the tree already pushed each tick down, so
    re-expanding Taxes would silently re-admit the Property row the user just
    unticked."""
    win = ReportWindow(conn, spec=TRANSACTIONS_SPEC)
    try:
        win.filters.set_range(*JAN)
        _pick_taxes_without_property(win.filters)
        win.filters.apply_button.click()

        # listing_rows' cells are Date, Payee, Category / Account, Tag, Memo, and
        # the last row is the "Total" summary line.
        payees = [r.cells[1] for r in win._rows if r.cells[0] != "Total"]
        assert "ANON County" not in payees
        assert set(payees) == {"ANON Tax Prep", "ANON Treasury", "ANON Payroll Tax"}
        assert win._rows[-1].cells[1] == "3 transactions"
    finally:
        win.close()


# -- (e) ids, not names: a rename cannot move a saved selection ---------------

def test_a_rename_does_not_change_what_a_saved_selection_resolves_to(qapp, conn,
                                                                     seeded):
    """`ledger.rename_category` keeps the id, which is the whole reason the
    selection travels as ids: renaming a CHOSEN category must leave a saved
    filter set pointing at exactly the same categories."""
    src = _bar(conn)
    try:
        _pick_taxes_without_property(src)
        state = filter_state_to_dict(src)
        saved_ids = list(state["category_ids"])
    finally:
        src.deleteLater()

    ledger.rename_category(conn, seeded["federal"], "US Federal")

    dst = _bar(conn)
    try:
        apply_filter_state(dst, state)
        assert dst.selected_category_ids() == saved_ids
        renamed = _node(dst.category_list, "Taxes:US Federal")
        assert renamed.category_id == seeded["federal"]
        assert renamed.checkState() == Qt.Checked, \
            "a rename must not silently drop the category from the filter"
        assert _node(dst.category_list,
                     "Taxes:Property").checkState() == Qt.Unchecked
    finally:
        dst.deleteLater()


def test_a_legacy_name_keyed_spec_still_loads(qapp, conn, seeded):
    """Back-compat: a set saved before the picker went id-keyed holds top-level
    NAMES and no ids. It must still resolve -- ticking a top level has always
    meant that whole subtree."""
    bar = _bar(conn)
    try:
        apply_filter_state(bar, {"start": JAN[0], "end": JAN[1],
                                 "include_hidden": False,
                                 "account_ids": None,
                                 "categories": ["Taxes"]})
        assert _top(bar.category_list, "Taxes").checkState() == Qt.Checked
        assert bar.selected_category_ids() == sorted(
            [seeded["taxes"], seeded["federal"], seeded["withholding"],
             seeded["property"]])
        assert _top(bar.category_list, "Groceries").checkState() == Qt.Unchecked
    finally:
        bar.deleteLater()


def test_a_subcategory_selection_round_trips_through_a_saved_set(qapp, conn,
                                                                 seeded):
    bar = _bar(conn)
    other = _bar(conn)
    try:
        _pick_taxes_without_property(bar)
        state = filter_state_to_dict(bar)
        apply_filter_state(other, state)
        assert filter_state_to_dict(other) == state
        assert other.selected_category_ids() == bar.selected_category_ids()
        assert _top(other.category_list, "Taxes").checkState() == Qt.PartiallyChecked
    finally:
        bar.deleteLater()
        other.deleteLater()
