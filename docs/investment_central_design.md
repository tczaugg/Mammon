# Investment Central — Design Document (for review)

Status: DRAFT for user review. No code has been written. This document proposes a
consolidated "Investment Central" dashboard that lives in the register area and gives one
view of every investment and crypto account at once: what is held, how it is allocated,
what it is worth, and how it has performed.

All amounts in the examples below are synthetic. Account names ("Taxable Brokerage",
"Roth IRA", "ETH Wallet") and tickers ("ACME", "GLBX", "BRDMKT", "ETH", "BTC") are
placeholders, not anyone's real holdings.

---

## 1. Scope

### 1.1 What Investment Central is

A single, non-modal page in the register area that aggregates **every account whose type
is in `ledger.INVESTMENT_LIKE_TYPES = ("investment", "crypto")`** (`mammon/ledger.py:51`)
into one dashboard. It answers, in one screen, the questions today's user must open four
separate gear dialogs and several account registers to answer:

- **Holdings across all investment and crypto accounts** — one consolidated positions
  table (symbol, account, quantity, price, cost basis, market value, gain), collapsible or
  groupable by account or by asset class.
- **Allocation** — where the money is, by asset class / by security / by account, with the
  same scope choices the existing Allocation window offers (investments only; investments +
  cash; everything owned).
- **Period performance / returns** — money-weighted return, money in/out, income, and gain
  over a chosen period, per account and as a portfolio total, plus per-holding
  inception-to-date and period gain.
- **Price-history charts** — a security's recorded close series, on demand from the
  holdings table.
- **Total investment net worth** — the summed market value of all investment-like accounts,
  with a net-worth-over-time chart scoped to just those accounts.

### 1.2 What it is NOT (non-goals for this feature)

- **Not a new write path.** Investment Central is strictly read-only. It renders data;
  it never inserts, edits, or deletes a transaction. `mammon.ledger` / `mammon.crypto`
  remain the sole writers. (CLAUDE.md: "Adding a second write path is the main way to break
  this codebase.")
- **Not a replacement** for the per-account investment/crypto registers or their gear
  dialogs (Lots, Capital Gains, Performance, Allocation). It is a roll-up *above* them; the
  registers stay the place you edit activity, and drill-downs open the existing windows.
- **Not new money math in the UI.** Every dollar, percentage, and return comes from the
  domain (`investments`, `crypto`, `portfolio`) or the `reports/` layer. The UI is a thin
  projection: no SQL, no cents arithmetic beyond formatting. (CLAUDE.md: "PyQt5
  models/widgets — a thin projection, holds no SQL and no money logic.")
- **Not a target/rebalance planner.** Target allocation and drift already exist
  (SRD 5.8f/5.8g); Investment Central can *link to* them but does not reimplement them.
- Excluded on principle (matches existing product decisions): real-time streaming quotes,
  time-weighted return (only money-weighted return exists today), and any hosted data feed.

### 1.3 Conventions this design honors

- **Money is signed integer cents** end to end; **share quantities and per-share prices are
  Decimal-encoded TEXT.** The dashboard consumes the domain's already-correct values and
  formats them at the edge; it introduces no float money.
- **Dates are ISO `YYYY-MM-DD`** in every function call; anything the user sees or types
  goes through `ui/models.fmt_date` / `parse_date` and the date-edit helpers.
- **Equity vs crypto are routed by EXACT account type**, not by `INVESTMENT_LIKE_TYPES`
  membership, because the two read different tables (`investment_transactions`/`holdings`
  vs `crypto_transactions`/`crypto_holdings`). The membership set decides *which accounts
  belong on the dashboard*; the concrete type string decides *which module answers the
  detail lookup*. `investments.display_balance` / `crypto.display_balance` already
  encapsulate that dispatch and are safe to call on any account id.

---

## 2. Data sources

The guiding rule: **reuse first.** Almost everything the dashboard needs already exists as a
plain-data aggregation. Where a gap exists, it is filled by a NEW read-only function in the
`reports/` layer (or `portfolio.py`) that only *composes* existing domain values — never new
SQL in the UI, never new money math outside the domain.

### 2.1 Existing aggregations to reuse (no new code)

Total investment net worth
- `investments.net_worth(conn, as_of=None, prices=None, *, account_ids=None, include_hidden=False) -> int` (cents) — `mammon/investments.py:2115`. Pass the ids of all
  `INVESTMENT_LIKE_TYPES` accounts to get the investment-only total. `net_worth` sums
  `display_balance`, which already market-values equity, crypto, and asset accounts and
  reads crypto through `crypto.display_balance`, so the total is correct across both tables
  with no special-casing here.
- `investments.display_balance(conn, account_id, as_of=None, prices=None) -> int` (cents) —
  `mammon/investments.py:2079` — the type-agnostic per-account "what is this worth" chokepoint.
- `investments.valuation_as_of(conn) -> Optional[str]` — `mammon/investments.py:2053` — the
  latest date the ledger knows any transaction or price; the natural default for the "as of"
  control.

Allocation
- `portfolio.allocation(conn, account_ids=None, as_of=None, prices=None, scope="investments", include_hidden=False) -> Allocation` — `mammon/portfolio.py:455`. Returns an `Allocation`
  (`portfolio.py:430`) with `by_class`, `by_security`, `by_account` (each a list of
  `Slice(key, label, value_cents, pct)`, largest first), plus `unpriced`,
  `cash_only_accounts`, and `account_classes`. Crypto positions already flow in with
  `asset_class = 'crypto'`, so one call covers equity and crypto together. `scope` supports
  `ALLOCATION_SCOPES` (investments only / investments + cash / everything owned).

Per-holding performance table (equity accounts)
- `reports.investment_performance(conn, as_of=None, *, start=None, account_ids=None, include_hidden=False, include_sold=True, prices=None) -> InvestmentPerformanceReport` —
  `mammon/reports/investment_performance.py:187`. One `HoldingPerformance` row per
  (account, symbol) with `quantity` (Decimal), `cost_basis`, `market_value`,
  `unrealized_pl`, `realized_pl`, `dividends`, `return_of_capital`, `price`, `is_open`,
  and — when `start` is given — `period_gain` / `period_basis`, plus report-level totals.
  This is exactly the consolidated equity holdings table; passing `start` turns on period
  mode for period returns. **Caveat: it covers `investment_transactions`/`holdings` only —
  it does not include crypto accounts** (see the new function in 2.2).

Money-weighted return over a period (per equity account / per security)
- `portfolio.account_performance(conn, account_id, start, end, prices=None) -> Performance` —
  `mammon/portfolio.py:322`. `Performance` (`portfolio.py:267`) = `start_value`, `end_value`,
  `money_in`, `money_out`, `income`, `irr` (annualized money-weighted), and a `gain` property.
- `portfolio.security_performance(conn, account_id, symbol, start, end, prices=None)` —
  `mammon/portfolio.py:344`.
- `portfolio.xirr(flows) -> Optional[float]` — `mammon/portfolio.py:230` — the solver, if a
  new portfolio-total roll-up needs it (see 2.2).

Price history (charts)
- `investments.price_history(conn, symbol, as_of=None) -> [(date, close_price)]` (Decimal
  closes, ascending) — `mammon/investments.py:1699`.
- `investments.price_history_bounds(conn, symbol, as_of=None)` — `mammon/investments.py:1714`
  — adds low/high for derived (non-exact) prices, useful for shading.
- `crypto.price_history(conn, symbol, as_of=None)` — `mammon/crypto.py:1182` — the same
  shape, keyed through `crypto.pair_symbol` ("ETH" -> "ETH-USD") into the shared
  `price_history` table.

Net worth over time (investment slice)
- `reports.charts.net_worth_series(conn, start=None, end=None, points=24, *, account_ids=None, include_hidden=False, exclude_categories=None) -> NetWorthSeries` —
  `mammon/reports/charts.py:330`. Returns `NetWorthPoint(date, cents)` samples. Pass the
  investment-like `account_ids` to get an investment-only history line (it values investment
  accounts at market at each sampled date).

Per-account balances (for the per-account subtotal strip)
- `reports.balances.account_balances(conn, as_of=None, *, include_hidden=False, include_closed=True) -> BalanceReport` — `mammon/reports/balances.py:44` — `AccountBalance(account_id, name, type, hidden, closed, cents)` rows plus a total, valued the same way the account bar values them.

Underlying domain reads available if a new aggregator needs finer detail
- `investments.security_positions` / `held_positions` / `closed_positions` — `investments.py:1851/1867/1874`.
- `investments.holding_values_at(conn, account_id, as_of) -> {symbol: market_value_cents}` (rewinds shares to the date) — `investments.py:1922`.
- `investments.net_contributions_by_symbol(conn, account_id, start, end) -> {symbol: net_cents}` — `investments.py:1946`.
- `crypto.holding_values` / `crypto.account_valuation` / `crypto.realized_gains` / `crypto.symbols_used` — `crypto.py:1196/1231/691/943`.
- Checkpoints (`balance_checkpoints`, `holdings_checkpoints`, `crypto_holdings_checkpoints`) keep these fast on a 40-year file; the dashboard inherits that for free by calling through the existing functions.

### 2.2 New read-only aggregations needed (composition only, no new SQL in UI)

These belong in the `reports/` package (thin, dataclass-returning, no Qt), or in
`portfolio.py` where they extend the money-weighted-return machinery. Each is justified
because no existing function spans **equity + crypto together**.

1. **`reports.investment_central.overview(conn, as_of=None, *, start=None, include_hidden=False, prices=None) -> InvestmentOverview`** — the top-of-page roll-up spanning BOTH
   investment and crypto accounts. Fields (all cents unless noted): `as_of`,
   `total_market_value`, `total_cash`, `total_cost_basis`, `total_unrealized_pl`,
   `total_realized_pl`, `total_income` (dividends/interest/distributions),
   `total_period_gain` (Optional, when `start` given), plus a list of per-account subtotals
   `AccountSubtotal(account_id, name, type, market_value, cost_basis, unrealized_pl, cash)`.
   Implementation: iterate `ledger.list_accounts` filtered to `INVESTMENT_LIKE_TYPES`, call
   `investments.display_balance` for the market value, and pull cost/gain from
   `reports.investment_performance` (equity) and `crypto.holding_values` +
   `crypto.realized_gains` (crypto). **No new money math** — it sums figures the domain
   already produced. Total investment net worth for the header is
   `investments.net_worth(conn, account_ids=<those ids>)`.

2. **`reports.investment_central.holdings(conn, as_of=None, *, start=None, include_sold=False, include_hidden=False, prices=None) -> list[UnifiedHolding]`** — the consolidated
   positions table across equity + crypto. `UnifiedHolding(account_id, account_name,
   account_type, symbol, asset_class, quantity: Decimal, price: Optional[Decimal],
   cost_basis, market_value, unrealized_pl, period_gain: Optional[int], is_open)`.
   Implementation: for equity accounts, reuse `reports.investment_performance` rows; for
   crypto accounts, project `crypto.holding_values` (+ `security_mix`/`asset_class` lookup)
   into the same shape. This is the one genuinely new "shape," and it exists only because
   equity and crypto live in different tables — the numbers are still the domain's.

3. **`portfolio.portfolio_performance(conn, account_ids, start, end, prices=None) -> Performance`** (optional, Phase 2) — a portfolio-level money-weighted return across
   several accounts: start/end values summed, external flows concatenated and re-dated, one
   `xirr` over the merged flow list. Reuses `account_performance`'s pieces and the existing
   `xirr`. Without this, the dashboard shows per-account IRR (already available) and a simple
   aggregate gain, and leaves a single portfolio IRR to a later phase.

4. **`crypto.net_contributions_by_symbol(conn, account_id, start, end)`** (only if per-symbol
   *period* gain is shown for crypto) — the crypto twin of the equity function, which does
   **not** exist today (confirmed absent from `crypto.py`). If period columns for crypto
   holdings are deferred, this is not needed for v1.

All four return plain Python (dataclasses / lists / dicts / Decimal / int) and hold their
SQL internally — matching every other module in `reports/` and the `investments`/`crypto`
domain contract. The UI calls them and formats the result; that is the whole boundary.

---

## 3. UI layout and placement

### 3.1 Where it lives

**A new page in the register area's `QStackedWidget`,** exactly the pattern the Financial
Calendar already uses. `MainWindow._install_central` (`mammon/ui/widgets.py:6633`) builds a
`QSplitter` of the `AccountBar` sidebar (`ui/widgets.py:5650`) and a `QStackedWidget`
(`self.stack`, `ui/widgets.py:6641`) whose **page 0 is the `CalendarPanel`**
(`ui/projection_dialogs.py:458`), described in its own docstring as "a PAGE OF THE REGISTER
AREA, not a modal." Investment Central is the same kind of citizen: a `QWidget` added to
`self.stack`, shown where a register would go.

Proposed class: **`InvestmentCentralPanel(QWidget)`** in a new `mammon/ui/` module
(e.g. `ui/investment_central.py`), constructed as `__init__(self, conn, parent=None)`.
It follows the lightweight contract the stack pages already use rather than the full register
contract:

- expose **`reload()`** and **`mark_stale()`** so a write anywhere (via the same
  `mark_stale`/refresh mechanism `CalendarPanel` uses, `ui/widgets.py:8391`) recomputes it
  only when next shown — a 40-year file should not re-aggregate on every keystroke elsewhere;
- it does **not** need the register's `model.reload()` / `apply_display_prefs` /
  `set_view_mode` / `select_txn` surface, because it is not a register; MainWindow keeps a
  reference to it as a dedicated page (like the calendar), not in `self._registers`.

### 3.2 How it is reached

Two entry points, both established patterns; recommend shipping the first, and the second if
cheap:

1. **The AccountBar "Investing" section header.** The sidebar already groups accounts into
   Banking / Investing / Property & Debt (`ui/widgets.py:5650-5656`). Make the Investing
   group's header (or its subtotal row) activate Investment Central — the intuitive "show me
   all of this together" gesture, and it keeps the feature *in the register view* as the
   brief requires. (Individual account rows still open their own registers as today via
   `open_register`, `ui/widgets.py:6977`.)
2. **A menu action** — "View > Investment Central" (or under Reports), mirroring how
   "Tools > Financial Calendar" returns to the calendar page. Modeless; the page is retained
   for the session.

### 3.3 On-page layout (top to bottom)

A vertical stack inside the panel, so the most-summarized information is highest:

1. **Header / summary strip.** Total investment net worth (large), with cost basis, total
   unrealized gain (colored via the per-theme negative/positive colors, SRD 5.10a), and an
   "as of <date>" that defaults to `investments.valuation_as_of`. Fed by
   `reports.investment_central.overview`.
2. **Controls row.** A period selector (This Year / 1Y / Since inception / custom range,
   reusing the report period presets and `make_date_edit`), a scope selector for the
   allocation view (`ALLOCATION_SCOPES`), an "include hidden accounts" toggle, and a
   "prices as of" that reuses the as-of date. All read-only filters; changing one calls
   `reload()`.
3. **Two side-by-side panels** (a horizontal splitter):
   - **Allocation chart** (left) — see section 4.
   - **Net-worth-over-time chart** (right) — see section 4.
4. **Per-account subtotal strip** — a compact table: account name, market value, cash,
   unrealized gain, and period return (IRR) where available. Rows drill down: double-clicking
   an account opens its register via the existing `open_register`; a gear/context action
   opens that account's existing Performance / Lots / Capital Gains / Allocation dialogs
   (already wired on the investment register, `ui/widgets.py:2386-2403`).
5. **Consolidated holdings table** — the big one. Columns: Account, Symbol, Asset Class,
   Quantity, Price, Cost Basis, Market Value, Gain ($ and %), and (in period mode) Period
   Gain. Groupable by account or by asset class, sortable, closed positions optionally shown
   (`include_sold`). Fed by `reports.investment_central.holdings`. Right-click / double-click
   a symbol opens its **price-history chart**. Rendered by a thin `QAbstractTableModel`
   projection over the plain `UnifiedHolding` list — same discipline as
   `InvestmentRegisterModel` ("THIN projection over `investments.register_rows`",
   `ui/models.py:1630/1779`), zero SQL in the model.

### 3.4 Thin-projection compliance

Every widget/model on this page takes plain data structures from section 2 and only renders
them. No `SELECT`, no `execute`, no cents arithmetic beyond display formatting; money in from
the domain as cents, shares/prices as Decimal, dates ISO -> `fmt_date`. This mirrors the
existing investment register models, which a grep confirms hold no raw SQL.

---

## 4. Charts

All charts reuse `mammon/ui/charts.py`, whose canvases subclass `FigureCanvasQTAgg` and take
plain report data (matplotlib stays out of `mammon.ui`'s import path except here). Number
crunching stays in `reports/` / `portfolio`; these classes only render.

1. **Allocation pie — `SlicesPieCanvas`** (`ui/charts.py:263`). It takes plain
   `[(label, cents)]` slices and already provides the "Other" roll-up of small slices and
   drill-down (`zoomChanged`), with a stable color palette. Feed it
   `portfolio.allocation(...).by_class` (default) with a control to switch to `by_security` /
   `by_account`, and honor the scope selector. This is the same canvas the existing Reports >
   Asset Allocation dialog uses, so the dashboard's pie matches it exactly. `unpriced` and
   `cash_only_accounts` from the `Allocation` result should surface as a small caption/warning
   under the pie (they silently distort percentages otherwise — the reason those fields exist).

   For chart color/palette and stat-tile choices, follow the project's dataviz skill so the
   dashboard reads as one system in light and dark themes.

2. **Net-worth-over-time — `NetWorthCanvas`** (`ui/charts.py:499`). Feed it a
   `NetWorthSeries` from `reports.charts.net_worth_series(conn, account_ids=<investment-like ids>, ...)` so the line reflects only investment accounts. (An "all accounts" toggle could
   reuse the same call without the `account_ids` filter, but default to the investment slice.)

3. **Price history — `PriceHistoryCanvas`** (`ui/charts.py:660`), opened on demand from a
   holdings row via the generic **`ChartDialog`** (`ui/charts.py:881`) — the same lightweight
   modal frame the price-history chart already uses from the investment register
   (`ui/widgets.py:3288-3301`). Data from `investments.price_history` (equity) or
   `crypto.price_history` (crypto), routed by the row's account type. `price_history_bounds`
   can shade derived-price uncertainty.

4. **Optional later — a period-return bar** per account (money in / income / gain), using the
   `Performance` figures. Not required for v1; listed so the layout leaves room.

No new chart class is strictly required for v1: `SlicesPieCanvas`, `NetWorthCanvas`, and
`PriceHistoryCanvas` cover the four visuals. Any new canvas would follow the same
"plain-data-in, render-only" contract.

---

## 5. Open questions for the user

1. **Entry point.** Preferred way in: the Investing sidebar-group header, a "View >
   Investment Central" menu action, or both? (Recommendation: sidebar-group header, plus a
   menu action if cheap.)

2. **Default scope and account set.** Should the dashboard default to *investments only*
   (Quicken's answer), *investments + cash*, or *everything owned including property*? The
   allocation function supports all three; the header total and holdings table should agree
   with whatever the default is. (Recommendation: investments-only default, scope selector to
   widen it.)

3. **Crypto performance depth for v1.** Crypto has holdings/valuation and realized gains, but
   **no money-weighted `account_performance` and no `net_contributions_by_symbol`** (both
   exist for equity only). Options: (a) v1 shows crypto in holdings, allocation, and totals
   but shows IRR/period-return columns for equity accounts only; (b) build the crypto
   performance twins first so returns are uniform. (Recommendation: (a) for v1, note the gap.)

4. **Portfolio-level IRR.** Do you want a single money-weighted return for the whole
   portfolio (requires the new `portfolio.portfolio_performance` roll-up), or is per-account
   IRR plus an aggregate dollar gain enough for v1? (Recommendation: per-account IRR in v1,
   portfolio IRR in a Phase 2.)

5. **Hidden and closed positions.** Hidden accounts are excluded from allocation and net
   worth by design (an "incomplete records" signal). Confirm the dashboard should default to
   *excluding* hidden accounts (with a toggle) and *hiding* closed/sold positions in the
   holdings table (with a "show sold" toggle).

6. **Refresh cost vs. freshness.** The `mark_stale`/recompute-on-show model keeps a 40-year
   file responsive but means the page is a snapshot until reopened. Acceptable, or do you want
   a manual "Refresh" affordance and/or live recompute while the page is visible?

7. **Prices/quotes.** Should the dashboard offer an "Update quotes" action (the quote fetch is
   a mutator that touches the network), or stay strictly read-only and value everything at the
   latest recorded close? (Recommendation: strictly read-only; quote fetching stays where it
   is today.)

8. **Relationship to existing windows.** Should Investment Central *replace* the separate
   Reports-menu Allocation / Performance windows over time, or coexist as the consolidated
   entry that links out to them? (Recommendation: coexist first; revisit once it is in use.)

---

## Appendix: SRD / roadmap touchpoints

- Investment lots, cost basis, capital gains, performance, allocation: SRD 5.8d; target
  allocation and drift: SRD 5.8f; asset-class mixture per security: SRD 5.8g.
- Reporting as pure computations (the plain-data contract): SRD 5.9a; report windows,
  export, saved filters: SRD 5.9b (already hosts an Investment Performance report).
- The parity roadmap lists a general "Home dashboard" (item 19) and "Investment views:
  security list, watchlist, security detail, income report" (item 20); Investment Central is
  the investment-focused realization of that direction, reusing the item-7 aggregations.
- Crypto is first-class (`accounts.type = 'crypto'`, tables `crypto_transactions` /
  `crypto_holdings` / `crypto_holdings_checkpoints`), grouped under Investing via
  `ledger.INVESTMENT_LIKE_TYPES`, with `asset_class = 'crypto'` already flowing into the
  allocation pie.
