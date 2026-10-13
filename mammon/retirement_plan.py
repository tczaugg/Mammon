"""mammon.retirement_plan -- the retirement plan's projection, without a screen.

The Retirement Planner page (``ui/retirement_planner.py``, SRD 5.8o) draws what
the stored plan pays out year by year and what happens to the fund it comes out
of. Everything that COMPUTES that lives here; the page only shows it. Two
readers need the same answer: the page, and the read-only MCP surface
(``mammon/mcp_tools.py``, SRD 7.3), which lets a connected LLM ask about the
plan. Two copies of a projection this size would drift - the page's editors
already had to be made to share one RMD rule for that reason - so there is one,
and the page's editors INHERIT it:

* :class:`PlanEngine` is what both inline editors compute: accounts, balances,
  the required-minimum floor, the stored plan's flows and each account's
  projected value. ``ui.retirement_planner._PlanEditor`` is a QWidget over it.
* :class:`HouseholdEngine` adds the household plan - one spending amount split
  across the whole pool, with its tax, IRMAA and the year the money runs out
  (:func:`mammon.retirement.plan_household_withdrawals` does the split itself).
  ``WithdrawalSchedule`` is the editor over it.
* :class:`HouseholdPlanner` builds one with no widget, configured the way the
  page configures its schedule: the stored terminal age, the default filing
  status, and the household plan's own balances as the projection every cap
  and minimum reads.

Nothing here writes. Applying a plan, netting Roth draws against conversions
and seeding the required minimums are WRITES and stay with the page, which owns
the moment the user asks for them; a reader here sees the plan as last written
(the page seeds minimums and taxable income each time it opens). The MCP
connection is ``query_only``, so a write slipping in here fails loudly there.

Module functions are the per-year arithmetic the page and the engines share:
who is on file and how old, Social Security by year, other income, the taxable
pieces, contributions, the growth each account's mix implies, and
:func:`income_rows`, every bar on the page. No Qt is imported, directly or
through another module: ``test_retirement_plan`` pins that.
"""

from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Optional, Sequence

from mammon import forecast, ledger, portfolio, retirement
from mammon.retirement import PlanAccount

#: The age the plan is drawn through is the user's to pick, any whole year in
#: this range, so a plan through 87 or 95 can be asked for. Still a CASE the user chooses,
#: never one life-expectancy figure: see the module docstring.
MIN_TERMINAL_AGE = 60
MAX_TERMINAL_AGE = 120

#: The plan is WRITTEN through at least this age (or the chosen one, if later),
#: whatever is on screen: a required minimum is owed in a year whether or not it
#: is drawn, and moving the age must not leave years unplanned.
PLAN_WRITTEN_THROUGH_AGE = 100

#: The case the page opens on. Chosen because it is the longest case that most
#: plans still reach, not because it is anybody's expectancy.
DEFAULT_TERMINAL_AGE = 90

#: How far the page projects when nobody on file has a birth year, so no
#: terminal age can be turned into a year.
FALLBACK_HORIZON_YEARS = 12

#: A hard cap on the number of bars, so a typo in a birth year cannot ask
#: matplotlib for three hundred of them.
MAX_YEARS = 60


# ---------------------------------------------------------------------------
# the plan, as this page reads it
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IncomeYear:
    """One bar: everything the plan says about a single calendar year.

    Every amount is a MAGNITUDE in cents, matching the plan tables. The page
    draws withdrawals as positive height because the question the chart answers
    is "how much income", not "which direction did the money move".
    """

    year: int
    age: Optional[int] = None
    social_security_cents: int = 0
    deferred_draw_cents: int = 0
    roth_draw_cents: int = 0
    conversion_cents: int = 0
    fund_value_cents: int = 0
    other_income_cents: int = 0          # rentals, royalties (retirement.IncomeSource)
    taxable_draw_cents: int = 0          # sales from taxable brokerage accounts

    @property
    def income_cents(self) -> int:
        """Spendable income: the stacked series. A conversion is NOT in here -
        it is taxable but buys the household nothing to spend."""
        return (self.social_security_cents + self.other_income_cents
                + self.deferred_draw_cents + self.taxable_draw_cents
                + self.roth_draw_cents)

    @property
    def taxable_cents(self) -> int:
        """What the year adds to ordinary income: a deferred distribution plus
        anything converted out of a deferred account. A Roth distribution is
        not income (IRC 408A(d)(1)) and Social Security's taxable share depends
        on the rest of the return, so it is deliberately not folded in here."""
        return self.deferred_draw_cents + self.conversion_cents


def plan_accounts(conn) -> list[PlanAccount]:
    """Every retirement account, tax-deferred first, as the plan sees it.

    A thin pass-through to :func:`retirement.plan_accounts`, kept as a name on
    this module because the page and its tests read it: which accounts are
    "retirement" is a domain question, and two screens answering it separately
    would put a draw in the plan that never appears on the chart.
    """
    return retirement.plan_accounts(conn)


def account_value_cents(conn, account_id: int) -> int:
    """Today's value of one account, in cents.

    An investment account is valued through ``mammon.portfolio`` (never
    ``investments.account_valuation``, whose scope drags in crypto), anything
    else falls back to its ledger balance. The two broad ``except`` clauses are
    deliberate: a valuation that cannot be computed must degrade to a balance
    and then to zero, because a planning page that raises shows the user nothing
    at all.
    """
    key = int(account_id)
    if retirement.is_planned(key):
        return 0                    # a planned Roth holds nothing until converted into
    try:
        from mammon import portfolio

        if key in portfolio.scope_account_ids(conn, include_hidden=True):
            return int(portfolio.account_valuation(conn, key).total)
        return int(ledger.account_balance(conn, key))
    except Exception:
        try:
            return int(ledger.account_balance(conn, key))
        except Exception:
            return 0


#: The start a growth rate is measured from (see ``growth_factors``). Any
#: positive figure gives the same factor; large keeps the cent rounding out of it.
GROWTH_PROBE_CENTS = 100_000_000_00


def horizon_people(conn) -> list[dict]:
    """Whose age ends the plan: the SELF person, when one has a birth year.

    "Plan through age 90" is read as the user's own 90, the same person the
    chart's age axis counts. Reported twice: a child's 90th birthday ran the plan
    to the 60-year cap (age 120), and the last-surviving adult's ran it past the
    year the user asked for. Falls back to the spouse, then to everyone with a
    birth year, only when no self is on file.
    """
    people = planning_people(conn)
    scenario = retirement.get_survivor_scenario(conn)
    if scenario is not None:
        # Under the survivor scenario the plan runs to the SURVIVOR's age.
        survivor = [p for p in people if int(p["id"]) == scenario.survivor_id]
        if survivor:
            return survivor
    for relationship in ("self", "spouse"):
        chosen = [p for p in people if p.get("relationship") == relationship]
        if chosen:
            return chosen
    return people


def year_status(conn, filing_status: str, year: int) -> str:
    """``filing_status`` as it stands in ``year`` - single for a survivor."""
    return retirement.survivor_status(filing_status, int(year),
                                      retirement.get_survivor_scenario(conn))


def living_couple(conn, year: int, filing_status: str,
                  people: Optional[Sequence[Mapping]] = None) -> list[Mapping]:
    """The self and spouse alive in ``year`` whom a return of ``filing_status``
    covers: both on a joint return, otherwise one - the self, or under the
    survivor scenario the survivor."""
    people = planning_people(conn) if people is None else people
    scenario = retirement.get_survivor_scenario(conn)
    alive = [p for p in people if p.get("relationship") in ("self", "spouse")
             and not (scenario is not None and scenario.widowed(year)
                      and int(p["id"]) == scenario.deceased_id)]
    alive.sort(key=lambda p: p.get("relationship") != "self")
    if filing_status in ("joint", "separate"):
        return alive
    return alive[:1]


def planning_people(conn) -> list[dict]:
    """The household members a projection can be built for: everyone with a
    birth year, self first (``retirement.list_people`` orders it)."""
    return [p for p in retirement.list_people(conn) if p.get("birth_year")]


def household_irmaa(conn, year: int, magi_cents: int, status: str,
                    people: Optional[Sequence[Mapping]] = None,
                    enrolled_status: Optional[str] = None) -> tuple[int, int, int]:
    """(surcharge for the household, tier, enrolled person-months) in premium
    ``year``, from ``magi_cents`` - the income two years earlier.

    Charged per person on Medicare, so the same income costs twice as much
    once both spouses are enrolled. Only the self person, and the spouse on a
    joint or separate return."""
    # ``status`` is the LOOKBACK return's - SSA reads the tiers off that
    # return - while who is enrolled is the premium year's: a survivor's first
    # premiums are set by the last joint return, on the joint tiers. The Part
    # D step only for someone with drug coverage (the person's "Part D").
    enrolled = [(p, medicare_months_in(conn, p, year))
                for p in living_couple(conn, year, enrolled_status or status, people)]
    months = sum(m for _p, m in enrolled)
    if not months:
        return 0, 0, 0
    total = tier = 0
    for person, paid in enrolled:
        if not paid:
            continue
        tier, per_person = retirement.irmaa_surcharge_cents(
            magi_cents, status, year,
            index_pct=retirement.get_bracket_index_pct(conn),
            growth_pct=retirement.get_medicare_growth_pct(conn),
            part_d=bool(person.get("part_d", 1)))
        total += (per_person * paid + 6) // 12
    return total, tier, months


def medicare_months_in(conn, person: Mapping, year: int, sources=None) -> int:
    """:func:`retirement.medicare_months`, enrolling later than 65 when the
    person's salary runs past it: on the employer's coverage until the job
    ends, then Medicare from the January after (the 8-month special
    enrollment period). A salary with no end runs to the household's
    retirement year."""
    birth_year = person.get("birth_year")
    if birth_year is None:
        return 0
    found = retirement.plan_income_sources(conn) if sources is None else sources
    default = retirement_start_year(conn)
    start = None
    for src in found:
        if src.kind != "salary":
            continue
        mine = (src.person_id == int(person["id"])
                or (src.person_id is None and person.get("relationship") == "self"))
        if not mine:
            continue
        ends = src.end_year if src.end_year is not None else (
            None if default is None else int(default) - 1)
        if ends is not None and int(ends) >= int(birth_year) + retirement.MEDICARE_AGE \
                and src.cents_in(int(ends)) > 0:
            start = max(start or (0, 0), (int(ends) + 1, 1))
    return retirement.medicare_months(person, year, start_override=start)


def dependents_in(conn, year: int, people: Optional[Sequence[Mapping]] = None) -> int:
    """Children of record under 19 in ``year``: in the tax household for the
    poverty line (IRC 36B(d)(1)), on nobody's return otherwise."""
    people = planning_people(conn) if people is None else people
    return sum(1 for p in people if p.get("relationship") == "child"
               and p.get("birth_year") and 0 <= int(year) - int(p["birth_year"]) < 19)


def aca_marketplace_months(conn, year: int, status: str, people=None) -> int:
    """The months of ``year`` the household buys marketplace coverage: someone
    in the return is under 65 (not on Medicare) and the household has retired
    (no employer coverage). 0 when no credit is at stake."""
    start = retirement_start_year(conn)
    if not retirement.get_aca_benchmark_cents(conn) or start is None or int(year) < start:
        return 0
    return max((12 - medicare_months_in(conn, p, year)
                for p in living_couple(conn, year, status, people)), default=0)


def aca_poverty_line_in(conn, year: int, status: str, people=None) -> int:
    """The poverty guideline for the tax household in coverage year ``year``:
    the PRIOR year's guidelines, the ones in force at its open enrollment
    (45 CFR 155.300), indexed at the bracket rate."""
    size = max(1, len(living_couple(conn, year, status, people))
               + dependents_in(conn, year, people))
    return retirement.indexed_cents(retirement.federal_poverty_level(size),
                                    retirement.get_bracket_index_pct(conn),
                                    int(year) - 1)


def aca_cliff_in(conn, year: int, status: str, people=None) -> int:
    """400% of the poverty line for the tax household: where the credit ends."""
    return aca_poverty_line_in(conn, year, status, people) * retirement.ACA_CLIFF_MULTIPLE


def aca_benchmark_in(conn, year: int) -> int:
    """The benchmark premium in ``year``: today's, grown with the Medicare
    premium rate (health costs)."""
    grown = Decimal(retirement.get_aca_benchmark_cents(conn)) * (
        1 + retirement.get_medicare_growth_pct(conn) / 100) ** max(
            0, int(year) - _dt.date.today().year)
    return int(grown.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def aca_credit_in(conn, year: int, aca_magi_cents: int, status: str, people=None) -> int:
    """The premium tax credit for the whole of ``year`` at this ACA MAGI (AGI
    plus the untaxed Social Security): the benchmark less the applicable
    share of income, nothing over the cliff (IRC 36B)."""
    return retirement.aca_premium_credit_cents(
        aca_benchmark_in(conn, year), int(aca_magi_cents),
        aca_poverty_line_in(conn, year, status, people))


def aca_cost(conn, year: int, aca_magi_cents: int, status: str, people=None, *,
             baseline_magi_cents: int = 0) -> int:
    """The premium credit the plan's own draws and conversions cost in
    ``year``, for the months on the marketplace: the credit at
    ``baseline_magi_cents`` - the income the household has WITHOUT them
    (Social Security and other income), or the poverty line if that is
    lower, since under it there is no credit to lose - less the credit at
    ``aca_magi_cents``. A slope up to the cliff, where the rest goes at once.
    Reported: modeled as the cliff alone, every dollar under it looked free."""
    months = aca_marketplace_months(conn, year, status, people)
    if not months:
        return 0
    floor = aca_poverty_line_in(conn, year, status, people)
    lost = (aca_credit_in(conn, year, max(int(baseline_magi_cents), floor), status, people)
            - aca_credit_in(conn, year, aca_magi_cents, status, people))
    return max(0, lost) * months // 12


def appealed_premium_years(conn) -> dict[int, int]:
    """Premium year -> the year of the life-changing event that lets it be
    appealed (Form SSA-44), when the appeal is assumed.

    The events the plan knows (20 CFR 418.1205): each earner stopping work
    (the year after their salary ends), and under the survivor scenario the
    spouse's death. A retirement date with no salary ending is not one - SSA
    asks what stopped - so the household's start year counts only through
    the salary that ends with it. Each lets the premium year of the event and
    the year after be set from that year's own income - after them the
    lookback year is already one the event changed."""
    if not retirement.get_ssa44_appeal(conn):
        return {}
    events = set()
    for src in retirement.plan_income_sources(conn):
        if src.kind == "salary" and src.end_year is not None:
            events.add(int(src.end_year) + 1)
    scenario = retirement.get_survivor_scenario(conn)
    if scenario is not None:
        events.add(scenario.death_year + 1)
    out: dict[int, int] = {}
    for event in sorted(events):
        for year in (event, event + 1):
            out.setdefault(year, event)
    return out


def plan_last_year(conn, today: Optional[_dt.date] = None) -> Optional[int]:
    """The last year of the plan as the planner draws it, or None with nobody
    to end it on.

    Exactly :func:`plan_horizon`'s last year at the stored "Plan through age":
    the same people, the same stretch to cover a conversion scheduled past
    the case, the same cap. It used to take the OLDEST horizon person's year
    while the planner drew to the youngest's, so the dashboard's "Plan (N
    years)" and the planner's last bar disagreed whenever the household had
    no self person on file."""
    if not horizon_people(conn):
        return None
    return plan_horizon(conn, retirement.get_plan_through_age(conn),
                        today or _dt.date.today())[-1]


def taxable_basis_cents(conn, account_ids, as_of: str) -> dict[int, list[tuple[int, int]]]:
    """Each taxable account's positions today as ``(value, basis)`` pairs -
    its cash (no gain) and each holding at what it is worth and what it
    cost - for the plan to sell cheapest first. Every gain is long-term: the
    plan's sales are years off."""
    from mammon import portfolio
    out = {}
    for aid in account_ids:
        try:
            value = portfolio.account_valuation(conn, int(aid), as_of)
        except Exception:
            continue                   # an account that will not value has no basis
        positions = [(int(value.cash), int(value.cash))] if value.cash > 0 else []
        positions += [(int(h.market_value), int(h.cost_basis or 0))
                      for h in value.holdings if int(h.market_value) > 0]
        out[int(aid)] = positions
    return out


def tax_exempt_cents(conn, year: int, sources=None) -> int:
    """Tax-exempt interest in ``year``: investment income the Income dialog
    marks not taxable (municipal bonds). Out of taxable income, but in
    provisional income (IRC 86(b)(2)(B)), IRMAA's MAGI and the ACA's."""
    found = retirement.plan_income_sources(conn) if sources is None else sources
    return sum(src.cents_in(year) for src in found
               if src.kind == "investment" and not src.taxable)


def preferential_cents(conn, year: int, sources=None) -> int:
    """Taxable income the Income dialog marks for the capital-gains rates -
    qualified dividends, long-term gains from a fund (IRC 1(h)(11)) - which
    stacks on ordinary income like the plan's own gains."""
    found = retirement.plan_income_sources(conn) if sources is None else sources
    return sum(src.cents_in(year) for src in found if src.taxable and src.preferential)


def pension_cents(conn, year: int, sources=None) -> int:
    """Taxable pension income in ``year``: what a state that exempts
    retirement income leaves out, with the IRA draws and conversions."""
    found = retirement.plan_income_sources(conn) if sources is None else sources
    return sum(src.cents_in(year) for src in found if src.taxable and src.kind == "pension")


def salary_contributions(conn, src, year: int, people=None) -> tuple[int, int, int]:
    """(pre-tax deferral, Roth catch-up, employer match) a salary source puts
    into its plan in ``year``, capped at the law's limits for the earner's
    age (:func:`retirement.capped_contribution_cents`). An earner with no
    birth year on file is read as under 50."""
    # Any source with a deferral into a plan, whatever it is called: a salary
    # the Income dialog's own form made, or one entered as other income.
    if src.into_account_id is None or not (src.deferral_pct or src.match_pct):
        return 0, 0, 0
    people = planning_people(conn) if people is None else people
    person = next((p for p in people if src.person_id is not None
                   and int(p["id"]) == int(src.person_id)), None)
    if person is None:
        person = next((p for p in people if p.get("relationship") == "self"), None)
    age = (int(year) - int(person["birth_year"])) if person and person.get("birth_year") else 0
    return retirement.capped_contribution_cents(
        src.cents_in(year), src.deferral_pct, src.match_pct, age, int(year),
        retirement.get_bracket_index_pct(conn), prior_wages_cents=src.cents_in(int(year) - 1))


def basis_ratio_by_year(conn, editor, years: Sequence[int]) -> dict[int, Decimal]:
    """Per year, the share of the household's tax-deferred draws and
    conversions that is a return of after-tax basis (Form 8606's pro-rata
    rule, IRC 408(d)(2)): each owner's basis over the balance their deferred
    accounts enter the year with, applied to the stored plan's draws and
    conversions out of them, the basis shrinking by what came back. Zero with
    no basis on file."""
    def balance_of(account_id: int, year: int) -> int:
        return int(editor.projected_values(account_id).get(int(year), 0))

    def out_of(account_id: int, year: int) -> int:
        flow = editor.flows_for(int(year)).get(account_id)
        return (int(flow.distribution_cents) + int(flow.conversion_out_cents)
                if flow is not None else 0)

    return basis_ratios(editor.accounts(), years, balance_of, out_of)


def basis_ratios(accounts, years: Sequence[int], balance_of, out_of) -> dict[int, Decimal]:
    """:func:`basis_ratio_by_year`'s arithmetic over any plan: ``balance_of``
    and ``out_of`` give an account's balance entering a year and what leaves
    it that year (draws and conversions). The page reads the STORED plan; the
    fill solver reads the plan it is trying, so the two measure one number
    (a fill of later years stopped $10 short, measured with the ratio of the
    plan before the earlier years' conversions)."""
    accounts = [a for a in accounts if a.treatment == "deferred" and not a.planned]
    owners: dict = {}
    for acct in accounts:
        owners.setdefault(acct.owner_person_id, []).append(acct)
    remaining = {owner: sum(a.after_tax_basis_cents for a in group)
                 for owner, group in owners.items()}
    if not any(remaining.values()):
        return {}
    out: dict[int, Decimal] = {}
    for year in years:
        nontaxable = taxable = Decimal(0)
        for owner, group in owners.items():
            if remaining[owner] <= 0:
                continue
            balance = sum(max(0, int(balance_of(a.account_id, int(year)))) for a in group)
            out_of_year = sum(int(out_of(a.account_id, int(year))) for a in group)
            if balance <= 0 or out_of_year <= 0:
                continue
            ratio = min(Decimal(1), Decimal(remaining[owner]) / Decimal(balance))
            back = min(remaining[owner], int((Decimal(out_of_year) * ratio)
                                             .quantize(Decimal(1), rounding=ROUND_HALF_UP)))
            remaining[owner] -= back
            nontaxable += back
            taxable += out_of_year
        if taxable > 0 and nontaxable > 0:
            out[int(year)] = nontaxable / taxable
    return out


def investment_income_cents(conn, year: int, sources=None) -> int:
    """The year's taxable net investment income from the Income dialog: the
    investment-income section and any other income marked for the NIIT."""
    found = retirement.plan_income_sources(conn) if sources is None else sources
    return sum(src.cents_in(year) for src in found
               if src.taxable and src.investment_income)


def claim_age_months(person: Mapping) -> Optional[int]:
    """The claim age this projection uses, in months, or None.

    A planned claim age is the user's own; when none is recorded the statutory
    full retirement age stands in. That is not a recommendation - it is the one
    age the law itself calls normal, and the caption says which was used.
    """
    planned = person.get("planned_claim_age_months")
    if planned:
        return int(planned)
    year = person.get("birth_year")
    if year is None:
        return None
    return int(retirement.full_retirement_age_months(int(year)))


def claim_start(person: Mapping, months: int) -> Optional[tuple[int, int]]:
    """The (year, month) the first benefit is payable for, or None.

    Built on :func:`retirement.attainment_year_month` for the whole-year part so
    the born-on-the-first rule (20 CFR 404.2(c)(4)) is applied in exactly one
    place, then advanced by the leftover months of the claim age.
    """
    if months is None:
        return None
    years, extra = divmod(int(months), 12)
    base = retirement.attainment_year_month(person, years)
    if base is None:
        return None
    year, month = base
    total = int(month) + int(extra)
    return (int(year) + (total - 1) // 12, (total - 1) % 12 + 1)


def _months_from(start: Optional[tuple[int, int]], year: int) -> int:
    """Monthly payments during ``year`` from a (year, month) start: 0 before
    it, a part year in the start year, 12 after."""
    if start is None:
        return 0
    start_year, start_month = start
    if int(year) < start_year:
        return 0
    if int(year) > start_year:
        return 12
    return 13 - int(start_month)


def months_claimed(person: Mapping, months: int, year: int) -> int:
    """How many monthly benefits a person is paid during ``year``: 0 before the
    claim, a part year in the year they claim, 12 after."""
    return _months_from(claim_start(person, months), year)


def age_months_at(person: Mapping, year: int, month: int) -> int:
    """A person's age in whole months at (``year``, ``month``)."""
    born_month = int(person.get("birth_month") or 1)
    return (int(year) - int(person["birth_year"])) * 12 + int(month) - born_month


def self_person_id(people: Sequence[Mapping]) -> Optional[int]:
    return next((int(p["id"]) for p in people if p.get("relationship") == "self"), None)


def salary_cents_in(person_id: int, year: int, sources, people: Sequence[Mapping]) -> int:
    """A person's wages in ``year`` from the salary sources: theirs by
    ``person_id``; a salary naming nobody is the self person's."""
    mine = int(person_id)
    default = self_person_id(people)
    return sum(src.cents_in(year) for src in sources
               if src.kind == "salary"
               and (src.person_id == mine or (src.person_id is None and mine == default)))


@dataclass
class BenefitRecord:
    """One person's Social Security as the projection pays it, in today's
    dollars: their own retirement benefit, the spousal benefit on top of it,
    and the retirement earnings test's withholding before full retirement
    age. Built by :func:`benefit_records`."""

    person: Mapping
    pia_cents: int                              # 0 with no earnings record
    claim_months: int
    start: Optional[tuple[int, int]]            # first month of the own benefit
    fra_months: int
    fra_start: Optional[tuple[int, int]]        # the month full retirement age is reached
    own_monthly_cents: int = 0                  # at the claim factor
    spousal_monthly_cents: int = 0              # on the spouse's record
    spousal_start: Optional[tuple[int, int]] = None
    withheld_months: int = 0                    # earnings test, before full retirement age
    own_after_fra_cents: int = 0                # recomputed for the months withheld

    @property
    def person_id(self) -> int:
        return int(self.person["id"])

    def split_months(self, start, year: int) -> tuple[int, int]:
        """(months before full retirement age, months from it) paid in ``year``
        for a benefit that began at ``start``."""
        paid = _months_from(start, year)
        after = min(paid, _months_from(self.fra_start, year)) if self.fra_start else 0
        return paid - after, after

    def paid_in(self, year: int, *, spousal: bool = True) -> tuple[int, int]:
        """(paid in the months before full retirement age, paid from it) in
        ``year`` - own and, unless ``spousal`` is off, spousal together."""
        before, after = self.split_months(self.start, year)
        early = self.own_monthly_cents * before
        late = self.own_after_fra_cents * after
        if spousal and self.spousal_monthly_cents:
            sp_before, sp_after = self.split_months(self.spousal_start, year)
            early += self.spousal_monthly_cents * sp_before
            late += self.spousal_monthly_cents * sp_after
        return early, late

    def withheld_in(self, year: int, earnings_cents: int, index_pct) -> int:
        """The earnings test: what ``earnings_cents`` of wages in ``year``
        withhold out of the months before full retirement age."""
        if self.fra_start is None or int(year) > self.fra_start[0]:
            return 0
        before, _after = self.paid_in(year)
        if before <= 0:
            return 0
        fra_year, fra_month = self.fra_start
        in_fra_year = int(year) == fra_year
        earned = int(earnings_cents)
        if in_fra_year:
            earned = earned * (fra_month - 1) // 12      # the months before it
        return retirement.earnings_test_withheld_cents(
            earned, before, int(year), index_pct, year_of_full_retirement=in_fra_year)


def benefit_records(conn, people: Sequence[Mapping], sources=None) -> dict[int, BenefitRecord]:
    """Every planning person's benefit record, with the spousal benefit and
    the earnings test settled between the self and spouse."""
    sources = retirement.plan_income_sources(conn) if sources is None else sources
    index_pct = retirement.get_bracket_index_pct(conn)
    records: dict[int, BenefitRecord] = {}
    for person in people:
        months = claim_age_months(person)
        if months is None:
            continue
        months = retirement.earliest_claim_months(person, months)
        earnings = retirement.earnings_map(conn, int(person["id"]))
        birth_year = int(person["birth_year"])
        pia = (retirement.primary_insurance_cents(earnings, birth_year)
               if any(int(cents) > 0 for cents in earnings.values()) else 0)
        fra = retirement.full_retirement_age_months(birth_year)
        rec = BenefitRecord(person=person, pia_cents=pia, claim_months=int(months),
                            start=claim_start(person, months), fra_months=fra,
                            fra_start=claim_start(person, fra))
        rec.own_monthly_cents = retirement.benefit_from_pia_cents(
            pia, retirement.claim_factor(birth_year, int(months)))
        rec.own_after_fra_cents = rec.own_monthly_cents
        records[int(person["id"])] = rec
    # The spousal benefit (42 USC 402(b), (c)): half the worker's PIA less the
    # person's own, from the later of the two claims, reduced for the person's
    # age then. Reported: a spouse with no record of their own was paid nothing.
    couple = [r for r in records.values()
              if r.person.get("relationship") in ("self", "spouse")]
    if len(couple) == 2:
        for rec, worker in (couple, couple[::-1]):
            if rec.start is None or worker.start is None or worker.pia_cents <= 0:
                continue
            begins = max(rec.start, worker.start)
            rec.spousal_monthly_cents = retirement.spousal_benefit_cents(
                worker.pia_cents, rec.pia_cents, int(rec.person["birth_year"]),
                age_months_at(rec.person, *begins))
            rec.spousal_start = begins if rec.spousal_monthly_cents else None
    # The earnings test (42 USC 403(b), (f)): a salary running past an early
    # claim withholds benefits, and the months withheld raise the benefit at
    # full retirement age as if it had been claimed that much later.
    for rec in records.values():
        if rec.start is None or rec.fra_start is None or rec.own_monthly_cents <= 0:
            continue
        months = 0
        for year in range(rec.start[0], rec.fra_start[0] + 1):
            withheld = rec.withheld_in(
                year, salary_cents_in(rec.person_id, year, sources, people), index_pct)
            if withheld > 0:
                months += -(-withheld // rec.own_monthly_cents)     # whole checks
        months = min(months, max(0, rec.fra_months - rec.claim_months))
        if months:
            rec.withheld_months = months
            rec.own_after_fra_cents = retirement.benefit_from_pia_cents(
                rec.pia_cents, retirement.claim_factor(int(rec.person["birth_year"]),
                                                       rec.claim_months + months))
    return records


def _deceased_factor(rec: BenefitRecord, death_year: int) -> Decimal:
    """What the deceased's benefit carried into the survivor's: their claim
    factor when they had claimed by the death, otherwise the delayed credits
    earned past full retirement age, or none."""
    birth_year = int(rec.person["birth_year"])
    if rec.start is not None and rec.start[0] <= int(death_year):
        return retirement.claim_factor(birth_year, rec.claim_months)
    age = age_months_at(rec.person, int(death_year), 12)
    if age <= rec.fra_months:
        return Decimal(1)
    return retirement.claim_factor(birth_year, min(age, retirement.SS_LATEST_CREDIT_MONTHS))


def social_security_cents(conn, year: int, people: Optional[Sequence[Mapping]] = None,
                          *, base_year: Optional[int] = None) -> int:
    """The household's Social Security for one year, in cents.

    Each person's own earnings record run through the published formula at their
    claim age, plus the spousal benefit where half the other's primary insurance
    amount is more than their own, less the earnings test's withholding while a
    salary runs before full retirement age (:func:`benefit_records`) - in TODAY's
    dollars, the basis the Social Security dialog states - and then, when
    ``base_year`` (today's year) is given, raised by the household's COLA
    (``retirement.get_cola_pct``) for every year after it. Reported: without a
    COLA the benefit stood still while the spending need rose 3% a year, so the
    plan leaned harder on the accounts every year than a real household would.
    A person with no earnings on file draws nothing of their own: a child of
    record is not a Social Security claimant, and neither is a person whose
    earnings rows are all zero, so neither one reaches the benefit formula at
    all - but a spouse with no record still draws the spousal benefit.
    """
    rows = planning_people(conn) if people is None else people
    scenario = retirement.get_survivor_scenario(conn)
    widowed = scenario is not None and scenario.widowed(year)
    sources = retirement.plan_income_sources(conn)
    index_pct = retirement.get_bracket_index_pct(conn)
    records = benefit_records(conn, rows, sources)
    cola = retirement.get_cola_pct(conn)

    def raised(cents: int, birth_year: int) -> int:
        """The COLA (42 USC 415(i)) runs from the year the worker turns 62 -
        the figure is in that year's dollars - and from today for anyone past
        it (found in an audit: a younger worker's benefit was raised from
        today, in years the law raises it with wages instead)."""
        if base_year is None or not cents:
            return cents
        since = max(int(base_year), int(birth_year) + retirement.SS_ELIGIBILITY_AGE)
        if int(year) <= since:
            return cents
        return retirement.household_amount_cents(cents, cola, int(year) - since)

    paid_by: dict[int, int] = {}
    for pid, rec in records.items():
        # A survivor's spousal benefit ended with the marriage.
        keep_spousal = not (widowed and pid == scenario.survivor_id)
        before, after = rec.paid_in(year, spousal=keep_spousal)
        withheld = (rec.withheld_in(year, salary_cents_in(pid, year, sources, rows),
                                    index_pct) if keep_spousal else 0)
        paid_by[pid] = raised(max(0, before - withheld) + after,
                              int(rec.person["birth_year"]))
    if widowed:
        # The widow(er)'s benefit (42 USC 402(e), (f)): the deceased's stops,
        # and the survivor is paid the larger of their own and the deceased's
        # benefit reduced for the survivor's age when it begins - 71.5% at 60,
        # in full at the survivor full retirement age - payable from 60.
        paid_by.pop(scenario.deceased_id, None)
        deceased = records.get(scenario.deceased_id)
        survivor = records.get(scenario.survivor_id)
        if deceased is not None and survivor is not None and deceased.pia_cents > 0:
            who = survivor.person
            begins = max(scenario.death_year + 1, int(who["birth_year"]) + 60)
            if int(year) >= begins:
                monthly = retirement.survivor_benefit_cents(
                    deceased.pia_cents, _deceased_factor(deceased, scenario.death_year),
                    int(who["birth_year"]), age_months_at(who, begins, 1))
                sid = scenario.survivor_id
                paid_by[sid] = max(paid_by.get(sid, 0),
                                   raised(monthly * 12, int(deceased.person["birth_year"])))
    total = sum(paid_by.values())
    if base_year is not None and total:
        # Not "always there": from the trust fund's depletion year only the
        # payable share is paid (retirement.get_ss_shortfall; reported).
        total = retirement.get_ss_shortfall(conn).payable(total, int(year))
    return total


def plan_horizon(conn, terminal_age: int, today: _dt.date) -> list[int]:
    """The years to draw, this year through the terminal-age case.

    A planned CONVERSION is never dropped off the end - one scheduled past the
    selected case still has to be visible, or the user would think it had been
    lost. A planned withdrawal deliberately does not extend the horizon: since
    the page seeds every required minimum out to the longest longevity case,
    an end that stretched to cover withdrawals would always stretch to age 100
    and the terminal-age case would stop meaning anything.
    """
    conversions = retirement.conversion_years(conn)
    # THIS year first, never an earlier one. Plan rows for a year gone by are
    # kept (nothing deletes history), and starting the horizon at the earliest
    # row put today's balance at the start of last year and took last year's
    # draw out of it again every January - while the Investment Dashboard,
    # which starts at its as-of year, did not (reported by audit).
    first = today.year
    people = horizon_people(conn)
    if people:
        last = max(int(p["birth_year"]) + int(terminal_age) for p in people)
    else:
        last = first + FALLBACK_HORIZON_YEARS - 1
    if conversions:
        last = max(last, max(int(y) for y in conversions))
    last = max(last, first)
    last = min(last, first + MAX_YEARS - 1)
    return list(range(first, last + 1))


def projection_mix(conn, account_ids: Sequence[int], as_of: str) -> Optional[dict]:
    """The asset-class weights the retirement accounts are projected at, or
    None when they cannot be measured.

    The plan's own mix first (``retirement.plan_projection_mix``: the typed
    stocks/bonds/cash at its OWN weights, or the ladder's mix at a level copied
    in), because the household intends to hold it and projecting today's
    holdings would overstate or understate growth (reported). Otherwise the
    mix the real accounts hold today, through the Investment Dashboard's
    ``current_mix`` so the two pages classify a holding once. None means "we
    could not measure it", and the caller then draws NO fund line: a made-up
    return assumption behind the bars would be the one number on this page
    the user could not trace to anything.
    """
    planned = retirement.plan_projection_mix(conn)
    if planned is not None:
        return planned
    # A planned Roth has no holdings to measure; the real accounts in the pool
    # still say how the money is invested.
    account_ids = [a for a in account_ids if not retirement.is_planned(a)]
    if not account_ids:
        return None
    try:
        return portfolio.current_mix(conn, list(account_ids), as_of)
    except Exception:
        return None


def measured_risk(conn, account_ids: Sequence[int], as_of: str) -> Optional[float]:
    """The thermometer level of :func:`projection_mix` - the ladder rung of
    the same volatility - or None. A POSITION, kept for callers that want one;
    every projection here uses the mix itself."""
    mix = projection_mix(conn, account_ids, as_of)
    return None if mix is None else float(forecast.risk_for_mix(mix))


def _moments_for(conn, account_ids: Sequence[int], as_of: str,
                 risk: Optional[float]) -> Optional[tuple[float, float]]:
    """(mu, sigma) for a projection: at ``risk``'s ladder mix when a caller
    names a level, else at :func:`projection_mix`."""
    if risk is not None:
        return forecast.portfolio_moments(forecast.mix_for_risk(risk))
    mix = projection_mix(conn, account_ids, as_of)
    return None if mix is None else forecast.portfolio_moments(mix)


def fund_values_cents(conn, account_ids: Sequence[int], net_by_year: Sequence[int],
                      *, start_cents: int, as_of: str,
                      risk: Optional[float] = None,
                      draws_by_year: Optional[Sequence[int]] = None) -> list[int]:
    """The median projected value of the pool at the START of each plan year.

    The plan's net flow for a year (``AccountFlow.net_cents`` summed over the
    pool, so a conversion between two accounts inside it nets to zero) is spread
    across monthly steps by ``forecast.steps_from_annual``, the one place a
    per-year series becomes a step plan (the Investment Dashboard's projection
    fan builds its planned flows through the same function, so the two pictures
    of one plan cannot drift apart). The first year is the months LEFT in it
    (``forecast.first_year_periods``): every entry of ``net_by_year`` is that
    year's rate, and the current year's is taken only for what remains of it.
    Note the units: a ``Step`` contribution is in DOLLARS while
    ``fan_from_steps`` takes and returns CENTS.

    ``draws_by_year`` applies the plan's run-out rule
    (``forecast.cut_after_ruin``): from the first year whose median cannot
    cover its draw, the line is zero.

    An empty list means the line cannot be drawn honestly.
    """
    if not net_by_year:
        return []
    moments = _moments_for(conn, account_ids, as_of, risk)
    if moments is None:
        return []
    mu, sigma = moments
    first = forecast.first_year_periods(as_of)
    plan = forecast.steps_from_annual([int(net) / 100.0 for net in net_by_year],
                                      mu, sigma, first_year_periods=first)
    points = forecast.fan_from_steps(int(start_cents), plan, first_year_periods=first)
    if draws_by_year is not None:
        points = forecast.cut_after_ruin(points, draws_by_year)
    # One point per YEAR boundary: index 0 is today, so a bar's line value is
    # the fund at the start of that bar's year.
    return [int(p.p50) for p in points[:len(net_by_year)]]


def mix_account_ids(conn, account_id: int,
                    accounts: Optional[Sequence[PlanAccount]] = None) -> list[int]:
    """The accounts whose mix a projection of ``account_id`` grows at.

    Itself, for a real account. A planned Roth holds nothing yet, so it grows
    at the mix of the accounts converting into it (money converted in kind
    keeps its investments), or failing that at the whole pool's."""
    key = int(account_id)
    if not retirement.is_planned(key):
        return [key]
    sources = sorted({int(r["from_account_id"])
                      for r in retirement.list_conversions(conn)
                      if int(r["to_account_id"]) == key})
    pool = plan_accounts(conn) if accounts is None else accounts
    return sources or [a.account_id for a in pool if not a.planned]


def account_growth_factors(conn, account_id: int, years: Sequence[int], *,
                           as_of: str, risk: Optional[float] = None,
                           accounts: Optional[Sequence[PlanAccount]] = None
                           ) -> Optional[dict]:
    """Plan year -> the MEDIAN growth factor carrying the account into the next.

    A RATE, so it is measured from a nominal start (``GROWTH_PROBE_CENTS``)
    rather than the account's balance: from the balance, an account that starts
    empty - a planned Roth, before any conversion lands in it - projects zero
    every year, reads as 0%, and then holds everything later converted into it
    flat for decades. None when the mix cannot be measured.
    """
    values = fund_values_cents(conn, mix_account_ids(conn, account_id, accounts),
                               [0] * len(years), start_cents=GROWTH_PROBE_CENTS,
                               as_of=as_of, risk=risk)
    if not values:
        return None
    factors: dict[int, Decimal] = {}
    for index, year in enumerate(years):
        here = values[index] if index < len(values) else 0
        nxt = values[index + 1] if index + 1 < len(values) else 0
        factors[int(year)] = (Decimal(nxt) / Decimal(here)
                              if here > 0 and nxt > 0 else Decimal(1))
    return factors


def deduction_conditions(conn, year: int, filing_status: str) -> int:
    """How many IRC 63(f) age-65 conditions the return claims in ``year``: the
    self, and on a joint return the spouse too, once each is 65."""
    covered = living_couple(conn, year, filing_status)
    if filing_status != "joint":
        covered = covered[:1]           # a separate return claims only its filer

    def reached(p: Mapping) -> int:
        # Sixty-five on the day before the birthday: born January 1, the
        # year before (IRS Pub. 501).
        early = 1 if p.get("born_on_the_first") and int(p.get("birth_month") or 0) == 1 else 0
        return int(p["birth_year"]) + 65 - early

    return sum(1 for p in covered if reached(p) <= int(year))


def other_taxable_cents(conn, year: int, deferred_draws: int, sources=None) -> int:
    """Ordinary income apart from Social Security: taxable other income LESS
    pre-tax 401(k) deferrals out of it, plus tax-deferred draws. A deferral into
    a Roth 401(k) is not pre-tax. This is what IRC 86 measures the benefit
    against."""
    found = retirement.plan_income_sources(conn) if sources is None else sources
    return (retirement.other_income_cents(conn, year, taxable_only=True,
                                          sources=found)
            - pretax_deferrals_cents(conn, year, found)
            - preferential_cents(conn, year, found)
            + int(deferred_draws))


def gross_taxable_cents(conn, year: int, social_security: int, deferred_draws: int,
                        sources=None, filing_status: str = "single") -> int:
    """Ordinary income before the deduction: :func:`other_taxable_cents` plus
    the share of Social Security that income makes taxable (IRC 86)."""
    return retirement.gross_with_social_security_cents(
        other_taxable_cents(conn, year, deferred_draws, sources),
        social_security, filing_status)


def pretax_deferrals_cents(conn, year: int, sources) -> int:
    """The employee 401(k) deferrals in ``year`` that are pre-tax: into a
    tax-deferred account, or into none named (assumed traditional)."""
    roths = {a.account_id for a in plan_accounts(conn) if a.is_roth}
    people = planning_people(conn)
    return sum(salary_contributions(conn, src, year, people)[0] for src in sources
               if src.taxable and src.into_account_id not in roths)


def retirement_start_year(conn) -> Optional[int]:
    """The household's retirement year: the stored plan's start year, or None.

    Only a year the household STATED. Falling back to the Social Security claim
    age guessed that the saver stops working when benefits start, which turned
    a current employer's plan into one owing minimums nobody said applied."""
    params = retirement.get_withdrawal_plan(conn)
    return int(params.start_year) if params.start_year is not None else None


#: While a fill runs, the measured inflows by (connection, day, accounts):
#: nothing in the ledger changes during one, and measuring them - every
#: account's trailing-year deposits - was most of each round's time (a fill of
#: sixteen years took 38 seconds). None outside :func:`inflows_measured_once`.
_MEASURED_INFLOWS: Optional[dict] = None


@contextmanager
def inflows_measured_once():
    """Measure each account's trailing-year inflow once for the whole block."""
    global _MEASURED_INFLOWS
    outer = _MEASURED_INFLOWS
    if outer is None:
        _MEASURED_INFLOWS = {}
    try:
        yield
    finally:
        if outer is None:
            _MEASURED_INFLOWS = None


def _measured_inflows(conn, today: _dt.date, account_ids) -> dict[int, int]:
    key = (id(conn), today, tuple(sorted(account_ids)))
    if _MEASURED_INFLOWS is not None and key in _MEASURED_INFLOWS:
        return dict(_MEASURED_INFLOWS[key])
    try:
        measured = {int(a.account_id): int(a.total)
                    for a in portfolio.inflow_arrows(conn, today.isoformat(),
                                           account_ids=list(account_ids))}
    except Exception:
        measured = {}                 # a measurement that fails is no contribution
    if _MEASURED_INFLOWS is not None:
        _MEASURED_INFLOWS[key] = dict(measured)
    return measured


def planned_contributions(conn, today: _dt.date,
                          until_year: Optional[int]) -> dict[int, dict[int, int]]:
    """Year -> account -> cents of new money going into each retirement account,
    each year's figure a yearly RATE - this year's included.

    Each account's MEASURED yearly inflow - the trailing-year deposits the
    Investment Dashboard's inflow arrows show - carried flat from today until the
    retirement year (exclusive). Reported: a current employer's 401(k) was
    projected from today's balance as if nothing more would ever go in. A flat
    measured amount is the simple version; a savings plan that varies by year
    belongs to budgeting. Nothing is measured without a retirement year to
    stop at.

    An account a salary names (``IncomeSource.into_account_id`` with a deferral
    or match) is not measured: it gets that salary's deferral plus match each
    year the salary runs - its raises included - which is the number the
    household actually knows (a deferral percentage and an employer match).

    This year's figure is prorated to the months left
    (``forecast.first_year_periods``): a contribution is a stream, and only
    the rest of this year's is still to come. The projection spreads each
    year's flow over the months left of it as an AMOUNT, which is right for a
    conversion or a planned draw - money that has to move before December 31 -
    and so the stream is cut to its remaining share here, before it joins them.
    (For a day the audit of 2026-09-26 took every first-year flow as a rate
    instead; a quarter of each conversion then moved, and fills stopped short.)
    """
    real = [a.account_id for a in plan_accounts(conn) if not a.planned]
    if not real:
        return {}
    sources = [src for src in retirement.plan_income_sources(conn)
               if src.into_account_id in real and (src.deferral_pct or src.match_pct)]
    linked = {src.into_account_id for src in sources}
    measured: dict[int, int] = {}
    if until_year is not None and int(until_year) > today.year:
        measured = {aid: cents for aid, cents
                    in _measured_inflows(conn, today, real).items()
                    if aid not in linked}
    last = max([int(until_year) - 1 if until_year is not None else today.year]
               + [src.end_year if src.end_year is not None
                  else (int(until_year) - 1 if until_year is not None else today.year)
                  for src in sources])
    out: dict[int, dict[int, int]] = {}
    left = Decimal(forecast.first_year_periods(today)) / 12
    for year in range(today.year, last + 1):
        share = left if year == today.year else Decimal(1)
        cut = {}
        if until_year is None or year < int(until_year):
            cut.update(measured)
        for src in sources:
            # A linked salary runs on ITS OWN years - each earner retires on
            # their own schedule - not the household's retirement year. Capped
            # at the law's limits; a Roth catch-up lands in the same account.
            cut[src.into_account_id] = (cut.get(src.into_account_id, 0)
                                        + sum(salary_contributions(conn, src, year)))
        cut = {aid: int((Decimal(cents) * share).quantize(
            Decimal(1), rounding=ROUND_HALF_UP)) for aid, cents in cut.items() if cents}
        if cut:
            out[year] = cut
    return out


def pool_fund(conn, accounts: Sequence[PlanAccount], *, as_of: str,
              risk: Optional[float] = None):
    """The whole retirement pool as a ``forecast.TrackedFund``, or None.

    Started from the pool's value and grown at the pool's measured mix - the
    same start and mix :func:`income_rows` hands :func:`fund_values_cents` for
    the fund line, which is what makes the household check and the line agree.
    """
    ids = [a.account_id for a in accounts]
    moments = _moments_for(conn, ids, as_of, risk)
    if moments is None:
        return None
    mu, sigma = moments
    start = sum(max(0, account_value_cents(conn, aid)) for aid in ids)
    # The first year is the months left in it, as the fund line's is.
    return forecast.TrackedFund(int(start), mu, sigma,
                                first_year_periods=forecast.first_year_periods(as_of))


def income_rows(conn, *, terminal_age: int = DEFAULT_TERMINAL_AGE,
                today: Optional[_dt.date] = None,
                risk: Optional[float] = None) -> list[IncomeYear]:
    """Every bar on the page, oldest year first."""
    today = today or _dt.date.today()
    accounts = retirement.spending_accounts(conn)
    deferred_ids = {a.account_id for a in accounts if a.treatment == "deferred"}
    roth_ids = {a.account_id for a in accounts if a.is_roth}
    taxable_ids = {a.account_id for a in accounts if a.is_taxable}
    pool = sorted(deferred_ids | roth_ids | taxable_ids)
    people = planning_people(conn)
    # Ages are the same person's the horizon ends on, so "age 90" on the axis
    # is the last bar of the age-90 case.
    born = min((int(p["birth_year"]) for p in horizon_people(conn)), default=None)

    years = plan_horizon(conn, terminal_age, today)
    sources = retirement.plan_income_sources(conn)
    contributions = planned_contributions(conn, today, retirement_start_year(conn))
    partial: list[tuple] = []
    nets: list[int] = []
    for year in years:
        flows = retirement.plan_flows(conn, year, contributions.get(year))
        deferred = sum(f.distribution_cents for aid, f in flows.items()
                       if aid in deferred_ids)
        roth = sum(f.distribution_cents for aid, f in flows.items() if aid in roth_ids)
        taxable = sum(f.distribution_cents for aid, f in flows.items()
                      if aid in taxable_ids)
        converted = sum(f.conversion_out_cents for aid, f in flows.items()
                        if aid in deferred_ids or aid in roth_ids)
        nets.append(sum(f.net_cents for aid, f in flows.items() if aid in pool))
        partial.append((year, deferred, roth, converted,
                        social_security_cents(conn, year, people,
                                              base_year=today.year),
                        sum(src.cents_in(year) for src in sources), taxable))

    # The pool's MEDIAN from the recursion that carries mean and variance and
    # floors the fund at zero (forecast's notes) - the same one the household
    # check steps a year at a time (``pool_fund``), so the line enters the year
    # the money runs out holding exactly what that year can draw. The line is
    # the MEDIAN outcome; once a year draws everything the median holds, the
    # money has run out and stays out (``forecast.cut_after_ruin``, which the
    # Investment Dashboard's fan applies too) - the household plan draws
    # nothing after that year, and without the cut the line crept back up
    # from the spread of outcomes left behind, showing money the plan says
    # is gone.
    start = sum(max(0, account_value_cents(conn, aid)) for aid in pool)
    spent = [d + r + t for (_y, d, r, _c, _b, _o, t) in partial]
    line = fund_values_cents(conn, pool, nets, start_cents=start,
                             as_of=today.isoformat(), risk=risk,
                             draws_by_year=spent)

    rows: list[IncomeYear] = []
    for index, (year, deferred, roth, converted, benefit, other,
                taxable) in enumerate(partial):
        rows.append(IncomeYear(
            year=year,
            age=(year - born) if born is not None else None,
            social_security_cents=benefit,
            deferred_draw_cents=deferred,
            roth_draw_cents=roth,
            conversion_cents=converted,
            fund_value_cents=line[index] if index < len(line) else 0,
            other_income_cents=other,
            taxable_draw_cents=taxable,
        ))
    return rows


def default_filing_status(conn) -> str:
    """Which bracket ladder to draw first: "joint" when a spouse is on file.

    A starting VIEW, not a stored tax election - the user switches it freely and
    nothing is written, because Mammon does not know how anybody files and will
    not record a guess as if it did.
    """
    try:
        if retirement.list_people(conn, relationship="spouse"):
            return "joint"
    except Exception:
        pass
    return "single"


def stale_withdrawal_years(conn, plan) -> list[int]:
    """The years whose STORED withdrawals are not the ones ``plan`` draws.

    Apply writes a household plan computed from the ledger as it stood that
    day - today's balances and prices, each account's trailing-year deposits,
    the part of this year left - and nothing rewrites it when those move:
    editing a conversion re-plans from that year on, but a new download, a
    price or the calendar is a change from THIS year on, and no edit follows
    it (found on a ledger: a month on, the stored draws were thousands off in
    some years, and the page's tax disagreed with the plan's). Nothing
    is rewritten here: the page flags the schedule, and the user re-applies.

    ``plan`` is the stored spending plan as computed now
    (``HouseholdEngine.plan_for`` with ``retirement.get_withdrawal_plan``);
    a missing stored row counts as zero, which is what a plan year with no
    draw from an account looks like. Exact to the cent: Apply writes these
    amounts, so any difference is the inputs moving.
    """
    if plan is None:
        return []
    stored: dict[int, dict[int, int]] = {}
    for row in retirement.list_withdrawals(conn):
        stored.setdefault(int(row["year"]), {})[int(row["account_id"])] = \
            int(row["amount_cents"])
    return [entry.year for entry in plan.years
            if any(stored.get(entry.year, {}).get(int(aid), 0) != int(cents)
                   for aid, cents in entry.amounts.items())]


# ---------------------------------------------------------------------------
# the engines the page's editors are built on
# ---------------------------------------------------------------------------
class PlanEngine:
    """What the page's two inline tables compute: accounts, balances, the RMD.

    Both of them are a grid of plan years that writes through
    ``mammon.retirement``, and both have to answer "what is the smallest
    distribution the law allows here" the same way, so the rule lives once,
    here, and defers to the domain layer for the rule itself. Holds no widget:
    ``ui.retirement_planner._PlanEditor`` is a QWidget over this class, and
    :class:`HouseholdPlanner` is one with no screen at all.
    """

    def _init_engine(self, conn, today: Optional[_dt.date] = None) -> None:
        self.conn = conn
        self._today = today or _dt.date.today()
        self._years: list[int] = []
        self._accounts: list[PlanAccount] = []
        self._roths: list[PlanAccount] = []
        self._balances: dict[int, int] = {}
        self._flows: dict[int, dict] = {}
        self._projected: dict[int, dict[int, int]] = {}
        # The retirement year: contributions stop and a current employer's plan
        # becomes a former one. None = read the stored plan (plan_for sets it).
        self._until: Optional[int] = None
        self._contributions: dict = {}

    def accounts(self) -> list[PlanAccount]:
        return list(self._accounts or plan_accounts(self.conn))

    def _read_accounts(self) -> None:
        self._accounts = plan_accounts(self.conn)
        self._roths = [a for a in self._accounts if a.is_roth]

    # -- balances, and the assumption under them ----------------------------
    def balance_cents(self, account_id: int) -> int:
        """Today's value of an account, in cents. An overridable seam.

        Cached, because a table of sixty years asks for the same handful of
        accounts on every row and a portfolio valuation is not free.
        """
        key = int(account_id)
        if key not in self._balances:
            self._balances[key] = account_value_cents(self.conn, key)
        return self._balances[key]

    def prior_year_end_balance_cents(self, account_id: int, year: int) -> int:
        """The December 31 balance an RMD for ``year`` is computed from.

        The account's PROJECTED value entering ``year`` - the same median
        projection of the stored plan the page draws (:meth:`projected_values`).
        It used to be held flat at today's value, which made every later
        minimum wrong: too low once the account had grown, too high once the
        plan had drawn it down. Today's value only when the year is outside the
        projection.
        """
        projected = self.projected_values(account_id)
        if int(year) in projected:
            return max(0, int(projected[int(year)]))
        return max(0, self.balance_cents(account_id))

    def invalidate_balances(self) -> None:
        self._balances.clear()
        self.invalidate_plan()

    # -- the person a rule is read against ----------------------------------
    def birth_for_account(self, account_id: int) -> tuple[Optional[int], Optional[int]]:
        """(birth year, birth month) of the person whose account this is.

        Asking the ACCOUNT rather than a person picker is what lets two people's
        RMDs be right on one screen. The rule itself is
        :func:`retirement.account_birth`, so the seeding pass and this table
        cannot disagree about whose age applies.
        """
        return retirement.account_birth(self.conn, int(account_id))

    # -- the RMD floor ------------------------------------------------------
    def rmd_floor_cents(self, account_id: int, year: int) -> int:
        """The smallest distribution the law allows from this account in this year.

        Zero for a Roth account, for a current employer's plan, before the
        cohort's applicable age, and whenever no birth year is on file - in which
        case there is no rule to apply and Mammon does not invent one.
        """
        return retirement.account_rmd(
            self.conn, int(account_id),
            self.prior_year_end_balance_cents(account_id, year), int(year),
            employer_until=retirement.employer_plan_ends(self.conn,
                                                         self.retirement_year()),
        )

    # -- the ceiling: what is still in the account --------------------------
    def flows_for(self, year: int) -> dict:
        """This year's per-account plan flows, cached for the life of a rebuild."""
        key = int(year)
        if key not in self._flows:
            self._flows[key] = retirement.plan_flows(self.conn, key,
                                                     self.contributions_in(key))
        return self._flows[key]

    def account_nets(self, account_id: int, *,
                     draw_cents: Optional[int] = None,
                     years: Optional[Sequence[int]] = None) -> list[int]:
        """One account's net flow per plan year, in cents.

        ``draw_cents`` replaces the stored distribution with a proposed one - it
        is how the per-year button asks "would this amount last?" without writing
        anything first. The RMD still applies to the proposal, because a year
        whose minimum exceeds it takes the minimum.
        """
        nets: list[int] = []
        for year in (self._years if years is None else years):
            flow = self.flows_for(year).get(int(account_id))
            if draw_cents is None:
                nets.append(int(flow.net_cents) if flow else 0)
                continue
            out = int(flow.conversion_out_cents) if flow else 0
            into = int(flow.conversion_in_cents) if flow else 0
            draw = max(int(draw_cents), self.rmd_floor_cents(account_id, year))
            nets.append(into - out - draw)
        return nets

    def project_account(self, account_id: int, nets: Sequence[int], *,
                        start_cents: Optional[int] = None) -> list[int]:
        """One account's projected value at the START of each plan year, in cents.

        The same median projection the page already draws behind the bars, run
        over one account, so the ceiling on a cell and the line on the chart
        cannot tell the user two different stories about what is left. When the
        mix cannot be measured, :func:`fund_values_cents` draws nothing and this
        falls back to carrying the balance forward flat - a ceiling has to exist
        even when a growth rate does not.
        """
        start = max(0, self.balance_cents(account_id)
                    if start_cents is None else int(start_cents))
        values = fund_values_cents(self.conn, self._mix_ids(account_id), list(nets),
                                   start_cents=start,
                                   as_of=self._today.isoformat())
        if not values:
            values = []
            running = start
            for net in nets:
                values.append(running)
                running += int(net)
        return [max(0, int(v)) for v in values]

    def _mix_ids(self, account_id: int) -> list[int]:
        return mix_account_ids(self.conn, account_id, self.accounts())

    def projection_years(self) -> list[int]:
        """The years :meth:`projected_values` covers: the ones on screen."""
        return list(self._years)

    #: Set by the page: account -> {year: cents entering the year} from the
    #: stored HOUSEHOLD plan, or None when there is none (see
    #: :meth:`WithdrawalSchedule.household_balances`).
    plan_balances = None

    def projected_values(self, account_id: int) -> dict[int, int]:
        """Plan year -> what this account is projected to be worth entering it.

        With a household plan on file, the PLAN's own balances. The plan
        projects the whole pool and splits it among the accounts; this used to
        project each account alone from its own flows instead, a different
        model of the same money. A conversion capped by one and checked by the
        other did not fit, the plan's re-apply cut it, and the next re-apply
        recomputed that year's tax draws - so re-applying an unchanged plan
        rewrote years that nothing had touched (reported: "Re-applying the
        plan should produce exactly the same results in years before the
        change"). One model now answers both questions."""
        key = int(account_id)
        # Asked every time rather than cached here: the household cache is
        # keyed on the stored conversions, so it is the one that knows when
        # a conversion written a moment ago has moved the balances.
        household = self.plan_balances() if self.plan_balances else None
        if household is not None and key in household:
            return household[key]
        if key not in self._projected:
            years = self.projection_years()
            values = self.project_account(key, self.account_nets(key, years=years))
            self._projected[key] = {
                year: (values[i] if i < len(values) else 0)
                for i, year in enumerate(years)
            }
        return self._projected[key]

    def cap_cents(self, account_id: int, year: int) -> int:
        """The most this account can pay out in this year: what is left in it.

        An account cannot distribute money it no longer holds, and a plan that
        let it would quietly turn the fund line negative rather than say so.
        With a household plan on file, the plan's own balance
        (``plan_balance_at``), which stays valid while a year's own
        conversion is being tried.
        """
        if self.plan_balance_at is not None:
            held = self.plan_balance_at(int(account_id), int(year))
            if held is not None:
                return max(0, int(held))
        return max(0, int(self.projected_values(account_id).get(int(year), 0)))

    #: Set by the page: (account, year) -> cents entering the year in the
    #: stored household plan, or None (``household_balance_at``).
    plan_balance_at = None

    def invalidate_plan(self) -> None:
        """Forget the cached flows and projections - the stored plan moved."""
        self._flows = {}
        self._projected = {}
        self._contributions = {}

    def retirement_year(self) -> Optional[int]:
        return self._until if self._until is not None else retirement_start_year(self.conn)

    def contributions_in(self, year: int) -> dict[int, int]:
        until = self.retirement_year()
        if until not in self._contributions:
            self._contributions[until] = planned_contributions(self.conn, self._today,
                                                               until)
        return self._contributions[until].get(int(year), {})


class HouseholdEngine(PlanEngine):
    """The household plan: one spending amount, every year, across the pool.

    ``WithdrawalSchedule`` is the editor over this class; see its docstring for
    why the amount is the HOUSEHOLD's and not an account's.
    :meth:`plan_for` computes a plan and writes nothing; applying one is the
    editor's (``apply_household``).
    """

    def _init_engine(self, conn, today: Optional[_dt.date] = None) -> None:
        super()._init_engine(conn, today)
        self._apply_years: list[int] = []
        self.filing_status = default_filing_status(conn)

    # -- what "every year" means --------------------------------------------
    def set_apply_years(self, years: Sequence[int]) -> None:
        """The years the per-year button writes: the LONGEST longevity case.

        Deliberately not the years on screen. A user looking at the plan-through
        age-80 view who sets a withdrawal has still decided something about age
        100, and leaving those years unplanned would make the terminal-age combo
        silently change what the button does.
        """
        self._apply_years = [int(y) for y in years]

    def apply_years(self) -> list[int]:
        return list(self._apply_years or self._years)

    def projection_years(self) -> list[int]:
        """Every year the plan is written for, not only the ones on screen: a
        required minimum is owed in a year whether or not it is drawn."""
        return self.apply_years()

    def accounts(self) -> list[PlanAccount]:
        """The retirement accounts AND the taxable brokerage accounts: the
        household spends from both (``retirement.spending_accounts``)."""
        return list(self._accounts or retirement.spending_accounts(self.conn))

    def _read_accounts(self) -> None:
        self._accounts = retirement.spending_accounts(self.conn)
        self._roths = [a for a in self._accounts if a.is_roth]

    # -- one household amount, every year -----------------------------------
    def growth_factors(self, account_id: int, years: Sequence[int]) -> dict:
        """Plan year -> the factor carrying this account into the next year.

        :func:`account_growth_factors`. These per-account factors only WEIGHT
        the household split; how much the pool can fund each year comes from
        :func:`pool_fund`, the same tracked recursion the fund line draws. When
        the mix cannot be measured the factor is 1: a plan has to exist even
        when a return assumption does not.
        """
        found = account_growth_factors(self.conn, int(account_id), years,
                                       as_of=self._today.isoformat(),
                                       accounts=self.accounts())
        return found or {int(y): Decimal(1) for y in years}

    def household_balances(self) -> Optional[dict]:
        """account -> {year: cents entering the year} in the STORED household
        plan, or None when there is none or it is being computed right now.

        Keyed on what the plan is computed from that changes during a session
        - the conversions and the plan's own parameters - so a fill that writes
        a conversion is answered from the plan that includes it. The page
        clears it on every re-read (``invalidate_household``)."""
        params = retirement.get_withdrawal_plan(self.conn)
        if not params.is_set or getattr(self, "_computing_plan", False):
            return None
        key = (tuple((int(r["year"]), int(r["from_account_id"]), int(r["to_account_id"]),
                      int(r["amount_cents"]))
                     for r in retirement.list_conversions(self.conn)),
               params)
        cached = getattr(self, "_household_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        plan = self.plan_for(params.start_cents, params.increase_pct,
                             start_year=params.start_year,
                             bracket_rate=params.bracket_rate)
        found: dict[int, dict[int, int]] = {}
        for entry in plan.years:
            for account_id, cents in entry.balances.items():
                found.setdefault(int(account_id), {})[int(entry.year)] = max(0, int(cents))
        self._household_cache = (key, found)
        return found

    def household_balance_at(self, account_id: int, year: int) -> Optional[int]:
        """What ``account_id`` holds entering ``year`` in the stored household
        plan, or None without one.

        A year's ENTERING balance depends only on the conversions before it -
        the plan is causal - so a cached plan still answers while a fill tries
        amounts in ``year`` itself. That is what lets the fill solver
        (:meth:`RetirementPlannerPage._solve_fill`) ask for a cap after
        every trial without recomputing the whole plan each time."""
        frozen = getattr(self, "_frozen_balances", None)
        if frozen is not None:
            # A fill round: the balances of the last plan the solver computed,
            # so writing a round's trial conversions costs no plan per year.
            return frozen.get(int(account_id), {}).get(int(year))
        params = retirement.get_withdrawal_plan(self.conn)
        if not params.is_set or getattr(self, "_computing_plan", False):
            return None
        year = int(year)
        cached = getattr(self, "_household_cache", None)
        if cached is not None and cached[0][1] == params:
            earlier = tuple(c for c in cached[0][0] if c[0] < year)
            now = tuple((int(r["year"]), int(r["from_account_id"]),
                         int(r["to_account_id"]), int(r["amount_cents"]))
                        for r in retirement.list_conversions(self.conn)
                        if int(r["year"]) < year)
            if earlier == now:
                return cached[1].get(int(account_id), {}).get(year)
        found = self.household_balances()
        return None if found is None else found.get(int(account_id), {}).get(year)

    def invalidate_household(self) -> None:
        """Forget the household plan's balances: something it reads moved."""
        self._household_cache = None
        self.invalidate_plan()

    def plan_for(self, wanted: int, pct, *, start_year: Optional[int] = None,
                 bracket_rate: Optional[str] = None):
        """The household plan for these settings, computed and NOT written
        (:meth:`_plan_for`). While it runs, :meth:`household_balances` answers
        None: the plan's own inputs - its growth factors and required
        minimums - are projected account by account, and asking the plan
        being computed for its balances would never return."""
        was = getattr(self, "_computing_plan", False)
        self._computing_plan = True
        try:
            return self._plan_for(wanted, pct, start_year=start_year,
                                  bracket_rate=bracket_rate)
        finally:
            self._computing_plan = was

    def _plan_for(self, wanted: int, pct, *, start_year: Optional[int] = None,
                  bracket_rate: Optional[str] = None):
        """The household plan for these settings, computed and NOT written.

        :meth:`apply_household` writes it; :meth:`most_we_can_spend` asks it
        repeatedly while searching.

        Giving is deliberately not modeled here: a tithe or any other gift is
        part of the spending amount the household enters, and taxable income
        takes the standard deduction. A planner that projected itemized
        deductions, bunching and QCDs thirty years out claimed a precision no
        household plans with (decided after building it).
        """
        years = self.apply_years()
        accounts = self.accounts()
        balances = {a.account_id: max(0, self.balance_cents(a.account_id))
                    for a in accounts}
        factors = {a.account_id: self.growth_factors(a.account_id, years)
                   for a in accounts}
        # The plan being asked about retires in ITS start year: contributions
        # stop and a current employer's plan owes minimums from then on.
        self._until = int(start_year) if start_year is not None else None
        # Re-read the plan: a conversion written through the OTHER editor (the
        # conversion schedule) is not in this editor's cached flows.
        self.invalidate_plan()
        until = self.retirement_year()
        employer_ends = retirement.employer_plan_ends(self.conn, until)
        covered, room = self.household_income(years, accounts, bracket_rate)
        by_id = {a.account_id: a for a in accounts}
        scenario = retirement.get_survivor_scenario(self.conn)
        births = {a.account_id: retirement.account_birth(self.conn, a.account_id)
                  for a in accounts if a.treatment in ("deferred", "roth")}
        # Early distributions (IRC 72(t)): an owner under 59 1/2 pays 10% more,
        # except out of a current employer's plan left in or after the year
        # they turn 55 - left in the retirement year here. Reported: the plan
        # spent IRAs before 59 1/2 at a rate ten points too low. A Roth is
        # named too: its young conversions and earnings pay it (408A(d)(3)(F)).
        early_last: dict[int, int] = {}
        for acct in accounts:
            if acct.treatment not in ("deferred", "roth"):
                continue
            born, born_month = births.get(acct.account_id, (None, None))
            if born is None:
                continue
            last = retirement.early_distribution_last_year(born, born_month)
            ends = employer_ends.get(acct.account_id) if acct.current_employer_plan else None
            if ends is not None and int(ends) - born >= retirement.SEPARATION_FROM_SERVICE_AGE:
                last = min(last, int(ends) - 1)
            early_last[acct.account_id] = last
        # Rolled over to the survivor: the survivor's age sets the minimum.
        widowed_births = ({a.account_id: retirement.account_birth(
                              self.conn, a.account_id, scenario.death_year + 1)
                           for a in accounts if a.treatment == "deferred"}
                          if scenario is not None else births)
        spouses = {a.account_id: retirement.spouse_birth_year(self.conn, a.owner_person_id)
                   for a in accounts if a.treatment == "deferred"}
        defer_first = retirement.get_defer_first_rmd(self.conn)
        entered: dict[int, dict[int, int]] = {}

        def rmd_on(account_id, year, balance):
            # Every year's entering balance is remembered: the April 1
            # deferral takes the first year's minimum from the year before's.
            entered.setdefault(int(account_id), {})[int(year)] = max(0, int(balance))
            acct = by_id.get(int(account_id))
            if acct is None or not acct.floored_in(year, employer_ends):
                return 0
            widowed = scenario is not None and scenario.widowed(year)
            chosen = widowed_births if widowed else births
            birth_year, birth_month = chosen.get(int(account_id), (None, None))
            if birth_year is None:
                return 0
            if acct.inherited_death_year is not None:
                return retirement.inherited_minimum_cents(
                    max(0, int(balance)), birth_year, acct.inherited_death_year,
                    acct.inherited_after_rbd, int(year))
            return retirement.owner_minimum_cents(
                max(0, int(balance)), entered[int(account_id)].get(int(year) - 1),
                birth_year, birth_month, int(year),
                first_year=acct.first_floored_year(birth_year, employer_ends),
                spouse_birth_year=None if widowed else spouses.get(int(account_id)),
                defer_first=defer_first)

        def converted_in(account_id, year):
            flow = self.flows_for(int(year)).get(int(account_id))
            return int(flow.conversion_in_cents) if flow is not None else 0

        exempt = retirement.spending_exempt_ids(self.conn)
        self.last_plan = retirement.plan_household_withdrawals(
            years, accounts, balances,
            start_cents=wanted, increase_pct=pct,
            floor_cents=self.rmd_floor_cents,
            growth=lambda aid, yr: factors[int(aid)].get(int(yr), Decimal(1)),
            other_net_cents=self.plan_other_net_cents,
            pool=pool_fund(self.conn, [a for a in accounts
                                       if a.account_id not in exempt],
                           as_of=self._today.isoformat()),
            start_year=start_year,
            covered_cents=lambda yr: covered.get(int(yr), 0),
            deferred_room_cents=room,
            rmd_cents=rmd_on,
            employer_until=employer_ends,
            tax_cents=self.tax_owed(years, accounts, start_year),
            spending_order=retirement.get_spending_order(self.conn),
            exempt_ids=exempt,
            surcharge_cents=self.irmaa_owed(years, accounts, start_year),
            basis_cents=taxable_basis_cents(
                self.conn, [a.account_id for a in accounts if a.is_taxable
                            and not a.planned], self._today.isoformat()),
            tax_paid_from=retirement.get_tax_paid_from(self.conn),
            early_ids=lambda yr: {aid for aid, last in early_last.items()
                                  if int(yr) <= last},
            conversions_in=converted_in,
            # The step-up at a death (IRC 1014): half of joint property, all
            # of community property.
            basis_step_up=(None if scenario is None else (
                scenario.death_year + 1,
                Decimal(1) if retirement.get_community_property(self.conn)
                else Decimal("0.5"))),
            spending_scale=(None if scenario is None else
                            (lambda yr: (scenario.spending_pct / 100
                                         if scenario.widowed(yr) else 1))),
        )
        return self.last_plan

    def tax_owed(self, years: Sequence[int], accounts, start_year: Optional[int]):
        """``(year, deferred_draw_cents) -> cents``: the federal income tax the
        accounts pay that year, for :func:`retirement.plan_household_withdrawals`.

        From the plan's start year, the whole year's tax - the spending amount
        is what the household LIVES on, after tax. Before it, only what the
        year's conversion adds: the paycheck pays the tax on the paycheck, but
        the conversion's tax comes out of savings. Reported: the planner
        computed the tax and never paid it, so every plan looked richer than it
        was and the Roth bigger than it would be."""
        people = planning_people(self.conn)
        sources = retirement.plan_income_sources(self.conn)
        pct = retirement.get_bracket_index_pct(self.conn)
        state_pct = retirement.get_state_tax_pct(self.conn)
        state_ss = retirement.get_state_taxes_ss(self.conn)
        deferred = {a.account_id for a in accounts if a.treatment == "deferred"}
        first = (int(start_year) if start_year is not None
                 else (int(years[0]) if years else None))
        excludes = retirement.get_state_excludes_retirement(self.conn)
        ratios = basis_ratio_by_year(self.conn, self, years)
        parts: dict[int, tuple] = {}
        for year in years:
            y = int(year)
            status_y = year_status(self.conn, self.filing_status, y)
            seniors_y = deduction_conditions(self.conn, y, status_y)
            parts[y] = (
                social_security_cents(self.conn, y, people, base_year=self._today.year),
                other_taxable_cents(self.conn, y, 0, sources),
                retirement.standard_deduction_in(status_y, seniors_y, y, pct),
                sum(int(f.conversion_out_cents) for aid, f in self.flows_for(y).items()
                    if aid in deferred),
                status_y,
                investment_income_cents(self.conn, y, sources),
                seniors_y,
                tax_exempt_cents(self.conn, y, sources),
                preferential_cents(self.conn, y, sources),
                pension_cents(self.conn, y, sources) if excludes else 0,
                ratios.get(y, Decimal(0)),
            )

        def tax(year: int, deferred_draws: int, gains: int = 0) -> int:
            found = parts.get(int(year))
            if found is None:
                return 0
            (benefit, other, deduction, converted, status, invest, seniors,
             exempt, pref, pensions, ratio) = found

            def on(extra: int, realized: int) -> int:
                # The after-tax basis comes back untaxed, pro rata (Form 8606).
                out_of_iras = int(deferred_draws) + extra
                taxable_iras = out_of_iras - int((Decimal(out_of_iras) * ratio)
                                                 .quantize(Decimal(1), rounding=ROUND_HALF_UP))
                net_gains = int(realized) + pref
                ordinary = other + taxable_iras + retirement.loss_offset_cents(net_gains)
                taxed = retirement.taxable_social_security_cents(
                    benefit, ordinary + max(0, net_gains) + exempt, status)
                return retirement.year_tax(
                    ordinary + taxed, net_gains, deduction, status, int(year), pct,
                    invest, state_pct=state_pct, social_security_taxed_cents=taxed,
                    state_taxes_ss=state_ss, seniors=seniors,
                    state_excluded_cents=(taxable_iras + pensions) if excludes else 0).total

            if first is not None and int(year) < first:
                # The paycheck pays the tax on the paycheck; the conversion's
                # tax, and the tax on the shares sold to pay it, come out of
                # savings (an audit found the sale's gains netted away).
                return max(0, on(converted, gains) - on(0, 0))
            return on(converted, gains)

        return tax

    def irmaa_owed(self, years: Sequence[int], accounts, start_year: Optional[int]):
        """``(year, earlier_years) -> cents``: the Medicare surcharge the
        accounts pay in ``year``, set by the plan's own income two years
        earlier. Paid from the plan's start year; before it the paycheck
        pays. No year two back in the plan (the first two years) is none."""
        people = planning_people(self.conn)
        sources = retirement.plan_income_sources(self.conn)
        deferred = [a.account_id for a in accounts if a.treatment == "deferred"]
        first = (int(start_year) if start_year is not None
                 else (int(years[0]) if years else None))
        horizon_start = int(years[0]) if years else None
        ratios = basis_ratio_by_year(self.conn, self, years)

        appealed = appealed_premium_years(self.conn)

        def taxable_out_of_iras(year: int, cents: int) -> int:
            ratio = ratios.get(int(year), Decimal(0))
            return int(cents) - int((Decimal(int(cents)) * ratio)
                                    .quantize(Decimal(1), rounding=ROUND_HALF_UP))

        def from_income(year: int, income_year: int, deferred_cents: int,
                        gains: int = 0) -> int:
            converted = sum(int(f.conversion_out_cents)
                            for aid, f in self.flows_for(income_year).items()
                            if aid in deferred)
            other = (other_taxable_cents(self.conn, income_year, 0, sources)
                     + taxable_out_of_iras(income_year, int(deferred_cents) + converted))
            benefit = social_security_cents(self.conn, income_year, people,
                                            base_year=self._today.year)
            status = year_status(self.conn, self.filing_status, income_year)
            exempt = tax_exempt_cents(self.conn, income_year, sources)
            net_gains = int(gains) + preferential_cents(self.conn, income_year, sources)
            # Gains are not ordinary income, but they are in MAGI - and so is
            # tax-exempt interest (42 USC 1395r(i)(4)).
            magi = (retirement.gross_with_social_security_cents(
                other, benefit, status, net_gains, exempt) + max(0, net_gains) + exempt)
            return household_irmaa(
                self.conn, int(year), magi, status, people,
                year_status(self.conn, self.filing_status, year))[0]

        def owed(year: int, earlier, deferred_now: int = 0, gains_now: int = 0) -> int:
            if first is not None and int(year) < first:
                return 0
            back = next((e for e in reversed(list(earlier))
                         if e.year == int(year) - 2), None)
            found = []
            if back is not None:
                found.append(from_income(year, back.year,
                                         sum(int(back.amounts.get(a, 0))
                                             for a in deferred),
                                         int(getattr(back, "gains_cents", 0))))
            elif horizon_start is not None and int(year) - 2 < horizon_start:
                # Before the plan's first year: the return the household
                # filed, typed in the Social Security dialog (reported: the
                # first two years charged nothing whatever was earned).
                entered = retirement.get_prior_magi(self.conn, int(year) - 2)
                if entered is not None:
                    found.append(household_irmaa(
                        self.conn, int(year), entered,
                        year_status(self.conn, self.filing_status, int(year) - 2),
                        people, year_status(self.conn, self.filing_status, int(year)))[0])
            if int(year) in appealed:
                # The appeal: this year's own income, filed only when lower.
                found.append(from_income(year, int(year), deferred_now, gains_now))
            return (min(found) if found else 0) + aca_now(year, deferred_now, gains_now)

        def aca_now(year: int, deferred_cents: int, gains: int) -> int:
            """The ACA credit lost this year, on this year's own income."""
            status = year_status(self.conn, self.filing_status, year)
            if not aca_marketplace_months(self.conn, year, status, people):
                return 0
            converted = sum(int(f.conversion_out_cents)
                            for aid, f in self.flows_for(int(year)).items()
                            if aid in deferred)
            base = (other_taxable_cents(self.conn, int(year), 0, sources)
                    + social_security_cents(self.conn, int(year), people,
                                            base_year=self._today.year)
                    + tax_exempt_cents(self.conn, int(year), sources))
            net_gains = int(gains) + preferential_cents(self.conn, int(year), sources)
            magi = (base + taxable_out_of_iras(year, int(deferred_cents) + converted)
                    + max(0, net_gains) + retirement.loss_offset_cents(net_gains))
            return aca_cost(self.conn, int(year), magi, status, people,
                            baseline_magi_cents=base)

        return owed

    def most_we_can_spend(self, increase_pct=0, *, start_year: Optional[int] = None,
                          bracket_rate: Optional[str] = None,
                          reserve_cents: int = 0,
                          step_cents: int = 1_000_00) -> int:
        """The largest starting spending amount that lasts through the case on
        screen AND still leaves ``reserve_cents`` in the retirement accounts at
        its end, to the nearest ``step_cents``. Nothing is written.

        A search over :meth:`plan_for`, the same plan Apply writes. The reserve
        is the pool's median at the end of the last year shown, in that year's
        (nominal) dollars - a reserve to leave at the end (reported).
        """
        years = self.apply_years()
        if not years or not self.accounts():
            return 0
        must_reach = self._years[-1] if self._years else years[-1]

        def lasts(amount: int) -> bool:
            plan = self.plan_for(amount, increase_pct, start_year=start_year,
                                 bracket_rate=bracket_rate)
            if not (plan.lasts or plan.depleted_year > must_reach):
                return False
            return reserve_cents <= 0 or \
                self.ending_balance_cents(plan, must_reach) >= reserve_cents

        low, high = 0, 50_000_00
        while lasts(high) and high < 100_000_000_00:
            low, high = high, high * 2
        while high - low > step_cents:
            mid = (low + high) // 2
            if lasts(mid):
                low = mid
            else:
                high = mid
        return low - low % step_cents

    @staticmethod
    def ending_balance_cents(plan, last_year: int) -> int:
        """What the plan leaves at the END of ``last_year``: the pool entering
        the next year, or - past the plan's own years - the last year's pool
        less what it drew."""
        entries = {e.year: e for e in plan.years}
        if last_year + 1 in entries:
            return int(entries[last_year + 1].pool_cents)
        last = entries.get(last_year)
        if last is None:
            return 0
        return max(0, int(last.pool_cents) - int(last.drawn_cents))

    def household_income(self, years: Sequence[int], accounts, bracket_rate):
        """Per year: the income already paying part of the need, and - with a
        bracket target - how much the tax-deferred accounts may draw.

        The room is what an IRA draw can add before the next bracket starts:
        the bracket's top plus the standard deduction, less taxable other
        income and the Social Security that income - and the draw itself -
        makes taxable (IRC 86). The year's conversions are not subtracted."""
        people = planning_people(self.conn)
        sources = retirement.plan_income_sources(self.conn)
        index_pct = retirement.get_bracket_index_pct(self.conn)
        status = self.filing_status
        has_top = bool(bracket_rate) and retirement.bracket_top_cents(
            bracket_rate, status) is not None
        covered: dict[int, int] = {}
        parts: dict[int, tuple] = {}
        ratios = basis_ratio_by_year(self.conn, self, years) if has_top else {}
        for year in years:
            benefit = social_security_cents(self.conn, int(year), people,
                                            base_year=self._today.year)
            # A deferral is withheld from the paycheck: it pays nothing toward
            # the household's spending, pre-tax or Roth - as much as the law
            # lets the plan take (capped).
            other = sum(src.cents_in(year) - sum(salary_contributions(
                self.conn, src, year, people)[:2]) for src in sources)
            covered[int(year)] = benefit + other
            if has_top:
                status = year_status(self.conn, self.filing_status, int(year))
                # Both indexed to the year: the tables move with inflation.
                top = retirement.bracket_top_cents(bracket_rate, status,
                                                   year=int(year),
                                                   index_pct=index_pct)
                seniors = deduction_conditions(self.conn, int(year), status)
                deduction = retirement.standard_deduction_in(
                    status, seniors, int(year), index_pct)
                parts[int(year)] = (
                    top, other_taxable_cents(self.conn, int(year), 0, sources),
                    benefit, status, deduction, seniors,
                    tax_exempt_cents(self.conn, int(year), sources),
                    preferential_cents(self.conn, int(year), sources),
                    ratios.get(int(year), Decimal(0)))
        if not has_top:
            return covered, None

        def room(year: int, gains: int = 0) -> int:
            # NOT less the year's conversions: spending comes first, and a
            # conversion stacks on top of the draws. Subtracting it let a
            # conversion filled to a higher bracket squeeze the draws, the
            # Roth paid the difference, and the no-Roth-draw-while-
            # converting rule then shrank the conversion - each re-apply
            # again, until fills past 2032 vanished (reported).
            # Found by bisection: an IRA dollar can also make up to 85
            # cents of Social Security taxable (IRC 86), and so can the
            # year's gains (the engine hands them in). A draw that is partly
            # after-tax basis is that much bigger for the same taxable income.
            found = parts.get(int(year))
            if found is None:
                return 0
            top, other, benefit, status, deduction, seniors, exempt, pref, ratio = found
            taxable = retirement.taxable_room_cents(
                top, other, benefit, status, int(gains) + pref, deduction,
                seniors=seniors, year=int(year), tax_exempt_cents=exempt)
            if ratio >= 1:
                return taxable
            return int((Decimal(taxable) / (Decimal(1) - ratio))
                       .quantize(Decimal(1), rounding=ROUND_HALF_UP))

        return covered, room

    def plan_other_net_cents(self, account_id: int, year: int) -> int:
        """Everything the plan does to this account besides the withdrawal.

        In practice the Roth conversions: money leaving a deferred account and
        arriving in a Roth changes what each one has to fund next year, and a
        split blind to it would drain the Roth it had just filled.
        """
        flow = self.flows_for(int(year)).get(int(account_id))
        if flow is None:
            return 0
        return (int(flow.conversion_in_cents) + int(flow.contribution_cents)
                - int(flow.conversion_out_cents))


class HouseholdPlanner(HouseholdEngine):
    """A household plan with no page: the engine, configured as the page
    configures its withdrawal schedule.

    The years on screen are the stored terminal-age case
    (``retirement.get_plan_through_age``) unless ``terminal_age`` names
    another; the plan is computed through age 100 or that case, whichever is
    later (:data:`PLAN_WRITTEN_THROUGH_AGE`), because a required minimum is
    owed whether or not a year is on screen. Every cap and minimum reads the
    household plan's own balances, as the page wires them. The filing status
    is the page's starting view, ``default_filing_status``, unless given:
    Mammon does not know how anybody files and does not store a guess.
    """

    def __init__(self, conn, *, today: Optional[_dt.date] = None,
                 terminal_age: Optional[int] = None,
                 filing_status: Optional[str] = None):
        self._init_engine(conn, today)
        if filing_status:
            self.filing_status = filing_status
        self.terminal_age = int(terminal_age if terminal_age is not None
                                else retirement.get_plan_through_age(conn))
        self._years = plan_horizon(conn, self.terminal_age, self._today)
        self._apply_years = plan_horizon(
            conn, max(PLAN_WRITTEN_THROUGH_AGE, self.terminal_age), self._today)
        self.plan_balances = self.household_balances
        self.plan_balance_at = self.household_balance_at


# ---------------------------------------------------------------------------
# each year's income tax, as the page states it
# ---------------------------------------------------------------------------
class PlanTaxes:
    """Each plan year's WHOLE income tax - federal, state and the 10% on early
    distributions - as the Retirement Planner page states it: the Roth
    section's bars, and its figures row's "Federal tax" and "State tax"
    (totaled in today's dollars by :meth:`tax_totals`).

    Not the household plan's ``HouseholdYear.tax_cents``, which is the tax the
    ACCOUNTS pay: before the plan's start year that is only what the year's
    conversion adds, because the paycheck pays the tax on the paycheck. This
    is the whole year's, every year, so the two agree from the start year on
    and differ by the working years' paycheck tax before it. Both are true;
    they answer different questions, and a reader handed one under the
    other's name is told something the screen contradicts (reported: the MCP
    surface's total disagreed with the page's).

    Built from :func:`income_rows` (the bars), the household plan when one is
    stored (its realized gains and its early-distribution penalties) and the
    filing status on screen. Reads only: the page stores :attr:`seeded` as
    the year's taxable income itself. ``editor`` is the engine whose
    projections the after-tax basis ratio reads (:func:`basis_ratio_by_year`).
    """

    def __init__(self, conn, rows: Sequence[IncomeYear], plan, filing_status: str,
                 editor, *, base: Optional[Mapping[int, int]] = None):
        self.conn = conn
        self.rows = list(rows)
        self.plan = plan
        self.filing_status = filing_status
        self.base = (retirement.taxable_income_map(conn) if base is None
                     else dict(base))
        sources = retirement.plan_income_sources(conn)
        index_pct = retirement.get_bracket_index_pct(conn)
        self.parts: dict[int, tuple] = {}
        self.seniors: dict[int, int] = {}
        self.other_base: dict[int, int] = {}
        self.exempt: dict[int, int] = {}
        self.pref: dict[int, int] = {}
        self.pensions: dict[int, int] = {}
        #: Year -> the taxable income the page seeds: the plan's OWN flows -
        #: the taxable share of Social Security, taxable other income and the
        #: year's tax-deferred withdrawals - LESS the standard deduction,
        #: without the year's conversion (stacked on top at render time).
        self.seeded: dict[int, int] = {}
        self.ratio = basis_ratio_by_year(conn, editor, [r.year for r in self.rows])
        self.invest = {r.year: investment_income_cents(conn, r.year, sources)
                       for r in self.rows}
        for r in self.rows:
            status = self.status_in(r.year)
            other = other_taxable_cents(
                conn, r.year, self.taxable_share(r.year, r.deferred_draw_cents), sources)
            seniors = deduction_conditions(conn, r.year, status)
            deduction = retirement.standard_deduction_in(status, seniors, r.year,
                                                         index_pct)
            self.parts[r.year] = (r.social_security_cents, other, deduction)
            self.seniors[r.year] = seniors
            self.other_base[r.year] = other_taxable_cents(conn, r.year, 0, sources)
            self.exempt[r.year] = tax_exempt_cents(conn, r.year, sources)
            self.pref[r.year] = preferential_cents(conn, r.year, sources)
            self.pensions[r.year] = pension_cents(conn, r.year, sources)
            # Less the standard deduction and, 2025-2028, the senior deduction.
            self.seeded[r.year] = retirement.taxable_ordinary_cents(
                other, r.social_security_cents, status, self.gains_in(r.year),
                deduction, seniors=seniors, year=r.year,
                tax_exempt_cents=self.exempt[r.year])

    def status_in(self, year: int) -> str:
        """The filing status in ``year``: single for a survivor."""
        return year_status(self.conn, self.filing_status, int(year))

    def seniors_in(self, year: int) -> int:
        """People 65 or older on ``year``'s return (the senior deduction)."""
        return self.seniors.get(int(year), 0)

    def _entry(self, year: int):
        if self.plan is None:
            return None
        return next((e for e in self.plan.years if e.year == int(year)), None)

    def penalty_in(self, year: int) -> int:
        """The plan's 10% additional tax on early distributions in ``year``
        (IRC 72(t)); nothing without a plan."""
        entry = self._entry(year)
        return int(getattr(entry, "penalty_cents", 0)) if entry is not None else 0

    def gains_in(self, year: int) -> int:
        """The year's income at the capital-gains rates: the net gain the plan
        realizes selling taxable accounts (negative for a net loss, up to the
        $3,000 that comes off ordinary income; nothing without a plan) plus
        the Income dialog's qualified dividends and fund gains."""
        entry = self._entry(year)
        realized = int(getattr(entry, "gains_cents", 0)) if entry is not None else 0
        return realized + self.pref.get(int(year), 0)

    def exempt_in(self, year: int) -> int:
        """Tax-exempt interest in ``year`` (in provisional income and MAGI)."""
        return self.exempt.get(int(year), 0)

    def taxable_share(self, year: int, out_of_iras: int) -> int:
        """What is taxable of ``out_of_iras`` drawn or converted in ``year``:
        less the pro-rata return of after-tax basis (Form 8606)."""
        ratio = self.ratio.get(int(year), Decimal(0))
        cents = int(out_of_iras)
        return cents - int((Decimal(cents) * ratio).quantize(Decimal(1),
                                                            rounding=ROUND_HALF_UP))

    def taxable_with(self, year: int, conversion_cents: int) -> int:
        """Taxable income in ``year`` with ``conversion_cents`` converted: the
        conversion is ordinary income AND raises provisional income, so it can
        make more Social Security taxable than the base year did (IRC 86)."""
        parts = self.parts.get(int(year))
        if parts is None:
            return int(self.base.get(int(year), 0)) + int(conversion_cents)
        benefit, other, deduction = parts
        return retirement.taxable_ordinary_cents(
            other + self.taxable_share(year, conversion_cents), benefit,
            self.status_in(year), self.gains_in(year), deduction,
            seniors=self.seniors_in(year), year=int(year),
            tax_exempt_cents=self.exempt_in(year))

    def tax_in(self, year: int, conversion_cents: int):
        """``year``'s federal tax (ordinary, gains, NIIT) with this conversion."""
        parts = self.parts.get(int(year))
        pct = retirement.get_bracket_index_pct(self.conn)
        status = self.status_in(year)
        if parts is None:
            return retirement.YearTax(retirement.federal_tax_cents(
                self.taxable_with(year, conversion_cents), status, int(year), pct), 0, 0)
        benefit, other, deduction = parts
        gains = self.gains_in(year)
        exempt = self.exempt_in(year)
        converted = self.taxable_share(year, conversion_cents)
        ordinary = other + converted + retirement.loss_offset_cents(gains)
        taxed = retirement.taxable_social_security_cents(
            benefit, ordinary + max(0, gains) + exempt, status)
        row = next((r for r in self.rows if r.year == int(year)), None)
        excluded = 0
        if retirement.get_state_excludes_retirement(self.conn):
            drawn = self.taxable_share(year, row.deferred_draw_cents if row else 0)
            excluded = drawn + converted + self.pensions.get(int(year), 0)
        return retirement.year_tax(
            ordinary + taxed, gains, deduction, status, int(year), pct,
            self.invest.get(int(year), 0),
            state_pct=retirement.get_state_tax_pct(self.conn),
            social_security_taxed_cents=taxed,
            state_taxes_ss=retirement.get_state_taxes_ss(self.conn),
            seniors=self.seniors_in(year), state_excluded_cents=excluded)

    def tax_by_year(self) -> dict[int, int]:
        """Estimated income tax per year: that year's taxable income - the
        base plus its conversion - through the indexed brackets, plus the
        plan's additional tax on early distributions."""
        return {r.year: self.tax_in(r.year, r.conversion_cents).total
                + self.penalty_in(r.year) for r in self.rows}

    def tax_totals(self, today_year: int) -> tuple[int, int]:
        """(total, total in today's dollars): each year's tax deflated at the
        bracket-indexing rate, so plans that move tax earlier or later can be
        compared - a dollar of tax in 2050 is not a dollar today."""
        pct = Decimal(str(retirement.get_bracket_index_pct(self.conn)))
        total, today = 0, Decimal(0)
        for year, cents in self.tax_by_year().items():
            total += cents
            factor = (1 + pct / 100) ** max(0, year - int(today_year))
            today += Decimal(cents) / factor
        return total, int(today.quantize(Decimal(1), rounding=ROUND_HALF_UP))
