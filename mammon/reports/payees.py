"""mammon.reports.payees -- money by payee and by tag (SRD 5.9; parity
roadmap item 4).

Quicken's Spending by Payee and Spending by Tag. ``direction`` picks what is
summed: ``"out"`` (the default) totals money out as positive magnitudes and
ignores inflows, so a payee that also refunded you shows what you paid them;
``"in"`` is the mirror; ``"net"`` keeps the sign. ``count`` is the number of
TRANSACTIONS, not lines. Transfers are excluded.

**A payee is billed the whole transaction.** :func:`by_payee` reads collapsed
lines (``split_lines=False``): a split transaction contributes its parent's own
amount, not its legs. Split legs carry no payee of their own, so decomposing
one only to re-group it by payee would change nothing -- except that the shared
extraction drops transfer legs, which unbalances the split. That is how an
employer's paychecks (salary in, tax legs out, a 401(k) deferral transferring
away) totalled to the WITHHOLDING under the employer's name. :func:`by_tag`
keeps the legs: a tag annotates what money was FOR, which is a category-shaped
question, and payroll tax is a real expense however the paycheck nets out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from mammon.reports._lines import Line, resolve_accounts, signed_lines

NO_PAYEE = "(no payee)"
NO_TAG = "(no tag)"
DIRECTIONS = ("out", "in", "net")


@dataclass
class PayeeRow:
    name: str
    count: int                     # distinct transactions
    cents: int                     # magnitude for out/in, signed for net


@dataclass
class PayeeReport:
    start: str
    end: str
    key: str                       # payee | tag
    direction: str
    account_ids: list[int]
    rows: list[PayeeRow]           # largest first
    total: int


def _group(lines: list[Line], keyfn: Callable[[Line], str], direction: str) -> list[PayeeRow]:
    if direction not in DIRECTIONS:
        raise ValueError("direction must be out|in|net")
    cents: dict[str, int] = {}
    txns: dict[str, set[int]] = {}
    for ln in lines:
        if direction == "out":
            if ln.amount >= 0:
                continue
            value = -ln.amount
        elif direction == "in":
            if ln.amount <= 0:
                continue
            value = ln.amount
        else:
            value = ln.amount
        k = keyfn(ln)
        cents[k] = cents.get(k, 0) + value
        txns.setdefault(k, set()).add(ln.txn_id)
    rows = [PayeeRow(k, len(txns[k]), v) for k, v in cents.items()]
    rows.sort(key=lambda r: (-abs(r.cents), r.name.lower()))
    return rows


def _report(conn, start, end, *, key, keyfn, account_ids, include_hidden,
            direction, include_scheduled, split_lines=True) -> PayeeReport:
    acct_list = resolve_accounts(conn, account_ids, include_hidden)
    lines = signed_lines(conn, start, end, acct_list,
                         include_scheduled=include_scheduled,
                         split_lines=split_lines)
    rows = _group(lines, keyfn, direction)
    return PayeeReport(start=start, end=end, key=key, direction=direction,
                       account_ids=acct_list, rows=rows,
                       total=sum(r.cents for r in rows))


def by_payee(conn, start: str, end: str, *,
             account_ids: Optional[Iterable[int]] = None,
             include_hidden: bool = False, direction: str = "out",
             include_scheduled: bool = False) -> PayeeReport:
    """Money by payee over ``[start, end]``. Payee text is matched as stored
    (trimmed); a blank payee groups under ``(no payee)``.

    A split transaction is billed to its payee at its own amount rather than by
    its legs (module docstring), so a paycheck counts as the deposit it was."""
    return _report(conn, start, end, key="payee",
                   keyfn=lambda ln: ln.payee.strip() or NO_PAYEE,
                   account_ids=account_ids, include_hidden=include_hidden,
                   direction=direction, include_scheduled=include_scheduled,
                   split_lines=False)


def by_tag(conn, start: str, end: str, *,
           account_ids: Optional[Iterable[int]] = None,
           include_hidden: bool = False, direction: str = "out",
           include_scheduled: bool = False) -> PayeeReport:
    """Money by tag over ``[start, end]``; untagged money groups under
    ``(no tag)``."""
    return _report(conn, start, end, key="tag",
                   keyfn=lambda ln: ln.tag.strip() or NO_TAG,
                   account_ids=account_ids, include_hidden=include_hidden,
                   direction=direction, include_scheduled=include_scheduled)
