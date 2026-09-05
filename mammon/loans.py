"""mammon.loans -- the loan / liability domain engine (SRD loan subsystem).

A pure-domain module (NO Qt), sitting on mammon.db, that models a loan the way
Quicken does and then some. It persists per-loan PARAMETERS and derives, in pure
Python, two things Quicken keeps behind its loan wizard:

  * :func:`amortization_schedule` -- the full period-by-period payoff schedule,
    honoring an effective-dated interest-rate HISTORY (Quicken stores one rate;
    a real mortgage resets, so Mammon tracks every rate change and the schedule
    switches rate at each effective date), and
  * :func:`payment_split` -- decompose a single real payment (date + total
    amount) into PRINCIPAL / INTEREST / ESCROW(+other categorized extras), where
    ``interest = outstanding_balance * period_rate`` for the rate active on that
    date and ``principal = amount - interest - extras``. So an imported bank
    payment (one lumped number) can be posted as Quicken's real split, with
    interest and escrow landing in their own categories instead of being lost.
    A principal-only paydown reduces the balance when it posts, so the next
    payment's interest is a full quantum on the reduced balance (never prorated
    by day count -- the servicer charges one quantum per payment, whatever the
    period's length).

Conventions mirror mammon.investments + mammon.ledger:
  * Money is signed INTEGER cents. Interest is rounded HALF_UP at the cent.
  * Rates are Decimal, stored as TEXT annual PERCENTAGE ('6.5' == 6.5% APR);
    the per-period rate is ``annual_rate / 100 / periods_per_year``.
  * Dates are ISO YYYY-MM-DD strings (they sort lexicographically, so the active
    rate is simply the latest effective row whose date <= the payment date).

mammon.ledger stays the sole writer of TRANSACTIONS; this module only owns the
loan_params / loan_rates / loan_extras parameter rows and the math over them.
"""
from __future__ import annotations

import calendar
import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

_CENT = Decimal("1")
_HUNDRED = Decimal("100")

# interval name -> (periods_per_year, (advance-unit, n)). The advance moves one
# payment date to the next; monthly/quarterly/etc. move by calendar months (with
# day-of-month clamping), weekly/biweekly by fixed days.
_INTERVALS: dict[str, tuple[int, tuple[str, int]]] = {
    "weekly": (52, ("days", 7)),
    "biweekly": (26, ("days", 14)),
    "semimonthly": (24, ("days", 15)),
    "monthly": (12, ("months", 1)),
    "quarterly": (4, ("months", 3)),
    "semiannual": (2, ("months", 6)),
    "annual": (1, ("months", 12)),
}


# ---------------------------------------------------------------------------
# Decimal / money helpers (mirroring mammon.investments)
# ---------------------------------------------------------------------------
def _D(value) -> Decimal:
    """Tolerant Decimal parse; '' / None -> 0."""
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return Decimal(0)


def _cents(value: Decimal) -> int:
    """Round a Decimal amount to integer cents, HALF_UP."""
    return int(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _rate_text(value) -> str:
    """A clean, exponent-free Decimal string for storing a rate ('6.5', '0')."""
    d = _D(value)
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def _parse(date: str) -> _dt.date:
    return _dt.date.fromisoformat(date)


def _add_months(date: str, n: int) -> str:
    d = _parse(date)
    idx = d.year * 12 + (d.month - 1) + n
    year, month0 = divmod(idx, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return _dt.date(year, month, day).isoformat()


def _add_days(date: str, n: int) -> str:
    return (_parse(date) + _dt.timedelta(days=n)).isoformat()


def _advance(date: str, spec: tuple[str, int]) -> str:
    unit, n = spec
    return _add_months(date, n) if unit == "months" else _add_days(date, n)


def origination_from_first_payment(first_payment_date: str,
                                   interval: str = "monthly") -> str:
    """The origination (funding) date implied by a FIRST-PAYMENT date: exactly
    one payment interval earlier. Stored as ``origination_date`` it makes
    :meth:`LoanParams.first_payment_date` reproduce the date the borrower
    entered, so the schedule's first row lands on their real first payment. The
    setup wizard collects a first-payment date (what a borrower actually knows)
    and derives the origination with this."""
    if interval not in _INTERVALS:
        raise ValueError(f"unknown payment interval: {interval!r}")
    unit, n = _INTERVALS[interval][1]
    return _advance(first_payment_date, (unit, -n))


# ---------------------------------------------------------------------------
# Parameter model (dataclasses returned by get_loan_params)
# ---------------------------------------------------------------------------
@dataclass
class RateRow:
    effective_date: str          # ISO; rate applies on/after this date
    annual_rate: Decimal         # annual percentage (6.5 == 6.5%)


@dataclass
class ExtraLine:
    category: str                # posts to this category (escrow/PMI/HOA/...)
    amount: int                  # cents added to every payment
    label: Optional[str] = None
    effective_date: Optional[str] = None   # ISO; amount applies on/after this date
    #   (None / '' == from the beginning). Several rows per category form a
    #   timeline, like the interest-rate history.


@dataclass
class PaymentRow:
    effective_date: str          # ISO; this total payment applies on/after here
    amount: int                  # cents, total scheduled payment (P&I + extras)
    #   A history of the whole scheduled payment: when escrow, the rate, or an
    #   extra changes the total changes too, and the new total is stored as a
    #   dated row so the projected schedule uses the amount in force per date.


@dataclass
class LoanParams:
    account_id: int
    original_principal: int      # cents
    origination_date: Optional[str]
    term_months: int
    payment_amount: int          # cents, total scheduled payment (P&I + extras)
    interval: str
    rates: list = field(default_factory=list)    # list[RateRow], sorted by date
    extras: list = field(default_factory=list)   # list[ExtraLine]
    payments: list = field(default_factory=list)  # list[PaymentRow], dated totals
    # Category path the computed-interest split line posts to (e.g. 'Int Exp' or
    # 'Landlord:Int Exp'). None => the split generator falls back to its module
    # default (loans_schedule.INTEREST_CATEGORY / importers' LOAN_INTEREST_CATEGORY).
    interest_category: Optional[str] = None
    # The account the payment is made FROM (checking, usually). Pre-entries post
    # there as a split with a principal leg transferring to the loan -- the shape
    # the user's real payments have -- so the funding account's download merges
    # into them. None = not stored; see :func:`funding_account` for inference.
    funding_account_id: Optional[int] = None

    @property
    def periods_per_year(self) -> int:
        return _INTERVALS[self.interval][0]

    @property
    def extras_total(self) -> int:
        """Cents added to every payment on top of principal + interest, at the
        loan's CURRENT (latest-dated) state: for each extra category the most
        recent amount in its history. A category with a dated history contributes
        only its latest amount, never the sum of every past amount."""
        return sum(e.amount for e in _active_extras(self.extras, "9999-12-31"))

    def first_payment_date(self) -> str:
        """The first scheduled payment date: one interval after origination
        (a loan's first payment falls one period after it funds)."""
        if not self.origination_date:
            raise ValueError("loan has no origination_date; cannot date the schedule")
        return _advance(self.origination_date, _INTERVALS[self.interval][1])


@dataclass
class ScheduleRow:
    period: int
    date: str
    payment: int                 # cents (principal + interest + escrow)
    principal: int
    interest: int
    escrow: int                  # total of the extra lines this period
    balance: int                 # remaining principal AFTER this payment


@dataclass
class ExtraSplit:
    category: str
    amount: int
    label: Optional[str] = None


@dataclass
class PaymentSplit:
    date: str
    amount: int                  # the payment being decomposed (cents)
    principal: int
    interest: int
    escrow: int                  # total of the categorized extras
    extras: list = field(default_factory=list)   # list[ExtraSplit] breakdown
    balance_before: int = 0      # outstanding principal the interest is charged on
    balance_after: int = 0       # balance_before - principal
    annual_rate: Decimal = Decimal(0)
    period_rate: Decimal = Decimal(0)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def _unpack_rate(item) -> tuple[str, str]:
    """Accept a RateRow or a ``(effective_date, annual_rate)`` tuple."""
    if isinstance(item, RateRow):
        return item.effective_date, _rate_text(item.annual_rate)
    eff, rate = item
    return eff, _rate_text(rate)


def _unpack_extra(item) -> tuple[str, int, Optional[str], Optional[str]]:
    """Accept an ExtraLine or a ``(category, amount[, label[, effective_date]])``
    tuple, returning ``(category, amount, label, effective_date)``."""
    if isinstance(item, ExtraLine):
        return item.category, int(item.amount), item.label, item.effective_date
    if len(item) == 2:
        cat, amt = item
        return cat, int(amt), None, None
    if len(item) == 3:
        cat, amt, label = item
        return cat, int(amt), label, None
    cat, amt, label, eff = item
    return cat, int(amt), label, eff


def set_loan_params(
    conn,
    account_id: int,
    *,
    original_principal: int,
    term_months: int,
    payment_amount: int,
    origination_date: Optional[str] = None,
    interval: str = "monthly",
    rates=None,
    extras=None,
    note: Optional[str] = None,
    interest_category: Optional[str] = None,
    funding_account_id: Optional[int] = None,
) -> LoanParams:
    """Create or replace the loan parameters for ``account_id`` and return the
    stored :class:`LoanParams`. ``rates`` (list of RateRow / (date, rate)) and
    ``extras`` (list of ExtraLine / (category, amount[, label])) REPLACE the
    existing child rows when provided; pass ``None`` to leave them untouched.
    ``interest_category`` is the category the computed-interest split posts to
    (a blank/None value clears it, restoring the module-default fallback).
    ``funding_account_id`` is the account the payment is made from (None =
    infer from history, see :func:`funding_account`)."""
    if interval not in _INTERVALS:
        raise ValueError(f"unknown payment interval: {interval!r}")
    if funding_account_id is not None and int(funding_account_id) == int(account_id):
        raise ValueError("a loan cannot be paid from itself")
    interest_category = (interest_category or "").strip() or None
    conn.execute(
        "INSERT INTO loan_params"
        "(account_id, original_principal, origination_date, term_months,"
        " payment_amount, payment_interval, note, interest_category,"
        " funding_account_id) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(account_id) DO UPDATE SET "
        "original_principal=excluded.original_principal, "
        "origination_date=excluded.origination_date, "
        "term_months=excluded.term_months, "
        "payment_amount=excluded.payment_amount, "
        "payment_interval=excluded.payment_interval, "
        "note=excluded.note, "
        "interest_category=excluded.interest_category, "
        "funding_account_id=excluded.funding_account_id",
        (account_id, int(original_principal), origination_date, int(term_months),
         int(payment_amount), interval, note, interest_category,
         None if funding_account_id is None else int(funding_account_id)),
    )
    if rates is not None:
        conn.execute("DELETE FROM loan_rates WHERE account_id=?", (account_id,))
        for item in rates:
            eff, rate = _unpack_rate(item)
            conn.execute(
                "INSERT INTO loan_rates(account_id, effective_date, annual_rate) "
                "VALUES (?,?,?)", (account_id, eff, rate))
    if extras is not None:
        conn.execute("DELETE FROM loan_extras WHERE account_id=?", (account_id,))
        for i, item in enumerate(extras):
            cat, amt, label, eff = _unpack_extra(item)
            conn.execute(
                "INSERT INTO loan_extras"
                "(account_id, category, amount, label, sort_order, effective_date) "
                "VALUES (?,?,?,?,?,?)", (account_id, cat, amt, label, i, eff))
    conn.commit()
    return get_loan_params(conn, account_id)


def add_rate(conn, account_id: int, effective_date: str, annual_rate) -> None:
    """Append (or replace, on the same effective_date) one interest-rate row."""
    conn.execute(
        "INSERT INTO loan_rates(account_id, effective_date, annual_rate) VALUES (?,?,?) "
        "ON CONFLICT(account_id, effective_date) DO UPDATE SET annual_rate=excluded.annual_rate",
        (account_id, effective_date, _rate_text(annual_rate)),
    )
    conn.commit()


def add_extra(conn, account_id: int, category: str, amount: int,
              label: Optional[str] = None,
              effective_date: Optional[str] = None) -> None:
    """Append one categorized extra-amount line (escrow / PMI / HOA / ...).
    ``effective_date`` (None == from the beginning) dates when the amount takes
    effect, so a loan's escrow/PMI can carry a history like its rate."""
    nxt = conn.execute(
        "SELECT COALESCE(MAX(sort_order) + 1, 0) FROM loan_extras WHERE account_id=?",
        (account_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO loan_extras"
        "(account_id, category, amount, label, sort_order, effective_date) "
        "VALUES (?,?,?,?,?,?)",
        (account_id, category, int(amount), label, nxt, effective_date))
    conn.commit()


def add_extra_change(conn, account_id: int, effective_date: str, category: str,
                     amount: int, label: Optional[str] = None) -> None:
    """Record a DATED change to a categorized extra (escrow / PMI / HOA / ...) --
    the analog of :func:`add_rate` for extras. The ``amount`` takes effect on and
    after ``effective_date``; earlier periods keep whatever amount was in force
    before, so payments already posted before the effective date are untouched
    while the schedule and every payment from the effective date forward use the
    new amount. Idempotent on (account, category, effective_date): a second call
    for the same date overwrites that dated row rather than stacking a duplicate
    (uniqueness is enforced here, not by a DB constraint)."""
    existing = conn.execute(
        "SELECT id FROM loan_extras "
        "WHERE account_id=? AND category=? AND effective_date=?",
        (account_id, category, effective_date),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE loan_extras SET amount=?, label=COALESCE(?, label) WHERE id=?",
            (int(amount), label, existing["id"]))
    else:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order) + 1, 0) FROM loan_extras WHERE account_id=?",
            (account_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO loan_extras"
            "(account_id, category, amount, label, sort_order, effective_date) "
            "VALUES (?,?,?,?,?,?)",
            (account_id, category, int(amount), label, nxt, effective_date))
    conn.commit()


def add_payment_change(conn, account_id: int, effective_date: str,
                       amount: int) -> None:
    """Record a DATED change to the whole scheduled payment (P&I + escrow/extras)
    -- the analog of :func:`add_rate`/:func:`add_extra_change` for the total. The
    ``amount`` takes effect on and after ``effective_date``; the projected
    schedule uses whatever total was in force on each period's date.

    The first time a loan's total is changed this seeds a BASELINE dated row at
    the loan's origination date carrying the pre-change (current flat)
    ``payment_amount``, so periods before the change keep the old total instead of
    inheriting the new one. Idempotent on (account, effective_date): a repeat call
    for the same date overwrites that row. Keeps the flat ``loan_params.payment_amount``
    in sync with the latest-dated total (still the loan's CURRENT total)."""
    row = conn.execute(
        "SELECT origination_date, payment_amount FROM loan_params WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"account {account_id} has no loan parameters")
    have_history = conn.execute(
        "SELECT COUNT(*) c FROM loan_payments WHERE account_id=?", (account_id,)
    ).fetchone()["c"]
    if not have_history:
        # Seed the pre-change total for earlier periods (see docstring).
        conn.execute(
            "INSERT INTO loan_payments(account_id, effective_date, amount) "
            "VALUES (?,?,?) ON CONFLICT(account_id, effective_date) DO NOTHING",
            (account_id, row["origination_date"] or "", row["payment_amount"]))
    conn.execute(
        "INSERT INTO loan_payments(account_id, effective_date, amount) "
        "VALUES (?,?,?) ON CONFLICT(account_id, effective_date) DO UPDATE SET "
        "amount=excluded.amount",
        (account_id, effective_date, int(amount)))
    latest = conn.execute(
        "SELECT amount FROM loan_payments WHERE account_id=? "
        "ORDER BY effective_date DESC, id DESC LIMIT 1", (account_id,)
    ).fetchone()
    conn.execute("UPDATE loan_params SET payment_amount=? WHERE account_id=?",
                 (latest["amount"], account_id))
    conn.commit()


def get_loan_params(conn, account_id: int) -> Optional[LoanParams]:
    """Load the loan parameters for ``account_id`` (with rate history + extras),
    or ``None`` if the account has none."""
    row = conn.execute(
        "SELECT * FROM loan_params WHERE account_id=?", (account_id,)
    ).fetchone()
    if row is None:
        return None
    rates = [
        RateRow(r["effective_date"], _D(r["annual_rate"]))
        for r in conn.execute(
            "SELECT * FROM loan_rates WHERE account_id=? ORDER BY effective_date, id",
            (account_id,),
        )
    ]
    extras = [
        ExtraLine(e["category"], e["amount"], e["label"], e["effective_date"])
        for e in conn.execute(
            "SELECT * FROM loan_extras WHERE account_id=? ORDER BY sort_order, id",
            (account_id,),
        )
    ]
    payments = [
        PaymentRow(p["effective_date"], p["amount"])
        for p in conn.execute(
            "SELECT * FROM loan_payments WHERE account_id=? ORDER BY effective_date, id",
            (account_id,),
        )
    ]
    return LoanParams(
        account_id=row["account_id"],
        original_principal=row["original_principal"],
        origination_date=row["origination_date"],
        term_months=row["term_months"],
        payment_amount=row["payment_amount"],
        interval=row["payment_interval"],
        rates=rates,
        extras=extras,
        payments=payments,
        interest_category=row["interest_category"],
        funding_account_id=(row["funding_account_id"]
                            if "funding_account_id" in row.keys() else None),
    )


# ---------------------------------------------------------------------------
# Which account pays the loan
# ---------------------------------------------------------------------------
def delete_loan_params(conn, account_id: int) -> bool:
    """Remove a loan's setup -- parameters, rate history, extras and payment
    changes -- so it schedules no further payments. The ACCOUNT and every
    transaction on it are untouched: this is "stop treating this account as an
    amortizing loan", not "delete the loan". This is what Delete does on a loan
    row of the Scheduled Payments manager; already-generated pre-entries stay
    in their registers, exactly as for a deleted manual definition. Returns
    whether a setup existed."""
    n = conn.execute("DELETE FROM loan_params WHERE account_id=?",
                     (account_id,)).rowcount
    for table in ("loan_rates", "loan_extras", "loan_payments"):
        conn.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
    conn.commit()
    return n > 0


def infer_funding_account(conn, loan_account_id: int) -> Optional[int]:
    """The account the loan has been paid FROM, read off history: the account
    of the most recent posted payment carrying a split leg INTO the loan (the
    Quicken shape -- interest + escrow + a ``[Loan]`` principal leg), else of
    the most recent plain transfer into it. ``None`` when nothing has ever paid
    it."""
    row = conn.execute(
        "SELECT t.account_id AS aid FROM splits s "
        "JOIN transactions t ON t.id = s.transaction_id "
        "WHERE s.transfer_account_id=? AND t.scheduled=0 AND t.amount<0 "
        "AND t.account_id<>? ORDER BY t.date DESC, t.id DESC LIMIT 1",
        (loan_account_id, loan_account_id)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT account_id AS aid FROM transactions "
            "WHERE transfer_account_id=? AND scheduled=0 AND amount<0 AND account_id<>? "
            "ORDER BY date DESC, id DESC LIMIT 1",
            (loan_account_id, loan_account_id)).fetchone()
    return int(row["aid"]) if row is not None else None


def set_funding_account(conn, loan_account_id: int,
                        funding_account_id: Optional[int]) -> None:
    """Store which account pays the loan (None = infer from history again).
    Written by Loan Setup, and by a one-off payment from another account,
    which pins the current default first so the one-off does not become it."""
    if funding_account_id is not None and int(funding_account_id) == int(loan_account_id):
        raise ValueError("a loan cannot be paid from itself")
    conn.execute("UPDATE loan_params SET funding_account_id=? WHERE account_id=?",
                 (None if funding_account_id is None else int(funding_account_id),
                  int(loan_account_id)))
    conn.commit()


def funding_account(conn, loan_account_id: int) -> Optional[int]:
    """The stored funding account, else the inferred one, else ``None`` (the
    payment is then pre-entered on the loan register itself)."""
    lp = get_loan_params(conn, loan_account_id)
    if lp is not None and lp.funding_account_id is not None:
        return int(lp.funding_account_id)
    return infer_funding_account(conn, loan_account_id)


def last_payment_payee(conn, loan_account_id: int, funding_account_id: int) -> Optional[str]:
    """The payee on the most recent posted payment from the funding account
    into the loan -- the name the user actually uses (``US Bank``), which a
    pre-entry should carry rather than a made-up one."""
    row = conn.execute(
        "SELECT t.payee FROM splits s JOIN transactions t ON t.id = s.transaction_id "
        "WHERE s.transfer_account_id=? AND t.account_id=? AND t.scheduled=0 "
        "AND t.amount<0 AND t.payee IS NOT NULL AND t.payee<>'' "
        "ORDER BY t.date DESC, t.id DESC LIMIT 1",
        (loan_account_id, funding_account_id)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT payee FROM transactions WHERE transfer_account_id=? AND account_id=? "
            "AND scheduled=0 AND amount<0 AND payee IS NOT NULL AND payee<>'' "
            "ORDER BY date DESC, id DESC LIMIT 1",
            (loan_account_id, funding_account_id)).fetchone()
    return row["payee"] if row is not None else None


# ---------------------------------------------------------------------------
# Rate history
# ---------------------------------------------------------------------------
def _active_rate(rates, date: str) -> Decimal:
    """The annual rate in effect on ``date``: the latest row whose effective_date
    is on/before ``date``. A date before the first rate row falls back to the
    earliest row (a loan always carries a rate). Raises if there are none."""
    if not rates:
        raise ValueError("loan has no interest-rate rows")
    ordered = sorted(rates, key=lambda r: r.effective_date)
    chosen = ordered[0]
    for r in ordered:
        if r.effective_date <= date:
            chosen = r
        else:
            break
    return chosen.annual_rate


def _period_rate(annual_rate: Decimal, periods_per_year: int) -> Decimal:
    """Per-period fractional rate from an annual percentage."""
    return _D(annual_rate) / _HUNDRED / Decimal(periods_per_year)


def _period_interest(lp: "LoanParams", pay_date: str, balance: int) -> int:
    """Interest owed for the accrual period ending on ``pay_date``: the plain
    periodic quantum, ``balance * annual_rate / periods_per_year``.

    US Bank charges one periodic quantum per scheduled payment REGARDLESS of the
    number of days in the period. the user's Recast statements confirm this
    to the cent across wildly uneven gaps -- as the register dates them, the
    02/27/2026 payment covered 26 days and the 04/01/2026 payment 33, and both were
    charged exactly one month.
    Days never enter the interest calculation, so a principal-only paydown landing
    mid-period is NOT prorated either: it retires principal when it posts, and the
    next payment accrues a full quantum on the reduced balance. Callers therefore
    subtract any paydown from ``balance`` BEFORE calling this.

    (An earlier model day-weighted the quantum across a mid-period paydown. Nothing
    in the statements supported it -- the only real paydown posted on a payment
    date, where both models agree -- and it charged $183.34 instead of $169.22 on
    the payment after the $50,000 recast when the register happened to date that
    paydown one day into the period.)"""
    rate = _period_rate(_active_rate(lp.rates, pay_date), lp.periods_per_year)
    return _cents(Decimal(balance) * rate)


def _active_extras(extras, date: str) -> list:
    """The extra lines in force on ``date``: for each category, the latest row
    whose effective_date is on/before ``date``. Unlike the rate history, an extra
    does NOT fall back to its earliest row -- a category whose first effective
    date is AFTER ``date`` contributes nothing then (an escrow/PMI added later
    does not apply retroactively; one that ended can be zeroed with a dated $0
    row). Rows with no effective_date (None / '') apply from the very beginning.
    Category order follows the input (stable for the split legs)."""
    by_cat: dict = {}
    order: list = []
    for e in extras:
        if e.category not in order:
            order.append(e.category)
        eff = e.effective_date or ""
        if eff > date:
            continue
        cur = by_cat.get(e.category)
        if cur is None or (cur.effective_date or "") <= eff:
            by_cat[e.category] = e
    return [by_cat[c] for c in order if c in by_cat]


def _active_payment(payments, default: int, date: str) -> int:
    """The whole scheduled payment (cents) in force on ``date``: the latest dated
    total whose effective_date is on/before ``date``. A date before the first
    dated row falls back to ``default`` (the loan's flat ``payment_amount``), and
    a loan with no dated history always uses ``default`` -- so a loan created
    without a payment history behaves exactly as before. Only once a change stores
    dated rows (with a baseline for the pre-change amount) does the schedule vary
    the total by date."""
    if not payments:
        return default
    ordered = sorted(payments, key=lambda p: (p.effective_date or ""))
    chosen = None
    for p in ordered:
        if (p.effective_date or "") <= date:
            chosen = p
        else:
            break
    return chosen.amount if chosen is not None else default


# ---------------------------------------------------------------------------
# Amortization
# ---------------------------------------------------------------------------
def standard_payment(principal_cents: int, annual_rate, term_months: int,
                     periods_per_year: int = 12) -> int:
    """The level PRINCIPAL+INTEREST payment (cents) that amortizes
    ``principal_cents`` over the term at a fixed ``annual_rate`` -- the standard
    annuity payment, ``P*r / (1 - (1+r)^-n)`` (or ``P/n`` at 0%). Handy for the
    setup wizard and tests; ADD any escrow/extras on top for the full payment."""
    n = round(term_months / 12 * periods_per_year)
    if n <= 0:
        raise ValueError("term must be positive")
    r = _period_rate(_D(annual_rate), periods_per_year)
    P = Decimal(principal_cents)
    if r == 0:
        return _cents(P / Decimal(n))
    factor = Decimal(1) - (Decimal(1) + r) ** (-n)
    return _cents(P * r / factor)


def _posted_payments(conn, account_id: int) -> dict:
    """The ACTUAL posted FULL payments (P&I + escrow) on the loan (liability)
    account, keyed by date -> total cents paid that date. A payment reduces the
    debt, so it posts as a POSITIVE amount; draws/charges (negative) and
    not-yet-posted pending pre-entries (``scheduled=1``) are excluded. A bare
    transfer with no split of its own is an ADDITIONAL PRINCIPAL payment (all
    principal, no interest/escrow) and is handled separately by
    :func:`_extra_principal_payments`, so it is excluded here rather than
    mis-decomposed as a whole scheduled payment. Same-date full payments are
    summed, so an extra amount folded into a scheduled payment row still folds
    into that period's total. The schedule replays over these real amounts, so a
    larger-than-scheduled payment drops the running balance faster and every
    later period's interest is charged on that lower balance (never forced back
    onto the original level-payment schedule)."""
    rows = conn.execute(
        "SELECT date, COALESCE(SUM(amount), 0) AS total FROM transactions "
        "WHERE account_id=? AND scheduled=0 AND amount > 0 "
        "AND (transfer_account_id IS NULL "
        "     OR id IN (SELECT transaction_id FROM splits)) "
        "GROUP BY date",
        (account_id,),
    ).fetchall()
    return {r["date"]: r["total"] for r in rows}


def _extra_principal_payments(conn, account_id: int) -> dict:
    """Additional PRINCIPAL-ONLY payments on the loan account, keyed by date ->
    total cents. An extra principal payment is entered as a plain, two-sided
    TRANSFER (money moved from checking to the loan, ``create_transfer`` -- so it
    owns a real counterpart leg via ``transactions.transfer_pair_id``) with no
    split of its own: the WHOLE amount retires principal -- no interest, no escrow
    -- so it must reduce the running balance and shorten the term rather than be
    split like a scheduled payment. Pending pre-entries (``scheduled=1``) and
    draws/charges (negative) are excluded.

    ``transfer_pair_id IS NOT NULL`` is REQUIRED so a ONE-SIDED loan-side leg is
    NOT counted here. A one-sided leg (``transfer_pair_id`` NULL) is the PRINCIPAL
    PORTION of a regular scheduled full payment -- either an imported ``[Loan]``
    principal split leg or the mirror a checking-side payment split creates
    (``splits.transfer_pair_id`` -> a one-sided loan leg, see
    ``ledger._create_split_mirror``). That principal is ALREADY accounted for by
    the period's scheduled/actual total (``payment_total - interest - escrow``);
    counting it AGAIN here double-retired it, driving the running balance (and
    therefore every later period's interest) far too low -- the "the split didn't
    work right" corruption on the user's Recast loan, where every monthly
    payment posts as exactly such a one-sided principal leg. Only a genuine,
    two-sided extra paydown (e.g. a recast lump) is an extra principal payment."""
    rows = conn.execute(
        "SELECT date, COALESCE(SUM(amount), 0) AS total FROM transactions "
        "WHERE account_id=? AND scheduled=0 AND amount > 0 "
        "AND transfer_account_id IS NOT NULL "
        "AND transfer_pair_id IS NOT NULL "
        "AND id NOT IN (SELECT transaction_id FROM splits) "
        "GROUP BY date",
        (account_id,),
    ).fetchall()
    return {r["date"]: r["total"] for r in rows}


def _transfer_in_payments(conn, account_id: int) -> dict:
    """The ACTUAL full loan payments that post on ANOTHER account (checking) as a
    split whose principal leg transfers INTO this loan -- a real recast
    model, where the loan side is only a one-sided mirror leg (no split of its own,
    so :func:`_posted_payments` on the loan account never sees them). Keyed date ->
    total cents actually paid (the funding-side payment amount, ``-amount``), with
    same-date payments summed. These are the amounts the schedule must replay over
    so the running balance -- and therefore every payment's interest -- tracks what
    was really paid, not the projected level total. Pending pre-entries
    (``scheduled=1``) and inflows (``amount>=0``) are excluded. A one-sided mirror
    leg on the loan account carries ``transfer_account_id`` but no split, so it is
    NOT matched here (only the funder-side parent, which owns the split, is)."""
    rows = conn.execute(
        "SELECT t.date AS date, COALESCE(SUM(-t.amount), 0) AS total "
        "FROM transactions t WHERE t.scheduled=0 AND t.amount < 0 "
        "AND t.id IN (SELECT transaction_id FROM splits "
        "             WHERE transfer_account_id=?) "
        "GROUP BY t.date",
        (account_id,),
    ).fetchall()
    return {r["date"]: int(r["total"]) for r in rows}


def _all_posted(conn, account_id: int) -> dict:
    """Every ACTUAL full payment affecting the loan, keyed date -> total cents:
    direct payments posted on the loan account (:func:`_posted_payments`) merged
    with checking-side transfer-in payments (:func:`_transfer_in_payments`). The
    two live on different accounts, so they never double-count; same-date totals
    are summed. This is the authoritative payment stream the schedule replays over
    (extra principal-only paydowns are carried separately)."""
    merged = dict(_posted_payments(conn, account_id))
    for date, total in _transfer_in_payments(conn, account_id).items():
        merged[date] = merged.get(date, 0) + total
    return merged


def _build_schedule(lp: LoanParams, actual_payments: Optional[dict] = None,
                    extra_principal: Optional[dict] = None) -> list[ScheduleRow]:
    """Payment-by-payment payoff over the ACTUAL posted payments (their real dates
    and totals) then the projected schedule, DAY-WEIGHTING interest across any
    mid-period balance change.

    Each period runs from the previous payment date to this one; its interest is
    one periodic quantum (:func:`_period_interest`) on the balance entering the
    payment, AFTER retiring any principal-only paydown posted since the previous
    payment -- so a paydown lowers the very next payment's interest in full, not
    one payment too late, and not prorated by the days it was in effect. ``actual_payments`` maps date -> the real total paid on that
    date (direct loan payments AND checking-side transfer-in payments); a date with
    a posted payment uses its real date and total, so the balance tracks what was
    really paid. Once the posted stream is exhausted the schedule projects forward
    on the payment grid using the total in force on each date (:func:`_active_payment`).
    ``extra_principal`` maps date -> a pure principal-only paydown: applied at its
    date (a same-day paydown reduces the balance right AFTER that day's payment, so
    the next period accrues fully on the reduced balance -- the recast case). The
    final payment is trimmed so the balance lands on exactly 0; a safety cap
    prevents a runaway if the payment barely covers interest after a rate hike."""
    actual_payments = actual_payments or {}
    extras = sorted((extra_principal or {}).items())        # [(date, cents), ...]
    ei = 0                                                   # next unconsumed extra
    ppy, advance = _INTERVALS[lp.interval]
    balance = lp.original_principal
    posted_dates = sorted(d for d in actual_payments if actual_payments[d] > 0)
    pi = 0                                                   # next posted date
    prev = lp.origination_date or _advance(
        lp.first_payment_date(), (advance[0], -advance[1]))
    scheduled = max(1, round(lp.term_months / 12 * ppy))
    cap = scheduled + max(24, ppy * 2)
    rows: list[ScheduleRow] = []
    period = 0
    # Any paydown dated on/before the first accrual start retires principal upfront.
    while ei < len(extras) and extras[ei][0] <= prev:
        balance -= extras[ei][1]
        ei += 1
    while balance > 0 and period < cap:
        period += 1
        # This payment's date/total: a real posted payment (actual date + amount)
        # while any remain, else the next projected grid date at its dated total.
        if pi < len(posted_dates):
            date = posted_dates[pi]
            payment_total = actual_payments[date]
            pi += 1
        else:
            date = lp.first_payment_date() if not rows else _advance(rows[-1].date, advance)
            payment_total = _active_payment(lp.payments, lp.payment_amount, date)
        # Principal-only paydowns strictly INSIDE (prev, date) day-weight this
        # period's interest and lower the balance it amortizes from.
        mid = []
        while ei < len(extras) and extras[ei][0] < date:
            pd_date, pd_amount = extras[ei]
            if pd_date > prev:
                mid.append((pd_date, pd_amount))
            else:
                balance -= pd_amount        # stray (dated on/before period start)
            ei += 1
        # A paydown posted since the last payment retires principal when it posts,
        # so THIS period accrues its full quantum on the already-reduced balance.
        balance -= sum(a for _, a in mid)
        if balance <= 0:
            break
        interest = _period_interest(lp, date, balance)
        # Escrow/PMI/extras in force this period (each carries its own dated history).
        escrow = sum(e.amount for e in _active_extras(lp.extras, date))
        principal = payment_total - interest - escrow
        if principal <= 0:
            # Payment no longer covers interest + escrow: it would never amortize.
            break
        if principal >= balance:
            principal = balance
            payment = principal + interest + escrow
        else:
            payment = payment_total
        balance -= principal
        rows.append(ScheduleRow(period, date, payment, principal, interest, escrow, balance))
        # A paydown dated ON this payment date retires principal AFTER the payment,
        # so this payment kept its full interest and the NEXT period starts on the
        # reduced balance (0 days at the old balance -- the same-day recast).
        while ei < len(extras) and extras[ei][0] == date:
            balance -= extras[ei][1]
            ei += 1
        prev = date
    return rows


def amortization_schedule(conn, account_id: int) -> list[ScheduleRow]:
    """The full amortization schedule for the loan on ``account_id``, honoring its
    interest-rate history over time AND any extra principal already posted to the
    register (which retires the loan ahead of term). Raises ``LookupError`` if the
    account has no loan parameters."""
    lp = get_loan_params(conn, account_id)
    if lp is None:
        raise LookupError(f"account {account_id} has no loan parameters")
    return _build_schedule(lp, _all_posted(conn, account_id),
                           _extra_principal_payments(conn, account_id))


def _outstanding_before(conn, account_id: int, lp: LoanParams, date: str) -> int:
    """Outstanding principal just BEFORE a payment on ``date`` -- the running
    balance after every payment dated strictly earlier, replaying the schedule
    over the ACTUAL posted payments (full payments AND principal-only extras).
    Interest for the payment on ``date`` is charged on this balance, so a
    manually-entered extra principal payment lowers it and therefore lowers this
    period's (and every later period's) interest."""
    balance = lp.original_principal
    for row in _build_schedule(lp, _all_posted(conn, account_id),
                               _extra_principal_payments(conn, account_id)):
        if row.date < date:
            balance = row.balance
        else:
            break
    return balance


# ---------------------------------------------------------------------------
# Per-payment split
# ---------------------------------------------------------------------------
def payment_split(conn, account_id: int, date: str, amount_cents: int) -> PaymentSplit:
    """Decompose a payment of ``amount_cents`` on ``date`` into
    principal / interest / escrow(+other categorized extras).

    ``interest`` is the periodic quantum for the accrual period ending on ``date``
    (:func:`_period_interest` -- ``balance * period_rate``, charged on the balance
    left after any principal-only paydown posted since the previous payment);
    each extra line contributes its own categorized amount; and
    ``principal = amount - interest - sum(extras)``. So principal + interest +
    every extra reconstitutes the payment to the cent -- a whole imported bank
    payment can be posted as Quicken's real split with nothing lost. Raises
    ``LookupError`` if the account has no loan parameters."""
    lp = get_loan_params(conn, account_id)
    if lp is None:
        raise LookupError(f"account {account_id} has no loan parameters")
    annual = _active_rate(lp.rates, date)
    prate = _period_rate(annual, lp.periods_per_year)
    # Replay the actual posted payments + paydowns to get the balance ENTERING this
    # payment and its interest. Prefer the schedule row on ``date`` (its balance
    # already reflects paydowns retired within the period); fall back to a direct
    # replay for a date that is not itself a payment row (an off-grid import/pending).
    sched = _build_schedule(lp, _all_posted(conn, account_id),
                            _extra_principal_payments(conn, account_id))
    row = next((r for r in sched if r.date == date), None)
    if row is not None:
        interest = row.interest
        balance_before = row.balance + row.principal
    else:
        prev_date = lp.origination_date or lp.first_payment_date()
        balance_before = lp.original_principal
        for r in sched:
            if r.date < date:
                prev_date, balance_before = r.date, r.balance
            else:
                break
        mid = sorted((d, a) for d, a in
                     _extra_principal_payments(conn, account_id).items()
                     if prev_date < d < date)
        balance_before -= sum(a for _, a in mid)
        interest = _period_interest(lp, date, balance_before)
    # The escrow/PMI/extras in force ON ``date`` (each with its own dated
    # history), so a payment posted before an escrow change keeps the old amount
    # and one on/after it gets the new -- deterministically, from stored data.
    active = _active_extras(lp.extras, date)
    extras = [ExtraSplit(e.category, e.amount, e.label) for e in active]
    escrow_total = sum(e.amount for e in active)
    principal = amount_cents - interest - escrow_total
    return PaymentSplit(
        date=date,
        amount=amount_cents,
        principal=principal,
        interest=interest,
        escrow=escrow_total,
        extras=extras,
        balance_before=balance_before,
        balance_after=balance_before - principal,
        annual_rate=annual,
        period_rate=prate,
    )
