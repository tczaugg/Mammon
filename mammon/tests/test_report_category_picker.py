"""Regression: ONE category picker, parameterized by kind, behind every report
that narrows by category (SRD 5.9c).

The user's report: "I'm looking at itemize by category and it has an income
section with the income categories and it has a expenses section with the
spending categories, but only the spending categories can be selected in the
custume category picker. This is not acceptable. The picker needs both for this
report." The cause was four ad-hoc lists, the worst of which took its names from
``reports.spending_by_category`` -- an aggregation of money OUT -- so an income
category could not be offered by construction, and a category with no activity in
the shown range dropped off the list whenever the dates moved.

The fix is a scope, not another list: ``category_types.top_level_categories``
answers for ``expense``, ``income`` or ``both`` from the ledger's own category
TREE, ``report_filters.category_picker_names`` projects the names, and each
report declares the kind it can honor. Three things are pinned here, because the
bug could come back at any of them:

* the SCOPES themselves -- each kind lists exactly its own side, and ``both``
  lists everything, including an income category with no activity at all;
* the LIFE CYCLE -- ticking an income category in Itemize really renders that
  income category's rows and drops the ones left unticked, all the way from the
  widget through ``itemize_tree`` to the rendered rows;
* the WIRING -- every category-narrowing report names a kind, and no picker
  anywhere is sourced from a report aggregation again.

Synthetic data only: invented payees and categories, no PII. Money stays signed
integer cents. The two direct UPDATEs below are test-only setup for columns the
domain layer has no setter for yet (``categories.type`` / ``hidden``); nothing
under test writes them.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import inspect

import pytest
from PyQt5.QtCore import Qt

from mammon import category_types, db, ledger
from mammon.ui.report_filters import (
    CATEGORY_KIND_BOTH,
    CATEGORY_KIND_EXPENSE,
    CATEGORY_KIND_INCOME,
    CATEGORY_KINDS,
    ReportFilterBar,
    category_picker_names,
)
from mammon.ui.report_window import (
    ACCOUNT_BALANCES_SPEC,
    BY_PAYEE_SPEC,
    BY_TAG_SPEC,
    CASH_FLOW_SPEC,
    INCOME_EXPENSE_SPEC,
    INVESTMENT_PERFORMANCE_SPEC,
    ITEMIZE_SPEC,
    TRANSACTIONS_SPEC,
    ReportWindow,
)

JAN = ("2026-01-01", "2026-01-31")

# Every report whose customization bar narrows by category, and the scope it asked
# for. Itemize, Cash Flow, Income vs Expense and Transactions all render both
# signs, so all four take BOTH.
CATEGORY_REPORTS = [
    (ITEMIZE_SPEC, CATEGORY_KIND_BOTH),
    (CASH_FLOW_SPEC, CATEGORY_KIND_BOTH),
    (INCOME_EXPENSE_SPEC, CATEGORY_KIND_BOTH),
    (TRANSACTIONS_SPEC, CATEGORY_KIND_BOTH),
]

# The reports that do not group by category at all. A picker here would be a
# control that silently filters nothing.
NO_CATEGORY_REPORTS = [ACCOUNT_BALANCES_SPEC, BY_PAYEE_SPEC, BY_TAG_SPEC,
                       INVESTMENT_PERFORMANCE_SPEC]


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "picker.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    """A January of both signs, plus the three shapes that break an
    aggregation-sourced picker:

    * ``Royalties`` -- income, DECLARED via ``categories.type``, never used. An
      aggregation cannot know it exists; the tree can.
    * ``Consulting`` -- income whose only transaction is in February, so it is
      absent from a January aggregation but must still be offered in January.
    * ``Rental`` -- a parent with no rows of its own whose child holds the money.
      It is income only if the subtree is rolled up, which is exactly what
      ``itemize_tree`` does when it picks a section.
    """
    checking = ledger.create_account(conn, "ANON Checking", "checking")
    ids = {"checking": checking}

    fuel = ledger.resolve_category(conn, "Auto & Transport:Fuel")
    groceries = ledger.resolve_category(conn, "Groceries")
    salary = ledger.resolve_category(conn, "Salary")
    rent = ledger.resolve_category(conn, "Rental:Rent")
    consulting = ledger.resolve_category(conn, "Consulting")
    royalties = ledger.resolve_category(conn, "Royalties")
    ids.update(fuel=fuel, groceries=groceries, salary=salary, rent=rent,
               consulting=consulting, royalties=royalties)

    ledger.add_transaction(conn, checking, "2026-01-05", -60_00,
                           category_id=fuel, payee="ANON Fuel Stop")
    ledger.add_transaction(conn, checking, "2026-01-12", -140_00,
                           category_id=groceries, payee="ANON Market")
    ledger.add_transaction(conn, checking, "2026-01-28", 3000_00,
                           category_id=salary, payee="ANON Employer")
    ledger.add_transaction(conn, checking, "2026-01-30", 900_00,
                           category_id=rent, payee="ANON Tenant")
    # Outside January on purpose: a range-scoped aggregation would forget it.
    ledger.add_transaction(conn, checking, "2026-02-10", 500_00,
                           category_id=consulting, payee="ANON Client")
    # Declared income with no activity anywhere -- the derived rule would call a
    # zero net an expense, so the stored label has to win.
    conn.execute("UPDATE categories SET type=? WHERE id=?",
                 (category_types.INCOME, royalties))
    conn.commit()
    return ids


def _names(conn, kind):
    return category_picker_names(conn, kind)


def _tree_labels(win, depth):
    """The non-blank Category-column labels at ``depth`` in the rendered rows --
    depth 1 is the top-level categories, 2 their sub-categories."""
    return [r.cells[0] for r in win._rows if r.depth == depth and r.cells[0]]


def _check_only(lst, names):
    """Tick exactly ``names`` in a check-list widget, untick the rest."""
    for i in range(lst.count()):
        item = lst.item(i)
        item.setCheckState(Qt.Checked if item.text() in names else Qt.Unchecked)


# -- (a) the three scopes -----------------------------------------------------

def test_expense_scope_lists_only_expense_categories(conn, seeded):
    listed = _names(conn, CATEGORY_KIND_EXPENSE)
    assert {"Auto & Transport", "Groceries"} <= set(listed)
    for income_name in ("Salary", "Rental", "Consulting", "Royalties"):
        assert income_name not in listed, \
            "an expense-scoped picker must not offer an income category"


def test_income_scope_lists_only_income_categories(conn, seeded):
    listed = _names(conn, CATEGORY_KIND_INCOME)
    assert set(listed) == {"Consulting", "Rental", "Royalties", "Salary"}
    for expense_name in ("Auto & Transport", "Groceries"):
        assert expense_name not in listed


def test_both_scope_lists_every_top_level_including_zero_activity_income(
        conn, seeded):
    """``both`` is the scope Itemize asks for: the income side is tickable
    alongside the expense side, and a never-used income category is still there
    -- the defect was a list that could only ever contain money-out names."""
    listed = _names(conn, CATEGORY_KIND_BOTH)
    assert set(listed) >= {"Auto & Transport", "Consulting", "Groceries",
                           "Rental", "Royalties", "Salary"}
    assert set(_names(conn, CATEGORY_KIND_INCOME)) <= set(listed)
    assert set(_names(conn, CATEGORY_KIND_EXPENSE)) <= set(listed)
    # No overlap and no gap: every top level lands on exactly one side.
    assert not (set(_names(conn, CATEGORY_KIND_INCOME))
                & set(_names(conn, CATEGORY_KIND_EXPENSE)))
    assert set(listed) == (set(_names(conn, CATEGORY_KIND_INCOME))
                           | set(_names(conn, CATEGORY_KIND_EXPENSE)))
    assert listed == sorted(listed, key=str.lower)


def test_zero_activity_income_category_is_offered_to_the_income_scope(conn,
                                                                     seeded):
    """Royalties has no transactions at all. A picker reading an aggregation
    cannot name it; one reading the category tree can, and the stored
    ``categories.type`` says which side it belongs on."""
    assert "Royalties" in _names(conn, CATEGORY_KIND_INCOME)
    assert "Royalties" in _names(conn, CATEGORY_KIND_BOTH)
    assert "Royalties" not in _names(conn, CATEGORY_KIND_EXPENSE)


def test_a_category_with_no_activity_in_range_is_still_offered(qapp, conn,
                                                               seeded):
    """Consulting's only transaction is in February. The list must not shrink
    when the user narrows the dates -- a tick that vanishes on a date change was
    the second symptom of sourcing names from a report."""
    bar = ReportFilterBar(conn, *JAN, category_kind=CATEGORY_KIND_INCOME)
    try:
        listed = [bar.category_list.item(i).text()
                  for i in range(bar.category_list.count())]
        assert "Consulting" in listed
    finally:
        bar.deleteLater()


def test_top_level_kind_follows_the_rolled_up_subtree(conn, seeded):
    """Rental has no rows of its own; all the money is on Rental:Rent. It must
    classify as INCOME, because that is the section ``itemize_tree`` will put it
    in -- a picker that disagreed with the report would offer a name that never
    appears where the user looked for it."""
    assert "Rental" in _names(conn, CATEGORY_KIND_INCOME)
    assert "Rental" not in _names(conn, CATEGORY_KIND_EXPENSE)
    rows = category_types.top_level_categories(conn, CATEGORY_KIND_BOTH)
    rental = next(r for r in rows if r["name"] == "Rental")
    assert rental["type"] == category_types.INCOME
    assert rental["id"] == ledger.resolve_category(conn, "Rental")


def test_hidden_top_levels_are_left_out_unless_asked_for(conn, seeded):
    conn.execute("UPDATE categories SET hidden=1 WHERE id=?",
                 (seeded["groceries"],))
    conn.commit()
    assert "Groceries" not in _names(conn, CATEGORY_KIND_BOTH)
    shown = category_types.top_level_categories(conn, CATEGORY_KIND_BOTH,
                                                include_hidden=True)
    assert "Groceries" in [c["name"] for c in shown]


def test_unknown_kind_is_refused(conn, seeded):
    """Three kinds, named constants. A typo must fail loudly rather than quietly
    filtering everything out."""
    assert CATEGORY_KINDS == (CATEGORY_KIND_EXPENSE, CATEGORY_KIND_INCOME,
                              CATEGORY_KIND_BOTH)
    with pytest.raises(ValueError):
        category_types.top_level_categories(conn, "spending")


# -- (b) the life cycle: an income tick reaches the rendered report ------------

def test_itemize_income_pick_renders_that_income_categorys_rows(qapp, conn,
                                                                seeded):
    """The user's case end to end: open Itemize, tick ONE INCOME category, and
    the report shows that category's income rows and nothing else. Before the
    fix the tick did not exist to make."""
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        listed = [win.filters.category_list.item(i).text()
                  for i in range(win.filters.category_list.count())]
        assert "Salary" in listed, "the income side must be tickable"

        win.filters.set_range(*JAN)
        _check_only(win.filters.category_list, {"Salary"})
        assert win.filters.selected_categories() == {"Salary"}
        win.filters.apply_button.click()          # -> applied -> refresh

        sections = [r.cells[0] for r in win._rows if r.kind == "section"]
        assert sections == ["INCOME"], \
            "an income-only pick must render the INCOME section, not an empty one"
        assert _tree_labels(win, 1) == ["Salary"]
        payees = [r.cells[2] for r in win._rows if r.kind == "txn"]
        assert payees == ["ANON Employer"]
        # ...and every unticked category is gone, on both sides.
        labels = _tree_labels(win, 1)
        for dropped in ("Groceries", "Auto & Transport", "Rental"):
            assert dropped not in labels
    finally:
        win.close()


def test_itemize_pick_spanning_both_signs_keeps_both_sections(qapp, conn,
                                                              seeded):
    """One income and one expense category ticked together: both sections
    survive. The name filter is applied BEFORE the income/expense split, so a
    two-sided pick is not collapsed to whichever sign wins."""
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        win.filters.set_range(*JAN)
        _check_only(win.filters.category_list, {"Rental", "Groceries"})
        win.filters.apply_button.click()

        sections = [r.cells[0] for r in win._rows if r.kind == "section"]
        assert sections == ["INCOME", "EXPENSES"]
        assert set(_tree_labels(win, 1)) == {"Rental", "Groceries"}
        assert "Rent" in _tree_labels(win, 2)     # the income side still drills
        assert "Salary" not in _tree_labels(win, 1)
    finally:
        win.close()


# -- (c) the wiring: every report names its kind ------------------------------

@pytest.mark.parametrize("spec,kind", CATEGORY_REPORTS,
                         ids=lambda v: getattr(v, "title", str(v)))
def test_each_category_report_declares_the_kind_it_asked_for(spec, kind):
    assert spec.category_kind == kind
    # The older boolean is kept in step, so nothing can claim a list it has no
    # scope for (or a scope with no list).
    assert spec.show_categories is True


@pytest.mark.parametrize("spec", NO_CATEGORY_REPORTS,
                         ids=lambda v: v.title)
def test_reports_that_do_not_group_by_category_declare_no_kind(spec):
    assert spec.category_kind is None
    assert spec.show_categories is False


@pytest.mark.parametrize("spec,kind", CATEGORY_REPORTS,
                         ids=lambda v: getattr(v, "title", str(v)))
def test_report_window_list_is_exactly_the_shared_pickers_answer(
        qapp, conn, seeded, spec, kind):
    """The window builds no list of its own: what it shows is what the one
    picker answers for the kind the spec named."""
    win = ReportWindow(conn, spec=spec)
    try:
        lst = win.filters.category_list
        assert lst is not None, f"{spec.title} must show a category check-list"
        listed = [lst.item(i).text() for i in range(lst.count())]
        assert listed == _names(conn, kind)
        assert win.filters.category_kind == kind
        assert win.filters.category_names == listed
        # Everything starts ticked, which the bar reports as "no filter".
        assert win.filters.selected_categories() is None
    finally:
        win.close()


def test_chart_dialogs_declare_an_explicit_kind(qapp):
    """The three chart dialogs cannot be driven here -- each ends in
    ``exec_()``, which blocks forever under the offscreen platform -- so their
    wiring is read from the source instead. Each must name a kind and none may
    hand the bar a list it assembled itself."""
    from mammon.ui.widgets import MainWindow

    expected = {
        "_spending_report_dialog": "CATEGORY_KIND_EXPENSE",
        "_spending_chart_dialog": "CATEGORY_KIND_EXPENSE",
        "_income_chart_dialog": "CATEGORY_KIND_INCOME",
        # "as if this SPENDING had never happened" -- a what-if over money out.
        "_net_worth_chart_dialog": "CATEGORY_KIND_EXPENSE",
    }
    for method, kind in expected.items():
        src = inspect.getsource(getattr(MainWindow, method))
        assert f"category_kind={kind}" in src, \
            f"{method} must declare its category scope"
        assert "categories=self." not in src, \
            f"{method} must not build a category list of its own"


def test_no_picker_is_sourced_from_a_report_aggregation(qapp):
    """The defect in one line: a picker whose names came from
    ``spending_by_category`` could only ever offer money-out categories. Those
    two helpers are gone and must not come back."""
    from mammon.ui.widgets import MainWindow

    assert not hasattr(MainWindow, "_report_categories")
    assert not hasattr(MainWindow, "_income_report_categories")

    for module_name in ("mammon.ui.widgets", "mammon.ui.report_window"):
        import importlib

        src = inspect.getsource(importlib.import_module(module_name))
        for line in src.splitlines():
            if line.lstrip().startswith("#"):
                continue                      # the comments explain the history
            assert "spending_by_category" not in line or "categories=" not in line


def test_the_bar_can_still_be_given_an_explicit_list(qapp, conn, seeded):
    """The older ``categories=`` way in stays supported for callers that already
    hold names; it simply carries no kind."""
    bar = ReportFilterBar(conn, *JAN, categories=["Alpha", "Beta"])
    try:
        assert bar.category_kind is None
        assert bar.category_names == ["Alpha", "Beta"]
        assert bar.category_list.count() == 2
    finally:
        bar.deleteLater()


def test_no_kind_and_no_list_means_no_control(qapp, conn, seeded):
    bar = ReportFilterBar(conn, *JAN)
    try:
        assert bar.category_list is None
        assert bar.selected_categories() is None
    finally:
        bar.deleteLater()
