"""Amazon invoice itemization, phase 2: review-flow integration.

Phase 1 (``mammon.importers.amazon_items``) parsed an invoice and allocated one
split leg per item with tax and any gift-card / reward-point offset spread
PROPORTIONALLY so the legs net to the charged amount in exact integer cents.
This suite covers phase 2: turning a loaded invoice file into review rows through
:mod:`mammon.import_review` (the SOLE writer of the review flow) and accepting
them -- either creating a NEW card charge with its item splits, or itemizing an
already-accepted Amazon charge a review row MATCHES (requirement A8).

Amazon invoice data is NEVER persisted (requirement A6): the review lives only in
memory (``ReviewEntry.amazon_alloc``) and every write funnels through
:func:`ledger.add_transaction` / :func:`ledger.set_splits`, so no second write
path is introduced. Fixtures are synthetic (ANON order numbers, no PII); money is
signed integer cents.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger, import_review


# --- synthetic invoice fixture (newer priceAccounting scrape schema) --------
def _order_rec(number, order_date, subtotal, grand, items,
               tax=None, gift_card=None, rewards=None, card="0000"):
    """One order record. ``items`` is a list of ``(description, price_string)``.
    Amounts are plain '$x.xx' strings, matching the real scrape."""
    acct = [{"fieldName": "Item(s) Subtotal:", "fieldPrice": subtotal},
            {"fieldName": "Shipping & Handling:", "fieldPrice": "$0.00"}]
    if tax is not None:
        acct.append({"fieldName": "Estimated tax to be collected:", "fieldPrice": tax})
    if gift_card is not None:
        acct.append({"fieldName": "Gift Card Amount:", "fieldPrice": gift_card})
    if rewards is not None:
        acct.append({"fieldName": "Rewards Points:", "fieldPrice": rewards})
    acct.append({"fieldName": "Grand Total:", "fieldPrice": grand})
    return {
        "orderNumber": number,
        "orderDate": order_date,
        "orderPrice": subtotal,
        "cardNumber": card,
        "cardType": "Visa",
        "priceAccounting": acct,
        "items": [{"itemDescription": d, "itemPrice": p} for d, p in items],
    }


def _write_invoice(tmp_path, orders, name="amazon_invoices.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"numOrders": str(len(orders)), "orders": orders}),
                    encoding="utf-8")
    return str(path)


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def card(conn):
    return ledger.create_account(conn, "Visa Card", "credit")


def _one_order_file(tmp_path):
    """One $65 order: $80 of items, $10 gift card + $5 rewards offsetting it."""
    rec = _order_rec(
        "ANON-1001", "March 1, 2026",
        subtotal="$80.00", grand="$65.00", gift_card="$10.00", rewards="$5.00",
        items=[("ANON Widget A", "$30.00"), ("ANON Widget B", "$50.00")],
    )
    return _write_invoice(tmp_path, [rec])


# --- building the review (NEW) ---------------------------------------------
def test_build_amazon_review_creates_new_rows_with_allocation(conn, card, tmp_path):
    path = _one_order_file(tmp_path)
    entries = import_review.build_amazon_review(conn, card, path)
    assert len(entries) == 1
    e = entries[0]
    assert e.label == import_review.LABEL_NEW
    assert e.matched_txn_id is None
    # The card charge is a debit: signed negative cents, magnitude == Grand Total.
    assert e.mapped.amount_cents == -6500
    assert e.mapped.payee == "Amazon"
    # The proposed per-item split rides on the entry, never persisted.
    assert e.amazon_alloc is not None
    assert e.amazon_alloc.total_cents == -6500
    assert len(e.amazon_alloc.item_legs) == 2


def test_build_amazon_review_skips_zero_charge_order(conn, card, tmp_path):
    # Fully covered by a gift-card balance -> never hit the card -> nothing to
    # reconcile on the card register.
    rec = _order_rec(
        "ANON-2002", "April 2, 2026",
        subtotal="$40.00", grand="$0.00", gift_card="$40.00",
        items=[("ANON Widget C", "$40.00")],
    )
    path = _write_invoice(tmp_path, [rec])
    assert import_review.build_amazon_review(conn, card, path) == []


# --- accepting a NEW row ----------------------------------------------------
def test_accept_amazon_new_writes_one_leg_per_item(conn, card, tmp_path):
    path = _one_order_file(tmp_path)
    [entry] = import_review.build_amazon_review(conn, card, path)

    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    txn_id = import_review.accept_amazon_new(conn, card, entry)
    after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert after == before + 1

    txn = ledger.get_transaction(conn, txn_id)
    assert txn["payee"] == "Amazon"
    assert txn["amount"] == -6500
    # A split parent carries no category of its own.
    assert txn["category_id"] is None
    assert ledger.has_splits(conn, txn_id)

    splits = ledger.get_splits(conn, txn_id)
    # Two item legs + gift-card leg + rewards leg.
    assert len(splits) == 4
    assert sum(s["amount"] for s in splits) == -6500

    kinds = {s["category_label"].lower(): s for s in splits}
    # Gift card and reward points are CATEGORIES by those names (A3/A5), money-in.
    assert kinds["gift cards"]["amount"] == 1000
    assert kinds["reward points"]["amount"] == 500
    # ... and NOT accounts.
    assert conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE LOWER(name) IN ('gift cards', "
        "'reward points')").fetchone()[0] == 0

    # Item legs default to 'household' (A1) and net to the item portion.
    item_legs = [s for s in splits if s["category_label"].lower() == "household"]
    assert len(item_legs) == 2
    assert sum(s["amount"] for s in item_legs) == -8000
    memos = {s["memo"] for s in item_legs}
    assert memos == {"ANON Widget A", "ANON Widget B"}


# --- matching an already-accepted Amazon charge (A8) ------------------------
def test_build_amazon_review_matches_existing_amazon_charge(conn, card, tmp_path):
    # The card charge was downloaded and accepted first, categorised whole.
    shopping = ledger.resolve_category(conn, "Shopping")
    existing = ledger.add_transaction(
        conn, card, "2026-03-01", -6500, payee="Amazon",
        memo="AMZN MKTP US", category_id=shopping)

    path = _one_order_file(tmp_path)
    [entry] = import_review.build_amazon_review(conn, card, path)
    assert entry.label == import_review.LABEL_MATCHING
    assert entry.matched_txn_id == existing
    assert entry.match_method == "date+amount"


def test_accept_amazon_match_itemizes_existing_charge_without_duplicate(conn, card, tmp_path):
    shopping = ledger.resolve_category(conn, "Shopping")
    existing = ledger.add_transaction(
        conn, card, "2026-03-01", -6500, payee="Amazon",
        memo="AMZN MKTP US", category_id=shopping)
    assert not ledger.has_splits(conn, existing)

    path = _one_order_file(tmp_path)
    [entry] = import_review.build_amazon_review(conn, card, path)

    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    txn_id = import_review.accept_amazon_match(conn, entry)
    after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    # Itemizing REPLACES the charge's splits -- it never adds a duplicate row.
    assert txn_id == existing
    assert after == before

    txn = ledger.get_transaction(conn, existing)
    assert txn["amount"] == -6500          # amount and date untouched
    assert txn["date"] == "2026-03-01"
    assert txn["category_id"] is None      # now a split parent
    assert ledger.has_splits(conn, existing)

    splits = ledger.get_splits(conn, existing)
    assert len(splits) == 4
    assert sum(s["amount"] for s in splits) == -6500
    labels = {s["category_label"].lower() for s in splits}
    assert "gift cards" in labels and "reward points" in labels
    assert "shopping" not in labels        # the old whole-charge category is gone


def test_amazon_match_requires_an_amazon_payee(conn, card, tmp_path):
    # A same-day, same-amount charge from an unrelated merchant must NOT be
    # silently itemized as Amazon: the match is gated on payee LIKE '%amazon%'.
    ledger.add_transaction(
        conn, card, "2026-03-01", -6500, payee="Target", memo="TARGET T-1234")

    path = _one_order_file(tmp_path)
    [entry] = import_review.build_amazon_review(conn, card, path)
    assert entry.label == import_review.LABEL_NEW
    assert entry.matched_txn_id is None


def test_reload_after_accept_reclassifies_as_matching(conn, card, tmp_path):
    # A6: the file is re-loadable across sessions. After accepting a NEW row the
    # now-existing Amazon charge re-classifies as MATCHING on the next load, so a
    # second accept updates rather than duplicates.
    path = _one_order_file(tmp_path)
    [first] = import_review.build_amazon_review(conn, card, path)
    txn_id = import_review.accept_amazon_new(conn, card, first)

    [second] = import_review.build_amazon_review(conn, card, path)
    assert second.label == import_review.LABEL_MATCHING
    assert second.matched_txn_id == txn_id
    # And re-accepting the match adds no new transaction.
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    import_review.accept_amazon_match(conn, second)
    after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert after == before


# --- UI: thin projection over the domain layer ------------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    # review_visibility() reads QSettings; keep it out of the real profile.
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


def test_cash_register_reveals_load_action_investment_hides_it(qapp, conn, card):
    from mammon.ui.widgets import InvestmentRegisterWidget, RegisterWidget
    reg = RegisterWidget(conn, card)
    try:
        # Cash register: the gear action and the panel button are both offered.
        # (The panel is created hidden, so assert the button's own hidden state
        # rather than isVisible(), which also folds in ancestor visibility.)
        assert reg.toolbar.act_load_invoices.isVisible()
        assert not reg.review_panel.load_amazon_btn.isHidden()
    finally:
        reg.deleteLater()

    inv = ledger.create_account(conn, "Broker", "investment")
    ireg = InvestmentRegisterWidget(conn, inv)
    try:
        # Investment register shares the toolbar but keeps the action hidden, and
        # its review panel offers no invoice button (no item-split concept).
        assert not ireg.toolbar.act_load_invoices.isVisible()
        assert ireg.review_panel.load_amazon_btn.isHidden()
    finally:
        ireg.deleteLater()


def test_gear_action_routes_to_panel_load(qapp, conn, card, monkeypatch):
    from mammon.ui.widgets import RegisterWidget
    reg = RegisterWidget(conn, card)
    try:
        calls = []
        monkeypatch.setattr(reg.review_panel, "load_amazon_invoices",
                            lambda *a, **k: calls.append(True))
        reg.toolbar.act_load_invoices.trigger()
        assert calls == [True]
    finally:
        reg.deleteLater()


def test_panel_load_then_accept_new_posts_the_split(qapp, conn, card, tmp_path):
    from mammon.ui.widgets import RegisterWidget
    path = _one_order_file(tmp_path)
    reg = RegisterWidget(conn, card)
    try:
        n = reg.review_panel.load_amazon_invoices(path=path)
        assert n == 1
        assert not reg.review_panel.isHidden()
        assert reg.review_panel.pending_count() == 1
        # Accept via the panel's Accept button slot (row 0 is auto-selected). It
        # must route to the Amazon split writer, not the single-category path.
        reg.review_panel._on_accept()
        # The card charge posted with its item split, and the panel emptied.
        assert reg.review_panel.pending_count() == 0
        assert ledger.account_balance(conn, card) == -6500
        [txn] = conn.execute(
            "SELECT id FROM transactions WHERE account_id=? AND payee='Amazon'",
            (card,)).fetchall()
        assert ledger.has_splits(conn, txn[0])
        assert len(ledger.get_splits(conn, txn[0])) == 4
    finally:
        reg.deleteLater()


def test_panel_load_then_accept_matching_updates_existing_charge(qapp, conn, card, tmp_path):
    from mammon.ui.widgets import RegisterWidget
    existing = ledger.add_transaction(
        conn, card, "2026-03-01", -6500, payee="Amazon", memo="AMZN MKTP US",
        category_id=ledger.resolve_category(conn, "Shopping"))
    path = _one_order_file(tmp_path)
    reg = RegisterWidget(conn, card)
    try:
        reg.review_panel.load_amazon_invoices(path=path)
        entry = reg.review_panel._entries[0]
        assert entry.label == import_review.LABEL_MATCHING
        before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        reg.review_panel._on_accept()
        after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        # Itemized the existing charge in place -- no duplicate row.
        assert after == before
        assert ledger.has_splits(conn, existing)
        assert len(ledger.get_splits(conn, existing)) == 4
    finally:
        reg.deleteLater()
