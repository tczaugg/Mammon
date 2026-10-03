"""The Budget page (mammon/ui/budget_page.py; SRD 5.12,
docs/budget_one_page_design.md).

One month, one list, one balance: take-home income on top, every line marked F
or V, three columns (Planned, Spent, Left) and two sentences. These tests pin
what a first-time budgeter can check by eye - the page opens from the View menu
as a PAGE in the central stack, an empty ledger says what to do first, Add a
line proposes from history and writes the months its cadence lands on, the two
sentences say whether the plan balances and how the month is going - and that
every figure is the domain layer's (``mammon.budgets``), the page adding only
the rows up.

Synthetic data only. No modal is ever exec_()'d: the dialogs run through the
page's ``_run_dialog`` seam and the confirmations through
``QMessageBox.question``, both replaced here.
"""
from __future__ import annotations

import datetime as _dt
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QDate, Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from mammon import budgets, ledger
from mammon.tests import fresh_db
from mammon.ui import prefs
from mammon.ui.models import fmt_cents
from mammon.ui.budget_page import (CERTAIN_MONTHS, EVERY_MONTH, EVERY_TWO_WEEKS,
                                   FOR_DEBT, FOR_INCOME, FOR_SAVING, FOR_SPENDING,
                                   YEARLY_BILL, BudgetPage, PageLine, left_text, left_tooltip,
                                   month_sentence, page_lines, plan_sentence)

#: Fixed "today" so the trailing windows are the same months on every run.
TODAY = _dt.date(2026, 6, 17)
START = "2026-06"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "budget_page.db")
    yield c
    c.close()


@pytest.fixture
def cats(conn):
    return {
        "groceries": ledger.resolve_category(conn, "Groceries"),
        "dining": ledger.resolve_category(conn, "Dining"),
        "fuel": ledger.resolve_category(conn, "Auto & Transport:Fuel"),
        "rent": ledger.resolve_category(conn, "Rent"),
        "salary": ledger.resolve_category(conn, "Salary"),
    }


@pytest.fixture
def seeded(conn, cats, monkeypatch):
    """A beginner's ledger: one checking account, a year of unsplit paychecks
    (no deductions anywhere), groceries and dining every month, fuel in three,
    plus the current partial month's spending."""
    monkeypatch.setattr(prefs, "auto_enter_on_launch", lambda *a, **k: False)
    monkeypatch.setattr(prefs, "budget_count_scheduled", lambda *a, **k: False)
    monkeypatch.setattr(prefs, "set_budget_count_scheduled", lambda *a, **k: None)
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    sav = ledger.create_account(conn, "Rainy Day", "savings")
    for period, _s, _e in budgets.trailing_months(TODAY, 12):
        ledger.add_transaction(conn, acct, f"{period}-01", 2_400_00,
                               payee="Employer", category_id=cats["salary"])
        ledger.add_transaction(conn, acct, f"{period}-10", -300_00,
                               category_id=cats["groceries"])
        ledger.add_transaction(conn, acct, f"{period}-14", -120_00,
                               category_id=cats["dining"])
        ledger.create_transfer(conn, acct, sav, f"{period}-02", 100_00)
    for period, _s, _e in budgets.trailing_months(TODAY, 3):
        ledger.add_transaction(conn, acct, f"{period}-20", -60_00,
                               category_id=cats["fuel"])
    # June 2026, the current month: a paycheck and some spending.
    ledger.add_transaction(conn, acct, "2026-06-01", 2_400_00, payee="Employer",
                           category_id=cats["salary"])
    ledger.add_transaction(conn, acct, "2026-06-03", -180_00, category_id=cats["groceries"])
    ledger.add_transaction(conn, acct, "2026-06-09", -45_00, category_id=cats["dining"])
    ledger.add_transaction(conn, acct, "2026-06-11", -30_00, category_id=cats["fuel"])
    ledger.add_transaction(conn, acct, "2026-06-12", -12_50)             # uncategorized
    return {"checking": acct, "savings": sav}


def _menu_actions(window):
    out = []
    for menu_act in window.menuBar().actions():
        menu = menu_act.menu()
        if menu is not None:
            out.extend(menu.actions())
    return out


def _open_page(conn):
    """Open the Budget page the way the user does: the View-menu action."""
    from mammon.ui.widgets import MainWindow

    win = MainWindow(conn)
    win.budget_page._today = TODAY
    act = next(a for a in _menu_actions(win) if a.text().startswith("Budget"))
    act.trigger()
    return win, win.budget_page


def _cell(page, row, col):
    item = page.table.item(row, col)
    return "" if item is None else item.text()


def _row_of(page, label):
    for r in range(page.table.rowCount()):
        text = _cell(page, r, page.LINE).strip()
        if text == label or text.startswith(label + "   "):
            return r
    raise AssertionError(f"no {label!r} row on the page")


def _accepting(**values):
    """A ``_run_dialog`` replacement that fills the Add a line dialog."""
    def run(dialog):
        # The dialog opens on Income until the plan has an income line, so a
        # test that means spending says so (the default here, as for a user who
        # turns the first combo).
        dialog.purpose_combo.setCurrentText(values.get("purpose", FOR_SPENDING))
        if "ids" in values:
            dialog.choose(values["ids"])
        if "name" in values:
            dialog.name_edit.setText(values["name"])
        if "fixed" in values:
            (dialog.fixed_radio if values["fixed"] else dialog.variable_radio
             ).setChecked(True)
        if "how" in values:
            dialog.how_combo.setCurrentText(values["how"])
        if "first_date" in values:
            dialog.first_date_edit.setDate(
                QDate.fromString(values["first_date"], "yyyy-MM-dd"))
        if "months" in values:
            for cb in dialog.month_boxes:
                cb.setChecked(cb.property("period") in values["months"])
        if "amount" in values:
            dialog.amount_edit.setText(values["amount"])
            dialog._amount_touched = True
        return True
    return run


# ---- the page in the window ---------------------------------------------------
def test_view_menu_opens_budget_and_there_is_no_savings_debt_surface(qapp, conn,
                                                                     seeded):
    """Budget is a home page under View. Goals and payoff live ON the save-into
    and pay-down lines (the Add dialog and the Spent hover), not on a page or a
    report window of their own - the user's ruling."""
    win, page = _open_page(conn)
    try:
        assert win.stack.currentWidget() is page
        assert isinstance(page, BudgetPage)
        titles = [a.text().replace("&&", "&") for a in _menu_actions(win)]
        assert any(t.startswith("Budget") for t in titles)
        assert not any("Savings" in t or "Budget Planner" in t for t in titles)
        assert not hasattr(win, "savings_debt")
    finally:
        win.close()
        qapp.processEvents()


def test_empty_ledger_says_what_to_do_first_and_start_makes_a_budget(qapp, conn,
                                                                     seeded):
    win, page = _open_page(conn)
    try:
        assert page.budget is None
        assert page.start_button.isVisibleTo(page)
        assert not page.add_button.isEnabled()
        assert "No budget yet" in page.plan_label.text()
        page.start_button.click()
        b = page.budget
        assert b is not None and b.active
        assert (b.start_period, b.end_period) == ("2026-06", "2027-05")
        assert page.period == "2026-06"
        assert page.month_combo.currentText() == "June 2026"
        assert not page.start_button.isVisibleTo(page)
        assert page.add_button.isEnabled()
        # Everything else already reports this month's spending.
        lines = page.lines()
        assert [ln.kind for ln in lines] == ["other"]
        assert lines[0].spent_cents == 180_00 + 45_00 + 30_00 + 12_50
        assert "Nothing is planned yet" in page.plan_label.text()
        assert "choose Income" in page.plan_label.text()
        assert "take-home pay first" in page.status.text()
        # The empty Income section carries a hint row that opens Add a line on
        # Income, and so does a right-click on the section itself.
        assert _cell(page, 0, page.LINE) == "Income"
        assert "Add your take-home pay" in _cell(page, 1, page.LINE)
        assert [a.text() for a in page.menu_for_row(0).actions()] == ["Add income..."]
        assert [a.text() for a in page.menu_for_row(1).actions()] == ["Add income..."]
        assert [a.text() for a in page.menu_for_row(2).actions()] == ["Add a line..."]
        opened = {}

        def capture(dialog):
            opened["purpose"] = dialog.purpose
            return False
        page._run_dialog = capture
        page.table.cellClicked.emit(1, page.LINE)
        assert opened["purpose"] == FOR_INCOME
        # Add a line itself opens on Income while the plan has no income line.
        opened.clear()
        page.add_button.click()
        assert opened["purpose"] == FOR_INCOME
    finally:
        win.close()
        qapp.processEvents()


# ---- adding lines ---------------------------------------------------------------
def test_ten_minutes_to_a_plan_that_balances(qapp, conn, seeded, cats, monkeypatch):
    """The acceptance walk-through of the design: income, a Fixed cost, a line
    covering two categories, a saving line, then Everything else takes the
    remainder and the plan balances."""
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        # 1. Net pay, proposed from the last twelve months of deposits.
        dlg = page.make_add_dialog()
        try:
            dlg.purpose_combo.setCurrentText(FOR_INCOME)
            labels = [dlg.choice_list.item(i).text() for i in range(dlg.choice_list.count())]
            assert labels and labels[0].startswith("Salary")
            assert "2,400.00 a month" in labels[0]
            dlg.choose([cats["salary"]])
            assert dlg.amount_edit.text() == "2,400.00"
            assert dlg.how == EVERY_MONTH
            assert not dlg.kind_widget.isVisibleTo(dlg)        # not asked for income
            assert "Last 12 months: 28,800.00 in all" in dlg.basis_label.text()
        finally:
            dlg.deleteLater()
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            purpose=FOR_INCOME, ids=[cats["salary"]]))
        page.add_button.click()
        assert budgets.get_setting(conn, bid, cats["salary"]).bucket == "income"
        assert "2,400.00 of your income is not planned yet" in page.plan_label.text()
        # 2. Rent, Fixed, typed.
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["rent"]], fixed=True, amount="900.00"))
        page.add_button.click()
        assert budgets.get_setting(conn, bid, cats["rent"]).bucket == "fixed"
        # 3. Food covering groceries and dining: one line, proposed as their sum.
        dlg = page.make_add_dialog()
        try:
            dlg.choose([cats["groceries"], cats["dining"]])
            assert dlg.name_edit.isVisibleTo(dlg)
            assert dlg.amount_edit.text() == "420.00"
            assert not dlg.buttons.button(dlg.buttons.Ok).isEnabled()   # needs a name
            dlg.name_edit.setText("Food")
            assert dlg.buttons.button(dlg.buttons.Ok).isEnabled()
        finally:
            dlg.deleteLater()
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["groceries"], cats["dining"]], name="Food"))
        page.add_button.click()
        (food,) = budgets.list_groups(conn, bid)
        assert food.name == "Food" and food.bucket == "flex"
        assert sorted(budgets.group_members(conn, bid)[food.id]) == sorted(
            [cats["groceries"], cats["dining"]])
        # 4. Save into Rainy Day.
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            purpose=FOR_SAVING, ids=[seeded["savings"]]))
        page.add_button.click()
        # The list: income, then every expense line by amount, then other.
        lines = page.lines()
        assert [(ln.kind, ln.label) for ln in lines] == [
            ("income", "Salary"), ("category", "Rent"), ("group", "Food"),
            ("account", "Rainy Day"), ("other", "Everything else")]
        assert [ln.planned_cents for ln in lines] == [2_400_00, 900_00, 420_00,
                                                      100_00, 0]
        assert "980.00 of your income is not planned yet" in page.plan_label.text()
        # 5. Everything else takes the remainder: the plan balances.
        other_row = _row_of(page, "Everything else")
        page.table.item(other_row, page.PLANNED).setText("980")
        assert budgets.get_other_lines(conn, bid)["2026-06"] == 980_00
        assert page.plan_label.text() == (
            "Planned 2,400.00 of 2,400.00 income: the plan balances.")
        # The month sentence: spent so far against take-home, days left.
        # No June transfer to savings yet, so saving contributes nothing so far.
        spent = 180_00 + 45_00 + 30_00 + 12_50
        assert page.month_label.text() == (
            f"Spent {spent / 100:,.2f} so far; {(2_400_00 - spent) / 100:,.2f} left; "
            f"13 days remaining in June.")
        # Three columns and the Left words, by line.
        assert _cell(page, _row_of(page, "Rent"), page.KIND) == "F"
        assert _cell(page, _row_of(page, "Rent"), page.LEFT) == "900.00"
        assert _cell(page, _row_of(page, "Food"), page.KIND) == "V"
        assert _cell(page, _row_of(page, "Food"), page.SPENT) == "225.00"
        assert _cell(page, _row_of(page, "Food"), page.LEFT) == "195.00"
        assert _cell(page, _row_of(page, "Rainy Day"), page.LEFT) == "100.00"
        assert _cell(page, _row_of(page, "Salary"), page.SPENT) == "2,400.00"
        assert _cell(page, _row_of(page, "Salary"), page.LEFT) == ""
        assert _cell(page, other_row, page.SPENT) == "42.50"
        assert _cell(page, other_row, page.LEFT) == "937.50"
        assert "(2 categories not on the plan)" in _cell(page, other_row, page.LINE)
    finally:
        win.close()
        qapp.processEvents()


def test_how_often_biweekly_yearly_and_certain_months(qapp, conn, seeded, cats,
                                                      monkeypatch):
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        # Every two weeks from a payday: two or three times a month.
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            purpose=FOR_INCOME, ids=[cats["salary"]], how=EVERY_TWO_WEEKS,
            first_date="2026-06-19", amount="1,000.00"))
        page.add_button.click()
        lines = {ln.period: ln.amount_cents for ln in budgets.get_lines(conn, bid)
                 if ln.category_id == cats["salary"]}
        assert lines["2026-06"] == 1_000_00 and lines["2026-07"] == 3_000_00
        assert lines["2026-08"] == 2_000_00 and sum(lines.values()) == 25_000_00
        # A yearly bill: spread over twelve months, carrying forward.
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["fuel"]], how=YEARLY_BILL, amount="1,000.00"))
        page.add_button.click()
        st = budgets.get_setting(conn, bid, cats["fuel"])
        assert (st.bucket, st.annual_cents, st.rollover_mode) == (
            "nonmonthly", 100_000, "both")
        fuel_lines = [ln.amount_cents for ln in budgets.get_lines(conn, bid)
                      if ln.category_id == cats["fuel"]]
        assert sum(fuel_lines) == 100_000 and sorted(set(fuel_lines)) == [83_33, 83_34]
        assert _cell(page, _row_of(page, "Auto & Transport:Fuel"), page.KIND) == "V"
        # Certain months only.
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["rent"]], fixed=True, how=CERTAIN_MONTHS,
            months=["2026-09", "2027-03"], amount="250.00"))
        page.add_button.click()
        rent = {ln.period: ln.amount_cents for ln in budgets.get_lines(conn, bid)
                if ln.category_id == cats["rent"]}
        assert rent == {"2026-09": 250_00, "2027-03": 250_00}
        # Paying down a loan is a line like any other.
        loan = ledger.create_account(conn, "Car Loan", "liability")
        page.refresh()
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            purpose=FOR_DEBT, ids=[loan], amount="350.00"))
        page.add_button.click()
        assert [sl.amount_cents for sl in budgets.get_saving_lines(conn, bid)
                if sl.account_id == loan] == [350_00] * 12
        assert "(extra principal)" in _cell(page, _row_of(page, "Car Loan"), page.LINE)
    finally:
        win.close()
        qapp.processEvents()


def test_add_dialog_offers_only_what_is_not_on_the_plan_and_never_split(
        qapp, conn, seeded, cats, monkeypatch):
    ledger.resolve_category(conn, "--Split--")
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["groceries"]], amount="300.00"))
        page.add_button.click()
        dlg = page.make_add_dialog()
        try:
            # With no income line yet the dialog opens on Income - the lesson's
            # first line - so this test turns it to Spending explicitly.
            assert dlg.purpose == FOR_INCOME
            dlg.purpose_combo.setCurrentText(FOR_SPENDING)
            ids = [dlg.choice_list.item(i).data(Qt.UserRole).id
                   for i in range(dlg.choice_list.count())]
            assert cats["groceries"] not in ids
            assert cats["dining"] in ids and cats["fuel"] in ids
            labels = [dlg.choice_list.item(i).text() for i in range(dlg.choice_list.count())]
            assert all("--Split--" not in t for t in labels)
            # Biggest spender first; the income list is income categories only.
            assert ids[0] == cats["dining"]
            dlg.purpose_combo.setCurrentText(FOR_INCOME)
            assert [dlg.choice_list.item(i).data(Qt.UserRole).id
                    for i in range(dlg.choice_list.count())] == [cats["salary"]]
        finally:
            dlg.deleteLater()
    finally:
        win.close()
        qapp.processEvents()


# ---- editing on the page --------------------------------------------------------
def test_planned_cell_kind_click_row_menu_and_drill_down(qapp, conn, seeded, cats,
                                                         monkeypatch):
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["groceries"]], amount="300.00"))
        page.add_button.click()
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["dining"]], amount="120.00"))
        page.add_button.click()
        # Typing a planned amount writes the month; emptying removes it.
        row = _row_of(page, "Groceries")
        page.table.item(row, page.PLANNED).setText("350")
        lines = {ln.period: ln.amount_cents for ln in budgets.get_lines(conn, bid)
                 if ln.category_id == cats["groceries"]}
        assert lines["2026-06"] == 350_00 and lines["2026-07"] == 300_00
        page.table.item(_row_of(page, "Groceries"), page.PLANNED).setText("")
        assert "2026-06" not in {ln.period for ln in budgets.get_lines(conn, bid)
                                 if ln.category_id == cats["groceries"]}
        # Clicking the Kind cell switches V to F and back.
        row = _row_of(page, "Groceries")
        assert _cell(page, row, page.KIND) == "V"
        page.table.cellClicked.emit(row, page.KIND)
        assert budgets.get_setting(conn, bid, cats["groceries"]).bucket == "fixed"
        assert _cell(page, _row_of(page, "Groceries"), page.KIND) == "F"
        assert "now Fixed" in page.status.text()
        page.table.cellClicked.emit(_row_of(page, "Groceries"), page.KIND)
        assert budgets.get_setting(conn, bid, cats["groceries"]).bucket == "flex"
        # The row menu: move, carry forward, remove.
        groceries = next(ln for ln in page.lines() if ln.label == "Groceries")
        dining = next(ln for ln in page.lines() if ln.label == "Dining")
        # The first edit froze the order the lines were in (Groceries 300 ahead
        # of Dining 120), so emptying Groceries' month did not move it.
        assert [ln.label for ln in page.lines() if ln.kind == "category"] == [
            "Groceries", "Dining"]
        assert budgets.line_order(conn, bid) == {
            ("category", cats["groceries"]): 0, ("category", cats["dining"]): 1}
        page.move_line(groceries, 1)
        assert [ln.label for ln in page.lines() if ln.kind == "category"] == [
            "Dining", "Groceries"]
        assert budgets.line_order(conn, bid)[("category", cats["groceries"])] == 1
        # Sort lines by amount forgets the stored order: Dining 120 now leads
        # Groceries, whose June amount is gone.
        page.sort_action.trigger()
        assert budgets.line_order(conn, bid) == {}
        assert [ln.label for ln in page.lines() if ln.kind == "category"] == [
            "Dining", "Groceries"]
        page.set_carry(dining, True)
        assert budgets.get_setting(conn, bid, cats["dining"]).rollover_mode == "positive"
        dining = next(ln for ln in page.lines() if ln.label == "Dining")
        assert dining.rollover_mode == "positive"
        menu = page.row_menu(dining)
        texts = [a.text() for a in menu.actions() if a.text()]
        assert texts == ["Edit line...", "Move up", "Move down",
                         "Carry unspent amounts forward", "Remove line"]
        assert next(a for a in menu.actions()
                    if a.text().startswith("Carry")).isChecked()
        menu.deleteLater()
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        page.remove_line(dining)
        assert cats["dining"] in {ln.category_id for ln in budgets.get_lines(conn, bid)}
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        page.remove_line(dining)
        assert cats["dining"] not in {ln.category_id for ln in budgets.get_lines(conn, bid)}
        assert "Dining" not in [ln.label for ln in page.lines()]
        # Everything else offers only to add a line, and drills into the
        # categories behind it, uncategorized included.
        other = next(ln for ln in page.lines() if ln.kind == "other")
        assert [a.text() for a in page.row_menu(other).actions() if a.text()] == [
            "Add a line..."]
        dlg = page.drill_down(_row_of(page, "Everything else"))
        try:
            assert dlg is not None
            cats_shown = sorted(dlg.table.item(r, 3).text() for r in range(dlg.table.rowCount()))
            assert cats_shown == ["Auto & Transport:Fuel", "Dining", "Uncategorized"]
            assert "87.50" in dlg.footer.text()
        finally:
            dlg.close()
            dlg.deleteLater()
        assert page.drill_down(0) is None                 # the Income header
    finally:
        win.close()
        qapp.processEvents()


def test_change_line_rewrites_its_months(qapp, conn, seeded, cats, monkeypatch):
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["groceries"]], amount="300.00"))
        page.add_button.click()
        groceries = next(ln for ln in page.lines() if ln.label == "Groceries")
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            how=CERTAIN_MONTHS, months=["2026-07"], amount="90.00", fixed=True))
        page.change_line(groceries)
        lines = {ln.period: ln.amount_cents for ln in budgets.get_lines(conn, bid)
                 if ln.category_id == cats["groceries"]}
        assert lines == {"2026-07": 90_00}
        assert budgets.get_setting(conn, bid, cats["groceries"]).bucket == "fixed"
    finally:
        win.close()
        qapp.processEvents()


# ---- More -----------------------------------------------------------------------
def test_propose_a_plan_from_last_year(qapp, conn, seeded, cats):
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        page.propose_action.trigger()
        lines = page.lines()
        by_label = {ln.label: ln for ln in lines}
        assert by_label["Salary"].kind == "income"
        assert by_label["Salary"].planned_cents == 2_400_00
        assert by_label["Groceries"].planned_cents == 300_00
        assert by_label["Dining"].planned_cents == 120_00
        assert by_label["Auto & Transport:Fuel"].planned_cents == 15_00   # 180 / 12
        assert all(ln.bucket == "flex" for ln in lines
                   if ln.kind == "category")
        assert "Proposed 4 lines" in page.status.text()
        assert budgets.get_setting(conn, bid, cats["salary"]).bucket == "income"
        assert "1,965.00 of your income is not planned yet" in page.plan_label.text()
        page.propose_action.trigger()
        assert "Nothing to propose" in page.status.text()
    finally:
        win.close()
        qapp.processEvents()


def test_plan_the_year_edits_any_month(qapp, conn, seeded, cats, monkeypatch):
    from mammon.ui.budget_page import PlanYearDialog

    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["groceries"]], amount="300.00"))
        page.add_button.click()
        dlg = PlanYearDialog(conn, bid, lines=page.lines(), parent=page)
        try:
            assert dlg.table.rowCount() == 2                 # Groceries, Everything else
            assert dlg.table.columnCount() == 13
            assert dlg.table.horizontalHeaderItem(1).text() == "Jun 2026"
            assert dlg.table.item(0, 7).text() == "300.00"      # Dec 2026
            dlg.table.item(0, 7).setText("450")
            dlg.table.item(1, 7).setText("100")                 # Everything else
        finally:
            dlg.deleteLater()
        lines = {ln.period: ln.amount_cents for ln in budgets.get_lines(conn, bid)}
        assert lines["2026-12"] == 450_00 and lines["2026-11"] == 300_00
        assert budgets.get_other_lines(conn, bid) == {"2026-12": 100_00}
    finally:
        win.close()
        qapp.processEvents()


def test_budgets_dialog_new_rename_copy_activate_delete(qapp, conn, seeded,
                                                        monkeypatch):
    from mammon.ui.budget_page import BudgetsDialog

    win, page = _open_page(conn)
    try:
        page.start_button.click()
        first = page.budget_id
        dlg = BudgetsDialog(conn, today=TODAY, parent=page)
        try:
            assert dlg.selected_id == first
            dlg.name_edit.setText("Household 2026")
            dlg.rename_button.click()
            assert budgets.get_budget(conn, first).name == "Household 2026"
            dlg.copy_button.click()
            copy = dlg.selected_id
            assert copy != first
            assert budgets.get_budget(conn, copy).start_period == "2027-06"
            dlg.active_button.click()
            assert budgets.get_budget(conn, copy).active
            assert not budgets.get_budget(conn, first).active
            monkeypatch.setattr(QMessageBox, "question",
                                lambda *a, **k: QMessageBox.Yes)
            dlg.delete_button.click()
            assert budgets.get_budget(conn, copy) is None
            assert budgets.get_budget(conn, first).active     # one is always active
            dlg.new_button.click()
            assert len(budgets.list_budgets(conn)) == 2
        finally:
            dlg.deleteLater()
        page.refresh()
        assert page.budget_id == first
    finally:
        win.close()
        qapp.processEvents()


def test_count_scheduled_bills_as_spent_is_a_choice_under_more(qapp, conn, seeded,
                                                              cats, monkeypatch):
    from mammon import scheduled

    scheduled.add_scheduled(conn, seeded["checking"], payee="Power Co", amount=-80_00,
                            frequency="monthly", next_date="2026-06-25",
                            category_id=cats["fuel"])
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        # A scheduled bill proposes Fixed at the schedule's amount; this test
        # wants a graded line, so it says Variable and types its own amount.
        dlg = page.make_add_dialog()
        try:
            dlg.purpose_combo.setCurrentText(FOR_SPENDING)
            dlg.choose([cats["fuel"]])
            assert dlg.fixed_radio.isChecked()
            assert dlg.amount_edit.text() == "80.00"
            assert "'Power Co' monthly" in dlg.basis_label.text()
        finally:
            dlg.deleteLater()
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["fuel"]], fixed=False, amount="150.00"))
        page.add_button.click()
        row = _row_of(page, "Auto & Transport:Fuel")
        assert _cell(page, row, page.SPENT) == "30.00"
        assert _cell(page, row, page.LEFT) == "120.00"
        page.scheduled_action.setChecked(True)
        row = _row_of(page, "Auto & Transport:Fuel")
        assert _cell(page, row, page.SPENT) == "110.00"
        assert _cell(page, row, page.LEFT) == "40.00"
        assert "80.00 scheduled and not yet entered" in             page.table.item(row, page.SPENT).toolTip()
    finally:
        win.close()
        qapp.processEvents()


def test_send_spending_to_the_retirement_planner_emits_the_basis(qapp, conn, seeded,
                                                                 cats, monkeypatch):
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["groceries"]], amount="300.00"))
        page.add_button.click()
        got = []
        page.basis_ready.connect(got.append)
        monkeypatch.setattr(page, "_run_dialog", lambda d: True)
        page.retire_action.trigger()
        assert len(got) == 1 and got[0].annual_cents > 0
        assert got[0].source == "budget"
        assert "Sent" in page.status.text()
    finally:
        win.close()
        qapp.processEvents()


# ---- the pure parts ---------------------------------------------------------------
def _line(kind="category", **kw):
    fields = {"ident": 1, "label": "x"}
    fields.update(kw)
    return PageLine(kind=kind, **fields)


def test_left_is_just_the_amount_left():
    """The user's ruling: the Left cell is the amount left and nothing else.
    "not yet", "as planned", "n above plan", "n to go" all restated what the
    Planned and Spent numbers beside it already show. Over is negative."""
    assert left_text(_line(planned_cents=200_00, spent_cents=150_00)) == "50.00"
    assert left_text(_line(planned_cents=200_00, spent_cents=230_00)) == "-30.00"
    assert left_text(_line(planned_cents=200_00, spent_cents=150_00,
                           committed_cents=60_00), count_scheduled=True) == "-10.00"
    assert left_text(_line(bucket="fixed", planned_cents=900_00)) == "900.00"
    assert left_text(_line(bucket="fixed", planned_cents=900_00,
                           spent_cents=900_00)) == "0.00"
    assert left_text(_line(bucket="fixed", planned_cents=900_00,
                           spent_cents=950_00)) == "-50.00"
    assert left_text(_line("account", planned_cents=100_00, spent_cents=40_00)) == "60.00"
    assert left_text(_line("account", planned_cents=100_00, spent_cents=130_00)) == "-30.00"
    assert left_text(_line("income", planned_cents=100_00)) == ""
    assert left_text(_line("other", ident=None, spent_cents=40_00)) == ""
    assert left_text(_line("other", ident=None, planned_cents=100_00,
                           spent_cents=40_00)) == "60.00"
    # Carry raises what is left; the hover says so, the cell does not.
    carried = _line(planned_cents=200_00, carried_cents=30_00, spent_cents=150_00)
    assert left_text(carried) == "80.00"
    assert left_tooltip(carried) == "Includes 30.00 carried from last month."
    overspent = _line(planned_cents=200_00, carried_cents=-30_00, spent_cents=150_00)
    assert left_text(overspent) == "20.00"
    assert "30.00 overspent last month" in left_tooltip(overspent)
    assert left_tooltip(_line(planned_cents=200_00)) == ""


def test_the_two_sentences():
    income = _line("income", planned_cents=2_400_00, spent_cents=2_400_00)
    rent = _line(bucket="fixed", planned_cents=900_00, spent_cents=900_00)
    food = _line(planned_cents=420_00, spent_cents=225_00)
    other = _line("other", ident=None, planned_cents=1_080_00, spent_cents=42_50)
    assert plan_sentence([income, rent, food, other]) == (
        "Planned 2,400.00 of 2,400.00 income: the plan balances.")
    assert plan_sentence([income, rent, food]) == (
        "1,080.00 of your income is not planned yet.")
    assert plan_sentence([income, rent, food, _line(planned_cents=1_200_00)]) == (
        "Planned 2,520.00 against 2,400.00 income: short 120.00.")
    assert plan_sentence([rent]) == (
        "Planned 900.00. Add an income line so the plan can balance against what "
        "you take home.")
    assert plan_sentence([_line("other", ident=None)]).startswith("Nothing is planned yet")
    today = _dt.date(2026, 6, 17)
    assert month_sentence([income, rent, food, other], "2026-06", today) == (
        "Spent 1,167.50 so far; 1,232.50 left; 13 days remaining in June.")
    assert month_sentence([income, _line(planned_cents=0, spent_cents=3_000_00)],
                          "2026-06", today) == (
        "Spent 3,000.00 so far, 600.00 more than your income; 13 days remaining in June.")
    assert month_sentence([income, food], "2026-05", today).endswith("May is over.")
    assert month_sentence([income, food], "2026-08", today).endswith(
        "August has not started.")
    assert month_sentence([_line("other", ident=None)], "2026-06", today) == ""


def test_page_lines_partition_the_month_and_follow_the_stored_order(conn, seeded, cats):
    bid = budgets.new_budget(conn, START)
    budgets.set_settings(conn, bid, cats["salary"], bucket="income")
    budgets.set_line(conn, bid, cats["salary"], START, 2_400_00)
    food = budgets.create_group(conn, bid, "Food")
    budgets.set_group_members(conn, food, [cats["groceries"], cats["dining"]])
    budgets.set_group_line(conn, bid, food, START, 420_00)
    budgets.set_line(conn, bid, cats["rent"], START, 900_00)
    budgets.set_settings(conn, bid, cats["rent"], bucket="fixed")
    budgets.set_saving_line(conn, bid, seeded["savings"], START, 100_00)
    lines = page_lines(conn, bid, START)
    assert [(ln.kind, ln.label) for ln in lines] == [
        ("income", "Salary"), ("category", "Rent"), ("group", "Food"),
        ("account", "Rainy Day"), ("other", "Everything else")]
    food_line = lines[2]
    assert food_line.detail == "(Dining, Groceries)"
    assert food_line.spent_cents == 225_00
    assert sorted(food_line.members) == sorted([cats["groceries"], cats["dining"]])
    other = lines[-1]
    assert other.spent_cents == 30_00 + 12_50          # fuel and the uncategorized
    assert other.members == (cats["fuel"],)
    assert lines[0].spent_cents == 2_400_00            # take-home received
    # The user's order wins within the expense section.
    budgets.set_line_order(conn, bid, [("group", food), ("category", cats["rent"]),
                                       ("account", seeded["savings"])])
    assert [ln.label for ln in page_lines(conn, bid, START)] == [
        "Salary", "Food", "Rent", "Rainy Day", "Everything else"]


def test_a_mortgage_payment_as_one_line_by_payee_and_the_spent_hover(
        qapp, conn, seeded, cats, monkeypatch):
    """A split mortgage payment (principal to the loan, interest, escrow) is
    one line matched by payee and counted whole; its legs leave Everything
    else. Hovering Spent lists the transactions behind any line."""
    from mammon.ui.budget_page import FOR_PAYEE

    loan = ledger.create_account(conn, "Home Loan", "liability")
    interest = ledger.resolve_category(conn, "Mortgage:Interest")
    escrow = ledger.resolve_category(conn, "Mortgage:Escrow")
    for period, _s, _e in budgets.trailing_months(TODAY, 12):
        tid = ledger.add_transaction(conn, seeded["checking"], f"{period}-01",
                                     -1_500_00, payee="Acme Mortgage Co")
        ledger.set_splits(conn, tid, [
            {"transfer_account_id": loan, "amount": -900_00, "memo": ""},
            {"category_id": interest, "amount": -450_00, "memo": ""},
            {"category_id": escrow, "amount": -150_00, "memo": ""}])
    tid = ledger.add_transaction(conn, seeded["checking"], "2026-06-01", -1_500_00,
                                 payee="Acme Mortgage Co")
    ledger.set_splits(conn, tid, [
        {"transfer_account_id": loan, "amount": -900_00, "memo": ""},
        {"category_id": interest, "amount": -450_00, "memo": ""},
        {"category_id": escrow, "amount": -150_00, "memo": ""}])

    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        other = next(ln for ln in page.lines() if ln.kind == "other")
        assert other.spent_cents == 180_00 + 45_00 + 30_00 + 12_50 + 450_00 + 150_00
        dlg = page.make_add_dialog()
        try:
            dlg.purpose_combo.setCurrentText(FOR_PAYEE)
            assert dlg.payee_edit.isVisibleTo(dlg) and not dlg.choice_list.isVisibleTo(dlg)
            assert dlg.fixed_radio.isChecked()
            dlg.payee_edit.setText("acme mortgage")
            assert dlg.amount_edit.text() == "1,500.00"
            assert dlg.name_edit.text() == "Acme Mortgage"
            assert "18,000.00 in all over 12 months" in dlg.basis_label.text()
            assert dlg.buttons.button(dlg.buttons.Ok).isEnabled()
            req = dlg.result()
            assert (req.purpose, req.payee_match, req.bucket) == (
                FOR_PAYEE, "acme mortgage", "fixed")
        finally:
            dlg.deleteLater()

        def accept(dialog):
            dialog.purpose_combo.setCurrentText(FOR_PAYEE)
            dialog.payee_edit.setText("acme mortgage")
            dialog.name_edit.setText("Mortgage")
            return True
        monkeypatch.setattr(page, "_run_dialog", accept)
        page.add_button.click()
        (g,) = budgets.list_groups(conn, bid)
        assert (g.name, g.payee_match, g.bucket) == ("Mortgage", "acme mortgage", "fixed")
        row = _row_of(page, "Mortgage")
        assert "(payments to 'acme mortgage')" in _cell(page, row, page.LINE)
        assert _cell(page, row, page.KIND) == "F"
        assert _cell(page, row, page.PLANNED) == "1,500.00"
        assert _cell(page, row, page.SPENT) == "1,500.00"
        assert _cell(page, row, page.LEFT) == "0.00"
        # The interest and escrow legs left Everything else.
        other = next(ln for ln in page.lines() if ln.kind == "other")
        assert other.spent_cents == 180_00 + 45_00 + 30_00 + 12_50
        # Hover: the payment behind the figure; Everything else: its rows.
        tip = page.table.item(row, page.SPENT).toolTip()
        assert "Acme Mortgage Co  1,500.00" in tip and "Double-click" in tip
        other_tip = page.table.item(_row_of(page, "Everything else"), page.SPENT).toolTip()
        assert "(no payee)  12.50" in other_tip
        assert "1,500.00" not in other_tip
        # The drill-down lists the whole payment, with its account.
        dlg = page.drill_down(row)
        try:
            assert dlg.table.rowCount() == 1
            assert dlg.table.item(0, 1).text() == "Everyday Checking"
            assert dlg.table.item(0, 3).text() == "(whole payment)"
            assert dlg.table.item(0, 4).text() == "-1,500.00"
        finally:
            dlg.close()
            dlg.deleteLater()
        # Editing the line keeps it a payee line.
        line = next(ln for ln in page.lines() if ln.label == "Mortgage")
        assert line.by_payee

        def change(dialog):
            assert dialog.purpose == FOR_PAYEE
            assert dialog.payee_edit.text() == "acme mortgage"
            dialog.amount_edit.setText("1,550.00")
            dialog._amount_touched = True
            return True
        monkeypatch.setattr(page, "_run_dialog", change)
        page.change_line(line)
        assert _cell(page, _row_of(page, "Mortgage"), page.PLANNED) == "1,550.00"
        assert budgets.get_group(conn, g.id).payee_match == "acme mortgage"
    finally:
        win.close()
        qapp.processEvents()


def test_gear_totals_tracking_toggle_and_scope(qapp, conn, seeded, cats, monkeypatch):
    """The page's chrome after the user's review: a gear menu (not More), the
    balance and the month's reading above the table, a legend for F and V, a
    Total row, a toggle that hides Spent and Left, no status log, and the
    accounts-and-categories dialog that keeps business money out."""
    business = ledger.create_account(conn, "Business Checking", "checking")
    supplies = ledger.resolve_category(conn, "Business:Supplies")
    ledger.add_transaction(conn, business, "2026-06-04", -500_00, category_id=supplies)
    ledger.add_transaction(conn, seeded["checking"], "2026-06-05", -20_00,
                           category_id=supplies)
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        texts = [a.text() for a in page.gear_menu.actions() if a.text()]
        assert texts == ["Show spending", "Count scheduled bills as spent",
                         "Accounts and categories...", "Sort lines by amount",
                         "Plan the year...", "Propose a plan from last year",
                         "Budgets...", "Send spending to the Retirement Planner..."]
        assert page.gear_button.text() == "⚙"
        assert not hasattr(page, "more_button")
        assert "F = Fixed" in page.legend_label.text() and "V = Variable" in page.legend_label.text()
        assert not page.status.isVisibleTo(page)            # no log on the page
        # Business spending lands in Everything else until the scope says no.
        other = next(ln for ln in page.lines() if ln.kind == "other")
        assert other.spent_cents == 180_00 + 45_00 + 30_00 + 12_50 + 500_00 + 20_00
        dlg = page.make_scope_dialog()
        try:
            # Checking, the savings account and the business account: every
            # spending-type account, all in by default.
            assert dlg.accounts_list.count() == 3
            assert all(dlg.accounts_list.item(i).checkState() == Qt.Checked
                       for i in range(3))
            dlg.set_accounts([seeded["checking"]])
            dlg.set_excluded([supplies])
            accounts, excluded = dlg.result()
            assert accounts == [seeded["checking"]] and excluded == [supplies]
        finally:
            dlg.deleteLater()
        monkeypatch.setattr(page, "_run_dialog", lambda d: (
            d.set_accounts([seeded["checking"]]), d.set_excluded([supplies]), True)[-1])
        page.scope_action.trigger()
        assert budgets.budget_account_ids(conn, bid) == [seeded["checking"]]
        assert budgets.excluded_categories(conn, bid) == {supplies}
        other = next(ln for ln in page.lines() if ln.kind == "other")
        assert other.spent_cents == 180_00 + 45_00 + 30_00 + 12_50
        # The excluded category is not offered as a line either.
        add = page.make_add_dialog()
        try:
            add.purpose_combo.setCurrentText(FOR_SPENDING)
            assert supplies not in [add.choice_list.item(i).data(Qt.UserRole).id
                                    for i in range(add.choice_list.count())]
        finally:
            add.deleteLater()
        # A Total row closes the Expenses section.
        monkeypatch.setattr(page, "_run_dialog", _accepting(
            ids=[cats["groceries"]], amount="300.00"))
        page.add_button.click()
        total_row = _row_of(page, "Total")
        assert total_row == page.table.rowCount() - 1
        assert _cell(page, total_row, page.PLANNED) == "300.00"
        assert _cell(page, total_row, page.SPENT) == fmt_cents(180_00 + 45_00 + 30_00 + 12_50)
        assert _cell(page, total_row, page.LEFT) == "32.50"
        assert page.menu_for_row(total_row) is None
        # Show spending off: Spent and Left go, and the month's reading with them.
        assert not page.table.isColumnHidden(page.SPENT)
        page.tracking_action.setChecked(False)
        assert page.table.isColumnHidden(page.SPENT) and page.table.isColumnHidden(page.LEFT)
        assert not page.month_label.isVisibleTo(page)
        assert page.plan_label.isVisibleTo(page)
        page.tracking_action.setChecked(True)
        assert not page.table.isColumnHidden(page.LEFT)
    finally:
        win.close()
        qapp.processEvents()


def test_a_goal_lives_on_the_save_line_and_a_payoff_on_the_pay_down_line(
        qapp, conn, seeded, cats, monkeypatch):
    """Savings goals and debt payoff are properties of their lines (the
    user's ruling over a page of their own): the Add dialog proposes the
    required amount from a target and date, or says when a debt clears at the
    typed payment, and the Spent hover reports where each stands."""
    from decimal import Decimal

    from mammon import debt, goals

    loan = ledger.create_account(conn, "Car Loan", "liability")
    ledger.add_transaction(conn, loan, "2026-01-05", -6_000_00, payee="Opening Balance")
    debt.set_terms(conn, loan, apr=Decimal("6"), min_form="A", min_floor_cents=150_00)
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        bid = page.budget_id
        # The saving line: a goal of 2,400.00 by next May proposes 200.00 a
        # month (12 months, June through May, counting new money only).
        dlg = page.make_add_dialog()
        try:
            dlg.purpose_combo.setCurrentText(FOR_SAVING)
            assert dlg.goal_edit.isVisibleTo(dlg) and not dlg.apr_edit.isVisibleTo(dlg)
            dlg.choose([seeded["savings"]])
            assert dlg.amount_edit.text() == "100.00"           # history first
            dlg.goal_edit.setText("2,400.00")
            dlg.goal_date_edit.setDate(QDate(2027, 5, 31))
            assert dlg.amount_edit.text() == "200.00"
            assert "takes 200.00 a month over 12 months" in dlg.basis_label.text()
            assert "Rainy Day holds 1,200.00 today" in dlg.basis_label.text()
            req = dlg.result()
            assert (req.goal_cents, req.goal_date) == (240_000, "2027-05-31")
        finally:
            dlg.deleteLater()

        def accept_goal(dialog):
            dialog.purpose_combo.setCurrentText(FOR_SAVING)
            dialog.choose([seeded["savings"]])
            dialog.goal_edit.setText("2,400.00")
            dialog.goal_date_edit.setDate(QDate(2027, 5, 31))
            return True
        monkeypatch.setattr(page, "_run_dialog", accept_goal)
        page.add_button.click()
        goal = goals.goal_for_account(conn, seeded["savings"], budget_id=bid)
        assert goal is not None
        assert (goal.target_cents, goal.target_date, goal.monthly_cents) == (
            240_000, "2027-05-31", 200_00)
        assert goal.baseline_cents == 1_200_00               # new money only
        row = _row_of(page, "Rainy Day")
        tip = page.table.item(row, page.SPENT).toolTip()
        assert tip.startswith("Goal 2,400.00 by 05/31/2027: 0.00 saved, 2,400.00 to go.")
        assert "200.00 a month needed over 12 months; planned 200.00: on pace." in tip
        # Typing a smaller amount makes the goal behind, by the shortfall.
        page.table.item(row, page.PLANNED).setText("150")
        assert goals.goal_for_account(conn, seeded["savings"]).monthly_cents == 150_00
        tip = page.table.item(_row_of(page, "Rainy Day"), page.SPENT).toolTip()
        assert "behind by 50.00 a month" in tip
        # Removing the line removes its goal.
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        page.remove_line(next(ln for ln in page.lines() if ln.label == "Rainy Day"))
        assert goals.goal_for_account(conn, seeded["savings"]) is None

        # The pay-down line is a fixed EXTRA principal payment on top of the
        # regular one (the user's ruling): the regular payment is budgeted where
        # it is paid. The APR comes from the terms, nothing is proposed without
        # a history of extra payments, and the sentence compares the regular
        # payment alone with the extra added.
        dlg = page.make_add_dialog()
        try:
            dlg.purpose_combo.setCurrentText(FOR_DEBT)
            assert dlg.apr_edit.isVisibleTo(dlg)
            assert dlg.amount_caption.text() == "Extra principal a month:"
            dlg.choose([loan])
            assert dlg.apr_edit.text() == "6"
            assert dlg.amount_edit.text() == ""
            text = dlg.basis_label.text()
            assert "Car Loan owes 6,000.00 at 6%" in text
            assert "At the regular 150.00 a month it clears in" in text
            dlg.amount_edit.setText("100.00")
            dlg._amount_touched = True
            text = dlg.basis_label.text()
            assert "With 100.00 extra principal a month it clears in" in text
            assert "sooner and" in text and "less interest" in text
            dlg.apr_edit.setText("12")
            assert dlg.result().apr == "12"
        finally:
            dlg.deleteLater()

        def accept_debt(dialog):
            dialog.purpose_combo.setCurrentText(FOR_DEBT)
            dialog.choose([loan])
            dialog.amount_edit.setText("100.00")
            dialog._amount_touched = True
            dialog.apr_edit.setText("12")
            return True
        monkeypatch.setattr(page, "_run_dialog", accept_debt)
        page.add_button.click()
        assert debt.get_terms(conn, loan).apr == Decimal("12")
        row = _row_of(page, "Car Loan")
        tip = page.table.item(row, page.SPENT).toolTip()
        assert tip.startswith("Owes 6,000.00 at 12%.")
        assert "At the regular 150.00 a month it clears in" in tip
        assert "With 100.00 extra principal a month it clears in" in tip
    finally:
        win.close()
        qapp.processEvents()


def test_the_remaining_column_shows_negatives_in_red(qapp):
    """The user's ruling: the column is "Remaining", and a negative amount -- a
    line that ran over -- is drawn in the theme's negative color, as the
    register draws one."""
    from PyQt5.QtGui import QColor

    from mammon.ui import budget_page, style
    assert BudgetPage.HEADERS[BudgetPage.LEFT] == "Remaining"
    over = budget_page._CentsItem(-18_20)
    assert over.text() == "-18.20"
    assert over.foreground().color().name() == QColor(style.negative_color()).name()
    assert budget_page._CentsItem(18_20).foreground().style() == Qt.NoBrush


def test_a_pay_down_line_counts_only_extra_principal(conn):
    """The regular mortgage payment is budgeted by payee; the pay-down line
    measures only principal beyond it: a principal-only transfer, and the
    excess of a payment over the scheduled payment (the loan engine puts that
    excess into principal)."""
    from mammon import budgets, loans
    chk = ledger.create_account(conn, "Checking", "checking")
    mortgage = ledger.create_account(conn, "Mortgage", "liability")
    ledger.add_transaction(conn, mortgage, "2026-01-01", -200_000_00,
                           payee="Opening Balance")
    loans.set_loan_params(conn, mortgage, original_principal=200_000_00,
                          term_months=360, payment_amount=1_500_00,
                          origination_date="2026-01-01", rates=[("2026-01-01", "6")])
    # The regular payment, and one 100.00 over it, as plain payments posted on
    # the loan account (no split: the engine divides them).
    ledger.add_transaction(conn, mortgage, "2026-06-01", 1_500_00, payee="Bank")
    ledger.add_transaction(conn, mortgage, "2026-07-01", 1_600_00, payee="Bank")
    # A principal-only transfer.
    ledger.create_transfer(conn, chk, mortgage, "2026-07-15", 250_00,
                           payee="Extra principal")
    assert budgets.month_extra_principal(conn, "2026-06", [mortgage]) == {mortgage: 0}
    assert budgets.month_extra_principal(conn, "2026-07", [mortgage]) == {
        mortgage: 350_00}
    items = loans.extra_principal_items(conn, mortgage, "2026-07-01", "2026-07-31")
    assert items == [("2026-07-01", "Paid over the scheduled payment", 100_00),
                     ("2026-07-15", "Extra principal", 250_00)]


def test_the_page_says_typing_planned_changes_this_month_only(qapp, conn, seeded):
    """Typing in Planned edits the month on screen; every month is Edit line
    or Plan the year. Not obvious from the cell, so it is
    said above the table, on the column header and on each Planned cell."""
    from mammon.ui.budget_page import PLANNED_HINT
    win, page = _open_page(conn)
    try:
        page.start_button.click()
        assert "this month only" in PLANNED_HINT and "Plan the year" in PLANNED_HINT
        assert PLANNED_HINT in page.legend_label.text()
        assert page.table.horizontalHeaderItem(page.PLANNED).toolTip() == PLANNED_HINT
        rows = [r for r in range(page.table.rowCount())
                if page.table.item(r, page.PLANNED) is not None
                and page.table.item(r, page.PLANNED).flags() & Qt.ItemIsEditable]
        assert rows and all(page.table.item(r, page.PLANNED).toolTip() == PLANNED_HINT
                            for r in rows)
    finally:
        win.close()
        qapp.processEvents()
