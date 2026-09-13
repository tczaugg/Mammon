"""mammon.projection -- projected balances and the financial calendar's data
(parity roadmap item 5).

Quicken's Projected Balances answers "what will checking be on the 28th, after
the mortgage and before payday?" from three things and nothing else: the
balance today, the rows already entered with future dates (pre-entered
placeholders included), and the reminders that have not been entered yet. It
does not look at history, budgets or interest; neither does this. The result
is one point per day, so the same call feeds the balance chart, the events
table and the month calendar.

What counts as a future event, and why each is not double-counted:

* an ENTERED row dated in the window (posted or a ``scheduled=1`` placeholder)
  -- already in the ledger, so it also already sits in the opening balance
  when dated before the window; inside the window it is listed as an event;
* a manual definition's OCCURRENCES from its ``next_date`` forward -- the
  generator advances ``next_date`` past every placeholder it creates, so
  everything from ``next_date`` on is, by construction, not yet entered. An
  occurrence already overdue lands on the window's first day (it is money
  still expected to move). A transfer definition yields a leg on each side;
* a LOAN's schedule rows in the window that carry no payment yet (the same
  guard :mod:`mammon.loans_schedule` uses before pre-entering one). When the
  loan's funding account is known (stored in Loan Setup, else inferred from
  history), the whole payment is projected leaving that account and only the
  principal arriving at the loan -- the shape the real payments have; a loan
  with no known funder is projected on its own register;
* a PREDICTED occurrence -- a payee that recurred at a steady interval and a
  similar amount over the last few months (:mod:`mammon.predictions`), from
  its next expected date on. This is the departure from Quicken, whose
  Projected Balances knows only what the user defined: a month ahead built
  from reminders alone was, in practice, mostly empty. A prediction is an
  estimate and is marked as one; it is not shown beside an entered row for
  the same payee within a few days (that row IS the occurrence), it never
  duplicates a definition (a scheduled payee is not predicted), and it can
  be dismissed or turned into a definition. ``include_predictions=False``
  gives the reminders-only view.

Predicted-vs-scheduled dedup, and why it lives HERE. Two guards used to be
asked to keep a reminder and a prediction of the same bill apart, and both
are month-shaped or text-shaped:
:func:`mammon.predictions.predict_recurring` skips a payee that a definition
already covers, but only when the definition's stored payee text normalizes
to the same key as the ledger rows -- a rename applied after the definition
was written, or different bank text, and the guard misses silently; and
:func:`mammon.predictions.is_entered` suppresses a prediction only where a
real posted row sits within a few days, which is true of the CURRENT month
and false of every month further out. So the duplicate was invisible in the
month that already had the payment and appeared in the next one. The merge
point below is the only place that sees both lists, so it does the
reconciliation: a PREDICTED event is dropped when a SCHEDULED event on the
same account falls within ``ENTERED_WINDOW_DAYS`` of it and agrees on EITHER
the normalized payee key OR the signed amount. Either, not both: the case
this exists for is precisely payee text that drifted, and demanding both
would leave it broken, while demanding neither would collapse two genuinely
different bills that happen to fall on one day. The asymmetry is deliberate
-- what the user defined always wins, a guess never suppresses a definition,
and two definitions never suppress each other.

Balances are ledger balances (cash), so an investment account projects its
cash only; projection is a spending-account question.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable, Optional

from mammon import ledger, loans, scheduled

ENTERED, SCHEDULED, LOAN, PREDICTED = "entered", "scheduled", "loan", "predicted"


@dataclass
class ProjectedEvent:
    date: str
    account_id: int
    payee: str
    amount: int                    # signed cents on this account
    source: str                    # entered | scheduled | loan | predicted
    pending: bool = False          # an entered placeholder (scheduled=1)
    definition_id: Optional[int] = None
    txn_id: Optional[int] = None
    automatic: bool = False        # a predicted payment the bank marks automatic
    payee_key: Optional[str] = None


@dataclass
class ProjectedDay:
    date: str
    balance: int                   # end of day, across the projected accounts
    events: list = field(default_factory=list)


@dataclass
class Projection:
    account_ids: list[int]
    start: str
    end: str
    opening: int                   # balance the day before ``start``
    days: list[ProjectedDay]
    closing: int
    low: int
    low_date: str

    def events(self) -> list[ProjectedEvent]:
        return [e for d in self.days for e in d.events]


def _iso(d: _dt.date) -> str:
    return d.isoformat()


def _day_before(iso: str) -> str:
    return _iso(_dt.date.fromisoformat(iso) - _dt.timedelta(days=1))


def projected_events(conn, account_ids: Iterable[int], start: str, end: str, *,
                     include_predictions: bool = True,
                     today: Optional[str] = None) -> list[ProjectedEvent]:
    """Every future event on the accounts in ``[start, end]`` (see the module
    docstring for what counts), in date order. Predictions are read from
    history up to ``today`` (``start`` when omitted) and placed from their next
    expected date on."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return []
    today = today or start
    inset = set(ids)
    marks = ",".join("?" for _ in ids)
    out: list[ProjectedEvent] = []
    # (account_id, date, payee key, signed cents) of every SCHEDULED event put
    # in the window, so a prediction of the same bill can be dropped below.
    sched_marks: list[tuple[int, _dt.date, str, int]] = []
    for t in conn.execute(
            f"SELECT id, account_id, date, payee, amount, scheduled FROM transactions "
            f"WHERE account_id IN ({marks}) AND date >= ? AND date <= ? ORDER BY date, id",
            [*ids, start, end]).fetchall():
        out.append(ProjectedEvent(t["date"], int(t["account_id"]), t["payee"] or "",
                                  int(t["amount"]), ENTERED, pending=bool(t["scheduled"]),
                                  txn_id=int(t["id"])))
    for d in scheduled.list_scheduled(conn, active_only=True):
        legs = []
        if d["account_id"] in inset:
            legs.append((d["account_id"], d["amount"]))
        taid = d.get("transfer_account_id")
        if taid is not None and taid in inset:
            legs.append((taid, -d["amount"]))
        if not legs:
            continue
        # An OVERDUE occurrence (due before the window) is money still expected
        # to move, so it lands on the first day rather than vanishing: walking
        # from next_date with no lower bound and clamping does that.
        for due in scheduled.occurrences(d["next_date"], d["frequency"], "0000-01-01", end):
            when = max(due, start)
            for aid, amount in legs:
                name = d["payee"] or "Scheduled payment"
                out.append(ProjectedEvent(when, aid, name, amount, SCHEDULED,
                                          definition_id=d["id"]))
                sched_marks.append((aid, _dt.date.fromisoformat(when),
                                    scheduled._payee_key(name), int(amount)))
    from mammon import loans_schedule
    for r in conn.execute("SELECT account_id FROM loan_params").fetchall():
        aid = int(r["account_id"])
        funder = loans.funding_account(conn, aid)
        if aid not in inset and (funder is None or funder not in inset):
            continue
        try:
            sched = loans.amortization_schedule(conn, aid)
        except LookupError:
            continue
        acct = ledger.get_account(conn, aid)
        name = acct["name"] if acct else "Loan"
        payee = ((loans.last_payment_payee(conn, aid, funder) if funder is not None else None)
                 or f"{name} Payment")
        for row in sched:
            if row.date > end:
                break
            # A schedule row before the window with no payment posted is an
            # overdue payment: it lands on the first day, like a reminder.
            when = max(row.date, start)
            if funder is not None:
                # The user's model: the whole payment leaves the funder and only
                # the principal reaches the loan (interest and escrow are spent).
                if loans_schedule.payment_for(conn, aid, row.date) is not None:
                    continue
                if funder in inset:
                    out.append(ProjectedEvent(when, funder, payee, -int(row.payment),
                                              LOAN, definition_id=aid))
                if aid in inset:
                    out.append(ProjectedEvent(when, aid, payee, int(row.principal),
                                              LOAN, definition_id=aid))
                continue
            if loans_schedule.payment_for(conn, aid, row.date) is not None:
                continue
            out.append(ProjectedEvent(when, aid, payee, int(row.payment),
                                      LOAN, definition_id=aid))
    if include_predictions:
        from mammon import predictions as _pred
        first = max(start, today)
        if first <= end:
            entered = _pred.entered_dates(conn, ids, first, end)

            def _scheduled_covers(aid: int, key: str, amount: int, due: str) -> bool:
                """A reminder for this bill already sits within a few days of
                ``due``. Payee key OR amount: see the module docstring."""
                d_due = _dt.date.fromisoformat(due)
                for s_aid, s_date, s_key, s_amount in sched_marks:
                    if s_aid != aid:
                        continue
                    if abs((d_due - s_date).days) > _pred.ENTERED_WINDOW_DAYS:
                        continue
                    if (s_key and s_key == key) or s_amount == amount:
                        return True
                return False

            for p in _pred.predict_recurring(conn, today, account_ids=ids):
                for due in scheduled.occurrences(p.next_date, p.frequency, first, end):
                    if _pred.is_entered(entered, p.account_id, p.key, due):
                        continue
                    if _scheduled_covers(p.account_id, p.key, p.amount, due):
                        continue
                    out.append(ProjectedEvent(due, p.account_id, p.payee, p.amount,
                                              PREDICTED, automatic=p.automatic,
                                              payee_key=p.key))
    out.sort(key=lambda e: (e.date, e.source != ENTERED, e.account_id, e.payee.lower()))
    return out


def project(conn, account_ids: Iterable[int], start: str, end: str, *,
            include_predictions: bool = True, today: Optional[str] = None) -> Projection:
    """Balance at the end of every day from ``start`` through ``end`` across
    ``account_ids`` (summed), starting from the ledger balance the day before
    ``start`` and applying each day's events. ``low``/``low_date`` mark the
    lowest point, the number a person checks a projection for."""
    ids = [int(a) for a in account_ids]
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
        start, end = end, start
    opening = sum(ledger.account_balance(conn, aid, _day_before(start)) for aid in ids)
    by_day: dict[str, list[ProjectedEvent]] = {}
    for e in projected_events(conn, ids, start, end,
                              include_predictions=include_predictions, today=today):
        by_day.setdefault(e.date, []).append(e)
    days: list[ProjectedDay] = []
    balance = opening
    low, low_date = opening, _day_before(start)
    cur = d0
    while cur <= d1:
        iso = _iso(cur)
        events = by_day.get(iso, [])
        balance += sum(e.amount for e in events)
        days.append(ProjectedDay(iso, balance, events))
        if balance < low:
            low, low_date = balance, iso
        cur += _dt.timedelta(days=1)
    return Projection(account_ids=ids, start=start, end=end, opening=opening,
                      days=days, closing=balance, low=low, low_date=low_date)


def month_range(year: int, month: int) -> tuple[str, str]:
    """``(first, last)`` ISO dates of a calendar month."""
    first = _dt.date(year, month, 1)
    nxt = _dt.date(year + 1, 1, 1) if month == 12 else _dt.date(year, month + 1, 1)
    return _iso(first), _iso(nxt - _dt.timedelta(days=1))
