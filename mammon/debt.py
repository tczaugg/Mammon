"""Debt payoff projection: revolving-debt terms and one engine.

The Budget page's pay-down line answers one question -- "with this much extra
principal a month, when is this debt gone and what does the interest cost?" --
by running the debt twice, at its regular payment and with the extra added
(:func:`project_extra`). This module returns numbers; it does not choose an
amount or an account for the household.

WHY ONE ENGINE. "Snowball" and "avalanche" are not two algorithms. They are one
simulation with a different sort key, and the CFPB's "highest interest rate
method" is the second of them under another name. :func:`simulate` takes a
strategy name and everything else about the month is identical, so a
one-member run -- all the Budget page makes -- uses the same arithmetic a
multi-debt run would.

WHY THE BUDGET IS A TOTAL. ``budget_cents`` is the WHOLE monthly outlay, not an
extra on top of the minimums. The alternative framing needs a separate
"snowball amount" accumulator -- the payment freed up by a cleared debt, carried
forward -- and that accumulator is the classic double-count bug, because the
minimum it was derived from moves every month as the balance falls. Cascading
the entire remainder down the ordered list each month (step 5 of
:func:`simulate`) produces the roll-in for free and cannot double-count.

WHY INTEREST ACCRUES FIRST. Each simulated month charges interest and then
applies payment, which is what a statement cycle does. The reverse order
understates interest by a month's worth on every debt, compounding.

WHAT IS NOT HERE. No amortization. An installment loan enters through
:func:`mammon.loans.standard_payment` for its regular payment; a second copy of
the mortgage math in this module would be a defect, not a convenience. No
transaction writing either: the projection is a READ, it emits nothing, and
``mammon.ledger`` remains the only writer of transaction rows -- which is also
why ``debt_terms.scheduled_id`` exists, so a debt already carrying a scheduled
payment can be recognized instead of counted twice. And no stored results: the
tables hold what the user typed and nothing the engine computed, because a
payoff month saved today is a lie after the next import.

ACCURACY, STATED RATHER THAN IMPLIED. Interest is APR/12 on the month-end
balance. A real issuer charges daily interest on an average daily balance, so
the projection differs from a statement by roughly a percent, more in a short or
long month, and a payoff is labeled as a MONTH, never a day -- a projection accurate to plus or
minus a month has no business naming the 14th.

Rounding happens at exactly one site per debt-month: the interest accrual.
Principal is derived by subtraction, never rounded, and never clamped -- under
negative amortization it is genuinely negative and the balance genuinely grows.
The engine reports that (``negative_amortizing``, no payoff month) rather than
quietly raising the minimum to cover the interest, which would be a projection
of a plan the issuer never offered. Same reasoning for minimums that exceed the
budget: the shortfall is reported, nothing is pro-rated, and no payoff date is
shown at all (see SRD 5.12h).
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from mammon import budgets, ledger

__all__ = [
    "ExtraPrincipal", "project_debt", "project_extra", "set_apr",
    "DEBT_ACCOUNT_TYPES", "MIN_FORMS", "DEFAULT_MIN_FORM",
    "DEFAULT_MIN_FLOOR_CENTS", "DEFAULT_MIN_PCT", "SNOWBALL", "AVALANCHE",
    "CUSTOM", "STRATEGIES", "MAX_MONTHS",
    "DebtTerms", "DebtInput", "DebtOutcome", "MonthRow", "Simulation",
    "set_terms", "get_terms", "debt_accounts",
    "monthly_rate", "required_minimum", "simulate",
]

#: Account kinds that can carry a debt to pay down. ``credit`` is a revolving
#: card, ``liability`` anything else owed (an installment loan, a note, a
#: balance a relative is owed). Both hold a NEGATIVE ledger balance when money
#: is owed; the sign flip to a positive magnitude happens once, in
#: :func:`_debt_input`, and nowhere else.
DEBT_ACCOUNT_TYPES = ("credit", "liability")

#: The three minimum-payment forms real issuers use:
#: ``A`` a flat floor, ``B`` the greater of the floor and a percent of the
#: balance, ``C`` the greater of the floor and a percent plus that month's
#: interest. ``C`` is the default because it is the commonest.
MIN_FORMS = ("A", "B", "C")
DEFAULT_MIN_FORM = "C"
DEFAULT_MIN_FLOOR_CENTS = 2500
DEFAULT_MIN_PCT = Decimal("1")          # percent of balance

SNOWBALL = "snowball"                   # smallest balance first, rate ignored
AVALANCHE = "avalanche"                 # highest APR first
CUSTOM = "custom"                       # the order the user stored
STRATEGIES = (SNOWBALL, AVALANCHE, CUSTOM)

#: Months the projection will run before it gives up. Fifty years is past any
#: honest plan, so hitting it is an OUTCOME the page reports, not an error.
MAX_MONTHS = 600

_CENT = Decimal(1)
_HUNDRED = Decimal(100)
_MONTHS_PER_YEAR = Decimal(1200)        # percent per year -> fraction per month


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _cents(value: Decimal) -> int:
    """A Decimal to integer cents, ROUND_HALF_UP -- the app's one rounding rule.
    This is the ONLY rounding site in the simulation."""
    return int(Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP))


def _dec(value, default: Decimal = Decimal(0)) -> Decimal:
    """A stored TEXT rate (or a number) as a Decimal, never a float."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):        # a caller's convenience, not storage
        return Decimal(repr(value))
    return Decimal(str(value))


def _validate_date(date: str) -> str:
    _dt.date.fromisoformat(str(date))
    return str(date)


def _opt_date(date: Optional[str]) -> Optional[str]:
    return None if date in (None, "") else _validate_date(date)


def _today() -> str:
    return _dt.date.today().isoformat()


def _period_of(date: str) -> str:
    return _validate_date(date)[:7]


def _validate_period(period: str) -> str:
    budgets._split_period(period)
    return period


def _period_plus(period: str, months: int) -> str:
    """``'YYYY-MM'`` advanced by ``months`` whole months."""
    year, month = budgets._split_period(period)
    total = year * 12 + (month - 1) + int(months)
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def monthly_rate(apr) -> Decimal:
    """A percent-per-year APR as the fraction charged per month, APR/12, at FULL
    Decimal precision. Never pre-round this: 19.99/1200 rounded to six places
    moves a long payoff by a month."""
    return _dec(apr) / _MONTHS_PER_YEAR


def _validate_strategy(strategy: str) -> str:
    s = str(strategy or "").strip().lower()
    if s not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    return s


def _validate_min_form(form: str) -> str:
    f = str(form or DEFAULT_MIN_FORM).strip().upper()
    if f not in MIN_FORMS:
        raise ValueError(f"unknown minimum-payment form {form!r}")
    return f


# ---------------------------------------------------------------------------
# Stored inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DebtTerms:
    """What one account's lending agreement says. Rates are percent-per-year
    Decimals; ``estimated`` means Mammon synthesized these rather than the user
    entering them, and the page must say so."""
    account_id: int
    apr: Decimal = Decimal(0)
    min_form: str = DEFAULT_MIN_FORM
    min_floor_cents: int = DEFAULT_MIN_FLOOR_CENTS
    min_pct: Decimal = DEFAULT_MIN_PCT
    credit_limit_cents: Optional[int] = None
    due_day: Optional[int] = None
    promo_rate: Optional[Decimal] = None
    promo_end_date: Optional[str] = None
    promo_deferred: bool = False
    scheduled_id: Optional[int] = None
    estimated: bool = False
    updated_at: str = ""


@dataclass(frozen=True)
class DebtInput:
    """One debt as the engine sees it. ``balance_cents`` is a POSITIVE magnitude
    of what is owed -- the flip out of the ledger's negative liability balance
    happens in :func:`_debt_input`, once, and no sign rule is added to the
    domain layer."""
    account_id: int
    name: str
    balance_cents: int
    monthly_rate: Decimal
    min_form: str = DEFAULT_MIN_FORM
    min_floor_cents: int = DEFAULT_MIN_FLOOR_CENTS
    min_pct: Decimal = DEFAULT_MIN_PCT
    apr: Decimal = Decimal(0)
    promo_rate: Optional[Decimal] = None
    promo_end_date: Optional[str] = None
    promo_deferred: bool = False
    scheduled_id: Optional[int] = None
    estimated: bool = False


# ---------------------------------------------------------------------------
# Results (computed, never stored)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MonthRow:
    """One debt in one simulated month. The engine keeps these because a page
    that shows a per-month split has to iterate -- the closed form cannot
    produce one, and neither can it handle a minimum that recomputes from the
    balance, which every revolving debt has."""
    month: int                  # 1-based
    period: str                 # ISO 'YYYY-MM'
    account_id: int
    interest_cents: int
    payment_cents: int
    balance_cents: int          # after this month's payment


@dataclass(frozen=True)
class DebtOutcome:
    """What the projection says about one debt."""
    account_id: int
    name: str
    balance_cents: int          # opening balance, positive magnitude
    apr: Decimal
    minimum_cents: int          # the FIRST month's required minimum
    first_interest_cents: int   # the first month's accrual, for the Reg Z line
    interest_cents: int = 0     # interest charged over the whole projection
    paid_cents: int = 0
    cleared_month: Optional[int] = None   # 1-based; 0 = already clear; None = never
    negative_amortizing: bool = False
    estimated: bool = False

    @property
    def clears(self) -> bool:
        return self.cleared_month is not None


@dataclass(frozen=True)
class Simulation:
    """One strategy's projection. Nothing here is written to the database."""
    strategy: str
    budget_cents: int
    start_period: str
    debts: tuple = ()                   # tuple[DebtOutcome, ...], plan order
    schedule: tuple = ()                # tuple[MonthRow, ...], month then plan order
    total_interest_cents: int = 0
    total_paid_cents: int = 0
    payoff_month: Optional[int] = None  # 1-based; 0 = nothing owed; None = no date
    months_run: int = 0
    minimum_total_cents: int = 0
    negative_amortizing: bool = False
    cap_reached: bool = False
    minimums_exceed_budget: bool = False

    @property
    def pays_off(self) -> bool:
        return self.payoff_month is not None

    @property
    def shortfall_cents(self) -> int:
        """How far the monthly amount falls short of the required minimums."""
        return max(0, self.minimum_total_cents - self.budget_cents)

    def period_of(self, month: Optional[int]) -> Optional[str]:
        """The ``'YYYY-MM'`` of a 1-based simulated month. A payoff is a MONTH:
        the accrual convention is not accurate to a day."""
        if month is None or month <= 0:
            return None
        return _period_plus(self.start_period, month - 1)

    @property
    def payoff_period(self) -> Optional[str]:
        return self.period_of(self.payoff_month)

    def outcome(self, account_id: int) -> Optional[DebtOutcome]:
        for d in self.debts:
            if d.account_id == int(account_id):
                return d
        return None

    def month_payments(self, month: int) -> dict:
        """``account id -> cents paid`` in one 1-based simulated month."""
        return {r.account_id: r.payment_cents for r in self.schedule
                if r.month == int(month)}


@dataclass
class _Live:
    """Mutable per-debt state inside the loop. Not public: a caller gets
    :class:`DebtOutcome` instead, which cannot drift."""
    debt: DebtInput
    order: int                              # position in the plan, for CUSTOM
    balance_cents: int = 0
    interest_cents: int = 0
    paid_cents: int = 0
    deferred_cents: int = 0                 # accrued under a deferred-interest promo
    cleared_month: Optional[int] = None
    negative_amortizing: bool = False
    first_interest_cents: int = 0
    minimum_cents: int = 0


# ---------------------------------------------------------------------------
# Terms CRUD
# ---------------------------------------------------------------------------
def _terms_from_row(row) -> DebtTerms:
    return DebtTerms(
        account_id=int(row["account_id"]),
        apr=_dec(row["apr"]),
        min_form=_validate_min_form(row["min_form"]),
        min_floor_cents=int(row["min_floor_cents"]),
        min_pct=_dec(row["min_pct"]),
        credit_limit_cents=(None if row["credit_limit_cents"] is None
                            else int(row["credit_limit_cents"])),
        due_day=None if row["due_day"] is None else int(row["due_day"]),
        promo_rate=(None if row["promo_rate"] in (None, "")
                    else _dec(row["promo_rate"])),
        promo_end_date=row["promo_end_date"] or None,
        promo_deferred=bool(row["promo_deferred"]),
        scheduled_id=(None if row["scheduled_id"] is None
                      else int(row["scheduled_id"])),
        estimated=bool(row["estimated"]),
        updated_at=row["updated_at"] or "",
    )


def set_terms(conn: sqlite3.Connection, account_id: int, *,
              apr=Decimal(0), min_form: str = DEFAULT_MIN_FORM,
              min_floor_cents: int = DEFAULT_MIN_FLOOR_CENTS,
              min_pct=DEFAULT_MIN_PCT,
              credit_limit_cents: Optional[int] = None,
              due_day: Optional[int] = None,
              promo_rate=None, promo_end_date: Optional[str] = None,
              promo_deferred: bool = False,
              scheduled_id: Optional[int] = None,
              estimated: bool = False,
              updated_at: Optional[str] = None) -> DebtTerms:
    """Upsert one account's lending terms. A full row: the dialog that edits
    terms edits all of them, and a partial update that silently kept last
    month's promo rate would project interest nobody is charging."""
    acct = ledger.get_account(conn, int(account_id))
    if acct is None:
        raise KeyError(f"no account {account_id}")
    if (acct["type"] or "") not in DEBT_ACCOUNT_TYPES:
        raise ValueError(f"account {account_id} is a {acct['type']!r} account; "
                         f"debt terms belong to {DEBT_ACCOUNT_TYPES}")
    apr_d = _dec(apr)
    pct_d = _dec(min_pct)
    if apr_d < 0:
        raise ValueError("apr cannot be negative")
    if not (0 <= pct_d <= _HUNDRED):
        raise ValueError("min_pct is a percent of the balance, 0 to 100")
    if int(min_floor_cents) < 0:
        raise ValueError("min_floor_cents cannot be negative")
    if credit_limit_cents is not None and int(credit_limit_cents) < 0:
        raise ValueError("credit_limit_cents cannot be negative")
    if due_day is not None and not (1 <= int(due_day) <= 31):
        raise ValueError("due_day is a day of the month, 1 to 31")
    promo = None if promo_rate in (None, "") else _dec(promo_rate)
    if promo is not None and promo < 0:
        raise ValueError("promo_rate cannot be negative")
    end = _opt_date(promo_end_date)
    if promo is not None and end is None:
        raise ValueError("a promotional rate needs promo_end_date: an "
                         "open-ended promotion is just the APR")
    conn.execute(
        """
        INSERT INTO debt_terms (account_id, apr, min_form, min_floor_cents,
                                min_pct, credit_limit_cents, due_day,
                                promo_rate, promo_end_date, promo_deferred,
                                scheduled_id, estimated, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            apr = excluded.apr, min_form = excluded.min_form,
            min_floor_cents = excluded.min_floor_cents,
            min_pct = excluded.min_pct,
            credit_limit_cents = excluded.credit_limit_cents,
            due_day = excluded.due_day, promo_rate = excluded.promo_rate,
            promo_end_date = excluded.promo_end_date,
            promo_deferred = excluded.promo_deferred,
            scheduled_id = excluded.scheduled_id,
            estimated = excluded.estimated, updated_at = excluded.updated_at
        """,
        (int(account_id), str(apr_d), _validate_min_form(min_form),
         int(min_floor_cents), str(pct_d),
         None if credit_limit_cents is None else int(credit_limit_cents),
         None if due_day is None else int(due_day),
         None if promo is None else str(promo), end,
         1 if promo_deferred else 0,
         None if scheduled_id is None else int(scheduled_id),
         1 if estimated else 0,
         _validate_date(updated_at) if updated_at else _today()),
    )
    conn.commit()
    return get_terms(conn, account_id)


def get_terms(conn: sqlite3.Connection, account_id: int) -> Optional[DebtTerms]:
    """One account's stored terms, or ``None`` when it has none -- which is not
    an error: :func:`_debt_input` synthesizes them and marks them estimated."""
    row = conn.execute("SELECT * FROM debt_terms WHERE account_id = ?",
                       (int(account_id),)).fetchone()
    return None if row is None else _terms_from_row(row)


# ---------------------------------------------------------------------------
# Turning accounts into engine inputs
# ---------------------------------------------------------------------------
def debt_accounts(conn: sqlite3.Connection, *,
                  include_closed: bool = False,
                  as_of: Optional[str] = None,
                  owing_only: bool = True) -> list:
    """The accounts a pay-down line can name, by name.

    ``owing_only`` (the default) keeps only the accounts that actually owe
    something as of ``as_of``: a retired loan and a card paid off sit at a zero
    balance, and a card the user has overpaid sits at a credit balance, and
    neither is a thing to pay down. The user's ruling on the Save & Pay Down
    list - there is no point in listing zero-balance accounts, and it is hard
    to pay off a debt that does not exist. THIS is the one place that decides
    it; a caller that genuinely wants every debt-type account regardless of
    balance (a chooser for entering terms, say) passes ``owing_only=False``
    rather than re-deriving the test.

    The sign convention is the ledger's: money owed is a NEGATIVE balance on a
    debt account, so owing means ``balance < 0``. Zero and positive are both
    out.
    """
    out = [a for a in ledger.list_accounts(conn, include_closed=include_closed,
                                           include_hidden=True)
           if (a["type"] or "") in DEBT_ACCOUNT_TYPES]
    if owing_only:
        out = [a for a in out
               if int(ledger.account_balance(conn, int(a["id"]), as_of)) < 0]
    return out


def _loan_terms(conn: sqlite3.Connection, account_id: int) -> Optional[DebtTerms]:
    """Terms DERIVED from an installment loan's own setup, when it has one.

    The APR is the rate in force and the minimum is the level principal-and-
    interest payment from :func:`mammon.loans.standard_payment` -- form A, a flat
    required payment, which is what an installment loan has. Escrow and other
    extras are left out on purpose: they are not debt service and retire no
    balance. The amortization is NOT re-derived here; that is loans.py's job and
    a second copy of it would be the defect."""
    from mammon import loans

    params = loans.get_loan_params(conn, int(account_id))
    if params is None or params.term_months <= 0:
        return None
    apr = _dec(params.rates[-1].annual_rate) if params.rates else Decimal(0)
    try:
        payment = loans.standard_payment(params.original_principal, apr,
                                         params.term_months)
    except (ValueError, ZeroDivisionError):
        return None
    return DebtTerms(account_id=int(account_id), apr=apr, min_form="A",
                     min_floor_cents=max(0, int(payment)),
                     min_pct=Decimal(0), estimated=True, updated_at=_today())


def _synthesized_terms(conn: sqlite3.Connection, account_id: int) -> DebtTerms:
    """Terms for an account that has none: an installment loan's own setup when
    there is one, else the conservative revolving default (a 25.00 floor plus
    one percent plus interest, at a zero APR until the user enters one). Marked
    ``estimated`` either way, because the page has to be able to say so rather
    than present a guess as the issuer's number."""
    derived = _loan_terms(conn, account_id)
    if derived is not None:
        return derived
    return DebtTerms(account_id=int(account_id), apr=Decimal(0),
                     min_form=DEFAULT_MIN_FORM,
                     min_floor_cents=DEFAULT_MIN_FLOOR_CENTS,
                     min_pct=DEFAULT_MIN_PCT, estimated=True,
                     updated_at=_today())


def _debt_input(conn: sqlite3.Connection, acct, as_of: Optional[str]) -> DebtInput:
    """One account as a :class:`DebtInput`. THE SIGN FLIP LIVES HERE: a debt
    account holds a negative ledger balance when money is owed, and every figure
    past this point is a positive magnitude. A credit balance (the card owes
    the user) comes through as zero, not as a negative debt."""
    account_id = int(acct["id"])
    balance = ledger.account_balance(conn, account_id, as_of)
    terms = get_terms(conn, account_id) or _synthesized_terms(conn, account_id)
    return DebtInput(
        account_id=account_id, name=acct["name"],
        balance_cents=max(0, -int(balance)),
        monthly_rate=monthly_rate(terms.apr),
        min_form=terms.min_form, min_floor_cents=terms.min_floor_cents,
        min_pct=terms.min_pct, apr=terms.apr,
        promo_rate=terms.promo_rate, promo_end_date=terms.promo_end_date,
        promo_deferred=terms.promo_deferred, scheduled_id=terms.scheduled_id,
        estimated=terms.estimated)


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------
def required_minimum(debt: DebtInput, balance_cents: int,
                     interest_cents: int = 0) -> int:
    """This month's required minimum on ``debt``, never more than the balance.

    Form A is the flat floor, B the greater of the floor and a percent of the
    balance, C the greater of the floor and that percent PLUS the month's
    interest. Only C guarantees the balance falls; B is how a balance grows
    forever, and the engine reports that rather than quietly switching to C.

    ``balance_cents`` is the balance AFTER the month's interest, which is what
    an issuer bills a percentage of -- a statement balance includes the cycle's
    finance charge. :func:`simulate` calls this in that order for that reason."""
    balance = max(0, int(balance_cents))
    form = _validate_min_form(debt.min_form)
    floor = max(0, int(debt.min_floor_cents or 0))
    if form == "A":
        minimum = floor
    else:
        part = _cents(Decimal(balance) * _dec(debt.min_pct) / _HUNDRED)
        if form == "C":
            part += max(0, int(interest_cents))
        minimum = max(floor, part)
    return max(0, min(minimum, balance))


def _effective_apr(debt: DebtInput, period: str) -> Decimal:
    """The APR in force in ``period`` -- the promotional rate through the month
    the promotion ends, the ordinary APR after it. The engine asks this every
    month, which is what lets an expiring 0 percent promo re-target the plan."""
    if debt.promo_rate is not None and debt.promo_end_date:
        if period <= debt.promo_end_date[:7]:
            return _dec(debt.promo_rate)
    return _dec(debt.apr)


def _rate_for(debt: DebtInput, period: str) -> Decimal:
    if debt.promo_rate is not None and debt.promo_end_date:
        return monthly_rate(_effective_apr(debt, period))
    return _dec(debt.monthly_rate)


def _sort_key(strategy: str, live: _Live, period: str):
    """1.3's ordering, ties included: same APR, smaller balance first; same
    balance, higher APR first; then the account id, so the order is total and a
    test can pin it."""
    apr = _effective_apr(live.debt, period)
    if strategy == SNOWBALL:
        return (live.balance_cents, -apr, live.debt.account_id)
    if strategy == AVALANCHE:
        return (-apr, live.balance_cents, live.debt.account_id)
    return (live.order, live.debt.account_id)


def _accrue(live: _Live, period: str) -> int:
    """Charge one month's interest, the single rounding site. Returns the amount
    charged, including a deferred-interest promotion's retroactive lump in the
    month it expires -- the whole point of deferred interest is that it is not
    waived, it is waiting."""
    rate = _rate_for(live.debt, period)
    interest = _cents(Decimal(live.balance_cents) * rate)
    debt = live.debt
    if debt.promo_deferred and debt.promo_end_date:
        end_period = debt.promo_end_date[:7]
        if period < end_period:
            live.deferred_cents += _cents(Decimal(live.balance_cents)
                                          * _dec(debt.monthly_rate))
        elif period == end_period:
            live.deferred_cents += _cents(Decimal(live.balance_cents)
                                          * _dec(debt.monthly_rate))
            if live.balance_cents > 0:
                interest += live.deferred_cents
            live.deferred_cents = 0
    live.balance_cents += interest
    live.interest_cents += interest
    return interest


def simulate(debts: Sequence[DebtInput], budget_cents: int,
             strategy: str = AVALANCHE, *, max_months: int = MAX_MONTHS,
             start_period: Optional[str] = None) -> Simulation:
    """Run one strategy month by month and report what it costs.

    The month, in the order it happens: order the live debts by the strategy's
    key, charge interest on each, pay every required minimum, then cascade the
    WHOLE remainder down the ordered list, then note anything that reached zero.
    Step 5's cascade is the roll-in; there is no separate snowball amount to
    track and nothing to double-count.

    Nothing is pro-rated. If the minimums come to more than ``budget_cents``
    that is detected BEFORE the loop runs and returned as a shortfall with no
    payoff date, because a part-paid minimum is a missed payment and the
    projection has no business drawing a line through one."""
    strategy = _validate_strategy(strategy)
    budget = int(budget_cents)
    if budget < 0:
        raise ValueError("budget_cents cannot be negative")
    if int(max_months) <= 0:
        raise ValueError("max_months must be positive")
    start = start_period or _dt.date.today().strftime("%Y-%m")
    _validate_period(start)

    state = [_Live(debt=d, order=i, balance_cents=max(0, int(d.balance_cents)))
             for i, d in enumerate(debts)]
    for s in state:
        if s.balance_cents <= 0:
            s.cleared_month = 0          # nothing owed: clear before we start

    # The first month's figures, which the table shows and the pre-check needs.
    for s in state:
        rate = _rate_for(s.debt, start)
        s.first_interest_cents = _cents(Decimal(s.balance_cents) * rate)
        s.minimum_cents = required_minimum(
            s.debt, s.balance_cents + s.first_interest_cents,
            s.first_interest_cents)
    minimum_total = sum(s.minimum_cents for s in state)

    live = [s for s in state if s.balance_cents > 0]
    if live and minimum_total > budget:
        return _result(strategy, budget, start, state, (), 0, None,
                       minimum_total, cap_reached=False,
                       minimums_exceed_budget=True)

    rows: list = []
    month = 0
    while live and month < int(max_months):
        month += 1
        period = _period_plus(start, month - 1)
        order = sorted(live, key=lambda s: _sort_key(strategy, s, period))   # 1
        accrued = {id(s): _accrue(s, period) for s in live}                  # 2
        remaining = budget                                                  # 3
        paid = {id(s): 0 for s in live}
        for s in live:                                                       # 4
            m = min(required_minimum(s.debt, s.balance_cents, accrued[id(s)]),
                    s.balance_cents, remaining)
            s.balance_cents -= m
            s.paid_cents += m
            paid[id(s)] += m
            remaining -= m
        for s in order:                                                      # 5
            if remaining <= 0:
                break
            p = min(remaining, s.balance_cents)
            s.balance_cents -= p
            s.paid_cents += p
            paid[id(s)] += p
            remaining -= p
        for s in live:                                                       # 6
            if accrued[id(s)] > 0 and paid[id(s)] < accrued[id(s)]:
                s.negative_amortizing = True
            rows.append(MonthRow(month=month, period=period,
                                 account_id=s.debt.account_id,
                                 interest_cents=accrued[id(s)],
                                 payment_cents=paid[id(s)],
                                 balance_cents=s.balance_cents))
            if s.balance_cents == 0 and s.cleared_month is None:
                s.cleared_month = month
        live = [s for s in live if s.balance_cents > 0]                      # 7

    cap = bool(live)
    payoff = None if live else month
    return _result(strategy, budget, start, state, tuple(rows), month, payoff,
                   minimum_total, cap_reached=cap, minimums_exceed_budget=False)


def _result(strategy, budget, start, state, rows, months_run, payoff,
            minimum_total, *, cap_reached, minimums_exceed_budget) -> Simulation:
    outcomes = tuple(
        DebtOutcome(account_id=s.debt.account_id, name=s.debt.name,
                    balance_cents=max(0, int(s.debt.balance_cents)),
                    apr=_dec(s.debt.apr), minimum_cents=s.minimum_cents,
                    first_interest_cents=s.first_interest_cents,
                    interest_cents=s.interest_cents, paid_cents=s.paid_cents,
                    cleared_month=s.cleared_month,
                    negative_amortizing=s.negative_amortizing,
                    estimated=bool(s.debt.estimated))
        for s in state)
    return Simulation(
        strategy=strategy, budget_cents=budget, start_period=start,
        debts=outcomes, schedule=rows,
        # Summed from the per-debt integers and never re-rounded: rounding a
        # total that is already a sum of rounded cents is how a projection and
        # its own table stop agreeing.
        total_interest_cents=sum(d.interest_cents for d in outcomes),
        total_paid_cents=sum(d.paid_cents for d in outcomes),
        payoff_month=payoff, months_run=int(months_run),
        minimum_total_cents=int(minimum_total),
        negative_amortizing=any(d.negative_amortizing for d in outcomes),
        cap_reached=bool(cap_reached),
        minimums_exceed_budget=bool(minimums_exceed_budget))


# ---------------------------------------------------------------------------
# A pay-down line's projection
# ---------------------------------------------------------------------------
def project_debt(conn: sqlite3.Connection, account_id: int, monthly_cents: int, *,
                 as_of: Optional[str] = None,
                 apr: Optional[Decimal] = None) -> Simulation:
    """One debt at one monthly payment: when it clears and what the interest
    costs (SRD 5.12h, as a pay-down line's own projection).

    The account's terms as stored or synthesized, with ``apr`` overriding the
    rate when the page offers one the user typed; a single-member run of
    :func:`simulate`, so the arithmetic is the engine's own and a
    payment below the minimum comes back as a shortfall with no date rather
    than a line drawn through a missed payment. Nothing is stored."""
    acct = ledger.get_account(conn, int(account_id))
    if acct is None:
        raise KeyError(f"no account {account_id}")
    debt = _debt_input(conn, acct, as_of)
    if apr is not None:
        debt = DebtInput(**{**debt.__dict__, "apr": _dec(apr),
                            "monthly_rate": monthly_rate(_dec(apr))})
    return simulate([debt], int(monthly_cents),
                    start_period=_period_of(as_of or _today()))


@dataclass(frozen=True)
class ExtraPrincipal:
    """A pay-down line's projection: the debt at its regular payment, and the
    same with a fixed extra principal payment added every month."""
    regular_cents: int           # the regular monthly payment the projection assumes
    extra_cents: int
    base: Simulation             # regular payment alone
    with_extra: Simulation       # regular payment plus the extra

    @property
    def months_sooner(self) -> Optional[int]:
        a, b = self.base.payoff_month, self.with_extra.payoff_month
        return None if a is None or b is None else max(0, a - b)

    @property
    def interest_saved_cents(self) -> Optional[int]:
        if self.base.payoff_month is None or self.with_extra.payoff_month is None:
            return None
        return max(0, self.base.total_interest_cents
                   - self.with_extra.total_interest_cents)


def project_extra(conn: sqlite3.Connection, account_id: int, extra_cents: int, *,
                  as_of: Optional[str] = None,
                  apr: Optional[Decimal] = None) -> ExtraPrincipal:
    """A pay-down line's projection (SRD 5.12h): the debt at its REGULAR payment,
    and at that payment plus ``extra_cents`` of principal every month.

    A pay-down line is a fixed extra principal payment. The regular payment is
    budgeted where it is paid -- a mortgage as one line by payee, principal,
    interest and escrow together -- and the principal inside it changes every
    month, so the only thing a budget line can fix is the extra. Projecting the
    line's amount as the WHOLE payment (the first build) ignored the regular
    payment, and a mortgage "paid" at a hundred dollars a month never clears.

    The regular payment is the debt's required payment this month: an
    installment loan's level principal-and-interest payment from its setup
    (escrow retires no balance and is left out), else the stored or synthesized
    minimum. Both runs are single-member :func:`simulate` calls."""
    acct = ledger.get_account(conn, int(account_id))
    if acct is None:
        raise KeyError(f"no account {account_id}")
    d = _debt_input(conn, acct, as_of)
    if apr is not None:
        d = DebtInput(**{**d.__dict__, "apr": _dec(apr),
                         "monthly_rate": monthly_rate(_dec(apr))})
    interest = _cents(Decimal(d.balance_cents) * d.monthly_rate)
    regular = required_minimum(d, d.balance_cents + interest, interest)
    start = _period_of(as_of or _today())
    extra = max(0, int(extra_cents))
    base = simulate([d], regular, start_period=start)
    more = simulate([d], regular + extra, start_period=start)
    return ExtraPrincipal(regular_cents=regular, extra_cents=extra,
                          base=base, with_extra=more)


def set_apr(conn: sqlite3.Connection, account_id: int, apr) -> DebtTerms:
    """Change only the rate. :func:`set_terms` writes a full row on purpose; a
    caller that knows only the APR -- the Budget page's pay-down line -- must
    keep everything else: the stored terms, or else the ones the account's own
    setup implies (an installment loan's level payment). Writing the APR over
    the defaults turned a car loan's 150.00 payment into a revolving minimum."""
    terms = get_terms(conn, int(account_id)) or _synthesized_terms(conn, int(account_id))
    return set_terms(
        conn, int(account_id), apr=apr, min_form=terms.min_form,
        min_floor_cents=terms.min_floor_cents, min_pct=terms.min_pct,
        credit_limit_cents=terms.credit_limit_cents,
        due_day=terms.due_day, promo_rate=terms.promo_rate,
        promo_end_date=terms.promo_end_date, promo_deferred=terms.promo_deferred,
        scheduled_id=terms.scheduled_id, estimated=False)
