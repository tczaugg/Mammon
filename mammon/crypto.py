"""mammon.crypto -- the cryptocurrency domain layer.

Why this is a PARALLEL module to :mod:`mammon.investments` rather than folded
into it, or routed through :mod:`mammon.ledger`:

- A coin position is not an equity lot. Quantities are wei-scale (up to 18
  decimals), the event taxonomy adds swaps, on-chain transfers, staking/airdrop/
  mining income and network gas fees that have no equity analogue, and the
  natural exact-dedup key is an on-chain ``tx_hash``. So crypto gets its OWN
  tables (``crypto_transactions`` / ``crypto_holdings`` /
  ``crypto_holdings_checkpoints``, schema v51-53), the twins of
  ``investment_transactions`` / ``holdings`` / ``holdings_checkpoints``.

- SINGLE-WRITER discipline. Investment rows are NOT written through
  ``ledger.py``; ``mammon.investments`` owns them. Crypto is likewise its own
  writer: this module is the SOLE writer of the ``crypto_*`` tables, so the
  transfer-mirror and swap-pairing invariants can be enforced in exactly one
  place. ``ledger.py`` remains the only writer of the cash ``transactions``
  table -- crypto does not add a second writer there. The fiat side of a
  buy/sell rides on the crypto row's own ``amount`` (an internal cash sleeve,
  exactly as ``investments`` keeps buy/sell cash in ``investment_transactions``
  rather than opening a second cash ledger), so there is ONE story for both.

Conventions (locked, same as the rest of the app):

- Cash / fiat is signed integer CENTS. Quantities and per-unit prices are
  Decimal-encoded TEXT, which round-trips EXACTLY at any decimal count (an
  18-decimal wei value survives ``str()``/TEXT storage losslessly).

- The one genuinely new precision hazard crypto introduces over equities is
  SUMMATION, not storage. Python's DEFAULT decimal context is only 28
  significant digits, so ``Decimal('1e11') + 1 wei`` silently drops the wei.
  Storage is always exact; only quantity ARITHMETIC is at risk. Every quantity
  calculation here therefore runs inside a local high-precision decimal context
  (>= 40 significant digits) via :func:`quantity_context` -- the replay entry
  points wrap their whole body in ``with decimal.localcontext(...)``, so all the
  nested lot math inherits it -- rather than trusting the process default.

A crypto account is a DISTINCT ``accounts.type`` value (``'crypto'``) -- a coin
wallet is not an equity brokerage -- but is classified INVESTMENT-LIKE
(``ledger.INVESTMENT_LIKE_TYPES``) for net worth, sidebar grouping and the
allocation pie. The wallet address lives in the existing
``accounts.account_number`` column (already blanked from the MCP surface), and
``asset_class = 'crypto'`` carries the allocation classification.

Two crypto account KINDS share that type, recorded explicitly in
``accounts.crypto_kind`` (migration 61; SRD §5.8) -- BOTH multi-token and valued
like securities (a quantity per token, priced at market):

- ``'wallet'`` -- a single address / paper wallet holding coins/ERC-20 tokens
  ONLY, no fiat. Coin arrives/leaves with NO USD leg (the ShrsIn/ShrsOut analogue),
  the network fee is paid IN the coin, and the on-chain ``From``/``To`` counterparty
  IS the row's payee (stored in ``crypto_transactions.payee``, migration 61 -- there
  is no separate "counterparty" concept). Coin-native moves go through
  :func:`record_wallet_credit` / :func:`record_wallet_debit` (and
  :func:`record_wallet_transfer` for own-wallet moves).
- ``'exchange'`` -- coins PLUS fiat currencies: the BUY/SELL/SWAP model below with
  an internal USD cash sleeve.

``crypto_kind`` is a real typed value, never an overloaded NULL; a NULL kind means
"not a crypto account". Both kinds keep every token as a distinct security position
('USDC', 'ETH', 'LINK' never merge), so a wallet that receives ERC-20 tokens holds
them alongside its ETH without special-casing.

Event taxonomy (schema v51 ``action`` enum) mapped to primitives:

- ``BUY`` / ``SELL`` -- fiat<->coin. BUY adds a lot and debits the cash sleeve
  (``amount<0``); SELL disposes coin for cash (``amount>0``) and books realized
  gain (proceeds - the cost relieved from lots, per ``accounts.lot_method``).
- ``SWAP_OUT`` + ``SWAP_IN`` -- a coin-for-coin trade is TWO single-asset legs
  in ONE account, linked by ``swap_group_id`` (the intra-account analogue of
  ``transfer_pair_id``, distinct because a swap is two coins in one account, not
  one coin across two accounts). SWAP_OUT is a disposal at fair-market value;
  SWAP_IN establishes a new lot whose basis is that same FMV.
- ``TRANSFER_OUT`` + ``TRANSFER_IN`` -- a wallet-to-wallet move of the SAME coin
  is the EXISTING transfer mirror model re-expressed for coin QUANTITY instead
  of cents: two rows linked by ``transfer_pair_id``, each ``transfer_account_id``
  pointing at the other wallet. No fiat, no realized gain; the cost basis rides
  along (the OUT leg relieves it, the IN leg re-adds exactly that basis). Edit
  one leg's date/quantity -> the mirror syncs; delete one -> both go.
- ``SEND`` / ``RECEIVE`` -- to/from a third party. SEND is a disposal at FMV;
  RECEIVE is ordinary income at FMV (basis = FMV).
- ``REWARD`` / ``INTEREST`` / ``AIRDROP`` / ``MINING`` -- in-kind income credited
  as coin QUANTITY (not cents), basis = FMV at receipt, accruing to the
  checkpoint ``income`` column (the crypto analogue of an investments dividend).
- ``FEE`` -- a network/gas fee. When it rides an existing action it is carried
  in ``fee_symbol`` / ``fee_quantity`` / ``fee_amount`` on the parent row (on
  Ethereum, moving 100 USDC costs gas IN ETH -- one action, two holdings deltas);
  when the fee coin must debit a distinct holding on its own it is a standalone
  ``FEE`` row. Either way the default treatment is a PLAIN EXPENSE: the fee
  quantity's basis simply leaves the holding, no realized gain is booked (tax-lot
  precision on gas is an opt-in the user has not asked for).
- ``FORK`` -- a chain split crediting a new symbol; basis per policy (default the
  caller-supplied FMV, or 0 -- disputed, so never hardcoded here).
"""
from __future__ import annotations

import datetime as _dt
import decimal
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

import sqlite3

from mammon import ledger, investments

# The distinct account.type value shared by both crypto account KINDS, and the
# asset_class that flows a coin position into the allocation pie / rebalance drift.
CRYPTO_ACCOUNT_TYPE = "crypto"
CRYPTO_ASSET_CLASS = "crypto"

# The two crypto account KINDS (SRD §5.8; both share accounts.type='crypto' and are
# BOTH multi-token, valued like securities -- a quantity per token, priced at market).
# They differ only in whether an internal fiat cash sleeve exists:
#   * WALLET   -- a single address / paper wallet holding coins/ERC-20 tokens ONLY.
#                 Movements are coin-native (the ShrsIn/ShrsOut analogue): a quantity
#                 in or out, NO fiat leg, the on-chain From/To counterparty as the
#                 payee, and any network fee paid IN the coin.
#   * EXCHANGE -- coins PLUS fiat currencies: the existing BUY/SELL/SWAP model with an
#                 internal USD cash sleeve.
# Stored explicitly in ``accounts.crypto_kind`` (migration 61) -- never an overloaded
# NULL. A NULL kind means "not a crypto account".
CRYPTO_KIND_WALLET = "wallet"
CRYPTO_KIND_EXCHANGE = "exchange"
CRYPTO_KINDS = (CRYPTO_KIND_WALLET, CRYPTO_KIND_EXCHANGE)

# Significant digits for quantity arithmetic. The default context (28) can drop
# a wei when summing a large balance; 40 clears realistic integer-part +
# 18-decimal magnitudes with room to spare. See the module docstring.
QUANTITY_PRECISION = 40

LOT_METHODS = ("average", "fifo", "lifo")

# ---------------------------------------------------------------------------
# Action vocabulary (compared normalized to UPPER-case; stored UPPER-case to
# match the schema v51 enum). Which actions ADD coin (with basis), which REMOVE
# it, which removals are true DISPOSALS (book realized gain at FMV/proceeds) as
# opposed to a basis-preserving transfer/fee, and which are income (accrue FMV
# to the checkpoint ``income`` total).
# ---------------------------------------------------------------------------
_ADD_ACTIONS = {
    "BUY", "BUYX", "SWAP_IN", "TRANSFER_IN", "RECEIVE",
    "REWARD", "INTEREST", "AIRDROP", "MINING", "FORK",
}
_REMOVE_ACTIONS = {"SELL", "SELLX", "SWAP_OUT", "TRANSFER_OUT", "SEND", "FEE"}
# Quicken's X-twins, and Mammon already speaks them on the investment side
# (`investments._CASH_ZERO_ACTIONS`): a trade whose cash arrives or leaves by
# TRANSFER rather than sitting in the account. Same meaning in coin -- SELLX is a
# real disposal (gain is booked against the relieved basis) whose proceeds go
# straight to another account, so this account's cash nets to zero.
CROSS_ACTIONS = {"BUYX", "SELLX"}
# Fiat moving in or out of an EXCHANGE account's internal cash sleeve, carrying
# NO coin: a bank deposit, a cash withdrawal, or dollars arriving from another
# venue. Deliberately in NEITHER add nor remove set -- they open and relieve no
# position, and `_apply_txn` already skips a symbol-less row, so the holdings
# replay ignores them while `crypto_cash` (which sums every non-NULL `amount`)
# picks them up. A WALLET never has one: it holds no fiat.
_CASH_ACTIONS = {"DEPOSIT", "WITHDRAW"}
CASH_ACTIONS = _CASH_ACTIONS
# A disposal for value books realized gain; a TRANSFER_OUT (basis rides to the
# other wallet) and a FEE (plain expense) remove coin WITHOUT booking a gain.
_DISPOSAL_ACTIONS = {"SELL", "SELLX", "SWAP_OUT", "SEND"}
# In-kind income: credited as coin, valued at FMV, summed into checkpoint income.
_INCOME_ACTIONS = {"RECEIVE", "REWARD", "INTEREST", "AIRDROP", "MINING"}

ACTIONS = _ADD_ACTIONS | _REMOVE_ACTIONS | _CASH_ACTIONS


def payee_role(action) -> str | None:
    """Which END of the movement a row's ``payee`` names, from its ACTION alone.

    Coin coming IN (an :data:`_ADD_ACTIONS` credit) names its SOURCE -- the
    on-chain ``From``/sender -- so the payee is a ``"from"``. Coin going OUT (a
    :data:`_REMOVE_ACTIONS` debit) names its DESTINATION -- the ``To``/recipient
    -- so the payee is a ``"to"``. A cash row (DEPOSIT/WITHDRAW) has no on-chain
    counterparty and returns ``None``.

    This makes the convention ``crypto_transactions.payee`` already stores under
    (schema v61) EXPLICIT and testable: flipping a row's direction (SEND ->
    RECEIVE) keeps the same counterparty string but flips what it MEANS, from the
    recipient to the sender, and this is the one function that says so."""
    a = _norm(action)
    if a in _ADD_ACTIONS:
        return "from"
    if a in _REMOVE_ACTIONS:
        return "to"
    return None


def quantity_context() -> decimal.Context:
    """A high-precision :class:`decimal.Context` for wei-scale quantity math.

    Use as ``with decimal.localcontext(crypto.quantity_context()): ...`` so a
    sum of many wei-scale quantities does not silently lose low-order digits
    under the process default 28-significant-digit context. Storage is exact
    regardless; this guards the SUMMATION, which is the only place the ceiling
    bites.
    """
    return decimal.Context(prec=QUANTITY_PRECISION)


# ---------------------------------------------------------------------------
# Decimal / money helpers (mirror investments._D / _cents / _qty_text -- kept
# local so this module does not reach into another module's private internals).
# ---------------------------------------------------------------------------
_CENT = Decimal("1")
_HUNDRED = Decimal("100")


def _D(value) -> Decimal:
    """Tolerant Decimal parse; '' / None -> 0."""
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return Decimal(0)


def _cents(value: Decimal) -> int:
    """Round a Decimal amount to integer cents, HALF_UP."""
    return int(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _qty_text(qty: Decimal) -> str:
    """A clean, exponent-free Decimal string for storage ('100' not '1E+2'),
    normalized under the high-precision quantity context so a wei-scale value
    normalizes without loss. Mirrors ``investments._qty_text``."""
    with decimal.localcontext(quantity_context()):
        if qty == 0:
            return "0"
        return format(qty.normalize(), "f")


def _row_value(row, key):
    """Read ``key`` from a sqlite3.Row OR a plain dict, tolerating its absence."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _norm(action) -> str:
    """Normalize an action for comparison: trim, upper-case, collapse spaces."""
    return (action or "").strip().upper().replace(" ", "_").replace("-", "_")


def _validate_date(date: str) -> None:
    """Fail loudly on a non-ISO date rather than storing garbage the replay
    (which slices ``substr(date,1,4)`` for the year) would silently mishandle."""
    _dt.date.fromisoformat(date)


def pair_symbol(symbol: str) -> str:
    """The yfinance USD pair ticker for a bare coin symbol -- ``'ETH'`` ->
    ``'ETH-USD'``. Prices are stored in the shared ``price_history`` table under
    this pair, which is the namespace that keeps a coin ``ABC`` from colliding
    with a stock ``ABC`` (see db.py migration 51-53 rationale)."""
    return f"{(symbol or '').strip().upper()}-USD"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def is_crypto_account(acct: Optional[sqlite3.Row]) -> bool:
    """Whether ``acct`` (a row, or None) is a crypto account of EITHER kind."""
    return acct is not None and (acct["type"] or "") == CRYPTO_ACCOUNT_TYPE


def _norm_kind(kind: Optional[str]) -> str:
    """Normalise/validate a crypto account KIND, raising on anything unexpected so a
    bad value is caught at creation rather than silently mis-routing later."""
    k = (kind or "").strip().lower()
    if k not in CRYPTO_KINDS:
        raise ValueError(f"unknown crypto account kind {kind!r}; one of {CRYPTO_KINDS}")
    return k


def account_kind(acct: Optional[sqlite3.Row]) -> Optional[str]:
    """The crypto account KIND -- :data:`CRYPTO_KIND_WALLET` or
    :data:`CRYPTO_KIND_EXCHANGE` -- read from ``accounts.crypto_kind``, or ``None``
    for a non-crypto account."""
    if not is_crypto_account(acct):
        return None
    return _row_value(acct, "crypto_kind")


def is_wallet_account(acct: Optional[sqlite3.Row]) -> bool:
    """A single address / paper wallet: coins/tokens only, coin-native, no fiat
    sleeve. Movements go through :func:`record_wallet_credit` /
    :func:`record_wallet_debit` (and :func:`record_wallet_transfer` for own-wallet
    moves)."""
    return account_kind(acct) == CRYPTO_KIND_WALLET


def is_exchange_account(acct: Optional[sqlite3.Row]) -> bool:
    """A crypto exchange/custodial account: coins PLUS a fiat cash sleeve, driven by
    the BUY/SELL/SWAP writers. Any crypto account not explicitly a wallet reads as an
    exchange -- migration 61 backfills legacy crypto accounts (whose kind predates the
    split) to 'exchange', so this is the historical behaviour, not NULL-overloading."""
    return is_crypto_account(acct) and account_kind(acct) != CRYPTO_KIND_WALLET


def is_investment_like(acct: Optional[sqlite3.Row]) -> bool:
    """Whether ``acct`` is valued at market and grouped with investments -- an
    equity brokerage OR a crypto wallet. Classification tests membership in the
    single source of truth ``ledger.INVESTMENT_LIKE_TYPES``."""
    return acct is not None and (acct["type"] or "") in ledger.INVESTMENT_LIKE_TYPES


def create_account(conn: sqlite3.Connection, name: str, *,
                   kind: str = CRYPTO_KIND_EXCHANGE,
                   opening_balance: int = 0,
                   opening_date: Optional[str] = None,
                   institution: Optional[str] = None,
                   note: Optional[str] = None,
                   wallet_address: Optional[str] = None) -> int:
    """Create a crypto account (``type='crypto'``) of the given ``kind`` and return
    its id.

    ``kind`` is :data:`CRYPTO_KIND_WALLET` (a single address / paper wallet holding
    coins/tokens only, coin-native) or :data:`CRYPTO_KIND_EXCHANGE` (a multi-coin
    exchange with a fiat sleeve). It defaults to EXCHANGE -- the model this
    constructor has always produced -- so existing callers are unchanged; a
    coin-native wallet opts in with ``kind='wallet'``. The chosen kind is stored
    explicitly in ``accounts.crypto_kind``.

    Account rows are written by ``ledger`` (the one writer of the accounts table);
    this wrapper only fixes the type to ``'crypto'``, stamps
    ``asset_class='crypto'`` so the position flows into the allocation pie, and
    records the kind. The wallet address, when supplied, is stored in
    ``account_number`` (the same column the MCP authorizer blanks) -- crypto
    introduces no credential storage.
    """
    k = _norm_kind(kind)
    account_id = ledger.create_account(
        conn, name, CRYPTO_ACCOUNT_TYPE,
        opening_balance=opening_balance, opening_date=opening_date,
        institution=institution, note=note,
    )
    fields: dict = {"asset_class": CRYPTO_ASSET_CLASS, "crypto_kind": k}
    if wallet_address is not None:
        fields["account_number"] = wallet_address
    ledger.update_account(conn, account_id, **fields)
    return account_id


def get_account(conn: sqlite3.Connection, account_id: int) -> Optional[sqlite3.Row]:
    """Read a crypto account back. Reads go through ``ledger.get_account`` (the
    accounts table has one reader path); provided here so callers can round-trip
    a crypto account entirely through :mod:`mammon.crypto`."""
    return ledger.get_account(conn, account_id)


def list_accounts(conn: sqlite3.Connection,
                  include_closed: bool = False,
                  include_hidden: bool = False) -> list[sqlite3.Row]:
    """Just the crypto accounts (either kind), filtered from ``ledger.list_accounts``."""
    return [a for a in ledger.list_accounts(
        conn, include_closed=include_closed, include_hidden=include_hidden)
        if (a["type"] or "") == CRYPTO_ACCOUNT_TYPE]


def get_lot_method(conn, account_id: int) -> str:
    """How this account costs a disposal: ``average`` (the default), ``fifo`` or
    ``lifo``. Shares the ``accounts.lot_method`` column with investments."""
    row = conn.execute("SELECT lot_method FROM accounts WHERE id=?",
                       (account_id,)).fetchone()
    m = (row["lot_method"] if row is not None else None) or "average"
    return m if m in LOT_METHODS else "average"


# ---------------------------------------------------------------------------
# Replay state (per-symbol lots + running totals), mirrored on investments'
# _Lot / _Position but with an ``income`` total (coin-denominated income at FMV)
# in place of ``dividends``.
# ---------------------------------------------------------------------------
@dataclass
class _Lot:
    """One tax lot: coin acquired together, and what it cost (cents). ``date`` /
    ``txn_id`` name the acquiring row; both None for coin whose lot history is
    not known (a position restored from a pre-lots snapshot)."""
    qty: Decimal = Decimal(0)
    cost: int = 0
    date: Optional[str] = None
    txn_id: Optional[int] = None


@dataclass
class _Position:
    """Full per-symbol replay state: the open lots (qty + cost) plus the running
    totals the holdings/per-coin views need -- ``income`` (in-kind income valued
    at FMV), ``realized`` gain/loss booked on disposals, and ``ever_held``."""
    qty: Decimal = Decimal(0)
    cost: int = 0            # cents, cost basis of the remaining coin
    income: int = 0          # cents, in-kind income attributed to the symbol
    realized: int = 0        # cents, realized gain/loss from disposals
    ever_held: bool = False
    lots: list = field(default_factory=list)
    gains: list = field(default_factory=list, compare=False)


@dataclass
class RealizedGain:
    """One disposal matched to one lot: when the coin was acquired and disposed,
    what it brought, what it cost."""
    sale_txn_id: Optional[int]
    symbol: str
    acquired: Optional[str]
    sold: str
    quantity: Decimal
    proceeds: int                     # cents
    basis: int                        # cents relieved

    @property
    def gain(self) -> int:
        return self.proceeds - self.basis


def _basis_of(t, qty: Decimal) -> int:
    """Cost basis (cents) an acquiring action assigns to the acquired ``qty``.
    Prefers an explicit ``basis`` (income FMV, or a transfer's basis-riding
    value), then the fiat ``amount`` a BUY spent, then ``price*qty``."""
    b = _row_value(t, "basis")
    if b is not None:
        return abs(int(b))
    amount = _row_value(t, "amount")
    if amount not in (None, 0):
        return abs(int(amount))
    price = _row_value(t, "price")
    return _cents(qty * _D(price) * _HUNDRED) if price else 0


def _proceeds_of(t, qty: Decimal) -> int:
    """Disposal proceeds (cents) at FMV: the fiat ``amount`` a SELL brought in,
    else ``price*qty`` (a SWAP_OUT/SEND carries its FMV as the per-unit price),
    else an explicit ``basis``."""
    amount = _row_value(t, "amount")
    if amount not in (None, 0):
        return abs(int(amount))
    price = _row_value(t, "price")
    if price:
        return _cents(qty * _D(price) * _HUNDRED)
    b = _row_value(t, "basis")
    return abs(int(b)) if b is not None else 0


def _planned(plan: list, lot) -> Decimal:
    return sum((x for l, x in plan if l is lot), Decimal(0))


def _spread_cost(pos: _Position) -> None:
    """Re-spread ``pos.cost`` over the open lots in proportion to their coin, the
    last lot absorbing the rounding so the lots always sum to the position."""
    total_qty = sum((lot.qty for lot in pos.lots), Decimal(0))
    if not pos.lots or total_qty <= 0:
        return
    allotted = 0
    for i, lot in enumerate(pos.lots):
        lot.cost = (pos.cost - allotted if i == len(pos.lots) - 1
                    else _cents(Decimal(pos.cost) * lot.qty / total_qty))
        allotted += lot.cost


def _relieve(pos: _Position, q: Decimal, method: str) -> list:
    """Take ``q`` coin out of ``pos`` and return what each lot gave up as
    ``[(lot_date, lot_txn_id, qty, cost)]``, lowering ``pos.cost`` by the total.
    Order per ``method`` -- oldest first (fifo/average) or newest first (lifo);
    average spreads the position's per-coin average over the coin removed. With
    no lot history the aggregate average is relieved. Mirrors
    ``investments._relieve`` minus specify-lots (crypto has no lot_assignments)."""
    if q <= 0 or pos.qty <= 0:
        return []
    if not pos.lots:
        avg = Decimal(pos.cost) / pos.qty
        taken = min(pos.cost, _cents(avg * q))
        pos.cost -= taken
        return [(None, None, q, taken)]
    method = (method or "average").lower()
    plan: list = []
    remaining = q
    order = list(reversed(pos.lots)) if method == "lifo" else list(pos.lots)
    for lot in order:
        if remaining <= 0:
            break
        avail = lot.qty - _planned(plan, lot)
        if avail <= 0:
            continue
        take = min(avail, remaining)
        plan.append((lot, take))
        remaining -= take
    out: list = []
    if method == "average":
        avg = Decimal(pos.cost) / pos.qty
        total = min(pos.cost, _cents(avg * q))
        planned_qty = sum((x for _, x in plan), Decimal(0))
        allotted = 0
        for i, (lot, take) in enumerate(plan):
            c = (total - allotted if i == len(plan) - 1
                 else _cents(Decimal(total) * take / planned_qty))
            allotted += c
            out.append((lot.date, lot.txn_id, take, c))
        for lot, take in plan:
            lot.qty -= take
        pos.lots = [lot for lot in pos.lots if lot.qty > 0]
        pos.cost -= total
        _spread_cost(pos)
    else:
        total = 0
        for lot, take in plan:
            c = lot.cost if take >= lot.qty else _cents(Decimal(lot.cost) * take / lot.qty)
            lot.cost -= c
            lot.qty -= take
            total += c
            out.append((lot.date, lot.txn_id, take, c))
        pos.lots = [lot for lot in pos.lots if lot.qty > 0]
        pos.cost -= total
    return out


def _gain_rows(t, sym: str, q: Decimal, proceeds: int, taken: list) -> list:
    """One :class:`RealizedGain` per lot a disposal drew on, the net proceeds
    shared out by coin (the last lot absorbs the rounding)."""
    if not taken:
        return []
    total_qty = sum((x for _, _, x, _ in taken), Decimal(0))
    out, allotted = [], 0
    for i, (date, txn_id, take, cost) in enumerate(taken):
        p = (proceeds - allotted if i == len(taken) - 1
             else _cents(Decimal(proceeds) * take / total_qty))
        allotted += p
        out.append(RealizedGain(_row_value(t, "id"), sym, date, t["date"], take, p, cost))
    return out


def _apply_fee(positions: dict, t, method: str) -> None:
    """Apply a network/gas fee carried on the parent row (``fee_symbol`` /
    ``fee_quantity``) as a PLAIN EXPENSE: the fee coin's basis simply leaves the
    holding, no realized gain. Handles the same-coin case (ETH gas on an ETH move
    reduces the same position) and the different-coin case (ETH gas on a token
    move reduces the ETH position) uniformly."""
    fsym = _row_value(t, "fee_symbol")
    fq = _D(_row_value(t, "fee_quantity"))
    if not fsym or fq <= 0:
        return
    fpos = positions.setdefault(fsym, _Position())
    _relieve(fpos, fq, method)
    fpos.qty -= fq
    if fpos.qty == 0:
        fpos.cost = 0
        fpos.lots = []
    fpos.ever_held = True


def _apply_txn(positions: dict, t, method: str = "average") -> None:
    """Fold one crypto transaction into the running per-symbol :class:`_Position`
    map. ONE branch table shared by the from-inception replay and the
    snapshot+delta replay, so a holdings read and the checkpoint path can never
    disagree. Quantity is stored SIGNED (out negative); the magnitude used here is
    ``abs`` so a mis-signed import cannot flip an add into a remove."""
    sym = _row_value(t, "symbol")
    a = _norm(_row_value(t, "action"))
    q = _D(_row_value(t, "quantity"))
    aq = abs(q)
    if sym:
        pos = positions.setdefault(sym, _Position())
        if a in _ADD_ACTIONS:
            basis = _basis_of(t, aq)
            pos.qty += aq
            pos.cost += basis
            if aq > 0:
                pos.lots.append(_Lot(aq, basis, _row_value(t, "date"),
                                     _row_value(t, "id")))
            if a in _INCOME_ACTIONS:
                pos.income += basis
            pos.ever_held = True
        elif a in _REMOVE_ACTIONS:
            cost_before = pos.cost
            taken = _relieve(pos, aq, method)
            pos.qty -= aq
            if pos.qty == 0:
                pos.cost = 0
                pos.lots = []
            if a in _DISPOSAL_ACTIONS:
                proceeds = _proceeds_of(t, aq)
                pos.realized += proceeds - (cost_before - pos.cost)
                pos.gains.extend(_gain_rows(t, sym, aq, proceeds, taken))
            pos.ever_held = True
    # A network fee can ride ANY action (or a standalone FEE row carries it in the
    # main quantity, handled by the _REMOVE branch above).
    _apply_fee(positions, t, method)


# ---------------------------------------------------------------------------
# Holdings replay + per-year checkpoints (a direct port of the investments
# machinery, targeting the crypto_* tables; the ``income`` column stands in for
# investments' ``dividends``).
# ---------------------------------------------------------------------------
def _txn_years(conn, account_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT substr(date,1,4) AS yr FROM crypto_transactions "
        "WHERE account_id=? ORDER BY yr",
        (account_id,),
    ).fetchall()
    return [int(r["yr"]) for r in rows]


def _boundary_year(conn, account_id: int, as_of: Optional[str]) -> int:
    if as_of is not None:
        return int(as_of[:4])
    row = conn.execute(
        "SELECT MAX(substr(date,1,4)) FROM crypto_transactions WHERE account_id=?",
        (account_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] else 0


def _list_txns_in_range(conn, account_id: int,
                        after: Optional[str], through: Optional[str]) -> list:
    """Crypto transactions in application order (date, id) with dates in
    ``(after, through]`` -- either bound may be ``None`` for open-ended."""
    sql = "SELECT * FROM crypto_transactions WHERE account_id=?"
    params: list = [account_id]
    if after is not None:
        sql += " AND date>?"
        params.append(after)
    if through is not None:
        sql += " AND date<=?"
        params.append(through)
    sql += " ORDER BY date, id"
    return conn.execute(sql, tuple(params)).fetchall()


def _load_holdings_checkpoint(conn, account_id: int, before_year: int):
    """Seed positions from the newest year-end snapshot strictly before
    ``before_year``. Returns ``(positions, snapshot_year)``; ``snapshot_year`` is
    ``None`` (positions empty) when no earlier snapshot exists."""
    yr = conn.execute(
        "SELECT MAX(year) FROM crypto_holdings_checkpoints WHERE account_id=? AND year<?",
        (account_id, before_year),
    ).fetchone()
    snap_year = yr[0] if yr else None
    if snap_year is None:
        return {}, None
    positions: dict[str, _Position] = {}
    for r in conn.execute(
        "SELECT symbol, quantity, cost_basis, income, realized, ever_held, lots "
        "FROM crypto_holdings_checkpoints WHERE account_id=? AND year=?",
        (account_id, snap_year),
    ).fetchall():
        qty = _D(r["quantity"])
        raw = _row_value(r, "lots")
        if raw:
            lots = [_Lot(_D(l[1]), int(l[2]), l[0], l[3]) for l in json.loads(raw)]
        else:
            lots = [_Lot(qty, r["cost_basis"] or 0, None, None)] if qty > 0 else []
        positions[r["symbol"]] = _Position(
            qty=qty, cost=r["cost_basis"], income=r["income"],
            realized=r["realized"], ever_held=bool(r["ever_held"]), lots=lots,
        )
    return positions, snap_year


def _replay_positions(conn, account_id: int, as_of: Optional[str] = None,
                      use_snapshots: bool = True) -> dict:
    """Replay the account's crypto transactions into per-symbol
    :class:`_Position` records as of ``as_of`` (default: everything). The ONE
    engine behind :func:`compute_holdings` and the valuation/realized-gain views.
    When ``use_snapshots`` is set (and snapshots exist), the position is seeded
    from the prior year's checkpoint and only the remaining current-year rows are
    replayed -- IDENTICAL to a from-inception replay (asserted in the tests); pass
    ``use_snapshots=False`` for that inception oracle. All quantity math runs
    under the high-precision context (wraps the whole body)."""
    with decimal.localcontext(quantity_context()):
        positions: dict[str, _Position]
        lower: Optional[str] = None
        if use_snapshots:
            boundary_year = _boundary_year(conn, account_id, as_of)
            positions, snap_year = _load_holdings_checkpoint(conn, account_id, boundary_year)
            if snap_year is not None:
                lower = f"{snap_year}-12-31"
        else:
            positions = {}
        method = get_lot_method(conn, account_id)
        for t in _list_txns_in_range(conn, account_id, lower, as_of):
            _apply_txn(positions, t, method)
        return positions


def compute_holdings(conn, account_id: int, as_of: Optional[str] = None) -> dict:
    """Replay into ``{symbol: _Lot(qty, cost)}`` -- pure read, a thin projection
    of :func:`_replay_positions` (qty + cost only)."""
    return {
        sym: _Lot(pos.qty, pos.cost)
        for sym, pos in _replay_positions(conn, account_id, as_of).items()
    }


def _write_snapshot(conn, account_id: int, year: int, positions: dict) -> None:
    """Persist the per-symbol replay state as the year-end snapshot for ``year``
    (replacing any existing rows for that year)."""
    conn.execute(
        "DELETE FROM crypto_holdings_checkpoints WHERE account_id=? AND year=?",
        (account_id, year),
    )
    for sym, pos in positions.items():
        lots = json.dumps([[lot.date, _qty_text(lot.qty), lot.cost, lot.txn_id]
                           for lot in pos.lots])
        conn.execute(
            "INSERT INTO crypto_holdings_checkpoints"
            "(account_id, year, symbol, quantity, cost_basis, income, realized,"
            " ever_held, lots) VALUES (?,?,?,?,?,?,?,?,?)",
            (account_id, year, sym, _qty_text(pos.qty), pos.cost,
             pos.income, pos.realized, int(pos.ever_held), lots),
        )


def _replay_snapshots_from(conn, account_id: int, years: list[int], seed: dict) -> None:
    """Replay ``years`` forward starting from ``seed`` (a per-symbol
    :class:`_Position` map), writing the running state as each year's snapshot."""
    with decimal.localcontext(quantity_context()):
        positions = seed
        method = get_lot_method(conn, account_id)
        for year in years:
            lower = f"{year - 1}-12-31"
            for t in _list_txns_in_range(conn, account_id, lower, f"{year}-12-31"):
                _apply_txn(positions, t, method)
            _write_snapshot(conn, account_id, year, positions)


def rebuild_holdings_checkpoints(conn, account_id: int) -> None:
    """Recompute every year-end holdings snapshot for the account from inception.
    A snapshot for year Y is the full replay state through Dec 31 of Y."""
    conn.execute("DELETE FROM crypto_holdings_checkpoints WHERE account_id=?",
                 (account_id,))
    _replay_snapshots_from(conn, account_id, _txn_years(conn, account_id), {})
    conn.commit()


def recompute_holdings_checkpoints_from_year(conn, account_id: int, from_year: int) -> None:
    """Cascade a change dated in ``from_year``: drop snapshots for that year and
    all later, reseed from the ``from_year - 1`` snapshot, replay each affected
    year forward. Earlier snapshots are untouched (the analogue of
    ledger._touch_checkpoints)."""
    conn.execute(
        "DELETE FROM crypto_holdings_checkpoints WHERE account_id=? AND year>=?",
        (account_id, from_year),
    )
    seed, _ = _load_holdings_checkpoint(conn, account_id, from_year)
    years = [y for y in _txn_years(conn, account_id) if y >= from_year]
    _replay_snapshots_from(conn, account_id, years, seed)
    conn.commit()


def _invalidate_holdings_checkpoints_from(conn, account_id: int, date: str) -> None:
    """Drop snapshots for the year of ``date`` and later after a raw write, so
    reads fall back to a correct from-inception replay until the next
    :func:`rebuild_holdings` rebuilds them."""
    if not date:
        return
    conn.execute(
        "DELETE FROM crypto_holdings_checkpoints WHERE account_id=? AND year>=?",
        (account_id, int(date[:4])),
    )


def rebuild_holdings(conn, account_id: int) -> list[dict]:
    """Recompute the account's crypto_holdings from its transactions and replace
    the rows (dropping any fully-closed position). Refreshes the year-end
    snapshots first (from-inception) so the replay below reads fresh checkpoints.
    Returns the resulting holdings as dicts."""
    rebuild_holdings_checkpoints(conn, account_id)
    positions = _replay_positions(conn, account_id)
    conn.execute("DELETE FROM crypto_holdings WHERE account_id=?", (account_id,))
    out: list[dict] = []
    for sym, pos in positions.items():
        if pos.qty == 0:
            continue
        conn.execute(
            "INSERT INTO crypto_holdings(account_id, symbol, quantity, cost_basis) "
            "VALUES (?,?,?,?)",
            (account_id, sym, _qty_text(pos.qty), pos.cost),
        )
        out.append({"symbol": sym, "quantity": _qty_text(pos.qty),
                    "cost_basis": pos.cost})
    conn.commit()
    return out


def list_holdings(conn, account_id: int) -> list:
    return conn.execute(
        "SELECT * FROM crypto_holdings WHERE account_id=? ORDER BY symbol",
        (account_id,)).fetchall()


def get_holding(conn, account_id: int, symbol: str):
    return conn.execute(
        "SELECT * FROM crypto_holdings WHERE account_id=? AND symbol=?",
        (account_id, symbol)).fetchone()


def realized_gains(conn, account_id: int, as_of: Optional[str] = None) -> list:
    """Every :class:`RealizedGain` this account's disposals booked on/before
    ``as_of``. Uses the from-inception replay (``use_snapshots=False``) because
    the per-lot gain detail is not stored in the snapshot rows."""
    out: list = []
    for pos in _replay_positions(conn, account_id, as_of,
                                 use_snapshots=False).values():
        out.extend(pos.gains)
    out.sort(key=lambda g: (g.sold or "", g.symbol))
    return out


# ---------------------------------------------------------------------------
# Event writers (SINGLE WRITER of crypto_transactions / crypto_holdings)
# ---------------------------------------------------------------------------
_EDITABLE = {
    "date", "action", "symbol", "quantity", "price", "amount", "basis",
    "fee_symbol", "fee_quantity", "fee_amount", "memo", "payee", "tx_hash", "fitid",
}


def record_event(conn, account_id: int, date: str, action: str, *,
                 symbol=None, quantity=None, price=None, amount=None, basis=None,
                 fee_symbol=None, fee_quantity=None, fee_amount=None,
                 transfer_account_id=None, transfer_pair_id=None, swap_group_id=None,
                 tx_hash=None, memo=None, payee=None, fitid=None, import_id=None,
                 commit: bool = True) -> int:
    """The low-level insert -- one row per single-asset delta. Quantities/prices
    are encoded to exponent-free Decimal TEXT; cents are stored as-is (signed).
    Invalidates checkpoints from ``date`` forward; does NOT rebuild holdings (the
    caller does, after a batch of edits). Use the ``record_buy`` / ``record_sell``
    / ``record_swap`` / ``record_wallet_transfer`` / ``record_income`` wrappers
    for the common events; this is the escape hatch."""
    _validate_date(date)
    act = _norm(action)
    if act not in ACTIONS:
        raise ValueError(f"unknown crypto action {action!r}; one of {sorted(ACTIONS)}")
    cur = conn.execute(
        "INSERT INTO crypto_transactions"
        "(account_id, date, action, symbol, quantity, price, amount, basis,"
        " fee_symbol, fee_quantity, fee_amount, transfer_account_id,"
        " transfer_pair_id, swap_group_id, tx_hash, memo, payee, import_id, fitid)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            account_id, date, act, symbol or None,
            _qty_text(_D(quantity)) if quantity not in (None, "") else None,
            _qty_text(_D(price)) if price not in (None, "") else None,
            int(amount) if amount is not None else None,
            int(basis) if basis is not None else None,
            fee_symbol or None,
            _qty_text(_D(fee_quantity)) if fee_quantity not in (None, "") else None,
            int(fee_amount) if fee_amount is not None else None,
            transfer_account_id, transfer_pair_id, swap_group_id,
            tx_hash or None, memo or None, payee or None, import_id, fitid or None,
        ),
    )
    _invalidate_holdings_checkpoints_from(conn, account_id, date)
    if commit:
        conn.commit()
    return cur.lastrowid


def record_buy(conn, account_id: int, date: str, symbol: str, quantity, cost, *,
               price=None, fee_symbol=None, fee_quantity=None, fee_amount=None,
               memo=None, tx_hash=None, fitid=None, import_id=None) -> int:
    """Buy ``quantity`` of ``symbol`` for ``cost`` cents (fiat out). Establishes a
    lot; the fiat debits the account's internal cash sleeve (``amount<0``)."""
    q = abs(_D(quantity))
    cost_cents = abs(int(cost))
    if price is None and q > 0:
        with decimal.localcontext(quantity_context()):
            price = (Decimal(cost_cents) / _HUNDRED) / q
    return record_event(conn, account_id, date, "BUY", symbol=symbol, quantity=q,
                        price=price, amount=-cost_cents, fee_symbol=fee_symbol,
                        fee_quantity=fee_quantity, fee_amount=fee_amount,
                        memo=memo, tx_hash=tx_hash, fitid=fitid, import_id=import_id)


def record_sell(conn, account_id: int, date: str, symbol: str, quantity, proceeds, *,
                price=None, fee_symbol=None, fee_quantity=None, fee_amount=None,
                memo=None, tx_hash=None, fitid=None, import_id=None) -> int:
    """Sell ``quantity`` of ``symbol`` for ``proceeds`` cents (fiat in). Books
    realized gain from the lots per the account's method."""
    q = abs(_D(quantity))
    proceeds_cents = abs(int(proceeds))
    if price is None and q > 0:
        with decimal.localcontext(quantity_context()):
            price = (Decimal(proceeds_cents) / _HUNDRED) / q
    return record_event(conn, account_id, date, "SELL", symbol=symbol, quantity=-q,
                        price=price, amount=proceeds_cents, fee_symbol=fee_symbol,
                        fee_quantity=fee_quantity, fee_amount=fee_amount,
                        memo=memo, tx_hash=tx_hash, fitid=fitid, import_id=import_id)


def record_send(conn, account_id: int, date: str, symbol: str, quantity, fmv, *,
                payee=None, fee_symbol=None, fee_quantity=None, fee_amount=None,
                memo=None, tx_hash=None, fitid=None, import_id=None) -> int:
    """Send ``quantity`` of ``symbol`` to a third party -- a disposal at ``fmv``
    cents fair-market value (books realized gain vs the relieved basis). A network
    fee (gas) rides the ``fee_*`` fields. ``payee`` is the on-chain ``To``
    recipient; the counterparty IS the payee on an exchange row exactly as it is
    on a wallet row -- when the user moves coin from their own paper wallet to an
    exchange, the exchange side must show that wallet's address. If the recipient
    is really an intermediary the user controls, prefer
    :func:`record_wallet_transfer` with an account-per-intermediary instead
    (CLAUDE.md), so no gain is realized."""
    q = abs(_D(quantity))
    fmv_cents = abs(int(fmv))
    price = None
    if q > 0:
        with decimal.localcontext(quantity_context()):
            price = (Decimal(fmv_cents) / _HUNDRED) / q
    return record_event(conn, account_id, date, "SEND", symbol=symbol, quantity=-q,
                        price=price, fee_symbol=fee_symbol, fee_quantity=fee_quantity,
                        fee_amount=fee_amount, memo=memo, payee=payee,
                        tx_hash=tx_hash, fitid=fitid, import_id=import_id)


def record_income(conn, account_id: int, date: str, action: str, symbol: str,
                  quantity, fmv, *, payee=None, memo=None, tx_hash=None,
                  fitid=None, import_id=None) -> int:
    """Credit ``quantity`` of ``symbol`` as in-kind income (RECEIVE / REWARD /
    INTEREST / AIRDROP / MINING / FORK) at ``fmv`` cents fair-market value. Basis
    = FMV; the income actions accrue FMV to the checkpoint ``income`` total (FORK
    basis is per policy -- pass ``fmv=0`` for the $0-basis treatment). ``payee`` is
    the on-chain ``From`` sender -- the counterparty IS the payee here too."""
    act = _norm(action)
    if act not in (_INCOME_ACTIONS | {"FORK"}):
        raise ValueError(f"{action!r} is not an income/fork action")
    q = abs(_D(quantity))
    fmv_cents = abs(int(fmv))
    price = None
    if q > 0:
        with decimal.localcontext(quantity_context()):
            price = (Decimal(fmv_cents) / _HUNDRED) / q
    return record_event(conn, account_id, date, act, symbol=symbol, quantity=q,
                        price=price, basis=fmv_cents, memo=memo, payee=payee,
                        tx_hash=tx_hash, fitid=fitid, import_id=import_id)


# ---------------------------------------------------------------------------
# Coin-native WALLET writers (SRD §5.8; the ShrsIn/ShrsOut analogue). A wallet
# moves coin with NO fiat leg -- a quantity arrives or leaves, valued at market
# like a security -- so ``price``/``amount``/``basis`` stay NULL and the counterparty
# address IS the payee. These route through ``record_event``, so :mod:`mammon.crypto`
# stays the SOLE writer of ``crypto_*``. Own-wallet moves keep using
# :func:`record_wallet_transfer` (the coin mirror model), and coin-for-coin trades
# :func:`record_swap`; those legs MUST be written as a pair, so they are excluded
# from the single-leg credit/debit action sets below.
# ---------------------------------------------------------------------------
_WALLET_CREDIT_ACTIONS = {"RECEIVE", "REWARD", "INTEREST", "AIRDROP", "MINING", "FORK"}
_WALLET_DEBIT_ACTIONS = {"SEND"}
# Public aliases: the import-review accept path has to decide which writer a
# reviewed wallet row belongs to, and that decision must read the SAME sets the
# writers validate against rather than re-listing the vocabulary somewhere else.
WALLET_CREDIT_ACTIONS = _WALLET_CREDIT_ACTIONS
WALLET_DEBIT_ACTIONS = _WALLET_DEBIT_ACTIONS
# The full coin-direction sets, for surfaces that must render or route an
# EXCHANGE row too (SELL, SWAP_OUT, TRANSFER_OUT...). Reading these rather than
# re-listing the vocabulary is what keeps a register column, a review column and
# the accept path from drifting apart.
ADD_ACTIONS = _ADD_ACTIONS
REMOVE_ACTIONS = _REMOVE_ACTIONS


def record_wallet_credit(conn, account_id: int, date: str, symbol: str, quantity, *,
                         payee=None, action: str = "RECEIVE", price=None, basis=None,
                         memo=None, tx_hash=None, fitid=None, import_id=None) -> int:
    """A coin-native INCREASE (the ShrsIn analogue): ``quantity`` of ``symbol``
    arrives with NO fiat leg. ``payee`` is the on-chain ``From`` counterparty and
    populates the register's Payee directly (there is no separate "counterparty"
    field). ``price``/``basis`` stay NULL for a bare receive; supply an FMV ``basis``
    only when a cost is known (an airdrop/reward's fair-market value, which the
    income actions accrue). ``action`` defaults to ``RECEIVE`` and must be a
    single-leg add action (RECEIVE / REWARD / INTEREST / AIRDROP / MINING / FORK) --
    own-wallet TRANSFER_IN goes through :func:`record_wallet_transfer` and SWAP_IN
    through :func:`record_swap`. Each ``symbol`` is a distinct security position:
    'USDC', 'ETH', 'LINK' never merge."""
    act = _norm(action)
    if act not in _WALLET_CREDIT_ACTIONS:
        raise ValueError(f"{action!r} is not a coin-in action; "
                         f"one of {sorted(_WALLET_CREDIT_ACTIONS)}")
    q = abs(_D(quantity))
    return record_event(conn, account_id, date, act, symbol=symbol, quantity=q,
                        price=price, basis=basis, memo=memo, payee=payee,
                        tx_hash=tx_hash, fitid=fitid, import_id=import_id)


def record_wallet_debit(conn, account_id: int, date: str, symbol: str, quantity, *,
                        payee=None, action: str = "SEND", price=None,
                        fee_symbol=None, fee_quantity=None,
                        memo=None, tx_hash=None, fitid=None, import_id=None) -> int:
    """A coin-native DECREASE (the ShrsOut analogue): ``quantity`` of ``symbol``
    leaves with NO fiat proceeds. ``payee`` is the on-chain ``To`` recipient. An
    optional network fee is a COIN-NATIVE leg on this SAME row -- ``fee_symbol`` /
    ``fee_quantity`` (e.g. ETH gas), never a USD amount -- and only the sender books
    it. ``action`` defaults to ``SEND`` (own-wallet TRANSFER_OUT goes through
    :func:`record_wallet_transfer`). With no USD proceeds/basis, no realized gain is
    booked, which is correct for a coin-native wallet."""
    act = _norm(action)
    if act not in _WALLET_DEBIT_ACTIONS:
        raise ValueError(f"{action!r} is not a coin-out action; "
                         f"one of {sorted(_WALLET_DEBIT_ACTIONS)}")
    q = abs(_D(quantity))
    return record_event(conn, account_id, date, act, symbol=symbol, quantity=-q,
                        price=price, fee_symbol=fee_symbol, fee_quantity=fee_quantity,
                        memo=memo, payee=payee, tx_hash=tx_hash, fitid=fitid,
                        import_id=import_id)


def record_cash(conn, account_id: int, date: str, amount_cents: int, *,
                action: Optional[str] = None, payee=None, memo=None,
                tx_hash=None, fitid=None, import_id=None) -> int:
    """Move FIAT in or out of an exchange account's internal cash sleeve, with no
    coin leg at all -- a bank deposit, a withdrawal, or dollars arriving from
    another venue.

    ``amount_cents`` is SIGNED (negative = money out) and its sign chooses the
    action, so a caller cannot label a withdrawal a deposit; pass ``action``
    only to name one explicitly. ``symbol``/``quantity``/``price`` stay NULL,
    which is what keeps this row out of the holdings replay (``_apply_txn`` skips
    a symbol-less row) while ``crypto_cash`` still counts it.

    A WALLET must never carry one of these -- a paper-wallet address holds no
    fiat -- but that is the caller's concern; this is the sleeve's writer."""
    cents = int(amount_cents)
    act = _norm(action) if action else ("DEPOSIT" if cents >= 0 else "WITHDRAW")
    if act not in _CASH_ACTIONS:
        raise ValueError(f"{action!r} is not a cash-sleeve action; "
                         f"one of {sorted(_CASH_ACTIONS)}")
    return record_event(conn, account_id, date, act, amount=cents, payee=payee,
                        memo=memo, tx_hash=tx_hash, fitid=fitid,
                        import_id=import_id)


def record_fee(conn, account_id: int, date: str, symbol: str, quantity, *,
               usd_value=None, memo=None, tx_hash=None, fitid=None,
               import_id=None) -> int:
    """A STANDALONE network/gas fee row (use this only when the fee is not carried
    on a parent action's ``fee_*`` fields -- e.g. a periodic fee sweep). Removes
    ``quantity`` of ``symbol`` as a plain expense; ``usd_value`` (cents) is stored
    on ``fee_amount`` for reference and does not move the fiat cash sleeve."""
    q = abs(_D(quantity))
    return record_event(conn, account_id, date, "FEE", symbol=symbol, quantity=-q,
                        fee_amount=usd_value, memo=memo, tx_hash=tx_hash,
                        fitid=fitid, import_id=import_id)


def record_swap(conn, account_id: int, date: str, symbol_out: str, quantity_out,
                symbol_in: str, quantity_in, fmv, *, fee_symbol=None,
                fee_quantity=None, fee_amount=None, memo=None, tx_hash=None,
                fitid=None, import_id=None) -> tuple[int, int]:
    """A coin-for-coin swap: dispose ``quantity_out`` of ``symbol_out`` and
    acquire ``quantity_in`` of ``symbol_in``, both valued at ``fmv`` cents (the
    agreed USD value of the trade). Written as TWO linked single-asset legs
    (SWAP_OUT + SWAP_IN) sharing a ``swap_group_id`` -- never one row with two
    symbols, which would break holdings replay. The OUT leg is a disposal at FMV
    (books realized gain vs its relieved basis); the IN leg's basis IS that FMV.
    Any network fee rides the OUT leg. Returns ``(out_id, in_id)``."""
    _validate_date(date)
    if not symbol_out or not symbol_in:
        raise ValueError("a swap needs both an out and an in symbol")
    qo = abs(_D(quantity_out))
    qi = abs(_D(quantity_in))
    fmv_cents = abs(int(fmv))
    with decimal.localcontext(quantity_context()):
        price_out = (Decimal(fmv_cents) / _HUNDRED) / qo if qo > 0 else None
        price_in = (Decimal(fmv_cents) / _HUNDRED) / qi if qi > 0 else None
    out_id = record_event(conn, account_id, date, "SWAP_OUT", symbol=symbol_out,
                          quantity=-qo, price=price_out, fee_symbol=fee_symbol,
                          fee_quantity=fee_quantity, fee_amount=fee_amount,
                          memo=memo, tx_hash=tx_hash, fitid=fitid,
                          import_id=import_id, commit=False)
    in_id = record_event(conn, account_id, date, "SWAP_IN", symbol=symbol_in,
                         quantity=qi, price=price_in, basis=fmv_cents, memo=memo,
                         tx_hash=tx_hash, import_id=import_id, commit=False)
    conn.execute(
        "UPDATE crypto_transactions SET swap_group_id=? WHERE id IN (?,?)",
        (out_id, out_id, in_id),
    )
    conn.commit()
    return out_id, in_id


def _basis_to_move(conn, account_id: int, date: str, symbol: str,
                   q: Decimal, method: str) -> int:
    """The cost basis (cents) that ``q`` coin of ``symbol`` carries out of the
    source wallet as of ``date`` -- computed by relieving ``q`` from a THROWAWAY
    replay of the source, so it equals exactly what the TRANSFER_OUT leg will
    relieve on replay. Stamped on both transfer legs so basis is conserved."""
    with decimal.localcontext(quantity_context()):
        positions = _replay_positions(conn, account_id, as_of=date)
        pos = positions.get(symbol)
        if pos is None or pos.qty <= 0:
            return 0
        cost_before = pos.cost
        _relieve(pos, min(q, pos.qty), method)   # mutates the throwaway copy
        return cost_before - pos.cost


def record_wallet_transfer(conn, from_account_id: int, to_account_id: int,
                           date: str, symbol: str, quantity, *, basis=None,
                           fee_symbol=None, fee_quantity=None, fee_amount=None,
                           memo=None, tx_hash=None, fitid=None,
                           import_id=None) -> tuple[int, int]:
    """Move ``quantity`` of ``symbol`` between the user's OWN wallets, using the
    EXISTING transfer mirror model re-expressed for coin quantity: two rows
    (TRANSFER_OUT in ``from``, TRANSFER_IN in ``to``) linked by
    ``transfer_pair_id``, each ``transfer_account_id`` pointing at the other
    wallet. No fiat, no realized gain; the cost basis rides along (computed from
    the source's lots unless ``basis`` cents is supplied). A network fee (usually
    gas in a third coin) rides the OUT leg. Returns ``(out_id, in_id)``."""
    _validate_date(date)
    if from_account_id == to_account_id:
        raise ValueError("cannot transfer to the same wallet")
    for aid in (from_account_id, to_account_id):
        if get_account(conn, aid) is None:
            raise KeyError(f"no account {aid}")
    q = abs(_D(quantity))
    if basis is None:
        basis = _basis_to_move(conn, from_account_id, date, symbol, q,
                               get_lot_method(conn, from_account_id))
    basis = int(basis)
    out_id = record_event(conn, from_account_id, date, "TRANSFER_OUT",
                          symbol=symbol, quantity=-q,
                          transfer_account_id=to_account_id, basis=basis,
                          fee_symbol=fee_symbol, fee_quantity=fee_quantity,
                          fee_amount=fee_amount, memo=memo, tx_hash=tx_hash,
                          fitid=fitid, import_id=import_id, commit=False)
    in_id = record_event(conn, to_account_id, date, "TRANSFER_IN", symbol=symbol,
                         quantity=q, transfer_account_id=from_account_id,
                         basis=basis, memo=memo, tx_hash=tx_hash,
                         import_id=import_id, commit=False)
    conn.execute("UPDATE crypto_transactions SET transfer_pair_id=? WHERE id=?",
                 (in_id, out_id))
    conn.execute("UPDATE crypto_transactions SET transfer_pair_id=? WHERE id=?",
                 (out_id, in_id))
    conn.commit()
    return out_id, in_id


# How far apart two legs of the same movement may be dated and still be
# recognised as one transfer. A coin send and its arrival are the SAME on-chain
# event, but two sources date it differently: an exchange stamps when it credited
# the account, the chain stamps when the block confirmed, and a weekend or a
# congested network puts days between them. Wider than the cash window because
# nothing here is ambiguous -- the coin, the quantity and the two accounts must
# all agree before a date is even consulted.
TRANSFER_LINK_WINDOW_DAYS = 10


def link_as_transfer(conn, txn_id: int, other_account_id: int, *, basis=None):
    """Turn a one-sided coin movement into a linked transfer PAIR with
    ``other_account_id``. Returns ``(out_id, in_id)``.

    This is the operation behind naming one of your own accounts as the
    counterparty. Coin moving between two accounts the user controls is not a
    disposal: no gain is realized and the cost basis rides along. Left as a
    SEND on one side and a RECEIVE on the other, the same movement books a
    realized gain that never happened AND restarts the basis at market -- the
    coin analogue of the double-counted transfer CLAUDE.md warns about.

    It LINKS an existing counter-leg when one is there, and only creates the
    mirror when there is none. That distinction is the whole point: a
    wallet-to-exchange move is usually already recorded TWICE, once from each
    side's own export, so minting a third row would leave the wallet short. A
    candidate must match on coin, on magnitude and on direction, and fall within
    :data:`TRANSFER_LINK_WINDOW_DAYS`; anything less exact is not a match.
    """
    row = get_event(conn, txn_id)
    if row is None:
        raise KeyError(f"no crypto transaction {txn_id}")
    if row["transfer_pair_id"] is not None:
        raise ValueError("this row is already one leg of a transfer")
    if row["swap_group_id"] is not None:
        raise ValueError("a swap leg cannot be re-linked as a transfer")
    other_acct = ledger.get_account(conn, other_account_id)
    if other_acct is None:
        raise KeyError(f"no account {other_account_id}")
    if not is_crypto_account(other_acct):
        # The other account holds DOLLARS, not coin, so what crosses is the
        # row's fiat leg -- Quicken's SellX / BuyX. Handled apart from the coin
        # mirror below because a cash account's rows live in `transactions`, and
        # writing a coin row into one would be a second writer of the wrong table.
        return _link_cash_leg(conn, row, other_account_id)
    symbol = row["symbol"]
    qty = _D(row["quantity"])
    if not symbol or qty == 0:
        # A FIAT row between two crypto accounts: both have cash sleeves, and
        # dollars move between them as readily as coin does (an exchange venue
        # and its retail front, say). Mirrored the same way -- adopt the leg the
        # other account already holds, or mint it -- but in DEPOSIT/WITHDRAW,
        # because there is no coin here to add to or relieve from a position.
        return _link_cash_mirror(conn, row, other_account_id)
    if int(row["account_id"]) == int(other_account_id):
        raise ValueError("cannot transfer to the same account")
    if get_account(conn, other_account_id) is None:
        raise KeyError(f"no account {other_account_id}")

    outgoing = qty < 0
    from_id = row["account_id"] if outgoing else other_account_id
    to_id = other_account_id if outgoing else row["account_id"]
    if basis is None:
        basis = _basis_to_move(conn, from_id, row["date"], symbol, abs(qty),
                               get_lot_method(conn, from_id))
    basis = int(basis)

    other = _find_counter_leg(conn, other_account_id, symbol, qty, row["date"])
    if other is None:
        # No leg on the other side: mint the mirror, so the pair is complete.
        other_id = record_event(
            conn, other_account_id, row["date"],
            "TRANSFER_IN" if outgoing else "TRANSFER_OUT", symbol=symbol,
            quantity=(abs(qty) if outgoing else -abs(qty)),
            transfer_account_id=row["account_id"], basis=basis,
            memo=row["memo"], commit=False)
    else:
        # Both legs already exist -- adopt them rather than adding a third row.
        other_id = int(other["id"])
        _apply_update(conn, other_id, {
            "action": "TRANSFER_IN" if outgoing else "TRANSFER_OUT",
            "basis": basis,
            # A transfer books no proceeds: whatever fiat the source guessed at
            # for this leg was a valuation, not money that moved.
            "amount": None,
        })
    _apply_update(conn, txn_id, {
        "action": "TRANSFER_OUT" if outgoing else "TRANSFER_IN",
        "basis": basis,
        "amount": None,
    })
    conn.execute("UPDATE crypto_transactions SET transfer_account_id=?, "
                 "transfer_pair_id=? WHERE id=?",
                 (other_account_id, other_id, txn_id))
    conn.execute("UPDATE crypto_transactions SET transfer_account_id=?, "
                 "transfer_pair_id=? WHERE id=?",
                 (row["account_id"], txn_id, other_id))
    for aid, d in ((row["account_id"], row["date"]),
                   (other_account_id, row["date"])):
        _invalidate_holdings_checkpoints_from(conn, aid, d)
    conn.commit()
    for aid in (row["account_id"], other_account_id):
        rebuild_holdings(conn, aid)
    return (txn_id, other_id) if outgoing else (other_id, txn_id)


def _link_cash_mirror(conn, row, other_account_id: int):
    """Pair a cash-sleeve movement with its opposite in ANOTHER CRYPTO account.

    The fiat twin of the coin mirror above, and it exists for the same reason:
    money the user moved between two accounts they hold is one event recorded
    twice, once by each side's export. Left unpaired it reads as a withdrawal to
    nowhere and a deposit from nowhere.

    The actions stay DEPOSIT / WITHDRAW -- there is no position to open or
    relieve, so the coin transfer actions would be wrong here -- and the two
    amounts are equal and opposite, so the pair nets to zero across the books."""
    amount = int(row["amount"] or 0)
    if amount == 0:
        raise ValueError(
            "This row moves neither coin nor money, so there is nothing to "
            "transfer. Set the Action first, so the row states what moved.")
    txn_id = int(row["id"])
    other = _find_cash_counter_leg(conn, other_account_id, amount, row["date"])
    if other is None:
        other_id = record_cash(conn, other_account_id, row["date"], -amount,
                               memo=row["memo"] or None)
    else:
        other_id = int(other["id"])
    conn.execute("UPDATE crypto_transactions SET transfer_account_id=?, "
                 "transfer_pair_id=? WHERE id=?",
                 (other_account_id, other_id, txn_id))
    conn.execute("UPDATE crypto_transactions SET transfer_account_id=?, "
                 "transfer_pair_id=? WHERE id=?",
                 (row["account_id"], txn_id, other_id))
    for aid in (row["account_id"], other_account_id):
        _invalidate_holdings_checkpoints_from(conn, aid, row["date"])
    conn.commit()
    return (txn_id, other_id) if amount < 0 else (other_id, txn_id)


def _find_cash_counter_leg(conn, account_id: int, amount: int, date: str):
    """The unpaired cash row in ``account_id`` that is the other half of this
    money movement: no coin, the exact opposite amount, within
    :data:`TRANSFER_LINK_WINDOW_DAYS`. Nearest date wins."""
    import datetime as _date
    try:
        anchor = _date.date.fromisoformat(date)
    except (TypeError, ValueError):
        return None
    best = best_gap = None
    for r in conn.execute(
            "SELECT * FROM crypto_transactions WHERE account_id=? "
            "AND symbol IS NULL AND amount=? AND transfer_pair_id IS NULL "
            "AND transfer_account_id IS NULL", (account_id, -int(amount))).fetchall():
        try:
            gap = abs((_date.date.fromisoformat(r["date"]) - anchor).days)
        except (TypeError, ValueError):
            continue
        if gap > TRANSFER_LINK_WINDOW_DAYS:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = r, gap
    return best


def _link_cash_leg(conn, row, cash_account_id: int):
    """Send a crypto row's FIAT leg to a cash account -- Quicken's SellX / BuyX.

    A sale whose proceeds are wired to a bank never parks the money in the
    exchange's cash sleeve, and a purchase funded from a bank never takes money
    out of it. So the crypto row keeps its ``amount`` (that IS what the disposal
    realized, and the realized gain is computed from it) and gains a
    ``transfer_account_id``; :func:`crypto_cash` then excludes it from the sleeve,
    and the matching ordinary transaction in the cash account is where the money
    actually lands. Counting it in both places would double the cash.

    The cash leg is written through :mod:`mammon.ledger` -- the sole writer of
    ``transactions`` -- so neither table grows a second writer."""
    amount = int(row["amount"] or 0)
    if amount == 0:
        raise ValueError(
            "This row moves no money, so there is nothing to transfer to a cash "
            "account. Set the Action to a trade (or a deposit/withdrawal) first, "
            "so the row states an amount.")
    txn_id = int(row["id"])
    crypto_acct = ledger.get_account(conn, row["account_id"])
    name = (crypto_acct["name"] if crypto_acct else "") or "crypto"
    # A REAL double-entry transfer, through ledger.create_transfer -- the one
    # path that writes `transfer_account_id` and the mirror invariant with it.
    # Routing around it (a bare INSERT setting that column) would make a second
    # writer of the transfer relationship, which is the main way to break this
    # codebase. The crypto row's `amount` stays in the sleeve and this leg takes
    # it straight back out, so the exchange nets to zero and the money lands in
    # the bank -- the same arithmetic Quicken's SellX does, expressed as the two
    # rows it really is.
    if amount > 0:                       # proceeds leaving the exchange
        from_id, to_id = int(row["account_id"]), cash_account_id
    else:                                # a purchase funded from the bank
        from_id, to_id = cash_account_id, int(row["account_id"])
    legs = ledger.create_transfer(
        conn, from_id, to_id, row["date"], abs(amount),
        memo=row["memo"] or None, payee=name)
    # A trade whose cash went elsewhere is Quicken's X form. Renaming the action
    # is not cosmetic: it is how the register SAYS the proceeds left, so a row
    # reading SELL with a Transfer beside it cannot be mistaken for a sale whose
    # money is still sitting in the account.
    act = _norm(row["action"])
    updates = {"transfer_account_id": cash_account_id}
    if act in ("SELL", "BUY"):
        updates["action"] = act + "X"
    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE crypto_transactions SET {sets} WHERE id=?",
                 (*updates.values(), txn_id))
    _invalidate_holdings_checkpoints_from(conn, row["account_id"], row["date"])
    conn.commit()
    rebuild_holdings(conn, row["account_id"])
    return legs


def unlink_transfer(conn, txn_id: int) -> bool:
    """Break a transfer pair back into two independent one-sided rows, keeping
    both. The inverse of :func:`link_as_transfer`; returns whether anything was
    unlinked. Deliberately NOT a delete -- the two movements really happened, and
    only the claim that they are the same movement is being withdrawn."""
    row = get_event(conn, txn_id)
    if row is None:
        return False
    if row["transfer_pair_id"] is None:
        # A cash leg is linked by transfer_account_id alone (its other half lives
        # in `transactions`, so there is no crypto id to pair with). Unlinking
        # returns the money to the sleeve and removes the cash row -- that row was
        # created BY the link and represents nothing without it.
        if row["transfer_account_id"] is None:
            return False
        _unlink_cash_leg(conn, row)
        return True
    pair_id = int(row["transfer_pair_id"])
    for tid in (txn_id, pair_id):
        r = get_event(conn, tid)
        if r is None:
            continue
        act = _norm(r["action"])
        if act in ("TRANSFER_OUT", "TRANSFER_IN"):
            # Only a COIN transfer becomes a plain send/receive. A cash leg keeps
            # its DEPOSIT/WITHDRAW -- money still moved in or out of the sleeve;
            # all that is withdrawn is the claim about where it went.
            _apply_update(conn, tid, {
                "action": "SEND" if act == "TRANSFER_OUT" else "RECEIVE",
            })
        conn.execute("UPDATE crypto_transactions SET transfer_account_id=NULL, "
                     "transfer_pair_id=NULL WHERE id=?", (tid,))
        _invalidate_holdings_checkpoints_from(conn, r["account_id"], r["date"])
    conn.commit()
    for tid in (txn_id, pair_id):
        r = get_event(conn, tid)
        if r is not None:
            rebuild_holdings(conn, r["account_id"])
    return True


def _unlink_cash_leg(conn, row) -> None:
    """Undo :func:`_link_cash_leg`: drop the ordinary transaction the link
    created and return the amount to the exchange's cash sleeve."""
    txn_id = int(row["id"])
    # Found by the shape the link itself created (same account, date, amount and
    # a transfer pointing back here), then deleted through `ledger`, the sole
    # writer of `transactions`. A plain SELECT to locate it is a READ, which the
    # single-writer rule does not restrict.
    hit = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date=? AND amount=? "
        "AND transfer_account_id=? ORDER BY id LIMIT 1",
        (int(row["transfer_account_id"]), row["date"], int(row["amount"] or 0),
         int(row["account_id"]))).fetchone()
    if hit is not None:
        # Deleting either leg of a transfer removes both (the mirror model), so
        # this one call clears the crypto account's offsetting row too.
        ledger.delete_transaction(conn, int(hit[0]))
    # ...and back to the plain trade when the link is withdrawn.
    act = _norm(row["action"])
    plain = act[:-1] if act in CROSS_ACTIONS else act
    conn.execute("UPDATE crypto_transactions SET transfer_account_id=NULL, "
                 "action=? WHERE id=?", (plain, txn_id))
    _invalidate_holdings_checkpoints_from(conn, row["account_id"], row["date"])
    conn.commit()
    rebuild_holdings(conn, row["account_id"])


def find_transfer_candidate(conn, account_id: int, date: str, *, symbol=None,
                            quantity=None, amount=None):
    """The OTHER crypto account already holding the counter-leg of this movement,
    as ``(account_id, name)``, or ``None``.

    Money and coin moved between two accounts the user holds are recorded TWICE,
    once by each venue's own export. Nothing pairs them automatically -- a review
    row is new to the account it lands in, whatever another account already
    knows -- so 43 real transfers arrived as 86 unexplained halves. This is what
    lets the review offer the answer instead of leaving the user to find it.

    Deliberately exact: same coin and magnitude (or the exact opposite amount for
    cash), within :data:`TRANSFER_LINK_WINDOW_DAYS`, and only when exactly ONE
    account has such a leg. A near-miss here silently welds two unrelated
    movements together, which is worse than offering nothing."""
    hit = find_transfer_leg(conn, account_id, date, symbol=symbol,
                            quantity=quantity, amount=amount)
    if hit is None:
        return None
    acct = ledger.get_account(conn, int(hit["account_id"]))
    return (int(hit["account_id"]), (acct["name"] if acct else "") or "")


def find_transfer_leg(conn, account_id: int, date: str, *, symbol=None,
                      quantity=None, amount=None):
    """The counter-leg ROW itself (see :func:`find_transfer_candidate`), or
    ``None`` when no other account has one -- or when more than one does."""
    hits = []
    for acct in list_accounts(conn, include_closed=True, include_hidden=True):
        other = int(acct["id"])
        if other == int(account_id):
            continue
        if symbol and quantity is not None:
            leg = _find_counter_leg(conn, other, symbol, quantity, date)
        elif amount:
            leg = _find_cash_counter_leg(conn, other, int(amount), date)
        else:
            leg = None
        if leg is not None:
            hits.append(leg)
    return hits[0] if len(hits) == 1 else None


def _find_counter_leg(conn, account_id: int, symbol: str, quantity, date: str):
    """The unpaired row in ``account_id`` that is the OTHER half of this coin
    movement, or ``None``. Same coin, opposite direction, equal magnitude, within
    :data:`TRANSFER_LINK_WINDOW_DAYS`; the nearest date wins. Exactness is the
    point -- a near-miss here silently welds two unrelated movements together."""
    import datetime as _date
    want_positive = _D(quantity) < 0        # an OUT leg needs an IN on the other side
    try:
        anchor = _date.date.fromisoformat(date)
    except (TypeError, ValueError):
        return None
    best = None
    best_gap = None
    for r in conn.execute(
            "SELECT * FROM crypto_transactions WHERE account_id=? AND symbol=? "
            "AND transfer_pair_id IS NULL AND swap_group_id IS NULL",
            (account_id, symbol)).fetchall():
        q = _D(r["quantity"])
        if (q > 0) != want_positive or abs(q) != abs(_D(quantity)):
            continue
        try:
            gap = abs((_date.date.fromisoformat(r["date"]) - anchor).days)
        except (TypeError, ValueError):
            continue
        if gap > TRANSFER_LINK_WINDOW_DAYS:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = r, gap
    return best


def get_event(conn, txn_id: int):
    return conn.execute("SELECT * FROM crypto_transactions WHERE id=?",
                        (txn_id,)).fetchone()


def list_events(conn, account_id: int) -> list:
    return conn.execute(
        "SELECT * FROM crypto_transactions WHERE account_id=? ORDER BY date, id",
        (account_id,)).fetchall()


def symbols_used(conn, account_id: int) -> list:
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM crypto_transactions "
        "WHERE account_id=? AND symbol IS NOT NULL AND symbol<>'' ORDER BY symbol",
        (account_id,)).fetchall()
    return [r["symbol"] for r in rows]


def register_rows(conn, account_id: int) -> list[dict]:
    """The account's crypto events in application order (date, id), each augmented
    with the running-balance / label fields the register shows -- so the register
    UI stays a THIN projection and all quantity/cents math is tested HERE, not in
    :mod:`mammon.ui` (the crypto twin of :func:`investments.register_rows`). Each
    returned row is a plain dict: the stored ``crypto_transactions`` columns plus
    five derived keys.

      * ``coin_bal`` -- running quantity of THAT ROW's symbol as clean Decimal
        text, after this row's main leg (:data:`_ADD_ACTIONS` -> +qty,
        :data:`_REMOVE_ACTIONS` -> -qty, applied to ``abs(quantity)`` so a signed
        OUT row cannot flip an add into a remove -- the same rule
        :func:`_apply_txn` uses) AND its same-coin gas ``fee_*`` leg. ``None`` on
        rows that move no coin. The last ``coin_bal`` per symbol ties to
        :func:`rebuild_holdings` (both fold the main delta then the fee).
      * ``cash_amt`` -- the row's effect on the internal fiat cash sleeve: the
        signed ``amount`` on a BUY/SELL (the only events that move fiat), else 0.
      * ``cash_bal`` -- running cash sleeve AFTER the row, opening at the account's
        ``opening_balance`` and accumulating ``cash_amt``.
      * ``label`` -- the Coin/Wallet column text: ``[Other Wallet]`` for either
        leg of a wallet transfer (the mirror model, rendered exactly like a cash
        transfer's ``[Other]`` and consuming no category), ``OUT->IN`` for either
        leg of a swap (so the two ``swap_group_id`` legs read as one paired
        trade), else the bare symbol.
      * ``fee_label`` -- the gas that rode this event as ``"<qty> <SYM>"`` (e.g.
        ``"0.01 ETH"``), or ``""``.

    All quantity arithmetic runs under the wei-scale high-precision context.
    """
    acct = ledger.get_account(conn, account_id)
    opening = 0
    if acct is not None:
        try:
            opening = int(acct["opening_balance"] or 0)
        except (KeyError, IndexError):
            opening = 0
    events = list_events(conn, account_id)

    # swap_group_id -> {out, in} symbols, so BOTH legs render the same pair label.
    swap_pairs: dict = {}
    for t in events:
        gid = _row_value(t, "swap_group_id")
        if gid is None:
            continue
        entry = swap_pairs.setdefault(gid, {"out": None, "in": None})
        a = _norm(_row_value(t, "action"))
        if a == "SWAP_OUT":
            entry["out"] = _row_value(t, "symbol")
        elif a == "SWAP_IN":
            entry["in"] = _row_value(t, "symbol")

    name_cache: dict = {}

    def _other_wallet(taid):
        if taid is None:
            return ""
        if taid not in name_cache:
            oa = ledger.get_account(conn, taid)
            name_cache[taid] = (oa["name"] if oa else "") or ""
        return name_cache[taid]

    out: list[dict] = []
    with decimal.localcontext(quantity_context()):
        bals: dict[str, Decimal] = {}
        cash = opening
        for t in events:
            a = _norm(_row_value(t, "action"))
            sym = _row_value(t, "symbol")
            aq = abs(_D(_row_value(t, "quantity")))
            coin_bal = None
            if sym and a in _ADD_ACTIONS:
                bals[sym] = bals.get(sym, Decimal(0)) + aq
                coin_bal = _qty_text(bals[sym])
            elif sym and a in _REMOVE_ACTIONS:
                bals[sym] = bals.get(sym, Decimal(0)) - aq
                coin_bal = _qty_text(bals[sym])
            # Gas riding this action: a same-coin fee folds into this row's
            # coin_bal; any fee coin's running balance is reduced so a later row
            # of that coin reflects it (keeping the column tied to holdings).
            fsym = _row_value(t, "fee_symbol")
            fq = _D(_row_value(t, "fee_quantity"))
            fee_label = ""
            if fsym and fq > 0:
                bals[fsym] = bals.get(fsym, Decimal(0)) - fq
                fee_label = f"{_qty_text(fq)} {fsym}"
                if fsym == sym:
                    coin_bal = _qty_text(bals[fsym])
            # Fiat moves on a trade AND on a bare cash deposit/withdrawal; every
            # other action leaves `amount` NULL. Kept in step with `crypto_cash`,
            # which sums every non-NULL amount -- if these disagree the register's
            # running Cash Bal stops matching the account's own cash figure.
            camt = (int(_row_value(t, "amount") or 0)
                    if a in ("BUY", "SELL") or a in _CASH_ACTIONS else 0)
            cash += camt

            # The linked account is its OWN field, not a label smuggled into the
            # coin column. A register needs to say WHICH coin moved and WHERE it
            # went at the same time; folding them into one cell means a transfer
            # row cannot state its own symbol.
            transfer_name = _other_wallet(_row_value(t, "transfer_account_id"))
            if a in ("TRANSFER_OUT", "TRANSFER_IN"):
                label = f"[{transfer_name}]"
            elif a in ("SWAP_OUT", "SWAP_IN"):
                pair = swap_pairs.get(_row_value(t, "swap_group_id"), {})
                o, i = pair.get("out"), pair.get("in")
                label = f"{o or '?'}->{i or '?'}" if (o or i) else (sym or "")
            else:
                label = sym or ""

            row = dict(t)
            row["coin_bal"] = coin_bal
            row["cash_amt"] = camt
            row["cash_bal"] = cash
            row["label"] = label
            row["transfer_name"] = transfer_name
            row["fee_label"] = fee_label
            out.append(row)
    return out


def _apply_update(conn, txn_id: int, updates: dict) -> None:
    cols = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE crypto_transactions SET {cols} WHERE id=?",
                 (*updates.values(), txn_id))


def _encode_update(k, v):
    """Encode one editable field the way :func:`record_event` stores it."""
    if k in ("quantity", "price", "fee_quantity"):
        return _qty_text(_D(v)) if v not in (None, "") else None
    if k in ("amount", "basis", "fee_amount"):
        return int(v) if v is not None else None
    if k == "action":
        return _norm(v)
    return v


def update_event(conn, txn_id: int, **fields) -> None:
    """Edit an event. If it is one side of a wallet transfer, the linked leg is
    kept in sync -- ``date`` mirrors, ``quantity`` mirrors NEGATED, ``memo`` and
    ``basis`` mirror verbatim (the CLAUDE.md transfer invariant, in coin). If it
    is a swap leg, the sibling leg's ``date`` mirrors (a swap's two legs share a
    date; their quantities differ per coin and are NOT mirrored). Checkpoints are
    invalidated from the earliest year touched on every affected account."""
    row = get_event(conn, txn_id)
    if row is None:
        raise KeyError(f"no crypto transaction {txn_id}")
    old_date = row["date"]
    if "date" in fields:
        _validate_date(fields["date"])
    if "action" in fields and _norm(fields["action"]) not in ACTIONS:
        raise ValueError(f"unknown crypto action {fields['action']!r}")
    updates = {}
    for k, v in fields.items():
        if k not in _EDITABLE:
            raise ValueError(f"unknown crypto transaction field: {k}")
        updates[k] = _encode_update(k, v)
    if not updates:
        return
    _apply_update(conn, txn_id, updates)
    affected: list[tuple[int, str]] = [(row["account_id"], old_date)]
    if "date" in updates:
        affected.append((row["account_id"], updates["date"]))

    pair_id = row["transfer_pair_id"]
    if pair_id is not None:
        pair = get_event(conn, pair_id)
        if pair is not None:
            mirror = {}
            if "date" in updates:
                mirror["date"] = updates["date"]
            if "quantity" in updates:
                with decimal.localcontext(quantity_context()):
                    mirror["quantity"] = _qty_text(-_D(updates["quantity"]))
            if "memo" in fields:            # mirror an explicit clear too
                mirror["memo"] = updates["memo"]
            if "basis" in updates:
                mirror["basis"] = updates["basis"]
            if mirror:
                _apply_update(conn, pair_id, mirror)
                affected.append((pair["account_id"], pair["date"]))
                if "date" in mirror:
                    affected.append((pair["account_id"], mirror["date"]))

    group_id = row["swap_group_id"]
    if group_id is not None and "date" in updates:
        for r in conn.execute(
                "SELECT id, account_id, date FROM crypto_transactions "
                "WHERE swap_group_id=? AND id<>?", (group_id, txn_id)).fetchall():
            _apply_update(conn, r["id"], {"date": updates["date"]})
            affected.append((r["account_id"], r["date"]))
            affected.append((r["account_id"], updates["date"]))

    for aid, d in affected:
        _invalidate_holdings_checkpoints_from(conn, aid, d)
    conn.commit()


def delete_event(conn, txn_id: int) -> bool:
    """Delete an event. If it is one side of a wallet transfer, BOTH legs go; if
    it is a swap leg, ALL legs sharing its ``swap_group_id`` go (a swap or a
    transfer is one economic event -- half of it is never valid). Returns whether
    anything was deleted."""
    row = get_event(conn, txn_id)
    if row is None:
        return False
    ids = {txn_id}
    if row["transfer_pair_id"] is not None:
        ids.add(row["transfer_pair_id"])
    if row["swap_group_id"] is not None:
        for r in conn.execute(
                "SELECT id FROM crypto_transactions WHERE swap_group_id=?",
                (row["swap_group_id"],)).fetchall():
            ids.add(r["id"])
    marks = ",".join("?" * len(ids))
    affected = [(r["account_id"], r["date"]) for r in conn.execute(
        f"SELECT account_id, date FROM crypto_transactions WHERE id IN ({marks})",
        tuple(ids)).fetchall()]
    conn.execute(f"DELETE FROM crypto_transactions WHERE id IN ({marks})", tuple(ids))
    for aid, d in affected:
        _invalidate_holdings_checkpoints_from(conn, aid, d)
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Price history + valuation
# ---------------------------------------------------------------------------
# The shared money/holding value shapes: reuse the investments dataclasses so a
# crypto holding and an equity holding project identically for the UI.
HoldingValue = investments.HoldingValue
AccountValuation = investments.AccountValuation
Quote = investments.Quote
QuoteSourceUnavailable = investments.QuoteSourceUnavailable


def latest_price(conn, symbol: str, as_of: Optional[str] = None) -> Optional[Decimal]:
    """The most recent recorded USD close for a BARE coin ``symbol`` on/before
    ``as_of`` (looked up under the ``'{SYM}-USD'`` pair in ``price_history``)."""
    return investments.latest_price(conn, pair_symbol(symbol), as_of)


def price_history(conn, symbol: str, as_of: Optional[str] = None) -> list:
    """``(date, close)`` pairs for the coin's ``'{SYM}-USD'`` series, ascending."""
    return investments.price_history(conn, pair_symbol(symbol), as_of)


def _resolve_price(conn, symbol, as_of, prices) -> Optional[Decimal]:
    """The price to value ``symbol`` at ``as_of``: an explicit ``prices`` override
    (keyed by BARE symbol) wins, else the latest recorded pair close. ``None`` when
    neither is available -- the holding is reported unpriced, never guessed at."""
    if prices and symbol in prices:
        return _D(prices[symbol])
    return latest_price(conn, symbol, as_of)


def holding_values(conn, account_id: int, as_of: Optional[str] = None,
                   prices: Optional[dict] = None) -> list:
    """Value each crypto holding at its latest pair price (or an injected
    ``prices`` override, BARE symbol -> price). Unpriced holdings get
    market_value 0 and gain None. Reads the cached ``crypto_holdings`` table, so
    call :func:`rebuild_holdings` after writing events."""
    out: list = []
    for h in list_holdings(conn, account_id):
        sym = h["symbol"]
        qty = _D(h["quantity"])
        cost = h["cost_basis"] or 0
        price = _resolve_price(conn, sym, as_of, prices)
        if price is None:
            out.append(HoldingValue(sym, qty, cost, None, 0, None))
        else:
            with decimal.localcontext(quantity_context()):
                mv = _cents(qty * price * _HUNDRED)
            out.append(HoldingValue(sym, qty, cost, price, mv, mv - cost))
    return out


def crypto_cash(conn, account_id: int, as_of: Optional[str] = None) -> int:
    """Net fiat (cents) from the account's crypto transactions on/before ``as_of``
    -- the internal cash sleeve. Only BUY (``amount<0``) and SELL (``amount>0``)
    carry fiat; every other action's ``amount`` is NULL/0. Kept parallel to
    ``investments.investment_cash`` so both domains tell one cash story."""
    sql = ("SELECT COALESCE(SUM(amount), 0) FROM crypto_transactions "
           "WHERE account_id=? AND amount IS NOT NULL")
    params: list = [account_id]
    if as_of is not None:
        sql += " AND date<=?"
        params.append(as_of)
    return int(conn.execute(sql, tuple(params)).fetchone()[0] or 0)


def account_valuation(conn, account_id: int, as_of: Optional[str] = None,
                      prices: Optional[dict] = None) -> AccountValuation:
    """Total value of a crypto account: cash (the ordinary ledger balance -- an
    opening balance / any linked cash rows -- plus the crypto cash sleeve) plus
    the market value of its coin holdings. ``prices`` (bare symbol -> price)
    overrides recorded prices for what-if / testing."""
    hvs = holding_values(conn, account_id, as_of, prices)
    securities = sum(hv.market_value for hv in hvs)
    cash = (ledger.account_balance(conn, account_id, as_of)
            + crypto_cash(conn, account_id, as_of))
    unpriced = [hv.symbol for hv in hvs if hv.price is None]
    return AccountValuation(
        account_id=account_id, cash=cash, securities=securities,
        total=cash + securities, holdings=hvs, unpriced=unpriced,
    )


def valuation_as_of(conn) -> Optional[str]:
    """The date to value at when the caller names none: the most recent date the
    ledger knows ANYTHING -- a transaction (cash, investment or crypto) or a
    recorded price. A recorded price counts as activity, so a just-fetched crypto
    quote actually moves the displayed value (see investments.valuation_as_of)."""
    return investments.valuation_as_of(conn)


def display_balance(conn, account_id: int, as_of: Optional[str] = None,
                    prices: Optional[dict] = None) -> int:
    """The balance to SHOW for a crypto account (cents): the full market
    valuation (cash sleeve + coin value). For a non-crypto account this defers to
    :func:`investments.display_balance` so a single call values any account
    correctly."""
    acct = ledger.get_account(conn, account_id)
    if not is_crypto_account(acct):
        return investments.display_balance(conn, account_id, as_of, prices)
    eff = as_of if as_of is not None else valuation_as_of(conn)
    return account_valuation(conn, account_id, eff, prices).total


# ---------------------------------------------------------------------------
# Auto-quotes (network behind an injectable QuoteSource; tests pass a fake).
# ---------------------------------------------------------------------------
class CryptoQuoteSource:
    """A quote backend for crypto: it accepts BARE coin symbols, maps each to its
    yfinance ``'{SYM}-USD'`` pair, and returns :class:`Quote` objects keyed by the
    PAIR (so they store under the pair symbol in ``price_history``, avoiding a
    coin/stock namespace clash). The mapping is the only crypto-specific bit; the
    actual network fetch is delegated to a backend (default: the shared
    :class:`investments.YFinanceQuoteSource`, whose lazy ``import yfinance`` keeps
    the app from hard-depending on it). Tests inject their own ``source`` into
    :func:`fetch_quotes` and never touch this."""

    source_name = "yfinance"

    def __init__(self, backend=None):
        self._backend = backend

    def get_quotes(self, symbols):
        pairs = [pair_symbol(s) for s in symbols if (s or "").strip()]
        backend = self._backend or investments.YFinanceQuoteSource()
        return backend.get_quotes(pairs)

    def get_history(self, symbols, *, start=None, end=None, interval="1mo"):
        """Historical closes for each BARE coin symbol, keyed by the ``'{SYM}-USD'``
        pair -- the crypto twin of :meth:`investments.YFinanceQuoteSource.get_history`.
        Delegates to the backend's ``get_history``; a backend that only reports the
        latest close (no such method) raises :class:`QuoteSourceUnavailable`, so the
        caller can tell "no history" apart from "no rows"."""
        pairs = [pair_symbol(s) for s in symbols if (s or "").strip()]
        backend = self._backend or investments.YFinanceQuoteSource()
        getter = getattr(backend, "get_history", None)
        if getter is None:
            raise QuoteSourceUnavailable(
                "%s cannot fetch historical quotes; it only reports the latest "
                "close." % type(backend).__name__)
        return getter(pairs, start=start, end=end, interval=interval)


def default_quote_source():
    """The default crypto quote backend, or raise if none is installed. Only
    called when :func:`fetch_quotes` is given no explicit source."""
    return CryptoQuoteSource(investments.default_quote_source())


def fetch_quotes(conn, symbols, source=None) -> list:
    """Fetch the latest USD close for each BARE coin symbol and store it in
    ``price_history`` under the ``'{SYM}-USD'`` pair. The source is given the bare
    symbols and returns Quotes already keyed by the pair (see
    :class:`CryptoQuoteSource`). Tests pass a fake ``source``; production omits it
    and gets yfinance via :func:`default_quote_source`."""
    syms: list[str] = []
    for s in symbols:
        s = (s or "").strip()
        if s and s not in syms:
            syms.append(s)
    if not syms:
        return []
    src = source or default_quote_source()
    quotes = src.get_quotes(syms)
    default_name = getattr(src, "source_name", None)
    for q in quotes:
        investments.record_price(conn, q.symbol, q.date, q.close,
                                 q.source or default_name)
    return quotes


def fetch_quote_history(conn, symbols, *, start=None, end=None, source=None,
                        interval: str = "1mo") -> int:
    """Download HISTORICAL USD closes for each BARE coin symbol and store them in
    ``price_history`` under the ``'{SYM}-USD'`` pair -- the crypto twin of
    :func:`investments.fetch_quote_history`. The source is handed the bare symbols
    and returns Quotes already keyed by the pair (see :class:`CryptoQuoteSource`);
    a source that reports only the latest close (no ``get_history``) raises
    :class:`QuoteSourceUnavailable`, so "no history support" is distinct from "no
    rows found". Downloaded rows are REFETCHABLE -- a re-download on a corrected
    scale replaces the old ones -- while a register-carried ``txn`` price (see
    :func:`learn_prices_from_transactions`) is kept, exactly as securities do via
    :data:`investments.REFETCHABLE_SOURCES`. Tests inject a fake ``source``;
    production omits it and gets yfinance via :func:`default_quote_source`. Returns
    the number of price rows actually written."""
    syms: list[str] = []
    for s in symbols:
        s = (s or "").strip()
        if s and s not in syms:
            syms.append(s)
    if not syms:
        return 0
    src = source or default_quote_source()
    getter = getattr(src, "get_history", None)
    if getter is None:
        raise QuoteSourceUnavailable(
            "%s cannot fetch historical quotes; it only reports the latest "
            "close." % type(src).__name__)
    quotes = getter(syms, start=start, end=end, interval=interval)
    default_name = getattr(src, "source_name", None)
    rows = [(q.symbol, q.date, q.close, q.source or default_name) for q in quotes]
    return investments.record_prices_if_absent(
        conn, rows, replace_sources=investments.REFETCHABLE_SOURCES)


def _price_from_amount(amount, quantity) -> Optional[Decimal]:
    """The per-unit USD price a fiat ``amount`` (signed cents) and a coin
    ``quantity`` (Decimal text) imply: ``|amount| / 100 / |quantity|``, or ``None``
    when either is missing/zero. Computed in the high-precision quantity context so
    a wei-scale quantity does not lose its low-order digits. Not a guess -- it is
    the same number the ``price`` column would already hold; the wrappers
    (``record_buy`` etc.) fill ``price`` for us, so this only rescues a raw
    :func:`record_event` row that stated a value and a quantity but no price."""
    if not amount or quantity in (None, ""):
        return None
    with decimal.localcontext(quantity_context()):
        qty = abs(_D(quantity))
        if qty == 0:
            return None
        return (Decimal(abs(int(amount))) / _HUNDRED) / qty


def learn_prices_from_transactions(conn, account_id=None, *, txn_id=None) -> int:
    """Record each crypto event's own per-unit USD price into ``price_history``
    under the ``'{SYM}-USD'`` pair, so the register ITSELF is a source of price
    history -- the crypto twin of
    :func:`investments.learn_prices_from_transactions`. A buy/sell/income/swap row
    states what one coin was worth on its date (the ``price`` column the wrappers
    fill from the fiat leg); a raw :func:`record_event` row that stated a value and
    a quantity but no price has it DERIVED (see :func:`_price_from_amount`) -- the
    same number, not a guess. Written with source ``txn`` and DO-NOTHING
    precedence, so an already-recorded quote for the same (pair, date) is KEPT --
    a single trade's implied price never stomps a market close (and a later
    current-quote upsert via :func:`fetch_quotes` still supersedes a ``txn`` row).
    Scope with ``account_id`` (one account) or ``txn_id`` (one row); omit both to
    walk every crypto account. Returns the number of price rows written."""
    cols = "symbol, date, price, amount, quantity"
    if txn_id is not None:
        rows = conn.execute(
            "SELECT %s FROM crypto_transactions WHERE id=?" % cols,
            (txn_id,)).fetchall()
    elif account_id is not None:
        rows = conn.execute(
            "SELECT %s FROM crypto_transactions WHERE account_id=?" % cols,
            (account_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT %s FROM crypto_transactions" % cols).fetchall()
    priced = []
    for r in rows:
        sym = str(r["symbol"] or "").strip()
        if not sym or not r["date"]:
            continue
        price = r["price"]
        if price in (None, ""):
            price = _price_from_amount(r["amount"], r["quantity"])
        if price not in (None, ""):
            priced.append((pair_symbol(sym), r["date"], price, "txn"))
    return investments.record_prices_if_absent(conn, priced) if priced else 0
