"""mammon.reports.balances -- account balances now and over time (SRD 5.9;
parity roadmap item 4).

Quicken's Account Balances report and its balances-over-time chart, as pure
functions. Every account is valued the way the account bar values it
(:func:`mammon.investments.display_balance`): an investment account at market
(cash plus securities at the newest price on or before the date), everything
else at its ledger balance. Hidden accounts are left out unless asked for,
matching :func:`mammon.ledger.net_worth`, so a report and the account bar
never disagree by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mammon import investments, ledger
from mammon.reports._lines import resolve_accounts
from mammon.reports.flows import bucket_end, buckets_in


def _flag(row, key: str) -> bool:
    keys = row.keys() if hasattr(row, "keys") else ()
    return bool(row[key]) if key in keys else False


@dataclass
class AccountBalance:
    account_id: int
    name: str
    type: str
    hidden: bool
    closed: bool
    cents: int


@dataclass
class BalanceReport:
    as_of: Optional[str]           # None = the ledger's latest valuation date
    rows: list[AccountBalance]     # account-bar order
    total: int


def account_balances(conn, as_of: Optional[str] = None, *,
                     include_hidden: bool = False,
                     include_closed: bool = True) -> BalanceReport:
    """Every account's balance as of a date (default: latest), with the total
    -- net worth over the accounts listed."""
    rows: list[AccountBalance] = []
    for a in ledger.list_accounts(conn, include_closed=include_closed,
                                  include_hidden=include_hidden):
        rows.append(AccountBalance(
            account_id=int(a["id"]), name=a["name"], type=a["type"] or "",
            hidden=_flag(a, "hidden"), closed=_flag(a, "closed_flag"),
            cents=investments.display_balance(conn, int(a["id"]), as_of)))
    return BalanceReport(as_of=as_of, rows=rows, total=sum(r.cents for r in rows))


@dataclass
class BalanceSample:
    bucket: str
    as_of: str                     # the bucket's last day, clamped to the range end
    by_account: dict[int, int]
    total: int


@dataclass
class BalanceSeries:
    start: str
    end: str
    bucket: str
    accounts: list[dict]           # [{id, name, type}] in account-bar order
    samples: list[BalanceSample]


def balances_over_time(conn, start: str, end: str, *, bucket: str = "month",
                       account_ids: Optional[Iterable[int]] = None,
                       include_hidden: bool = False) -> BalanceSeries:
    """Each account's balance at the end of every bucket in the range, plus
    the total per bucket. Balances are point-in-time, so a bucket with no
    activity repeats the previous one -- that is the flat stretch on the chart,
    not a gap."""
    if bucket == "total":
        raise ValueError("balances over time need month|quarter|year buckets")
    acct_list = resolve_accounts(conn, account_ids, include_hidden)
    names = {}
    for a in ledger.list_accounts(conn, include_closed=True, include_hidden=True):
        names[int(a["id"])] = {"id": int(a["id"]), "name": a["name"], "type": a["type"] or ""}
    accounts = [names[aid] for aid in acct_list if aid in names]
    samples: list[BalanceSample] = []
    for key in buckets_in(start, end, bucket):
        as_of = bucket_end(key, bucket, end)
        by = {aid: investments.display_balance(conn, aid, as_of) for aid in acct_list}
        samples.append(BalanceSample(bucket=key, as_of=as_of, by_account=by,
                                     total=sum(by.values())))
    return BalanceSeries(start=start, end=end, bucket=bucket,
                         accounts=accounts, samples=samples)
