# Mammon - Software Requirements Document (SRD)

Status: DRAFT for user review. Maintained alongside the code; the plan of record
tracks execution, this document tracks WHAT we are building and WHY.
Last updated: 2026-08-06.

---

## 1. Purpose and vision
Mammon is a Python desktop personal-finance application that reproduces the look
and feel of Quicken 2017's account ledger, while storing all data in its own
open SQLite database. It exists so the user is never again locked out of their
own financial data by a vendor's sunset policy. It must be able to absorb ~40
years of existing financial history and take over ongoing transaction download
from the user's institutions.

Non-goals (for now): bill pay, tax-form generation, mobile/web, multi-user.

## 2. Users and platform
- Single user, single machine (Windows). Python 3.12 for EVERYTHING (backend
  and GUI), PyQt5 for the desktop UI. (Language decision RESOLVED: Python.)
- Data lives in one SQLite file the user owns and can back up/copy freely.
- The project will be OPEN-SOURCED, so it needs a new name (see Section 11) and
  it takes a runtime dependency on webSlinger for downloads (see Section 7).

## 3. Guiding principles
- The ledger is sacred: balances must always be correct and reconcilable.
- Own your data: open schema, no proprietary lock-in, easy backup/export.
- Match Quicken's ergonomics where they are good; improve where they are not
  (e.g. better auto-categorization, learned payee renaming).
- Import must be lossless enough to trust for a 40-year archive.

## 4. Data model (canonical schema)
One SQLite database. Core tables:

- accounts(id, name, type[checking|savings|credit|cash|investment|asset|liability],
  currency, opening_balance, opening_date, institution, note, closed_flag) -
  `currency` is the account's native ISO 4217 code (TEXT NOT NULL DEFAULT 'USD',
  the base), chosen at creation and treated as immutable thereafter (see 5.4a).
- categories(id, name, parent_id, type[income|expense], hidden) - hierarchical,
  Quicken-style "Parent:Child".
- payees(id, name, normalized_name) - for matching/auto-fill.
- transactions(id, account_id, date, num, payee, category_id, memo, tag,
  amount, cleared, reconciled, transfer_account_id,
  transfer_pair_id, import_id, fitid, created_at) - `tag` is a normalized,
  comma-joined CACHE of the row's first-class tags (see `tags` below), not an
  independent field.
- splits(id, transaction_id, category_id, amount, memo, tag_id) - a transaction
  split across multiple categories. `tag_id` is the LEG's own tag (v54): Quicken
  tags a split leg to attribute part of one payment to a project, and folding
  those onto the parent would credit the whole payment to every tag in the
  split. A leg carries at most one tag, which is all QIF can express.
- tags(id, name[UNIQUE, COLLATE NOCASE], color, description) /
  transaction_tags(transaction_id,
  tag_id, PRIMARY KEY(transaction_id, tag_id)) - first-class,
  MANY-tags-per-transaction (schema v45). The junction is the AUTHORITATIVE store;
  `transactions.tag` is kept as a normalized comma-joined cache of a row's tag
  names so the register cell, the global Find and the report line loader keep
  reading one plain column. `mammon/ledger` is the SOLE writer of both, in
  lockstep (`set_tags` / `set_tags_text` rebuild the junction, then recompute the
  cache from it). Tags are PER-LEG: a transfer's mirror is never tagged from its
  other side, so net worth never double-counts. `ON DELETE CASCADE` retires a
  deleted transaction's (and both transfer legs') tag links. `name` collates
  NOCASE, so "Home" and "home" are one tag. The legacy single free-text `tag`
  (schema v2) is migrated forward, one tag per row. App-entered/derived, never
  imported.
- balance_checkpoints(account_id, year, balance) - fast running balances.
- import_mappings(id, payee_pattern, mapped_payee, mapped_category_id, source,
  hit_count) - learned payee -> category for auto-categorization.
- category_rules(id, keyword, category_id, match_count, account_id,
  amount_min_cents, amount_max_cents, memo_contains, created_at, updated_at) /
  transfer_rules(id, keyword, transfer_account_id, match_count, account_id,
  amount_min_cents, amount_max_cents, memo_contains, ...) - learned
  keyword -> category and keyword -> account rules, keyed on a distinguishing
  UPPER-CASED token pulled from raw statement text. The last four columns are
  OPTIONAL, NULLABLE conditions (roadmap item 10, schema v40) that narrow WHEN a
  rule fires: an account scope, an inclusive SIGNED-cent amount range
  (amount_min_cents .. amount_max_cents), and a case-insensitive memo substring.
  App-learned from the user's corrections, never imported.
- imports(id, provider, source_format, file_hash, filename, imported_at, status,
  counts) - audit of each import run + dedup at file level.
- transaction_matches(id, imported_txn_id, existing_txn_id, score, approved) -
  fuzzy dedup review on import.
- holdings(id, account_id, symbol, name, quantity, cost_basis) - investment
  positions.
- price_history(id, symbol, date, close_price, source) - per-holding quotes.
- asset_values(id, account_id, date, value_cents, source, note,
  UNIQUE(account_id, date)) - the dated MARKET-VALUE series for a non-investment
  `asset` account (schema v42), shaped like `price_history` is for securities and
  kept separate from the account's ledger balance (its cost basis). `value_cents`
  is signed integer cents; `date` is ISO YYYY-MM-DD; `source` distinguishes a
  hand-entered `manual` value from a fetched `zillow` one. Net worth reads the
  LATEST value on or before the as-of date through `investments.display_balance`.
  Written only by `mammon/asset_values.py`; the zEstimate wiring is 5.8e.
- investment_transactions(id, account_id, date, action[Buy|Sell|Div|ReinvDiv|
  IntInc|...], symbol, quantity, price, amount, commission, memo) - lot-level
  investment activity (kept distinct from cash transactions).
- crypto_transactions(id, account_id, date, action[BUY|SELL|SWAP_OUT|SWAP_IN|
  TRANSFER_OUT|TRANSFER_IN|SEND|RECEIVE|REWARD|INTEREST|AIRDROP|MINING|FEE|FORK],
  symbol, quantity, price, amount, basis, fee_symbol, fee_quantity, fee_amount,
  transfer_account_id, transfer_pair_id, swap_group_id, tx_hash, memo, import_id,
  fitid, created_at) - the event log for a cryptocurrency wallet-account (account
  type 'crypto'), one row per single-asset delta (schema v51). Kept distinct from
  both cash `transactions` and equity `investment_transactions`: quantities and
  per-unit prices are text-encoded Decimal at wei scale (18 decimals), while the
  fiat that moves is signed integer cents. A wallet-to-wallet move uses the same
  transfer-mirror model as cash (two legs linked by `transfer_pair_id`), a
  coin-for-coin swap links its two legs by `swap_group_id`, and `tx_hash` is the
  on-chain exact-dedup key (fitid's role for chain activity). MAMMON domain layer
  in mammon/crypto.py, which is the SOLE writer of the crypto_* tables (mirroring
  investments.py's single-writer discipline); ledger.py stays the only writer of
  the cash `transactions` table.
- crypto_holdings(id, account_id, symbol, name, quantity, cost_basis,
  UNIQUE(account_id, symbol)) - current coin position per (account, symbol), a
  replay cache like `holdings` (schema v52).
- crypto_holdings_checkpoints(account_id, year, symbol, quantity, cost_basis,
  income, realized, ever_held, lots, PRIMARY KEY(account_id, year, symbol)) - the
  per-(account, year, symbol) replay snapshot, the crypto twin of
  `holdings_checkpoints` for fast open/scroll (schema v52). `income` is
  staking/airdrop/mining income to date; `lots` is the JSON lot list.
- The 'crypto' account type is a DISTINCT `accounts.type` value but is classified
  INVESTMENT-LIKE for net-worth valuation, sidebar grouping and the allocation
  pie via a single-source-of-truth `ledger.INVESTMENT_LIKE_TYPES` constant (which
  every such classification tests membership in, rather than comparing to the
  "investment" literal). The wallet address reuses the existing `account_number`
  column (already blanked from the MCP surface); `asset_class = 'crypto'` carries
  the allocation classification. Crypto quantity arithmetic runs under a raised
  (>= 40 significant-digit) decimal context, because the Python default 28-digit
  context can silently drop a wei when summing a large balance -- TEXT storage is
  exact at any decimal count, so only summation is at risk.
- reconciliations(id, account_id, statement_date, statement_balance, ...) - the
  record of COMPLETED reconciliations only.
- reconciled_change_log(id, account_id, transaction_id, changed_at, operation,
  field, old_value, new_value) - a per-account audit trail of every change made
  to an ALREADY-reconciled transaction (schema v63; see 5.11a). Append-only,
  written only by `mammon/ledger.py` (the sole transaction writer) and otherwise
  read-only. `transaction_id` carries no foreign key on purpose: after a delete
  the row it names is gone, and the log entry must outlive it.
- reconcile_drafts(account_id PK, statement_date, beginning/ending_cents,
  charges/payments/credits/finance_cents, finance_category, finance_txn_id) -
  the statement inputs of an UNFINISHED reconcile, one per account (schema v30).
  A reconcile spans several sittings: the user closes the window to look
  something up in the register and comes back, and the typed statement figures
  must survive that. Deleted when finish_reconciliation locks the statement in.
  finance_txn_id is load-bearing - it is how a reopened reconcile UPDATES the
  posted finance charge instead of posting a duplicate.
- loan_params / loan_rates / loan_extras - a liability account's amortization
  definition (principal, term, rate history, escrow/PMI extras). Drives the loan
  payment schedule and the principal/interest/escrow split.
- scheduled_payments(id, account_id, payee, amount, frequency[weekly|biweekly|
  monthly|quarterly|annual], next_date, category_id, memo, active) - generalized
  recurring-payment DEFINITIONS for subscriptions, fixed-price utilities,
  memberships, etc. (schema v16). MAMMON-GENERATED, never imported (see 5.10).
- budgets(id, name, active) / budget_lines(id, budget_id, category_id, period
  ['YYYY-MM'], amount_cents, rollover, UNIQUE(budget_id, category_id, period)) -
  named per-category monthly spending targets (roadmap item 6, schema v39). App-
  generated, never imported. Actuals are NOT stored: budgeted-vs-actual is
  derived read-only from transactions via reports/spending.py, so the ledger
  stays the sole writer. Domain layer in mammon/budgets.py; the Budgets panel
  (Tools ▸ Budgets…, mammon/ui/budget_widget.py) is a thin projection over it -
  choose/create a budget, edit per-category monthly targets, and view
  budgeted/actual/remaining for a month, with no SQL or money logic of its own.

Monetary amounts: stored as signed integer CENTS (avoids float rounding).
Share quantity and per-share price: stored as text-encoded Decimal to preserve
precision for fractional shares / multi-decimal prices. (Q5 RESOLVED: confirmed.)
Implemented in mammon/db.py (schema v1); smoke tests in mammon/tests/test_db.py.

## 5. Functional requirements

### 5.1 Account ledger (FIRST PRIORITY)
- Register view per account, classic Quicken columns:
  Date | Num | Payee | Category | Memo | Payment | Deposit | Balance | Clr
  (Two separate Payment/Deposit columns per user preference; a signed +
  color-coded single-amount mode may be offered later as an option.)
- Inline editing, a blank quick-entry row at the bottom, date picker, category
  and payee auto-complete, running balance recomputed on every change.
- Cleared/reconciled flags; right-click edit/delete/copy/paste/insert.
- Accounts overview with per-account balance and total net worth.

### 5.1a QuickFill (memorized payees)
- The Payee cell completes from the payees the register has actually used,
  most recently used first (`ledger.list_payees`, derived from `transactions`;
  the schema's `payees` table is written by nothing and is not consulted). The
  completer (`ui/delegates.PayeeCompleter`) matches at the start of a name
  first and INSIDE it second, because bank descriptors rarely begin with the
  word a person remembers. Tab accepts a completion that extends what was
  typed; a substring-only match is offered in the popup but never silently
  taken.
- Entering a known payee in the blank quick-entry row PRE-ENTERS that payee's
  last category, memo, tag and amount into the fields the user has not typed
  (`categorize.quickfill` over `ledger.last_transaction_for_payee`: this
  account's own most recent posted row is preferred, scheduled placeholders are
  skipped). The category comes from the last row's shape when it was a transfer
  (`[Account]`), else from the learned/user mapping, else from the last row; a
  split's category is left blank because its lines belong to the split dialog.
- **A pre-entered amount never records the row by itself.** The blank row
  commits when the USER has supplied a date and an amount; QuickFill's guesses
  live in a separate buffer (`RegisterModel._auto`) beneath what was typed, so
  a typed field always wins, a cleared field stays cleared, and a typed amount
  in either money column discards the guessed one so the two can never net.
  This auto-commit fires from inside the just-typed cell's `setModelData`, so
  it writes the transaction through `mammon.ledger` at once but DEFERS the model
  reset one event-loop turn (`add_from_values(defer_reload=True)`), the same as
  an inline edit and the Enter commit below. Resetting the model synchronously
  there tore it down under the editor Qt was still destroying: the app hung,
  then the new row vanished instead of committing (the §5.5a `setModelData`
  hazard). The quick-entry buffer is cleared the instant the row is written, so
  a racing Enter/`closeEditor` commit finds it empty and cannot double-insert.
- **Enter records a pre-entered row**, from inside any cell editor. The hook is
  the delegate's `closeEditor` hint (`SubmitModelCache` means Enter), not the
  key event, because an editable combo swallows Enter before the view sees it.
  The commit is deferred one event-loop turn (the `setModelData` hazard).
- **A NEW transaction can be SPLIT during entry**, not only assigned a single
  category -- the same gesture (Split menu/toolbar action, or double-click the
  Category cell) that splits an existing row now works on the blank quick-entry
  row. This matters most for a QuickFilled recurring payee whose amount is
  pre-entered (a paycheck, a mortgage bill): its amount waits in `_auto` and so
  never trips the single-category auto-commit above, yet it is exactly the row a
  user wants to break across categories. Mirroring how a pending review row is
  split, `RegisterWidget._split_row` commits the in-progress row through
  `mammon.ledger` FIRST (`commit_blank_returning_id` -> `ledger.add_transaction`,
  the sole writer), then opens the very same split dialog on the saved
  transaction -- Copy-from-previous-`<payee>` button and all -- whose lines are
  written by `ledger.set_splits`. An empty blank row (no date + amount) stays a
  no-op, and a transfer entered on the blank row commits but is not splittable.
- **The split dialog's live Remainder is exact integer cents.** A leg's amount is a
  spin box; backspacing it empty used to leave `value()` returning the LAST accepted
  number (Qt treats the empty string as an intermediate edit, not the value 0) and
  fire no change, so clearing a leg to drop it left the running Remainder — and the
  uncategorized slot it lands in on save — stale by that amount (often ~$4).
  `ui/delegates.SplitAmountSpinBox` reads a cleared box as 0 and the dialog
  recomputes on the line edit's text change, so the Remainder is right the instant a
  leg is cleared. All remainder math stays in signed integer cents (`ROUND_HALF_UP`
  at the cents boundary), never floats.
- The full-field New Transaction dialog completes and pre-enters the same way
  on leaving the Payee field; the Edit dialog completes but fills nothing.
- Deliberately NOT a separate memorized-payee table: the ledger is the memory,
  so it cannot drift from history and needs no maintenance. A per-payee
  lock/"do not memorize" list is the natural follow-up if a payee's amount
  varies enough that recalling it is noise.

### 5.1b Register sort, filter, columns, batch edits, void, replace
- **Sorting.** A header click sorts by that column (indicator shown; a second
  click flips it). The model keeps the ledger-ordered rows and shows the view a
  PROJECTION (`RegisterModel._view`) that every row-indexed method reads, so an
  edit made through a sorted row reaches the transaction on screen. Date
  ascending IS the ledger order; other columns break ties by it. **Same-date
  rows keep a stable total order, tiebroken by their insertion `id`**, so the
  register never reshuffles between opens or after an unrelated edit:
  `ledger.register_rows` queries `ORDER BY date, id` and every other column's
  sort key ends in `(date, id)`; `id` is a monotonic `INTEGER PRIMARY KEY`
  written only by `ledger`, and an in-place edit preserves it. **Balance
  keeps each row's date-ordered running value whatever the sort** (Quicken's
  behaviour): sorting by payee never recomputes a balance. Num sorts numbers
  numerically, then text, blanks last; the money columns sort by magnitude;
  Clr ranks blank < c < R. An open editor is committed before the reset.
- **Filter bar** (gear ▸ Filter Register, Ctrl+Shift+F): text over payee /
  memo / num / tag / category label / formatted amount, a date range, an
  absolute amount range, and the cleared state (`cleared` is the `c` state
  alone). Shows "Showing n of m". The blank quick-entry row stays, so entry
  works while filtered; a new row that does not match is recorded but not
  shown. **Hiding the bar clears the filter** so a closed bar never leaves the
  register silently narrowed. Not persisted.
- **Column chooser** (right-click the header): Num, Tag, Memo, Clr and Balance
  may be hidden; Date, Payee, Category and the money columns may not. Stored
  globally by column NAME (`ui/prefs.hidden_columns`) and layered UNDER what the
  layout hides -- two-line mode's collapsed Category/Memo/Tag and a loan
  register's dropped Num/Tag -- in one place, `_apply_column_visibility`.
- **Batch edits** on a multi-row selection (Shift/Ctrl-click; context menu):
  change category / payee / memo / tag, mark cleared or uncleared, void,
  delete. Transaction ids are resolved BEFORE the first write (rows shift on
  reload); the batch skips what it cannot change -- a split's or transfer's
  category (an `[Account]` target converts a plain row and re-points a plain
  transfer), a reconciled row's cleared flag -- and reports the skips. One
  reload per batch, no per-row save sound. A batch category teaches the payee
  mapping like a single edit.
- **Void** (`ledger.void_transaction`): amount to zero (both legs of a
  transfer, via the mirror sync), `**VOID**` prefixed to the payee, the
  original amount noted in the memo, split lines dropped first. Cleared /
  reconciled flags are left alone -- a voided check that cleared stays
  cleared. Idempotent.
- **Find and replace** (Find dialog): set payee, memo, tag, num or category on
  the highlighted result or on every result, replacing the WHOLE field
  (Quicken's semantics; blank clears). Category skips transfers and splits and
  teaches the payee mapping. Confirms first; open registers reload after.
- **The investment register shares this whole toolkit** (parity roadmap item 2 /
  upgrade_priorities #5), reusing the cash register's pattern and differing only
  where the content does. `InvestmentRegisterModel` gains the same
  `_view`-projection indirection, so a header click sorts (Quantity / Price /
  Share Bal by Decimal magnitude, Inv Amt / Cash Amt / Cash Bal by cents, Action
  and Security / Category by text, Date the identity); a filter bar
  (`InvestmentFilter`) narrows by text over action / security / memo / amount, a
  date range and an amount range, reporting "Showing n of m"; a header-menu
  column chooser hides Price / Share Bal / Inv Amt / Cash Amt, persisted through
  `ui/prefs.hidden_columns` under its OWN scope (`investment_register`) so it
  never collides with the cash names; a multi-row selection batch-changes the
  memo, voids or deletes; and a memo/security find-and-replace rewrites matching
  rows. **Void** has no payee to stamp and no splits to drop, so
  `investments.void_investment` zeros amount / commission / quantity / price
  (taking the row out of BOTH the money and the share math), prefixes `**VOID**`
  to the memo with the original amount noted, and the register renders the mark
  on the Action cell; idempotent, like the cash void. **Every write still goes
  through `mammon.investments`** (the sole `investment_transactions` writer) --
  batch edits via `update_investment_fields`, so no second write path opens and
  a cash-only transfer leg shown here is skipped rather than mis-resolved against
  the shared id space. Only the CONTENT differs (shares/price columns, an action
  verb, no cash-only payee/category cells); the behaviour matches.
- **Tags** (the register's Tag column): a transaction carries any number of tags,
  entered and edited as ONE comma-separated string in the single Tag cell -- a
  free-text slot, NOT a multi-widget picker (explicit UX choice). Commas separate;
  a tag may contain spaces ("Ski Trip" is one tag); leading/trailing whitespace is
  trimmed, empties dropped, case-insensitive duplicates collapsed. Qt only hands
  the raw string across: the parse (string -> tag names) and join (tag names ->
  the displayed string) live in `mammon/ledger` (`parse_tags` / `format_tags`),
  so the register stays a thin projection over the domain. Filter/report by tag:
  `ledger.transactions_with_tag(tag[, account_id])` is the exact per-tag filter
  (the substring Find also hits payees/memos), and Reports ▸ By Tag (Window)
  (`reports/tags.py`) totals spending per tag, attributing a line to EACH of its
  tags so overlapping tags can each show the full amount.
- **Tag colors** (identity, not rank): each tag carries an optional display color
  (`tags.color`, a `#RRGGBB` hex string, NULL = "not chosen"). The color is per-tag
  IDENTITY, so a tag keeps the SAME color everywhere -- the register Tag cell paints
  a small colored square before each tag name (in both one- and two-line view), the
  split dialog tints a tagged leg's row with its tag color, and the By Tag report
  colors each row's tag -- instead of color following a chart slice's rank and
  changing as the ranking moves. A split's per-leg tags surface on the collapsed
  register row as the UNION of the row's own tags and its legs' tags (the same
  `reports/_lines._line_tags` union), so leg colors show without double-counting.
  Every surface reads color through ONE domain accessor, `ledger.tag_colors(conn)`
  (casefolded name -> hex), keeping `ui/` free of SQL. Colors are written only
  through `ledger.set_tag_color` (validated `#RRGGBB`), which -- with `rename_tag`
  and `delete_tag` -- keeps `mammon/ledger` the sole writer of the tags table and
  its `transactions.tag` cache. A split leg's own single tag round-trips through
  `ledger.set_splits` (get-or-created by name), so editing a split no longer drops a
  per-leg tag an import set.
- **Tag Manager** (Tools ▸ Tag Manager…, mirroring the Category Manager): lists
  every tag with its color swatch and usage count, and renames a tag (in place,
  keeping its id so links and color survive), sets or clears its color via a color
  picker, or deletes it (removing it from every transaction and split leg). A thin
  `QDialog` over the ledger verbs above; its destructive delete confirms through the
  `QMessageBox.question` seam, and it emits `changed` so open registers and the By
  Tag report refresh.

### 5.1c Register Undo/Redo
- The register has multi-level Undo and Redo for edits the user makes there:
  adding a transaction, editing a transaction's fields, deleting a transaction,
  editing splits, and creating / editing / deleting a transfer. The Edit menu
  carries **Undo** (Ctrl+Z) and **Redo** (Ctrl+Y and Ctrl+Shift+Z); both items
  enable/disable from the stack and show what they would reverse (e.g. "Undo Add
  transaction", "Redo Delete transfer").
- **No second write path.** Undo/redo does not touch SQL. It captures a snapshot
  of the affected transaction(s) and REPLAYS the inverse THROUGH the same
  `mammon.ledger` verbs the forward edit used (`add_transaction`,
  `update_transaction`, `delete_transaction`, `create_transfer`, `set_splits`,
  `clear_splits`). So every transfer/split/checkpoint invariant stays enforced in
  the one place it already lives, and undoing a transfer inverts BOTH mirror legs
  together for free (delete removes both sides; `update_transaction` mirrors
  date/amount/payee/memo to the pair) — see 5.2.
- The stack is **in-memory and session-scoped**, one history per open register
  (`mammon/undo.py`, hung off the register's `RegisterModel`). Nothing is
  persisted; there is no schema change. Closing the register or the app clears it.
- **Identity churn.** The ledger allocates a fresh row id on every insert, so
  undoing a create (delete) then redoing (recreate) yields a different row id.
  The manager keeps a small remap table so a later stack entry that referenced
  the old id still resolves to whatever row currently stands in for it.
- **Deliberate limitations.** Converting a plain transaction into a transfer, or
  re-pointing a transfer at a different account, have no clean ledger inverse;
  they are treated as a barrier that drops the redo stack rather than recording a
  step that could not be cleanly reversed. Batch (multi-row) edits are likewise a
  barrier in this version — single-row add / edit / delete / transfer / split are
  the undoable acts. A new edit always clears the redo stack.

### 5.2 Transfers between accounts (FIRST PRIORITY)
- Classic Quicken behavior (user confirmed Q1): a transfer is one transaction
  whose Category is "[Other Account]". Entering it AUTO-CREATES a linked mirror
  transaction in the other account with the opposite sign.
- The two sides are linked (transfer_pair_id). Editing amount/date/clearing on
  one side updates the other; deleting one deletes both. No double-counting in
  net worth.

### 5.3 Categories and memos
- Every transaction has a Category and a Memo.
- Categories are hierarchical (Parent:Child), like Quicken.

### 5.3a Category Manager (Tools ▸ Category Manager…)
- A dialog (`mammon/ui/categories_dialog.py`, `CategoriesDialog`) showing the
  full category tree with each node's income/expense type and how many rows use
  it, and offering **Add / Rename / Reparent / Merge / Delete**. It is a THIN
  projection over the category domain in `mammon/ledger.py` — every write goes
  through a domain function (`create_category`, `rename_category`,
  `reparent_category`, `merge_category`, `delete_category`), so the dialog holds
  no SQL and no invariant logic, mirroring the Budgets and Rules Manager
  dialogs. It emits `changed`, so open registers refresh when a label moves.
- **Rename / reparent keep the category id.** Renaming or reparenting updates the
  row in place, so every learned rule, budget line, import mapping and scheduled
  payment that points at the category still does after the change — the
  delete-and-recreate route that would forfeit the id is deliberately not taken.
  Both refuse a move that would violate the `UNIQUE(name, parent_id)` invariant
  (a same-named sibling under one parent) and reparent additionally refuses a
  cycle (a category cannot become its own parent or a child of a descendant).
- **Merge re-points, it never drops.** Merging `A` into `B` moves every reference
  onto `B` — transactions and split lines (so no posting is orphaned onto NULL),
  learned `category_rules` and payee `import_mappings`, `scheduled_payments`, and
  `budget_lines` (colliding `(budget, period)` targets are summed onto the
  survivor) — then removes `A`. `A`'s children move under `B`, a same-named child
  merged recursively. This is why merge is a domain operation and not a UI-level
  delete: a plain delete would let the `ON DELETE CASCADE` on `budget_lines` and
  `category_rules` silently destroy those rows. Category ids never index into
  balance history, so no balance checkpoint is affected by any of these verbs.
- **Delete never orphans an in-use category.** Deleting a category still used by
  transactions demands a replacement (an existing category or a new
  `Parent:Child` name) and reassigns every posting to it first; only an unused
  category is removed after a plain confirmation.

### 5.4 Intermediary payees - Venmo, PayPal (RESOLVED: no dedicated field)
- Money moving through an intermediary needs the REAL counterparty recorded, not
  just "Venmo". A dedicated field on every transaction was specified for this and
  rejected in practice: give the intermediary its own ACCOUNT instead.
- Money between it and a bank account is then an ordinary transfer, and the real
  payee and memo live on the intermediary account's own register rows. This needs
  no extra field, scales past a handful of payments a month, and reuses the
  transfer machinery that is already tested. See migration 28 in `mammon/db.py`.

### 5.4a Per-account currency (multi-currency)
Every account has a native currency (`accounts.currency`, ISO 4217, `NOT NULL
DEFAULT 'USD'`). The base/presentation currency is USD (`fx.BASE_CURRENCY`); an
account with no explicit currency IS the base. The backend is `mammon/fx.py`: a
dated `fx_rates` store (Decimal-encoded TEXT rates, direct or derived inverse),
`convert_cents` (integer cents, `ROUND_HALF_UP`), `net_worth_by_currency`, and a
`total_in_currency` fold. Money stays integer cents; rates stay Decimal text; no
floats and no money math in the UI layer.

- **Currency is chosen at CREATION and is immutable.** The New Account dialog
  offers a currency selector (an editable ISO-4217 combo, defaulting to the base
  so the all-USD case needs no thought). Creation funnels through the sole account
  writer `ledger.create_account`, which takes and normalises a `currency`
  argument — there is no second write path. Changing an account's currency after
  it holds transactions would silently reinterpret every past amount, so the
  Account Details dialog shows the currency **read-only** and never writes it back.
- **Per-account amounts render in the account's own currency.** A base-currency
  account renders bare (the dense classic look — no symbol clutter); a foreign
  account is tagged with its currency symbol/code (`ui/models.fmt_amount_ccy`,
  `currency_symbol`) so its balance is never misread as base dollars. This applies
  to the account bar / accounts overview per-account balances and to the register
  header (a foreign register names its currency once, in the title, and shows its
  ending balance in that currency). Formatting is presentation only — the cents
  are unconverted.
- **Net worth folds to the base currency through `mammon.fx`.** The accounts
  overview / account bar net-worth figure is `fx.total_in_currency(conn,
  BASE_CURRENCY)`; when a needed rate is missing it falls back to the naive
  base-currency sum rather than breaking — identical to the figure shown before
  any foreign account existed, and byte-identical for an all-USD ledger. All the
  cents arithmetic and rate lookup live in `mammon/fx.py`, not the UI.
- The app holds no FX credentials; rate fetching is behind the same injectable
  seam as investment quotes (`fx.fetch_rates`, default yfinance source).
- **Rates are entered and refreshed from a file-wide "Exchange Rates" manager**
  (`ui/fx_rates_dialog.py`, Tools menu). It lists the dated `fx_rates` store
  (`fx.list_rates`, a pure read — the UI keeps no SQL of its own), lets the user
  ENTER a rate for a (from-currency, to-currency, date) — "how many *to* units
  equal 1 *from* unit on this date" — and REFRESH the rates every account's
  currency implies against the base, plus every pair already recorded. Dates use
  the standard `ui/delegates.make_date_edit` / `date_edit_iso` chokepoints and
  render through `ui/models.fmt_date`. Every write funnels through
  `fx.set_rate` — the store's single writer, an upsert on (date, from, to) —
  including a refresh (`fx.fetch_rates` calls `set_rate` for each fetched rate),
  so the UI adds **no second write path** to `fx_rates`. There is no
  delete-rate writer by design, so editing an existing rate corrects its value
  in place with the (from/to/date) key locked. The network fetch is behind the
  `_fetch_rates` seam and the result notice behind an overridable `_notify`, so
  a headless test injects a fake source and opens no blocking modal. Entering or
  refreshing a rate immediately revalues the open registers' foreign-currency
  accounts (the dialog's `changed` signal drives a refresh, and net worth folds
  through the newest stored rate on/before the date).

### 5.5 Auto-categorization from history
- When a payee recurs, Mammon learns its category and auto-fills/suggests the
  category on new and imported transactions once a pattern is established
  (e.g. a given grocery store -> Groceries).
- User can always override; overrides update what is learned.
- **The category is resolved BELOW the payee.** Payee renaming settles first
  (`rename_tree`), then `category_tree` walks a discrimination trie rooted at
  that payee over the row's source text, ranked by category entropy. So one
  payee can hold several categories and still be answered exactly:
  `COSTCO GAS ...` -> Auto:Fuel, `COSTCO WHSE ...` -> Groceries, on the single
  token that separates them.
- **A candidate category is never one the payee has not already carried.** This
  is the locked requirement, and it is structural rather than a threshold: the
  vote counts `suggest` reads are themselves keyed by payee. A first-ever payee
  therefore proposes nothing at all -- during the learning period a blank
  category is the correct answer, and an unrelated guess is worse than silence.
- **When not confident, blank the category and RANK the picker.** The register's
  category dropdown promotes the categories this payee has carried (the ones
  matching this description first, then the rest by frequency), with the full
  alphabetical list underneath so any category stays reachable. Measured on the
  real ledger's first year, the promoted first entry was the right answer 67% of
  the time on rows too uncertain to fill in.
- Two gates decide "confident": node purity (does this description
  discriminate?) and payee coherence (is this merchant categorizable at all?).
  Coherence applies only at the trie root, where nothing distinguished the row
  and the answer is the payee's bare prior -- that is the case where a catalogue
  payee like Amazon would otherwise stamp its most common category onto every
  unrelated purchase. Below the root a matched token has earned its answer and
  is not overruled by the payee's overall mix.
- Investment rows are excluded end to end: `investment_transactions` has no
  category column, so there is nothing to predict and nothing the user could
  correct to train on.
- **Keyword category rules are hand-written only.** The old `keyword ->
  category` learner minted a global rule from a single correction and is
  removed; migration 57 deleted every row it had produced. What remains in
  `category_rules` is what the user typed into the Rules manager, so it is
  honoured directly when the payee tree declines -- an explicit instruction,
  not the system guessing.
- **Two votes fill; QuickFill covers one.** `category_tree.MIN_COUNT` is 2,
  the same floor as a rename (the 4 it inherited was compensating for the old
  trie's resetting counts). Below that, once the payee is SETTLED -- filled by
  the rename tree or supplied by the source -- `predict_fields` asks the
  register's own QuickFill (`categorize.quickfill`, this account's row
  preferred) what it would pre-enter for that payee typed by hand, so a payee
  the ledger has categorized for years fills on its first download. Typing the
  payee already did this; an auto-filled payee must not do worse. Withheld only
  when review history has shown the payee to be a catalogue (coherence below
  `PAYEE_COHERENCE` on at least `MIN_COUNT` votes). The accepted category is
  recorded as a vote whichever source supplied it. Measured over the older
  ledger's 599 categorized accepts, replayed cold: 294 fills, 249 right; the
  floor at 2 versus 4 adds 10 right fills and no wrong ones, and the errors
  that remain sit on well-corroborated nodes where the user's own
  categorization changed over the years.

### 5.6 Import (see Section 6 for the strategy)
- One-time migration of the full ~40-year Quicken 2017 history into Mammon's DB.
- Ongoing import of downloaded/scraped data (QFX/OFX, JSON, CSV).
- Every importer maps to a common normalized record, then dedups (fitid +
  fuzzy match) before insert; transfers are recognized so both sides are not
  double-imported.

### 5.7 Download from institutions (see Section 7)
- Pull transactions from the user's banks, cards, and investment institutions,
  preferring a direct download format (QFX/OFX/CSV) where the site offers one,
  and webSlinger scraping -> JSON only where it does not.

### 5.8 Investment accounts
- Maintain holdings and a per-holding price history.
- An investment account's displayed balance is its full market valuation, cash
  PLUS securities (`investments.display_balance`). The Holdings window therefore
  ends Currently Held with a **Cash** line and footers Securities / Cash /
  Total, so the window reconciles to the very number in the accounts list the
  user clicked to open it.
- Retrieve updated quotes automatically via a Python package where one exists
  (e.g. yfinance for public tickers), falling back to webSlinger only where no
  package covers a source.
- The investment register offers the same register toolkit as the cash register
  -- sort, filter, column chooser, multi-row batch edit, find-and-replace and
  Void -- over the investment columns; see §5.1b for the shared behaviour and the
  investment-specific Void.

### 5.8b Stock splits
- A split's ratio is stored EXACTLY, as an integer `split_num`/`split_den` pair
  on `investment_transactions` (migration 34), and applied as
  `qty * num / den` — multiplying **before** dividing, so 300 shares at 4:3 give
  exactly 400 rather than `300 * 1.3333...` = 399.99999999999999.
- It used to be stored only as `quantity` = new shares per TEN old (Quicken's
  encoding: a 2-for-1 is 20). That form has no exact value for a 4-for-3 or a
  1-for-3, so those splits were unrecordable — the storage encoding was
  dictating which corporate actions the user is allowed to own. `quantity` is
  now a derived legacy column; every reader falls back to it when the pair is
  NULL (`investments.split_factor`), so pre-migration rows replay unchanged and
  no backfill was needed.
- OFX states a split as NUMERATOR/DENOMINATOR (or NEWUNITS/OLDUNITS), which
  `importers/ofx._split_pair` carries through whole rather than pre-dividing.
- The user never sees the encoding. All three boundaries speak announced ratios,
  and each was wrong in its own way before:
  - The dialog's **Split ratio** field accepts `8:1`, `4:3`, `1:2`, `8-for-1`,
    `3 for 2`, `1/2` or a bare `8`/`1.5` (`investments.parse_split_ratio`), and
    shows the stored ratio back in the same notation. It previously took a bare
    number ONLY — so `8:1`, the exact string the register displays, was rejected
    — and before that it stored the typed number raw, turning an 8-for-1 into a
    0.8x reverse (26 shares → 20.8).
  - The register's **Quantity** cell shows `8:1` (`investments.split_display`),
    not `80`.
  - The register's **Share Bal** applies the split. A StkSplit is neither an ADD
    nor a REMOVE action, so `register_rows` skipped it: the split row showed a
    blank balance and every LATER row for that security carried a pre-split
    running total, disagreeing with `holdings` (which was always right, since it
    replays through `_apply_txn`). Share Bal ties to holdings by documented
    invariant; the split case has to be in both.

### 5.8d Lots, capital gains, performance and allocation (roadmap item 7)
- **Shares are kept as tax lots**, one per acquiring transaction, inside the
  same replay that has always produced holdings (`investments._apply_txn`), so
  the Holdings window, the security-filtered register and the new windows
  read one state. A disposal takes shares out of the lots by the account's
  **cost-basis method** (`accounts.lot_method`, set in Account Details):
  `average` -- every share carries the position's average per share, lots
  give up shares oldest first, which is how the holding period is fixed under
  average cost -- or `fifo` / `lifo`, each lot's own cost. Average is the
  default because it is what every figure was computed under before lots were
  kept; a brokerage reports stock sales FIFO unless lots were specified, so a
  stock account should be switched. Changing the method replays the account:
  every open position's basis, every realized gain and every snapshot follow.
- **Specify Lots** on a sale's row (`lot_assignments`, `investments.assign_lots`)
  names the lots that sale disposes of; named shares are relieved first, the
  rest by the method. Validated: purchase lots of the same account and
  security, dated on or before the sale, not more shares than were sold.
- **Every sale books one realized gain per lot it drew on**
  (`investments.RealizedGain`: acquired, sold, shares, proceeds net of
  commission, basis, term -- long when held more than a year, short
  otherwise, unknown when the acquisition date is not known). Capital Gains
  (`portfolio.capital_gains`, `gains_summary`) lists them for a year or any
  range with the Schedule D footings by term; Lots (`portfolio.open_lots`)
  lists the open lots valued at a date with each one's term and days held.
- **Year-end snapshots carry the lots** (`holdings_checkpoints.lots`, JSON), so
  the snapshot+delta replay reproduces the from-inception lot state exactly
  (`test_lots.py` proves it for all three methods). Migration 37 drops the
  earlier snapshots, which held no lots; a read without snapshots is a
  from-inception replay, so nothing is wrong meanwhile, and the next rebuild
  writes them back. Gains already booked are not snapshotted -- Capital Gains
  replays from inception, which is cheap at this scale.
- **Performance is money-weighted** (`portfolio.xirr`, `account_performance`,
  `security_performance`): the annualized rate at which the starting value,
  every dated flow across the boundary and the ending value net to zero. For
  the account the boundary is the account: cash transferred in or out (a
  ledger transfer leg, unless an `XIn`/`XOut` row already records it -- the
  valuation's multiset rule, so nothing counts twice) and securities moved in
  or out; buys, sells and dividends only move value around inside. For one
  security the boundary is the security: buys and shares in are money put in,
  sales, cash dividends and shares out are money back. Bisection on the net
  present value, so any data the user has yields a rate or an honest "n/a".
- **Hidden accounts are out of the allocation**, matching
  `investments.net_worth`, which has always excluded them. Hiding is how a user
  says "the records here are incomplete -- leave it out of my totals", and such
  an account values as pure CASH, so including it did not merely inflate the
  total: it invented a cash slice and skewed every other class's percentage.
  `include_hidden=True` opts into the fuller picture, as the report bar does for
  net worth. Naming `account_ids` explicitly overrides both.
- **An investment account with a balance but NO holdings is reported**
  (`Allocation.cash_only_accounts`). Money went in as a transfer and the shares
  it bought were never entered, so the ledger can only call the balance cash --
  and that slice is indistinguishable from a genuine cash position. On the real
  ledger five 529 plans and a 401k put ~$200,000 into "Cash", and the reasonable
  conclusion was that the scope was wrongly pulling in bank accounts. Both the
  Allocation window and Target & Drift now name the accounts and say the Cash
  figure is a records gap, not a decision.
- **Allocation** (`portfolio.allocation`) groups every priced holding's market
  value by the asset class recorded for its security (`securities` table:
  domestic stock, international stock, bonds, cash, real estate, other;
  `unclassified` until the user says, never guessed), by security and by
  account, each investment account's cash counted as cash. The Allocation
  window (Reports ▸ Asset Allocation…, and the investment gear) sets the class
  per security in place, and draws a pie of whichever grouping the open tab
  shows -- class, security or account.
- **The pie groups its own tail** (`ui/charts.group_small_slices`). The `Other`
  wedge is the SET OF LOWEST-share categories that together make up **10%** of
  the total: accumulate categories from the smallest upward until their combined
  share reaches 10% (the category that tips the sum past the bar is included),
  fold those into `Other`, and draw the rest individually. Rolling up *by
  combined share* rather than a per-slice cutoff keeps `Other` a real, clickable
  fraction of the pie -- never a sliver, never most of it. Any wedge still under
  **5%** of the drawn pie loses its INLINE label: a dozen tiny slices otherwise
  stack their labels on one arc and the picture stops carrying information. No
  wedge is anonymous, though -- an un-labelled sliver gets a **hover tooltip**
  (`SlicesPieCanvas._on_motion`) naming the category, its share of the whole and
  its dollar amount, and the table beside the chart still names it too. **Every
  wedge's percentage is its share
  of the whole period total, at any drill depth** -- a category that is 3% of
  everything reads "3%" even when it is 40% of the `Other` it was drilled into
  (`SlicesPieCanvas.whole_total` is the one denominator). Clicking `Other`
  redraws the pie as its members alone (full size, percentages still of the
  whole) and the window's Back button -- or a click off the pie -- returns; a
  drill-down without a way back is a trap, so the two ship together. Two guards
  keep the rule honest: a single small slice is never renamed `Other`, and when
  the roll-up would swallow every category the pie is drawn ungrouped rather than
  collapsing into one wedge. A class genuinely called Other joins the group
  instead of being drawn twice under the same name. **All three category pies
  reuse this one helper** -- Asset Allocation (`portfolio.allocation` groupings),
  the Income Chart (`reports.income_category_rows`) and the Spending Chart
  (`reports.spending_category_rows`) each hand `SlicesPieCanvas` their raw,
  ungrouped top-level rows (money IN for income, money OUT for spending, of the
  matching category type, transfers excluded, splits honored), so the
  threshold/rollup/drill-down/percentage logic lives in exactly one place rather
  than being re-implemented per chart.
- **Distinct wedge colours, with `Other` pinned** (`ui/charts.wedge_colors`,
  `_PIE_PALETTE`). The palette carries enough visually separated hues for the
  worst realistic pie -- ~19 divisions, which happens when every real category is
  roughly 5% of the period and the sub-10% tail rolls up into an `Other` that is
  itself >=10%. The `Other` wedge always takes the palette's FINAL entry (a
  neutral gray), so it stays a stable, recognizable colour no matter how many
  real categories precede it. The old ten-colour list wrapped with `i % len`, so
  an 11th category reused -- and became indistinguishable from -- the `Other`
  wedge's colour.
- **Net Worth Over Time reads against both axes** (`ui/charts.NetWorthCanvas`).
  The cumulative curve draws BOTH horizontal and vertical grid lines, the
  vertical ones aligned to the x-axis date ticks, at the crisp weight/opacity of
  the app's financial calendar table grid (`_GRID_LINEWIDTH`, `_GRID_ALPHA`)
  rather than matplotlib's washed-out default, which left the horizontals barely
  visible and drew no verticals at all.
- **An allocation is not only of the brokerage** (`ALLOCATION_SCOPES`,
  migration 41). Quicken allocates investment accounts and nothing else, so a
  house never appears in it; its own forums answer the question with "invent a
  dummy security in a dummy brokerage". Mammon instead scopes the window:
  `investments` (Quicken's answer), `with_cash` (plus checking/savings/cash) or
  `everything` (plus property and other asset accounts). A NON-investment
  account contributes its whole balance under `accounts.asset_class`
  (`portfolio.set_account_asset_class`, set on the By account tab). With none
  said, a cash-shaped account counts as cash -- the rule that already applied to
  a brokerage's idle cash -- and everything else stays `unclassified`, because
  what an "other asset" account holds (a house, a car, a violin) cannot be read
  off the ledger. **Debt is in no scope**: an allocation is of what you own, a
  pie cannot draw a negative slice, and a mortgage belongs to net worth. The
  scope is a display preference (QSettings), defaulting to `everything`.
- The MCP surface gains `lots`, `capital_gains`, `performance`,
  `investment_performance` (the consolidated per-holding snapshot: cost basis,
  market value, gain, % return and income, as decimal dollar strings) and
  `allocation`, read-only like the rest.

### 5.8e What an asset is worth (market value vs. cost basis)
- An asset account's **ledger balance is its cost basis** -- purchase price plus
  the improvements posted to it. That is the number a capital gain needs and the
  wrong number for allocation, leverage or net worth. Market value therefore has
  its own series, `asset_values(account_id, date, value_cents, source, note)`
  (migration 42), shaped like `price_history` is for securities. The two are
  never conflated: Quicken's "Update Account Balance" writes an adjusting
  transaction instead, which makes an appreciation indistinguishable from money
  spent on a new roof and destroys the basis.
- `investments.display_balance` is the single chokepoint: an asset account with a
  recorded value on or before the date shows that value, otherwise its ledger
  balance. So net worth, the account bar and `portfolio.allocation` all follow
  from one rule, and an unvalued asset is unchanged rather than zeroed.
- Values are read **on or before** a date, never nearest. A valuation taken today
  is not evidence of what a house was worth in 2012, and letting one leak
  backwards would rewrite every historical net-worth figure the first time the
  user clicked Get Value.
- **Fetching is source-injected** (`asset_values.fetch_values`), exactly like
  `investments.fetch_quotes`: any object with `get_values(requests)`, so the
  network lives inside the source and tests inject a fake.
  `ZillowValueSource` drives a recorded webSlinger script (`GetZEstimate`,
  input `houseAddress`, output `zestimate`). Zillow retired its public API on
  2021-09-30 and the partner API does not expose a Zestimate, so a recorded
  script is the only route; it runs in the user's own Chrome profile and session,
  which is the user looking up their own house rather than a datacenter scraper.
  A price-index roll-forward (FHFA HPI) is the natural second source and is the
  only way to get HISTORY, since a Zestimate is today's number only.
- Three rules are load-bearing, each a bug that was hit:
  - **An address is confirmed by the user and stored, never derived.** A name is
    not an address ("Condo (Asset)"); one that looks like an address may be
    incomplete ("240 Birch" needs its city); a neighbouring account's
    address is not this account's. Inferring "118 Cedar Ln" from a liability
    of that name returned a real, cleanly-extracted $400,000 for a house sold
    years earlier. `suggest_address` offers a starting point; only a stored value
    is ever fetched. Same discipline as `investments.ticker_of`.
  - **A closed account is never valued.** A property account outlives ownership
    because the history stays after the sale, so `valuable_accounts` excludes
    closed accounts. Hidden accounts are still included -- hidden means
    "incomplete records", not "sold".
  - **A failed fetch leaves the last good value standing.** A source that raises,
    returns nothing, or returns an unparseable or non-positive value writes
    NOTHING, and the account is named in `FetchReport.missing`. A wrong valuation
    is worse than a stale one.
- **A loan names the asset it is secured by** (`accounts.secured_by_account_id`,
  migration 43; `asset_values.set_lien`). The column is on the LIABILITY because
  the relationship is MANY loans to ONE property -- a first and second mortgage,
  or a loan and its refinance, all sit against one house -- so putting it on the
  asset would model only the single-loan case. `ON DELETE SET NULL`, so removing
  a property un-links its loans rather than cascading a delete into rows holding
  real payment history. The link is validated (a loan must be a liability, its
  security an asset) and never inferred from name similarity, for the same reason
  an address never is. `debt_against` sums the magnitudes of the linked balances
  and ignores a liability that has swung positive, which would otherwise report
  leverage below 1.0 and read as the house being worth more than it is.
- Account Details is TYPE-CONDITIONAL: Address on an asset, Secured by on a
  liability, Cost basis on an investment. Institution hides for an asset (a house
  has no institution) and **seeds the Address field when it is empty**, because
  that is where addresses were typed before Address existed -- offered as a
  confirmable value, never fetched from unseen. A lien onto a since-closed
  property is still offered in the picker rather than silently reading as
  "(not secured)".
- **A valuation is never a register row.** The asset register stays a cost-basis
  ledger of events that actually moved money; the value series is reached from
  the register's GEAR, as `Get Value…` (fetch, the `Get Quotes…` analog) and
  `Value History…` (`mammon/ui/asset_value_dialog.py`). Both are hidden on every
  other account type, re-checked on each gear open because Account Details can
  retype an account under an open register. Storing valuations as balance
  adjustments was considered and rejected three times over: a revaluation
  REPEATS (fifteen years of quarterly Zestimates is sixty adjusting transactions
  in a register holding three real ones); every adjustment needs a CATEGORY, so
  the file grows an "Unrealized Gain" pseudo-category that then has to be
  excluded from every income report, budget and spending pie -- the same
  pervasive-exclusion burden transfers already carry; and BASIS becomes
  unrecoverable, since a $30k roof and a $30k appreciation are the same row once
  both are transactions.
- **The series is fully editable, not a fetch-only cache.** A house has an
  appraisal history predating the app, so Value History adds, edits and deletes
  dated values -- backfilling a purchase or refinance appraisal is what makes the
  chart worth opening, and `ZillowValueSource` can only ever supply today's
  number. A hand-entered value is tagged source `manual` so it stays
  distinguishable from a fetched one. The date is the series key and `set_value`
  UPSERTS, so moving a value onto an occupied date is confirmed first and an edit
  that moves a date deletes the old row rather than leaving a duplicate.
- **An asset register's status line names the basis.** Labelling a decades-old
  purchase price "Ending Balance" states the number nobody wants as though it
  were the answer, so an asset register reads `Cost Basis` beside `Value` (with
  its as-of date) and, when loans are linked, `Debt` and `Equity` -- the way the
  investment register shows Market Value beside its cash. An unvalued property
  shows its basis and says the value is not recorded, never a fabricated one.
- `asset_values.exposure` reports gross value, the debt secured against it, net
  equity and leverage -- the number a pie cannot show, since a $400k house
  against a $250k mortgage is not a $400k position but $150k of equity at 2.7x.
  Leverage is `None` when equity is zero or negative, where a ratio is
  meaningless rather than merely large. `AssetValueCanvas` charts the value
  series with the cost basis as a flat reference line, so the gap between them
  reads directly as unrealized appreciation.

### 5.8e-2 A security's identity vs. its description
- A security has **two facts and, until now, one column**. `investment_transactions
  .symbol` (and `holdings`, `price_history`, `review_items`, `holdings_checkpoints`,
  `security_mixtures`, `securities`) stored whatever the source called the thing,
  so the same ETF was `VGT VANGUARD INFO TECH ETF` from a 2021 QIF and `VGT` from
  a 2026 Interactive Brokers CSV -- two securities, two price series, a position
  split from its own dividends, and a chart that drew two points. `securities.name`
  and `holdings.name` had existed since migration 37 and were never written to,
  while `securities.symbol` -- the primary key -- carried the display name.
- **`symbol` is the IDENTITY**: the ticker where one exists, so the key a quote
  provider understands and the key a holding is stored under are the same string.
  That is what stops a fetch filing a price under a name nothing uses.
  **`name` is the DESCRIPTION**, shown to people and never used to look anything
  up. `mammon/securities.py` owns the split; `securities.display` is the single
  chokepoint for rendering one.
- **A ticker is suggested and confirmed, never derived** (`securities.suggest`,
  the Tools ▸ Securities dialog). `investments.ticker_of` returns `FID` for the
  plan fund "FID BALANCED K6" and `INTL` for "INTL EQUITY INDEX" -- a real listed
  company whose prices are already in the file -- so a blind migration would file
  a stranger's prices against a retirement fund with nothing in the data to say
  so. Same discipline as a property address (5.8e) and a quote target.
- **A security with no ticker keeps its name as its identity.** A plan's internal
  fund has no public ticker and never will; inventing one is worse than the
  problem. Identity is therefore heterogeneous (ticker, else name) and honest --
  and is what a surrogate `securities.id` would replace if this file outgrows it.
- **Re-keying is GLOBAL**, unlike `investments.apply_security_renames`, which is
  account-scoped because a user fixing a typo means it in the register they are
  looking at. Merging two spellings of one security is the opposite: leaving
  another account on the old spelling IS the bug. `holdings_checkpoints` is
  PRIMARY KEY `(account_id, year, symbol)`, so a merge is DROPPED and recomputed
  rather than renamed -- a merged position's year-end state is the combined lot
  replay, not either half's. `holdings.name` is likewise re-read from
  `securities` on every `rebuild_holdings`, or it would survive exactly until the
  next import.
- **A merge is confirmed against the FILE, not against the other proposals**
  (`securities.merge_preview`). Renaming `VGT VANGUARD INFO TECH ETF` to `VGT`
  merges it into `VGT` rows already present, and that spelling needs no change of
  its own so it never appears among the proposals -- reporting only
  proposal-vs-proposal collisions described a two-way merge as a simple rename,
  which is the one change here that re-running cannot undo.

### 5.8e-3 Downloaded prices are AS TRADED
- `YFinanceQuoteSource` passes **`auto_adjust=False`** on both `get_quotes` and
  `get_history`. yfinance defaults it to True (1.7.0), returning a total-return
  series back-adjusted for dividends AND splits, while every other price in the
  file -- the QIF price, the transaction-carried price, the latest close -- is as
  traded. Mixing the scales is not cosmetic: a high-yield holding came back at
  half its real 2021 price (QYLD 11.67 against an as-traded 22.62) and an 8:1
  split put VGT's whole pre-split history at an eighth of what it traded for, so
  the chart read as a flat line with the real prices spiking out of it. Splits are
  already modelled (`split_ratio`) and a dividend is a transaction, not a price
  revision, so the provider must adjust for neither.
- **`fetch_quotes` writes under the HOLDING's name, never the ticker it was
  handed** -- callers pass a `names` mapping exactly as `fetch_quote_history`
  does. Writing under the bare ticker looked like it worked and valued nothing,
  and left a two-row phantom series that charted instead of the real one.
- **A downloaded price is replaceable; a paid one is not.**
  `record_prices_if_absent` takes `replace_sources`, and `fetch_quote_history`
  opts in with `REFETCHABLE_SOURCES`. `DO NOTHING` for every conflict made a
  wrong-scale download permanently uncorrectable -- re-running changed nothing
  and said it had changed hundreds of rows, because the function returned the
  count OFFERED rather than the count written. It now returns what landed.

### 5.8f Target asset mix and drift (SRD 5.8f)
- `mammon/rebalance.py` holds a **target mix** and measures the real one against
  it. `portfolio.allocation` answers "where is my money"; this answers "is it
  where I meant it to be", which is the question an allocation view exists to
  serve -- classes are worth separating because they behave differently, and the
  payoff for holding several is keeping them at chosen weights as they diverge.
- Migration 44: `allocation_targets` (name, active, sleeve, the two bands) and
  `allocation_target_lines` (`target_id, asset_class, pct`), shaped like
  `budgets`/`budget_lines`. Several NAMED targets, at most one active.
  Percentages are **Decimal-encoded TEXT**, like share prices: a mix is a precise
  quantity the user typed, and float drift in numbers that must total 100 shows
  up as phantom deviation.
- **The sleeve.** A target governs the accounts it can be applied to
  (`TARGET_SLEEVES`: `investments`, `with_cash`). `everything` is deliberately
  absent -- a target including a house is a target you cannot rebalance to.
  Property comes back in `DriftReport.fixed_rows` as CONTEXT, computed from the
  asset accounts in their own right (NOT as "everything minus the sleeve", which
  would count a chequing balance outside an investments-only sleeve as an
  untradeable holding). A drift number dominated by an illiquid position is not
  actionable, which is the failure most tools avoid only by not knowing the house
  exists.
- **Hidden accounts count for zero** in BOTH halves -- the sleeve and the
  property context (`scope_account_ids`/`list_accounts` with
  `include_hidden=False`, matching `net_worth`). Hiding is how a user flags
  incomplete records, and a hidden balance values as pure cash, so leaking one in
  would not merely inflate the total -- it invents a cash slice from nothing and
  skews every other class's percentage. On a real file a hidden employer plan
  turned a $5k brokerage-cash position into $245k of reported cash.
- **The bands are the 5/25 rule** (`in_band`): act when a class is off by 5
  absolute percentage points OR 25% of its own target weight, whichever fires
  first. Neither half works alone -- 5 points never fires on a 4% sleeve that has
  doubled, and a relative band alone fires constantly on a 60% one. Stored per
  target, so they can be loosened or tightened. With a target of zero the
  relative test is undefined and only the absolute one applies.
- **A target that does not total 100 is reported, never normalized**
  (`target_is_complete`): normalizing a forgotten line would produce a plausible,
  wrong target reporting drift nobody can clear. `target_from_current` seeds a
  target from the mix held today (the usual starting point) and lands the
  rounding remainder on the largest class so the lines total exactly 100.
- **Nothing here writes a transaction.** `move_cents` is arithmetic -- what would
  close the gap -- and executing it stays the user's job in the register. Tax is
  not modelled: a sale in a taxable account realizes a gain, and the cheapest
  rebalance is usually the one funded by new contributions rather than sales. The
  lot data to do that properly exists (`portfolio`); this reports the gap, not
  the tax.
- **Cash is never sold** (`ClassDrift.action`, keyed on `portfolio.CASH_CLASS`).
  Cash cannot be traded to its own weight -- it is spent by BUYING securities and
  raised by SELLING them. So an overweight cash line proposes `invest` (deploy the
  surplus into the underweight securities) and an underweight one proposes `raise`
  (sell the overweight ones to top it up), never `sell`. A rebalance is therefore
  set up NOT to be cash-neutral on purpose: the cash gap is what drives how far
  the securities trades diverge. The verb lives in the domain layer so the "cash
  is never sold" rule holds in one place; the UI is a thin projection of it.
- **Rejected: efficient-frontier optimization.** Mean-variance optimization is
  dominated by expected-return estimates nobody can make reliably, and screening
  candidates by trailing performance feeds it exactly the assets whose histories
  are most overstated -- a portfolio built that way in early 2008 discovered that
  six apparently uncorrelated bets were one leveraged bet on credit. Holding a
  target and rebalancing to it degrades gracefully when the inputs are wrong;
  optimizing to a frontier does not. If correlation work is ever added it should
  be DIAGNOSTIC (conditional correlation in down markets, historical drawdown of
  the current mix), never prescriptive.
- UI: **Reports ▸ Target & Drift…** (`ui/rebalance_dialog.py`, its own module).
  Target percent editable in place, current percent, signed drift in points and
  relative, and a Buy/Sell figure per class (cash reads Invest/Raise, never Sell);
  out-of-band rows coloured (red overweight, blue underweight -- they must not
  both be red, since they mean opposite actions). MCP: `allocation_drift`.

### 5.8g Asset-class mixture per security (SRD 5.8g)
- **One security is not always one class.** A target-date fund is roughly 58%
  equity / 40% bonds / 2% cash. Counting the whole position under a single class
  does not merely coarsen the allocation, it makes every drift figure in
  `mammon.rebalance` WRONG, because the misallocated weight is subtracted from
  some other class that then reads underweight by exactly that amount. Measured
  on a four-fund portfolio of the user's own plan-fund proxies, bonds read **0%**
  without mixtures and **34.8%** with them.
- `security_mixtures` (migration 46) holds one row per `(symbol, asset_class)`
  with a Decimal-text `pct`, a `source` and an `as_of`. A security with no rows
  keeps its single `securities.asset_class` and behaves exactly as before, so
  the feature is additive. Where a mixture exists it WINS over the single class
  in `portfolio.allocation`.
- **Where the split comes from.** `yfinance`'s `funds_data.asset_classes` gives
  cash / stock / bond / preferred / convertible / other — the Morningstar X-Ray
  axis. `security_mix.YFinanceMixtureSource` is injected the same way
  `investments.fetch_quotes`' source is, so the network lives in the source and
  tests use a fake.
- **The domestic/international split is the one thing the data cannot give.**
  There is no region breakdown in `funds_data`; the domicile appears only in
  `fund_overview['categoryName']` as English ("Foreign Large Blend"). Mammon
  splits equity by domicile, so the stock slice lands in the class the USER has
  already assigned to that security, and where they have not assigned one it
  lands in `unclassified` — visibly, and named in
  `MixtureFetchReport.needs_stock_class`. `suggest_stock_class` reads the
  category text as a SUGGESTION the UI offers; it is never a fetch-time default.
  Same discipline as `investments.ticker_of`, where guessing turned
  `INTL EQUITY INDEX` into a real listed company called INTL. Only the equity
  slice needs this: a bond index classifies itself.
  A fund of funds therefore reports its equity in one bucket even when its
  underlying spans two — a known limit of the source, recorded not papered over.
- **Preferred and convertible holdings map to `other`**, not to bonds or equity:
  they are hybrids, routinely under 0.5% of a fund, and Mammon has no class
  meaning either. `other` is the honest bucket rather than a guess about
  behaviour.
- **Two rounding rules are load-bearing.** `normalize` forces the weights to
  total exactly 100 (a provider's own fractions sum to 99.98 or 100.01, and a
  mixture over 100 would allocate more than the position is worth), with the
  remainder landing on the largest class. `split_value` apportions a position's
  cents by LARGEST REMAINDER so the parts sum to the position exactly — naive
  per-share rounding loses or invents a cent per holding, and an allocation whose
  parts do not reconcile is a bug found later by someone else.
- **A fetch that finds nothing leaves the existing mixture alone**: a stale split
  is a far better answer than a wiped one. Nothing here writes a transaction.
- UI: Reports ▸ Asset Allocation… gains a **Mixture** column on the By security
  tab and a **Get fund mixtures…** button; the class combo keeps its meaning as
  "which equity bucket", said in its tooltip. MCP: `security_mixtures`.

### 5.8a Security names (rename / merge)
- One security ends up under two names: a broker abbreviates
  (`ALTY GLOBAL X SUPERDIVIDEND ALTER` -> `ALTY`), or a fund company renames
  outright (`Fidelity 500 Index Fund` -> `FXAIX`). The register then shows two
  positions where the user holds one.
- The investment gear's **Rename Security** does a **search/replace over the
  security names of one account** (`investments.plan_security_rename` /
  `apply_security_renames`). Matching is by SUBSTRING, case-insensitively:
  dropping a descriptive tail and replacing a name outright are both required,
  and neither whole-name nor prefix matching covers both.
- Scoped to ONE account. A name means what that account's broker meant by it,
  and fusing two positions is a judgement about one account's history.
- Renaming onto a name the account already holds **merges** the positions: their
  lots, cost basis and dividends replay as one holding. That is usually the
  point, but it is not undone by renaming back, so the dialog previews every
  change as `old -> new (n transactions)`, flags the merges, and lets the user
  untick any of them before anything is written.
- There is deliberately **no "shorten every name to its ticker" bulk action.**
  In the real trading account this was built against, 310 of 313 distinct names
  have a ticker-shaped first token, because that is how option contracts are
  named; blanket shortening would fuse a stock with its expired options into one
  position.
- `price_history` is keyed by symbol ALONE, with no account column, so a rename
  COPIES prices to the new name (filling only dates it lacks, since a fetched
  quote beats a price derived from an old transaction) and drops the old name's
  rows only once nothing anywhere refers to it. Another account holding the same
  security under the old name keeps everything it needs.
- `holdings` and `holdings_checkpoints` are derived, so they are REBUILT, not
  patched: a merge changes the lot replay itself.

### 5.8h Cryptocurrency accounts
- A cryptocurrency wallet is a DISTINCT account type (`accounts.type = 'crypto'`)
  that is classified INVESTMENT-LIKE (`ledger.INVESTMENT_LIKE_TYPES`) for net
  worth, sidebar grouping and the allocation pie -- a coin wallet is not an
  equity brokerage, but it is valued at market, not at its cash balance. The
  domain layer is `mammon/crypto.py`, a parallel of `mammon/investments.py`: it
  is the SOLE writer of the `crypto_*` tables (mirroring investments' single-
  writer discipline), and `ledger.py` stays the only writer of the cash
  `transactions` table -- crypto adds no second writer there. On an EXCHANGE-kind account (below) the fiat side of a
  buy/sell rides on the crypto row's own `amount` (an internal cash sleeve, the
  way investments keeps buy/sell cash in `investment_transactions`), so both
  domains tell one cash story and no cash-ledger row is written for a trade; a
  WALLET-kind account has no cash sleeve at all.
- **Two account KINDS, an explicit typed `accounts.crypto_kind` (migration 61).**
  A `'wallet'` is a single address / paper wallet holding coins and ERC-20 tokens
  with NO fiat: coin simply arrives and leaves, the network fee is paid IN the
  coin, and the on-chain COUNTERPARTY IS THE PAYEE (`crypto_transactions.payee` --
  the `From` address on a coin increase, the `To` on a coin decrease; there is no
  separate "counterparty" concept and no third-party-payee field). An `'exchange'`
  is a custodial account holding coins PLUS fiat currencies through the internal
  cash sleeve (the `BUY`/`SELL`/`SWAP` model below). The kind is a REAL value in
  (`'wallet'`, `'exchange'`), never an overloaded NULL -- a NULL kind means "not a
  crypto account"; migration 61 backfills every pre-split crypto account to
  `'exchange'`, the model they were built on. Predicates `crypto.is_wallet_account`
  / `is_exchange_account` / `account_kind` classify a row. Both kinds are
  multi-token, security-style: each token is a distinct position valued at market.
- **Coin-native wallet writers (the ShrsIn/ShrsOut analogue).** A wallet coin
  increase is `crypto.record_wallet_credit` (add coin, no fiat leg -- `price` /
  `amount` / `basis` left NULL) and a decrease is `record_wallet_debit` (remove
  coin, with an optional coin-native fee leg via `fee_symbol`/`fee_quantity`, the
  USD `fee_amount` left NULL); both take the on-chain counterparty as `payee`.
  With no USD proceeds or basis, no realized gain is booked -- correct for a
  coin-native wallet. Each token (`ETH`, `USDC`, `LINK`) stays a DISTINCT position
  in `crypto_holdings` (`UNIQUE(account_id, symbol)`); symbols never merge. An
  own-wallet move between two of the user's accounts is still the coin transfer
  mirror (`record_wallet_transfer`, below). All route through the existing
  `record_event`, so `crypto.py` remains the sole writer of `crypto_*`.
- **Both crypto KINDS are creatable from the New Account dialog.** `Crypto` is
  offered in the dialog's type list alongside `Investment` (both are
  investment-like); when `Crypto` is chosen a **Wallet vs Exchange** control
  appears, and the choice flows to `crypto.create_account(kind=...)` through the
  shared `widgets.create_account_from_values` -- the ONE place the dialog's
  `values()` become an account, so the crypto routing lives in a single spot and
  `crypto_kind`/`asset_class` are set. `crypto.create_account` itself funnels the
  base row through the sole writer `ledger.create_account` and then sets the
  crypto fields via `ledger.update_account`. The account groups under Investing
  and routes to its own `CryptoRegisterWidget` (SRD 5.8j).
- **Quantities and per-unit prices are Decimal-encoded TEXT; fiat is signed
  integer cents.** TEXT storage round-trips an 18-decimal wei value exactly. The
  one new hazard over equities is SUMMATION, not storage: Python's default
  28-significant-digit decimal context can silently drop a wei from a large
  balance, so every quantity calculation in `crypto.py` runs inside a local
  high-precision context (`crypto.quantity_context()`, `prec >= 40`). The replay
  entry points wrap their whole body in `decimal.localcontext(...)`, so the
  nested lot math inherits it.
- **Event taxonomy** (the `crypto_transactions.action` enum), each one
  `crypto.record_*` call: `BUY`/`SELL` (fiat<->coin; SELL books realized gain
  from the lots per `accounts.lot_method`), a coin-for-coin **swap** as two
  linked single-asset legs `SWAP_OUT`+`SWAP_IN` sharing a `swap_group_id`
  (SWAP_OUT is a disposal at fair-market value, SWAP_IN's basis IS that FMV --
  never one row crammed with two symbols, which would break holdings replay),
  `SEND`/`RECEIVE` to/from a third party (on an exchange, SEND disposes at FMV and
  RECEIVE is income at FMV; on a coin-native WALLET these carry no fiat and are
  written by `record_wallet_debit`/`record_wallet_credit` above), the in-kind
  income actions `REWARD`/`INTEREST`/`AIRDROP`/`MINING`
  (credited as coin quantity, basis = FMV, accruing to the checkpoint `income`
  total -- the crypto twin of a dividend), `FEE` (a network/gas fee), and `FORK`
  (basis per policy, default the supplied FMV or 0 -- disputed, never hardcoded).
- **A wallet-to-wallet move of the same coin is the EXISTING transfer mirror
  model, re-expressed for coin QUANTITY instead of cents**: two rows
  (`TRANSFER_OUT` in the source, `TRANSFER_IN` in the destination) linked by
  `transfer_pair_id`, each `transfer_account_id` pointing at the other wallet. No
  fiat, no realized gain; the cost basis rides along (the OUT leg relieves it, the
  IN leg re-adds exactly that basis, computed from the source's lots). Editing one
  leg's date/quantity syncs the mirror (quantity negated); deleting one deletes
  both -- the CLAUDE.md transfer invariant, in coin. A transfer renders as
  `[Other Wallet]`, consuming no category row, exactly like a cash transfer.
- **Gas / network fee.** When a fee rides an existing action it is carried in
  `fee_symbol`/`fee_quantity`/`fee_amount` on the parent row (moving a token on
  Ethereum costs gas IN ETH -- one action, two holdings deltas); a fee that must
  debit a distinct holding on its own is a standalone `FEE` row. Either way the
  default treatment is a PLAIN EXPENSE: the fee quantity's basis simply leaves the
  holding, no realized gain is booked (tax-lot precision on gas is an opt-in the
  user has not requested).
- **Holdings and per-year checkpoints** are the direct crypto ports of the
  investments machinery. `crypto_holdings` is a replay cache; `crypto_holdings_
  checkpoints` is the per-(account, year, symbol) snapshot for fast open/scroll,
  with an `income` column standing in for investments' `dividends`. Any write
  invalidates the affected years' snapshots; a read seeds from the prior year's
  snapshot and replays only the current-year delta, which is IDENTICAL to a
  from-inception replay (asserted against that oracle, mirroring
  `test_year_end_snapshots.py`).
- **Valuation reuses the price-history infrastructure, namespaced.** Crypto
  quotes are stored in the shared `price_history` table under the yfinance USD
  pair (`'ETH'` -> `'ETH-USD'`), the namespace that keeps a coin `ABC` from
  colliding with a stock `ABC`. `crypto.fetch_quotes` maps a bare symbol to its
  pair via a `CryptoQuoteSource` (default backend: the shared yfinance source,
  lazily imported; tests inject a fake), and valuation looks the coin up under
  that pair. A crypto account's displayed balance -- and its net-worth
  contribution -- is its full market valuation (cash sleeve + coin value):
  `investments.display_balance` delegates a `'crypto'` account to
  `crypto.display_balance`, so the app's single valuation entry point values any
  account correctly.
- **Coins get the SAME quote plumbing securities have -- current, historical, and
  register-sourced -- reusing the investments code rather than duplicating it.**
  `crypto.fetch_quotes` fetches the latest close per coin (the injectable
  `CryptoQuoteSource`; a missing yfinance backend is a setup step, never raised);
  a just-fetched quote counts toward the displayed total because
  `valuation_as_of` treats a recorded price as activity (§5.5c). Historical
  backfill mirrors §5.5e: `crypto.fetch_quote_history` asks the source for a
  monthly series via `get_history` (a source that only reports the latest close
  says so with `QuoteSourceUnavailable` rather than failing obscurely) and writes
  it with `record_prices_if_absent`, so a downloaded close is refetchable while a
  register-carried price is kept. And the register is itself price history:
  `crypto.learn_prices_from_transactions` records each buy/sell/income/swap row's
  own per-unit USD price (the mirror of §5.5f -- derived from `|amount|/quantity`
  when a raw event states value and quantity but no price) under the coin's pair,
  with DO-NOTHING precedence so a single trade never stomps a market close. Unlike
  equities there is no name-to-ticker guess to confirm -- a coin symbol IS its
  ticker.
- **Net worth breaks out per coin AND per currency, USD applied only at this
  layer (the wallet's rule).** `fx.net_worth_by_asset` returns one line per coin
  (its NATIVE quantity and its USD-converted market value, coin x latest
  `{SYM}-USD` price) and one per fiat currency (native cents and its FX-converted
  value), plus a base-currency total. A crypto account contributes its holdings as
  coin lines and its cash sleeve as a fiat line -- the SAME two halves
  `display_balance` already sums -- so a holding is counted EXACTLY once and the
  total equals `fx.total_in_currency` (no double count anywhere net worth is
  computed). A wallet has no cash sleeve, so it adds only coin lines. The accounts
  overview renders this as a grid (`ui/models.NetWorthByAssetModel`): one column
  per asset, a NATIVE-quantity row, a USD-converted row, and a Total column -- a
  thin projection holding no coin/cents math of its own.

### 5.8i Cryptocurrency import (Etherscan-style native-coin CSV)
- **ETHERSCAN RENAMES ITS COLUMNS, so the header must be matched broadly.**
  Exports through ~2023 head the hash column `Txhash` and the timestamp
  `DateTime`; current ones say `Transaction Hash` and `DateTime (UTC)` (and append
  a `Method` column). That column is load-bearing twice — it is the exact-dedup
  key AND the signature that identifies the file as a by-address export at all —
  so matching only the old spelling rejected a real export outright: the parser
  found no header, the file read as EMPTY, and the import reported it in the cash
  importer's words ("use Adjust mapping to point out the date and amount
  columns"), naming a control the crypto path does not have. Every accepted
  spelling lives in `crypto_csv._is_hash_col`, used by both the header scan and
  the column lookup so the two cannot drift. A zero-row import on a crypto
  account says so in crypto's terms, and a parse failure carries its REASON
  through the multi-file batch path rather than collapsing to "could not read".
- A block-explorer by-address CSV export (Etherscan's per-wallet "Export CSV" is
  the reference shape) lands as crypto events on a `type='crypto'` account. The
  path mirrors the file-import hourglass (Section 6): a PURE parser turns text
  into normalized records with NO database access, and one core function owns all
  DB-facing work. `importers/crypto_csv.parse_etherscan(text) -> list[CryptoRecord]`
  is the parser (the crypto twin of `NormalizedTxn`); `importers/crypto_core`
  (`import_crypto_records` / `import_etherscan_file`) resolves the wallet, dedups,
  classifies each row and writes.
- **The importer dispatches on the account KIND and adds NO second writer of the
  `crypto_*` tables.** `import_crypto_records` routes a `kind='wallet'` account to
  a coin-native path (`_import_wallet_records`) and a `kind='exchange'` account to
  the fiat-sleeve FMV path. Every write funnels through `mammon.crypto`'s event
  writers -- `record_wallet_credit`/`record_wallet_debit` for a coin-native wallet,
  `record_income`/`record_send` for an exchange, and `record_wallet_transfer` for
  an own-wallet move -- never a raw `INSERT`, so the SINGLE-WRITER discipline and
  the transfer-mirror/lot invariants stay enforced in one place.
- **A WALLET import is coin-native (the redesign).** A `Value_IN(ETH)` row becomes
  a `record_wallet_credit` (coin in, the `From` address as `payee`); a
  `Value_OUT(ETH)` row a `record_wallet_debit` (coin out, the `To` address as
  `payee`) with the gas as a coin-native `fee_symbol`/`fee_quantity` leg. NO USD is
  written on any row (`price`/`amount`/`basis` NULL, `fee_amount` NULL) -- a wallet
  is valued at market only at the net-worth layer (SRD 5.8h), so there is no cost
  basis, no realized gain, and no cash sleeve to move. The rules below (gas
  attribution, sign, own-wallet transfer, `tx_hash` dedup, failed-row skip) hold
  for both kinds.
- **Gas is the user's only when the user is the sender.** The export prints a
  `TxnFee` on EVERY row, including inbound ones, but on-chain only the sender pays
  gas. Gas is booked as a same-coin `fee_*` leg on the parent event ONLY when the
  sender address is one of the user's own registered wallets; an inbound row's fee
  belongs to the counterparty and must never debit the user's coin. This was the
  single most consequential finding from the real 2020 ETH export (23 inbound
  rows, 1 outbound). On a WALLET-kind account no address registry is needed: an
  Etherscan by-address export puts the wallet on the `From` of every `Value_OUT`
  row, so a coin-out row IS a row the user sent and its gas is the coin-native fee
  leg; the registered-sender test applies to the exchange-kind FMV path.
- **Sign derives from the two unsigned columns.** Value is split across
  `Value_IN` / `Value_OUT` (exactly one non-zero per row); `Value_IN>0` acquires,
  `Value_OUT>0` disposes.
- **Fair-market value comes from `Historical $Price/Eth`, never `CurrentValue`
  (EXCHANGE-kind path only).** The `CurrentValue @ $<rate>/Eth` column values every
  row at one export-time rate and is ignored; on an exchange-kind account the
  per-transaction historical price supplies the basis / proceeds / gas value,
  computed to signed integer cents under the wei-scale high-precision decimal
  context (`crypto.quantity_context`). A WALLET-kind import writes no USD on the
  row at all (SRD 5.8h), so this historical price is not booked there.
- **The counterparty is the payee on BOTH kinds.** `record_send` / `record_income`
  (the exchange path) take a `payee=` just as the wallet writers do, and the
  importer fills it from `To` (out) / `From` (in). This is what makes a move from
  the user's own paper wallet to their exchange legible: the EXCHANGE side shows
  the paper-wallet address (a real holding the user controls), while the
  exchange-assigned deposit address on the paper side is institutional and is not
  tracked as a user holding.
- **An own-wallet transfer needs a known-address registry.** A row is a
  wallet-to-wallet transfer (mirror model, no realized gain) only when the OTHER
  address also belongs to one of the user's Mammon crypto accounts (matched on
  `accounts.account_number`); otherwise it stays SEND / RECEIVE for the user to
  reclassify, since own-wallet intent is not derivable from the chain data alone.
  The address is captured at CREATION: the New Account dialog shows a "Wallet
  address" field for a crypto type and passes it to
  `crypto.create_account(wallet_address=)`, which stores it in `account_number`
  (the column the MCP authorizer already blanks, so crypto adds no new place a
  private identifier can leak from). Asking only in the after-the-fact properties
  dialog meant a freshly created wallet could never auto-classify anything.
- **The crypto KIND is changeable after creation.** The account-properties dialog
  shows a "Crypto kind" selector for a `type='crypto'` account, writing through
  `ledger.update_account`. This is not a convenience: migration 61 backfilled
  every pre-split crypto account to `'exchange'`, including the ones that are
  really paper wallets, so without it those accounts keep the exchange register,
  the exchange review columns and the exchange import path — and the redesign
  never reaches the account it was built for. Changing the kind only changes how
  existing rows are READ and how future ones are written; no stored row is
  rewritten.
- **`tx_hash` is the exact-dedup key, so a re-import is a NO-OP.** The on-chain
  hash is globally unique and immutable -- the crypto analogue of `fitid`, and
  strictly better. Each row whose `(account_id, tx_hash)` already exists is
  skipped before any write, so re-scraping a wallet's full history never
  double-inserts. Failed on-chain transactions (non-empty `Status`/`ErrCode`)
  move no value and are skipped. After a batch, `crypto.rebuild_holdings` refreshes
  the FIFO/spec-ID lots and per-year checkpoints; a disposal books realized gain
  against them per the account's `lot_method`.
- Wallet addresses and tx hashes flow through only in memory to attribute gas and
  detect own-wallet transfers; they are never written to a tracked file, and the
  test fixtures use synthetic ANON placeholders only.
- **A crypto account's file import routes to the coin parser BEFORE anything
  else, and this is load-bearing.** `MainWindow._ingest_file_via_review` tests the
  account for `crypto.is_crypto_account` first and hands the file to
  `_ingest_crypto_file`; only a non-crypto account reaches the multi-account /
  cash / investment routing below it. Without that test a by-address export fell
  through to the generic delimited importer, which knows only date/payee/amount
  columns: it inferred **`Blockno` as the money column** and produced cash-shaped
  review rows with an Amount and a Cash Bal for an account where no dollar ever
  moves. A file that does not parse as an on-chain export is reported as such
  rather than silently reinterpreted as cash.
- **A WALLET import goes through the import-review queue like every other
  source; an EXCHANGE import writes through.** `import_review.build_crypto_review`
  classifies the parsed records and `persist_entries` stores them as coin-native
  `review_items` (schema v62: `is_crypto`, `fee_symbol`, `fee_quantity`, `tx_hash`,
  reusing `symbol`/`quantity`/`action`/`payee`/`memo`/`date`) — REAL typed columns,
  never a serialized blob, so the panel's DB-backed bulk operations keep working.
  Nothing reaches `crypto_transactions` until the user accepts a row. Every row is
  NEW: there is no fuzzy amount/date matching because `tx_hash` is an exact key, so
  a row either already exists in this wallet (posted OR still pending) and is
  skipped, or it is new — inventing a fuzzy match would manufacture ambiguity the
  chain does not have. Accepting routes through `import_review._save_crypto` into
  `crypto.record_wallet_credit` / `record_wallet_debit`, keeping `crypto.py` the
  sole writer. `_crypto_hash_state` is the single authority on what counts as a
  duplicate, so what gets skipped and what gets reported as skipped cannot
  disagree; a DISCARDED row is deliberately not a duplicate (discard means "not
  now", not "never again", the same policy the cash path states).
- **Rows READ and rows QUEUED are different numbers, and reporting one as the
  other is a lie.** A re-import of an already-imported export queues nothing;
  calling that "0 rows read" made the zero-row message explain it as a layout
  problem, for a file that had just been read perfectly. `crypto_import_counts`
  reports what the export HELD plus the state of the rows already known ("130
  already in the register"), so the "Already imported" wording fires. The cash
  path documents the same distinction in `MainWindow._import_report`.
- **A leftover cash-shaped review row cannot be added to a wallet.** A ledger
  that imported a by-address export before on-chain routing existed still holds
  the rows that import queued: a fiat amount (often the BLOCK NUMBER the generic
  importer mistook for money) and no coin. `save_new` dispatches on
  `mapped.is_crypto`, so accepting one takes the CASH branch and posts that
  amount against an account with no cash sleeve. The review pane names such a row
  `(not on-chain)` rather than drawing four blank coin cells, opens no pending
  line for it, and Accept refuses with the reason and the remedy (discard, then
  re-import).
- **Boundary (not yet built): the webSlinger SCRAPE path is still cash-shaped for
  crypto.** A download whose script DROPS A FILE (EXPORT mode) goes through
  `_ingest_file_via_review` and therefore gets the coin-native routing above. A
  download that SCRAPES ROWS calls `import_review.build_review` on flat row dicts,
  which maps date/payee/amount — there is no coin row shape for it to map because
  no recorded script scrapes an address explorer yet. When one exists, the mapping
  belongs beside `mapped_from_crypto_record`, keyed off the account kind, and must
  not be guessed at in advance: the field names come from the generated script.

### 5.8i-2 Cryptocurrency import (custodial exchange history)

- A custodial exchange's transaction history (Coinbase's per-year "Transactions"
  CSV is the reference shape) lands on a `kind='exchange'` crypto account.
  `importers/coinbase_csv.parse_coinbase(text) -> list[ExchangeRecord]` is the
  pure parser -- the exchange twin of `parse_etherscan` -- and the same
  `crypto_core` / `import_review` machinery does everything DB-facing.
- **The FILE picks the reader; the ACCOUNT only decides whether USD rides along.**
  `crypto_core.parse_export_file` returns `(shape, records)`, sniffing the two
  signatures (`looks_like_coinbase` / `looks_like_etherscan`). Both documents are
  CSVs imported into crypto accounts and only their CONTENT tells them apart, so
  routing by account kind hands one of them a reader that cannot read it and then
  blames the file -- the reported failure. The account kind gets exactly one say:
  an on-chain export into a WALLET is coin-native with no USD on the row, while
  into an EXCHANGE the export's historical price rides along
  (`mapped_from_crypto_record(with_price=True)`), because an exchange keeps cost
  basis and a disposal without an FMV books a phantom loss equal to its basis.
- **The header is not the first line.** The export opens with a blank line, a
  `Transactions` title and a `User,<name>,<uuid>` line. The header is located by
  its `Transaction Type` / `Asset` columns, as the Etherscan reader locates its
  own by `Transaction Hash`.
- **The SIGN on `Quantity Transacted` fixes direction; the type only chooses
  which action of that direction.** The same `Withdrawal` is coin leaving on one
  row and dollars leaving on the next, and `Exchange Withdrawal` is money
  ARRIVING (withdrawn from the exchange venue INTO this account). Reading
  direction off the word inverts those.
- **A row can be pure FIAT.** When `Asset` equals `Price Currency` the row moves
  dollars against the cash sleeve and opens no position; a reader that assumes
  every row carries a coin books a phantom `USD` holding. Conversely a coin move
  that is NOT a trade moves no cash at all -- the export prints a USD figure on
  it for tax purposes, and that is a valuation, not money the account saw.
- **Coin moved between the user's own venues is a TRANSFER, never a disposal.**
  `Pro Deposit` / `Pro Withdrawal` / `Exchange Deposit` / `Exchange Withdrawal`
  map to `TRANSFER_IN` / `TRANSFER_OUT` (basis rides, no gain booked); booking
  them as SEND would realize a gain on coin that never left the user's control.
  A one-sided leg is legitimate here: the other end is a sub-ledger Mammon does
  not model.
- **An unknown Transaction Type is NOT an error.** The vocabulary is open and
  Coinbase keeps extending it; an unrecognised type falls back to the direction
  the sign already fixed, and the raw type is kept (`raw_type`, surfaced as the
  Action cell's tooltip) so the review row shows what the file actually said and
  the user corrects the action before accepting. Refusing a file over one
  unrecognised word would make every future Coinbase product a broken import.
- **A pure cash move needs its own writer.** `crypto.record_cash` (actions
  `DEPOSIT` / `WITHDRAW`, `crypto.CASH_ACTIONS`) writes fiat with `symbol`,
  `quantity` and `price` NULL -- which is what keeps the row out of the holdings
  replay (`_apply_txn` skips a symbol-less row) while `crypto_cash` and the
  register's running Cash Bal still count it. A WALLET never carries one.
- **An exchange import goes through the review queue like every other source.**
  It used to write straight through; its rows need MORE judgement than a
  wallet's, not less, precisely because the source's product names only
  approximate what happened. `import_review.build_exchange_review` shares one
  body (`_build_crypto_entries`) with the wallet builder so the two can never
  dedupe by different rules.

### 5.8j Cryptocurrency accounts in the UI (register, holdings, grouping)
- **Grouping is already investment-like, by the shared constant.** A `type='crypto'`
  wallet appears under the sidebar's "Investing" section next to equity brokerages
  because the grouping table tests membership in `ledger.INVESTMENT_LIKE_TYPES`
  (the single source of truth), not the `'investment'` literal. Its net-worth
  contribution is its full market valuation, via `investments.display_balance`'s
  delegation to `crypto.display_balance` (SRD 5.8h).
- **A crypto wallet opens its OWN register, not the cash or investment one.**
  `MainWindow.open_register` dispatches `type='crypto'` to `CryptoRegisterWidget`
  (the crypto twin of the investment register). The register class is chosen by
  EXACT type -- crypto and investment are both investment-like for valuation and
  grouping, but their activity lives in DIFFERENT tables (`crypto_transactions`
  vs `investment_transactions`), so widening the dispatch to the membership test
  would misroute a wallet into a register that reads the wrong table.
- **The crypto register is a THIN, read-only projection, and ITS COLUMNS DEPEND ON
  THE ACCOUNT KIND.** It renders through `crypto.register_rows`, which owns every
  running-balance / cents / label computation (the UI holds no SQL and no money or
  precision math). `CryptoRegisterModel` lists the columns each kind shows and maps
  position -> column KEY, so a position that means Price on an exchange is a coin
  quantity on a wallet; callers address columns through `column_index(key)`, and a
  key the kind does not show returns -1.
  - **EXCHANGE** (custodial: a fiat cash sleeve, coin traded for dollars) —
    Date, Action, Coin / Wallet, Payee, Quantity (stored SIGNED — an OUT leg is
    negative), Price, Coin Bal (running per-coin balance, folding in the same-coin
    gas so it ties to `rebuild_holdings`), Amount (the fiat cash-sleeve effect of a
    Buy/Sell), Cash Bal, Fee.
  - **WALLET** (a single address) — Date, Action, Coin / Wallet, Payee, Memo,
    Coin Out, Coin In, Coin Bal, Fee. **Price, Amount and Cash Bal are ABSENT, not
    blank.** For a paper-wallet address there is no per-row USD, no fiat leg and no
    cash sleeve, so those columns are not merely empty — they invite a reading of
    the register that is false. Increases and decreases get their own columns (the
    source's `Value_IN` / `Value_OUT`) because on-chain they are different events
    with different counterparties, which one signed column hides. The Fee is
    coin-denominated (`<qty> <SYM>`), never USD. USD is not absent from a wallet,
    it is just not on the ROW: each coin is a security valued at quantity x market
    price at the holdings and net-worth layers.
  The crypto event taxonomy renders as designed: a swap's two
  `swap_group_id` legs BOTH read as one paired `OUT->IN` trade; a same-coin wallet
  transfer renders as `[Other Wallet]` (the mirror model in coin, consuming no
  category, SRD 5.8h); gas that rides an event shows as a `<qty> <SYM>` entry in the
  Fee column.
- **The crypto register carries the same import-review pane as the cash and
  investment registers.** It mounts an `ImportReviewPanel` below its button row and
  exposes `show_review` / `reopen_review` / `_sync_review_action`, so the shared
  MainWindow import and download paths (all guarded on `hasattr(reg, "show_review")`
  / `reopen_review`) drive a crypto account exactly as they do a cash one, and the
  gear's `Review…` action enables whenever `review_items` are pending. Previously
  `CryptoRegisterWidget` mounted no panel and had no `_sync_review_action`, so a
  crypto import wrote `review_items` (lighting the sidebar's red dot, whose
  `count_pending` has no account-type filter) but NO widget showed them and the
  `Review…` action stayed permanently disabled — the pane never appeared and the
  menu item read as grayed. A NEW row is accepted from its editable PENDING
  register line through the single `import_review` chokepoint (`_save_crypto` for
  a coin-native wallet row, into `crypto_transactions`; a MATCHING row still
  points at its existing register line where one can be found).
- **A crypto review row opens a PENDING register line, like every other kind.**
  Selecting a NEW row shows the line about to be added at the bottom of the
  register with an Accept button on it. Most of a chain row is fact and stays
  read-only — editing the date, coin, quantity or fee would invent history, and
  the review list is meant to be ground truth. Three fields ARE a judgement and
  are editable: the **action** (the chain shows coin arriving and cannot say
  whether it was a plain receive, a staking reward, an airdrop, interest, mined
  coin or a fork), the **payee** (the address is the truthful default, a
  recognisable name is more useful), and the **memo**. The action is PICKED from
  the vocabulary the domain layer validates (`ChoiceDelegate`), scoped to the
  direction the chain already fixed — a typo would otherwise surface as an
  exception at accept, with the row already gone from the list.
- **The crypto review list auto-renames the payee through the SAME rename tree the
  cash review uses.** The pending line's payee is resolved first, exactly as the
  cash register's is (`import_review.predict_crypto_payee` → `rename_tree.suggest`,
  `kind='payee'`): once the user has renamed a counterparty ADDRESS to a friendly
  name on enough accepted rows (`rename_tree.MIN_FILL`), the next import of that
  address auto-fills the name instead of showing the raw hex; an address the user
  has not yet named stays the honest address, because the tree fills only from
  CORRECTIONS. The content differs from the cash path — a crypto row's stable
  identifier is the on-chain address, not a bank's statement text, so that address
  is what drives the rename (`_crypto_rename_source`) — but the path is identical:
  accepting a corrected payee feeds `rename_tree.learn` (`_learn_crypto_rename`),
  and only a genuine rename (final ≠ the raw address) teaches anything. Crypto
  examples are stored with **`txn_id=None`**: the rename corpus reads a live label
  by joining `rename_examples.txn_id` against the `transactions` table, and a
  crypto id lives in the overlapping id space of `crypto_transactions`, so
  forwarding it could let an unrelated cash row's payee masquerade as the label —
  storing the label directly sidesteps that collision.
- **The crypto register FUNCTIONS like the cash register (behavioural parity),
  differing only where the content requires it.** The gaps closed:
  - a per-row **context menu** (right-click) offering New / Edit / Delete. Edit
    opens `CryptoTransactionDialog` — the crypto twin of the cash
    `TransactionDialog` — where the fields that are facts (date, coin, quantity,
    price, fee) are corrected through `crypto.update_event`; Delete removes the
    row (both legs of a transfer or all legs of a swap) through
    `crypto.delete_event`. Both rebuild holdings afterward. The numbers stay OUT
    of inline editing on purpose, so a stray click cannot rewrite chain history;
    the dialog is the deliberate place to change them.
  - a trailing **blank quick-entry row**, the cash register's manual-entry
    gesture, that records a brand-new event through `mammon.crypto` (a wallet's
    Coin In / Coin Out → `record_wallet_credit` / `record_wallet_debit`, an
    exchange's trade/deposit → `record_buy` / `record_sell` / `record_cash` /
    `record_event`). It commits on Enter (not per keystroke as the two-field cash
    row can): a crypto event spans several fields, so a partial auto-commit would
    post a wrong transaction. No second write path — `crypto.py` stays the sole
    writer of `crypto_*`, and a wallet write never touches the cash `transactions`.
  - **field navigation identical to the cash register**: keyboard-only edit
    triggers (no double-click-to-edit), a SINGLE click opens the editor, and the
    coin/text cells use the same focus-select editor the cash Payee/Memo cells use
    (`FocusSelectDelegate` → `_FocusSelectLineEdit`: Tab replaces the value, a
    mouse click appends), with the calendar `DateDelegate` on Date.
  Only the CONTENT differs (coin quantities as Decimal text, a coin-denominated
  fee, and a wallet's absent Price / Amount / Cash Bal); the behaviour is the same.
  `show_review` must re-fire the selection handler AFTER revealing the panel:
  `set_entries` selects row 0 and emits while the panel is still hidden, so the
  first row — the one already selected, whose re-click changes no selection and
  emits nothing — is precisely the one whose pending line never appeared.
- **A coin column is sized from the font, and clamped.** A coin quantity is not a
  dollar amount: ETH carries 18 decimals, so a real row reads
  `25.566401739928923937` — 21 characters where a fiat cell needs 9 — and fixed
  96-110px columns elided them to `0....`. Sizing to CONTENT instead starved the
  stretched Payee (the 42-character address that identifies the row) to a 21px
  stub, so the width is measured against a worst-case quantity and clamped at
  both ends, with a header minimum so no section can collapse. Past the cap the
  number elides and the cell's tooltip carries the full value: two fields that
  both need room is a scrollbar problem, not a reason to lose either.
- **The review PANE has a FOURTH column set for an exchange.** Status, Date,
  Payee, Memo, **Action**, Coin, Quantity, Price, Amount -- a wallet's fields
  plus the fiat a custodial account really does move. The cash layout hid the
  coin entirely (a dollar figure and no asset, for rows whose whole content is
  "0.25 ETH moved"); the wallet layout would hide what a Buy cost. The ACTION is
  shown because on this source it is the least certain field, with the export's
  own product name on its tooltip whenever it differs from the mapped action.
- **The review PANE has a third column set for a wallet.** The cash layout
  (Status, Date, Num, Payee, Memo, Amount) and the investment one (Status, Date,
  Security, Action, Shares, Price, Amount) are both wrong for an address: a wallet
  row's identity is date + coin + quantity + the counterparty ADDRESS, and no fiat
  moves, so an Amount column has nothing to put in it. A `kind='wallet'` account
  gets **Status, Date, Payee, Memo, Coin, Coin In, Coin Out, Fee** — read-only in
  every cell (the chain is not a guess the way an importer's action mapping is),
  with the fee shown as `<qty> <SYM>`. A crypto EXCHANGE keeps the cash layout: it
  really does trade coin for dollars.
- **The holdings window values coins (+ cash on an exchange) to the account's own
  balance.** `CryptoHoldingsDialog` lists Coin | Quantity | Cost Basis | Price |
  Market Value | Gain/Loss from `crypto.holding_values` (priced through the shared
  `{SYM}-USD` path), and a footer that totals to `crypto.account_valuation().total`
  -- the SAME number the accounts list shows, so the two cannot drift. On an
  EXCHANGE the fiat cash sleeve is the last row; a WALLET has no cash sleeve at all,
  so it gets no Cash row, no Cash total and no `Cash: $0.00` in the register header
  -- printing a zero states a balance that is not even a concept there. An unpriced
  coin leaves Price / Market Value / Gain-Loss blank. Get Quotes prices the coins (a
  coin symbol IS its ticker, so unlike equities there is no name-to-ticker guess to
  confirm) behind the injectable `crypto.fetch_quotes` source.
- **Net worth breaks out per coin and per currency, from the Reports menu.**
  `Reports ▸ Net Worth by Asset…` opens `NetWorthByAssetDialog`: one COLUMN per
  coin/currency plus a Total, a NATIVE row (a coin's quantity, a currency bucket's
  own cents) and a USD row converting each through price history / `fx`. Once a
  ledger holds coin or a foreign currency, one folded dollar figure hides what it
  is made of -- the same total can be four coins or one. It is a THIN projection of
  `NetWorthByAssetModel` over `fx.net_worth_by_asset`, which splits exactly the two
  halves `display_balance` already sums, so a crypto holding is counted EXACTLY
  once and the Total equals the sidebar's Net Worth strip.

### 5.8c Backup scoping (one folder per database)
- Snapshots live in `data/backups/<db-file-name>/`, one folder per database, and
  the Restore picker opens in the CURRENT database's folder.
- **Restore refuses a snapshot belonging to a different database.** A snapshot
  names the ledger it was taken from (`backup.snapshot_db_name`), and
  `backup.restore_backup` compares that against the file being replaced,
  raising `ForeignSnapshotError` on a mismatch. The check applies when the
  target already exists — writing a snapshot out to a NEW file destroys nothing
  and stays allowed.
- Why both: a scratch `mammon2.db` was opened for a test, and hours later — from
  the old shared flat folder — its snapshot was picked to roll back a mistake
  and overwrote the real ledger. The restore succeeded and produced a valid,
  completely wrong ledger; the loss was not noticed until accounts looked
  unfamiliar, and weeks of investment work had to be redone. Folder separation
  keeps a foreign snapshot out of sight; the name check makes navigating to one
  insufficient to destroy a ledger. Opening another database is what
  File ▸ Open Database is for.
- Nothing already on disk was stranded: `list_backups` reads the per-database
  folder AND legacy flat snapshots, a delta looks for its baseline in both, and
  `python -m mammon.backup organize [--dry-run]` moves the old files into place.
- **Each snapshot carries a "what changed" summary**, so a user choosing among a
  hundred near-identical one-minute auto-backups can tell them apart — the file
  name records only the moment and the manual/auto tag. At snapshot time
  `backup.build_manifest` takes a lightweight, READ-ONLY census of the just-taken
  copy (total transaction count, and per account: id, name, transaction count,
  balance in cents, latest date/payee), and `backup.summarize` diffs it against
  the previous snapshot **of the same tag** to produce one line, e.g.
  `+3 txns: Checking +3 txns (bal 1,234.56->1,250.00); deleted Visa`. The first
  snapshot of a tag has no predecessor and reads `baseline / initial`. The
  manifest is read-only — it never writes the ledger (single-writer rule) — and
  money stays integer cents, rendered without floats.
- **The summary is stored WITHOUT changing the restorable bytes.** A delta keeps
  the manifest+summary as extra keys in its existing JSON header (which is not
  part of the checksummed image: the recorded `sha256` covers the reconstructed
  database bytes, never the header, so a rebuilt delta still verifies); a full
  `.bak` — a plain SQLite file anything can open — gets a `<name>.bak.meta.json`
  sidecar next to it. Retention (`prune_backups`, `purge_auto_backups`) deletes a
  sidecar in lockstep with its `.bak`, and `organize_backups` moves it along, so
  a sidecar never outlives or is orphaned from its snapshot.
- **Where it surfaces:** `python -m mammon.backup list` appends the summary to
  each snapshot's line, and the Restore confirm dialog shows the chosen
  snapshot's summary. The UI reads it through `backup.snapshot_summary` only — no
  SQL and no money logic live in `ui/`.

### 5.8d The universal report customization bar
- ONE control set for every report (`ui/report_filters.ReportFilterBar`, opened
  by the gear as `CustomizeDialog`): **time range, accounts, categories, and
  Include hidden accounts**. Every reporting dialog — Spending by Category,
  Spending Pie / Spending Chart, Income Pie / Income Chart, Itemize, Net Worth
  Over Time — shows the same bar AND the same Period dropdown; the three chart
  windows (Net Worth over time, Income by category, Spending by category) were
  once the odd ones out with no Period dropdown at all, and now host the shared
  one like the rest.
- **One look-and-feel across every report.** The affordance is the same
  everywhere: a shared **Period dropdown** on top beside one enlarged gear
  tool-button. Its option set (`report_filters.PERIOD_PRESETS`) is the **UNION**
  of the two families this dropdown has ever offered — the original calendar
  ranges **This Month, Last Month, This Year, Last Year, Year-to-Date** and the
  rolling ranges **Last 7 days, Last 30 days, Last 12 months, Last 3 years, Last
  5 years, Last 10 years, This quarter, Last quarter, Earliest to date** — ending
  with **Custom**. Ordered shortest span
  first, widening to the whole ledger. An earlier unification wrongly REPLACED the
  calendar ranges with only the rolling ones; both families must remain reachable
  so neither set of habits is broken. A preset re-ranges and refreshes at once;
  **Custom** opens the gear's `CustomizeDialog` to type an explicit From/To. The
  rolling and calendar presets delegate to the pure
  `reports.spending.preset_range`; "Earliest to date" spans the ledger's own
  bounds. Every report and chart window opens on **Year-to-Date**
  (`report_filters.PERIOD_DEFAULT`), so a freshly opened report answers "how am I
  doing this year" without a first trip to the dropdown. **Net Worth Over Time is
  the one deliberate exception** — it opens on "Earliest to date"
  (`report_filters.NET_WORTH_PERIOD_DEFAULT`), because a cumulative curve is
  meaningless over a partial-year slice and a YTD default would lop off decades of
  history. `report_filters.resolve_period(key, conn, today)` is the one
  place a key becomes a range (returning `None` for Custom), keeping the date
  math in the report layer, not the UI. The **saved filter sets** (Save / Recall /
  Delete) live INSIDE that gear popup too, not on an inline row — range,
  accounts, categories AND saved sets share the single gear.
- **Hiding an account EXCLUDES it**, from `ledger.net_worth`, the account bar,
  and every report by default. That is not merely a display convention: hiding
  is the only tool Mammon offers for "the records for this account are
  incomplete, leave it out of my totals" — an employer plan whose internals were
  never entered carries a balance the ledger cannot justify. Do not make hidden
  accounts count by default; it was tried, and it silently added a phantom
  balance to a real net worth.
- **Include hidden accounts is an opt-in checkbox**, off by default. Ticking it
  adds hidden accounts to the picker AND to the report. It earns its place on a
  growth curve: an account zeroed before it was hidden contributes nothing today
  but held money for years, and leaving it out makes decades of saving look like
  a recent windfall.
- Each check-list carries **Mark all** and **Clear all**. Narrowing to one
  account out of ninety is clear-then-tick-one, so widening back has to be one
  click rather than ninety. Both are built by `_list_button`, which denies
  auto-default — see the next point for why that matters.
- **Enter in the bar means Apply.** A `QPushButton` in a dialog is `autoDefault`,
  so Return fired the first one in the focus chain — the accounts "Clear all" —
  and pressing Enter after typing a date wiped every account checkbox. The
  Clear-all buttons are explicitly not auto-default; Apply is the default button.
- With hidden accounts excluded, an all-checked picker is a REAL filter, so
  `selected_account_ids()` returns the explicit visible list rather than `None`.
  `None` means "no filter", and a report that receives it queries every account —
  which would have quietly re-included the accounts just excluded.
- **Net worth accepts account and category filters.** Accounts subset it (a
  balance belongs to an account). Categories do NOT — a balance carries no
  category — so unticking one draws a **counterfactual**: net worth as if that
  spending had never happened, with the cumulative flow added back at each
  sample so the effect compounds forward. A top-level name reaches its whole
  subtree (money posts to leaves), split lines count (a split's parent row
  carries a NULL category), scheduled placeholders do not, and transfers never
  register as spending since they carry no category. The window titles itself
  "as if no <categories>" so a what-if curve cannot be mistaken for the real one.

### 5.9 Reporting
- Priority: a spending report itemized by category over a selectable time
  period (month/quarter/year/custom).
- Then other Quicken-equivalent reports (income vs expense, net worth over
  time, cash flow, account balances).

### 5.9a Report computations as a tool surface (roadmap item 4)
The aggregations Quicken ships as windows exist first as pure functions in
`mammon/reports/`, returning plain dataclasses, so the MCP server (item 3)
and a future report window compose the same deterministic numbers. Shared
extraction lives in `reports/_lines.py` and fixes three rules no report may
drift from: a split is represented by its lines (each inheriting the
parent's payee/tag), a transfer is excluded unless a report asks for the
legs whose other side is OUTSIDE its account set, and a scheduled placeholder
(`scheduled = 1`) is excluded unless asked for. Hidden accounts are left out
of every report unless asked for (§5.8d). Amounts are SIGNED cents throughout
(negative = out), so a refund shrinks its expense category rather than
counting as income.

- `flows.income_expense(start, end, bucket)` -- every category's signed net
  per month / quarter / year / total, income rows then expense rows, with
  per-bucket and grand totals. Categories are classified income/expense the
  way the pies are (`category_types`, by the sign of the whole-ledger net);
  uncategorized money is one pseudo-row classified by its net in the window.
  A range yields every bucket it spans, quiet ones included.
- `flows.cash_flow(start, end)` -- the same over one window, plus a
  TRANSFERS section when the report covers a subset of accounts: money that
  went to or came from accounts outside the subset really did move. Over every
  account that section is empty by construction.
- `flows.compare_periods(a, b)` -- category nets in period A against period
  B with the change in cents and percent (percent is None when A is zero).
- `flows.category_averages(start, end, bucket)` -- per-bucket averages whose
  divisor is the number of buckets the range spans, quiet ones included, which
  is the number a budget wants. ROUND_HALF_UP.
- `balances.account_balances(as_of)` and `balances.balances_over_time(start,
  end, bucket)` -- valued the way the account bar values accounts
  (`investments.display_balance`: investments at market), so a report and the
  bar never disagree by default.
- `payees.by_payee` / `payees.by_tag` with `direction` out (magnitudes of
  money out; the default), in, or net; `count` is transactions, not lines.
  (`payees.by_tag` groups by a row's whole tag string, the single-tag view.)
  **A payee is billed the WHOLE transaction.** `by_payee` reads collapsed lines
  (`_lines.signed_lines(split_lines=False)`): a split contributes its parent's
  own amount, never its legs. Split legs carry no payee of their own, so
  re-grouping them by payee changes nothing -- except that the shared extraction
  drops transfer legs, which unbalances the split. That is how a real ledger's
  paychecks (salary in, tax legs out, a 401(k) deferral transferring away)
  totalled an employer at $12,000 of withholding for a year in which they had
  deposited $60,000. The parent's amount is safe to use because
  `ledger.set_splits` guarantees the legs sum to it. `by_tag` and
  `tags.spending_by_tag` keep the legs: a tag says what money was FOR, a
  category-shaped question, and payroll tax is a real expense however the
  paycheck nets out.
  **The By Payee window and the `by_payee` MCP tool both default to `net`**, and
  that amount is SIGNED the way the register is -- negative = money out. A payee
  on both sides nets: pay someone $200 and take $50 back and they read
  `-150.00`. The report total is signed too, not a magnitude. Rows still rank by
  MAGNITUDE, so the biggest mover leads whichever way it points.
- `tags.spending_by_tag` -- money by FIRST-CLASS tag, same `direction`s, but each
  line is attributed to EVERY tag it carries (many-per-transaction), so a
  two-tagged charge counts under both and overlapping tag totals can exceed the
  grand total. Untagged money groups under `(no tag)`. Surfaced as Reports ▸ By
  Tag (Window).
- `listing.transactions(...)` -- the structured cousin of Find: date range,
  accounts, categories (subtree; a split matches on any line), payee/memo
  text, tag, absolute amount range, cleared state, transfers on/off, with a
  `limit` and a `truncated` flag so a tool answer stays bounded.

### 5.9b Report windows: export, print, saved filter sets (roadmap item 9)
The pure computations of §5.9a become on-screen windows through a reusable
framework (`ui/report_window.ReportWindow`, a modeless `QDialog`) that gives
every hosted report ONE look-and-feel: the shared **Period dropdown** on top
(beside the enlarged gear that opens the `ReportFilterBar` as `CustomizeDialog`,
§5.8d), a plain table below, and Export CSV, Export HTML and Print (PDF) actions.
The Period dropdown re-ranges the report from a preset (or "Custom", which opens
the gear); its **Year-to-Date** default (§5.8d) makes the first paint answer "how
am I doing this year", while Net Worth Over Time alone keeps the whole-ledger
"Earliest to date" default. Its options are the UNION of the calendar and rolling
preset families (§5.8d) — no previously offered range was dropped when the rolling
ones were added. The same dropdown was retrofitted onto the three inline chart windows
that lacked it (Net Worth over time, Income by category, Spending by category),
so every window that shows money over a range now offers the identical control.
It is built by one shared factory (`report_filters.make_period_combo`), used by
both the generalized window and the inline chart dialogs, so its width is pinned
wide enough for the longest preset — `Earliest to date` — which the surrounding
stretch layout otherwise clipped to `Earliest to d...`. **Named saved filter sets moved off the old inline row into
that same gear popup** (`CustomizeDialog.add_saved_filter_row`), so range,
accounts, categories and saved sets share one affordance. The
window is a **thin projection**: it holds no SQL and does no money math. It
calls a report function (e.g. `reports.cash_flow`) with the bar's getters
(`start_iso`, `end_iso`, `selected_account_ids`, `include_hidden`), projects the
returned dataclass into display rows, and repaints. Adding a report to the
framework is writing one pure `*_rows(report) -> list[ReportRow]` projector
(like `cash_flow_rows`) and one `ReportSpec` (title + a `run` that calls the
pure function + that projector) — no new window class, no new write path, no
new money logic. The framework hosts seven reports, each opened from its own
Reports-menu entry and driven by one shared `ReportWindow`: **Cash Flow**
(`reports.cash_flow`, whose spec renames the shared first column `Section` ->
`Direction` — its values are Income / Expense / Transfers / Net, the directions
money flowed — because Cash Flow alone adds a TRANSFERS section and folds it into
its Net), **Income vs Expense** (`reports.income_expense`; kept DISTINCT from Cash
Flow deliberately, not a duplicate — it excludes transfers entirely and its Net is
income + expense only, so the two answer different questions),
**Account Balances** (`reports.account_balances`, whose spec hides the account
checklist and reads the "To" date as its as-of; it is a print/export table, so its
rows are re-ordered to match the LEFT SIDEBAR's account grouping — Banking, Credit
Card, Investing, Property & Debt (`_SIDEBAR_TYPE_ORDER`, which mirrors
`widgets._BAR_GROUPS` and a drift-guard test keeps in step), stable within each
group — and its first column is renamed `Section` -> `Account Type`), **By Payee**
(`reports.by_payee`, whose spec overrides the shared header with a two-column
`["Payee", "Net Amount"]` set — **Payee first** — so the payee name has its own
column instead of riding in the generic Category / Account slot, and the
always-"Payee" Section column is dropped; the shared `payee_rows` projector is
unchanged, so By Tag still shows its Section column. The window runs it at
`direction="net"`, NOT the pure function's `"out"` default: half a household's
payees pay IN — an employer, a pension, a tenant, a marketplace that both bills
and remits — and summing one side reported an employer at their withholding
while summing the other would hide every store. Net signs each payee the way the
register does, negative = money out, so both read correctly in one column, and
the header names it; the spec also sets `fit_first_column`, so the Payee column
opens sized to its widest name rather than truncating long payees), the **Transactions**
listing (`reports.transactions`, whose spec overrides the shared three-column
header with an explicit six-column set — Date, Payee, Category / Account, Tag,
Memo, Amount — via `ReportSpec.columns`, **Date first** so the Date header sits
over the date value and not over the payee, and the payee is its own column
instead of being jammed onto the category. Each `ReportRow` from the
`listing_rows` projector carries a `cells` list, one string per column — the Tag
and Memo the register already holds surfaced beside the resolved
Category / Account (`[Other Account]` for a transfer) — that `_row_cells` renders
verbatim, so the one chokepoint keeps the table, CSV, HTML and PDF in agreement),
**Itemize by Category** (`reports.itemize_tree`, an expandable drill-down — the
one hosted report that is a `QTreeWidget` rather than the flat table, flagged by
`ReportSpec.is_tree`; each INCOME / EXPENSES / TRANSFERS section expands to its
categories, a category to its sub-categories, and a leaf to the individual dated
transactions. Its four columns — Category, Date, Payee / Memo, Amount — override
the shared three-column set via `ReportSpec.columns`, **Category first** so the
Date header sits over the dates and not over the category names; its spec hides
the include-hidden toggle because the pure function takes no such flag. The
`ReportWindow` projects the tree through one pure `itemize_tree_rows(tree)` into a
depth-tagged `TreeRow` list feeding both the widget — re-nested by a depth stack —
and the CSV / HTML / PDF export, which flattens it by indenting the Category
column by depth so the hierarchy survives. Clicking a column header sorts the
transactions within each open group — Date (2nd key payee), Payee (2nd key date)
or Amount (2nd key date), a second click on the same column toggling descending
while the secondary key stays ascending; `itemize_tree_rows` takes the sort as a
pure argument (`sort_key`/`sort_desc`) and the window re-projects the cached tree
and re-opens the same groups. TRANSFERS counterparty rows stay in alphabetical
order UNLESS Amount is the sort key, where they order by their net), and **Investment Performance**
(`reports.investment_performance`, a consolidated
per-holding snapshot — cost basis, market value, unrealized/realized gain, %
return, dividend/interest income and return of capital — valued at prices as of
the "To" date, with portfolio totals. Its five columns — Account, Ticker, Amount
(market value), Gain/Loss $, Gain/Loss % — override the shared three via
`ReportSpec.columns`: the ticker and both gain figures each get their own header
instead of the account landing under a generic "Section" and the gain being
buried inside the label. Because it carries three distinct numeric columns and the
shared layout offers only one trailing money column, each `ReportRow` fills every
column explicitly through `cells`; `_row_cells` renders a full-length `cells`
verbatim, so the projector renders the money through the same `fmt_cents`
chokepoint and the percent — not money — as signed text). The last is a pure read-only assembly
over `mammon.investments`: it reuses the same `security_positions` replay the
Holdings window and a security-filtered register read, so the three never
disagree; `as_of` caps only the valuation price, never the share/cost replay.
**The Gain/Loss columns are bounded to the resolved period.** Given a `start`
(the report window always passes the filter bar's From date), each open, priced
holding's Gain/Loss $ and % are measured over the window as `value_at(To) −
value_at(From) − net contributions in (From, To]` — buys add capital, sells
return it, income and reinvestments are not contributions — so `Last 3 years`,
`Last 5 years` and `Last 10 years` report DIFFERENT gains rather than the same
inception-to-date figure they all used to show (the start date was previously
decorative for this report). This period path rewinds the share count to BOTH
dates (via `investments.holding_values_at` / `net_contributions_by_symbol`),
unlike the inception path where `as_of` caps only the price. Called with no
`start` (the `investment_performance` MCP tool, Holdings reconciliation) it stays
inception-to-date, byte-for-byte as before. The Portfolio **Market Value**
headline gain is the sum of the per-holding period gains, so it reconciles with
the line items; the separate lifetime Unrealized / Realized / Dividend / Return
of Capital total lines are unchanged. Clicking a column header sorts the holdings
— by period Gain/Loss, ticker, account then ticker (ticker secondary), or
Gain/Loss % — reusing the exact `sort_key`/`sort_desc` seam the Itemize tree uses
(`ReportSpec.sortable` names which flat columns sort); a second click toggles
direction and the Portfolio totals never move.

- **One money chokepoint feeds table, CSV, HTML and PDF.** A `ReportRow`
  carries `section`, `label`, and `amount` as **signed integer cents** (negative
  = out) and does no arithmetic; every surface renders that amount through the
  single `ui/models.fmt_cents` chokepoint, so what is on screen, in the exported
  file, and on the printed page agree by construction rather than by three
  parallel formatters drifting apart. Share/price figures, when a report
  surfaces them, stay Decimal-encoded text per the money conventions.
- **CSV export is a pure, importable function.** `report_rows_to_csv(rows,
  columns=COLUMNS) -> str` writes the header (a report's own set — the shared
  three, By Payee's `Payee,Net Amount`, or Itemize's `Category,Amount`) then one line
  per row, the amount via
  `fmt_cents`, using `\n` endings and the `csv` module's quoting so a
  thousands-separated amount or a comma-bearing category name survives. A single
  `_row_cells(row, columns)` helper maps a `ReportRow` onto whichever set is in
  force, so table, CSV, HTML and PDF never drift. `ReportWindow.export_csv_to(
  path)` is the testable seam — an explicit path, no dialog — that
  `_export_csv_dialog` calls after the file picker; e.g. a Cash Flow window
  writes `Direction,Category / Account,Amount` then rows like
  `Income,Salary,"4,200.00"`, a By Payee window writes `Payee,Net Amount` then rows
  like `Acme Grocers,150.00`, and an Itemize window writes `Category,Amount`.
- **HTML/PDF print reuses one renderer.** `report_rows_to_html(rows, title,
  columns=COLUMNS) -> str` mirrors the CSV writer — same columns, same order, amount via `fmt_cents`,
  every field HTML-escaped (the analogue of CSV quoting) so a category name with
  `&` or `<` renders as text — and emits a standalone document whose `title` is
  both `<title>` and heading. `export_html_to(path)` writes that string;
  `print_to_pdf(path)` renders it to a PDF. Both are dialog-free seams so the
  output is verified headless. There is exactly one HTML→document
  implementation: `ui/printing.py`'s QTextDocument/QPrinter helper
  (`render_html_to_pdf` for a file, `render_html_to_printer` for a live
  `QPrintDialog`), which works under the offscreen platform (an offscreen
  `QApplication` is enough to emit a PDF), so printing needs no physical printer.
- **Named saved filter sets are a UI display preference, never DB.** Following
  the `ui/prefs.py` convention, a filter configuration recalled by name lives in
  QSettings (an INI file in the user's scope) — there is no schema change and
  nothing is written into `mammon.db`. This spares re-ticking a ninety-account
  check-list to reproduce a monthly report.
- **The serializers are pure and QSettings-free.** `report_saved_filters.
  filter_state_to_dict(bar)` captures the bar's state as a JSON-able dict —
  `{start, end, include_hidden, account_ids, categories}`, where `account_ids`
  and `categories` are `None` for "no filter" (everything checked) and category
  names are sorted for a stable blob — and `apply_filter_state(bar, state)`
  re-applies it. The round-trip is unit-testable on a bare bar without QSettings.
  Order matters on re-apply: the include-hidden toggle is set BEFORE the account
  ticks, because flipping it rebuilds the account list (preserving surviving
  ticks by id), and setting the accounts first would lose them (§5.8d).
- **Storage is one JSON blob under one key.** The whole `name -> state` mapping
  is kept as a single value under `reports/saved_filters` rather than one key per
  name — QSettings treats `/` in a key as a group separator, so a single blob
  lets a saved-set name contain any character. `save_filter_set`,
  `load_filter_set`, `delete_filter_set` and `saved_filter_names` take an
  injectable QSettings store (the test seam), matching how `ui/prefs.py` is
  exercised. The name prompt (`ReportWindow._prompt_filter_name`) is an
  overridable seam so headless tests never block on a modal.

### 5.10 Scheduled / recurring payments (generalized beyond loans)
- Mammon can PRE-ENTER an upcoming recurring payment as a pending placeholder row
  (`transactions.scheduled = 1`) a few days before its due date, so the register
  shows what is coming and (for loans) already carries the principal/interest/
  escrow split. When the real bank transaction later imports, a loan payment
  MATCHES and MERGES into its placeholder instead of duplicating.
- Two sources of scheduled definitions:
  - Loans: derived from `loan_params` + the amortization schedule (edited via
    Loan Setup). See `mammon/loans_schedule.py`.
  - Everything else (subscriptions, fixed-price utilities like cable/internet,
    memberships, dues): the `scheduled_payments` table (payee, account, amount,
    frequency, next date, category), managed in the **Scheduled Payments**
    manager (Tools ▸ Scheduled Payments…). CRUD + a "Generate pre-entries"
    action. Loan schedules also appear there, read-only. See `mammon/scheduled.py`
    and `mammon/ui/scheduled_payments_dialog.py`.
- **Learned split template (generic, non-loan).** A definition may also carry a
  fixed per-line breakdown in `scheduled_splits` — a paycheck's gross/taxes/
  deferrals, a bill split across electric+water — so its pre-entries already
  carry that split the way a loan pre-entry already carries principal/interest/
  escrow. The template is LEARNED, not typed: when a definition is created from a
  PREDICTED calendar entry (right-click ▸ "Schedule &lt;payee&gt;") whose history
  is split, the calendar copies the payee's most recent split — the same lines
  the register's "Copy from previous &lt;payee&gt; split" copies
  (`ledger.previous_split_for_payee`) — and stores it on the definition. Every
  generated pre-entry then reproduces it as real split lines through
  `ledger.set_splits`, which stays the sole writer of transaction/split rows;
  `scheduled_splits` is definition data only, owned by `mammon.scheduled`. The
  lines are reconciled to each pre-entry's own total, so a varying amount still
  balances (any drift lands in an uncategorized line). A definition with no
  template pre-enters a plain single-category row exactly as before, and a
  transfer definition (a single whole-transaction move) carries no template.
  Coverage: `mammon/tests/test_scheduled_split.py`.
- **The editor SURFACES what will be inherited.** The learned split used to be
  invisible: the Schedule-&lt;payee&gt; editor showed only the prediction's single
  dominant category, so the user could not tell a split (or a different category)
  was about to be entered. Now, when a payee is entered or selected, the editor
  reads that payee's history read-only
  (`categorize.inherited_entry_for_payee`) and, under the Category field, states
  what it will inherit: a payee whose most recent transaction is SPLIT shows the
  breakdown ("Salary $3,000.00; Taxes -$600.00") and DISABLES the lone Category
  (the split overrides it at entry); an unsplit payee pre-fills its learned/most-
  recent category (keyed on the NORMALIZED payee via `import_mappings`, the same
  key the categorizer uses) and notes the provenance, never clobbering a category
  the user typed. The split shown is exactly the one the write path
  (`schedule_prediction` → `ledger.previous_split_for_payee`) enters, so the two
  cannot drift. This is display only — no second writer; entry still funnels
  through `scheduled`/`ledger`. The split note appears only on the calendar's
  Schedule-&lt;payee&gt; flow, which actually learns the template; the plain
  manager Add stores none and so promises none. Coverage:
  `mammon/tests/test_scheduled_calendar_prefill.py`.
- **QIF-vs-generated distinction (important):** these pre-entries are
  MAMMON-GENERATED, NOT carried in any imported QIF/OFX/CSV file. An import file
  contains only posted transactions; it never contains a "this bill is due next
  month" definition. The definitions live only in `scheduled_payments` (and
  `loan_params`), which the importers never read or write. Consequently a
  scheduled definition SURVIVES a QIF re-import unchanged — neither duplicated
  nor lost — and re-importing a file only re-inserts/dedups posted transactions.
  The generated `scheduled=1` placeholder rows are likewise never candidates for
  import dedup (`importers.core._find_dup_cash` only matches `scheduled=0`
  rows), so they survive re-import too. Regression coverage:
  `mammon/tests/test_scheduled.py::test_scheduled_definitions_survive_qif_reimport`.
  The manager UI and the module docstrings state this distinction to the user.

### 5.10c Reminders, projected balances and the calendar (roadmap item 5)
- A scheduled definition is now a REMINDER with a status computed against
  today and its lead: `overdue`, `due_today`, `due_soon` (within the lead) or
  `upcoming` (`scheduled.reminder_status`, `list_reminders`, `due_counts`).
  The Tools menu entry reads "Scheduled Payments (n due)…" and the manager
  shows the status first, colored. Migration 35 gives a definition its own
  `lead_days` (NULL = the default 5), `auto_enter` (0 = remind only: never
  pre-entered, waits for Enter or Skip) and `transfer_account_id`.
- **Kinds.** A negative amount is a bill, a positive one income (a paycheck),
  and a definition with `transfer_account_id` is a transfer whose pre-entry
  is created through `ledger.create_transfer` with its mirror, both legs
  flagged `scheduled=1`; the sign says the direction. The frequency list is
  the loan engine's (adds `semimonthly`, `semiannual`). Semimonthly pairs a
  day on or before the 15th with the day fifteen later and the 15th with the
  LAST day of the month (the paycheck case); a flat +15 days drifts.
- **Enter and Skip** (Quicken's gestures). Enter records the next occurrence
  as a POSTED transaction and advances `next_date`; Skip advances without a
  row. Pre-entry (the `scheduled=1` placeholder an import later merges into)
  is unchanged and now runs at launch when the preference
  `prefs.auto_enter_on_launch` is on (default; switchable in the manager).
- **Projected Balances** (`mammon/projection.py`, Tools ▸ Projected
  Balances…): the balance at the end of every day across the chosen spending
  accounts from today over 7 days to 12 months, from exactly three inputs --
  the ledger balance today, rows already entered with future dates
  (placeholders included), and reminder occurrences from each definition's
  `next_date` forward (which the generator advances past every placeholder it
  creates, so nothing is counted twice; an overdue occurrence lands on the
  first day, being money still expected to move) -- plus a loan's schedule
  rows that carry no payment yet. Not history, not budgets, not interest, as
  in Quicken. The window marks the LOW point, which is what the projection is
  opened for.
- **A loan knows which account pays it** (migration 36, `loan_params
  .funding_account_id`; Loan Setup's "Paid from"). Unset, it is INFERRED from
  history: the account whose posted payments carry a split leg into the loan
  (`loans.funding_account`). The user's real model -- and Quicken's -- posts
  the whole payment on checking as a split (interest, escrow, a `[Loan]`
  principal leg) with only the principal mirror on the loan register. A
  pre-entry therefore posts on the FUNDING account in exactly that shape, both
  the split parent and its principal mirror flagged pending, under the payee
  the user actually uses (the last posted one, e.g. `US Bank`); only a loan
  with no known funder is pre-entered on its own register. Before this, the
  placeholder went on the loan with the full amount and the split there: it
  matched no real payment, the checking download could never merge into it,
  and the funding account showed nothing coming.
- **"Paid from" auto-completes like the register.** The field is the app's shared
  category/transfer input (`ui/delegates.make_category_combo`: editable, a
  case-insensitive popup completer, the `:` gesture) restricted to the fundable
  ACCOUNTS ONLY (checking, savings, credit, cash) -- never a category, since a
  loan is paid from an account. It was a plain combo that only jumped to the
  first item matching the typed letter; the account-name -> account-id mapping and
  the "(not set -- pre-enter on the loan register)" default are preserved, and a
  typed-and-completed name reads back the chosen account id.
- **The wizard's amortization check is time-aligned, and names a growing balance
  distinctly.** Before saving, the payment must at least cover the first period's
  interest plus the extras (escrow/PMI) in force then, leaving something toward
  principal. All three are measured at the SAME period: the interest at that
  date's rate (`loans._period_rate`), the extras active then
  (`loans._active_extras`), and -- the fix -- the payment in force then
  (`loans._active_payment`), which is a dated New-total override when the user
  entered one for that date, NOT the step-2 initial amount. Pitting the initial
  payment against a later-edited (current) escrow made a consistent escrow+payment
  edit in step 4 false-trip "too small". A payment SMALLER than interest + extras
  (its principal would be NEGATIVE, so the payment would GROW the balance) now
  gets its own warning -- distinct from the amortization "too small" message, and
  a hard gate, so a negative-principal split (`loans.payment_split`) is never
  persisted. A valid adjustment re-splits every affected payment through
  `ledger.set_splits` and leaves no uncategorized remainder, so the register's
  `--Split--` warning triangle (`ledger.uncategorized_split_amount`) clears.
- **Loan rows in the Scheduled Payments manager are live, not read-only.** A
  loan row's next date is the first schedule period no register holds yet --
  pending or posted, on the funder (`loans_schedule.next_due_date`) -- the loan
  analogue of a manual row's stored `next_date`. Enter posts that payment on
  the funding account in the posted shape (`loans_schedule.enter_payment`), so
  the row moves on; deleting the payment moves it back. The Edit slot reads
  "Loan Setup…" and opens the wizard; Delete removes the loan setup only
  (`loans.delete_loan_params`: parameters, rates, extras, payment changes),
  never the account or its history; Skip stays disabled because a loan period
  is paid, not skipped. "Is this period entered?" tolerates a payment dated
  within the match window of its due date (`payment_on_funder`): autopay pulls
  early and a merge stamps the bank's actual date, and an exact-date test would
  pre-enter and project the same period twice. Before this every button was
  gray on a loan row and nothing could remove a loan's setup at all.
- **Enter Payment… in the loan register's gear** (`ui/loan_payment_dialog.py`).
  A configured loan is pre-entered automatically only inside its reminder lead;
  outside it, or with pre-entry at launch off, the register had no way to
  record a payment short of typing the three-leg split by hand. The gear item
  posts one the way the schedule would -- on the account that pays the loan,
  the split (interest, extras, principal to `[Loan]`) previewed and recomputed
  as the date, amount or account changes -- through
  `loans_schedule.enter_payment`. "Pay from" defaults to the loan's funder and
  can name any spending account for this one payment (the card, because
  checking is low); a one-off pins the loan's current default first so it does
  not become the inferred funder. A pending pre-entry already standing for the
  period is a placeholder, not a block: saving posts it with the values
  entered, moving it when the account differs, so Enter never doubles a period
  and is usable in exactly the month the pre-entry already sits on checking.
  A period that already holds a POSTED payment only gets a note: an extra
  payment is a legitimate entry. The loan's schedule itself is its setup, so
  the same gear's Loan Setup… / Edit Loan… is where the payment, rates, extras
  and "Paid from" are defined; there is no separate reminder wizard for a loan.
  The gear re-reads the ledger each time it opens
  (`RegisterWidget._sync_loan_actions`), since all of this changes under an
  open register.
- **The life of a pre-entry, end to end.** A pending row (`scheduled=1`) reads
  gray in the register with a tooltip saying how it becomes real; it posts
  when a download merges into it, when the user marks it cleared, or from the
  row menu's Enter Pending Payment (`ledger.set_scheduled`, which carries the
  transfer mirror along). Before this it was indistinguishable from a posted
  row and could stand forever. Downloads meet pre-entries in three ways
  (`importers/core.py`): a funding-side loan pre-entry merges on the exact
  amount, else on the PAYEE when the amount moved within half of the expected
  (a changed escrow or rate), recording a `PaymentChange`; a bill placeholder
  merges on the exact amount, else on the payee (utilities); a lender's own
  download of the principal applied CONFIRMS the pending principal mirror on
  the loan register rather than filing a second payment or re-splitting one
  side of a transfer (`find_matching_pending_mirror`). A different debit of a
  similar size in the window is never taken for the mortgage: the payee is the
  signal. Loan Setup re-issues every standing pre-entry from the saved setup
  (`realign_pending_payments`): a new "Paid from" moves them, a changed
  payment, rate or extra re-splits them. "Is this period paid?"
  (`loans_schedule.payment_for`) looks at every account that pays into the
  loan, so a payment made from the card counts for its period in the
  reminder, the projection and the calendar. A manual definition's standing
  pre-entry is a plain register row: edit it there; the definition's next date
  is a stored counter edited in the manager, and deleting a posted bill does
  not roll it back (Quicken's behaviour too).
- **The projection learns what is regular only from the user.** It projects
  entered rows, defined reminders and loan schedules, and never guesses from
  history on its own. The manager's Suggest… (`scheduled.suggest_recurring`)
  finds payees that recur at a steady interval and amount -- at least three
  posted rows whose spacing fits one interval and whose amounts stay within a
  third of the latest -- excluding transfers, split parents and payees an
  active definition already names, and offers them ticked; accepting one adds
  a definition with the latest amount, the most common category and the next
  date advanced past today. A varying bill is marked ≈ and still merges with
  its download on the payee.

- **Predictions from history** (`mammon/predictions.py`, migration 38). A
  projection built from reminders alone was, in practice, mostly empty: the
  user defines a few bills and the rest of the month's money moves on its
  own. So the projection and the calendar also carry PREDICTED occurrences: a
  payee paid or received at a steady interval and a similar amount over the
  last six months (long enough that a stretch of payroll trouble, with
  paychecks missed and made up close together, is outweighed by the
  regular months around it), from its next expected date on, marked as an
  estimate (`~`). A series is read the way a person reads it: rows within
  three days are one occurrence (the same amount again counts once, a
  payment in parts is summed); the interval is fitted on a grid anchored
  on the last date, so a payday moved for a holiday or a rent paid on the
  third one month and the fifth the next still fits; when a payee is
  usually a plain row of one category and one month arrives as a split,
  that month counts for its line in that category (the rent, not the rent
  less a refrigerator credit), while a payee that is always split keeps
  its total (the net paycheck is what lands); and the amount to expect is
  the latest when the last two agree (a rent that went up stays up), else
  the value most occurrences share, else the median. Trust is weighed:
  three or more
  occurrences with at least three fifths near the typical amount, or two
  within a tenth, or two of any similarity when the
  bank descriptor says automatic (AUTOPAY, ACH, RECURRING, DIRECT DEP,
  PAYROLL, looked for on the rows and in the learned payee mappings). A
  prediction never duplicates a definition (a scheduled payee is not
  predicted), is not shown within three days of an entered row for the same
  payee (that row is the occurrence), leaves loan payments to the loan
  schedule, and can be dismissed per account and payee
  (`prediction_dismissals`, restorable) or, better, turned into a definition
  from the calendar's right-click menu with the editor pre-filled — and when the
  predicted payee's history is split, that split is learned onto the new
  definition and carried forward on every pre-entry (see the learned split
  template under 5.10). Both dialogs have "Include predictions from history".
- **The calendar colours what it shows**, in both themes: scheduled payment
  red, scheduled deposit green, predicted payment yellow (amber on white),
  predicted deposit blue, pending pre-entry muted, entered row plain; a legend
  sits under the grid. Today's highlight comes from the theme: a fixed light
  green under dark-mode text had made today the one unreadable day.
- **"All spending accounts" means checking, savings, credit and cash.** It
  was every visible account but investments, so the house and the loans were
  summed into a cash projection and it read over a million dollars against a
  checking balance of a hundred thousand.
- **The calendar has up to five account slots** across its top
  (`AccountSlots`): click or right-click a slot to choose its account from
  the spending accounts, the × beside it to clear it; the month is summed
  over the filled slots, or over every spending account when none is filled,
  and the summary line names which. One account occupies one slot. The
  choice is a display preference (`prefs.projection_slots`, QSettings), so
  the calendar opens on the same accounts next time; an explicit account
  passed to the dialog shows for that window without changing the memory.

### 5.10d Full-ledger export (roadmap item 8)
- Owning the file is not owning the data unless another program can read it.
  File ▸ Export Ledger… (`ui/export_dialog.py`, `mammon/export.py`, and
  `python -m mammon.export` for a terminal) writes the ledger in three open
  shapes, each for a different need:
  - **QIF** -- accounts (the account list and a section per account),
    categories with income/expense, every posted cash transaction with its
    splits, transfer legs on both sides, cleared/reconciled flags and check
    numbers, every investment transaction, the security list and the price
    history. Quicken's conventions: `MM/DD/YYYY` dates, a Buy/Sell/Div amount
    as a positive magnitude, a stock split as new shares per ten old, `U`
    beside `T` on every cash record. **The dialect is verified against ground
    truth, never reasoned about**: `D:\QLite\data` holds Intuit's
    `QIF_Specification.txt` and 27 years of Quicken's own exports of this very
    ledger, and every shape here was checked against both. Two things that
    look wrong are right, and were briefly "fixed" on a guess before the files
    said otherwise: a split's `L` line echoes the first leg including a
    bracketed `[Account]` (the spec's own mortgage example is `L[linda]`
    followed by `S[linda]`), and both sides of a transfer are written. What
    was genuinely wrong: a cash row on an INVESTMENT account must be an
    investment record -- an `XIn`/`XOut` for a transfer leg with the
    counter-account bracketed and a `$` amount line, a `Cash` line with its
    category otherwise -- never a bank-style record, which carries no action
    code and is malformed inside `!Type:Invst`. Quicken read the 401(k) side
    of a paycheck written that way as a bare transfer and, on matching the
    pair, kept the transfer and dropped the paycheck's whole split. (The spec
    also documents `!Option:AllXfr`, placed right after the top header, which
    forces Quicken to import all transfers regardless of its Ignore Transfers
    setting; not written today.) **The migration is one-way, by decision
    (2026-09-02).** The file is right and Quicken still cannot read it back:
    its QIF import applies transfer detection but NOT inside a split, so it
    lifts the transfer line out of a split and files it separately -- a
    paycheck arrives as nothing but its 401(k) deferral, a mortgage payment as
    nothing but its principal. This is a longstanding Quicken limitation,
    reported across versions and confirmed in its own community; 2017 is
    affected, and later versions dropped nearly all QIF import processing
    EXCEPT the transfer detection that causes it. Mammon will not contort the
    format to suit it -- writing transfer legs as ordinary categories would
    survive the import but silently sever every transfer. Bring a ledger to
    Mammon; do not plan on taking it back to Quicken. The
    measure of completeness is the **round trip**: importing the file into an
    empty Mammon database reproduces every balance, split leg, holding and
    price (`test_export.py`). What QIF cannot carry stays behind, and the
    dialog says so: tags, loan setups, scheduled payments, learned rules, and
    the exact ratio of an odd split (4:3 is rounded).
  - **JSON** -- every table, every row, exactly as stored, under the schema
    version: the complete ledger, nothing interpreted so nothing lost. The
    identifying columns (account numbers, bank URLs, download settings) are
    blanked unless asked for, since sharing the ledger is the common case and
    moving it to another machine the rare one; the file records which it was.
  - **CSV** -- one register per account into a folder, with the running
    balance (the balance on that date even when a date range narrows the
    rows) and each split's legs in the last column.
- Pending pre-entries (`scheduled=1`) are placeholders, not history, and are
  left out of QIF and CSV; JSON has them like everything else.
- **One file, or one per year.** The default is one QIF for the whole ledger;
  Mammon has no trouble writing or reading it. Quicken could not WRITE its
  whole ledger as one QIF (the migration had to be exported a year at a
  time); whether it reads one large file is untested, so the dialog and
  `--by-year` offer `<name>-YYYY.qif` per year of activity as a fallback. Each
  yearly file stands alone (account list, categories, securities, that year's
  prices and transactions), only the first carries the opening balances, and
  an account with nothing that year has no section in it. Importing the set
  in date order into an empty database reproduces the ledger exactly as the
  single file does (`test_export.py`).
- Two things the round trip taught the importer, which apply to any Quicken
  file too. An account's opening balance travels as a transfer to the account
  itself payee'd "Opening Balance"; into an account holding nothing from
  before that import it now becomes the account's opening balance
  (`ledger.set_opening_balance`) rather than a row -- but a ledger built under
  the older reading, with the opening balance as a +row, re-imports exactly as
  before, since the row dedups against itself. And the `!Type:Cat` list is
  read (`QifExtras.categories`): a category is created if missing and given
  its income/expense kind when it has none, never overriding a kind already
  set. The exported security list names each security by the text its
  transactions use, because that is how the importer keys `!Type:Prices`
  quotes; the descriptive name lives in the JSON export.
- **Downloads merge into placeholders** (`importers/core.py`). A funding-side
  loan pre-entry merges with the funder's debit on amount within the loan
  match window, re-deriving the split for the actual amount and clearing the
  pending flag on both legs; a plain pre-entry from a definition (a bill, a
  paycheck, a transfer leg) merges on exact amount within 7 days
  (`scheduled.find_matching_placeholder`), keeping the definition's own payee
  and category. Before this only loan-register placeholders merged, so every
  other pre-entry became a duplicate the moment the real row downloaded.
- The projection follows the money the same way: with a funder known, the
  whole payment leaves the funder and only the principal reaches the loan.
- **Financial Calendar** (the register area's own page, shown at startup and
  again from Tools ▸ Financial Calendar): the same projection laid out as a
  month grid, Sunday first, each day listing its events (a dot marks one not
  yet entered) and its end-of-day balance; past days show what was entered,
  days from today on the projection; Prev/Next step months. It is a PAGE of the
  register stack rather than a modal -- it is what the window opens on, it
  survives account switches, and a register can be opened over it and the
  calendar returned to without losing the month. A write elsewhere marks it
  stale and it redraws when next shown, so edits never pay for a projection
  nobody is looking at.
- **Spending trend chart** (below the calendar on the same page): a bar chart of
  total money-out per month over the trailing twelve months ending with the
  month on screen, so stepping the calendar walks the chart with it. The data is
  a pure read-only aggregation, `reports.charts.spending_by_period` — money-OUT
  magnitude in integer cents per calendar bucket, transfers excluded (moving
  your own money is not spending), splits honored, scheduled placeholders left
  out, quiet months zero-filled — with no SQL or money logic in the Qt layer
  (`ui/charts.SpendingBarCanvas` only renders it). **Its value axis does not
  start at zero**: it auto-scales to the data's own min–max with a little
  padding, holding the lower bound strictly above zero while every value is
  positive. On a real ledger each month's spending sits in the thousands and the
  month-to-month change worth seeing is only hundreds; a zero-based axis would
  flatten every bar to the same height and hide exactly that swing. matplotlib
  is imported lazily and a draw failure degrades to no chart, never a crash of
  the home page.

### 5.5c Quotes
- The investment register's gear menu offers **Get Quotes**: fetch the latest
  close for the account's holdings and record it in `price_history`. The network
  stays behind `investments.fetch_quotes`' injectable `QuoteSource`, so a missing
  backend (yfinance is deliberately not installed) is reported as a setup step,
  never raised into the register.
- A ticker is DERIVED from the security name (`investments.ticker_of` — the
  leading token, when it is ticker-shaped) and **shown for confirmation before
  any fetch**. That confirmation is not politeness: a plan fund named
  `INTL EQUITY INDEX` yields `INTL`, which is a real listed company, so an
  unasked fetch would file a stranger's price against the user's holding and
  value the account wrongly. Names yielding no ticker at all (`DOMESTIC BOND
  INDEX`, `Fidelity 500 Index Fund`) are reported as skipped, never guessed.
- A returned quote is recorded against the **holding's own name**, not the
  ticker: `price_history` is keyed by the name a holding is stored under (see
  `investments.latest_price`), so recording `ALTY` when the holding is
  `ALTY GLOBAL X SUPERDIVIDEND ALTER` would look like it worked and value
  nothing.
- A fetched quote must COUNT toward the displayed valuation. The default as-of
  for a display valuation is `investments.valuation_as_of` — the latest date the
  ledger knows anything about, **transaction or recorded price**. It was the
  last transaction alone, which capped `latest_price`'s `date <= as_of` filter
  below every quote just fetched: the user was told nine quotes were downloaded
  and no total moved. The original reason for the cap survives where it bites —
  a security with no price after 1997 is still valued at its 1997 price, because
  the newest quote is taken per SYMBOL on or before that date.

### 5.5j One line, one match
- **An EXACT match outranks a tolerant one across the whole batch, not just
  within a row.** The tolerant scheduled tier already runs last inside
  `_find_match`, but claiming is global: a tolerant row earlier in the file took
  the register line, and the row matching it to the cent -- later in the file --
  found the line claimed and classified NEW. `build_review` therefore runs TWO
  passes: pass 1 offers every row only the exact tiers, pass 2 lets what is
  still NEW try the tolerant tier.
- **A register line is one event, so at most one review row may hold it.**
  `set_manual_match` releases any other row already matched to the chosen line
  (`release_txn_matches`) before taking it. Hand-matching onto a taken line used
  to leave both rows pointing at it, and the second then reconciled a
  transaction already spoken for.
- **A single match can be undone.** `unmatch_one` returns one row to pending NEW
  and, when it had been accepted, restores that register line's prior
  `fitid`/`cleared`/`reconciled` from the values `accept_match` recorded.
  Previously the only undo was `undo_all_matches`, so correcting one wrong match
  tore down every correct one with it. Reached from the review row's context
  menu (**Unmatch**), which is now offered on an already-accepted MATCHING row
  too. Investment rows are restored in `investment_transactions`, their own
  table.

### 5.5k Learning starts empty
- **The rename and category trees are never seeded from register history.** They
  learn only from rows the user accepts in review. Bootstrapping replayed every
  posted transaction's `memo -> payee` pair as an accepted rename, which is only
  true for downloaded rows; for hand-entered and QIF-imported history the memo is
  a note the USER typed. One 1998 memo of "deposit" on a Foothill Place row put
  that payee on the trie, and `MOBILE DEPOSIT` -- pure bank boilerplate -- then
  renamed to Foothill Place in a ledger started deliberately fresh.
  `ensure_bootstrapped` remains callable on both modules for an explicit
  seed-from-history action, but nothing invokes it on open.
- **Offering a name and applying it are separate gates.**
  `import_review.RENAME_MIN_SIGHTINGS` (2) decides whether a payee appears in the
  dropdown; `rename_tree.MIN_FILL` (2) decides whether it is written into the
  cell, and applies to the matched leaf -- the same text renamed twice -- not to
  the payee overall. A payee renamed twice on other text is offered for a new
  variant but not applied to it. A payee chosen once is neither filled nor
  offered.
- **Prior register rows never fill the payee.** Rows that never went through
  review carry memos the user typed, and matching a download against them is
  how a 1998 note of "deposit" once renamed `MOBILE DEPOSIT`. Those rows still
  back-fill the CATEGORY when they agree (`PRIOR_TXN_MIN_ROWS`,
  `PRIOR_TXN_PURITY`) and the resolved payee is theirs.

### 5.5i Discarding a review row removes it
- **Discard means "not now", not "never again".** Discarding used to TOMBSTONE:
  the row stayed with `state='discarded'` so a re-download's `INSERT OR IGNORE`
  would collide with it and not re-add it. That inverted the gesture. The user
  discarded a list expecting to download the range again and take another run at
  matching it; instead 106 tombstones ate the re-download and only the 5
  genuinely new transaction ids came back -- and with no un-discard action
  anywhere, discard was a one-way door only a hand-written UPDATE could reopen.
- `discard_all` and `discard_one` therefore DELETE the row. Downloading the same
  date range offers it again. A row the user genuinely never wants is excluded by
  choosing a different date range, which they control directly; it needs no
  permanent per-row veto.
- **ACCEPTED rows still persist**, and must: they became register transactions,
  so re-offering them would duplicate work already done. The dedupe that matters
  is `(account_id, transaction_id)` against accepted rows plus the register
  itself.

### 5.5h Matching a scheduled pre-entry (tolerant amounts)
- **A pre-entry's amount is a forecast, so the matcher must not demand it be
  right.** The finance calendar enters a recurring payment as the MEDIAN of the
  last six months, and a loan pre-entry uses the amortization schedule's figure.
  Both are wrong by construction the moment escrow or a rate moves: the real
  debit arrives for a different amount, misses its placeholder, and lands as a
  NEW row beside a pre-entry that then stands forever.
- The signal is the visible `num` of `Sched`, not the internal `scheduled` flag
  — the marker is what survives a QIF round trip (on a reloaded ledger 523 rows
  carry the num and none carries the flag).
- `import_review._find_match` therefore adds a LAST tier: a `num='Sched'` row in
  the date window, SAME SIGN, whose amount is within
  `SCHED_AMOUNT_TOLERANCE` (half the placeholder's own amount — the same
  latitude the file-import path already allows for this case in
  `importers/core._funding_pending_by_payee`). Running last means an exact match
  always wins and this only ever rescues a row that would have been NEW.
- **Unambiguous-only.** `core.py` can afford that tolerance because it also
  demands the payee match; a downloaded review row has no payee at
  classification time (`map_row` leaves it empty by design). So the safety
  property here is different: the tolerant tier fires only when exactly ONE
  scheduled candidate is in the window. Two are a guess, and a guess that
  silently reconciles the wrong row is worse than leaving it NEW.
- **Manual match opens both tolerances, and is capped at a fortnight.** The user
  reaching for manual match has already been failed by the automatic one, so
  candidates are offered within ±`MANUAL_WINDOW_DAYS` (15 — never more, by
  request) at any amount within the same tolerance, ordered nearest-amount then
  nearest-date so an exact match still heads the list. The SIGN never varies: a
  payment is not answered by a deposit. `set_manual_match` enforces the same two
  rules on a hand-picked id. The dialog shows a **Difference** column, because
  once amounts may differ the value alone no longer tells the user whether they
  are looking at the right row.

### 5.5d Accepting a review in bulk
- **A MATCH merges into the existing line. The bank owns the AMOUNT; the
  register owns everything else.** Accepting a MATCHING review row — one at a
  time or via Accept All — reconciles the register (or scheduled/loan
  placeholder) line it matched: it marks that line cleared, stamps the source's
  transaction id *only* when the line had none, and **adopts the downloaded
  amount**. It leaves the date, payee, memo, category and SPLIT the user entered
  untouched, so a download can never overwrite a manually-entered date with the
  bank's posting date — the failure users report of other tools.
- The amount is the exception because a matched line's own figure is often a
  FORECAST: a scheduled pre-entry carries the finance calendar's six-month
  median, a loan pre-entry an amortization figure whose escrow may have moved
  since. That staleness is exactly why such a row needed a tolerant or hand-made
  match, and leaving it meant the register kept a number that never happened.
  The file-import path already merged this way (`merge_import_into_placeholder`:
  "adopt the actual date and amount… the pre-entry keeps its own payee and
  category").
- A split whose lines no longer sum to the adopted amount is allowed and stays
  visible — that sum invariant was deliberately removed so a row carrying a
  discrepancy can still be edited, and the discrepancy is the signal that the
  loan or the schedule needs updating.
- Undo restores it: `review_items.prior_amount` (migration 59) records the line's
  own figure, and both `unmatch_one` and `undo_all_matches` put it back through
  one shared helper, alongside fitid/cleared/reconciled.
  `import_review.accept_match` is the sole writer of this path, and the review
  panel makes the policy inspectable: a matched row's Status cell states, on
  hover, what merges versus what is preserved. Regression:
  `test_merge_policy_visibility.py`.
- **Accept All is "accept every row without editing it"**, and must agree with
  doing exactly that by hand. `import_review.accept_all` therefore applies the
  same predictions the register's pending row shows — the learned rename, the
  learned category, the learned transfer account, and (for investment rows) the
  learned action — recomputed per row inside the loop, so a rename learned from
  an earlier row of the same batch reaches the later ones.
- It previously saved NEW rows with the RAW mapped values, so a rename tree the
  user had spent weeks training produced bank gobbledygook the moment they used
  the bulk button instead of the single one.
- **A bulk accept LEARNS nothing** (`save_new(learn=False)`). A prediction nobody
  looked at is not evidence: reinforcing a dropdown candidate the user never
  chose — or, where nothing is learned yet, teaching the tree that a statement
  text maps to a tidied copy of *itself* — would make the tree more confident
  precisely where it got no confirmation. Accepting a row individually is the act
  that teaches.
- The investment-action prediction lives in `import_review.predict_action`, not
  in the register model, so the bulk and single paths cannot drift apart again.

### 5.5l Accepting a NEW row creates the category you typed
- **A category typed into the pending row and confirmed is CREATED on accept.**
  When the user types a brand-new category into the register's editable pending
  row, confirms the "Create new category?" prompt, and Accepts the NEW review
  row, the accept path (`import_review_widget.PendingRowController.accept_new`)
  resolves the buffered text through `import_review.resolve_or_create_category`,
  a thin get-or-create wrapper over `ledger.resolve_category` — the single writer
  of category rows. Previously accept used the LOOKUP-ONLY
  `category_id_for_name`, which returns `None` for unknown text, so a freshly
  typed category was silently dropped and the transaction posted with no
  category; the user had to re-add it by hand afterward.
- **Only an ACCEPTED row invents a category, never a previewed one.**
  `category_id_for_name` stays lookup-only and is still what `predict_fields`
  uses while a row is merely being previewed — a category is only ever minted at
  the commit point, from text the user typed and confirmed.
- **Transfer detection still wins, and blank invents nothing.** A `[Account]`
  category names a transfer target and is resolved first
  (`transfer_account_id_for_name`); only when the text is neither a transfer
  target nor blank is a category get-or-created. `ledger.resolve_category`
  reuses an existing category case-insensitively and creates each missing
  `Parent:Child` level, so accept never forks a near-duplicate. Regression:
  `test_review_new_category.py`.

### 5.5e Historical quote backfill
- Get Quotes carries **"Also fetch monthly history back to each holding's first
  transaction"**. Ticked, it calls `investments.fetch_quote_history`, which asks
  the `QuoteSource` for a monthly series (`get_history`, optional — a source that
  only reports the latest close says so rather than failing obscurely).
- Why: net worth values a holding at the newest price recorded ON OR BEFORE the
  sampled date, so a security priced in 2015 and not again until 2023 holds its
  2015 value flat and then jumps. Across a portfolio those steps land on
  different dates and the growth curve comes out jagged — not because the money
  moved that way, but because that is when prices happened to get recorded.
- **Existing prices are never overwritten** (`record_prices_if_absent`): a price
  carried by a real transaction is what the user actually paid that day, and a
  monthly close must not displace it. Re-running is therefore cheap and
  idempotent.
- Recorded under the **holding's name**, not the ticker — same reason as
  §5.5c — and bounded by each security's first transaction IN THIS ACCOUNT
  (`first_transaction_dates`), grouped so it is one request per distinct start
  date rather than one per holding.
- The backfill is a separate, failable step from the latest-close fetch: a
  provider with no history for one security must not cost the user the closes it
  already returned.

### 5.5f A share move's price is data, not decoration
- `ShrsIn`/`ShrsOut` carry **price and gross amount** in the edit dialog's field
  map. `ShrsOut` listed neither, so opening a plan's fee removal in the editor
  and pressing OK silently blanked what the broker sent. Cash effect is zero
  either way (`_CASH_ZERO_ACTIONS`), so the amount is a gross annotation, never a
  cash movement.
- `learn_prices_from_transactions` **derives** the per-share price as
  `(|amount| - |commission|) / quantity` when the row states value and shares but
  no price. Commission comes out first, matching the basis convention
  (`basis = price*qty + commission`), so a commissioned trade never reports a
  price no share traded at.
- Why it matters: a quarterly fee removal from a fund quoted nowhere public is
  often the ONLY quote that fund will ever have, and net worth values a holding
  at the newest price on or before each sampled date. Measured on the real
  ledger, 552 of 599 `ShrsOut` rows carry no price at all.
- A share move stating only a share count yields nothing, and nothing is
  invented for it.

### 5.5b Learned description mapping (payees and investment actions)
- One engine, two domains (`rename_tree._DOMAINS`): statement description
  and/or the source's own payee field → payee, and a source's raw activity text
  → Quicken investment action. Both are importer vocabulary guesses that only
  the user's own corrections can make right.
- **The engine is a decision tree rebuilt from the user's accepted corrections
  at every prediction** (modelled on webSlinger's `selector_tree`, features
  replaced by tokens, branching one). Nothing is learned online. The corpus is
  `rename_examples` (schema v60): one row per accepted review row — source
  text, supplied payee field, the label chosen, and the id of the transaction
  the accept created. The label is read **live** through that id, so a payee
  edited in the register or an undone accept changes the next answer without
  re-teaching. Review retention never touches the table, so a rename taught
  from a review row purged a year later is not forgotten.
- **Only corrections train the payee domain.** A row accepted with the text it
  was shown with (the description, its title-cased default, or the supplied
  payee) is not a rename and is not counted. A bulk Accept All logs only a row
  whose payee was actually renamed by an applied fill. The action domain counts
  every accept: a kept importer guess is a confirmation.
- **The user's display rule.** The register shows the source's payee field if
  the record carried one, else the bank's description (title-cased when the
  feed shouts), until the same text has been renamed **at least twice**
  (`rename_tree.MIN_FILL`) and the matched leaf names ONE payee — then that
  payee fills the cell. A leaf with several payees is a dropdown, never a fill.
- Answering a row: (1) candidates are the examples sharing a *distinctive*
  token — a non-boilerplate token carried by at most
  `MAX_LABELS_PER_TOKEN` (5) payees — plus exact token-set matches (how an
  all-boilerplate `MOBILE DEPOSIT` finds its own history); (2) a binary
  decision tree on token presence is grown over them by information gain;
  (3) the leaf's leading label is **generalized the way webSlinger
  generalizes an array selector over its fields** (the user's rule): each
  example is a feature set — every token, the token before it, the token
  after it, the same over the payee field, and "no payee field" as a value —
  and the pattern keeps a feature only when every example has it, with its
  value when they agree, as a bare "something here" slot when they differ,
  dropped when any example lacks it. `June rent` and `July rent` renamed
  Tenant generalize to "a token, then RENT": `August rent` fits, a bare
  `rent` does not until it is named too, after which anything with RENT
  fits. A pattern from identical rows stays exact, so a youth theater in
  Anytown does not inherit Walmart's exact rows on the town's tokens. A
  label spanning unrelated formats (Amazon) is split by shape first so its
  pattern pins a merchant token. (4) the leading label fills when the row
  fits, ≥ `MIN_FILL` (2) of its examples back it, and it holds ≥
  `FILL_PURITY` (0.9) of the leaf; else the leaf's labels and the other
  candidates (each with ≥ `RENAME_MIN_SIGHTINGS` sightings) go in the
  dropdown and the raw text stays.
- Measured, predicting each accepted row before learning it: on the fresh
  ledger (42 corrections) 20 fills, 0 wrong, first fill on the third sighting
  of every recurring payee (the online trie: 15 fills, 1 wrong, on the fourth);
  on the older ledger (679 rows) 369 fills at 98.6% precision with the right
  answer in the dropdown for 71 of the 138 unfilled rows (the trie: 255 fills
  at 96.9%, 58 of 71). The five misses: two payees the user spelled two ways,
  one ambiguous deposit, two first sightings. Suggest costs under a
  millisecond.
- **A supplied payee field is evidence, not a gate.** Its tokens join the
  description's in the query and its features join the pattern, so the
  truncated-field case (`Dividend Earned For Period O`) is renamed once
  corrected twice, while the Venmo counterparty case is not: a pattern learned
  from description-only rows requires the absence of a payee field, and a
  pattern learned under one counterparty's name requires that name.
- Importer `action_map` tables are being retired in favour of this learning —
  Interactive Brokers ships with none.

### 5.5g Rule conditions and the Rules Manager (roadmap item 10)
The keyword engines above learn a bare `keyword -> category` / `keyword ->
account` that fires on any transaction whose bank text carries the token. That
is right most of the time and wrong exactly when one merchant means two things —
an "AMAZON" that is Shopping on the credit card but a Prime-Video charge on
checking, a round "TRANSFER" that is savings below a threshold and brokerage
above it. Roadmap item 10 lets a rule ALSO carry optional conditions — roughly
*which account, how much, what memo* — and gathers all three learned-rule
engines behind one editor. The schema lives in Section 4 (four NULLABLE columns
on both `category_rules` and `transfer_rules`, schema v40); the matcher is
`mammon/keywords.py`, the domain layer `category_rules.py` / `transfer_rules.py`
/ `categorize.py`, and the editor `mammon/ui/rules_manager_widget.py`, reached
from **Tools ▸ Rules Manager…**. Rules are app-learned and never imported, and
`mammon.ledger` stays the sole writer of transaction rows — these tables hold no
money movement.

- **A condition narrows WHEN a keyword rule fires; all-NULL is the old
  behaviour.** A rule may carry any subset of `account_id` (scope to one
  account), `amount_min_cents` / `amount_max_cents` (an inclusive SIGNED
  integer-cent range, negative = money out), and `memo_contains` (a
  case-insensitive substring). `keywords.match_rule(desc, rules, *,
  context=None)` still matches the keyword whole-token, longest-first (so "CAT"
  never matches "CATERPILLAR" and "AMAZON WEB" out-ranks "AMAZON"); when the
  caller supplies transaction `context` (a mapping/object exposing `account_id`,
  `amount_cents` and/or `memo`), a candidate must ALSO satisfy every non-NULL
  condition (`_conditions_hold`, ANDed together), and a rule that fails one is
  skipped so a narrowly-conditioned keyword falls through to a broader rule.
  **`context=None` — the default, and what every shipped import caller passes —
  consults no conditions and is byte-identical to the prior keyword-only
  match**, so nothing changes on the import path until a caller opts in by
  passing context.

- **A set condition the context cannot confirm fails closed.** If a rule is
  scoped but `context` has no `account_id` (or it is `None`), the rule does not
  fire — a scoped rule never classifies a row it cannot positively place.
  Amounts compare as signed cents against the inclusive bounds, so a "-$120.00
  or more out" rule is `amount_max_cents = -12000`. `normalize_conditions` casts
  the integer columns (a stray Decimal/str cannot poison the row) and collapses
  a blank `memo_contains` to NULL, so an empty substring can never degrade into
  an always-false condition.

- **The domain layer forwards conditions but never invents them.**
  `load_rules` / `list_rules` / `get_rule` select the four columns;
  `apply_rules` / `match_rule` take and forward the optional `context`;
  `upsert_rule` accepts the four conditions, each defaulting to `None`. On a
  fresh row all four are written (`None` → NULL); when re-teaching an existing
  keyword only the conditions explicitly supplied (non-`None`) are changed, so
  learning a keyword again never silently wipes a condition the manager set.
  Clearing a condition back to NULL is the editor's job, not upsert's — delete
  and re-add the rule for that. `transfer_rules` gained `list_rules` /
  `get_rule` / `delete_rule` for parity with `category_rules`, and `categorize`
  gained `list_mappings` / `forget_mapping` so the learned payee → category
  table is inspectable and prunable too.

- **The Rules Manager is a thin projection** (`ui/rules_manager_widget.py`, a
  `QDialog` opened from **Tools ▸ Rules Manager…**, mirroring `budget_widget`).
  It holds no SQL and no money logic; every read and write goes through the
  domain layer, and amounts are rendered/parsed only through the shared
  `ui.models.fmt_cents` / `parse_amount` chokepoints and stored as signed
  integer cents like everywhere else. A `QTabWidget` carries three tabs:
  - **Category Rules** and **Transfer Rules** — one row per keyword rule.
    Keyword and target are display-only (fixed when the rule is first learned);
    the four conditions are inline-editable: the **account scope** through a
    combo delegate where a blank choice means *all accounts* (NULL `account_id`),
    **min** and **max** through `MoneyDelegate` as signed integer cents, and
    **memo** as free text. Each edit saves through `upsert_rule` with
    **`compound=True`** — the non-compound path would re-run `extract_keyword`
    and collapse a multi-word keyword into a *different* rule, so an in-place
    condition edit must keep the existing keyword verbatim.
  - **Learned Payees** — a read-only list of the `import_mappings` (normalized
    payee → category, §5.5) with a **forget** action (`categorize
    .forget_mapping`); it is inspect-and-prune only, since the mapping itself is
    (re)learned from the next correction.
  - Deletes on every tab route through the `QMessageBox.question` seam the tests
    patch, never a bare `exec_()` modal, so the dialog terminates under the
    offscreen platform.

- Regression: `mammon/tests/test_rule_conditions.py` (matcher plus the domain
  forwarding and re-teach preservation) and `mammon/tests/test_rules_manager_ui
  .py` (the dialog as a thin projection, including the delete seam).

### 5.10a Register chrome and entry feedback
- Account actions (Details, Reconcile, Import, Download, Review, Loan Setup,
  Hide) live in a **gear menu** at the right of the account-name line, not on a
  toolbar row — in the investment register too, where the gear additionally
  carries **Get Quotes** (§5.5c). Every one of them opens a dialog, and a row of buttons that only
  lead elsewhere is a poor trade for register height. `AccountToolbar` still owns
  the actions and their account-id wiring; it is simply never shown.
- The **one-line/two-line choice is per account** (`ui/prefs.account_view_mode`,
  QSettings). The layout that suits a register depends on the account: a card
  whose rows carry long statement descriptions wants the second line, a
  hand-entered checking account does not. Settings sets the DEFAULT; an account
  that chose from its gear menu keeps its choice when the default changes.
- **Display Preferences (Settings) are QSettings, never DB, and theme-sensitive
  colors are remembered PER THEME.** Font family/size, the date display format,
  row-shading on/off and the two-line default mean the same thing in light and
  dark, so they are stored globally. But the **alternate-row shade** and the
  **negative-amount color** are legible only against their own background, so
  each theme keeps its own slot (`display/theme_colors/<theme>/<role>`) and
  `ui/prefs.alt_row_color()` / `negative_color()` read the slot of the currently
  active theme, falling back to that theme's palette value (`style.palette_for`)
  when unset. A single shared key was the reported defect: a color picked in dark
  mode overwrote light's (and vice-versa), so switching themes back and forth
  lost the alternate-row color. A pre-namespacing single-key value is migrated
  into the active theme's slot on first read, so an existing user loses nothing
  and the pick lands in the theme it was chosen under rather than leaking into
  the other. A color chosen in the same Display-Preferences change that also
  switches the theme belongs to the newly chosen theme (the dialog has already
  reseeded its swatches to that theme), and the dialog's theme combo reseeds each
  swatch from that theme's remembered pick so toggling it can never clobber the
  other theme's saved color.
- The accounts list gives **credit cards their own heading**. Quicken separates
  them only by ordering, leaving the boundary implicit; the order still matches
  (cards directly below Banking).
- A **transaction-accepted sound** plays on save, switchable in Settings. It is
  driven by `RegisterModel.transactionSaved`, which fires only when a write
  actually altered or created a row — NOT by `committed`, which also fires for a
  no-op rewrite.
- **One sound per TRANSACTION, not per field.** The register saves per field:
  editing an existing row commits each cell as its editor closes, so tabbing
  across four fields is four database writes to one transaction. The unit the
  user works in is the transaction, so `transactionSaved` carries the txn id and
  the register coalesces on it, sounding when the user is done with that row —
  on Enter, or on moving to another row. A brand-new row commits as a whole and
  needs no coalescing. **Enter on an existing row is the accept gesture**: it
  commits any open cell editor and acknowledges the transaction once. (It does
  not perform the write; the row is already saved.)
- The sound also fires from the **review list** (`ImportReviewPanel
  .transactionSaved`) for a MATCHING row accepted or a NEW row saved, and not for
  a discard. Review is the high-volume accept gesture and where the confirmation
  is worth most; `changed` is not a substitute, as it also fires for edits,
  visibility switches and discards. Qt commits an editor on focus-out whether or not it was touched,
  so a chime keyed off "an edit was committed" would fire for clicking a cell and
  clicking away, and a confirmation that fires on nothing is one you stop
  hearing. The waveform is synthesized into the data dir on first use rather than
  shipped as a binary blob; playback degrades silently with no audio backend.
- Reconcile panes **gray a row once it is cleared**. The Clr mark is at the far
  left and the amount at the far right, so with two identical amounts it is easy
  to re-click the row already cleared. Dimming answers "have I done this one?" at
  the amount itself.

### 5.5a Category entry
- A category is completed from any **unambiguous fragment**
  (`ui/delegates.resolve_category_input`), in order: exact path, exact LEAF name
  (`fuel` → `Auto:Fuel`), path prefix (`land` → `Landscaping`), leaf prefix. Each
  step requires a unique winner; ambiguity resolves to nothing and falls through
  to the existing typo guard, which is what keeps that guard useful for genuinely
  new categories. Before this, typing `land` for the sole Landscaping category
  prompted "Create new category 'land'?" — the guard firing on exactly the
  gesture it should have completed.
- **A split line's category widget is the register's**, built by the same
  `make_category_combo`. It was a bare combo with no completer at all, so typing
  habits learned in the register silently did not work one dialog away.
- **The completer popup matches on the subcategory name too**
  (`CategoryCompleter` / `category_completions`). Qt's default filter is "starts
  with" against the whole `Parent:Child` path, so typing a leaf produced an EMPTY
  popup — nothing begins with `fuel` — and the subcategory was unreachable by its
  own name. The list is the ordered union across match tiers (exact path, exact
  leaf, path prefix, leaf prefix, substring), so the highlighted entry is what
  the fragment almost always means.
- **Enter/Tab takes the first offering; focus-out does not.** A deliberate commit
  with the popup on screen resolves an ambiguous fragment to the highlighted
  entry (`accept_category_text(take_first=True)`). Focus-out chose nothing, so
  only an unambiguous fragment is accepted — guessing there would categorize a
  transaction because a click landed elsewhere, the same failure `MoneyDelegate`
  prevents for amounts.
- An **ambiguous** fragment reaching commit anyway offers the matches to choose
  from (`category_matches`), never "create a new category". A real ledger has both
  `Auto:Fuel` and `Moving:Fuel`, so `fuel` is a question — and offering to create
  a third category called `fuel` is a wrong answer to a question the user did not
  ask.
- **Any dialog raised from a delegate's `setModelData` MUST be deferred** one
  event-loop turn (`QTimer.singleShot(0, …)`) and parented to the VIEW, never the
  editor. `setModelData` runs while Qt is destroying the editor; a modal there
  opens a nested event loop that lets the teardown finish and frees the editor
  the frame still holds. The observed result was a silent heap corruption
  (`0xc0000374`) with no Python traceback — the same class of failure
  `RegisterModel._write` defers its reload to avoid. The deferred write goes
  through a `QPersistentModelIndex` and drops a row that vanished meanwhile.
- **Tab selects the field, a click places the caret.** Opening a classification
  editor (Category, Payee, Memo, Tag) by Tab or the keyboard SELECTS ALL its text,
  so the first keystroke REPLACES the highlighted value — matching the
  click-to-edit path, which select-alls too. The ONE case that appends is a mouse
  click, which lands the caret where the user pressed. The choice is a pure function
  of the Qt focus reason (`ui/delegates.select_all_on_focus`), applied on focus-in
  by `_FocusSelectLineEdit` (and, through it, `CategoryLineEdit`), so it is testable
  headless. Before this, Tabbing into a pending review row's field left the caret at
  the end and the first key APPENDED — disagreeing with a click on the same field,
  the same "typed into a pre-filled field without selecting first" hazard §5.11
  removed for statement dates.

### 5.10b Dates: one format, everywhere
- A single **date-format preference** (`ui/prefs.date_format`, one of
  `MM/DD/YYYY`, `DD/MM/YYYY`, `YYYY-MM-DD`) governs every date the user sees or
  types — registers, dialogs, report filters, printed output and chart axes.
- Two chokepoints, and no date may bypass them:
  - `ui/models.fmt_date` — stored ISO → the chosen DISPLAY format.
  - `ui/models.parse_date` — anything typed (or a QDate) → ISO `YYYY-MM-DD`, or
    `""` when it is not a date. ISO is always accepted, since it is the storage
    format and what download scripts return. A two-slash date is genuinely
    ambiguous (`03/04` is March 4th or April 3rd depending on where you live), so
    the PREFERENCE decides — that is what the preference is for. Separators may
    be `/ - .`; two-digit years map into 1970-2069.
- **Storage, the ledger validators and every download script speak ISO.** The
  conversion happens once, at the parse chokepoint, never at the call site.
- **Every date field offers both entry routes**: typing and a calendar picker.
  They are all built by `ui/delegates.make_date_edit`, read back through
  `date_edit_iso`. A `QDateEdit` cannot hold a non-date, so nothing downstream
  has to defend against one.
- **An unbound date field opens on TODAY and is typeable.** With no initial value
  `make_date_edit` sets the widget to the current date, never the Qt sentinel
  minimum (1752-09-14) — which rendered as blank AND refused keystrokes, so a
  field defaulted to it looked broken. A `blank_ok` field also opens on today; the
  sentinel is reached only when the user clears the field, which `date_edit_iso`
  reports as `""`. The one deliberate exception is a field that must be blank ON
  OPEN — a date FILTER, where today would hide all history — which opts in with
  `setDate(edit.minimumDate())` after building; an ENTRY field never does.
- Changing the preference **reaches windows already open**:
  `ui/delegates.refresh_date_format` re-stamps every date editor under the main
  window, and displayed dates re-render through `fmt_date`.
- This was the inconsistency the preference was supposed to remove. Each site had
  hardcoded its own answer — the register editor said `MM/dd/yyyy` while report
  filters, scheduled payments, the reconcile setup and the download date range
  all said `yyyy-MM-dd` — and the transaction dialog, the investment dialog and
  the loan wizard took dates as FREE TEXT that only accepted ISO, with no picker
  at all. Choosing `DD/MM/YYYY` changed the register and nothing else.
- **Tabular date cells follow the preference too.** After the loan wizard's scalar
  date fields adopted `make_date_edit`, its rate-history and extra-amount TABLES
  still carried each effective date as raw ISO text in a bare `QTableWidgetItem`,
  bypassing both chokepoints. Every effective-date cell is now a `make_date_edit`
  editor (calendar-pickable, in the chosen format), read back through
  `date_edit_iso`; storage and the amortization domain stay ISO.
- **The new-account dialog's opening date is a date editor too.** Its optional
  opening-date field was the last holdout — a bare `QLineEdit` with a hardcoded
  `YYYY-MM-DD` placeholder read raw, so it ignored the preference and only accepted
  ISO. It is now `make_date_edit(blank_ok=True)` read through `date_edit_iso`, like
  the reconcile setup: the DISPLAY honors the setting while the stored/returned
  value stays ISO `YYYY-MM-DD`. It **opens on today** and is immediately typeable;
  the first `make_date_edit(blank_ok=True)` port pinned it to the sentinel minimum,
  which rendered blank and refused input, so the field looked broken. `blank_ok` is
  retained only so the user can clear the field back to `None`; the visible default
  is today.

### 5.11 Reconcile against a statement
- Two-pane workspace (debits left, credits right) after a setup dialog that
  collects the statement figures. Only rows dated ON OR BEFORE the statement date
  appear in either pane.
- **"Cleared balance" is the sum of the items CHECKED in the reconcile window,
  and nothing else** (user-stated definition). It previously showed
  `beginning + checked`, so a card whose history arrived from Quicken already
  carrying `R` displayed thousands of dollars "cleared" with every box unchecked
  — pressing Clear All did not move it.
- **A credit-card reconcile is self-contained and never consults the register's
  reconciled history.** The setup dialog asks for the totals the card statement
  prints — charges & cash advances, payments, credits, ending balance owed, plus
  a finance-charge box — and NOT a beginning balance. A statement is closed
  arithmetic:

      previous + charges + finance − payments − credits = ending

  so the previous balance is recoverable from the rest
  (`ledger.implied_card_beginning`). That implied figure is displayed and is what
  the checked items are measured against. It is deliberately NOT validated
  against the sum of the account's `R` rows: if the two disagree, the statement
  wins, because the statement is the document being reconciled. Quicken behaves
  this way, and it is the only way a card with imported history can be reconciled
  at all — deriving the beginning from `R` flags opened the window thousands of
  dollars adrift with no in-dialog correction.
- The finance-charge box defaults to blank. Institutions now list the finance
  charge on the statement as its own transaction, so it arrives with the download
  and is checked off like any charge; filling the box in posts one (idempotently)
  as the fallback for an institution that omits it.
- **Bank/asset accounts work the opposite way, deliberately.** There the running
  balance is the thing being protected and must carry forward unbroken from the
  last reconcile, so the reconcile is anchored on BOTH endpoints: the beginning
  comes from opening + already-reconciled rows, and the ending from the
  statement. The typed beginning is a CHECK against the register's, not an
  override — a disagreement means previously-reconciled history moved, which is
  surfaced in the summary but never blocks Finish. (It was previously collected
  and discarded, hiding exactly that signal.)
- The asymmetry is not an accident of Quicken's UI. A bank balance rules supreme
  and its history must stay continuous. A card holder cares about what is owed
  now, not the history — statements carry more line items than many people will
  track to the penny, and it is normal to stop reconciling a card for months and
  pick it back up needing only the latest statement. Anchoring a card on the
  register's history would make that resumption impossible.
- **No reconcile asks for a start date.** The window is everything not yet
  reconciled through the statement ending date — past uncleared items included,
  which is the point: they are still outstanding and still need to be found. The
  cleared sum uses that same single bound, so it matches the panes exactly; rows
  cleared after the statement date are reported separately as held for the next
  statement. (A start date was briefly added on a misdiagnosis and removed in
  migration 32 — see the note there before reaching for one again.) `finish_reconciliation`
  applies that SAME cutoff to its guard and its UPDATE, so the set that zeroed
  the difference is exactly the set that gets locked — reconciling January must
  never silently reconcile a checked-off March payment.
- **Statement dates are entered through a calendar control, never free text.**
  Every bound is a string comparison on ISO text, so a malformed or mistyped date
  does not fail — it sorts past every real row and silently widens the statement
  to months that are not on it. A live ledger stored `2026-03-152025-11-04` (typed
  into a pre-filled field without selecting first) and reported four payments from
  other months as cleared; a wrong YEAR does the same thing while looking
  perfectly valid. The workspace additionally drops any unparseable date to blank
  rather than passing it to a comparison.
- **An investment statement's own cash activity is an INVESTMENT row.** OFX
  models brokerage interest and account fees as `<STMTTRN>` inside
  `<INVBANKTRAN>`; taking that literally (an STMTTRN is a cash row) made the
  destination table depend on the FILE FORMAT — the same interest payment landed
  in `investment_transactions` from a broker's CSV and in `transactions` from its
  QFX, so the two never deduped and importing both formats over one period
  duplicated every interest and fee row and double-counted the account's cash.
  `TRNTYPE` is a fixed enumeration from the OFX spec (not an institution's own
  vocabulary), so translating the unambiguous values — `INT`→IntInc, `FEE`/
  `SRVCHG`→MiscExp, `DIV`→Div, `XFER`→XIn/XOut by sign — is reading the format,
  not guessing at wording. Anything else keeps its raw `TRNTYPE` for review and
  the learned action tree. A plain bank/CC `STMTTRN` is unaffected.
- **Investment dedup near-matches across export formats.** Different exports of
  the same broker spell the same security differently — a Flex/QIF history
  stores `ALTY GLOBAL X SUPERDIVIDEND ALTER`, the activity CSV sends `ALTY` —
  so exact `(date, symbol, quantity, amount)` identity could never bridge a
  format change and a re-download classified a whole year of held dividends as
  NEW. After fitid and exact identity, `_find_investment_match` accepts same
  amount + date within the dedup window + same leading TICKER token (or both
  rows security-less: interest, fees). A row with a security never matches one
  without, so a fee cannot swallow a same-amount dividend; the claimed-id set
  keeps N identical lots matching N distinct rows.
- Statement inputs PERSIST while a reconcile is unfinished (`reconcile_drafts`,
  Section 4). Closing the window to check something in the register and reopening
  it returns to the workspace with the figures intact and skips the setup prompt;
  `Balances…` still edits them. This also carries the posted finance charge's
  transaction id, so a reopened reconcile updates that charge rather than posting
  a duplicate.

### 5.11a Audit log of changes to reconciled transactions
A reconciled transaction should rarely be changed. Once a row is locked in
against a statement, editing its amount or date, un-reconciling it, or deleting
it outright silently throws off the NEXT reconcile of that account, and the
failure then surfaces far from its cause. So every such change is recorded, on a
per-account basis, to make that class of problem easy to diagnose after the fact.

- The store is `reconciled_change_log` (Section 4, schema v63). It is written
  ONLY from `mammon/ledger.py` -- the sole writer of transaction rows -- so the
  same choke point that enforces the transfer invariants records the audit. The
  edit path (`_apply_update`, through which `update_transaction`, `void` and
  `replace` all pass) logs one row per changed field on a transaction whose
  `reconciled` flag was already set; the delete path (`delete_transaction`) logs
  one row per surviving value field of a reconciled row it removes. Both legs of
  a reconciled transfer are audited, each in its own account's log.
- What is captured per row: the account, the transaction id, an ISO timestamp,
  the operation (`edit`/`delete`), the field name, and the old and new values as
  TEXT (amounts as signed cents, stored verbatim -- the log carries no money
  logic). Only fields that affect a reconcile are tracked (date, num, payee,
  category, memo, amount, and the cleared/reconciled flags for edits); internal
  ids are excluded. Editing an UN-reconciled transaction, or reconciling a row
  for the first time (a 0 -> 1 transition), logs nothing.
- The read side is `ledger.reconciled_change_log(conn, account_id)` (oldest
  first). The viewer is a strictly read-only dialog reachable from the account's
  Details window ("Reconciled change log…"); like all of `ui/` it holds no SQL
  and no money logic, rendering the stored strings as-is.

### 5.12 Budgets (roadmap item 6)
Named per-category monthly spending targets, so "did I overspend Groceries in
March" and "where do I stand year-to-date" are answered against the very ledger
the register writes. The data model lives in Section 4 (`budgets` /
`budget_lines`, schema v39); the domain layer is `mammon/budgets.py`, the
multi-period roll-up `mammon/reports/budget.py`, the read-only tool surface
rides the MCP server (§7.3), and the editor is `mammon/ui/budget_widget.py`,
reached from **Tools ▸ Budgets…**. Budgets are app-generated and never imported.

- **A budget is a named envelope set; a line is one target per (category,
  month).** `budgets(id, name, active)` toggles a whole plan on or off without
  deleting it; each `budget_lines(id, budget_id, category_id, period ['YYYY-MM'],
  amount_cents, rollover)` pins one month's target for one category. Keeping the
  target per-month rather than a single annual figure lets a plan bend around a
  known lumpy month — a quarterly insurance premium, a December of gifts —
  without inventing a row per day. `UNIQUE(budget_id, category_id, period)` is
  the upsert target, so re-setting a line's amount rewrites the one row instead
  of accreting duplicates (`budgets.set_line`, an `INSERT ... ON CONFLICT DO
  UPDATE`). Foreign keys cascade, so deleting a budget — or a category — cannot
  orphan lines. `rollover` is **consumed**: for a line with the flag set, the net
  remainder of its prior rollover periods (`budgeted − actual`, summed) is carried
  into this period's available amount, so an underspend flows forward as extra
  headroom and an overspend eats into the next month. The carry is compared
  chronologically by the ISO `'YYYY-MM'` period string — which sorts as text in
  calendar order — so it crosses the December → January boundary with no reset
  (the year-boundary case Quicken keeps getting wrong), and it is exact
  integer-cent addition (no division, so ROUND_HALF_UP is honoured trivially). A
  `rollover = 0` line is unaffected: its carry is 0 and its remaining stays
  `budgeted − actual`. Per-line gating: a prior month whose own line does not roll
  over contributes nothing to a later rollover month's carry.

- **Actuals are never stored.** A budget records only intent; what was actually
  spent is derived read-only from the ledger, so there is no second write path
  into transaction rows and `mammon.ledger` stays the sole writer.
  `budgets.budget_vs_actual(conn, budget_id, period)` reuses
  `reports.spending.spending_by_category` for the month and subtracts, so there
  is exactly one definition of "spending" — transfers excluded, splits honoured,
  own (directly-booked) spend as a positive magnitude, so a parent and its child
  are never double-counted. Each row is a `BudgetActualRow` (`category_id`,
  `category_name`, `budgeted_cents`, `actual_cents`, `carried_in_cents` [the
  rollover remainder from prior periods, 0 for a non-rollover or unbudgeted
  category], `remaining_cents = budgeted + carried_in − actual`, negative =
  overspent); with `include_unbudgeted` on, a
  category spent but not on the plan appears with `budgeted_cents = 0`. The rest
  of the domain API is plain CRUD over the two tables — `create_budget`,
  `list_budgets`, `get_budget`, `rename_budget`, `set_active`, `delete_budget`,
  `set_line`, `get_lines`, `delete_line` — all in signed integer cents, UI-free.
  (`reports.spending` is imported lazily inside `budget_vs_actual` to break a
  package import cycle, `budgets → reports.spending → reports.budget → budgets`.)

- **A plan is read across a span, not just one month.**
  `mammon/reports/budget.py` is pure and read-only like the rest of
  `mammon/reports/`: it stacks the single-period primitive across a range and
  sums it. `budget_vs_actual_range(conn, budget_id, start_period, end_period)`
  runs `budget_vs_actual` once per ISO month in `[start, end]` inclusive;
  `budget_vs_actual_ytd(conn, budget_id, year, through_month=…)` pins that span
  to January through a month. The report never touches transaction rows — it
  only adds cents another function already computed, so summing across periods is
  integer addition with no rounding and no unit change. It returns three views of
  the same numbers (`BudgetRangeReport`): `category_totals` (each category summed
  over the whole span), `period_totals` (each month's own budgeted/actual/
  remaining), the grand totals, and `by_period` keeping the untouched per-month
  rows for a caller that wants the detail. A category present in only some months
  contributes 0 to the rest, so the totals still align. Rollover rides through
  for free: the per-period primitive already consumes each line's carry, so a
  year-to-date span picks up the remainder carried across the January boundary. A
  category total's `carried_in_cents` is its **opening** balance — the carry into
  the first period it appears in, which may predate the span — recorded once so it
  never double-counts, and its `remaining_cents = carried_in + budgeted − actual`
  is the envelope's closing balance at the end of the range.

- **The read-only MCP surface (§7.3) gains `list_budgets`, `budget_vs_actual`
  and `budget_ytd`.** `list_budgets` names the plans; `budget_vs_actual(budget_id,
  start, end=None)` reports a single month or an inclusive `YYYY-MM` range;
  `budget_ytd(budget_id, year, through_month=12)` reports year-to-date. Each
  returns the range report as JSON with money rendered as decimal dollar strings,
  and, like every tool, reads through the `query_only` connection — a budget
  question can never write. The model is told once (`mcp_tools.INSTRUCTIONS`) to
  start with `list_budgets`, then `budget_vs_actual` (a month or range) or
  `budget_ytd` (year-to-date).

- **The Budgets panel is a thin projection** (`ui/budget_widget.py`, a `QDialog`
  opened modally from **Tools ▸ Budgets…**). The user chooses or creates a named
  budget, toggles it active, deletes it (confirmed through `QMessageBox.question`
  so it is safe headless), picks a month, and adds categories to the plan. The
  table shows one row per category — **Category, Budgeted, Carried, Actual,
  Remaining** — where **Carried** is the rollover remainder brought in from prior
  periods (0 unless the line rolls over) and only **Budgeted** is editable; a
  typed target writes straight through `budgets.set_line` and the
  Carried/Actual/Remaining columns refresh live from `budget_vs_actual` (the
  footer summary shows the carried total only when it is nonzero). The panel holds
  no SQL and no money logic of its own, and
  because budgets never touch transaction rows, closing it triggers no register
  refresh. The month control is built with `make_date_edit`/`date_edit_iso` and
  the new-budget name prompt is an overridable seam, per the app's date and
  headless-modal conventions.

## 6. Import strategy (Quicken 2017 -> Mammon)

### 6.1 Can we read Quicken's native file directly? (answering Q3)
User direction: TRY BOTH a direct-from-Quicken read and the QIF export path,
and let whichever reconciles win.
- Direct read (spike): MoneyDance can "import from Quicken", which is evidence a
  usable path exists, so we will spike it. Caveat to verify: MoneyDance's Quicken
  import is (as far as is publicly documented) driven by Quicken EXPORT formats
  (QIF/QMTF), not a raw read of the binary .QDF. The .QDF (plus auxiliary
  .QEL/.QSD/.QPH) is a proprietary, undocumented compound-document container with
  no public spec or maintained parser, so a raw-QDF read is high-risk for a
  40-year archive. The spike's job is to confirm what actually works on the
  user's real file before committing.
- QIF export (workhorse): high probability of covering the full 40-year file;
  Quicken now exports all account types to QIF. This is the reliable path and the
  default the parser is built against.
- Decision rule: build the QIF importer first, run the direct-read spike in
  parallel, and keep whichever reconciles per account (Section 6.2 acceptance).

### 6.2 Recommended path: export from Quicken, import into Mammon
Quicken can export its own data; we import that. Options and their limits:
- QIF (Quicken Interchange Format): Quicken can now export ALL account types to
  QIF. Best coverage for a full-history dump, BUT: transfers appear on both
  accounts (must dedup the mirror), investment transactions can be lossy, and it
  is single-currency per file. We will handle transfer-dedup and investment
  quirks explicitly.
- QXF (Quicken Transfer Format): Quicken's own migration format, cleaner for
  cash/categories/scheduled txns, BUT does NOT reliably carry investment or
  business accounts - so it cannot be the whole story for a 40-year file with
  investments.
- Plan: use QIF for the bulk one-time migration (all cash + investment
  accounts), treat investment accounts with extra care, and reconcile the
  resulting Mammon balances against Quicken's reported balances per account as
  the acceptance test. If specific accounts import poorly via QIF, fall back to
  a per-account QXF or CSV export for those.
- User to provide a representative sample export (a few accounts incl. one
  investment) so the parser is built and validated against real data.

### 6.3 Ongoing import formats
- QFX/OFX: the standard bank/CC/investment download format (SGML/XML). Direct
  parser -> normalized records. Preferred for institutions that offer it.
- CSV: per-institution CSV (e.g. Fidelity) where QFX is unavailable.
- Crypto CSV: a block-explorer by-address native-coin export (Etherscan shape)
  imported onto a `type='crypto'` wallet-account via its own parser + core
  (`importers/crypto_csv` + `importers/crypto_core`), with the on-chain `tx_hash`
  as the exact-dedup key. See Section 5.8i for the import rules (gas attribution,
  historical FMV, own-wallet transfer detection).
- JSON: canonical schema for webSlinger-scraped sites (Section 7.2).

### 6.4 Normalized import record (all parsers converge here)
Cash transaction:
```
{ "external_account": "<institution account id or name>",
  "date": "YYYY-MM-DD",
  "amount_cents": -12345,           # signed; negative = money out
  "payee": "SAFEWAY #123",
  "memo": "",
  "category": "Groceries",          # optional; auto-categorize fills if absent
  "check_number": "",
  "fitid": "<unique id from source, for dedup>",
  "type": "" }
```
Investment transaction adds: action, symbol, quantity, price, commission.

## 7. Download / institution list (Q4)

### 7.1 Institutions to support
There is deliberately NO central institution registry. Each account carries its
own recorded webSlinger script name (`accounts.download_script`) and that
script's saved inputs (`accounts.download_config`), both set in Account Details,
where the script's declared inputs drive the form. Adding an institution is
recording a webSlinger script and naming it on the account. Whether a run was
EXPORT (the site dropped a file) or SCRAPE (the script returned rows) is decided
from what the run actually returned, never from a table. An early registry that
pre-listed institutions with a mode and format per entry was removed
(2026-09-01): nothing on the download path consulted it, and it duplicated
per-account state.

Approach (user direction): webSlinger INITIATES every download so the user never
has to log in and click Export by hand. A webSlinger script per institution does
one of two things and hands the result to Mammon:
1. Drive the site's own export button -> download a QFX/OFX/CSV file, which Mammon
   then imports. Preferred WHEN the site's export covers a useful date range.
2. Scrape the on-screen transaction list -> JSON in the canonical schema
   (Section 6.4). Used when there is no export, OR when the export is
   range-limited. (Real example: Wells Fargo's official download capped at ~6
   months, so we scrape the transaction list that shows a full year instead.)
Per institution, one at a time: pick option 1 or 2, then request the webSlinger
recording (kind="user" task) with the full target_url / task_description / goals
spec. Even "direct download" institutions still get a webSlinger script (for
option 1) so downloading is one-click/automatic from Mammon.

Runtime dependency: this makes webSlinger a required companion for downloads.
Accepted by the user -- webSlinger has a $2/month execute-only tier that runs
already-recorded scripts, which is the intended cost for Mammon users after their
institution scripts exist. Mammon itself (ledger, import of files, reports) works
with no subscription; only the automated download step calls webSlinger.

### 7.2 Utilities (separate from the main Mammon program)
A companion utility set (not the core ledger app):
- Venmo, PayPal: capture the real counterparty. These get their own *account*, so
  money moving through them is an ordinary transfer and the real payee lives on
  that account's register rows (see 5.4).
- Amazon Orders (`mammon/amazon_invoices.py`, IMPLEMENTED): enrich the blank
  `Memo` column of a Chase card CSV with the items bought, from a webSlinger
  invoice scrape. Run as a CLI before import; it writes a new CSV and never
  touches the database, so `mammon.ledger` stays the single writer.

  Locked decisions, each established against real data:
  - **No join key exists.** The bank descriptor's trailing token
    (`AMAZON MKTPL*5O9NO1XM1`) is a card-network code, not the order number.
    Matching is exact-amount plus a date window, one-to-one and consuming both
    sides so a repeated amount cannot reuse one order.
  - **Match on the invoice's `Grand Total`, not `orderPrice`.** They diverge on
    exactly the orders partly paid by gift card or rewards, and the charge is what
    the statement shows. Two scrape schemas are read: the newer carries the invoice
    accounting block as `priceAccounting` (`Item(s) Subtotal:`, `Gift Card Amount:`,
    `Rewards Points:`, `Grand Total:` ...), the older a flat `invoicePrice`.
    **`Grand Total` is already NET of gift card and rewards** - subtracting them
    again under-counts every partial order. Verified: `Grand Total` reproduced
    `invoicePrice` on all 136 orders the two scrape files share, no exceptions.
  - **Match on `Transaction Date`, not `Post Date`** - it sits closer to the
    order date and is worth ~13 extra matches on a single statement.
  - **The date window is DIRECTIONAL, 0..21 days from order to charge.** Amazon
    bills at ship time, which never precedes the order. Observed lag is 0-13 days
    and wholly one-sided; every exact-amount pairing on the negative side was 28+
    days out, i.e. a coincidental amount collision. A symmetric window admits
    those and lets an old collision consume the order its true charge needed.
  - **Filter by card**: the invoice feed covers every card on the Amazon account.
  - Orders charged $0.00 (gift card / rewards) are INSERTED as $0.00 rows, since
    the order happened and a zero amount disturbs no balance. Inserts are
    count-aware per date so re-running on the output does not duplicate them.
    `--verbose` prints each one's `[Amazon Gift Card]` / `[Amazon Rewards]` offset
    so the manual split is transcription rather than a hunt back through Amazon.

  Known gaps, both characterized against a real statement (86 of 113 purchase
  rows matched; 20 of the 27 misses were simply outside the scrape's date range):
  - **Splits.** CSV cannot express them, so the offsetting `[Amazon Gift Card]`
    leg the user would normally enter is not reconstructed. QIF *can* carry it
    (`importers/qif.py` already parses `S[Account]` split legs into
    `NormalizedTxn.splits`), so a QIF-emitting mode is the natural upgrade once
    the scrape captures payment method per order.
  - **Multi-shipment orders.** There is ONE invoice per order; the split is on the
    payment side, since Amazon bills at ship time. So one order's total can equal
    the SUM of several charges and match none of them individually. On the sample
    statement this was the entire remaining in-coverage gap: two unused orders
    decomposed exactly into four unmatched charges ($243.36 + $100.13 = $343.49;
    $82.16 + $7.51 = $89.67).

    RESOLVED without more scraping. The 2026-08-29 re-record added
    `priceAccounting` - the invoice's ACCOUNTING block (subtotal / shipping / tax /
    gift card / rewards / grand total), not its payment block, so per-shipment
    charges are still absent from the JSON. But the accounting block is enough to
    reconstruct them, because a shipment is billed for its own items plus that
    invoice's tax:

      charge  ==  sum(items in that shipment) x (1 + (grand - subtotal) / subtotal)

    A second pass therefore runs on the leftovers only, and commits only when BOTH
    subset searches have a unique answer: the charges must sum to the invoice
    total (groups of two or more; a group of one is just an exact match), and the
    items must then partition across those charges with every item used exactly
    once. The complete-partition requirement is what makes this safe rather than a
    guess - a coincidental subset almost never leaves a remainder that covers the
    remaining charges exactly. Ambiguity is reported, never resolved by picking.
    Disable with `--no-split-shipments`; recovered rows are counted separately in
    the report so an inferred memo is never mistaken for an exact one.

    Verified on the sample statement: all three split orders recovered, six charges
    given their own items, zero orders left unused.

    Note when reading that page: an item shipped in parts prints once per shipment
    ("1 of 6", "2 of 6" ...), each line repeating the price of the whole lot.
    Those rows are real, not a scraping fault. **Never sum item prices to
    reconcile an invoice** - the invoice total is authoritative, and this module
    matches on `invoicePrice` alone for exactly that reason.
  - **Refunds** are matched only in a provable subset. The invoice carries a
    `Refund Total`, but a credit is far harder to attribute than a charge: a
    refund returns ONE item, so there is no complete-partition constraint to
    catch a wrong pick, and the invoice's blended tax rate does not hold per item
    (a real 5-item order blends to 6.64% while the returned item was taxed at
    7.45%, missing by 14c). So the pass commits only where the order holds exactly
    one item and its `Refund Total` equals the credit exactly - then no arithmetic
    is needed and no ambiguity exists. Memos it writes are prefixed `Return: ` so
    a credit never reads like a purchase. Refunds also need their own much wider
    window (54 days observed, against 0-13 for charges): `--refund-window`,
    default 180. Disable with `--no-refunds`.
  - Subscription and membership charges (Prime fee, Prime Video) are not orders
    and will never appear in the invoice feed.
  - Orders placed on a DIFFERENT Amazon account are invisible to a scrape of this
    one. The tool filters by card, not by account, so pointing it at the other
    account's scrape picks them up.

- Amazon invoice ITEMIZATION (`mammon/importers/amazon_items.py` phase-1 core +
  `mammon/import_review.py` phase-2 review integration, both IMPLEMENTED; see
  §7.2a for the review/UI wiring). Where §7.2's CSV tool only
  names the items in a memo, this turns one matched card charge into a proper
  SPLIT - one leg per item - so each purchase lands in the right category. It is
  a pure, offline core: it reads the webSlinger invoice report file (the
  time-tagged JSON that lands in the user's Downloads folder, `priceAccounting`
  schema assumed present) ON DEMAND via `amazon_invoices.load_orders` and returns
  plain data structures. **Mammon does not store Amazon data**; the file is
  re-loaded when needed, across sessions. Nothing here writes the ledger - the
  legs it emits reach the register only through the review queue and
  `mammon.ledger`, the single writer, in the later phase.

  Data flow: invoice file in Downloads -> `load_invoice_orders` -> `Order`
  records (cents fields; LIST prices index-aligned with de-duplicated item
  titles) -> `allocate_order` / `allocate_charges` -> `ChargeAllocation`
  (per-item split legs + offsets) -> review rows (§7.2a) -> ledger.

  The proportional-allocation rule (locked; integer cents throughout, no floats,
  `ROUND_HALF_UP` at the cents boundary during money parsing):
  - **One leg per item.** Each item's leg gets its share of the *item portion* -
    the items' cost grossed up by tax - distributed across items by LIST PRICE
    with **largest-remainder rounding** (ties broken by lowest index), so the item
    legs sum to the item portion to the cent. TAX is therefore allocated
    PROPORTIONALLY across the item legs and is never a leg of its own. The item
    portion is `|charge| + gift_card + rewards`, because the invoice `Grand Total`
    (the charge) is already net of the offsets. List prices weight the split but
    are never summed to reconcile it - the charge is authoritative.
  - **Gift card and reward points are CATEGORIES, not accounts.** A partial
    gift-card / rewards payment adds a balancing money-IN leg categorised
    `gift cards` / `reward points`. This is the locked override of the earlier
    design, which modelled the offset as a transfer to a synthetic
    `[Amazon Gift Card]` / `[Amazon Rewards]` account: **no account is ever
    created here.** The offset legs net against the proportionally allocated item
    legs to reach the charge. (Worked example: subtotal $80, tax $6, gift card
    $30 -> charge -$56.00; items list $30 / $50 -> `-32.25`, `-53.75`,
    `gift cards +30.00` -> sum `-56.00`.)
  - **Multiple charges per order.** Amazon bills per shipment, so one order can be
    charged several times; each charge is allocated INDEPENDENTLY from its own
    item subset and is self-consistent to its own cents (`allocate_charges`). Which
    shipment consumed a shared gift card is not in the scrape, so the caller
    supplies the per-charge offset (0 by default); this core does not guess.
  - **Exactness is the invariant.** Every allocation satisfies
    `sum(legs) == charge` in signed integer cents; any residual (only possible
    when list prices are missing and the fallback puts the money on one visible
    `Amazon - unallocated` leg) is folded deterministically so the sum never
    silently breaks.
  - Item legs are left UNCATEGORISED for the user to fill from a dropdown ordered
    by `default_category_order`: the fixed fallback list (`household`, `groceries`,
    `electronics accessories`, `computer accessories`, `electronic hardware`,
    `computer hardware`) first, then the categories the user has historically put
    on Amazon rows (`amazon_categories_from_history`, READ-ONLY). Ordering only -
    it overrides no learned rule and proposes nothing the data has not shown.
  - **Refunds are out of scope** (not on an invoice; handled manually). The
    allocator rejects a credit outright.

### 7.2a Amazon invoice itemization: review integration (phase 2, IMPLEMENTED)

Phase 1 (§7.2) parses an invoice and allocates the split; phase 2 wires that into
the register's import-review flow (`mammon/import_review.py`, the SOLE review-flow
writer) so the item legs actually reach the ledger. Locked behavior:

- **Loading is on demand and never stored (requirement A6).** The cash register
  offers a `Load Amazon Invoices…` action (gear menu, and a button on the
  `ImportReviewPanel` — cash accounts only; investment/crypto registers share the
  panel but hide it). It opens a file picker defaulting to the user's Downloads
  folder — where the webSlinger Amazon script drops its time-tagged report — and
  calls `import_review.build_amazon_review`, which reads the file ON DEMAND and
  returns in-memory review rows. Amazon data is NEVER persisted: the proposed
  per-item split rides on `ReviewEntry.amazon_alloc` (transient), no `review_items`
  row records it, and `persist_entries` neither reads nor writes it. The same file
  can be re-loaded across sessions.
- **Each order becomes one card review row, classified NEW or MATCHING
  (requirement A8).** `_find_amazon_match` mirrors `_find_match`'s date+amount tier
  — same signed cents within +/- `DEFAULT_WINDOW_DAYS`, closest date first — and
  ADDS a payee gate: the register line must already read as Amazon
  (`payee LIKE '%amazon%'`), so an unrelated same-day, same-amount charge is never
  silently itemized. A whole-transaction transfer is excluded (a transfer cannot
  be split), and a line already claimed by an earlier order in the same load is
  skipped.
- **A MATCH updates the existing charge's splits; it never duplicates.**
  `accept_amazon_match` replaces the matched register line's splits/categories with
  the invoice's per-item split via `ledger.set_splits`, leaving its date and amount
  (the user's already-accepted line) untouched, and fills the payee only if it was
  blank. `accept_amazon_new` posts a fresh `Amazon` card charge carrying the split
  via `ledger.add_transaction` + `ledger.set_splits`. A lone-leg order (one item,
  no offset) collapses to a plain categorised transaction, since a split needs two
  legs. Because the accept re-derives nothing from storage, re-loading the file
  after a NEW accept re-classifies the now-existing charge as MATCHING, so a second
  pass updates rather than duplicates.
- **Item legs default to `household` (requirement A1).** `build_amazon_review`
  passes `default_category_order`'s head (`household`) as the item-leg category, a
  sensible default the user can correct; the offsets keep the `gift cards` /
  `reward points` categories from §7.2. The fallback dropdown ordering
  (`household`, then the user's past-Amazon categories) never overrides a learned
  rule.
- **Orders with no card charge create no review row.** An order fully covered by a
  gift-card balance (`charged_cents == 0`) and refunds (out of scope, A7) are
  skipped — there is nothing to reconcile on the card register.
- **No second write path.** `import_review` stays the sole review-flow writer and
  `ledger` the sole transaction writer; every Amazon accept funnels through
  `ledger.add_transaction` / `ledger.set_splits`. The UI stays a thin projection:
  the panel opens the file dialog and calls the domain build/accept functions,
  holding no SQL and no money math, and routes accept to the Amazon path by the
  presence of `amazon_alloc` on the row.

### 7.3 The MCP server: asking an LLM about the ledger (roadmap item 3)
- `python -m mammon.mcp_server [--db PATH] [--transport stdio|streamable-http|sse]`
  serves the ledger over the Model Context Protocol. The tool surface is
  `mammon/mcp_tools.py`: plain functions over a connection returning JSON
  dicts, testable without MCP; `mammon/mcp_server.py` binds them through the
  `mcp` SDK, imported only there, so the app keeps no dependency on it.
- **Read-only, by construction.** The server's connection has
  `PRAGMA query_only` set, so no tool -- and no SQL that turns out not to be a
  SELECT -- can touch the ledger. `mammon.ledger` stays the single writer.
  When the LLM should CHANGE something, the intended path is a proposal into
  the review queue that the user accepts in the app (a later step), never a
  write from the server.
- **Schema must match.** A database older than the code is refused with
  "open it in Mammon once to migrate": nothing that opens a real ledger by
  accident may migrate it (the lesson of the acceptance tests). A newer one is
  refused too.
- **Aggregates first, identifiers never.** The tools wrap the report
  computations (§5.9a), balances, holdings (`holdings` for securities,
  `crypto_holdings` for coin positions -- quantity, cost basis, price, market
  value and gain per coin, plus the cash sleeve and total, with the wallet address
  in `account_number` blanked like every other identifier), upcoming scheduled
  payments and loan schedules; a bounded `transactions` listing, `search` and a
  `query`
  (read-only SQL, with `schema`) cover the long tail, each capped by a
  `limit` with a `truncated` flag. `accounts.account_number`, `url` and
  `download_config` are never returned by any tool: the SQL tool runs under an
  authorizer that blanks those columns and denies every non-read operation.
- Conventions the model is told once (`mcp_tools.INSTRUCTIONS`): amounts are
  decimal dollar strings, negative = money out; dates ISO, ranges inclusive;
  accounts by name or id, hidden accounts excluded unless asked; categories by
  `Parent:Child` path; start with `overview`.
- **Local model option.** stdio is what desktop MCP hosts speak; an
  MCP-capable local client (LM Studio, Open WebUI and the like) launches the
  command and talks to a model on the same machine, so no row leaves it. A
  hosted model is a per-user choice; aggregate-first tools keep what it sees
  small. The HTTP transport exists for clients that connect over a local port.

## 8. Non-functional requirements
- Correctness first: automated tests for balances, transfers, and import dedup.
- Data safety: easy backup (the SQLite file), and an import audit trail so any
  import can be traced and, ideally, undone.
- Performance: 40 years x several accounts must open and scroll smoothly (the
  per-year lazy-load + checkpoint scheme handles this).
- Portable, dependency-light: stdlib + PyQt5 + a small, justified set of
  packages (e.g. yfinance for quotes).

## 9. Architecture
- mammon/ package: db.py (schema/migrations), domain layer (accounts,
  transactions, transfers, balances), importers/ (qif, ofx, json, csv),
  investments (quotes), reports/, and a UI layer.
- Clear separation of DB/domain from UI so logic is unit-testable headless.

### 9.1 Language / UI technology (RESOLVED: Python for everything)
Decision: PYTHON backend AND GUI (PyQt5 desktop). Rationale retained below.
- Data ownership: Python's sqlite3 reads/writes a real .db file the user owns
  and backs up. A bare .html file is browser-sandboxed (IndexedDB) or must
  manually download/re-pick the SQLite file each session - breaks the core
  "own your data" principle.
- The hard parts are libraries: OFX/QFX + QIF parsing, and quote retrieval
  (yfinance) - the goal explicitly requires "a Python package where one exists";
  from a browser these also hit CORS walls.
- The register is a dense editable grid; Qt's table view is built for it and a
  working prototype already exists. Quicken's look is functional, not fancy, and
  PyQt supports Qt Style Sheets (CSS-like) for styling.
- webSlinger being JavaScript does NOT force Mammon to be JS: it is an external
  tool that emits JSON, which Mammon imports regardless of language.
If browser-grade CSS + a web UI are strongly wanted, the sound alternative is a
HYBRID: a small Python backend (local server) + HTML/CSS/JS frontend - keeps all
Python packages and real on-disk SQLite while writing the UI in JS. More moving
parts than one PyQt window; a bare .html file is the option to avoid.
IMPORTANT: the DB + domain layer (schema, ledger, transfers, importers, quotes)
is Python in BOTH recommended options, so ledger-core work can proceed before the
UI skin is chosen. Only the UI task depends on this decision.

## 10. Open items / decisions to confirm
- Q5: integer cents + Decimal-text prices/quantities - RESOLVED (confirmed).
- Language/UI - RESOLVED: Python everywhere, PyQt5 desktop.
- Direct Quicken read - RESOLVED to "try both" (Section 6.1); QIF is the
  workhorse, direct read is a spike.
- Per-institution: whether webSlinger drives the site's export button or scrapes
  the transaction list - decided one at a time as we reach each (Section 7).
- Reconciliation acceptance test for the migration: match Mammon per-account
  balances to Quicken's - confirm this is the bar you want.
- Project NAME - RESOLVED: Mammon (Section 11).
- Sample Quicken export (a few accounts incl. one investment) - needed to build
  and validate the QIF importer (Task 5a) and the direct-read spike.

## 11. Project name
RESOLVED: the project is **Mammon**. The name is a deliberate reminder rather
than a boast -- "you cannot serve God and mammon" -- so the ledger you open every
day names the thing it is meant to keep in its place. It is short, ownable, and
carries no vendor echo. The Python package is `mammon`; the default database is
`data/mammon.db`.
