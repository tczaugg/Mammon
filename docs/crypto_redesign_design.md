# Crypto model redesign — coin-native wallet accounts (DESIGN ONLY)

Status: **design proposal. No code and no migrations are written here** — this document specifies
exactly what would change and why, so the shape can be reviewed and locked before anything is
appended. It is a tracked doc; it deliberately cites only `docs/SRD.md` and source modules, never any
private planning note.

## 0. Why the current model is wrong for a paper wallet

Mammon already ships a crypto subsystem (schema v51–v53; domain layer `mammon/crypto.py`; parser
`mammon/importers/crypto_csv.py` + `mammon/importers/crypto_core.py`; register/holdings UI inline in
`mammon/ui/models.py` and `mammon/ui/widgets.py`; SRD §5.8h/§5.8i/§5.8j). It was built as an
**exchange** model: an account holds a small internal **fiat cash sleeve**, trades one coin for USD or
for other coins, and every row carries a USD price, a USD amount, a running USD cash balance and a
cost basis. The register columns are `Date, Action, Coin / Wallet, Quantity, Price, Coin Bal, Amount,
Cash Bal, Fee` (`mammon/ui/models.py:1949-1954`); the holdings dialog always shows a Cash row and a
Cost Basis column (`mammon/ui/widgets.py:3585-3672`).

That does not describe a **paper Ethereum wallet address**. A wallet address is not a brokerage. It
holds one coin (its native unit, e.g. ETH). It has no USD leg, no cash sleeve, no "buy/sell price"
per event — coin simply arrives and leaves, and the network fee is paid **in the coin itself**. The
user's framing (locked): a crypto **address** account is *like a CASH account denominated in a foreign
currency where that currency is the coin* — the account unit is the coin, the running balance is a
coin quantity, and there is **no USD price, no USD amount, no cash-balance or price column** on the
register.

Two concrete defects follow from forcing a wallet through the exchange model:

1. **The import path is wrong end-to-end.** The dedicated Etherscan parser
   (`crypto_csv.parse_etherscan` → `CryptoRecord`) exists and maps `Value_IN`/`Value_OUT`/`From`/`To`/
   `TxnFee(ETH)` sensibly — **but it has no UI call site at all** (confirmed: no reference to
   `import_etherscan_file` / `import_crypto_records` / `parse_etherscan` anywhere under `mammon/ui/`).
   The **Import…** button on a crypto account instead runs the *generic cash* flow:
   `MainWindow._import_account` → `_ingest_file_via_review` → `importers.import_single_account`
   (`mammon/importers/__init__.py:176`), which has no `account_type == "crypto"` branch, parses the
   file with the generic delimited/`PARSERS` machinery into a cash `NormalizedTxn`, and on accept
   calls `ledger.add_transaction` — writing the row into the cash `transactions` table (the fiat
   sleeve), **not** `crypto_transactions` (`mammon/import_review.py:1286-1297`;
   `mammon/ui/widgets.py:3935-3939`). So a "crypto import" today is indistinguishable from importing a
   bank statement into a checking account: it captures no coin quantity, no symbol, and no
   counterparty. Note on the requirement's "block-number is mapped to an amount": the *dedicated*
   parser never reads `Blockno` at all, but the *generic* importer that the button actually reaches
   has no idea what an Etherscan file is — it sniffs the file's numeric columns for a date/amount/
   balance, which is exactly how a block-number or a running column becomes a bogus "amount." Either
   way the corrected wallet import must map `Value_IN`/`Value_OUT` to a coin increase/decrease and
   must have **no amount column and no cash-balance column**.

2. **The register/holdings UI structurally assumes a cash sleeve.** Even on a wallet where the sleeve
   is always zero, the register shows Price / Amount / Cash Bal, and the holdings dialog appends a
   Cash row and a Cost Basis column. There is no coin-native register and no coin-native review pane
   (the review panel picks its columns by `type == "investment"` only, so a crypto account falls
   through to the generic cash columns `Status, Date, Num, Payee, Memo, Amount` —
   `mammon/ui/import_review_widget.py:55-56,450-455`).

This redesign keeps the exchange model for accounts that really are exchanges, and adds a **coin-native
wallet model** for address accounts — the two styles below.

## 1. The two account styles

### Style A — Address / wallet account (paper wallet; single-coin; cash-like; coin-native)

The account's unit **is** a coin (its "native coin", e.g. ETH). It behaves like a foreign-currency
cash account:

- Every row is a coin delta: coin in, or coin out. There is **no USD price, no USD amount, no cash
  balance, no cost-basis** shown or required on the register.
- The running balance is a **coin quantity** (Decimal), not dollars.
- The payee is the on-chain **counterparty address**: the `From` address for a coin increase, the `To`
  address for a coin decrease.
- The network **fee is denominated in the coin** (ETH), and only the sender pays it.
- Net worth still values the account in USD at the net-worth layer (coin balance × latest price), the
  same way a foreign-currency cash account contributes to net worth at an FX rate — but that USD
  number lives in the net-worth/holdings valuation, **never as a per-row column or a cash balance in
  the register** (see §6). Whether the account tile shows USD at all is Open Question 8.

### Style B — Exchange account (multi-coin; trades coin ↔ coin or coin ↔ currency)

This is the **existing** model, kept as-is: an internal fiat cash sleeve, `BUY`/`SELL` (fiat leg on the
row's `amount`), coin-for-coin `SWAP_OUT`+`SWAP_IN`, USD price/amount/cash-balance columns, cost basis
and lots, USD valuation. Everything in `mammon/crypto.py`'s exchange writers, the current register
columns and the holdings dialog continue to apply unchanged for this style.

### How the two are distinguished and created

Both styles remain `accounts.type = 'crypto'` and both store their events in `crypto_transactions`
(coin quantity is already Decimal TEXT there; the fiat/USD columns are all nullable — see §5). The
wallet address continues to live in `accounts.account_number` (already blanked from the MCP surface).
The distinguishing fact a wallet needs that the schema does not carry today is **its native coin** and
the **flag that it is coin-native**. Recommended (specified, not written):

- Add one nullable column, `accounts.crypto_native_coin TEXT`. **Non-NULL ⇒ Style A** (a single-coin
  wallet whose unit and register denomination is that coin, e.g. `'ETH'`); **NULL ⇒ Style B** (an
  exchange/multi-coin account, the current behaviour). This single column both selects the style and
  names the unit, and it is append-only (see §5, migration `_V61`).
- Creation: the New/Edit account dialog already offers `Crypto` (`_ACCOUNT_TYPES` in
  `mammon/ui/widgets.py:80-81` already contains `"crypto"`). The dialog gains one field shown only for
  a crypto account: **"Native coin (blank = exchange)"**. Entering `ETH` makes a wallet; leaving it
  blank makes an exchange account. Per the recorded invariant, adding behaviour to the domain does not
  make anything creatable — the field must be added to the dialog explicitly; `_ACCOUNT_TYPES` itself
  needs no change because `crypto` is already there.
- Grouping/valuation routing is unchanged: both styles stay in `ledger.INVESTMENT_LIKE_TYPES` and open
  `CryptoRegisterWidget` via the existing exact-type dispatch (`open_register`,
  `mammon/ui/widgets.py:7107`). The widget branches internally on `crypto_native_coin` to render
  coin-native (Style A) vs the current USD layout (Style B).

Alternative considered and **not** recommended: a second free-text account type (`'crypto_wallet'`).
It needs no migration to introduce a *value*, but it forces adding the type to `_ACCOUNT_TYPES`,
`INVESTMENT_LIKE_TYPES`, the sidebar grouping and the `open_register` dispatch, still needs a separate
place to record the native coin, and doubles the number of `== 'crypto'` sites to keep in lock-step. A
single nullable coin column is smaller and safer.

## 2. Corrected Etherscan column → field mapping (Style A wallet)

Etherscan's per-address export header, in order:

```
Txhash, Blockno, UnixTimestamp, DateTime, From, To, ContractAddress,
Value_IN(ETH), Value_OUT(ETH), CurrentValue @ $<rate>/Eth, TxnFee(ETH),
TxnFee(USD), Historical $Price/Eth, Status, ErrCode
```

Corrected mapping onto a coin-native wallet row (the account's native coin supplies the symbol; there
is deliberately **no amount column and no cash-balance column**):

| Etherscan column | Wallet-row field | Rule |
|---|---|---|
| `Txhash` | `tx_hash` | Exact-dedup key `(account_id, tx_hash)`; a re-import is a no-op. |
| `Blockno` | **ignored** | Never a quantity, never an amount. (This is the column the generic importer mis-reads as money.) |
| `UnixTimestamp` (preferred) / `DateTime` | `date` (ISO `YYYY-MM-DD`) | Storage/domain is always ISO. |
| `From` | **payee (counterparty)** — used **when `Value_IN > 0`** | The sender is the payee for a coin increase. |
| `To` | **payee (counterparty)** — used **when `Value_OUT > 0`** | The recipient is the payee for a coin decrease. |
| `ContractAddress` | (token identity; empty for native ETH) | Kept for future multi-token support; a native-ETH wallet leaves it empty (Open Question 6). |
| `Value_IN(ETH)` | **coin quantity, INCREASE (+)** — when `> 0` | Sign/direction derives from the two unsigned columns; exactly one is non-zero. |
| `Value_OUT(ETH)` | **coin quantity, DECREASE (−)** — when `> 0` | |
| `CurrentValue @ $<rate>/Eth` | **ignored** | Export-time rate; never a basis. |
| `TxnFee(ETH)` | **`fee_quantity`, `fee_symbol` = the coin** | Fee is in the coin (ETH), never USD. Booked **only when `From` == the wallet's own address** (only the sender pays gas); an inbound row's fee is the counterparty's and must not debit the user. |
| `TxnFee(USD)` | **ignored** | Fee is coin-denominated, not USD. |
| `Historical $Price/Eth` | **ignored for the wallet register** | The register carries no USD. (It may optionally seed `price_history` for net-worth valuation only — Open Question 8; it never becomes a register column, an `amount`, or a per-row basis.) |
| `Status` / `ErrCode` | row **skipped** when non-empty | A failed transaction moved no value. |

Direction/classification (unchanged from the rules the real 2020 export forced, SRD §5.8i): `Value_IN
> 0` is an inbound coin increase; `Value_OUT > 0` is an outbound coin decrease. A row is an
**own-wallet transfer** (mirror model, no gain, basis carried) only when the *other* address also
belongs to one of the user's Mammon crypto accounts (matched on `accounts.account_number`); otherwise
it is a plain receive/send and the user reclassifies. On the raw chain data none auto-classify as
own-wallet — that is user intent.

Synthetic example rows (ANON addresses only — never a real address or hash):

```
inbound:  Value_IN=0.500000000 ETH, From=0xANON00000000000000000000000000000000A1
          -> +0.5 ETH, payee 0xANON...A1, fee not booked (user is not the sender)
outbound: Value_OUT=0.250000000 ETH, To=0xANON00000000000000000000000000000000B2,
          TxnFee=0.002100000 ETH -> -0.25 ETH, payee 0xANON...B2, fee 0.0021 ETH (user is the sender)
```

## 3. Wallet-address register column set (Style A)

The wallet register is the coin-native analogue of the classic cash register (cash uses `Date, Num,
Payee, Category, Memo, Payment, Deposit, Balance`; coin replaces cents). Proposed columns, in order:

| Column | Content |
|---|---|
| Date | ISO rendered through `ui/models.fmt_date`. |
| Payee | The counterparty address — `From` for a coin increase, `To` for a coin decrease. Rendered as `[Other Wallet]` when the row is an own-wallet transfer (the existing virtual-category convention, consuming no category row). |
| Memo | The user's own note (blanked on accept per the recorded convention; the counterparty address is **not** stuffed into memo). |
| Coin Out | Coin quantity leaving (blank on an increase). |
| Coin In | Coin quantity arriving (blank on a decrease). |
| Balance | Running **coin** balance (Decimal), folding the same-coin gas fee so it ties to `crypto.rebuild_holdings`. |
| Fee | Network fee in the coin, e.g. `0.0021 ETH` (blank when the user is not the sender). |

Explicitly **absent**: Price (USD), Amount (USD), Cash Bal — none exist on a wallet register. (`Coin
Out`/`Coin In` may instead be a single signed `Quantity` column to match the stored signed
`quantity`; the split-column form is recommended for register familiarity — Open Question 7.) An
Action/Num column is optional and omitted by default: on a single-coin wallet the direction is already
carried by the in/out columns and the payee, and "receive vs staking-reward vs mining" is user intent
layered on top, not needed to read the balance.

Like the current crypto grid, the wallet register is **read-only** (events arrive by import; no inline
editor, so no `setModelData` modal hazard). Edits/deletes go through `mammon/crypto.py`'s
`update_event`/`delete_event`.

## 4. Wallet-address review-pane column set (Style A)

The review pane must show the same coin-native shape, not the generic cash columns. Proposed
crypto-wallet review columns (a third header set beside `_HEADERS` and `_INV_HEADERS` in
`mammon/ui/import_review_widget.py:55-56`, selected when the account has a `crypto_native_coin`):

| Column | Content |
|---|---|
| Status | `NEW` / `MATCHING` (existing review status). |
| Date | ISO. |
| Payee | Counterparty address (`From` inbound / `To` outbound). |
| Memo | Optional note. |
| Coin In | Coin quantity arriving (blank on a decrease). |
| Coin Out | Coin quantity leaving (blank on an increase). |
| Fee | Network fee in the coin (blank when the user is not the sender). |

Explicitly **absent**: Amount (USD), Price. The pane selection must key on the wallet flag, not on
`type == "investment"`; today a crypto account silently gets the cash columns because that gate never
matches (`_account_is_investment`, `mammon/ui/import_review_widget.py:450-455`).

## 5. Data-model implications (what storage already does, and what must change)

### 5.1 Storage already supports coin-native rows — no change needed for the row itself

The event columns needed for a wallet row already exist and are nullable, so a coin-only row (quantity
+ coin fee, **no USD**) is storable today (`crypto_transactions`, `db.py:1470-1493`):

- `quantity TEXT` — signed Decimal coin quantity (out = negative). Used directly.
- `fee_symbol TEXT`, `fee_quantity TEXT` — the coin fee. Used directly. `fee_amount INTEGER` (USD) left
  NULL.
- `price INTEGER`→`TEXT` USD FMV, `amount INTEGER` USD cents, `basis INTEGER` USD cents — all
  **nullable**, all left NULL for a wallet row.
- `tx_hash`, `transfer_account_id`, `transfer_pair_id`, `date`, `action`, `memo` — used as today.
- `crypto_holdings.cost_basis` is nullable and `quantity` is Decimal TEXT (`db.py:1504-1512`), so a
  coin-only holding stores fine; the checkpoint columns default to 0 (`db.py:1513-1524`). A wallet's
  cost basis / realized / income simply stay 0.

Net: **no migration is required merely to store coin-native wallet events.** The gaps are (a) the
account-level style marker and native coin, (b) a home for the counterparty address, and (c) the
review-queue payload — below.

### 5.2 New columns/migrations that ARE needed (specified, not written)

Current `SCHEMA_VERSION = 60` (`db.py:1865`, `MIGRATIONS` runs `_V1..._V60`); the next free index is
`_V61`. Each new `_Vn` is appended with its rationale comment and never edits an existing migration.

1. **`_V61` — account style + native coin.** `ALTER TABLE accounts ADD COLUMN crypto_native_coin
   TEXT`. Non-NULL marks a Style-A wallet and names its unit; NULL keeps the current exchange
   behaviour. (One column carries both the flag and the unit.)

2. **`_V62` — counterparty address on the event.** `crypto_transactions` has **no** From/To/payee
   column today (only `memo` and `tx_hash`); a wallet register must display the counterparty as its
   Payee. `ALTER TABLE crypto_transactions ADD COLUMN counterparty TEXT` — the on-chain address that
   is the row's payee (`From` for an inbound row, `To` for an outbound). Alternative: reuse `memo`;
   rejected because the address is machine text that must render as a distinct Payee column and must
   not collide with the user's own note (which they routinely blank on accept). This is a design
   choice to confirm (Open Question 3).

3. **`_V63` — coin-native review payload.** `review_items` is cash-shaped (`amount INTEGER` cents,
   `payee`, `memo`, `check_number`, `is_transfer`, `transfer_account`; `db.py:349-370`) and has no coin
   quantity or coin fee. A wallet review row needs coin in/out, a coin fee, and the counterparty. Two
   options:
   - (a) Add nullable coin columns to `review_items`: `coin_quantity TEXT` (signed), `coin_symbol
     TEXT`, `fee_quantity TEXT`, `fee_symbol TEXT`, `counterparty TEXT`. Simple, queryable, and the
     DB-backed bulk review ops keep working.
   - (b) Carry a serialized crypto payload (the raw record is already available in
     `review_items.raw_json`) and special-case the crypto-wallet accept path to read from it.
   Recommend (a): it keeps the review panel's DB-backed bulk operations (`accept_all`,
   `reload_pending`, visibility toggle) working without the transient in-memory trap that Amazon
   review rows fell into. (Confirm scope — Open Question 4.)

These three migrations are the whole schema surface. No table is redesigned; the exchange tables are
untouched; every add is nullable and append-only.

### 5.3 Domain-layer additions (coin-native writers) — `mammon/crypto.py` stays the sole writer

The existing high-level writers (`record_buy`/`record_sell`/`record_send`/`record_income`/
`record_swap`) each **require** a USD figure as a positional argument and derive `price` from it — they
cannot express "coin arrived, no USD." The low-level `record_event` already accepts everything as
optional and tolerates NULL USD, so the wallet writers are thin coin-native wrappers over it:

- `record_wallet_credit(account_id, date, coin, quantity, *, counterparty, tx_hash, memo=None,
  import_id=None)` — a coin increase; `price`/`amount`/`basis` stay NULL. (Chosen action label is user
  intent — RECEIVE by default; REWARD/AIRDROP/MINING/TRANSFER_IN on reclassification.)
- `record_wallet_debit(account_id, date, coin, quantity, *, counterparty, tx_hash,
  fee_symbol=None, fee_quantity=None, memo=None, import_id=None)` — a coin decrease with an optional
  same-coin fee; USD fields NULL. (SEND by default; TRANSFER_OUT on reclassification.)
- Own-wallet moves keep using `record_wallet_transfer` (the coin mirror model), which already needs no
  FMV and computes basis from the source's lots — for a wallet with no basis history it simply carries
  0, which is correct for a coin-native account.

Because these route through `record_event`/the existing mirror/swap helpers, **`crypto.py` remains the
sole writer of `crypto_*`** and the transfer-mirror/`tx_hash`-dedup invariants stay in one place.
`register_rows` gains a coin-native projection (no cash sleeve, counterparty as payee) selected by the
account's `crypto_native_coin`; the existing USD projection is kept for exchange accounts.

## 6. Preserving the two load-bearing invariants

### 6.1 Sole writer (`mammon/crypto.py`)

- The importer (`crypto_core.import_crypto_records` / `import_etherscan_file`) continues to run **only**
  a `SELECT` for `(account_id, tx_hash)` dedup and to dispatch to `crypto.py`'s writers — never a raw
  `INSERT` (`mammon/importers/crypto_core.py:7-13,160-165`). The new coin-native writers (§5.3) are the
  dispatch targets for wallet rows.
- The UI wiring bug is fixed by routing, not by a new writer: `importers.import_single_account` /
  `_ingest_file_via_review` must detect a `type == 'crypto'` account and hand an Etherscan file to
  `crypto_csv.parse_etherscan` + `crypto_core`, instead of the generic cash `PARSERS` +
  `ledger.add_transaction`. No second writer of `crypto_*` is introduced; the cash path stops being
  (mis)used for coin.

### 6.2 Single `import_review` chokepoint

- Nothing enters `crypto_transactions` until the user accepts a review row, exactly as for cash and
  investments. `mammon/import_review.py` stays the sole writer for that flow.
- The accept path (`import_review.py`, currently branching `is_investment` →
  `investment_transactions`, else `ledger.add_transaction` → `transactions`, `~1239-1297`) gains a
  **crypto-wallet branch**: when the target account has a `crypto_native_coin`, accept calls
  `crypto.record_wallet_credit`/`record_wallet_debit`/`record_wallet_transfer` into
  `crypto_transactions` — **not** `ledger.add_transaction`. This closes the current defect where
  accepting a crypto review row posts into the cash `transactions` sleeve
  (`mammon/import_review.py:1286-1297`; `mammon/ui/widgets.py:3935-3939`).
- `build_review` classifies each parsed wallet row `NEW`/`MATCHING` using `tx_hash` as the exact key
  (the crypto analogue of `fitid`), storing the coin payload per §5.2(3). After a batch,
  `crypto.rebuild_holdings` refreshes holdings + checkpoints, as today.

### 6.3 Net worth vs the coin-native register

The register and review pane are strictly coin-denominated (requirement 1). Net worth is unaffected
because it is computed at a different layer: `investments.display_balance` delegates a `type='crypto'`
account to `crypto.display_balance`, which values coin balance × latest `{COIN}-USD` price from the
shared `price_history`. For a wallet, `crypto.crypto_cash` is 0 (no BUY/SELL rows), so the valuation is
purely coin × price — the foreign-currency-cash-account contribution to net worth. That USD number
appears only in net worth / the holdings valuation, never as a register column. Whether the wallet's
own account tile should show a USD figure or the coin quantity is Open Question 8.

## 7. What stays unchanged (Style B, exchange accounts)

For accounts with `crypto_native_coin IS NULL`: the fiat cash sleeve, `BUY`/`SELL`/`SWAP_OUT`/`SWAP_IN`,
the current register columns (`Date, Action, Coin / Wallet, Quantity, Price, Coin Bal, Amount, Cash
Bal, Fee`), the holdings dialog with Cost Basis and a Cash row, USD valuation, lots and realized-gain
checkpoints — all as they are today. The redesign is strictly additive: it introduces a coin-native
rendering + import + accept path selected by one nullable account column, and leaves the exchange path
byte-for-byte where it is.

## 8. Open questions for the user

1. **Native-coin marker.** Approve `accounts.crypto_native_coin TEXT` (non-NULL ⇒ single-coin wallet,
   NULL ⇒ exchange) as the style selector, versus a separate `'crypto_wallet'` account type? (§1)
2. **Multi-coin on a wallet.** An ETH wallet that receives ERC-20 tokens (or pays ETH gas on a token
   move) would hold more than one symbol. For the first cut, is a wallet strictly single-coin (its
   native coin only), with token activity out of scope until a second design pass? (§1, §2)
3. **Counterparty storage.** Add a dedicated `crypto_transactions.counterparty` column for the From/To
   address, or reuse `memo`? (Recommend a dedicated column; §5.2)
4. **Review payload.** Add nullable coin columns to `review_items`, or serialize a crypto payload into
   the existing `raw_json` and special-case accept? (Recommend real columns; §5.2)
5. **Fee treatment.** Confirm the coin fee is recorded as a plain coin outflow (the `fee_*` leg on the
   parent row) with **no** USD value and **no** tax-lot / basis adjustment — matching SRD §5.8h's
   default? Or should a wallet fee be a standalone row?
6. **Token identity.** When tokens appear, is a bare ticker (`USDC`) sufficient, or must
   `ContractAddress` disambiguate same-ticker tokens across chains? (Drives whether a future column is
   needed; §2)
7. **Register column form.** Split `Coin In` / `Coin Out` columns (cash-register familiarity), or a
   single signed `Quantity` column matching the stored value? Include an Action/Num column, or omit it
   on a single-coin wallet? (§3)
8. **USD anywhere for a wallet?** The register carries no USD by requirement. Should the wallet still
   contribute to **net worth** in USD (coin × latest price, as a foreign-currency cash account would),
   and should its account tile show USD or the coin quantity? If USD net worth is wanted, may the
   importer seed `price_history` from `Historical $Price/Eth` (value only, never a register column)?
   (§6.3, §2)
9. **Own-wallet transfer detection.** Confirm the mirror-model rule: a row is an own-wallet transfer
   only when the other address matches another Mammon crypto account's `account_number`; otherwise it
   stays receive/send for the user to reclassify. (§2)
10. **Exchange style scope.** Is any exchange (Style B) work in scope now, or is the immediate goal
    only the paper-wallet (Style A) path, leaving the existing exchange model untouched until an
    exchange CSV is on hand? (§7)
