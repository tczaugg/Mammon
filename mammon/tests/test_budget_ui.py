"""The Budgets panel (mammon/ui/budget_widget.py): a thin projection over
mammon.budgets. Exercises create, edit-amount (as signed integer cents), the
view of budgeted/actual/remaining for a month, adding a category line, and the
delete-confirmation and name-prompt seams. Synthetic data only."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from mammon import budgets, db, ledger
from mammon.ui import budget_widget
from mammon.ui.budget_widget import BudgetLinesModel, BudgetWidget

B = BudgetLinesModel.BUDGETED
A = BudgetLinesModel.ACTUAL
R = BudgetLinesModel.REMAINING


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "budget_ui.db")
    yield c
    c.close()


@pytest.fixture
def cats(conn):
    return {
        "fuel": ledger.resolve_category(conn, "Auto & Transport:Fuel"),
        "groceries": ledger.resolve_category(conn, "Groceries"),
        "dining": ledger.resolve_category(conn, "Dining"),
    }


@pytest.fixture
def seeded(conn, cats):
    """A checking account with January 2026 spending: fuel -100, groceries -250,
    dining -30. February is deliberately empty."""
    a = ledger.create_account(conn, "Checking", "checking")
    ledger.add_transaction(conn, a, "2026-01-05", -100_00, category_id=cats["fuel"])
    ledger.add_transaction(conn, a, "2026-01-15", -250_00, category_id=cats["groceries"])
    ledger.add_transaction(conn, a, "2026-01-20", -30_00, category_id=cats["dining"])
    return a


def _row_for_category(w, cat_id):
    for r in range(w.model.rowCount()):
        if w.model.row_at(r).category_id == cat_id:
            return r
    raise AssertionError(f"category {cat_id} not shown in the budget table")


def test_create_budget_through_widget(qapp, conn, seeded):
    w = BudgetWidget(conn)
    assert w.budget_combo.count() == 0
    assert w.current_budget_id() is None
    assert w.model.rowCount() == 0  # no budget selected -> empty table

    w._ask_budget_name = lambda: "Household 2026"
    w._on_new()

    assert w.budget_combo.count() == 1
    assert w.current_budget_id() is not None
    assert [b.name for b in budgets.list_budgets(conn)] == ["Household 2026"]
    # the selected budget's active flag reflects into the checkbox
    assert w.active_check.isChecked() is True
    w.deleteLater()


def test_blank_name_prompt_creates_nothing(qapp, conn):
    w = BudgetWidget(conn)
    w._ask_budget_name = lambda: None  # user cancelled / left it blank
    w._on_new()
    assert budgets.list_budgets(conn) == []
    assert w.budget_combo.count() == 0
    w.deleteLater()


def test_edit_amount_persists_as_integer_cents(qapp, conn, seeded, cats):
    bid = budgets.create_budget(conn, "Plan")
    w = BudgetWidget(conn)
    w.set_period("2026-01")

    row = _row_for_category(w, cats["groceries"])  # has spending, so it's listed
    m = w.model
    m.setData(m.index(row, B), "300.00", Qt.EditRole)

    # Stored as exact signed integer cents via the domain layer -- no float.
    lines = {ln.category_id: ln.amount_cents
             for ln in budgets.get_lines(conn, bid, period="2026-01")}
    assert lines[cats["groceries"]] == 300_00
    assert isinstance(lines[cats["groceries"]], int)

    # And the cell re-renders through fmt_cents.
    assert m.data(m.index(row, B), Qt.DisplayRole) == "300.00"
    w.deleteLater()


def test_view_budget_vs_actual_for_month(qapp, conn, seeded, cats):
    bid = budgets.create_budget(conn, "Plan")
    budgets.set_line(conn, bid, cats["groceries"], "2026-01", 300_00)
    budgets.set_line(conn, bid, cats["fuel"], "2026-01", 120_00)

    w = BudgetWidget(conn)
    w.set_period("2026-01")

    grow = _row_for_category(w, cats["groceries"])
    m = w.model
    assert m.data(m.index(grow, B), Qt.DisplayRole) == "300.00"
    assert m.data(m.index(grow, A), Qt.DisplayRole) == "250.00"
    assert m.data(m.index(grow, R), Qt.DisplayRole) == "50.00"

    frow = _row_for_category(w, cats["fuel"])
    assert m.data(m.index(frow, A), Qt.DisplayRole) == "100.00"
    assert m.data(m.index(frow, R), Qt.DisplayRole) == "20.00"

    # A month with no spending and no lines shows an empty table.
    w.set_period("2026-02")
    assert w.model.rowCount() == 0
    w.deleteLater()


def test_add_category_line_then_edit(qapp, conn, seeded, cats):
    # 'salary' has no spending in Feb; adding it must surface an editable row.
    salary = ledger.resolve_category(conn, "Salary")
    bid = budgets.create_budget(conn, "Plan")
    w = BudgetWidget(conn)
    w.set_period("2026-02")
    assert w.model.rowCount() == 0

    w.add_category(salary)
    assert w.model.rowCount() == 1
    row = _row_for_category(w, salary)
    assert w.model.row_at(row).budgeted_cents == 0

    m = w.model
    m.setData(m.index(row, B), "42.50", Qt.EditRole)
    lines = {ln.category_id: ln.amount_cents
             for ln in budgets.get_lines(conn, bid, period="2026-02")}
    assert lines[salary] == 42_50
    w.deleteLater()


def test_delete_budget_confirmation_seam(qapp, conn, monkeypatch):
    bid = budgets.create_budget(conn, "Temp")
    w = BudgetWidget(conn)
    assert w.current_budget_id() == bid

    monkeypatch.setattr(budget_widget.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    w._on_delete()
    assert budgets.get_budget(conn, bid) is not None  # declined -> still there
    assert w.budget_combo.count() == 1

    monkeypatch.setattr(budget_widget.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    w._on_delete()
    assert budgets.get_budget(conn, bid) is None
    assert w.budget_combo.count() == 0
    w.deleteLater()


def test_active_checkbox_syncs_domain(qapp, conn):
    bid = budgets.create_budget(conn, "Plan", active=True)
    w = BudgetWidget(conn)
    assert w.active_check.isChecked() is True

    w.active_check.setChecked(False)  # user toggle -> budgets.set_active
    assert budgets.get_budget(conn, bid).active is False

    w.active_check.setChecked(True)
    assert budgets.get_budget(conn, bid).active is True
    w.deleteLater()
