"""The DB-facing half of the crypto import path: resolve the target wallet,
dedup on the on-chain ``tx_hash``, classify each :class:`CryptoRecord`, and
dispatch to :mod:`mammon.crypto`'s event writers.

WHY this is the whole DB surface (the parser knows nothing):

- SINGLE WRITER. ``mammon.crypto`` is the sole writer of the ``crypto_*`` tables
  (mirroring ``ledger`` for cash and ``investments`` for equities). This module
  therefore NEVER runs ``INSERT INTO crypto_transactions`` itself -- every write
  goes through ``crypto``'s event writers (``record_wallet_credit`` /
  ``record_wallet_debit`` on a coin-native ``kind='wallet'`` account,
  ``record_send`` / ``record_income`` on an ``kind='exchange'`` account, and
  ``record_wallet_transfer`` for an own-wallet move), so the transfer-mirror and
  lot invariants stay enforced in exactly one place. Adding a second writer is
  the main way to break the codebase (CLAUDE.md).

- TWO ACCOUNT KINDS. A ``kind='wallet'`` account is coin-native (the redesign):
  a Value_IN row is a coin credit with no fiat leg, a Value_OUT row a coin debit
  with the gas as a coin-native fee leg (never USD), the on-chain counterparty
  rides ``payee``, and USD lives only at the net-worth layer. A ``kind='exchange'``
  account keeps the fiat-sleeve FMV model (cost basis, realized gain, cash)
  described by the four rules below. ``import_crypto_records`` dispatches on the
  account kind; the wallet path is :func:`_import_wallet_records`.

The four import rules the real 2020 ETH export forced (all applied here):

1. **Gas is the user's only when the user is the sender.** Etherscan prints a
   ``TxnFee`` on every row, including the 23 inbound ones, but on-chain only the
   SENDER pays gas. Gas is booked as a same-coin ``fee_*`` leg ONLY when the
   sender address is one of the user's own registered wallets -- so an inbound
   row's fee (the counterparty's) never debits the user's ETH.
2. **Sign from the two columns.** ``Value_IN>0`` acquires, ``Value_OUT>0``
   disposes (the parser already split this into ``direction`` + magnitude).
3. **FMV from ``Historical $Price/Eth``, never ``CurrentValue``.** The parser
   carries the historical per-unit price; the fiat cents booked as
   basis/proceeds/fee value are computed from THAT, under the high-precision
   quantity context (the wei-summation guard).
4. **Own-wallet transfer needs a known-address registry.** A row is a
   wallet-to-wallet transfer (mirror model, no gain) only when the OTHER address
   also belongs to one of the user's Mammon crypto accounts (matched on
   ``accounts.account_number``); otherwise it is SEND / RECEIVE and the user
   reclassifies. From the data alone none auto-classify -- that is user intent.

Dedup: the on-chain ``tx_hash`` is a globally-unique, immutable exact key -- the
crypto analogue of ``fitid`` and strictly better. A re-import is a NO-OP: each
row whose ``(account_id, tx_hash)`` already exists is skipped before any write,
so re-scraping a wallet's full history never double-inserts (the discipline of
the QIF-reimport regression tests, re-expressed for chain data).
"""
from __future__ import annotations

import decimal
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from mammon import crypto
from mammon.importers.crypto_csv import CryptoRecord, parse_etherscan


@dataclass
class CryptoImportResult:
    """Outcome of a crypto import run. A wallet-to-wallet transfer counts as ONE
    imported action even though it writes two mirror legs."""

    imported: int = 0            # actions written (send/receive/transfer)
    duplicates: int = 0          # rows skipped -- (account_id, tx_hash) already present
    skipped_failed: int = 0      # failed on-chain txns (no value moved)
    gas_legs: int = 0            # actions that carried a booked gas fee
    account_ids: set = field(default_factory=set)   # accounts touched (rebuilt)


def _cents(dollars: Decimal) -> int:
    """Round a Decimal dollar amount to signed integer cents, HALF_UP."""
    return int((dollars * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _wallet_registry(conn: sqlite3.Connection) -> dict[str, int]:
    """Map every registered crypto-wallet address (lower-cased) to its account
    id -- the known-address registry rule 4 needs. An account with no address
    (``account_number`` unset) simply is not in the map, so it can never be the
    'other own wallet' of an auto-classified transfer."""
    reg: dict[str, int] = {}
    for a in crypto.list_accounts(conn, include_closed=True, include_hidden=True):
        addr = (a["account_number"] or "").strip().lower()
        if addr:
            reg[addr] = a["id"]
    return reg


def import_crypto_records(conn: sqlite3.Connection, records: list[CryptoRecord],
                          account_id: int, *, rebuild: bool = True) -> CryptoImportResult:
    """Book ``records`` (from a native-coin by-address export) onto the crypto
    wallet ``account_id``, funnelling every write through ``mammon.crypto``.

    Idempotent: a row already present by ``(account_id, tx_hash)`` is skipped, so
    re-importing the same export changes nothing.
    """
    acct = crypto.get_account(conn, account_id)
    if acct is None:
        raise KeyError(f"no account {account_id}")
    if not crypto.is_crypto_account(acct):
        raise ValueError(f"account {account_id} is not a crypto wallet-account")

    # A 'wallet'-kind account is coin-native (the redesign): a Value_IN row is a
    # coin CREDIT with no fiat leg and a Value_OUT row a coin DEBIT with the gas as
    # a coin-native fee leg (never a USD amount/basis). An 'exchange'-kind account
    # keeps the fiat-sleeve FMV model below (cost basis, realized gain, cash).
    if crypto.is_wallet_account(acct):
        return _import_wallet_records(conn, records, account_id, rebuild=rebuild)

    registry = _wallet_registry(conn)
    result = CryptoImportResult()
    result.account_ids.add(account_id)

    for rec in records:
        if not rec.is_success:
            result.skipped_failed += 1
            continue
        if rec.tx_hash and _already_imported(conn, account_id, rec.tx_hash):
            result.duplicates += 1
            continue

        # The parser guarantees clean Decimal-text for quantity/price ("0" at
        # worst) and either "" or Decimal-text for the gas quantity.
        qty = Decimal(rec.quantity)
        price = Decimal(rec.price)
        fee_qty = Decimal(rec.fee_quantity) if rec.fee_quantity else Decimal(0)
        # Rule 1: the gas is the user's iff the SENDER is one of the user's own
        # wallets. The fee is attributed to that sender account (== this account
        # for a plain send; the OUT-leg account for an own-wallet transfer).
        sender_id = registry.get(rec.from_addr)
        gas_is_users = sender_id is not None and fee_qty > 0

        # Rules 2+3: value the coin (and the gas) at the HISTORICAL price, under
        # the wei-scale high-precision context.
        with decimal.localcontext(crypto.quantity_context()):
            fmv = _cents(price * qty)
            fee_amount = _cents(price * fee_qty) if gas_is_users else None
        fee_symbol = rec.fee_symbol if gas_is_users else None
        fee_quantity = rec.fee_quantity if gas_is_users else None

        # Rule 4: an own-wallet transfer needs the OTHER address in the registry.
        other_addr = rec.to_addr if rec.direction == "out" else rec.from_addr
        other_id = registry.get(other_addr)
        is_own_transfer = other_id is not None and other_id != account_id

        if is_own_transfer:
            frm, to = (account_id, other_id) if rec.direction == "out" else (other_id, account_id)
            crypto.record_wallet_transfer(
                conn, frm, to, rec.date, rec.symbol, qty,
                fee_symbol=fee_symbol, fee_quantity=fee_quantity,
                fee_amount=fee_amount, tx_hash=rec.tx_hash, memo=rec.memo or None)
            result.account_ids.update((frm, to))
        elif rec.direction == "out":
            crypto.record_send(
                conn, account_id, rec.date, rec.symbol, qty, fmv,
                fee_symbol=fee_symbol, fee_quantity=fee_quantity,
                fee_amount=fee_amount, tx_hash=rec.tx_hash, memo=rec.memo or None)
        else:  # direction == "in"
            crypto.record_income(
                conn, account_id, rec.date, "RECEIVE", rec.symbol, qty, fmv,
                tx_hash=rec.tx_hash, memo=rec.memo or None)

        result.imported += 1
        if gas_is_users:
            result.gas_legs += 1

    if rebuild:
        for aid in result.account_ids:
            crypto.rebuild_holdings(conn, aid)
    return result


def _import_wallet_records(conn: sqlite3.Connection, records: list[CryptoRecord],
                           account_id: int, *, rebuild: bool = True) -> CryptoImportResult:
    """Book a coin-native wallet export onto ``account_id`` (a ``kind='wallet'``
    account), the ShrsIn/ShrsOut analogue with NO fiat leg:

    - a ``Value_IN`` row -> :func:`crypto.record_wallet_credit` (coin in), the
      on-chain ``From`` riding ``payee``; no fee (the sender, not the user, paid
      the gas);
    - a ``Value_OUT`` row -> :func:`crypto.record_wallet_debit` (coin out), the
      ``To`` riding ``payee`` and the gas booked as a coin-native ``fee_symbol`` /
      ``fee_quantity`` leg -- NEVER a USD ``fee_amount``. No USD proceeds/basis
      means no realized gain, correct for a coin-native wallet;
    - an own-wallet move (the OTHER address is a registered account of the user's)
      stays a coin mirror :func:`crypto.record_wallet_transfer`, also coin-native
      (``fee_amount=None``).

    Gas attribution needs no address registry here: an Etherscan by-address export
    puts the wallet on the ``From`` of every ``Value_OUT`` row, so a coin-out row
    IS a row the user sent. ``price``/``amount``/``basis`` stay NULL -- USD lives
    only at the net-worth layer. Re-import is a no-op via the same
    ``(account_id, tx_hash)`` dedup as the exchange path."""
    registry = _wallet_registry(conn)
    result = CryptoImportResult()
    result.account_ids.add(account_id)

    for rec in records:
        if not rec.is_success:
            result.skipped_failed += 1
            continue
        if rec.tx_hash and _already_imported(conn, account_id, rec.tx_hash):
            result.duplicates += 1
            continue

        qty = Decimal(rec.quantity)
        fee_qty = Decimal(rec.fee_quantity) if rec.fee_quantity else Decimal(0)
        # Gas is the user's only on a row the wallet SENT (a coin-out row).
        book_fee = rec.direction == "out" and fee_qty > 0
        fee_symbol = rec.fee_symbol if book_fee else None
        fee_quantity = rec.fee_quantity if book_fee else None

        # An own-wallet move needs the OTHER address to be a registered account.
        other_addr = rec.to_addr if rec.direction == "out" else rec.from_addr
        other_id = registry.get(other_addr)
        is_own_transfer = other_id is not None and other_id != account_id

        if is_own_transfer:
            frm, to = ((account_id, other_id) if rec.direction == "out"
                       else (other_id, account_id))
            crypto.record_wallet_transfer(
                conn, frm, to, rec.date, rec.symbol, qty,
                fee_symbol=fee_symbol, fee_quantity=fee_quantity,
                fee_amount=None, tx_hash=rec.tx_hash, memo=rec.memo or None)
            result.account_ids.update((frm, to))
        elif rec.direction == "out":
            crypto.record_wallet_debit(
                conn, account_id, rec.date, rec.symbol, qty,
                payee=rec.to_addr or None, fee_symbol=fee_symbol,
                fee_quantity=fee_quantity, tx_hash=rec.tx_hash,
                memo=rec.memo or None)
        else:  # direction == "in": a coin credit, counterparty = the From address
            crypto.record_wallet_credit(
                conn, account_id, rec.date, rec.symbol, qty,
                payee=rec.from_addr or None, tx_hash=rec.tx_hash,
                memo=rec.memo or None)

        result.imported += 1
        if book_fee:
            result.gas_legs += 1

    if rebuild:
        for aid in result.account_ids:
            crypto.rebuild_holdings(conn, aid)
    return result


def _already_imported(conn: sqlite3.Connection, account_id: int, tx_hash: str) -> bool:
    """Whether this wallet already carries an event for ``tx_hash`` -- the exact
    dedup that makes a re-import a no-op."""
    return conn.execute(
        "SELECT 1 FROM crypto_transactions WHERE account_id=? AND tx_hash=? LIMIT 1",
        (account_id, tx_hash)).fetchone() is not None


# ---------------------------------------------------------------------------
# File entry
# ---------------------------------------------------------------------------
def _decode(raw: bytes) -> str:
    """Decode an export's bytes, tolerating a BOM and legacy code pages (mirrors
    the cash importer's decode ladder)."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def import_etherscan_file(conn: sqlite3.Connection, path, account_id: int, *,
                          rebuild: bool = True) -> CryptoImportResult:
    """Read an Etherscan native-coin by-address CSV at ``path`` and book it onto
    the crypto wallet ``account_id``. The account should carry its wallet address
    in ``account_number`` so rule 1 (gas) and rule 4 (own-wallet transfer) can
    fire; without it, gas is conservatively not booked and no row auto-classifies
    as a transfer."""
    raw = Path(path).read_bytes()
    records = parse_etherscan(_decode(raw))
    return import_crypto_records(conn, records, account_id, rebuild=rebuild)
