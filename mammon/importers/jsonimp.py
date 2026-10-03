"""JSON parser for webSlinger-scraped transaction lists (SRD 6.4 / 7.2).

webSlinger emits scraped rows in a few shapes depending on how the demonstration
was recorded, so we accept all of them:
  * a top-level list of record objects,
  * ``{"transactions": [ ... ]}``,
  * the flattened array form ``{"transactions[0]": {...}, "transactions[1]": {...}}``
    (as seen in legacy/convertJson2Qif.py).
Field names are matched loosely (payee/payeeName, amount/transactionAmount, ...),
and money may arrive as ``amount_cents`` (int) or a dollar string/number.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from mammon.importers.record import (
    NormalizedTxn,
    decimal_text,
    dollars_to_cents,
)

_DEBIT_WORDS = {"debit", "withdrawal", "payment", "purchase", "sale", "charge"}


def parse_json(text: str, default_account: Optional[str] = None) -> list[NormalizedTxn]:
    data = json.loads(text)
    return [_record(r, default_account) for r in _rows(data)]


def _rows(data) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        txns = data.get("transactions")
        if isinstance(txns, list):
            return [r for r in txns if isinstance(r, dict)]
        flat = [v for k, v in data.items() if re.search(r"\[\d+\]$", k) and isinstance(v, dict)]
        if flat:
            return flat
        vals = [v for v in data.values() if isinstance(v, dict)]
        if vals:
            return vals
    return []


def _record(r: dict, default_account: Optional[str]) -> NormalizedTxn:
    amount = _amount(r)
    action = _g(r, "action", "transactionType", "investmentAction")
    symbol = _g(r, "symbol", "ticker", "security")
    is_inv = bool(action) and bool(symbol)
    return NormalizedTxn(
        external_account=_g(r, "external_account", "account", "accountName") or (default_account or ""),
        date=_date(r),
        amount_cents=amount,
        payee=_g(r, "payee", "payeeName", "name", "merchant", "description"),
        memo=_g(r, "memo", "note", "notes"),
        category=_g(r, "category", "categoryName"),
        check_number=_g(r, "check_number", "checkNumber", "check"),
        fitid=_g(r, "fitid", "id", "transactionId", "referenceNumber"),
        type=_g(r, "type", "transactionType"),
        action=action if is_inv else "",
        symbol=symbol if is_inv else "",
        quantity=decimal_text(_g(r, "quantity", "shares", "units")) if is_inv else "",
        price=decimal_text(_g(r, "price", "unitPrice")) if is_inv else "",
        commission_cents=dollars_to_cents(_g(r, "commission", "fees")) if is_inv else 0,
        transfer_account=_g(r, "transfer_account", "transferAccount"),
    )


def _amount(r: dict) -> int:
    if "amount_cents" in r and r["amount_cents"] not in (None, ""):
        return int(r["amount_cents"])
    cents = dollars_to_cents(_g(r, "amount", "transactionAmount", "amount_usd"))
    # If the source gives a positive magnitude plus a debit/credit hint, sign it.
    if cents > 0:
        hint = str(_g(r, "type", "transactionType", "direction")).lower()
        if any(w in hint for w in _DEBIT_WORDS):
            cents = -cents
    return cents


def _date(r: dict) -> str:
    from mammon.importers.record import parse_date

    return parse_date(
        _g(
            r,
            "date",
            "transactionDate",
            "postedDate",
            "posted",
            "postDate",
            "effectiveDate",
            "effective",
            "tradeDate",
        )
    )


def _g(r: dict, *names: str) -> str:
    for n in names:
        if n in r and r[n] not in (None, ""):
            return str(r[n]).strip()
    return ""
