"""Retirement planning: the published rule tables, and the household's plan.

A pure-domain module (NO Qt, no UI, no printing). It owns two things that are
easy to confuse:

1. **Rule tables** - numbers Congress, the IRS, SSA, HHS and CMS publish, which
   Mammon does not get to choose. Each one carries a :class:`Provenance` record
   naming the table, the year it is effective for, who published it and where,
   and how fast it moves. That record exists because the failure mode of a
   planner is not a wrong formula, it is a right formula run on last year's
   thresholds: an IRMAA bracket is a cliff, and a figure computed against a
   stale bracket is wrong by a whole tier with nothing on screen to say so.
   A figure inherits the WORST staleness of every table it touched
   (:func:`worst_provenance`), so a projection can show one honest date rather
   than five.

   Only the tables this task needs live here. The longer-term shape (design
   note section 4.1) is a ``mammon/rule_defs/`` directory of YAML, one file per
   table, loaded by a ``mammon/rules.py``; the :class:`Provenance` record is
   deliberately the seam for that move - a loader would produce these same
   objects and nothing calling this module would change.

2. **The plan** - what the household intends to withdraw and convert, per
   account, per year. These are intentions about a future year, which is
   exactly why they are not transactions: ``mammon.ledger`` stays the only
   writer of transaction rows, and nothing here ever writes one. A planned
   withdrawal becomes a real transaction only when the user actually takes it,
   through the register like anything else.

Conventions, the same as everywhere else in Mammon:

* Money is signed INTEGER cents. No floats, anywhere, ever. Ratios and
  divisors are :class:`~decimal.Decimal`, and money rounds ``ROUND_HALF_UP``
  at the cents boundary.
* Stored plan amounts are MAGNITUDES (non-negative): a row in
  ``retirement_withdrawals`` says "draw $40,000 out", and the direction is
  carried by the table, not by a sign the user would have to remember to type.
  The SIGNED view - negative for money leaving an account, positive for money
  arriving - is produced by :class:`AccountFlow`, which is the form
  ``mammon/forecast.py`` consumes. Storing the sign as well would give two
  places to disagree about it.
* Years are plain integers (a distribution calendar year), not dates. Birth
  dates are a month and a year, because that is all any of these rules use;
  see the ``people`` table comment in ``db.py`` for why, and for the one
  common-law edge case that ``born_on_the_first`` covers.

Health columns on ``people`` are optional and blank by default. They are named
in :data:`HEALTH_COLUMNS`, and ``mammon/mcp_tools.py`` refuses every one of
them at the MCP boundary - not as policy but structurally, so a connected LLM
cannot read them even if it asks by name.
"""

# ---------------------------------------------------------------------------
# ANNUAL UPDATE CHECKLIST - the page to open once a year
# ---------------------------------------------------------------------------
#
# Every figure in Mammon that changes with law or annual indexing lives in THIS
# file and nowhere else. That is mechanically enforced:
# ``test_retirement.py::test_no_indexed_figure_lives_outside_this_file`` fails
# if one is re-typed into another module, so the list below stays the whole job
# rather than the part someone remembered.
#
# Updating a table means THREE edits, not one: the numbers, the Provenance
# ``effective_year``, and its ``checked_on``. A table whose year is behind and
# whose ``republished_by`` date has passed announces itself as stale on screen
# (:meth:`Provenance.is_stale`); one whose year was bumped without its numbers
# lies quietly. Work top to bottom.
#
# INDEXED - a new figure to transcribe every year:
#
#   TAX_BRACKETS              2026  IRS Rev. Proc., published ~mid-October
#                                   (2026 = Rev. Proc. 2025-32)
#                                   https://www.irs.gov/pub/irs-drop/
#   STANDARD_DEDUCTION        2026  the SAME Rev. Proc. as TAX_BRACKETS, incl.
#                                   the IRC 63(f) age-65/blind add-ons
#   CONTRIBUTION_LIMITS       2026  IRS Notice, published ~early November
#                                   (2026 = Notice 2025-67)
#                                   https://www.irs.gov/pub/irs-drop/
#   SS_BEND_POINTS            2026  SSA, with the October COLA announcement
#                                   https://www.ssa.gov/oact/COLA/bendpoints.html
#   SS_WAGE_BASE              2026  SSA contribution and benefit base, October
#                                   https://www.ssa.gov/oact/cola/cbb.html
#   AWI_SERIES                2024  SSA average wage index, ~mid-October, and it
#                                   lags TWO years behind the calendar by design
#                                   https://www.ssa.gov/oact/cola/AWI.html
#   FEDERAL_POVERTY_LEVEL     2026  HHS ASPE guidelines, published each January
#                                   https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines
#   IRMAA_TIERS               2026  CMS annual Medicare premium fact sheet,
#   PART_B_STANDARD_CENTS     2026  published each November
#                                   https://www.cms.gov/newsroom/fact-sheets
#
# PROJECTED, not published - re-check when the assumption moves:
#
#   SS_PROJECTED_WAGE_GROWTH  2026  SSA Trustees Report long-range economic
#                                   assumptions, published each spring
#                                   https://www.ssa.gov/oact/TR/
#   ACA_CLIFF_MULTIPLE        2026  the 4x multiplier is statute (IRC 36B), but
#                                   Congress suspended the cliff for 2021-2025
#                                   and let the suspension lapse; re-check
#                                   whether it is in force before each open
#                                   enrollment
#
# STATUTORY - no annual update; revisit only when Congress or Treasury amends:
#
#   RMD_APPLICABLE_AGE        2023  IRC 401(a)(9)(C)(v), SECURE 2.0 sec. 107
#   UNIFORM_LIFETIME_TABLE    2022  26 CFR 1.401(a)(9)-9(c), reprinted as IRS
#                                   Pub 590-B Appendix B Table III
#                                   https://www.irs.gov/publications/p590b
#   SS_PIA_FACTORS            1979  42 U.S.C. 415(a)(1)(A) - the 90/32/15 factors
#   SS_FULL_RETIREMENT_AGE    1983  42 U.S.C. 416(l)
#   SS_DELAYED_PER_MONTH      1983  42 U.S.C. 402(q), 402(w)
#
# ---------------------------------------------------------------------------

from __future__ import annotations

import datetime as _dt
import inspect
from dataclasses import dataclass, replace
from fractions import Fraction
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Callable, Iterable, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

#: How fast a rule table moves, which decides when a stale copy is a bug.
#:
#: ``stable``   - fixed by statute or regulation until amended. The 90/32/15
#:                PIA factors have not moved since 1979. Never warns.
#: ``indexed``  - republished on a schedule, usually annually, by a formula.
#:                Bend points, FPL, IRMAA thresholds. Warns once its year is
#:                behind and the replacement is due.
#: ``volatile`` - changes without a schedule. Carries a checked-on date.
VOLATILITIES = ("stable", "indexed", "volatile")


@dataclass(frozen=True)
class Provenance:
    """Where one rule table came from and how long it can be trusted.

    ``republished_by`` is an ISO ``MM-DD``: the point in the following year by
    which the issuer normally has the replacement out. Before that date a
    one-year-old ``indexed`` table is simply the current one, so warning about
    it would train the user to ignore the warning.
    """

    table: str
    effective_year: int
    volatility: str
    publisher: str
    source: str
    republished_by: str = "01-01"
    checked_on: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.volatility not in VOLATILITIES:
            raise ValueError(f"unknown volatility {self.volatility!r}")

    def is_stale(self, today: Optional[_dt.date] = None) -> bool:
        """True when this table is old enough that a figure using it should say so."""
        if self.volatility == "stable":
            return False
        today = today or _dt.date.today()
        behind = today.year - self.effective_year
        if behind <= 0:
            return False
        if behind > 1:
            return True
        return today.isoformat()[5:] >= self.republished_by

    def describe(self) -> str:
        """One line a UI can print under a figure without further formatting."""
        text = f"{self.table} ({self.publisher}, {self.effective_year})"
        if self.note:
            text += f" - {self.note}"
        return text


def worst_provenance(
    records: Iterable[Provenance], today: Optional[_dt.date] = None
) -> Optional[Provenance]:
    """The record a computed figure should cite: stalest first, then oldest.

    A number derived from five tables is only as current as the worst of them,
    and showing five dates makes the reader do the comparison we already can.
    """
    items = list(records)
    if not items:
        return None
    return min(items, key=lambda p: (not p.is_stale(today), p.effective_year))


@dataclass(frozen=True)
class RuleTable:
    """A published table plus the record of where it came from."""

    provenance: Provenance
    rows: Mapping

    def __getitem__(self, key):
        return self.rows[key]

    def __contains__(self, key) -> bool:
        return key in self.rows


# ---------------------------------------------------------------------------
# Rule table: the RMD applicable age (SECURE 2.0)
# ---------------------------------------------------------------------------

_RMD_AGE_PROV = Provenance(
    table="RMD applicable age",
    effective_year=2023,
    volatility="stable",
    publisher="IRC 401(a)(9)(C)(v), as amended by SECURE 2.0 Act sec. 107",
    source="https://www.law.cornell.edu/uscode/text/26/401",
    note=(
        "SECURE 2.0's two amendments overlap for the 1959 cohort, which the "
        "statute as enacted makes both 73 and 75. The IRS proposed regulations "
        "of July 2024 (REG-103529-23) resolve it as 73, which is what this "
        "table encodes; a figure for a 1959 birth year should say so."
    ),
)

#: Birth year (inclusive lower bound) -> the age at which RMDs begin.
#: 1950 and earlier is SECURE 1.0's 72 and is here only so the function is
#: total; those cohorts have been in pay status for years.
_RMD_AGE_ROWS: dict[int, int] = {0: 72, 1951: 73, 1960: 75}

RMD_APPLICABLE_AGE = RuleTable(provenance=_RMD_AGE_PROV, rows=_RMD_AGE_ROWS)


def applicable_age(birth_year: int) -> int:
    """The age at which required minimum distributions begin for this cohort."""
    best = 0
    for start in _RMD_AGE_ROWS:
        if birth_year >= start >= best:
            best = start
    return _RMD_AGE_ROWS[best]


# ---------------------------------------------------------------------------
# Rule table: the IRS Uniform Lifetime Table
# ---------------------------------------------------------------------------

_ULT_PROV = Provenance(
    table="Uniform Lifetime Table",
    effective_year=2022,
    volatility="stable",
    publisher="IRS, 26 CFR 1.401(a)(9)-9(c)",
    source="https://www.govinfo.gov/content/pkg/FR-2020-11-12/html/2020-24723.htm",
    note=(
        "Transcribed from TD 9930, the final regulation that promulgated it, "
        "and applicable to distribution calendar years beginning on or after "
        "January 1, 2022. Reprinted as IRS Pub 590-B Appendix B Table III. "
        "This table is never typed from memory: check any change against the "
        "source above."
    ),
)

#: Attained age -> distribution period (the divisor). 120 covers 120 and older.
_ULT_ROWS: dict[int, Decimal] = {
    age: Decimal(text)
    for age, text in {
        72: "27.4", 73: "26.5", 74: "25.5", 75: "24.6", 76: "23.7",
        77: "22.9", 78: "22.0", 79: "21.1", 80: "20.2", 81: "19.4",
        82: "18.5", 83: "17.7", 84: "16.8", 85: "16.0", 86: "15.2",
        87: "14.4", 88: "13.7", 89: "12.9", 90: "12.2", 91: "11.5",
        92: "10.8", 93: "10.1", 94: "9.5", 95: "8.9", 96: "8.4",
        97: "7.8", 98: "7.3", 99: "6.8", 100: "6.4", 101: "6.0",
        102: "5.6", 103: "5.2", 104: "4.9", 105: "4.6", 106: "4.3",
        107: "4.1", 108: "3.9", 109: "3.7", 110: "3.5", 111: "3.4",
        112: "3.3", 113: "3.1", 114: "3.0", 115: "2.9", 116: "2.8",
        117: "2.7", 118: "2.5", 119: "2.3", 120: "2.0",
    }.items()
}

UNIFORM_LIFETIME_TABLE = RuleTable(provenance=_ULT_PROV, rows=_ULT_ROWS)

_ULT_MIN_AGE = min(_ULT_ROWS)
_ULT_MAX_AGE = max(_ULT_ROWS)


def uniform_lifetime_divisor(age: int) -> Decimal:
    """The distribution period for an attained age, clamped to the table's ends."""
    return _ULT_ROWS[min(max(int(age), _ULT_MIN_AGE), _ULT_MAX_AGE)]


_SLT_PROV = Provenance(
    table="Single Life Table",
    effective_year=2022,
    volatility="stable",
    publisher="IRS, 26 CFR 1.401(a)(9)-9(b)",
    source="https://www.law.cornell.edu/cfr/text/26/1.401(a)(9)-9",
    checked_on="2026-09-26",
    note=(
        "Transcribed from the regulation as published at that address, every "
        "row, and never typed from memory. A beneficiary's life expectancy for "
        "an inherited account: set once, at the beneficiary's age in the year "
        "after the death, and reduced by one each later year. It governs the "
        "annual minimums of an account inherited under the ten-year rule from "
        "someone who had reached their required beginning date (T.D. 10001, "
        "from 2025), and a pre-2020 inheritance throughout."
    ),
)

#: Attained age -> life expectancy, from 26 CFR 1.401(a)(9)-9(b).
_SLT_ROWS: dict[int, Decimal] = {
    age: Decimal(text)
    for age, text in {
        0: "84.6", 1: "83.7", 2: "82.8", 3: "81.8", 4: "80.8", 5: "79.8",
        6: "78.8", 7: "77.9", 8: "76.9", 9: "75.9", 10: "74.9", 11: "73.9",
        12: "72.9", 13: "71.9", 14: "70.9", 15: "69.9", 16: "69.0", 17: "68.0",
        18: "67.0", 19: "66.0", 20: "65.0", 21: "64.1", 22: "63.1", 23: "62.1",
        24: "61.1", 25: "60.2", 26: "59.2", 27: "58.2", 28: "57.3", 29: "56.3",
        30: "55.3", 31: "54.4", 32: "53.4", 33: "52.5", 34: "51.5", 35: "50.5",
        36: "49.6", 37: "48.6", 38: "47.7", 39: "46.7", 40: "45.7", 41: "44.8",
        42: "43.8", 43: "42.9", 44: "41.9", 45: "41.0", 46: "40.0", 47: "39.0",
        48: "38.1", 49: "37.1", 50: "36.2", 51: "35.3", 52: "34.3", 53: "33.4",
        54: "32.5", 55: "31.6", 56: "30.6", 57: "29.8", 58: "28.9", 59: "28.0",
        60: "27.1", 61: "26.2", 62: "25.4", 63: "24.5", 64: "23.7", 65: "22.9",
        66: "22.0", 67: "21.2", 68: "20.4", 69: "19.6", 70: "18.8", 71: "18.0",
        72: "17.2", 73: "16.4", 74: "15.6", 75: "14.8", 76: "14.1", 77: "13.3",
        78: "12.6", 79: "11.9", 80: "11.2", 81: "10.5", 82: "9.9", 83: "9.3",
        84: "8.7", 85: "8.1", 86: "7.6", 87: "7.1", 88: "6.6", 89: "6.1",
        90: "5.7", 91: "5.3", 92: "4.9", 93: "4.6", 94: "4.3", 95: "4.0",
        96: "3.7", 97: "3.4", 98: "3.2", 99: "3.0", 100: "2.8", 101: "2.6",
        102: "2.5", 103: "2.3", 104: "2.2", 105: "2.1", 106: "2.1", 107: "2.1",
        108: "2.0", 109: "2.0", 110: "2.0", 111: "2.0", 112: "2.0", 113: "1.9",
        114: "1.9", 115: "1.8", 116: "1.8", 117: "1.6", 118: "1.4", 119: "1.1",
        120: "1.0",
    }.items()
}

SINGLE_LIFE_TABLE = RuleTable(provenance=_SLT_PROV, rows=_SLT_ROWS)


def single_life_expectancy(age: int) -> Decimal:
    """The Single Life Table's expectancy for an attained age, clamped."""
    return _SLT_ROWS[min(max(int(age), 0), 120)]


#: The spouse as sole beneficiary must be MORE than this many years younger
#: for the Joint and Last Survivor Table to apply (Reg. 1.401(a)(9)-5).
JOINT_LIFE_AGE_GAP = 10


def rmd_divisor(age: int, spouse_age: Optional[int] = None) -> Decimal:
    """The distribution period for an owner's own account.

    The Uniform Lifetime Table, unless the spouse (taken to be the sole
    beneficiary of a married owner's account) is more than ten years younger,
    when the Joint and Last Survivor Table applies (Reg. 1.401(a)(9)-5). That
    table is not transcribed here - 14,641 cells. Its value can never be less
    than the younger spouse's OWN single life expectancy (the last survivor
    lives at least as long as the survivor), which is within about a year of
    it for the age gaps the rule covers, so that stands in: a minimum a shade
    high, never one under the law's.
    """
    base = uniform_lifetime_divisor(age)
    if spouse_age is not None and int(age) - int(spouse_age) > JOINT_LIFE_AGE_GAP:
        return max(base, single_life_expectancy(spouse_age))
    return base


def rmd(
    balance_cents: int,
    birth_year: int,
    birth_month: Optional[int],
    plan_year: int,
    *,
    spouse_birth_year: Optional[int] = None,
) -> int:
    """The required minimum distribution for ``plan_year``, in cents.

    ``balance_cents`` is the prior December 31 account balance, which is what
    the regulation divides. The age used is the age ATTAINED during the
    distribution calendar year, so it is the plain year difference - the birth
    month never changes the divisor (the regulation keys off the calendar year,
    not a birthday). ``birth_month`` is accepted anyway so callers pass a whole
    birth record rather than picking it apart. ``spouse_birth_year`` brings in
    the joint-life divisor for a much younger spouse (:func:`rmd_divisor`).
    The first year's April 1 deferral is :func:`owner_minimum_cents`.

    Returns 0 before the applicable age, and for a non-positive balance.
    """
    if balance_cents <= 0:
        return 0
    if birth_month is not None and not 1 <= int(birth_month) <= 12:
        raise ValueError(f"birth_month must be 1-12, got {birth_month!r}")
    age = int(plan_year) - int(birth_year)
    if age < applicable_age(int(birth_year)):
        return 0
    divisor = rmd_divisor(age, None if spouse_birth_year is None
                          else int(plan_year) - int(spouse_birth_year))
    return int(
        (Decimal(int(balance_cents)) / divisor).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )


def owner_minimum_cents(balance_cents: int, prior_balance_cents: Optional[int],
                        birth_year: int, birth_month: Optional[int], year: int, *,
                        first_year: Optional[int], spouse_birth_year: Optional[int] = None,
                        defer_first: bool = False) -> int:
    """The minimum out of an account the person OWNS in ``year``, with the
    first distribution year's deferral when the household chose it: IRC
    401(a)(9)(C)(i) lets the first minimum wait until April 1 of the year
    after ``first_year`` (the applicable-age year, or the retirement year for
    a current employer's plan), so that year takes none and the next takes
    two - the first from ``prior_balance_cents``, the balance the first year
    was entered with."""
    if defer_first and first_year is not None:
        if int(year) == int(first_year):
            return 0
        if int(year) == int(first_year) + 1 and prior_balance_cents is not None:
            return (rmd(prior_balance_cents, birth_year, birth_month, int(year) - 1,
                        spouse_birth_year=spouse_birth_year)
                    + rmd(balance_cents, birth_year, birth_month, year,
                          spouse_birth_year=spouse_birth_year))
    return rmd(balance_cents, birth_year, birth_month, year,
               spouse_birth_year=spouse_birth_year)


#: SECURE Act: a death in this year or later puts a designated beneficiary
#: under the ten-year rule (IRC 401(a)(9)(H)).
INHERITED_TEN_YEAR_FROM = 2020
INHERITED_TEN_YEARS = 10


def inherited_floored_in(death_year: int, after_rbd: bool, year: int) -> bool:
    """Whether an inherited account owes a minimum in ``year``: under the
    ten-year rule every year from the one after the death when the decedent
    had reached their required beginning date, otherwise only the tenth;
    a pre-2020 inheritance every year after the death (the stretch)."""
    death, y = int(death_year), int(year)
    if death < INHERITED_TEN_YEAR_FROM:
        return y > death
    if after_rbd:
        return death < y <= death + INHERITED_TEN_YEARS
    return y == death + INHERITED_TEN_YEARS


def inherited_minimum_cents(balance_cents: int, beneficiary_birth_year: int,
                            death_year: int, after_rbd: bool, year: int) -> int:
    """The minimum out of an inherited account in ``year`` (IRC 401(a)(9)(H);
    Reg. 1.401(a)(9)-5): the whole balance in the tenth year after a death in
    2020 or later; before that, and throughout a pre-2020 inheritance, the
    balance over the beneficiary's single life expectancy fixed in the year
    after the death and reduced by one a year, once the decedent had begun
    required distributions (or the inheritance predates 2020)."""
    balance = max(0, int(balance_cents))
    death, y = int(death_year), int(year)
    if not inherited_floored_in(death, after_rbd, y):
        return 0
    if death >= INHERITED_TEN_YEAR_FROM and y == death + INHERITED_TEN_YEARS:
        return balance
    age_after = death + 1 - int(beneficiary_birth_year)
    divisor = max(Decimal(1), single_life_expectancy(age_after) - (y - death - 1))
    return int((Decimal(balance) / divisor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rmd_provenance() -> tuple[Provenance, ...]:
    """The tables :func:`rmd` relies on, for a figure that wants to cite them."""
    return (_RMD_AGE_PROV, _ULT_PROV, _SLT_PROV)


# ---------------------------------------------------------------------------
# Rule: the additional tax on early distributions (IRC 72(t))
# ---------------------------------------------------------------------------

_EARLY_DISTRIBUTION_PROV = Provenance(
    table="Additional tax on early distributions",
    effective_year=1986,
    volatility="stable",
    publisher="IRC 72(t)",
    source="https://www.law.cornell.edu/uscode/text/26/72",
    note=(
        "Ten percent on top of the income tax for a distribution from an IRA "
        "or employer plan before 59 1/2, unless an exception applies: a series "
        "of substantially equal periodic payments (72(t)(2)(A)(iv)), which the "
        "planner does not schedule, or separation from an employer in or after "
        "the year of turning 55, for THAT employer's plan (72(t)(2)(A)(v)). A "
        "Roth conversion is not a distribution for this purpose; drawing the "
        "converted money within five years while under 59 1/2 is "
        "(408A(d)(3)(F)) and is not modeled."
    ),
)

EARLY_DISTRIBUTION_TAX_PCT = 10
EARLY_DISTRIBUTION_AGE_MONTHS = 59 * 12 + 6
SEPARATION_FROM_SERVICE_AGE = 55

EARLY_DISTRIBUTION_TAX = RuleTable(
    provenance=_EARLY_DISTRIBUTION_PROV,
    rows={"additional_tax_pct": EARLY_DISTRIBUTION_TAX_PCT,
          "age_months": EARLY_DISTRIBUTION_AGE_MONTHS,
          "separation_from_service_age": SEPARATION_FROM_SERVICE_AGE},
)


def early_distribution_last_year(birth_year: int, birth_month: Optional[int]) -> int:
    """The last calendar year a distribution to this person is EARLY: the year
    before the one they turn 59 1/2, a draw in that year being taken after the
    date. With no birth month, the year they turn 59 - they may reach 59 1/2
    only the year after, and guessing the other way would plan a penalty
    away."""
    year = int(birth_year) + 59
    if birth_month is None or int(birth_month) + 6 > 12:
        return year                      # 59 1/2 falls in the year they turn 60
    return year - 1


# ---------------------------------------------------------------------------
# Rule tables: Social Security
# ---------------------------------------------------------------------------

_SS_BEND_PROV = Provenance(
    table="Social Security PIA bend points",
    effective_year=2026,
    volatility="indexed",
    publisher="SSA Office of the Chief Actuary",
    source="https://www.ssa.gov/oact/progdata/retirebenefit2.html",
    republished_by="11-01",
    note=(
        "Bend points are LOCKED IN at age 62 by the national average wage "
        "index of the year the worker turns 60, so they are stored per "
        "eligibility-year cohort and an older cohort's row never becomes "
        "stale - which is why the whole published series is here rather than "
        "one current row. Add each new cohort year as SSA publishes it, in "
        "October, for the cohort turning 62 the following year."
    ),
)

#: Eligibility year (the year the worker turns 62) -> (first bend point,
#: second bend point) as MONTHLY AIME in cents.
#:
#: Transcribed from SSA's published table, not computed. The statutory formula
#: (42 U.S.C. 415(a)(1)(B)(ii)) is $180 and $1,085 scaled by
#: ``AWI[year - 2] / AWI[1977]`` and rounded to the nearest dollar, and
#: ``test_retirement`` checks every row here against that formula run on
#: :data:`AWI_SERIES` - so a typo in either table fails the suite rather than
#: quietly moving somebody's benefit.
_SS_BEND_ROWS: dict[int, tuple[int, int]] = {
    1979: (18_000, 108_500), 1980: (19_400, 117_100), 1981: (21_100, 127_400),
    1982: (23_000, 138_800), 1983: (25_400, 152_800), 1984: (26_700, 161_200),
    1985: (28_000, 169_100), 1986: (29_700, 179_000), 1987: (31_000, 186_600),
    1988: (31_900, 192_200), 1989: (33_900, 204_400), 1990: (35_600, 214_500),
    1991: (37_000, 223_000), 1992: (38_700, 233_300), 1993: (40_100, 242_000),
    1994: (42_200, 254_500), 1995: (42_600, 256_700), 1996: (43_700, 263_500),
    1997: (45_500, 274_100), 1998: (47_700, 287_500), 1999: (50_500, 304_300),
    2000: (53_100, 320_200), 2001: (56_100, 338_100), 2002: (59_200, 356_700),
    2003: (60_600, 365_300), 2004: (61_200, 368_900), 2005: (62_700, 377_900),
    2006: (65_600, 395_500), 2007: (68_000, 410_000), 2008: (71_100, 428_800),
    2009: (74_400, 448_300), 2010: (76_100, 458_600), 2011: (74_900, 451_700),
    2012: (76_700, 462_400), 2013: (79_100, 476_800), 2014: (81_600, 491_700),
    2015: (82_600, 498_000), 2016: (85_600, 515_700), 2017: (88_500, 533_600),
    2018: (89_500, 539_700), 2019: (92_600, 558_300), 2020: (96_000, 578_500),
    2021: (99_600, 600_200), 2022: (102_400, 617_200), 2023: (111_500, 672_100),
    2024: (117_400, 707_800), 2025: (122_600, 739_100), 2026: (128_600, 774_900),
}

SS_BEND_POINTS = RuleTable(provenance=_SS_BEND_PROV, rows=_SS_BEND_ROWS)

_SS_WAGE_GROWTH_PROV = Provenance(
    table="Social Security projected average-wage growth (unpublished cohorts)",
    effective_year=2026,
    volatility="indexed",
    publisher="SSA Office of the Chief Actuary",
    source="https://www.ssa.gov/oact/TR/2026/2026_Long-Range_Economic_Assumptions.pdf",
    republished_by="07-01",
    checked_on="2026-09-23",
    note=(
        "The Trustees Report intermediate (alternative II) assumptions put "
        "ultimate price growth at 2.4 percent a year and the real-wage "
        "differential a little over one point above it, so nominal average "
        "wages grow about 3.5 percent a year. A worker who turns 62 after the "
        "last cohort SSA has published gets that growth applied to the last "
        "published bend points; the projection replaces nothing that is "
        "published, and every published cohort still comes from the table "
        "above. Recheck when the next Trustees Report lands, usually by "
        "midyear."
    ),
)

#: The one growth assumption in this module: nominal average-wage-index growth,
#: per year, used ONLY to carry the PIA bend points past the last cohort SSA has
#: published. It is a rate, so it is a ``Decimal`` (CLAUDE.md), and it lives
#: here and nowhere else - the rule is that every figure which moves with law or
#: indexing is in this file.
SS_WAGE_GROWTH = Decimal("0.035")

#: Wrapped as a rule table so the Retirement FAQ (SRD 5.8p) enumerates it with
#: the published tables and prints its provenance - an assumption the household
#: is relying on has to be as visible as a transcribed figure.
SS_PROJECTED_WAGE_GROWTH = RuleTable(
    provenance=_SS_WAGE_GROWTH_PROV,
    rows={"nominal_average_wage_index_growth": SS_WAGE_GROWTH},
)


@dataclass(frozen=True)
class BendPoints:
    """One cohort's two PIA bend points, and whether they were published.

    ``projected`` is the part callers need: a figure resting on a projected
    cohort is an assumption, and the FAQ says so. Nothing downstream may print
    a projected benefit as though SSA had published the bend points behind it.
    """

    eligibility_year: int
    first_cents: int
    second_cents: int
    projected: bool


def _grow_bend_point(cents: int, growth: Decimal) -> int:
    """One bend point carried forward and rounded to a whole dollar, as SSA does."""
    dollars = (Decimal(int(cents)) / 100 * growth).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return int(dollars) * 100


def pia_bend_points(eligibility_year: int) -> BendPoints:
    """The bend points for the cohort turning 62 in ``eligibility_year``.

    Three cases, and the difference between them matters:

    * a PUBLISHED cohort returns its transcribed row, ``projected`` false;
    * a cohort LATER than the published series is projected forward from the
      last published row at :data:`SS_WAGE_GROWTH` a year. A projection is an
      assumption, not an error: a plan that runs to age 100 asks about workers
      whose cohort SSA cannot have published yet, and refusing to answer took
      the whole Retirement Planner page down for any household with a member
      under 62;
    * a cohort EARLIER than the series still raises. That is a data error -
      the published row exists and something asked for it wrong - and guessing
      a past figure that could simply be looked up would hide the bug.

    This is the only year-keyed table in this module that ever refused a year.
    The wage index holds at its last published ratio
    (:func:`indexing_factor`), the taxable maximum passes a future year through
    uncapped (:func:`capped_earnings`), the RMD divisors and applicable ages
    floor to their last row, and the tax, FPL and IRMAA tables carry no year
    axis at all - so no projected year can raise out of any of them.
    """
    year = int(eligibility_year)
    first_published = min(_SS_BEND_ROWS)
    if year < first_published:
        raise KeyError(
            f"no PIA bend points recorded for eligibility year {year}; the "
            f"published series starts at {first_published} and a cohort before "
            "it is a data error, not a projection"
        )
    if year in _SS_BEND_ROWS:
        first, second = _SS_BEND_ROWS[year]
        return BendPoints(year, first, second, False)
    last_published = max(_SS_BEND_ROWS)
    first, second = _SS_BEND_ROWS[last_published]
    growth = (Decimal(1) + SS_WAGE_GROWTH) ** (year - last_published)
    return BendPoints(
        year, _grow_bend_point(first, growth), _grow_bend_point(second, growth), True
    )


_SS_FACTOR_PROV = Provenance(
    table="Social Security PIA formula factors",
    effective_year=1979,
    volatility="stable",
    publisher="42 U.S.C. 415(a)(1)(A)",
    source="https://www.law.cornell.edu/uscode/text/42/415",
    note="90/32/15 percent; unchanged since the 1977 amendments took effect.",
)

#: The three marginal percentages applied to the AIME bands.
SS_PIA_FACTORS = RuleTable(
    provenance=_SS_FACTOR_PROV,
    rows={1: Decimal("0.90"), 2: Decimal("0.32"), 3: Decimal("0.15")},
)

_SS_FRA_PROV = Provenance(
    table="Social Security full retirement age",
    effective_year=1983,
    volatility="stable",
    publisher="42 U.S.C. 416(l)",
    source="https://www.law.cornell.edu/uscode/text/42/416",
    note="66 for 1943-1954, then two months per birth year, 67 for 1960 and later.",
)

_SS_FRA_ROWS: dict[int, int] = {
    0: 65 * 12,
    1938: 65 * 12 + 2, 1939: 65 * 12 + 4, 1940: 65 * 12 + 6,
    1941: 65 * 12 + 8, 1942: 65 * 12 + 10,
    1943: 66 * 12,
    1955: 66 * 12 + 2, 1956: 66 * 12 + 4, 1957: 66 * 12 + 6,
    1958: 66 * 12 + 8, 1959: 66 * 12 + 10,
    1960: 67 * 12,
}

SS_FULL_RETIREMENT_AGE = RuleTable(provenance=_SS_FRA_PROV, rows=_SS_FRA_ROWS)

_SS_CLAIM_PROV = Provenance(
    table="Social Security claim-age adjustment factors",
    effective_year=1983,
    volatility="stable",
    publisher="42 U.S.C. 402(q) and 402(w); 20 CFR 404.410, 404.313",
    source="https://www.law.cornell.edu/uscode/text/42/402",
    note=(
        "Early: 5/9 of one percent per month for the first 36 months before "
        "full retirement age, 5/12 of one percent for each further month. "
        "Late: 8 percent per year (2/3 of one percent per month) for births "
        "in 1943 and later, stopping at age 70."
    ),
)

SS_EARLY_FIRST_36 = Decimal(5) / Decimal(9) / Decimal(100)
SS_EARLY_BEYOND_36 = Decimal(5) / Decimal(12) / Decimal(100)
SS_DELAYED_PER_MONTH = Decimal(2) / Decimal(3) / Decimal(100)

SS_EARLIEST_CLAIM_MONTHS = 62 * 12
SS_LATEST_CREDIT_MONTHS = 70 * 12


def full_retirement_age_months(birth_year: int) -> int:
    """Full retirement age for this cohort, as a whole number of months."""
    best = 0
    for start in _SS_FRA_ROWS:
        if birth_year >= start >= best:
            best = start
    return _SS_FRA_ROWS[best]


def claim_factor(birth_year: int, claim_age_months: int) -> Decimal:
    """The multiplier applied to the PIA for claiming at a given age.

    ``claim_age_months`` is an age in months (62 * 12 = 744 at the earliest).
    Returns exactly 1 at full retirement age, less before it, more after -
    clamped at 62 and at 70, past which no further credit accrues.
    """
    months = max(SS_EARLIEST_CLAIM_MONTHS, int(claim_age_months))
    months = min(SS_LATEST_CREDIT_MONTHS, months)
    fra = full_retirement_age_months(int(birth_year))
    if months == fra:
        return Decimal(1)
    if months < fra:
        early = fra - months
        first = min(early, 36)
        rest = early - first
        return Decimal(1) - first * SS_EARLY_FIRST_36 - rest * SS_EARLY_BEYOND_36
    return Decimal(1) + (months - fra) * SS_DELAYED_PER_MONTH


_SS_FAMILY_PROV = Provenance(
    table="Social Security spousal and survivor benefit factors",
    effective_year=1983,
    volatility="stable",
    publisher="42 U.S.C. 402(b), (c), (e), (f), (q); 20 CFR 404.330-404.338",
    source="https://www.law.cornell.edu/uscode/text/42/402",
    note=(
        "A spouse is entitled to half the worker's primary insurance amount "
        "less their own, once the worker has claimed, reduced 25/36 of one "
        "percent a month for the first 36 months before full retirement age "
        "and 5/12 of one percent for each month beyond; never raised for "
        "starting late. A widow(er) is entitled to the deceased's benefit in "
        "full at the survivor full retirement age (67 for those born 1962 and "
        "later), 71.5 percent of it at 60, and never less than 82.5 percent of "
        "the primary insurance amount when the deceased had claimed early."
    ),
)

SS_SPOUSAL_SHARE = Decimal("0.5")
SS_SPOUSAL_EARLY_FIRST_36 = Decimal(25) / Decimal(36) / Decimal(100)
SS_SPOUSAL_EARLY_BEYOND_36 = Decimal(5) / Decimal(12) / Decimal(100)
SS_SURVIVOR_EARLIEST_MONTHS = 60 * 12
SS_SURVIVOR_SHARE_AT_60 = Decimal("0.715")
SS_SURVIVOR_FLOOR_OF_PIA = Decimal("0.825")

SS_FAMILY_FACTORS = RuleTable(
    provenance=_SS_FAMILY_PROV,
    rows={
        "spousal_share_of_pia": SS_SPOUSAL_SHARE,
        "spousal_reduction_per_month_first_36": SS_SPOUSAL_EARLY_FIRST_36,
        "spousal_reduction_per_month_beyond_36": SS_SPOUSAL_EARLY_BEYOND_36,
        "survivor_share_at_60": SS_SURVIVOR_SHARE_AT_60,
        "survivor_floor_of_pia_when_claimed_early": SS_SURVIVOR_FLOOR_OF_PIA,
    },
)

#: Survivor full retirement age (42 U.S.C. 416(l)(2)): 66 for 1945-1956, then
#: two months a year, 67 for 1962 and later - the retirement table shifted two
#: birth years.
_SS_SURVIVOR_FRA_ROWS: dict[int, int] = {
    0: 65 * 12,
    1940: 65 * 12 + 2, 1941: 65 * 12 + 4, 1942: 65 * 12 + 6,
    1943: 65 * 12 + 8, 1944: 65 * 12 + 10,
    1945: 66 * 12,
    1957: 66 * 12 + 2, 1958: 66 * 12 + 4, 1959: 66 * 12 + 6,
    1960: 66 * 12 + 8, 1961: 66 * 12 + 10,
    1962: 67 * 12,
}


def survivor_full_retirement_age_months(birth_year: int) -> int:
    """Full retirement age for a widow(er)'s benefit, as a number of months."""
    best = 0
    for start in _SS_SURVIVOR_FRA_ROWS:
        if birth_year >= start >= best:
            best = start
    return _SS_SURVIVOR_FRA_ROWS[best]


def spousal_factor(birth_year: int, age_months: int) -> Decimal:
    """The multiplier on a spousal benefit begun at ``age_months``: exactly 1
    at full retirement age or later (no delayed credit), less before it."""
    months = max(SS_EARLIEST_CLAIM_MONTHS, int(age_months))
    fra = full_retirement_age_months(int(birth_year))
    if months >= fra:
        return Decimal(1)
    early = fra - months
    first = min(early, 36)
    return (Decimal(1) - first * SS_SPOUSAL_EARLY_FIRST_36
            - (early - first) * SS_SPOUSAL_EARLY_BEYOND_36)


def spousal_benefit_cents(worker_pia_cents: int, own_pia_cents: int,
                          birth_year: int, age_months: int) -> int:
    """The monthly spousal benefit paid ON TOP of a person's own retirement
    benefit: half the worker's primary insurance amount less the person's own
    (zero when their own is the larger), reduced for starting before full
    retirement age. Payable only once the worker has claimed, which is the
    caller's to check."""
    excess = (Decimal(max(0, int(worker_pia_cents))) * SS_SPOUSAL_SHARE
              - Decimal(max(0, int(own_pia_cents))))
    if excess <= 0:
        return 0
    return to_lower_dime_cents(excess * spousal_factor(birth_year, age_months))


def survivor_factor(birth_year: int, age_months: int) -> Decimal:
    """The multiplier on a widow(er)'s benefit begun at ``age_months``: 1 at
    the survivor full retirement age, sliding evenly down to 0.715 at 60."""
    fra = survivor_full_retirement_age_months(int(birth_year))
    months = max(SS_SURVIVOR_EARLIEST_MONTHS, int(age_months))
    if months >= fra:
        return Decimal(1)
    span = fra - SS_SURVIVOR_EARLIEST_MONTHS
    return Decimal(1) - (Decimal(1) - SS_SURVIVOR_SHARE_AT_60) * (fra - months) / span


def survivor_benefit_cents(deceased_pia_cents: int, deceased_factor,
                           survivor_birth_year: int, survivor_age_months: int) -> int:
    """The monthly widow(er)'s benefit begun at ``survivor_age_months``.

    ``deceased_factor`` is the multiplier the deceased's own benefit carried:
    their claim factor when they had claimed, the delayed credits earned by
    their death when they had passed full retirement age without claiming,
    and 1 otherwise. Delayed credits pass to the survivor; an early claim
    caps the survivor at the larger of the deceased's reduced benefit and
    82.5 percent of the primary insurance amount (the RIB-LIM)."""
    pia = Decimal(max(0, int(deceased_pia_cents)))
    factor = Decimal(str(deceased_factor))
    reduced = (pia * max(factor, Decimal(1))
               * survivor_factor(survivor_birth_year, survivor_age_months))
    if factor < 1:
        reduced = min(reduced, max(pia * factor, pia * SS_SURVIVOR_FLOOR_OF_PIA))
    return to_lower_dime_cents(reduced)


def primary_insurance_amount(aime_cents: int, eligibility_year: int) -> int:
    """Monthly PIA in cents from an average indexed monthly earnings figure.

    The bend points are the ones for the year the worker turns 62, from
    :func:`pia_bend_points` - published where SSA has published them, projected
    at :data:`SS_WAGE_GROWTH` beyond that, and a hard error for a cohort before
    the series starts. A neighbor's row is never borrowed, because a wrong bend
    point is a wrong benefit for the rest of the projection.
    """
    points = pia_bend_points(eligibility_year)
    first, second = points.first_cents, points.second_cents
    aime = max(0, int(aime_cents))
    band1 = min(aime, first)
    band2 = max(0, min(aime, second) - first)
    band3 = max(0, aime - second)
    total = (
        band1 * SS_PIA_FACTORS[1]
        + band2 * SS_PIA_FACTORS[2]
        + band3 * SS_PIA_FACTORS[3]
    )
    return to_lower_dime_cents(total)


def to_lower_dime_cents(amount) -> int:
    """Cents rounded DOWN to a multiple of ten: how SSA states a primary
    insurance amount and a monthly benefit (42 U.S.C. 415(a)(1)(A), (g))."""
    return int(Decimal(amount).to_integral_value(rounding=ROUND_DOWN)) // 10 * 10


def benefit_from_pia_cents(pia_cents: int, factor) -> int:
    """A monthly benefit: the primary insurance amount times a claim, spousal
    or survivor factor, to the lower dime."""
    return to_lower_dime_cents(Decimal(max(0, int(pia_cents))) * Decimal(str(factor)))


def earliest_claim_months(person: Mapping, claim_age_months: int) -> int:
    """The claim age as SSA counts it: a claim "at 62" is first payable for
    the month the person is 62 THROUGHOUT (42 U.S.C. 402(a); 20 CFR 404.310),
    so anyone not born on the first is entitled a month later, at 62 and one
    month, with one month less of reduction. (Born on the second counts too
    and is not recorded; it is read as later.)"""
    months = int(claim_age_months)
    if months == SS_EARLIEST_CLAIM_MONTHS and not person.get("born_on_the_first"):
        return months + 1
    return months


# ---------------------------------------------------------------------------
# Rule tables: the wage series an AIME is built from
# ---------------------------------------------------------------------------
#
# A PIA needs an AIME, and an AIME needs two national series: the average wage
# index that restates old earnings in today's wage terms, and the contribution
# and benefit base that caps what any one year can contribute. Both are read
# through the Internet Archive rather than ssa.gov directly - every /oact/ page
# answers an automated fetch with HTTP 403 - so the snapshot date is part of
# the citation.

_SS_AWI_PROV = Provenance(
    table="Social Security national average wage index",
    effective_year=2024,
    volatility="indexed",
    publisher="SSA Office of the Chief Actuary",
    source=(
        "https://web.archive.org/web/20260102114257/"
        "https://www.ssa.gov/OACT/COLA/awiseries.html"
    ),
    republished_by="10-15",
    checked_on="2026-09-23",
    note=(
        "The index for a year is published in October of the FOLLOWING year, "
        "so a current copy of this series always ends two years back. For a "
        "worker who has already turned 60 that is not a gap at all: the "
        "indexing is frozen at the year they turned 60, which is why "
        "ss_benefit_provenance() cites the frozen year for them and this "
        "record only for someone younger."
    ),
)

#: Calendar year -> the national average wage for that year, in DOLLARS as a
#: Decimal (SSA publishes it to the cent). Not cents: this series is only ever
#: a ratio's numerator or denominator, never money the user holds.
AWI_SERIES = RuleTable(
    provenance=_SS_AWI_PROV,
    rows={
        1951: Decimal("2799.16"), 1952: Decimal("2973.32"), 1953: Decimal("3139.44"),
        1954: Decimal("3155.64"), 1955: Decimal("3301.44"), 1956: Decimal("3532.36"),
        1957: Decimal("3641.72"), 1958: Decimal("3673.80"), 1959: Decimal("3855.80"),
        1960: Decimal("4007.12"), 1961: Decimal("4086.76"), 1962: Decimal("4291.40"),
        1963: Decimal("4396.64"), 1964: Decimal("4576.32"), 1965: Decimal("4658.72"),
        1966: Decimal("4938.36"), 1967: Decimal("5213.44"), 1968: Decimal("5571.76"),
        1969: Decimal("5893.76"), 1970: Decimal("6186.24"), 1971: Decimal("6497.08"),
        1972: Decimal("7133.80"), 1973: Decimal("7580.16"), 1974: Decimal("8030.76"),
        1975: Decimal("8630.92"), 1976: Decimal("9226.48"), 1977: Decimal("9779.44"),
        1978: Decimal("10556.03"), 1979: Decimal("11479.46"), 1980: Decimal("12513.46"),
        1981: Decimal("13773.10"), 1982: Decimal("14531.34"), 1983: Decimal("15239.24"),
        1984: Decimal("16135.07"), 1985: Decimal("16822.51"), 1986: Decimal("17321.82"),
        1987: Decimal("18426.51"), 1988: Decimal("19334.04"), 1989: Decimal("20099.55"),
        1990: Decimal("21027.98"), 1991: Decimal("21811.60"), 1992: Decimal("22935.42"),
        1993: Decimal("23132.67"), 1994: Decimal("23753.53"), 1995: Decimal("24705.66"),
        1996: Decimal("25913.90"), 1997: Decimal("27426.00"), 1998: Decimal("28861.44"),
        1999: Decimal("30469.84"), 2000: Decimal("32154.82"), 2001: Decimal("32921.92"),
        2002: Decimal("33252.09"), 2003: Decimal("34064.95"), 2004: Decimal("35648.55"),
        2005: Decimal("36952.94"), 2006: Decimal("38651.41"), 2007: Decimal("40405.48"),
        2008: Decimal("41334.97"), 2009: Decimal("40711.61"), 2010: Decimal("41673.83"),
        2011: Decimal("42979.61"), 2012: Decimal("44321.67"), 2013: Decimal("44888.16"),
        2014: Decimal("46481.52"), 2015: Decimal("48098.63"), 2016: Decimal("48642.15"),
        2017: Decimal("50321.89"), 2018: Decimal("52145.80"), 2019: Decimal("54099.99"),
        2020: Decimal("55628.60"), 2021: Decimal("60575.07"), 2022: Decimal("63795.13"),
        2023: Decimal("66621.80"), 2024: Decimal("69846.57"),
    },
)

_SS_WAGE_BASE_PROV = Provenance(
    table="Social Security contribution and benefit base",
    effective_year=2026,
    volatility="indexed",
    publisher="SSA Office of the Chief Actuary",
    source=(
        "https://web.archive.org/web/20260101065255/"
        "https://www.ssa.gov/OACT/COLA/cbb.html"
    ),
    republished_by="11-01",
    checked_on="2026-09-23",
    note=(
        "The taxable maximum: earnings above it in a year pay no Social "
        "Security tax and never enter an AIME. A year's base never changes "
        "after the fact, so old rows here are history, not staleness."
    ),
)

#: Calendar year -> that year's taxable maximum, in CENTS.
SS_WAGE_BASE = RuleTable(
    provenance=_SS_WAGE_BASE_PROV,
    rows={
        1951: 360_000, 1952: 360_000, 1953: 360_000, 1954: 360_000,
        1955: 420_000, 1956: 420_000, 1957: 420_000, 1958: 420_000,
        1959: 480_000, 1960: 480_000, 1961: 480_000, 1962: 480_000,
        1963: 480_000, 1964: 480_000, 1965: 480_000, 1966: 660_000,
        1967: 660_000, 1968: 780_000, 1969: 780_000, 1970: 780_000,
        1971: 780_000, 1972: 900_000, 1973: 1_080_000, 1974: 1_320_000,
        1975: 1_410_000, 1976: 1_530_000, 1977: 1_650_000, 1978: 1_770_000,
        1979: 2_290_000, 1980: 2_590_000, 1981: 2_970_000, 1982: 3_240_000,
        1983: 3_570_000, 1984: 3_780_000, 1985: 3_960_000, 1986: 4_200_000,
        1987: 4_380_000, 1988: 4_500_000, 1989: 4_800_000, 1990: 5_130_000,
        1991: 5_340_000, 1992: 5_550_000, 1993: 5_760_000, 1994: 6_060_000,
        1995: 6_120_000, 1996: 6_270_000, 1997: 6_540_000, 1998: 6_840_000,
        1999: 7_260_000, 2000: 7_620_000, 2001: 8_040_000, 2002: 8_490_000,
        2003: 8_700_000, 2004: 8_790_000, 2005: 9_000_000, 2006: 9_420_000,
        2007: 9_750_000, 2008: 10_200_000, 2009: 10_680_000, 2010: 10_680_000,
        2011: 10_680_000, 2012: 11_010_000, 2013: 11_370_000, 2014: 11_700_000,
        2015: 11_850_000, 2016: 11_850_000, 2017: 12_720_000, 2018: 12_840_000,
        2019: 13_290_000, 2020: 13_770_000, 2021: 14_280_000, 2022: 14_700_000,
        2023: 16_020_000, 2024: 16_860_000, 2025: 17_610_000, 2026: 18_450_000,
    },
)

#: The number of earning years an AIME averages, and the denominator that
#: follows from it (35 years x 12 months). Fewer than 35 years of earnings does
#: not shrink the denominator - the missing years count as zero, which is the
#: single most misunderstood part of the formula and the reason a 20-year
#: career reads so much lower than people expect.
SS_AIME_YEARS = 35
SS_AIME_MONTHS = SS_AIME_YEARS * 12

#: Earnings are indexed to the wage level of the year the worker turns 60, and
#: years at or after that one count at face value.
SS_INDEX_AGE = 60

#: The year the worker turns 62: the cohort whose bend points apply.
SS_ELIGIBILITY_AGE = 62


def _awi_years() -> tuple[int, int]:
    rows = AWI_SERIES.rows
    return min(rows), max(rows)


def awi_provenance(birth_year: int) -> Provenance:
    """The wage-index citation for ONE worker, which is not the same for everyone.

    Indexing freezes at the year the worker turns 60. Once this series reaches
    that year, no later publication can move that worker's figure, so citing
    the series' own last year (and warning that it is two years back) would be
    telling them to worry about something settled. For a younger worker the
    missing years really are missing - see :func:`indexing_factor` for what is
    assumed in their place - so they get the live, warnable record.
    """
    first, last = _awi_years()
    index_year = int(birth_year) + SS_INDEX_AGE
    if index_year <= last:
        return replace(
            _SS_AWI_PROV,
            effective_year=index_year,
            volatility="stable",
            note=(
                f"Indexing is frozen at {index_year}, the year this worker "
                f"turned {SS_INDEX_AGE}; no later wage index changes this figure."
            ),
        )
    return replace(
        _SS_AWI_PROV,
        note=(
            _SS_AWI_PROV.note
            + f" This worker turns {SS_INDEX_AGE} in {index_year}, so the "
            f"{last + 1}-{index_year} index is not published yet and is "
            "projected at the wage-growth assumption that also carries the "
            "bend points; the figure is in the dollars of the year they turn 62."
        ),
    )


def indexing_factor(earning_year: int, birth_year: int) -> Decimal:
    """What one year's earnings are multiplied by before they enter an AIME.

    ``AWI(year the worker turns 60) / AWI(the year they earned it)``, and
    exactly 1 for a year at or after that one - SSA does not index the last
    working years, so a late-career raise arrives at face value.

    Beyond the published series the index is carried forward at
    :data:`SS_WAGE_GROWTH` - the same assumption that carries the bend points,
    so a younger worker's figure is in the dollars of the year they turn 62
    all the way through (held flat, it read low against bend points that had
    grown; found in an audit).
    """
    first, last = _awi_years()
    earned = max(int(earning_year), first)
    index_year = int(birth_year) + SS_INDEX_AGE
    if earned >= index_year:
        return Decimal(1)

    def awi(year: int) -> Decimal:
        if year <= last:
            return Decimal(AWI_SERIES[year])
        return Decimal(AWI_SERIES[last]) * (Decimal(1) + SS_WAGE_GROWTH) ** (year - last)

    return awi(index_year) / awi(earned)


def capped_earnings(year: int, earnings_cents: int) -> int:
    """One year's earnings, cut to that year's taxable maximum.

    Figures copied off an SSA Earnings Report are already capped, so this is a
    no-op for them; it exists for the years Mammon estimated from the ledger,
    where a bonus can easily push a paycheck total past a cap SSA would have
    ignored. Years outside the published table pass through uncapped.
    """
    amount = max(0, int(earnings_cents))
    if int(year) in SS_WAGE_BASE:
        return min(amount, int(SS_WAGE_BASE[int(year)]))
    return amount


def average_indexed_monthly_earnings(
    earnings: Mapping[int, int], birth_year: int
) -> int:
    """AIME in cents from a year -> earnings-cents record.

    Each year is capped at its taxable maximum, indexed to the worker's age-60
    wage level, and the best :data:`SS_AIME_YEARS` of those are averaged over
    :data:`SS_AIME_MONTHS` months. The result is truncated to a whole dollar,
    as SSA does, so the figure here matches the one on their statement rather
    than sitting a few cents off it.
    """
    indexed = []
    for year, cents in earnings.items():
        amount = capped_earnings(int(year), int(cents))
        if amount <= 0:
            continue
        indexed.append(Decimal(amount) * indexing_factor(int(year), birth_year))
    indexed.sort(reverse=True)
    total = sum(indexed[:SS_AIME_YEARS], Decimal(0))
    dollars = (total / Decimal(SS_AIME_MONTHS) / Decimal(100)).to_integral_value(
        rounding=ROUND_DOWN
    )
    return int(dollars) * 100


def primary_insurance_cents(earnings: Mapping[int, int], birth_year: int) -> int:
    """The monthly primary insurance amount from an earnings record, in cents:
    0 with no earnings, decided BEFORE the formula is asked (a person with an
    AIME of zero is not a claimant - see :func:`monthly_benefit`)."""
    aime = average_indexed_monthly_earnings(earnings, birth_year)
    if aime <= 0:
        return 0
    return primary_insurance_amount(aime, int(birth_year) + SS_ELIGIBILITY_AGE)


def monthly_benefit(
    earnings: Mapping[int, int], birth_year: int, claim_age_months: int
) -> int:
    """The monthly Social Security benefit in cents for claiming at a given age.

    Three published steps, in order: average the indexed earnings, run them
    through the PIA bands for the cohort turning 62, then apply the claim-age
    factor. In today's dollars - no COLA is projected forward, because a COLA
    applies to every future year alike and inventing one would dress a guess up
    as a benefit.

    To the lower dime, as SSA states it; the check is then truncated to the
    dollar after the Part B premium comes off, which is not modeled.

    No earnings means no benefit, and the check comes FIRST: a person with an
    AIME of zero - a child of record, or someone whose earnings rows are all
    zero - is not a claimant, and running the PIA formula for them would ask
    for a cohort's bend points to multiply a zero by.
    """
    pia = primary_insurance_cents(earnings, birth_year)
    if pia <= 0:
        return 0
    return benefit_from_pia_cents(pia, claim_factor(int(birth_year), int(claim_age_months)))


def ss_benefit_provenance(birth_year: int) -> tuple[Provenance, ...]:
    """Every published table a :func:`monthly_benefit` figure rests on.

    Hand it to :func:`worst_provenance` for the one line to print under the
    number, or show them all where there is room. A worker whose cohort SSA has
    not published yet rests on one more record than a retiree does - the growth
    assumption behind their projected bend points - and it is listed, because a
    figure resting on an assumption has to be able to say which one.
    """
    records = (
        awi_provenance(birth_year),
        _SS_WAGE_BASE_PROV,
        _SS_BEND_PROV,
        _SS_FACTOR_PROV,
        _SS_FRA_PROV,
        _SS_CLAIM_PROV,
    )
    if int(birth_year) + SS_ELIGIBILITY_AGE > max(_SS_BEND_ROWS):
        records += (_SS_WAGE_GROWTH_PROV,)
    return records


def ss_benefit_citation(birth_year: int) -> str:
    """One line naming the rules behind a benefit figure, for printing under it.

    :func:`worst_provenance` answers a different question - which table will go
    stale first - and under a benefit it reliably returns the 1979 PIA factors,
    because a table that cannot move is never the stale one. That is correct
    and useless to read. This names the two tables that decide the number for
    THIS worker: their cohort's bend points and their frozen indexing year.
    """
    year = int(birth_year) + SS_ELIGIBILITY_AGE
    awi = awi_provenance(birth_year)
    return (
        f"{_SS_BEND_PROV.table} for the {year} cohort and {_SS_FACTOR_PROV.table} "
        f"({_SS_BEND_PROV.publisher}), with earnings indexed to "
        f"{awi.effective_year} wages"
    )


def claim_age_choices(birth_year: int) -> tuple[tuple[str, int], ...]:
    """The claim ages worth putting side by side: 62, full retirement age, 70.

    A range, deliberately, and always the same three. One number invites the
    question "is that the best one", which is a question Mammon does not
    answer; three numbers with the rule beside each let the user see the shape
    of the trade and decide it themselves.
    """
    fra = full_retirement_age_months(int(birth_year))
    label = f"{fra // 12}" + (f" and {fra % 12}mo" if fra % 12 else "")
    return (
        ("62", SS_EARLIEST_CLAIM_MONTHS),
        (f"full retirement age ({label})", fra),
        ("70", SS_LATEST_CREDIT_MONTHS),
    )


# ---------------------------------------------------------------------------
# Rule table: the retirement earnings test
# ---------------------------------------------------------------------------

_SS_EARNINGS_TEST_PROV = Provenance(
    table="Social Security retirement earnings test exempt amounts",
    effective_year=2026,
    volatility="indexed",
    publisher="SSA Office of the Chief Actuary (42 U.S.C. 403(b), (f))",
    source="https://www.ssa.gov/oact/cola/rtea.html",
    republished_by="11-01",
    checked_on="2026-09-26",
    note=(
        "Claiming before full retirement age while still working: $1 of "
        "benefit is withheld for every $2 of earnings over the lower amount "
        "in a year before the one full retirement age is reached, and $1 for "
        "every $3 over the higher amount in that year, counting only the "
        "months before it. The months withheld raise the benefit at full "
        "retirement age as though the claim had been that many months later. "
        "Indexed to average wages; carried past the table year here at the "
        "bracket-indexing rate."
    ),
)

#: Cents, 2026: the lower amount (years before the year of full retirement
#: age) and the higher one (that year).
_SS_EARNINGS_TEST_ROWS: dict[str, int] = {
    "under_full_retirement_age": 24_480_00,
    "year_of_full_retirement_age": 65_160_00,
}

SS_EARNINGS_TEST = RuleTable(provenance=_SS_EARNINGS_TEST_PROV,
                             rows=_SS_EARNINGS_TEST_ROWS)


def earnings_test_withheld_cents(earnings_cents: int, benefit_cents: int, year: int,
                                 index_pct, *, year_of_full_retirement: bool) -> int:
    """The benefit withheld in ``year`` out of ``benefit_cents`` (the months
    before full retirement age only) for ``earnings_cents`` of wages in those
    months. Never more than the benefit itself."""
    key = ("year_of_full_retirement_age" if year_of_full_retirement
           else "under_full_retirement_age")
    exempt = indexed_cents(_SS_EARNINGS_TEST_ROWS[key], index_pct, int(year))
    over = max(0, int(earnings_cents) - exempt)
    withheld = over // (3 if year_of_full_retirement else 2)
    return min(max(0, int(benefit_cents)), withheld)


# ---------------------------------------------------------------------------
# Rule table: the federal poverty level
# ---------------------------------------------------------------------------

_FPL_PROV = Provenance(
    table="HHS poverty guidelines",
    effective_year=2026,
    volatility="indexed",
    publisher="HHS, Office of the Assistant Secretary for Planning and Evaluation",
    source="https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines",
    republished_by="02-01",
    checked_on="2026-09-23",
    note="Published in the Federal Register January 15, 2026. ACA subsidy "
    "eligibility uses the PRIOR year's guidelines, so a coverage year and a "
    "guideline year are not the same number.",
)

#: Region -> (one-person amount in cents, additional per person in cents).
_FPL_ROWS: dict[str, tuple[int, int]] = {
    "contiguous": (1_596_000, 568_000),
    "alaska": (1_995_000, 710_000),
    "hawaii": (1_836_000, 653_000),
}

FEDERAL_POVERTY_LEVEL = RuleTable(provenance=_FPL_PROV, rows=_FPL_ROWS)


def federal_poverty_level(household_size: int, region: str = "contiguous") -> int:
    """The poverty guideline for a household of this size, in cents."""
    size = int(household_size)
    if size < 1:
        raise ValueError("household_size must be at least 1")
    key = (region or "contiguous").strip().lower()
    if key not in _FPL_ROWS:
        raise KeyError(f"unknown region {region!r}; have {sorted(_FPL_ROWS)}")
    base, extra = _FPL_ROWS[key]
    return base + extra * (size - 1)


# ---------------------------------------------------------------------------
# Rule table: IRMAA
# ---------------------------------------------------------------------------

_IRMAA_PROV = Provenance(
    table="Medicare IRMAA income tiers",
    effective_year=2026,
    volatility="indexed",
    publisher="CMS",
    source="https://www.cms.gov/newsroom/fact-sheets/"
    "2026-medicare-parts-b-premiums-and-deductibles",
    republished_by="12-01",
    checked_on="2026-09-23",
    note=(
        "Released November 14, 2025. A CLIFF, not a phase-in: one dollar over "
        "a threshold costs the whole step. Tiers are tested against MAGI from "
        "TWO years earlier, so a 2026 premium turns on 2024 income and a Roth "
        "conversion shows up in the premium two years later."
    ),
)


@dataclass(frozen=True)
class IrmaaTier:
    """One IRMAA step: the MAGI ceilings and what it costs per month, in cents."""

    single_max_cents: Optional[int]      # None = no upper bound
    joint_max_cents: Optional[int]
    part_b_total_cents: int
    part_d_surcharge_cents: int


#: Ordered low to high; the last tier is unbounded. Ceilings are INCLUSIVE for
#: the bounded tiers as CMS states them ("greater than X and less than or equal
#: to Y"), except the top two, which CMS states as a strict cut at $500,000 /
#: $750,000 - handled by making the fifth tier's ceiling exclusive below.
_IRMAA_STANDARD: tuple[IrmaaTier, ...] = (
    IrmaaTier(10_900_000, 21_800_000, 20_290, 0),
    IrmaaTier(13_700_000, 27_400_000, 28_410, 1_450),
    IrmaaTier(17_100_000, 34_200_000, 40_580, 3_750),
    IrmaaTier(20_500_000, 41_000_000, 52_750, 6_040),
    IrmaaTier(49_999_999, 74_999_999, 64_920, 8_330),
    IrmaaTier(None, None, 68_990, 9_100),
)

#: Married filing separately has its own, much shorter ladder.
_IRMAA_SEPARATE: tuple[IrmaaTier, ...] = (
    IrmaaTier(10_900_000, None, 20_290, 0),
    IrmaaTier(39_099_999, None, 64_920, 8_330),
    IrmaaTier(None, None, 68_990, 9_100),
)

IRMAA_TIERS = RuleTable(
    provenance=_IRMAA_PROV,
    rows={"standard": _IRMAA_STANDARD, "separate": _IRMAA_SEPARATE},
)

#: The Part B premium with no adjustment, for 2026.
PART_B_STANDARD_CENTS = 20_290

IRMAA_LOOKBACK_YEARS = 2

FILING_STATUSES = ("single", "joint", "separate")


#: The year the IRMAA table above is published for.
IRMAA_TABLE_YEAR = 2026
IRMAA_TOP_TIER_FROZEN_THROUGH = 2027
MEDICARE_AGE = 65


def medicare_months(person: Mapping, year: int,
                    start_override: Optional[tuple[int, int]] = None) -> int:
    """Months of ``year`` a person is on Medicare, enrolling at 65.

    Coverage starts the month the person turns 65 - a month earlier for
    someone born on the 1st, who attains the age the day before (20 CFR
    404.2) - or at ``start_override`` (year, month) when that is later:
    someone still working past 65 stays on the employer's coverage and
    enrolls when the job ends (the caller reads that off the salary)."""
    birth_year = person.get("birth_year")
    if birth_year is None:
        return 0
    start_year = int(birth_year) + MEDICARE_AGE
    start_month = int(person.get("birth_month") or 1)
    if person.get("born_on_the_first"):
        start_month -= 1
        if start_month == 0:
            start_year, start_month = start_year - 1, 12
    if start_override is not None and tuple(start_override) > (start_year, start_month):
        start_year, start_month = int(start_override[0]), int(start_override[1])
    if int(year) < start_year:
        return 0
    if int(year) > start_year:
        return 12
    return 13 - start_month


def _irmaa_ladder(filing_status: str) -> tuple[tuple[IrmaaTier, ...], bool]:
    status = (filing_status or "single").strip().lower()
    if status == "separate":
        return _IRMAA_SEPARATE, False
    return _IRMAA_STANDARD, status == "joint"


def irmaa_ceilings_cents(filing_status: str, year: int, index_pct=0) -> list[int]:
    """The MAGI ceiling of every tier below the top, for premium ``year``.

    Indexed from the table year at ``index_pct`` - the tiers follow CPI-U, a
    shade above the chained CPI behind the brackets, too little to be worth a
    setting of its own. The top cut is frozen through 2027 by statute and indexed
    only after (42 U.S.C. 1395r(i)(6))."""
    ladder, joint = _irmaa_ladder(filing_status)
    out = []
    for step, tier in enumerate(ladder[:-1]):
        ceiling = tier.joint_max_cents if joint else tier.single_max_cents
        if ceiling is None:
            ceiling = tier.single_max_cents
        if step == len(ladder) - 2:
            # The top cut ($500,000 / $750,000) is fixed by statute through
            # 2027 and indexed only after (42 U.S.C. 1395r(i)(6)).
            out.append(household_amount_cents(
                int(ceiling), index_pct, max(0, int(year) - IRMAA_TOP_TIER_FROZEN_THROUGH)))
        else:
            out.append(indexed_cents(int(ceiling), index_pct, int(year)))
    return out


def irmaa_surcharge_cents(magi_cents: int, filing_status: str, year: int, *,
                          index_pct=0, growth_pct=None, part_d: bool = True) -> tuple[int, int]:
    """(tier - 0 is no surcharge; the surcharge for one enrollee for a full
    ``year``, Part B and - for someone with drug coverage, ``part_d`` - Part
    D (42 U.S.C. 1395w-113(a)(7))). ``magi_cents`` is the income from two
    years before ``year`` (the lookback). The dollars grow at ``growth_pct``
    from the table year: the surcharges are shares of Medicare's cost per
    enrollee, which has outrun inflation."""
    if growth_pct is None:
        growth_pct = DEFAULT_MEDICARE_GROWTH_PCT
    ladder, _joint = _irmaa_ladder(filing_status)
    ceilings = irmaa_ceilings_cents(filing_status, year, index_pct)
    step = next((i for i, c in enumerate(ceilings) if int(magi_cents) <= c),
                len(ceilings))
    tier = ladder[step]
    monthly = (tier.part_b_total_cents - PART_B_STANDARD_CENTS
               + (tier.part_d_surcharge_cents if part_d else 0))
    grown = (Decimal(monthly * 12)
             * (1 + Decimal(str(growth_pct)) / 100) ** max(0, int(year) - IRMAA_TABLE_YEAR))
    return step, int(grown.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def irmaa_tier(magi_cents: int, filing_status: str = "single") -> IrmaaTier:
    """The IRMAA step a MAGI lands in. ``filing_status`` is single/joint/separate."""
    status = (filing_status or "single").strip().lower()
    if status not in FILING_STATUSES:
        raise ValueError(f"unknown filing status {filing_status!r}")
    magi = int(magi_cents)
    if status == "separate":
        tiers, joint = _IRMAA_SEPARATE, False
    else:
        tiers, joint = _IRMAA_STANDARD, status == "joint"
    for tier in tiers:
        ceiling = tier.joint_max_cents if joint else tier.single_max_cents
        if ceiling is None or magi <= ceiling:
            return tier
    return tiers[-1]


# ---------------------------------------------------------------------------
# Rule table: federal ordinary-income brackets
# ---------------------------------------------------------------------------
#
# These are here so that a screen comparing "the bracket a projected RMD lands
# in" with "the bracket you are in today" can CITE both rates instead of
# asserting them. Mammon still does not KNOW the user's bracket and must never
# infer one from the ledger (see mammon/reports/capital_gains.py): every figure
# below is a lookup against taxable income the user typed in, on a table with a
# publisher and a year on it.

_TAX_BRACKET_PROV = Provenance(
    table="Federal ordinary income tax brackets",
    effective_year=2026,
    volatility="indexed",
    publisher="IRS Rev. Proc. 2025-32",
    source="https://www.irs.gov/pub/irs-drop/rp-25-32.pdf",
    republished_by="11-01",
    checked_on="2026-09-23",
    note=(
        "Bounds are TAXABLE income (after the standard or itemized deduction), "
        "not gross and not MAGI. Married filing separately is the joint ladder "
        "halved, including its top step, which is why it is not the single "
        "ladder above 35%."
    ),
)


@dataclass(frozen=True)
class TaxBracket:
    """One ordinary-income step: its rate and the taxable income it spans."""

    rate_percent: int
    lower_cents: int
    upper_cents: Optional[int]      # None = the top step, no edge above it

    @property
    def rate_label(self) -> str:
        return f"{self.rate_percent}%"

    def headroom_cents(self, taxable_cents: int) -> Optional[int]:
        """Taxable income left below this step's upper edge, or None at the top."""
        if self.upper_cents is None:
            return None
        return max(0, self.upper_cents - int(taxable_cents))


def _ladder(*edges: tuple[int, int]) -> tuple[TaxBracket, ...]:
    """(rate, lower bound) pairs, ascending, into a closed ladder."""
    out: list[TaxBracket] = []
    for i, (rate, lower) in enumerate(edges):
        upper = edges[i + 1][1] if i + 1 < len(edges) else None
        out.append(TaxBracket(rate, lower, upper))
    return tuple(out)


#: Filing status -> the ladder, ascending. Cents, tax year 2026.
_TAX_BRACKET_ROWS: dict[str, tuple[TaxBracket, ...]] = {
    "single": _ladder(
        (10, 0), (12, 1_240_000), (22, 5_040_000), (24, 10_570_000),
        (32, 20_177_500), (35, 25_622_500), (37, 64_060_000),
    ),
    "joint": _ladder(
        (10, 0), (12, 2_480_000), (22, 10_080_000), (24, 21_140_000),
        (32, 40_355_000), (35, 51_245_000), (37, 76_870_000),
    ),
    "separate": _ladder(
        (10, 0), (12, 1_240_000), (22, 5_040_000), (24, 10_570_000),
        (32, 20_177_500), (35, 25_622_500), (37, 38_435_000),
    ),
}

TAX_BRACKETS = RuleTable(provenance=_TAX_BRACKET_PROV, rows=_TAX_BRACKET_ROWS)


def tax_brackets(filing_status: str = "single") -> tuple[TaxBracket, ...]:
    """The whole ordinary-income ladder for a filing status, ascending."""
    status = (filing_status or "single").strip().lower()
    if status not in _TAX_BRACKET_ROWS:
        raise ValueError(f"unknown filing status {filing_status!r}")
    return _TAX_BRACKET_ROWS[status]


def marginal_bracket(taxable_cents: int, filing_status: str = "single") -> TaxBracket:
    """The step this taxable income tops out in. Upper bounds are inclusive."""
    taxable = max(0, int(taxable_cents))
    ladder = tax_brackets(filing_status)
    for step in ladder:
        if step.upper_cents is None or taxable <= step.upper_cents:
            return step
    return ladder[-1]


def bracket_above(
    taxable_cents: int, filing_status: str = "single"
) -> Optional[TaxBracket]:
    """The next step up, or None when already in the top one."""
    ladder = tax_brackets(filing_status)
    here = marginal_bracket(taxable_cents, filing_status)
    for step in ladder:
        if step.lower_cents > here.lower_cents:
            return step
    return None


def tax_bracket_provenance() -> tuple[Provenance, ...]:
    """The table :func:`marginal_bracket` relies on."""
    return (_TAX_BRACKET_PROV,)


# ---------------------------------------------------------------------------
# Rule table: federal standard deduction
# ---------------------------------------------------------------------------
#
# The bracket ladder above is measured in TAXABLE income, which is what is left
# after this. Without the deduction here, a projected-income bar drawn against
# those brackets would be drawn against gross income and would place every
# household a step or two too high - the exact "right formula on the wrong
# number" failure the Provenance records exist to catch. It is indexed by the
# same Rev. Proc. as the brackets, so the two move together or not at all.

_STANDARD_DEDUCTION_PROV = Provenance(
    table="Federal standard deduction",
    effective_year=2026,
    volatility="indexed",
    publisher="IRS Rev. Proc. 2025-32",
    source="https://www.irs.gov/pub/irs-drop/rp-25-32.pdf",
    republished_by="11-01",
    checked_on="2026-09-23",
    note=(
        "Base amounts are IRC 63(c)(2). The age-65-or-blind add-on is IRC "
        "63(f) and is counted PER QUALIFYING CONDITION, so a married couple "
        "both over 65 gets it twice; an unmarried filer who is not a surviving "
        "spouse gets the larger amount. Head of household is not carried here "
        "because the bracket ladder does not carry it either."
    ),
)

#: Filing status -> the base standard deduction in cents, tax year 2026.
_STANDARD_DEDUCTION_ROWS: dict[str, int] = {
    "single": 1_610_000,
    "joint": 3_220_000,
    "separate": 1_610_000,
}

#: IRC 63(f), one per qualifying condition (age 65 or older, blind). The larger
#: amount is for an unmarried filer who is not a surviving spouse.
ADDITIONAL_DEDUCTION_UNMARRIED_CENTS = 205_000
ADDITIONAL_DEDUCTION_MARRIED_CENTS = 165_000

STANDARD_DEDUCTION = RuleTable(
    provenance=_STANDARD_DEDUCTION_PROV, rows=_STANDARD_DEDUCTION_ROWS
)


def standard_deduction(
    filing_status: str = "single", qualifying_conditions: int = 0
) -> int:
    """The standard deduction in cents for a filing status.

    ``qualifying_conditions`` counts IRC 63(f) conditions across the whole
    return - each spouse who is 65 or older, and each who is blind, counts one.
    It is a COUNT and not a pair of booleans because a joint return can claim it
    up to four times, and a caller that had to pass two flags per person would
    be the place that got that wrong.
    """
    status = (filing_status or "single").strip().lower()
    if status not in _STANDARD_DEDUCTION_ROWS:
        raise ValueError(f"unknown filing status {filing_status!r}")
    count = int(qualifying_conditions)
    if count < 0:
        raise ValueError("qualifying_conditions cannot be negative")
    extra = (
        ADDITIONAL_DEDUCTION_UNMARRIED_CENTS
        if status == "single"
        else ADDITIONAL_DEDUCTION_MARRIED_CENTS
    )
    return _STANDARD_DEDUCTION_ROWS[status] + extra * count


def taxable_after_deduction(
    gross_cents: int, filing_status: str = "single", qualifying_conditions: int = 0
) -> int:
    """Gross income less the standard deduction, floored at zero, in cents."""
    return max(
        0,
        int(gross_cents) - standard_deduction(filing_status, qualifying_conditions),
    )


def standard_deduction_provenance() -> tuple[Provenance, ...]:
    """The table :func:`standard_deduction` relies on."""
    return (_STANDARD_DEDUCTION_PROV,)


# ---------------------------------------------------------------------------
# Rule: the additional deduction for seniors, 2025-2028
# ---------------------------------------------------------------------------

_SENIOR_DEDUCTION_PROV = Provenance(
    table="Additional deduction for seniors (2025-2028)",
    effective_year=2025,
    volatility="volatile",
    publisher="Pub. L. 119-21 sec. 70103 (2025)",
    source="https://www.congress.gov/bill/119th-congress/house-bill/1/text",
    republished_by="12-31",
    checked_on="2026-09-26",
    note=(
        "For tax years 2025 through 2028 only: $6,000 for each individual 65 "
        "or older, itemizing or not, on top of the standard deduction and its "
        "age-65 add-on, reduced by 6 percent of modified AGI over $75,000 "
        "($150,000 on a joint return) - gone at $175,000 single, $250,000 "
        "joint with one senior, $350,000 with two. Married filers must file "
        "jointly. Not indexed. Volatile because it sunsets: whether Congress "
        "extends it is a question about Congress."
    ),
)

SENIOR_DEDUCTION_CENTS = 600_000
SENIOR_DEDUCTION_YEARS = (2025, 2028)
SENIOR_DEDUCTION_PHASEOUT_CENTS = {"single": 75_000_00, "joint": 150_000_00}
SENIOR_DEDUCTION_PHASEOUT_PCT = 6

SENIOR_DEDUCTION = RuleTable(
    provenance=_SENIOR_DEDUCTION_PROV,
    rows={"per_person_cents": SENIOR_DEDUCTION_CENTS,
          "first_year": SENIOR_DEDUCTION_YEARS[0],
          "last_year": SENIOR_DEDUCTION_YEARS[1],
          "phaseout_single_cents": SENIOR_DEDUCTION_PHASEOUT_CENTS["single"],
          "phaseout_joint_cents": SENIOR_DEDUCTION_PHASEOUT_CENTS["joint"],
          "phaseout_pct": SENIOR_DEDUCTION_PHASEOUT_PCT},
)


def senior_deduction_cents(filing_status: str, seniors: int, magi_cents: int,
                           year: int) -> int:
    """The senior deduction for ``seniors`` people 65 or older on a return of
    ``filing_status`` with ``magi_cents`` of modified AGI in ``year``: zero
    outside 2025-2028, on a separate return, or once phased out."""
    status = (filing_status or "single").strip().lower()
    first, last = SENIOR_DEDUCTION_YEARS
    if not first <= int(year) <= last or int(seniors) <= 0 or status == "separate":
        return 0
    full = SENIOR_DEDUCTION_CENTS * int(seniors)
    over = max(0, int(magi_cents)
               - SENIOR_DEDUCTION_PHASEOUT_CENTS.get(status, 75_000_00))
    return max(0, full - over * SENIOR_DEDUCTION_PHASEOUT_PCT // 100)


# ---------------------------------------------------------------------------
# Rule table: retirement contribution and catch-up limits
# ---------------------------------------------------------------------------
#
# These are ceilings on what a household may still put IN, which is the other
# half of a plan that is otherwise all about taking money out: the years before
# the first withdrawal are exactly the years a catch-up contribution is
# available, and the limit moves every year. Per person, not per account - one
# elective-deferral ceiling covers every employer plan a person is in, and one
# IRA ceiling covers traditional and Roth together.

_CONTRIBUTION_PROV = Provenance(
    table="Retirement contribution limits",
    effective_year=2026,
    volatility="indexed",
    publisher="IRS Notice 2025-67",
    source="https://www.irs.gov/pub/irs-drop/n-25-67.pdf",
    republished_by="11-15",
    checked_on="2026-09-23",
    note=(
        "Per person per year, not per account. The elective-deferral ceiling "
        "is shared across 401(k), 403(b), most 457(b) and the TSP, and a "
        "designated Roth deferral counts against the same ceiling; the IRA "
        "ceiling is shared between traditional and Roth. The larger 60-to-63 "
        "catch-up is SECURE 2.0 sec. 109 and does NOT apply at 64 or later. "
        "Deferral plus match is capped by the annual-additions limit (IRC "
        "415(c)), the catch-up on top; and from 2026 a catch-up by someone "
        "whose prior-year FICA wages exceeded $150,000 must be a Roth "
        "deferral (SECURE 2.0 sec. 603), so it is not pre-tax. Indexed here "
        "at the bracket rate past the table year."
    ),
)

#: SECURE 2.0 sec. 603: the prior-year wage line above which a catch-up must
#: be Roth. Cents, 2026 ($145,000 indexed).
CONTRIBUTION_ROTH_CATCH_UP_WAGES_CENTS = 15_000_000
CONTRIBUTION_ROTH_CATCH_UP_FROM = 2026


@dataclass(frozen=True)
class ContributionLimit:
    """One account kind's annual ceiling for one person, in cents."""

    limit_cents: int
    catch_up_cents: int                          # age 50 and over
    catch_up_60_to_63_cents: Optional[int]       # None where SECURE 2.0 sec. 109
                                                 # does not reach this kind

    def for_age(self, age: int) -> int:
        """The most this person may contribute at the age they attain this year."""
        attained = int(age)
        if attained < 50:
            return self.limit_cents
        if self.catch_up_60_to_63_cents is not None and 60 <= attained <= 63:
            return self.limit_cents + self.catch_up_60_to_63_cents
        return self.limit_cents + self.catch_up_cents


#: Account kind -> the ceiling. Cents, tax year 2026.
_CONTRIBUTION_ROWS: dict[str, ContributionLimit] = {
    # 401(k), 403(b), governmental 457(b), TSP - one shared ceiling.
    "elective_deferral": ContributionLimit(2_450_000, 800_000, 1_125_000),
    # Traditional and Roth IRA together. No 60-to-63 step for an IRA.
    "ira": ContributionLimit(750_000, 110_000, None),
    # IRC 415(c): every addition to a plan in a year - deferral, match, other
    # employer money - with the catch-up excluded from it (on top).
    "annual_additions": ContributionLimit(7_200_000, 800_000, 1_125_000),
}

CONTRIBUTION_KINDS = tuple(_CONTRIBUTION_ROWS)

CONTRIBUTION_LIMITS = RuleTable(
    provenance=_CONTRIBUTION_PROV, rows=_CONTRIBUTION_ROWS
)


def contribution_limit(kind: str = "elective_deferral") -> ContributionLimit:
    """The ceiling for an account kind: ``elective_deferral`` or ``ira``."""
    key = (kind or "elective_deferral").strip().lower()
    if key not in _CONTRIBUTION_ROWS:
        raise ValueError(
            f"unknown contribution kind {kind!r}; have {sorted(_CONTRIBUTION_ROWS)}"
        )
    return _CONTRIBUTION_ROWS[key]


def contribution_provenance() -> tuple[Provenance, ...]:
    """The table :func:`contribution_limit` relies on."""
    return (_CONTRIBUTION_PROV,)


def capped_contribution_cents(salary_cents: int, deferral_pct, match_pct, age: int,
                              year: int, index_pct, *,
                              prior_wages_cents: int = 0) -> tuple[int, int, int]:
    """(pre-tax deferral, Roth catch-up, employer match) a salary puts into its
    plan in ``year``: the deferral percentage capped at the elective-deferral
    limit plus the catch-up for ``age`` (attained in the year), the match then
    capped by the annual-additions limit, and the catch-up turned Roth - not
    pre-tax, same account - when the prior year's wages were over the
    SECURE 2.0 line. Reported: a high deferral percentage projected money no
    plan will accept, and took it off taxable income too."""
    salary = max(0, int(salary_cents))
    wanted = _pct_of(salary, deferral_pct)
    match = _pct_of(salary, match_pct)
    elective = contribution_limit("elective_deferral")
    additions = contribution_limit("annual_additions")
    base = indexed_cents(elective.limit_cents, index_pct, year)
    catch_up = indexed_cents(elective.for_age(age) - elective.limit_cents, index_pct, year)
    deferral = min(wanted, base + catch_up)
    catch_up_used = max(0, deferral - base)
    overall = indexed_cents(additions.for_age(age), index_pct, year)
    match = min(match, max(0, overall - deferral))
    roth = (catch_up_used
            if int(year) >= CONTRIBUTION_ROTH_CATCH_UP_FROM
            and int(prior_wages_cents) > indexed_cents(
                CONTRIBUTION_ROTH_CATCH_UP_WAGES_CENTS, index_pct, year)
            else 0)
    return deferral - roth, roth, match


# ---------------------------------------------------------------------------
# Rule: the ACA premium-tax-credit income cliff
# ---------------------------------------------------------------------------
#
# A Roth conversion raises household income, and above this line the premium tax
# credit does not taper - it stops. One dollar over can cost a household a whole
# year of subsidy, which is why the multiplier belongs beside the poverty
# guidelines it multiplies rather than inside whichever screen happens to draw
# the line. It was a bare constant in a conversions dialog once; that is exactly
# how a figure ends up updated in one place and stale in another.

_ACA_CLIFF_PROV = Provenance(
    table="ACA premium tax credit income cliff",
    effective_year=2026,
    volatility="volatile",
    publisher="IRC 36B(c)(1)(A)",
    source="https://www.law.cornell.edu/uscode/text/26/36B",
    republished_by="11-01",
    checked_on="2026-09-23",
    note=(
        "The 400%-of-poverty ceiling is statutory and has not been reindexed, "
        "but ARPA and the Inflation Reduction Act suspended it for 2021-2025 "
        "and that suspension lapsed, so the cliff is back for coverage year "
        "2026. Whether it is in force is a question about Congress, not about "
        "indexing - re-check before each open enrollment. Eligibility is "
        "measured against the PRIOR year's poverty guidelines."
    ),
)

#: IRC 36B(c)(1)(A): the credit stops above this multiple of the poverty line.
ACA_CLIFF_MULTIPLE = 4

ACA_PREMIUM_CREDIT_CLIFF = RuleTable(
    provenance=_ACA_CLIFF_PROV, rows={"multiple_of_poverty_level": ACA_CLIFF_MULTIPLE}
)


def aca_cliff_cents(household_size: int, region: str = "contiguous") -> int:
    """Household income at which the premium tax credit stops, in cents."""
    return federal_poverty_level(household_size, region) * ACA_CLIFF_MULTIPLE


def aca_cliff_provenance() -> tuple[Provenance, ...]:
    """Both tables :func:`aca_cliff_cents` relies on - the multiple and the line."""
    return (_ACA_CLIFF_PROV, _FPL_PROV)


_ACA_PCT_PROV = Provenance(
    table="ACA premium tax credit applicable percentages",
    effective_year=2026,
    volatility="indexed",
    publisher="IRS Rev. Proc. 2025-25 (IRC 36B(b)(3)(A))",
    source="https://www.irs.gov/pub/irs-drop/rp-25-25.pdf",
    republished_by="08-01",
    checked_on="2026-09-26",
    note=(
        "The share of household income a family is expected to pay toward "
        "the benchmark (second-lowest-cost silver) plan, by income as a "
        "multiple of the poverty line, rising within each band; the credit is "
        "the benchmark premium less that share - nothing under 100 percent "
        "(Medicaid) and nothing over 400 (the cliff). The 2021-2025 "
        "enhancement that capped the share at 8.5 percent and reached past "
        "400 percent lapsed; these are the underlying percentages, reindexed "
        "yearly, held here at the 2026 figures."
    ),
)

#: (from % of poverty, to %, share at from, share at to) - linear between.
#: Rev. Proc. 2025-25 section 2.01, for 2026.
_ACA_PCT_ROWS: tuple[tuple[int, int, Decimal, Decimal], ...] = (
    (100, 133, Decimal("2.10"), Decimal("2.10")),
    (133, 150, Decimal("3.14"), Decimal("4.19")),
    (150, 200, Decimal("4.19"), Decimal("6.60")),
    (200, 250, Decimal("6.60"), Decimal("8.44")),
    (250, 300, Decimal("8.44"), Decimal("9.96")),
    (300, 400, Decimal("9.96"), Decimal("9.96")),
)

ACA_APPLICABLE_PERCENTAGES = RuleTable(
    provenance=_ACA_PCT_PROV,
    rows={f"{lo}_to_{hi}_pct_of_poverty": (low, high)
          for lo, hi, low, high in _ACA_PCT_ROWS},
)


def aca_applicable_pct(magi_cents: int, poverty_line_cents: int) -> Optional[Decimal]:
    """The share of income expected toward the benchmark plan, or None when
    no credit is available (under 100 or over 400 percent of poverty)."""
    if int(poverty_line_cents) <= 0:
        return None
    ratio = Decimal(max(0, int(magi_cents))) * 100 / Decimal(int(poverty_line_cents))
    for lo, hi, low, high in _ACA_PCT_ROWS:
        if lo <= ratio <= hi:
            return low + (high - low) * (ratio - lo) / (hi - lo)
    return None


def aca_premium_credit_cents(benchmark_cents: int, magi_cents: int,
                             poverty_line_cents: int) -> int:
    """The year's premium tax credit: the benchmark premium less the
    applicable share of income, floored at zero; zero where no credit is
    available."""
    pct = aca_applicable_pct(magi_cents, poverty_line_cents)
    if pct is None:
        return 0
    share = Decimal(max(0, int(magi_cents))) * pct / 100
    credit = Decimal(max(0, int(benchmark_cents))) - share
    return max(0, int(credit.quantize(Decimal(1), rounding=ROUND_HALF_UP)))


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

RELATIONSHIPS = ("self", "spouse", "child", "other")

#: Health columns on ``people``. ``mammon/mcp_tools.py`` builds its denial set
#: from this tuple, so adding a column here is what makes it unreachable from
#: MCP - and test_mcp_tools.py fails if a column on ``people`` is neither
#: listed here nor in :data:`PEOPLE_PLAIN_COLUMNS`, so a new one cannot slip
#: through unclassified.
HEALTH_COLUMNS: tuple[str, ...] = (
    "smoker",
    "bmi_band",
    "diabetes",
    "major_conditions",
    "family_history",
)

#: Everything else on ``people``: ordinary, not sensitive, safe over MCP.
PEOPLE_PLAIN_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "relationship",
    "birth_month",
    "birth_year",
    "born_on_the_first",
    "planned_claim_age_months",
    "ss_statement_date",
    "part_d",
    "created_at",
)

_PERSON_WRITABLE = (
    "name",
    "relationship",
    "birth_month",
    "birth_year",
    "born_on_the_first",
    "planned_claim_age_months",
    "ss_statement_date",
    "part_d",
) + HEALTH_COLUMNS


def _check_person(values: Mapping) -> None:
    if "relationship" in values:
        rel = values["relationship"]
        if rel is not None and rel not in RELATIONSHIPS:
            raise ValueError(f"relationship must be one of {RELATIONSHIPS}, got {rel!r}")
    month = values.get("birth_month")
    if month is not None and not 1 <= int(month) <= 12:
        raise ValueError(f"birth_month must be 1-12, got {month!r}")
    name = values.get("name")
    if "name" in values and not (name or "").strip():
        raise ValueError("a person needs a name")
    claim = values.get("planned_claim_age_months")
    if claim is not None and not (
        SS_EARLIEST_CLAIM_MONTHS <= int(claim) <= SS_LATEST_CREDIT_MONTHS
    ):
        raise ValueError(
            "planned_claim_age_months must be between "
            f"{SS_EARLIEST_CLAIM_MONTHS} (62) and {SS_LATEST_CREDIT_MONTHS} (70), "
            f"got {claim!r}"
        )
    statement = values.get("ss_statement_date")
    if statement:
        _dt.date.fromisoformat(str(statement))  # ISO YYYY-MM-DD or raise


def add_person(conn, name: str, relationship: str = "other", **fields) -> int:
    """Insert a person and return the new id. Health fields are optional kwargs."""
    unknown = set(fields) - set(_PERSON_WRITABLE)
    if unknown:
        raise ValueError(f"unknown person field(s): {sorted(unknown)}")
    values = dict(fields)
    values["name"] = name
    values["relationship"] = relationship
    _check_person(values)
    if "born_on_the_first" in values:
        values["born_on_the_first"] = 1 if values["born_on_the_first"] else 0
    cols = [c for c in _PERSON_WRITABLE if c in values]
    sql = (
        f"INSERT INTO people ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})"
    )
    cur = conn.execute(sql, [values[c] for c in cols])
    conn.commit()
    return int(cur.lastrowid)


def update_person(conn, person_id: int, **fields) -> None:
    """Change any subset of a person's fields. Unmentioned fields are untouched."""
    unknown = set(fields) - set(_PERSON_WRITABLE)
    if unknown:
        raise ValueError(f"unknown person field(s): {sorted(unknown)}")
    if not fields:
        return
    _check_person(fields)
    values = dict(fields)
    if "born_on_the_first" in values:
        values["born_on_the_first"] = 1 if values["born_on_the_first"] else 0
    cols = [c for c in _PERSON_WRITABLE if c in values]
    sql = f"UPDATE people SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?"
    conn.execute(sql, [values[c] for c in cols] + [int(person_id)])
    conn.commit()


def get_person(conn, person_id: int) -> Optional[dict]:
    """One person as a plain dict, or None."""
    row = conn.execute("SELECT * FROM people WHERE id = ?", (int(person_id),)).fetchone()
    return dict(row) if row is not None else None


def list_people(conn, relationship: Optional[str] = None) -> list[dict]:
    """Everyone in the household, self first, then by name."""
    sql = (
        "SELECT * FROM people "
        "{where}"
        "ORDER BY CASE relationship WHEN 'self' THEN 0 WHEN 'spouse' THEN 1 "
        "         WHEN 'child' THEN 2 ELSE 3 END, name, id"
    )
    if relationship:
        if relationship not in RELATIONSHIPS:
            raise ValueError(f"relationship must be one of {RELATIONSHIPS}")
        rows = conn.execute(
            sql.format(where="WHERE relationship = ? "), (relationship,)
        ).fetchall()
    else:
        rows = conn.execute(sql.format(where="")).fetchall()
    return [dict(r) for r in rows]


def delete_person(conn, person_id: int) -> None:
    conn.execute("DELETE FROM people WHERE id = ?", (int(person_id),))
    conn.commit()


def attainment_year_month(person: Mapping, age: int) -> Optional[tuple[int, int]]:
    """The (year, month) in which a person attains ``age``, or None if unknown.

    SSA and CMS follow the common-law rule that a person attains an age on the
    day BEFORE the anniversary of their birth, so someone born on the first of
    a month attains it in the PRIOR month - which is the whole reason
    ``born_on_the_first`` is stored. See 20 CFR 404.2(c)(4) and SSA POMS
    RS 00615.015.
    """
    year, month = person.get("birth_year"), person.get("birth_month")
    if year is None or month is None:
        return None
    y, m = int(year) + int(age), int(month)
    if person.get("born_on_the_first"):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return (y, m)


def retirement_year(person: Mapping) -> Optional[int]:
    """The calendar year a person reaches their planned claim age, or None.

    The planned Social Security claim age (set in the Social Security dialog) is
    the one retirement age the household states anywhere, so it is what stops
    contributions in a projection. A second "retirement age" field would be a
    second place to say the same thing, free to disagree with the first.
    """
    months = person.get("planned_claim_age_months")
    year = person.get("birth_year")
    if months is None or year is None:
        return None
    month = int(person.get("birth_month") or 1)
    if person.get("born_on_the_first"):
        month -= 1
    return (int(year) * 12 + (month - 1) + int(months)) // 12


# ---------------------------------------------------------------------------
# Social Security earnings records
# ---------------------------------------------------------------------------
#
# One row per person per year, carrying WHERE the figure came from. The source
# is not decoration: a benefit built on typed SSA figures and a benefit built
# on Mammon's reading of a checking account are different claims about the
# world, and the screen showing either one has to be able to say which it is
# without guessing.

#: What produced a year's earnings figure.
#:
#: ``reported``  - typed off the SSA Earnings Report. SSA has already capped it
#:                 at that year's taxable maximum and counted only covered
#:                 wages, so it needs no interpretation.
#: ``estimated`` - summed by Mammon from wage-category inflows in this ledger.
#: ``projected`` - a year the user expects to earn but has not yet.
EARNINGS_SOURCES = ("reported", "estimated", "projected")

#: Category-name fragments that look like covered wages, used only when the
#: caller does not name the categories itself. Matched case-insensitively
#: against income categories and their descendants.
WAGE_CATEGORY_HINTS = ("salary", "wage", "paycheck", "payroll", "bonus", "commission")


def set_earnings(
    conn, person_id: int, year: int, earnings_cents: int, source: str = "reported"
) -> None:
    """Record (or replace) one year of earnings for a person.

    The amount is a MAGNITUDE in cents, like every other stored plan figure:
    an earnings record has no outflows, and a signed one would let a negative
    year quietly lower an AIME.
    """
    if source not in EARNINGS_SOURCES:
        raise ValueError(f"source must be one of {EARNINGS_SOURCES}, got {source!r}")
    amount = _magnitude(earnings_cents)
    conn.execute(
        "INSERT INTO person_earnings (person_id, year, earnings_cents, source) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(person_id, year) DO UPDATE SET "
        "earnings_cents = excluded.earnings_cents, source = excluded.source",
        (int(person_id), int(year), amount, source),
    )
    conn.commit()


def delete_earnings(conn, person_id: int, year: int) -> None:
    conn.execute(
        "DELETE FROM person_earnings WHERE person_id = ? AND year = ?",
        (int(person_id), int(year)),
    )
    conn.commit()


def list_earnings(conn, person_id: int) -> list[dict]:
    """Every recorded year for a person, oldest first."""
    rows = conn.execute(
        "SELECT year, earnings_cents, source FROM person_earnings "
        "WHERE person_id = ? ORDER BY year",
        (int(person_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def earnings_map(conn, person_id: int) -> dict[int, int]:
    """The year -> cents mapping :func:`monthly_benefit` wants."""
    return {int(r["year"]): int(r["earnings_cents"]) for r in list_earnings(conn, person_id)}


def replace_earnings(
    conn, person_id: int, rows: Mapping[int, int], source: str = "estimated"
) -> None:
    """Overwrite every year of one source with a fresh set, leaving others alone.

    Re-estimating from the ledger must not touch a year the user typed off an
    SSA statement, and must not leave behind an estimated year that this run no
    longer finds any wages in. So a year already held by a DIFFERENT source is
    skipped entirely: the user's own figure outranks anything Mammon inferred,
    and silently promoting a typed year to an estimate would throw away the one
    number in the table that came from SSA itself.
    """
    if source not in EARNINGS_SOURCES:
        raise ValueError(f"source must be one of {EARNINGS_SOURCES}, got {source!r}")
    conn.execute(
        "DELETE FROM person_earnings WHERE person_id = ? AND source = ?",
        (int(person_id), source),
    )
    held = {
        int(r["year"])
        for r in conn.execute(
            "SELECT year FROM person_earnings WHERE person_id = ?",
            (int(person_id),),
        ).fetchall()
    }
    for year, cents in sorted(rows.items()):
        if int(year) in held:
            continue
        conn.execute(
            "INSERT INTO person_earnings (person_id, year, earnings_cents, source) "
            "VALUES (?, ?, ?, ?)",
            (int(person_id), int(year), _magnitude(cents), source),
        )
    conn.commit()


def describe_earnings_basis(rows: Iterable[Mapping]) -> str:
    """One sentence saying which basis is in force, for printing above the table.

    Both shapes are offered - type the SSA Earnings Report, or have
    Mammon read the ledger - and the two produce visibly different numbers.
    Whichever is in force has to be on screen beside the benefit, not a mode
    the user has to remember setting.
    """
    items = [dict(r) for r in rows]
    if not items:
        return (
            "No earnings history yet. Type the years from your SSA Earnings "
            "Report, or estimate them from this ledger's wage categories."
        )
    counts: dict[str, list[int]] = {}
    for item in items:
        counts.setdefault(str(item["source"]), []).append(int(item["year"]))
    parts = []
    for source in EARNINGS_SOURCES:
        years = sorted(counts.get(source, []))
        if not years:
            continue
        span = f"{years[0]}-{years[-1]}" if len(years) > 1 else f"{years[0]}"
        label = {
            "reported": "from your SSA Earnings Report",
            "estimated": "estimated by Mammon from this ledger",
            "projected": "projected by you",
        }[source]
        parts.append(f"{len(years)} year{'s' if len(years) != 1 else ''} {label} ({span})")
    return "Basis: " + "; ".join(parts) + "."


@dataclass(frozen=True)
class EarningsEstimate:
    """What the ledger says a person earned, plus everything it assumed."""

    rows: dict[int, int]
    category_names: tuple[str, ...]
    capped_years: tuple[int, ...] = ()
    method: str = ""

    def assumptions(self) -> tuple[str, ...]:
        """The lines a screen must print beside any figure derived from this."""
        if not self.category_names:
            return (
                "No income category here looks like wages (tried: "
                + ", ".join(WAGE_CATEGORY_HINTS)
                + "). Nothing was estimated - name the categories yourself, or "
                "type the years from your SSA Earnings Report.",
            )
        lines = [
            "Summed from deposits categorized " + ", ".join(self.category_names)
            + ", and their subcategories.",
            "Transfers and scheduled pre-entries are excluded; only money IN counts.",
            "This is gross pay as it landed in the ledger. SSA counts covered wages "
            "before deductions, so a year with a 401(k) deferral or a pre-tax "
            "premium reads LOW here - correct any year by typing over it.",
        ]
        if self.capped_years:
            lines.append(
                "Cut to that year's Social Security taxable maximum in: "
                + ", ".join(str(y) for y in self.capped_years)
                + "."
            )
        return tuple(lines)


def wage_category_ids(conn, names: Optional[Iterable[str]] = None) -> dict[int, str]:
    """Category ids that look like covered wages -> the name they matched under.

    Walks the category tree itself rather than calling ``mammon/reports`` -
    the domain layer does not import upward. The duplication is three lines of
    tree walk, and it is the price of the layering.
    """
    rows = [
        dict(r)
        for r in conn.execute("SELECT id, name, parent_id, type FROM categories").fetchall()
    ]
    hints = tuple(str(n).lower() for n in (names or WAGE_CATEGORY_HINTS))
    by_parent: dict[Optional[int], list[dict]] = {}
    for row in rows:
        by_parent.setdefault(row["parent_id"], []).append(row)
    roots = [
        r
        for r in rows
        if any(h in str(r["name"]).lower() for h in hints)
        and (r["type"] is None or r["type"] == "income")
    ]
    found: dict[int, str] = {}
    for root in roots:
        stack = [root]
        while stack:
            node = stack.pop()
            found.setdefault(int(node["id"]), str(root["name"]))
            stack.extend(by_parent.get(node["id"], []))
    return found


def estimate_earnings_from_ledger(
    conn,
    category_names: Optional[Iterable[str]] = None,
    first_year: Optional[int] = None,
    last_year: Optional[int] = None,
) -> EarningsEstimate:
    """Wage income per calendar year, read out of this ledger.

    A deliberate approximation of an SSA Earnings Report, for the user who has
    not got the statement in front of him. It counts positive amounts posted to
    wage-looking income categories (and their splits), excludes transfers and
    scheduled rows, and caps each year at the taxable maximum. Every one of
    those choices is an assumption, and :meth:`EarningsEstimate.assumptions`
    exists so a screen cannot show the result without showing them.
    """
    matched = wage_category_ids(conn, category_names)
    if not matched:
        return EarningsEstimate(rows={}, category_names=())
    marks = ", ".join("?" * len(matched))
    params = list(matched)
    clauses = ["t.scheduled = 0", "t.transfer_account_id IS NULL"]
    if first_year:
        clauses.append("t.date >= ?")
        params.append(f"{int(first_year)}-01-01")
    if last_year:
        clauses.append("t.date <= ?")
        params.append(f"{int(last_year)}-12-31")
    where = " AND ".join(clauses)
    totals: dict[int, int] = {}
    direct = conn.execute(
        f"SELECT CAST(strftime('%Y', t.date) AS INTEGER) AS y, SUM(t.amount) AS total "
        f"FROM transactions t WHERE t.category_id IN ({marks}) AND {where} "
        f"AND t.amount > 0 GROUP BY y",
        params,
    ).fetchall()
    split = conn.execute(
        f"SELECT CAST(strftime('%Y', t.date) AS INTEGER) AS y, SUM(s.amount) AS total "
        f"FROM splits s JOIN transactions t ON t.id = s.transaction_id "
        f"WHERE s.category_id IN ({marks}) AND {where} AND s.amount > 0 GROUP BY y",
        params,
    ).fetchall()
    for row in list(direct) + list(split):
        if row["y"] is None:
            continue
        totals[int(row["y"])] = totals.get(int(row["y"]), 0) + int(row["total"] or 0)
    capped = {}
    hit = []
    for year, cents in sorted(totals.items()):
        cut = capped_earnings(year, cents)
        if cut != cents:
            hit.append(year)
        capped[year] = cut
    names = tuple(sorted(set(matched.values())))
    return EarningsEstimate(
        rows=capped,
        category_names=names,
        capped_years=tuple(hit),
        method="positive amounts in wage income categories, transfers excluded",
    )


# ---------------------------------------------------------------------------
# The plan: the accounts it is made of, and who owns them
# ---------------------------------------------------------------------------

#: The tax treatments that make an account a retirement account for the plan.
PLAN_TREATMENTS = ("deferred", "roth")


@dataclass(frozen=True)
class PlanAccount:
    """One retirement account as the plan sees it.

    Lives here rather than on a screen because two screens disagreeing about
    which accounts are "retirement" would put a draw in the plan that never
    appears on a chart.
    """

    account_id: int                     # NEGATIVE for a planned account
    name: str
    treatment: str                      # "deferred" or "roth"
    current_employer_plan: bool = False
    owner_person_id: Optional[int] = None
    institution: str = ""
    is_ira: bool = False                # an IRA, whose minimums aggregate (db._V106)
    inherited_death_year: Optional[int] = None   # inherited from a non-spouse
    inherited_after_rbd: bool = False   # ...who had begun required distributions
    five_percent_owner: bool = False    # no still-working exception (IRC 416(i))
    after_tax_basis_cents: int = 0      # nondeductible contributions (Form 8606)

    @property
    def planned(self) -> bool:
        """A hypothetical Roth that exists only in the plan (see
        :func:`conversion_target`): it holds nothing today and has no ledger
        row, so anything that values or names it must not go to ``accounts``."""
        return is_planned(self.account_id)

    @property
    def is_roth(self) -> bool:
        return self.treatment == "roth"

    @property
    def is_taxable(self) -> bool:
        """A taxable brokerage account the plan may spend (``taxable_accounts``):
        not a retirement account, never converted, never owing a minimum."""
        return self.treatment == "taxable"

    def employer_until_for(self, employer_until) -> Optional[int]:
        if isinstance(employer_until, Mapping):
            return employer_until.get(self.account_id)
        return employer_until

    def floored_in(self, year: int, employer_until=None) -> bool:
        """:attr:`floored`, for one year. A current employer's plan stops being
        one when the household retires: from ``employer_until`` (the plan's
        start year) it is a former employer's plan and owes minimums.
        Reported: a current employer's plan stayed "current" to age 120.
        An inherited account - a Roth too - follows the beneficiary rules
        (:func:`inherited_floored_in`)."""
        if self.inherited_death_year is not None:
            return inherited_floored_in(self.inherited_death_year,
                                        self.inherited_after_rbd, year)
        if self.treatment != "deferred":
            return False
        if not self.current_employer_plan or self.five_percent_owner:
            return True
        until = self.employer_until_for(employer_until)
        return until is not None and int(year) >= int(until)

    def first_floored_year(self, birth_year: int, employer_until=None) -> Optional[int]:
        """The first distribution calendar year of an account the person owns:
        the applicable-age year, or for a current employer's plan the later of
        that and the year the job ends. None when it never is."""
        if self.treatment != "deferred" or self.inherited_death_year is not None:
            return None
        year = int(birth_year) + applicable_age(int(birth_year))
        if self.current_employer_plan and not self.five_percent_owner:
            until = self.employer_until_for(employer_until)
            if until is None:
                return None
            year = max(year, int(until))
        return year

    @property
    def floored(self) -> bool:
        """Whether this account's withdrawal cells carry an RMD floor.

        A Roth account has no lifetime RMD (IRC 408A(c)(4)). A CURRENT
        employer's plan is covered by the still-working exception while the user
        works there (IRC 401(a)(9)(C)(i)(II)) - unless they own 5% of the
        employer (IRC 416(i)); a former employer's plan and a traditional IRA
        are not. An inherited account is, on its own schedule.
        """
        if self.inherited_death_year is not None:
            return True
        return self.treatment == "deferred" and (not self.current_employer_plan
                                                 or self.five_percent_owner)

    @property
    def label(self) -> str:
        if self.is_roth:
            return f"{self.name} (Roth)"
        if self.current_employer_plan:
            return f"{self.name} (current employer plan)"
        return f"{self.name} (tax-deferred)"


def plan_accounts(conn) -> list[PlanAccount]:
    """Every retirement account, tax-deferred first then by name.

    ``mammon.ledger`` and ``mammon.rebalance`` are imported inside the function:
    both are peers of this module, and a top-level import would make an import
    cycle out of what is only a read of the account list.
    """
    from mammon import ledger, rebalance

    out: list[PlanAccount] = []
    for acct in ledger.list_accounts(conn):
        treatment = rebalance.account_treatment(acct)
        if treatment not in PLAN_TREATMENTS:
            continue
        out.append(PlanAccount(
            account_id=int(acct["id"]),
            name=acct["name"],
            treatment=treatment,
            current_employer_plan=bool(_column(acct, "current_employer_plan")),
            owner_person_id=_optional_int(_column(acct, "owner_person_id")),
            institution=_institution(_column(acct, "institution")),
            is_ira=bool(_column(acct, "is_ira")),
            inherited_death_year=_optional_int(_column(acct, "inherited_death_year")),
            inherited_after_rbd=bool(_column(acct, "inherited_after_rbd")),
            five_percent_owner=bool(_column(acct, "five_percent_owner")),
            after_tax_basis_cents=int(_column(acct, "after_tax_basis_cents") or 0),
        ))
    out.extend(planned_accounts(conn))
    out.sort(key=lambda a: (a.is_roth, a.name))
    return out


# ---------------------------------------------------------------------------
# Planned (hypothetical) Roth accounts
# ---------------------------------------------------------------------------
#
# A conversion lands in a Roth of the same owner at the same institution, and
# when the ledger has none the plan invents one (see db._V82 for why it is not an
# accounts row). Everywhere above the storage functions a planned account is
# keyed by its NEGATED id, so the planner's int-keyed maps, projections and
# PlanAccount lists carry it with no second key type; real account ids are
# always positive, so the sign alone says which table a key belongs to. Only the
# functions in this module that touch retirement_withdrawals and
# retirement_conversions translate between the two.


def is_planned(account_key: int) -> bool:
    return int(account_key) < 0


def _institution(value) -> str:
    return " ".join(str(value or "").split())


def _same_institution(left: str, right: str) -> bool:
    return _institution(left).casefold() == _institution(right).casefold()


def planned_account_name(conn, owner_person_id: Optional[int], institution: str) -> str:
    """"<Custodian> Roth - <owner> (planned)": what the planner shows for one."""
    where = _institution(institution) or "New"
    owner = get_person(conn, owner_person_id) if owner_person_id else None
    who = f" - {owner['name']}" if owner else ""
    return f"{where} Roth{who} (planned)"


def planned_accounts(conn) -> list[PlanAccount]:
    rows = conn.execute(
        "SELECT id, owner_person_id, institution FROM retirement_planned_accounts "
        "ORDER BY institution, id"
    ).fetchall()
    return [PlanAccount(
        account_id=-int(r["id"]),
        name=planned_account_name(conn, _optional_int(r["owner_person_id"]),
                                  r["institution"]),
        treatment="roth",
        owner_person_id=_optional_int(r["owner_person_id"]),
        institution=_institution(r["institution"]),
    ) for r in rows]


def _find_planned(conn, owner_person_id: Optional[int], institution: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM retirement_planned_accounts "
        " WHERE COALESCE(owner_person_id, 0) = ? AND institution = ?",
        (int(owner_person_id or 0), _institution(institution)),
    ).fetchone()
    return None if row is None else -int(row["id"])


def resolve_conversion_target(conn, from_account_id: int
                              ) -> tuple[Optional[int], str]:
    """Where a conversion out of ``from_account_id`` lands, without writing.

    Returns ``(key, name)``. The rule is the custodian's: a Roth of the SAME
    owner at the SAME institution - an existing ledger account first, then a
    planned one already on file. ``key`` is None when neither exists yet, and
    ``name`` then names the planned Roth :func:`conversion_target` would create.
    Owner equality here is strict (unknown matches only unknown), unlike
    :func:`same_owner`: routing money INTO an account is a stronger claim than
    permitting a pairing the user typed.
    """
    source = next((a for a in plan_accounts(conn)
                   if a.account_id == int(from_account_id)), None)
    if source is None or source.is_roth:
        return None, ""
    for acct in plan_accounts(conn):
        if (acct.is_roth and not acct.planned
                and acct.owner_person_id == source.owner_person_id
                and _same_institution(acct.institution, source.institution)):
            return acct.account_id, acct.name
    key = _find_planned(conn, source.owner_person_id, source.institution)
    name = planned_account_name(conn, source.owner_person_id, source.institution)
    return key, name


def conversion_target(conn, from_account_id: int) -> Optional[int]:
    """:func:`resolve_conversion_target`, creating the planned Roth if needed."""
    key, _name = resolve_conversion_target(conn, from_account_id)
    if key is not None:
        return key
    source = next((a for a in plan_accounts(conn)
                   if a.account_id == int(from_account_id)), None)
    if source is None or source.is_roth:
        return None
    cur = conn.execute(
        "INSERT INTO retirement_planned_accounts (owner_person_id, institution) "
        "VALUES (?, ?)",
        (source.owner_person_id, _institution(source.institution)),
    )
    conn.commit()
    return -int(cur.lastrowid)


def prune_planned_accounts(conn) -> None:
    """Drop every planned Roth nothing converts into any more.

    A planned account exists only to receive conversions; once none are left it
    is a name in the planner for money that will never arrive. Its planned
    withdrawals go with it (ON DELETE CASCADE).
    """
    conn.execute(
        "DELETE FROM retirement_planned_accounts WHERE id NOT IN "
        "(SELECT to_planned_id FROM retirement_conversions "
        "  WHERE to_planned_id IS NOT NULL)"
    )
    conn.commit()


def taxable_accounts(conn) -> list[PlanAccount]:
    """The taxable brokerage accounts the household can spend in retirement:
    open, visible accounts whose tax treatment is explicitly 'taxable'. An
    account with no treatment is left out - a college fund or a business
    account must not be spent because nobody labelled it."""
    from mammon import ledger, rebalance

    out = [PlanAccount(account_id=int(acct["id"]), name=acct["name"],
                       treatment="taxable",
                       owner_person_id=_optional_int(_column(acct, "owner_person_id")),
                       institution=_institution(_column(acct, "institution")))
           for acct in ledger.list_accounts(conn)
           if rebalance.account_treatment(acct) == "taxable"]
    out.sort(key=lambda a: a.name)
    return out


def spending_accounts(conn) -> list[PlanAccount]:
    """Everything the household plan may draw from: the retirement accounts
    and the taxable ones."""
    return plan_accounts(conn) + taxable_accounts(conn)


def _column(row, name: str):
    """One column of a row that may predate the column. None when absent."""
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


def _optional_int(value) -> Optional[int]:
    return None if value is None else int(value)


def set_account_owner(conn, account_id: int, person_id: Optional[int]) -> None:
    """Record which household member owns an account, or clear it with None.

    An owner is what makes the no-spousal-conversion rule checkable; an account
    with no owner recorded is "unknown", not "mine".
    """
    conn.execute(
        "UPDATE accounts SET owner_person_id = ? WHERE id = ?",
        (None if person_id is None else int(person_id), int(account_id)),
    )
    conn.commit()


def account_owner(conn, account_id: int) -> Optional[int]:
    """The person id on file for an account, or None when nobody is recorded."""
    if is_planned(account_id):
        row = conn.execute(
            "SELECT owner_person_id FROM retirement_planned_accounts WHERE id = ?",
            (-int(account_id),)).fetchone()
    else:
        row = conn.execute(
            "SELECT owner_person_id FROM accounts WHERE id = ?", (int(account_id),)
        ).fetchone()
    if row is None:
        return None
    return _optional_int(row["owner_person_id"])


def same_owner(conn, first_account_id: int, second_account_id: int) -> bool:
    """Whether two accounts may be treated as one person's for a conversion.

    True when both owners are recorded and equal, and true when either is
    unknown: a ledger that predates the owner column must keep working, and
    refusing every conversion until the user labels two accounts would be a
    rule Mammon invented rather than one the Code imposes.
    """
    left = account_owner(conn, first_account_id)
    right = account_owner(conn, second_account_id)
    if left is None or right is None:
        return True
    return left == right


# ---------------------------------------------------------------------------
# The plan: withdrawals and conversions
# ---------------------------------------------------------------------------


def _magnitude(amount_cents: int) -> int:
    value = int(amount_cents)
    if value < 0:
        raise ValueError(
            "plan amounts are stored as magnitudes in cents; direction is carried "
            "by the table, not by a sign"
        )
    return value


def set_withdrawal(conn, account_id: int, year: int, amount_cents: int) -> None:
    """Plan a distribution out of ``account_id`` in ``year``. Replaces any prior one.

    An explicit zero is kept, not deleted: "we decided to take nothing from
    this account that year" is a different statement from "we have not decided".
    """
    amount = _magnitude(amount_cents)
    column, key = _withdrawal_column(account_id)
    cur = conn.execute(
        f"UPDATE retirement_withdrawals SET amount_cents = ? WHERE {column} = ? AND year = ?",
        (amount, key, int(year)),
    )
    if not cur.rowcount:
        conn.execute(
            f"INSERT INTO retirement_withdrawals ({column}, year, amount_cents) "
            "VALUES (?, ?, ?)",
            (key, int(year), amount),
        )
    conn.commit()


def _withdrawal_column(account_id: int) -> tuple[str, int]:
    """The column and stored id a plan key maps to (see ``is_planned``)."""
    if is_planned(account_id):
        return "planned_id", -int(account_id)
    return "account_id", int(account_id)


def get_withdrawal(conn, account_id: int, year: int) -> Optional[int]:
    """The planned distribution in cents, or None when nothing is planned."""
    column, key = _withdrawal_column(account_id)
    row = conn.execute(
        f"SELECT amount_cents FROM retirement_withdrawals WHERE {column} = ? AND year = ?",
        (key, int(year)),
    ).fetchone()
    return None if row is None else int(row["amount_cents"])


def list_withdrawals(conn, year: Optional[int] = None) -> list[dict]:
    """Planned distributions, optionally just one year, oldest year first."""
    sql = (
        "SELECT id, COALESCE(account_id, -planned_id) AS account_id, year, "
        "       amount_cents FROM retirement_withdrawals "
        "{where}ORDER BY year, account_id"
    )
    if year is None:
        rows = conn.execute(sql.format(where="")).fetchall()
    else:
        rows = conn.execute(sql.format(where="WHERE year = ? "), (int(year),)).fetchall()
    return [dict(r) for r in rows]


def delete_withdrawals_after(conn, year: int) -> int:
    """Forget every planned withdrawal after ``year``; returns how many.

    The household plan owns every year through its horizon. Rows past it were
    written under an older, longer horizon and nothing rewrites them - they sat
    in the withdrawal schedule as draws nobody planned, and the Investment
    Center's projection took them as real.
    """
    cur = conn.execute("DELETE FROM retirement_withdrawals WHERE year > ?",
                       (int(year),))
    conn.commit()
    return int(cur.rowcount or 0)


def delete_withdrawal(conn, account_id: int, year: int) -> None:
    column, key = _withdrawal_column(account_id)
    conn.execute(
        f"DELETE FROM retirement_withdrawals WHERE {column} = ? AND year = ?",
        (key, int(year)),
    )
    conn.commit()


def set_conversion(
    conn, from_account_id: int, to_account_id: int, year: int, amount_cents: int
) -> None:
    """Plan a Roth conversion: money leaves ``from_account_id``, arrives in ``to_account_id``.

    One row, two flows. Storing it as a pair of unrelated withdrawal and
    contribution rows would let them drift apart, and the tax figure depends on
    them being the same number.

    Both accounts must belong to the same person. There is no spousal Roth
    conversion: IRC 408A(d)(3) rolls a distribution from an IRA or employer plan
    into a Roth IRA of the SAME individual, so a plan that converted one
    spouse's IRA into the other's Roth would be a plan no custodian could
    execute. The check is skipped when either owner is unknown - see
    ``same_owner``.
    """
    amount = _magnitude(amount_cents)
    if int(from_account_id) == int(to_account_id):
        raise ValueError("a conversion needs two different accounts")
    if not same_owner(conn, from_account_id, to_account_id):
        raise ValueError(
            "a Roth conversion has to land in a Roth owned by the same person as "
            "the source account; there is no spousal conversion (IRC 408A(d)(3))"
        )
    if is_planned(from_account_id):
        raise ValueError("a planned Roth cannot be the source of a conversion")
    column, key = _target_column(to_account_id)
    cur = conn.execute(
        "UPDATE retirement_conversions SET amount_cents = ? "
        f" WHERE from_account_id = ? AND {column} = ? AND year = ?",
        (amount, int(from_account_id), key, int(year)),
    )
    if not cur.rowcount:
        conn.execute(
            "INSERT INTO retirement_conversions "
            f"(from_account_id, {column}, year, amount_cents) VALUES (?, ?, ?, ?)",
            (int(from_account_id), key, int(year), amount),
        )
    conn.commit()


def _target_column(to_account_id: int) -> tuple[str, int]:
    if is_planned(to_account_id):
        return "to_planned_id", -int(to_account_id)
    return "to_account_id", int(to_account_id)


def get_conversion(
    conn, from_account_id: int, to_account_id: int, year: int
) -> Optional[int]:
    column, key = _target_column(to_account_id)
    row = conn.execute(
        "SELECT amount_cents FROM retirement_conversions "
        f" WHERE from_account_id = ? AND {column} = ? AND year = ?",
        (int(from_account_id), key, int(year)),
    ).fetchone()
    return None if row is None else int(row["amount_cents"])


def list_conversions(conn, year: Optional[int] = None) -> list[dict]:
    sql = (
        "SELECT id, from_account_id, "
        "       COALESCE(to_account_id, -to_planned_id) AS to_account_id, "
        "       year, amount_cents "
        "  FROM retirement_conversions {where}"
        " ORDER BY year, from_account_id, to_account_id"
    )
    if year is None:
        rows = conn.execute(sql.format(where="")).fetchall()
    else:
        rows = conn.execute(sql.format(where="WHERE year = ? "), (int(year),)).fetchall()
    return [dict(r) for r in rows]


def delete_conversion(conn, from_account_id: int, to_account_id: int, year: int) -> None:
    column, key = _target_column(to_account_id)
    conn.execute(
        "DELETE FROM retirement_conversions "
        f" WHERE from_account_id = ? AND {column} = ? AND year = ?",
        (int(from_account_id), key, int(year)),
    )
    conn.commit()
    if is_planned(to_account_id):
        prune_planned_accounts(conn)


@dataclass(frozen=True)
class AccountFlow:
    """One account's planned money movement for one year, all in cents.

    The three components are kept apart rather than netted because they are
    taxed differently and reported differently: a distribution is spendable
    income, a conversion out is taxable but not spendable, and a conversion in
    is neither. Netting them at storage time would throw that away.
    """

    account_id: int
    distribution_cents: int = 0
    conversion_out_cents: int = 0
    conversion_in_cents: int = 0
    contribution_cents: int = 0       # new money in (a paycheck deferral, a match)

    @property
    def outflow_cents(self) -> int:
        """Everything leaving the account, as a positive magnitude."""
        return self.distribution_cents + self.conversion_out_cents

    @property
    def inflow_cents(self) -> int:
        """Everything arriving in the account, as a positive magnitude."""
        return self.conversion_in_cents

    @property
    def net_cents(self) -> int:
        """Signed, Mammon's way round: negative means the balance goes down.

        This is the number ``mammon/forecast.py`` adds to a year's contribution.
        """
        return self.inflow_cents + self.contribution_cents - self.outflow_cents

    @property
    def taxable_cents(self) -> int:
        """What this account's plan adds to ordinary income for the year.

        Both a distribution and a conversion out of a tax-deferred account are
        taxed; whether THIS account's are depends on its tax treatment, which
        the caller knows and this module deliberately does not.
        """
        return self.distribution_cents + self.conversion_out_cents


def plan_flows(conn, year: int,
               contributions: Optional[Mapping[int, int]] = None
               ) -> dict[int, AccountFlow]:
    """Every account's planned movement for ``year``, keyed by account id.

    Accounts with nothing planned are absent rather than present with zeros, so
    a caller can tell "no plan" from "a plan of nothing". The destination of a
    conversion - a Roth account - appears here with only ``conversion_in_cents``
    set, which is how a later task feeds the inflow side of the projection.
    ``contributions`` (account -> cents this year) adds new money going in: the
    Retirement Planner passes ``planned_contributions`` (the linked salaries'
    deferrals, the measured deposits until retirement for the rest). The
    Investment Dashboard reads the same schedule but adds it beside these
    flows rather than through them, because a What If edit scales it first.
    """
    contributions = {int(k): int(v) for k, v in (contributions or {}).items() if v}
    dist: dict[int, int] = {}
    out: dict[int, int] = {}
    into: dict[int, int] = {}

    for row in list_withdrawals(conn, year):
        aid = int(row["account_id"])
        dist[aid] = dist.get(aid, 0) + int(row["amount_cents"])
    for row in list_conversions(conn, year):
        src, dst = int(row["from_account_id"]), int(row["to_account_id"])
        amount = int(row["amount_cents"])
        out[src] = out.get(src, 0) + amount
        into[dst] = into.get(dst, 0) + amount

    flows: dict[int, AccountFlow] = {}
    for aid in sorted(set(dist) | set(out) | set(into) | set(contributions)):
        flows[aid] = AccountFlow(
            account_id=aid,
            distribution_cents=dist.get(aid, 0),
            conversion_out_cents=out.get(aid, 0),
            conversion_in_cents=into.get(aid, 0),
            contribution_cents=contributions.get(aid, 0),
        )
    return flows


def plan_years(conn) -> list[int]:
    """Every year the plan says anything about, ascending."""
    rows = conn.execute(
        "SELECT year FROM retirement_withdrawals "
        "UNION SELECT year FROM retirement_conversions ORDER BY year"
    ).fetchall()
    return [int(r[0]) for r in rows]


def conversion_years(conn) -> list[int]:
    """Every year a CONVERSION is planned in, ascending.

    Separate from :func:`plan_years` because the two answer different
    questions. A conversion is a discrete event somebody scheduled, so a view
    that dropped one off its end would look like the plan had lost it. A
    withdrawal row, since :func:`seed_withdrawals`, exists for every year of the
    longest longevity case the moment a birth year is on file - so a horizon
    that stretched to cover withdrawals would always stretch to the longest
    case, and choosing a shorter one would do nothing.
    """
    rows = conn.execute(
        "SELECT DISTINCT year FROM retirement_conversions ORDER BY year"
    ).fetchall()
    return [int(r[0]) for r in rows]


def shortfall_against_rmd(
    conn,
    account_id: int,
    balance_cents: int,
    birth_year: int,
    birth_month: Optional[int],
    plan_year: int,
) -> int:
    """How far the plan falls short of the required minimum, in cents.

    Zero when the plan meets or exceeds it. The RMD is a FLOOR the user may
    raise but not lower, and a conversion does NOT satisfy it - the required
    amount must come out before any conversion (IRC 408A(d)(3)(E)), so only
    the distribution counts here.
    """
    required = rmd(balance_cents, birth_year, birth_month, plan_year)
    if required <= 0:
        return 0
    planned = get_withdrawal(conn, account_id, plan_year) or 0
    return max(0, required - planned)


def account_birth(conn, account_id: int, year: Optional[int] = None
                  ) -> tuple[Optional[int], Optional[int]]:
    """The birth (year, month) whose age drives this account's RMD.

    The recorded owner when there is one, and otherwise the first household
    member with a birth year on file: a one-person household never labels its
    accounts, and refusing to compute a required minimum until it does would be
    a rule Mammon invented rather than one the Code imposes. ``(None, None)``
    means nobody's age is known, which is the one honest reason to skip the
    calculation entirely.

    One function so the seeding pass and the floor under an edited cell cannot
    disagree about whose age applies.
    """
    owner = account_owner(conn, int(account_id))
    if year is not None and owner:
        # Under the survivor scenario the deceased's IRAs roll over to the
        # survivor, whose own age then sets the minimum (a spousal rollover,
        # IRC 402(c)(9), 408(d)(3)).
        scenario = get_survivor_scenario(conn)
        if scenario is not None and scenario.widowed(year) \
                and int(owner) == scenario.deceased_id:
            owner = scenario.survivor_id
    person = get_person(conn, owner) if owner else None
    if person is None or not person.get("birth_year"):
        person = next((p for p in list_people(conn) if p.get("birth_year")), None)
    if not person or not person.get("birth_year"):
        return None, None
    return int(person["birth_year"]), _optional_int(person.get("birth_month"))


def spouse_birth_year(conn, owner_person_id: Optional[int]) -> Optional[int]:
    """The birth year of the account owner's spouse - the other of the self
    and spouse on file - or None. An unowned account is read as the self
    person's, like :func:`account_birth`."""
    couple = [p for p in list_people(conn)
              if p.get("relationship") in ("self", "spouse") and p.get("birth_year")]
    if len(couple) != 2:
        return None
    owner = int(owner_person_id) if owner_person_id is not None else next(
        (int(p["id"]) for p in couple if p.get("relationship") == "self"), None)
    other = next((p for p in couple if int(p["id"]) != owner), None)
    return int(other["birth_year"]) if other is not None else None


def account_minimum_cents(conn, acct: "PlanAccount", balance_cents: int, year: int, *,
                          prior_balance_cents: Optional[int] = None,
                          employer_until=None, widowed: bool = False) -> int:
    """The minimum out of ``acct`` in ``year`` from the balance it entered
    with: an inherited account's beneficiary rules, otherwise the owner's own
    with the much-younger-spouse divisor and the April 1 deferral the
    household chose. ``prior_balance_cents`` is the balance the year before
    was entered with, for that deferral. Zero when no minimum is owed or
    nobody's age is on file."""
    if not acct.floored_in(year, employer_until):
        return 0
    birth_year, birth_month = account_birth(conn, acct.account_id,
                                            int(year) if widowed else None)
    if birth_year is None:
        return 0
    balance = max(0, int(balance_cents))
    if acct.inherited_death_year is not None:
        return inherited_minimum_cents(balance, birth_year, acct.inherited_death_year,
                                       acct.inherited_after_rbd, year)
    return owner_minimum_cents(
        balance, prior_balance_cents, birth_year, birth_month, int(year),
        first_year=acct.first_floored_year(birth_year, employer_until),
        spouse_birth_year=None if widowed else spouse_birth_year(conn, acct.owner_person_id),
        defer_first=get_defer_first_rmd(conn))


def account_rmd(conn, account_id: int, balance_cents: int, year: int, *,
                employer_until: Optional[int] = None,
                prior_balance_cents: Optional[int] = None) -> int:
    """The required minimum out of one account in one year, in cents.

    Zero whenever no minimum is required: a Roth (IRC 408A(c)(4)), a current
    employer's plan while the user still works there (IRC 401(a)(9)(C)(i)(II)),
    a year before the applicable age, or an account whose owner's age is
    unknown. Every rule is :func:`account_minimum_cents`.
    """
    acct = next((a for a in plan_accounts(conn) if a.account_id == int(account_id)), None)
    if acct is None:
        return 0
    return account_minimum_cents(conn, acct, balance_cents, year,
                                 prior_balance_cents=prior_balance_cents,
                                 employer_until=employer_until)


def seed_withdrawals(conn, years: Iterable[int], balance_cents: Callable[[int], int],
                     *, accounts: Optional[Sequence[PlanAccount]] = None,
                     balance_for_year: Optional[Callable[[int, int], int]] = None,
                     employer_until: Optional[int] = None,
                     ) -> list[tuple[int, int]]:
    """Fill every UNPLANNED year of every RMD-floored account with its minimum.

    The user's numbers always win. ``retirement_withdrawals`` has no ``source``
    column, so an existing row is by definition something a person put there -
    even an explicit zero - and is left alone. That makes this idempotent and
    safe to run on every page load and every time the Social Security dialog
    closes: the plan stops being empty the moment a birth year is on file, and
    nothing the user then types is ever seeded back over.

    A Roth and a current employer's plan are skipped, because neither has a
    lifetime required minimum to seed. So is a year whose minimum is zero:
    writing a zero row would claim the user had decided to take nothing, and
    would block the seeding that should happen when they turn 73.

    ``balance_cents`` is a callable taking an account id rather than a number,
    because what an investment account is WORTH is a valuation question that
    lives a layer above this one.

    Returns the ``(account_id, year)`` pairs written.
    """
    wanted = sorted({int(y) for y in years})
    if not wanted:
        return []
    written: list[tuple[int, int]] = []
    for acct in (plan_accounts(conn) if accounts is None else accounts):
        if not any(acct.floored_in(y, employer_until) for y in wanted):
            continue
        birth_year, _birth_month = account_birth(conn, acct.account_id)
        if birth_year is None:
            continue
        balance = max(0, int(balance_cents(acct.account_id)))
        prior = None
        for year in wanted:
            if not acct.floored_in(year, employer_until):
                continue
            if get_withdrawal(conn, acct.account_id, year) is not None:
                continue
            # The prior December 31 balance, projected, when the caller can
            # project one (``balance_for_year``); today's otherwise.
            if balance_for_year is not None:
                balance = max(0, int(balance_for_year(acct.account_id, year)))
                prior = max(0, int(balance_for_year(acct.account_id, year - 1)))
            required = account_minimum_cents(conn, acct, balance, year,
                                             prior_balance_cents=prior,
                                             employer_until=employer_until)
            if required <= 0:
                continue
            set_withdrawal(conn, acct.account_id, year, required)
            written.append((acct.account_id, year))
    return written


# ---------------------------------------------------------------------------
# The plan: projected taxable income
# ---------------------------------------------------------------------------

#: The largest share of a Social Security benefit that can be includable in
#: gross income, as a percentage (IRC 86(a)(2)).
SS_TAXABLE_MAX_SHARE = 85

#: IRC 86(c): the base amount and the adjusted base amount provisional income
#: is measured against, per filing status. Fixed in the statute in 1983 and
#: 1993 and NEVER indexed - they are not in the bracket-indexing path on
#: purpose. "separate" is a married-filing-separately return that lived with
#: the spouse, whose base amounts are zero (IRC 86(c)(1)(C)(ii), (c)(2)).
SS_PROVISIONAL_THRESHOLDS = {
    "single": (25_000_00, 34_000_00),
    "joint": (32_000_00, 44_000_00),
    "separate": (0, 0),
}


def taxable_social_security_cents(benefit_cents: int,
                                  other_income_cents: Optional[int] = None,
                                  filing_status: str = "single") -> int:
    """The taxable share of a year's Social Security benefit, in cents.

    With ``other_income_cents`` - the year's ordinary income apart from the
    benefit (IRA draws, conversions, pensions, wages less pre-tax deferrals,
    interest) - this is the IRC 86 worksheet: provisional income is that plus
    half the benefit; nothing is taxable up to the base amount, half the excess
    up to the adjusted base, 85% of the excess above it plus the lesser of the
    middle tier's half and half the benefit, never more than 85% of the
    benefit. Roth distributions and the basis in a taxable sale are not in it.

    Without it, the 85% ceiling. Reported: the planner charged the ceiling in
    every year, overstating taxable income in the low-income years a plan
    spends from Roth and taxable accounts - exactly the plans the tax total is
    there to compare - and hiding the phase-in zone where a $1 IRA draw adds
    $1.85 of taxable income.
    """
    benefit = abs(int(benefit_cents))
    ceiling = (benefit * SS_TAXABLE_MAX_SHARE + 50) // 100
    if other_income_cents is None:
        return ceiling
    base, adjusted = SS_PROVISIONAL_THRESHOLDS.get(
        filing_status, SS_PROVISIONAL_THRESHOLDS["single"])
    half_benefit = (benefit + 1) // 2
    provisional = max(0, int(other_income_cents)) + half_benefit
    if provisional <= base:
        return 0
    if provisional <= adjusted:
        return min((provisional - base + 1) // 2, half_benefit)
    middle = min(half_benefit, (adjusted - base + 1) // 2)
    return min(ceiling, ((provisional - adjusted) * SS_TAXABLE_MAX_SHARE + 50) // 100
               + middle)


#: IRC 1211(b): the net capital loss that comes off ordinary income in a year;
#: the rest carries forward. Not indexed.
CAPITAL_LOSS_LIMIT_CENTS = 300_000


def loss_offset_cents(gains_cents: int) -> int:
    """What a year's net capital gain does to ORDINARY income: nothing when
    positive, up to $3,000 off it when a net loss (IRC 1211(b)). Zero or
    negative."""
    return min(0, max(int(gains_cents), -CAPITAL_LOSS_LIMIT_CENTS))


def gross_with_social_security_cents(other_income_cents: int, benefit_cents: int,
                                     filing_status: str = "single",
                                     gains_cents: int = 0,
                                     tax_exempt_cents: int = 0) -> int:
    """ORDINARY income before the deduction: the other income (less the
    capital-loss offset, when the year's gains are a net loss) plus the
    taxable share of the benefit. Capital gains are not ordinary income, but
    they are in provisional income, so they can make more of the benefit
    taxable - and so is tax-exempt interest (IRC 86(b)(2)(B))."""
    other = int(other_income_cents) + loss_offset_cents(gains_cents)
    return other + taxable_social_security_cents(
        benefit_cents, other + max(0, int(gains_cents)) + max(0, int(tax_exempt_cents)),
        filing_status)


def ordinary_room_cents(target_gross_cents: int, other_income_cents: int,
                        benefit_cents: int, filing_status: str = "single",
                        gains_cents: int = 0, tax_exempt_cents: int = 0) -> int:
    """The most extra ordinary income (an IRA draw, a conversion) that keeps
    gross income at or under ``target_gross_cents``.

    Not target minus today's gross: every dollar added can also pull up to 85
    cents of Social Security into income, so the room is found by bisection
    over the (non-decreasing) gross. Zero when already over.
    """
    other = int(other_income_cents)
    target = int(target_gross_cents)
    if gross_with_social_security_cents(other, benefit_cents, filing_status,
                                        gains_cents, tax_exempt_cents) >= target:
        return 0
    lo, hi = 0, max(0, target - other + CAPITAL_LOSS_LIMIT_CENTS)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if gross_with_social_security_cents(other + mid, benefit_cents,
                                            filing_status, gains_cents,
                                            tax_exempt_cents) <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def taxable_ordinary_cents(other_income_cents: int, benefit_cents: int,
                           filing_status: str, gains_cents: int, deduction_cents: int,
                           *, seniors: int = 0, year: Optional[int] = None,
                           tax_exempt_cents: int = 0) -> int:
    """ORDINARY taxable income: the gross with the benefit's taxable share, less
    the deduction and - in 2025-2028, given ``year`` - the senior deduction,
    which phases out with the year's modified AGI (the gross plus the gains).
    Floored at zero; what the bracket lines are measured against."""
    gross = gross_with_social_security_cents(other_income_cents, benefit_cents,
                                             filing_status, gains_cents, tax_exempt_cents)
    extra = (0 if year is None else senior_deduction_cents(
        filing_status, seniors, gross + max(0, int(gains_cents)), int(year)))
    return max(0, gross - max(0, int(deduction_cents)) - extra)


def taxable_room_cents(top_cents: int, other_income_cents: int, benefit_cents: int,
                       filing_status: str, gains_cents: int, deduction_cents: int,
                       *, seniors: int = 0, year: Optional[int] = None,
                       tax_exempt_cents: int = 0) -> int:
    """The most extra ordinary income (an IRA draw, a conversion) that keeps
    :func:`taxable_ordinary_cents` at or under ``top_cents``. Bisection, like
    :func:`ordinary_room_cents`: a dollar added can pull Social Security into
    income AND take six cents of senior deduction away, so taxable income is
    non-decreasing in the extra but not a straight line. Zero when over."""
    def taxable(extra: int) -> int:
        return taxable_ordinary_cents(int(other_income_cents) + extra, benefit_cents,
                                      filing_status, gains_cents, deduction_cents,
                                      seniors=seniors, year=year,
                                      tax_exempt_cents=tax_exempt_cents)

    top = int(top_cents)
    if taxable(0) >= top:
        return 0
    lo = 0
    hi = max(0, top + max(0, int(deduction_cents)) + CAPITAL_LOSS_LIMIT_CENTS
             + SENIOR_DEDUCTION_CENTS * max(0, int(seniors)) + 1)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if taxable(mid) <= top:
            lo = mid
        else:
            hi = mid - 1
    return lo


def set_taxable_income(conn, year: int, amount_cents: int, *, source: str = "entered") -> None:
    """Store a year's projected taxable income BEFORE any planned conversion.

    Base income only. The Roth section stacks the year's conversions on top when
    it draws the bar, so a stored total that already included them would double
    count the moment the user clicked a bracket line to set one.
    """
    amount = _magnitude(amount_cents)
    if source not in ("entered", "seeded"):
        raise ValueError("taxable income comes from an entry or from seeding")
    cur = conn.execute(
        "UPDATE retirement_taxable_income SET amount_cents = ?, source = ? WHERE year = ?",
        (amount, source, int(year)),
    )
    if not cur.rowcount:
        conn.execute(
            "INSERT INTO retirement_taxable_income (year, amount_cents, source) "
            "VALUES (?, ?, ?)",
            (int(year), amount, source),
        )
    conn.commit()


def get_taxable_income(conn, year: int) -> Optional[int]:
    """A year's stored base taxable income in cents, or None when unset.

    None and zero are different answers: zero is a year the user says has no
    taxable income, None is a year nobody has said anything about.
    """
    row = conn.execute(
        "SELECT amount_cents FROM retirement_taxable_income WHERE year = ?", (int(year),)
    ).fetchone()
    return None if row is None else int(row[0])


def list_taxable_income(conn) -> list[dict]:
    """Every stored year of base taxable income, oldest first."""
    rows = conn.execute(
        "SELECT id, year, amount_cents, source FROM retirement_taxable_income "
        "ORDER BY year"
    ).fetchall()
    return [dict(r) for r in rows]


def taxable_income_map(conn) -> dict[int, int]:
    """Stored base taxable income keyed by year, for drawing a series."""
    return {int(r["year"]): int(r["amount_cents"]) for r in list_taxable_income(conn)}


def delete_taxable_income(conn, year: int) -> None:
    conn.execute("DELETE FROM retirement_taxable_income WHERE year = ?", (int(year),))
    conn.commit()


def seed_taxable_income(conn, amounts: Mapping[int, int], *,
                         overwrite_entered: bool = False) -> list[int]:
    """Fill in the years the user has not taken over, and return the ones written.

    ``overwrite_entered`` recomputes typed years too. The planner passes it: once
    other income could be stated as its own source (v83), a typed override had
    nothing left to say, and a cleared cell had quietly pinned years at an
    "entered" $0 that ignored every draw and every rental.

    A year the user typed (``source='entered'``) is never overwritten by the
    plan's own arithmetic, however much the plan changes around it. A year still
    marked ``'seeded'`` IS refreshed: the seed is derived from the plan's own
    flows, so leaving it frozen after a withdrawal is planned would show a base
    income nobody chose. Taking over one year therefore costs the user that year
    only.
    """
    rows = list_taxable_income(conn)
    entered = ({int(r["year"]) for r in rows if r["source"] == "entered"}
               if not overwrite_entered else set())
    sources = {int(r["year"]): r["source"] for r in rows}
    current = {int(r["year"]): int(r["amount_cents"]) for r in rows}
    written: list[int] = []
    for year in sorted(amounts):
        key = int(year)
        if key in entered:
            continue
        amount = _magnitude(amounts[year])
        if current.get(key) == amount and sources.get(key) == "seeded":
            continue
        set_taxable_income(conn, key, amount, source="seeded")
        written.append(key)
    return written


# ---------------------------------------------------------------------------
# The plan: one household withdrawal, spread across the whole pool
# ---------------------------------------------------------------------------
#
# A person does not retire an ACCOUNT, they retire. What they can say is "we
# need this much a year, and it has to keep up with prices"; which of the four
# IRAs it comes out of is arithmetic, not a decision. The planner used to ask
# the other question - pick one account, name an amount - and then refused any
# amount large enough to satisfy that account's own required minimum, because
# it measured the draw against that ONE account's balance while the rest of the
# household sat untouched. That is the defect this section exists to remove.
#
# Three rules, in this order, and each one is load-bearing:
#
# 1. The RMD floor is law (IRC 401(a)(9)) and is satisfied FIRST, per account.
#    It cannot be traded between accounts: a minimum owed on one IRA is not met
#    by a larger draw from another.
# 2. What is left of the year's total is split in proportion to each account's
#    PROJECTED BALANCE ENTERING THAT YEAR, so the accounts drain together and
#    reach empty together. Proportional and not equal: equal shares would empty
#    the smallest account decades early and then need the same redistribution
#    anyway, one account at a time.
# 3. An account that cannot cover its share pays what it holds and the rest is
#    redistributed over the accounts that still have money - which is the
#    "switch to a different one when one runs out" behavior, happening every
#    year rather than once. ROTH accounts (and any other unfloored plan
#    account) are the LAST tier: they are drawn only once the pre-tax accounts
#    are empty, and never carry a floor.
#
# "Does not last" is therefore a statement about the POOL, never about an
# account. A single empty IRA in a household that still has three funded ones
# and a Roth has not run out of anything.


@dataclass(frozen=True)
class WithdrawalPlanParams:
    """The two numbers the household typed: a first-year amount and a raise.

    Stored rather than derived because they are an intent. The per-account,
    per-year rows in ``retirement_withdrawals`` are what this intent PRODUCED,
    and a user who reopens the page should see what they asked for, not have to
    reverse-engineer it out of sixty rows of arithmetic.
    """

    start_cents: int = 0
    increase_pct: Decimal = Decimal(0)
    start_year: Optional[int] = None        # None: the first plan year
    bracket_rate: Optional[str] = None      # "22": IRA draws stop at its top

    @property
    def is_set(self) -> bool:
        return self.start_cents > 0


def get_withdrawal_plan(conn) -> WithdrawalPlanParams:
    """The stored household withdrawal parameters, or an unset pair of zeros."""
    row = conn.execute(
        "SELECT start_cents, increase_pct, start_year, bracket_rate "
        "  FROM retirement_withdrawal_plan WHERE id = 1"
    ).fetchone()
    if row is None:
        return WithdrawalPlanParams()
    try:
        pct = Decimal(str(row["increase_pct"]))
    except Exception:                       # a hand-edited row is not a crash
        pct = Decimal(0)
    return WithdrawalPlanParams(start_cents=int(row["start_cents"]), increase_pct=pct,
                                start_year=_optional_int(row["start_year"]),
                                bracket_rate=row["bracket_rate"] or None)


def set_withdrawal_plan(conn, start_cents: int, increase_pct, *,
                        start_year: Optional[int] = None,
                        bracket_rate: Optional[str] = None) -> WithdrawalPlanParams:
    """Store the household's plan: amount, raise, first year, bracket. One row."""
    params = WithdrawalPlanParams(
        start_cents=_magnitude(start_cents),
        increase_pct=Decimal(str(increase_pct)),
        start_year=_optional_int(start_year),
        bracket_rate=(str(bracket_rate) if bracket_rate else None),
    )
    conn.execute(
        "INSERT INTO retirement_withdrawal_plan "
        "(id, start_cents, increase_pct, start_year, bracket_rate) "
        "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "start_cents = excluded.start_cents, increase_pct = excluded.increase_pct, "
        "start_year = excluded.start_year, bracket_rate = excluded.bracket_rate",
        (params.start_cents, str(params.increase_pct), params.start_year,
         params.bracket_rate),
    )
    conn.commit()
    return params


#: The steps the household spends in, after required minimums (always first -
#: the law takes them whatever the order). "deferred" is IRA/401(k) money up to
#: the bracket target's room (all of it without a target), "deferred_over" the
#: IRA money beyond that room.
SPENDING_STEPS = ("deferred", "taxable", "roth", "deferred_over")
SPENDING_STEP_LABELS = {
    "deferred": "IRA / 401(k), up to the bracket target",
    "taxable": "Taxable accounts",
    "roth": "Roth",
    "deferred_over": "IRA / 401(k), beyond the bracket target",
}


def normalize_spending_order(order) -> tuple[str, ...]:
    """Every step exactly once, unknown ones dropped, missing ones appended in
    the default order - and the IRA room before the IRA beyond it, since
    drawing past a line before drawing up to it is not a thing."""
    seen: list[str] = []
    for step in order or ():
        if step in SPENDING_STEPS and step not in seen:
            seen.append(step)
    seen += [s for s in SPENDING_STEPS if s not in seen]
    if seen.index("deferred_over") < seen.index("deferred"):
        seen.remove("deferred_over")
        seen.insert(seen.index("deferred") + 1, "deferred_over")
    return tuple(seen)


def get_spending_order(conn) -> tuple[str, ...]:
    row = conn.execute(
        "SELECT spending_order FROM retirement_assumptions WHERE id = 1").fetchone()
    stored = row["spending_order"] if row is not None else None
    return normalize_spending_order(stored.split(",") if stored else SPENDING_STEPS)


def set_spending_order(conn, order) -> tuple[str, ...]:
    value = normalize_spending_order(order)
    conn.execute(
        "INSERT INTO retirement_assumptions (id, spending_order) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET spending_order = excluded.spending_order",
        (",".join(value),))
    conn.commit()
    return value


def spending_exempt_ids(conn) -> set[int]:
    """Accounts the plan never draws from for spending or tax (db._V96)."""
    return {int(r[0]) for r in conn.execute(
        "SELECT account_id FROM retirement_spending_exempt")}


def set_spending_exempt(conn, account_id: int, exempt: bool) -> None:
    if exempt:
        conn.execute("INSERT OR IGNORE INTO retirement_spending_exempt (account_id) "
                     "VALUES (?)", (int(account_id),))
    else:
        conn.execute("DELETE FROM retirement_spending_exempt WHERE account_id = ?",
                     (int(account_id),))
    conn.commit()


#: The Social Security COLA the planner assumes when the household has not set
#: one: the long-range CPI-W increase in the Social Security Trustees Report's
#: intermediate assumptions (about 2.4%), which is the index COLAs follow
#: (42 USC 415(i)). An editable ASSUMPTION, stored per ledger (db._V84).
DEFAULT_COLA_PCT = Decimal("2.4")


def get_cola_pct(conn) -> Decimal:
    row = conn.execute(
        "SELECT cola_pct FROM retirement_assumptions WHERE id = 1").fetchone()
    if row is None:
        return DEFAULT_COLA_PCT
    try:
        return Decimal(str(row["cola_pct"]))
    except Exception:                       # a hand-edited row is not a crash
        return DEFAULT_COLA_PCT


def set_cola_pct(conn, pct) -> Decimal:
    value = Decimal(str(pct))
    conn.execute(
        "INSERT INTO retirement_assumptions (id, cola_pct) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET cola_pct = excluded.cola_pct",
        (str(value),))
    conn.commit()
    return value


#: The year the bracket and standard-deduction tables are published for; later
#: years are that table indexed forward (:func:`indexed_cents`).
TAX_TABLE_YEAR = _TAX_BRACKET_PROV.effective_year

#: How fast the bracket tops and the standard deduction are assumed to rise when
#: the household has not set it: they are indexed to chained CPI-U (IRC 1(f)(3),
#: 63(c)(4)), which runs about a quarter point below the CPI-W behind the COLA
#: (``DEFAULT_COLA_PCT``). An editable ASSUMPTION, stored per ledger (db._V85).
DEFAULT_BRACKET_INDEX_PCT = Decimal("2.2")


#: When Social Security's retirement trust fund (OASI) runs out and what share of
#: scheduled benefits payroll taxes still pay after that: the 2025 Social
#: Security Trustees Report's intermediate projection (depletion in 2033, about
#: 77% payable). An editable ASSUMPTION, stored per ledger (db._V86); a payable
#: share of 100 means no cut.
DEFAULT_SS_SHORTFALL_YEAR = 2033
DEFAULT_SS_PAYABLE_PCT = Decimal("77")


@dataclass(frozen=True)
class SocialSecurityShortfall:
    year: int = DEFAULT_SS_SHORTFALL_YEAR
    payable_pct: Decimal = DEFAULT_SS_PAYABLE_PCT

    def applies(self, year: int) -> bool:
        return int(year) >= self.year and self.payable_pct < 100

    def payable(self, cents: int, year: int) -> int:
        """``cents`` of scheduled benefit as actually payable in ``year``."""
        if not self.applies(year):
            return int(cents)
        return int((Decimal(int(cents)) * self.payable_pct / Decimal(100))
                   .quantize(Decimal(1), rounding=ROUND_HALF_UP))


def get_ss_shortfall(conn) -> SocialSecurityShortfall:
    row = conn.execute(
        "SELECT ss_shortfall_year, ss_payable_pct FROM retirement_assumptions "
        "WHERE id = 1").fetchone()
    if row is None:
        return SocialSecurityShortfall()
    try:
        return SocialSecurityShortfall(int(row["ss_shortfall_year"]),
                                       Decimal(str(row["ss_payable_pct"])))
    except Exception:                       # a hand-edited row is not a crash
        return SocialSecurityShortfall()


def set_ss_shortfall(conn, year: int, payable_pct) -> SocialSecurityShortfall:
    value = SocialSecurityShortfall(int(year), Decimal(str(payable_pct)))
    if not (0 <= value.payable_pct <= 100):
        raise ValueError("the payable share of benefits is a percentage, 0 to 100")
    conn.execute(
        "INSERT INTO retirement_assumptions (id, ss_shortfall_year, ss_payable_pct) "
        "VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "ss_shortfall_year = excluded.ss_shortfall_year, "
        "ss_payable_pct = excluded.ss_payable_pct",
        (value.year, str(value.payable_pct)))
    conn.commit()
    return value


DEFAULT_SURVIVOR_SPENDING_PCT = Decimal("75")
DEFAULT_PENSION_SURVIVOR_PCT = Decimal("50")


@dataclass(frozen=True)
class SurvivorScenario:
    """One spouse dies at the END of ``death_year``; from the year after, the
    plan is the survivor's (db._V98)."""

    deceased_id: int
    survivor_id: Optional[int]
    death_year: int
    spending_pct: Decimal = DEFAULT_SURVIVOR_SPENDING_PCT
    pension_pct: Decimal = DEFAULT_PENSION_SURVIVOR_PCT

    def widowed(self, year: int) -> bool:
        return int(year) > self.death_year


def _assumption_pct(conn, column: str, default: Decimal) -> Decimal:
    row = conn.execute(
        f"SELECT {column} FROM retirement_assumptions WHERE id = 1").fetchone()
    if row is None or row[column] is None:
        return default
    try:
        return Decimal(str(row[column]))
    except Exception:                       # a hand-edited row is not a crash
        return default


def _set_assumption(conn, column: str, value) -> None:
    conn.execute(
        f"INSERT INTO retirement_assumptions (id, {column}) VALUES (1, ?) "
        f"ON CONFLICT(id) DO UPDATE SET {column} = excluded.{column}", (value,))
    conn.commit()


TAX_PAID_FROM = ("spending", "taxable")


def get_tax_paid_from(conn) -> str:
    """Where the plan pays the income tax from: "spending" (the spending
    order) or "taxable" (taxable accounts first) - db._V102."""
    row = conn.execute(
        "SELECT tax_paid_from FROM retirement_assumptions WHERE id = 1").fetchone()
    value = (row["tax_paid_from"] if row is not None else None) or "spending"
    return value if value in TAX_PAID_FROM else "spending"


def set_tax_paid_from(conn, value: str) -> None:
    if value not in TAX_PAID_FROM:
        raise ValueError(f"tax is paid from one of {TAX_PAID_FROM}")
    _set_assumption(conn, "tax_paid_from", value)


def get_plan_through_age(conn) -> int:
    """The planner's terminal-age case (db._V101)."""
    row = conn.execute(
        "SELECT plan_through_age FROM retirement_assumptions WHERE id = 1").fetchone()
    try:
        return int(row["plan_through_age"]) if row is not None else 90
    except (TypeError, ValueError):
        return 90


def set_plan_through_age(conn, age: int) -> None:
    _set_assumption(conn, "plan_through_age", int(age))


def get_state_tax_pct(conn) -> Decimal:
    return _assumption_pct(conn, "state_tax_pct", Decimal(0))


def set_state_tax_pct(conn, pct) -> Decimal:
    value = Decimal(str(pct))
    if not (0 <= value <= 20):
        raise ValueError("a state income tax rate is 0 to 20%")
    _set_assumption(conn, "state_tax_pct", str(value))
    return value


def get_state_taxes_ss(conn) -> bool:
    row = conn.execute(
        "SELECT state_taxes_ss FROM retirement_assumptions WHERE id = 1").fetchone()
    return bool(row["state_taxes_ss"]) if row is not None else False


def set_state_taxes_ss(conn, on: bool) -> None:
    _set_assumption(conn, "state_taxes_ss", 1 if on else 0)


def get_aca_benchmark_cents(conn) -> int:
    """The yearly premium of the marketplace's benchmark plan (the second-
    lowest-cost silver plan) for the household before 65, in today's dollars;
    0 = not on the marketplace (db._V105). The credit is computed from it
    each year (:func:`aca_premium_credit_cents`); the entered credit of
    db._V104 is no longer read."""
    row = conn.execute(
        "SELECT aca_benchmark_cents FROM retirement_assumptions WHERE id = 1").fetchone()
    return int(row["aca_benchmark_cents"] or 0) if row is not None else 0


def set_aca_benchmark_cents(conn, cents: int) -> None:
    _set_assumption(conn, "aca_benchmark_cents", max(0, int(cents)))


def get_prior_magi(conn, year: int) -> Optional[int]:
    """The modified AGI the household reported for ``year``, a year BEFORE the
    plan's own (db._V105): what sets IRMAA two years later, which the plan
    cannot read from itself. None = not entered."""
    row = conn.execute("SELECT magi_cents FROM retirement_prior_income WHERE year = ?",
                       (int(year),)).fetchone()
    return int(row["magi_cents"]) if row is not None else None


def set_prior_magi(conn, year: int, cents: Optional[int]) -> None:
    """Store a prior year's modified AGI; None or 0 forgets it."""
    if not cents:
        conn.execute("DELETE FROM retirement_prior_income WHERE year = ?", (int(year),))
    else:
        conn.execute(
            "INSERT INTO retirement_prior_income (year, magi_cents) VALUES (?, ?) "
            "ON CONFLICT(year) DO UPDATE SET magi_cents = excluded.magi_cents",
            (int(year), max(0, int(cents))))
    conn.commit()


def _assumption_flag(conn, column: str) -> bool:
    row = conn.execute(
        f"SELECT {column} FROM retirement_assumptions WHERE id = 1").fetchone()
    return bool(row[column]) if row is not None else False


def get_defer_first_rmd(conn) -> bool:
    """Take the first required minimum by April 1 of the following year
    instead of in the applicable-age year (db._V106)."""
    return _assumption_flag(conn, "defer_first_rmd")


def set_defer_first_rmd(conn, on: bool) -> None:
    _set_assumption(conn, "defer_first_rmd", 1 if on else 0)


def get_community_property(conn) -> bool:
    """A community-property state: the whole of the couple's taxable holdings
    steps up in basis at the first death, not half (IRC 1014(b)(6))."""
    return _assumption_flag(conn, "community_property")


def set_community_property(conn, on: bool) -> None:
    _set_assumption(conn, "community_property", 1 if on else 0)


def get_state_excludes_retirement(conn) -> bool:
    """The state exempts retirement income - pensions, IRA and 401(k) draws,
    conversions - from its income tax (db._V106)."""
    return _assumption_flag(conn, "state_excludes_retirement")


def set_state_excludes_retirement(conn, on: bool) -> None:
    _set_assumption(conn, "state_excludes_retirement", 1 if on else 0)


def get_ssa44_appeal(conn) -> bool:
    """Whether the plan assumes the IRMAA life-changing-event appeal (db._V99)."""
    row = conn.execute(
        "SELECT ssa44_appeal FROM retirement_assumptions WHERE id = 1").fetchone()
    return True if row is None or row["ssa44_appeal"] is None else bool(row["ssa44_appeal"])


def set_ssa44_appeal(conn, on: bool) -> None:
    _set_assumption(conn, "ssa44_appeal", 1 if on else 0)


def get_survivor_spending_pct(conn) -> Decimal:
    return _assumption_pct(conn, "survivor_spending_pct", DEFAULT_SURVIVOR_SPENDING_PCT)


def set_survivor_spending_pct(conn, pct) -> Decimal:
    value = Decimal(str(pct))
    if not (0 <= value <= 200):
        raise ValueError("the survivor's spending is 0 to 200% of the household's")
    _set_assumption(conn, "survivor_spending_pct", str(value))
    return value


def get_pension_survivor_pct(conn) -> Decimal:
    return _assumption_pct(conn, "pension_survivor_pct", DEFAULT_PENSION_SURVIVOR_PCT)


def set_pension_survivor_pct(conn, pct) -> Decimal:
    value = Decimal(str(pct))
    if not (0 <= value <= 100):
        raise ValueError("a pension's survivor share is 0 to 100%")
    _set_assumption(conn, "pension_survivor_pct", str(value))
    return value


def get_survivor_scenario(conn) -> Optional[SurvivorScenario]:
    """The scenario, or None when it is off or no longer makes sense (the
    person was deleted, or there is no one left to survive them)."""
    row = conn.execute(
        "SELECT survivor_person_id, survivor_death_year FROM retirement_assumptions "
        "WHERE id = 1").fetchone()
    if row is None or row["survivor_person_id"] is None \
            or row["survivor_death_year"] is None:
        return None
    deceased = int(row["survivor_person_id"])
    couple = [p for p in list_people(conn)
              if p.get("relationship") in ("self", "spouse")]
    if not any(int(p["id"]) == deceased for p in couple):
        return None
    survivor = next((int(p["id"]) for p in couple if int(p["id"]) != deceased), None)
    if survivor is None:
        return None
    return SurvivorScenario(deceased, survivor, int(row["survivor_death_year"]),
                            get_survivor_spending_pct(conn),
                            get_pension_survivor_pct(conn))


def set_survivor_scenario(conn, person_id: Optional[int],
                          death_year: Optional[int] = None) -> None:
    """Turn the scenario on (who dies, and the year) or off (None)."""
    if person_id is not None and death_year is None:
        raise ValueError("the survivor scenario needs the year of death")
    conn.execute(
        "INSERT INTO retirement_assumptions (id, survivor_person_id, survivor_death_year) "
        "VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "survivor_person_id = excluded.survivor_person_id, "
        "survivor_death_year = excluded.survivor_death_year",
        (None if person_id is None else int(person_id),
         None if person_id is None else int(death_year)))
    conn.commit()


def survivor_status(filing_status: str, year: int,
                    scenario: Optional[SurvivorScenario]) -> str:
    """The filing status in ``year``: a joint return becomes single the year
    after a death (the death year itself is still joint, IRC 6013(a)(2)). The
    qualifying-surviving-spouse status needs a dependent child and is not
    modeled."""
    if scenario is not None and scenario.widowed(year) and filing_status == "joint":
        return "single"
    return filing_status


#: How fast the IRMAA surcharge dollars grow (db._V97): the standard Part B
#: premium rose about 5.2% a year 2016-2026 and 4.2% a year 2006-2026.
DEFAULT_MEDICARE_GROWTH_PCT = Decimal("5")


def get_medicare_growth_pct(conn) -> Decimal:
    row = conn.execute(
        "SELECT medicare_growth_pct FROM retirement_assumptions WHERE id = 1"
    ).fetchone()
    if row is None:
        return DEFAULT_MEDICARE_GROWTH_PCT
    try:
        return Decimal(str(row["medicare_growth_pct"]))
    except Exception:                       # a hand-edited row is not a crash
        return DEFAULT_MEDICARE_GROWTH_PCT


def set_medicare_growth_pct(conn, pct) -> Decimal:
    value = Decimal(str(pct))
    if not (0 <= value <= 30):
        raise ValueError("Medicare premium growth is 0 to 30% a year")
    conn.execute(
        "INSERT INTO retirement_assumptions (id, medicare_growth_pct) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET medicare_growth_pct = excluded.medicare_growth_pct",
        (str(value),))
    conn.commit()
    return value


DEFAULT_END_IRA_TAX_PCT = Decimal("24")


def get_end_ira_tax_pct(conn) -> Decimal:
    """The rate applied to what the tax-deferred accounts still hold at the
    plan's end (db._V95)."""
    row = conn.execute(
        "SELECT end_ira_tax_pct FROM retirement_assumptions WHERE id = 1").fetchone()
    if row is None:
        return DEFAULT_END_IRA_TAX_PCT
    try:
        return Decimal(str(row["end_ira_tax_pct"]))
    except Exception:                       # a hand-edited row is not a crash
        return DEFAULT_END_IRA_TAX_PCT


def set_end_ira_tax_pct(conn, pct) -> Decimal:
    value = Decimal(str(pct))
    if not (0 <= value <= 100):
        raise ValueError("the tax rate on IRAs left at the end is 0 to 100%")
    conn.execute(
        "INSERT INTO retirement_assumptions (id, end_ira_tax_pct) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET end_ira_tax_pct = excluded.end_ira_tax_pct",
        (str(value),))
    conn.commit()
    return value


def plan_mix_weights(mix) -> dict:
    """(stocks, bonds, cash) percentages -> the forecast's class weights, the
    stock share split 60/40 domestic/international as the risk ladder is."""
    stocks, bonds, cash = (float(Decimal(str(v))) / 100.0 for v in mix)
    return {"domestic_stock": 0.6 * stocks, "intl_stock": 0.4 * stocks,
            "bond": bonds, "cash": cash}


def get_plan_mix(conn) -> Optional[tuple[Decimal, Decimal, Decimal]]:
    """The plan's (stocks, bonds, cash) percentages, or None for the mix held
    today (db._V100). A level copied from What If before the mix could be
    typed is read back as the ladder's mix at that level."""
    row = conn.execute(
        "SELECT plan_mix, planning_risk FROM retirement_assumptions WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    if row["plan_mix"]:
        try:
            parts = tuple(Decimal(p) for p in str(row["plan_mix"]).split(","))
            if len(parts) == 3:
                return parts
        except Exception:                   # a hand-edited row is not a crash
            pass
    level = get_planning_risk(conn)
    if level is None:
        return None
    from mammon import forecast
    w = forecast.mix_for_risk(level)
    stocks = round((w.get("domestic_stock", 0) + w.get("intl_stock", 0)) * 100)
    bonds = round(w.get("bond", 0) * 100)
    return Decimal(stocks), Decimal(bonds), Decimal(100 - stocks - bonds)


def set_plan_mix(conn, mix) -> None:
    """Set the plan's mix - (stocks, bonds, cash) summing to 100 - or None
    for the mix held today. Stores the level the projections use with it:
    the ladder level of the same volatility (``forecast.risk_for_mix``)."""
    if mix is None:
        conn.execute(
            "INSERT INTO retirement_assumptions (id, plan_mix, planning_risk) "
            "VALUES (1, NULL, NULL) ON CONFLICT(id) DO UPDATE SET "
            "plan_mix = NULL, planning_risk = NULL")
        conn.commit()
        return
    parts = tuple(Decimal(str(v)) for v in mix)
    if len(parts) != 3 or any(p < 0 or p > 100 for p in parts):
        raise ValueError("stocks, bonds and cash are each 0 to 100%")
    if sum(parts) != 100:
        raise ValueError(f"stocks, bonds and cash add up to {sum(parts)}%, not 100%")
    from mammon import forecast
    level = forecast.risk_for_mix(plan_mix_weights(parts))
    conn.execute(
        "INSERT INTO retirement_assumptions (id, plan_mix, planning_risk) "
        "VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "plan_mix = excluded.plan_mix, planning_risk = excluded.planning_risk",
        (",".join(str(p) for p in parts), str(level)))
    conn.commit()


def get_planning_risk(conn) -> Optional[float]:
    """The risk level (0 to ``forecast.MAX_RISK_LEVEL``, the Investment
    Center thermometer's scale - ``forecast.mix_for_risk``) the planner projects at,
    or None to measure each account's actual holdings (db._V94)."""
    row = conn.execute(
        "SELECT planning_risk FROM retirement_assumptions WHERE id = 1").fetchone()
    if row is None or row["planning_risk"] in (None, ""):
        return None
    try:
        return float(row["planning_risk"])
    except (TypeError, ValueError):        # a hand-edited row is not a crash
        return None


def plan_projection_mix(conn) -> Optional[dict]:
    """The asset-class WEIGHTS the plan projects at, or None for the mix each
    account holds today.

    A typed mix (``get_plan_mix``) is projected at its OWN weights - the 60/40
    stock split of :func:`plan_mix_weights` - not at the ladder rung of equal
    volatility that ``planning_risk`` names. The rung is a position for the
    Investment Dashboard's thermometer, and a one-dimensional ladder cannot
    match both moments: an all-bond plan sits at the rung of a 37/23/40 mix,
    whose mean return is higher, so a fan drawn at the rung was a fan of a
    mix the caption did not name (an audit measured a 13% higher 30-year
    median). A level copied in before any mix was typed still projects at the
    ladder's mix for that level, because that IS what it says.
    """
    row = conn.execute(
        "SELECT plan_mix FROM retirement_assumptions WHERE id = 1").fetchone()
    if row is not None and row["plan_mix"]:
        try:
            parts = tuple(Decimal(p) for p in str(row["plan_mix"]).split(","))
            if len(parts) == 3:
                return plan_mix_weights(parts)
        except Exception:                   # a hand-edited row is not a crash
            pass
    level = get_planning_risk(conn)
    if level is None:
        return None
    from mammon import forecast
    return forecast.mix_for_risk(level)


def set_planning_risk(conn, level: Optional[float]) -> None:
    # The thermometer's own scale, 0-10. Clamped to 0-1 here once, which turned
    # a 30/21/49 What If mix into 10/9/81 in the planner (reported).
    from mammon import forecast
    value = (None if level is None
             else str(max(0.0, min(float(forecast.MAX_RISK_LEVEL), float(level)))))
    conn.execute(
        "INSERT INTO retirement_assumptions (id, planning_risk) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET planning_risk = excluded.planning_risk",
        (value,))
    conn.commit()


def get_bracket_index_pct(conn) -> Decimal:
    row = conn.execute(
        "SELECT bracket_index_pct FROM retirement_assumptions WHERE id = 1"
    ).fetchone()
    if row is None:
        return DEFAULT_BRACKET_INDEX_PCT
    try:
        return Decimal(str(row["bracket_index_pct"]))
    except Exception:                       # a hand-edited row is not a crash
        return DEFAULT_BRACKET_INDEX_PCT


def set_bracket_index_pct(conn, pct) -> Decimal:
    value = Decimal(str(pct))
    conn.execute(
        "INSERT INTO retirement_assumptions (id, bracket_index_pct) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET bracket_index_pct = excluded.bracket_index_pct",
        (str(value),))
    conn.commit()
    return value


def indexed_cents(cents: int, index_pct, year: int) -> int:
    """A tax-table amount carried from :data:`TAX_TABLE_YEAR` to ``year``.

    Reported: the plan used the 2026 brackets and deduction for every year to
    2065 while spending, benefits and minimums all rose, which overstated the
    squeeze into higher brackets. Years at or before the table's are the table.
    The IRS also rounds each indexed figure down to $50; a projection this far
    out does not pretend to that precision.
    """
    return household_amount_cents(int(cents), index_pct,
                                  max(0, int(year) - TAX_TABLE_YEAR))


def bracket_top_cents(rate: str, filing_status: str = "single", *,
                      year: Optional[int] = None,
                      index_pct=None) -> Optional[int]:
    """The top of the bracket taxed at ``rate`` ("22" or "22%"), or None;
    indexed to ``year`` when a year and a rate are given."""
    wanted = str(rate).strip().rstrip("%")
    for bracket in tax_brackets(filing_status):
        if bracket.rate_label.rstrip("%") == wanted:
            top = bracket.upper_cents
            if top is not None and year is not None and index_pct is not None:
                return indexed_cents(top, index_pct, year)
            return top
    return None


def federal_tax_cents(taxable_cents: int, filing_status: str, year: int,
                      index_pct) -> int:
    """Federal ordinary income tax on ``taxable_cents`` (after the deduction),
    through the bracket ladder indexed to ``year``. Ordinary rates only: no
    capital-gains rates, credits, AMT or state tax - a figure for COMPARING
    plans, not a return (reported: scenarios could not be judged without
    seeing the tax)."""
    remaining = max(0, int(taxable_cents))
    tax = Decimal(0)
    for bracket in tax_brackets(filing_status):
        lower = indexed_cents(bracket.lower_cents, index_pct, year)
        upper = (None if bracket.upper_cents is None
                 else indexed_cents(bracket.upper_cents, index_pct, year))
        if remaining <= lower:
            break
        top = remaining if upper is None else min(remaining, upper)
        tax += Decimal(top - lower) * bracket.rate_percent / 100
    return int(tax.quantize(Decimal(1), rounding=ROUND_HALF_UP))


#: 2026 long-term capital gains brackets: the TOTAL taxable income at which
#: gains stop being taxed at 0% and at 15% (20% above). Rev. Proc. 2025-32.
#: Indexed like the ordinary brackets.
LTCG_BRACKETS = {
    "joint": (98_900_00, 613_700_00),
    "single": (49_450_00, 545_500_00),
    "separate": (49_450_00, 306_850_00),
}

#: IRC 1411: 3.8% on the lesser of net investment income or MAGI over these,
#: which are fixed in the statute and NOT indexed (since 2013).
NIIT_THRESHOLDS = {"joint": 250_000_00, "single": 200_000_00, "separate": 125_000_00}
NIIT_RATE = Decimal("3.8")


def capital_gains_tax_cents(ordinary_taxable_cents: int, gains_taxable_cents: int,
                            filing_status: str, year: int, index_pct) -> int:
    """Tax on long-term gains, stacked ON TOP of ordinary taxable income: the
    part of the gains below the 0% top is untaxed, the part up to the 15% top
    pays 15%, the rest 20%. Gains never push ordinary income into a higher
    ordinary bracket; ordinary income decides how much of the gains is 0%."""
    zero, fifteen = (indexed_cents(c, index_pct, year) for c in
                     LTCG_BRACKETS.get(filing_status, LTCG_BRACKETS["single"]))
    low = max(0, int(ordinary_taxable_cents))
    high = low + max(0, int(gains_taxable_cents))

    def span(a: int, b: Optional[int]) -> int:
        return max(0, (high if b is None else min(high, b)) - max(low, a))

    tax = Decimal(span(zero, fifteen)) * 15 / 100 + Decimal(span(fifteen, None)) * 20 / 100
    return int(tax.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def niit_cents(investment_income_cents: int, magi_cents: int,
               filing_status: str) -> int:
    """IRC 1411: 3.8% of the lesser of net investment income and MAGI over
    the threshold. Not a cliff: a dollar over costs 3.8 cents."""
    over = max(0, int(magi_cents) - NIIT_THRESHOLDS.get(
        filing_status, NIIT_THRESHOLDS["single"]))
    base = min(max(0, int(investment_income_cents)), over)
    return int((Decimal(base) * NIIT_RATE / 100).quantize(Decimal(1),
                                                          rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class YearTax:
    """A year's federal tax, in its three parts."""
    ordinary: int
    gains: int
    niit: int
    state: int = 0

    @property
    def federal(self) -> int:
        return self.ordinary + self.gains + self.niit

    @property
    def total(self) -> int:
        return self.federal + self.state


def year_tax(ordinary_gross_cents: int, gains_cents: int, deduction_cents: int,
             filing_status: str, year: int, index_pct,
             investment_income_cents: int = 0, *, state_pct=0,
             social_security_taxed_cents: int = 0,
             state_taxes_ss: bool = False, seniors: int = 0,
             state_excluded_cents: int = 0) -> YearTax:
    """The year's federal tax from ordinary income before the deduction (the
    taxable Social Security in it, and a net capital loss's offset already
    off it - :func:`loss_offset_cents`), the year's net long-term gains (a
    net loss counts as none here, and cuts the investment income) and the
    other net investment income. The deduction comes off ordinary income
    first; what is left of it comes off the gains. MAGI - for the NIIT - is
    both, before it. ``seniors`` (people 65 or older on the return) adds the
    2025-2028 senior deduction, phased out on that same MAGI.
    ``state_excluded_cents`` is retirement income the state does not tax."""
    ordinary = max(0, int(ordinary_gross_cents))
    net_gains = int(gains_cents)
    gains = max(0, net_gains)
    deduction = max(0, int(deduction_cents)) + senior_deduction_cents(
        filing_status, seniors, ordinary + gains, int(year))
    ordinary_taxable = max(0, ordinary - deduction)
    gains_taxable = max(0, gains - max(0, deduction - ordinary))
    # State: a flat rate on federal AGI - gains included, Social Security's
    # taxable share left out unless the state taxes it (most do not), and the
    # retirement income the state exempts when the household says it does.
    # No state deduction is modeled; the rate is the household's estimate.
    state_base = (ordinary + gains
                  - (0 if state_taxes_ss else max(0, int(social_security_taxed_cents)))
                  - max(0, int(state_excluded_cents)))
    state = int((Decimal(max(0, state_base)) * Decimal(str(state_pct or 0)) / 100)
                .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return YearTax(
        federal_tax_cents(ordinary_taxable, filing_status, year, index_pct),
        capital_gains_tax_cents(ordinary_taxable, gains_taxable, filing_status,
                                year, index_pct),
        niit_cents(int(investment_income_cents) + net_gains, ordinary + gains,
                   filing_status),
        state)


def standard_deduction_in(filing_status: str, qualifying_conditions: int,
                          year: int, index_pct) -> int:
    """The standard deduction for ``year``, indexed from the published table."""
    return indexed_cents(standard_deduction(filing_status, qualifying_conditions),
                         index_pct, year)


# ---------------------------------------------------------------------------
# Other income: rentals, royalties, a pension
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncomeSource:
    """Income from outside the retirement accounts, as an annual amount.

    ``amount_cents`` in ``start_year``, changing by ``change_pct`` a year after
    that (0 for flat rent, negative for royalties that fall off), through
    ``end_year`` when one is set. See db._V83.
    """

    id: int
    name: str
    amount_cents: int
    start_year: int
    end_year: Optional[int] = None
    change_pct: Decimal = Decimal(0)
    taxable: bool = True
    # A salary's 401(k): the employee deferral and the employer match, as
    # percentages of this source, going into ``into_account_id`` (db._V90).
    deferral_pct: Decimal = Decimal(0)
    match_pct: Decimal = Decimal(0)
    into_account_id: Optional[int] = None
    kind: str = "other"                     # salary | pension | investment | other
    person_id: Optional[int] = None         # whose salary (db._V92)
    # The survivor scenario: after ``survivor_after`` the source pays
    # ``survivor_pct`` of itself (a pension's survivor share). Never stored -
    # set by :func:`plan_income_sources`.
    survivor_after: Optional[int] = None
    survivor_pct: Decimal = Decimal(100)
    niit: bool = False                      # net investment income (db._V102)
    preferential: bool = False              # qualified dividends, long-term gains (db._V106)

    @property
    def investment_income(self) -> bool:
        """Net investment income for IRC 1411: the investment-income section
        always, other income (rent) when marked."""
        return self.kind == "investment" or bool(self.niit)

    def deferral_cents_in(self, year: int) -> int:
        """The employee's deferral out of this source in ``year``."""
        return _pct_of(self.cents_in(year), self.deferral_pct)

    def contribution_cents_in(self, year: int) -> int:
        """Deferral plus match: what goes into ``into_account_id`` in ``year``."""
        return _pct_of(self.cents_in(year), self.deferral_pct + self.match_pct)

    def cents_in(self, year: int) -> int:
        year = int(year)
        if year < self.start_year or (self.end_year is not None
                                      and year > self.end_year):
            return 0
        amount = household_amount_cents(self.amount_cents, self.change_pct,
                                        year - self.start_year)
        if self.survivor_after is not None and year > self.survivor_after:
            amount = _pct_of(amount, self.survivor_pct)
        return amount


def _pct_of(cents: int, pct) -> int:
    return int((Decimal(int(cents)) * Decimal(str(pct)) / 100)
               .quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:                       # a hand-edited row is not a crash
        return Decimal(0)


def _source(row) -> IncomeSource:
    try:
        pct = Decimal(str(row["change_pct"]))
    except Exception:
        pct = Decimal(0)
    return IncomeSource(id=int(row["id"]), name=row["name"],
                        deferral_pct=_decimal(_column(row, "deferral_pct") or 0),
                        match_pct=_decimal(_column(row, "match_pct") or 0),
                        into_account_id=_optional_int(_column(row, "into_account_id")),
                        kind=str(_column(row, "kind") or "other"),
                        person_id=_optional_int(_column(row, "person_id")),
                        niit=bool(_column(row, "niit") or 0),
                        preferential=bool(_column(row, "preferential") or 0),
                        amount_cents=int(row["amount_cents"]),
                        start_year=int(row["start_year"]),
                        end_year=_optional_int(row["end_year"]),
                        change_pct=pct, taxable=bool(row["taxable"]))


def list_income_sources(conn) -> list[IncomeSource]:
    """The sources as ENTERED - what the Income dialog edits. A projection
    reads :func:`plan_income_sources`, which applies the survivor scenario."""
    rows = conn.execute(
        "SELECT * FROM retirement_income_sources ORDER BY start_year, name, id"
    ).fetchall()
    return [_source(r) for r in rows]


def plan_income_sources(conn, scenario=None) -> list[IncomeSource]:
    """The sources as the PLAN sees them: under the survivor scenario the
    deceased's salary ends with the death year and a pension of theirs pays
    its survivor share after it. Investment and other income go on - the
    assets pass to the survivor."""
    import dataclasses
    found = list_income_sources(conn)
    scenario = get_survivor_scenario(conn) if scenario is None else scenario
    if not scenario:
        return found
    out = []
    for src in found:
        if src.person_id == scenario.deceased_id:
            if src.kind == "salary":
                end = scenario.death_year if src.end_year is None \
                    else min(src.end_year, scenario.death_year)
                src = dataclasses.replace(src, end_year=end)
            elif src.kind == "pension":
                src = dataclasses.replace(src, survivor_after=scenario.death_year,
                                          survivor_pct=scenario.pension_pct)
        out.append(src)
    return out


def add_income_source(conn, name: str, amount_cents: int, start_year: int, *,
                      end_year: Optional[int] = None, change_pct=0,
                      taxable: bool = True, kind: str = "other") -> int:
    if not (name or "").strip():
        raise ValueError("an income source needs a name")
    cur = conn.execute(
        "INSERT INTO retirement_income_sources "
        "(name, amount_cents, start_year, end_year, change_pct, taxable, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name.strip(), _magnitude(amount_cents), int(start_year),
         _optional_int(end_year), str(Decimal(str(change_pct))), 1 if taxable else 0,
         kind),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_income_source(conn, source_id: int, **fields) -> None:
    allowed = {"name", "amount_cents", "start_year", "end_year", "change_pct",
               "deferral_pct", "match_pct", "into_account_id", "person_id",
               "taxable", "niit", "preferential"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown income source field(s): {sorted(bad)}")
    values = dict(fields)
    if "amount_cents" in values:
        values["amount_cents"] = _magnitude(values["amount_cents"])
    for key in ("change_pct", "deferral_pct", "match_pct"):
        if key in values:
            value = Decimal(str(values[key]))
            if key != "change_pct" and not (0 <= value <= 100):
                raise ValueError(f"{key.replace('_', ' ')} is a percentage, 0 to 100")
            values[key] = str(value)
    for key in ("into_account_id", "person_id"):
        if key in values:
            values[key] = _optional_int(values[key])
    if "taxable" in values:
        values["taxable"] = 1 if values["taxable"] else 0
    if not values:
        return
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE retirement_income_sources SET {sets} WHERE id = ?",
                 [*values.values(), int(source_id)])
    conn.commit()


def salary_source(conn, person_id: Optional[int] = None,
                  kind: str = "salary") -> Optional[IncomeSource]:
    """A person's salary (or pension, with ``kind``), or None. Without a
    person, the first one on file."""
    return next((src for src in list_income_sources(conn)
                 if src.kind == kind
                 and (person_id is None or src.person_id == int(person_id))), None)


def trailing_investment_income_cents(conn, as_of: str) -> int:
    """Dividends, capital-gain distributions and interest paid in the last 365
    days by the household's TAXABLE investment accounts - the starting figure
    for the Income dialog's investment-income line. Retirement accounts are
    left out: their income stays inside them and is not taxed as it is paid.
    The action set is the one the holdings view totals as dividends."""
    from mammon import investments, ledger, rebalance

    end = _dt.date.fromisoformat(as_of)
    start = (end - _dt.timedelta(days=364)).isoformat()
    taxable = [int(a["id"]) for a in ledger.list_accounts(conn)
               if rebalance.account_treatment(a) in ("", "taxable")]
    if not taxable:
        return 0
    marks = ",".join("?" * len(taxable))
    actions = sorted(investments._DIVIDEND_ACTIONS)
    rows = conn.execute(
        f"SELECT amount FROM investment_transactions WHERE account_id IN ({marks}) "
        f"AND date BETWEEN ? AND ? AND LOWER(action) IN "
        f"({','.join('?' * len(actions))})",
        [*taxable, start, end.isoformat(), *actions]).fetchall()
    return sum(abs(int(r[0] or 0)) for r in rows)


def employer_plan_ends(conn, default_year: Optional[int]) -> dict[int, int]:
    """Account id -> the first year a CURRENT employer's plan is a former one.

    The year after the salary that funds it (its ``into_account_id``) ends -
    each earner retires on their own schedule (reported); otherwise ``default_year``, the household's retirement.
    """
    out: dict[int, int] = {}
    for src in plan_income_sources(conn):
        if src.kind == "salary" and src.into_account_id is not None \
                and src.end_year is not None:
            out[int(src.into_account_id)] = int(src.end_year) + 1
    if default_year is not None:
        for acct in plan_accounts(conn):
            if acct.current_employer_plan and acct.account_id not in out:
                out[acct.account_id] = int(default_year)
    return out


def delete_income_source(conn, source_id: int) -> None:
    conn.execute("DELETE FROM retirement_income_sources WHERE id = ?",
                 (int(source_id),))
    conn.commit()


def other_income_cents(conn, year: int, *, taxable_only: bool = False,
                       sources: Optional[Sequence[IncomeSource]] = None) -> int:
    """Every other-income source's amount in ``year``, in cents."""
    found = plan_income_sources(conn) if sources is None else sources
    return sum(src.cents_in(year) for src in found
               if src.taxable or not taxable_only)


def clear_withdrawal_plan(conn) -> None:
    conn.execute("DELETE FROM retirement_withdrawal_plan WHERE id = 1")
    conn.commit()


def household_amount_cents(start_cents: int, increase_pct, offset: int) -> int:
    """The household's draw ``offset`` years after the first planned one.

    ``start * (1 + pct/100) ** offset``, in :class:`~decimal.Decimal` and
    rounded half up to the cent. The compounding is the cost-of-living part: a
    level amount is a shrinking amount, and forty years of it is the plan
    failing quietly rather than being refused.
    """
    start = _magnitude(start_cents)
    step = int(offset)
    if step < 0:
        raise ValueError("a plan year cannot precede the first plan year")
    rate = Decimal(1) + (Decimal(str(increase_pct)) / Decimal(100))
    if rate < 0:
        rate = Decimal(0)
    amount = Decimal(start) * (rate ** step)
    return max(0, int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def apportion_cents(total_cents: int, weights: Mapping[int, int]) -> dict[int, int]:
    """Split ``total_cents`` across ``weights`` so the parts sum EXACTLY to it.

    Largest remainder: every key takes its floor share, then the cents left over
    go one each to the largest remainders, ties to the larger weight and then to
    the lower key. Proportional rounding done any other way loses or invents
    cents, and a withdrawal table whose row does not add up to the amount the
    user typed is the table being wrong about the one number they chose.
    """
    total = int(total_cents)
    keys = list(weights)
    pool = sum(max(0, int(weights[k])) for k in keys)
    if total <= 0 or pool <= 0 or not keys:
        return {k: 0 for k in keys}
    shares: dict[int, int] = {}
    order: list[tuple[int, int, int]] = []
    assigned = 0
    for key in keys:
        weight = max(0, int(weights[key]))
        numerator = total * weight
        base = numerator // pool
        shares[key] = base
        assigned += base
        order.append((numerator - base * pool, weight, int(key)))
    order.sort(key=lambda t: (-t[0], -t[1], t[2]))
    for i in range(total - assigned):
        shares[order[i % len(order)][2]] += 1
    return shares


def _draw_proportionally(need_cents: int, account_ids: Sequence[int],
                         balances: Mapping[int, int],
                         assigned: dict[int, int]) -> int:
    """Take ``need_cents`` more from these accounts by balance. Returns the shortfall.

    Weighted by the balance ENTERING the year, not by what is still unassigned,
    so the floors an account has already paid count toward its share. Each pass
    either finishes the need or empties at least one account, and the emptied
    ones drop out of the next pass - that redistribution is the whole point.
    """
    remaining = max(0, int(need_cents))
    active = [int(a) for a in account_ids
              if int(balances.get(int(a), 0)) - assigned.get(int(a), 0) > 0]
    while remaining > 0 and active:
        weights = {a: max(0, int(balances.get(a, 0))) for a in active}
        if sum(weights.values()) <= 0:
            break
        shares = apportion_cents(remaining, weights)
        moved = 0
        for account_id in active:
            room = int(balances.get(account_id, 0)) - assigned.get(account_id, 0)
            give = min(shares.get(account_id, 0), room)
            if give > 0:
                assigned[account_id] = assigned.get(account_id, 0) + give
                moved += give
        if moved <= 0:                      # belt and braces; cannot happen
            break
        remaining -= moved
        active = [a for a in active
                  if int(balances.get(a, 0)) - assigned.get(a, 0) > 0]
    return remaining


@dataclass(frozen=True)
class HouseholdYear:
    """One year of the household plan, after the floors and the redistribution."""

    year: int
    target_cents: int                   # spending asked for, plus the tax
    drawn_cents: int                    # what the pool could actually fund
    floor_cents: int                    # the sum of that year's required minimums
    pool_cents: int                     # what the whole pool held entering the year
    amounts: Mapping[int, int]          # account id -> cents out
    balances: Mapping[int, int]         # account id -> cents entering the year
    tax_cents: int = 0                  # income tax paid out of the accounts
    tax_deferred_cents: int = 0         # the extra IRA draw that paying it took
    surcharge_cents: int = 0            # IRMAA paid out of the accounts
    gains_cents: int = 0                # net gains realized selling (a loss is negative)
    penalty_cents: int = 0              # the 10% on early distributions (in tax_cents)
    roth_taxable_cents: int = 0         # Roth earnings drawn before the account's 5th year

    @property
    def raised_by_floor(self) -> bool:
        """The required minimums alone already exceeded what was asked for."""
        return self.drawn_cents > self.target_cents

    @property
    def shortfall_cents(self) -> int:
        return max(0, self.target_cents - self.drawn_cents)


@dataclass(frozen=True)
class HouseholdPlan:
    """What a starting amount and an annual increase do to the whole pool."""

    years: Sequence[HouseholdYear]
    depleted_year: Optional[int] = None
    depleted_pool_cents: int = 0

    @property
    def lasts(self) -> bool:
        return self.depleted_year is None

    @property
    def floor_raised_years(self) -> list[int]:
        return [y.year for y in self.years if y.raised_by_floor]

    def amounts_by_account(self) -> dict[int, dict[int, int]]:
        """account id -> year -> cents, which is the shape the writer wants."""
        out: dict[int, dict[int, int]] = {}
        for entry in self.years:
            for account_id, cents in entry.amounts.items():
                out.setdefault(int(account_id), {})[entry.year] = int(cents)
        return out


def carry_forward(entering_cents: int, net_cents: int, factor) -> int:
    """One account, one year: what it enters the NEXT year holding.

    The year's net flow (withdrawals and conversions out negative, conversions
    in positive) comes off first, the balance is floored at zero - an account
    that runs dry stops paying rather than going into debt - and then the
    year's median growth factor applies. With a tracked pool (see
    :func:`plan_household_withdrawals`) these per-account balances only
    WEIGHT the split; how much there is comes from the pool.
    """
    left = max(0, int(entering_cents) + int(net_cents))
    factor = Decimal(str(factor))
    if left and factor != 1:
        left = int((Decimal(left) * factor).quantize(Decimal(1),
                                                     rounding=ROUND_HALF_UP))
    return max(0, left)


def plan_household_withdrawals(
    years: Sequence[int],
    accounts: Sequence[PlanAccount],
    balances: Mapping[int, int],
    *,
    start_cents: int,
    increase_pct=0,
    floor_cents: Optional[Callable[[int, int], int]] = None,
    growth: Optional[Callable[[int, int], Decimal]] = None,
    other_net_cents: Optional[Callable[[int, int], int]] = None,
    pool=None,
    start_year: Optional[int] = None,
    covered_cents: Optional[Callable[[int], int]] = None,
    deferred_room_cents: Optional[Callable] = None,
    rmd_cents: Optional[Callable[[int, int, int], int]] = None,
    employer_until: Optional[int] = None,
    tax_cents: Optional[Callable[[int, int], int]] = None,
    spending_order: Sequence[str] = SPENDING_STEPS,
    exempt_ids: Iterable[int] = (),
    surcharge_cents: Optional[Callable[[int, Sequence["HouseholdYear"], int], int]] = None,
    spending_scale: Optional[Callable[[int], object]] = None,
    basis_cents: Optional[Mapping[int, object]] = None,
    tax_paid_from: str = "spending",
    early_ids: Optional[Callable[[int], Iterable[int]]] = None,
    conversions_in: Optional[Callable[[int, int], int]] = None,
    basis_step_up: Optional[tuple[int, object]] = None,
) -> HouseholdPlan:
    """Spread one household amount per year across the whole retirement pool.

    ``balances`` is what each account holds entering the FIRST year; every later
    year's balance is simulated here, because the split depends on the balances
    and the balances depend on the split. ``growth`` returns the factor carrying
    an account from one year into the next (1 when no growth can be measured - a
    plan has to exist even when a return assumption does not), and
    ``other_net_cents`` is everything else the plan does to the account,
    principally Roth conversions.

    ``floor_cents(account_id, year)`` is the required minimum. It is asked only
    about accounts that are :attr:`PlanAccount.floored`, and its answer is
    capped at what the account actually holds: the law cannot make an empty IRA
    distribute, and a floor above the balance would drive it negative.
    ``rmd_cents(account_id, year, balance)``, when given, replaces it and is
    handed the balance the account ENTERS the year with - the prior December 31
    balance IRC 401(a)(9) divides - as this simulation has carried it forward.
    Reported: the minimum was computed from TODAY's balance for every year,
    too low once the accounts had grown and too high once they were drawn down.
    The minimums of one owner's IRAs (:attr:`PlanAccount.is_ira`) are then
    totaled and drawn from those IRAs by balance, the exempt ones last
    (Reg. 1.408-8): an IRA the household leaves alone pays its minimum out
    of the others.

    ``pool`` (a ``forecast.TrackedFund`` over the whole pool) decides HOW MUCH
    each year can draw: at most the pool's median entering the year, from a
    recursion that carries the mean and variance and floors the fund at zero -
    the same one the page's fund line draws, so the notice and the chart agree
    about when the money runs out. Reported: without it the line showed a large
    balance in the very year the notice said the money was gone. The per-account
    balances then only weight how that amount is split. Without a pool (no
    measurable mix) the per-account balances cap the draws themselves.

    ``start_cents`` is a SPENDING need, from ``start_year`` (the first plan year
    when None) rising ``increase_pct`` a year; before it nothing is drawn but
    the required minimums. ``covered_cents(year)`` is the income that already
    pays part of it - Social Security, rentals, royalties - so only the rest
    comes out of the accounts. ``deferred_room_cents(year)`` - or ``(year,
    gains)``, handed the year's realized gains, which pull more Social Security
    into income - when given, is the most the TAX-DEFERRED accounts may pay that
    year (the room under a bracket top): they draw up to it, Roth pays the rest,
    and only when Roth runs dry does a deferred draw go over the line. Required
    minimums are owed either way.

    ``tax_cents(year, deferred_draw_cents, gains_cents)``, when given, is the
    income tax the accounts must pay that year given that year's taxable draws
    (the tax-deferred ones plus any Roth earnings drawn before the account's
    fifth year) and the year's NET gains, negative for a net loss after the
    carryforward; it is added to the year's need and drawn in the same order.

    ``surcharge_cents(year, earlier_years, deferred_draw_cents, gains_cents)``,
    when given, is a cost the accounts pay that year - the IRMAA surcharge, set
    by income two years back, or after an SSA-44 appeal by the year's own - and
    joins the need like the tax, settled with it.

    ``basis_cents`` is each TAXABLE account's positions, ``[(value, basis),
    ...]`` as of today (cash is a position whose basis is its value), or one
    basis figure for the whole account. The positions follow the account's
    balance; a sale takes the CHEAPEST first - a loss position, then the
    smallest gain per dollar (specific identification, IRC 1012), never the
    account's average. A year's net loss offsets other gains, then up to
    $3,000 of ordinary income, and carries forward (IRC 1211(b)); every gain
    is long-term (the plan's sales are years off). ``basis_step_up`` is
    ``(year, share)``: from that year the positions' basis moves that share
    of the way to their value - the step-up at a death (IRC 1014), half of
    joint property, all of community property. With
    ``tax_paid_from="taxable"`` the tax is drawn from the taxable accounts
    before anything else (then in the order), so it no longer uses IRA bracket
    room a conversion could fill.

    ``early_ids(year)`` names the accounts whose owner is under 59 1/2 in
    ``year`` (IRC 72(t)). A tax-deferred one is drawn only after every other
    step has run dry, and every dollar out of it costs a further
    :data:`EARLY_DISTRIBUTION_TAX_PCT` percent, settled with the tax and
    reported as ``penalty_cents``. A Roth's draws come out of what it held
    today first, then its planned conversions oldest first
    (``conversions_in(account_id, year)``), then earnings (Reg. 1.408A-6): a
    conversion drawn within five years, and earnings, cost the same 10 percent
    while the owner is under 59 1/2 (IRC 408A(d)(3)(F)), and earnings drawn
    from a Roth that held nothing today before its fifth year are taxed as
    ordinary income (IRC 408A(d)(2)(B)), reported as ``roth_taxable_cents``.

    ``spending_order`` is the household's order after required minimums
    (:data:`SPENDING_STEPS`); ``exempt_ids`` are accounts never drawn for
    spending or tax - they still pay their own required minimums, and they are
    outside the ``pool``, which is then the DRAWABLE money only: running out
    means the drawable money ran out.

    Pure arithmetic in signed integer cents - no connection, no Qt, and nothing
    written. :func:`apportion_cents` guarantees each year's parts sum exactly to
    that year's total.
    """
    horizon = [int(y) for y in years]
    first_year = horizon[0] if (start_year is None or not horizon) else int(start_year)
    exempt = {int(a) for a in exempt_ids}
    order = normalize_spending_order(spending_order)
    tax_first = tax_paid_from == "taxable"
    deferred_ids = [a.account_id for a in accounts if a.treatment == "deferred"]
    all_roth_ids = [a.account_id for a in accounts if a.is_roth]
    roth_ids = [aid for aid in all_roth_ids if aid not in exempt]
    taxable_ids = [a.account_id for a in accounts
                   if a.is_taxable and a.account_id not in exempt]
    drawable_deferred = [aid for aid in deferred_ids if aid not in exempt]
    drawable = [int(a.account_id) for a in accounts if int(a.account_id) not in exempt]
    # Floored (owing a minimum) is decided PER YEAR: a current employer's plan
    # becomes a former employer's at ``employer_until`` (the retirement year).
    live = {int(a.account_id): max(0, int(balances.get(a.account_id, 0)))
            for a in accounts}

    # The bracket room may or may not take the year's gains (older callers).
    room_of = None
    if deferred_room_cents is not None:
        try:
            takes_gains = len(inspect.signature(deferred_room_cents).parameters) >= 2
        except (TypeError, ValueError):
            takes_gains = False
        room_of = (deferred_room_cents if takes_gains
                   else (lambda yr, _gains: deferred_room_cents(yr)))

    # Taxable positions: [value, basis] each, following the account's balance.
    lots: dict[int, list[list[int]]] = {}
    for key, held in (basis_cents or {}).items():
        aid = int(key)
        if isinstance(held, (list, tuple)):
            lots[aid] = [[max(0, int(v)), max(0, int(b))] for v, b in held]
        else:
            lots[aid] = [[live.get(aid, 0), max(0, int(held))]]
    loss_carry = 0
    step_up_year = None if basis_step_up is None else int(basis_step_up[0])
    step_up_share = (Decimal(0) if basis_step_up is None
                     else Decimal(str(basis_step_up[1])))

    def cheapest_first(held):
        return sorted(held, key=lambda lot: (Fraction(lot[0] - lot[1], lot[0])
                                             if lot[0] > 0 else Fraction(0)))

    def sale_gain(aid: int, sold: int) -> int:
        """The net gain (signed) of selling ``sold`` from this account's
        positions, cheapest first. Reads only."""
        held = lots.get(aid)
        if not held or sold <= 0:
            return 0
        total, left = 0, int(sold)
        for value, basis in cheapest_first(held):
            if left <= 0:
                break
            take = min(left, value)
            if value > 0 and take > 0:
                total += take * (value - basis) // value
            left -= take
        return total

    def sell(aid: int, sold: int) -> None:
        held = lots.get(aid)
        if not held or sold <= 0:
            return
        left = int(sold)
        for lot in cheapest_first(held):
            if left <= 0:
                break
            take = min(left, lot[0])
            if lot[0] > 0 and take > 0:
                lot[1] -= lot[1] * take // lot[0]
                lot[0] -= take
            left -= take

    # Roth layers: what each held today (penalty-free), then conversions in.
    roth_free = {aid: live.get(aid, 0) for aid in all_roth_ids}
    roth_layers: dict[int, list[list[int]]] = {aid: [] for aid in all_roth_ids}
    roth_opened: dict[int, int] = {}      # first conversion year, when empty today

    def roth_parts(aid: int, drawn: int, year: int) -> tuple[int, int, int, int]:
        """(from today's holdings, from conversions under five years old, from
        older conversions, from earnings) of a draw. Reads only."""
        free = min(int(drawn), roth_free.get(aid, 0))
        rest = int(drawn) - free
        young = old = 0
        for layer_year, amount in roth_layers.get(aid, ()):
            if rest <= 0:
                break
            take = min(rest, amount)
            if layer_year + 5 > year:
                young += take
            else:
                old += take
            rest -= take
        return free, young, old, rest

    def roth_consume(aid: int, drawn: int, year: int) -> None:
        free, young, old, _earnings = roth_parts(aid, drawn, year)
        roth_free[aid] = roth_free.get(aid, 0) - free
        rest = young + old
        layers = roth_layers.get(aid, [])
        while rest > 0 and layers:
            take = min(rest, layers[0][1])
            layers[0][1] -= take
            rest -= take
            if layers[0][1] <= 0:
                layers.pop(0)

    entries: list[HouseholdYear] = []
    depleted_year: Optional[int] = None
    depleted_pool = 0

    for year in horizon:
        if year < first_year:
            target = 0
        else:
            target = household_amount_cents(start_cents, increase_pct,
                                            year - first_year)
            if spending_scale is not None:
                # The survivor scenario: one person lives on less than two.
                target = int((Decimal(target) * Decimal(str(spending_scale(year))))
                             .quantize(Decimal(1), rounding=ROUND_HALF_UP))
            if covered_cents is not None:
                target = max(0, target - max(0, int(covered_cents(year))))
        floored_ids = [a.account_id for a in accounts
                       if a.floored_in(year, employer_until)]
        entering = dict(live)
        if pool is not None and sum(entering.get(k, 0) for k in drawable) > 0:
            # The pool says how much there IS; the accounts only say whose it
            # is. Rescale them to the pool's median every year, shares kept, so
            # the two cannot drift apart. Reported: past 100 the accounts had
            # all reached $0 on their own median-return path while the pool
            # still held millions, and the split fell back to drawing evenly from
            # every account - "spending IRA again" after the IRAs were empty.
            # Only the drawable accounts: an exempt one is outside the pool and
            # carries its own balance forward.
            entering.update(apportion_cents(max(0, int(pool.median_cents())),
                                            {k: entering[k] for k in drawable}))
        pool_total = sum(entering.values())
        # The positions follow the account: scaled to what it enters with (the
        # growth is all gain - a basis never rises), stepped up at a death.
        for aid, held in lots.items():
            value = int(entering.get(aid, 0))
            total_value = sum(lot[0] for lot in held)
            if total_value > 0 and value != total_value:
                scaled = apportion_cents(value, {i: lot[0] for i, lot in enumerate(held)})
                for i, lot in enumerate(held):
                    lot[0] = scaled.get(i, 0)
            elif total_value <= 0 and value > 0:
                held[:] = [[value, value]]
            if step_up_year is not None and year == step_up_year and step_up_share > 0:
                for lot in held:
                    lot[1] += int((Decimal(lot[0] - lot[1]) * step_up_share)
                                  .quantize(Decimal(1), rounding=ROUND_HALF_UP))

        # Every account gets a key even when its share is zero: a year left out
        # of the plan is an UNPLANNED year, which seed_withdrawals would refill
        # with a required minimum computed off today's balance - putting money
        # back into an account this plan has already spent.
        floors: dict[int, int] = {}
        for account_id in floored_ids:
            if rmd_cents is not None:
                required = max(0, int(rmd_cents(account_id, year,
                                                entering.get(account_id, 0))))
            else:
                required = (0 if floor_cents is None
                            else max(0, int(floor_cents(account_id, year))))
            floors[account_id] = min(required, entering.get(account_id, 0))
        _aggregate_ira_floors(floors, accounts, entering, exempt)
        floors_total = sum(floors.values())
        early = {int(a) for a in (early_ids(year) if early_ids is not None else ())}
        regular_deferred = [a for a in drawable_deferred if a not in early]
        early_deferred = [a for a in drawable_deferred if a in early]

        def converting_into(aid: int) -> bool:
            """Whether ``aid`` receives a planned conversion this year."""
            return (conversions_in is not None
                    and int(conversions_in(int(aid), year)) > 0)

        # What spending may draw from: the balance entering the year LESS what
        # this year's conversions take out of it (net of any contribution
        # arriving). A required minimum comes first, out of the whole entering
        # balance (the law has it taken before anything is converted); a
        # conversion next; spending from what is left. Drawing spending from
        # the entering balance alone let the plan spend money a conversion had
        # already moved, and the page then cut the conversion on every write
        # (reported: "Re-applying the plan should produce exactly the same
        # results").
        available = dict(entering)
        if other_net_cents is not None:
            for aid in available:
                moved = int(other_net_cents(aid, year))
                if moved < 0:
                    available[aid] = max(0, int(available[aid]) + moved)

        def split(need: int, tax_first: int = 0,
                  gains: int = 0) -> tuple[dict[int, int], int]:
            """Draw ``need`` in the household's order. (amounts, shortfall).
            ``tax_first`` of it comes from the taxable accounts before that."""
            assigned: dict[int, int] = {int(a.account_id): 0 for a in accounts}
            assigned.update(floors)
            left = max(0, need - floors_total)
            if tax_first:
                pay = min(int(tax_first), left)
                left = left - pay + _draw_proportionally(pay, taxable_ids,
                                                         available, assigned)
            # The household's order (reported: make it adjustable, and let an
            # account be left out). The default puts taxable brokerage money
            # between the IRA room and the Roth: a sale is taxed only on its
            # gain, at capital-gains rates. Exempt accounts are in no step.
            room = (None if room_of is None
                    else max(0, int(room_of(year, gains)) - floors_total))
            for step in order:
                if step == "deferred":
                    if room is None:
                        left = _draw_proportionally(
                            left, [a for a in floored_ids
                                   if a not in exempt and a not in early],
                            available, assigned)
                        left = _draw_proportionally(
                            left, [a for a in regular_deferred if a not in floored_ids],
                            available, assigned)
                    else:
                        inside = min(left, room)
                        left = left - inside + _draw_proportionally(
                            inside, regular_deferred, available, assigned)
                elif step == "taxable":
                    left = _draw_proportionally(left, taxable_ids, available, assigned)
                elif step == "roth":
                    # Never a Roth that is being converted INTO this year:
                    # converting C and drawing R from it is the same tax and
                    # the same balances as converting C - R and drawing R from
                    # the IRA, and the IRA beyond the target pays it instead
                    # (below). The page used to rewrite that AFTER the plan -
                    # shrinking the stored conversion - and a re-apply of the
                    # unchanged plan then drew the Roth again and shrank it
                    # again (reported: "Re-applying the plan should produce
                    # exactly the same results"). Decided here, the plan
                    # reproduces itself.
                    left = _draw_proportionally(
                        left, [aid for aid in roth_ids if not converting_into(aid)],
                        available, assigned)
                elif step == "deferred_over" and room is not None:
                    left = _draw_proportionally(left, regular_deferred, available,
                                                assigned)
            # A Roth converted into this year is the last resort: only when
            # nothing else can pay.
            left = _draw_proportionally(
                left, [aid for aid in roth_ids if converting_into(aid)],
                available, assigned)
            if left > 0 and early_deferred:
                # Last of all: an owner under 59 1/2 pays 10% more on every
                # dollar out (IRC 72(t)), so nothing else may be left first.
                left = _draw_proportionally(left, early_deferred, available, assigned)
            return assigned, left

        def deferred_of(amounts: Mapping[int, int]) -> int:
            return sum(int(amounts.get(aid, 0)) for aid in deferred_ids)

        def gains_of(amounts: Mapping[int, int]) -> int:
            """The net gain (signed) these taxable-account sales realize."""
            return sum(sale_gain(aid, int(amounts.get(aid, 0))) for aid in lots)

        def net_gains_for_tax(raw: int) -> int:
            """This year's gains after the loss carried in: positive, or the
            part of a net loss that comes off ordinary income, negative."""
            net = int(raw) - loss_carry
            if net < 0:
                return -min(CAPITAL_LOSS_LIMIT_CENTS, -net)
            return net

        def roth_taxable_of(amounts: Mapping[int, int]) -> int:
            """Roth earnings drawn while the owner is under 59 1/2, or before
            the account's fifth year (it held nothing today, so it opened
            with its first conversion): ordinary income."""
            total = 0
            for aid in all_roth_ids:
                opened = roth_opened.get(aid)
                young_account = opened is not None and year < opened + 5
                # Earnings are taxable until the owner is 59 1/2 AND the
                # account is five years old (IRC 408A(d)(2)(A)).
                if not young_account and aid not in early:
                    continue
                total += roth_parts(aid, int(amounts.get(aid, 0)), year)[3]
            return total

        def penalty_of(amounts: Mapping[int, int]) -> int:
            """The additional tax on this year's early distributions: every
            dollar out of a tax-deferred account, and out of a Roth the
            conversions under five years old and the earnings."""
            drawn = sum(int(amounts.get(aid, 0)) for aid in early if aid in deferred_ids)
            for aid in all_roth_ids:
                if aid in early:
                    _free, young, _old, earnings = roth_parts(aid, int(amounts.get(aid, 0)), year)
                    drawn += young + earnings
            return drawn * EARLY_DISTRIBUTION_TAX_PCT // 100

        # The tax is part of the year's need (reported: the planner computed the
        # tax and never paid it, so every plan looked richer than it was). It
        # depends on the IRA draws, and an IRA draw that pays it is taxed too,
        # so the year is re-split until the tax stops moving - each pass moves
        # it by the marginal rate of the last, so a handful of passes settle it.
        assigned, short = split(target)
        surcharge = tax = penalty = 0
        if tax_cents is not None or surcharge_cents is not None or early:
            # Both depend on the year's IRA draws, and the draws that pay them
            # are income too: re-split until neither moves. A surcharge tier
            # is a cliff, so a year can flip between two; the pass cap ends it.
            # The 10% on an early distribution rides with the tax.
            for _ in range(12):
                taxable_draws = deferred_of(assigned) + roth_taxable_of(assigned)
                realized = net_gains_for_tax(gains_of(assigned))
                owed = penalty_of(assigned) + (
                    0 if tax_cents is None else
                    max(0, int(tax_cents(year, taxable_draws, realized))))
                charged = (0 if surcharge_cents is None
                           else max(0, int(surcharge_cents(year, entries,
                                                           taxable_draws, realized))))
                if owed == tax and charged == surcharge:
                    break
                tax, surcharge = owed, charged
                assigned, short = split(target + surcharge + tax,
                                        tax if tax_first else 0, realized)
        spend_deferred = (deferred_of(split(target + surcharge, 0,
                                            net_gains_for_tax(gains_of(assigned)))[0])
                          if tax else deferred_of(assigned))
        need = target + surcharge + tax
        tax_deferred = max(0, deferred_of(assigned) - spend_deferred)

        if pool is not None:
            # Once the money has run out it stays out: after the run-out year
            # nothing more is drawn. Reported: past it the plan kept drawing
            # the median of what was left - small, tapering sums "way below
            # the spend rate" - because in the median case the fund sits right
            # at the edge of empty rather than at zero.
            capacity = (0 if depleted_year is not None
                        else max(0, int(pool.median_cents())))
            want = max(need, floors_total)
            # The pool is the drawable money; an exempt account's minimum
            # comes out of the exempt account, not out of the pool.
            outside = sum(assigned.get(k, 0) for k in exempt)
            pool_want = max(0, want - outside)
            allowed = min(pool_want, capacity)
            inside = {k: assigned.get(k, 0) for k in drawable}
            drawn_now = sum(inside.values())
            if drawn_now > allowed:
                inside = apportion_cents(allowed, inside)
            elif drawn_now < allowed and drawable:
                weights = {k: entering.get(k, 0) for k in drawable
                           if entering.get(k, 0) > 0} or {k: 1 for k in drawable}
                for key, extra in apportion_cents(allowed - drawn_now,
                                                  weights).items():
                    inside[key] = inside.get(key, 0) + extra
            assigned = {int(a.account_id): (inside.get(int(a.account_id), 0)
                                            if int(a.account_id) in inside
                                            else assigned.get(int(a.account_id), 0))
                        for a in accounts}
            short = pool_want - allowed
            pool_net = -sum(inside.values())
            if other_net_cents is not None:
                pool_net += sum(int(other_net_cents(k, year)) for k in drawable)
            pool.step_year(pool_net)
            pool_value = capacity
        else:
            pool_value = pool_total

        drawn = sum(assigned.values())
        penalty = penalty_of(assigned)
        roth_taxable = roth_taxable_of(assigned)
        # Settle the year: the positions sold, the loss carried forward, the
        # Roth layers drawn and this year's conversions layered in.
        raw = gains_of(assigned)
        for aid in lots:
            sell(aid, int(assigned.get(aid, 0)))
        net = raw - loss_carry
        if net < 0:
            used = min(CAPITAL_LOSS_LIMIT_CENTS, -net)
            loss_carry = -net - used
            realized = -used
        else:
            loss_carry = 0
            realized = net
        for aid in all_roth_ids:
            roth_consume(aid, int(assigned.get(aid, 0)), year)
            converted_in = (0 if conversions_in is None
                            else max(0, int(conversions_in(aid, year))))
            if converted_in > 0:
                if aid not in roth_opened and roth_free.get(aid, 0) <= 0 \
                        and not roth_layers.get(aid):
                    roth_opened[aid] = year
                roth_layers.setdefault(aid, []).append([year, converted_in])
        entries.append(HouseholdYear(
            year=year, target_cents=need, drawn_cents=drawn,
            floor_cents=floors_total, pool_cents=pool_value,
            amounts=dict(assigned), balances=entering,
            tax_cents=tax, tax_deferred_cents=tax_deferred,
            surcharge_cents=surcharge, gains_cents=realized,
            penalty_cents=penalty, roth_taxable_cents=roth_taxable,
        ))
        if short > 0 and depleted_year is None:
            depleted_year = year
            depleted_pool = pool_value

        for account_id in list(live):
            net_flow = -assigned.get(account_id, 0)
            if other_net_cents is not None:
                net_flow += int(other_net_cents(account_id, year))
            live[account_id] = carry_forward(
                entering.get(account_id, 0), net_flow,
                Decimal(1) if growth is None else growth(account_id, year))

    return HouseholdPlan(years=entries, depleted_year=depleted_year,
                         depleted_pool_cents=depleted_pool)


def _aggregate_ira_floors(floors: dict[int, int], accounts: Sequence[PlanAccount],
                          entering: Mapping[int, int], exempt: set) -> None:
    """One owner's IRA minimums may be totaled and taken from any of their
    IRAs (Reg. 1.408-8): the total is drawn from those IRAs by balance, the
    exempt ones only for what the others cannot cover. Edits ``floors``.
    Employer plans and inherited accounts each pay their own."""
    groups: dict[object, list[int]] = {}
    for acct in accounts:
        if (acct.is_ira and acct.treatment == "deferred"
                and acct.inherited_death_year is None and acct.account_id in floors):
            groups.setdefault(acct.owner_person_id, []).append(int(acct.account_id))
    for ids in groups.values():
        total = sum(floors.get(a, 0) for a in ids)
        if total <= 0 or len(ids) < 2:
            continue
        assigned = {a: 0 for a in ids}
        left = _draw_proportionally(total, [a for a in ids if a not in exempt],
                                    entering, assigned)
        _draw_proportionally(left, [a for a in ids if a in exempt], entering, assigned)
        for a in ids:
            floors[a] = assigned[a]
