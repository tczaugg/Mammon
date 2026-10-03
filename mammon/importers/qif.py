"""QIF parser -- the workhorse for the one-time full-history Quicken migration.

QIF is a line-oriented format: a leading ``!Type:`` (or ``!Account`` block) sets
the context, then each transaction is a run of single-letter-coded lines ending
in ``^``. The letter meanings DIFFER between cash (!Type:Bank/Cash/CCard/Oth) and
investment (!Type:Invst) sections, so we branch on the active mode.

Cash codes:  D date, T/U amount, P payee, L category or [transfer account],
             M memo, N check-number, C cleared, S/E/$ split cat/memo/amount.
Invst codes: D date, N action, Y security, Q quantity, I price, T/U amount,
             O commission, M memo, L [transfer account], C cleared.

Transfers appear on BOTH accounts as an ``L[Other Account]`` line; this parser
just marks ``transfer_account`` and the core collapses the mirror pair into one
ledger transfer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from mammon.importers.record import (
    NormalizedTxn,
    normalize_security_name,
    clean_category,
    split_category_tag,
    decimal_text,
    dollars_to_cents,
    parse_date,
    parse_price_text,
)

# A !Type:Prices row is a bare CSV line: "SYMBOL",price,"M/DD' YY".
_PRICE_RE = re.compile(r'^"([^"]*)",([^,]*),"([^"]*)"$')


@dataclass
class QifExtras:
    """Non-transaction data a Quicken export can carry alongside the register:
    the security master list (!Type:Security) and the per-security price history
    (!Type:Prices). ``securities`` are (name, symbol, type) tuples; ``prices`` are
    (symbol, iso_date, close_text) tuples. Passed to a :func:`parse_qif`
    ``collector`` so the importer can build price_history + holdings and value
    investment accounts, without changing parse_qif's list-of-txns return type."""

    securities: list = field(default_factory=list)
    prices: list = field(default_factory=list)
    # (path, "income" | "expense" | None) from !Type:Cat -- the category list a
    # Quicken export (and mammon.export) leads with, so a category's kind
    # survives the trip even when no transaction in the file uses it.
    categories: list = field(default_factory=list)
    # (name, description) from !Type:Tag, the tag master a Quicken export leads
    # with. Carried for the same reason as `categories`: a tag the user defined
    # but has not used yet still exists, and its description is the only place
    # its meaning is written down.
    tags: list = field(default_factory=list)
    # (account name, account type) for every account whose REGISTER the file
    # carries -- a transaction section opened under its !Account header -- even
    # when that register holds no transactions in the file. It is what tells the
    # importer that a transfer's other account supplies its own side, so that side
    # is never invented (core._counterparty_absent); a register with nothing in
    # it cannot be seen from the transactions alone.
    registers: list = field(default_factory=list)


def parse_qif(
    text: str,
    default_account: Optional[str] = None,
    collector: Optional[QifExtras] = None,
) -> list[NormalizedTxn]:
    """Parse a QIF file into NormalizedTxn records. When ``collector`` is given,
    the security master (!Type:Security) and price history (!Type:Prices) are
    parsed into it as well (they are metadata, not transactions, so they never
    enter the returned list)."""
    records: list[NormalizedTxn] = []
    account = default_account or ""
    account_type = "checking"
    mode = "cash"                      # "cash" | "invst" | "security" | "prices" | "skip"
    in_account_block = False

    fields: dict[str, str] = {}
    sec_fields: dict[str, str] = {}
    tag_fields: dict[str, str] = {}
    cat_fields: dict[str, str] = {}
    split_cat: list[str] = []
    split_amt: list[str] = []
    split_memo: list[str] = []

    def flush() -> None:
        nonlocal fields, split_cat, split_amt, split_memo
        if mode in ("cash", "invst") and fields.get("D"):
            rec = (
                _build_invst(fields, account, account_type)
                if mode == "invst"
                else _build_cash(fields, split_cat, split_amt, split_memo, account, account_type)
            )
            if rec is not None:
                records.append(rec)
        fields = {}
        split_cat, split_amt, split_memo = [], [], []

    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("!"):
            header = line[1:].strip()
            low = header.lower()
            if low.startswith("account"):
                in_account_block = True
            elif low.startswith("type:"):
                in_account_block = False
                kind = low.split(":", 1)[1].strip()
                if _is_txn_section(kind):
                    mode = "invst" if _is_invst(kind) else "cash"
                    account_type = _acct_type(kind)
                    if collector is not None and account and \
                            (account, account_type) not in collector.registers:
                        collector.registers.append((account, account_type))
                elif kind.startswith("cat"):
                    mode, cat_fields = "cat", {}
                elif kind.startswith("security"):
                    mode, sec_fields = "security", {}
                elif kind.startswith("prices"):
                    mode = "prices"
                elif kind.startswith("tag") or kind.startswith("class"):
                    # Quicken's tag master (called "class" before 2010).
                    mode, tag_fields = "tag", {}
                else:
                    # !Type:Memorized / Budget / Template -- metadata sections we
                    # do not consume.
                    mode = "skip"
            # other ! directives (Option, Clear:AutoSwitch, ...) are ignored
            continue

        code, val = line[0], line[1:].rstrip()
        if in_account_block:
            if code == "N":
                account = val.strip()
            elif code == "T":
                account_type = _acct_type(val.strip().lower())
            elif code == "^":
                in_account_block = False
            continue

        # Category list: N path, I income / E expense, D description (a text,
        # never a date -- which is why this section is read on its own terms
        # instead of as transactions); one block per ^.
        if mode == "cat":
            if code == "^":
                _flush_category(collector, cat_fields)
                cat_fields = {}
            else:
                cat_fields[code] = val
            continue
        # Tag master: N name, D description; one block per ^. Same shape as the
        # category list above, and read on its own terms for the same reason.
        if mode == "tag":
            if code == "^":
                _flush_tag(collector, tag_fields)
                tag_fields = {}
            else:
                tag_fields[code] = val
            continue
        # Security master: N name, S symbol, T type; one block per ^.
        if mode == "security":
            if code == "^":
                _flush_security(collector, sec_fields)
                sec_fields = {}
            else:
                sec_fields[code] = val
            continue
        # Price history: each entry is a single quoted CSV line (the ^ that
        # follows carries no data).
        if mode == "prices":
            if line.startswith('"'):
                _add_price(collector, line)
            continue

        if code == "^":
            flush()
            continue
        if code == "S":
            split_cat.append(val)
            split_amt.append("")
            split_memo.append("")
        elif code == "E" and split_memo:
            split_memo[-1] = val
        elif code == "$" and split_amt:
            split_amt[-1] = val
        else:
            fields[code] = val

    flush()  # tolerate a trailing txn with no closing ^
    return records


def _flush_tag(collector: Optional[QifExtras], f: dict) -> None:
    """Record one !Type:Tag block: N is the name, D an optional description."""
    if collector is None:
        return
    name = (f.get("N") or "").strip()
    if name:
        collector.tags.append((name, (f.get("D") or "").strip() or None))


def _flush_category(collector: Optional[QifExtras], f: dict) -> None:
    if collector is None:
        return
    path = clean_category(f.get("N", ""))
    if not path:
        return
    typ = "income" if "I" in f else ("expense" if "E" in f else None)
    collector.categories.append((path, typ))


def _flush_security(collector: Optional[QifExtras], f: dict) -> None:
    if collector is None:
        return
    # The name is the security's IDENTITY: transaction rows (``Y``) and the price
    # section both reach the security through it, so it is normalized exactly as
    # ``Y`` is. Left raw, an option named "ACME  260417C00045000" (the standard
    # symbol's padded root) was one security in the master and another on its
    # own trades, and nearly every contract in a real export went unrecognized
    # as an option.
    name = normalize_security_name(f.get("N", ""))
    symbol = f.get("S", "").strip()
    typ = f.get("T", "").strip()
    if name or symbol:
        collector.securities.append((name, symbol, typ))


def _add_price(collector: Optional[QifExtras], line: str) -> None:
    if collector is None:
        return
    m = _PRICE_RE.match(line)
    if not m:
        return
    symbol = m.group(1).strip()
    close = parse_price_text(m.group(2))
    if not symbol or not close:
        return
    try:
        iso = parse_date(m.group(3))
    except ValueError:
        return
    collector.prices.append((symbol, iso, close))


def _build_cash(fields, split_cat, split_amt, split_memo, account, account_type) -> Optional[NormalizedTxn]:
    lcat = fields.get("L", "").strip()
    transfer_account = ""
    category = ""
    tag = ""
    if lcat.startswith("[") and lcat.endswith("]"):
        transfer_account = lcat[1:-1].strip()
    else:
        category, tag = split_category_tag(lcat)
    cleared, reconciled = _cleared(fields.get("C", ""))
    amount = dollars_to_cents(fields.get("T") or fields.get("U") or "0")

    splits = []
    for c, a, m in zip(split_cat, split_amt, split_memo):
        c = c.strip()
        # Preserve a bracketed ``[Account]`` split leg verbatim -- it is a
        # TRANSFER leg (a mortgage principal posting to the house/loan account,
        # a paycheck 401(k) deferral to the retirement account); the core insert
        # path resolves the bracket to a transfer_account_id on the split row.
        if c.startswith("[") and c.endswith("]"):
            cat, leg_tag = c, ""
        else:
            cat, leg_tag = split_category_tag(c)
        splits.append((cat, dollars_to_cents(a or "0"), m, leg_tag))

    if splits:
        # A split transaction is ``--Split--``, not itself a transfer: Quicken
        # echoes the first split leg's category in the top-level ``L`` line, so
        # an ``L[House]`` here is redundant with a ``[House]`` split leg. Letting
        # it set the whole transaction's transfer_account hid the split (the
        # register showed a lone ``[House]``) and broke reconciliation -- the
        # legs, including any ``[Account]`` transfer leg, own the posting.
        transfer_account = ""
        category = ""
        # ...and for the same reason the echoed L tag is the FIRST LEG's tag,
        # already captured on that leg. Keeping it here too would tag the whole
        # transaction with one leg's project.
        tag = ""

    return NormalizedTxn(
        external_account=account,
        account_type=account_type,
        date=parse_date(fields["D"]),
        amount_cents=amount,
        payee=fields.get("P", ""),
        memo=fields.get("M", ""),
        category=category,
        transfer_account=transfer_account,
        check_number=fields.get("N", ""),
        cleared=cleared,
        reconciled=reconciled,
        tags=[tag] if tag else [],
        splits=splits,
    )


def _build_invst(fields, account, account_type) -> Optional[NormalizedTxn]:
    lacct = fields.get("L", "").strip()
    transfer_account = lacct[1:-1].strip() if lacct.startswith("[") and lacct.endswith("]") else ""
    cleared, reconciled = _cleared(fields.get("C", ""))
    return NormalizedTxn(
        external_account=account,
        account_type=account_type if account_type == "investment" else "investment",
        date=parse_date(fields["D"]),
        amount_cents=dollars_to_cents(fields.get("T") or fields.get("U") or "0"),
        action=fields.get("N", "").strip(),
        symbol=normalize_security_name(fields.get("Y", "")),
        quantity=decimal_text(fields.get("Q", "")),
        price=decimal_text(fields.get("I", "")),
        commission_cents=dollars_to_cents(fields.get("O") or "0"),
        memo=fields.get("M", ""),
        payee=fields.get("P", ""),
        transfer_account=transfer_account,
        cleared=cleared,
        reconciled=reconciled,
    )


def _cleared(flag: str) -> tuple[int, int]:
    f = (flag or "").strip().upper()
    if f in ("X", "R"):
        return 1, 1                    # reconciled implies cleared
    if f in ("*", "C"):
        return 1, 0
    return 0, 0


def _is_txn_section(kind: str) -> bool:
    """True for QIF !Type sections that carry transactions (vs Cat/Tag/Class/
    Memorized/Prices/Security/Budget metadata sections)."""
    k = kind.lower().strip()
    return (
        k.startswith("bank")
        or k.startswith("ccard")
        or k.startswith("cash")
        or k.startswith("oth")          # Oth A (asset) / Oth L (liability)
        or k.startswith("port")
        or _is_invst(k)
    )


def _is_invst(kind: str) -> bool:
    """True for a QIF investment section header.

    Quicken writes ``!Type:Invst``, but brokers exporting "Quicken format" also
    emit the unabbreviated ``!Type:Invest`` -- which shares no substring with
    "invst" (invEst vs invst), so it fell through to the metadata branch and the
    whole file parsed to ZERO transactions with no error at all."""
    k = kind.lower()
    return ("invst" in k or "invest" in k or "port" in k
            or "401" in k or "403" in k)


def _acct_type(kind: str) -> str:
    k = kind.lower()
    if _is_invst(k):
        return "investment"
    if "ccard" in k:
        return "credit"
    if k.startswith("cash"):
        return "cash"
    if "oth l" in k:
        return "liability"
    if "oth a" in k:
        return "asset"
    return "checking"
