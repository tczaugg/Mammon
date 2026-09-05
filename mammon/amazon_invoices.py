"""Fill a Chase CSV export's blank ``Memo`` column with what was actually bought.

An Amazon-heavy credit-card statement is nearly unreadable: the bank descriptor is
``AMAZON MKTPL*5O9NO1XM1`` for every row, so the register records only that money
went to Amazon, never what for. Amazon knows, but only on its orders page. This
module joins the two sources -- a webSlinger invoice scrape and the card's own CSV
download -- and writes a new CSV whose ``Memo`` names the items.

It is a pure file-to-file transform. It opens no database and imports nothing; the
enriched CSV goes through the ordinary import path afterwards, so
:mod:`mammon.ledger` remains the single writer of transaction rows.

Five things here were established against real data and are easy to undo by
accident:

- **There is no join key.** The trailing token in ``AMAZON MKTPL*5O9NO1XM1`` is a
  card-network descriptor, NOT the ``113-xxxxxxx-xxxxxxx`` order number. Nothing is
  shared between the two files, so a match can only be made on amount and date
  proximity. Exact amount plus a tight date window is what keeps that honest.

- **Match on the invoice's ``Grand Total``, never ``orderPrice``.** They diverge on
  precisely the orders partly paid by gift card or rewards -- a $17.78 order that
  billed the card $2.78 -- and the charge is what the statement shows. Using
  ``orderPrice`` silently loses every such row. Two scrape schemas are supported:
  the newer one carries the invoice accounting block in ``priceAccounting`` (whose
  ``Grand Total`` is already net of gift card / rewards, so never subtract those
  again), the older one a flat ``invoicePrice``. See :func:`_charged_cents`.

- **Match on ``Transaction Date``, not ``Post Date``.** The transaction date is
  when the charge was made, so it sits close to the order date; the post date
  drifts further and costs matches at any sane window.

- **The date window is DIRECTIONAL: the charge falls 0..N days AFTER the order,
  never before.** Amazon bills at ship time, which cannot precede the order. In
  real data the observed lag runs 0-13 days and is entirely one-sided (0d and 1d
  alone carry most of it), while every exact-amount pairing on the negative side
  was 28+ days out -- a coincidental amount collision, never a true match. A
  symmetric window admits those and lets one steal an order from its real charge.

- **Filter by card.** The invoice feed covers every card on the Amazon account, so
  a scrape for one card carries orders billed to others. Left in, they collide with
  this card's charges on duplicate amounts and produce confident nonsense.

Orders whose ``invoicePrice`` is zero never reached the card at all -- gift card or
rewards covered them -- so no statement row exists to enrich. They are INSERTED as
$0.00 rows instead, because the order still happened and the register is where that
history belongs. A zero-amount row leaves every balance untouched.

A SECOND pass then reunites one order with the several charges that paid for it,
because Amazon bills per shipment. It runs only on what exact matching could not
place, and commits only when two subset searches each have a UNIQUE answer: the
charges must sum to the invoice total, and the items must then partition across
those charges with every item used exactly once, grossed by the invoice's own
effective tax rate. Requiring a complete partition is what makes inference safe
here -- a coincidental subset almost never leaves a remainder that covers the
other charges exactly. Anything ambiguous is reported, never guessed.

A THIRD, deliberately narrow pass names the item behind a ``Return`` credit, using
the invoice's ``Refund Total``. It is far stricter than the purchase passes because
both of their guards are missing: a refund returns ONE item, so no complete-partition
check can catch a wrong pick, and the per-item tax rate is not derivable from a mixed
order (a real 5-item invoice blends to 6.64% while the item actually returned was
taxed at 7.45%). It therefore commits only when the order holds exactly one item and
its ``Refund Total`` equals the credit exactly -- no arithmetic, no ambiguity. See
:func:`_match_refunds`.

CSV cannot express splits, so a gift-card offset that would normally be entered as a
split against ``[Amazon Gift Card]`` is not reconstructed here; the item text lands
in the memo and the offsetting leg stays a manual step, printed by ``--verbose``.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from mammon.importers.record import dollars_to_cents

# Chase's export header. Only ``Memo`` is strictly required; the rest shape the
# inserted rows and are looked up defensively, so a column rename degrades to a
# clear error instead of a malformed file.
COL_TXN_DATE = "Transaction Date"
COL_POST_DATE = "Post Date"
COL_DESC = "Description"
COL_CATEGORY = "Category"
COL_TYPE = "Type"
COL_AMOUNT = "Amount"
COL_MEMO = "Memo"

CSV_DATE_FMT = "%m/%d/%Y"
ORDER_DATE_FMT = "%B %d, %Y"

# Only purchase rows can correspond to an order. Payments and returns never do: a
# refund is not an order, and a statement payment is not a purchase.
PURCHASE_TYPE = "Sale"
#: Chase's type for a credit back from the merchant. Positive amount.
REFUND_TYPE = "Return"

# Inserted zero-charge rows read as ordinary Amazon activity so they sort and
# categorize alongside everything else.
ZERO_DESCRIPTION = "Amazon.com"
ZERO_CATEGORY = "Shopping"

# Days the charge may lag the order (ship time). Directional: never negative.
DEFAULT_WINDOW_DAYS = 21
DEFAULT_WORDS_PER_ITEM = 6

# Split-shipment recovery bounds. Past these the subset search stops being
# evidence of anything: with enough values almost any total becomes reachable, so
# a "unique" solution would no longer mean the assignment is real.
MAX_SPLIT_CHARGES = 6
MAX_SPLIT_ITEMS = 12
MAX_SPLIT_POOL = 12
#: Rounding slack, in cents, when reconciling a grossed-up item subset against a
#: charge. Amazon rounds tax per shipment, so a penny or two of drift is normal.
SPLIT_TOLERANCE_CENTS = 2

# A refund lags its order far more than a charge does -- 54 days in real data,
# against 0-13 for purchases -- so it needs its own, much wider window.
DEFAULT_REFUND_WINDOW_DAYS = 180
#: Marks a memo the refund pass wrote, so a credit never reads like a purchase.
REFUND_PREFIX = "Return: "


@dataclass
class Order:
    """One Amazon order from the webSlinger scrape."""

    order_number: str = ""
    date: Optional[dt.date] = None
    charged_cents: int = 0      # what the card was billed (invoice Grand Total)
    ordered_cents: int = 0      # orderPrice -- the orders-list figure
    gift_card_cents: int = 0    # magnitude of any "Gift Card Amount:" line
    rewards_cents: int = 0      # magnitude of any "Rewards Points:" line
    subtotal_cents: int = 0     # "Item(s) Subtotal:" -- items before tax/shipping
    refund_cents: int = 0       # "Refund Total" -- money Amazon sent back
    item_prices: list = field(default_factory=list)   # cents, aligned with items
    card_number: str = ""
    card_type: str = ""
    items: list = field(default_factory=list)

    @property
    def is_zero_charge(self) -> bool:
        """True when nothing reached the card (gift card / rewards covered it)."""
        return self.charged_cents == 0

    @property
    def offset_cents(self) -> int:
        """What gift card + rewards covered. This is the amount that would become
        the offsetting ``[Amazon Gift Card]`` / ``[Amazon Rewards]`` split leg --
        which CSV cannot express, so it is reported rather than written."""
        return self.gift_card_cents + self.rewards_cents


@dataclass
class Plan:
    """What the rewrite will do, computed before anything is written."""

    memos: dict = field(default_factory=dict)        # csv row index -> memo text
    inserts: list = field(default_factory=list)      # (Order, row dict)
    unmatched_rows: list = field(default_factory=list)
    unused_orders: list = field(default_factory=list)
    skipped_other_card: int = 0
    skipped_unparsable: int = 0
    already_had_memo: int = 0
    already_present: int = 0
    split_matched: int = 0
    refund_matched: int = 0
    split_groups: list = field(default_factory=list)   # (Order, [(idx, cents)])
    split_failed: list = field(default_factory=list)   # (Order, reason)
    coverage: tuple = (None, None)

    @property
    def matched(self) -> int:
        return len(self.memos)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _parse_order_date(value) -> Optional[dt.date]:
    """Amazon renders order dates as 'July 29, 2026'. Returns None if unparsable."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(str(value).strip(), ORDER_DATE_FMT).date()
    except ValueError:
        return None


def _money_cents(value) -> Optional[int]:
    """Parse a scraped money string to cents, or None when absent/unparsable.

    Telling None (field missing) from 0 (genuinely charged nothing) is the whole
    point: a zero charge is a gift-card order worth inserting, while a missing
    price is an incomplete scrape record worth skipping.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        return dollars_to_cents(value)
    except ValueError:
        return None


#: Invoice accounting lines, as scraped into ``priceAccounting``. The trailing
#: colon is part of the label on the page and is stripped before lookup.
ACCT_GRAND_TOTAL = "Grand Total"
ACCT_GIFT_CARD = "Gift Card Amount"
ACCT_REWARDS = "Rewards Points"
ACCT_SUBTOTAL = "Item(s) Subtotal"
ACCT_REFUND_TOTAL = "Refund Total"


def _accounting(rec) -> dict:
    """``priceAccounting`` as a {label: amount-string} map, colon stripped."""
    out = {}
    for line in (rec.get("priceAccounting") or []):
        name = str(line.get("fieldName") or "").strip().rstrip(":").strip()
        if name:
            out[name] = line.get("fieldPrice")
    return out


def _charged_cents(rec, acct: dict) -> Optional[int]:
    """What the card was actually billed, across both scrape schemas.

    The newer scrape carries the invoice's accounting block in ``priceAccounting``
    and has no ``invoicePrice``; its ``Grand Total`` is already NET of any gift
    card or rewards line, so it must NOT be reduced again. That was verified
    against the older scrape: ``Grand Total`` reproduced ``invoicePrice`` on all
    136 orders the two files share, with no exceptions -- including every partial
    gift-card order (a $43.12 order with a $10.00 gift card has a $33.12 Grand
    Total and posted $33.12).

    ``orderPrice`` is the LAST resort: it comes from the orders-list page and, for
    a gift-carded order, still shows the pre-gift-card total.
    """
    for value in (acct.get(ACCT_GRAND_TOTAL), rec.get("invoicePrice"),
                  rec.get("orderPrice")):
        cents = _money_cents(value)
        if cents is not None:
            return cents
    return None


def load_orders(path, card_number: str = "") -> tuple:
    """Read the webSlinger invoice JSON.

    Returns ``(orders, skipped_other_card, skipped_unparsable)``. Orders kept are
    those billed to ``card_number``, plus any order paid entirely from an Amazon
    gift-card balance (which names no card at all, but is exactly the zero-charge
    history worth keeping). An empty ``card_number`` keeps every card.
    """
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    raw = payload.get("orders") or []

    orders: list = []
    other_card = 0
    unparsable = 0
    for rec in raw:
        date = _parse_order_date(rec.get("orderDate"))
        acct = _accounting(rec)
        charged = _charged_cents(rec, acct)
        ordered = _money_cents(rec.get("orderPrice"))
        # A record with no date or no price carries nothing usable: the older
        # scrape padded its tail with all-null rows, and the newer one emits an
        # order whose invoice page was unreachable (no priceAccounting, no items).
        if date is None or charged is None:
            unparsable += 1
            continue

        rec_card = str(rec.get("cardNumber") or "").strip()
        card_type = str(rec.get("cardType") or "").strip()
        gift_balance = not rec_card and "gift card" in card_type.lower()
        if card_number and rec_card != card_number and not gift_balance:
            other_card += 1
            continue

        # Descriptions and prices are kept index-aligned, and DE-DUPLICATED here:
        # an item shipped in parts prints once per shipment ("1 of 6", "2 of 6"),
        # every line repeating the price of the whole lot. Keeping the repeats
        # would both clutter the memo and, worse, multiply that item's price in
        # the split-shipment arithmetic below.
        items: list = []
        item_prices: list = []
        for i in (rec.get("items") or []):
            desc = str(i.get("itemDescription") or "").strip()
            if not desc or desc in items:
                continue
            items.append(desc)
            item_prices.append(_money_cents(i.get("itemPrice")) or 0)
        orders.append(
            Order(
                order_number=str(rec.get("orderNumber") or "").strip(),
                date=date,
                charged_cents=abs(charged),
                ordered_cents=abs(ordered if ordered is not None else charged),
                gift_card_cents=abs(_money_cents(acct.get(ACCT_GIFT_CARD)) or 0),
                rewards_cents=abs(_money_cents(acct.get(ACCT_REWARDS)) or 0),
                subtotal_cents=abs(_money_cents(acct.get(ACCT_SUBTOTAL)) or 0),
                refund_cents=abs(_money_cents(acct.get(ACCT_REFUND_TOTAL)) or 0),
                item_prices=item_prices,
                card_number=rec_card,
                card_type=card_type,
                items=items,
            )
        )
    return orders, other_card, unparsable


def load_csv(path) -> tuple:
    """Read the card CSV, returning ``(fieldnames, rows)``.

    ``utf-8-sig`` because Chase writes a BOM; leaving it attached corrupts the
    first column's name and every lookup against it.
    """
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    if COL_MEMO not in fieldnames:
        raise ValueError(
            f"{path}: no {COL_MEMO!r} column (found {fieldnames}). "
            "This does not look like a Chase activity export."
        )
    return fieldnames, rows


# ---------------------------------------------------------------------------
# memo text
# ---------------------------------------------------------------------------
def summarize(items: Sequence[str], words: int = DEFAULT_WORDS_PER_ITEM) -> str:
    """Join item descriptions, trimming each to its first ``words`` words.

    Amazon titles are keyword-stuffed to several hundred characters ("... - Gift
    Idea for Birthdays - 76460"), and the distinguishing words come first. Taking a
    fixed prefix of each keeps every item in a multi-item order represented while
    the memo stays readable in a register column.

    Repeated titles are collapsed. When one item ships in parts, Amazon's invoice
    prints a line PER SHIPMENT -- "1 of 6", "2 of 6" ... -- each repeating the
    price of the whole lot, so a six-shipment item appears six times at $226.49.
    Those rows are real, but they describe one purchase: the invoice total is
    already correct and the repeated prices are redundant (never sum item prices
    to reconcile an invoice; this module matches on ``invoicePrice`` alone).
    Trimming can likewise make two long titles collide on a shared prefix. Either
    way a memo repeating one phrase carries no information, so first occurrence
    wins and order is preserved.
    """
    parts = []
    seen = set()
    for raw in items:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not text:
            continue
        if words > 0:
            text = " ".join(text.split()[:words])
            # A cut mid-title strands the separator that followed the last kept
            # word ("Utopia Bedding Twin Sheet Set -"), which reads as corruption
            # rather than truncation. Drop trailing joining punctuation, but keep
            # characters that belong to the word itself (")", '"', digits).
            text = re.sub(r"[\s,;:\-–—&/|+]+$", "", text)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            parts.append(text)
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
def build_plan(
    fieldnames: Sequence[str],
    rows: Sequence[dict],
    orders: Sequence[Order],
    window_days: int = DEFAULT_WINDOW_DAYS,
    words: int = DEFAULT_WORDS_PER_ITEM,
    skipped_other_card: int = 0,
    skipped_unparsable: int = 0,
    split_shipments: bool = True,
    refunds: bool = True,
    refund_window_days: int = DEFAULT_REFUND_WINDOW_DAYS,
) -> Plan:
    """Pair statement rows with orders, and decide the zero-charge inserts.

    Matching is exact on amount, with the charge falling 0..``window_days`` days
    AFTER the order date, assigned greedily from the smallest lag outward. The
    assignment is one-to-one and consumes both sides: identical amounts recur
    (four $10.73 charges in one real file), and reusing one order for two rows
    would invent history.
    """
    plan = Plan(skipped_other_card=skipped_other_card,
                skipped_unparsable=skipped_unparsable)

    dated = [o for o in orders if o.date]
    if dated:
        plan.coverage = (min(o.date for o in dated), max(o.date for o in dated))

    chargeable = [o for o in orders if not o.is_zero_charge]
    zero_charge = [o for o in orders if o.is_zero_charge]

    # Candidate statement rows: purchases only, with a parsable date and amount.
    candidates: dict = {}
    for idx, row in enumerate(rows):
        if (row.get(COL_TYPE) or "").strip() != PURCHASE_TYPE:
            continue
        try:
            date = dt.datetime.strptime(
                (row.get(COL_TXN_DATE) or "").strip(), CSV_DATE_FMT
            ).date()
        except ValueError:
            continue
        try:
            cents = abs(dollars_to_cents(row.get(COL_AMOUNT)))
        except ValueError:
            continue
        candidates[idx] = (date, cents)

    # (lag, row index, order index) for every legal pairing, smallest lag first.
    # The lag is signed and must be >= 0: a charge cannot precede the order that
    # produced it, and admitting negatives lets a months-old coincidental amount
    # match consume the order its real charge needed. Sorting on the full triple
    # keeps the outcome deterministic when two orders sit the same lag from a row.
    pairs = []
    for idx, (date, cents) in candidates.items():
        for oi, order in enumerate(chargeable):
            if order.charged_cents != cents:
                continue
            lag = (date - order.date).days
            if 0 <= lag <= window_days:
                pairs.append((lag, idx, oi))
    pairs.sort()

    used_rows: set = set()
    used_orders: set = set()
    for _lag, idx, oi in pairs:
        if idx in used_rows or oi in used_orders:
            continue
        row = rows[idx]
        if (row.get(COL_MEMO) or "").strip():
            # Never clobber a memo the user (or a previous run) already wrote.
            # The order is still consumed: it DID pair with this row, and leaving
            # it free would let it stamp its items onto some other charge.
            plan.already_had_memo += 1
            used_rows.add(idx)
            used_orders.add(oi)
            continue
        memo = summarize(chargeable[oi].items, words)
        if not memo:
            continue
        plan.memos[idx] = memo
        used_rows.add(idx)
        used_orders.add(oi)

    # Second pass, on leftovers only: one order paid by several charges.
    if split_shipments:
        _recover_split_shipments(plan, rows, candidates, chargeable, used_rows,
                                 used_orders, window_days, words)

    if refunds:
        _match_refunds(plan, rows, orders, refund_window_days, words)

    plan.unmatched_rows = [i for i in candidates if i not in used_rows]
    plan.unused_orders = [o for i, o in enumerate(chargeable) if i not in used_orders]

    # Zero-charge inserts are count-aware per date, mirroring the multiset dedup
    # in importers/record.identity_key: if this file already carries N zero-amount
    # rows on a date, only the surplus is added. Without it, re-running the tool on
    # its own output duplicates every gift-card order.
    existing_zero = {}
    for row in rows:
        if (row.get(COL_TYPE) or "").strip() != PURCHASE_TYPE:
            continue
        try:
            if dollars_to_cents(row.get(COL_AMOUNT)) != 0:
                continue
            date = dt.datetime.strptime(
                (row.get(COL_TXN_DATE) or "").strip(), CSV_DATE_FMT
            ).date()
        except ValueError:
            continue
        existing_zero[date] = existing_zero.get(date, 0) + 1

    for order in sorted(zero_charge, key=lambda o: o.date, reverse=True):
        if existing_zero.get(order.date, 0) > 0:
            existing_zero[order.date] -= 1
            plan.already_present += 1
            continue
        plan.inserts.append((order, _zero_row(fieldnames, order, words)))
    return plan


def _subsets_summing_to(values: Sequence[int], target: int, tolerance: int,
                        max_size: int, min_size: int = 1) -> list:
    """Every index-combination of ``values`` summing to ``target`` within slack."""
    found = []
    limit = min(len(values), max_size)
    for size in range(min_size, limit + 1):
        for combo in itertools.combinations(range(len(values)), size):
            if abs(sum(values[i] for i in combo) - target) <= tolerance:
                found.append(combo)
    return found


def _find_charge_group(order: Order, pool: Sequence, window_days: int) -> Optional[list]:
    """Find the unique set of leftover charges that sums to this order's total.

    ``pool`` is ``[(row_index, date, cents)]`` of still-unmatched purchase rows.
    Groups of one are excluded: a single charge equal to the total is an ordinary
    exact match, which the main pass already had its chance at. Ambiguity is
    fatal by design -- if two different subsets reach the total there is no
    evidence which is real, and guessing would put wrong items on a row.
    """
    near = [(idx, cents) for idx, date, cents in pool
            if 0 <= (date - order.date).days <= window_days]
    if not near or len(near) > MAX_SPLIT_POOL:
        return None
    combos = _subsets_summing_to([c for _i, c in near], order.charged_cents,
                                 SPLIT_TOLERANCE_CENTS, MAX_SPLIT_CHARGES,
                                 min_size=2)
    if len(combos) != 1:
        return None
    return [near[i][0] for i in combos[0]]


def _partition_items(item_prices: Sequence[int], charges: Sequence[int],
                     rate: float) -> Optional[list]:
    """Split the order's items across its charges, or None if not exactly one way.

    Amazon bills each shipment for its own items plus that invoice's tax, so a
    charge equals ``sum(items in that shipment) * (1 + rate)``. Requiring a
    COMPLETE partition -- every item used exactly once, every charge satisfied --
    is what makes this safe: a coincidental subset almost never leaves the
    remainder able to cover the other charges exactly. Returns one index list per
    charge, in the order the charges were given.
    """
    if not item_prices or len(item_prices) > MAX_SPLIT_ITEMS:
        return None
    solutions: list = []

    def gross(indices) -> int:
        return round(sum(item_prices[i] for i in indices) * (1.0 + rate))

    def assign(charge_i: int, remaining: frozenset, acc: list) -> None:
        if len(solutions) > 1:        # ambiguous; stop early
            return
        if charge_i == len(charges):
            if not remaining:
                solutions.append([list(a) for a in acc])
            return
        pool = sorted(remaining)
        for size in range(1, len(pool) + 1):
            for combo in itertools.combinations(pool, size):
                if abs(gross(combo) - charges[charge_i]) <= SPLIT_TOLERANCE_CENTS:
                    acc.append(combo)
                    assign(charge_i + 1, remaining - set(combo), acc)
                    acc.pop()
                    if len(solutions) > 1:
                        return

    assign(0, frozenset(range(len(item_prices))), [])
    return solutions[0] if len(solutions) == 1 else None


def _recover_split_shipments(plan: Plan, rows: Sequence[dict], candidates: dict,
                             chargeable: Sequence[Order], used_rows: set,
                             used_orders: set, window_days: int, words: int) -> None:
    """Second pass: reunite one order with the several charges that paid for it.

    Amazon bills per SHIPMENT, so one order total can equal the sum of two or more
    statement rows and match none of them individually. This runs only on what the
    exact pass could not place, and only commits when both subset searches have a
    unique answer -- so it never disturbs an exact match.
    """
    leftover_orders = [(i, o) for i, o in enumerate(chargeable) if i not in used_orders]
    for order_i, order in leftover_orders:
        pool = [(idx, date, cents) for idx, (date, cents) in candidates.items()
                if idx not in used_rows]
        group = _find_charge_group(order, pool, window_days)
        if group is None:
            continue
        # Largest charge first: the biggest shipment constrains hardest, which
        # prunes the search early and keeps the recursion shallow.
        group.sort(key=lambda idx: candidates[idx][1], reverse=True)
        charges = [candidates[idx][1] for idx in group]

        # The invoice's own effective rate, so tax/shipping are apportioned the
        # way Amazon actually billed them rather than by an assumed percentage.
        if order.subtotal_cents <= 0:
            plan.split_failed.append((order, "no Item(s) Subtotal to derive a tax rate"))
            continue
        rate = (order.charged_cents - order.subtotal_cents) / order.subtotal_cents
        assignment = _partition_items(order.item_prices, charges, rate)
        if assignment is None:
            plan.split_failed.append(
                (order, f"charges {[c / 100 for c in charges]} identified, "
                        f"but items do not partition uniquely"))
            continue

        placed = []
        for idx, item_indices in zip(group, assignment):
            memo = summarize([order.items[i] for i in item_indices], words)
            if not memo or (rows[idx].get(COL_MEMO) or "").strip():
                continue
            plan.memos[idx] = memo
            used_rows.add(idx)
            plan.split_matched += 1
            placed.append((idx, candidates[idx][1]))
        if placed:
            used_orders.add(order_i)
            plan.split_groups.append((order, placed))


def _match_refunds(plan: Plan, rows: Sequence[dict], orders: Sequence[Order],
                   window_days: int, words: int) -> None:
    """Name the item behind a ``Return`` credit -- but only where it is provable.

    This pass is deliberately much narrower than the purchase passes, because the
    two guards that make those safe are both absent here. A refund returns ONE
    item, so there is no complete-partition constraint to catch a wrong pick; and
    the per-item tax rate cannot be derived from a mixed order (a real 5-item
    invoice blends to 6.64% while the returned item was actually taxed at 7.45%,
    so grossing it up misses by 14 cents).

    So it commits only when the order has exactly ONE item and its ``Refund
    Total`` equals the credit exactly. Then no arithmetic is needed and no
    ambiguity exists: that order refunded that amount, and it contained one thing.
    Anything else is left alone rather than fitted.

    Purchase matching is untouched by this -- an order is both bought and
    refunded, so the two passes keep separate books and an order used here stays
    available to the charge passes.
    """
    eligible = [o for o in orders
                if o.refund_cents and len(o.items) == 1 and o.date]
    if not eligible:
        return
    used: set = set()
    for idx, row in enumerate(rows):
        if (row.get(COL_TYPE) or "").strip() != REFUND_TYPE:
            continue
        if (row.get(COL_MEMO) or "").strip():
            continue
        try:
            date = dt.datetime.strptime(
                (row.get(COL_TXN_DATE) or "").strip(), CSV_DATE_FMT
            ).date()
            cents = abs(dollars_to_cents(row.get(COL_AMOUNT)))
        except ValueError:
            continue
        hits = [o for o in eligible
                if id(o) not in used
                and o.refund_cents == cents
                and 0 <= (date - o.date).days <= window_days]
        if len(hits) != 1:
            continue
        memo = summarize(hits[0].items, words)
        if not memo:
            continue
        plan.memos[idx] = REFUND_PREFIX + memo
        plan.refund_matched += 1
        used.add(id(hits[0]))


def _zero_row(fieldnames: Sequence[str], order: Order, words: int) -> dict:
    """Build a $0.00 statement row for an order that never reached the card."""
    stamp = order.date.strftime(CSV_DATE_FMT)
    values = {
        COL_TXN_DATE: stamp,
        COL_POST_DATE: stamp,
        COL_DESC: ZERO_DESCRIPTION,
        COL_CATEGORY: ZERO_CATEGORY,
        COL_TYPE: PURCHASE_TYPE,
        COL_AMOUNT: "0.00",
        COL_MEMO: summarize(order.items, words),
    }
    # Honour the real header: unknown columns stay blank rather than vanishing.
    return {name: values.get(name, "") for name in fieldnames}


def apply_plan(fieldnames: Sequence[str], rows: Sequence[dict], plan: Plan) -> list:
    """Return the rewritten row list: memos filled, zero-charge rows inserted.

    Output stays sorted by transaction date descending, the order Chase ships, so
    the file still reads like a statement.
    """
    out = []
    for idx, row in enumerate(rows):
        new = {name: row.get(name, "") for name in fieldnames}
        if idx in plan.memos:
            new[COL_MEMO] = plan.memos[idx]
        out.append(new)
    out.extend(row for _order, row in plan.inserts)

    def sort_key(row):
        try:
            return dt.datetime.strptime(
                (row.get(COL_TXN_DATE) or "").strip(), CSV_DATE_FMT
            ).date()
        except ValueError:
            return dt.date.min

    # Stable sort: rows sharing a date keep their original relative order, and an
    # inserted row lands after the real rows for that day.
    out.sort(key=sort_key, reverse=True)
    return out


def write_csv(path, fieldnames: Sequence[str], rows: Sequence[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def detect_card(path) -> str:
    """Pull the card's last four from a Chase filename ('Chase4321_Activity...')."""
    m = re.search(r"(\d{4})[_-]?Activity", Path(path).name, re.I)
    return m.group(1) if m else ""


def format_report(plan: Plan, card: str, window_days: int) -> str:
    lines = [
        f"card              {card or '(all)'}",
        f"window            0..{window_days} day(s) order -> {COL_TXN_DATE}",
    ]
    lo, hi = plan.coverage
    if lo and hi:
        lines.append(f"invoice coverage  {lo} -> {hi}")
    lines += [
        f"memos filled      {plan.matched}"
        + (f"  ({plan.split_matched} via split shipments)" if plan.split_matched else "")
        + (f"  ({plan.refund_matched} refund)" if plan.refund_matched else ""),
        f"$0.00 inserted    {len(plan.inserts)}",
        f"unmatched rows    {len(plan.unmatched_rows)}",
        f"unused orders     {len(plan.unused_orders)}",
    ]
    if plan.already_had_memo:
        lines.append(f"memo kept as-is   {plan.already_had_memo}")
    if plan.already_present:
        lines.append(f"$0.00 already in  {plan.already_present}")
    if plan.skipped_other_card:
        lines.append(f"other-card orders {plan.skipped_other_card} (skipped)")
    if plan.skipped_unparsable:
        lines.append(f"unusable records  {plan.skipped_unparsable} (skipped)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m mammon.amazon_invoices",
        description="Fill a Chase CSV's Memo column from an Amazon invoice scrape.",
    )
    ap.add_argument("csv_path", help="Chase activity CSV download")
    ap.add_argument("json_path", help="webSlinger GetAmazonInvoices output JSON")
    ap.add_argument("-o", "--out", help="output CSV (default: <input>.enriched.csv)")
    ap.add_argument("--card", default=None,
                    help="card last-four to match (default: read from the CSV filename)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS,
                    help=f"max days the charge may lag the order "
                         f"(default {DEFAULT_WINDOW_DAYS}; the charge never precedes it)")
    ap.add_argument("--words", type=int, default=DEFAULT_WORDS_PER_ITEM,
                    help=f"words kept per item (default {DEFAULT_WORDS_PER_ITEM}; 0 = all)")
    ap.add_argument("--no-split-shipments", action="store_true",
                    help="skip the second pass that reunites one order with the "
                         "several charges that paid for it")
    ap.add_argument("--no-refunds", action="store_true",
                    help="skip naming the item behind a Return credit")
    ap.add_argument("--refund-window", type=int, default=DEFAULT_REFUND_WINDOW_DAYS,
                    help=f"max days a refund may lag its order "
                         f"(default {DEFAULT_REFUND_WINDOW_DAYS})")
    ap.add_argument("--no-zero-rows", action="store_true",
                    help="do not insert $0.00 rows for gift-card / rewards orders")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing a file")
    ap.add_argument("--verbose", action="store_true",
                    help="list unmatched statement rows and unused orders")
    args = ap.parse_args(argv)

    card = args.card if args.card is not None else detect_card(args.csv_path)
    fieldnames, rows = load_csv(args.csv_path)
    orders, other_card, unparsable = load_orders(args.json_path, card)
    plan = build_plan(fieldnames, rows, orders, window_days=args.window,
                      words=args.words, skipped_other_card=other_card,
                      skipped_unparsable=unparsable,
                      split_shipments=not args.no_split_shipments,
                      refunds=not args.no_refunds,
                      refund_window_days=args.refund_window)
    if args.no_zero_rows:
        plan.inserts = []

    print(format_report(plan, card, args.window))
    if args.verbose:
        lo, hi = plan.coverage
        print("\nunmatched statement rows:")
        for idx in plan.unmatched_rows:
            row = rows[idx]
            note = ""
            if lo and hi:
                try:
                    d = dt.datetime.strptime(row[COL_TXN_DATE], CSV_DATE_FMT).date()
                    if d < lo or d > hi:
                        note = "  (outside invoice coverage)"
                except ValueError:
                    pass
            print(f"  {row.get(COL_TXN_DATE)} {row.get(COL_AMOUNT):>10}  "
                  f"{row.get(COL_DESC)}{note}")
        print("\norders with no matching charge:")
        for o in plan.unused_orders:
            print(f"  {o.date} {o.charged_cents / 100:>10.2f}  {o.order_number}"
                  f"  {summarize(o.items, 5)[:60]}")
        if plan.split_groups:
            print("\nsplit shipments recovered (one order, several charges):")
            for order, placed in plan.split_groups:
                total = sum(c for _i, c in placed)
                print(f"  {order.date}  order {order.order_number} "
                      f"{total / 100:.2f} = "
                      f"{' + '.join(f'{c / 100:.2f}' for _i, c in placed)}")
                for idx, cents in placed:
                    print(f"      {rows[idx][COL_TXN_DATE]} {cents / 100:>9.2f}  "
                          f"{plan.memos.get(idx, '')[:56]}")
        for order, reason in plan.split_failed:
            print(f"\nsplit not resolved: order {order.order_number} "
                  f"{order.charged_cents / 100:.2f} -- {reason}")
        # The offsetting leg CSV cannot carry. Printing it turns the manual split
        # into transcription rather than a hunt back through the invoice.
        offsets = [o for o, _row in plan.inserts if o.offset_cents]
        if offsets:
            print("\ngift-card / rewards offsets for the inserted $0.00 rows:")
            for o in offsets:
                legs = []
                if o.gift_card_cents:
                    legs.append(f"[Amazon Gift Card] {o.gift_card_cents / 100:.2f}")
                if o.rewards_cents:
                    legs.append(f"[Amazon Rewards] {o.rewards_cents / 100:.2f}")
                print(f"  {o.date}  {' + '.join(legs)}"
                      f"  {summarize(o.items, 5)[:52]}")

    if args.dry_run:
        return 0

    out_path = args.out or str(
        Path(args.csv_path).with_suffix("").as_posix() + ".enriched.csv"
    )
    write_csv(out_path, fieldnames, apply_plan(fieldnames, rows, plan))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
