"""Scheduled loan-payment pre-entries and import matching (Task 55).

A few days before each due date we PRE-ENTER the upcoming loan payment as a
*pending* split (principal + interest + escrow, straight from the amortization
schedule) in the loan register, so the register shows what is coming and
already carries the correct category breakdown before the bank statement
arrives. A pending row is marked ``scheduled=1`` -- a not-yet-posted
placeholder.

When the real payment later imports (Task 52 splits a lumped loan payment in
the loan account), the importer first MATCHES it to a pending pre-entry by
amount + date window and MERGES the two -- it posts into the placeholder row
(fills fitid/import_id, flips ``scheduled`` to 0, marks it cleared, and
re-applies the split for the actual amount) instead of inserting a duplicate.
An unmatched (or first) payment still imports cleanly as a normal new row.

``mammon.ledger`` remains the sole writer of transaction/split rows; this module
only orchestrates it and the pure-domain split from ``mammon.loans``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from typing import Optional

from mammon import ledger, loans

# Category paths for the two legs the loan's own parameters do not name. These
# MUST mirror mammon.importers.core.LOAN_INTEREST_CATEGORY / LOAN_PRINCIPAL_CATEGORY
# so a pre-entry and the eventual import produce the SAME split (each escrow/PMI
# extra keeps its own configured category).
INTEREST_CATEGORY = "Interest Exp"
PRINCIPAL_CATEGORY = "Principal"

# How close (days) an imported payment's date must be to a pending pre-entry's
# due date to be considered the same payment.
DEFAULT_MATCH_WINDOW_DAYS = 7
# How many days ahead of a due date pre-entries are created by default.
DEFAULT_LEAD_DAYS = 5


# ---------------------------------------------------------------------------
# small date helpers (dates are ISO YYYY-MM-DD strings everywhere)
# ---------------------------------------------------------------------------
def _iso_to_date(s: str) -> _date:
    y, m, d = (int(x) for x in s.split("-"))
    return _date(y, m, d)


def _days_between(a: str, b: str) -> int:
    return abs((_iso_to_date(a) - _iso_to_date(b)).days)


def _shift_days(s: str, n: int) -> str:
    from datetime import timedelta
    return (_iso_to_date(s) + timedelta(days=n)).isoformat()


def _split_legs(split, interest_category: Optional[str] = None) -> list[tuple]:
    """(category_path, amount_cents, memo) legs for a PaymentSplit, in the same
    order the importer produces: interest, each configured extra, then
    principal. Sum == the payment amount by construction.

    The interest leg posts to the loan's own ``interest_category`` when set,
    else the module-default ``INTEREST_CATEGORY`` (preserving prior behavior)."""
    legs: list[tuple] = []
    if split.interest:
        legs.append((interest_category or INTEREST_CATEGORY,
                     split.interest, "Interest"))
    for ex in split.extras:
        if ex.amount:
            legs.append((ex.category, ex.amount, ex.label or ex.category))
    legs.append((PRINCIPAL_CATEGORY, split.principal, "Principal"))
    return legs


def _apply_split(conn, txn_id: int, legs: list[tuple]) -> None:
    lines = [(ledger.resolve_category(conn, cat), amt, memo)
             for cat, amt, memo in legs]
    ledger.set_splits(conn, txn_id, lines)


# ---------------------------------------------------------------------------
# schedule lookahead
# ---------------------------------------------------------------------------
def upcoming_due_dates(conn, loan_account_id: int, on_or_after: str,
                       count: int = 1) -> list:
    """The next ``count`` amortization rows whose date is on/after
    ``on_or_after`` (each a loans.ScheduleRow, so callers can read .date and the
    per-period principal/interest/escrow)."""
    sched = loans.amortization_schedule(conn, loan_account_id)
    return [row for row in sched if row.date >= on_or_after][:count]


def _nearest_payment(conn, date: str, day_window: int, sql: str,
                     params: tuple) -> Optional[int]:
    """Run ``sql`` (yielding id, date, scheduled) and pick the row nearest
    ``date`` within ``day_window`` days -- a still-pending row before a posted
    one at equal distance, then the lowest id -- or ``None``."""
    best = None
    for row in conn.execute(sql, params).fetchall():
        key = (abs(_days_between(row["date"], date)),
               -int(row["scheduled"] or 0), int(row["id"]))
        if key[0] <= day_window and (best is None or key < best):
            best = key
    return best[2] if best is not None else None


def _payment_on_loan(conn, loan_account_id: int, date: str,
                     day_window: int = DEFAULT_MATCH_WINDOW_DAYS) -> Optional[int]:
    """The id of the payment row for ``date`` on the loan's OWN register -- a
    still-pending pre-entry, a split-posted payment, or a bare principal/
    transfer leg such as an imported loan-payment leg -- dated within
    ``day_window`` days of it (see payment_on_funder for why not exact), or
    ``None``."""
    return _nearest_payment(
        conn, date, day_window,
        "SELECT id, date, scheduled FROM transactions "
        "WHERE account_id=? AND date BETWEEN ? AND ? "
        "  AND (scheduled=1 "
        "       OR transfer_account_id IS NOT NULL "
        "       OR id IN (SELECT transaction_id FROM splits))",
        (loan_account_id, _shift_days(date, -day_window),
         _shift_days(date, day_window)))


def payment_for(conn, loan_account_id: int, date: str,
                day_window: int = DEFAULT_MATCH_WINDOW_DAYS) -> Optional[int]:
    """The id of the payment already carrying this loan's ``date`` -- pending
    or posted, from WHICHEVER account paid it (the funder, or a one-off from
    a card because checking was low) or on the loan's own register -- or
    ``None``. The one answer to "is this period entered?": a payment counts
    for the period wherever it was made from, so nothing pre-enters, projects
    or reminds about it a second time."""
    hit = _nearest_payment(
        conn, date, day_window,
        "SELECT t.id, t.date, t.scheduled FROM transactions t "
        "WHERE t.account_id<>? AND t.date BETWEEN ? AND ? AND ("
        "  t.transfer_account_id=? OR t.id IN ("
        "    SELECT transaction_id FROM splits WHERE transfer_account_id=?))",
        (loan_account_id, _shift_days(date, -day_window), _shift_days(date, day_window),
         loan_account_id, loan_account_id))
    if hit is not None:
        return hit
    return _payment_on_loan(conn, loan_account_id, date, day_window)


def next_due_date(conn, loan_account_id: int, on_or_after: str) -> Optional[str]:
    """The first amortization date on/after ``on_or_after`` that no register
    carries yet. The Scheduled Payments manager shows this as a loan's next
    date, so entering or pre-entering a payment moves the row on -- the way a
    manual definition's stored ``next_date`` advances -- and deleting that
    payment moves it back, instead of the row offering the same period again
    or silently skipping one."""
    for row in loans.amortization_schedule(conn, loan_account_id):
        if row.date >= on_or_after and payment_for(conn, loan_account_id, row.date) is None:
            return row.date
    return None


# ---------------------------------------------------------------------------
# pre-entry creation
# ---------------------------------------------------------------------------
def create_pending_payment(conn, loan_account_id: int, due_date: str, *,
                           amount_cents: Optional[int] = None,
                           payee: Optional[str] = None) -> int:
    """Pre-enter a pending split payment dated ``due_date`` -- on the account
    that pays the loan, in the shape its real payments have (see
    _new_pending_on_funder), or on the loan's own register when no funder is
    known. Idempotent: a payment already carrying this period is returned
    unchanged. Returns the transaction id."""
    lp = loans.get_loan_params(conn, loan_account_id)
    if lp is None:
        raise LookupError(f"account {loan_account_id} has no loan parameters")

    # Idempotent / clear-before-regenerate guard: never pre-enter a payment for a
    # due date that ALREADY carries one. That covers not only a still-pending
    # pre-entry (scheduled=1) but an already-POSTED payment for this period -- a
    # split-posted payment (has splits) or a bare principal/transfer leg
    # (transfer_account_id set, e.g. an imported loan-payment leg). Guarding only
    # on scheduled=1 (the old check) let a re-run of generation after the payment
    # posted (merge flips scheduled -> 0), or after a recast re-materialised the
    # schedule, insert a SECOND row on the loan side while the checking leg stayed
    # single -- the doubled-payment regression. A still-pending pre-entry, when
    # present, wins (its id is returned) so the import merge still lands on it.
    existing = payment_for(conn, loan_account_id, due_date)
    if existing is not None:
        return existing
    return _new_pending_payment(conn, loan_account_id, due_date,
                                amount_cents=amount_cents, payee=payee)


def _new_pending_payment(conn, loan_account_id: int, due_date: str, *,
                         amount_cents: Optional[int] = None,
                         payee: Optional[str] = None,
                         memo: Optional[str] = "Scheduled payment",
                         funding_account_id: Optional[int] = None) -> int:
    """Create the pending row(s) for ``due_date`` with NO already-there check:
    on the funding account (the loan's own, or ``funding_account_id`` for this
    one payment) in the posted shape, else on the loan's own register. Callers
    either guard first (create_pending_payment) or mean a second payment in
    the period (enter_payment, an explicit extra payment)."""
    lp = loans.get_loan_params(conn, loan_account_id)
    if lp is None:
        raise LookupError(f"account {loan_account_id} has no loan parameters")
    funder = (int(funding_account_id) if funding_account_id is not None
              else loans.funding_account(conn, loan_account_id))
    if funder is not None:
        return _new_pending_on_funder(conn, loan_account_id, funder, due_date,
                                      amount_cents=amount_cents, payee=payee,
                                      memo=memo)

    amount = lp.payment_amount if amount_cents is None else amount_cents
    split = loans.payment_split(conn, loan_account_id, due_date, amount)
    if split.principal <= 0:
        raise ValueError(
            f"payment {amount} at {due_date} does not cover interest+escrow")
    legs = _split_legs(split, lp.interest_category)
    if payee is None:
        acct = ledger.get_account(conn, loan_account_id)
        name = acct["name"] if acct is not None else "Loan"
        payee = f"{name} Payment"

    txn_id = ledger.add_transaction(
        conn, loan_account_id, due_date, amount,
        payee=payee, memo=memo, scheduled=1, cleared=0,
    )
    _apply_split(conn, txn_id, legs)
    return txn_id


def payment_on_funder(conn, loan_account_id: int, funding_account_id: int,
                      date: str, day_window: int = DEFAULT_MATCH_WINDOW_DAYS
                      ) -> Optional[int]:
    """The id of the payment for this loan's ``date`` on the funding account
    -- a split with a leg into the loan, or a plain transfer into it, pending
    or posted -- or ``None``. A payment counts when dated within ``day_window``
    days of the due date (nearest wins; a still-pending row beats a posted one
    at equal distance). Not exact-date on purpose: autopay pulls a few days
    early, and an import merge stamps the row with the bank's actual date, so
    an exact test would forget the payment the moment it posted and pre-enter
    -- and project -- the same period twice."""
    return _nearest_payment(
        conn, date, day_window,
        "SELECT t.id, t.date, t.scheduled FROM transactions t "
        "WHERE t.account_id=? AND t.date BETWEEN ? AND ? AND ("
        "  t.transfer_account_id=? OR t.id IN ("
        "    SELECT transaction_id FROM splits WHERE transfer_account_id=?))",
        (funding_account_id, _shift_days(date, -day_window),
         _shift_days(date, day_window), loan_account_id, loan_account_id))


def _funder_lines(conn, loan_account_id: int, split, interest_category) -> list[dict]:
    """The split lines of a payment posted on the FUNDING account: interest,
    each extra, and a principal leg transferring into the loan. Amounts are
    negative (money leaves the funder); they sum to minus the payment."""
    lines: list[dict] = []
    if split.interest:
        lines.append({"category_id": ledger.resolve_category(
                          conn, interest_category or INTEREST_CATEGORY),
                      "amount": -split.interest, "memo": "Interest"})
    for ex in split.extras:
        if ex.amount:
            lines.append({"category_id": ledger.resolve_category(conn, ex.category),
                          "amount": -ex.amount, "memo": ex.label or ex.category})
    lines.append({"transfer_account_id": loan_account_id,
                  "amount": -split.principal, "memo": "Principal"})
    return lines


def _mark_split_mirrors(conn, txn_id: int, scheduled: int) -> None:
    conn.execute(
        "UPDATE transactions SET scheduled=? WHERE id IN "
        "(SELECT transfer_pair_id FROM splits WHERE transaction_id=? "
        " AND transfer_pair_id IS NOT NULL)", (scheduled, txn_id))
    conn.commit()


def _new_pending_on_funder(conn, loan_account_id: int, funding_account_id: int,
                           due_date: str, *, amount_cents: Optional[int] = None,
                           payee: Optional[str] = None,
                           memo: Optional[str] = "Scheduled payment") -> int:
    """Pre-enter the payment on the FUNDING account as a pending split --
    interest, each escrow/PMI extra, and a ``[Loan]`` principal leg whose mirror
    on the loan register is also flagged pending -- exactly the shape the
    user's posted payments have, so the funder's download merges into it.
    No already-there check (see _new_pending_payment). Returns the
    funding-side transaction id."""
    lp = loans.get_loan_params(conn, loan_account_id)
    amount = lp.payment_amount if amount_cents is None else amount_cents
    split = loans.payment_split(conn, loan_account_id, due_date, amount)
    if split.principal <= 0:
        raise ValueError(
            f"payment {amount} at {due_date} does not cover interest+escrow")
    if payee is None:
        payee = loans.last_payment_payee(conn, loan_account_id, funding_account_id)
    if payee is None:
        acct = ledger.get_account(conn, loan_account_id)
        payee = f"{acct['name'] if acct is not None else 'Loan'} Payment"
    txn_id = ledger.add_transaction(
        conn, funding_account_id, due_date, -amount,
        payee=payee, memo=memo, scheduled=1, cleared=0)
    ledger.set_splits(conn, txn_id, _funder_lines(conn, loan_account_id, split,
                                                  lp.interest_category))
    _mark_split_mirrors(conn, txn_id, 1)
    return txn_id


def standing_pre_entries(conn, loan_account_id: int) -> list:
    """Every still-pending pre-entry for this loan as ``(id, date, account_id)``,
    earliest first -- funding-side split parents with a leg into the loan, and
    pre-entries the loan register owns itself; never the pending principal
    mirrors, which belong to their parents."""
    rows = conn.execute(
        "SELECT id, date, account_id FROM transactions t "
        "WHERE t.scheduled=1 AND ("
        "  (t.account_id=? AND t.transfer_account_id IS NULL) OR "
        "  t.id IN (SELECT transaction_id FROM splits WHERE transfer_account_id=?)) "
        "ORDER BY t.date, t.id", (loan_account_id, loan_account_id)).fetchall()
    return [(int(r["id"]), r["date"], int(r["account_id"])) for r in rows]


def pending_payment(conn, loan_account_id: int):
    """The earliest still-pending pre-entry for this loan as
    ``(txn_id, date, account_id)``, or ``None`` -- what the register's Enter
    Payment tells the user about: that row is the next payment, and Enter
    restates it rather than posting another beside it."""
    rows = standing_pre_entries(conn, loan_account_id)
    return rows[0] if rows else None


def realign_pending_payments(conn, loan_account_id: int) -> list[int]:
    """Re-issue every standing pre-entry for this loan from its CURRENT setup:
    the account that pays it, the payment in force, the rate and extras behind
    the split, the payee. Loan Setup calls this on save, so changing "Paid
    from" moves the reminders already sitting in the old account's register,
    and a changed payment or rate shows in the rows the user is looking at
    rather than only in ones generated later. A pre-entry that stays on its
    account is restated in place (same row, same id, its memo and number
    kept); one whose account changed is re-created there at its own date.
    Returns the ids, one per standing pre-entry."""
    lp = loans.get_loan_params(conn, loan_account_id)
    if lp is None:
        raise LookupError(f"account {loan_account_id} has no loan parameters")
    funder = loans.funding_account(conn, loan_account_id)
    side = funder if funder is not None else int(loan_account_id)
    out = []
    for pid, date, aid in standing_pre_entries(conn, loan_account_id):
        amount = loans._active_payment(lp.payments, lp.payment_amount, date)
        if aid == side:
            out.append(_restate_pending(conn, loan_account_id, pid, date,
                                        amount_cents=amount))
        else:
            ledger.delete_transaction(conn, pid)
            out.append(_new_pending_payment(conn, loan_account_id, date,
                                            amount_cents=amount))
    return out


def find_matching_pending_mirror(conn, loan_account_id: int, date: str,
                                 amount_cents: int,
                                 day_window: int = DEFAULT_MATCH_WINDOW_DAYS
                                 ) -> Optional[int]:
    """The pending principal MIRROR on the loan register (the loan side of a
    funding-side pre-entry) equal to a downloaded ``amount_cents`` within the
    window, nearest first, or ``None``. A lender's own download shows the
    principal applied; that row is this mirror, to be confirmed
    (confirm_pending_mirror), never a new payment."""
    if amount_cents <= 0:
        return None
    return _nearest_payment(
        conn, date, day_window,
        "SELECT id, date, scheduled FROM transactions "
        "WHERE account_id=? AND scheduled=1 AND amount=? "
        "AND transfer_account_id IS NOT NULL AND date BETWEEN ? AND ?",
        (loan_account_id, amount_cents, _shift_days(date, -day_window),
         _shift_days(date, day_window)))


def confirm_pending_mirror(conn, mirror_id: int, *, fitid: Optional[str] = None,
                           import_id: Optional[int] = None) -> int:
    """The lender confirmed the principal leg: mark the mirror cleared and
    posted (its own side only -- the funding-side parent stays pending until
    that account's download merges into it). Returns ``mirror_id``."""
    fields = dict(cleared=1, scheduled=0)
    if fitid:
        fields["fitid"] = fitid
    if import_id is not None:
        fields["import_id"] = import_id
    ledger.update_transaction(conn, mirror_id, **fields)
    return mirror_id


def enter_payment(conn, loan_account_id: int, date: str, *,
                  amount_cents: Optional[int] = None,
                  payee: Optional[str] = None,
                  funding_account_id: Optional[int] = None) -> int:
    """Quicken's Enter on a loan: record a payment dated ``date`` as a POSTED
    transaction in the posted shape -- on the loan's funding account, or on
    ``funding_account_id`` for this one payment (paying from a card because
    checking is low). A one-off from another account pins the loan's current
    default first, so it does not become the inferred funder next time.

    A pending pre-entry already standing for that period is a placeholder: it
    becomes what the user entered (date, amount, payee, and account -- moved
    when the account differs) and is posted, so Enter never doubles a period
    and never has to be blocked by its own reminder. A period that already
    holds a POSTED payment gets another: an explicit second payment is the
    caller's intent (the Enter Payment dialog says so before saving). The
    manager's next date is read back from the registers (next_due_date), so it
    moves on by itself and moves back if the user deletes the payment again.
    Returns the transaction id."""
    lp = loans.get_loan_params(conn, loan_account_id)
    if lp is None:
        raise LookupError(f"account {loan_account_id} has no loan parameters")
    default = loans.funding_account(conn, loan_account_id)
    funder = int(funding_account_id) if funding_account_id is not None else default
    if funder != default and lp.funding_account_id is None and default is not None:
        loans.set_funding_account(conn, loan_account_id, default)
    existing = payment_for(conn, loan_account_id, date)
    row = ledger.get_transaction(conn, existing) if existing is not None else None
    if row is not None and row["scheduled"]:
        side = funder if funder is not None else int(loan_account_id)
        if int(row["account_id"]) == side:
            tid = _restate_pending(conn, loan_account_id, existing, date,
                                   amount_cents=amount_cents, payee=payee)
        else:
            ledger.delete_transaction(conn, existing)
            tid = _new_pending_payment(conn, loan_account_id, date,
                                       amount_cents=amount_cents, payee=payee,
                                       memo=None, funding_account_id=funder)
    else:
        tid = _new_pending_payment(conn, loan_account_id, date,
                                   amount_cents=amount_cents, payee=payee,
                                   memo=None, funding_account_id=funder)
    ledger.set_scheduled(conn, tid, False)
    return tid


def _restate_pending(conn, loan_account_id: int, pending_id: int, date: str, *,
                     amount_cents: Optional[int] = None,
                     payee: Optional[str] = None) -> int:
    """Make a standing pre-entry say what the user entered -- date, amount,
    payee -- and re-derive its split for that amount at that date, on
    whichever side it sits. Returns ``pending_id``."""
    txn = ledger.get_transaction(conn, pending_id)
    lp = loans.get_loan_params(conn, loan_account_id)
    on_loan = int(txn["account_id"]) == int(loan_account_id)
    amount = abs(int(txn["amount"])) if amount_cents is None else int(amount_cents)
    split = loans.payment_split(conn, loan_account_id, date, amount)
    if split.principal <= 0:
        raise ValueError(f"payment {amount} at {date} does not cover interest+escrow")
    fields = dict(date=date, amount=amount if on_loan else -amount)
    if payee:
        fields["payee"] = payee
    ledger.update_transaction(conn, pending_id, **fields)
    if on_loan:
        _apply_split(conn, pending_id, _split_legs(split, lp.interest_category))
    else:
        ledger.set_splits(conn, pending_id, _funder_lines(conn, loan_account_id, split,
                                                          lp.interest_category))
        # set_splits re-creates the principal mirror; keep it as pending (or
        # posted) as its parent is.
        _mark_split_mirrors(conn, pending_id, int(txn["scheduled"] or 0))
    return pending_id


def pending_funding_pre_entries(conn, account_id: int, date: str,
                                day_window: int = DEFAULT_MATCH_WINDOW_DAYS) -> list:
    """Every pending funding-side loan pre-entry on ``account_id`` within the
    window as rows of ``id, date, amount, payee, loan_id``, nearest first: the
    candidates for a download whose amount no longer matches any pre-entry.
    The importer takes the one whose payee the bank's descriptor names -- a
    changed payment (escrow, rate) -- never merely the nearest debit of a
    similar size, which could be anything."""
    rows = conn.execute(
        "SELECT t.id, t.date, t.amount, t.payee, s.transfer_account_id AS loan_id "
        "FROM transactions t JOIN splits s ON s.transaction_id = t.id "
        "JOIN loan_params lp ON lp.account_id = s.transfer_account_id "
        "WHERE t.account_id=? AND t.scheduled=1 AND t.date BETWEEN ? AND ?",
        (account_id, _shift_days(date, -day_window), _shift_days(date, day_window))
    ).fetchall()
    return sorted(rows, key=lambda r: _days_between(r["date"], date))


def find_matching_funding_pending(conn, account_id: int, date: str, amount_cents: int,
                                  day_window: int = DEFAULT_MATCH_WINDOW_DAYS):
    """A pending funding-side loan pre-entry on ``account_id`` equal to the
    imported ``amount_cents`` (negative) within ``day_window`` days of ``date``,
    as ``(pending_id, loan_account_id)`` -- the closest by date -- or ``None``."""
    if amount_cents >= 0:
        return None
    rows = conn.execute(
        "SELECT t.id, t.date, s.transfer_account_id AS loan_id FROM transactions t "
        "JOIN splits s ON s.transaction_id = t.id "
        "JOIN loan_params lp ON lp.account_id = s.transfer_account_id "
        "WHERE t.account_id=? AND t.scheduled=1 AND t.amount=?",
        (account_id, amount_cents)).fetchall()
    best, best_diff = None, None
    for row in rows:
        diff = _days_between(row["date"], date)
        if diff <= day_window and (best_diff is None or diff < best_diff):
            best, best_diff = (int(row["id"]), int(row["loan_id"])), diff
    return best


def merge_import_into_funding_pending(conn, pending_id: int, loan_account_id: int, *,
                                      date: str, amount_cents: int,
                                      fitid: Optional[str] = None,
                                      import_id: Optional[int] = None) -> int:
    """Post an imported payment INTO a funding-side pre-entry: adopt the actual
    date and amount, stamp fitid/import_id, mark it cleared and no longer
    pending, and re-derive the split for the actual amount at the pre-entry's
    scheduled date. The payee is the pre-entry's own (the user's name for the
    lender), not the bank's descriptor. Returns ``pending_id``."""
    txn = ledger.get_transaction(conn, pending_id)
    if txn is None:
        raise KeyError(f"no transaction {pending_id}")
    lp = loans.get_loan_params(conn, loan_account_id)
    split = loans.payment_split(conn, loan_account_id, txn["date"], -amount_cents)
    fields = dict(date=date, amount=amount_cents, cleared=1, scheduled=0)
    if fitid:
        fields["fitid"] = fitid
    if import_id is not None:
        fields["import_id"] = import_id
    ledger.update_transaction(conn, pending_id, **fields)
    ledger.set_splits(conn, pending_id, _funder_lines(
        conn, loan_account_id, split, lp.interest_category if lp else None))
    _mark_split_mirrors(conn, pending_id, 0)
    return pending_id


def ensure_pending_payments(conn, loan_account_id: int, as_of_date: str, *,
                            lead_days: int = DEFAULT_LEAD_DAYS) -> list:
    """Create a pending pre-entry for every scheduled due date falling in the
    window [as_of_date, as_of_date + lead_days] that does not already have one.
    Returns the list of pre-entry transaction ids (existing or newly created)."""
    horizon = _shift_days(as_of_date, lead_days)
    sched = loans.amortization_schedule(conn, loan_account_id)
    out = []
    for row in sched:
        if as_of_date <= row.date <= horizon:
            out.append(create_pending_payment(conn, loan_account_id, row.date))
    return out


# ---------------------------------------------------------------------------
# import matching + merge
# ---------------------------------------------------------------------------
def find_matching_pending(conn, loan_account_id: int, date: str,
                          amount_cents: int,
                          day_window: int = DEFAULT_MATCH_WINDOW_DAYS
                          ) -> Optional[int]:
    """The id of a pending pre-entry in this loan account whose amount equals
    ``amount_cents`` and whose date is within ``day_window`` days of ``date`` --
    the closest by date if several qualify -- or ``None``. Only a pre-entry
    the loan register OWNS (no ``transfer_account_id``): the pending principal
    MIRROR of a funding-side pre-entry is not a payment to merge into --
    re-splitting it would turn one side of a transfer into a split parent
    (find_matching_pending_mirror confirms those instead)."""
    rows = conn.execute(
        "SELECT id, date FROM transactions "
        "WHERE account_id=? AND scheduled=1 AND amount=? "
        "AND transfer_account_id IS NULL",
        (loan_account_id, amount_cents),
    ).fetchall()
    best_id, best_diff = None, None
    for row in rows:
        diff = _days_between(row["date"], date)
        if diff <= day_window and (best_diff is None or diff < best_diff):
            best_id, best_diff = row["id"], diff
    return best_id


def merge_import_into_pending(conn, pending_id: int, *, date: str,
                              amount_cents: int, fitid: Optional[str] = None,
                              import_id: Optional[int] = None,
                              payee: Optional[str] = None) -> int:
    """Post an imported payment INTO an existing pending pre-entry: adopt the
    actual date/amount, stamp fitid/import_id, mark it cleared and no longer
    scheduled, and re-derive the split for the actual amount so it reconciles to
    the cent. Returns ``pending_id`` (no new row is created)."""
    txn = ledger.get_transaction(conn, pending_id)
    if txn is None:
        raise KeyError(f"no transaction {pending_id}")
    loan_account_id = txn["account_id"]

    # Re-derive the split at the pre-entry's SCHEDULED due date -- the period it
    # represents -- not the (possibly days-later) posting date, so a payment that
    # merely arrives late keeps its own period's interest/principal breakdown. If
    # the amount changed, the difference is absorbed into principal here.
    split = loans.payment_split(conn, loan_account_id, txn["date"], amount_cents)
    lp = loans.get_loan_params(conn, loan_account_id)
    legs = _split_legs(split, lp.interest_category if lp else None)

    fields = dict(date=date, amount=amount_cents, cleared=1, scheduled=0)
    if fitid:
        fields["fitid"] = fitid
    if import_id is not None:
        fields["import_id"] = import_id
    if payee:
        fields["payee"] = payee
    ledger.update_transaction(conn, pending_id, **fields)
    _apply_split(conn, pending_id, legs)
    return pending_id


# ---------------------------------------------------------------------------
# payment-change detection + downstream auto-fix (Task 56)
# ---------------------------------------------------------------------------
@dataclass
class PaymentChange:
    """A reconcile-time mismatch: an imported loan payment landed near a pending
    pre-entry's due date but with a DIFFERENT amount than we pre-entered, i.e.
    the payment changed (rate reset or escrow adjustment) after we scheduled it.
    ``pending_id`` is the pre-entry it lines up with (the row the import was
    merged into); the UI uses this to ask the user the change type + effective
    date before ``apply_payment_change`` fixes the schedule going forward."""
    pending_id: int
    scheduled_date: str          # the pre-entry's own due date
    expected_amount: int         # what we pre-entered (cents)
    actual_amount: int           # what actually imported (cents)

    @property
    def delta(self) -> int:
        return self.actual_amount - self.expected_amount


def detect_payment_change(conn, loan_account_id: int, date: str,
                          amount_cents: int,
                          day_window: int = DEFAULT_MATCH_WINDOW_DAYS
                          ) -> Optional[PaymentChange]:
    """If an imported payment does NOT exactly match a pending pre-entry (so
    :func:`find_matching_pending` returns ``None``) but a pending pre-entry
    exists within ``day_window`` days carrying a DIFFERENT amount, return a
    :class:`PaymentChange` describing the mismatch; else ``None``. The closest
    pre-entry by date wins when several qualify."""
    rows = conn.execute(
        "SELECT id, date, amount FROM transactions "
        "WHERE account_id=? AND scheduled=1 AND transfer_account_id IS NULL",
        (loan_account_id,),
    ).fetchall()
    best, best_diff = None, None
    for row in rows:
        if row["amount"] == amount_cents:
            continue                         # exact match -> ordinary merge path
        diff = _days_between(row["date"], date)
        if diff <= day_window and (best_diff is None or diff < best_diff):
            best, best_diff = row, diff
    if best is None:
        return None
    return PaymentChange(best["id"], best["date"], best["amount"], amount_cents)


# ---------------------------------------------------------------------------
# repair: collapse doubled one-sided loan-payment legs (the user's recast regression)
# ---------------------------------------------------------------------------
# A loan payment in the user's data posts on the FUNDING account (checking) as a split
# whose principal leg transfers INTO the loan; the loan side is just the one-sided
# mirror that split creates (``splits.transfer_pair_id`` -> a loan leg whose own
# ``transfer_pair_id`` stays NULL). An OLDER, reverted regenerate path also left a
# second one-sided leg for the same payment (an imported ``[Loan]`` principal leg,
# or a prior mirror), so the loan register showed each payment DOUBLED and the
# running balance (opening + sum of amounts) retired principal twice.
#
# This deletes a one-sided loan leg that is provably the STALE DUPLICATE of a real
# payment: it is bare (no split of its own), NOT itself a live split mirror, is NOT
# a two-sided transfer (so no counterpart is ever orphaned), AND there is ANOTHER
# leg on the same loan account and date that IS a live split mirror (the survivor).
# Grouping on date (NOT amount) is what lets it collapse the different-amount case
# migration _V21 leaves behind (e.g. a stale import leg 50657 beside a live mirror
# 60626). A LONE one-sided leg with no live-mirror twin is kept untouched -- it is
# the only record of that payment, never a duplicate. Idempotent: once one leg per
# payment remains, a re-run matches nothing.
_DEDUPE_ONE_SIDED_LOAN_LEGS = """
DELETE FROM transactions WHERE id IN (
  SELECT t.id FROM transactions t
    JOIN loan_params lp ON lp.account_id = t.account_id
   WHERE t.scheduled = 0
     AND t.transfer_account_id IS NOT NULL
     AND t.transfer_pair_id IS NULL
     AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)
     AND t.id NOT IN (SELECT transfer_pair_id FROM splits
                       WHERE transfer_pair_id IS NOT NULL)
     AND EXISTS (SELECT 1 FROM transactions m
                  WHERE m.account_id = t.account_id AND m.date = t.date
                    AND m.id <> t.id
                    AND m.id IN (SELECT transfer_pair_id FROM splits
                                  WHERE transfer_pair_id IS NOT NULL))
     {account_filter}
)
"""


def dedupe_loan_payment_legs(conn, loan_account_id: Optional[int] = None) -> int:
    """Collapse doubled one-sided loan-payment legs (see the module note above),
    keeping the live split-mirror survivor and deleting the stale duplicate. Scopes
    to one loan when ``loan_account_id`` is given, else every loan. Returns the
    number of rows deleted. Idempotent."""
    if loan_account_id is None:
        sql = _DEDUPE_ONE_SIDED_LOAN_LEGS.format(account_filter="")
        cur = conn.execute(sql)
    else:
        sql = _DEDUPE_ONE_SIDED_LOAN_LEGS.format(
            account_filter="AND t.account_id = ?")
        cur = conn.execute(sql, (loan_account_id,))
    conn.commit()
    return cur.rowcount


def _resplit_transfer_payments(conn, loan_account_id: int,
                               effective_date: str,
                               interest_category: Optional[str] = None) -> list:
    """Re-derive every posted loan payment that lives on ANOTHER account as a split
    with a transfer leg INTO this loan (the user's real model: the full payment posts on
    checking as interest + each escrow/PMI extra + a ``[Loan]`` principal leg), for
    parents dated >= ``effective_date``.

    Each line's AMOUNT is recomputed from the balance-driven model
    (:func:`mammon.loans.payment_split` -- interest = prior balance x periodic rate,
    each extra its dated amount, principal = total - interest - escrow). The
    interest line is RE-CATEGORIZED to the loan's own ``interest_category`` when
    set (the user's chosen interest-expense category), else its existing CATEGORY
    is PRESERVED (prior behavior -- keep the loan's stored Interest/Escrow
    categories rather than guessing a constant). Extra lines always keep their own
    category. ``ledger.set_splits`` rebuilds the single loan-side mirror leg
    (deleting the prior one first), so the stored leg == principal and re-running
    is idempotent -- never doubling a row. A parent whose shape is not exactly one
    loan leg + extras + one interest line is left untouched. Returns the re-split
    parent transaction ids."""
    interest_cat_id = (ledger.resolve_category(conn, interest_category)
                       if interest_category else None)
    parents = conn.execute(
        "SELECT DISTINCT s.transaction_id AS tid FROM splits s "
        "JOIN transactions t ON t.id = s.transaction_id "
        "WHERE s.transfer_account_id=? AND t.date>=? AND t.scheduled=0 "
        "ORDER BY t.date, t.id",
        (loan_account_id, effective_date),
    ).fetchall()
    fixed = []
    for prow in parents:
        tid = prow["tid"]
        txn = ledger.get_transaction(conn, tid)
        if txn is None or int(txn["amount"]) >= 0:
            continue                      # a draw/charge, not a payment out
        total = -int(txn["amount"])       # payment posts negative on the funder
        stored = ledger.get_splits(conn, tid)
        legs_into_loan = [s for s in stored
                          if s["transfer_account_id"] == loan_account_id]
        if len(legs_into_loan) != 1:
            continue                      # unexpected shape -> leave it alone
        split = loans.payment_split(conn, loan_account_id, txn["date"], total)
        if split.principal <= 0:
            continue
        extra_by_cat = {}
        for ex in split.extras:
            extra_by_cat[ledger.resolve_category(conn, ex.category)] = ex.amount
        new_lines, interest_lines, bad = [], 0, False
        for s in stored:
            if s["transfer_account_id"] == loan_account_id:
                new_lines.append({"transfer_account_id": loan_account_id,
                                  "amount": -split.principal, "memo": s["memo"]})
            elif s["category_id"] in extra_by_cat:
                new_lines.append({"category_id": s["category_id"],
                                  "amount": -extra_by_cat[s["category_id"]],
                                  "memo": s["memo"]})
            else:
                interest_lines += 1
                new_lines.append({"category_id": interest_cat_id or s["category_id"],
                                  "amount": -split.interest, "memo": s["memo"]})
        if interest_lines != 1:
            continue                      # can't unambiguously place interest
        # Preserve the loan leg's own reconcile/clear state across the rebuild.
        mark = conn.execute(
            "SELECT MAX(cleared) AS c, MAX(reconciled) AS r FROM transactions "
            "WHERE id IN (SELECT transfer_pair_id FROM splits "
            "             WHERE transaction_id=? AND transfer_pair_id IS NOT NULL)",
            (tid,)).fetchone()
        ledger.set_splits(conn, tid, new_lines)
        if mark and (mark["c"] or mark["r"]):
            conn.execute(
                "UPDATE transactions SET cleared=?, reconciled=? "
                "WHERE id IN (SELECT transfer_pair_id FROM splits "
                "             WHERE transaction_id=? AND transfer_pair_id IS NOT NULL)",
                (mark["c"] or 0, mark["r"] or 0, tid))
        fixed.append(tid)
    conn.commit()
    return fixed


def apply_payment_change(conn, loan_account_id: int, effective_date: str, *,
                         change_type: str,
                         new_annual_rate=None,
                         new_escrow_cents: Optional[int] = None,
                         escrow_category: str = "Escrow",
                         new_payment_amount: Optional[int] = None,
                         resplit_posted: bool = True) -> list:
    """User-confirmed auto-fix: record the payment change in the loan's stored
    parameters and re-derive every affected entry from ``effective_date``
    forward so it reconciles to the cent.

    ``change_type`` is ``'rate'``, ``'escrow'``, ``'both'`` or ``'payment'``. A
    ``'payment'`` change is a PURE new-total change (no rate/escrow re-attribution):
    it requires ``new_payment_amount`` and only stores the dated total and re-splits
    every affected entry forward under it. A rate change adds
    a dated rate row (``loans.add_rate`` -- history-correct: earlier periods keep
    the old rate). An escrow change adds a DATED escrow-extra row
    (``loans.add_extra_change`` -- likewise history-correct: the escrow/PMI amount
    is first-class dated data, so earlier periods keep the old amount and only the
    effective date forward gets the new one).

    The TOTAL PAYMENT is preserved, NEVER computed: a rate/escrow change only
    re-attributes interest/principal/escrow WITHIN the existing total.
    ``new_payment_amount`` is the user's authoritative new total -- the amount he
    actually pays (a higher imported draw after an escrow bump he never re-keyed,
    or an ARM recast); it is stored as first-class DATED data so the schedule
    adopts it from the effective date forward (earlier periods keep the old total).
    When omitted the total is left UNCHANGED and only the split is re-derived.

    Re-splits every PENDING pre-entry dated >= ``effective_date`` (adopting the new
    authoritative total when one was given, else keeping its own) and, when
    ``resplit_posted``, every already-posted loan payment (a row that carries
    splits) dated >= ``effective_date`` -- keeping its actual amount but
    re-attributing principal/interest/escrow. Returns the list of transaction ids
    that were re-split."""
    if change_type not in ("rate", "escrow", "both", "payment"):
        raise ValueError(f"unknown change_type {change_type!r}")
    if change_type == "payment" and new_payment_amount is None:
        raise ValueError("payment change requires new_payment_amount")
    lp = loans.get_loan_params(conn, loan_account_id)
    if lp is None:
        raise LookupError(f"account {loan_account_id} has no loan parameters")

    if change_type in ("rate", "both"):
        if new_annual_rate is None:
            raise ValueError("rate change requires new_annual_rate")
        loans.add_rate(conn, loan_account_id, effective_date, new_annual_rate)

    if change_type in ("escrow", "both"):
        if new_escrow_cents is None:
            raise ValueError("escrow change requires new_escrow_cents")
        # Store the change as first-class DATED data (not a flat overwrite), so a
        # re-derived split reproduces every payment under whichever amount was in
        # force on its date -- earlier posted rows stay correct without re-splitting.
        loans.add_extra_change(conn, loan_account_id, effective_date,
                               escrow_category, new_escrow_cents)

    # The TOTAL PAYMENT is the user's only authoritative input; it is NEVER computed
    # here. A rate/escrow change re-attributes interest/principal/escrow within the
    # existing total -- it does NOT synthesize a new total. Only when the user supplies
    # ``new_payment_amount`` (the amount he actually pays) do we adopt a new total,
    # stored as first-class DATED data so the projected schedule uses the amount in
    # force on each period's date; add_payment_change also syncs the loan's flat
    # CURRENT payment_amount to the latest-dated total.
    #
    # An earlier version computed ``old_pi + new_escrow_total`` and stamped THAT
    # over the payment amount -- exactly backwards (the total must never be replaced
    # by a computed number). Removed: only interest/principal/balance are computed.
    if new_payment_amount is not None:
        loans.add_payment_change(conn, loan_account_id, effective_date,
                                 int(new_payment_amount))

    fixed = []
    rows = conn.execute(
        "SELECT id, amount, scheduled FROM transactions "
        "WHERE account_id=? AND date>=? ORDER BY date, id",
        (loan_account_id, effective_date),
    ).fetchall()
    for row in rows:
        txn = ledger.get_transaction(conn, row["id"])
        if row["scheduled"]:
            # A pending pre-entry's amount IS the scheduled total. Adopt the user's new
            # authoritative total when he gave one; otherwise keep the total already
            # on the row -- NEVER overwrite it with a computed value.
            if new_payment_amount is not None:
                amount = int(new_payment_amount)
                ledger.update_transaction(conn, row["id"], amount=amount)
            else:
                amount = row["amount"]
        elif resplit_posted and ledger.has_splits(conn, row["id"]):
            amount = row["amount"]           # posted: keep the ACTUAL paid amount
        else:
            continue
        split = loans.payment_split(conn, loan_account_id, txn["date"], amount)
        _apply_split(conn, row["id"], _split_legs(split, lp.interest_category))
        fixed.append(row["id"])

    # a real model does NOT post the payment on the loan side at all: the full
    # payment posts on the FUNDING account (checking) as a split whose principal
    # leg transfers INTO this loan, and the loan side is only that one-sided mirror
    # leg (no split of its own), which the loop above cannot re-derive. First
    # collapse any doubled/stale one-sided legs (the recast duplication), then
    # re-split those posted payments from the effective date forward so their
    # interest/escrow/principal match the model and the single loan leg =
    # principal. Both steps are idempotent, so re-applying the change (or saving
    # Edit Loan twice) never doubles a row.
    dedupe_loan_payment_legs(conn, loan_account_id)
    if resplit_posted:
        fixed.extend(_resplit_transfer_payments(conn, loan_account_id,
                                                effective_date,
                                                lp.interest_category))
    return fixed
