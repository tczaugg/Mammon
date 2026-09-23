"""Category Manager -- domain verbs (rename / reparent / merge) plus the
offscreen dialog that projects them.

The domain tests pin the behavior that makes a merge safe on a 40-year archive:
ids are preserved on a rename/reparent (so learned rules and budget lines keep
pointing at the right category), and a merge REPOINTS every reference onto the
survivor -- transactions, split lines, keyword rules, payee mappings, scheduled
payments and budget lines (summing colliding targets) -- rather than letting a
delete cascade them away. The widget tests confirm the dialog is a thin, headless
projection: the public verbs mutate through the domain and reload the tree, and
delete never orphans an in-use category (it reassigns first, via the
QMessageBox.question / replacement-dialog seams).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from mammon import budgets, category_rules, db, ledger, scheduled
from mammon.ui import categories_dialog
from mammon.ui.categories_dialog import CategoriesDialog
from mammon.tests import fresh_db


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "catmgr.db")
    yield c
    c.close()


# ---- seed helpers --------------------------------------------------------
def _acct(conn):
    return ledger.create_account(conn, "Checking", "bank")


def _cat(conn, path):
    return ledger.create_category(conn, path)


def _post(conn, acct, cat_id, amount=-1000, date="2024-03-15", payee="X"):
    return ledger.add_transaction(
        conn, acct, date, amount, category_id=cat_id, payee=payee)


def _cat_exists(conn, cat_id):
    return conn.execute(
        "SELECT COUNT(*) FROM categories WHERE id=?", (cat_id,)).fetchone()[0] == 1


# ======================================================================
# Domain: rename
# ======================================================================
def test_rename_keeps_id_and_learned_links(conn):
    cid = _cat(conn, "Auto")
    category_rules.upsert_rule(conn, "SHELL", cid)
    ledger.rename_category(conn, cid, "Vehicle")
    assert ledger.category_path(conn, cid) == "Vehicle"  # same id, new label
    row = conn.execute(
        "SELECT category_id FROM category_rules WHERE keyword='SHELL'").fetchone()
    assert row["category_id"] == cid                     # link survived the rename


def test_rename_rejects_sibling_collision_case_insensitive(conn):
    _cat(conn, "Food")
    fuel = _cat(conn, "Fuel")
    with pytest.raises(ValueError):
        ledger.rename_category(conn, fuel, "food")


def test_rename_rejects_colon(conn):
    cid = _cat(conn, "Food")
    with pytest.raises(ValueError):
        ledger.rename_category(conn, cid, "Food:Sub")


def test_rename_rejects_blank(conn):
    cid = _cat(conn, "Food")
    with pytest.raises(ValueError):
        ledger.rename_category(conn, cid, "   ")


def test_rename_allows_same_name_under_different_parents(conn):
    _cat(conn, "Auto:Gas")
    heat = _cat(conn, "Home:Heating")
    ledger.rename_category(conn, heat, "Gas")             # different parent: OK
    assert ledger.category_path(conn, heat) == "Home:Gas"


# ======================================================================
# Domain: reparent
# ======================================================================
def test_reparent_moves_subtree(conn):
    auto = _cat(conn, "Auto")
    gas = _cat(conn, "Auto:Gas")
    veh = _cat(conn, "Vehicle")
    ledger.reparent_category(conn, gas, veh)
    assert ledger.category_path(conn, gas) == "Vehicle:Gas"
    assert ledger.category_path(conn, auto) == "Auto"


def test_reparent_to_top_level(conn):
    _cat(conn, "Auto")
    gas = _cat(conn, "Auto:Gas")
    ledger.reparent_category(conn, gas, None)
    assert ledger.category_path(conn, gas) == "Gas"


def test_reparent_rejects_cycle(conn):
    auto = _cat(conn, "Auto")
    gas = _cat(conn, "Auto:Gas")
    with pytest.raises(ValueError):
        ledger.reparent_category(conn, auto, gas)        # parent under its child


def test_reparent_rejects_self(conn):
    auto = _cat(conn, "Auto")
    with pytest.raises(ValueError):
        ledger.reparent_category(conn, auto, auto)


def test_reparent_rejects_sibling_collision(conn):
    _cat(conn, "Vehicle:Gas")
    auto_gas = _cat(conn, "Auto:Gas")
    veh = ledger.resolve_category(conn, "Vehicle")
    with pytest.raises(ValueError):
        ledger.reparent_category(conn, auto_gas, veh)    # Vehicle already has Gas


# ======================================================================
# Domain: merge
# ======================================================================
def test_merge_repoints_transactions_and_splits(conn):
    acct = _acct(conn)
    a = _cat(conn, "Auto")
    b = _cat(conn, "Vehicle")
    t = _post(conn, acct, a)
    n = ledger.merge_category(conn, a, b)
    assert n == 1
    assert ledger.get_transaction(conn, t)["category_id"] == b
    assert not _cat_exists(conn, a)                       # source removed


def test_merge_repoints_rules_scheduled_and_mappings(conn):
    acct = _acct(conn)
    a = _cat(conn, "Auto")
    b = _cat(conn, "Vehicle")
    category_rules.upsert_rule(conn, "SHELL", a)
    scheduled.add_scheduled(
        conn, acct, payee="DMV", amount=-5000, frequency="monthly",
        next_date="2024-04-01", category_id=a)
    conn.execute(
        "INSERT INTO import_mappings(payee_pattern, mapped_category_id, source) "
        "VALUES ('SHELL OIL', ?, 'user')", (a,))
    conn.commit()

    ledger.merge_category(conn, a, b)

    assert conn.execute(
        "SELECT category_id FROM category_rules WHERE keyword='SHELL'"
    ).fetchone()["category_id"] == b
    assert conn.execute(
        "SELECT category_id FROM scheduled_payments WHERE payee='DMV'"
    ).fetchone()["category_id"] == b
    assert conn.execute(
        "SELECT mapped_category_id FROM import_mappings WHERE payee_pattern='SHELL OIL'"
    ).fetchone()["mapped_category_id"] == b


def test_merge_repoints_noncolliding_budget_line(conn):
    a = _cat(conn, "Auto")
    b = _cat(conn, "Vehicle")
    bud = budgets.create_budget(conn, "2024")
    budgets.set_line(conn, bud, a, "2024-03", -5000)
    ledger.merge_category(conn, a, b)
    row = conn.execute(
        "SELECT category_id, amount_cents FROM budget_lines WHERE budget_id=?",
        (bud,)).fetchone()
    assert row["category_id"] == b
    assert row["amount_cents"] == -5000


def test_merge_sums_colliding_budget_lines(conn):
    a = _cat(conn, "Auto")
    b = _cat(conn, "Vehicle")
    bud = budgets.create_budget(conn, "2024")
    budgets.set_line(conn, bud, a, "2024-03", -5000)
    budgets.set_line(conn, bud, b, "2024-03", -3000)     # same budget+period
    ledger.merge_category(conn, a, b)
    rows = conn.execute(
        "SELECT category_id, amount_cents FROM budget_lines WHERE budget_id=?",
        (bud,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["category_id"] == b
    assert rows[0]["amount_cents"] == -8000              # targets summed


def test_merge_moves_children_to_survivor(conn):
    acct = _acct(conn)
    auto = _cat(conn, "Auto")
    auto_gas = _cat(conn, "Auto:Gas")
    veh = _cat(conn, "Vehicle")
    t = _post(conn, acct, auto_gas)
    ledger.merge_category(conn, auto, veh)
    assert not _cat_exists(conn, auto)
    assert ledger.category_path(conn, auto_gas) == "Vehicle:Gas"
    assert ledger.get_transaction(conn, t)["category_id"] == auto_gas


def test_merge_recursively_merges_colliding_child(conn):
    acct = _acct(conn)
    auto = _cat(conn, "Auto")
    auto_gas = _cat(conn, "Auto:Gas")
    veh = _cat(conn, "Vehicle")
    veh_gas = _cat(conn, "Vehicle:Gas")
    t = _post(conn, acct, auto_gas)
    ledger.merge_category(conn, auto, veh)
    assert not _cat_exists(conn, auto)
    assert not _cat_exists(conn, auto_gas)               # collided child folded in
    assert ledger.get_transaction(conn, t)["category_id"] == veh_gas


def test_merge_rejects_self(conn):
    a = _cat(conn, "Auto")
    with pytest.raises(ValueError):
        ledger.merge_category(conn, a, a)


def test_merge_rejects_descendant(conn):
    auto = _cat(conn, "Auto")
    gas = _cat(conn, "Auto:Gas")
    with pytest.raises(ValueError):
        ledger.merge_category(conn, auto, gas)           # into its own child


# ======================================================================
# Widget: thin projection over the domain, headless
# ======================================================================
def test_widget_add_reloads_tree_and_signals(qapp, conn):
    w = CategoriesDialog(conn)
    fired = []
    w.changed.connect(lambda: fired.append(1))
    cid = w.add_category("Groceries")
    assert cid in w._nodes
    assert cid in w._items
    assert fired == [1]


def test_widget_rename_updates_tree(qapp, conn):
    cid = _cat(conn, "Auto")
    w = CategoriesDialog(conn)
    w.rename_category(cid, "Vehicle")
    assert w._nodes[cid]["path"] == "Vehicle"


def test_widget_reparent_updates_tree(qapp, conn):
    _cat(conn, "Auto")
    gas = _cat(conn, "Auto:Gas")
    veh = _cat(conn, "Vehicle")
    w = CategoriesDialog(conn)
    w.reparent_category(gas, veh)
    assert w._nodes[gas]["path"] == "Vehicle:Gas"


def test_widget_merge_removes_source_from_tree(qapp, conn):
    acct = _acct(conn)
    a = _cat(conn, "Auto")
    b = _cat(conn, "Vehicle")
    t = _post(conn, acct, a)
    w = CategoriesDialog(conn)
    w.merge_into(a, b)
    assert a not in w._nodes                              # source gone from the tree
    assert ledger.get_transaction(conn, t)["category_id"] == b


def test_widget_delete_unused_confirm_yes(qapp, conn, monkeypatch):
    cid = _cat(conn, "Spare")
    w = CategoriesDialog(conn)
    w.tree.setCurrentItem(w._items[cid])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w._on_delete()
    assert cid not in w._nodes


def test_widget_delete_unused_confirm_no(qapp, conn, monkeypatch):
    cid = _cat(conn, "Spare")
    w = CategoriesDialog(conn)
    w.tree.setCurrentItem(w._items[cid])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    w._on_delete()
    assert cid in w._nodes                                # declined -> still present


def test_widget_delete_in_use_reassigns_not_orphans(qapp, conn, monkeypatch):
    acct = _acct(conn)
    doomed = _cat(conn, "Auto")
    keep = _cat(conn, "Vehicle")
    t = _post(conn, acct, doomed)
    w = CategoriesDialog(conn)
    w.tree.setCurrentItem(w._items[doomed])

    class _FakeReplace:
        def __init__(self, *a, **k):
            pass

        def exec_(self):
            return QDialog.Accepted

        def value(self):
            return "Vehicle"

    monkeypatch.setattr(categories_dialog, "ReplacementCategoryDialog", _FakeReplace)
    w._on_delete()

    assert doomed not in w._nodes                         # category deleted
    txn = ledger.get_transaction(conn, t)
    assert txn["category_id"] == keep                     # reassigned, not orphaned
    assert txn["category_id"] is not None
