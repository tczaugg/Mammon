# Mammon - Software Requirements Document (SRD)

The requirements document of record: WHAT we are building and WHY.
Maintained alongside the code; The roadmap document tracks
execution. Last restructured into compartments 2026-09-11.

**How to read this.** The document is organised into COMPARTMENTS
(A-M), each one a mostly self-contained subsystem with its own
requirements and the modules that implement them. Read the
compartment you are working in; you should not need the others.
`CLAUDE.md` is the lean index that points here.

**Section numbers are stable anchors.** Around fifty comments in
live code and tests cite them (`SRD 5.8f`), so a section keeps its
number wherever it sits. That is why the numbering inside a
compartment is not sequential - do not renumber to tidy it.

| Compartment | Covers |
|---|---|
| **A** Register, entry and editing | The account register itself: how a row is entered, edited, sorted, filtered and undone, and how categories and dates are typed. |
| **B** Accounts, transfers and account types | What an account is, the account types the app supports, and the mirror model that makes a transfer two linked rows instead of one. |
| **C** Multi-currency | Per-account currency, FX rates, and how a foreign-currency balance rolls up into a home-currency net worth. |
| **D** Investments | Securities, holdings, lots and valuation: what a share is worth, what it cost, and how the two are kept apart. |
| **E** Cryptocurrency | Crypto accounts, their two import shapes (on-chain CSV and custodial exchange history), and how they present in the register and holdings. |
| **F** Import and the review queue | Getting outside data in: file parsers, the delimited-file column map, the dedup rules, and the review queue that stands between a downloaded row and the ledger. Nothing enters the ledger until a row is accepted. |
| **G** Learned rules | The five cooperating engines that turn bank gobbledygook into a payee and a category. Payee is resolved FIRST, category BELOW it. Nothing is seeded from history; all five learn only from what the user accepts. |
| **H** Scheduled pre-entries, reminders and the calendar | Recurring bills and loan payments pre-entered as placeholder rows before the bank transaction arrives, and the forward-looking views built on them. A pre-entry matches on TOLERANCE, not exact cents. |
| **I** Reconciliation | Reconciling a register against a paper or downloaded statement, and the audit trail for anything that changes a row after it is reconciled. |
| **J** Reports, charts and customization | Read-only aggregations and how they are presented: the shared customization bar, chart behavior, saved filter sets, export and print, and budgets. |
| **K** Database, backups and encryption | The canonical schema, how it migrates, how balances stay fast over 40 years, and how the file is snapshotted. The database is one SQLite file the user owns. Encryption is its own document: `docs/encryption.md`. |
| **L** Downloads, webSlinger and the MCP server | How transactions arrive from institutions, and the read-only tool surface an LLM sees. The app holds NO credentials: login and secrets belong entirely to the webSlinger/keyCocoon side. |
| **M** Platform, architecture and open items | Non-functional requirements, the resolved technology decisions, and what is still open. |

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


---

## Compartment A. Register, entry and editing

The account register itself: how a row is entered, edited, sorted,
filtered and undone, and how categories and dates are typed.

Code: `mammon/ledger.py` (the ONLY writer of transaction rows),
`mammon/ui/models.py`, `mammon/ui/widgets.py`, `mammon/ui/delegates.py`.

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
  no-op. A transfer entered on the blank row commits and IS splittable: the
  local leg is the row the dialog opens on, and the transfer becomes one LINE of
  the resulting split (SRD 5.2a).
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
  rows are ordered by cash balance, high to low** (user preference, 2026-09-15):
  "That way a transfer of funds arriving in an account shows before the
  purchases that use those funds. The sell of a rebalance occur before the buys."
  A day's rows carry no time, so their order was an accident of entry; within a
  date the row that raises the balance most comes first and the largest outflow
  last, which keeps the running balance as high as it can be at every row and
  never dips below where the day ends. Concretely: `ledger.register_rows` queries
  `ORDER BY date, amount DESC, id`; the investment register
  (`investments._register_sequence`) orders by date, then each row's cash
  effect high to low (share-only rows, which move no cash, sit between inflows
  and outflows), then id; a crypto account, whose events DO state a time, orders
  by date, then time, then the cash-sleeve effect high to low, then id (5.8j).
  The final `id` keeps the order total and stable, so the register never
  reshuffles between opens; every other column's sort key ends in `(date, id)`.
  This is DISPLAY order: the holdings and lot replays keep application order
  (date, id -- or date, time, id for crypto), so a same-day buy and sale of one
  security are never replayed as a short. **Balance
  keeps each row's date-ordered running value whatever the sort** (Quicken's
  behavior): sorting by payee never recomputes a balance. Num sorts numbers
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
- **The investment register shares this whole toolkit**, reusing the cash
  register's pattern and differing only where the content does. `InvestmentRegisterModel` gains the same
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
  verb, no cash-only payee/category cells); the behavior matches.
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


### 5.10a Register chrome and entry feedback
- Account actions (Details, Reconcile, Print Register, Import, Download,
  Review, Loan Setup, Hide) live in a **gear menu** at the right of the
  account-name line, not on a toolbar row — in the investment register too,
  where the gear additionally carries **Get Quotes** (§5.5c) and **Reconcile
  Shares** (§5.11b). Every one of them opens a dialog, and a row of buttons
  that only lead elsewhere is a poor trade for register height.
  `AccountToolbar` still owns the actions and their account-id wiring; it is
  simply never shown. Edit Account Details, Reconcile to Statement, Reconcile
  Shares and Print Register used to be duplicated on the Settings menu as
  well; they were dropped from there because each acts on "the account
  currently open in a register", which the gear menu already is — Settings
  now holds only the three preference submenus (Display, Sound, Format)
  described below.
- The **one-line/two-line choice is per account** (`ui/prefs.account_view_mode`,
  QSettings) and lives only in the gear menu — a View-menu copy of the same
  toggle was redundant with it and was removed. The layout that suits a
  register depends on the account: a card whose rows carry long statement
  descriptions wants the second line, a hand-entered checking account does
  not. Settings ▸ Display Preferences sets the DEFAULT; an account that chose
  from its gear menu keeps its choice when the default changes.
- **Sound and date-display format each get their own Settings submenu**
  (Sound Preferences, Format Preferences) rather than a field on the Display
  Preferences dialog, so each can grow: Sound Preferences is built to later
  hold more than one sound and more sound-worthy events (import complete,
  download complete, ...) beside "play a sound when a transaction is saved";
  Format Preferences is built to later hold more than the date format. Both
  are `QSettings`, wired straight from a checkable `QAction` with no
  dialog/OK-Cancel step, since a single choice does not need one.
- **Display Preferences (Settings) are QSettings, never DB, and theme-sensitive
  colors are remembered PER THEME.** Font family/size, row-shading on/off and
  the two-line default mean the same thing in light and dark, so they are
  stored globally. But the **alternate-row shade** and the
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
  other theme's saved color. In the same spirit, the **tree expand/collapse
  indicators** (the Itemize-by-Category drill-down, the report category picker,
  the categories and tags dialogs) are theme-driven rather than native: the
  platform style drew them near-black, which is invisible on the dark surfaces,
  so each palette names a `branch_indicator` color and one global
  `QTreeView::branch` rule in `style.build_qss` gives every tree in the app its
  arrows at 4.5:1 contrast or better.
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


## Compartment B. Accounts, transfers and account types

What an account is, the account types the app supports, and the mirror
model that makes a transfer two linked rows instead of one.

Code: `mammon/ledger.py`, `mammon/loans.py`.

### 5.2 Transfers between accounts (FIRST PRIORITY)
- Classic Quicken behavior (user confirmed Q1): a transfer is one transaction
  whose Category is "[Other Account]". Entering it AUTO-CREATES a linked mirror
  transaction in the other account with the opposite sign.
- The two sides are linked (transfer_pair_id). Editing amount/date/clearing on
  one side updates the other; deleting one deletes both. No double-counting in
  net worth.
- **"Go to [account]", in EVERY register.** Right-clicking a transfer leg offers
  a context-menu entry naming the counterpart ACCOUNT; choosing it opens (or
  raises) that account's register with the mirror row selected. A transfer is
  one movement seen twice, so whichever side is on screen has to lead to the
  other. This is register-kind-blind in both directions: the cash/loan, the
  investment (5.8) and the crypto (5.8j) registers all OFFER it, and all three
  are valid destinations. A row that is not a transfer leg offers nothing -- the
  entry is absent, not grayed. One entry per distinct counterpart account (a
  split touching the same account twice does not repeat it), the account itself
  is never offered, and a leg whose counterpart account no longer exists offers
  nothing rather than an unnamed jump.

### 5.2a A transfer can be split (user-reported, 2026-09-11)
- A transfer is one transaction whose money may move more than one way. The user
  sold crypto for 859.70 in a Coinbase account and 843.09 arrived in checking;
  the missing 16.61 is a transaction fee and has to be categorized. The register
  refused with "a transfer cannot be split", leaving no way to record it.
- **The transfer is a property of one split LINE, not of the ROW.** Splitting a
  transfer produces a row totalling 859.70 whose Category renders `--Split--`,
  one line transferring -843.09 to `[Checking]` and one line of -16.61 against a
  fee category.
- **The mirror equals the transfer LINE, not the row total.** The counter-account
  register shows a single 843.09 deposit -- the amount that actually moved.
  Correcting the split moves the other side with it; there is never a second
  deposit and never a stale one.
- The split must keep a line pointing at the account the transfer went to.
  Dropping it would orphan the other half of the user's transfer, so it is
  refused (naming the account) before anything is written.
- Undo of "split this transfer" restores the whole transfer with the SAME mirror
  row re-linked; deleting the split transaction removes both legs. Either way no
  orphan is left in the other account.

### 5.2b An opening balance counts from its date (2026-09-15)
- **An account's opening balance is part of its balance on and after
  `opening_date`, and not before.** Quicken writes an opening balance as a
  transaction ON its date, and every balance it reports for an earlier date
  leaves it out. Adding it for all dates made a mortgage opened in 2002 appear,
  whole, in a 2000 balance and in every net-worth figure from before the account
  existed. An account with no `opening_date` keeps the old reading: the opening
  balance is there from the beginning.
- One reader, `ledger.opening_balance_on(acct, as_of)`, answers it, and every
  balance path goes through it: the from-inception sum, the register's running
  balance (which picks the opening balance up at the first row on or after its
  date), the investment register's cash column, and the year-end snapshots
  (built, read and recomputed). Migration 69 drops the snapshots for years ending
  before an account's opening date, which were written with the opening balance
  in them; later snapshots are right under both readings and are kept.


### 5.4 Intermediary payees - Venmo, PayPal (RESOLVED: no dedicated field)
- Money moving through an intermediary needs the REAL counterparty recorded, not
  just "Venmo". A dedicated field on every transaction was specified for this and
  rejected in practice: give the intermediary its own ACCOUNT instead.
- Money between it and a bank account is then an ordinary transfer, and the real
  payee and memo live on the intermediary account's own register rows. This needs
  no extra field, scales past a handful of payments a month, and reuses the
  transfer machinery that is already tested. See migration 28 in `mammon/db.py`.


### Implementation notes

How this compartment is built, and what each shape
prevents. Moved out of CLAUDE.md 2026-09-11: it is
reference for whoever works here, not per-call context.

#### Transfers (the mirror model)

A transfer is **two** linked transactions, one per account, equal and opposite, cross-linked by
`transfer_pair_id` with each row's `transfer_account_id` pointing at the other account. Editing
amount/date on one side syncs the other; deleting one deletes both. The "category" of a transfer is
virtual — it renders as `[Other Account]` and consumes no category row. Every layer that touches
transactions must exclude or collapse transfers explicitly, or net worth double-counts.

This model is also why the app needs no third-party-payee field: give an intermediary (Venmo,
PayPal) its own **account**, and money moving to and from it is an ordinary transfer with the real
payee and memo living on that account's own register rows. A vestigial `third_party` column was
dropped in migration 28.

#### Splitting a transfer: the mirror follows the LINE (5.2a)

`ledger.set_splits` used to refuse a transfer outright. It now **adopts** the mirror onto a line: the
parent row's own `transfer_account_id`/`transfer_pair_id` go NULL, the split line targeting that same
account takes over the existing counter row — **same id**, so its date, payee, register position and
reconcile status survive — and that row is re-amounted to the negated LINE. The line remembers it in
`splits.transfer_pair_id`, exactly like any other split transfer leg, and like every split leg the
mirror is **one-sided** (its own `transfer_pair_id` stays NULL). Adoption, not delete-and-recreate, is
the point: recreating would silently unreconcile the other side and break anything referencing that id.

Consequences that are load-bearing:

- **The invariant is "mirror == transfer LINE", never the row total.** `update_transaction` mirrors the
  row total to `transactions.transfer_pair_id`, which is why the parent must NOT keep a row-level
  transfer link while carrying splits; that alternative shape was rejected for exactly this reason.
- **A split that drops the leg to the counter-account is rejected** (`ValueError` naming the account),
  checked BEFORE any mutation, because it would orphan the other half of the transfer.
- **A one-sided parent yields a one-sided leg.** If the row being split had `transfer_pair_id` NULL
  (an asymmetric import, or itself the mirror of someone else's split leg), no counter row is
  fabricated — that would post money into the other account that never existed.
- **A transfer that ALREADY carries splits is left alone**: an imported mortgage payment is
  legitimately both a transfer to `[House]` and a principal+interest split, and re-splitting it keeps
  the parent link as-is.
- `clear_splits(conn, id, restore_transfer_to=<account>)` is the exact inverse — it hands the surviving
  mirror BACK to the row and re-cross-links both sides. The keyword is opt-in and only `undo` passes it;
  the default still deletes leg mirrors, because a loan payment's principal leg to `[Mortgage]` must not
  be promoted into a whole-row transfer.
- `undo._structural` exempts this move in both directions (a transfer field going set→NULL normally
  means a non-undoable barrier), so "split this transfer" is an ordinary undo step.

Covered by `mammon/tests/test_transfer_split.py` (ledger life cycle, undo/redo, delete, and the
register path through `SplitDialog`).

#### "Go to [account]" is one feature, not three

`ui/widgets.TransferGotoMixin` holds the whole of it — naming, de-duplication by target account,
building the menu entries, dispatching the chosen one, and the jump itself (`MainWindow.open_register`
then the widget's `select_txn`, which all three register widgets expose). Each register supplies only
`_transfer_pairs(row)`: "what `(account_id, txn_id)` does this row transfer to". It was originally
written inline in the cash register; the crypto and investment registers went without until the user
reported the gap, and the fix was to EXTRACT rather than to copy, because a second copy is how the
three drift apart again.

The counterpart id is not always stored, so `txn_id` may be `None` and the jump then lands on the
target register without selecting a row — still the right answer, and better than hiding the entry.
Three shapes exist, and the lookup for each lives in the DOMAIN layer (`ledger`, `crypto.transfer_targets`,
`investments.transfer_targets`), never in the UI, which keeps no SQL:

- **cash and crypto↔crypto** — both legs carry `transfer_pair_id`, so the mirror id is simply read back.
  A loan payment's one-sided mirror leg is reachable only through the REVERSE link, and a split leg
  carries its own pair id.
- **crypto↔ordinary account** — the cash leg is written by `ledger` into `transactions` and the crypto
  row keeps only `transfer_account_id`; there is no id to store, so the mirror is relocated by shape
  (same account, date and **magnitude**, pointing back). Magnitude, not signed amount: a DEPOSIT's or
  WITHDRAW's cash leg carries the OPPOSITE sign of the crypto row, so a signed match silently finds
  nothing for exactly half the cash links.
- **investment XIn/XOut** — `investment_transactions` has no pair-id column AT ALL, so the counterpart
  is matched on `(date, counter-account, |amount|)` — the identical key `_unrepresented_transfer_legs`
  and the valuation double-count guard already use. A backfilled cash leg shown in that register is an
  ordinary `transactions` row and pairs the cash register's way.

**The link is not symmetric, and the cash side has to translate.** `transfer_pair_id` only ever names a
`transactions` row, so leaving the cash register it is the WRONG id whenever the target register reads a
different table. For a crypto target it names the shadow `transactions` row `ledger.create_transfer`
writes on the exchange purely to carry the mirror invariant — a row the crypto grid, built from
`crypto_transactions`, never shows; for an investment target it names the mirror cash leg, which that
register HIDES whenever an XIn/XOut already represents the same movement. Either way `select_txn` found
nothing and the register opened unselected (the user landed at the bottom), while the reverse direction
worked because it relocates by shape. So `RegisterWidget._transfer_pairs` passes every pair id — top-level
leg and split leg alike — through `_pair_id_in_targets_space`, which asks the DOMAIN module
(`crypto.crypto_txn_for_cash_leg` / `investments.investment_txn_for_cash_leg`) to re-locate the counterpart
on the SAME shape key the other direction uses. Two rows matching that key are resolved toward a matching
memo, then the lowest id: a deterministic jump beats none.

Covered by `mammon/tests/test_register_goto_account.py`.

## Compartment C. Multi-currency

Per-account currency, FX rates, and how a foreign-currency balance rolls
up into a home-currency net worth.

Code: `mammon/fx.py`, `mammon/ui/fx_rates_dialog.py`.

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
  writer `ledger.create_account`, which takes and normalizes a `currency`
  argument — there is no second write path. Changing an account's currency after
  it holds transactions would silently reinterpret every past amount, so the
  Account Details dialog shows the currency **read-only** and never writes it back.
- **An OFX/QFX download states its own currency, and it is honoured on creation.**
  `<CURDEF>` (ISO 4217, OFX 2.2 sec 5.2) is read by `importers/ofx.parse_ofx`,
  carried on `NormalizedTxn.account_currency`, and passed to
  `ledger.create_account` by `importers/core._resolve_account` and by
  `importers.import_single_account` (the review path a single-account file takes)
  ONLY when the account is being created. An existing account is never
  re-stamped, for the immutability reason above, and a code that is not three
  letters is ignored rather than stored -- it would otherwise be unfixable
  without rebuilding the account. A per-transaction
  `<CURRENCY>`/`<ORIGCURRENCY>` amount is not read: it needs an FX rate per row.
  **QIF cannot carry a currency at all** -- Intuit's `!Account` block is
  N/T/D/L//$ and no revision ever added one (the format was deprecated in favour
  of OFX/QFX instead), so a QIF-imported account takes the USD default and a
  foreign-currency ledger migrated from Quicken must have its currencies set by
  hand at account creation.
- **Per-account amounts render in the account's own currency.** A base-currency
  account renders bare (the dense classic look — no symbol clutter); a foreign
  account is tagged with its currency symbol/code (`ui/models.fmt_amount_ccy`,
  `currency_symbol`) so its balance is never misread as base dollars. This applies
  to the account bar / accounts overview per-account balances and to the register
  header (a foreign register names its currency once, in the title, and shows its
  ending balance in that currency). Formatting is presentation only — the cents
  are unconverted.
- **Net worth is a per-currency presentation folded to the base through
  `mammon.fx`.** `fx.net_worth_currencies(conn)` returns a `NetWorthCurrencies`:
  one `CurrencyLine` per native currency (the base currency first, then the rest
  A→Z), each carrying its **native subtotal** (signed cents in that currency),
  its **converted** value in the base currency, and a **grand total** equal to
  the sum of the converted lines — the layout the user asked for: each currency
  listed, then its conversion, then one USD total. The accounts overview / account
  bar figure is `net_worth_currencies(conn).total_cents`; `ledger.net_worth`
  (hence the whole reports/charts stack and every AS-OF sample of
  `net_worth_series`) folds through the same object, taking a single-currency fast
  path that delegates straight to `investments.net_worth` so an all-USD ledger is
  byte-for-byte unchanged and never pays for the currency machinery. All the cents
  arithmetic and rate lookup live in `mammon/fx.py`, not the UI.
- **A missing rate is surfaced, never folded at 1:1.** A **non-zero** foreign
  balance with no recorded FX rate becomes an UNCONVERTED line
  (`converted_cents is None`, `rate_missing`) and is **excluded** from
  `total_cents`; `NetWorthCurrencies.unconverted` names those currencies and
  `is_complete` is false, so the total is honestly *incomplete* rather than
  silently overstated by the raw foreign number. Adding such a balance at 1:1
  would overstate net worth by the whole foreign amount — the same shape of error
  as a double-counted transfer — so the code refuses to. A **zero-valued bucket
  needs no rate**: `convert_cents` returns 0 for a zero amount before any rate
  lookup (zero converts to zero at any rate), so an empty foreign account
  (balance 0, no recorded rate) is a clean converted line, not an unconverted
  one; a non-zero foreign balance with no rate raises `FxRateUnavailable`
  internally, which `net_worth_currencies` catches to mark that one line
  unconverted without disturbing the others.
- **An uncomputable total is shown as a WARNING MARK, never as a number.** The
  account bar's Net Worth strip renders `warning_triangle_icon()` -- the same
  amber triangle the register shows before `--Split--` for an unassigned split
  remainder, drawn in one place so one mark means one thing -- in place of the
  figure whenever `AccountsModel.unconverted_currencies()` is non-empty, with a
  tooltip naming the missing currency AND both ways to supply a rate ("Add..."
  for a dated rate by hand, "Refresh Rates" to fetch the latest close, both in
  Tools > Exchange Rates). This is the presentation half of the rule above: the
  per-currency subtotals are each complete, so the information the user needs is
  already on screen, and the only thing missing is the roll-up. Printing a number
  anyway would silently omit a real balance -- the total would read as though the
  foreign account did not exist -- which is the same lie as folding it in at 1:1,
  just in the other direction. Once any rate is recorded the strip reverts to a
  number and the tooltip clears (`test_multicurrency_ui.py`).
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


## Compartment D. Investments

Securities, holdings, lots and valuation: what a share is worth, what it
cost, and how the two are kept apart.

Code: `mammon/investments.py`, `mammon/reports/portfolio.py`,
`mammon/ui/rebalance_dialog.py`.

### 5.8 Investment accounts
- Maintain holdings and a per-holding price history.
- A price-history plot is labeled with the account's currency -- the currency of
  the ACCOUNT the holding sits in, so a TSX-listed holding kept in a CAD account
  plots as CAD.
  A security carries no currency of its own -- the account does (§5.4a,
  `fx.get_account_currency`) -- so a fund held in a CAD brokerage has CAD prices,
  and the chart must not imply otherwise. Every entry point into it hands an
  account down to the shared chart helper: the investment register's
  Security-cell double-click and its "Price history" context entry, and the
  Holdings window, are each a window scoped to ONE account; the Investment
  Performance report (§5.9b) spans accounts, so its per-holding row carries the
  same "Price history: SYM" right-click entry and supplies the account of THAT
  row's holding -- one security held in two accounts plots twice, each in its own
  currency. The helper labels the y axis
  `Price (CCY)`, names the currency in the plot title and the dialog title, and
  prints the `$` on the ticks ONLY for USD -- a CAD price wearing a bare dollar
  sign reads as USD and invites the reader to add it straight into a USD total.
  The currency crosses that boundary as an explicit argument
  (`ui/charts.PriceHistoryCanvas(symbol, points, currency=)`), and a missing or
  blank one normalizes to USD -- an account with no currency recorded is USD,
  the base, so the two-argument construction the older callers and the tests use
  keeps its old meaning instead of drawing an unlabelled axis. The same rule
  binds any coin chart a crypto window later grows, since it shares the one
  canvas.
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
  Void -- over the investment columns; see §5.1b for the shared behavior and the
  investment-specific Void.
- Its row context menu carries **"Go to [account]" on a transfer leg**, the same
  entry the cash register has (5.2): it names the counterpart account and jumps to
  the mirror row. Both shapes this register shows are covered -- an XIn/XOut
  carrying a `transfer_account_id`, and a backfilled cash leg -- and the entry is
  absent, not grayed, on a row that is not a transfer. `investment_transactions`
  has no pair-id column at all, so `investments.transfer_targets` locates the
  counterpart by (date, counter-account, |amount|); the register keeps no SQL of
  its own.


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
  - The register's **Share Bal** applies the split. It also decides what moves
    shares by asking the module's ONE classification rather than restating it:
    a row counts when `investments.is_quantity_action` says so, and contributes
    `investments.share_qty_delta`, the single signed helper `_apply_txn` already
    replays into `holdings`. `register_rows` used to carry a second, sign-blind
    copy of that rule (ADD-or-REMOVE), which omitted the short actions, so a
    short leg fell through to the cash-only branch and rendered as an empty
    cell. With the one classification, a short leg carries a negative Share Bal
    — ShtSell 10, ShtSell 5, CvrShrt 4 reads -10, -15, -11 — a fully covered
    short reads `0` rather than blank, and a position crossing back through zero
    (short 10, then Buy 25) reads -10, 15, which is also the sort order, the
    column being numeric. Short legs gained their gross security amount (**Inv
    Amt**) in the same move, both answers coming from the one question. A row
    that moves no shares — Div, IntInc, a bare cash transfer naming no security
    — still shows a BLANK Share Bal, and blank means "this row moves no shares",
    never "the balance here is unknown": that is the Quicken register's behavior,
    and carrying the running total forward onto those rows would claim a
    reconcilable share position where the transaction states none.
    A StkSplit is neither an ADD nor a REMOVE action, so `register_rows` skipped
    it: the split row showed a blank balance and every LATER row for that
    security carried a pre-split running total, disagreeing with `holdings`
    (which was always right, since it replays through `_apply_txn`). Share Bal
    ties to holdings by documented invariant; the split case has to be in both.


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
- **A trade is read against the position's sign** (`investments._apply_trade`,
  2026-09-14). Buy, Sell, ShtSell and CvrShrt (and the other plain buy/sell
  verbs) state a direction; the position decides what that means. A buying
  trade covers any short first, realizing the credit less its share of the
  cash spent, and only the rest opens a long lot. A selling trade sells any
  long first, exactly as a sale always has, and only the rest opens a short.
  A trade that crosses zero shares its cash between the two sides by shares.
  Quicken's verbs do not track the sign: a short is covered with Buy as often
  as with CvrShrt, and an option writer's whole history is ShtSell/CvrShrt. A
  cover used to realize nothing, which put a migrated ledger's option-writing
  history hundreds of percent away from the cash it produced in Previously
  Held. A covered short
  is short-term. The option life-cycle verbs (5.8e-7) keep their own branches.
- **A trade's amount is the net cash that moved**, commission included, in both
  directions: Quicken's `T`, an OFX `<TOTAL>`, and what the cash balance posts.
  Realized P/L no longer subtracts a sale's commission from it a second time;
  commission is applied only when a row has no amount and is priced from
  quantity x price. The edit dialog's Amount placeholder says so. The property
  both rules protect: once a symbol is flat and only traded, its realized P/L
  equals its net cash to the cent (`test_investments_short_realized.py`).
  Migration 68 drops the year-end snapshots written under the old rules.
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
  sales, cash dividends, returned capital and shares out are money back. Bisection
  on the net present value, so any data the user has yields a rate or an honest
  "n/a".
- **One definition of a holding's return** (`portfolio.holding_performances`,
  2026-09-15), read by the Investment Performance report, the Holdings window and
  `security_performance` alike. User request: "We need a dividends column, and to
  include the dividends in the gain and gain%. Also an annualized rate of return
  ... over the selected period or the duration of the holding. Same on the
  holdings dialog." And: "Reinvested dividends are included in the value of the
  security. But cash dividends are not."
  - **Gain** = ending value - starting value - money put in + money taken out.
    A cash dividend is money taken out, so it is added. A reinvested dividend
    bought shares that are already in the ending value and is neither in nor out,
    so it counts once. Returned capital is money taken out.
  - **Dividends** is every distribution in the span, cash and reinvested.
  - **Gain %** is the gain over the capital at work (starting value plus money
    put in). **Annual %** is the money-weighted rate per year over the span the
    flows cover, and is blank under a year: annualizing a few months compounds
    noise into a rate nobody earned.
  - **The span** is the report's period when it has one, and otherwise the
    CURRENT HOLDING: from the day the share count last left zero, so a fund sold
    out in 2010 and bought again in 2021 is measured from 2021. Only a position
    empty at the end of an EARLIER day restarts: rows within a day have no order,
    so shares removed and re-added by a CUSIP change, or sold and bought back the
    same day, are one continuous holding.
  - **Shares removed are a fee unless another account received them**
    (`portfolio.share_moves`; user rule: "Quicken doesn't have a way to move
    shares from one account to another. You would just see shares removed in one
    and shares added in the other. So treat shares removed as fees unless you see
    that."). A 401(k) takes its administrative and recordkeeping fees as shares,
    and counting those removals as money handed back added every fee to the
    return. A removal pairs with an addition in a DIFFERENT account of exactly the
    same number of shares of the same security (canonical symbol or recorded
    ticker) within 10 days; the pair is money out of one position and into the
    other, valued at whichever side states a value (brokers send arriving shares
    with no price). The account-level return (`external_flows`) applies the same
    rule. A removal whose addition is dated months away, or recorded under
    another listing's symbol, stays a fee: pairing records seven months apart is
    guessing, and the user corrects the records instead (user decision
    2026-09-15: "That is something for the user to fix.").
  - **A fund conversion can be kept as one holding** (migration 73,
    `investments.link_holding`; the Holdings window's right-click "Continues from
    ..."). A plan that closes a fund records the old fund sold and a new one
    bought, usually at a different share price -- in the user's words "like a
    split and a rename all at once, but the split isn't a nice ratio of
    integers." The link is per account and the person's choice, and it is
    offered and accepted only for a WHOLE conversion: every share of the old fund
    sold that day, and exactly those proceeds buying the new one
    (`conversion_proceeds`). Linked, the old fund's rows count under the new
    one, the conversion day's sale and purchase move no money, and the value
    carries over at the new share count, so the holding's gain and annual rate
    run from the first fund's purchase. Chains follow (A -> B -> C). It changes
    nothing else: prices, charts, holdings and stored rows are untouched, the old
    fund stays in Previously Held marked "continued as", and unlinking restores
    the two. Not a `security_aliases` row, because an alias pools the two price
    histories and folds the holdings into one identity everywhere. A link whose
    day has since been edited so the conversion is no longer whole is ignored
    (`holding_successors`).
  - **A portfolio rate** is solved over every holding's flows pooled, not averaged.
    A position with no capital at work (shares that arrived from a merger with no
    cost, an expired written option) still counts in the dollars but not in the
    rate: it has no rate of return, and pooling it left a real ledger's lifetime
    flows with none at all.
  - Values come from the replay at each boundary date through `_market_value`,
    so a short or an option values as it does everywhere else, and aliases fold
    as the replay folds them.
- **A broker's "Dividend" and "Cash Dividend" are dividends**
  (`investments._DIVIDEND_ACTIONS`, migration 72 drops the holdings snapshots that
  summed without them). Accepted through review as the download wrote them, 63
  rows on a real ledger -- every 2026 dividend of its ETFs -- were cash into the
  account but no one's income.
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
- **Distinct wedge colors, with `Other` pinned** (`ui/charts.wedge_colors`,
  `_PIE_PALETTE`). The palette carries enough visually separated hues for the
  worst realistic pie -- ~19 divisions, which happens when every real category is
  roughly 5% of the period and the sub-10% tail rolls up into an `Other` that is
  itself >=10%. The `Other` wedge always takes the palette's FINAL entry (a
  neutral gray), so it stays a stable, recognizable color no matter how many
  real categories precede it. The old ten-color list wrapped with `i % len`, so
  an 11th category reused -- and became indistinguishable from -- the `Other`
  wedge's color.
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
- **A security valuation is as-of on BOTH the price and the share count.** The same
  leak in the other direction was the historical-net-worth bug: `holding_values`
  threaded the as-of date into the price but sourced its positions from the derived
  `holdings` table (today's share counts, no date), so every past date valued the
  shares held NOW. A position since sold vanished from the past and an account
  closed out years ago reported its cash sleeve alone at every historical date --
  the whole net-worth curve understated history. Given an explicit `as_of`,
  `holding_values` now REPLAYS positions to that date via `compute_holdings`
  (snapshot-seeded through `holdings_checkpoints`, the same fast path the 40-year
  open relies on) and values the shares actually held then; a position closed by
  the date is dropped, exactly as the derived table drops it. With no `as_of` it
  keeps the fast `holdings`-table read, so today's totals are unchanged. This is
  the source set behind `account_valuation` / `investments.net_worth` /
  `ledger.net_worth` / `reports.net_worth_series` / `fx.net_worth_by_currency`
  (all via `display_balance`), so the whole net-worth stack is point-in-time
  correct. (The Holdings window and a security-filtered register instead read
  `security_positions`, which by design freezes the share count at today and caps
  only the price -- "what I hold now, priced then" -- so those two views and the
  valuation stack agree at the current date and diverge, intentionally, for a
  historical as-of.)
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
- **A tickerless fund's price comes from the holdings snapshot import.** A
  retirement-plan fund with no ticker can never be quoted, so the only price it
  will ever have is the per-share price printed on a statement. The holdings
  snapshot import (SRD 5.6; its dialog and the reconcile it feeds, SRD 5.11b)
  records that price in `price_history` under its own source `snapshot`
  (`investments.SNAPSHOT_PRICE_SOURCE`) through the ordinary `record_price`
  path, deriving it as `market value / shares` in Decimal when the file prints
  a value but no price. It creates no transactions, so the share count itself
  still comes only from the account's own history -- one import answers two
  different questions: what the account is worth (here) and whether the book
  share count agrees with the statement (SRD 5.11b, which reads the stated
  ending quantity the same import left in that security's reconcile draft).
- A snapshot price is **as traded on its statement date**, like a hand-typed or
  transaction-derived one, so it is split-adjusted on the way out with them
  (SRD 5.8e-4): `snapshot` is deliberately NOT in `SPLIT_ADJUSTED_SOURCES`,
  which names only the provider feeds that already arrive restated.


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
- **Instrument kind is STORABLE but not yet populated.** Schema v67 adds `kind`
  and `kind_source` to `securities`, plus the option terms `multiplier`,
  `underlying`, `expiration`, `strike` and `option_right` (compartment K). The
  migration only makes those facts recordable: it classifies nothing, backfills
  nothing and rewrites no identity, so every existing security comes out with
  NULL `kind` and behaves exactly as it did before. Valuation, holdings and cost
  basis still treat every security as a thing with a symbol and a share count;
  what reads the columns is the classification audit and its review screen
  (5.8e-2b), which is how a row stops being NULL. NULL must be read as "not
  known to be an option", never as "equity".
- **An option contract's identity is its canonical OSI symbol**, not its
  underlying's ticker. One contract is spelled `XYZ   260117C00150000`,
  `.XYZ260117C150` or `-XYZ260117C150` by three brokers, and a position opened
  under one spelling can never be closed under another; `mammon/instruments.py`
  (pure domain) parses any of them into `OptionTerms` and `OptionTerms.osi()`
  emits the one 21-char string every row for that contract shares. Two contracts
  differing only in strike, expiry or right are DIFFERENT identities, and
  collapsing either onto `XYZ` fuses the option position into the stock position
  -- the destructive path 5.8e-2's re-keying must never take. An ADJUSTED root
  (`XYZ1`) is likewise a distinct deliverable and survives into the symbol.
- **A contract's multiplier is read from the feed, never assumed.** The standard
  100 shares per contract does not hold for an adjusted contract, and guessing it
  is how a $645 settlement gets reported as $6.45. Where the feed states the
  deliverable, that wins; where nothing states it and the root is adjusted, the
  multiplier is UNKNOWN rather than 100. What a source states about an
  instrument lands in the v67 `securities` columns as it is imported
  (`securities.classify_source` for a QIF type word, `classify_seclist` for an
  OFX `<SECLIST>` block, both reached from the one import writer): a stated type
  gives `kind_source='source'`, a symbol that parses gives the terms, and what
  is allowed to land is decided by the precedence rule of 5.8e-2b, so nothing
  the user has classified is ever reclassified by an import. A pre-2010 symbol
  lands PARTIAL on purpose: the terms it cannot state
  stay NULL and are NAMED as unknown rather than defaulted, because a guessed
  strike is indistinguishable from a known one once it is stored.
- **What the source's own trades show outranks what the symbol says**
  (`securities.observed_multiplier`, 2026-09-15). The multiplier is the factor
  that turns quantity x price into cash, and that is a fact about how the SOURCE
  counted quantity. A broker feed counts contracts at a per-share premium (x100).
  Quicken's QIF counts `Q` as "number of shares" and every option trade in decades
  of its exports has `T = Q x I` -- shares at a per-share premium in some years,
  contracts at a per-contract price in others -- so the symbol's 100 valued those
  positions a hundred times too high. Each trade with a quantity, a price and an
  amount gives `gross / (quantity x price)`; it counts for 1, 10, 100 or 1000 when
  within 3% of it, or when the price it implies at that scale rounds to the stated
  price at the places the export wrote (a 0.026 premium written 0.03). All counting
  trades must agree, or nothing is set. An import applies it to the symbols it
  traded (`record_observed_multipliers`) but never over a person's own
  classification (`kind_source='user'`); a repair the person asks for may.
- **A NULL `multiplier` means one for a share and UNSTATED for an option**, so
  the column is never read without `kind` beside it. `securities.contract_multiplier`
  is the one place that resolves the pair: `Decimal(1)` for anything not known
  to be an option, the stated size for a classified contract, and UNKNOWN when
  the row is an option no source has sized.
- **Every site that derives or validates `amount` from `quantity x price` must
  apply that multiplier**, because an option's price is a per-share premium
  while its quantity counts CONTRACTS -- 2 at 1.75 is $350, and a site that does
  not know it both computes $3.50 and reports the user's correct $350 as an
  inconsistency. There are exactly two such sites, and a third must not appear:
  `importers/record.derive_investment_amounts` (the import path, including the
  tolerance it compares against) and `ui/widgets.InvestmentTransactionDialog.resolve_qpa`
  (the manual-entry path; its recompute prompt goes back through the same solver
  rather than repeating the arithmetic). Everything else takes the source's
  stated cash verbatim -- notably the QIF investment record, whose `T` amount is
  recorded as given.
- **When the multiplier is UNSTATED, nothing is derived and nothing is flagged.**
  The source's own amount is recorded as it stands; an inconsistency that is
  indistinguishable from an unknown contract size is not a finding to put in
  front of a user. The one place it is refused is manual entry with no amount
  typed, where the cash cannot be recovered from quantity and price at all.


### 5.8e-2a Ticker renames without rewriting history (security aliases)
- A **ticker rename** is a different problem from the merge above. When a listed
  company changes its symbol (`FB`->`META`, `GOOG`->`GOOGL`), the security is ONE
  identity that traded under two tickers on either side of a rename date, and both
  spellings are legitimately present in the file -- pre-rename lots and prices
  under the old ticker, post-rename ones under the new. The merge in 5.8e-2
  REWRITES every old-ticker row to the new spelling; a rename does not need that,
  and rewriting 27 years of `FB` rows to `META` loses the fact that they WERE
  `FB`. The **`security_aliases`** table keeps both spellings and resolves them to
  one identity at READ time instead.
- **How the table is created.** `security_aliases(alias_symbol PRIMARY KEY,
  canonical_symbol NOT NULL REFERENCES securities(symbol))` is created by
  migration 64 (`init_db` applies it; nothing else creates it). The alias points
  AT the canonical securities row (the surviving identity); the retired ticker
  need not keep a `securities` row of its own.
- **How aliases are added and removed.** `mammon/investments.py` is the SOLE
  writer: `add_alias(conn, alias_symbol, canonical_symbol)`,
  `remove_alias(conn, alias_symbol)`, `list_aliases(conn)`. `add_alias` guards
  four corrupt mappings -- a security cannot alias ITSELF; the canonical MUST be
  an existing security (it is the identity everything folds onto); the alias
  must not close a CYCLE (the canonical must not already resolve back to the
  alias), which would make resolution ambiguous; and NEITHER SIDE may be an
  option contract (below). `remove_alias` reverses the rename for lookup
  purposes; because no historical row was ever touched, the old ticker's lots
  and prices simply resolve to themselves again.
- **The rename-only invariant.** security_aliases means rename and only rename;
  an option contract is a distinct instrument, never an alias of its underlying.
  A contract has its own terms, its own price series and its own expiry, and it
  ends while the stock goes on, so folding the two onto one identity would value
  the contract at the stock's price and merge two unrelated holdings with no way
  to tell them apart again. The pull towards that mistake is structural, not
  hypothetical: `investments.ticker_of` reads the first token of
  `XYZ 260117C00150000 XYZ 17JAN26 150 C` as `XYZ`, and a QIF `!Type:Security`
  block for an option states the ROOT ticker in its `S` field, so every
  ticker-derivation path in the codebase is one step from proposing the
  collapse. Three refusals hold the line, all keyed on
  `investments.looks_like_option` -- a deliberately conservative OSI-shape test
  (it matches only the unambiguous `YYMMDD` + `C`/`P` + 8-digit strike body, and
  is documented as an interim stand-in for a real instrument classifier):
  `securities.suggest` never proposes an identity change for a contract and
  never returns `confident=True` for one even when a source stated the root;
  `securities.apply_splits` raises before touching anything if a split's target
  identity already belongs to a differently-classified security, because
  `_rekey` merges by DELETING the losing rows and that is not undoable; and
  `securities.fetch_ticker` returns None for a contract, so no stock price is
  ever filed against one until option quoting exists.
- **How aliases are used.** `resolve_symbol(conn, symbol)` maps a symbol to its
  canonical identity (identity when there is no alias). Every price / holdings /
  valuation lookup routes through it: the position replay folds an aliased
  ticker's lots onto the canonical symbol (`_fold_aliases`), and a price read
  unions the whole canonical identity -- the canonical symbol PLUS every alias
  that resolves to it (`_identity_symbols`) -- so a renamed ticker's pre-rename
  price series still values its holding. The continuity is entirely read-time:
  holdings and valuation are UNCHANGED by the mere act of adding the alias, and
  the fast paths (`list_holdings`, `holdings_checkpoints`) keep working because
  `rebuild_holdings` writes the folded position under the canonical symbol.
- **Alias vs. merge, when to use which.** Use an alias for a genuine ticker
  rename, where preserving that the security once traded under the old symbol is
  correct and reversibility is cheap. Use the 5.8e-2 merge to collapse two
  spellings that were never distinct (an import typo, a name-as-symbol vs. its
  real ticker), where the old spelling is simply wrong and should disappear.


### 5.8e-2b Classifying what each security IS (audit first, then confirmation)
Forty years of imports left `securities` describing instruments in whatever
words each source happened to use, and schema v67 (5.8e-2) gave those facts a
home without filling them in. Filling them in is deliberately TWO moves, not
one: a report that only reads, and a screen where the user confirms. Nothing in
either deletes a securities row, re-keys one, or touches a position.

- **The audit reads and cannot write.** `mammon/reports/security_audit.py` is a
  pure aggregation -- no Qt, no writes, plain data structures -- returning for
  every securities row what the row currently says it is, what
  `instruments.classify` PROPOSES it is, and the option terms its symbol parses
  into, plus counts and row lists. Running it leaves the database byte for byte
  as it was, and a connection under `PRAGMA query_only` is enough to run the
  whole thing. That property is the entire reason the reading half is separated
  from the acting half: the first pass over a real 40-year ledger is a report
  the user can run without deciding anything.
- **Three findings, kept apart, because they mean different things.** A `ticker`
  differing from the `symbol` is the ordinary rename of 5.8e-2a and is BENIGN,
  not a finding. A row whose identity is a CONTRACT while its ticker is its
  underlying's is one Apply away from being absorbed into the stock, and is
  flagged. A row whose identity is not a contract while its DESCRIPTION is one
  is already two instruments fused into a single row -- what 5.8e-2's
  destructive re-keying leaves behind -- and is flagged separately. Symbols
  sharing one ticker are additionally reported as a group, noting whether the
  group mixes kinds. Raising one undifferentiated alarm over all three would
  bury the two real ones under every legitimate rename in the file.
- **A fused row is proposed NO kind change.** Relabelling it picks one of the
  two instruments and loses the other silently. The audit names the row and
  stops; un-fusing is a separate operation that does not exist yet.
- **The legacy OPRA decoder is never used as a detector.**
  `instruments.parse_legacy_option` reads `IBM` happily as a contract, so it is
  asked only about rows something ALREADY calls an option. A pre-2010 symbol
  yields the terms it states and NAMES the rest as unknown rather than
  defaulting them, exactly as at import time (5.8e-2).
- **`securities.set_kinds` is the one writer, and it writes seven columns.**
  `kind`, `kind_source` and the five option terms, on an EXISTING row: never
  `symbol`, never `ticker`, never a holding, a lot assignment or a transaction.
  It validates the whole batch before writing any of it, so one bad row in a
  bulk confirmation cannot leave the file half-updated, and terms offered for a
  kind that is not an option are refused rather than stored. A confirmed change
  carries `kind_source='user'`, which the import path then declines to
  overwrite. Setting a kind back to NULL clears `kind_source` and every term
  with it -- unclassified is a real answer, and it must not leave the terms of a
  previous guess behind.
- **Precedence is `user` > `source` > `derived`, and it is not negotiable.**
  This is the whole reason `kind_source` is stored rather than just `kind`. An
  import states what a FEED said; the review screen records what a PERSON
  decided; `instruments.classify` only works something out from a symbol's
  shape. So: a NULL `kind` is filled by any statement; a statement of higher
  rank replaces one of lower rank; a row marked `user` is never touched by an
  import at all. Statements are re-imported constantly -- the same statement,
  the same year-end file -- and a correction that the next import quietly undoes
  is worse than having no correction screen.
- **At equal rank, a restatement lands only if it is STRICTLY MORE COMPLETE.**
  A second source may fill terms the first left NULL, but it may not disagree:
  a different kind, or a different value for a term already recorded, is a
  conflict to leave alone rather than a refinement to apply, and last-import-wins
  would make a row's contents depend on file order. Terms compare by VALUE, so
  `100` and `100.00` are the same statement, not an update. That is also what
  makes re-import IDEMPOTENT: importing the same file twice leaves one row with
  the same seven columns.
- **The import side decides, `set_kinds` still writes.**
  `securities.record_stated_kinds` is the gate in front of the one writer: it
  reads the row, applies the rule above, and hands the survivors to `set_kinds`,
  so the seven classification columns keep exactly one writer whether the change
  came from a person or a feed. Which importers state a kind today: OFX, from
  the `<SECLIST>` wrapper -- `<OPTINFO>` gives `kind='option'` with the
  deliverable from `<SHPERCTRCT>`, the resolved underlying ticker, an ISO
  expiration, the strike and the right; `<MFINFO>` gives `mutual_fund` and
  `<DEBTINFO>` gives `bond`. And QIF, from a type word on its security master.
  `<STOCKINFO>` and `<OTHERINFO>` state NOTHING and are left NULL on purpose:
  brokers file shares, ETFs, ADRs and sweep funds alike under `<STOCKINFO>`, so
  mapping it to `equity` would give a guess the standing of a statement.
- **Reclassification is additive and reversible.** It moves no money, no share
  count and no identity, so a wrong confirmation is corrected by confirming
  again. This is what makes a bulk confirmation safe to offer at all.
- **The review screen is a thin projection.** `SecurityKindDialog` (Tools ▸
  Security Kinds…) holds no SQL and no classification logic; it reads the audit
  and applies through `set_kinds`, and every write leaves it by one overridable
  seam. It opens with its own proposals already ticked -- 900 securities are not
  confirmed one at a time -- but it NEVER pre-ticks a flagged row, and ticking
  in bulk by symbol-or-kind pattern skips flagged rows too, so the two shapes
  that need a human are the two the blanket action cannot sweep up. The
  confirmation names the rows going back to unclassified and names the flagged
  ones, and always ends by stating that no symbol, ticker, holding or
  transaction is changed. Option terms ride along only when the user accepts the
  PROPOSED kind; an override to a different kind writes the kind alone, because
  terms read off a symbol the user has just contradicted are not evidence.


### 5.8e-2c Kinds that settle their own price: plan funds and money-market sweeps
A kind is worth recording only where it CHANGES what the program does. Two of
them decide, on their own, that a security needs no market data at all -- and
for both, asking for it is worse than not asking.

- **A tickerless plan fund is never handed to a quote provider.** A 401(k), 403(b)
  or 529 internal fund ("INTL EQUITY INDEX", "STABLE VALUE FUND") is not listed
  anywhere; no provider has a series for it, and its price arrives from the
  statement or by hand. `fetch_ticker` rule 3 already refuses to GUESS a ticker
  out of such a name -- INTL is a real listed company and the guess prices the
  holding at a stranger's stock -- but refusing the guess still leaves the
  download path trying and failing on every run. `kind='mutual_fund'` with no
  resolvable ticker now makes it SKIP the row outright: `securities.never_quote`
  answers the question once, and `investments.fetch_quotes`,
  `fetch_quote_history` and the Holdings window's quotable list all drop the row
  before a provider is called. A mutual fund WITH a real ticker (FIPDX) has a
  daily NAV and is quoted exactly as before -- it is the absence of a ticker,
  not the kind alone, that makes a fund unquotable.
- **A money-market sweep is pinned at 1.** `kind='money_market'` fixes the price
  at `Decimal(1)` inside `_resolve_price`, ahead of both the recorded price
  history and a caller's injected override, because a downloaded 0.9998 (or a
  stale 1.0001) is precisely the noise that makes a cash sleeve drift against a
  statement that says the number is 5,000.00. The pin is a Decimal, never a
  float, like every other price in the file. A pinned kind is also a never-quote
  kind: there is no series to download for something whose price is a
  definition.
- **NULL kind still means UNCLASSIFIED, and behaves exactly as it always did.**
  Neither rule fires on a row nobody has classified: `never_quote` answers False
  and the old rule-3 behavior stands, unchanged. Nothing here reclassifies
  anything retroactively -- the kinds come from 5.8e-2b, one confirmation at a
  time.
- **Whether a sweep is CASH or a SECURITY is a preference, and it ships OFF.**
  `prefs.money_market_as_cash` (Asset Allocation window, "Count money-market
  funds as cash") passes down to `investments.account_valuation` and
  `portfolio.allocation` as a parameter -- the domain layer reads no Qt setting.
  **Default: OFF**, deliberately, because ON would change the cash-versus-
  securities split of every existing file the first time it opened, and a number
  that moves on upgrade with no user action is indistinguishable from a bug. Its
  default answer is therefore the one that reports today's figures.
- **The flag moves money between buckets and never changes the amount.** With it
  on, the sweep's market value leaves `securities` for `cash`
  (`AccountValuation.cash_equivalents` records how much moved, so a footer can
  still show Securities + Cash = Total), and in the allocation it is counted in
  the `cash` class ONCE -- the per-holding loop skips a money-market position
  precisely because the valuation already folded it into cash. `total` and
  `by_security` are identical either way: how much of the fund you hold is not a
  matter of opinion, only which bucket it is shown in.


### 5.8e-2d An option contract is a SEPARATE INSTRUMENT from its underlying
A contract on ACME is not a spelling of ACME, not a share of ACME, and not a
line of ACME's position. Ten contracts and a hundred shares are 110 of nothing.
Everything below keys off `kind='option'` **explicitly**; a NULL kind is
UNCLASSIFIED (5.8e-2b) and takes the pre-existing path untouched, so an
unclassified ledger behaves bit-for-bit as it did before any of this landed.

- **The kind partition, and where it applies.** `investments.security_kind`
  reads the row's OWN kind first and only falls back across the identity when
  this spelling says nothing; `_kind_identity_symbols` then narrows the alias
  set (5.8e-2a) to the spellings whose option-ness MATCHES the symbol asked
  about, and `_kind_canon` narrows the canonical resolution the same way. Both
  are no-ops on an identity of one spelling and on an identity where no member
  is a classified option -- which is every identity in an un-backfilled file --
  so the cost is a handful of indexed primary-key reads and the behavior is
  unchanged.
- **Why the resolution and not just the set.** Canonicalising first is how a bad
  alias row would still win: ask about the contract, resolve to the stock, and
  report the stock's shares under the contract's name. The partition has to
  apply to the resolution itself.
- **The position replay refuses a fused set outright.** `add_alias` will not
  record a contract as an alias of its underlying (5.8e-2a); `_fold_aliases`
  raises a `ValueError` naming both symbols if such a row exists anyway. Merging
  a contract count into a share count is unrecoverable at read time -- they
  become one number and nothing downstream can separate them -- so loud beats
  wrong, and the user removes the alias.
- **A reconciliation runs WITHIN ONE INSTRUMENT.** `share_identity`,
  `_share_identity_clause` and `share_reconcile_summary` are scoped by the same
  partition: an option reconciles its own CONTRACT count against the statement's
  options section, and can never contribute to the underlying's share count. The
  summary carries `kind` and `adjustment_allowed` so the dialog can say which it
  is showing.
- **`record_share_adjustment` REFUSES an option position.** The share adjustment
  exists to close an unexplained SHARE gap with `ShrsIn`/`ShrsOut` (5.8a); there
  is no such thing as settling a contract count by inventing shares, and the
  right repair for a missing contract is the missing trade. It raises rather
  than writing, under either spelling, and creates no row.
- Covered by `mammon/tests/test_options_domain.py` (independent positions and
  independent reconciliations, the hand-written fusing alias on both the read
  and replay sides, the refused adjustment, and a NULL-kind twin for each).


### 5.8e-2e Option contracts arriving from a broker statement (OFX/QFX)
A broker's own statement is where most contracts enter the file, and for years
`mammon/importers/ofx.py` dropped or flattened them: the option trade aggregates
were unmapped, and every ending was collapsed into one cash-neutral
`RemoveShares`. An options account imported that way had permanently wrong
holdings and never realized a premium. The parser now reads the contract, the
trade and the ending for what they are. The parser-level mechanics live in 6.3a;
what follows is what the investments compartment is entitled to rely on.

- **`<SECLIST>`/`<OPTINFO>` is the contract's definition, and Mammon spells the
  contract itself.** The parse reads `OPTTYPE` (the right), `STRIKEPRICE`,
  `DTEXPIRE` (normalized to ISO), `SHPERCTRCT` (the deliverable) and the
  UNDERLYING's `SECID`, resolved against the securities already seen in the same
  `<SECLIST>`. From those STATED TERMS it builds the canonical OSI symbol of
  5.8e-2 -- it does not adopt the broker's `TICKER` as the identity. The ticker is
  parsed first for the one thing the terms cannot give, an ADJUSTED root
  (`XYZ1`), and `SECNAME` is tried as a fallback when the ticker will not parse.
  An option resolved through the `<SECLIST>` then OVERRIDES the transaction row's
  own `TICKER`, so three brokers' three spellings of one contract land on one
  symbol and on one position, which is the whole point of 5.8e-2d. The exception
  is a spelling whose terms cannot be recovered at all (a pre-2010 OPRA symbol):
  there the broker's text is kept verbatim as the symbol, because an invented
  contract is worse than an unparsed one.
- **`<BUYOPT>`/`<SELLOPT>` are mapped, with their open/close flag.** The flag is
  load-bearing, not cosmetic -- it is what separates opening a position from
  closing one, and a write from a sale:

  | Flag | Action | What it is |
  |---|---|---|
  | `BUYTOOPEN` | `Buy` | goes long the contract |
  | `SELLTOCLOSE` | `Sell` | sells a contract held |
  | `SELLTOOPEN` | `ShtSell` | WRITES the contract: a short position, cash in |
  | `BUYTOCLOSE` | `CvrShrt` | buys back a contract written |

  A flag that is absent or unrecognized falls back to a plain `Buy` for
  `<BUYOPT>` and a plain `Sell` for `<SELLOPT>` rather than being dropped.
  Units are carried as a MAGNITUDE whatever sign the feed put on them, because
  direction lives in the action alone and the domain layer negates a disposal's
  quantity itself.
- **`<CLOSUREOPT>` ends the position with its real outcome**, chosen from the
  `OPTACTION` and whether the position is short (negative units, or an
  assignment, since only a writer can be assigned):

  | Ending | Long | Short |
  |---|---|---|
  | `EXPIRE` | `Sell` at a price of **zero** | `CvrShrt` |
  | `EXERCISE` | `RemoveShares` (cash-neutral) | `CvrShrt` |
  | `ASSIGN` | -- | `CvrShrt` |
  | absent | `RemoveShares` | `CvrShrt` |

  An exercise is the one deliberately cash-neutral case: its premium is not lost,
  it becomes part of the basis of the shares arriving on the OTHER leg, and
  `RELFITID` is carried through on the option record so the two halves can be
  tied together. This is the import-side counterpart of the basis rules 5.8e-7
  states for the domain writers; the importer maps onto the EXISTING action
  vocabulary (`Buy`/`Sell`/`ShtSell`/`CvrShrt`/`RemoveShares`) and introduces no
  new action and no schema change.
- **An expiring contract is now a Sell at a price of zero -- a deliberate
  behavior flip, recorded as such.** `mammon/tests/test_importers.py`'s
  `test_ofx_closureopt_removes_option_units` previously expected the
  cash-neutral `RemoveShares` and now asserts `action == 'Sell'`, `amount == 0`,
  the holding fully closed, and cash unmoved by the close. That is the change
  that realizes the premium: a removal disposes of the position while reporting
  no result, so the entire cost of a contract bought and held to expiry
  disappeared from the file. The old assertion was not a regression to preserve.
- **A contract arriving this way is CLASSIFIED, and nothing else is.** The
  `<SECLIST>` statement reaches the v67 columns only through
  `securities.record_stated_kinds` and its one writer `securities.set_kinds`
  (5.8e-2b), at `kind_source='source'`, so it fills what is NULL and never
  overwrites what a person confirmed. The parsed terms that ride on the
  normalized record are not otherwise persisted today. NULL `kind` still means
  UNCLASSIFIED and never `equity`: a legacy ledger that has imported none of
  this behaves exactly as it did before.
- Covered by `mammon/tests/test_ofx_options.py` (19 tests in three groups:
  `<OPTINFO>` to identity, OSI symbol and multiplier, including divergent broker
  spellings canonicalizing to one symbol and an unrecoverable spelling kept as
  it stands; each open/close flag's action, with and without a `<SECLIST>`; and
  each ending with its `RELFITID` linkage, round-tripped through the database).


### 5.8e-2f Pre-2010 broker shorthand option symbols, and the never-merge guarantee
- A symbol that decodes as an option contract is **NEVER proposed for merging
  into its underlying**, by any route. This holds for the modern 21-character
  OSI symbol (5.8e-2, 5.8e-2e) and equally for the **pre-2010 OPRA / broker
  shorthand** a long history carries: a root, then one month/right letter and
  one strike letter. Brokers write the root in a fixed-width three-character
  field, so both `MS DJ` (two-character root, space-padded) and `LOWFX`
  (three-character root, closed up) are that one form. User-reported: an
  identity review over a real ledger proposed collapsing dozens of them onto
  their underlying, unlabelled and unrecognizable.
- `instruments.parse_legacy_option` decodes both spellings. The month/right
  letter table is the validity test (A-L = Jan-Dec calls, M-X = Jan-Dec puts;
  Y and Z decode to nothing), the root must be two letters or more, and a
  symbol whose letters are not a valid code returns None rather than a guess.
  It yields the underlying, expiration MONTH and call/put right; strike, day
  and year stay UNKNOWN, because the strike letter is a code into a table that
  depends on the contract's price range and is not recoverable from the symbol
  alone.
- That decoder remains **NOT a detector** and must not be used as one: every
  four- or five-letter ticker "decodes" (`AAPL`, `VFIAX`). `securities.suggest`
  therefore consults it **only for a row whose proposal would otherwise change
  the key** -- the spaced form always does, and the compact form does when a
  source stated the ROOT as the ticker, which is the QIF/OFX case where
  `apply_splits` would execute the merge as recorded fact. A row already being
  left alone is never stamped as an option, so ordinary funds do not acquire a
  false label.
- A refused row comes back as ITSELF: `old == symbol`, no description,
  `confident=False`, plus a human-readable `Split.reason` naming the decoded
  contract. `apply_splits` and `_rekey` act only on a key change or a
  description, so such a row has nothing either could execute -- the guarantee
  is structural, not a matter of the caller behaving. `security_aliases`
  semantics are untouched (rename only, 5.8e-2a).
- The securities dialog shows refused rows **unticked and not tickable**,
  sorted together at the foot of the list rather than interleaved with real
  proposals, with the reason text in its "What happens" column; a count line at
  the top reads "N securities: X proposed changes, Y left alone", naming how
  many of those are option contracts. Apply semantics are unchanged.
- **Letter case alone is never a proposal.** A stored spelling that differs from
  its proposed identity, or a description that differs from the one already
  recorded, by nothing but case is not a change: the row is shown unticked, not
  tickable, counted as left alone, and no re-keying or re-naming happens. The
  ONE exception is two DISTINCT stored rows whose spellings fold together
  (`vgt` and `VGT`), which are one security stored twice and stay proposed as a
  merge with a reason saying they differ only in case. User-reported: "requiring
  approval to change case is annoying and stupid ... this just clutters the
  table."
- **Held periods are shown, and an overlap disqualifies a rename.** The
  proposals table carries a **Held** column giving, per stored symbol, the date
  ranges during which it actually had a position and in which direction
  (`2004-03-12 - 2011-07-01`, an open one as `... - present`, a short one marked
  `(short)`); at most three are spelled out, the rest counted, with the full
  list in the cell's tooltip, and a symbol with no quantity rows shows nothing.
  Ranges come from `investments.held_ranges`, which replays that stored
  spelling's `investment_transactions` in date order in Decimal: a range opens
  when the running quantity leaves zero, closes on the date it returns, and the
  sign gives the direction. The user's ask: *"What would be helpful for the
  securities table is if it showed the date ranges when it was owned (long or
  short). Then you could disqualify a name change proposition if the time ranges
  overlap."* So `securities.suggest_all` **refuses** any proposal that changes
  the identity key when the target identity also exists as a distinct stored
  symbol with held ranges of its own and ANY range of the two overlaps
  (inclusive dates; an open range extends to today) -- two securities held at
  the same time are not one renamed to the other. The refusal takes the same
  shape as the option one (`old == symbol`, no description, non-actionable) with
  a reason naming the other symbol and the overlapping period. Two EXEMPTIONS:
  a case-only twin merge (above) is one security by definition and its total
  overlap is evidence for the merge, not against it; and a target identity that
  exists nowhere else in the data has no ranges and so can never overlap, which
  leaves the ordinary rename-to-its-ticker case untouched.
- **A proposal that would affect zero rows is never offered.** The proposals
  table carries a **Rows** column counting the stored rows that carry the
  spelling in "Stored as" -- the same key `apply_splits` and `_rekey` act on, so
  the number states exactly how much approving the row would move. A row reading
  **0** moves nothing and is therefore shown unticked and not tickable, counted
  as left alone, with a "What happens" text saying plainly that it exists in the
  security catalog only and no transaction, holding or price row uses it (or,
  for a spelling nothing at all carries, that nothing carries it) -- never
  described as a change worth approving. The usual source is a `securities`
  catalog row that outlived its data: `suggest_all` draws its symbols from the
  union of the symbol-bearing tables AND that catalog, and the catalog keeps
  entries whose transactions were deleted, or that the allocation feature
  recorded and nothing else ever used. `securities._settle_unused` runs last,
  after the case and held-range passes, and reads the SAME `usage_counts` the
  dialog displays, so the count on screen and the decision to offer the row
  cannot drift apart; the refusal takes the established shape (`old == symbol`,
  no description, non-actionable) so there is nothing to execute even if a
  caller passes the whole list back. It is tallied separately from the option
  contracts ("N used by no transaction, holding or price") because a catalog
  orphan is not an option. Nothing is deleted or cleaned up -- the row is
  listed, just not proposed. User-reported: *"I'm seeing a row that says 0
  description recorded. Why would you propose something if it affects 0
  items?"*
- Covered by `mammon/tests/test_securities_legacy_options.py`,
  `mammon/tests/test_securities_case.py`,
  `mammon/tests/test_securities_ranges.py` and
  `mammon/tests/test_securities_dialog.py`, plus the legacy decoder cases in
  `mammon/tests/test_instruments.py`.


### 5.8e-3 Downloaded prices: as traded for DIVIDENDS, restated for SPLITS
- `YFinanceQuoteSource` passes **`auto_adjust=False`** on both `get_quotes` and
  `get_history`. yfinance defaults it to True (1.7.0), returning a total-return
  series back-adjusted for dividends, while every other price in the file -- the
  QIF price, the transaction-carried price, the latest close -- is as traded.
  Mixing the scales is not cosmetic: a high-yield holding came back at half its
  real 2021 price (QYLD 11.67 against an as-traded 22.62). A dividend is a
  transaction, not a price revision, so the flag stays False.
- **The flag buys less than it was once thought to: it does NOT suppress the
  SPLIT adjustment.** Yahoo's chart endpoint serves OHLC already restated into
  the unit trading at fetch time, whatever `auto_adjust` says. So after VGT's 8:1
  split every downloaded pre-split close arrives at an eighth of what it traded
  for, while the prices this app derives itself stay as traded -- and the chart
  drew the difference as pre-split spikes poking out of the downloaded curve.
  That is not fixable at the provider, so it is absorbed on the READ side
  (SRD 5.8e-4). The earlier claim here that `auto_adjust=False` kept downloaded
  history on the as-traded scale was wrong; it is corrected rather than deleted
  because the same reasoning is still exactly right for dividends.
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


### 5.8e-4 A price series reads on ONE scale (split adjustment at read time)
- **Stored prices stay RAW -- as traded on their own date.** `price_history`
  holds two kinds of row: quotes from a provider, and prices this app derived
  itself (`learn_prices_from_transactions`, source `txn`/`qif-txn`, plus broker
  positions and hand-typed prices). Nothing rewrites a stored close, ever.
- **The READ applies the split: a series is split-adjusted on the way out,
  never in storage.** `investments.price_history` and
  `investments.price_history_bounds` divide each as-traded price by the exact
  cumulative factor of every `StkSplit` on that security dated **strictly after**
  the price's own date, so the series comes back in today's units end to end. A
  price on or after the last split is returned unchanged. Multiple splits
  compound exactly -- a 2:1 then a 3:1 divides an earlier price by 6.
- **Provider rows are passed through untouched** (`SPLIT_ADJUSTED_SOURCES`, the
  same set as `REFETCHABLE_SOURCES`): they already arrive restated (SRD 5.8e-3),
  and scaling them again would replace the user's spike with an equal and
  opposite trough. Source is what distinguishes the two scales, so it is read
  alongside the close.
- **Exact arithmetic only.** The factor comes from `split_factor` as a
  `Fraction` (from the exact `split_num`/`split_den` pair, SRD 5.8b), the divide
  is done in `Fraction`, and the result is rounded HALF_UP at the precision a
  price is stored with. A float divide by 3 puts drift into a number the chart
  then draws as a step.
- **Split rows are deduplicated by date, not summed.** The same corporate action
  is recorded once per account holding the security, so multiplying every row
  would tell a two-account holder that an 8:1 split was 64:1. Voided split rows
  are excluded, and alias resolution is shared with the share math, so a renamed
  ticker still finds its splits.
- **Why read time rather than restating the stored prices.** Rewriting history
  when a split lands is a one-way door: a mistyped ratio could not be undone,
  deleting a split entered by accident could not put the old numbers back, and a
  second pass over the same history would divide twice. Recomputing from the
  split rows means editing or deleting one self-corrects the whole series.
- **`latest_price` reads on the share scale of the date it values** (corrected
  2026-09-15). It prices a holding against a share count replayed as of that
  date, which has every split up to that date applied and none after. So an
  as-traded row is divided by the splits between its own date and the valuation
  date, and a provider row -- already in today's units -- is multiplied back up
  by the splits after the valuation date. It used to return every row raw, on
  the reasoning that pre-split shares pair with pre-split prices; that holds
  only for as-traded rows. A provider's back-adjusted close paired with the
  pre-split share count valued a holding's shares before its 8:1 split at an
  eighth of their worth, and the Investment Performance report measured from a
  date inside that year showed a gain near 1,000% where the real figure was
  under 30%. Every past-date valuation --
  net worth history, the performance report's period start, allocation at a
  date -- goes through this read.
- **The split lookup is indexed** (migration 71, a partial index over split rows
  alone). `latest_price` asks for a security's splits on every read, and
  filtering by identity in SQL scanned every investment row: net worth history
  took three times as long. `_split_events` fetches the split rows through the
  index and matches identity in Python; the query's WHERE must stay textually
  identical to the index's for SQLite to use it, and a test checks the plan.
- **Known limit: a provider row fetched BEFORE a split is as traded, not
  restated**, but its source says provider, so both reads treat it as today's
  units once a split is recorded after its date. Re-fetching the history after
  the split replaces those rows (`REFETCHABLE_SOURCES`) and restores one scale.
- Covered by `mammon/tests/test_price_history_split_adjust.py` (the reported 8:1
  life cycle, the boundary date, compounding splits, a 3:2 ratio, a security with
  no splits, the bounds read, the plotted chart series, past-date valuations on
  both sides of a split, the performance report's period gain, and the index).


### 5.8e-5 What an option position is WORTH (quantity x premium x multiplier)
A quoted option premium is per UNIT of the underlying, and one contract controls
`multiplier` units. Valuing ten contracts at a 4.20 premium as $42.00 instead of
$4,200.00 is the net-worth error this section exists to stop.

- **One chokepoint.** `investments._market_value(conn, symbol, qty, price)` is
  the only place in the module where a quantity meets a price, and it is the
  only place the multiplier is applied. `holding_values`, `_value_position`
  (`security_positions`) and `holding_values_at` all route through it, so the
  Holdings window, the register footer and a historical valuation cannot
  disagree about a contract.
- **The multiplier is READ, never assumed.** `securities.contract_multiplier` is
  the one reader that knows a NULL multiplier column means 1 for a share and
  UNSTATED for a contract; `investments.contract_multiplier` wraps it with
  identity resolution and turns the UNSTATED sentinel into `Decimal(1)`, because
  a valuation site cannot multiply by a sentinel. It does **not** guess 100: a
  mini, an index contract and one adjusted for a split are exactly the cases a
  guess gets wrong by 100x silently. The gap is reported instead (5.8e-6).
- **A SHORT position signs as a liability.** A written contract is an
  obligation, so its market value SUBTRACTS from net worth. This needs no
  special case and gets none: `rebuild_holdings` keeps a negative quantity (it
  drops only `qty == 0`), the sign flows through `_market_value`, and nothing on
  the path takes an absolute value.
- **Everything stays exact.** Quantity, price and multiplier are Decimal;
  rounding is HALF_UP at the cents boundary; the result is signed integer cents.
- **Known gap (Task 55 territory):** `_cost_of`/`_proceeds_of` use the row's
  recorded `amount` when it has one -- the normal case, and already correct --
  but their `price * qty` FALLBACK for a row with no amount is multiplier-blind.
  `_apply_txn` takes no connection, so making the fallback kind-aware is a
  refactor that belongs with the exercise/assignment/expiration basis rules, not
  here. Only an option row imported without a cash amount is affected.
- Covered by `mammon/tests/test_options_domain.py` (the x100, a x10 mini, a
  short position at both the holding and the account rollup, all three valuation
  entry points agreeing, and NULL-kind twins that must still value at x1).


### 5.8e-6 An option position the data says cannot be true
A contract that is still open after its expiration date is a DATA ERROR: the
closing transaction -- exercise, assignment, a closing trade, or expiring
worthless -- is missing from the ledger. The program reports it; it does not
repair it.

- **`investments.option_position_problems(conn, account_id, as_of=None)`** is
  read-only and returns `OptionPositionProblem` records
  (`account_id, symbol, quantity, problem, as_of, expiration, detail`). Two
  problems today: `OPTION_PROBLEM_EXPIRED` (nonzero position past its recorded
  expiration, long or short) and `OPTION_PROBLEM_NO_MULTIPLIER` (a classified
  option with no recorded multiplier, so it is being valued at 1x -- 5.8e-5).
- **Neither silently carried forward nor silently zeroed.** Zeroing it would
  invent a disposal, a realized gain and a tax year the user never recorded;
  carrying it silently shows an expired contract as a live asset forever. Both
  are wrong in a way the user cannot see, so the position is left exactly as
  recorded and the discrepancy is surfaced for them to resolve.
- **A NULL-kind row is never reported.** UNCLASSIFIED is not "option", so an
  un-backfilled ledger returns an empty list and nothing new appears in front of
  the user until they classify a row (5.8e-2b).
- Covered by `mammon/tests/test_options_domain.py` (long and short past
  expiration, nothing before it, a closed position never reported, the missing
  multiplier, and that reporting leaves the replayed position and its realized
  P/L untouched).


### 5.8e-7 How an option ends: exercise, assignment, expiry
An option has four ways out that a share has no analogue for, and in three of
them **the premium does not become a gain or a loss -- it moves.** These are the
lot and basis rules (IRS Pub 550, "Options"); getting them wrong misstates the
cost basis of shares the user may hold for a decade afterwards.

- **The vocabulary is eight new actions**, stored in
  `investment_transactions.action` like every other action and normalized the
  same way: `Buy to Open`, `Sell to Close`, `Sell to Open`, `Buy to Close`,
  `Exercise`, `Assign`, `Expire`, `Expire Short`. They are NEW spellings -- no
  pre-existing ledger row carries one -- which is why the replay branches need no
  kind test to stay safe on an unclassified ledger. The explicit `kind='option'`
  gate lives in the writers.
- **The rules, one row each:**

  | Ending | Result |
  |---|---|
  | Long call exercised | Shares acquired at `strike + premium` |
  | Written put assigned | Shares acquired at `strike - premium` |
  | Long put exercised | Shares sold, premium reducing the proceeds |
  | Written call assigned | Shares sold at `strike + premium` |
  | Long option expiring | Proceeds of zero: the loss is the whole premium |
  | Written option expiring | Gain of the whole premium, always SHORT-term |
  | Closing trade (`Sell to Close` / `Buy to Close`) | Ordinary realized gain on the premium difference, x the multiplier |

  The exercised or assigned contract's own holding period is **discarded**: the
  share lot is dated the day of the exercise, and what matters afterwards is how
  long the shares are held.
- **`investments.record_option_exercise(conn, account_id, date, symbol,
  quantity=None, *, memo=None)`** writes the exercise or assignment as **two
  rows, and they are two for a reason.** The share leg is a plain `Buy`/`Sell`
  carrying the adjusted figure, so lots, lot method, realized gain,
  reconciliation and the register all apply to it with no option awareness
  whatsoever. The option leg (`Exercise` when long, `Assign` when short) closes
  the contract, books **no** realized gain, and carries the SIGNED premium being
  rolled. That makes the pair cash-neutral on the premium: writing the share leg
  at the adjusted basis alone would charge the user a premium that already left
  the account when the contract was bought. `quantity` is in CONTRACTS and
  defaults to the whole position; the share leg is `quantity x multiplier` units
  of the underlying. It refuses a contract with no recorded right, no strike, or
  **no underlying** -- a cash-settled index contract has nothing to deliver and is
  closed with `Sell to Close` or an expiry, never an exercise -- and refuses any
  combination that would give the shares a negative cost or proceeds.
- **`investments.record_option_expiration(conn, account_id, date, symbol,
  quantity=None, *, memo=None)`** writes ONE row, no cash, and **no shares**: an
  expiring contract that leaves a phantom share position behind is the classic
  option-as-a-share bug. It picks `Expire` or `Expire Short` from the position's
  sign, so the caller cannot get it wrong. **Expiry is two actions rather than one
  whose direction is read off the position** because a share-quantity row has to
  state its own direction: the share reconciliation sums `share_qty_delta` per
  row with no position to consult, so a single `Expire` would be counted with the
  wrong sign for one of the two sides and would silently disagree with the
  replay. Split in two, each expiry is an ordinary removal or an ordinary cover
  and every existing path is already correct.
- **A share leg's price is the contract's, not the market's**, so it never
  becomes price history (5.5f).
- **A written option's gain is short-term however long it was open.** Writing an
  option is not holding property, so no holding period runs (`RealizedGain
  .term_override`). Without this a LEAPS written two years ago and expiring
  worthless would report a long-term gain -- wrong on the tax form.
- **A written contract is relieved against its own short lots**, so the credit
  released is lot-exact rather than the position average; a short position
  carries negative quantity and negative cost, and `holdings_checkpoints` already
  serializes lots, so short lots round-trip.
- **Commission is not accepted by either writer.** An exercise fee is its own
  row: folding it into an amount that also encodes a basis roll makes both
  unreadable.
- **Neither writer creates a `securities` row** for the underlying, and neither
  rebuilds holdings -- call `rebuild_holdings` after. An unclassified underlying
  stays UNCLASSIFIED (5.8e-2), which is the point.
- Known gap, inherited from 5.8e-5: `_cost_of`'s `price x quantity` fallback is
  not multiplier-aware, so an option row must carry an explicit `amount`.
- Covered end to end by `mammon/tests/test_option_lifecycle.py` (one test per row
  of the table above, plus the cash-settled refusal and a stock with two
  contracts on it staying three independent positions, price series and
  reconciliations).


### 5.8e-8 Options in front of the user: holdings, the expiry cue, the verbs
The domain sections above make a contract a first-class position; this one is
what the user actually sees. Everything here keys off `securities.kind ==
'option'` and NOTHING else -- not the shape of the symbol, not the presence of a
multiplier. **An account holding no classified contract renders exactly as it
did before options existed**, columns, titles and colors unchanged, which is the
whole of an unclassified 40-year ledger.

- **`investments.holdings_view(conn, account_id, as_of=None, prices=None,
  soon_days=7)`** is the single read the UI makes. It returns `HoldingLine`
  records wrapping each `held_positions` row with what a display needs and
  cannot work out for itself: `is_option`, the `group` it sorts under, the terms
  (`underlying`, `expiration`, `strike`, `right`, `multiplier`), the `problems`
  reported for it, and a `cue`. When no line is an option it returns the
  positions in plain symbol order and asks nothing further -- the unclassified
  ledger pays neither an extra query nor a reordering.
- **A contract sorts UNDER its underlying and counts SEPARATELY.** The sort key
  is `(group, options-after-shares, expiration, strike, symbol)`, so `ACME` is
  followed by the contracts written on it, oldest expiry first. `grouped` marks
  a row whose underlying is itself held, and the holdings table indents that
  row's symbol -- indentation only, because folding a contract into the share
  count is the option-as-a-share bug in its most expensive form: 2 contracts on
  100 shares are 2 contracts, never 300 shares. The un-indented true symbol
  stays on the cell in `Qt.UserRole`, so charting an indented row charts that
  contract.
- **Its value is its own.** The Shares column reads CONTRACTS and the Price
  column the per-share PREMIUM (the two columns are retitled "Shares /
  Contracts" and "Price / Premium" when the account holds a contract); the
  Market Value column is whatever `_market_value` returned, which is
  `contracts x premium x multiplier` (5.8e-5). Three columns appear alongside --
  Expires, Strike, Right -- and the arithmetic that is not visible in any of them
  is spelled out in the row's tooltip, because a market value 100x a premium the
  user can see is otherwise indistinguishable from a bug.
- **A written contract reads as a liability.** Negative contracts, a negative
  market value, and the row drawn in `ui/style.negative_color()` -- the same
  theme color a negative balance uses, resolved at paint time so it is legible
  in dark mode. Nothing here hardcodes a color.
- **The expiry cue is SOURCED, not re-derived.** `holdings_view` calls
  `option_position_problems` twice, once at `as_of` and once at
  `as_of + soon_days`; a position reported at both is `expired`, one reported
  only at the horizon is `expiring`, and the difference is the whole rule. There
  is no second expiry comparison anywhere in the UI to drift from 5.8e-6. An
  expired row is drawn in the negative color and an expiring one in the accent
  color, and both carry the problem text as a tooltip.
- **The register offers option verbs for an option row only.** Choosing a
  security whose kind is `option` appends the four open/close verbs plus
  Exercise, Assignment and Expire worthless to the action combo; choosing
  anything else -- including any unclassified security -- removes them again and
  leaves the pre-existing list byte for byte. `InvestmentTransactionDialog
  .action_codes()` is the seam a test reads. Editing an existing row sets the
  SYMBOL before resolving the action, or an option row's verb would find no
  match and be offered as an "(as imported)" stray beside the real one.
- **The three endings are routed to their domain writers**, not to
  `record_investment`: Exercise and Assignment to `record_option_exercise` and
  Expire worthless to `record_option_expiration` (5.8e-7), so the premium roll,
  the share leg and the short-lot relief keep their single implementation. The
  pair share one writer, which reads long-vs-short off the position, so a user
  who picks the wrong one of the two still gets the right rows. A refusal from
  the writer is shown as a warning and the row is not written.
- **The UI writes NONE of the kind columns.** `securities.set_kinds` remains the
  only writer of `kind`, `multiplier`, `underlying`, `expiration`, `strike`,
  `option_right` and `kind_source` (5.8e-2).
- Covered by `mammon/tests/test_options_ui.py`, which builds a synthetic ledger,
  establishes the positions through the ordinary domain path, and asserts
  grouping, the separate contract count, the terms and premium, the short row's
  liability color, and the three cue states -- each with a **NULL-kind twin**
  account whose symbol is deliberately OSI-SHAPED but unclassified, proving the
  display keys off the kind and not the string.


### 5.8e-9 Options and allocation: excluded, and SAID SO
An allocation answers "where is my money", and a contract answers it badly in
both directions. Counting one call as 100 shares of its underlying invents
exposure the premium never bought -- $400 of premium becomes $1,200 of "domestic
stock" and every percentage on the screen is wrong. Dropping it silently is no
better: the user holds the position, and an allocation that omits it without
saying so is a lie about the ledger. **The rule is exclusion plus a visible
note.**

- **`portfolio.allocation` removes an option contract from every number** it
  reports -- `total`, `by_class`, `by_security`, and each account's slice --
  before any of them is computed. The domain layer owns this, so the MCP tool
  (7.3), the allocation view and `rebalance.drift` (5.8f, which builds on
  `allocation`) all inherit one implementation and cannot drift apart.
- **Excluding is not the same as being unpriced.** A contract with a price is
  not reported in `unpriced`; it was valued perfectly well (5.8e-5) and then
  deliberately left out.
- **`Allocation.excluded_options`** names what was removed: `(symbol, cents)`
  pairs carrying each contract's premium value, ordered by value descending then
  symbol, with `option_value` their sum. A short contract's value is NEGATIVE
  there, exactly as it is everywhere else.
- **`Allocation.options_note` is the sentence a presenter shows verbatim**
  beside the percentages, naming the excluded contracts and their premium value.
  It is `""` when nothing was excluded -- an allocation holding no contract says
  nothing about options at all, which is the whole of an unclassified ledger.
- **Only an EXPLICIT `kind='option'` is removed**, tested through
  `investments.is_option` and nothing else. A NULL-kind row allocates exactly as
  it always did even when its symbol is OSI-shaped, because `kind IS NULL` means
  unclassified, never "option" and never "equity" (5.8e-2).
- Covered by `mammon/tests/test_portfolio.py` and
  `mammon/tests/test_mcp_tools.py`: a synthetic ledger holding shares, a long
  call and a short put asserts the exclusion, the named premium values and the
  note, each with a **NULL-kind twin** whose symbol looks like a contract and is
  still allocated -- and which, once classified, moves to the excluded list with
  the multiplier applied.


### 5.8f Target asset mix and drift (SRD 5.8f)
- `mammon/rebalance.py` holds a **target mix** and measures the real one against
  it. `portfolio.allocation` answers "where is my money"; this answers "is it
  where I meant it to be", which is the question an allocation view exists to
  serve -- classes are worth separating because they behave differently, and the
  payoff for holding several is keeping them at chosen weights as they diverge.
- Migration 44: `allocation_targets` (name, active, sleeve, the two bands, and
  v74's `rebalanced_on`), `allocation_target_accounts` (v74) and
  `allocation_target_lines` (`target_id, asset_class, pct`), shaped like
  `budgets`/`budget_lines`. Several NAMED targets, at most one active.
  Percentages are **Decimal-encoded TEXT**, like share prices: a mix is a precise
  quantity the user typed, and float drift in numbers that must total 100 shows
  up as phantom deviation.
- **The accounts are the user's, and they hold one kind of money** (migration 74,
  four defects reported together on 2026-09-15).
  - *"This mixes kinds of money. 401K + IRA shouldn't be mixed with ROTH which
    shouldn't be mixed with non-tax special holdings."* `accounts.tax_treatment`
    records which of `TAX_TREATMENTS` an account holds -- taxable, tax-deferred,
    Roth, or special-purpose (529, HSA, money held for others) -- set in Account
    details and never guessed from a name ("IRA" appears in Roth IRAs too).
    `set_target_accounts` REFUSES a set holding more than one treatment, naming
    them: averaging a Roth dollar with a 401(k) dollar hides that the Roth is all
    bonds, and the two are rebalanced apart because they are taxed apart. An
    account with no treatment set may still be chosen -- saying what it is, is a
    separate decision.
    The same column is the ONLY answer to "does this account pay capital gains"
    (`rebalance.is_capital_gains_exempt`, `CAPITAL_GAINS_EXEMPT_TREATMENTS` =
    everything but `taxable`), consumed by the Capital Gains report (J). One
    column, two consumers, deliberately: a second retirement flag would ask the
    user to say the same thing twice and the two copies would drift. Unset reads
    as taxable, so a file that never said keeps its old report exactly.
  - *"There is no customization for accounts. I wouldn't want to include the
    [529] accounts here as those are for my kids."* `allocation_target_accounts` is the
    target's own account list, picked from a check-list grouped by treatment. No
    scope rule can know whose money an account holds.
  - This REPLACES the `sleeve` enum, whose fourth defect was its own: *"The
    difference between investments and cash and investments is unclear, as both
    have cash, yet the advice changes."* Both values included cash -- a
    brokerage's idle cash is cash -- so the difference was invisible while the
    advice moved. Cash is now in the mix exactly when its account is ticked. A
    target with no account list still reads its stored sleeve, so a file made
    before this keeps working until its accounts are chosen, and the window says
    so ("chosen by a rule, not by you").
  - Property comes back in `DriftReport.fixed_rows` as CONTEXT, computed from the
    asset accounts in their own right (NOT as "everything minus the sleeve", which
    would count a chequing balance outside the chosen accounts as an untradeable
    holding). A drift number dominated by an illiquid position is not actionable,
    which is the failure most tools avoid only by not knowing the house exists.
- **Each class expands into its holdings** (`ClassDrift.holdings`,
  `HoldingDrift`). *"If the goal is to rebalance ... wouldn't it also make sense
  to show which assets within the asset class have changed the most as those may
  be the ones we'd want to sell/buy?"* Each holding carries its account, its
  value, its share of the class and its CHANGE over the span -- total return,
  dividends included (`portfolio.holding_performances`). The span is
  `allocation_targets.rebalanced_on`, the date the user last acted ("Rebalanced
  today"), falling back to the last year until one is set: what has moved since
  you last rebalanced is exactly what put the class off target. A security split
  across classes contributes its parts, and its change splits the same way. The
  window proposes no per-holding trade -- which to sell is the user's call, and a
  split of a class's dollars across its holdings would read as advice.
- **The unclassified bucket is never traded or banded** (`ClassDrift.is_unclassified`,
  action `classify`). It is not a class anyone holds: it is the stock slice of a
  fund whose domestic/international split nobody has set (SRD 5.8g) plus any
  security with no class. Judged against a band it produced a bold red "Sell"
  of six figures for securities whose only problem was a gap in the records, and
  it is left out of `to_move_cents` for the same reason.
- **Holdings with no price are named** (`DriftReport.unpriced`). They count as
  zero in every percentage, so silence about them made the whole mix wrong: a
  wallet's coins valued at nothing because their prices were filed under the bare
  symbol instead of the `SYM-USD` pair (SRD 5.8j).
- **A crypto account is valued as crypto** (`portfolio.allocation`). It used to be
  valued with the brokerage rule, which reads the cash `transactions` legs and
  cannot see `crypto_transactions`: an exchange whose own balance was zero came
  out as tens of thousands of NEGATIVE cash, and dragged the "no holdings
  recorded" note with it.
  One valuation per kind of account, the same one the accounts list and net worth
  show.
- **A stored percentage never carries an exponent.** `_pct_text` formats with
  `format(..., "f")`: `Decimal.normalize()` turned 70 into `"7E+1"`, the same
  stored-exponent defect that made an option multiplier read `1E+2`.
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
  rounding remainder on the largest class so the lines total exactly 100. That
  report is for a target written straight through the domain API, or by a build
  predating the locks below; the EDITOR can no longer produce such a total.
- **Locked weights and the always-100 edit.** A mix that does not add up to 100
  is not a mix, and the old editor let the user build one a keystroke at a time.
  The fix is not validation after the fact but an edit that cannot leave the
  total: raising one class LOWERS the others. `rebalance.apply_target_edit(lines,
  locked, asset_class, pct)` is that rule, pure Decimal over a plain dict -- no
  database, no float. The LOCKED classes keep their exact stored values; what is
  left of 100 after them is split between the edited class and the other
  UNLOCKED classes in proportion to what those already held (evenly if they are
  all at zero -- a proportional share of nothing is nothing, and the total would
  break). Recipients clamp at zero, so no weight can go negative, and the
  rounding remainder lands on the largest unlocked recipient -- the same
  convention `target_from_current` uses -- so **the column totals exactly 100
  after every edit**. An edit larger than the unlocked classes can absorb is
  CLAMPED to `100 - locked` rather than honoured at the cost of the total: a
  silently wrong mix is worse than a number that stops where it must.
- **The lock is what makes that rule livable: a locked allocation target line
  holds its weight while the UNLOCKED lines absorb every later edit, so the mix
  stays at exactly 100%.** Without it the class settled
  three edits ago drifts back out from under the user; with it the workflow is
  set a class, lock it, move on, until everything is where it should be. It is
  persisted per line (migration 66's `locked` column on
  `allocation_target_lines`) because that decision outlives the dialog:
  `set_locked`, `is_locked`, `locked_classes`. Locking a class with no line yet
  writes a zero line, and a LOCKED line driven to zero keeps its row where an
  unlocked one is deleted -- "hold nothing here" is a decision, not an empty
  field, and dropping it would let the next redistribution hand the class the
  weight the user just refused. `set_line_balanced` is the editor's single write
  path, so the UI never computes a weight itself.
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
  A Lock checkbox sits LEFT of each target weight -- reading across the row,
  "this one is settled" comes before the number it settles -- and a locked row's
  spin box is disabled, since editing a weight whose own lock forbids the answer
  is an invitation to confusion. Target percent editable in place (through
  `set_line_balanced`, which redraws every row, not just the edited one),
  current percent, signed drift in points and relative, and a Buy/Sell figure per class (cash reads Invest/Raise, never Sell);
  out-of-band rows colored (red overweight, blue underweight -- they must not
  both be red, since they mean opposite actions). MCP: `allocation_drift`.


### 5.8g Asset-class mixture per security (SRD 5.8g)
- **An ACCOUNT may hold a mixture too** (`account_mixtures`, migration 76,
  2026-09-21). `accounts.asset_class` held one class, so a conservative sleeve
  or a managed account reported as a single balance had to pick one and be wrong
  about the rest -- and an account holding no securities at all could say
  nothing. Same table shape as `security_mixtures` so `normalize` and
  `split_value` serve both. An account mixture governs only what the account
  contributes ITSELF: a non-investment account's whole value, and an investment
  account's idle CASH. It never touches the securities inside, which say what
  they are for themselves. `allocation` also honors an INVESTMENT account's
  explicit single class now, which it previously never read at all -- that
  balance was cash whatever the user set.
- **`crypto` is an assignable class** (2026-09-21). It lived only in
  `forecast.PROJECTION_CLASSES`, so the projection knew it was four times as
  volatile as equity while nothing upstream could emit it: a spot-crypto ETF or
  a wallet could not be described as crypto at all, and `set_mixture`,
  `set_security` and `set_account_asset_class` each refused the word.
  `PROJECTION_CLASSES` is now exactly `ASSET_CLASSES`.
- **A mixture can be typed by hand** (`ui/asset_allocation.MixEditor`).
  `set_mixture` had existed since mixtures did and only `fetch_mixtures` ever
  called it, so a 401(k) fund -- which has no public ticker for a provider to
  look up -- could not be described at all. Percentages are scaled to 100 on
  save, because 60/30/5 is plainly a ratio; all zeros clears the mixture and
  returns the security to its single class.

### 5.8g-1 The Asset Allocation report (`ui/asset_allocation.py`)
- **Accounts, expandable into their securities, with a stacked bar each.** A pie
  shows one grouping at a time and cannot compare two things, so "is my 401(k)
  more aggressive than my taxable account?" -- the question that decides what to
  buy next -- had no answer on screen. Bars over a shared class-to-color scale
  answer it. Segments are ordered by `ASSET_CLASSES`, never by size, so bonds
  are in the same place in every bar; the color map is one definition
  (`class_colors`) so a class cannot change color between accounts.
- **A legend, because the bars share one color map.** One legend serves every
  bar on the page (reported), listing only the classes actually held -- a legend
  naming classes nobody holds is one the eye learns to skip. It wraps by hand:
  Qt has no flow layout, and a single row silently clips its tail at a narrow
  window rather than saying so.
- **INVESTMENTS ONLY.** Property, vehicles and other owned assets are out of
  scope (reported): the dashboard's ring already excludes them, `forecast` has
  no (mu, sigma) for them so they can never join a projection, and here they
  bought nothing but a wedge. The older `AllocationDialog` keeps the wider
  scopes and stays reachable from the register's gear.
- **Amber triangles, not a silent bucket.** A holding with neither a mixture nor
  a class lands in `unclassified`, which the old view reported as one more wedge
  and left the user to trace. Every undefined row is marked AND so is every
  rollup above it, so an account with one unallocated fund says so on its own
  line. Same rule as `options_note` and `position_discrepancies`: state what is
  missing rather than averaging it in. `unclassified` is drawn in the theme's
  muted gray, never a class color -- the absence of an answer must not look like
  one.
- **The equity slice follows the class the security carries NOW**
  (`security_mix._settle_stock_slice`, read-time, 2026-09-15). A provider states
  what fraction of a fund is stock, bond and cash but never the domestic/overseas
  split, so the stock slice is filed under the class the user gave that security;
  with none, it is stored as `unclassified` and shown as such. Stored is not
  settled, though: a fund marked `bond` by mistake when its composition was
  fetched froze nearly all of itself into `unclassified`, and setting its class
  afterwards changed nothing until someone re-fetched (user-reported: "when I
  expand Unclassified it shows [a fund] ... but the other funds in that account
  are listed under Domestic stock"). The read now moves the slice onto whatever
  STOCK class the security carries, so the correction takes effect at once and
  un-setting the class puts it back. Same principle as the split adjustment
  (5.8e-4): the stored row is the source's statement, the read is what it means
  today. A non-stock class never absorbs the slice -- `bond` is how the fund got
  there.
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
  behavior.
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


## Compartment E. Cryptocurrency

Crypto accounts, their two import shapes (on-chain CSV and custodial
exchange history), and how they present in the register and holdings.

Code: `mammon/importers/crypto_core.py`, `crypto_tabular.py`,
`coinbase_csv.py`.

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
  account correctly. **The "cash sleeve" is EXACTLY the register's Cash Bal --
  `opening_balance` plus `crypto_cash` (the fiat that settles HERE: BUY/SELL and
  bare DEPOSIT/WITHDRAW) -- and nothing else.** `account_valuation` does NOT read
  `ledger.account_balance`: a crypto account's ordinary `transactions` rows (a
  SELLX/BUYX's mirror leg -- its proceeds went to a LINKED cash account, so it
  leaves this sleeve untouched -- or an imported cash row) are not part of the
  sleeve, and folding them in reported cash a coin-only exchange does not hold
  (e.g. $78k of "cash" on an account whose register Cash Bal is correctly $0,
  nearly doubling its net worth). So a coin-only exchange reads $0 cash, matching
  its register exactly.
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

**Editing price history (§5.5g).** A price is an OBSERVATION -- what the market
closed at on a day -- so the editor's repertoire is deliberately narrow:
BACKFILL a close nobody downloaded, and DELETE one that is wrong.
`investments.delete_price` is the only removal verb; there is no "correct it to
the right number", because offline the app is in no position to know what that
number was, and an absent price is a state every valuation already reports
honestly (an unpriced holding says so) while an invented one is not. Changing a
price in place is offered only because a typo in a hand-entered backfill is real.

Three properties are load-bearing:
- **What is shown is what is STORED.** `investments.price_history` divides each
  close by the splits dated after it so the chart reads in today's units;
  `investments.stored_prices` does not, and the editor reads that. An editor
  showing adjusted numbers would write one back as as-traded and a single
  round-trip would silently restate the history. The SOURCE column is shown for
  the same reason -- `SPLIT_ADJUSTED_SOURCES` treats a provider's closes and a
  hand-entered one differently, so a backfill is written as `manual` and never
  mislabelled as a download.
- **A zero close is refused.** It reads exactly like a real collapse; the way to
  say a day is not known is to delete the row.
- **A moved price is an add plus a delete.** The date is the series key, so
  editing one onto another date would otherwise leave a ghost at the old date.

It is reached from a corner button on every price chart (`ChartDialog(on_edit=)`
-- the chart is where a wrong or missing close is NOTICED, so it is where the fix
belongs) and from the holdings context menu in both the securities and crypto
windows. "No recorded price history" now offers the editor instead of being a
dead end: that message names exactly the case a backfill exists to fix, and there
was previously nowhere in the app to add the price it was complaining about.

- **A coin's price series is reachable from its holding, and editable.** A coin
  is filed under `{SYM}-USD` and a security under its own ticker, so a lookup by
  the holding's symbol found nothing for every coin and the app reported "no
  recorded price history" for a coin whose prices were in the table.
  `price_symbol_for` is the one mapping both entry points use (an unpriced coin
  still resolves to the pair, or a backfill would be filed where nothing reads
  it). `CryptoHoldingsDialog` gained the double-click and context menu its
  securities twin already had; without both halves a coin's prices were
  unreachable at all.
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
- **WHICH reader runs is decided by the FILE's column signature, not by the
  account** (`crypto_core.parse_export_file`, SRD 5.8i-2): a Coinbase-shaped
  document gets the exchange reader and a by-address export the on-chain one
  wherever they are imported, and a document matching neither falls back to the
  generic delimited reader (`crypto_tabular.read_records`), which infers the same
  roles from whatever the headers are called. The account kind then decides only
  whether USD rides along.
- **The asset is read out of the COLUMN HEADER when no cell carries it.** A
  block-explorer by-address export never names the coin in a row -- it heads the
  quantity columns `Value_IN(ETH)` / `Value_OUT(ETH)` and the gas column
  `TxnFee(ETH)` -- so the ticker is taken from the parenthesized symbol in the
  header itself (`crypto_tabular._asset_from_header`, checked in the order
  quantity-in, quantity-out, quantity, fee). A reader that insisted on a per-row
  `Asset` column would read the whole export as assetless and import nothing.
  Exactly one of the in/out pair is non-zero on a row, which is what gives the
  event its direction: `Value_IN` is an increase, `Value_OUT` a decrease, and the
  counterparty follows the coin -- the `From` address is the payee on an increase,
  the `To` address on a decrease.
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
- **Linking a cash move to a bank account reads its sign the OPPOSITE way from a
  trade.** Both shapes reach `crypto._link_cash_leg`, which writes the bank side
  through `ledger.create_transfer` (the sole writer of `transactions` and of the
  mirror invariant), but `crypto_transactions.amount` means two different things.
  A TRADE's cash is CREATED by the trade: `amount` is what the disposal realized
  (+) or the purchase cost (-), so a +ve SELL means the exchange holds proceeds
  and the link says they were wired OUT to the bank (SellX), a -ve BUY that the
  bank funded it (BuyX). A `DEPOSIT`/`WITHDRAW`'s cash is MOVED, not created:
  `amount` is already THIS side's sleeve delta -- a WITHDRAW is -ve because money
  left the exchange -- and the linked account is just the other end of that same
  move, so the direction is the MIRROR of the trade rule. A withdrawal out of an
  exchange is therefore negative there and **POSITIVE in the bank register (the
  Deposit column)**; reading it the trade way made it a Payment on both sides, a
  sign error the register's columns faithfully reported and must never be
  "corrected" in the display layer. `_link_cash_mirror` (crypto to crypto, no
  ordinary account involved) already expressed this as a plain `-amount`; the two
  paths now agree. Only `SELL`/`BUY` are renamed to the X form -- a linked
  `DEPOSIT`/`WITHDRAW` keeps its action and stays in the sleeve, because the
  money genuinely did leave (or enter) it.
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
- **The crypto register is a THIN projection whose writes all go through
  `crypto.py`, and ITS COLUMNS DEPEND ON THE ACCOUNT KIND.** It renders through
  `crypto.register_rows`, which owns every running-balance / cents / label
  computation (the UI holds no SQL and no money or precision math), and every edit
  it accepts funnels through `crypto.update_event` / `link_as_transfer` (the sole
  writer of `crypto_*`). `CryptoRegisterModel` lists the columns each kind shows and maps
  position -> column KEY, so a position that means Price on an exchange is a coin
  quantity on a wallet; callers address columns through `column_index(key)`, and a
  key the kind does not show returns -1.
  - **EXCHANGE** (custodial: a fiat cash sleeve, coin traded for dollars) —
    Date, Action, Coin / Wallet, Payee, Transfer, Quantity (stored SIGNED — an OUT
    leg is negative), Price, Coin Bal (running per-coin balance, folding in the
    same-coin gas so it ties to `rebuild_holdings`), Amount (the fiat cash-sleeve
    effect of a Buy/Sell), Cash Bal, Fee, Memo.
  - **WALLET** (a single address) — Date, Action, Coin / Wallet, Payee, Transfer,
    Memo, Coin Out, Coin In, Coin Bal, Fee.
- **A crypto event keeps its time of day, and a day is ordered by it**
  (migration 70, user request 2026-09-15: "Crypto has precise time, not just the
  date. Sort by time as the primary key with balance as the secondary key.").
  Every crypto source states the moment -- a block explorer's `UnixTimestamp`,
  an exchange's `Timestamp` -- and the importers used to keep only the date.
  `crypto_transactions.time` is `HH:MM:SS` on the same clock as `date` (UTC for
  every current source; `importers.record.stamp_time` reads it from the same
  stamp the date comes from), NULL when unknown, and a writer refuses anything
  not in that form because the column is sorted as text. It rides a queued
  review row (`review_items.time`) to the accept. The register orders by date,
  time, cash-sleeve effect high to low, id (5.1b); the replay by date, time, id.
  A row with no time sorts ahead of the timed ones on its day.
- **Re-importing an export gives its rows their time back.** Rows imported before
  migration 70 have none. The same file's hash identifies each event exactly, so
  a re-import (direct, `crypto_core`; or through review,
  `import_review.fill_crypto_times`) stamps the stated time on a posted row and
  its transfer pair, and on a still-pending review row, and never replaces a
  time already recorded. Holdings are rebuilt because the day's order may change.
    **The wallet register omits Price, Amount and Cash Bal** -- they are ABSENT, not
    blank. For a paper-wallet address there is no per-row USD, no fiat leg and no
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
- **The crypto register FUNCTIONS like the cash register (behavioral parity),
  differing only where the content requires it.** The gaps closed:
  - a per-row **context menu** (right-click) offering New / Edit / Delete. Edit
    opens `CryptoTransactionDialog` — the crypto twin of the cash
    `TransactionDialog` — which shows every field of a posted event on one form;
    Delete removes the row (both legs of a transfer or all legs of a swap) through
    `crypto.delete_event`. Both rebuild holdings afterward.
  - **"Go to [account]" on a transfer leg**, the same entry the cash register has
    (5.2): it names the counterpart account and jumps to the mirror row. It covers
    both shapes a crypto link takes — a crypto↔crypto pair, and a cash leg living
    in an ordinary account — and it is absent, not grayed, on a row that is not a
    transfer. `crypto.transfer_targets` locates the counterpart, so the register
    keeps no SQL of its own.
  - **Every field a posted crypto row legitimately has is editable — inline AND
    in the Edit dialog.** An import is not an oracle: an address is mistyped, a
    coin symbol comes through wrong, a source dates a row a day off, so a register
    you cannot fix is one you cannot trust. The reported gap was that a posted row
    was inline-editable in only a few cells (payee, memo, action, transfer, an
    exchange's amount) and its direction was uncorrectable even in the dialog.
    Now the ONLY read-only cells are the DERIVED running balances (Coin Bal, Cash
    Bal) — computed, never stored, so nothing there to edit. Every write funnels
    through `crypto.update_event` (or `link_as_transfer` for the Transfer link),
    so `crypto.py` stays the sole writer and no second write path appears.
  - **The DIRECTION is correctable: `SEND` ↔ `RECEIVE`, `BUY` ↔ `SELL`.** A
    mis-recorded direction was the sharpest edge of the bug — the Action list on a
    posted row was scoped to its own direction, so the flip was impossible. The
    Action cell now offers the full same-KIND vocabulary (a coin row cannot become
    a bare cash DEPOSIT and vice versa, since that swaps a quantity for a fiat
    amount that is not there), and flipping the action **re-signs the magnitude to
    match**: the stored quantity keeps its size but takes the new direction's sign,
    and a trade's fiat amount flips with it (a buy is money out, a sell money in).
    Coin In / Coin Out render off the quantity's sign and the cash column off the
    amount's, so the flip moves the coin to the correct side and the payee's
    meaning with it — the same counterparty is the recipient (`To`) on a send and
    the sender (`From`) on a receive (`crypto.payee_role` names which, surfaced as
    the Payee cell's tooltip). Entered as a positive magnitude in the dialog, the
    sign follows the action, so the user never reasons about the stored sign. The
    quantity/action consistency this keeps is exactly why the direction used to be
    frozen; re-signing in one place (`_write_action`, and the dialog's `values()`)
    is what makes the flip safe to allow.
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
  fee, and a wallet's absent Price / Amount / Cash Bal); the behavior is the same.
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

### 5.8k Investment Dashboard and projection
- **The dashboard is a PICTURE of the portfolio, not another table.** The
  Investment Center (SRD 5.8d) already owns the tabular views -- allocation,
  per-account performance, top holdings, drift -- so `InvestmentDashboardPage`
  draws only things no table states: a donut ring of the portfolio, arrows for
  money coming in, one line of numbers, two charts, and a projection. Anything
  that wants a table is a CORNER LAUNCHER that opens the window already owning
  that table. It adds no money arithmetic of its own: every figure is composed
  from `investments`, `portfolio` and `forecast`.
- **The securities ring carries a CASH wedge, and a single security's value
  does not.** `allocation.by_security` is built from priced holdings, so the
  securities ring once summed to less money than the accounts ring while the
  center block showed the account-based total in both -- the difference, cash,
  appeared on screen nowhere. It is a wedge now, last in the ring so it reads as
  the remainder, and drawn only when there is some. Selecting it scopes the page
  to cash, which has a value and deliberately nothing else: no gain, no
  dividends and no rate, because inventing columns of zeros would claim
  otherwise. Its key is a sentinel rather than the string `CASH`, since a real
  security may be that ticker.
- **A dividend paid in CASH is in the rates but not in the value, and the value
  says so.** Both kinds are income and both count in a scope's dividend figure
  and in every return percentage: `security_performance` treats a cash dividend
  as money BACK, so it lands in the gain exactly as a reinvested one lands in
  the ending value -- two positions earning the same $300 a year show the same
  gain by either route, differing only in rate, because money returned sooner
  earns more per dollar-year. What differs is the VALUE. A reinvested dividend
  bought shares and is inside the position; a cash one went to the account's
  cash, and there is no honest way to attribute that cash back to the security
  that paid it, least of all once the position is sold. So a single security's
  Total is asterisked when it has paid dividends in cash, the asterisk reading
  "dividends not reinvested" (`portfolio.cash_dividends` decides, from the same
  `_CASH_DIVIDENDS` set the flow classification uses, so the two cannot drift).
  A whole-portfolio total is never asterisked: its cash is already in it.
- **The thermometer follows the SELECTION, not just the account scope.** It
  reads `current_mix`, whose scope used to be expressed only as account ids --
  so a security selection measured the whole portfolio and a bond fund and an
  equity fund in the same account put the needle in the same place (reported).
  A security's mix is its own (its stated mixture, else its single class), and
  an asset class is all of itself.
- **The ring has a THIRD mode: by asset class** (reported, 2026-09-21).
  `allocation().by_class` is the same composition the Asset Allocation report
  draws as a bar, so the slices need no arithmetic here -- and the palette is
  that report's `class_colors`, so bonds are the same hue in the ring as in the
  report. Two pictures of one fact that disagreed about color would be worse
  than one picture. This mode needs no cash wedge of its own: `by_class` already
  counts cash and splits every mixture. An unallocated slice is labelled
  "Unallocated", not `unclassified`: in a picture of the portfolio the word has
  to say something is MISSING rather than name a category.
- **A class scope states a VALUE and nothing else**, like the cash wedge. A
  class is a property OF holdings, not one of them, and a gain is not a
  difference of endpoints -- it is that difference less the money put in, and
  money is put into HOLDINGS. Attributing a purchase to the classes its security
  happens to be made of would invent flows the user never made, and a rate
  solved on invented flows is worse than no rate.
- **A class DOES have a history, and the plot draws it** (`class_series`,
  2026-09-21). The user's formulation: `V = H @ C`, where H's columns are each
  security's value history and C's rows are that security's class weights, so
  V's columns are the classes' histories. It is computed by asking
  `portfolio.allocation` for each sample date rather than by assembling the two
  matrices, because that is the same arithmetic done by the ONE implementation
  that already splits a holding -- a second one would be a second thing to keep
  in step with mixtures, account mixtures, the sweep and the option exclusion,
  and the first time it drifted the plot would disagree with the ring above it.
  A test asserts the classes' curves sum to the portfolio's at every sample,
  which is the identity C's rows summing to 1 guarantees. **C is held at
  TODAY's weights**: `security_mix` stores one mixture per security with no
  date, so this is what the current classification says the past looked like,
  not what the funds actually held then.
- **The CASH wedge still does not reach the plots**: its key is a sentinel, so
  `value_series` would price it at zero and draw a flat line on the floor -- a
  picture of a scope worth nothing rather than one that cannot be charted that
  way.
- **The mode switch NARROWS before it sinks.** Its distance above the top plot's
  title is capped by the inner circle, which narrows as it rises; a third button
  made the row half again as wide and sinking it to fit collapsed the reported
  gap from 39px to 1. It now gives up width down to `MODE_ROW_MIN_WIDTH` to keep
  the height, and only sinks when even a minimal row will not fit.
- **One gear sets the page's account scope, and it is the application's
  EXISTING customization widget.** A gear at the top of the ring area, just left
  of the Performance Report corner (reported), opens the report bar's
  `CustomizeDialog` -- not a second account picker invented for this page, which
  would be one more place for the user's idea of "which accounts" to drift. Its
  selection is the page's universe: the ring's wedges in both modes, both hole
  charts, the center line, the inflow arrows and the projection all read it. No
  selection means EVERY investment account, and a non-investment account ticked
  in the dialog never enters the scope -- the scope is intersected with the
  dashboard's own account set, so the gear narrows that set and can never widen
  it into accounts this page has no valuation for.
  The dialog **offers only the investment-like accounts** (`investment` and
  `crypto`), all ticked, so it opens stating the page's actual universe
  (reported: "default the customization picker to have only selected the
  investment accounts"). It listed the whole roster before, all ticked, and the
  intersection above then discarded the cash accounts silently -- checkboxes
  that could not change anything. The restriction is an opt-in `account_types`
  argument on `ReportFilterBar`/`CustomizeDialog`; every other report window
  omits it and keeps the all-accounts, all-checked picker (§5.9).
- **The ring reads in two modes, and a wedge is a filter.** "Accounts" gives one
  wedge per investment account (`investments.account_valuation`); "Securities"
  gives one wedge per security held across all of them
  (`portfolio.allocation(...).by_security`). Zero and negative values are
  omitted, and there is NO "Other" grouping in either mode -- folding small
  slices would make exactly the holdings the user is hunting for unclickable.
  Colors are assigned in sorted-key order, so a wedge keeps its color as
  values move beneath it and across the mode toggle. Clicking a wedge filters
  the whole page to it (`filterChanged` carries `("account", id)` or
  `("security", symbol)`); clicking it again clears the filter, as does
  switching mode, and the payload is then `None`. The selected wedge slides out
  by a SMALL fraction of the radius (`SELECTED_EXPLODE`), enough to read as
  picked and no more: the ring's view limit is `1.0 + SELECTED_EXPLODE`, so
  every pixel of displacement is paid for by shrinking the whole donut, and a
  showy explode makes the picture smaller for no information.
- **The center line is the one line of numbers.** Inside the ring's hole, for
  whichever subject is selected (or "All investments"): total market value, the
  trailing-year gain, trailing-year dividends, and the annualized return at 1,
  3, 5 and 10 years. A horizon with too little history is ABSENT -- never shown
  as `0.0%` or a dash, which would assert a return the ledger cannot support.
  It gets its OWN rectangle, laid over the charts' rectangle rather than sitting
  as a row inside it, spanning the inner circle's full width: the charts' rect
  is inscribed in the hole and so is narrower than the circle at its widest, and
  a line of eight numbers held to that narrower width was being cut off. It is
  ON TOP OF EVERYTHING in the area -- raised last by every path that places the
  children, not once at construction -- and it is NOT mouse-transparent. It was
  transparent at first, so that it could not swallow the clicks belonging to
  the chart underneath it; but the attribute makes Qt skip the widget's whole
  subtree when it decides what is in front, which is exactly what "the center
  line is invisible" turned out to mean, twice. Nothing is lost by taking the
  mouse there: the charts' rect reserves a blank strip the height of the line,
  so what the line covers is blank.
- **The value-history chart shows what the subject has been worth**, sampled
  across a period the user picks (1, 2, 3, 5, 8, 10 years or Max, default 1
  year; Max is discovered from the ledger and capped at 40 years). It is drawn
  in the selected wedge's color, so the ring and the chart cannot be read as
  describing different things; with no wedge selected it falls back to the
  palette's accent rather than keeping the ex-selection's color. Both hole
  charts take every color they draw with -- series, bands, ticks, labels,
  spines, grid -- from the ACTIVE theme's palette, and both carry gridlines
  behind the data, because a chart tuned for a white page is unreadable on a
  dark one. They share one rectangle inside the hole, and that rectangle is not
  the inscribed SQUARE: it is widened (`HOLE_WIDTH_SCALE`) to the widest
  rectangle whose corners still lie on or inside the inner circle, buying chart
  width -- the scarce axis, since these are time series -- at the cost of
  height. The corners-inside-the-circle rule is the binding constraint for the
  right edge and the height, so no amount of widening can push a plot out from
  under the ring; the LEFT edge is then stretched a further
  `HOLE_LEFT_STRETCH` (10%) of that width, bounded by the ring's OUTER radius
  rather than its inner one -- the near corners end up behind the annulus,
  which paints in front of them, and still never reach the left band.
- **Each hole chart is CAPTIONED, on one row with its own period selector.**
  The caption is to the left of the selector, and the selector is the chart's
  own control moved into that row, not a second copy. The top chart's caption
  names the current scope -- "Total Performance", "<account> Performance",
  "<TICKER> Performance" -- because the curve is the same shape whichever of
  the three it is drawing, and a wedge-scoped curve read as the whole
  portfolio's is the misreading a title is cheapest at preventing; it is
  refreshed wherever the scope is, so it can never name a curve that is no
  longer drawn. Captions are as wide as the chart they caption and no wider.
- **The projection is a percentile FAN, and it is labelled as an estimate.**
  Below the center line, over a horizon of 5/10/20/30/40/50 years (default 20),
  `forecast.fan` projects today's value forward from three stated inputs: the
  current market value, the annual contribution implied by the inflow arrows,
  and the risk level. It draws the 5-95% and 25-75% bands with a median line --
  a fan, not a single curve, because a single curve reads as a promise. It is
  closed-form, not simulated, so it is reproducible. The bands are NAMED on the
  plot -- "5th-95th pct (model)", "25th-75th pct", "median (50th pct)" -- never
  as standard deviations, which they are not, and the legend naming them is set
  a size UP from the plot's base type, because prose needs more type than a
  number to be read at the same distance. Its caption reads "Projected Future
  Value", and directly under that row, full width and a size down, is the
  disclaimer: "projections show estimated future performance ranges based on
  risk models, but no model can predict the actual future." A fan read as a
  forecast is the failure this page is most exposed to, so the sentence is part
  of the chart, not a tooltip.
- **The projection's contributions are SCOPED to the selection.** An inflow is
  measured per account, so a fan drawn for one wedge carries only that account's
  own stream and an account with no arrow projects ZERO contributions; the
  unfiltered portfolio view carries the sum. A SECURITY selection also projects
  zero: an inflow arrives in an account, not in a holding, so charging one
  security with its account's whole stream would inflate that security's fan
  with money that mostly buys something else, and splitting it by share would
  invent a contribution policy the user never stated.
- **The inflow arrows COUNT contributions; they do not infer a cadence.** An
  investment account with at least 4 positive external flows in the trailing 365
  days gets an arrow entering the ring from the left, inscribed with the ACTUAL
  SUM of those flows over that window -- never an annualized extrapolation of a
  guessed schedule. No cadence is detected, stored or named anywhere.
- **The thermometer is the mix, stated in plain percentages.** A vertical cold
  (all cash) to hot (all stocks) ladder that sets the mix behind the projection,
  captioned with the stocks/bonds/cash split it currently means. It starts on
  the rung matching the portfolio's REAL mix and follows it on every refresh, so
  its resting position is a measurement rather than a default. It is read-only
  until What If is on. The handle is an OVAL painted on the colored bar
  itself -- one widget, driven by click, drag and the arrow/page keys -- not a
  separate slider beside it, which read as a cross rather than a thermometer.
  It takes only a FRACTION of an even share of the left
  band's height (`THERMOMETER_HEIGHT_SCALE`, half): it is a selector, and at
  full stretch it read as the page's main subject, which it is not.
- **The arrows and the thermometer share a left band, clear of the ring.** They
  are stacked in a fixed-width column whose right edge is the ring's left outer
  tangent LESS a gap of `BAND_RING_GAP` (50px) -- aligned to the tangent so the
  band reads as one column, and held off it by the gap so an arrow head does not
  appear to touch the donut. The band is an OVERLAY placed against the ring
  area, never a cell of the page layout: the tangent is a function of the ring
  area's width, which would be a function of the band if the band were a cell.
  The layout reserves its column as a left margin only.
- **What If never writes anything.** Toggling it makes the arrow amounts
  editable and unlocks the thermometer, and overlays a second fan on a dashed
  outline of the measured baseline. Measured values are what the database says
  and what-if values are what the user is imagining; NOTHING moves from the
  second set into the first, and Reset restores the measured inflows and mix
  without leaving the mode.
- **What If is SCOPED to the selection, and it is never disabled.** It follows
  the ring wedge exactly as the rest of the page does: with an account selected,
  the starting value, the measured contribution, the measured mix and the
  editable inflow are all that account's, so a $100 change to a $5,000 account
  visibly moves that account's curve. It used to gray itself out whenever a
  wedge was selected, and turning it on cleared the filter, which made the only
  question it is good for unanswerable (reported: "I want to be able to do what
  if on an individual account. Otherwise being able to change the inflow is
  meaningless as a tiny inflow in a small account can't move the needle vs a
  large total"). Two rules keep a scoped fan honest. Both fans are ALWAYS on the
  same subject -- the measured baseline is re-measured for the scope on every
  refresh, never left portfolio-wide underneath a one-account What If fan -- and
  the What If control NAMES its scope in words ("All investments" or the
  selected account or security), because a scoped fan misread as the portfolio's
  is a worse failure than the gray button was. Selecting or clearing a wedge
  while What If is on re-scopes the projection live rather than turning it off.
  Contributions follow SRD 5.8k's scoping rule above, so a SECURITY selection
  projects zero contributions and only the mix and horizon are in play.
- **Every inflow arrow that is DRAWN is editable and clickable while What If is
  on** -- no arrow is ever silently read-only or covered by something else.
  Editing an arrow for an account outside the current selection is allowed and
  RE-SCOPES the page to that account's own wedge (or, where there is no such
  wedge to select -- securities mode -- back out to the whole portfolio), so the
  typed number always visibly moves a fan. The earlier rule refused those edits
  on the grounds that they could not move the drawn fan; the arrows then sat
  there painted and inert whenever any wedge was selected, which reads as a
  broken control rather than as a scope. The clickability half is a placement
  requirement on the left band: the band is positioned by hand under overlays
  that are raised above it, so no arrow may be laid out underneath a corner
  launcher or the mode buttons -- the furniture below gives way instead.
- **The four corners are launchers, one per quadrant, fixed by the user.** Top
  left "Capital Gains and Taxes" (the report below); top right "Performance
  Report"; bottom left "Set Asset Categories", which opens Asset Allocation ON
  ITS BY-SECURITY TAB -- the tab where a security is actually given its asset
  class (SRD 5.8g); bottom right "Explore Rebalancing", the target-and-drift
  window (SRD 5.8f). They are overlays inside the ring area's corners, which a
  circle inscribed in a square can never reach, so they cost the ring no height.
  They sit in the MIDDLE of a three-level stack -- left band at the back, corner
  launchers over it, ring canvas over both (reported: "the left side corner
  boxes need to be in front of the arrow and thermometer panels but behind the
  ring segments") -- so an exploded wedge paints over a corner box rather than
  under it. A corner click still reaches its button because the ring canvas is
  masked to an ANNULUS from the inner radius out to the view limit: only the
  drawn band, plus the room an exploded wedge needs, is opaque to the mouse, and
  the corners fall outside it. Each is one small page method
  (`open_capital_gains`, `open_performance_report`, `open_asset_categories`,
  `open_rebalancing`) funnelling through two seams -- one modeless, one modal --
  so a test can assert what a corner aimed at without entering a modal loop.
- **Each launcher is a prominent title over a themed graphic it paints itself.**
  Four identical gray boxes of small text gave no clue which was which
  (reported: "Replace the Explore Rebalancing box with a graphic showing two pie
  charts with different proportions of the same colors in each with an arrow
  between them and the title Explore Rebalancing. Similarly, replace the other
  boxes with themed graphics and prominant titles"). So the bottom-right corner
  draws two pies -- the SAME wedge colors in different proportions, today's
  drifted mix on the left and the target on the right, an arrow between them --
  and the other three draw what they open: a gain with the taxed share bitten
  out of it (twice, a short lot against a long one), a rising series under a
  trend arrow, a legend of colored swatches against named classes. The four
  share one layout -- same box, same padding, title on top, graphic in what is
  left -- because they are seen together and per-corner tuning would show. The
  title word-wraps (a plain `QPushButton` clips "Capital Gains and Taxes" at any
  realistic width) and steps down through a FINITE list of sizes until its block
  fits, so the fit cannot loop; below a minimum the graphic is dropped and the
  title keeps the whole box, an unreadable caption being the worse outcome.
  Everything is painted with `QPainter` from palette NAMES resolved at paint
  time -- no image files, no hex literals (reported before, of the plots: "poor
  contrast in both dark and light mode") -- and the category hues are the ring's
  own wedge colors, so the pies are recognisably the ring's. The frame is still
  drawn by the style and the widget is still an ordinary `QPushButton`: hover,
  focus, `childAt()` hit-testing and `clicked` are unchanged, and so is the
  z-order above.
- **Capital Gains and Taxes answers "what does selling this cost me?", one line
  per OPEN TAX LOT.** The holding period is a property of the lot, not of the
  position: the same ticker can hold a long-term lot and a short-term one at
  once, so a per-position report cannot state it. Each line gives the account,
  the ticker with its share count, the acquisition date, whether the lot is
  **long-term or short-term**, the **date a short-term lot turns long-term** and
  how many days away that is, the **tax consequence of selling before that
  date** in prose, and then shares, cost basis, market value and unrealized
  gain. A lot whose acquisition date is unknown is reported as such rather than
  assumed long or short. The totals split the book into long-term gain, long-term
  loss, short-term gain and short-term loss, and price the difference: the EXTRA
  tax owed if the short-term book were sold today instead of held to term, at
  the stated long-term and ordinary rates.
- **It is a report in the shared window, not a bespoke dialog**, so it inherits
  the filter bar, the saved filter sets, column sorting and CSV/HTML/PDF export
  for free. **It has TWO entry points and must keep both**: the dashboard's
  top-left corner launcher (`open_capital_gains`) and `Reports > Capital Gains
  and Taxes (Window)…` (`widgets._capital_gains_window`), which open the same
  `CAPITAL_GAINS_SPEC` through their respective `_open_report` /
  `_open_report_window` seams -- exactly the dual wiring Investment Performance
  has. A report reachable only from a page the user has to know to visit first
  is, for that user, not there. **Two ways in, one window**:
  `_open_report_window` raises an already-open window on the same spec rather
  than stacking a second copy over it, so the second trip returns the user to
  the report they already have, date range and sorting intact; a window the
  user has CLOSED is dropped and reopened fresh, since the retention list only
  exists to keep a modeless window alive while it is on screen. It is a snapshot as of the bar's "To" date -- a lot's term depends on
  when it was bought and what day it is, not on a reporting window, so the
  "From" date is ignored rather than silently dropping older lots. The prose
  column sits BEFORE the money columns because the trailing column is always
  right-aligned, and a right-aligned paragraph is unreadable; sorting is offered
  on account, ticker, acquisition date, term, the becomes-long date, market
  value and unrealized gain, but not on the prose.
- **Staleness follows the shared contract.** The page takes an optional as-of
  date (today by default) that every query keys off, and `mark_stale()` refreshes
  at once when the page is visible and defers to the next show when it is not.
  A refresh rebuilds the ring, the arrows, the center line and both charts, and
  disturbs neither the current filter nor What If. The filter is DERIVED from
  the ring after every rebuild rather than remembered beside it: whatever the
  ring says is selected is the filter, and a refresh that drops the selected key
  -- a security sold, an account narrowed away by the gear -- leaves no filter
  at all. Holding the two independently let the page go on filtering, and now
  projecting, a subject no wedge was showing as picked.


## Compartment F. Import and the review queue

Getting outside data in: file parsers, the delimited-file column map, the
dedup rules, and the review queue that stands between a downloaded row
and the ledger. Nothing enters the ledger until a row is accepted.

Code: `mammon/importers/` (`core.py` owns everything database-facing),
`mammon/import_review.py` (the sole writer for that flow),
`mammon/ui/import_review_widget.py`, `mammon/ui/import_mapping.py`.

### 5.6 Import (see Section 6 for the strategy)
- One-time migration of the full ~40-year Quicken 2017 history into Mammon's DB.
- Ongoing import of downloaded/scraped data (QFX/OFX, JSON, CSV).
- Every importer maps to a common normalized record, then dedups (fitid +
  fuzzy match) before insert; transfers are recognized so both sides are not
  double-imported.
- **One import shape is deliberately NOT a transaction import**: the holdings
  balance SNAPSHOT (`importers/holdings_csv.py` pure parser +
  `importers/holdings_core.py` glue over `investments.apply_holdings_snapshot`).
  It carries positions, not activity — share counts and prices as of a
  statement date — so it bypasses `core.py`'s normalized-record insert/dedup
  path entirely and creates no transactions at all. It exists because a manual
  brokerage/401(k) download cannot put balances and transactions in the same
  file, and share reconciliation needs the balances. Per matched line it leaves
  the stated ending share count in that security's reconcile draft and records
  the stated per-share price in `price_history`, which for a tickerless plan
  fund is the only price anyone will ever have (SRD 5.8e, compartment D). Lines
  match by symbol when the file prints one and by name otherwise, and a line
  matching no security is reported rather than guessed at. Full shape and the
  tabbed, one-tab-per-security dialog it feeds: SRD 5.11b (compartment I).


### 6. Import strategy (Quicken 2017 -> Mammon)

#### 6.1 Can we read Quicken's native file directly? (answering Q3)
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

#### 6.2 Recommended path: export from Quicken, import into Mammon
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

#### 6.2a What a fresh QIF import has to get right (2026-09-15)
Other people will import their own Quicken history with no one to repair it
afterwards, so the import itself has to land what Quicken held. Two sources of
truth, and only these: the QIF specification (Quicken's own `QIF_Specification`
document) for what a field means, and the balances Quicken itself reports for
the same data. Never a ledger someone has since corrected by hand.

- **The acceptance test.** Import every yearly export into an empty database and
  compare against Quicken's reports: every non-investment account's balance at
  several dates, and for investment accounts the SHARES of each security and the
  cash -- not market value, which depends on prices Quicken may never have
  exported. It runs locally against a real history and is never committed; each
  fix it drove has a synthetic regression test. Last run (2026-09-15, the full
  yearly set): every non-investment balance and all investment cash matched Quicken
  at every date checked, and every share balance but six, which are exactly
  the six "removed but not held" findings below.
- **A transfer's other side is created only when the file carries no register for
  that account at all** (`core._counterparty_absent`). Keyed by account and date,
  a register the file DID carry but with nothing on that day counted as absent
  and the other side was invented, doubling the money in the other account. That
  is common in Quicken's exports: two sides of a card payment dated days apart, a
  transfer whose other side is in next year's file. The file's register list
  (`QifExtras.registers`) counts a register section even when it holds no
  transactions.
- **An opening balance counts from its date** (5.2b).
- **An option's units come from its own trades** (5.8e-2, "multiplier"): Quicken
  exports have `T = Q x I`, so the observed multiplier is 1 however the source
  counted `Q`.
- **A security's name is normalized once, the same way in every section.** The
  `!Type:Security` master, the `Y` field of each trade and the `!Type:Prices`
  fallback all go through `normalize_security_name`; left raw, the standard
  option symbol's padded root ("ACME  260417C00045000") made the master one
  security and its trades another, and almost no contract was recognized as an
  option.
- **An omitted item is blank.** The specification: "If an item is omitted from
  the transaction in the QIF file, Quicken treats it as a blank item." A buy or
  sell with a quantity and a price but no `T` moved no cash; Quicken writes an
  option's expiry or assignment removal that way. Its cost or proceeds are zero
  (`investments._states_no_cash`), so the whole premium is realized, instead of
  quantity x price being charged as cash that never moved.
- **The price list's strike closes are dropped** (5.5f), in whatever order the
  yearly files arrive.
- **What the import cannot settle is reported with it** (`reports/import_audit.py`,
  shown under "Worth checking" in the import-complete message, audited once
  after a whole set of files). The person importing is the only one who can
  resolve these, and each was a real discrepancy against Quicken:
  - *Shares removed that were not held*: a position that ENDS negative, dated
    by the removal that last took it below zero. Quicken exports a plan fund
    under two spellings across the years, so fee removals land on a name that
    never received the purchases; or the arrival before a removal was exported
    with no `Q`. Judged at the end of each day, and only for a position that
    stays negative, so a same-day sale ahead of its purchase and an old-style
    short (Sell, later Buy) are not reported. Written options are exempt.
  - *Shares that arrived with no cost basis*: a `ShrsIn`/`AddShares`/`XIn` with
    no price and no amount -- the new shares of a merger, as Quicken writes them.
  - *Holdings with no recent price*: held in an open account, newest price more
    than 90 days older than the newest price in the ledger (not today: an old
    export is uniformly old). Quicken does not export every valuation it holds.
  - *Options whose contract size the trades do not settle*: no trade shows what
    quantity x price is in cash, so the value rests on the symbol's multiplier.
    A person's own classification is not reported.

#### 6.3 Ongoing import formats
- QFX/OFX: the standard bank/CC/investment download format (SGML/XML). Direct
  parser -> normalized records. Preferred for institutions that offer it.
- CSV: per-institution CSV (e.g. Fidelity) where QFX is unavailable.
- Crypto CSV: a block-explorer by-address native-coin export (Etherscan shape)
  imported onto a `type='crypto'` wallet-account via its own parser + core
  (`importers/crypto_csv` + `importers/crypto_core`), with the on-chain `tx_hash`
  as the exact-dedup key. See Section 5.8i for the import rules (gas attribution,
  historical FMV, own-wallet transfer detection).
- JSON: canonical schema for webSlinger-scraped sites (Section 7.2).

#### 6.3a Option contracts in an OFX/QFX investment statement
- **The statement's `<SECLIST>` is the contract's definition.** An `<OPTINFO>`
  states OPTTYPE, STRIKEPRICE, DTEXPIRE, SHPERCTRCT and the UNDERLYING's SECID;
  the parser resolves it into the canonical OSI symbol (5.8e-2) and the
  deliverable, keyed by UNIQUEID beside the existing CUSIP->ticker resolution, so
  a transaction that names its security only by CUSIP still lands on the contract.
  An `<OPTINFO>` carries TWO SECIDs -- its own and the underlying's -- and they
  are told apart by POSITION; a flat scan reports the option as its own
  underlying and aliases the contract onto itself. Where the feed states terms
  too incomplete to name a contract (a pre-2010 OPRA symbol), what the spelling
  does say is kept, the rest stays unknown, and the broker's own text remains the
  symbol: an invented contract is worse than an unparsed one.
- **`<BUYOPT>`/`<SELLOPT>` are imported, not dropped.** They were formerly
  unmapped: reported by the unmapped-action audit and then discarded, which left
  an options account's holdings permanently wrong. Their OPTBUYTYPE/OPTSELLTYPE open/close
  flag is load-bearing and not cosmetic: a SELLTOOPEN WRITES a contract (a short
  position), and importing it as a plain sale relieves units the account never
  held and invents a realized gain out of the premium. Units are carried as a
  MAGNITUDE: OFX signs them, but direction lives in the action alone, and the
  domain layer negates a sale's quantity itself -- a negative quantity on a Sell
  would ADD contracts to the position.
- **`<CLOSUREOPT>` ends the position with its real outcome.** An expiry realizes
  the whole premium (a disposal at a price of zero, or a cover of a written
  contract); an assignment covers a short, since only a writer can be assigned;
  an exercise is the one deliberately cash-neutral case, because its premium
  becomes part of the basis of the shares arriving on the leg named by RELFITID,
  which is carried through so the two halves can be tied together. Treating all
  three as one cash-neutral removal, as the parser formerly did, discards the only
  realized result an option position ever has.
- These map onto the EXISTING action vocabulary (Buy / Sell / ShtSell / CvrShrt /
  RemoveShares) -- no new action, no schema change. The parsed contract terms and
  the RELFITID link ride on the normalized record (6.4) and are not yet persisted;
  the handoff to the v67 `securities` columns (5.8e-2) is a later step.

#### 6.4 Normalized import record (all parsers converge here)
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
- **The share side of an option exercise or assignment is never learned as a
  price** (`investments.option_delivery_leg_ids`, 2026-09-14). It changes hands at
  the strike, or at strike +/- premium once the premium is rolled (5.8e-7), not at
  market. Quicken makes this mistake in its own price list, recording the strike
  as the close on assignment days, and a migrated ledger carried dozens of such
  closes tens of percent away from the market. Two shapes
  are recognized, both only for a contract explicitly classified
  `kind='option'` with its underlying recorded: Mammon's own `Exercise`/`Assign`
  pair, and the broker/Quicken pair of a share row priced at the strike plus an
  unpriced, zero-amount close of that contract the same day. Shares match
  one-for-one (Quicken counts option quantity in shares) or as contracts x
  multiplier. A round-priced trade with no matching close keeps its price. The
  accept-match price fill in review applies the same test.
- **The strike closes already in a price list are removed at import**
  (`investments.drop_delivery_strike_closes`): a recorded close equal to a
  recognized share leg's price on its day. It runs after each file's rows and
  prices have landed, over every account trading a symbol that file priced, not
  only the accounts it traded in -- every yearly Quicken export repeats the whole
  price list, so a later file with no rows for an account re-added the closes an
  earlier import had removed.


### Implementation notes

How this compartment is built, and what each shape
prevents. Moved out of CLAUDE.md 2026-09-11: it is
reference for whoever works here, not per-call context.

#### Two ingestion paths

1. **Files** — `importers.import_file(conn, path, ...)` infers the format and dispatches to a parser
   (`qif`, `ofx`/`qfx`, `csv`, `json`). Every parser is a pure function
   `parse(text, default_account=None) -> list[NormalizedTxn]` with no database access.
   `importers/core.py` owns everything database-facing: account/category resolution, dedup (exact by
   `fitid`, else a fuzzy amount/date/payee score recorded in `transaction_matches`), collapsing
   transfer mirrors into one `ledger.create_transfer`, and writing the run to `imports`.
   **A QIF migration is a SET of files, and File > Import Quicken File(s) takes them all at
   once** (`MainWindow._choose_qif_files` multi-selects; the caption says so). Quicken could not
   write this ledger as one QIF -- the export had to be taken a year at a time -- so the set is
   imported in `MainWindow._qif_import_order`: alphanumeric by file NAME, case-insensitively.
   Order is load-bearing, and the files must therefore be NAMED so that alphanumeric order is
   chronological order (a four-digit year does it); the completion report echoes the order used so a
   set that did not sort as intended is visible rather than silent. Each file is still routed by its
   own content -- multi-account (or a securities/price master) in bulk, single-account through the
   review queue, with several files of one account forming ONE review batch. **The cutover watermark
   is deferred across the set** (`import_records(set_cutover=False)`, applied once after the last
   file): it marks the migration's END, and applied per file it would make `_before_cutover` skip
   every row of every later-imported file dated on or before the first file's newest row -- silently
   discarding most of a set imported in any order but oldest-first. `ledger.set_account_cutover_date`
   is monotonic, so applying the set's watermarks at the end is order-independent.
2. **Downloads** — `mammon/downloads.py` runs a recorded webSlinger script through an *injected*
   runner, producing a downloaded file (EXPORT mode) or scraped rows (SCRAPE mode). Rows do **not**
   go straight into the register: `import_review.build_review` classifies each as `NEW` or
   `MATCHING`, stores them in `review_items`, and `ui/import_review_widget.py` renders a review panel
   below the register. Nothing enters the ledger until the user accepts or saves a row, and
   `mammon/import_review.py` is the sole writer for that flow.

A **scheduled pre-entry matches on tolerance, not on exact cents.** Its amount is a
forecast (the calendar's six-month median, or the amortization figure), so escrow or a rate
change makes it miss by construction. `_find_match` adds a last tier for `num='Sched'` rows —
same sign, within `SCHED_AMOUNT_TOLERANCE` (half the placeholder's amount, matching
`importers/core._funding_pending_by_payee`) — and fires it only when exactly ONE such candidate
is in the window, because a review row has no payee yet to disambiguate with. Manual match
opens the same amount tolerance over `MANUAL_WINDOW_DAYS` (15, a hard cap); the sign never
varies.

`NormalizedTxn` (`importers/record.py`, SRD §6.4) is the waist of the hourglass: parsers stay
ignorant of the database, and `core.py` stays ignorant of file formats. A parser may also state a
property of the ACCOUNT rather than of the row -- `account_type`, and `account_currency` from an OFX
`<CURDEF>` (SRD 5.4a) -- which `core.py` applies only when it auto-creates that account.

#### Delimited (CSV-ish) imports and the column map

`importers/tabular.py` detects a delimited file's real header (skipping preamble/footer), sniffs the
delimiter (`, ; tab |` — so `.csv`, `.tsv`, `.tab`, `.txt` all work), and infers which column feeds
each role. `ui/import_mapping.py` is the wizard that lets the user overrule it, reachable from the
new-format prompt (Save / Adjust / Cancel). An accepted map is saved as a profile fingerprinted by
header signature, so that format never prompts again.

Two things here are load-bearing and easy to undo by accident:

- **A user-confirmed map must win.** `parse_csv` routes on header vocabulary, and a recognized date
  column sends it down the legacy path. Passing `roles_authoritative=True` overrides that for cash
  sources. Without it, a saved profile was silently discarded whenever the header merely *looked*
  conventional — exactly when an override is needed, since in `Date,Description,Amount,Balance` both
  money columns are recognizable and equally money-shaped.
- **Previews must name the source column.** A wrong column still produces plausible output (a
  balance column parses as money just like an amount column), so values alone cannot tell a user
  whether the mapping is right. Both the prompt and the wizard render `Role <- source column`.

#### Tags, and the two places a tag can live

A tag is first-class (`tags` + the `transaction_tags` junction, migration 45), and
a transaction can carry several. A **split leg** carries its own single `tag_id`
(migration 54) — that is not redundancy. Quicken tags a leg to attribute part of
one payment to a project, and folding those up onto the parent credits the WHOLE
payment to every tag in the split: measured on a real ledger, that reported 2.6x
the money actually spent, an overstatement of the same shape as a double-counted
transfer. `reports/_lines.py` gives a split line
the union of the parent's tags and the leg's, so both apply and neither inflates.

QIF carries tags in three places and all three are read and written: the
`!Type:Tag` master (`N` name, `D` description — `!Type:Class` in files older than
2010), a `/Tag` suffix on the `L` category line, and the same suffix on each `S`
split leg. **`record.clean_category` discards the tag half**; use
`split_category_tag` anywhere the tag matters. A split's `L` line echoes the
first leg's category tag and all, so reading it at row level double-tags the
transaction with one leg's project — `_build_cash` clears it deliberately.

## Compartment G. Learned rules

The five cooperating engines that turn bank gobbledygook into a payee and
a category. Payee is resolved FIRST, category BELOW it. Nothing is
seeded from history; all five learn only from what the user accepts.

Code: `mammon/rename_tree.py`, `category_tree.py`, `category_rules.py`,
`transfer_rules.py`, `categorize.py`, `keywords.py`.

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
  behavior.** A rule may carry any subset of `account_id` (scope to one
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


### Implementation notes

How this compartment is built, and what each shape
prevents. Moved out of CLAUDE.md 2026-09-11: it is
reference for whoever works here, not per-call context.

#### Learned rules (five cooperating engines)

Raw bank `statementDescription` text is gobbledygook, so the app learns from corrections.
**Payee is resolved first, then category is resolved BELOW it** — that ordering is what keeps a
suggestion scoped to the merchant it came from:

- `rename_tree.py` — payee renaming AND investment-action mapping as a **decision tree rebuilt
  from the accepted corrections at every prediction** (webSlinger's `selector_tree` with tokens
  for features, branching one). Nothing is learned online: the corpus is the `rename_examples`
  log (one row per accepted review row, label read LIVE through the transaction it created, so
  a register edit or an undo changes the next answer), and review retention never touches it.
  Per query: candidates = examples sharing a *distinctive* token (carried by ≤ 5 payees) or the
  exact token set; a binary presence tree by information gain over them; then the leaf's
  leading label is GENERALIZED like webSlinger's array selector (features = each token, the
  token before, the token after, the same over the payee field, plus "no field": agreed value
  kept, varying slot binarized, feature missing from any example dropped) and the row must FIT
  that pattern — `June rent` + `July rent` fit `August rent` but not a bare `rent`; identical
  rows stay exact. The label fills at ≥ `MIN_FILL` (2) fitting examples and ≥ `FILL_PURITY`
  (0.9) of the leaf, anything else is a dropdown. Only CORRECTIONS train the payee domain — a
  row kept as its shown text (description, title-cased default, supplied payee) is skipped — so
  the title-cased bank text can never become a "payee".
  The old trie reset its counts on every split, split on tokens both sides shared, and hid a
  parent's labels behind a weak child; the module docstring records the measurements.
**Neither tree is seeded from history.** Both learn ONLY from what the user accepts in
review. `rename_tree` used to bootstrap at startup, replaying every posted row's
`memo -> payee` pair as an accepted rename — true for downloaded rows, where the memo IS the
bank's text, and false for hand-entered and QIF-imported history, where it is a note the user
typed. A 1998 memo of "deposit" on a Foothill Place row taught the tree that `MOBILE DEPOSIT`
means Foothill Place, in a ledger deliberately started fresh. `ensure_bootstrapped` survives on
both modules as callable API for an explicit seed-from-history action; nothing calls it on open.
For the same reason `_prior_txn_for` no longer fills the PAYEE from register rows sharing the
statement text (category only).

Two thresholds, and they are different questions. `import_review.RENAME_MIN_SIGHTINGS` (2) is
whether a payee is OFFERED in the dropdown at all; `rename_tree.MIN_FILL` (2) is whether it is
APPLIED to the cell, and it counts the matched leaf — the same text renamed twice — not the
payee overall. Between them the user sees the bank's own text and a one-click list. A payee
chosen ONCE is neither filled nor offered.

- `category_tree.py` — the auto-categorizer: one discrimination trie **per payee**, over the source
  text, ranked by category entropy. Two gates, and both are load-bearing: **node purity** asks *does
  this description discriminate?*, **payee coherence** (`PAYEE_COHERENCE`) asks *is this merchant
  categorizable at all?* — the second applies only at DEPTH 0, where the answer is the payee's bare
  prior and nothing distinguished the row. A node below the root matched a token that has meant one
  category every time, and must not be refused because the payee is bimodal overall: gating every
  depth on the payee's mix would refuse Costco's `GAS` branch, the one answer the tree is surest of.
  **A candidate can only ever be a category that payee has already carried**; a first-ever payee
  proposes nothing at all. Replayed cold over the real ledger's first year (625 accepted rows) it
  auto-fills 29% at 97% precision with zero never-seen proposals, against the old keyword table's
  67% fire rate at 74% precision, half of whose errors were a category from an unrelated merchant.
  Text-less evidence (a register edit, 30 years of imported Quicken rows) updates the payee TALLY
  only and never the trie — with no tokens to walk it would all land on the root, where mismatched
  categories accumulate and kill the confident cases. `MIN_COUNT` is 2 (by request, same as a
  rename); below it, for a SETTLED payee, `predict_fields` asks the register's QuickFill what it
  would pre-enter for that payee typed by hand — an auto-filled payee must not do worse than a
  typed one — unless review history already shows the payee to be a catalogue.
- `category_rules.py`, `transfer_rules.py` — `keyword -> category_id` / `keyword -> account_id`,
  sharing their tokenizer with each other via `keywords.py` so they stay in lock-step.
  **`category_rules` no longer learns.** One correction minted a GLOBAL keyword rule, which is how
  the town name `ANYTOWN` became a Utilities rule that fired on Subway, O'Reilly and the youth
  theater. `learn_from_edit` is deleted and migration 57 purged every row it had written (all 228 on
  the reference ledger — 67 carried multi-token keywords, which only `refine_keyword` produces and
  the Rules manager's Add dialog cannot). Nothing writes the table automatically now, so a row in it
  is a deliberate Rules-manager entry and `predict_fields` honours it directly, *after* the tree
  declines. That is the one place a category outside the payee's own history can be filled in, and
  it is there because the user typed it. `transfer_rules` still learns — it maps statement text to
  an ACCOUNT, a far smaller and less ambiguous target.
- `categorize.py` — payee-level `import_mappings` keyed on the *normalized* payee, with a `source`
  ranking where an explicit user choice outranks an inferred one.

## Compartment H. Scheduled pre-entries, reminders and the calendar

Recurring bills and loan payments pre-entered as placeholder rows before
the bank transaction arrives, and the forward-looking views built on
them. A pre-entry matches on TOLERANCE, not exact cents.

Code: `mammon/scheduled.py`, `mammon/loans_schedule.py`,
`mammon/loans.py`.

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
  not roll it back (Quicken's behavior too).
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
- **A prediction is never shown beside the reminder for the same bill.** The
  "a scheduled payee is not predicted" guard above compares the definition's
  stored payee text with the ledger rows' payee text, so any drift between
  them — a rename applied after the definition was written, different bank
  text — lets the same bill through as both a reminder and a prediction; and
  the entered-row guard only suppresses a prediction in a month that already
  has the payment posted, so the duplicate was invisible in the current month
  and appeared in the next one. Therefore the projection reconciles its two
  sources where they merge (`mammon/projection.py`): a PREDICTED event is
  dropped when a SCHEDULED event on the same account falls within three days
  of it (the same window as the entered-row guard) and agrees on **either**
  the normalized payee key **or** the signed amount. Either, not both,
  because the case this exists for is payee text that drifted; requiring both
  would leave it duplicated, requiring neither would collapse two genuinely
  different bills that share a day. The suppression is one-directional: what
  the user defined always wins, an entered row and a reminder are never
  dropped, and two definitions never suppress each other.
- **One payee can be several bills, and is predicted as several.** A payee
  whose history is really several interleaved series — a phone carrier paid for
  four family members in rotation, each line billed every four weeks, so the
  payee posts about weekly — was read as ONE much-too-frequent series: a weekly
  payment of an arbitrary amount, four estimates a month matching no real bill.
  So a payee's rows are clustered into SUB-STREAMS before any interval is
  fitted (`predictions.split_substreams`) and each sub-stream is fitted,
  amounted and predicted on its own. Amount is the cluster key, within two
  percent floored at fifty cents — tight on purpose, far tighter than the
  one-third band that lets a single series' amount drift — and memo, then
  category, break the tie when several lines cost the same. The split is kept
  only when it is clearly the better reading: two or more sub-streams that EACH
  pass the same interval, amount-consistency and minimum-occurrence tests a
  whole series must pass, each still posting within one and a half of its own
  periods of the payee's last row, together accounting for at least half its
  occurrences. Anything less falls back to reading the payee as one series, so
  an ordinary bill — including one whose amount drifts, which clusters into
  singletons — keeps exactly the prediction it had before, and a bill whose
  amount went up for good is predicted once at the new amount rather than twice
  (the old amount's cluster is stale). A sub-stream with too few occurrences is
  simply not predicted: predicting it badly is worse than leaving it out. The
  sub-stream predictions are ordinary predictions — they pass through the same
  merge, the same entered-row and reminder dedup above, and the same per-payee
  dismissal (dismissing the payee dismisses all of its streams).
- **The calendar colors what it shows**, in both themes: scheduled payment
  red, scheduled deposit green, predicted payment yellow (amber on white),
  predicted deposit blue, pending pre-entry muted, entered row plain; a legend
  sits under the grid. Today's highlight comes from the theme: a fixed light
  green under dark-mode text had made today the one unreadable day.
- **The calendar's month title is a fixed width**, measured over all twelve
  month names in the title's own font, so the ◀ and ▶ month-step arrows keep
  exactly the same position whatever month is shown and can be clicked
  repeatedly without looking.
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
- **A transfer INTERNAL to the displayed account set is not shown at all —
  neither leg.** User-stated: "when both ends of the transfer are in the list
  of accounts we're showing info for, we have both a positive and negative
  amount with the same Payee. Since such a transfer is net-neutral, we
  shouldn't show either side. Only show transfers into or out of the account
  set, not within." The rule is scoped to the accounts actually being
  projected, so the same transfer keeps showing when only ONE end is displayed
  — that is real money arriving or leaving. It applies to every source that
  can produce a paired leg: an entered row, a transfer definition's two legs,
  and a projected loan payment whose funding account is displayed beside the
  loan. Suppression is read-side only and cannot move a number: the daily
  balances, opening, closing and low are identical with and without the
  internal transfer, because a dropped pair sums to zero and a leg dated
  before the window sits on both sides of the opening balance. Keeping that
  true is why an entered pair is dropped only when the two rows name each
  other through `transfer_pair_id` AND their amounts cancel exactly — a
  cross-currency transfer (legs in two currencies) and a split whose transfer
  sits on a split line (a pre-entered loan payment, where the funder also pays
  interest the loan account never receives) do NOT cancel, so those keep
  showing both legs rather than understating an outflow.
  The Projected Balances dialog goes through the same `projected_events` and
  so behaves identically; `show_internal_transfers=True` is the one switch
  back, and nothing in the app passes it. Coverage:
  `mammon/tests/test_projection_internal_transfers.py`.


### Implementation notes

How this compartment is built, and what each shape
prevents. Moved out of CLAUDE.md 2026-09-11: it is
reference for whoever works here, not per-call context.

#### Scheduled pre-entries vs. imports

Loan payments (`loans_schedule.py`) and generic recurring bills (`scheduled.py`) are pre-entered as
`scheduled=1` placeholder rows before the bank transaction arrives; the real import then *merges
into* the placeholder. These definitions are app-generated and never present in any QIF/OFX/CSV —
importers neither read nor write them, and `core._find_dup_cash` only matches `scheduled=0` rows, so
definitions and placeholders both survive a re-import. Regression:
`test_scheduled.py::test_scheduled_definitions_survive_qif_reimport`.

## Compartment I. Reconciliation

Reconciling a register against a paper or downloaded statement, and the
audit trail for anything that changes a row after it is reconciled.

Code: `mammon/ledger.py`, `mammon/ui/reconcile_dialog.py`.

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


### 5.11b Reconciling SHARE balances in investment accounts
**The share balance is to a security what the cash balance is to a bank
account** (user-stated), so every investment account gets a reconcile of its own,
per security, against the statement's share counts. The case that forces it: a
401(k) holding the plan's own internal funds has **no ticker** (`ticker_of`
returns "" — Compartment D), so no quote and no downloaded share count exist, and
the statement's share figure is the ONLY truth available. Accurate share balances
are a problem for listed securities too; this is how they are asserted rather
than assumed.

- **Scope: quantities, not value.** A plan statement prints, per fund, starting
  and ending shares, starting and ending price, and starting and ending value.
  The share reconciliation needs only the **first pair**; the prices are carried
  in the draft for the dialog's value columns and are never part of the
  arithmetic. What the user clears is **every transaction that changes the number
  of shares** for that security — acquisitions, disposals, short legs and splits
  (`investments.is_quantity_action`). A Div/IntInc/RtrnCap row moves cash or
  basis only and never appears as a line; a voided row is out of the share math
  (5.8-series void convention).
- **The shapes mirror the cash reconcile one-for-one**, deliberately, so the two
  cannot drift: `share_reconcile_summary` / `finish_share_reconciliation` /
  `get`-`save`-`clear_share_reconcile_draft` against
  `ledger.reconcile_summary` / `finish_reconciliation` / the cash draft
  functions. Per-row `cleared` and `reconciled` flags live on
  `investment_transactions`; a finished period is a `share_reconciliations` row
  keyed `(account_id, symbol, statement_date)`; an unfinished one persists in
  `share_reconcile_drafts`, keyed per `(account, security)` (schema v65,
  Section 4). Quantities are **Decimal-encoded TEXT** in and out, never floats.
  **`mammon/investments.py` is the only writer** — no second write path.
- Like the bank reconcile, no start date is asked for: the window is everything
  not yet reconciled through the statement date, and the same bound applies to
  the summary and to the UPDATE that stamps rows, so the set that zeroed the
  difference is exactly the set that gets locked. `prior_qty` comes from the
  already-reconciled rows; a typed statement starting count is accepted as an
  override (`starting_qty`) for a security whose earlier history was never
  entered. Statement dates are ISO and validated, never free text.
- **A SPLIT rescales the running balance; it is not a share delta.** Summing
  buys and transfers in, minus sells and transfers out, is wrong across a split
  by the whole ratio. On the split's date the running share balance is
  multiplied by M/N and the replay continues with the later transactions. This
  reuses the **existing `StkSplit` action** and the same exact-Fraction
  `investments.apply_split` the holdings replay uses (Compartment D, split_num /
  split_den, migration 34) — there is no new action and no second definition of
  a split, so a reconciliation and the Holdings window can never disagree. A
  split is not a line the user checks off: it is history the statement's share
  count already reflects, so it is reported separately and stamped along with
  the cleared lines when the period is finished.
- **A renamed security is ONE identity, summed BEFORE any difference is
  reported.** Quicken offered share adjustments that were sometimes enormous,
  and the usual cause was a security whose name had changed without the earlier
  transactions being renamed. Mammon has the alias table for that
  (`security_aliases`, 5.8-series / Compartment D), so every quantity is summed
  over the canonical symbol AND all of its aliases, via the `resolve_symbol` /
  `_identity_symbols` chokepoint, before a difference is shown and long before
  an adjustment is offered. The dialog can show WHICH spellings were summed
  (`share_identity`), and reconciling under an old spelling resolves to the
  canonical period.
- **The adjustment is an ordinary transaction, marked and deletable.** When the
  stated ending share count still cannot be reached, the remaining gap may be
  booked as a `ShrsIn`/`ShrsOut` row dated the statement date, with no cash
  effect, carrying `investments.SHARE_ADJUSTMENT_MARK` on its memo and linked
  from `share_reconciliations.adjustment_txn_id`, so it stays identifiable
  forever (`list_share_adjustments`).
- **The user must be told, up front, what deleting it later means**
  (`investments.SHARE_ADJUSTMENT_WARNING`, which the UI shows verbatim): they
  CAN delete the adjustment if the missing shares later turn up, but deleting it
  does not undo the reconciliation — **the affected period has to be reconciled
  again by hand**. `delete_share_adjustment` therefore deliberately unwinds
  nothing else: the rows stay stamped, the `share_reconciliations` row survives
  as the record that the period was reconciled, and it merely loses its link and
  gains a note saying the adjustment was removed. A reconcile is a stamped fact
  about rows; deleting one transaction cannot honestly un-stamp them.
- Finishing refuses a non-zero difference, exactly as the cash side does, unless
  an adjustment is booked; finishing the same `(account, security, statement
  date)` again is idempotent (one row, nothing re-stamped, the adjustment link
  preserved).

**Where the stated share counts come from: the holdings SNAPSHOT import.**
Quicken received positions along with the transactions it downloaded; a manual
download usually cannot. The user can fetch quotes and balances as a CSV, but
**not in the same file as the transactions** (a webSlinger extraction may
eventually hit both at once — Compartment L). So a share reconcile needs a
second, non-transaction import shape:

- **A snapshot is a statement of fact, and NEVER becomes a transaction.**
  Nothing on this path reaches `importers/core.py`'s insert/dedup path, and
  re-importing the same statement is a no-op: the price is keyed `(symbol,
  date)` and the reconcile target `(account, security)`, so there is no history
  to duplicate. Turning a position line into a trade would invent history that
  never happened.
- **Funds match by symbol when the file prints one and BY NAME otherwise**,
  because a 401(k) export of the plan's internal funds usually has no symbol
  column at all. Both routes end at the canonical symbol through `securities` /
  `security_aliases` (so a statement still printing an old fund name lands on
  the new identity), and a line matching nothing is **reported, never guessed
  at**.
- **The snapshot price is recorded** in `price_history` under its own source
  (`investments.SNAPSHOT_PRICE_SOURCE`). For a tickerless plan fund this is the
  only price that will ever exist, since no quote feed can price it; where the
  file prints a market value but no price, the price is derived from
  value / shares as Decimal, never a float.
- **Each stated ending count is left in the per-security reconcile draft** —
  `statement_date`, `ending_qty`, `ending_price` — which is exactly where the
  dialog opens on it (`investments.snapshot_targets`). Import then reconcile is
  one sitting with nothing retyped.

**The dialog (`mammon/ui/share_reconcile_dialog.py`).** The cash reconcile
workspace, multiplied per security, because the answers do not add up to one
number — missing shares of one fund say nothing about another:

- A top table lists **every security on the account** with the statement's
  starting and ending share counts (both editable in place, pre-filled from the
  draft and otherwise from the book quantity), what clearing has explained so
  far, and the difference still unexplained — green at zero, red otherwise.
- **One TAB per security** lists only that security's quantity-changing rows.
  Clicking a row toggles its cleared mark, persisted immediately, so a
  half-finished reconcile survives closing the window; a reconciled row shows
  `R` and is not the user's to un-clear, and a split shows as history rather
  than as a line to check off.
- Finish reconciles the **current** security's period and leaves the window
  open, because one statement covers several funds. A residual difference is
  closed only by an adjustment the user explicitly accepts, and
  `SHARE_ADJUSTMENT_WARNING` is shown **verbatim in a permanent label on the
  window** — not only in the prompt that is clicked through once — because what
  it warns about is discovered years later.
- 'Import Holdings Snapshot…' runs the import above against this account and
  adopts the file's statement date. Its modal seams (`confirm_adjustment`,
  `warn`, `choose_snapshot_file`) are overridable methods, so the whole life
  cycle is driven headless in tests (Compartment M, headless-modal hazard).


## Compartment J. Reports, charts and customization

Read-only aggregations and how they are presented: the shared
customization bar, chart behavior, saved filter sets, export and print,
and budgets.

Code: `mammon/reports/` (pure functions returning plain data),
`mammon/ui/report_filters.py`, `mammon/ui/widgets.py`.

- **Long-term lots COMBINE; short-term ones do not** (`combine_long_term`,
  2026-09-21). Reported: "all the lots that are long-term already should be
  combined ... that is fine for short-term capital gains, as the time to become
  long-term could vary." The columns that earn a short lot its own row are dead
  on a long one -- `long_term_on` is past, `days_to_long` is 0, there is no
  deadline to price, and every long lot taxes at the same rate. Reinvested
  dividends make it acute: each reinvestment opens a lot, and a real position in
  the user's ledger carries 92 open lots, 88 of them long.
- **The grouping is per account, security AND SIGN.** Summing a long lot at a
  loss into one at a gain nets them, and the harvestable loss -- the thing a
  December report exists to surface -- disappears into a figure that is true
  about the position and useless for deciding what to sell. `unknown`-term lots
  are never folded in either: a lot whose acquisition date was never recorded is
  a question, not a long holding. Totals are unchanged by construction, which is
  asserted, because this report has to tie to the Holdings window.
- **A combined row states its SPAN**, oldest to newest acquisition plus the lot
  count ("2019-03-04 - 2021-11-12 (88 lots)"). The oldest lot's date alone would
  read as a single acquisition and misdate the rest; blank would look like the
  unknown-term line, which means something else. `group_long_term=False` gives a
  row per lot, which is what a specific-identification sale needs.
### 5.9 Reporting
- Priority: a spending report itemized by category over a selectable time
  period (month/quarter/year/custom).
- Then other Quicken-equivalent reports (income vs expense, net worth over
  time, cash flow, account balances).
- A chart states the units it is drawn in. The shared price-history canvas
  (`ui/charts.PriceHistoryCanvas`) takes a `currency` argument from the window
  that opens it and labels the y axis, the plot title and the dialog title with
  it, printing a bare `$` on the ticks only for USD; see §5.8 for the rule that
  the currency is the ACCOUNT's, not the security's.


### 5.9c The universal report customization bar
- ONE control set for every report (`ui/report_filters.ReportFilterBar`, opened
  by the gear as `CustomizeDialog`): **time range, accounts, categories, and
  Include hidden accounts**. Every reporting dialog — Spending by Category,
  Spending Pie / Spending Chart, Income Pie / Income Chart, Itemize, Net Worth
  Over Time — shows the same bar AND the same Period dropdown; the three chart
  windows (Net Worth over time, Income by category, Spending by category) were
  once the odd ones out with no Period dropdown at all, and now host the shared
  one like the rest.
- **A check-list is shown only where it filters — and where a report CAN filter,
  it must.** `ReportSpec.show_accounts` / `show_categories` decide, per report,
  which of the two lists the gear popup renders; a report whose pure function
  takes no such argument must leave the flag off rather than offer a control
  that silently does nothing. The converse is equally binding: a report that
  groups by category may not withhold the category list. The matrix, which was
  full of holes and is now complete:

  | Report | Accounts | Categories | Scope |
  |---|---|---|---|
  | Itemize by Category | yes | yes (`reports.itemize_tree(top_level_names=…)`) | **both** |
  | Income by Category (pie) | yes | yes (by name over `reports.income_category_rows`) | **income** |
  | Spending by Category (pie/chart) | yes | yes | **expense** |
  | Cash Flow | yes | yes (`reports.cash_flow(top_level_names=…)`) | **both** |
  | Income vs Expense | yes | yes (`reports.income_expense(top_level_names=…)`) | **both** |
  | Transactions | yes | yes (`reports.transactions(top_level_names=…)`) | **both** |
  | Net Worth Over Time | yes | yes (the what-if below) | **expense** |
  | Account Balances | yes (`reports.account_balances(account_ids=…)`) | no — it does not group by category | — |
  | By Payee, By Tag, Investment Performance | yes | no — no category grouping | — |

  Each of those `run` functions really consumes `filters.selected_categories()`;
  a visible check-list that does not reach the pure function is the same bug in
  a new place. Two consequences of filtering in the reports layer rather than
  the projector: **Account Balances' total is net worth over exactly the rows
  shown**, never the whole ledger's, so the table and its total cannot disagree;
  and **Cash Flow's Transfers section is deliberately left whole** by a category
  pick, because a transfer carries no category and silencing the section would
  leave Net claiming money stayed put when it moved. In the Transactions
  listing, rows with no category — uncategorized entries and transfer legs —
  match no category filter, so narrowing drops them.
- **Restricting to a top-level category always includes its sub-categories**
  (`reports._lines.top_level_ids_for_names`, which expands each named top level
  through `category_subtree`). "Just my Church spending" must keep every
  Church:* row, expandable, or the restriction answers a different question than
  the one asked. **Expansion is skipped when the pick came from the category
  TREE** (`reports.listing.transactions(expand_subtree=False)`): there a parent
  can be ticked with one child deliberately unticked, and re-expanding the
  parent would put the excluded child straight back.
- **ONE category picker, parameterized by scope.** Category narrowing is
  available per report with an explicit scope — **expense, income or both**
  (`report_filters.CATEGORY_KIND_*`, declared per report as
  `ReportSpec.category_kind` and passed to `CustomizeDialog(category_kind=…)`;
  the older boolean `show_categories` is kept in step with it, a bare True
  meaning `both`). There is exactly one builder,
  `report_filters.category_picker_tree` over `category_types.category_forest`
  (with `category_picker_names`/`top_level_categories` still answering for the
  callers that only want the top-level names), and every report —
  window-driven or chart dialog — consumes it with the scope in the matrix
  above. Four ad-hoc lists is how this went wrong; adding a fifth is how it
  comes back.
  - **The picker is a checkable TREE, not a list of top levels.** A parent
    expands to its children, to any depth, and every row is individually
    checkable, because a top-level-only picker cannot say "Taxes:Federal but
    not Taxes:Property" — which is the ordinary shape of a tax report.
    Scope still applies at the TOP level only: a subtree is offered exactly
    when its top level is of the requested kind.
  - **Checking a parent implies its whole subtree; unticking one child leaves
    the parent PARTIALLY checked**, read as "this parent's own postings, plus
    the children still ticked". A parent therefore clears completely only when
    its own tick is gone — unticking the last child of `Taxes:Federal` must
    leave Federal partial, or Federal-without-Withholding would be
    unsayable. The one thing three states cannot express is "my children but
    not my own postings": ticking a lone child rounds the parent up to ticked,
    the visible and correctable direction to err in. Propagation is Python on
    `itemChanged`, not `Qt.ItemIsAutoTristate` (which also makes a user click
    cycle through partial, and whose offscreen behavior across Qt versions is
    not something the tests should have to pin).
  - **The selection travels to the reports as category IDS, at any depth**
    (`ReportFilterBar.selected_category_ids`, the ticked-or-partial rows).
    `ledger.rename_category` keeps the id, so a name-keyed filter silently
    drops a category the moment the user renames it; ids are also the only
    spelling that can name a single sub-category. Every category-filtered
    report takes `category_ids=` and really drops an unticked sub-category
    from the BODY and from its parent's rolled-up total, so the numbers on
    screen sum to what is shown. `selected_categories()` (top-level names) is
    kept for the callers that group by top level.
  - The tree answers `count()`/`item(i)`/`addItem()` over its top-level rows
    and defaults the column on `text()`/`checkState()`/`setCheckState()`, so
    everything written against the earlier flat `QListWidget` picker keeps
    working unchanged. Those three are non-virtual inline wrappers in Qt;
    the virtual `data()` is deliberately left alone.
  - **The names come from the ledger's category TREE, never from a report
    aggregation.** A picker sourced from `reports.spending_by_category` can only
    ever name money-OUT categories, so income was un-tickable by construction,
    and it silently dropped any category with no activity in the current range,
    making a tick vanish when the dates moved. The tree answer includes
    zero-activity categories and is range-independent; hidden top levels are
    excepted.
  - **A top level's side is decided by its ROLLED-UP subtree**, matching how
    `itemize_tree` picks the section it will render under — otherwise "Rental",
    whose money all sits on "Rental:Rent", would be offered on the wrong side
    and the ticked name would never appear where the user looked for it. A
    declared `categories.type` wins over the derived sign, which is what lets a
    never-used income category be listed as income at all.
- **Itemize by Category's picker is scoped `both`**, which for a while it was
  not: first the reusable report window hardcoded the list off, so Itemize's
  account picker worked while its category filter was dead; then the list was
  built from a spending-only source, so the report showed an INCOME section
  whose categories could not be selected — reported as unacceptable. Restricting
  to a subset is the point of the report — "a report of just my Church
  spending", or Rent income plus Landlord expense for a whole rental summary,
  and the second ask needs both signs. The filter applies BEFORE income/expense
  classification, so a pick spanning both signs keeps both sections, each still
  expanding to its sub-categories and their transactions, and an income-only
  pick renders the INCOME section rather than an empty report. All checked means
  no filter; an EMPTY pick also means all — for every category-filtered report,
  through the one `report_window._category_ids` helper, whose `or None` collapses
  the empty pick — since Clear-all is the first half of "clear, then tick one"
  and an empty report reads as a broken one.
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


### 5.9a Report computations as a tool surface (roadmap item 4)
The aggregations Quicken ships as windows exist first as pure functions in
`mammon/reports/`, returning plain dataclasses, so the MCP server (item 3)
and a future report window compose the same deterministic numbers. Shared
extraction lives in `reports/_lines.py` and fixes three rules no report may
drift from: a split is represented by its lines (each inheriting the
parent's payee/tag), a transfer is excluded unless a report asks for the
legs whose other side is OUTSIDE its account set, and a scheduled placeholder
(`scheduled = 1`) is excluded unless asked for. Hidden accounts are left out
of every report unless asked for (§5.9c). Amounts are SIGNED cents throughout
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
§5.9c), a plain table below, and Export CSV, Export HTML and Print (PDF) actions.
The Period dropdown re-ranges the report from a preset (or "Custom", which opens
the gear); its **Year-to-Date** default (§5.9c) makes the first paint answer "how
am I doing this year", while Net Worth Over Time alone keeps the whole-ledger
"Earliest to date" default. Its options are the UNION of the calendar and rolling
preset families (§5.9c) — no previously offered range was dropped when the rolling
ones were added. The same dropdown was retrofitted onto the three inline chart windows
that lacked it (Net Worth over time, Income by category, Spending by category),
so every window that shows money over a range now offers the identical control.
It is built by one shared factory (`report_filters.make_period_combo`), used by
both the generalized window and the inline chart dialogs, so its width is pinned
wide enough for the longest preset — `Earliest to date` — which the surrounding
stretch layout otherwise clipped to `Earliest to d...`.
The **Period selector follows the customization date range**: it is a label as
well as an input, and always names the range the report is actually using.
When the user changes the dates inside the
customization (gear) dialog — or recalls a saved filter set that carries its own
range — the dropdown re-reads the live From/To dates on Apply and selects the
preset they equal, or reads **Custom** when they match no preset. The reverse
lookup is `report_filters.period_for_range(start_iso, end_iso, conn, today)`,
which walks the same `PERIOD_PRESETS` through the same `resolve_period`, so the
two directions can never drift apart; `report_filters.sync_period_combo` applies
it with the combo's signals blocked, because re-entering the selection handler on
`Custom` would reopen the very dialog that triggered the sync. Both the
generalized `ReportWindow` and the inline chart dialogs' shared header go through
it, so every window that shows the dropdown behaves the same way. Dates cross
this boundary as ISO strings, like every other date in the domain. **Named saved filter sets moved off the old inline row into
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
**Account Balances** (`reports.account_balances`, which reads the "To" date as
its as-of and honors the account checklist via `account_ids=` — it once hid that
checklist, because the aggregation took no account filter at all; it is a
print/export table, so its
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
the "To" date, with portfolio totals. Its seven columns — Account, Ticker, Amount
(market value), Dividends, Gain/Loss $, Gain/Loss %, Annual Return % — override the
shared three via
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
**Dividends, Gain/Loss and Annual Return are bounded to the resolved period.**
Given a `start` (the report window always passes the filter bar's From date),
each open, priced holding's figures are its total return over `[From, To]` as
§5.8d "One definition of a holding's return" defines it -- dividends included,
each once -- so `Last 3 years`, `Last 5 years` and `Last 10 years` report
DIFFERENT gains. The period path rewinds the share count to both dates. With no
`start` the span is each holding's current holding, the same figures the
Holdings window shows. The `unrealized_pl` / `pct_return` fields stay
since-purchase and unchanged (the MCP tool reads them). The Portfolio **Market
Value** headline is the open line items' gains summed, dividends included, with
one pooled annual rate, so it reconciles with the lines above it; the
Unrealized / Realized / Dividend Income lines below it are labelled "(since
purchase)" because they do not follow the period and read as contradicting the
headline unlabelled. Every figure column is right-aligned
(`ReportSpec.right_align_from`). Clicking a column header sorts the holdings
— by Dividends, Gain/Loss, Gain/Loss %, Annual Return %, ticker, or account then
ticker — reusing the exact `sort_key`/`sort_desc` seam the Itemize tree uses
(`ReportSpec.sortable` names which flat columns sort); a second click toggles
direction and the Portfolio totals never move. **Right-clicking a holding row
offers `Price history: SYM…`** — the same label and the same shared chart the
investment register's context menu opens (§5.8), drawn in the currency of the
account that row's holding sits in. The offer is gated by `ReportSpec.price_history`
(true for this report alone) and by the row itself: the bare ticker is stashed on
each populated cell at `Qt.UserRole`, so a re-sort can never chart the holding that
used to occupy that row, and a Portfolio total line or a click over empty space —
carrying no symbol — pops up no menu at all rather than an empty one.

- **Capital Gains and Taxes reports the holding-period clock, one row per open
  tax lot.** `reports.capital_gains(conn, as_of=None, *, account_ids=None,
  include_hidden=False, prices=None, long_term_rate=…, short_term_rate=…) ->
  CapitalGainsReport` answers "which of my shares are long-term, which are short,
  and what does selling early cost". Each `LotTaxRow` carries the account, symbol,
  acquisition date, shares (Decimal), cost basis and market value (cents),
  unrealized gain/loss, `term`, the date the lot turns long (`long_term_on`) and
  `days_to_long`; the report totals long and short gains and losses in SEPARATE
  buckets — a short-term loss is a benefit, not a smaller gain, and netting the
  two hides the thing the user came to see. Three shapes are load-bearing:
  - **One holding-period definition.** `investments.long_term_date(acquired)`
    (anniversary + 1 day) is the date form of the rule `RealizedGain.term`
    already applies — long only when the sale falls strictly AFTER the
    anniversary. The report calls both, so a countdown can never disagree with
    the term the realized-gain rows report. Never restate the holding period.
  - **Per-lot, not average cost.** `compute_holdings` answers "how much and what
    did it cost" and structurally cannot say WHEN the shares were acquired.
    `investments.open_lots(conn, account_id, as_of=None) -> {symbol: [OpenLot]}`
    is a read-only projection of the SAME `_replay_positions` replay behind the
    Holdings window, so per symbol the lots sum to that position's quantity and
    cost basis whatever the account's lot method (`average`/`fifo`/`lifo`) did to
    them. Non-positive lots are omitted: a negative lot is a written option's
    premium, an obligation with no holding period to run. Per-lot market value is
    allocated pro-rata from the position's own value, last slice absorbing the
    rounding, so lot values sum exactly to the Holdings figure; shares the replay
    cannot date (restored from a pre-lot-tracking snapshot) become one
    `acquired=None` / `term='unknown'` residue row rather than being dropped,
    so the totals still tie out.
  - **Tax rates are the caller's, always.** `DEFAULT_LONG_TERM_RATE` (0.15) and
    `DEFAULT_ORDINARY_INCOME_RATE` (0.24) are documented ASSUMPTIONS and
    parameters, never read from the database and never inferred: the app does not
    know the user's bracket and must not pretend to. Each short-term row carries
    the same gain taxed at both rates and `extra_tax_if_sold_now` (the difference
    of the two ROUNDED figures, so the three numbers on screen add up), plus a
    plain-language `annotation`. A short-term LOSS is annotated the other way —
    it offsets short-term gains and then ordinary income, so it is worth MORE
    before the lot turns long — and its figures are negative, a benefit.
  - **A sheltered account is not in this report at all.** *"401K, IRA and
    Roth IRA do not pay capital gains. But then you only have the name of the
    account to go by."* (2026-09-19.) The answer is the account's own
    `tax_treatment` (B, migration 74) through
    `rebalance.is_capital_gains_exempt` — NEVER the account name, because "IRA"
    appears in taxable rollover-named accounts and a 401(k) can be called
    anything. The first build showed those lots with a blank term; the user
    rejected it — *"if they're not taxed, don't put them in the report. That is
    just a lot of clutter."* (2026-09-19) — so an exempt account is now SKIPPED
    WHOLE: no lot rows, and not one cent in any total or subtotal, including
    `total_cost_basis`, `total_market_value` and `total_unrealized`. There is no
    `sheltered` term and no sheltered bucket. The single trace is
    `CapitalGainsReport.excluded_accounts` (the names) rendered as the one-line
    `exclusion_note` ("Excluded (not subject to capital gains): …") in the
    footnote, never as row data — so the omission is stated, not silent. The
    consequence is accepted: this report deliberately does NOT tie to the
    Holdings window, which is where sheltered money is read.
  - **The tax consequence is a verdict in the cell and a sentence on hover.**
    *"the tax consequences field is way to long and unreadible without expanding
    the report to full screen."* (2026-09-19.) The `If Sold Now` cell is bounded
    by `ui/report_window.IF_SOLD_NOW_MAX_CHARS` ("+$412 tax", "LT loss",
    "Term unknown") and must stand on its own, because CSV/HTML/PDF export
    carries `cells` and cannot show a tooltip; the full `annotation` is the
    cell's tooltip, and the whole-report caveats — the assumed rates, and which
    accounts were left out as tax-sheltered — are one wrapping `footnote`
    line under the table (`capital_gains_footnote`). The spec sizes the window
    and fits the columns ONCE on populate, so ten columns are readable unmaximized
    and still draggable afterwards.
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
  `{start, end, include_hidden, account_ids, categories, category_ids}`, where
  `account_ids` and `categories` are `None` for "no filter" (everything checked)
  and every list is sorted for a stable blob — and `apply_filter_state(bar,
  state)` re-applies it. The round-trip is unit-testable on a bare bar without
  QSettings. Order matters on re-apply: the include-hidden toggle is set BEFORE
  the account ticks, because flipping it rebuilds the account list (preserving
  surviving ticks by id), and setting the accounts first would lose them
  (§5.9c).
  - **`category_ids` is the authoritative half, and the only one re-applied
    when a set has any.** It carries the picker's tick at whatever DEPTH the
    user set it (§5.9c), which names cannot express, and it survives
    `ledger.rename_category` — which keeps the id, so a name-keyed set silently
    drops a category the moment the user renames it. The top-level `categories`
    NAMES are still written beside it so a set stays readable by anything that
    only understands names.
  - **Back-compat runs both ways.** A set written before the picker went
    id-keyed has no `category_ids`, so re-apply falls back to resolving the
    names (ticking each named top level and its subtree, as ticking a top level
    has always meant) and the state left behind is id-keyed from then on. In
    the other direction `category_ids` is OMITTED entirely when the picker is
    in its no-filter state, so a no-filter set still serializes byte-for-byte
    as it did before the tree existed. Name resolution deliberately matches on
    the row TEXT rather than resolving through ids first: a bar built with an
    explicit `categories=` list may be showing synthesized buckets that are not
    rows in `categories` at all, and resolving those would tick nothing.
- **Storage is one JSON blob under one key.** The whole `name -> state` mapping
  is kept as a single value under `reports/saved_filters` rather than one key per
  name — QSettings treats `/` in a key as a group separator, so a single blob
  lets a saved-set name contain any character. `save_filter_set`,
  `load_filter_set`, `delete_filter_set` and `saved_filter_names` take an
  injectable QSettings store (the test seam), matching how `ui/prefs.py` is
  exercised. The name prompt (`ReportWindow._prompt_filter_name`) is an
  overridable seam so headless tests never block on a modal.


### 5.9r Taxes and custom reports
The reports of §5.9a/§5.9b are fixed aggregations: the code decides what a row
means and the user only chooses a period and some accounts. A tax return is the
other shape. Its lines are decided outside Mammon, they are stable for a year
and then change, and which of the user's categories feeds which line is a fact
only he knows. **A custom report is therefore a STORED DEFINITION the user
edits, not a computation the code names** — a named list of report ITEMS, each
of which says how to produce one signed amount, evaluated against a date range
on demand. Nothing computed is ever stored: re-pointing a definition at a
different year changes every number with no edit to the definition, which is what
makes "last year's return, this year's numbers" a two-second operation instead of
a rebuild.

**The model.** `mammon/reports/custom.py` is the domain layer (UI-free, read-only
against the ledger's transaction rows; it owns its own definition tables and
nothing else). A `report_defs` row is one report: a unique name, a `kind` of
`tax` or `custom`, an optional `definition_id` naming the year definition it was
built from (§below), and a range binding. Its `report_items` rows are ordered by
`seq` and unique by `name` within the report; each carries a `label` (what the
user wants printed, defaulting to the name), an optional `group_label`, a `kind`,
a `sign` of +1 or -1, and a `tag_enabled` flag. An item's selections live in
side tables keyed by INTEGER ID — `report_item_categories(item_id, category_id,
include_subtree)`, `report_item_accounts(item_id, account_id)` — so renaming a
category or an account cannot silently empty a report line. The one exception is
`report_item_securities(item_id, symbol)`, keyed by symbol text because
`securities` has no integer id; symbol renames are already handled by
`security_aliases` and `investments.resolve_symbol`.

**A category selection stores a RULE, not a snapshot.** `include_subtree = 1`
means "this category and everything under it, as the tree stands at evaluation
time", so a subcategory created next March is picked up by the tax line that
already covers its parent. `include_subtree = 0` means that one category alone.
The user expresses this with the ordinary tri-state `CategoryTree` (§5.9c): a
fully ticked parent saves as a subtree rule and its children are not enumerated;
a partially ticked parent saves as itself alone plus whatever is ticked beneath
it. Because a merge or a delete in Categories… moves the id a report line points
at, that dialog now reports how many report items reference the doomed category
before it asks for confirmation (`ui/categories_dialog._report_item_note`,
`custom.items_using_category`). It is a warning and never a veto — tidying the
tree before a merge is the normal case — but it is the one consequence the user
cannot see from the category tree itself.

**Item kinds.** All seven the schema admits are evaluated
(`custom.ALL_KINDS == custom.IMPLEMENTED_KINDS`); the two tuples stay separate
anyway, because a definition file may name a kind a future build drops, and the
refusal in `evaluate` has to SAY so rather than silently reading zero — a tax
line that reads 0.00 when it means "unimplemented" is the worst possible failure
here.
- `SOSC`, sum of selected categories: the signed net of every register LINE — an
  unsplit transaction or one split leg — whose category is in the item's
  selection, over the range.
- `EDAB`, end-date account balance: `investments.display_balance` at the range
  end, summed over the selected accounts.
- `SDAB`, start-date account balance: the same, taken as of the day BEFORE the
  start, so that `SDAB` plus the range's flows equals `EDAB` with no off-by-one
  at the boundary.
- `HOLDVAL`, holdings value at the range end over the selected accounts, narrowed
  to the selected securities. An EMPTY security selection means every symbol
  held, not none: "what is this account worth" would otherwise go stale the day a
  new holding is bought.
- `RGAIN`, realized gain booked by disposals dated in range
  (`portfolio.capital_gains`), filtered by `options["term"]` — `short`, `long`,
  or `all`. `all` is the default and counts a lot whose holding period could not
  be determined, because dropping it would understate the total silently.
- `NETGAIN`, `(EDAB − SDAB) − (money in − money out)`: what the selected accounts
  gained that was not contributed. Flows come from `portfolio.external_flows`,
  which reconciles ledger transfer legs against the `XIn`/`XOut` rows recording
  the same movement, so a contribution is subtracted once; a transfer between two
  SELECTED accounts cancels, which is what "net of the set" has to mean.
- `COMPUTED`, arithmetic over the other items of the same report — see §5.9t.

`EDAB`, `SDAB`, `HOLDVAL` and `NETGAIN` are `PRICED_KINDS`: their number depends
on a security price, and so can come back incomplete (§5.9t).

**The tag override.** Category selection is right for most lines and wrong for
the handful a user argues with his accountant about, so an item's NAME doubles as
a Mammon tag when `tag_enabled` is set. These are ordinary tags in the existing
`tags`/`transaction_tags`/`splits.tag_id` machinery of §5.6 — no parallel
vocabulary, and the tag row is created lazily on first real use, not when the
item is created. `ledger.validate_report_item_tag_name` forbids a comma and a
leading `!`, and enforces case-insensitive uniqueness ledger-wide among
tag-enabled item names, because `tags.name` is `COLLATE NOCASE`. Precedence is
fixed and deliberately one-directional:
1. **Exclusion wins absolutely.** `!Name` on a line beats `Name` on that same
   line and beats category selection. There is no way back in.
2. **Inclusion beats category selection.** `Name` pulls a line in even though
   its category is not selected.
3. **Category selection is the default, and only on `SOSC`.** The balance kinds
   have no category selection to override, so a tag on them does nothing.

**`tag_enabled` gates INCLUSION only; exclusion is always honoured.** Rule 2 is a
standing promise about a whole vocabulary — *any* line tagged `Name`, now or in
future, joins this item — and an item has to opt into that. Rule 1 is not that
promise reversed: `!Name` names one posting the user is looking at, it can drag
nothing in, and there is nothing to opt into. Gating both on the flag was a
defect: the drill-down's exclusion gesture (§5.9u) wrote the tag and 67 of a real
report's 68 lines ignored it — the number did not move, the row was not struck
through, and the ledger's tag vocabulary silently collected an entry that meant
nothing. A `!Name` tag is therefore matched against every `SOSC` item's name,
tag-enabled or not.

**A comma in an item's name makes it unexcludable, and that is refused up
front.** `transactions.tag` is a comma-joined cache parsed back by `parse_tags`,
so `!Schedule B:Div inc., non-taxable` would store and read back as two tags —
an exclusion matching nothing, plus junk in the vocabulary. This is the same
constraint `validate_report_item_tag_name` already imposes on a tag-ENABLED
name, applied to the other half of the mechanism; the packaged Quicken tax
definition has fourteen such names, so it is the common case rather than a
corner. `ledger.toggle_report_exclusion` raises before writing anything, and the
drill-down's menu shows the entry DISABLED with the reason rather than a
working-looking one that fails on click.

On a split, tagging the parent pulls in every leg at its own amount and tagging
one leg pulls in only that leg; a parent `Name` with `!Name` on one leg carves
that leg out and leaves its siblings in, because a leg's effective tag set is the
parent's union its own and rule 1 then applies.

**A transfer has no category, so an `SOSC` item reaches one by TAG or by
selecting the account at its far side** — rule 4, and in both cases it is that
leg only, at that leg's own sign. The selection exists because a tax line is
routinely stated NET of money that merely moved: W-2 box 1 wages are gross pay
less the 401(k) deferral, and that deferral is a transfer leg of the paycheck
carrying no category at all, so an item summing categories could not express the
figure the form asks for. (The same arithmetic is what a tithing report wants of
the same paycheck.) Matching on the FAR side is what makes it single-sided: a
transfer contributes two rows to `signed_lines`, and only the one sitting outside
the selected account points AT it, so selecting `[401k]` picks up the paycheck's
leg and not its mirror. Selecting BOTH accounts of one transfer does match both
rows — a real double count, reported through `transfer_double_counted` exactly as
tagging both legs is, never silently halved.

The selection is stored in `report_item_accounts`, the table the balance kinds
already use for "which accounts to value". The reuse needs no migration
(migrations are append-only and an `SOSC` item never had a row there) and cannot
collide, because an item has exactly one kind; `custom._selected_transfer_ids` is
a separate accessor from `item_accounts` so that a reader lands on the meaning
its kind gives the rows rather than on whichever accessor came to hand. Tags are also what an item can be SUBTOTALED by, one
sub-line per tag value, which is §5.9u.

**Coverage is computed, and shown AT the number it is about.** `evaluate`
returns an `Evaluation` (report id, name, resolved start and end, the rows, a
`Coverage`, and — with `detail=True` — the lines each item saw), and the report
always renders, whatever Coverage found. Four things are computed because each is
invisible otherwise and none is safely auto-fixable:
- `multi_claimed` — lines claimed by two or more items. Expected and allowed: one
  category legitimately feeds several tax lines. It is also why the items of a
  report do not necessarily sum to anything meaningful.
- `unclaimed` — lines inside the union of the report's category selections that
  no item ended up claiming, reachable only by an exclusion tag on every item
  that selected the category. On a tax report this is money left on the table.
- `transfer_double_counted` — both legs of one transfer pulled into the same item
  by inclusion tags, which is a guaranteed double count. Detection is deliberately
  conservative (a mutually linked `transfer_pair_id` whose amounts cancel exactly)
  and it is warned about, never silently de-duplicated, since an
  external-looking pair can be legitimate.
- `ignored_tags` — an item that is tag-enabled but whose kind cannot honor a tag.

`Coverage.clean` is true only when all four are empty.

**There is no Coverage PANEL, and that is the requirement.** One existed: a text
block under the report listing every finding. It printed EVIDENCE — one line per
transaction, with its date and amount — where the user needed FINDINGS, and on a
real tax report that meant eighty entries standing for thirteen facts, thirty-six
near-identical rows for two of them, and a forty-four-name comma run-on for the
setup list. Worse, it ordered them by category rather than by consequence, so the
two findings that mean a figure is WRONG printed below the wall of the one that
means "this is normal", and on a report where both of those were clean it said
nothing at all. A user cannot act on that, and a warning surface nobody reads is
worse than none, because it looks like diligence.

Every finding is therefore attached to the row it is about, in the drill-down
(§5.9u):
- `multi_claimed` and `transfer_double_counted` become an amber warning triangle
  ON each item involved — the app's one warning mark (`ui.models.warning_triangle_icon`) —
  carrying the explanation as a tooltip. The tooltip text is built in the DOMAIN
  layer (`custom.DrillMark`) because it is a statement about the arithmetic, and a
  rule the user can only reach through a tooltip is still a rule that must be
  testable without Qt.
- A line an exclusion tag removed is drawn struck through in place, carrying its
  real amount. `Coverage.unclaimed` answers a report-wide question and is a
  different set from "lines THIS item lost"; the latter is `ItemDetail.excluded`,
  and it holds only lines that would otherwise have been admitted — never every
  line in the ledger carrying a `!tag`.
- `ignored_tags` and the "nothing selected yet" list are not surfaced. A line with
  no selection reads `0.00` with no children, which is the same shape as a line
  that genuinely came to zero. This is a known and accepted loss of signal.

**Year definitions, Create and Update.** A tax year's line-up is data, not code.
`mammon/reports/report_defs.py` loads definition FILES — JSON canonically, YAML
if the optional parser happens to be present — from `paths.report_defs_dir()`
first and the shipped `mammon/report_defs` second, so a user's own definition
shadows a shipped one of the same id. A definition names its `version`, `id`,
`family`, `title`, `kind`, `year`, a `default_range`, and its `items`, and each
item may carry `migrated_from` naming its predecessor line. **Create**
(`create_from_definition`) builds the report and its items with NO selections at
all: it evaluates to zeros on purpose, and `unassigned_items` is the resulting
to-do list. If item insertion fails partway the whole report is deleted, so a
form is never left half-built. **Update** (`update_report`) never mutates the
source: it builds a fresh report from the new definition and then, for each new
item with a `migrated_from` pointer, copies the predecessor's category, account
and security selections, plus `sign` and `tag_enabled` only where the new
definition left them unstated — the definition wins on anything it states
explicitly. A kind change between the two is a refusal, not a coercion; nothing
is copied and the item is reported. The returned `MigrationResult` spells out
`carried`, `new_lines`, `dropped` (with the old selections written out by NAME so
they can be reassigned by hand), `kind_changed` and `unresolved`, and is `clean`
only when nothing needs attention. Many-to-one merges are not supported in this
version and raise rather than guessing.

**Ranges.** A definition binds one of three `range_kind`s: `fixed` (explicit ISO
`range_start`/`range_end`), `calendar_year` (`range_year`, resolved to Jan 1
through Dec 31), or `preset` (a Period preset name from the customization bar of
§5.9c, resolved against today). `custom.resolve_range` is the single dispatch and
raises on a kind whose supporting field is missing. `evaluate` accepts an explicit
start and end that override the binding without touching it, which is what lets
one definition be evaluated across several years for comparison without copying
it.

**The window.** Reports ▸ Taxes and Custom Reports…
(`ui/custom_report_window.CustomReportWindow`) is a modeless `QDialog` like the
other report windows, shown and never `exec_()`-ed, because a user assigning
fifty tax lines needs his register beside him. It is a thin projection in the
usual sense — no SQL and no money math, every number from `custom.evaluate` and
every string from `fmt_cents` — and it has three shapes worth stating as
requirements:
- **The item table never edits in place.** It is read-only; selecting a row loads
  that item into a sibling editor panel holding the name, label and group fields,
  the kind and sign combos, the tag checkbox, and a stacked picker of THREE
  pages: the `CategoryTree` for `SOSC`, the account list for every account kind
  (`EDAB`, `SDAB`, `HOLDVAL`, `RGAIN`, `NETGAIN`) and the formula page for
  `COMPUTED`. The third page was specified from the start and missing in
  practice — `COMPUTED` fell through to the account list, so the window offered
  a list of accounts to an item that sums other ITEMS and no way to type the
  formula at all, leaving a kind the evaluator fully supports unreachable. This
  is
  structural, not stylistic: an in-place delegate would have to open a picker
  from `setModelData`, which is the documented way to corrupt the heap in this
  codebase. The tag checkbox disables itself, with a tooltip saying why, on the
  kinds that ignore tags.
- **The `COMPUTED` page offers the report's other lines, it does not ask for
  them to be retyped.** A brace name must match another item EXACTLY, and a
  formula naming a line the report has not got is refused — correct, and useless
  if the names are `W-2:Soc Sec tax withhld, spouse` and the only way in is to
  transcribe one. Double-clicking a line inserts `{its name}` at the cursor
  (`insert_expr_name`, public because that is what the gesture means and a test
  should drive the verb). An item is never offered its OWN name: that is a cycle,
  caught at save either way, but an editor that offers the mistake invites it.
  The formula is written through only when the kind IS `COMPUTED`, so a formula
  left in the box cannot survive a kind change and reappear later as a stored
  contradiction nobody typed.
- **The `SOSC` picker carries a "Transfers" branch** listing every account as
  `[Name]` — Quicken's convention, and the notation the register and the
  drill-down already use for a transfer. It is one list because that is where the
  user looks: the gap it closes was reported as "transfers are not on the list".
  Those rows arrive UNTICKED while categories arrive ticked, for the same reason
  `build_account_picker` unticks everything — "every category" is the filter bar
  saying no filter, but "every transfer in the ledger" is never what someone
  adding a tax line meant. A transfer row carries `account_id` and not
  `category_id`, so every existing walker (all of which test `category_id is not
  None`) steps over it rather than counting an account as a category, and
  `set_item_selections` restores both halves in ONE pass because they share a
  widget and either restore alone would clear the other.
- **Every user choice goes through an overridable seam** (`_prompt_text`,
  `_confirm`, `_warn`), and an evaluation FAILURE — an empty or cyclic
  `COMPUTED` expression, an unresolvable range, a kind this build cannot
  evaluate — is rendered into the status line under the report rather than thrown
  at a modal, so the window stays usable long enough to fix the item that caused
  it.
- **What the tree shows and what the CSV contains come from one pure
  projection.** `drill_tree_rows(tree)` produces the drill-down rows (§5.9u) and
  `report_def_to_csv` writes exactly those under a
  `Line item / Tag / Category,Date,Payee / Memo,Amount` header, following the
  Export CSV convention of §5.9b. Export is a dialog-free `export_csv_to(path)`
  seam with the file chooser layered above it, so the exact bytes are testable.

Duplicating a report copies its items and all their selections but forces
`tag_enabled` off on every copy, since a tag-enabled item name must be unique
ledger-wide and the duplicate would otherwise collide with its source.


### 5.9t Computed items, multi-year comparison and export

**A `COMPUTED` item is a restricted expression, not code.** Item names in braces,
`+ - * /`, parentheses and numeric literals — and nothing else. No `eval`, no
attribute access, no function calls, because a definition file (§5.9r) is data a
user may have downloaded and must never be executable. Arithmetic runs on integer
cents through `Decimal`, never a float, and every DIVISION rounds `ROUND_HALF_UP`
to whole cents immediately, so "a tenth of the increase" is a number the user can
check by hand. A brace name refers to its referent's PRESENTED amount — after
that item's `sign` — because that is the number he reads off the row he is adding
up. Names resolve to item IDs at SAVE time and are stored as edges in
`report_item_refs`, so a rename cannot break a formula and the dependency graph
is inspectable without re-parsing anything; a forward reference (a definition
file listing a total above its parts) resolves as soon as the referent exists,
since every write re-syncs that report's edges. **Cycles are rejected at save AND
re-checked at evaluate** — not redundant, because edges are ordinary table rows a
hand-written `UPDATE` can corrupt, and the failure being prevented is an
evaluation that recurses until the interpreter dies. Detection and evaluation are
both iterative, and the error names the loop. An empty expression is allowed on
CREATE and refused at evaluation: the editor adds the row before the formula is
typed, and a half-built report must not be unsaveable. Item totals deliberately
do not add up to a grand total (§5.9r, `multi_claimed`); a `COMPUTED` item naming
the lines the user wants added is the only meaningful total.

**Multi-year comparison is a domain verb with no window on it.**
`custom.compare(conn, report_id, ranges)` evaluates the SAME report over several
`(start, end, label?)` ranges and aligns rows by item in `seq` order — the payoff
of storing a range as a binding rather than baking two dates into the items
(§5.9r, Ranges): a column cannot disagree with the single-range report about what
the definition MEANS. A label defaults to the year when the range is exactly a
calendar year. A column missing a row gets an explicit zero rather than a gap.
The definition itself, including its stored range, is untouched by comparing.
This is deliberately framed as ARBITRARY multi-range comparison, which subsumes
the two named comparisons other programs offer as separate features: a
**prior-period** comparison is this mechanism given the current and the preceding
range, and a **year-over-year** comparison is it given one range per year — no
extra machinery, and the same rows aligned the same way in both.

The WINDOW no longer offers it. The report panel is a drill-down (§5.9u), and a
tree whose leaves are individual transactions has no honest multi-column form:
one transaction does not appear in three years at once. `compare` is retained as
a computation — it is the only thing that justifies storing a range as a binding,
and the MCP surface can answer a year-over-year question with it — but the window
shows one range at a time. The per-column coverage badge, the `*` no-data marker
and the `?` incomplete marker went with the grid; `ReportRow.no_data` and
`ReportRow.unpriced` still carry both facts for any caller that renders columns
again.

**Export.** Two files, both behind a dialog-free seam with the file chooser
layered above it, so the exact bytes are testable headlessly:
- **Print** — `Print…` sits beside the exports and goes through a SETUP window
  first (`ui/report_print_dialog.py`), because a drill-down is as wide as its
  deepest open branch and does not fit a page by default. Four choices:
  orientation, font family, size, and **which column gives way** when neither
  orientation nor type size is enough. Amount and Date are not on that list —
  **the numbers always show**, since a clipped figure still reads as a number and
  is worse than no page at all. Auto-fit measures each column from its content
  (the hierarchy column WITH its indent, which is what makes a drill-down wide);
  what is left goes to the hierarchy and Payee columns, and a shortfall cuts the
  user's chosen one first, to an ellipsis, down to `MIN_FLEXIBLE` before the
  other gives way at all.

  **The page prints the tree AS IT STANDS** — `visible_drill_rows()` walks only
  expanded branches. Expansion state is already the user's statement of what he
  wants on paper, so there is no "print all levels" option: the tree is one. The
  PREVIEW in the setup window is the real fitted output over the real rows, not a
  mock-up, so a payee about to be cut is seen there rather than discovered on
  paper. Fitting is pure arithmetic on CHARACTERS (`ui/report_print.py`), with
  the page budget the one measured quantity: font metrics and page rect are both
  taken against the PRINTER, since measuring the font alone yields screen pixels
  against a page in points and reported US Letter as 45 characters wide.
- **CSV** — `export_csv_to(path)` writes whatever is on screen: the drill-down,
  flattened by `drill_tree_rows` under
  `Line item / Tag / Category,Date,Payee / Memo,Amount` (§5.9u), with the first
  column indented two spaces per level so the hierarchy survives. The tree and
  the file walk ONE row list, so an export cannot disagree with the screen. An
  excluded row reaches the file marked `[excluded]` in words: a strike-through is
  a screen effect, and a spreadsheet receiving a number that looks included is
  the misreading the strike exists to prevent.
- **TXF v042** — `export_txf_to(path)` over `mammon/reports/custom_export.py`.
  Only items with a non-NULL `txf_refnum` are emitted, one summary record
  (`TS`) each, numbered from 1 within each `(refnum, copy)` pair so that adding
  an unrelated line above employer two does not renumber it. **A `COMPUTED` item
  never emits**, even if someone puts a refnum on one: TXF's only aggregation
  concept is summary-over-detail (transactions into one form line), no record
  format holds another refnum, and emitting a computed total as an independent
  form line would double-count it against its parts. A report carrying no
  refnums at all is caught in the UI BEFORE the file chooser — a `_warn` saying
  so, and no file written — while the writer itself still answers honestly with a
  header-only file if called directly. Conversions happen at this boundary and
  nowhere else: cents become plain decimal strings with no `$` and no comma, ISO
  dates become `MM/DD/YYYY`, records are separated by `^` on its own line, lines
  end CRLF, and the file is written with `newline=""` so Windows does not turn
  that into CRCRLF. TXF always describes ONE tax year — whatever the window's
  date fields hold, because a tax form has no column for last year.


### 5.9u Per-tag breakdown of a report item

**The case.** Several rental properties, each carrying its own tag on every line
that belongs to it (`7344 Muirfield`, `6054 Mapleview`), and a Schedule E that
needs one COPY of the same set of lines per property. Maintaining one item per
property per line is the thing to avoid: four properties times six lines is
twenty-four definitions to keep in step, and adding a property means editing all
six. Instead ONE item — "Property tax", pointed at the category the way any
`SOSC` item is — says *subtotal me by tag*, and answers with one sub-line per
property.

**It is per item and ON BY DEFAULT, with an opt-out.** Nobody should have to type
the names of his own properties to see them subtotalled, and typing them was also
how a property got silently left out of its own report; so an item that says
nothing subtotals by EVERY tag its matched lines carry, and the only setting the
editor offers is "do not". The opt-out is `options["no_tag_breakdown"] = true`
(`ReportItem.break_by_tag` then reads `None`). It rides the `options` blob because
an item's per-kind settings already live there and a definition file written by an
older build must keep loading — and it is a SEPARATE key from `break_by_tag`
rather than `break_by_tag: false` because "off" is now the unusual state and
deserves to be the one written down, leaving `break_by_tag` free to go on carrying
the restrict LIST of a definition that has one. A list of tag names still
restricts and orders the buckets, and a listed tag is emitted **even in a range
with no activity**, so a property's TXF copy number cannot shift under it between
years; the editor has no way to write a new list, but a stored one is honoured.
`ReportItem.break_by_tag` reads back `None` (opted out), `()` (discover every tag
— the default, and what junk in the blob degrades to) or the tuple of names.

**Discovery with nothing to discover emits NO sub-rows** — not a lone
`(untagged)` row restating the item's total. That rule is what makes the default
safe: the overwhelming majority of items in the overwhelming majority of reports
match lines that carry no tags at all, and they render exactly the single row they
always did (in TXF, one record rather than two saying the same thing). An explicit
LIST still emits its rows, zeros included, because the user named those buckets.

**The row shape: sub-rows, then the untagged remainder, then the item's own
total.** The total row is bit-for-bit the number the item produced before any
breakdown existed — the breakdown re-cuts money the item already matched and
NEVER changes selection, since a line is bucketed only after §5.9r's precedence
rules have already admitted it. Sub-rows PRECEDE their total in
`Evaluation.rows` (the parts, then the sum) and are flagged
`ReportRow.is_breakdown` with the tag in `tag_value` and in `label`
(`UNTAGGED_LABEL` = `(untagged)` for the remainder). `Evaluation.by_name` and
`compare` (§5.9t) deliberately see the TOTAL only, so nothing that existed before
the breakdown moves.

Which tags are BUCKETS: every tag on the line except ones beginning with `!` (an
exclusion marker is not a property) and, in discovery mode, except THIS item's own
inclusion tag when it is tag-enabled (§5.9r) — that tag is how the line was
admitted, so bucketing by it would answer "all of it" and dress that up as a
property. **No OTHER item's name is suppressed.** A report-wide set of every
tag-enabled item's name once was, on the theory that report machinery is not a
property; in the rental shape those names ARE the property tags, and every sub-row
came out 0.00 while the whole amount fell into `(untagged)`. A tag that carries
money on the item's own lines is never rerouted into the remainder. With a
restrict list, an unlisted tag does not bucket at all and its line falls to the
remainder, so the sub-rows still account for every line the item matched, and a
LISTED tag is never dropped for any reason. **A line carrying
TWO breakdown tags is counted ONCE UNDER EACH** — nothing in the data could split
that money between them, and "first tag wins" would be a silent answer to a real
ambiguity. The total stays the un-duplicated sum, so the sub-rows are allowed to
over-sum it; that gap IS the report of the double tag.

**Each sub-row carries its own TXF copy number** — `item.txf_refnum`'s copy is
`item.txf_copy` plus the tag's 0-based position, with the untagged remainder
taking the copy after the last tag. That is precisely TXF's mechanism for the
second and third Schedule E property, and `custom_export` emits one record per
sub-row (§5.9t, Export).

**A `COMPUTED` item over broken-down referents computes PER TAG VALUE too**, over
the UNION of its referents' tag values, so "interest + property tax + maintenance
+ utilities" comes out once per property as well as in total. A referent with no
breakdown of its own contributes to the TOTAL only and reads as zero in every
per-tag cell. A brace name still resolves to the referent's TOTAL, as it always
did.

**In the window** (`ui/custom_report_window.py`) the breakdown is ONE widget: a
single unchecked box, "Do not subtotal by tag (one total line only)". There is no
tag-entry field — the tags come from the lines. (Quicken's own report
customization says "Subtotal by", with Tag among the things it subtotals by; the
familiar phrasing survives inverted, because here it is the default rather than a
choice.) The box is offered only for the kinds that have lines to bucket (`SOSC`,
`COMPUTED`) and is grayed elsewhere rather than accepted and quietly ignored.

**The report panel is a DRILL-DOWN: line item → tag → category → transaction.**
`custom.drill_down` builds it and `drill_tree_rows` projects it into depth-tagged
rows for both the tree and the CSV. It replaced a flat summary table, which could
show a number but never why it was that number, and it is the destination for
everything the Coverage panel used to say (§5.9r).

**There is no grand-total row, and `DrillTree` does not carry the number.** The
lines of a tax report share money by design — one category legitimately feeds
several of them, which is the same fact `multi_claimed` reports from the other
side — so adding them up measures nothing, and no tax form asks for the figure:
a return is filed line by line. Rendering it anyway was worse than leaving the
space blank, because a bold `TOTAL` under the last row is trusted BECAUSE it is
bold. It is not computed rather than merely not shown, so nothing can render it
back by reaching for a field that is already there. A report that wants the sum
of particular lines names them with a `COMPUTED` item (§5.9t), which is the only
total on this report that means anything.

- **It is built on ONE evaluation.** `drill_down` calls `evaluate(detail=True)`;
  the flag retains the lines each `SOSC` item admitted and the ones an exclusion
  removed, inside the existing loop. It is not a second walk of the ledger,
  because a drill-down that disagreed with the total it sits under would leave
  the user with two numbers and no way to tell which one the tax form gets.
- **No node sums its children.** Every node reports what the domain computed for
  it, and an ITEM node is `evaluate`'s row verbatim. A line wearing two of an
  item's tags is counted under each (above), so tag rows may legitimately exceed
  the item; a tree that rolled children up would invent a total that is not the
  filed one.
- **The tag level appears only when it means something.** Opted out: categories
  sit directly under the item. Discovery with no tags found: likewise, which is
  what keeps the default quiet. An explicit LIST always shows its buckets,
  including empty ones — an empty bucket is how a property with no rent booked to
  it this year announces itself, and it was invisible in the flat table.
- **An excluded line is shown, struck through**, carrying its real amount and
  contributing nothing above it. Dropping it would restore exactly the silence
  that makes a wrong total look like a right one.
- **A transfer leg** appears under its bracketed counterparty account, the label
  the register uses; an uncategorized line under `(uncategorized)`.
- Amounts carry the item's `sign` at EVERY level, so a deduction reads positive
  on the leaf exactly as on the line above it.

**Right-click on a transaction toggles its exclusion**, which is how a user
carves one transaction out of a tax line without leaving the report he is
checking. Only a transaction row offers it: "exclude this category" would have to
write a tag onto every line underneath, a much larger promise than the gesture
makes. WHERE an exclusion may live is a fact about tag storage, so the rule is
`ledger.toggle_report_exclusion` and the window only asks it:

| Target | | Why |
|---|---|---|
| any line whose item name has a comma | refused | `!item` would store as two tags; the menu entry is disabled with the reason |
| transaction | allowed | many tags per transaction; `!item` joins the comma list and displaces nothing |
| split leg, no tag | allowed | `splits.tag_id` is free; excludes that leg alone |
| split leg, tagged | refused | a leg holds ONE tag, and overwriting the property tag would destroy the attribution that put the line in the report |
| parent, any leg tagged | refused | the parent's tags reach every leg, so `!item` would drop lines a per-leg tag deliberately pulled in |
| parent, no leg tagged | allowed | nothing to displace |

Both refusals raise `ValueError` naming the tag in the way, and the window prints
it in the status line under the tree. **A refusal is not a choice and never
becomes a modal**, the same rule an evaluation failure follows.

**The window carries minimize/maximize hints**, which a `QDialog` does not get by
default. It is a workspace, not a question: a four-level tree beside a category
picker in a fixed-size letterbox is unusable. Expansion state survives a refresh,
keyed by the path down the tree rather than by row number, so toggling an
exclusion does not collapse the branch the user is reading.


### 5.9v Building a report FROM a definition file, and updating over one

**The case.** §5.9r's year definitions are the whole point of the compartment —
sixty-eight Quicken tax lines are not something a user retypes — and until this
existed there was no way to reach them from the window: "New…" made an EMPTY
report and the only file chooser in the window was a Save dialog. The two verbs
are therefore requirements of the WINDOW, not just of the library.

**"From definition…"** sits on the report row beside New/Duplicate/Delete. It
asks WHICH definition and hands the answer to
`report_defs.create_from_definition` — this window parses nothing and writes no
report row of its own. The chooser lists every definition on the search path
(`paths.report_defs_dir()` first, then the shipped `mammon/report_defs`), each
labelled with its title, year, id **and which root it came from**, because a
user's own file shadows a shipped one of the same id and a chooser that hid that
would make the shadowing look like the shipped file changing by itself. A file
that will not parse is left OUT of the list rather than breaking the chooser.
The list also carries a **Browse…** entry over `QFileDialog.getOpenFileName`
filtered to `*.yaml *.yml *.json`, so a definition file **anywhere on disk** —
one a user wrote this morning, one mailed to him, one that was never installed —
is exactly as usable as a shipped one. The offered report name defaults to the
definition's title plus its year; the created report is then SELECTED and its
items shown, so the to-do list of unassigned lines (§5.9r) is on screen
immediately.

**"Update from definition…"** takes the currently selected report to a newer
definition through `report_defs.update_report`, which builds a fresh report and
leaves the source untouched — last year's filed report keeps existing. What the
migration did is a RESULT, not a choice, so it is shown afterwards: carried
lines, renames written `old -> new`, lines added with nothing selected yet,
lines **dropped with their old selections spelled out by name**, kind changes
that refused to copy, and `migrated_from` pointers that resolved to nothing. An
update that silently dropped a line the user spent an evening pointing at
categories is the one failure he must not have to go looking for.

**Both choices are single overridable seams**, per §5.9r's rule: `_choose_definition`
(which delegates the Browse entry to the one-line `_browse_definition_file`) and
`_inform` for the result, with the migration report itself produced by a pure
`migration_text(MigrationResult)`. Nothing on this path builds and `exec_()`s a
dialog of its own, so the whole life cycle — choose, create, see the items,
update, read the report — is drivable headless in tests.


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
  again from View ▸ Financial Calendar): the same projection laid out as a
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


## Compartment K. Database, backups and encryption

### 5.8x Journal mode and durability (WAL)

Every connection is opened in **WAL** (`journal_mode=WAL`) with
`synchronous=NORMAL`, set in `db.connect` -- the one place a connection is ever
opened, so the MCP server and the backup stager inherit it.

This is a measured decision. The write path commits once per ROW
(`ledger.add_transaction` commits, and the checkpoint cascade it triggers commits
again), so importing one 1.1 MB yearly Quicken QIF issued about 2,700 commits and
spent 79% of its wall clock inside `commit()` on an encrypted SQLCipher file --
46s for a single file, which put a 32-file full-history migration into the tens of
minutes. Under WAL a commit appends to a log rather than rewriting and fsyncing a
rollback journal: the same import measured 5.68s -> 0.40s on a fresh ledger and
1.63s -> 0.31s on one already carrying history. Batching the commits instead would
have meant touching some 84 commit sites across `ledger`, `investments`,
`import_review`, `crypto` and `importers/core`; WAL captures most of the win from
one place and leaves `mammon.ledger` the sole writer, untouched.

WAL is safe with everything that copies a Mammon database, and that was checked
before enabling it:

- `backup.create_backup` uses SQLite's **online backup API** (`conn.backup`),
  which reads through the WAL and writes a fully checkpointed standalone file; the
  incremental page-delta diff then reads that *staged* copy, never the live
  database. A backup that copied only the `.db` file would instead have captured a
  stale ledger -- the specific trap WAL sets.
- `encryption` converts through `ATTACH` + `sqlcipher_export()` from an open
  connection, so it too reads committed data through the WAL.
- WAL leaves transient `<db>-wal` and `<db>-shm` files beside the database between
  commits; a clean close checkpoints and removes them.

**Durability is split from the journal mode, because only one of the two costs
anything.** WAL is permanent and free -- it cannot lose a commit and cannot corrupt
the file. `synchronous` is the setting that trades safety for speed, so:

- **At rest: `synchronous=FULL`** (`db.connect`). Every commit is forced to disk
  before it returns, so nothing the user types by hand can be lost. This is the
  right default for a ledger keyed in a transaction at a time.
- **During a bulk import: `synchronous=NORMAL`**, for the duration only, via the
  `db.bulk_write(conn)` context manager wrapped around `importers.core`'s
  `import_records` -- the one database-facing choke point every file import passes
  through. Under NORMAL a power loss can lose the most recent commits, which is
  acceptable here and nowhere else: an import's source file is still on disk, so
  the remedy is to run it again. `bulk_write` restores the PRIOR value rather than
  assuming FULL, so nesting cannot leak a relaxed setting, and the restore is in a
  `finally` so a failed import does not leave the session running without
  durability.

`test_db.py` pins all of it: the journal mode (a silent fall back to the rollback
journal is a 5-14x slowdown with no other symptom), FULL at rest, NORMAL inside the
scope, no leak on nesting or on an exception, that a real import runs inside the
scope and hands the connection back strict, and that a backup taken with rows still
resident in the WAL restores a complete ledger.

The canonical schema, how it migrates, how balances stay fast over 40
years, and how the file is snapshotted. The database is one SQLite
file the user owns.

Encryption is optional, off by default, and documented separately in
`docs/encryption.md` (SQLCipher via `sqlcipher3`: what it uses, where
the key lives, setting/changing/removing a password, what it means for
backups and for the MCP server). README.md points users at that file,
so it stays where it is rather than folding in here.

Code: `mammon/db.py` (schema + migrations only, no business logic),
`mammon/backup.py`, `mammon/paths.py`, `mammon/last_db.py`.

### 4. Data model (canonical schema)
One SQLite database. Core tables:

- accounts(id, name, type[checking|savings|credit|cash|investment|asset|liability],
  currency, opening_balance, opening_date, institution, note, closed_flag) -
  the opening balance is part of the balance from `opening_date` on (5.2b);
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
- allocation_target_accounts(target_id, account_id) - v74: the accounts one
  target mix governs, all of one `accounts.tax_treatment` (5.8f).
- holding_links(account_id, from_symbol, to_symbol, date) - v73: a fund
  conversion the user kept as one holding for measuring its return (5.8d).
- securities(symbol PRIMARY KEY, name, sec_type, asset_class, kind, multiplier,
  underlying, expiration, strike, option_right, kind_source) - one row per
  security identity; `holdings.symbol` and `price_history.symbol` resolve here.
  The last seven columns (schema v67) carry the INSTRUMENT TAXONOMY: `kind` is
  the instrument class with NULL meaning UNCLASSIFIED rather than equity, so
  code branches on "known option" vs "not known to be an option" and an
  unclassified row behaves exactly as it always has; `multiplier` and `strike`
  are Decimal-encoded TEXT like every other quantity and price, never floats;
  `underlying` names the root's identity and `expiration` is ISO with NULL
  meaning perpetual; `option_right` is 'C' or 'P' and is spelled with the
  prefix because RIGHT is a SQLite keyword (right joins, 3.39+) and a keyword
  column name is a latent parse failure in the one context nobody tests;
  `kind_source` ('source' | 'derived' | 'user') keeps a source-stated type, a
  derived classification and a user decision distinguishable forever. They live
  on `securities` rather than in a parallel options table because every read
  path already looks a security up by symbol, and a second table would mean a
  second lookup on every price, holdings and valuation path.
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
  fitid, created_at, time) - `time` (v70) is the source's time of day, HH:MM:SS,
  NULL when unknown (5.8j). The event log for a cryptocurrency wallet-account (account
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
- allocation_targets(id, name, active, sleeve, the two drift bands) /
  allocation_target_lines(id, target_id, asset_class, pct, locked) - the named
  target asset mixes and their per-class weights (roadmap item 7, schema v44).
  `pct` is Decimal-encoded TEXT, like a share price: a mix is a precise quantity
  the user typed and float drift in numbers that must total 100 reads as
  phantom deviation. `locked` (schema v66, `INTEGER NOT NULL DEFAULT 0`) is the
  per-line hold flag the Target & Drift editor sets; it lives in the database
  rather than the dialog because "this class is settled" outlives the window
  that said so, and every later edit redistributes across the unlocked lines
  only (5.8f). Domain layer in mammon/rebalance.py; no transaction is ever
  written from it.

Monetary amounts: stored as signed integer CENTS (avoids float rounding).
Share quantity and per-share price: stored as text-encoded Decimal to preserve
precision for fractional shares / multi-decimal prices. (Q5 RESOLVED: confirmed.)
Implemented in mammon/db.py (schema v1); smoke tests in mammon/tests/test_db.py.


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

### 5.8y Which database a launch opens (the last one used)

User request, 2026-09-14: "Can we change it so the last used db is the default
db? For a GUI launched Mammon, there is no command line option for db name."
A Start Menu or taskbar launch cannot pass `--db`, so before this a GUI launch
always came up on `<data dir>/mammon.db`, even for someone whose ledger lives
elsewhere.

- **A launch with no `--db` opens the database last opened in the window**,
  through File ▸ New Database, Open Database, Save Database As, or Restore from
  Backup. It opens `<data dir>/mammon.db` only when nothing has been chosen yet.
- **`--db` opens that file for one session and is NOT remembered.** Developers,
  the screenshot tool, tests and automation agents all launch with
  `--db <scratch>`. Remembering it would make the next Start Menu launch open a
  scratch ledger in place of the real one.
- **A remembered file that is missing is not forgotten.** The launch opens the
  default instead and says so in a message naming the missing file. The
  pointer is kept, because the usual cause is a drive or share that is not
  connected yet; overwriting it would lose track of the ledger.
- The MCP server's default (`python -m mammon.mcp_server` with no `--db`)
  follows the same rule, so it serves the ledger the app would open.
- A source checkout and an installed copy each remember their own last
  database. They may be different versions of the code, and one shared pointer
  would have each open whatever the other used last.


### Implementation notes

How this compartment is built, and what each shape
prevents. Moved out of CLAUDE.md 2026-09-11: it is
reference for whoever works here, not per-call context.

#### Schema migrations

`mammon/db.py` holds an ordered `MIGRATIONS` list; index *i* upgrades the DB from version *i* to
*i+1*, tracked in `PRAGMA user_version`, with `SCHEMA_VERSION = len(MIGRATIONS)` (currently 66 —
`_V66` added `locked INTEGER NOT NULL DEFAULT 0` to `allocation_target_lines`, see §5.8f).
**Append a new `_Vn` and add it to the list — never edit an existing migration**, since real
databases have already applied them. `init_db()` is idempotent and safe on new and existing files.

#### Balance checkpoints

Running balances use per-`(account, year)` rows in `balance_checkpoints` so a 40-year multi-account
file opens and scrolls fast. Any write that changes history calls `_touch_checkpoints(...)`, which
cascades a recompute from the earliest year touched forward. `ledger.account_balance` reads through
the checkpoint path; the full-sum version is retained as the oracle it must match, asserted by
`test_year_end_snapshots.py`. Investments have the analogous `holdings_checkpoints`.

#### Backups are incremental, by page

`backup.py` writes two kinds of snapshot into `data/backups/<db-file-name>/` — **one folder per
database**:

- `<db>.<tag>.<stamp>.bak` — a full copy, a plain SQLite file anything can open. Manual backups are
  always this.
- `<db>.<tag>.<stamp>.delta` — only the 4 KB pages that differ from the newest full snapshot,
  gzipped, behind a JSON header naming its baseline and the checksum of the reconstructed file.

Two independent guards keep the 1/minute auto-backup cheap, and both are load-bearing:
`db_fingerprint` skips the write entirely when nothing changed, and the delta stops the writes that
*do* happen from copying 12 MB to record a 15 KB change. Measured on the real ledger, a minute of
work touches ~14 pages of 2,999.

**Each snapshot also carries a "what changed" summary**, because the file name alone (time + tag)
cannot tell one one-minute auto-backup from the next when picking a restore point. At snapshot time
`build_manifest` takes a lightweight, READ-ONLY census of the staged copy — per-account transaction
count, balance in cents, and latest row — and `summarize` diffs it against the previous snapshot
**of the same tag** (within a tag the timestamp-before-extension name sorts chronologically; across
tags it does not, so the diff is tag-scoped, and each tag forms its own chain). The first snapshot
of a tag reads as `baseline / initial`. A delta stashes the manifest+summary as extra keys in its
JSON header; a full `.bak` (a plain SQLite file with nowhere to put metadata) gets a
`<name>.bak.meta.json` sidecar. `_describe` (CLI `list`) and the Restore confirm dialog surface the
summary; the UI reads it through `backup.snapshot_summary` only — no SQL or money logic in `ui/`.

Five invariants:

- **A restore never crosses databases.** `restore_backup` refuses a snapshot whose own
  `<db-file-name>` prefix disagrees with the file being replaced (`snapshot_db_name` /
  `check_same_database`), and the Restore picker opens in this database's own folder. This is not
  hypothetical: a scratch `mammon2.db` snapshot was once restored over the real ledger from the old
  shared folder, and the result is a perfectly valid, completely wrong ledger — the loss went
  unnoticed for hours. The check applies when the target EXISTS; writing a snapshot out to a new
  file destroys nothing and is allowed. Legacy flat snapshots are still listed and restorable;
  `python -m mammon.backup organize` moves them into their folders.


- **Deltas reference the baseline directly, never each other.** No chains — one damaged delta costs
  one restore point, not every point after it. Growth against a fixed baseline is sublinear, so this
  costs almost nothing.
- **Retention must never orphan a baseline — or a sidecar.** `prune_backups` and
  `purge_auto_backups` hold a full snapshot back past its turn while any surviving delta names it
  (dropping it is the one way this scheme loses real data), and both delete a full snapshot's
  `.bak.meta.json` sidecar in lockstep with the `.bak` (as does `organize_backups` when it moves a
  legacy snapshot into its per-db folder). A sidecar left behind names a snapshot that no longer
  exists.
- **A rebuilt delta is checksum-verified** against the hash taken when it was written; a mismatch
  raises rather than handing back a plausible-looking database. The manifest/summary keys must stay
  OUT of that hash: the `sha256` is of the reconstructed DB bytes, never of the header, so the added
  header keys cannot corrupt a restore (regression: `test_backup_summary.py`).

Anything needing a real file from either kind calls `backup.restore_backup(snapshot, out)`.
`python -m mammon.backup list|verify|restore` does the same from a terminal, so a delta is never a
dead end without the GUI. Rejected: year/account-scoped backups (SQLite doesn't lay rows out by
year, and rename-tree/mapping/review changes touch no transaction at all) and an operation log
(makes backup correctness depend on the code version that replays it, to save megabytes that no
longer exist).

#### Paths, and why they are the way they are

**`mammon/paths.py` is the only place that decides where data lives.** Never resolve a data path
anywhere else. Three modules used to answer this separately and the copies drifted: `backup` and
`download_log` honoured `$MAMMON_DATA_DIR`, `app._resolve_db` did not, so redirecting that variable
moved the snapshots and the log while leaving the database in the install.

`paths.data_dir()` answers in this order:

1. **`$MAMMON_DATA_DIR`** — wins outright. Tests and alternate installs use it, and it must move
   the database with everything else.
2. **A packaged build** — `~/Documents/Mammon`. That means a copy put in place by the Windows
   installer, which writes `mammon-install.json` beside the package (`paths.is_installed`), or a
   frozen executable (`sys.frozen`). The marker is the test that matters. The installer runs the
   stock embeddable Python, which sets no `sys.frozen`, so with that as the only test an installed
   copy would have kept its ledger inside `%LOCALAPPDATA%\Mammon`, which every upgrade replaces and
   uninstall removes. An installed app must never write beside itself anyway: `Program Files` is
   read-only to a standard user, and Windows does not fail cleanly there, it redirects the writes
   into a per-user VirtualStore copy, so the ledger appears to save and then appears to vanish.
   Documents over `%LOCALAPPDATA%` is deliberate — the whole promise is that the user owns the file,
   and a file they cannot find is not one they own.
3. **A source checkout** — `data/` beside the package, resolved from the package location and never
   from the CWD. Resolving relative to the CWD meant launching from a different directory silently
   opened a *different*, empty database, and learned rules looked lost.

`--db` is still authoritative and used verbatim, ahead of all of this.

`paths.data_dir()` answers where data lives; which database a launch opens is one step further
(5.8y). `app._resolve_db(None)` asks `last_db.startup_db()`: the path recorded in
`<data dir>/last_database.json` when that file still exists, else `paths.default_db_path()`. The
pointer lives in the data dir so `$MAMMON_DATA_DIR` isolates it like everything else. It is written
only through `MainWindow`'s `on_database_opened` hook, which only the real launch in `mammon.app`
connects to `last_db.remember`. Every test builds `MainWindow` without it, so no test can move the
default, whether or not it remembered to redirect the data dir. Writes are atomic, and a pointer
that is unreadable or unwritable counts as absent: failing to remember must never stop a database
from opening.

Nothing in `paths.py` creates directories; the callers that write do that, so importing it can never
leave a stray folder behind. `backup.DEFAULT_BACKUP_DIR` stays a module attribute resolved at import,
because tests monkeypatch it to redirect snapshots into a tmp dir — that seam is what keeps a test
run from writing into a real install's data folder. A test run once reached a real ledger and
migrated it. **Any new default path must go through `paths.py` and be test-overridable**; a test that
can write outside `tmp_path` will eventually write somewhere that matters.

## Compartment L. Downloads, webSlinger and the MCP server

How transactions arrive from institutions, and the read-only tool surface
an LLM sees. The app holds NO credentials: login and secrets belong
entirely to the webSlinger/keyCocoon side.

Code: `mammon/downloads.py`, `mammon/webslinger.py`,
`mammon/mcp_tools.py`, `mammon/mcp_server.py`.

### 5.7 Download from institutions (see Section 7)
- Pull transactions from the user's banks, cards, and investment institutions,
  preferring a direct download format (QFX/OFX/CSV) where the site offers one,
  and webSlinger scraping -> JSON only where it does not.


### 7. Download / institution list (Q4)

#### 7.1 Institutions to support
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

A SCRAPE script's `output_data` SHAPE is not a contract. The script is generated
from a recorded demonstration, so the generator picks how to organize the rows
and re-recording the same institution can change it: America First moved from
`{"Checking": {"transactions": [...]}}` to
`{"accounts": [{"accountName": "Checking", "transactions": [...]}]}` on
2026-09-19, and a reader that recursed only through dict values found no records,
reported "nothing returned", and left 8 transactions unimported. Mammon therefore
reads the rows by the KEY they sit under (`transactions`/`records`/...), searched
at EVERY depth through both dicts and the dicts inside lists, rather than by
position. Where no such key is used, the fallback still gathers arrays that hold
transaction-SHAPED dicts, or wrappers around them, so a per-bank array name keeps
working while a lookup table beside the rows (America First's `subAccountList`)
is rejected instead of becoming blank review rows.

Runtime dependency: this makes webSlinger a required companion for downloads.
Accepted by the user -- webSlinger has a $2/month execute-only tier that runs
already-recorded scripts, which is the intended cost for Mammon users after their
institution scripts exist. Mammon itself (ledger, import of files, reports) works
with no subscription; only the automated download step calls webSlinger.

#### 7.2 Utilities (separate from the main Mammon program)
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

#### 7.2a Amazon invoice itemization: review integration (phase 2, IMPLEMENTED)

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

#### 7.3 The MCP server: asking an LLM about the ledger (roadmap item 3)
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
- **What an instrument IS travels with it.** Every position row -- `holdings`,
  `lots`, `investment_performance` -- carries `kind` (the `securities.kind`
  value, `null` when unclassified) and `option`: `null` for anything that is not
  a contract, and otherwise `multiplier`, `underlying`, `expiration`, `strike`
  and `right`, each a STRING or `null` when the term was never recorded. A model
  must not have to infer a contract from the shape of a ticker, and `0.1` has no
  exact binary form, so no term crosses as a float. An option row's `quantity`
  is CONTRACTS and its `market_value` is `contracts x premium x multiplier`
  (5.8e-5), negative for a written contract. A NULL-kind security reports
  `kind: null, option: null` and is otherwise byte-for-byte what it was.
- **`allocation` excludes option contracts and says so** (5.8e-9): they are out
  of `total`, `by_class`, `by_security` and `by_account`, and the response
  carries `excluded_options` (a `symbol` / `market_value` pair per contract),
  `excluded_options_value` and a `note` -- the sentence to show the user, empty
  when nothing was excluded. One contract is never reported as 100 shares of its
  underlying, and never dropped in silence.
- Conventions the model is told once (`mcp_tools.INSTRUCTIONS`): amounts are
  decimal dollar strings, negative = money out; dates ISO, ranges inclusive;
  accounts by name or id, hidden accounts excluded unless asked; categories by
  `Parent:Child` path; start with `overview`.
- **Local model option.** stdio is what desktop MCP hosts speak; an
  MCP-capable local client (LM Studio, Open WebUI and the like) launches the
  command and talks to a model on the same machine, so no row leaves it. A
  hosted model is a per-user choice; aggregate-first tools keep what it sees
  small. The HTTP transport exists for clients that connect over a local port.


## Compartment M. Platform, architecture and open items

Non-functional requirements, the resolved technology decisions, and what
is still open.

### 8. Non-functional requirements
- Correctness first: automated tests for balances, transfers, and import dedup.
- Data safety: easy backup (the SQLite file), and an import audit trail so any
  import can be traced and, ideally, undone.
- Performance: 40 years x several accounts must open and scroll smoothly (the
  per-year lazy-load + checkpoint scheme handles this).
- Portable, dependency-light: stdlib + PyQt5 + a small, justified set of
  packages (e.g. yfinance for quotes).


### 9. Architecture
- mammon/ package: db.py (schema/migrations), domain layer (accounts,
  transactions, transfers, balances), importers/ (qif, ofx, json, csv),
  investments (quotes), reports/, and a UI layer.
- Clear separation of DB/domain from UI so logic is unit-testable headless.

#### 9.1 Language / UI technology (RESOLVED: Python for everything)
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

#### 9.2 Distribution: the Windows installer (RESOLVED 2026-09-14)
Requested 2026-09-05: "a Windows installer so it can be run via the Windows
launcher (or cross platform equivalents) and be pinned on the taskbar." Scope
settled 2026-09-14: the release is the installer; it needs no clone and brings
its own Python.

- A GitHub Release carries `Mammon-<version>-Setup.zip`. The user extracts it
  and runs `setup.bat`: no administrator rights, no Python or git of their own.
- Program files go in `%LOCALAPPDATA%\Mammon` (replaced on upgrade, removed on
  uninstall). Data goes in `Documents\Mammon` and is never touched by either.
- Setup offers to move an existing ledger from a source checkout, with its
  backups (approved 2026-09-14). It refuses a ledger that is in use, one whose
  name is already taken, and one from a newer schema than the installer carries.
- A Start Menu entry that can be pinned to the taskbar, and a Settings > Apps
  entry to uninstall from.
- An MCP launcher (`mammon-mcp.bat`) for MCP clients.
- Windows only; macOS and Linux run from a clone. Not code-signed (SmartScreen
  warns on first run) and no automatic updates, both accepted for now.

Decision: the Python embeddable package plus pip-installed dependencies, not a
frozen executable. It is the shape webSlinger's installer moved to after
starting with PyInstaller, it needs no hidden-import or data-file lists for
PyQt5, matplotlib or sqlcipher3, and the installed app runs exactly the code a
clone runs. The build (`installer/build.py`) installs, runs and uninstalls its
own payload before it will write a ZIP. `installer/README.md` has the details.


### 10. Open items / decisions to confirm
- Q5: integer cents + Decimal-text prices/quantities - RESOLVED (confirmed).
- Language/UI - RESOLVED: Python everywhere, PyQt5 desktop.
- Direct Quicken read - RESOLVED to "try both" (Section 6.1); QIF is the
  workhorse, direct read is a spike.
- Per-institution: whether webSlinger drives the site's export button or scrapes
  the transaction list - decided one at a time as we reach each (Section 7).
- Reconciliation acceptance test for the migration: match Mammon per-account
  balances to Quicken's - confirm this is the bar you want.
- OPEN (deferred 2026-09-21, dashboard ring): with option contracts held, the
  securities ring can still total less than the accounts ring. `allocation`
  excludes contracts outright (SRD 5.8e-2d/5.8e-9) while
  `account_valuation.securities` counts their premium, so the cash wedge closes
  only the CASH half of the gap. `allocation.options_note` already renders the
  sentence; nothing on the dashboard shows it. Decide whether the page states
  the excluded premium, gives contracts their own wedge, or leaves it to the
  Investment Center. Not a wrong number -- an unexplained one, and only for a
  portfolio holding options.
- Project NAME - RESOLVED: Mammon (Section 11).
- Sample Quicken export (a few accounts incl. one investment) - needed to build
  and validate the QIF importer (Task 5a) and the direct-read spike.


### 11. Project name
RESOLVED: the project is **Mammon**. The name is a deliberate reminder rather
than a boast -- "you cannot serve God and mammon" -- so the ledger you open every
day names the thing it is meant to keep in its place. It is short, ownable, and
carries no vendor echo. The Python package is `mammon`; the default database is
`data/mammon.db`.

### Implementation notes

#### Configuration

- **The app holds no credentials at all.** Login, MFA, and secret storage belong entirely to the
  webSlinger/keyCocoon side — the MCP server drives the site using the credentials in the user's own
  browser session. Nothing on the download path gates on a secret, and no credential vault belongs
  in this repo. Don't reintroduce one.
- UI display preferences go to QSettings, never to the database (`ui/prefs.py`). This includes
  the PER-ACCOUNT one/two-line register layout (`account_view_mode`) and the
  transaction-accepted sound.
- Installer-only env vars, used by the build's verification run to keep it off the real profile:
  `MAMMON_INSTALL_DIR`, `MAMMON_START_MENU`, `MAMMON_SKIP_REGISTRY`.
- Env vars: `MAMMON_WEBSLINGER_MCP_CMD` (MCP launch command), `MAMMON_DATA_DIR`,
  `MAMMON_DOWNLOAD_LOG`, `MAMMON_ACCEPTANCE_DB` (opt into the real-ledger acceptance tests).
- `crashlog.py` installs a `sys.excepthook` early in startup because PyQt otherwise swallows
  exceptions raised inside slots; crashes land in a log next to the database.
