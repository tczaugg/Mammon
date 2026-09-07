"""Pure Amazon invoice itemization: turn a downloaded invoice into split legs.

Phase-1 CORE only. This module never touches the register. It reads a webSlinger
Amazon invoice report file (the time-tagged JSON that lands in the user's
Downloads folder) on demand and returns plain data structures -- parsed orders
and, for a matched card charge, the per-item split legs plus their math. Writing
those legs, creating the review rows, and any UI belong to a later phase and go
through :mod:`mammon.import_review` / :mod:`mammon.ledger` (the single writer),
exactly like every other ingestion path. Mammon does NOT store Amazon data in its
database; the invoice file is loaded when needed and may be re-loaded across
sessions.

It builds on :mod:`mammon.amazon_invoices`, which already parses both scrape
schemas into :class:`~mammon.amazon_invoices.Order` records -- cents fields, with
LIST prices index-aligned to de-duplicated item titles (a part-shipped item
prints once per shipment, each line repeating the whole-lot price; those repeats
are collapsed there). Do not re-parse the JSON here; call
:func:`load_invoice_orders`.

The allocation rule (locked by the user; overrides the design doc where they
differ)
-----------------------------------------------------------------------------
A matched charge is a debit on the card (negative cents). It becomes a split
whose legs sum EXACTLY to that charge, in signed integer cents, with no floats:

* **One leg per item.** Each item's leg carries its share of the *item portion*
  -- the items' cost grossed up by tax -- distributed across items by LIST PRICE
  with largest-remainder rounding, so the item legs sum to the item portion to
  the cent. TAX is therefore allocated PROPORTIONALLY across the item legs and is
  never a leg of its own. The item portion is ``|charge| + gift_card + rewards``
  because the invoice ``Grand Total`` (which is the charge) is already net of the
  offsets, so the items cost more than the charge by exactly that offset. List
  prices are structurally non-additive, so they weight the split but are never
  summed to reconcile it -- the charge is authoritative.

* **Gift card and rewards are CATEGORIES, not accounts.** When an order was paid
  in part from an Amazon gift-card balance or reward points, a balancing money-IN
  leg is added for each, categorised ``gift cards`` / ``reward points``. This is
  the user's explicit override of the design doc, which modelled the offset as a
  transfer to a synthetic ``[Amazon Gift Card]`` / ``[Amazon Rewards]`` account:
  NEVER create an account here. The offset legs net against the proportionally
  allocated item legs to reach the charge. (Worked example: subtotal $80, tax $6,
  gift card $30 -> charge -$56.00; items B1 list $30, B2 list $50 -> legs
  ``B1 -32.25``, ``B2 -53.75``, ``gift cards +30.00`` -> sum ``-56.00``.)

* **Multiple charges per order.** Amazon bills per shipment, so one order can be
  charged several times. Each charge is allocated INDEPENDENTLY from its own item
  subset and is self-consistent to its own cents; see :func:`allocate_charges`.
  Which shipment consumed a shared gift card is not in the scrape, so the caller
  supplies the per-charge offset (0 by default) -- this module does not guess.

Item legs are left uncategorised (``category == ""``) for the user to fill from a
dropdown ordered by :func:`default_category_order`; matching a charge is the
expensive step, categorising it is the exception. Refunds are out of scope (they
are not on an invoice and are handled manually).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from mammon import amazon_invoices
from mammon.amazon_invoices import Order  # re-exported for callers' convenience


#: Category names for the two money-in offset legs. Ordinary categories (the
#: normal category-resolution path creates one lazily when a leg is written, in a
#: later phase), NEVER accounts -- a gift-card / rewards offset is value that did
#: not come from the card, not a transfer, and no account is ever created here.
GIFT_CARD_CATEGORY = "gift cards"
REWARDS_CATEGORY = "reward points"

#: Memo on the safety-net leg that absorbs the item portion when there are no
#: usable list prices to weight by. Visible, never silent -- see allocate_charge.
UNALLOCATED_MEMO = "Amazon - unallocated"

#: The item-leg category dropdown's default ORDERING, most-likely first. Only an
#: ordering: it never overrides a learned rule or the user's own pick, and never
#: proposes a category the payee has not already carried.
DEFAULT_CATEGORY_FALLBACK = (
    "household",
    "groceries",
    "electronics accessories",
    "computer accessories",
    "electronic hardware",
    "computer hardware",
)


@dataclass
class Item:
    """One line item on an invoice: its title and LIST price in cents.

    List prices are structurally non-additive (a part-shipped item repeats its
    whole-lot price), so they are used only as PROPORTIONAL WEIGHTS, never summed
    to reconcile a charge."""

    description: str
    list_price_cents: int


@dataclass
class SplitLeg:
    """One leg of the split proposed for a charge. Signed integer cents: negative
    is money out (an item, or the unallocated remainder), positive is money in (a
    gift-card / rewards offset)."""

    amount_cents: int
    memo: str = ""
    category: str = ""      # "" = leave for the user to fill (item legs)
    kind: str = "item"      # 'item' | 'giftcard' | 'rewards' | 'unallocated'


@dataclass
class ChargeAllocation:
    """The proposed split for a single charge. Invariant, always enforced:
    ``sum(leg.amount_cents for leg in legs) == charge_cents``."""

    charge_cents: int
    legs: list = field(default_factory=list)

    @property
    def total_cents(self) -> int:
        return sum(leg.amount_cents for leg in self.legs)

    @property
    def item_legs(self) -> list:
        return [leg for leg in self.legs if leg.kind == "item"]


@dataclass
class ChargeSpec:
    """Input to :func:`allocate_charges`: one shipment's charge, the items it paid
    for, and any gift-card / reward offset the caller attributes to it."""

    charge_cents: int                            # signed; <= 0 (a debit)
    items: list = field(default_factory=list)    # list[Item]
    gift_card_cents: int = 0                      # magnitude (>= 0)
    rewards_cents: int = 0                        # magnitude (>= 0)


# ---------------------------------------------------------------------------
# loading (reuse mammon.amazon_invoices; do NOT re-parse the JSON here)
# ---------------------------------------------------------------------------
def load_invoice_orders(path, card_number: str = "") -> list:
    """Parse the webSlinger invoice file at ``path`` into ``Order`` records.

    Thin pass-through to :func:`mammon.amazon_invoices.load_orders` (which reads
    both scrape schemas and de-duplicates part-shipment item lines), dropping the
    skip counters the CSV tool needs. No database access; the file is read on
    demand and nothing is stored."""
    orders, _skipped_other_card, _skipped_unparsable = amazon_invoices.load_orders(
        path, card_number
    )
    return orders


def items_of(order: Order) -> list:
    """The order's items as :class:`Item` records, list price index-aligned. A
    missing price falls back to 0 (it just carries no weight in the split)."""
    prices = list(order.item_prices or [])
    out: list = []
    for i, desc in enumerate(order.items or []):
        price = prices[i] if i < len(prices) else 0
        out.append(Item(description=str(desc), list_price_cents=int(price)))
    return out


# ---------------------------------------------------------------------------
# allocation
# ---------------------------------------------------------------------------
def _largest_remainder(total: int, weights: Sequence[int]) -> Optional[list]:
    """Split ``total`` (integer cents, ``>= 0``) across ``weights`` proportionally
    by the largest-remainder method, so the parts are integers summing to
    ``total`` EXACTLY. Ties in the remainder are broken by lowest index, so the
    result is deterministic. Returns ``None`` when the weights carry no signal
    (empty, or all zero) and the caller must fall back. Integer arithmetic only --
    no floats, so no rounding drift."""
    n = len(weights)
    if n == 0:
        return None
    tw = sum(weights)
    if tw <= 0:
        return None
    scaled = [total * w for w in weights]            # exact integers
    floors = [s // tw for s in scaled]
    parts = list(floors)
    deficit = total - sum(floors)                    # 0 <= deficit < n
    if deficit:
        rems = [s - f * tw for s, f in zip(scaled, floors)]   # s % tw, in [0, tw)
        order = sorted(range(n), key=lambda i: (-rems[i], i))
        for i in order[:deficit]:
            parts[i] += 1
    return parts


def allocate_charge(
    charge_cents: int,
    items: Sequence[Item],
    gift_card_cents: int = 0,
    rewards_cents: int = 0,
    default_category: str = "",
) -> ChargeAllocation:
    """Turn one card charge into per-item split legs that sum EXACTLY to it.

    ``charge_cents`` is the signed amount billed and must be a debit (``<= 0``);
    an Amazon refund is a credit and is out of scope. ``gift_card_cents`` /
    ``rewards_cents`` are non-negative magnitudes of the invoice's offset lines
    attributed to this charge. See the module docstring for the rule. Integer
    cents throughout; the returned allocation always satisfies
    ``result.total_cents == charge_cents``."""
    if charge_cents > 0:
        raise ValueError(
            "allocate_charge expects a debit (charge_cents <= 0); Amazon refunds "
            "are handled manually and are out of scope"
        )
    gift_card_cents = max(0, int(gift_card_cents))
    rewards_cents = max(0, int(rewards_cents))
    out_magnitude = -int(charge_cents)               # money that left the card, >= 0
    # The charge (invoice Grand Total) is already net of the offsets, so the items
    # cost more than the charge by exactly that offset. Gross the item portion back
    # up before splitting it; the offset legs below net it away again.
    item_portion = out_magnitude + gift_card_cents + rewards_cents

    weights = [max(0, int(it.list_price_cents)) for it in items]
    shares = _largest_remainder(item_portion, weights)

    legs: list = []
    if shares is None:
        # No usable list prices to weight by: keep the money on one visible leg
        # rather than fabricate a split. (item_portion == 0 -> a single zero leg,
        # harmless; a caller collapses a lone leg to a plain categorised row.)
        legs.append(
            SplitLeg(
                amount_cents=-item_portion,
                memo=UNALLOCATED_MEMO,
                category=default_category,
                kind="unallocated",
            )
        )
    else:
        for it, share in zip(items, shares):
            legs.append(
                SplitLeg(
                    amount_cents=-share,
                    memo=str(it.description),
                    category=default_category,
                    kind="item",
                )
            )

    if gift_card_cents:
        legs.append(
            SplitLeg(
                amount_cents=gift_card_cents,
                category=GIFT_CARD_CATEGORY,
                kind="giftcard",
            )
        )
    if rewards_cents:
        legs.append(
            SplitLeg(
                amount_cents=rewards_cents,
                category=REWARDS_CATEGORY,
                kind="rewards",
            )
        )

    alloc = ChargeAllocation(charge_cents=int(charge_cents), legs=legs)
    # Belt and suspenders: the largest-remainder split is already exact, but fold
    # any residual (only possible on the price-less fallback path) into the last
    # money-out leg so the invariant total_cents == charge_cents cannot silently
    # break. The design doc's residual-absorb rule, made explicit.
    residual = int(charge_cents) - alloc.total_cents
    if residual:
        target = next(
            (leg for leg in reversed(legs) if leg.kind in ("item", "unallocated")),
            None,
        )
        if target is None:
            legs.append(
                SplitLeg(
                    amount_cents=residual,
                    memo=UNALLOCATED_MEMO,
                    category=default_category,
                    kind="unallocated",
                )
            )
        else:
            target.amount_cents += residual
    return alloc


def allocate_order(order: Order, default_category: str = "") -> ChargeAllocation:
    """Single-charge convenience: allocate a whole order to one charge equal to its
    ``Grand Total`` (a debit), using the order's own gift-card / rewards. Use
    :func:`allocate_charges` when the order was billed across several shipments."""
    return allocate_charge(
        charge_cents=-int(order.charged_cents),
        items=items_of(order),
        gift_card_cents=int(order.gift_card_cents),
        rewards_cents=int(order.rewards_cents),
        default_category=default_category,
    )


def allocate_charges(
    specs: Iterable[ChargeSpec], default_category: str = ""
) -> list:
    """Allocate each charge of a multi-shipment order INDEPENDENTLY -- one
    :class:`ChargeAllocation` per :class:`ChargeSpec`, each self-consistent to its
    own charge. The item partition and per-charge offsets carried by the specs are
    the caller's decision (a later phase's matcher), never guessed here."""
    return [
        allocate_charge(
            charge_cents=s.charge_cents,
            items=s.items,
            gift_card_cents=s.gift_card_cents,
            rewards_cents=s.rewards_cents,
            default_category=default_category,
        )
        for s in specs
    ]


# ---------------------------------------------------------------------------
# default category ordering for the item-leg dropdown
# ---------------------------------------------------------------------------
def default_category_order(historical: Iterable[str] = ()) -> list:
    """The item-leg category dropdown's default ordering: the fixed fallback list
    (:data:`DEFAULT_CATEGORY_FALLBACK`) first, then any categories the user has
    historically put on Amazon rows (see :func:`amazon_categories_from_history`),
    de-duplicated case-insensitively with the first spelling and position kept.
    Ordering ONLY -- it never overrides a learned rule or the user's own pick, and
    proposes nothing the data has not already shown."""
    out: list = []
    seen: set = set()
    for name in list(DEFAULT_CATEGORY_FALLBACK) + [str(h) for h in historical]:
        name = str(name).strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def amazon_categories_from_history(conn, payee_like: str = "%amazon%") -> list:
    """READ-ONLY: categories the user has already applied to Amazon transactions,
    most-used first, as ``Parent:Child`` paths. Feeds :func:`default_category_order`
    so the dropdown surfaces the user's real Amazon habits after the fixed list.

    This is the module's ONLY database access and it only reads -- it opens no
    write path and honours the learned rules by not touching them. Kept out of the
    pure parse/allocate functions so those stay import-time DB-free and offline."""
    from mammon import ledger  # lazy: keep the allocator import path DB-free

    rows = conn.execute(
        """
        SELECT category_id, COUNT(*) AS n
          FROM transactions
         WHERE category_id IS NOT NULL
           AND payee IS NOT NULL
           AND LOWER(payee) LIKE LOWER(?)
      GROUP BY category_id
      ORDER BY n DESC, category_id ASC
        """,
        (payee_like,),
    ).fetchall()
    out: list = []
    for row in rows:
        path = ledger.category_path(conn, row[0])
        if path:
            out.append(path)
    return out
