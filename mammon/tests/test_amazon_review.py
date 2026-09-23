"""Wiring the Amazon invoice itemizer (mammon/importers/amazon_items) into the
review-queue flow (mammon/import_review + the cash register's ImportReviewPanel).

What is under test:

* A time-tagged Amazon invoice JSON, loaded on demand, classifies each order that
  reached the card as NEW (no matching charge yet) or MATCHING an already-accepted
  Amazon register line -- and a MATCH updates that line's splits/categories
  instead of adding a duplicate (requirement A8).
* The per-item split the phase-1 allocator computes reaches the ledger EXACTLY:
  one leg per item, tax and any gift-card / reward offset spread proportionally,
  the legs summing to the charge to the cent, with gift-card / reward legs posted
  as CATEGORIES (never accounts -- requirement A3/A5).
* Nothing about the invoice is persisted (requirement A6): re-loading the same
  file after an accept simply re-classifies the now-existing charge as MATCHING.
* The single writers hold: the review flows through import_review, the rows
  through ledger. No new table, no second write path.

Everything here is synthetic -- ANON order numbers, invented item titles, made-up
amounts -- so nothing carries PII. Offscreen Qt for the one panel test.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, import_review, ledger
from mammon.tests import fresh_db


# ---------------------------------------------------------------------------
# synthetic invoice fixtures (newer 'priceAccounting' scrape schema)
# ---------------------------------------------------------------------------
def _order_rec(number, order_date, subtotal, grand, items,
               tax=None, gift_card=None, rewards=None, card="4321"):
    """One order record in the newer scrape schema. ``items`` is a list of
    ``(description, price_string)``; amounts are plain '$x.xx' strings."""
    acct = [{"fieldName": "Item(s) Subtotal:", "fieldPrice": subtotal},
            {"fieldName": "Shipping & Handling:", "fieldPrice": "$0.00"}]
    if tax is not None:
        acct.append({"fieldName": "Estimated tax to be collected:",
                     "fieldPrice": tax})
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


def _write_invoice(path, orders):
    path.write_text(json.dumps({"numOrders": str(len(orders)), "orders": orders}),
                    encoding="utf-8")
    return str(path)


def _two_item_gift_card_file(tmp_path):
    """The module docstring's worked example: subtotal $80, tax $6, gift card $30
    -> charge -$56.00; item list prices $30 / $50 -> legs -32.25 / -53.75, gift
    card +30.00 -> sum -56.00."""
    rec = _order_rec(
        "ANON-0001", "March 1, 2026",
        subtotal="$80.00", tax="$6.00", gift_card="$30.00", grand="$56.00",
        items=[("ANON Widget B1", "$30.00"), ("ANON Widget B2", "$50.00")],
    )
    return _write_invoice(tmp_path / "invoices.json", [rec])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def card(conn):
    return ledger.create_account(conn, "ANON Rewards Visa", "credit")


def _txn_count(conn, account_id):
    return conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id=?",
        (account_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# NEW: no matching charge yet -> a fresh split transaction
# ---------------------------------------------------------------------------
def test_new_order_creates_split_transaction(conn, card, tmp_path):
    path = _two_item_gift_card_file(tmp_path)

    entries = import_review.build_amazon_review(conn, card, path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.is_new
    assert entry.matched_txn_id is None
    assert entry.mapped.payee == "Amazon"
    assert entry.mapped.date == "2026-03-01"
    assert entry.mapped.amount_cents == -5600      # signed debit == -Grand Total

    txn_id = import_review.accept_amazon_new(conn, card, entry)

    txn = ledger.get_transaction(conn, txn_id)
    assert txn["payee"] == "Amazon"
    assert txn["amount"] == -5600
    assert txn["category_id"] is None              # parent shows --Split--

    splits = ledger.get_splits(conn, txn_id)
    assert [s["amount"] for s in splits] == [-3225, -5375, 3000]
    # item legs carry the default 'household' category; the offset is a CATEGORY
    assert [s["category_label"] for s in splits] == \
        ["household", "household", "gift cards"]
    # EXACTNESS end to end: the legs sum to the charge to the cent, so set_splits
    # added no balancing 'uncategorized' leg.
    assert sum(s["amount"] for s in splits) == txn["amount"] == -5600
    assert all(s["category_id"] is not None for s in splits)


def test_reward_points_offset_is_a_category_leg(conn, card, tmp_path):
    # subtotal $40, reward points $10 -> charge -$30.00; one item, so the item
    # portion ($40) rides a single leg and the reward is a +$10 category leg.
    rec = _order_rec(
        "ANON-0002", "April 2, 2026",
        subtotal="$40.00", rewards="$10.00", grand="$30.00",
        items=[("ANON Gadget", "$40.00")],
    )
    path = _write_invoice(tmp_path / "inv.json", [rec])

    (entry,) = import_review.build_amazon_review(conn, card, path)
    txn_id = import_review.accept_amazon_new(conn, card, entry)

    splits = ledger.get_splits(conn, txn_id)
    assert [s["amount"] for s in splits] == [-4000, 1000]
    assert [s["category_label"] for s in splits] == ["household", "reward points"]
    assert sum(s["amount"] for s in splits) == -3000


def test_single_item_no_offset_is_plain_categorised_row(conn, card, tmp_path):
    # One item, no tax, no offset -> a lone leg. A split needs two legs, so this
    # collapses to a plain categorised transaction, not a split.
    rec = _order_rec(
        "ANON-0003", "May 3, 2026",
        subtotal="$25.00", grand="$25.00",
        items=[("ANON Cable", "$25.00")],
    )
    path = _write_invoice(tmp_path / "inv.json", [rec])

    (entry,) = import_review.build_amazon_review(conn, card, path)
    txn_id = import_review.accept_amazon_new(conn, card, entry)

    txn = ledger.get_transaction(conn, txn_id)
    assert txn["amount"] == -2500
    assert ledger.get_splits(conn, txn_id) == []       # not split
    assert txn["category_id"] is not None              # plain 'household'
    assert ledger.category_path(conn, txn["category_id"]) == "household"


# ---------------------------------------------------------------------------
# MATCHING: an already-accepted Amazon charge -> update its splits, no duplicate
# ---------------------------------------------------------------------------
def test_matching_updates_existing_txn_without_duplicate(conn, card, tmp_path):
    # The card charge is already in the register (payee 'Amazon', same amount,
    # a day off), accepted and reconciled -- exactly what a normal download left.
    existing = ledger.add_transaction(
        conn, card, "2026-03-02", -5600, payee="Amazon.com",
        memo="AMZN Mktp US", cleared=1, reconciled=1)
    assert _txn_count(conn, card) == 1

    path = _two_item_gift_card_file(tmp_path)
    (entry,) = import_review.build_amazon_review(conn, card, path)
    assert entry.is_matching
    assert entry.matched_txn_id == existing

    txn_id = import_review.accept_amazon_match(conn, entry)
    assert txn_id == existing
    assert _txn_count(conn, card) == 1                  # itemized in place, no dup

    txn = ledger.get_transaction(conn, txn_id)
    assert txn["category_id"] is None                  # now --Split--
    assert txn["cleared"] == 1 and txn["reconciled"] == 1   # user's line untouched
    assert txn["amount"] == -5600                      # amount/date not clobbered
    assert txn["date"] == "2026-03-02"

    splits = ledger.get_splits(conn, txn_id)
    assert [s["amount"] for s in splits] == [-3225, -5375, 3000]
    assert sum(s["amount"] for s in splits) == -5600


def test_matching_does_not_touch_a_non_amazon_same_amount_charge(conn, card,
                                                                 tmp_path):
    # A same-day, same-amount charge to a DIFFERENT merchant must never be
    # itemized: the payee gate keeps the invoice off it, so the order stays NEW.
    ledger.add_transaction(conn, card, "2026-03-01", -5600, payee="Hardware Store")

    path = _two_item_gift_card_file(tmp_path)
    (entry,) = import_review.build_amazon_review(conn, card, path)
    assert entry.is_new
    assert entry.matched_txn_id is None


# ---------------------------------------------------------------------------
# re-loadability: the file may be loaded across sessions; nothing is stored
# ---------------------------------------------------------------------------
def test_reloading_same_file_reclassifies_as_matching(conn, card, tmp_path):
    path = _two_item_gift_card_file(tmp_path)

    (first,) = import_review.build_amazon_review(conn, card, path)
    assert first.is_new
    txn_id = import_review.accept_amazon_new(conn, card, first)
    assert _txn_count(conn, card) == 1

    # Re-load the SAME file: the now-existing charge is recognised, so re-accepting
    # updates it rather than posting a second transaction.
    (second,) = import_review.build_amazon_review(conn, card, path)
    assert second.is_matching
    assert second.matched_txn_id == txn_id

    import_review.accept_amazon_match(conn, second)
    assert _txn_count(conn, card) == 1

    splits = ledger.get_splits(conn, txn_id)
    assert sum(s["amount"] for s in splits) == -5600

    # No table persisted the invoice: no review_items row was written for it.
    assert import_review.count_pending(conn, card) == 0


def test_zero_charge_order_is_skipped(conn, card, tmp_path):
    # An order fully covered by a gift-card balance never hits the card, so it
    # creates no review row for the card register.
    rec = _order_rec(
        "ANON-0004", "June 4, 2026",
        subtotal="$20.00", gift_card="$20.00", grand="$0.00",
        items=[("ANON Trinket", "$20.00")],
    )
    path = _write_invoice(tmp_path / "inv.json", [rec])
    assert import_review.build_amazon_review(conn, card, path) == []


def test_two_orders_do_not_both_claim_one_charge(conn, card, tmp_path):
    # Two identical-amount Amazon orders on the same day, but only ONE existing
    # charge: the first order claims it (MATCHING), the second stays NEW.
    ledger.add_transaction(conn, card, "2026-07-05", -3000, payee="Amazon")
    rec = _order_rec("ANON-A", "July 5, 2026", subtotal="$30.00", grand="$30.00",
                     items=[("ANON One", "$30.00")])
    rec2 = _order_rec("ANON-B", "July 5, 2026", subtotal="$30.00", grand="$30.00",
                      items=[("ANON Two", "$30.00")])
    path = _write_invoice(tmp_path / "inv.json", [rec, rec2])

    entries = import_review.build_amazon_review(conn, card, path)
    labels = sorted(e.label for e in entries)
    assert labels == ["MATCHING", "NEW"]


# ---------------------------------------------------------------------------
# UI wiring: the ImportReviewPanel loads a file and its Accept posts the split
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(tmp_path / "qsettings"))
    yield


def test_panel_loads_invoice_and_accepts_split(qapp, conn, card, tmp_path):
    from mammon.ui.import_review_widget import ImportReviewPanel

    path = _two_item_gift_card_file(tmp_path)
    panel = ImportReviewPanel(conn, card)
    # Feed the file path directly (the QFileDialog seam is bypassed in tests).
    n = panel.load_amazon_invoices(path)
    assert n == 1
    assert not panel.isHidden()

    txn_id = panel.accept_amazon_index(0)
    splits = ledger.get_splits(conn, txn_id)
    assert [s["amount"] for s in splits] == [-3225, -5375, 3000]
    assert sum(s["amount"] for s in splits) == -5600
    panel.deleteLater()
