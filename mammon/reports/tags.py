"""mammon.reports.tags -- money by first-class tag (many-per-transaction).

This is distinct from :func:`mammon.reports.payees.by_tag`, and deliberately so.
``payees.by_tag`` groups by a transaction's WHOLE tag string as one key -- it
predates first-class tags and is the right answer only while a row carries one
tag. Once a transaction can carry several tags (``vacation`` AND ``reimbursable``),
"spending by tag" must attribute a line's money to EACH of its tags: a $100 dinner
tagged both shows up under both. Tag totals can therefore exceed the grand total
when tags overlap -- that is correct, not a bug, and it is why this lives in its
own function rather than reusing the single-key grouper.

The tags themselves come from the ``transactions.tag`` cache, which ``ledger``
keeps as the normalized, comma-joined projection of the authoritative
``transaction_tags`` junction; splitting it with ``ledger.parse_tags`` yields the
same individual tags the junction holds, with no extra per-row query. Untagged
money groups under ``(no tag)``. Transfers/splits/scheduling follow the shared
line rules in :mod:`mammon.reports._lines`. Pure and read-only.
"""
from __future__ import annotations

from typing import Iterable, Optional

from mammon import ledger
from mammon.reports._lines import resolve_accounts, signed_lines
from mammon.reports.payees import DIRECTIONS, NO_TAG, PayeeReport, PayeeRow


def spending_by_tag(conn, start: str, end: str, *,
                    account_ids: Optional[Iterable[int]] = None,
                    include_hidden: bool = False, direction: str = "out",
                    include_scheduled: bool = False) -> PayeeReport:
    """Money by tag over ``[start, end]``, each line counted under every tag it
    carries. ``direction`` matches the payee/tag reports: ``"out"`` sums outflow
    magnitudes, ``"in"`` inflow, ``"net"`` keeps the sign. Returns a
    :class:`~mammon.reports.payees.PayeeReport` with ``key="tag"`` so it projects
    through the same report window as By Payee."""
    if direction not in DIRECTIONS:
        raise ValueError("direction must be out|in|net")
    acct_list = resolve_accounts(conn, account_ids, include_hidden)
    lines = signed_lines(conn, start, end, acct_list, include_scheduled=include_scheduled)
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
        names = ledger.parse_tags(ln.tag) or [NO_TAG]
        for name in names:
            cents[name] = cents.get(name, 0) + value
            txns.setdefault(name, set()).add(ln.txn_id)
    rows = [PayeeRow(k, len(txns[k]), v) for k, v in cents.items()]
    rows.sort(key=lambda r: (-abs(r.cents), r.name.lower()))
    return PayeeReport(start=start, end=end, key="tag", direction=direction,
                       account_ids=acct_list, rows=rows,
                       total=sum(r.cents for r in rows))
