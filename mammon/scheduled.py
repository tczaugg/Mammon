"""Generalized scheduled/recurring payment definitions and pre-entry generation.

Mammon has always been able to PRE-ENTER upcoming loan payments a few days before
their due date (see :mod:`mammon.loans_schedule`): a placeholder ``scheduled=1``
row in the loan register that the real bank payment later merges into. This
module generalizes that idea to ANY recurring bill -- subscriptions, fixed-price
utilities (cable/internet), memberships, dues -- via the ``scheduled_payments``
table, and backs a single CRUD surface (:mod:`mammon.ui.scheduled_payments_dialog`).

Two things are worth stating loudly, because they surprised at least one user:

  * These pre-entries are MAMMON-GENERATED, not imported. A QIF/OFX/CSV file
    carries only posted transactions; it never carries a "this bill is due next
    month" definition. Mammon creates the placeholder rows itself from the
    definitions stored here (and, for loans, from ``loan_params`` + the
    amortization schedule).

  * Because the DEFINITIONS live only in this table (loan definitions in
    ``loan_params``), and the importers never touch either table, a scheduled
    definition SURVIVES a QIF re-import unchanged -- it is neither duplicated nor
    lost. Re-importing a file only re-inserts/dedups posted transactions; the
    generated ``scheduled=1`` pre-entries are likewise never candidates for
    import dedup (:func:`mammon.importers.core._find_dup_cash` only matches
    ``scheduled=0`` rows), so they survive too.

A definition may also carry a SPLIT TEMPLATE (``scheduled_splits``): a fixed
per-line breakdown learned from a predicted entry's history -- a paycheck's
gross/taxes/deferrals, a bill split across two categories -- that every generated
pre-entry reproduces as real split lines. See :func:`set_scheduled_splits`.

:mod:`mammon.ledger` stays the sole writer of transaction/split rows; this module
owns only the ``scheduled_payments`` and ``scheduled_splits`` tables and
orchestrates ledger for the generated pre-entries (the split lines on a pre-entry
are still written through :func:`ledger.set_splits`, never here).
"""
from __future__ import annotations

from datetime import date as _date, timedelta
from typing import Optional

from mammon import ledger

# Recurrence intervals -> how to advance a date by one period. Monthly/quarterly/
# semiannual/annual advance by whole months (the day is clamped to the target
# month's last day, e.g. Jan 31 -> Feb 28); weekly/biweekly advance by days;
# semimonthly alternates the 1st/16th-style pair (see _advance_semimonthly).
# Keys are the lower-cased values stored in ``scheduled_payments.frequency``.
# The set matches what a loan may use (loans._INTERVALS) so the two lists no
# longer disagree about what "twice a year" is.
_MONTHLY = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12, "yearly": 12}
_DAILY = {"weekly": 7, "biweekly": 14, "fortnightly": 14}
_SEMIMONTHLY = "semimonthly"

# The recurrence choices the manager UI offers (order = display order).
FREQUENCIES = ("weekly", "biweekly", "semimonthly", "monthly", "quarterly",
               "semiannual", "annual")

# How many days ahead of a due date the generator pre-enters a payment, and how
# far ahead a reminder counts as "due soon", unless a definition says otherwise.
DEFAULT_LEAD_DAYS = 5

# Reminder statuses, in urgency order.
OVERDUE, DUE_TODAY, DUE_SOON, UPCOMING = "overdue", "due_today", "due_soon", "upcoming"


def _valid_frequency(frequency: str) -> str:
    f = str(frequency).lower()
    if f not in _MONTHLY and f not in _DAILY and f != _SEMIMONTHLY:
        raise ValueError(f"unknown frequency {frequency!r}")
    return f


def _iso_to_date(s: str) -> _date:
    y, m, d = (int(x) for x in s.split("-"))
    return _date(y, m, d)


def _add_months(s: str, months: int) -> str:
    d = _iso_to_date(s)
    m0 = d.month - 1 + months
    year = d.year + m0 // 12
    month = m0 % 12 + 1
    # clamp the day to the last day of the target month (Jan 31 + 1mo -> Feb 28)
    next_first = _date(year + 1, 1, 1) if month == 12 else _date(year, month + 1, 1)
    last_day = (next_first - timedelta(days=1)).day
    return _date(year, month, min(d.day, last_day)).isoformat()


def _last_day(year: int, month: int) -> int:
    next_first = _date(year + 1, 1, 1) if month == 12 else _date(year, month + 1, 1)
    return (next_first - timedelta(days=1)).day


def _advance_semimonthly(s: str) -> str:
    """Twice a month, Quicken-style: a date on or before the 15th pairs with the
    same day fifteen later (the 1st with the 16th, the 10th with the 25th), and
    the 15th pairs with the LAST day of the month -- the "15th and last day"
    paycheck. A date after the 15th steps to next month's first half; the last
    day of a month steps to next month's 15th. (Advancing by a flat fifteen days
    would drift through the calendar within a few months.)"""
    d = _iso_to_date(s)
    last = _last_day(d.year, d.month)
    if d.day <= 15:
        target = last if d.day == 15 else min(d.day + 15, last)
        return _date(d.year, d.month, target).isoformat()
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    day = 15 if d.day == last else d.day - 15
    return _date(y, m, min(day, _last_day(y, m))).isoformat()


def advance_date(s: str, frequency: str) -> str:
    """The date one ``frequency`` period after ISO date ``s``."""
    f = _valid_frequency(frequency)
    if f in _MONTHLY:
        return _add_months(s, _MONTHLY[f])
    if f == _SEMIMONTHLY:
        return _advance_semimonthly(s)
    return (_iso_to_date(s) + timedelta(days=_DAILY[f])).isoformat()


def occurrences(next_date: str, frequency: str, start: str, end: str, *,
                limit: int = 600) -> list[str]:
    """Every due date of a definition falling in ``[start, end]``, walking
    forward from ``next_date`` (occurrences before ``start`` are skipped). A
    definition whose ``next_date`` is already past ``end`` yields nothing."""
    out, due, guard = [], next_date, 0
    while due <= end and guard < limit:
        if due >= start:
            out.append(due)
        due = advance_date(due, frequency)
        guard += 1
    return out


def days_until(next_date: str, today: str) -> int:
    """Signed days from ``today`` to ``next_date`` (negative = overdue)."""
    return (_iso_to_date(next_date) - _iso_to_date(today)).days


def reminder_status(next_date: str, today: str, lead_days: int = DEFAULT_LEAD_DAYS) -> str:
    """Quicken's reminder states: ``overdue`` (past), ``due_today``,
    ``due_soon`` (within ``lead_days``) or ``upcoming``."""
    n = days_until(next_date, today)
    if n < 0:
        return OVERDUE
    if n == 0:
        return DUE_TODAY
    if n <= int(lead_days):
        return DUE_SOON
    return UPCOMING


# ---------------------------------------------------------------------------
# CRUD over the definitions table
# ---------------------------------------------------------------------------
def add_scheduled(conn, account_id: int, *, payee: Optional[str], amount: int,
                  frequency: str, next_date: str,
                  category_id: Optional[int] = None,
                  memo: Optional[str] = None, active: bool = True,
                  transfer_account_id: Optional[int] = None,
                  lead_days: Optional[int] = None,
                  auto_enter: bool = True,
                  splits: Optional[list] = None) -> int:
    """Insert a scheduled-payment definition. Returns its id.

    A definition is a BILL (negative amount), INCOME (positive) or, with
    ``transfer_account_id``, a TRANSFER: money moves between ``account_id`` and
    that account in the direction the sign says (negative = out of
    ``account_id``), and the pre-entry gets its mirror like any transfer.
    ``lead_days`` overrides the default lead for this definition;
    ``auto_enter=False`` makes it remind-only (never pre-entered).

    ``splits`` is an optional SPLIT TEMPLATE -- a list of split-line dicts
    (``category_id``/``transfer_account_id``/``amount``/``memo``, the shape
    :func:`ledger.get_splits` returns) LEARNED from a predicted entry's history,
    so a recurring paycheck or split bill reproduces its breakdown on every
    pre-entry (see :func:`set_scheduled_splits`). Ignored for a transfer
    definition, which is a single whole-transaction move."""
    freq = _valid_frequency(frequency)
    _iso_to_date(next_date)                       # validate ISO shape (raises)
    if transfer_account_id is not None and int(transfer_account_id) == int(account_id):
        raise ValueError("a transfer needs a different account on the other side")
    cur = conn.execute(
        "INSERT INTO scheduled_payments"
        "(account_id, payee, amount, frequency, next_date, category_id, memo, active,"
        " transfer_account_id, lead_days, auto_enter)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(account_id), payee, int(amount), freq, next_date,
         None if transfer_account_id is not None else category_id, memo,
         1 if active else 0,
         None if transfer_account_id is None else int(transfer_account_id),
         None if lead_days is None else int(lead_days),
         1 if auto_enter else 0),
    )
    sid = cur.lastrowid
    if transfer_account_id is None and splits:
        set_scheduled_splits(conn, sid, splits)
    conn.commit()
    return sid


_EDITABLE = {"account_id", "payee", "amount", "frequency", "next_date",
             "category_id", "memo", "active", "transfer_account_id", "lead_days",
             "auto_enter"}


def update_scheduled(conn, sid: int, **fields) -> None:
    """Update selected columns of a definition. Unknown fields raise."""
    bad = set(fields) - _EDITABLE
    if bad:
        raise ValueError(f"unknown scheduled_payments field(s): {sorted(bad)}")
    if "frequency" in fields:
        fields["frequency"] = _valid_frequency(fields["frequency"])
    if "next_date" in fields:
        _iso_to_date(fields["next_date"])
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0
    if "auto_enter" in fields:
        fields["auto_enter"] = 1 if fields["auto_enter"] else 0
    if "amount" in fields:
        fields["amount"] = int(fields["amount"])
    if "lead_days" in fields and fields["lead_days"] is not None:
        fields["lead_days"] = int(fields["lead_days"])
    if fields.get("transfer_account_id") is not None:
        fields["transfer_account_id"] = int(fields["transfer_account_id"])
        fields["category_id"] = None          # a transfer's category is the account
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE scheduled_payments SET {cols} WHERE id=?",
                 (*fields.values(), sid))
    conn.commit()


def delete_scheduled(conn, sid: int) -> None:
    conn.execute("DELETE FROM scheduled_payments WHERE id=?", (sid,))
    conn.commit()


# ---------------------------------------------------------------------------
# The split template: a definition's learned per-line breakdown
# ---------------------------------------------------------------------------
# A recurring payment is often not one category but a fixed split (a paycheck's
# gross/taxes/deferrals; a utility bill's electric+water). When a definition is
# created from a PREDICTED calendar entry whose history is split, the calendar
# learns that split (the same lines the register's "Copy from previous <payee>
# split" copies, via ledger.previous_split_for_payee) and stores it here as the
# definition's TEMPLATE. Every generated pre-entry then reproduces the breakdown
# as real split lines -- written, like all split rows, only through
# ledger.set_splits. These rows are pure definition data owned by this module,
# so ledger stays the sole writer of transaction/split rows.
def get_scheduled_splits(conn, sid: int) -> list:
    """The definition's stored split template as split-line dicts (the shape
    :func:`ledger.get_splits` / :func:`ledger.set_splits` take): ``category_id``
    or ``transfer_account_id``, signed ``amount`` cents, and ``memo``. Empty
    when the definition carries no split."""
    rows = conn.execute(
        "SELECT category_id, transfer_account_id, amount, memo "
        "FROM scheduled_splits WHERE scheduled_id=? ORDER BY id", (sid,)).fetchall()
    return [{"category_id": r["category_id"],
             "transfer_account_id": r["transfer_account_id"],
             "amount": int(r["amount"]),
             "memo": r["memo"] or ""} for r in rows]


def set_scheduled_splits(conn, sid: int, splits: Optional[list]) -> None:
    """Replace the definition's split template. ``splits`` is an iterable of
    split-line dicts (``category_id``/``transfer_account_id``/``amount``/``memo``,
    the shape :func:`ledger.get_splits` returns); a leg carrying a
    ``transfer_account_id`` reproduces as a transfer split leg. A template needs
    at least two lines to mean anything (one line is just a plain category), so
    fewer than two clears it. Does NOT commit (the caller does)."""
    conn.execute("DELETE FROM scheduled_splits WHERE scheduled_id=?", (sid,))
    lines = [ln for ln in (splits or []) if ln.get("amount") is not None]
    if len(lines) < 2:
        return
    for ln in lines:
        cat = ln.get("category_id")
        taid = ln.get("transfer_account_id")
        memo = (str(ln.get("memo")).strip() or None) if ln.get("memo") else None
        cat = None if cat in (None, "") else int(cat)
        taid = None if taid in (None, "") else int(taid)
        if taid is not None:
            cat = None                        # a transfer leg carries no category
        conn.execute(
            "INSERT INTO scheduled_splits(scheduled_id, category_id, "
            "transfer_account_id, amount, memo) VALUES (?,?,?,?,?)",
            (int(sid), cat, taid, int(ln["amount"]), memo))


def _category_labels(conn) -> dict:
    return {c["id"]: c["path"]
            for c in ledger.list_categories(conn, include_hidden=True)}


def _col(row, key, default=None):
    return row[key] if key in row.keys() else default


def _row_to_def(conn, row, cat_labels: Optional[dict] = None) -> dict:
    acct = ledger.get_account(conn, row["account_id"])
    if cat_labels is None:
        cat_labels = _category_labels(conn)
    taid = _col(row, "transfer_account_id")
    other = ledger.get_account(conn, taid) if taid is not None else None
    if taid is not None:
        kind = "transfer"
        label = f"[{other['name']}]" if other is not None else "[Transfer]"
    else:
        kind = "income" if row["amount"] > 0 else "bill"
        label = cat_labels.get(row["category_id"])
    return {
        "id": row["id"],
        "source": "manual",
        "kind": kind,
        "account_id": row["account_id"],
        "account_name": acct["name"] if acct is not None else None,
        "payee": row["payee"],
        "amount": row["amount"],
        "frequency": row["frequency"],
        "next_date": row["next_date"],
        "category_id": row["category_id"],
        "category_label": label,
        "transfer_account_id": taid,
        "transfer_account_name": other["name"] if other is not None else None,
        "memo": row["memo"],
        "active": bool(row["active"]),
        "lead_days": _col(row, "lead_days"),
        "auto_enter": bool(_col(row, "auto_enter", 1)),
    }


def get_scheduled(conn, sid: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM scheduled_payments WHERE id=?", (sid,)).fetchone()
    return _row_to_def(conn, row) if row is not None else None


def list_scheduled(conn, *, active_only: bool = False) -> list:
    """Every manual scheduled-payment definition as a dict, ordered by next
    date. ``source`` is ``'manual'`` (vs the ``'loan'`` rows from
    :func:`list_loan_schedules`)."""
    sql = "SELECT * FROM scheduled_payments"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY next_date, id"
    rows = conn.execute(sql).fetchall()
    labels = _category_labels(conn)
    return [_row_to_def(conn, r, labels) for r in rows]


# ---------------------------------------------------------------------------
# Loan schedules surfaced as read-only rows (so the manager shows them too)
# ---------------------------------------------------------------------------
def list_loan_schedules(conn, on_or_after: Optional[str] = None) -> list:
    """Every configured loan's payment schedule surfaced as a scheduled-payment
    row, so the manager shows loan payments alongside the manual definitions.
    The DEFINITION lives in ``loan_params`` (edited via Loan Setup, removed by
    ``loans.delete_loan_params``); these rows are derived on the fly from the
    amortization schedule, and ``next_date`` is the first period on/after
    ``on_or_after`` that no register holds yet (``loans_schedule.next_due_date``)
    -- the loan analogue of a manual row's stored ``next_date``, advanced by
    Enter/Generate and rolled back by deleting the payment. ``source`` is
    ``'loan'`` and ``id`` is the loan account id."""
    from mammon import loans, loans_schedule
    out = []
    rows = conn.execute(
        "SELECT account_id FROM loan_params ORDER BY account_id").fetchall()
    for r in rows:
        aid = r["account_id"]
        try:
            lp = loans.get_loan_params(conn, aid)
            if lp is None:
                continue
            acct = ledger.get_account(conn, aid)
            name = acct["name"] if acct is not None else "Loan"
            if on_or_after is not None:
                next_date = loans_schedule.next_due_date(conn, aid, on_or_after)
            else:
                sched = loans.amortization_schedule(conn, aid)
                next_date = sched[0].date if sched else None
        except Exception:
            # A partially-configured loan should never break the manager list.
            continue
        funder = loans.funding_account(conn, aid)
        funder_name = None
        if funder is not None:
            fa = ledger.get_account(conn, funder)
            funder_name = fa["name"] if fa is not None else None
        payee = (loans.last_payment_payee(conn, aid, funder) if funder is not None
                 else None) or f"{name} Payment"
        out.append({
            "id": aid,
            "source": "loan",
            "kind": "loan",
            "account_id": aid,
            "account_name": f"{funder_name} → {name}" if funder_name else name,
            "funding_account_id": funder,
            "funding_account_name": funder_name,
            "payee": payee,
            "amount": lp.payment_amount,
            "frequency": lp.interval,
            "next_date": next_date,
            "category_id": None,
            "category_label": "(loan split: principal/interest/escrow)",
            "transfer_account_id": None,
            "transfer_account_name": None,
            "memo": "Managed via Loan Setup",
            "active": True,
            "lead_days": None,
            "auto_enter": True,
        })
    return out


# ---------------------------------------------------------------------------
# Reminders: the definitions with a due status attached
# ---------------------------------------------------------------------------
def list_reminders(conn, today: str, *, default_lead: int = DEFAULT_LEAD_DAYS) -> list:
    """Every active definition (manual and loan) as a reminder row: the
    definition dict plus ``status`` (overdue / due_today / due_soon /
    upcoming), ``days_until`` and the ``lead`` in force. Most urgent first."""
    rows = [d for d in list_scheduled(conn, active_only=True) if d["next_date"]]
    rows += [d for d in list_loan_schedules(conn, on_or_after=today) if d["next_date"]]
    out = []
    for d in rows:
        lead = d["lead_days"] if d.get("lead_days") is not None else int(default_lead)
        r = dict(d)
        r["lead"] = lead
        r["days_until"] = days_until(d["next_date"], today)
        r["status"] = reminder_status(d["next_date"], today, lead)
        out.append(r)
    out.sort(key=lambda r: (r["next_date"], r["source"], r["payee"] or ""))
    return out


def due_counts(conn, today: str, *, default_lead: int = DEFAULT_LEAD_DAYS) -> dict:
    """``{"overdue": n, "due_today": n, "due_soon": n, "upcoming": n}`` --
    what the menu label and a startup notice summarise."""
    counts = {OVERDUE: 0, DUE_TODAY: 0, DUE_SOON: 0, UPCOMING: 0}
    for r in list_reminders(conn, today, default_lead=default_lead):
        counts[r["status"]] += 1
    return counts


def skip_next(conn, sid: int) -> str:
    """Skip one occurrence of a manual definition: advance ``next_date`` by a
    period without entering anything. Returns the new next date."""
    d = get_scheduled(conn, sid)
    if d is None:
        raise LookupError(f"no scheduled payment {sid}")
    nxt = advance_date(d["next_date"], d["frequency"])
    update_scheduled(conn, sid, next_date=nxt)
    return nxt


def enter_next(conn, sid: int, *, date: Optional[str] = None,
               placeholder: bool = False) -> int:
    """Quicken's Enter: record the next occurrence of a manual definition as a
    POSTED transaction (or a ``scheduled=1`` placeholder when ``placeholder``)
    dated ``date`` (default: its next date), and advance ``next_date`` one
    period. Returns the transaction id on the definition's own account."""
    d = get_scheduled(conn, sid)
    if d is None:
        raise LookupError(f"no scheduled payment {sid}")
    tid = create_pending_from_definition(conn, sid, date or d["next_date"],
                                         placeholder=placeholder)
    update_scheduled(conn, sid, next_date=advance_date(d["next_date"], d["frequency"]))
    return tid


# ---------------------------------------------------------------------------
# Pre-entry generation
# ---------------------------------------------------------------------------
def create_pending_from_definition(conn, sid: int,
                                   due_date: Optional[str] = None, *,
                                   placeholder: bool = True) -> int:
    """Pre-enter a pending (``scheduled=1``) transaction for definition ``sid``
    dated ``due_date`` (its stored ``next_date`` when omitted); with
    ``placeholder=False`` the row is POSTED (Quicken's Enter). A transfer
    definition creates both legs through :func:`ledger.create_transfer` and
    marks each a placeholder when asked. Idempotent for placeholders: a
    pending pre-entry already on this account+date+amount is returned
    unchanged. Returns the transaction id on the definition's own account."""
    d = get_scheduled(conn, sid)
    if d is None:
        raise LookupError(f"no scheduled payment {sid}")
    due_date = due_date or d["next_date"]
    if placeholder:
        existing = conn.execute(
            "SELECT id FROM transactions "
            "WHERE account_id=? AND scheduled=1 AND date=? AND amount=?",
            (d["account_id"], due_date, d["amount"]),
        ).fetchone()
        if existing is not None:
            return existing["id"]
    payee = d["payee"] or "Scheduled payment"
    memo = d["memo"] or "Scheduled payment"
    taid = d.get("transfer_account_id")
    if taid is not None and d["amount"] != 0:
        if d["amount"] < 0:
            own, other = ledger.create_transfer(conn, d["account_id"], taid, due_date,
                                                -d["amount"], memo=memo, payee=payee)
        else:
            other, own = ledger.create_transfer(conn, taid, d["account_id"], due_date,
                                                d["amount"], memo=memo, payee=payee)
        if placeholder:
            for tid in (own, other):
                ledger.update_transaction(conn, tid, scheduled=1)
        return own
    tid = ledger.add_transaction(
        conn, d["account_id"], due_date, d["amount"],
        payee=payee, category_id=d["category_id"], memo=memo,
        scheduled=1 if placeholder else 0, cleared=0,
    )
    # Reproduce the definition's learned split (a paycheck's breakdown, a split
    # bill) as real split lines -- through ledger, the sole writer of split rows.
    # set_splits reconciles the lines to this pre-entry's own total, so a
    # varying bill still balances (any drift lands in an uncategorized line).
    template = get_scheduled_splits(conn, sid)
    if len(template) >= 2:
        ledger.set_splits(conn, tid, template)
    return tid


# How many days apart a downloaded row and a pending pre-entry may be and still
# be the same payment (a bill pre-entered five days ahead can post a few late).
PLACEHOLDER_MATCH_WINDOW_DAYS = 7


def _shift_days(s: str, n: int) -> str:
    return (_iso_to_date(s) + timedelta(days=n)).isoformat()


def pending_placeholders(conn, account_id: int, date: str,
                         day_window: int = PLACEHOLDER_MATCH_WINDOW_DAYS) -> list:
    """Every pending pre-entry from a definition on ``account_id`` within
    ``day_window`` days of ``date`` -- plain rows and transfer legs, never split
    placeholders -- as rows of ``id, date, amount, payee``, nearest first. The
    importer falls back to these when no placeholder matches the amount
    exactly: a bill whose amount moves (utilities) is matched on its payee
    instead, adopting the amount, rather than filed as a duplicate beside a
    placeholder that then stands forever."""
    rows = conn.execute(
        "SELECT id, date, amount, payee FROM transactions WHERE account_id=? "
        "AND scheduled=1 AND date BETWEEN ? AND ? "
        "AND id NOT IN (SELECT transaction_id FROM splits) ORDER BY date, id",
        (account_id, _shift_days(date, -day_window), _shift_days(date, day_window))
    ).fetchall()
    return sorted(rows, key=lambda r: abs(
        (_iso_to_date(r["date"]) - _iso_to_date(date)).days))


def find_matching_placeholder(conn, account_id: int, date: str, amount_cents: int,
                              day_window: int = PLACEHOLDER_MATCH_WINDOW_DAYS) -> Optional[int]:
    """A pending pre-entry from a definition on ``account_id`` -- a plain bill
    or income row, or a transfer leg -- with exactly ``amount_cents``, within
    ``day_window`` days of ``date``; the closest by date, or ``None``. A split
    placeholder is not one of these (loans_schedule handles those)."""
    rows = conn.execute(
        "SELECT id, date FROM transactions WHERE account_id=? AND scheduled=1 "
        "AND amount=? AND id NOT IN (SELECT transaction_id FROM splits)",
        (account_id, amount_cents)).fetchall()
    best, best_diff = None, None
    for row in rows:
        diff = abs((_iso_to_date(row["date"]) - _iso_to_date(date)).days)
        if diff <= day_window and (best_diff is None or diff < best_diff):
            best, best_diff = int(row["id"]), diff
    return best


def merge_import_into_placeholder(conn, pending_id: int, *, date: str,
                                  amount_cents: int, fitid: Optional[str] = None,
                                  import_id: Optional[int] = None) -> int:
    """Post an imported row INTO a pending pre-entry: adopt the actual date and
    amount, stamp fitid/import_id, mark it cleared and no longer pending. The
    pre-entry keeps its own payee and category -- the definition's clean ones
    -- which is the point of pre-entering. A transfer placeholder's mirror leg
    stops being pending too (the amount and date already mirror)."""
    txn = ledger.get_transaction(conn, pending_id)
    if txn is None:
        raise KeyError(f"no transaction {pending_id}")
    fields = dict(date=date, amount=amount_cents, cleared=1, scheduled=0)
    if fitid:
        fields["fitid"] = fitid
    if import_id is not None:
        fields["import_id"] = import_id
    ledger.update_transaction(conn, pending_id, **fields)
    if txn["transfer_pair_id"] is not None:
        ledger.update_transaction(conn, txn["transfer_pair_id"], scheduled=0)
    return pending_id


def ensure_due_pre_entries(conn, sid: int, as_of_date: str, *,
                           lead_days: int = DEFAULT_LEAD_DAYS) -> list:
    """Create pending pre-entries for definition ``sid`` for every occurrence due
    on/before ``as_of_date + lead``, advancing the stored ``next_date`` past
    the horizon. The lead is the definition's own ``lead_days`` when set, else
    ``lead_days``. A remind-only definition (``auto_enter`` off) is never
    pre-entered here: it waits for the user's Enter or Skip. Returns the
    created/existing pre-entry transaction ids. Idempotent (re-running over the
    same window makes no new rows)."""
    d = get_scheduled(conn, sid)
    if d is None or not d["active"] or not d.get("auto_enter", True):
        return []
    lead = d["lead_days"] if d.get("lead_days") is not None else lead_days
    horizon = (_iso_to_date(as_of_date) + timedelta(days=int(lead))).isoformat()
    out, due = [], d["next_date"]
    guard = 0
    while due <= horizon and guard < 600:       # 600 = safety stop (~ >1 yr weekly)
        out.append(create_pending_from_definition(conn, sid, due))
        due = advance_date(due, d["frequency"])
        guard += 1
    if due != d["next_date"]:
        update_scheduled(conn, sid, next_date=due)
    return out


# ---------------------------------------------------------------------------
# Suggestions from history
# ---------------------------------------------------------------------------
# (label, days per period, tolerance in days): the intervals a run of dates is
# tested against, and how far one gap may stray from it (a monthly bill posts
# on the 1st one month and the 3rd the next; a payday is every other Friday).
_INTERVAL_TESTS = (("weekly", 7, 2), ("biweekly", 14, 3), ("monthly", 30.44, 6),
                   ("quarterly", 91.3, 12), ("semiannual", 182.6, 20),
                   ("annual", 365.25, 30))


def _classify_gaps(gaps: list) -> Optional[str]:
    """The recurrence a run of day-gaps fits, or None: the interval nearest
    the median gap, provided at least three quarters of the gaps sit within
    its tolerance."""
    if not gaps:
        return None
    s = sorted(gaps)
    median = s[len(s) // 2]
    label, days, tol = min(_INTERVAL_TESTS, key=lambda t: abs(median - t[1]))
    if abs(median - days) > tol:
        return None
    fits = sum(1 for g in gaps if abs(g - days) <= tol)
    return label if fits * 4 >= len(gaps) * 3 else None


def _payee_key(payee) -> str:
    return "".join(ch for ch in (payee or "").lower() if ch.isalnum())


def suggest_recurring(conn, today: str, *, months_back: int = 26,
                      min_count: int = 3) -> list:
    """Regular payments and income found in history that no definition covers
    yet: per account and payee, at least ``min_count`` posted rows over the
    last ``months_back`` months whose spacing fits one interval and whose
    amounts stay within a third of the latest one. Transfers, split parents (a
    loan payment belongs to the loan schedule) and pending rows are left out,
    as is any payee an active definition already names on that account.

    Each suggestion is the shape ``add_scheduled`` takes -- account_id, payee,
    amount (the latest), frequency, next_date (the last date advanced past
    today), category_id (the most common) -- plus ``count``, ``last_date`` and
    whether the amount ``varies``. This is how the projection learns what is
    regular: it never guesses on its own, and a suggestion becomes a
    definition only when the user accepts it in the manager."""
    since = (_iso_to_date(today) - timedelta(days=int(months_back * 30.44))).isoformat()
    rows = conn.execute(
        "SELECT account_id, date, amount, payee, category_id FROM transactions "
        "WHERE scheduled=0 AND date>=? AND date<=? AND payee IS NOT NULL AND payee<>'' "
        "AND transfer_account_id IS NULL "
        "AND id NOT IN (SELECT transaction_id FROM splits) "
        "ORDER BY date, id", (since, today)).fetchall()
    groups: dict = {}
    for r in rows:
        groups.setdefault((int(r["account_id"]), _payee_key(r["payee"])), []).append(r)
    covered = {(int(d["account_id"]), _payee_key(d["payee"]))
               for d in list_scheduled(conn, active_only=True)}
    names = {int(a["id"]): a["name"] for a in ledger.list_accounts(
        conn, include_closed=True, include_hidden=True)}
    out = []
    for (aid, key), items in groups.items():
        if len(key) < 3 or (aid, key) in covered:
            continue
        by_day: dict = {}                 # one row per day: two hits in a day are one period
        for r in items:
            by_day[r["date"]] = r
        items = [by_day[d] for d in sorted(by_day)]
        if len(items) < min_count:
            continue
        dates = [r["date"] for r in items]
        freq = _classify_gaps([days_until(b, a) for a, b in zip(dates, dates[1:])])
        if freq is None:
            continue
        latest = int(items[-1]["amount"])
        amounts = [int(r["amount"]) for r in items]
        if latest == 0 or any((a < 0) != (latest < 0) for a in amounts):
            continue
        spread = max(abs(a - latest) for a in amounts)
        if spread > abs(latest) // 3:
            continue
        nxt, guard = advance_date(dates[-1], freq), 0
        while nxt < today and guard < 400:
            nxt, guard = advance_date(nxt, freq), guard + 1
        cats = [r["category_id"] for r in items if r["category_id"] is not None]
        out.append({
            "account_id": aid, "account_name": names.get(aid, ""),
            "payee": items[-1]["payee"], "amount": latest, "varies": spread > 0,
            "frequency": freq, "next_date": nxt, "last_date": dates[-1],
            "count": len(items),
            "category_id": max(set(cats), key=cats.count) if cats else None,
        })
    out.sort(key=lambda s: (s["next_date"], s["account_name"], s["payee"].lower()))
    return out


def generate_all_due(conn, as_of_date: str, *,
                     lead_days: int = DEFAULT_LEAD_DAYS) -> list:
    """Ensure pre-entries for every active manual definition AND every configured
    loan (via :mod:`mammon.loans_schedule`). Returns all pre-entry txn ids created
    or already present. This is what the manager's "Generate pre-entries" button
    calls; it is idempotent and safe to run repeatedly."""
    out = []
    for d in list_scheduled(conn, active_only=True):
        out.extend(ensure_due_pre_entries(conn, d["id"], as_of_date,
                                          lead_days=lead_days))
    from mammon import loans_schedule
    rows = conn.execute("SELECT account_id FROM loan_params").fetchall()
    for r in rows:
        try:
            out.extend(loans_schedule.ensure_pending_payments(
                conn, r["account_id"], as_of_date, lead_days=lead_days))
        except Exception:
            continue
    return out
