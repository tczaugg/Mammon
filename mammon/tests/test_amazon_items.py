"""Pure-core tests for Amazon invoice itemization (mammon/importers/amazon_items).

No database, no UI, no Qt: this is the offline allocator and the invoice-file
parser wrapper. Everything here is synthetic -- ANON order numbers, invented item
titles, and made-up amounts -- so nothing carries PII.

The load-bearing property under test is EXACTNESS: for every allocation the split
legs must sum, in signed integer cents, to precisely the charge -- with tax and
the gift-card / reward offsets allocated proportionally and any rounding remainder
distributed deterministically.
"""

import json

from mammon.importers import amazon_items as ai
from mammon.importers.amazon_items import Item, ChargeSpec


# ---------------------------------------------------------------------------
# invoice-file fixture (newer 'priceAccounting' scrape schema)
# ---------------------------------------------------------------------------
def _write_invoice(path, orders):
    path.write_text(json.dumps({"numOrders": str(len(orders)), "orders": orders}),
                    encoding="utf-8")
    return str(path)


def _order_rec(number, order_date, subtotal, grand, items,
               tax=None, gift_card=None, rewards=None, card="4321"):
    """One order record in the newer scrape schema. ``items`` is a list of
    ``(description, price_string)``. Amounts are plain '$x.xx' strings."""
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


def _sum(alloc):
    return sum(leg.amount_cents for leg in alloc.legs)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_load_invoice_orders_reads_price_accounting(tmp_path):
    rec = _order_rec(
        "ANON-0001", "March 1, 2026",
        subtotal="$80.00", tax="$6.00", gift_card="$30.00", grand="$56.00",
        items=[("ANON Widget B1", "$30.00"), ("ANON Widget B2", "$50.00")],
    )
    p = _write_invoice(tmp_path / "invoices.json", [rec])

    orders = ai.load_invoice_orders(p)
    assert len(orders) == 1
    o = orders[0]
    assert o.charged_cents == 5600            # Grand Total, already net of gift card
    assert o.subtotal_cents == 8000
    assert o.gift_card_cents == 3000
    assert o.rewards_cents == 0
    assert o.items == ["ANON Widget B1", "ANON Widget B2"]
    assert o.item_prices == [3000, 5000]


# ---------------------------------------------------------------------------
# one leg per item + proportional tax
# ---------------------------------------------------------------------------
def test_one_leg_per_item_with_proportional_tax():
    # subtotal $100, tax $10 -> item portion $110 spread by list price 30:70.
    alloc = ai.allocate_charge(
        charge_cents=-11000,
        items=[Item("ANON X", 3000), Item("ANON Y", 7000)],
    )
    items = alloc.item_legs
    assert len(items) == 2
    assert [leg.amount_cents for leg in items] == [-3300, -7700]   # 30->33, 70->77
    # tax rode into the item legs proportionally; there is no separate tax leg
    assert all(leg.kind == "item" for leg in items)
    assert all(leg.category == "" for leg in items)               # user fills these
    assert _sum(alloc) == -11000 == alloc.charge_cents


def test_item_leg_memo_is_the_item_title():
    alloc = ai.allocate_charge(charge_cents=-5000,
                               items=[Item("ANON Cable", 2000), Item("ANON Mouse", 3000)])
    assert [leg.memo for leg in alloc.item_legs] == ["ANON Cable", "ANON Mouse"]


# ---------------------------------------------------------------------------
# proportional gift-card offset (the design doc's worked example)
# ---------------------------------------------------------------------------
def test_gift_card_offset_is_a_category_leg():
    # subtotal $80, tax $6, gift card $30 -> charge -$56.00; items list $30 / $50.
    alloc = ai.allocate_charge(
        charge_cents=-5600,
        items=[Item("ANON B1", 3000), Item("ANON B2", 5000)],
        gift_card_cents=3000,
    )
    by_kind = {leg.kind: leg for leg in alloc.legs}
    assert [leg.amount_cents for leg in alloc.item_legs] == [-3225, -5375]
    assert by_kind["giftcard"].amount_cents == 3000               # money IN
    assert by_kind["giftcard"].category == ai.GIFT_CARD_CATEGORY
    # a category, never an account/transfer
    assert not hasattr(by_kind["giftcard"], "transfer_account")
    assert _sum(alloc) == -5600 == alloc.charge_cents


def test_gift_card_and_rewards_both_offset_proportionally():
    # subtotal $100, gift $20, rewards $10 -> charge -$70.00; items $40 / $60.
    alloc = ai.allocate_charge(
        charge_cents=-7000,
        items=[Item("ANON P", 4000), Item("ANON Q", 6000)],
        gift_card_cents=2000,
        rewards_cents=1000,
    )
    by_kind = {leg.kind: leg for leg in alloc.legs}
    assert [leg.amount_cents for leg in alloc.item_legs] == [-4000, -6000]
    assert by_kind["giftcard"].amount_cents == 2000
    assert by_kind["rewards"].amount_cents == 1000
    assert by_kind["rewards"].category == ai.REWARDS_CATEGORY
    assert _sum(alloc) == -7000 == alloc.charge_cents


def test_zero_charge_order_fully_covered_by_gift_card():
    # nothing reached the card; the whole item portion is offset by the gift card.
    alloc = ai.allocate_charge(
        charge_cents=0,
        items=[Item("ANON Gift Buy", 4312)],
        gift_card_cents=4312,
    )
    assert _sum(alloc) == 0 == alloc.charge_cents
    assert alloc.item_legs[0].amount_cents == -4312


# ---------------------------------------------------------------------------
# exact-cents summation including the rounding remainder
# ---------------------------------------------------------------------------
def test_rounding_remainder_distributed_deterministically():
    # $1.00 across three equal-weight items: 100/3 does not divide evenly.
    alloc = ai.allocate_charge(
        charge_cents=-100,
        items=[Item("ANON A", 1), Item("ANON B", 1), Item("ANON C", 1)],
    )
    # largest remainder ties broken by lowest index -> the first leg gets the cent
    assert [leg.amount_cents for leg in alloc.item_legs] == [-34, -33, -33]
    assert _sum(alloc) == -100 == alloc.charge_cents


def test_many_awkward_splits_all_sum_exactly():
    # sweep a range of charges and weightings; every one must reconcile to the cent
    weightings = [
        [111, 222, 3, 40007],
        [1, 1, 1, 1, 1, 1, 1],
        [999999, 1],
        [5000, 5000, 5000],
        [12345, 67, 890, 4],
    ]
    for weights in weightings:
        for charge in (-1, -7, -99, -100, -12345, -100000):
            items = [Item(f"ANON {i}", w) for i, w in enumerate(weights)]
            alloc = ai.allocate_charge(charge_cents=charge, items=items,
                                       gift_card_cents=13, rewards_cents=29)
            assert _sum(alloc) == charge, (charge, weights)
            # item legs are money out, offsets money in
            assert all(leg.amount_cents <= 0 for leg in alloc.item_legs)


def test_no_item_prices_falls_back_to_one_visible_leg():
    alloc = ai.allocate_charge(
        charge_cents=-2500,
        items=[Item("ANON Untitled", 0), Item("ANON Also Zero", 0)],
    )
    assert len(alloc.legs) == 1
    assert alloc.legs[0].kind == "unallocated"
    assert alloc.legs[0].memo == ai.UNALLOCATED_MEMO
    assert _sum(alloc) == -2500 == alloc.charge_cents


# ---------------------------------------------------------------------------
# multiple charges per order (per-shipment billing)
# ---------------------------------------------------------------------------
def test_multi_charge_per_order_each_self_consistent():
    # Design doc's split example: subtotal $150, tax $12 (r = 0.08); items
    # A1 $40, A2 $60, A3 $50. Shipment 1 bills {A1,A2} = -$108, shipment 2 {A3} = -$54.
    a1, a2, a3 = Item("ANON A1", 4000), Item("ANON A2", 6000), Item("ANON A3", 5000)
    allocs = ai.allocate_charges([
        ChargeSpec(charge_cents=-10800, items=[a1, a2]),
        ChargeSpec(charge_cents=-5400, items=[a3]),
    ])
    assert len(allocs) == 2

    first, second = allocs
    assert [leg.amount_cents for leg in first.item_legs] == [-4320, -6480]   # *1.08
    assert _sum(first) == -10800 == first.charge_cents
    assert [leg.amount_cents for leg in second.item_legs] == [-5400]
    assert _sum(second) == -5400 == second.charge_cents

    # and together they reconcile to the order's grand total
    assert _sum(first) + _sum(second) == -16200


def test_multi_charge_offset_attributed_to_one_shipment():
    # gift card of $10 attributed to shipment 1 only; shipment 2 has none.
    allocs = ai.allocate_charges([
        ChargeSpec(charge_cents=-3000, items=[Item("ANON S1", 4000)],
                   gift_card_cents=1000),
        ChargeSpec(charge_cents=-5000, items=[Item("ANON S2", 5000)]),
    ])
    first, second = allocs
    assert any(leg.kind == "giftcard" and leg.amount_cents == 1000
               for leg in first.legs)
    assert all(leg.kind != "giftcard" for leg in second.legs)
    assert _sum(first) == -3000
    assert _sum(second) == -5000


# ---------------------------------------------------------------------------
# order-level convenience + refund rejection
# ---------------------------------------------------------------------------
def test_allocate_order_from_parsed_invoice(tmp_path):
    rec = _order_rec(
        "ANON-0002", "April 2, 2026",
        subtotal="$80.00", tax="$6.00", gift_card="$30.00", grand="$56.00",
        items=[("ANON B1", "$30.00"), ("ANON B2", "$50.00")],
    )
    p = _write_invoice(tmp_path / "invoices.json", [rec])
    order = ai.load_invoice_orders(p)[0]

    alloc = ai.allocate_order(order)
    assert [leg.amount_cents for leg in alloc.item_legs] == [-3225, -5375]
    assert _sum(alloc) == -5600 == alloc.charge_cents
    assert any(leg.kind == "giftcard" and leg.amount_cents == 3000
               for leg in alloc.legs)


def test_refund_charge_is_rejected():
    # a credit is a refund; refunds are out of scope for the allocator.
    try:
        ai.allocate_charge(charge_cents=500, items=[Item("ANON R", 500)])
    except ValueError as exc:
        assert "refund" in str(exc).lower()
    else:
        raise AssertionError("a positive (credit) charge must be rejected")


# ---------------------------------------------------------------------------
# default category ordering
# ---------------------------------------------------------------------------
def test_default_category_order_fixed_list_first():
    assert ai.default_category_order() == list(ai.DEFAULT_CATEGORY_FALLBACK)


def test_default_category_order_appends_history_deduped_case_insensitively():
    order = ai.default_category_order(["Groceries", "Pets:Vet", "household", "Pets:Vet"])
    # fixed list unchanged and first
    assert order[:len(ai.DEFAULT_CATEGORY_FALLBACK)] == list(ai.DEFAULT_CATEGORY_FALLBACK)
    # 'Groceries'/'household' already present (case-insensitive) -> not duplicated
    assert order.count("groceries") == 1
    assert "Groceries" not in order
    # a genuinely new one is appended once, keeping its first spelling
    assert order[-1] == "Pets:Vet"
    assert order.count("Pets:Vet") == 1
