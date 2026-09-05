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
  table -- crypto does not add a second writer there.

Conventions (locked, same as the rest of the app):

- Cash / fiat is signed integer CENTS. Quantities and per-unit prices are
  Decimal-encoded TEXT, which round-trips EXACTLY at any decimal count (an
  18-decimal wei value survives ``str()``/TEXT storage losslessly).

- The one genuinely new precision hazard crypto introduces over equities is
  SUMMATION, not storage. Python's DEFAULT decimal context is only 28
  significant digits, so ``Decimal('1e11') + 1 wei`` silently drops the wei.
  Storage is always exact; only quantity ARITHMETIC is at risk. Every quantity
  calculation here therefore runs inside a local high-precision decimal context
  (>= 40 significant digits) via :func:`quantity_context`, rather than trusting
  the process default.

A crypto account is a DISTINCT ``accounts.type`` value (``'crypto'``) -- a coin
wallet is not an equity brokerage -- but is classified INVESTMENT-LIKE
(``ledger.INVESTMENT_LIKE_TYPES``) for net worth, sidebar grouping and the
allocation pie. The wallet address lives in the existing
``accounts.account_number`` column (already blanked from the MCP surface), and
``asset_class = 'crypto'`` carries the allocation classification.

Phase 1 scope: schema plus enough domain scaffolding to CREATE a ``'crypto'``
account and read it back. The ``record_*`` / ``update_*`` / ``delete_*`` event
writers, holdings replay, per-year checkpoints, valuation, the wallet-to-wallet
transfer mirror (``transfer_pair_id``) and coin-for-coin swap pairing
(``swap_group_id``) arrive with the crypto_transactions writers.
"""
from __future__ import annotations

import decimal
from decimal import Decimal
from typing import Optional

import sqlite3

from mammon import ledger

# The distinct account.type value for a crypto wallet, and the asset_class that
# flows a coin position into the allocation pie / rebalance drift.
CRYPTO_ACCOUNT_TYPE = "crypto"
CRYPTO_ASSET_CLASS = "crypto"

# Significant digits for quantity arithmetic. The default context (28) can drop
# a wei when summing a large balance; 40 clears realistic integer-part +
# 18-decimal magnitudes with room to spare. See the module docstring.
QUANTITY_PRECISION = 40


def quantity_context() -> decimal.Context:
    """A high-precision :class:`decimal.Context` for wei-scale quantity math.

    Use as ``with decimal.localcontext(crypto.quantity_context()): ...`` so a
    sum of many wei-scale quantities does not silently lose low-order digits
    under the process default 28-significant-digit context. Storage is exact
    regardless; this guards the SUMMATION, which is the only place the ceiling
    bites.
    """
    return decimal.Context(prec=QUANTITY_PRECISION)


def _qty_text(qty: Decimal) -> str:
    """A clean, exponent-free Decimal string for storage ('100' not '1E+2'),
    normalized under the high-precision quantity context so a wei-scale value
    normalizes without loss. Mirrors ``investments._qty_text``."""
    with decimal.localcontext(quantity_context()):
        if qty == 0:
            return "0"
        return format(qty.normalize(), "f")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def is_crypto_account(acct: Optional[sqlite3.Row]) -> bool:
    """Whether ``acct`` (a row, or None) is a crypto wallet-account."""
    return acct is not None and (acct["type"] or "") == CRYPTO_ACCOUNT_TYPE


def is_investment_like(acct: Optional[sqlite3.Row]) -> bool:
    """Whether ``acct`` is valued at market and grouped with investments -- an
    equity brokerage OR a crypto wallet. Classification tests membership in the
    single source of truth ``ledger.INVESTMENT_LIKE_TYPES``."""
    return acct is not None and (acct["type"] or "") in ledger.INVESTMENT_LIKE_TYPES


def create_account(conn: sqlite3.Connection, name: str, *,
                   opening_balance: int = 0,
                   opening_date: Optional[str] = None,
                   institution: Optional[str] = None,
                   note: Optional[str] = None,
                   wallet_address: Optional[str] = None) -> int:
    """Create a crypto wallet-account (``type='crypto'``) and return its id.

    Account rows are written by ``ledger`` (the one writer of the accounts
    table); this wrapper only fixes the type to ``'crypto'`` and stamps
    ``asset_class='crypto'`` so the position flows into the allocation pie. The
    wallet address, when supplied, is stored in ``account_number`` (the same
    column the MCP authorizer blanks) -- crypto introduces no new sensitive
    column and no credential storage.
    """
    account_id = ledger.create_account(
        conn, name, CRYPTO_ACCOUNT_TYPE,
        opening_balance=opening_balance, opening_date=opening_date,
        institution=institution, note=note,
    )
    fields: dict = {"asset_class": CRYPTO_ASSET_CLASS}
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
    """Just the crypto wallet-accounts, filtered from ``ledger.list_accounts``."""
    return [a for a in ledger.list_accounts(
        conn, include_closed=include_closed, include_hidden=include_hidden)
        if (a["type"] or "") == CRYPTO_ACCOUNT_TYPE]
