"""mammon.reports.listing -- a filtered transaction listing (SRD 5.9; parity
roadmap item 4).

Quicken's Transactions report: the rows themselves, across accounts, narrowed
by any combination of date range, accounts, categories (a category includes
its subtree, and a split matches on any of its lines), payee or memo text,
tag, amount range and cleared state. Plain dicts, oldest first by default, with
a ``limit`` and a ``truncated`` flag so a tool answer stays bounded. The text
search over EVERYTHING lives in :func:`mammon.ledger.search_transactions`;
this is the structured cousin.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mammon import ledger
from mammon.reports._lines import (category_subtree, resolve_accounts,
                                   validate_date)

CLR_STATES = ("any", "uncleared", "cleared", "reconciled")


@dataclass
class ListingReport:
    start: str
    end: str
    account_ids: list[int]
    rows: list[dict]
    count: int                     # rows returned
    total_cents: int               # signed sum of the rows returned
    truncated: bool                # a limit cut the list short


def transactions(conn, start: str, end: str, *,
                 account_ids: Optional[Iterable[int]] = None,
                 include_hidden: bool = False,
                 category_ids: Optional[Iterable[int]] = None,
                 payee_contains: Optional[str] = None,
                 memo_contains: Optional[str] = None,
                 tag: Optional[str] = None,
                 amount_min: Optional[int] = None,
                 amount_max: Optional[int] = None,
                 cleared: str = "any",
                 include_transfers: bool = True,
                 include_scheduled: bool = False,
                 newest_first: bool = False,
                 limit: Optional[int] = None) -> ListingReport:
    """The transactions matching every given filter. ``amount_min``/``max``
    bound the ABSOLUTE amount in cents. ``cleared`` is ``any``, ``uncleared``,
    ``cleared`` (the ``c`` state alone) or ``reconciled``. A row carries the
    account name, the register's category label (``Parent:Child``,
    ``[Account]`` or ``--Split--``), and ``transfer_account`` when it is a
    transfer leg."""
    validate_date(start)
    validate_date(end)
    if cleared not in CLR_STATES:
        raise ValueError("cleared must be any|uncleared|cleared|reconciled")
    acct_list = resolve_accounts(conn, account_ids, include_hidden)
    if not acct_list:
        return ListingReport(start, end, [], [], 0, 0, False)
    marks = ",".join("?" for _ in acct_list)
    where = ["date >= ?", "date <= ?", f"account_id IN ({marks})"]
    params: list = [start, end, *acct_list]
    if not include_scheduled:
        where.append("scheduled = 0")
    order = "date DESC, id DESC" if newest_first else "date, id"
    txns = conn.execute(
        "SELECT * FROM transactions WHERE " + " AND ".join(where) + f" ORDER BY {order}",
        params).fetchall()

    cats = category_subtree(conn, category_ids) if category_ids is not None else None
    payee_q = (payee_contains or "").strip().lower()
    memo_q = (memo_contains or "").strip().lower()
    tag_q = (tag or "").strip().lower()
    names: dict[int, str] = {}

    def account_name(aid: int) -> str:
        if aid not in names:
            a = ledger.get_account(conn, aid)
            names[aid] = a["name"] if a else ""
        return names[aid]

    rows: list[dict] = []
    truncated = False
    for t in txns:
        split = ledger.has_splits(conn, t["id"])
        is_transfer = t["transfer_account_id"] is not None and not split
        if not include_transfers and is_transfer:
            continue
        if payee_q and payee_q not in (t["payee"] or "").lower():
            continue
        if memo_q and memo_q not in (t["memo"] or "").lower():
            continue
        if tag_q and (t["tag"] or "").strip().lower() != tag_q:
            continue
        magnitude = abs(int(t["amount"]))
        if amount_min is not None and magnitude < amount_min:
            continue
        if amount_max is not None and magnitude > amount_max:
            continue
        if cleared == "uncleared" and (t["cleared"] or t["reconciled"]):
            continue
        if cleared == "cleared" and not (t["cleared"] and not t["reconciled"]):
            continue
        if cleared == "reconciled" and not t["reconciled"]:
            continue
        if cats is not None:
            hit = t["category_id"] in cats
            if not hit and split:
                hit = any(s["category_id"] in cats for s in ledger.get_splits(conn, t["id"]))
            if not hit:
                continue
        if limit is not None and len(rows) >= limit:
            truncated = True
            break
        xfer = t["transfer_account_id"]
        rows.append({
            "id": int(t["id"]),
            "date": t["date"],
            "account_id": int(t["account_id"]),
            "account": account_name(int(t["account_id"])),
            "num": t["num"] or "",
            "payee": t["payee"] or "",
            "category": ledger.category_display(conn, t),
            "memo": t["memo"] or "",
            "tag": t["tag"] or "",
            "amount": int(t["amount"]),
            "cleared": bool(t["cleared"]),
            "reconciled": bool(t["reconciled"]),
            "is_split": split,
            "transfer_account": account_name(int(xfer)) if xfer is not None else "",
        })
    return ListingReport(start=start, end=end, account_ids=acct_list, rows=rows,
                         count=len(rows), total_cents=sum(r["amount"] for r in rows),
                         truncated=truncated)
