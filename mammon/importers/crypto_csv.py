"""Pure parser for a block-explorer *native-coin* by-address CSV export
(Etherscan's "Export CSV" for a single Ethereum wallet is the reference shape).

WHY this is its own parser and record type, separate from ``csvimp.py`` /
``NormalizedTxn``:

- A chain event is not a cash row. Its natural exact-dedup key is an on-chain
  ``tx_hash`` (not a bank ``fitid``); its value is split across TWO unsigned
  columns (``Value_IN`` / ``Value_OUT``) rather than one signed amount; and gas
  is a SECOND same-coin delta on the same event, which no cash row carries.
  ``NormalizedTxn`` has none of ``tx_hash`` / ``fee_symbol`` / ``fee_quantity``,
  so crypto gets its own :class:`CryptoRecord`, the twin of ``NormalizedTxn``.

- The hourglass discipline is preserved: this module is the WAIST on the crypto
  side. It is a pure ``text -> list[CryptoRecord]`` function with NO database
  access and NO knowledge of accounts, dedup or the crypto writers -- exactly
  as ``qif`` / ``csvimp`` parsers stay ignorant of the DB. All DB-facing work
  (account/wallet resolution, tx_hash dedup, dispatch to ``mammon.crypto``'s
  event writers) lives in :mod:`mammon.importers.crypto_core`.

Export schema (Etherscan per-address CSV), columns in order::

    Transaction Hash, Blockno, UnixTimestamp, DateTime (UTC), From, To,
    ContractAddress, Value_IN(ETH), Value_OUT(ETH), CurrentValue @ $<rate>/Eth,
    TxnFee(ETH), TxnFee(USD), Historical $Price/Eth, Status, ErrCode, Method

Exports through roughly 2023 name the first two columns ``Txhash`` and
``DateTime`` and omit ``Method``; both vocabularies are read (see
:func:`_is_hash_col`). Columns are located BY NAME, never by position, so a new
trailing column cannot shift the fee off its own column.

Three things in that header are load-bearing and easy to get wrong:

- ``CurrentValue @ $<rate>/Eth`` embeds the export-time rate in its NAME and
  values every row at that one rate -- it is NEVER the historical basis. This
  parser deliberately does not read it. The per-transaction FMV comes from
  ``Historical $Price/Eth`` (stored as :attr:`CryptoRecord.price`). ``TxnFee``
  is likewise given in both ETH and (CurrentValue-rate) USD; we keep only the
  ETH quantity and let the core value it at the Historical price.

- Value is UNSIGNED and split: exactly one of ``Value_IN`` / ``Value_OUT`` is
  non-zero. This parser records the magnitude in :attr:`CryptoRecord.quantity`
  and the sense in :attr:`CryptoRecord.direction` (``"in"`` acquire / ``"out"``
  dispose); it does NOT decide gas attribution or acquire/dispose semantics --
  that needs the account's own wallet address, which the parser does not have.

- The hash column is the file's SIGNATURE as well as its dedup key, so matching
  it narrowly is worse than matching any other column narrowly: a header the scan
  does not recognise makes the whole file read as EMPTY rather than as
  mis-mapped. It is the one column whose accepted spellings must be kept broad
  and in a single place.

Synthetic-fixture note: real exports are PII (wallet addresses, tx hashes). This
parser is exercised only against synthetic ANON fixtures under
``mammon/tests/fixtures/`` -- never a copied-in real address or hash.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


@dataclass
class CryptoRecord:
    """One on-chain event as produced by a parser, before it hits the ledger --
    the crypto twin of :class:`mammon.importers.record.NormalizedTxn`.

    Quantities and per-unit prices are Decimal-encoded TEXT (exact at any decimal
    count). ``direction`` gives the unsigned :attr:`quantity` its sense; the core
    applies the sign. Wallet addresses (:attr:`from_addr` / :attr:`to_addr`) flow
    through only in memory so the core can decide gas attribution and own-wallet
    transfer classification -- they are never written to a tracked file.
    """

    tx_hash: str = ""             # on-chain hash -- the exact-dedup key
    date: str = ""                # ISO YYYY-MM-DD (derived from the unix stamp)
    symbol: str = "ETH"           # bare coin ticker; native coin only for now
    direction: str = ""           # "in" (acquire) | "out" (dispose)
    quantity: str = ""            # Decimal text, UNSIGNED magnitude of coin moved
    price: str = ""               # Decimal text, per-unit historical USD FMV
    fee_symbol: str = ""          # coin the gas was paid in (native coin => symbol)
    fee_quantity: str = ""        # Decimal text, gas quantity as printed (unsigned)
    from_addr: str = ""           # sender wallet (lower-case hex)
    to_addr: str = ""             # recipient wallet (lower-case hex)
    contract_address: str = ""    # empty for a native-coin transfer
    status: str = ""              # Etherscan Status/ErrCode joined; "" == success
    memo: str = ""

    @property
    def is_success(self) -> bool:
        """Empty Status AND ErrCode means the transaction succeeded. A failed
        transaction still burned the sender's gas but moved no value -- the core
        must not import its value leg."""
        return self.status.strip() == ""


# ---------------------------------------------------------------------------
# Header / value helpers (pure)
# ---------------------------------------------------------------------------
def _dec(value: str) -> Optional[Decimal]:
    """Tolerant Decimal parse of a cell; blank/garbage -> None."""
    s = (value or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _iso_date(unix_ts: str, datetime_str: str) -> str:
    """The event date as ISO ``YYYY-MM-DD``. Prefer the unambiguous
    ``UnixTimestamp`` (UTC seconds, locale-free); fall back to the human
    ``DateTime`` string only if the stamp is missing."""
    s = (unix_ts or "").strip()
    if s.isdigit():
        return _dt.datetime.fromtimestamp(int(s), _dt.timezone.utc).date().isoformat()
    d = (datetime_str or "").strip()
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(d, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"cannot parse a date from {unix_ts!r} / {datetime_str!r}")


def _find_col(header: list[str], *predicates) -> Optional[int]:
    """First column index whose stripped name satisfies any predicate."""
    for pred in predicates:
        for i, name in enumerate(header):
            if pred(name.strip().lower()):
                return i
    return None


def _is_hash_col(n: str) -> bool:
    """Whether a normalised header name is the on-chain hash column.

    ETHERSCAN RENAMES ITS COLUMNS, and this one is load-bearing twice over: it is
    the exact-dedup key AND the signature that identifies the file as an
    by-address export at all. Exports through ~2023 head it ``Txhash``; current
    ones say ``Transaction Hash`` (and ``DateTime (UTC)`` in place of
    ``DateTime``). Matching only the old spelling rejected a real 2025 export
    outright -- the file parsed as nothing, and the import fell back to reporting
    it as an unreadable delimited file. Every accepted spelling lives HERE, so the
    header scan and the column lookup cannot drift apart."""
    return n in ("txhash", "transaction hash", "txn hash") or n == "hash"


def _is_datetime_col(n: str) -> bool:
    """The human-readable timestamp column: ``DateTime`` or ``DateTime (UTC)``.
    Only a FALLBACK -- the unambiguous ``UnixTimestamp`` is preferred -- but it is
    the whole date when a variant omits the stamp."""
    return n.startswith("datetime")


def looks_like_etherscan(text: str) -> bool:
    """A cheap signature test: does ``text`` look like an Etherscan by-address
    native-coin export? Used to route a CSV without guessing off its extension."""
    try:
        header = _locate_header(text)
    except ValueError:
        return False
    return (_find_col(header, _is_hash_col) is not None
            and _find_col(header, lambda n: n.startswith("value_in")) is not None
            and _find_col(header, lambda n: n.startswith("historical")) is not None)


def _locate_header(text: str) -> list[str]:
    """The header row -- the first row carrying the on-chain hash column
    (``Txhash`` on older exports, ``Transaction Hash`` on current ones).
    Etherscan puts it first, but scanning tolerates a leading preamble line."""
    for row in csv.reader(io.StringIO(text)):
        if any(_is_hash_col((c or "").strip().lower()) for c in row):
            return row
    raise ValueError(
        "no Etherscan header row (no 'Transaction Hash' / 'Txhash' column) found")


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------
def parse_etherscan(text: str, default_account: Optional[str] = None) -> list[CryptoRecord]:
    """Turn an Etherscan native-coin by-address CSV into :class:`CryptoRecord`
    rows. Pure: no DB, no account resolution, no dedup. ``default_account`` is
    accepted for signature-parity with the other parsers and is unused (a crypto
    import always targets an explicit wallet-account chosen by the caller).

    Rows with neither ``Value_IN`` nor ``Value_OUT`` moving coin (e.g. a bare
    contract call that only burned gas) are dropped -- there is no coin delta to
    record and gas-only contract calls are a residual case absent from real
    native-coin transfer data.
    """
    rows = list(csv.reader(io.StringIO(text)))
    header = None
    start = 0
    for i, row in enumerate(rows):
        if any(_is_hash_col((c or "").strip().lower()) for c in row):
            header, start = row, i + 1
            break
    if header is None:
        raise ValueError(
            "no Etherscan header row (no 'Transaction Hash' / 'Txhash' column) found")

    i_hash = _find_col(header, _is_hash_col)
    i_unix = _find_col(header, lambda n: n == "unixtimestamp")
    i_dt = _find_col(header, _is_datetime_col)
    i_from = _find_col(header, lambda n: n == "from")
    i_to = _find_col(header, lambda n: n == "to")
    i_contract = _find_col(header, lambda n: n.replace(" ", "") == "contractaddress")
    i_in = _find_col(header, lambda n: n.startswith("value_in"))
    i_out = _find_col(header, lambda n: n.startswith("value_out"))
    i_fee = _find_col(header, lambda n: n.startswith("txnfee(eth")
                      or (n.startswith("txnfee") and "usd" not in n))
    i_hist = _find_col(header, lambda n: n.startswith("historical"))
    i_status = _find_col(header, lambda n: n == "status")
    i_err = _find_col(header, lambda n: n == "errcode")

    missing = [name for name, idx in (
        ("Transaction Hash", i_hash), ("Value_IN", i_in), ("Value_OUT", i_out),
        ("Historical $Price/Eth", i_hist), ("From", i_from), ("To", i_to),
    ) if idx is None]
    if missing:
        raise ValueError(f"Etherscan CSV missing required column(s): {', '.join(missing)}")

    def cell(row, idx):
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    out: list[CryptoRecord] = []
    for row in rows[start:]:
        if not row or not cell(row, i_hash):
            continue
        val_in = _dec(cell(row, i_in)) or Decimal(0)
        val_out = _dec(cell(row, i_out)) or Decimal(0)
        if val_in > 0:
            direction, qty = "in", val_in
        elif val_out > 0:
            direction, qty = "out", val_out
        else:
            continue  # no coin delta -- gas-only/contract call, nothing to book

        fee_dec = _dec(cell(row, i_fee)) or Decimal(0)
        status = " ".join(p for p in (cell(row, i_status), cell(row, i_err)) if p).strip()

        out.append(CryptoRecord(
            tx_hash=cell(row, i_hash),
            date=_iso_date(cell(row, i_unix), cell(row, i_dt)),
            symbol="ETH",
            direction=direction,
            quantity=str(qty),
            price=str(_dec(cell(row, i_hist)) or Decimal(0)),
            fee_symbol="ETH" if fee_dec > 0 else "",
            fee_quantity=str(fee_dec) if fee_dec > 0 else "",
            from_addr=cell(row, i_from).lower(),
            to_addr=cell(row, i_to).lower(),
            contract_address=cell(row, i_contract).lower(),
            status=status,
        ))
    return out
