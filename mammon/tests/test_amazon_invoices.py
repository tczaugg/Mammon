"""Tests for the Amazon invoice -> Chase CSV memo enricher.

Each test here pins a behavior that a plausible refactor would otherwise break,
and several encode findings from the real data the utility was built against.
"""
import csv
import datetime as dt
import json

import pytest

from mammon import amazon_invoices as ai


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
HEADER = ["Transaction Date", "Post Date", "Description",
          "Category", "Type", "Amount", "Memo"]


def _row(txn_date, amount, desc="AMAZON MKTPL*ABC123", type_="Sale", memo=""):
    return {
        "Transaction Date": txn_date,
        "Post Date": txn_date,
        "Description": desc,
        "Category": "Shopping",
        "Type": type_,
        "Amount": amount,
        "Memo": memo,
    }


def _order(order_date, invoice_price, items, order_price=None,
           card="4321", card_type="Prime Visa", number="113-0000000-0000000"):
    return {
        "orderDate": order_date,
        "orderPrice": order_price if order_price is not None else invoice_price,
        "orderNumber": number,
        "invoiceLink": "/gp/css/summary/print.html?orderID=x",
        "cardType": card_type,
        "cardNumber": card,
        "invoicePrice": invoice_price,
        "items": [{"itemDescription": d, "itemPrice": "$1.00"} for d in items],
    }


def _write_json(tmp_path, orders, name="invoices.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"numOrders": str(len(orders)), "orders": orders}),
                    encoding="utf-8")
    return path


def _write_csv(tmp_path, rows, name="Chase4321_Activity.CSV", bom=True):
    path = tmp_path / name
    encoding = "utf-8-sig" if bom else "utf-8"
    with open(path, "w", encoding=encoding, newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    return path


def _plan_for(tmp_path, rows, orders, card="4321", **kw):
    csv_path = _write_csv(tmp_path, rows)
    json_path = _write_json(tmp_path, orders)
    fieldnames, loaded = ai.load_csv(csv_path)
    parsed, other, bad = ai.load_orders(json_path, card)
    plan = ai.build_plan(fieldnames, loaded, parsed,
                         skipped_other_card=other, skipped_unparsable=bad, **kw)
    return fieldnames, loaded, plan


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------
def test_summarize_trims_each_item_and_joins():
    items = ["LEGO Harry Potter Hogwarts Castle: Sorting Hat Ceremony Building Toy",
             "Eco Strong Pet Stain and Odor Remover - Cat Urine Enzyme Cleaner"]
    assert ai.summarize(items, words=4) == (
        "LEGO Harry Potter Hogwarts; Eco Strong Pet Stain"
    )


def test_summarize_words_zero_keeps_full_text():
    assert ai.summarize(["a b c d e f g h"], words=0) == "a b c d e f g h"


def test_summarize_collapses_whitespace_and_drops_blanks():
    assert ai.summarize(["  Tea   Infuser  ", "", None], words=6) == "Tea Infuser"


def test_summarize_drops_dangling_separator_from_a_mid_title_cut():
    """A trim landing on a separator strands it ('Twin Sheet Set -'), which reads
    as corruption rather than truncation."""
    assert ai.summarize(["Utopia Bedding Twin Sheet Set – 3 Piece"], words=6) == (
        "Utopia Bedding Twin Sheet Set"
    )
    assert ai.summarize(["A B C, D E F, G"], words=6) == "A B C, D E F"


def test_summarize_collapses_per_shipment_repeats():
    """One item shipped in parts prints once per shipment ('1 of 6', '2 of 6'),
    each line repeating the whole lot's price. The rows are real but describe one
    purchase, so the memo should name it once."""
    plates = "Fitvids 2-Inch Olympic Rubber Weight Plates Set with 7ft Bar"
    items = ["Ranzdn Window Privacy Film, Stained Glass"] + [plates] * 6
    assert ai.summarize(items, words=4) == (
        "Ranzdn Window Privacy Film; Fitvids 2-Inch Olympic Rubber"
    )


def test_summarize_dedup_is_case_insensitive_and_keeps_first_order():
    assert ai.summarize(["Blue Widget", "blue widget", "Red Widget"], words=0) == (
        "Blue Widget; Red Widget"
    )


def test_summarize_keeps_word_internal_punctuation():
    assert ai.summarize(["Mat 1/2\" Thick, EVA Interlocking Foam"], words=3) == (
        "Mat 1/2\" Thick"
    )


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def test_load_orders_filters_other_cards(tmp_path):
    path = _write_json(tmp_path, [
        _order("January 5, 2026", "10.00", ["Mine"], card="4321"),
        _order("January 5, 2026", "10.00", ["Not mine"], card="9265"),
    ])
    orders, other, bad = ai.load_orders(path, "4321")
    assert [o.items for o in orders] == [["Mine"]]
    assert (other, bad) == (1, 0)


def test_load_orders_keeps_gift_card_balance_orders_with_no_card(tmp_path):
    """An order paid entirely from gift-card balance names no card, but it is
    exactly the zero-charge history the register should still record."""
    path = _write_json(tmp_path, [
        _order("January 12, 2026", "0.00", ["Measuring Cups"], order_price="4.56",
               card=None, card_type="Amazon gift card balance"),
    ])
    orders, other, bad = ai.load_orders(path, "4321")
    assert len(orders) == 1
    assert orders[0].is_zero_charge
    assert orders[0].ordered_cents == 456
    assert other == 0


def test_load_orders_skips_null_padding_records(tmp_path):
    """The scrape pads its tail with all-null rows; they must not become orders."""
    path = _write_json(tmp_path, [
        _order("January 5, 2026", "10.00", ["Real"]),
        {"orderDate": None, "orderPrice": None, "orderNumber": None,
         "invoiceLink": None},
    ])
    orders, _other, bad = ai.load_orders(path, "4321")
    assert len(orders) == 1 and bad == 1


def test_load_orders_uses_invoice_price_not_order_price(tmp_path):
    """A partly gift-carded order bills the card less than the order total, and
    the statement shows the CHARGE. Matching on orderPrice loses these rows."""
    path = _write_json(tmp_path, [
        _order("January 6, 2026", "2.78", ["Thing"], order_price="17.78"),
    ])
    orders, _o, _b = ai.load_orders(path, "4321")
    assert orders[0].charged_cents == 278
    assert orders[0].ordered_cents == 1778
    assert not orders[0].is_zero_charge


def _order_v2(order_date, grand_total, items, order_price=None, gift_card=None,
              rewards=None, card="4321", card_type="Prime Visa",
              number="113-0000000-0000000"):
    """A record in the NEWER scrape schema: priceAccounting, no invoicePrice."""
    acct = [{"fieldName": "Item(s) Subtotal:", "fieldPrice": order_price or grand_total},
            {"fieldName": "Shipping & Handling:", "fieldPrice": "0.00"}]
    if gift_card:
        acct.append({"fieldName": "Gift Card Amount:", "fieldPrice": gift_card})
    if rewards:
        acct.append({"fieldName": "Rewards Points:", "fieldPrice": rewards})
    acct.append({"fieldName": "Grand Total:", "fieldPrice": grand_total})
    return {
        "orderNumber": number,
        "orderDate": order_date,
        "orderPrice": order_price if order_price is not None else grand_total,
        "orderLink": "/gp/css/summary/print.html?orderID=x",
        "cardType": card_type,
        "cardNumber": card,
        "priceAccounting": acct,
        "items": [{"itemDescription": d, "itemPrice": "$1.00"} for d in items],
    }


def test_new_schema_reads_grand_total_as_the_charge(tmp_path):
    path = _write_json(tmp_path, [
        _order_v2("January 5, 2026", "33.12", ["Thing"], order_price="43.12",
                  gift_card="-10.00"),
    ])
    orders, _o, _b = ai.load_orders(path, "4321")
    assert orders[0].charged_cents == 3312
    assert orders[0].ordered_cents == 4312


def test_grand_total_is_already_net_of_gift_card(tmp_path):
    """Verified against the older scrape: Grand Total reproduced invoicePrice on
    all 136 shared orders. Subtracting the gift-card line again would under-count
    every partial order and lose its match."""
    path = _write_json(tmp_path, [
        _order_v2("January 6, 2026", "2.78", ["Thing"], order_price="17.78",
                  gift_card="-15.00"),
    ])
    orders, _o, _b = ai.load_orders(path, "4321")
    assert orders[0].charged_cents == 278          # NOT 278 - 1500
    assert orders[0].gift_card_cents == 1500
    assert orders[0].offset_cents == 1500


def test_new_schema_captures_rewards_offset(tmp_path):
    path = _write_json(tmp_path, [
        _order_v2("January 7, 2026", "0.00", ["Thing"], order_price="0.00",
                  rewards="-12.88"),
    ])
    orders, _o, _b = ai.load_orders(path, "4321")
    assert orders[0].is_zero_charge
    assert orders[0].rewards_cents == 1288
    assert orders[0].offset_cents == 1288


def test_new_schema_order_with_unreachable_invoice_is_skipped(tmp_path):
    """The newer scrape emits an order whose invoice page did not load: no
    priceAccounting, no items, null price."""
    path = _write_json(tmp_path, [
        {"orderNumber": "113-1", "orderDate": "July 30, 2026",
         "orderPrice": None, "orderLink": None},
    ])
    orders, _o, bad = ai.load_orders(path, "4321")
    assert orders == [] and bad == 1


def test_old_schema_still_supported(tmp_path):
    """The user keeps prior scrape outputs; invoicePrice must keep working."""
    path = _write_json(tmp_path, [
        _order("January 6, 2026", "2.78", ["Thing"], order_price="17.78"),
    ])
    orders, _o, _b = ai.load_orders(path, "4321")
    assert orders[0].charged_cents == 278


def test_load_csv_rejects_file_without_memo_column(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("Date,Amount\n01/01/2026,-1.00\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Memo"):
        ai.load_csv(path)


def test_load_csv_strips_bom(tmp_path):
    """Chase writes a BOM; left attached it corrupts the first column name."""
    path = _write_csv(tmp_path, [_row("01/05/2026", "-10.00")], bom=True)
    fieldnames, rows = ai.load_csv(path)
    assert fieldnames[0] == "Transaction Date"
    assert rows[0]["Transaction Date"] == "01/05/2026"


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
def test_exact_amount_within_window_matches(tmp_path):
    _fn, _rows, plan = _plan_for(
        tmp_path,
        [_row("01/07/2026", "-12.88")],
        [_order("January 5, 2026", "12.88", ["Sorting Hat Building Toy"])],
    )
    assert plan.memos == {0: "Sorting Hat Building Toy"}


def test_amount_must_be_exact(tmp_path):
    """A penny off is not a match; near-miss amounts are a different purchase."""
    _fn, _rows, plan = _plan_for(
        tmp_path,
        [_row("01/05/2026", "-12.89")],
        [_order("January 5, 2026", "12.88", ["Thing"])],
    )
    assert plan.memos == {} and len(plan.unmatched_rows) == 1


def test_date_outside_window_does_not_match(tmp_path):
    _fn, _rows, plan = _plan_for(
        tmp_path,
        [_row("01/10/2026", "-12.88")],
        [_order("January 5, 2026", "12.88", ["Thing"])],
        window_days=3,
    )
    assert plan.memos == {}


def test_window_boundary_is_inclusive(tmp_path):
    _fn, _rows, plan = _plan_for(
        tmp_path,
        [_row("01/08/2026", "-12.88")],
        [_order("January 5, 2026", "12.88", ["Thing"])],
        window_days=3,
    )
    assert plan.memos == {0: "Thing"}


def test_charge_before_the_order_never_matches(tmp_path):
    """The window is DIRECTIONAL. Amazon bills at ship time, so a charge cannot
    precede its order; a pairing on the negative side is an amount coincidence."""
    _fn, _rows, plan = _plan_for(
        tmp_path,
        [_row("01/04/2026", "-12.88")],
        [_order("January 5, 2026", "12.88", ["Thing"])],
    )
    assert plan.memos == {}


def test_lag_zero_matches(tmp_path):
    """Same-day order and charge is the second-most common case in real data."""
    _fn, _rows, plan = _plan_for(
        tmp_path,
        [_row("01/05/2026", "-12.88")],
        [_order("January 5, 2026", "12.88", ["Thing"])],
    )
    assert plan.memos == {0: "Thing"}


def test_earlier_coincidental_charge_cannot_steal_an_order(tmp_path):
    """A months-old charge that happens to share an amount must not consume the
    order its own later charge needs. This is what the directional window buys:
    with a symmetric window the earlier row wins on distance and the true match
    is left empty."""
    rows = [_row("01/04/2026", "-10.73"),    # 1 day BEFORE the order: coincidence
            _row("01/09/2026", "-10.73")]    # 4 days after: the real charge
    orders = [_order("January 5, 2026", "10.73", ["Real items"])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.memos == {1: "Real items"}


def test_duplicate_amounts_assign_one_to_one(tmp_path):
    """Four identical amounts occur in real files. Each order may be used once,
    or one order's items get stamped onto several unrelated charges."""
    rows = [_row("01/05/2026", "-10.73"), _row("01/06/2026", "-10.73")]
    orders = [_order("January 5, 2026", "10.73", ["First"], number="A"),
              _order("January 6, 2026", "10.73", ["Second"], number="B")]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.memos == {0: "First", 1: "Second"}


def test_nearest_date_wins_when_two_orders_share_an_amount(tmp_path):
    rows = [_row("01/08/2026", "-10.73")]
    orders = [_order("January 5, 2026", "10.73", ["Far"], number="A"),
              _order("January 7, 2026", "10.73", ["Near"], number="B")]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.memos == {0: "Near"}


def test_surplus_charge_stays_unmatched(tmp_path):
    """Two identical charges but one order: the second must stay empty, not
    borrow the first order's items."""
    rows = [_row("01/05/2026", "-10.73"), _row("01/05/2026", "-10.73")]
    orders = [_order("January 5, 2026", "10.73", ["Only one"])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert len(plan.memos) == 1 and len(plan.unmatched_rows) == 1


def test_payments_and_returns_are_never_matched(tmp_path):
    """A refund is not an order and a statement payment is not a purchase."""
    rows = [_row("01/05/2026", "24.39", "Amazon.com", type_="Return"),
            _row("01/05/2026", "1461.08", "AUTOMATIC PAYMENT", type_="Payment")]
    orders = [_order("January 5, 2026", "24.39", ["Refunded thing"]),
              _order("January 5, 2026", "1461.08", ["Big thing"])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.memos == {}


def test_existing_memo_is_never_overwritten(tmp_path):
    rows = [_row("01/05/2026", "-12.88", memo="hand written")]
    orders = [_order("January 5, 2026", "12.88", ["Scraped"])]
    fieldnames, loaded, plan = _plan_for(tmp_path, rows, orders)
    assert plan.memos == {} and plan.already_had_memo == 1
    out = ai.apply_plan(fieldnames, loaded, plan)
    assert out[0]["Memo"] == "hand written"


# ---------------------------------------------------------------------------
# split shipments: one order, several charges
# ---------------------------------------------------------------------------
def _split_order(order_date, subtotal, tax, grand, items, number="113-SPLIT"):
    """An order whose accounting block supports the tax-rate arithmetic."""
    return {
        "orderNumber": number,
        "orderDate": order_date,
        "orderPrice": grand,
        "cardType": "Prime Visa",
        "cardNumber": "4321",
        "priceAccounting": [
            {"fieldName": "Item(s) Subtotal:", "fieldPrice": subtotal},
            {"fieldName": "Shipping & Handling:", "fieldPrice": "0.00"},
            {"fieldName": "Estimated tax to be collected:", "fieldPrice": tax},
            {"fieldName": "Grand Total:", "fieldPrice": grand},
        ],
        "items": [{"itemDescription": d, "itemPrice": p} for d, p in items],
    }


def test_split_shipment_assigns_items_to_each_charge(tmp_path):
    """The real 2-shipment case: 83.45 of items + 6.22 tax = 89.67, billed as
    82.16 (mattress) + 7.51 (protector). Each charge should name ITS items."""
    rows = [_row("04/02/2026", "-82.16"), _row("03/31/2026", "-7.51")]
    orders = [_split_order("March 31, 2026", "83.45", "6.22", "89.67",
                           [("Niagara Waterproof Mattress Protector Twin", "$6.99"),
                            ("FDW 8 Inch Twin Mattress Medium Firm", "$76.46")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.split_matched == 2
    assert plan.memos[0].startswith("FDW 8 Inch Twin Mattress")
    assert plan.memos[1].startswith("Niagara Waterproof Mattress Protector")
    assert plan.unused_orders == []


def test_split_shipment_groups_more_than_two_items_onto_one_charge(tmp_path):
    """52.47 + 3.91 tax = 56.38, billed 32.22 (tuner) + 24.16 (rest + rosin)."""
    rows = [_row("08/20/2026", "-32.22"), _row("08/17/2026", "-24.16")]
    orders = [_split_order("August 16, 2026", "52.47", "3.91", "56.38",
                           [("EVEREST EZ-4A Violin Shoulder Rest", "$16.49"),
                            ("DAddario Nexxus 360 Rechargeable Violin Tuner", "$29.99"),
                            ("DAddario Natural Violin Rosin Light", "$5.99")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.split_matched == 2
    assert plan.memos[0].startswith("DAddario Nexxus")
    assert "EVEREST" in plan.memos[1] and "Rosin" in plan.memos[1]


def test_split_shipment_requires_a_complete_partition(tmp_path):
    """If a charge subset works but leaves an item nothing can pay for, the whole
    assignment is rejected rather than dropping the leftover silently."""
    rows = [_row("04/02/2026", "-82.16"), _row("03/31/2026", "-7.51")]
    orders = [_split_order("March 31, 2026", "83.45", "6.22", "89.67",
                           [("Protector", "$6.99"), ("Mattress", "$76.46"),
                            ("Orphan item nothing can pay for", "$40.00")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.split_matched == 0
    assert plan.memos == {}
    assert plan.split_failed and "partition" in plan.split_failed[0][1]


def test_split_shipment_declines_an_ambiguous_charge_group(tmp_path):
    """Two different pairs of charges reach the same total; there is no evidence
    which pair is the order, so neither is used."""
    rows = [_row("04/02/2026", "-50.00"), _row("04/02/2026", "-39.67"),
            _row("04/01/2026", "-60.00"), _row("04/01/2026", "-29.67")]
    orders = [_split_order("March 31, 2026", "83.45", "6.22", "89.67",
                           [("Protector", "$6.99"), ("Mattress", "$76.46")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.split_matched == 0
    assert len(plan.unmatched_rows) == 4


def test_split_shipment_never_runs_on_an_exactly_matched_order(tmp_path):
    """An order the exact pass placed is consumed and must not be re-split."""
    rows = [_row("04/01/2026", "-89.67"), _row("04/02/2026", "-82.16"),
            _row("03/31/2026", "-7.51")]
    orders = [_split_order("March 31, 2026", "83.45", "6.22", "89.67",
                           [("Protector", "$6.99"), ("Mattress", "$76.46")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.memos.keys() == {0}          # only the exact match
    assert plan.split_matched == 0


def test_split_shipment_single_charge_is_not_a_split(tmp_path):
    """A group of one is an ordinary exact match, not a split; requiring two or
    more keeps this pass from re-deciding what the exact pass already handled."""
    rows = [_row("04/01/2026", "-89.67", memo="already written")]
    orders = [_split_order("March 31, 2026", "83.45", "6.22", "89.67",
                           [("Protector", "$6.99"), ("Mattress", "$76.46")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.split_matched == 0


def test_split_shipments_can_be_disabled(tmp_path):
    rows = [_row("04/02/2026", "-82.16"), _row("03/31/2026", "-7.51")]
    orders = [_split_order("March 31, 2026", "83.45", "6.22", "89.67",
                           [("Protector", "$6.99"), ("Mattress", "$76.46")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders, split_shipments=False)
    assert plan.split_matched == 0 and plan.memos == {}


def test_split_shipment_respects_the_date_window(tmp_path):
    """Charges outside the window are not candidates for a split group either."""
    rows = [_row("06/02/2026", "-82.16"), _row("06/01/2026", "-7.51")]
    orders = [_split_order("March 31, 2026", "83.45", "6.22", "89.67",
                           [("Protector", "$6.99"), ("Mattress", "$76.46")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders, window_days=21)
    assert plan.split_matched == 0


def test_split_shipment_dedupes_per_shipment_item_repeats(tmp_path):
    """The 6x weight-plate case: repeated lines each carry the WHOLE price, so
    counting them individually would inflate the subtotal past the invoice and
    find no partition at all."""
    rows = [_row("06/28/2026", "-243.36"), _row("07/01/2026", "-100.13")]
    plates = "Fitvids 2-Inch Olympic Rubber Weight Plates Set"
    orders = [_split_order("June 26, 2026", "319.68", "23.81", "343.49",
                           [("Ranzdn Window Privacy Film", "$5.80"),
                            ("Adjustable Squat Rack Multi-Function", "$87.39")]
                           + [(plates, "$226.49")] * 6)]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.split_matched == 2
    assert plan.memos[0].startswith("Fitvids")
    assert "Ranzdn" in plan.memos[1] and "Squat" in plan.memos[1]
    assert plan.memos[0].count("Fitvids") == 1


# ---------------------------------------------------------------------------
# refunds (deliberately narrow)
# ---------------------------------------------------------------------------
def _refund_order(order_date, grand, refund, items, number="113-REFUND"):
    return {
        "orderNumber": number,
        "orderDate": order_date,
        "orderPrice": grand,
        "cardType": "Prime Visa",
        "cardNumber": "4321",
        "priceAccounting": [
            {"fieldName": "Item(s) Subtotal:", "fieldPrice": grand},
            {"fieldName": "Grand Total:", "fieldPrice": grand},
            {"fieldName": "Refund Total", "fieldPrice": refund},
        ],
        "items": [{"itemDescription": d, "itemPrice": p} for d, p in items],
    }


def test_refund_names_the_item_for_a_single_item_order(tmp_path):
    rows = [_row("08/15/2026", "24.39", "Amazon.com", type_="Return")]
    orders = [_refund_order("June 22, 2026", "24.39", "24.39",
                            [("Amazon Essentials Mens Regular-Fit Shirt", "$22.70")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.refund_matched == 1
    assert plan.memos[0] == "Return: Amazon Essentials Mens Regular-Fit Shirt"


def test_refund_declines_a_multi_item_order(tmp_path):
    """With several items there is no way to tell WHICH came back: a refund has
    no partition constraint, and the invoice's blended tax rate does not hold per
    item (a real 5-item order blends to 6.64% while the returned item was taxed
    at 7.45%, missing by 14 cents)."""
    rows = [_row("06/27/2026", "18.35", "AMAZON MKTPLACE PMTS", type_="Return")]
    orders = [_refund_order("June 23, 2026", "257.89", "18.35",
                            [("Nutricost Calcium Citrate Powder", "$14.36"),
                             ("California King Mattress Protector", "$17.08")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.refund_matched == 0 and plan.memos == {}


def test_refund_requires_an_exact_amount(tmp_path):
    rows = [_row("08/15/2026", "24.40", "Amazon.com", type_="Return")]
    orders = [_refund_order("June 22, 2026", "24.39", "24.39",
                            [("Shirt", "$22.70")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.refund_matched == 0


def test_refund_cannot_precede_its_order(tmp_path):
    rows = [_row("06/21/2026", "24.39", "Amazon.com", type_="Return")]
    orders = [_refund_order("June 22, 2026", "24.39", "24.39",
                            [("Shirt", "$22.70")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.refund_matched == 0


def test_refund_respects_its_own_wider_window(tmp_path):
    """54 days is normal for a refund and must match; far beyond the window
    must not."""
    orders = [_refund_order("June 22, 2026", "24.39", "24.39",
                            [("Shirt", "$22.70")])]
    _fn, _rows, near = _plan_for(
        tmp_path, [_row("08/15/2026", "24.39", type_="Return")], orders)
    assert near.refund_matched == 1
    _fn, _rows, far = _plan_for(
        tmp_path, [_row("08/15/2026", "24.39", type_="Return")], orders,
        refund_window_days=10)
    assert far.refund_matched == 0


def test_refund_declines_when_two_orders_could_explain_it(tmp_path):
    rows = [_row("08/15/2026", "24.39", "Amazon.com", type_="Return")]
    orders = [_refund_order("June 22, 2026", "24.39", "24.39",
                            [("Shirt", "$22.70")], number="A"),
              _refund_order("July 2, 2026", "24.39", "24.39",
                            [("Socks", "$22.70")], number="B")]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.refund_matched == 0


def test_refund_does_not_consume_the_order_for_purchase_matching(tmp_path):
    """An order is both bought and refunded. Naming the refund must not stop the
    purchase charge from getting its own memo."""
    rows = [_row("06/23/2026", "-24.39"),
            _row("08/15/2026", "24.39", "Amazon.com", type_="Return")]
    orders = [_refund_order("June 22, 2026", "24.39", "24.39",
                            [("Amazon Essentials Shirt", "$22.70")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.memos[0] == "Amazon Essentials Shirt"
    assert plan.memos[1] == "Return: Amazon Essentials Shirt"


def test_refunds_can_be_disabled(tmp_path):
    rows = [_row("08/15/2026", "24.39", "Amazon.com", type_="Return")]
    orders = [_refund_order("June 22, 2026", "24.39", "24.39",
                            [("Shirt", "$22.70")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders, refunds=False)
    assert plan.refund_matched == 0 and plan.memos == {}


def test_refund_prefix_never_lands_on_a_purchase(tmp_path):
    rows = [_row("06/23/2026", "-24.39")]
    orders = [_refund_order("June 22, 2026", "24.39", "24.39",
                            [("Shirt", "$22.70")])]
    _fn, _rows, plan = _plan_for(tmp_path, rows, orders)
    assert plan.memos[0] == "Shirt"
    assert not plan.memos[0].startswith(ai.REFUND_PREFIX)


# ---------------------------------------------------------------------------
# zero-charge inserts
# ---------------------------------------------------------------------------
def test_zero_charge_order_is_inserted_as_a_row(tmp_path):
    fieldnames, loaded, plan = _plan_for(
        tmp_path,
        [_row("01/05/2026", "-10.00")],
        [_order("January 5, 2026", "10.00", ["Charged"], number="A"),
         _order("January 3, 2026", "0.00", ["Gift carded"], order_price="8.57",
                number="B")],
    )
    assert len(plan.inserts) == 1
    out = ai.apply_plan(fieldnames, loaded, plan)
    assert len(out) == 2
    inserted = out[-1]
    assert inserted["Amount"] == "0.00"
    assert inserted["Description"] == "Amazon.com"
    assert inserted["Category"] == "Shopping"
    assert inserted["Type"] == "Sale"
    assert inserted["Transaction Date"] == "01/03/2026"
    assert inserted["Memo"] == "Gift carded"


def test_zero_charge_order_never_consumes_a_statement_row(tmp_path):
    """A $0 order billed the card nothing, so it must not match any charge --
    including another $0-ish row -- and must not suppress a real match."""
    _fn, _rows, plan = _plan_for(
        tmp_path,
        [_row("01/05/2026", "-10.00")],
        [_order("January 5, 2026", "0.00", ["Free"], order_price="10.00",
                number="A"),
         _order("January 5, 2026", "10.00", ["Paid"], number="B")],
    )
    assert plan.memos == {0: "Paid"}
    assert len(plan.inserts) == 1


def test_no_zero_rows_flag_suppresses_inserts(tmp_path, capsys):
    csv_path = _write_csv(tmp_path, [_row("01/05/2026", "-10.00")])
    json_path = _write_json(tmp_path, [
        _order("January 3, 2026", "0.00", ["Gift carded"], order_price="8.57"),
    ])
    out = tmp_path / "out.csv"
    ai.main([str(csv_path), str(json_path), "-o", str(out), "--no-zero-rows"])
    with open(out, encoding="utf-8", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 1


# ---------------------------------------------------------------------------
# output shape
# ---------------------------------------------------------------------------
def test_output_preserves_header_and_row_count(tmp_path):
    rows = [_row("01/07/2026", "-12.88"), _row("01/05/2026", "-1.00")]
    fieldnames, loaded, plan = _plan_for(
        tmp_path, rows, [_order("January 5, 2026", "12.88", ["Thing"])])
    out = ai.apply_plan(fieldnames, loaded, plan)
    assert list(out[0].keys()) == HEADER
    assert len(out) == len(rows)


def test_output_stays_sorted_by_transaction_date_descending(tmp_path):
    rows = [_row("01/07/2026", "-1.00"), _row("01/02/2026", "-2.00")]
    fieldnames, loaded, plan = _plan_for(
        tmp_path, rows,
        [_order("January 5, 2026", "0.00", ["Middle"], order_price="9.00")])
    out = ai.apply_plan(fieldnames, loaded, plan)
    assert [r["Transaction Date"] for r in out] == [
        "01/07/2026", "01/05/2026", "01/02/2026"]


def test_roundtrip_through_csv_keeps_commas_and_unicode(tmp_path):
    """Item titles carry commas and en-dashes; the writer must quote and encode
    them so the enriched file re-reads identically."""
    item = "Utopia Bedding Twin Sheet Set – 3 Piece, Soft Brushed"
    csv_path = _write_csv(tmp_path, [_row("01/05/2026", "-12.88")])
    json_path = _write_json(tmp_path, [
        _order("January 5, 2026", "12.88", [item])])
    out = tmp_path / "out.csv"
    ai.main([str(csv_path), str(json_path), "-o", str(out), "--words", "0"])
    with open(out, encoding="utf-8", newline="") as fh:
        back = list(csv.DictReader(fh))
    assert back[0]["Memo"] == item


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_detect_card_from_chase_filename():
    assert ai.detect_card("Chase4321_Activity20260101_20260131.CSV") == "4321"
    assert ai.detect_card("something-else.csv") == ""


def test_dry_run_writes_no_file(tmp_path, capsys):
    csv_path = _write_csv(tmp_path, [_row("01/05/2026", "-12.88")])
    json_path = _write_json(tmp_path, [
        _order("January 5, 2026", "12.88", ["Thing"])])
    out = tmp_path / "out.csv"
    rc = ai.main([str(csv_path), str(json_path), "-o", str(out), "--dry-run"])
    assert rc == 0
    assert not out.exists()
    assert "memos filled      1" in capsys.readouterr().out


def test_cli_end_to_end(tmp_path, capsys):
    csv_path = _write_csv(tmp_path, [
        _row("01/07/2026", "-12.88"),
        _row("01/06/2026", "-99.99"),
    ])
    json_path = _write_json(tmp_path, [
        _order("January 5, 2026", "12.88", ["Sorting Hat Building Toy"], number="A"),
        _order("January 4, 2026", "0.00", ["Free thing"], order_price="8.57",
               number="B"),
        _order("January 5, 2026", "77.00", ["Other card"], card="9265", number="C"),
    ])
    out = tmp_path / "out.csv"
    rc = ai.main([str(csv_path), str(json_path), "-o", str(out)])
    assert rc == 0
    with open(out, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["Transaction Date"] for r in rows] == [
        "01/07/2026", "01/06/2026", "01/04/2026"]
    assert rows[0]["Memo"] == "Sorting Hat Building Toy"
    assert rows[1]["Memo"] == ""                      # unmatched, left alone
    assert rows[2]["Amount"] == "0.00"                # gift-card order inserted
    report = capsys.readouterr().out
    assert "other-card orders 1 (skipped)" in report


def test_rerun_on_enriched_output_is_idempotent(tmp_path):
    """Running twice must not double-insert zero rows or rewrite memos."""
    csv_path = _write_csv(tmp_path, [_row("01/05/2026", "-12.88")])
    json_path = _write_json(tmp_path, [
        _order("January 5, 2026", "12.88", ["Thing"], number="A"),
        _order("January 3, 2026", "0.00", ["Free"], order_price="8.57", number="B"),
    ])
    first = tmp_path / "first.csv"
    ai.main([str(csv_path), str(json_path), "-o", str(first)])
    second = tmp_path / "second.csv"
    ai.main([str(first), str(json_path), "-o", str(second), "--card", "4321"])
    with open(first, encoding="utf-8", newline="") as fh:
        a = list(csv.DictReader(fh))
    with open(second, encoding="utf-8", newline="") as fh:
        b = list(csv.DictReader(fh))
    assert a == b
