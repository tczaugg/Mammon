"""Learned Categories (Tools menu) and the payee completer's category lines.

Every learned behavior is a decision tree (a design ruling); the keyword
Rules Manager is gone with its tables (migration 116). This window shows what
the category tree learned for each payee -- every category and transfer account
with its count, overall and per account -- and Forget is its one action. The
register's payee completer lists each payee once per category it has carried,
most used first, and picking a line fills the category too.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from mammon import categorize, category_tree, ledger
from mammon.tests import fresh_db
from mammon.ui.delegates import (CATEGORY_ROLE, PAYEE_ROLE, PayeeCompleter,
                                 payee_completion_rows)
from mammon.ui.models import RegisterModel
from mammon.ui.rules_manager_widget import LearnedCategoriesDialog, label_text


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def book(conn):
    chk = ledger.create_account(conn, "Checking", "checking")
    biz = ledger.create_account(conn, "Business Checking", "checking")
    card = ledger.create_account(conn, "Store Card", "credit")
    groceries = ledger.resolve_category(conn, "Groceries")
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    supplies = ledger.resolve_category(conn, "Office Supplies")
    for i, cat in enumerate([groceries] * 4 + [fuel] * 2):
        ledger.add_transaction(conn, chk, f"2026-01-{i + 1:02d}", -1000,
                               payee="Costco", category_id=cat)
        categorize.record_user_categorization(conn, "Costco", cat, account_id=chk)
    categorize.record_user_categorization(conn, "Costco", supplies, account_id=biz)
    for _ in range(3):
        categorize.record_user_categorization(conn, "Anybank", None, account_id=chk,
                                              transfer_account_id=card)
    ledger.add_transaction(conn, chk, "2026-02-01", -5000, payee="Anybank")
    return {"chk": chk, "biz": biz, "card": card, "groceries": groceries,
            "fuel": fuel, "supplies": supplies}


def _yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------
def test_lists_each_payee_with_its_choices_and_counts(qapp, conn, book):
    dlg = LearnedCategoriesDialog(conn)
    names = [dlg.payees.item(i, dlg.PAYEE).text() for i in range(dlg.payees.rowCount())]
    assert names == ["Costco", "Anybank"]              # most used first
    dlg.payees.setCurrentCell(0, 0)
    rows = [(dlg.labels.item(i, dlg.LABEL).text(), dlg.labels.item(i, dlg.LABEL_TIMES).text(),
             dlg.labels.item(i, dlg.BY_ACCOUNT).text())
            for i in range(dlg.labels.rowCount())]
    assert rows[0] == ("Groceries", "4", "Checking: 4")
    assert rows[1] == ("Auto:Fuel", "2", "Checking: 2")
    assert rows[2] == ("Office Supplies", "1", "Business Checking: 1")
    dlg.payees.setCurrentCell(1, 0)
    assert dlg.labels.item(0, dlg.LABEL).text() == "[Store Card]"   # a transfer
    dlg.deleteLater()


def test_find_filters_payees(qapp, conn, book):
    dlg = LearnedCategoriesDialog(conn)
    dlg.filter_edit.setText("any")
    assert dlg.payees.rowCount() == 1
    assert dlg.selected_payee()["name"] == "Anybank"
    dlg.deleteLater()


def test_forget_a_payee(qapp, conn, book, monkeypatch):
    _yes(monkeypatch)
    dlg = LearnedCategoriesDialog(conn)
    dlg.filter_edit.setText("costco")
    assert dlg.forget_selected_payee()
    assert category_tree.known_categories(conn, "Costco") == []
    assert category_tree.labels_by_account(conn, "Costco") == {}
    dlg.filter_edit.setText("")
    assert dlg.payees.rowCount() == 1
    dlg.deleteLater()


def test_forget_one_choice_keeps_the_rest(qapp, conn, book, monkeypatch):
    _yes(monkeypatch)
    dlg = LearnedCategoriesDialog(conn)
    dlg.filter_edit.setText("costco")
    dlg.labels.setCurrentCell(1, 0)                    # Auto:Fuel
    assert dlg.forget_selected_label()
    assert category_tree.known_categories(conn, "Costco") == [
        (book["groceries"], 4), (book["supplies"], 1)]
    dlg.deleteLater()


def test_forget_asks_first(qapp, conn, book, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    dlg = LearnedCategoriesDialog(conn)
    assert not dlg.forget_selected_payee()
    assert category_tree.known_categories(conn, "Costco") != []
    dlg.deleteLater()


def test_a_forgotten_payee_stays_forgotten_through_a_rebuild(conn, book):
    """The explicit seed-from-history rebuild must not bring back what the
    user told Mammon to forget."""
    category_tree.forget_payee(conn, "Costco")
    category_tree.bootstrap(conn)
    assert category_tree.known_categories(conn, "Costco") == []


def test_label_text(conn, book):
    assert label_text(conn, book["fuel"]) == "Auto:Fuel"
    assert label_text(conn, category_tree.account_label(book["card"])) == "[Store Card]"


# ---------------------------------------------------------------------------
# the payee completer
# ---------------------------------------------------------------------------
def test_completion_rows_list_a_payee_once_per_category_most_used_first():
    variants = {"Costco": [("Groceries", 41), ("Auto:Fuel", 12)]}
    rows = payee_completion_rows("cost", ["Costco", "Cost Plus"],
                                 lambda p: variants.get(p, []))
    assert rows == [
        ("Costco — Groceries (41)", "Costco", "Groceries"),
        ("Costco — Auto:Fuel (12)", "Costco", "Auto:Fuel"),
        ("Cost Plus", "Cost Plus", None),
    ]
    assert payee_completion_rows("", ["Costco"], lambda p: []) == []


def test_register_variants_prefer_this_accounts_history(conn, book):
    home = RegisterModel(conn, book["chk"])
    assert home.payee_variants("Costco") == [("Groceries", 4), ("Auto:Fuel", 2)]
    work = RegisterModel(conn, book["biz"])
    assert work.payee_variants("Costco") == [("Office Supplies", 1)]
    # a payee the account has never used falls back to every account's
    assert work.payee_variants("Anybank") == [("[Store Card]", 3)]


def test_picking_a_line_inserts_the_payee_and_remembers_the_category(qapp, conn, book):
    m = RegisterModel(conn, book["chk"])
    comp = PayeeCompleter(m.payee_choices(), None, variants=m.payee_variants)
    comp.splitPath("cost")
    model = comp.model()
    displays = [model.index(i, 0).data() for i in range(model.rowCount())]
    assert displays[:2] == ["Costco — Groceries (4)", "Costco — Auto:Fuel (2)"]
    fuel_line = model.index(1, 0)
    assert fuel_line.data(PAYEE_ROLE) == "Costco"
    assert fuel_line.data(CATEGORY_ROLE) == "Auto:Fuel"
    comp._remember(fuel_line)
    assert comp.category_for("Costco") == "Auto:Fuel"
    assert comp.category_for("Somebody Else") is None


def test_blank_row_takes_the_picked_lines_category(qapp, conn, book):
    """The delegate writes the picked line's category before the payee, so a
    blank row with a date and amount already typed commits with it."""
    from PyQt5.QtWidgets import QStyleOptionViewItem, QTableView, QWidget

    from mammon.ui.delegates import PayeeTwoLineDelegate
    m = RegisterModel(conn, book["chk"])
    view = QTableView()
    view.setModel(m)
    delegate = PayeeTwoLineDelegate(view)
    blank = m.index(m.rowCount() - 1, RegisterModel.PAYEE)
    m.setData(m.index(blank.row(), RegisterModel.DATE), "2026-03-01", Qt.EditRole)
    host = QWidget()                     # kept alive: it owns the editor
    editor = delegate.createEditor(host, QStyleOptionViewItem(), blank)
    comp = editor.completer()
    comp.splitPath("cost")
    comp._remember(comp.model().index(1, 0))           # Costco - Auto:Fuel
    editor.setText("Costco")
    delegate.setModelData(editor, m, blank)
    assert m.blank_values()["category"] == "Auto:Fuel"
    view.deleteLater()
