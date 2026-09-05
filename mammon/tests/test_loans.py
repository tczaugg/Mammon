"""Tests for mammon.loans -- the loan domain engine: parameter CRUD (with an
effective-dated rate history + categorized extras), the amortization schedule
that honors the rate history over time, and the per-payment split into
principal / interest / escrow. The split must reconstitute the payment to the
cent, and a mid-loan rate change must shift interest (and therefore principal)
correctly from its effective date forward."""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, ledger, loans

# A synthetic 30-year, $300,000 fixed-rate mortgage at 6% with a $200/mo escrow.
PRINCIPAL = 300_000_00
TERM = 360
ORIGINATION = "2024-01-01"
ESCROW = 200_00
# Level P&I for 300k @ 6% / 360, plus escrow on top = the full scheduled payment.
PI_PAYMENT = loans.standard_payment(PRINCIPAL, "6.0", TERM)     # 1798.65
PAYMENT = PI_PAYMENT + ESCROW


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "loans.db")
    yield c
    c.close()


@pytest.fixture
def loan_acct(conn):
    return ledger.create_account(conn, "Home Mortgage", "liability", opening_balance=-PRINCIPAL)


def _fixed_rate_loan(conn, acct):
    return loans.set_loan_params(
        conn, acct,
        original_principal=PRINCIPAL, term_months=TERM, payment_amount=PAYMENT,
        origination_date=ORIGINATION, interval="monthly",
        rates=[(ORIGINATION, "6.0")],
        extras=[("Escrow", ESCROW, "Taxes + insurance")],
    )


# ---------------------------------------------------------------------------
# Parameter CRUD
# ---------------------------------------------------------------------------
def test_set_get_roundtrip(conn, loan_acct):
    lp = loans.set_loan_params(
        conn, loan_acct,
        original_principal=PRINCIPAL, term_months=TERM, payment_amount=PAYMENT,
        origination_date=ORIGINATION, interval="monthly",
        rates=[(ORIGINATION, "6.0"), ("2027-01-01", "7.25")],
        extras=[("Escrow", ESCROW), ("PMI", 75_00, "Mortgage insurance")],
    )
    assert lp.original_principal == PRINCIPAL
    assert lp.term_months == TERM
    assert lp.interval == "monthly"
    assert lp.periods_per_year == 12
    # rate history is loaded in effective-date order, as Decimals
    assert [(r.effective_date, r.annual_rate) for r in lp.rates] == [
        (ORIGINATION, Decimal("6.0")), ("2027-01-01", Decimal("7.25"))]
    # extras keep their categories, amounts and labels
    assert [(e.category, e.amount, e.label) for e in lp.extras] == [
        ("Escrow", ESCROW, None), ("PMI", 75_00, "Mortgage insurance")]
    assert lp.extras_total == ESCROW + 75_00

    # a second set REPLACES the child rows (no duplicates accumulate)
    lp2 = loans.set_loan_params(
        conn, loan_acct,
        original_principal=PRINCIPAL, term_months=TERM, payment_amount=PAYMENT,
        origination_date=ORIGINATION, rates=[(ORIGINATION, "5.5")], extras=[])
    assert [(r.effective_date, r.annual_rate) for r in lp2.rates] == [(ORIGINATION, Decimal("5.5"))]
    assert lp2.extras == []


def test_get_none_when_unset(conn, loan_acct):
    assert loans.get_loan_params(conn, loan_acct) is None


def test_add_rate_and_extra(conn, loan_acct):
    loans.set_loan_params(
        conn, loan_acct, original_principal=PRINCIPAL, term_months=TERM,
        payment_amount=PAYMENT, origination_date=ORIGINATION, rates=[(ORIGINATION, "6.0")])
    loans.add_rate(conn, loan_acct, "2028-06-01", "6.75")
    loans.add_extra(conn, loan_acct, "HOA", 45_00)
    lp = loans.get_loan_params(conn, loan_acct)
    assert [(r.effective_date, r.annual_rate) for r in lp.rates] == [
        (ORIGINATION, Decimal("6.0")), ("2028-06-01", Decimal("6.75"))]
    assert [(e.category, e.amount) for e in lp.extras] == [("HOA", 45_00)]


# ---------------------------------------------------------------------------
# standard_payment
# ---------------------------------------------------------------------------
def test_standard_payment_annuity():
    # 300k @ 6% for 360 months -> the classic $1,798.65 P&I payment.
    assert loans.standard_payment(300_000_00, "6.0", 360) == 1798_65
    # 0% -> straight-line principal
    assert loans.standard_payment(120_00, "0", 12) == 10_00


# ---------------------------------------------------------------------------
# Amortization schedule
# ---------------------------------------------------------------------------
def test_fixed_rate_schedule_pays_off_to_zero(conn, loan_acct):
    _fixed_rate_loan(conn, loan_acct)
    sched = loans.amortization_schedule(conn, loan_acct)
    # a level annuity payment retires the loan in its term, give or take the one
    # small final adjusting payment a rounded level payment leaves behind
    assert TERM <= len(sched) <= TERM + 1
    first = sched[0]
    # first payment: interest = 300000 * 6%/12 = 1500.00, escrow 200.00
    assert first.interest == 1500_00
    assert first.escrow == ESCROW
    assert first.principal == PAYMENT - 1500_00 - ESCROW
    assert first.date == "2024-02-01"      # first payment is one month after funding
    # balance strictly declines and lands on exactly zero
    assert sched[-1].balance == 0
    assert sum(r.principal for r in sched) == PRINCIPAL
    # every scheduled payment reconstitutes to the cent
    for r in sched:
        assert r.principal + r.interest + r.escrow == r.payment


def test_schedule_honors_rate_history(conn, loan_acct):
    loans.set_loan_params(
        conn, loan_acct, original_principal=PRINCIPAL, term_months=TERM,
        payment_amount=PAYMENT, origination_date=ORIGINATION, interval="monthly",
        rates=[(ORIGINATION, "6.0"), ("2027-01-01", "7.0")],
        extras=[("Escrow", ESCROW)])
    sched = loans.amortization_schedule(conn, loan_acct)
    by_date = {r.date: r for r in sched}
    # the December 2026 payment still charges 6%; January 2027 jumps to 7%
    dec = by_date["2026-12-01"]
    jan = by_date["2027-01-01"]
    assert dec.interest == _month_interest(dec.balance + dec.principal, "6.0")
    assert jan.interest == _month_interest(jan.balance + jan.principal, "7.0")
    # higher rate -> more of the same payment goes to interest, less to principal
    assert jan.interest > _month_interest(jan.balance + jan.principal, "6.0")
    # and honoring the hike has a real consequence: with the payment held fixed,
    # the loan no longer retires within its original term (a real ARM would recast
    # the payment -- that is Task 56, not the base engine).
    assert len(sched) > TERM


def _month_interest(balance_cents, annual_rate):
    from decimal import ROUND_HALF_UP
    return int((Decimal(balance_cents) * Decimal(annual_rate) / Decimal(100) / Decimal(12))
               .quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Per-payment split -- the core deliverable
# ---------------------------------------------------------------------------
def test_split_reconstitutes_to_the_cent(conn, loan_acct):
    _fixed_rate_loan(conn, loan_acct)
    split = loans.payment_split(conn, loan_acct, "2024-02-01", PAYMENT)
    assert split.interest == 1500_00                       # 300000 * 6%/12
    assert split.escrow == ESCROW
    assert split.principal == PAYMENT - 1500_00 - ESCROW
    # principal + interest + escrow == the whole payment, exactly
    assert split.principal + split.interest + split.escrow == PAYMENT
    assert split.balance_before == PRINCIPAL
    assert split.balance_after == PRINCIPAL - split.principal
    assert split.annual_rate == Decimal("6.0")
    # the categorized extras come back for the register / import split to post
    assert [(e.category, e.amount) for e in split.extras] == [("Escrow", ESCROW)]


def test_split_of_larger_payment_puts_extra_to_principal(conn, loan_acct):
    _fixed_rate_loan(conn, loan_acct)
    extra = 500_00
    split = loans.payment_split(conn, loan_acct, "2024-02-01", PAYMENT + extra)
    # interest + escrow are fixed for the period, so the whole overage amortizes
    assert split.interest == 1500_00
    assert split.escrow == ESCROW
    assert split.principal == (PAYMENT - 1500_00 - ESCROW) + extra
    assert split.principal + split.interest + split.escrow == PAYMENT + extra


def test_mid_loan_rate_change_shifts_the_split(conn, loan_acct):
    loans.set_loan_params(
        conn, loan_acct, original_principal=PRINCIPAL, term_months=TERM,
        payment_amount=PAYMENT, origination_date=ORIGINATION, interval="monthly",
        rates=[(ORIGINATION, "6.0"), ("2027-01-01", "7.0")],
        extras=[("Escrow", ESCROW)])

    # A payment BEFORE the reset (Dec 2026) still uses 6%.
    before = loans.payment_split(conn, loan_acct, "2026-12-01", PAYMENT)
    assert before.annual_rate == Decimal("6.0")
    assert before.interest == _month_interest(before.balance_before, "6.0")

    # A payment AT/AFTER the reset (Feb 2027) uses 7% on that date's balance.
    after = loans.payment_split(conn, loan_acct, "2027-02-01", PAYMENT)
    assert after.annual_rate == Decimal("7.0")
    assert after.interest == _month_interest(after.balance_before, "7.0")

    # The rate change genuinely shifts the split: on the SAME outstanding balance
    # 7% would take more interest than 6%, so more of the fixed payment is
    # interest and less is principal after the reset.
    would_be_6 = _month_interest(after.balance_before, "6.0")
    assert after.interest > would_be_6
    assert after.principal < PAYMENT - would_be_6 - ESCROW
    # still reconstitutes exactly
    assert after.principal + after.interest + after.escrow == PAYMENT


def test_split_requires_loan_params(conn, loan_acct):
    with pytest.raises(LookupError):
        loans.payment_split(conn, loan_acct, "2024-02-01", PAYMENT)
    with pytest.raises(LookupError):
        loans.amortization_schedule(conn, loan_acct)


# ---------------------------------------------------------------------------
# Balance-driven amortization -- interest recomputed from the RUNNING balance
# ---------------------------------------------------------------------------
def test_tom_repro_schedule_first_twelve_rows_match_to_the_cent(conn):
    """the user's authoritative repro: $113,394.32, 357 months, 5.875% APR, monthly,
    no escrow, no extra payments. With no extra principal the balance-driven
    recomputation must equal the standard level-payment schedule, reproducing
    his printed table to the cent (month 1: interest $555.16, principal $117.68,
    balance $113,276.64)."""
    acct = ledger.create_account(
        conn, "the user Mortgage", "liability", opening_balance=-113_394_32)
    pi = loans.standard_payment(113_394_32, "5.875", 357)
    assert pi == 672_84                                    # level P&I payment
    loans.set_loan_params(
        conn, acct,
        original_principal=113_394_32, term_months=357, payment_amount=pi,
        origination_date="2024-01-01", interval="monthly",
        rates=[("2024-01-01", "5.875")], extras=[])
    sched = loans.amortization_schedule(conn, acct)
    # (interest, principal, balance-after) for the first twelve payments
    expected = [
        (555_16, 117_68, 113_276_64),
        (554_58, 118_26, 113_158_38),
        (554_00, 118_84, 113_039_54),
        (553_42, 119_42, 112_920_12),
        (552_84, 120_00, 112_800_12),
        (552_25, 120_59, 112_679_53),
        (551_66, 121_18, 112_558_35),
        (551_07, 121_77, 112_436_58),
        (550_47, 122_37, 112_314_21),
        (549_87, 122_97, 112_191_24),
        (549_27, 123_57, 112_067_67),
        (548_66, 124_18, 111_943_49),
    ]
    assert len(sched) >= 12
    for i, (interest, principal, balance) in enumerate(expected):
        row = sched[i]
        assert (row.interest, row.principal, row.balance) == (interest, principal, balance)
        assert row.escrow == 0
        assert row.payment == principal + interest       # no escrow, reconstitutes
    # spelled out for the record: month 1 exactly matches the user's table
    assert (sched[0].interest, sched[0].principal, sched[0].balance) == \
        (555_16, 117_68, 113_276_64)


def test_extra_principal_shortens_the_term(conn, loan_acct):
    """An extra principal payment posted to the register must drop the running
    balance permanently: interest is ALWAYS recomputed from the actual balance,
    so the extra never gets forced back onto the original schedule. The month it
    is paid keeps its interest (charged on the balance ENTERING that period), but
    the balance falls by the whole extra, every later period's interest is strictly
    lower, and the loan retires in FEWER payments than its term."""
    _fixed_rate_loan(conn, loan_acct)
    baseline = loans.amortization_schedule(conn, loan_acct)   # nothing posted yet
    base = {r.date: r for r in baseline}

    EXTRA = 5_000_00
    dates = [r.date for r in baseline[:4]]
    # post the first four scheduled payments; the fourth carries the extra
    # principal (a larger-than-scheduled payment) -- positive = a debt paydown.
    for i, d in enumerate(dates):
        ledger.add_transaction(conn, loan_acct, d, PAYMENT + (EXTRA if i == 3 else 0))

    sched = loans.amortization_schedule(conn, loan_acct)
    got = {r.date: r for r in sched}
    d4 = dates[3]

    # (a) the extra-payment month's interest is unchanged (it is charged on the
    #     balance entering the period, which the extra has not touched yet) -- but
    #     the balance after the payment drops by the FULL extra principal.
    assert got[d4].interest == base[d4].interest
    assert got[d4].balance == base[d4].balance - EXTRA

    # (b) every later period's interest is strictly lower than the no-extra schedule
    for r in baseline[4:24]:
        assert got[r.date].interest < base[r.date].interest

    # (c) the loan reaches exactly $0 in fewer payments than the term (paid early)
    assert sched[-1].balance == 0
    assert len(sched) < len(baseline)
    assert len(sched) < TERM

    # and a payment SPLIT decomposed after the extra charges interest on the
    # lower running balance -- the extra principal flows into later interest.
    later_date = baseline[8].date                          # the 9th payment
    split = loans.payment_split(conn, loan_acct, later_date, PAYMENT)
    # interest is charged on the ACTUAL running balance (after the 8th payment),
    # which sits below the no-extra schedule ever since the extra principal.
    assert split.balance_before == got[baseline[7].date].balance
    assert split.interest < base[later_date].interest


def test_principal_only_transfer_shortens_term(conn, loan_acct):
    """An ADDITIONAL principal payment entered as a transfer (money moved in from
    checking, no interest/escrow) retires principal directly: it is NOT decomposed
    as a whole scheduled payment (no interest/escrow skimmed off it), it lowers the
    balance every LATER period's interest is charged on, and the loan pays off in
    fewer payments than its term. Dated off a scheduled payment date, too."""
    _fixed_rate_loan(conn, loan_acct)
    baseline = loans.amortization_schedule(conn, loan_acct)      # nothing posted
    base = {r.date: r for r in baseline}

    checking = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=50_000_00)
    EXTRA = 20_000_00
    # a $20,000 extra principal payment moved from checking to the loan, mid-month
    # (off any scheduled payment date) -- all principal, no interest/escrow.
    ledger.create_transfer(conn, checking, loan_acct, "2024-02-15", EXTRA)

    sched = loans.amortization_schedule(conn, loan_acct)
    got = {r.date: r for r in sched}

    # the period the extra falls in keeps its own interest (charged on the balance
    # entering the period, before the extra lands)...
    assert got["2024-02-01"].interest == base["2024-02-01"].interest
    # ...but the whole $20,000 is credited to principal, so every later period's
    # interest is strictly lower than the no-extra schedule.
    for r in baseline[1:24]:                 # 2024-03-01 onward
        assert got[r.date].interest < base[r.date].interest
    # the loan reaches exactly $0 in fewer payments than the term (paid early)
    assert sched[-1].balance == 0
    assert len(sched) < len(baseline)
    assert len(sched) < TERM

    # a payment split on a later date charges interest on the LOWER running balance
    later = baseline[8].date
    split = loans.payment_split(conn, loan_acct, later, PAYMENT)
    assert split.interest < base[later].interest


# ---------------------------------------------------------------------------
# Day-count accrual -- a mid-period principal paydown splits interest at the
# paydown by ACTUAL day count (interest for the next payment = old_balance over
# the days before it + reduced_balance over the days after), so a paydown reduces
# interest the very NEXT payment, not one payment too late. The periodic quantum
# is still balance x annual/periods (US Bank's monthly convention -- confirmed to
# the cent against the user's Recast statements, NOT actual/365).
# ---------------------------------------------------------------------------
def _daycount_loan(conn, acct):
    # $100,000 @ 6% monthly (periodic rate 0.5%), NO escrow, so interest is a clean
    # balance x 0.005 and the day-count math is easy to read.
    return loans.set_loan_params(
        conn, acct, original_principal=100_000_00, term_months=360,
        payment_amount=600_00, origination_date="2024-01-01", interval="monthly",
        rates=[("2024-01-01", "6.0")], extras=[])


def test_same_day_paydown_is_full_month_on_reduced_balance(conn, loan_acct):
    """The Recast case: a principal-only paydown on the SAME day as a
    scheduled payment leaves 0 days at the old balance, so the NEXT payment accrues
    a FULL month's interest on the post-paydown balance."""
    _daycount_loan(conn, loan_acct)
    checking = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=200_000_00)
    ledger.add_transaction(conn, loan_acct, "2024-02-01", 600_00)     # Feb payment
    ledger.create_transfer(conn, checking, loan_acct, "2024-02-01", 30_000_00)

    # Feb interest is on the FULL balance -- the paydown lands after the payment.
    feb = loans.payment_split(conn, loan_acct, "2024-02-01", 600_00)
    assert feb.balance_before == 100_000_00
    assert feb.interest == 500_00                       # 100000 * 0.5%
    # balance after Feb = 100000 - (600-500) = 99900 ; paydown -> 69900.
    mar = loans.payment_split(conn, loan_acct, "2024-03-01", 600_00)
    assert mar.balance_before == 69_900_00              # the reduced balance
    assert mar.interest == 349_50                       # FULL month on 69900
    assert mar.principal == 600_00 - 349_50


def test_midperiod_paydown_credits_the_next_payment_in_full(conn, loan_acct):
    """A paydown STRICTLY inside a period is credited IN FULL to the very next
    payment (not one payment too late, and not prorated by the days it was in
    effect): the servicer charges one periodic quantum per payment, so that
    payment accrues a whole month on the already-reduced balance."""
    _daycount_loan(conn, loan_acct)
    checking = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=200_000_00)
    ledger.add_transaction(conn, loan_acct, "2024-02-01", 600_00)
    ledger.create_transfer(conn, checking, loan_acct, "2024-02-15", 30_000_00)

    feb = loans.payment_split(conn, loan_acct, "2024-02-01", 600_00)
    assert feb.interest == 500_00                       # unaffected
    # The 02-15 paydown drops the balance to 69900_00; the 03-01 payment accrues
    # the FULL periodic quantum on it: 0.005 * 69900_00 == 349_50.
    mar = loans.payment_split(conn, loan_acct, "2024-03-01", 600_00)
    assert mar.balance_before == 69_900_00
    assert mar.interest == 349_50


def test_paydown_interest_does_not_depend_on_its_day_in_the_period(conn, loan_acct):
    """The invariant behind the fix: WHERE in the period a paydown lands cannot
    change the next payment's interest. Day count never enters the accrual, so a
    paydown on the 2nd and one on the 28th of the same period both hand the next
    payment a full quantum on the reduced balance."""
    results = []
    for acct, day in ((loan_acct, "02"), (None, "28")):
        if acct is None:
            acct = ledger.create_account(conn, f"Loan{day}", "liability")
        _daycount_loan(conn, acct)
        checking = ledger.create_account(conn, f"Checking{day}", "checking",
                                         opening_balance=200_000_00)
        ledger.add_transaction(conn, acct, "2024-02-01", 600_00)
        ledger.create_transfer(conn, checking, acct, f"2024-02-{day}", 30_000_00)
        results.append(loans.payment_split(conn, acct, "2024-03-01", 600_00).interest)
    assert results[0] == results[1] == 349_50


def test_multiple_midperiod_paydowns_all_land_before_the_next_accrual(conn, loan_acct):
    """Generalizes to any number of mid-period balance changes: every paydown is
    retired before the next payment accrues, which then charges one quantum on
    what is left."""
    _daycount_loan(conn, loan_acct)
    checking = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=200_000_00)
    ledger.add_transaction(conn, loan_acct, "2024-02-01", 600_00)
    ledger.create_transfer(conn, checking, loan_acct, "2024-02-10", 10_000_00)
    ledger.create_transfer(conn, checking, loan_acct, "2024-02-20", 20_000_00)

    mar = loans.payment_split(conn, loan_acct, "2024-03-01", 600_00)
    assert mar.balance_before == 69_900_00              # both paydowns retired
    assert mar.interest == 349_50                       # 0.005 * 69900_00


# ---------------------------------------------------------------------------
# Effective-dated escrow / PMI / extras -- a change applies FORWARD only
# ---------------------------------------------------------------------------
def test_dated_escrow_change_applies_forward_only(conn, loan_acct):
    """An escrow/PMI/extra change is first-class DATED data (like the rate
    history): a payment BEFORE its effective date splits with the old amount, one
    ON/AFTER with the new -- deterministically from the stored history."""
    _fixed_rate_loan(conn, loan_acct)               # $200 escrow from the start
    loans.add_extra_change(conn, loan_acct, "2025-01-01", "Escrow", 300_00)

    before = loans.payment_split(conn, loan_acct, "2024-12-01", PAYMENT)
    assert before.escrow == ESCROW
    assert [(e.category, e.amount) for e in before.extras] == [("Escrow", ESCROW)]

    after = loans.payment_split(conn, loan_acct, "2025-01-01", PAYMENT + 100_00)
    assert after.escrow == 300_00
    assert [(e.category, e.amount) for e in after.extras] == [("Escrow", 300_00)]
    # still reconstitutes the whole payment to the cent
    assert after.principal + after.interest + after.escrow == PAYMENT + 100_00

    # the stored history keeps BOTH amounts; extras_total is the CURRENT one
    lp = loans.get_loan_params(conn, loan_acct)
    assert sorted(e.amount for e in lp.extras if e.category == "Escrow") == \
        [ESCROW, 300_00]
    assert lp.extras_total == 300_00


def test_added_pmi_is_not_retroactive(conn, loan_acct):
    """A NEW extra (PMI) added mid-loan does NOT apply to earlier payments -- it
    contributes nothing before its own effective date (unlike a rate, an extra
    has no earlier-row fallback)."""
    _fixed_rate_loan(conn, loan_acct)
    loans.add_extra_change(conn, loan_acct, "2025-06-01", "PMI", 60_00)
    early = loans.payment_split(conn, loan_acct, "2024-06-01", PAYMENT)
    assert [(e.category, e.amount) for e in early.extras] == [("Escrow", ESCROW)]
    late = loans.payment_split(conn, loan_acct, "2025-06-01", PAYMENT + 60_00)
    assert sorted((e.category, e.amount) for e in late.extras) == \
        [("Escrow", ESCROW), ("PMI", 60_00)]
    assert late.escrow == ESCROW + 60_00


def test_dated_escrow_change_shifts_schedule_and_stays_consistent(conn, loan_acct):
    """The amortization schedule picks up a dated escrow change from its effective
    date; every row still reconstitutes and the running balance stays consistent
    (strictly declining period over period)."""
    _fixed_rate_loan(conn, loan_acct)
    loans.add_extra_change(conn, loan_acct, "2025-01-01", "Escrow", 300_00)
    sched = loans.amortization_schedule(conn, loan_acct)
    by_date = {r.date: r for r in sched}
    assert by_date["2024-12-01"].escrow == ESCROW
    assert by_date["2025-01-01"].escrow == 300_00
    for r in sched:
        assert r.principal + r.interest + r.escrow == r.payment
    balances = [PRINCIPAL] + [r.balance for r in sched]
    assert all(b2 < b1 for b1, b2 in zip(balances, balances[1:]))


def test_add_extra_change_is_idempotent_on_same_date(conn, loan_acct):
    """Recording the escrow amount twice for the SAME effective date overwrites
    that dated row rather than stacking a duplicate."""
    _fixed_rate_loan(conn, loan_acct)
    loans.add_extra_change(conn, loan_acct, "2025-01-01", "Escrow", 300_00)
    loans.add_extra_change(conn, loan_acct, "2025-01-01", "Escrow", 325_00)
    lp = loans.get_loan_params(conn, loan_acct)
    escrow_rows = [e for e in lp.extras
                   if e.category == "Escrow" and e.effective_date == "2025-01-01"]
    assert len(escrow_rows) == 1 and escrow_rows[0].amount == 325_00


# ---------------------------------------------------------------------------
# the user's authoritative loan model: the TOTAL PAYMENT is the ONLY preserved/input
# value. Interest = prior balance * periodic rate; principal = total - interest -
# extras; balance -= principal, cascading forward. The total comes from the dated
# schedule (or the actual entry when it differs) and is NEVER replaced by a
# computed number.
# ---------------------------------------------------------------------------
def _toms_loan(conn):
    """the user's example loan: a $200,000 / 30-yr / 5% monthly mortgage with a $200
    escrow, whose scheduled TOTAL is later changed to $1268.99 effective
    2026-04-01. Returns (account_id, base_total_cents)."""
    aid = ledger.create_account(conn, "the user Home", "liability",
                                opening_balance=-200_000_00)
    pi = loans.standard_payment(200_000_00, "5.0", 360)          # level P&I
    total = pi + 200_00
    loans.set_loan_params(
        conn, aid, original_principal=200_000_00, term_months=360,
        payment_amount=total, origination_date="2025-01-01", interval="monthly",
        rates=[("2025-01-01", "5.0")], extras=[("Escrow", 200_00, "Taxes + ins")])
    return aid, total


def test_steady_scheduled_payments_equal_the_dated_total_and_reconstitute(conn):
    """(a) Every steady period pays exactly the configured scheduled TOTAL, and
    interest + principal + escrow always sums back to it -- because interest is the
    prior balance * the periodic rate and principal is whatever is LEFT of the
    total after interest + escrow (computed, never stored)."""
    aid, total = _toms_loan(conn)
    sched = loans.amortization_schedule(conn, aid)

    bal = 200_000_00
    for row in sched[:-1]:                       # all but the trimmed final payoff
        assert row.payment == total              # the total is never substituted
        assert row.escrow == 200_00
        # interest is the PRIOR balance * the periodic rate...
        interest = _month_interest(bal, "5.0")
        assert row.interest == interest
        # ...principal is the remainder of the total after interest + escrow...
        assert row.principal == total - interest - 200_00
        # ...and the running balance cascades forward off that principal
        bal -= row.principal
        assert row.balance == bal
        # the pieces reconstitute the whole total, to the cent
        assert row.principal + row.interest + row.escrow == row.payment


def test_dated_payment_change_applies_from_effective_date_forward(conn):
    """(b) A dated payment change ($1268.99 effective 2026-04-01) takes effect on
    its effective date: periods before it keep the old total, periods on/after
    adopt $1268.99 -- and every row still reconstitutes to the total in force."""
    aid, base_total = _toms_loan(conn)
    loans.add_payment_change(conn, aid, "2026-04-01", 1268_99)

    sched = {r.date: r for r in loans.amortization_schedule(conn, aid)}
    # before the effective date the old total still stands
    assert sched["2026-03-01"].payment == base_total
    # on/after the effective date, $1268.99 is used forward
    for d in ("2026-04-01", "2026-05-01", "2026-06-01", "2027-01-01"):
        assert sched[d].payment == 1268_99
        assert sched[d].escrow == 200_00
        assert sched[d].principal + sched[d].interest + sched[d].escrow == 1268_99
    # the flat CURRENT total tracks the latest dated change
    assert loans.get_loan_params(conn, aid).payment_amount == 1268_99


def test_extra_actual_payment_lowers_next_interest_and_raises_principal(conn):
    """(c) An over-payment cascades forward: a +$1000 ACTUAL payment flows entirely
    to principal, so the NEXT period's interest is strictly LOWER (charged on the
    reduced balance) and its principal strictly HIGHER -- and the loan retires with
    strictly less lifetime interest. The scheduled total is never overwritten."""
    aid, total = _toms_loan(conn)
    baseline = loans.amortization_schedule(conn, aid)
    base = {r.date: r for r in baseline}

    # post the first payment on schedule, then the second $1000 larger (a debt
    # paydown posts as a POSITIVE amount, no interest/escrow skimmed on the extra)
    ledger.add_transaction(conn, aid, "2025-02-01", total)
    ledger.add_transaction(conn, aid, "2025-03-01", total + 1000_00)

    sched_list = loans.amortization_schedule(conn, aid)
    sched = {r.date: r for r in sched_list}

    # the extra-payment month keeps its own interest (charged on the balance
    # ENTERING the period, before the extra lands); the $1000 all goes to principal
    assert sched["2025-03-01"].interest == base["2025-03-01"].interest
    assert sched["2025-03-01"].principal == base["2025-03-01"].principal + 1000_00
    # the NEXT period: interest strictly LOWER, principal strictly HIGHER
    assert sched["2025-04-01"].interest < base["2025-04-01"].interest
    assert sched["2025-04-01"].principal > base["2025-04-01"].principal
    # the total was NEVER replaced -- April still pays the scheduled total
    assert sched["2025-04-01"].payment == total
    # accelerated paydown: lower balance from the extra onward, strictly less
    # lifetime interest, and the loan pays off no later than the no-extra schedule
    assert sched["2025-04-01"].balance < base["2025-04-01"].balance
    assert sum(r.interest for r in sched_list) < sum(r.interest for r in baseline)
    assert sched_list[-1].balance == 0
    assert len(sched_list) <= len(baseline)


def test_schedule_never_replaces_total_with_a_computed_value(conn):
    """(d) No code path replaces the TOTAL PAYMENT with a computed number. Across
    a dated change AND an over/under actual payment, every steady schedule row's
    ``payment`` equals a REAL input total -- the dated scheduled total in force, or
    the actual posted amount -- never a recomputed principal+interest+escrow (only
    the trimmed final payoff row may differ, and only downward)."""
    aid, base_total = _toms_loan(conn)
    loans.add_payment_change(conn, aid, "2026-04-01", 1268_99)
    # an under-payment one month and an over-payment another -- both must be honored
    # as the real total for their period, not normalized to a computed schedule value
    ledger.add_transaction(conn, aid, "2025-05-01", base_total - 500_00)
    ledger.add_transaction(conn, aid, "2025-06-01", base_total + 2000_00)

    sched = loans.amortization_schedule(conn, aid)
    posted = {"2025-05-01": base_total - 500_00, "2025-06-01": base_total + 2000_00}
    for row in sched[:-1]:                        # every steady (non-payoff) row
        if row.date in posted:
            expected = posted[row.date]           # the ACTUAL amount rules
        elif row.date >= "2026-04-01":
            expected = 1268_99                    # the dated change rules
        else:
            expected = base_total                 # the scheduled total rules
        assert row.payment == expected            # a real total, never computed
        # interest/principal/escrow are the ONLY computed quantities, and they
        # reconstitute the (input) total exactly
        assert row.principal + row.interest + row.escrow == row.payment
