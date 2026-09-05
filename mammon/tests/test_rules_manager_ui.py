"""The Rules Manager dialog (mammon/ui/rules_manager_widget.py): a thin
projection over category_rules / transfer_rules / categorize. Exercises adding a
category rule with the migration-40 amount-range + memo conditions, adding a
transfer rule scoped to an account, inline condition edits through the model
(the MoneyDelegate / account-scope path), and delete/forget through the
QMessageBox.question seam. Synthetic data only -- no PII."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QMessageBox

from mammon import categorize, category_rules, db, ledger, transfer_rules
from mammon.ui import rules_manager_widget
from mammon.ui.rules_manager_widget import RulesManagerWidget, _RuleModel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "rules_ui.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    checking = ledger.create_account(conn, "Checking", "checking")
    savings = ledger.create_account(conn, "Savings", "savings")
    coffee = ledger.create_category(conn, "Food:Coffee")
    return {"checking": checking, "savings": savings, "coffee": coffee}


def _rule_by_keyword(rules, keyword):
    for r in rules:
        if r["keyword"] == keyword:
            return r
    return None


def _row_for_keyword(model, keyword):
    for row in range(model.rowCount()):
        if model.data(model.index(row, _RuleModel.KEYWORD)) == keyword:
            return row
    return None


def test_add_category_rule_with_conditions_round_trips(qapp, conn, seeded):
    w = RulesManagerWidget(conn)
    w.add_category_rule(
        "STARBUCKS", seeded["coffee"],
        amount_min_cents=-2000, amount_max_cents=-100, memo_contains="latte")

    rule = _rule_by_keyword(category_rules.load_rules(conn), "STARBUCKS")
    assert rule is not None
    assert rule["category_id"] == seeded["coffee"]
    assert rule["amount_min_cents"] == -2000
    assert rule["amount_max_cents"] == -100
    assert rule["memo_contains"] == "latte"

    # get_rule agrees, and the widget's model shows the row.
    fetched = category_rules.get_rule(conn, rule["id"])
    assert fetched["memo_contains"] == "latte"
    assert _row_for_keyword(w.cat_model, "STARBUCKS") is not None
    w.deleteLater()


def test_add_transfer_rule_scoped_to_account(qapp, conn, seeded):
    w = RulesManagerWidget(conn)
    w.add_transfer_rule(
        "VENMO", seeded["savings"], account_id=seeded["checking"])

    rule = _rule_by_keyword(transfer_rules.load_rules(conn), "VENMO")
    assert rule is not None
    # payload is the transfer's far side; account_id is the scope condition.
    assert rule["transfer_account_id"] == seeded["savings"]
    assert rule["account_id"] == seeded["checking"]
    w.deleteLater()


def test_inline_condition_edit_persists_via_model(qapp, conn, seeded):
    w = RulesManagerWidget(conn)
    w.add_category_rule("AMAZON", seeded["coffee"])

    row = _row_for_keyword(w.cat_model, "AMAZON")
    assert row is not None
    # MoneyDelegate path: a typed dollar string parses to signed integer cents.
    assert w.cat_model.setData(
        w.cat_model.index(row, _RuleModel.MIN), "-50.00")
    # Account-scope path: the combo delegate hands the model an account id.
    assert w.cat_model.setData(
        w.cat_model.index(row, _RuleModel.SCOPE), seeded["checking"])

    rule = _rule_by_keyword(category_rules.load_rules(conn), "AMAZON")
    assert rule["amount_min_cents"] == -5000
    assert rule["account_id"] == seeded["checking"]
    # ...and re-teaching with only one condition preserves the others.
    assert w.cat_model.setData(
        w.cat_model.index(row, _RuleModel.MEMO), "prime")
    rule = _rule_by_keyword(category_rules.load_rules(conn), "AMAZON")
    assert rule["memo_contains"] == "prime"
    assert rule["amount_min_cents"] == -5000
    assert rule["account_id"] == seeded["checking"]
    w.deleteLater()


def test_delete_category_rule_through_question_seam(qapp, conn, seeded, monkeypatch):
    w = RulesManagerWidget(conn)
    w.add_category_rule("STARBUCKS", seeded["coffee"])
    assert _rule_by_keyword(category_rules.load_rules(conn), "STARBUCKS")

    monkeypatch.setattr(
        rules_manager_widget.QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.Yes))
    row = _row_for_keyword(w.cat_model, "STARBUCKS")
    w.cat_view.setCurrentIndex(w.cat_model.index(row, 0))
    w._on_delete_category()

    assert _rule_by_keyword(category_rules.load_rules(conn), "STARBUCKS") is None
    w.deleteLater()


def test_delete_declined_keeps_rule(qapp, conn, seeded, monkeypatch):
    w = RulesManagerWidget(conn)
    w.add_transfer_rule("VENMO", seeded["savings"])

    monkeypatch.setattr(
        rules_manager_widget.QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.No))
    row = _row_for_keyword(w.xfer_model, "VENMO")
    w.xfer_view.setCurrentIndex(w.xfer_model.index(row, 0))
    w._on_delete_transfer()

    assert _rule_by_keyword(transfer_rules.load_rules(conn), "VENMO") is not None
    w.deleteLater()


def test_learned_payee_mapping_list_and_forget(qapp, conn, seeded, monkeypatch):
    categorize.record_user_categorization(conn, "SAFEWAY #123", seeded["coffee"])
    assert len(categorize.list_mappings(conn)) == 1

    w = RulesManagerWidget(conn)
    assert w.map_model.rowCount() == 1

    monkeypatch.setattr(
        rules_manager_widget.QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.map_view.setCurrentIndex(w.map_model.index(0, 0))
    w._on_delete_mapping()

    assert categorize.list_mappings(conn) == []
    assert w.map_model.rowCount() == 0
    w.deleteLater()
