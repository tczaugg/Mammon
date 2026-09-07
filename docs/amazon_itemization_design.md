# Amazon Invoice Itemization — Concept of Operations

**Status: DESIGN, for user review. Nothing here is implemented; this document changes no runtime
behavior and proposes no code yet.**

## 0. Goal and scope

Turn a matched Amazon card charge into an itemized register row: fill its memo with what was bought
and, where it helps, break the charge into per-item **split legs** the user can categorize. The
concrete pieces the user asked for:

1. Run the `GetAmazonInvoices` webSlinger script (~20-30 min per year) to scrape Amazon invoices.
2. Download the Prime credit-card transactions through the **normal** review flow.
3. Add a review action that integrates an invoice's line items as **splits** on the matching card
   charge.

The hard case this document must solve: **one Amazon invoice (one order) charged across several
shipment dates.** Amazon issues one invoice per order but bills at ship time, so one order's grand
total can equal the *sum* of several separate card charges and match none of them individually. The
items must be partitioned across those charges.

Design principles carried from the repo (non-negotiable):

- **`mammon.ledger` is the only writer of transaction rows and splits.** Splits are written through
  `ledger.set_splits(conn, txn_id, lines)` only. The review layer (`import_review.py`) stays the sole
  review->ledger writer and gains a call *into* `ledger`; it does not learn to write splits itself.
- **Integer cents** for all cash; item list prices scaled to paid cents with largest-remainder
  rounding so legs sum exactly. No floats.
- **Thin-projection UI**: `ui/import_review_widget.py` shows proposals and dropdowns; all money and
  matching logic lives in the domain/review layer, no SQL in `ui/`.
- Exact + fuzzy dedup already exists in `importers/core.py`; the card download reuses it unchanged.
- Any schema change is an **appended** migration (`SCHEMA_VERSION` is 60 in `db.py`); never edit an
  existing one.

### What already exists (build on it, do not duplicate)

`mammon/amazon_invoices.py` is today a **pure CSV-in / CSV-out** pre-processor (CLI only, not wired
to the app). It already contains, and this design reuses, the load-bearing algorithms:

- `load_orders()` parses the `GetAmazonInvoices` JSON into `Order` records with cents fields
  (`charged_cents`, `subtotal_cents`, `gift_card_cents`, `rewards_cents`, `refund_cents`,
  `item_prices`, `items`, `card_number`). Two scrape schemas are read: newer `priceAccounting`
  (`Item(s) Subtotal:`, `Gift Card Amount:`, `Rewards Points:`, `Grand Total:`), older flat
  `invoicePrice`.
- Matching on the invoice **`Grand Total`, never `orderPrice`**, one-to-one, exact cents, directional
  date window `0..21` days (`DEFAULT_WINDOW_DAYS`).
- `_recover_split_shipments()` / `_partition_items()` — the split-across-shipments recovery, keyed on
  the invoice's own effective rate `r = (grand - subtotal) / subtotal`.
- `summarize()` — collapses repeated item titles (a part-shipped item prints once per shipment, each
  line repeating the whole-lot price) into a de-duplicated memo. **Item prices are structurally
  non-additive; never sum them to reconcile — the `Grand Total` is authoritative.**

The design's job is to **lift these algorithms off the CSV transform and onto the review flow**, and
to add split-writing, which CSV could not express.

---

## 1. End-to-end data flow

Two independent feeds meet at review time — the card charges and the invoices — exactly as the
current CSV+JSON join does, but joined against pending review rows instead of CSV rows.

```
 (A) Invoices feed                         (B) Card feed
 ─────────────────                         ─────────────
 webSlinger GetAmazonInvoices              webSlinger card-download script
   run via mcp__webslinger__run_script       (existing per-institution script)
        │  ~20-30 min/yr                          │
        ▼                                          ▼
   invoice scrape JSON                       downloaded file / scraped rows
   (orders[], priceAccounting)               (downloads.py, EXPORT or SCRAPE)
        │                                          │
        ▼                                          ▼
   parse via amazon_invoices.load_orders     import_review.build_review(conn, card_acct, rows)
   -> list[Order] (cents)                       -> ReviewEntry[] (NEW / MATCHING)
        │                                          │ (normal review panel below the register;
        │                                          │  exact+fuzzy dedup already applied)
        └──────────────┬───────────────────────────┘
                       ▼
        Amazon itemization pass  (NEW: mammon/amazon_itemize.py, domain layer, UI-free)
          • select the Amazon charges among pending rows (by account = Prime card,
            filtered by invoice card_number / descriptor)
          • run the existing single-charge match, then split-shipment recovery,
            then refund match, against the pending rows
          • for each matched charge, compute a proposed SPLIT set (cents)
          • skip charges already linked to an order (idempotency)
                       │
                       ▼  proposals attached to ReviewEntry (in memory + persisted, see §2)
        ui/import_review_widget.py renders an "itemized" badge + expandable legs
                       │
                       ▼  user accepts a row / a shipment group / bulk-accepts
        import_review.save_new(...) or accept_match(...)      ← sole review->ledger writer
                       │  then, when a proposal has >= 2 legs:
                       ▼
        ledger.set_splits(conn, txn_id, lines)                ← sole split writer
                       │
                       ▼  record order#->txn link (idempotency + undo)
```

Key sequencing rule: **the card feed is imported first** (rows must be pending in review before
itemization can attach to them), then the itemization pass runs over those pending rows plus the
invoice JSON. Running the pass with no matching charge present simply leaves the invoice unconsumed
and reportable ("outside charge coverage"), never an error.

The invoice scrape under-collects and can lag the statement period (a known gap: a run may report
more orders than it emits, and stop short of the last few weeks). The pass therefore treats "no
invoice for this charge" and "no charge for this invoice" as first-class, labeled outcomes, and the
UI surfaces the invoice date-span so a poor match rate points at the scrape before the matcher.

---

## 2. Schema needs

The matching algorithms are pure and need no schema. Two things want persistence: **idempotency**
(a re-run must not re-itemize a charge already done) and **undo/report** (which order fed which
charge). A minimal, appended set of tables:

Migration `_V60` -> version 61 (and `SCHEMA_VERSION` -> 61), tables:

```sql
-- One row per scraped invoice line the pass may consume. Cache of the scrape so
-- review survives a restart without re-scraping; cents are integer.
CREATE TABLE amazon_invoices (
    order_number    TEXT PRIMARY KEY,     -- opaque; never displayed as a join key
    order_date      TEXT NOT NULL,        -- ISO YYYY-MM-DD
    grand_total     INTEGER NOT NULL,     -- charged cents (net of gift card/rewards)
    subtotal        INTEGER,              -- Item(s) Subtotal, cents
    gift_card       INTEGER DEFAULT 0,    -- Gift Card Amount, cents
    rewards         INTEGER DEFAULT 0,    -- Rewards Points, cents
    refund_total    INTEGER DEFAULT 0,
    card_number     TEXT DEFAULT '',      -- last-four as scraped; for card filtering only
    raw_json        TEXT NOT NULL,        -- the order's raw record, for re-derivation
    scraped_at      TEXT NOT NULL
);

CREATE TABLE amazon_invoice_items (
    order_number    TEXT NOT NULL REFERENCES amazon_invoices(order_number),
    seq             INTEGER NOT NULL,     -- stable order within the invoice
    description     TEXT NOT NULL,
    list_price      INTEGER NOT NULL,     -- itemPrice cents (LIST price, non-additive)
    PRIMARY KEY (order_number, seq)
);

-- The consumed link: which charge txn received which order (and which shipment,
-- for the split-across-shipments case). Presence == "already itemized".
CREATE TABLE amazon_matches (
    order_number    TEXT NOT NULL REFERENCES amazon_invoices(order_number),
    txn_id          INTEGER NOT NULL REFERENCES transactions(id),
    shipment_index  INTEGER NOT NULL DEFAULT 0,   -- 0 for single-charge orders
    method          TEXT NOT NULL,                -- 'exact' | 'split-shipment' | 'refund' | 'manual'
    created_at      TEXT NOT NULL,
    PRIMARY KEY (order_number, txn_id)
);
```

Notes:

- **The splits themselves need no new table.** They are ordinary `splits` rows written by
  `ledger.set_splits`; `amazon_matches` only records provenance so the pass is idempotent and undoable.
- `amazon_matches.txn_id` must be cleared when the txn is deleted. Because SQLite reuses a deleted
  `transactions` rowid (INTEGER PRIMARY KEY, no AUTOINCREMENT), the link must be removed at delete
  time (an `ON DELETE`-style cleanup in the ledger delete path) so a recycled id cannot inherit a
  stale Amazon link.
- **Alternative (no schema at all):** keep the join fully file-driven — load the scrape JSON at
  action time, match against pending rows in memory, and rely on the fact that an already-split charge
  is visibly split to avoid double-application. Simpler, but loses cross-session idempotency and the
  "unconsumed orders" report, and re-running after a restart re-proposes everything. Recommended only
  if the user prefers zero schema growth. See open question Q6.

---

## 3. The invoice -> charge(s) matching algorithm

Runs in three passes over the pending Amazon review rows for the card account (those whose descriptor
is Amazon and whose amount is a debit), consuming each row and each order at most once. Passes 1-3
reuse the existing functions in `amazon_invoices.py`; the split *writing* (§4) is new.

**Pass 1 — exact single-charge match.** For each order, find a pending charge whose amount equals the
order's `grand_total` (exact cents) within the directional window `0 <= (charge_date - order_date) <=
21` days. Sort candidate pairs by `(lag, row, order)` and consume greedily one-to-one, so a repeated
amount cannot reuse one order. Match on `grand_total` (already net of gift card / rewards), never
`orderPrice`. This is `build_plan`'s core loop, pointed at review rows.

**Pass 2 — split across shipments (the hard case).** Runs on the leftovers only. For an unconsumed
order whose `grand_total` did not equal any single charge:

1. **Subset-sum on the charges.** Find a subset of the remaining Amazon charges (size 2..6,
   `MAX_SPLIT_CHARGES`) whose amounts sum to `grand_total` within `SPLIT_TOLERANCE_CENTS` (2c).
   Commit only if that subset is **unique** — no second subset of leftovers also sums to the total.
2. **Partition the items across those charges.** Using the invoice's effective rate
   `r = (grand_total - subtotal) / subtotal`, a shipment billed for a set of items costs
   `sum(item list prices) * (1 + r)`. Search for the assignment of every item to exactly one charge
   such that each charge equals its shipment's scaled item subtotal (within tolerance), **every item
   used exactly once**. Commit only if that partition is **unique**.
3. If either search is ambiguous or incomplete, **do not guess** — report the order as an ambiguous
   split group and leave those charges for manual handling. The complete-unique-partition requirement
   is the safety net: a coincidental subset almost never leaves a remainder that also partitions
   cleanly.

Worked synthetic example (no real data):

```
Invoice, order placed 2026-03-01:
  Item(s) Subtotal   $150.00
  Tax + shipping     $ 12.00
  Grand Total        $162.00      ->  r = 12.00 / 150.00 = 0.08
  Items:  A1 list $40.00 | A2 list $60.00 | A3 list $50.00

Pending Amazon charges on the Prime card:
  2026-03-04  -$108.00
  2026-03-06  -$ 54.00
  (others, unrelated)

Pass 1: no single charge == $162.00.
Pass 2 subset-sum: {108.00, 54.00} uniquely sums to 162.00.
Pass 2 partition:  {A1,A2} subtotal 100.00 -> 100*(1.08)=108.00  (charge #1)
                   {A3}    subtotal  50.00 ->  50*(1.08)= 54.00  (charge #2)
        unique -> commit. Charge #1 gets items A1,A2; charge #2 gets item A3.
```

**Pass 3 — refunds (unchanged policy).** A `Return` row matches an order's `Refund Total` only when
the order holds exactly one item and the credit equals `Refund Total` exactly, inside a wider window
(`--refund-window`, default 180 days). Refunds are written as **memo only** (prefixed `Return: `),
never split — a single-item credit has no partition to verify and the blended invoice tax rate does
not hold per returned item. This design keeps that as-is.

Not orders, never matched: Prime membership fee, Prime Video, and any charge on a *different* Amazon
account than the one scraped.

---

## 4. Turning a matched charge into splits

This is the new part. For each charge the matcher assigns a shipment's item set `I` and the invoice
context (`grand_total`, `subtotal`, `gift_card`, `rewards`, `r`). The card charge amount is **negative
cents** (money out on the Prime card). Build the split legs so they sum exactly to the charge:

1. **Item legs (money out).** For each item `i` in `I` with list price `p_i`, its paid share is
   `s_i = round(p_i * (1 + r))`, distributed with **largest-remainder rounding** so the item legs sum
   to the item portion of the charge to the cent (never lean on `set_splits`'s residual-absorb for
   ordinary rounding). Leg = `{amount: -s_i, memo: <item title>, category_id: None}`. Category is
   left for the user by default (see §5); the user's expensive step is matching, not categorizing.

2. **Gift-card / rewards offset legs (money in), single-shipment orders only.** When
   `gift_card > 0` or `rewards > 0`, the item legs (at full scaled price) exceed the charge by exactly
   that offset, because `grand_total` is already net of it. Add balancing legs as **transfers**:
   `{transfer_account_id: [Amazon Gift Card], amount: +gift_card}` and/or
   `{... [Amazon Rewards], amount: +rewards}`. `ledger.set_splits` creates the mirror on the offset
   account automatically. This models the offset as value that did not come from the card, matching
   the user's manual `[Amazon Gift Card]` / `[Amazon Rewards]` legs.

   Worked example: order subtotal $80.00, tax $6.00, gift card $30.00 -> charge = 80+6-30 = **-$56.00**.
   Items B1 list $30.00, B2 list $50.00, `r = 6/80 = 0.075`.
   Legs: `B1 -32.25`, `B2 -53.75`, `[Amazon Gift Card] +30.00` -> sum `-56.00` == charge.

   For a **multi-shipment** order that also used a gift card, which shipment consumed the gift card is
   not in the scrape. This design does **not** guess: it attaches item legs per shipment but leaves the
   offset for the user (or attaches the whole offset to one nominated charge as an option). See Q3.

3. **The two-leg rule.** `ledger.set_splits` requires >= 2 legs and NULLs the parent's own category.
   So: if a charge resolves to a single item and no offset (one leg), **do not split** — set the plain
   row's `category` + `memo` (the collapsed summary) instead. Split only when >= 2 legs result
   (multiple items, or item(s) + offset). This makes the single-item shipment case degrade to the
   valuable memo-only result rather than an illegal one-leg split.

4. **Residual.** After largest-remainder rounding, any true leftover (unmodeled fee, tolerance slack)
   is small; `set_splits` folds a signed difference into one uncategorized leg. The design labels that
   leg `Amazon — unallocated` in its memo so it is visible, not silent.

5. **Idempotency & non-clobber.** Before proposing, skip any charge already in `amazon_matches`, and
   any charge that already carries user-entered splits (never overwrite a hand-split row — surface it
   as "already split" instead). On accept, write the `amazon_matches` link(s).

All of §4 runs inside `import_review` at accept time, calling `ledger.set_splits` once per charge —
one writer, one code path.

---

## 5. The review UI surface

Rendered by `ui/import_review_widget.py` (thin projection; no money or SQL logic added there).

- **Trigger.** A panel toolbar action, "Itemize Amazon...". It (a) offers to run `GetAmazonInvoices`
  via `mcp__webslinger__run_script` *or* pick an existing scrape file, then (b) runs the itemization
  pass over the currently pending Amazon rows and annotates them. It is idempotent: re-running only
  proposes charges not already linked.
- **Per matched charge.** An "itemized" badge on the review row. Expanding it shows the proposed
  legs as a small table: item memo, amount (cents, read-only), and an editable **category** dropdown
  (and optional per-leg tag). This is the only place the user spends time — assigning categories to a
  multi-category order; a single-category order needs no expansion, just accept.
- **Shipment groups (the hard case made legible).** When an order was recovered across N charges, the
  N review rows are shown as a linked group ("Order of 2026-03-01, 2 shipments"), so the user sees the
  charges belong together and can accept the group in one action. Accepting the group applies each
  charge's own item legs.
- **Labels for the gaps.** Rows the matcher could not itemize are flagged with the reason —
  `no invoice (outside scrape coverage)`, `ambiguous split group`, `already split` — and the panel
  header shows the scrape's date span and order count, so the user checks the scrape before the
  matcher when the rate looks low.
- **Accept / undo.** Accepting routes through `import_review.save_new` (NEW rows) or `accept_match`
  (MATCHING rows), which then call `ledger.set_splits` and record the `amazon_matches` link. Undo is
  the existing review undo plus deleting the link; because `set_splits` is reversible (re-set to a
  single category, or the review revert path), no special teardown is needed beyond that.
- **Defaults honor the "matching is expensive" finding.** The headline value is that the charge is
  found and named; splitting is offered, not forced. A user who only wants the memo accepts without
  expanding, and the collapsed item summary lands as the row memo.

---

## 6. Open questions for the user

- **Q1 — Default leg category.** Leave item legs uncategorized for the user to fill (safest), or
  pre-fill each leg with the charge's single predicted category and let the user re-split only
  multi-category orders? The recorded workflow says most orders are one category and splitting is the
  exception, which argues for: memo + one category by default, expand-to-split on demand.

- **Q2 — Split granularity.** One leg per *item* (fine-grained, more categorizing) vs. one leg per
  *shipment* (coarser) vs. memo-only-by-default with per-item split as an opt-in. Which matches how
  you actually reconcile?

- **Q3 — Gift-card / rewards offset on multi-shipment orders.** The offset amount is known from the
  invoice accounting block, but not which shipment consumed it. Options: (a) leave the offset manual
  for multi-shipment orders; (b) attach the whole offset to one charge you nominate; (c) split the
  offset proportionally across shipments (a guess). Which do you want?

- **Q4 — Scrape's payment-method gap.** Earlier notes flagged that the scrape "does not capture
  payment method," blocking gift-card splits. But the newer `priceAccounting` block appears to carry
  `Gift Card Amount:` and `Rewards Points:` per order, which is enough to build the offset legs in §4
  for single-shipment orders. **Please confirm the current scrape reliably emits those lines** — if
  so, no scrape re-record is needed for the common case. (Per-shipment attribution, Q3, would still
  need more than the scrape gives.)

- **Q5 — `[Amazon Gift Card]` / `[Amazon Rewards]` accounts.** The offset legs assume these exist as
  real accounts (the intermediary-account model). Should the feature auto-create them on first use, or
  require the user to create them and skip offsets until they do?

- **Q6 — Persist invoices, or stay file-driven?** §2 recommends a small three-table cache for
  idempotency and an "unconsumed orders" report; the alternative is zero schema and re-loading the
  JSON each run. Which trade-off do you prefer?

- **Q7 — Refunds.** Keep refunds memo-only (current, conservative) or attempt per-item refund splits
  later? The blended-tax problem makes refund splitting unreliable; recommend leaving as-is.

- **Q8 — Coverage before accept.** Should the pass refuse to run (or warn hard) when the scrape's date
  span does not cover the pending charges' dates, to avoid a low match rate being mistaken for the
  matcher's fault?
