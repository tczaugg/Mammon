"""Shared row extraction for the flow-style reports (cash flow, income vs.
expense, by payee, by tag, comparisons, averages).

Every one of those reports asks the same first question -- "which money lines
fall in this window, on these accounts, and what do they carry?" -- and the
answer has three rules that must not drift between reports:

* a SPLIT transaction is represented by its split lines, never by its own
  NULL-category parent row (so nothing is double-counted and each line lands on
  its own category); a split line inherits the parent's payee, tag and date;
  ``split_lines=False`` collapses it back to the parent instead -- see below;
* a TRANSFER is excluded by default -- moving money between your own accounts
  is neither income nor spending. ``transfers="external"`` keeps the legs whose
  OTHER side is outside the selected accounts, which is the cash-flow report's
  question ("what left these accounts", including what went to savings);
  ``transfers="all"`` keeps every leg;
* a SCHEDULED placeholder (``scheduled = 1``) is excluded by default: it is
  Mammon's pre-entry of a bill that has not happened yet.

The spending report (:mod:`mammon.reports.spending`) predates this helper and
keeps its own extraction; it also drops a split transaction whose parent is a
transfer. Here a split's lines are used whatever the parent carries, so the
interest leg of a mortgage payment (a split whose principal leg transfers to
the loan) counts as the expense it is.

**Why ``split_lines`` exists.** Splitting is right for a CATEGORY question
("what did payroll tax cost me?") and wrong for a COUNTERPARTY one ("how much
moved between me and this payee?"). Split legs carry no payee of their own, so
re-grouping them by payee would be a no-op -- except that dropping the transfer
legs unbalances the split and leaves a number that is neither what the payee
paid you nor what you paid them. A real paycheck showed this: gross salary and
the employer match in, tax legs out, a 401(k) deferral transferring to the
retirement account, and the by-payee report totalled the employer at $12,000 --
the withholding -- for a year in which they had deposited $60,000. Passing
``split_lines=False`` bills the whole transaction to its payee at the parent's
own amount, which the ledger guarantees equals the sum of its legs
(:func:`mammon.ledger.set_splits` absorbs any difference into an uncategorized
line rather than letting the two disagree).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Optional

from mammon import ledger


@dataclass(frozen=True)
class Line:
    """One attributable money line. ``amount`` is signed cents (negative = out).
    ``category_id`` is None for uncategorized money and for a transfer leg,
    which carries ``transfer_account_id`` instead."""

    txn_id: int
    date: str
    account_id: int
    category_id: Optional[int]
    amount: int
    payee: str
    tag: str
    memo: str
    transfer_account_id: Optional[int]
    is_split_line: bool


def validate_date(date: str) -> None:
    try:
        _dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError(f"date must be ISO 'YYYY-MM-DD', got {date!r}")


def resolve_accounts(conn, account_ids: Optional[Iterable[int]],
                     include_hidden: bool = False) -> list[int]:
    """The accounts a report covers. An explicit list is taken as given (an
    empty one selects nothing); ``None`` means every account, closed ones
    included (they hold history), with hidden accounts left out unless asked
    for -- hiding is how the user excludes an account from every total."""
    if account_ids is not None:
        return [int(a) for a in account_ids]
    return [int(a["id"]) for a in ledger.list_accounts(
        conn, include_closed=True, include_hidden=include_hidden)]


def signed_lines(conn, start: str, end: str, acct_list: list[int], *,
                 transfers: str = "exclude",
                 include_scheduled: bool = False,
                 split_lines: bool = True) -> list[Line]:
    """Every money line dated in ``[start, end]`` on ``acct_list`` (see the
    module docstring for the rules), in ledger order.

    ``split_lines=False`` collapses a split back to ONE line carrying the
    parent's own signed amount -- the counterparty view the by-payee report
    needs. Such a line has ``category_id=None`` and ``is_split_line=False``,
    since a split has no single category."""
    validate_date(start)
    validate_date(end)
    if transfers not in ("exclude", "external", "all"):
        raise ValueError("transfers must be exclude|external|all")
    if not acct_list:
        return []
    inset = set(acct_list)
    marks = ",".join("?" for _ in acct_list)
    where = ["date >= ?", "date <= ?", f"account_id IN ({marks})"]
    params: list = [start, end, *acct_list]
    if not include_scheduled:
        where.append("scheduled = 0")
    txns = conn.execute(
        "SELECT id, date, account_id, category_id, amount, payee, tag, memo, "
        "transfer_account_id FROM transactions WHERE " + " AND ".join(where)
        + " ORDER BY date, id", params).fetchall()
    if not txns:
        return []
    ids = [t["id"] for t in txns]
    splits: dict[int, list] = {}
    split_parents: set[int] = set()
    for chunk_start in range(0, len(ids), 500):       # keep the IN() list bounded
        chunk = ids[chunk_start:chunk_start + 500]
        cmarks = ",".join("?" for _ in chunk)
        if not split_lines:
            # Only the FACT of a split matters in this mode, not its legs.
            for s in conn.execute(
                f"SELECT DISTINCT transaction_id FROM splits "
                f"WHERE transaction_id IN ({cmarks})", chunk).fetchall():
                split_parents.add(int(s["transaction_id"]))
            continue
        for s in conn.execute(
            f"SELECT s.transaction_id, s.category_id, s.amount, s.memo, "
            f"s.transfer_account_id, g.name AS tag "
            f"FROM splits s LEFT JOIN tags g ON g.id = s.tag_id "
            f"WHERE s.transaction_id IN ({cmarks}) ORDER BY s.id", chunk,
        ).fetchall():
            splits.setdefault(s["transaction_id"], []).append(s)

    def keep(taid) -> bool:
        if taid is None:
            return True
        if transfers == "all":
            return True
        if transfers == "external":
            return taid not in inset
        return False

    out: list[Line] = []
    for t in txns:
        lines = splits.get(t["id"])
        if lines:
            for s in lines:
                taid = s["transfer_account_id"]
                if not keep(taid):
                    continue
                out.append(Line(
                    txn_id=int(t["id"]), date=t["date"], account_id=int(t["account_id"]),
                    category_id=None if taid is not None else s["category_id"],
                    amount=int(s["amount"]), payee=t["payee"] or "",
                    tag=_line_tags(t["tag"], s["tag"]),
                    memo=s["memo"] or t["memo"] or "", transfer_account_id=taid,
                    is_split_line=True))
        else:
            taid = t["transfer_account_id"]
            # A collapsed SPLIT is kept whatever the parent carries: it is a real
            # payee transaction for its own amount even when it also holds a
            # transfer link (a mortgage payment posted as [Loan] plus an interest
            # leg). Only a PLAIN transfer is money moving between the user's own
            # accounts, and its mirror row repeats the payee, so counting it
            # would double. ``split_parents`` is empty unless split_lines=False.
            if int(t["id"]) not in split_parents and not keep(taid):
                continue
            out.append(Line(
                txn_id=int(t["id"]), date=t["date"], account_id=int(t["account_id"]),
                category_id=None if taid is not None else t["category_id"],
                amount=int(t["amount"]), payee=t["payee"] or "", tag=t["tag"] or "",
                memo=t["memo"] or "", transfer_account_id=taid, is_split_line=False))
    return out


def _line_tags(parent_tag, leg_tag) -> str:
    """The tags a SPLIT LINE carries: the parent transaction's, plus the leg's
    own, in one comma-joined string.

    Both apply. A row tagged ``reimbursable`` whose legs are tagged ``Rig 8``
    and ``Rig 9`` has a Rig 8 leg that is also reimbursable, and
    ``reports.tags`` counts a line under every tag it carries. Before
    ``splits.tag_id`` existed a line could only show the parent's, so a leg's
    project was invisible to every report."""
    names = ledger.parse_tags(parent_tag)
    for name in ledger.parse_tags(leg_tag):
        if name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    return ledger.format_tags(names)


def category_paths(conn) -> dict[int, str]:
    """``id -> 'Parent:Child'`` for every category, in one query."""
    meta = {r["id"]: (r["name"], r["parent_id"]) for r in conn.execute(
        "SELECT id, name, parent_id FROM categories").fetchall()}
    out: dict[int, str] = {}
    for cid in meta:
        parts, cur, seen = [], cid, set()
        while cur is not None and cur in meta and cur not in seen:
            seen.add(cur)
            parts.append(meta[cur][0])
            cur = meta[cur][1]
        out[cid] = ":".join(reversed(parts))
    return out


def category_subtree(conn, category_ids: Iterable[int]) -> set[int]:
    """The given category ids plus every descendant (money posts to leaves)."""
    children: dict[Optional[int], list[int]] = {}
    for r in conn.execute("SELECT id, parent_id FROM categories").fetchall():
        children.setdefault(r["parent_id"], []).append(r["id"])
    out: set[int] = set()
    stack = [int(c) for c in category_ids]
    while stack:
        cid = stack.pop()
        if cid in out:
            continue
        out.add(cid)
        stack.extend(children.get(cid, []))
    return out
