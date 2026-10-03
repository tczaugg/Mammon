"""The retirement planner's rule tables and plan model (mammon/retirement.py).

Two different kinds of test live here, and they fail for different reasons.

The rule-table tests check TRANSCRIPTION, not arithmetic: a divisor or a
threshold is right or wrong by comparison with what the government published,
and the citation is in the assertion's comment so the next reader can check it
against the same document rather than against a memory of it. The divisors
below are from TD 9930 (26 CFR 1.401(a)(9)-9(c)), reprinted as IRS Pub 590-B
Appendix B Table III.

The plan tests check the read/write helpers and the per-year rollup. All data
is synthetic.
"""
from __future__ import annotations

import datetime as _dt
from decimal import ROUND_HALF_UP, Decimal

import pytest

from mammon import ledger, retirement
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "retire.db")
    yield c
    c.close()


@pytest.fixture
def accts(conn):
    return {
        "ira": ledger.create_account(conn, "Traditional IRA", "investment"),
        "k401": ledger.create_account(conn, "Workplace 401k", "investment"),
        "roth": ledger.create_account(conn, "Roth IRA", "investment"),
    }


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------
def _public_rule_tables() -> dict:
    """Every public RuleTable in the module, found by looking rather than listing.

    Found by introspection on purpose: a hand-written list would pass forever
    after someone adds an eighth table and forgets to append it, which is the
    one case this test exists for.
    """
    return {
        name: value
        for name, value in vars(retirement).items()
        if not name.startswith("_") and isinstance(value, retirement.RuleTable)
    }


def test_every_rule_table_says_where_it_came_from():
    tables = _public_rule_tables()
    # The tables that existed when this was written, so introspection finding
    # nothing would fail loudly instead of passing over an empty dict.
    assert {"RMD_APPLICABLE_AGE", "UNIFORM_LIFETIME_TABLE", "SS_BEND_POINTS",
            "SS_PIA_FACTORS", "SS_FULL_RETIREMENT_AGE", "FEDERAL_POVERTY_LEVEL",
            "IRMAA_TIERS", "TAX_BRACKETS", "STANDARD_DEDUCTION",
            "CONTRIBUTION_LIMITS", "ACA_PREMIUM_CREDIT_CLIFF"} <= set(tables)

    for name, table in tables.items():
        p = table.provenance
        assert p.table.strip(), f"{name} has no table name"
        assert p.publisher.strip(), f"{name} names no publisher"
        assert p.source.startswith("http"), f"{name} has no source URL"
        assert 1900 < p.effective_year < 2200, f"{name} has no effective year"
        assert p.volatility in retirement.VOLATILITIES
        assert p.describe().startswith(p.table)


def test_staleness_is_about_the_republication_date_not_the_calendar_year():
    stable = retirement.SS_PIA_FACTORS.provenance
    assert stable.is_stale(_dt.date(2200, 1, 1)) is False      # fixed by statute

    indexed = retirement.Provenance(
        table="x", effective_year=2026, volatility="indexed", publisher="p",
        source="https://example.invalid", republished_by="11-01")
    assert indexed.is_stale(_dt.date(2026, 12, 31)) is False   # current year
    assert indexed.is_stale(_dt.date(2027, 3, 1)) is False     # replacement not due
    assert indexed.is_stale(_dt.date(2027, 11, 1)) is True     # replacement is out
    assert indexed.is_stale(_dt.date(2028, 1, 1)) is True      # two years behind


def test_a_figure_cites_the_worst_of_the_tables_it_used():
    today = _dt.date(2028, 6, 1)
    fresh = retirement.Provenance(table="fresh", effective_year=2028,
                                  volatility="indexed", publisher="p",
                                  source="https://example.invalid")
    old = retirement.Provenance(table="old", effective_year=2025,
                                volatility="indexed", publisher="p",
                                source="https://example.invalid")
    assert retirement.worst_provenance([fresh, old], today).table == "old"
    assert retirement.worst_provenance([fresh], today).table == "fresh"
    assert retirement.worst_provenance([], today) is None


def test_the_unknown_volatility_is_refused_at_construction():
    with pytest.raises(ValueError):
        retirement.Provenance(table="x", effective_year=2026, volatility="whenever",
                              publisher="p", source="https://example.invalid")


# ---------------------------------------------------------------------------
# the RMD applicable age (SECURE 2.0 sec. 107)
# ---------------------------------------------------------------------------
def test_applicable_age_by_birth_year():
    assert retirement.applicable_age(1949) == 72      # SECURE 1.0 cohorts
    assert retirement.applicable_age(1950) == 72
    assert retirement.applicable_age(1951) == 73      # first SECURE 2.0 cohort
    assert retirement.applicable_age(1955) == 73
    # 1959 is the statute's drafting overlap; IRS REG-103529-23 resolves it as 73.
    assert retirement.applicable_age(1959) == 73
    assert retirement.applicable_age(1960) == 75
    assert retirement.applicable_age(1985) == 75
    assert "1959" in retirement.RMD_APPLICABLE_AGE.provenance.note


# ---------------------------------------------------------------------------
# the Uniform Lifetime Table - transcription, checked against TD 9930
# ---------------------------------------------------------------------------
def test_uniform_lifetime_divisors_match_the_published_table():
    published = {72: "27.4", 73: "26.5", 75: "24.6", 80: "20.2", 85: "16.0",
                 90: "12.2", 95: "8.9", 100: "6.4", 110: "3.5", 120: "2.0"}
    for age, divisor in published.items():
        assert retirement.UNIFORM_LIFETIME_TABLE[age] == Decimal(divisor)


def test_the_table_is_complete_and_monotonic():
    rows = retirement.UNIFORM_LIFETIME_TABLE.rows
    assert set(rows) == set(range(72, 121))          # 72 through "120 and older"
    divisors = [rows[a] for a in sorted(rows)]
    assert all(a > b for a, b in zip(divisors, divisors[1:]))
    assert all(isinstance(d, Decimal) for d in divisors)   # never a float


def test_the_divisor_clamps_at_both_ends_of_the_table():
    assert retirement.uniform_lifetime_divisor(60) == Decimal("27.4")
    assert retirement.uniform_lifetime_divisor(120) == Decimal("2.0")
    assert retirement.uniform_lifetime_divisor(137) == Decimal("2.0")   # 120 and older


# ---------------------------------------------------------------------------
# the RMD itself
# ---------------------------------------------------------------------------
def test_rmd_is_zero_before_the_applicable_age():
    # Born 1953, applicable age 73, so nothing is required for 2025 (age 72).
    assert retirement.rmd(500_000_00, 1953, 6, 2025) == 0
    assert retirement.rmd(500_000_00, 1953, 6, 2026) > 0


def test_rmd_uses_the_attained_age_divisor():
    # $500,000 at 12/31, age 73 in the distribution year: 500000 / 26.5.
    assert retirement.rmd(500_000_00, 1953, 6, 2026) == 1_886_792
    assert Decimal(1_886_792) == (Decimal(500_000_00) / Decimal("26.5")
                                  ).quantize(Decimal(1))


def test_rmd_for_the_1960_cohort_starts_at_75_not_73():
    born = 1960
    assert retirement.rmd(1_000_000_00, born, 3, 2033) == 0    # age 73 - too early
    assert retirement.rmd(1_000_000_00, born, 3, 2034) == 0    # age 74 - still early
    # Age 75 in 2035: 1,000,000 / 24.6.
    assert retirement.rmd(1_000_000_00, born, 3, 2035) == 4_065_041


def test_rmd_ignores_the_birth_month_but_validates_it():
    # The regulation keys off the calendar year, so January and December agree.
    assert (retirement.rmd(250_000_00, 1953, 1, 2026)
            == retirement.rmd(250_000_00, 1953, 12, 2026))
    assert retirement.rmd(250_000_00, 1953, None, 2026) > 0
    with pytest.raises(ValueError):
        retirement.rmd(250_000_00, 1953, 13, 2026)


def test_rmd_of_an_empty_or_negative_balance_is_zero():
    assert retirement.rmd(0, 1953, 6, 2030) == 0
    assert retirement.rmd(-1_000_00, 1953, 6, 2030) == 0


def test_rmd_rounds_half_up_at_the_cents_boundary():
    # 100.05 cents worth of quotient: 2669 / 26.5 is 100.7169..., so 101.
    assert retirement.rmd(2669, 1953, 6, 2026) == 101
    assert isinstance(retirement.rmd(2669, 1953, 6, 2026), int)


def test_rmd_names_the_tables_it_depended_on():
    names = {p.table for p in retirement.rmd_provenance()}
    assert names == {"RMD applicable age", "Uniform Lifetime Table", "Single Life Table"}


# ---------------------------------------------------------------------------
# Social Security
# ---------------------------------------------------------------------------
def test_full_retirement_age_steps_two_months_a_year_from_1955():
    assert retirement.full_retirement_age_months(1950) == 66 * 12
    assert retirement.full_retirement_age_months(1954) == 66 * 12
    assert retirement.full_retirement_age_months(1955) == 66 * 12 + 2
    assert retirement.full_retirement_age_months(1959) == 66 * 12 + 10
    assert retirement.full_retirement_age_months(1960) == 67 * 12
    assert retirement.full_retirement_age_months(1990) == 67 * 12


def _f(value):
    return value.quantize(Decimal("0.0001"))


def test_claim_factor_at_62_67_and_70_for_a_1960_birth():
    # FRA 67. 60 months early: 36 at 5/9 of 1% (20%) plus 24 at 5/12 of 1% (10%).
    assert _f(retirement.claim_factor(1960, 62 * 12)) == Decimal("0.7000")
    assert retirement.claim_factor(1960, 67 * 12) == Decimal(1)
    # 36 months late at 2/3 of 1% a month: 24%.
    assert _f(retirement.claim_factor(1960, 70 * 12)) == Decimal("1.2400")


def test_claim_factor_clamps_outside_62_to_70():
    assert retirement.claim_factor(1960, 55 * 12) == retirement.claim_factor(1960, 62 * 12)
    assert retirement.claim_factor(1960, 75 * 12) == retirement.claim_factor(1960, 70 * 12)


def test_claim_factor_is_a_reduction_early_and_a_credit_late():
    for months in range(62 * 12, 70 * 12 + 1, 7):
        f = retirement.claim_factor(1957, months)
        fra = retirement.full_retirement_age_months(1957)
        assert (f < 1) is (months < fra)
        assert (f > 1) is (months > fra)


def test_pia_applies_90_32_and_15_across_the_bend_points():
    first, second = retirement.SS_BEND_POINTS[2026]
    # $6,000 of AIME: all of band 1, the rest in band 2, nothing in band 3.
    # To the lower dime, as SSA states it (42 U.S.C. 415(a)(1)(A)): 2,665.88 -> 2,665.80.
    assert retirement.primary_insurance_amount(600_000, 2026) == 266_580
    # Below the first bend point it is simply 90 percent.
    assert retirement.primary_insurance_amount(100_000, 2026) == 90_000
    # Above the second, the top slice earns 15 percent.
    above = retirement.primary_insurance_amount(second + 100_000, 2026)
    assert above - retirement.primary_insurance_amount(second, 2026) == 15_000
    assert retirement.primary_insurance_amount(0, 2026) == 0
    assert first < second


def test_a_cohort_before_the_published_series_raises_rather_than_borrowing_bend_points():
    """A PAST cohort SSA published is a lookup, so a miss there is a data error."""
    with pytest.raises(KeyError):
        retirement.primary_insurance_amount(600_000, 1950)


def test_pia_bend_points_are_projected_beyond_the_published_series():
    """A plan that runs to age 100 asks about cohorts SSA cannot have published.

    Refusing to answer is what took the planner page down for any household
    with a member under 62, so an unpublished FUTURE cohort is projected at the
    named wage-growth assumption and says it was projected. A cohort before the
    series still raises - that one really is a data error.
    """
    last = max(retirement.SS_BEND_POINTS.rows)

    published = retirement.pia_bend_points(last)
    assert not published.projected
    assert (published.first_cents, published.second_cents) == (
        retirement.SS_BEND_POINTS[last]
    )

    # The crashing case: a person born in 2017 turns 62 in 2079.
    future = retirement.pia_bend_points(2079)
    assert future.projected
    assert future.eligibility_year == 2079
    assert future.first_cents > published.first_cents
    assert future.second_cents > future.first_cents
    # Whole dollars, as every published row is.
    assert future.first_cents % 100 == 0 and future.second_cents % 100 == 0
    # Grown at the module's one assumption, not invented here.
    expected = Decimal(published.first_cents) * (
        (Decimal(1) + retirement.SS_WAGE_GROWTH) ** (2079 - last)
    )
    assert abs(future.first_cents - int(expected)) <= 100

    # Monotonic, cohort by cohort, so no later cohort can read lower.
    points = [retirement.pia_bend_points(y) for y in range(last, last + 40)]
    for earlier, later in zip(points, points[1:]):
        assert later.first_cents > earlier.first_cents
        assert later.second_cents > earlier.second_cents

    # And the PIA itself comes out, monotonically, instead of raising.
    assert retirement.primary_insurance_amount(600_000, 2079) > 0
    assert retirement.primary_insurance_amount(
        600_000, 2079
    ) > retirement.primary_insurance_amount(600_000, last), (
        "grown bend points put more of a fixed AIME in the 90 percent band"
    )


def test_the_projected_cohort_is_an_assumption_the_faq_can_name():
    """The growth figure is a provenanced rule table, like every other figure."""
    table = retirement.SS_PROJECTED_WAGE_GROWTH
    assert isinstance(table, retirement.RuleTable)
    assert isinstance(retirement.SS_WAGE_GROWTH, Decimal)
    record = table.provenance
    assert record.volatility == "indexed"
    assert record.checked_on
    assert "trustees report" in record.note.lower()
    assert "ssa.gov" in record.source

    # A retiree's figure does not rest on it; a younger worker's does.
    last = max(retirement.SS_BEND_POINTS.rows)
    retiree = retirement.ss_benefit_provenance(last - retirement.SS_ELIGIBILITY_AGE)
    assert record not in retiree
    young = retirement.ss_benefit_provenance(2017)
    assert record in young


def test_no_earnings_means_no_benefit_and_no_bend_point_lookup():
    """A child of record has an AIME of zero and never reaches the PIA formula."""
    assert retirement.monthly_benefit({}, 2017, 67 * 12) == 0
    assert retirement.monthly_benefit({2024: 0}, 2017, 67 * 12) == 0
    # The point of the guard: the formula is not reached, so 1950 cannot raise.
    assert retirement.monthly_benefit({}, 1888, 67 * 12) == 0


def test_the_bend_point_table_says_a_cohort_row_is_settled_forever():
    """A stored cohort never goes stale; only the MISSING cohort does.

    The whole published series is kept rather than one current row, so the note
    has to tell the next maintainer what upkeep actually means here: add a row
    each October for the cohort turning 62 next year, and leave the old ones
    alone.
    """
    note = retirement.SS_BEND_POINTS.provenance.note.lower()
    assert "october" in note
    assert "turns 60" in note or "turning 62" in note
    assert retirement.SS_BEND_POINTS.provenance.volatility == "indexed"
    # The series runs to the cohort turning 62 next year, as the note promises.
    assert max(retirement.SS_BEND_POINTS.rows) >= _dt.date.today().year


def test_every_bend_point_is_the_statutory_formula_run_on_the_wage_series():
    """42 U.S.C. 415(a)(1)(B)(ii): $180 and $1,085 indexed by the wage series.

    The bend points were transcribed from SSA's table rather than computed, and
    the two tables are independent transcriptions - so running the statute on
    AWI_SERIES and getting SSA's published figures back means a typo in either
    one fails here rather than quietly moving somebody's benefit. The 1977 base
    year and the two-year lag are both in the statute.
    """
    base = retirement.AWI_SERIES[1977]
    checked = 0
    for year, (first, second) in sorted(retirement.SS_BEND_POINTS.rows.items()):
        if year - 2 not in retirement.AWI_SERIES.rows:
            continue                      # the series stops before the table does
        ratio = retirement.AWI_SERIES[year - 2] / base
        for statutory, published in ((180, first), (1085, second)):
            dollars = (Decimal(statutory) * ratio).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            assert int(dollars) * 100 == published, (year, statutory)
        checked += 1
    assert checked >= 45              # 1979 through the last indexable cohort


# ---------------------------------------------------------------------------
# indexed earnings and the benefit
# ---------------------------------------------------------------------------
def test_earnings_are_indexed_to_the_wages_of_the_year_the_worker_turned_60():
    # Born 1964 -> age 60 in 2024, so 2024 wages are the yardstick and a year
    # at or after it is counted at face value (factor exactly 1).
    assert retirement.awi_provenance(1964) is not None
    assert retirement.indexing_factor(2024, 1964) == Decimal(1)
    assert retirement.indexing_factor(2030, 1964) == Decimal(1)
    # An older year is scaled up by how much wages have risen since.
    factor = retirement.indexing_factor(1990, 1964)
    assert factor > 1
    assert factor == retirement.AWI_SERIES[2024] / retirement.AWI_SERIES[1990]


def test_earnings_above_the_taxable_maximum_do_not_count():
    # SSA never credits a dollar it did not tax. 2024's maximum was $168,600.
    assert retirement.capped_earnings(2024, 500_000_00) == 168_600_00
    assert retirement.capped_earnings(2024, 50_000_00) == 50_000_00


def test_aime_averages_the_best_35_years_over_420_months():
    # Thirty-six flat years, so the lowest one is dropped and the remaining 35
    # are averaged over 35 * 12 months.
    earnings = {year: 60_000_00 for year in range(1990, 2026)}
    aime = retirement.average_indexed_monthly_earnings(earnings, 1964)
    assert aime == 953_600

    # A short history is padded with zero years, not averaged over what exists.
    short = retirement.average_indexed_monthly_earnings(
        {year: 60_000_00 for year in range(2016, 2026)}, 1964
    )
    assert 0 < short < aime
    assert retirement.average_indexed_monthly_earnings({}, 1964) == 0


def test_the_same_earnings_pay_differently_at_62_at_fra_and_at_70():
    """The whole point of the claim-age panel: one history, three answers."""
    earnings = {year: 60_000_00 for year in range(1990, 2026)}
    early = retirement.monthly_benefit(earnings, 1964, 62 * 12)
    fra = retirement.monthly_benefit(earnings, 1964, 67 * 12)
    late = retirement.monthly_benefit(earnings, 1964, 70 * 12)
    assert 0 < early < fra < late
    # Both ends are the published factors, not an interpolation: 70 percent at
    # 62 for this cohort, 124 percent at 70 - each to the lower dime, as SSA
    # states a benefit.
    assert early == retirement.to_lower_dime_cents(Decimal(fra) * Decimal("0.70"))
    assert late == retirement.to_lower_dime_cents(Decimal(fra) * Decimal("1.24"))
    assert retirement.monthly_benefit({}, 1964, 67 * 12) == 0


def test_the_offered_claim_ages_are_the_three_that_bracket_the_decision():
    labels = [label for label, _ in retirement.claim_age_choices(1964)]
    months = [m for _, m in retirement.claim_age_choices(1964)]
    assert labels[0] == "62" and labels[-1] == "70"
    assert "full retirement age" in labels[1]
    assert months == [62 * 12, retirement.full_retirement_age_months(1964), 70 * 12]
    # A cohort whose FRA is not a whole year still says so exactly.
    assert "66 and 2mo" in retirement.claim_age_choices(1955)[1][0]


def test_a_benefit_names_the_two_tables_that_decided_it():
    citation = retirement.ss_benefit_citation(1964)
    assert "bend points" in citation
    assert "2026" in citation               # the cohort that turns 62 in 2026
    assert "2024" in citation               # the wages it was indexed to
    records = retirement.ss_benefit_provenance(1964)
    assert len(records) == 6
    assert len({p.table for p in records}) == 6


# ---------------------------------------------------------------------------
# FPL and IRMAA
# ---------------------------------------------------------------------------
def test_federal_poverty_level_is_the_base_plus_one_step_per_extra_person():
    # 2026 guidelines, 48 contiguous states and DC: $15,960 plus $5,680 each.
    assert retirement.federal_poverty_level(1) == 15_960_00
    assert retirement.federal_poverty_level(4) == 33_000_00
    assert retirement.federal_poverty_level(8) == 55_720_00
    assert retirement.federal_poverty_level(1, "alaska") == 19_950_00
    assert retirement.federal_poverty_level(1, "hawaii") == 18_360_00
    with pytest.raises(ValueError):
        retirement.federal_poverty_level(0)
    with pytest.raises(KeyError):
        retirement.federal_poverty_level(2, "guam")


def test_irmaa_is_a_cliff_and_the_joint_thresholds_are_double():
    # 2026 CMS tiers. One dollar over the line costs the whole step.
    at_the_line = retirement.irmaa_tier(109_000_00, "single")
    just_over = retirement.irmaa_tier(109_000_01, "single")
    assert at_the_line.part_b_total_cents == retirement.PART_B_STANDARD_CENTS == 202_90
    assert at_the_line.part_d_surcharge_cents == 0
    assert just_over.part_b_total_cents == 284_10
    assert just_over.part_d_surcharge_cents == 14_50
    # The same income filing jointly is still in the bottom tier.
    assert retirement.irmaa_tier(109_000_01, "joint").part_b_total_cents == 202_90
    assert retirement.irmaa_tier(218_000_01, "joint").part_b_total_cents == 284_10


def test_irmaa_top_tier_and_married_filing_separately():
    assert retirement.irmaa_tier(500_000_00, "single").part_b_total_cents == 689_90
    assert retirement.irmaa_tier(750_000_00, "joint").part_b_total_cents == 689_90
    assert retirement.irmaa_tier(1_000_000_00, "single").part_d_surcharge_cents == 91_00
    # Separate has its own short ladder: no middle steps at all.
    assert retirement.irmaa_tier(109_000_00, "separate").part_b_total_cents == 202_90
    assert retirement.irmaa_tier(150_000_00, "separate").part_b_total_cents == 649_20
    assert retirement.irmaa_tier(391_000_00, "separate").part_b_total_cents == 689_90
    with pytest.raises(ValueError):
        retirement.irmaa_tier(100_000_00, "head_of_household")


def test_irmaa_records_its_two_year_lookback():
    assert retirement.IRMAA_LOOKBACK_YEARS == 2
    assert "two years" in retirement.IRMAA_TIERS.provenance.note.lower()


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------
def test_people_round_trip_and_sort_self_first(conn):
    me = retirement.add_person(conn, "Alpha", "self", birth_month=6, birth_year=1960)
    spouse = retirement.add_person(conn, "Beta", "spouse", birth_month=11, birth_year=1962)
    kid = retirement.add_person(conn, "Gamma", "child", birth_month=2, birth_year=1995)

    row = retirement.get_person(conn, me)
    assert row["name"] == "Alpha" and row["birth_year"] == 1960
    assert row["born_on_the_first"] == 0
    assert all(row[c] is None for c in retirement.HEALTH_COLUMNS)

    assert [p["id"] for p in retirement.list_people(conn)] == [me, spouse, kid]
    assert [p["id"] for p in retirement.list_people(conn, "child")] == [kid]

    retirement.update_person(conn, spouse, birth_month=12, smoker="no")
    row = retirement.get_person(conn, spouse)
    assert row["birth_month"] == 12 and row["smoker"] == "no"
    assert row["name"] == "Beta"                      # untouched fields stay

    retirement.delete_person(conn, kid)
    assert retirement.get_person(conn, kid) is None
    assert len(retirement.list_people(conn)) == 2


def test_people_validation(conn):
    with pytest.raises(ValueError):
        retirement.add_person(conn, "Delta", "cousin")
    with pytest.raises(ValueError):
        retirement.add_person(conn, "Delta", "other", birth_month=0, birth_year=1970)
    with pytest.raises(ValueError):
        retirement.add_person(conn, "  ", "other")
    with pytest.raises(ValueError):
        retirement.add_person(conn, "Delta", "other", favorite_color="blue")
    pid = retirement.add_person(conn, "Delta", "other")
    with pytest.raises(ValueError):
        retirement.update_person(conn, pid, relationship="cousin")
    retirement.update_person(conn, pid)               # nothing to do, no error


def test_health_fields_are_optional_and_stored_when_given(conn):
    pid = retirement.add_person(conn, "Epsilon", "self", birth_month=1, birth_year=1958,
                                smoker="former", bmi_band="25-29",
                                diabetes="none", major_conditions="none",
                                family_history="longevity both sides")
    row = retirement.get_person(conn, pid)
    assert row["smoker"] == "former" and row["bmi_band"] == "25-29"
    assert row["family_history"] == "longevity both sides"


def test_every_people_column_is_classified(conn):
    actual = {c["name"] for c in conn.execute("PRAGMA table_info(people)").fetchall()}
    assert actual == set(retirement.HEALTH_COLUMNS) | set(retirement.PEOPLE_PLAIN_COLUMNS)
    assert not set(retirement.HEALTH_COLUMNS) & set(retirement.PEOPLE_PLAIN_COLUMNS)


def test_attainment_follows_the_day_before_the_anniversary_rule(conn):
    normal = {"birth_year": 1960, "birth_month": 6, "born_on_the_first": 0}
    assert retirement.attainment_year_month(normal, 65) == (2025, 6)
    # Born on the first: attained the day before, so the PRIOR month.
    first = {"birth_year": 1960, "birth_month": 6, "born_on_the_first": 1}
    assert retirement.attainment_year_month(first, 65) == (2025, 5)
    january = {"birth_year": 1960, "birth_month": 1, "born_on_the_first": 1}
    assert retirement.attainment_year_month(january, 65) == (2024, 12)
    assert retirement.attainment_year_month({"birth_year": None, "birth_month": 6}, 65) is None


# ---------------------------------------------------------------------------
# the plan: withdrawals and conversions
# ---------------------------------------------------------------------------
def test_withdrawals_round_trip_and_replace_in_place(conn, accts):
    ira = accts["ira"]
    assert retirement.get_withdrawal(conn, ira, 2030) is None
    retirement.set_withdrawal(conn, ira, 2030, 40_000_00)
    assert retirement.get_withdrawal(conn, ira, 2030) == 40_000_00
    retirement.set_withdrawal(conn, ira, 2030, 45_000_00)
    assert retirement.get_withdrawal(conn, ira, 2030) == 45_000_00
    assert len(retirement.list_withdrawals(conn, 2030)) == 1     # replaced, not doubled

    # An explicit zero is a decision, not an absence.
    retirement.set_withdrawal(conn, ira, 2031, 0)
    assert retirement.get_withdrawal(conn, ira, 2031) == 0
    retirement.delete_withdrawal(conn, ira, 2031)
    assert retirement.get_withdrawal(conn, ira, 2031) is None


def test_withdrawals_are_magnitudes_not_signed(conn, accts):
    with pytest.raises(ValueError, match="magnitude"):
        retirement.set_withdrawal(conn, accts["ira"], 2030, -40_000_00)


def test_conversions_round_trip_and_need_two_accounts(conn, accts):
    ira, roth = accts["ira"], accts["roth"]
    retirement.set_conversion(conn, ira, roth, 2030, 25_000_00)
    assert retirement.get_conversion(conn, ira, roth, 2030) == 25_000_00
    retirement.set_conversion(conn, ira, roth, 2030, 30_000_00)
    assert retirement.get_conversion(conn, ira, roth, 2030) == 30_000_00
    assert len(retirement.list_conversions(conn, 2030)) == 1
    with pytest.raises(ValueError):
        retirement.set_conversion(conn, ira, ira, 2030, 1_00)
    with pytest.raises(ValueError):
        retirement.set_conversion(conn, ira, roth, 2030, -1_00)
    retirement.delete_conversion(conn, ira, roth, 2030)
    assert retirement.get_conversion(conn, ira, roth, 2030) is None


def test_an_owner_is_recorded_per_account_and_unknown_is_not_mine(conn, accts):
    ira, roth = accts["ira"], accts["roth"]
    assert retirement.account_owner(conn, ira) is None
    # Unknown on either side is not an owner MISMATCH: a ledger that predates
    # the column has to keep planning conversions.
    assert retirement.same_owner(conn, ira, roth) is True

    saver = retirement.add_person(conn, "ANON Saver", "self", birth_year=1960)
    spouse = retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=1962)
    retirement.set_account_owner(conn, ira, saver)
    assert retirement.account_owner(conn, ira) == saver
    assert retirement.same_owner(conn, ira, roth) is True       # roth still unknown
    retirement.set_account_owner(conn, roth, spouse)
    assert retirement.same_owner(conn, ira, roth) is False
    retirement.set_account_owner(conn, roth, saver)
    assert retirement.same_owner(conn, ira, roth) is True
    retirement.set_account_owner(conn, roth, None)               # cleared again
    assert retirement.account_owner(conn, roth) is None
    assert retirement.plan_accounts(conn) == []                  # no treatments set


def test_a_conversion_cannot_land_in_another_persons_roth(conn, accts):
    """IRC 408A(d)(3): there is no spousal Roth conversion."""
    ira, roth = accts["ira"], accts["roth"]
    saver = retirement.add_person(conn, "ANON Saver", "self", birth_year=1960)
    spouse = retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=1962)
    retirement.set_account_owner(conn, ira, saver)
    retirement.set_account_owner(conn, roth, spouse)

    with pytest.raises(ValueError, match="408A"):
        retirement.set_conversion(conn, ira, roth, 2030, 25_000_00)
    assert retirement.list_conversions(conn, 2030) == []

    # The same Roth, owned by the same person, is fine.
    retirement.set_account_owner(conn, roth, saver)
    retirement.set_conversion(conn, ira, roth, 2030, 25_000_00)
    assert retirement.get_conversion(conn, ira, roth, 2030) == 25_000_00

    # And the rule is enforced on a LATER change of owner, not only at first write.
    retirement.set_account_owner(conn, roth, spouse)
    with pytest.raises(ValueError, match="408A"):
        retirement.set_conversion(conn, ira, roth, 2030, 30_000_00)
    assert retirement.get_conversion(conn, ira, roth, 2030) == 25_000_00


# ---------------------------------------------------------------------------
# projected taxable income: a stored series the user owns
# ---------------------------------------------------------------------------
def test_taxable_social_security_follows_the_provisional_income_worksheet():
    """IRC 86: the share depends on the rest of the return (reported: a flat
    85% overstated taxable income in the years a plan spends Roth money)."""
    ss = retirement.taxable_social_security_cents
    # Joint, $60K benefit: provisional = other + $30K.
    assert ss(60_000_00, 0, "joint") == 0                        # $30K <= $32K
    assert ss(60_000_00, 10_000_00, "joint") == 4_000_00         # half of $8K over
    # $30K of IRA draws: 85% of $16K over $44K plus the $6K middle tier.
    assert ss(60_000_00, 30_000_00, "joint") == 19_600_00
    assert ss(60_000_00, 500_000_00, "joint") == 51_000_00       # the 85% cap
    assert ss(60_000_00, 30_000_00, "single") > ss(60_000_00, 30_000_00, "joint")
    # The room under a line counts the benefit each extra dollar pulls in:
    # in the phase-in zone $1 of draw adds $1.85 of income.
    room = retirement.ordinary_room_cents(60_000_00, 30_000_00, 60_000_00, "joint")
    assert retirement.gross_with_social_security_cents(
        30_000_00 + room, 60_000_00, "joint") <= 60_000_00
    assert retirement.gross_with_social_security_cents(
        30_000_00 + room + 1, 60_000_00, "joint") > 60_000_00
    assert room < 60_000_00 - retirement.gross_with_social_security_cents(
        30_000_00, 60_000_00, "joint")


def test_taxable_social_security_is_the_ceiling_share_rounded_half_up():
    assert retirement.SS_TAXABLE_MAX_SHARE == 85
    assert retirement.taxable_social_security_cents(0) == 0
    assert retirement.taxable_social_security_cents(10_000_00) == 8_500_00
    # 85% of 1.11 is 0.9435, which is a cent up, not down.
    assert retirement.taxable_social_security_cents(1_11) == 94
    assert retirement.taxable_social_security_cents(-10_000_00) == 8_500_00


def test_taxable_income_round_trips_and_knows_unset_from_zero(conn):
    assert retirement.get_taxable_income(conn, 2030) is None
    retirement.set_taxable_income(conn, 2030, 0)
    assert retirement.get_taxable_income(conn, 2030) == 0        # said, not unsaid
    retirement.set_taxable_income(conn, 2030, 62_000_00)
    retirement.set_taxable_income(conn, 2031, 64_000_00, source="seeded")
    assert retirement.taxable_income_map(conn) == {2030: 62_000_00, 2031: 64_000_00}
    assert [r["source"] for r in retirement.list_taxable_income(conn)] \
        == ["entered", "seeded"]
    with pytest.raises(ValueError, match="magnitude"):
        retirement.set_taxable_income(conn, 2032, -1_00)
    with pytest.raises(ValueError):
        retirement.set_taxable_income(conn, 2032, 1_00, source="guessed")
    retirement.delete_taxable_income(conn, 2030)
    assert retirement.get_taxable_income(conn, 2030) is None


def test_seeding_refreshes_its_own_years_and_never_a_year_the_user_typed(conn):
    retirement.set_taxable_income(conn, 2030, 50_000_00)                 # entered
    assert retirement.seed_taxable_income(
        conn, {2030: 1_00, 2031: 64_000_00}) == [2031]
    assert retirement.taxable_income_map(conn) == {2030: 50_000_00, 2031: 64_000_00}

    # The plan moved: a seeded year follows it, an entered year does not.
    assert retirement.seed_taxable_income(
        conn, {2030: 2_00, 2031: 70_000_00}) == [2031]
    assert retirement.taxable_income_map(conn) == {2030: 50_000_00, 2031: 70_000_00}
    # An unchanged seed writes nothing at all.
    assert retirement.seed_taxable_income(conn, {2031: 70_000_00}) == []


def test_plan_years_lists_everything_the_plan_mentions(conn, accts):
    retirement.set_withdrawal(conn, accts["ira"], 2032, 1_00)
    retirement.set_withdrawal(conn, accts["k401"], 2030, 1_00)
    retirement.set_conversion(conn, accts["ira"], accts["roth"], 2031, 1_00)
    assert retirement.plan_years(conn) == [2030, 2031, 2032]


def test_deleting_an_account_takes_its_plan_rows_with_it(conn, accts):
    conn.execute("PRAGMA foreign_keys = ON")
    ira = accts["ira"]
    retirement.set_withdrawal(conn, ira, 2030, 10_000_00)
    retirement.set_conversion(conn, ira, accts["roth"], 2030, 5_000_00)
    conn.execute("DELETE FROM accounts WHERE id = ?", (ira,))
    conn.commit()
    assert retirement.list_withdrawals(conn, 2030) == []
    assert retirement.list_conversions(conn, 2030) == []


# ---------------------------------------------------------------------------
# the per-year rollup that feeds the projection
# ---------------------------------------------------------------------------
def test_plan_flows_splits_outflow_and_inflow_per_account(conn, accts):
    ira, k401, roth = accts["ira"], accts["k401"], accts["roth"]
    retirement.set_withdrawal(conn, ira, 2030, 40_000_00)
    retirement.set_withdrawal(conn, k401, 2030, 10_000_00)
    retirement.set_conversion(conn, ira, roth, 2030, 25_000_00)
    retirement.set_conversion(conn, k401, roth, 2030, 5_000_00)
    retirement.set_withdrawal(conn, ira, 2031, 99_999_00)      # another year entirely

    flows = retirement.plan_flows(conn, 2030)
    assert set(flows) == {ira, k401, roth}

    f = flows[ira]
    assert f.distribution_cents == 40_000_00
    assert f.conversion_out_cents == 25_000_00
    assert f.conversion_in_cents == 0
    assert f.outflow_cents == 65_000_00 and f.inflow_cents == 0
    assert f.net_cents == -65_000_00          # signed: the balance goes down
    assert f.taxable_cents == 65_000_00

    # The Roth is pure inflow - the conversions from both source accounts.
    r = flows[roth]
    assert r.conversion_in_cents == 30_000_00
    assert r.distribution_cents == 0 and r.conversion_out_cents == 0
    assert r.net_cents == 30_000_00 and r.taxable_cents == 0

    # Conversions conserve money across the household in that year.
    assert sum(x.net_cents for x in flows.values()) == -50_000_00


def test_plan_flows_is_empty_when_nothing_is_planned(conn, accts):
    retirement.set_withdrawal(conn, accts["ira"], 2030, 1_00)
    assert retirement.plan_flows(conn, 2029) == {}
    assert set(retirement.plan_flows(conn, 2030)) == {accts["ira"]}


def test_shortfall_reports_how_far_the_plan_falls_under_the_rmd(conn, accts):
    ira = accts["ira"]
    required = retirement.rmd(500_000_00, 1953, 6, 2026)
    assert required == 1_886_792

    # Nothing planned at all: the whole RMD is short.
    assert retirement.shortfall_against_rmd(conn, ira, 500_000_00, 1953, 6, 2026) == required

    retirement.set_withdrawal(conn, ira, 2026, 1_000_00)
    assert (retirement.shortfall_against_rmd(conn, ira, 500_000_00, 1953, 6, 2026)
            == required - 1_000_00)

    # Drawing more than required is allowed; the shortfall floors at zero.
    retirement.set_withdrawal(conn, ira, 2026, 50_000_00)
    assert retirement.shortfall_against_rmd(conn, ira, 500_000_00, 1953, 6, 2026) == 0

    # A conversion does NOT satisfy the RMD (IRC 408A(d)(3)(E)).
    retirement.set_withdrawal(conn, ira, 2027, 0)
    retirement.set_conversion(conn, ira, accts["roth"], 2027, 100_000_00)
    assert retirement.shortfall_against_rmd(conn, ira, 500_000_00, 1953, 6, 2027) > 0

    # Before the applicable age there is no floor to fall short of.
    assert retirement.shortfall_against_rmd(conn, ira, 500_000_00, 1953, 6, 2024) == 0


# ---------------------------------------------------------------------------
# the earnings history and the ledger estimate
# ---------------------------------------------------------------------------
def test_earnings_round_trip_and_carry_their_source(conn):
    person = retirement.add_person(conn, "Synthetic Worker", "self", birth_year=1964)
    retirement.set_earnings(conn, person, 2024, 60_000_00)
    retirement.set_earnings(conn, person, 2025, 62_000_00, "estimated")
    retirement.set_earnings(conn, person, 2026, 64_000_00, "projected")

    rows = retirement.list_earnings(conn, person)
    assert [r["year"] for r in rows] == [2024, 2025, 2026]          # oldest first
    assert [r["source"] for r in rows] == ["reported", "estimated", "projected"]
    assert retirement.earnings_map(conn, person) == {
        2024: 60_000_00, 2025: 62_000_00, 2026: 64_000_00}

    # Setting a year again replaces it, source and all.
    retirement.set_earnings(conn, person, 2025, 63_000_00)
    assert dict(retirement.list_earnings(conn, person)[1]) == {
        "year": 2025, "earnings_cents": 63_000_00, "source": "reported"}

    retirement.delete_earnings(conn, person, 2025)
    assert [r["year"] for r in retirement.list_earnings(conn, person)] == [2024, 2026]


def test_earnings_are_magnitudes_and_the_source_is_checked(conn):
    person = retirement.add_person(conn, "Synthetic Worker", "self")
    with pytest.raises(ValueError, match="magnitude"):
        retirement.set_earnings(conn, person, 2024, -60_000_00)
    with pytest.raises(ValueError):
        retirement.set_earnings(conn, person, 2024, 60_000_00, "guessed")
    with pytest.raises(ValueError):
        retirement.replace_earnings(conn, person, {2024: 1}, "guessed")


def test_deleting_a_person_takes_the_earnings_with_them(conn):
    person = retirement.add_person(conn, "Synthetic Worker", "self")
    retirement.set_earnings(conn, person, 2024, 60_000_00)
    retirement.delete_person(conn, person)
    assert retirement.list_earnings(conn, person) == []


def test_replacing_one_source_never_overwrites_a_year_the_user_typed(conn):
    """The guarantee the Estimate button rests on.

    A re-estimate has to clear out estimated years it no longer finds wages in,
    and it has to leave the years off the SSA statement exactly as typed - the
    one figure in the table that came from SSA itself.
    """
    person = retirement.add_person(conn, "Synthetic Worker", "self")
    retirement.set_earnings(conn, person, 2023, 99_000_00)             # reported
    retirement.replace_earnings(
        conn, person, {2023: 50_000_00, 2024: 50_000_00}, "estimated")

    held = {r["year"]: dict(r) for r in retirement.list_earnings(conn, person)}
    assert held[2023]["earnings_cents"] == 99_000_00
    assert held[2023]["source"] == "reported"
    assert held[2024]["source"] == "estimated"

    # A second run drops the estimated year that is no longer found.
    retirement.replace_earnings(conn, person, {2025: 51_000_00}, "estimated")
    assert sorted(retirement.earnings_map(conn, person)) == [2023, 2025]


def test_the_basis_line_names_every_source_in_the_table(conn):
    assert "No earnings history yet" in retirement.describe_earnings_basis([])

    person = retirement.add_person(conn, "Synthetic Worker", "self")
    retirement.set_earnings(conn, person, 2020, 50_000_00)
    retirement.set_earnings(conn, person, 2021, 51_000_00)
    basis = retirement.describe_earnings_basis(retirement.list_earnings(conn, person))
    assert basis == "Basis: 2 years from your SSA Earnings Report (2020-2021)."

    retirement.set_earnings(conn, person, 2026, 60_000_00, "projected")
    basis = retirement.describe_earnings_basis(retirement.list_earnings(conn, person))
    assert "2 years from your SSA Earnings Report (2020-2021)" in basis
    assert "1 year projected by you (2026)" in basis


def _wage_ledger(conn, category="Salary", years=(2023, 2024), cents=50_000_00):
    account = ledger.create_account(conn, "Everyday Checking", "checking")
    cat = ledger.resolve_category(conn, category)
    for year in years:
        ledger.add_transaction(conn, account, "%d-03-15" % year, cents, category_id=cat)
    return account, cat


def test_the_ledger_estimate_sums_wage_deposits_by_calendar_year(conn):
    _wage_ledger(conn)
    estimate = retirement.estimate_earnings_from_ledger(conn)
    assert estimate.rows == {2023: 50_000_00, 2024: 50_000_00}
    assert estimate.category_names == ("Salary",)
    assert estimate.capped_years == ()
    assert any("Summed from deposits" in line for line in estimate.assumptions())
    # The understatement is printed, not buried: SSA counts pre-deduction wages.
    assert any("reads LOW" in line for line in estimate.assumptions())


def test_the_estimate_counts_subcategories_and_skips_outflows_and_transfers(conn):
    account = ledger.create_account(conn, "Everyday Checking", "checking")
    other = ledger.create_account(conn, "Savings", "savings")
    sub = ledger.resolve_category(conn, "Salary:Bonus")
    spend = ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, account, "2024-02-01", 10_000_00, category_id=sub)
    ledger.add_transaction(conn, account, "2024-02-02", -400_00, category_id=spend)
    ledger.create_transfer(conn, other, account, "2024-02-03", 1_000_00)

    estimate = retirement.estimate_earnings_from_ledger(conn)
    assert estimate.rows == {2024: 10_000_00}


def test_the_estimate_is_cut_at_the_taxable_maximum_and_says_which_years(conn):
    _wage_ledger(conn, years=(2024,), cents=500_000_00)
    estimate = retirement.estimate_earnings_from_ledger(conn)
    assert estimate.rows == {2024: 168_600_00}
    assert estimate.capped_years == (2024,)
    assert any("taxable maximum" in line for line in estimate.assumptions())


def test_the_estimate_can_be_bounded_by_year(conn):
    _wage_ledger(conn, years=(2020, 2023, 2024))
    bounded = retirement.estimate_earnings_from_ledger(conn, first_year=2023)
    assert sorted(bounded.rows) == [2023, 2024]
    assert sorted(retirement.estimate_earnings_from_ledger(
        conn, last_year=2023).rows) == [2020, 2023]


def test_an_estimate_with_nothing_to_go_on_returns_nothing_and_explains(conn):
    ledger.create_account(conn, "Everyday Checking", "checking")
    estimate = retirement.estimate_earnings_from_ledger(conn)
    assert estimate.rows == {} and estimate.category_names == ()
    assert len(estimate.assumptions()) == 1
    assert "No income category here looks like wages" in estimate.assumptions()[0]


def test_the_wage_categories_are_found_by_hint_or_named_outright(conn):
    _wage_ledger(conn, category="Wages:Employer A")
    ledger.resolve_category(conn, "Consulting")
    assert sorted(set(retirement.wage_category_ids(conn).values())) == ["Wages"]
    named = retirement.wage_category_ids(conn, ["consulting"])
    assert sorted(set(named.values())) == ["Consulting"]


# ---------------------------------------------------------------------------
# Ordinary-income brackets (transcription, then the two lookups over them)
# ---------------------------------------------------------------------------

def test_the_2026_ladders_are_transcribed_from_rev_proc_2025_32():
    # IRS Rev. Proc. 2025-32, tax year 2026 taxable-income thresholds.
    single = {b.rate_percent: b.lower_cents for b in retirement.tax_brackets("single")}
    assert single == {10: 0, 12: 1_240_000, 22: 5_040_000, 24: 10_570_000,
                      32: 20_177_500, 35: 25_622_500, 37: 64_060_000}
    joint = {b.rate_percent: b.lower_cents for b in retirement.tax_brackets("joint")}
    assert joint == {10: 0, 12: 2_480_000, 22: 10_080_000, 24: 21_140_000,
                     32: 40_355_000, 35: 51_245_000, 37: 76_870_000}
    # Married filing separately is the joint ladder halved - INCLUDING the top
    # step, which the IRS newsroom summary gets wrong by showing the single one.
    separate = {b.rate_percent: b.lower_cents
                for b in retirement.tax_brackets("separate")}
    assert separate[37] == joint[37] // 2 == 38_435_000
    assert all(separate[r] == joint[r] // 2 for r in separate)


def test_every_ladder_is_closed_and_ascending():
    for status in retirement.FILING_STATUSES:
        ladder = retirement.tax_brackets(status)
        assert ladder[0].lower_cents == 0
        assert ladder[-1].upper_cents is None
        for lower, upper in zip(ladder, ladder[1:]):
            assert lower.upper_cents == upper.lower_cents
            assert lower.rate_percent < upper.rate_percent


def test_an_unknown_filing_status_is_refused():
    with pytest.raises(ValueError):
        retirement.tax_brackets("head_of_household")


def test_the_marginal_bracket_is_the_step_income_tops_out_in():
    assert retirement.marginal_bracket(9_000_000).rate_label == "22%"
    assert retirement.marginal_bracket(9_000_000, "joint").rate_label == "12%"
    assert retirement.marginal_bracket(0).rate_percent == 10
    assert retirement.marginal_bracket(-500).rate_percent == 10
    assert retirement.marginal_bracket(10_000_000_000).rate_percent == 37
    # The upper edge belongs to the step below it: one cent more moves up.
    assert retirement.marginal_bracket(5_040_000).rate_percent == 12
    assert retirement.marginal_bracket(5_040_001).rate_percent == 22


def test_headroom_measures_the_distance_to_the_next_edge():
    here = retirement.marginal_bracket(9_000_000)
    assert here.headroom_cents(9_000_000) == 10_570_000 - 9_000_000
    assert here.headroom_cents(20_000_000) == 0        # never negative
    top = retirement.tax_brackets("single")[-1]
    assert top.headroom_cents(99_999_999) is None      # no edge above the top


def test_the_bracket_above_runs_out_at_the_top():
    assert retirement.bracket_above(9_000_000).rate_label == "24%"
    assert retirement.bracket_above(0, "joint").rate_percent == 12
    assert retirement.bracket_above(70_000_000) is None


# ---------------------------------------------------------------------------
# standard deduction
# ---------------------------------------------------------------------------
def test_the_2026_standard_deduction_is_transcribed_from_rev_proc_2025_32():
    # Rev. Proc. 2025-32 sec. 3.15: $16,100 single and separate, $32,200 joint.
    assert retirement.standard_deduction("single") == 1_610_000
    assert retirement.standard_deduction("joint") == 3_220_000
    assert retirement.standard_deduction("separate") == 1_610_000
    assert retirement.STANDARD_DEDUCTION.provenance.effective_year == 2026


def test_the_age_and_blindness_add_on_is_counted_per_condition():
    # IRC 63(f): $2,050 for an unmarried filer, $1,650 each for a married one.
    assert retirement.standard_deduction("single", 1) == 1_610_000 + 205_000
    assert retirement.standard_deduction("single", 2) == 1_610_000 + 410_000
    # A couple both over 65 claims it twice, not once.
    assert retirement.standard_deduction("joint", 2) == 3_220_000 + 330_000
    assert retirement.standard_deduction("separate", 1) == 1_610_000 + 165_000


def test_the_standard_deduction_refuses_what_it_cannot_look_up():
    with pytest.raises(ValueError, match="filing status"):
        retirement.standard_deduction("head_of_household")
    with pytest.raises(ValueError, match="negative"):
        retirement.standard_deduction("single", -1)


def test_taxable_after_deduction_floors_at_zero():
    assert retirement.taxable_after_deduction(5_000_000) == 5_000_000 - 1_610_000
    assert retirement.taxable_after_deduction(1_000_000) == 0      # never negative
    assert retirement.taxable_after_deduction(
        5_000_000, "joint", 2) == 5_000_000 - (3_220_000 + 330_000)


def test_the_deduction_and_the_brackets_come_from_the_same_document():
    """Taxable income is gross less THIS deduction, measured on THOSE brackets.
    One updated without the other is the silent wrong answer."""
    assert (retirement.standard_deduction_provenance()[0].publisher
            == retirement.tax_bracket_provenance()[0].publisher)


# ---------------------------------------------------------------------------
# contribution limits
# ---------------------------------------------------------------------------
def test_the_2026_contribution_limits_are_transcribed_from_notice_2025_67():
    deferral = retirement.contribution_limit("elective_deferral")
    assert deferral.limit_cents == 2_450_000            # $24,500
    assert deferral.catch_up_cents == 800_000           # $8,000 at 50+
    assert deferral.catch_up_60_to_63_cents == 1_125_000  # $11,250, SECURE 2.0

    ira = retirement.contribution_limit("ira")
    assert ira.limit_cents == 750_000                   # $7,500
    assert ira.catch_up_cents == 110_000                # $1,100 at 50+
    assert ira.catch_up_60_to_63_cents is None          # no IRA step at 60-63


def test_the_catch_up_steps_up_at_50_and_again_at_60_but_not_at_64():
    deferral = retirement.contribution_limit("elective_deferral")
    assert deferral.for_age(49) == 2_450_000
    assert deferral.for_age(50) == 2_450_000 + 800_000
    assert deferral.for_age(59) == 2_450_000 + 800_000
    assert deferral.for_age(60) == 2_450_000 + 1_125_000
    assert deferral.for_age(63) == 2_450_000 + 1_125_000
    # The larger window closes at 64: back to the ordinary catch-up, not to zero.
    assert deferral.for_age(64) == 2_450_000 + 800_000

    ira = retirement.contribution_limit("ira")
    assert ira.for_age(61) == 750_000 + 110_000


def test_an_unknown_contribution_kind_is_refused():
    with pytest.raises(ValueError, match="contribution kind"):
        retirement.contribution_limit("403b")


# ---------------------------------------------------------------------------
# the ACA premium-credit cliff
# ---------------------------------------------------------------------------
def test_the_aca_cliff_is_four_times_the_poverty_line():
    assert retirement.ACA_CLIFF_MULTIPLE == 4          # IRC 36B(c)(1)(A)
    one = retirement.federal_poverty_level(1)
    assert retirement.aca_cliff_cents(1) == one * 4
    assert retirement.aca_cliff_cents(4) == retirement.federal_poverty_level(4) * 4
    # Alaska has its own guideline, so it has its own cliff.
    assert (retirement.aca_cliff_cents(2, "alaska")
            > retirement.aca_cliff_cents(2, "contiguous"))


def test_the_cliff_cites_both_the_multiple_and_the_line_it_multiplies():
    names = {p.table for p in retirement.aca_cliff_provenance()}
    assert names == {"ACA premium tax credit income cliff", "HHS poverty guidelines"}


# ---------------------------------------------------------------------------
# the single-file guard
# ---------------------------------------------------------------------------
#
# The requirement: every rule that is subject to change lives in a single file,
# so all of them can be updated at once each year or as often as the laws and
# rules change. A comment saying so would not
# survive the next person who needs a threshold at 11pm. This does.

#: Name fragments that mark a figure Congress, the IRS, SSA, HHS or CMS sets.
#: Single tokens, and adjacent token PAIRS where the single word is too common
#: to be safe: "bracket" alone is a drawing constant in the planner's chart.
_RULE_TOKENS = {"irmaa", "fpl", "poverty", "cola", "bendpoint"}
_RULE_PAIRS = {
    ("bend", "point"), ("bend", "points"),
    ("wage", "base"), ("taxable", "maximum"),
    ("standard", "deduction"), ("contribution", "limit"),
    ("contribution", "limits"), ("catch", "up"),
    ("rmd", "divisor"), ("lifetime", "divisor"),
    ("bracket", "threshold"), ("bracket", "thresholds"),
    ("tax", "bracket"), ("tax", "brackets"),
    ("cliff", "multiple"), ("poverty", "level"),
}


def _names_a_rule(name: str) -> bool:
    """True for an ALL_CAPS or _private name that reads like a published figure."""
    bare = name.lstrip("_")
    if not bare:
        return False
    if not (name.startswith("_") or (bare.isupper() and bare[0].isalpha())):
        return False
    parts = [p for p in name.lower().split("_") if p]
    if _RULE_TOKENS & set(parts):
        return True
    return any(pair in _RULE_PAIRS for pair in zip(parts, parts[1:]))


def _carries_a_number(value) -> bool:
    """True when the assigned expression contains a numeric literal anywhere.

    A number inside a STRING does not count - the FAQ says "400% of the federal
    poverty level" in prose, and prose citing a rule is not a second copy of it.
    """
    import ast

    for node in ast.walk(value):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if not isinstance(node.value, bool):
                return True
    return False


def _indexed_figures_in(source: str) -> set:
    """Names in this source that assign a number to a rule-sounding name."""
    import ast

    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and _names_a_rule(target.id):
                if _carries_a_number(value):
                    found.add(target.id)
    return found


def test_the_guard_catches_a_figure_that_leaked():
    """Prove the scanner works on a sample, so a clean repo is evidence and not
    just an empty search."""
    leaked = _indexed_figures_in(
        "IRMAA_THRESHOLD_CENTS = 10_600_000\n"
        "_standard_deduction_cents = 1_610_000\n"
        "_FPL_ROWS = {'contiguous': (1_596_000, 568_000)}\n"
        "POVERTY_HELP = 'income above 400% of the federal poverty level'\n"
        "BRACKET_LINES_ABOVE = 2\n"
        "LINE_HIT_FRACTION = 0.04\n"
    )
    # Caught: the three that re-type a published number.
    assert leaked == {"IRMAA_THRESHOLD_CENTS", "_standard_deduction_cents",
                      "_FPL_ROWS"}
    # Not caught, and must not be: prose citing a rule, and chart geometry whose
    # name happens to contain "bracket" or "line".
    assert "POVERTY_HELP" not in leaked and "BRACKET_LINES_ABOVE" not in leaked


def test_no_indexed_figure_lives_outside_this_file():
    """Every figure that moves with law or annual indexing is in retirement.py.

    Excluded: retirement.py itself (the one home), the tests (which transcribe
    the same figures on purpose, to check the module against the document) and
    report_defs (report definitions, not rule tables).
    """
    import pathlib

    root = pathlib.Path(retirement.__file__).resolve().parent
    offenders = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] in ("tests", "report_defs") or rel.name == "retirement.py":
            continue
        names = _indexed_figures_in(path.read_text(encoding="utf-8"))
        if names:
            offenders[str(rel)] = sorted(names)
    assert not offenders, (
        "These figures belong in mammon/retirement.py, where the annual update "
        f"checklist can find them once a year: {offenders}")


# ---------------------------------------------------------------------------
# The household withdrawal plan
# ---------------------------------------------------------------------------
#
# Pure arithmetic: no connection, no Qt, no projection. Every balance here is
# handed in, so what these tests check is the SPLIT - floors first, then in
# proportion to the balance entering the year, then redistributed when an
# account cannot carry its share.


def _plan_account(account_id, name, treatment="deferred", **kw):
    return retirement.PlanAccount(account_id=account_id, name=name,
                                  treatment=treatment, **kw)


def test_the_household_amount_compounds_the_yearly_increase():
    """A level amount is a shrinking amount; the increase is the whole point."""
    assert retirement.household_amount_cents(40_000_00, "2.5", 0) == 40_000_00
    assert retirement.household_amount_cents(40_000_00, "2.5", 1) == 41_000_00
    # 40000 * 1.025**2 = 42025.00, to the cent and not to a float's idea of it.
    assert retirement.household_amount_cents(40_000_00, "2.5", 2) == 42_025_00
    assert retirement.household_amount_cents(40_000_00, 0, 30) == 40_000_00


def test_the_household_amount_rounds_half_up_at_the_cent():
    # 1.005 -> 1.01, which ROUND_HALF_EVEN would make 1.00.
    assert retirement.household_amount_cents(2_01, "0.5", 1) == 2_02


def test_apportioning_cents_sums_to_exactly_the_total():
    """Thirds of a dollar have to still be a dollar."""
    shares = retirement.apportion_cents(100_00, {1: 1, 2: 1, 3: 1})
    assert sum(shares.values()) == 100_00
    assert sorted(shares.values()) == [3333, 3333, 3334]


def test_apportioning_ignores_empty_accounts_and_nothing_to_split():
    assert retirement.apportion_cents(500, {1: 100, 2: 0}) == {1: 500, 2: 0}
    assert retirement.apportion_cents(0, {1: 100}) == {1: 0}
    assert retirement.apportion_cents(500, {1: 0}) == {1: 0}


def test_the_household_split_is_proportional_to_the_balances():
    accounts = [_plan_account(1, "Big IRA"), _plan_account(2, "Middle IRA"),
                _plan_account(3, "Small IRA")]
    plan = retirement.plan_household_withdrawals(
        [2030], accounts,
        {1: 300_000_00, 2: 200_000_00, 3: 100_000_00},
        start_cents=60_000_00,
    )
    year = plan.years[0]
    assert year.amounts == {1: 30_000_00, 2: 20_000_00, 3: 10_000_00}
    assert year.drawn_cents == year.target_cents == 60_000_00
    assert plan.lasts


def test_the_household_plan_pays_the_income_tax_too():
    """Reported: the planner computed the tax and never paid it. The tax joins
    the year's need, and an IRA draw that pays it is taxed too - solved until
    it settles: a flat 20% on IRA draws needs 100,000 to spend 80,000."""
    plan = retirement.plan_household_withdrawals(
        [2030], [_plan_account(1, "IRA")], {1: 500_000_00},
        start_cents=80_000_00,
        tax_cents=lambda year, deferred, gains=0: (deferred * 20 + 50) // 100,
    )
    year = plan.years[0]
    assert year.tax_cents == 20_000_00
    assert year.amounts == {1: 100_000_00}
    assert year.target_cents == year.drawn_cents == 100_000_00
    assert year.tax_deferred_cents == 20_000_00


def test_the_household_spends_in_its_own_order_and_skips_exempt_accounts():
    """Reported: make the spending order adjustable, and let an account be
    left out (selling appreciated stock when the tax is due is a choice)."""
    accounts = [_plan_account(1, "IRA"), _plan_account(2, "Brokerage", "taxable"),
                _plan_account(3, "Roth", "roth")]
    held = {1: 500_000_00, 2: 500_000_00, 3: 500_000_00}

    def year(**kw):
        return retirement.plan_household_withdrawals(
            [2030], accounts, held, start_cents=50_000_00, **kw).years[0].amounts

    assert year() == {1: 50_000_00, 2: 0, 3: 0}             # default: IRA first
    assert year(spending_order=("roth", "deferred", "taxable", "deferred_over"))         == {1: 0, 2: 0, 3: 50_000_00}
    # Taxable first, but exempt: the next step pays.
    assert year(spending_order=("taxable", "roth", "deferred", "deferred_over"),
                exempt_ids={2}) == {1: 0, 2: 0, 3: 50_000_00}
    # An exempt IRA still pays its required minimum; the rest moves on.
    assert year(exempt_ids={1}, floor_cents=lambda aid, yr: 10_000_00)         == {1: 10_000_00, 2: 40_000_00, 3: 0}


def test_a_spending_order_is_normalized():
    order = retirement.normalize_spending_order(["deferred_over", "roth", "bogus"])
    # Every step once; the IRA room comes before the IRA beyond it.
    assert order == ("roth", "deferred", "deferred_over", "taxable")


def test_medicare_starts_the_month_a_person_turns_65():
    july = {"birth_year": 1960, "birth_month": 7}
    assert retirement.medicare_months(july, 2024) == 0
    assert retirement.medicare_months(july, 2025) == 6          # July through December
    assert retirement.medicare_months(july, 2026) == 12
    # Born on the 1st: 65 is attained the day before - a month earlier.
    assert retirement.medicare_months(dict(july, born_on_the_first=1), 2025) == 7
    assert retirement.medicare_months({"birth_year": 1960, "birth_month": 1,
                                       "born_on_the_first": 1}, 2024) == 1


def test_irmaa_is_a_cliff_per_enrollee_that_grows_faster_than_its_tiers():
    """The tier is set by income (indexed like the brackets); the dollars grow
    at the Medicare premium rate."""
    s = retirement.irmaa_surcharge_cents
    assert s(200_000_00, "joint", 2026) == (0, 0)
    tier_one = (28_410 - 20_290 + 1_450) * 12                  # $1,148.40 a year
    assert s(218_000_00, "joint", 2026) == (0, 0)               # the ceiling is inclusive
    assert s(218_000_01, "joint", 2026) == (1, tier_one)
    assert s(120_000_00, "single", 2026)[0] == 1
    assert s(120_000_00, "separate", 2026)[0] == 1              # its own, shorter ladder
    later = s(218_000_01, "joint", 2027, index_pct="2.2", growth_pct="5")
    assert later == (0, 0)                                      # the tier moved up
    assert s(230_000_00, "joint", 2027, index_pct="2.2", growth_pct="5") \
        == (1, round(tier_one * 1.05))
    assert retirement.irmaa_ceilings_cents("joint", 2027, "2.2")[0] \
        == retirement.indexed_cents(218_000_00, "2.2", 2027)


def test_the_household_plan_pays_a_surcharge_set_by_earlier_years():
    """IRMAA depends on income two years back, so it joins the year's need
    without re-solving the year."""
    seen = []

    def surcharge(year, earlier, deferred_now, gains_now=0):
        seen.append([e.year for e in earlier])
        return 1_000_00 if year == 2032 else 0

    plan = retirement.plan_household_withdrawals(
        [2030, 2031, 2032], [_plan_account(1, "IRA")], {1: 500_000_00},
        start_cents=50_000_00, surcharge_cents=surcharge)
    assert seen[-1] == [2030, 2031]
    last = plan.years[-1]
    assert last.surcharge_cents == 1_000_00
    assert last.amounts[1] == 51_000_00


def test_the_survivor_scenario_changes_filing_income_and_whose_rmd(conn):
    """Reported: add the survivor scenario."""
    me = retirement.add_person(conn, "ANON Self", "self", birth_year=1960, birth_month=5)
    her = retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=1964,
                                birth_month=9)
    assert retirement.get_survivor_scenario(conn) is None
    retirement.set_survivor_scenario(conn, me, 2040)
    scenario = retirement.get_survivor_scenario(conn)
    assert (scenario.deceased_id, scenario.survivor_id, scenario.death_year) \
        == (me, her, 2040)
    assert scenario.spending_pct == 75 and scenario.pension_pct == 50
    # Joint through the death year, single after it.
    assert retirement.survivor_status("joint", 2040, scenario) == "joint"
    assert retirement.survivor_status("joint", 2041, scenario) == "single"
    # The deceased's salary ends; their pension pays its survivor share.
    salary = retirement.add_income_source(conn, "ANON pay", 90_000_00, 2026,
                                          kind="salary")
    pension = retirement.add_income_source(conn, "ANON pension", 30_000_00, 2026,
                                           kind="pension")
    for sid in (salary, pension):
        retirement.update_income_source(conn, sid, person_id=me)
    by_id = {s.id: s for s in retirement.plan_income_sources(conn)}
    assert by_id[salary].cents_in(2040) == 90_000_00 and by_id[salary].cents_in(2041) == 0
    assert by_id[pension].cents_in(2041) == 15_000_00
    # What the Income dialog edits is unchanged.
    assert {s.id: s for s in retirement.list_income_sources(conn)}[
        salary].cents_in(2041) == 90_000_00
    # The deceased's IRA rolls to the survivor, whose age then sets the minimum.
    ira = ledger.create_account(conn, "ANON IRA", "investment")
    retirement.set_account_owner(conn, ira, me)
    assert retirement.account_birth(conn, ira, 2040) == (1960, 5)
    assert retirement.account_birth(conn, ira, 2041) == (1964, 9)
    retirement.set_survivor_scenario(conn, None)
    assert retirement.get_survivor_scenario(conn) is None


def test_capital_gains_stack_on_ordinary_income_at_their_own_rates():
    """Long-term gains are taxable income, taxed 0/15/20% ON TOP of ordinary
    income: ordinary income decides how much of them falls in the 0% band."""
    tax = retirement.capital_gains_tax_cents
    # Joint 2026: 0% through $98,900 of total taxable income.
    assert tax(50_000_00, 40_000_00, "joint", 2026, 0) == 0
    assert tax(50_000_00, 100_000_00, "joint", 2026, 0) == 7_665_00   # 51,100 at 15%
    assert tax(700_000_00, 10_000_00, "joint", 2026, 0) == 2_000_00   # all at 20%
    # The deduction comes off ordinary income first, then off the gains.
    whole = retirement.year_tax(20_000_00, 60_000_00, 32_200_00, "joint", 2026, 0)
    assert whole.ordinary == 0 and whole.gains == 0


def test_the_net_investment_income_tax_is_3_8_percent_of_the_lesser():
    niit = retirement.niit_cents
    assert niit(21_000_00, 240_000_00, "joint") == 0            # under $250,000
    assert niit(21_000_00, 260_000_00, "joint") == 380_00       # $10,000 over
    assert niit(21_000_00, 465_000_00, "joint") == 798_00       # all $21,000
    assert niit(21_000_00, 210_000_00, "single") == 380_00      # its own threshold


def test_taxable_accounts_can_pay_the_tax_and_their_sales_realize_gains():
    """Reported: paying the conversion tax from the IRA used bracket room; a
    taxable account can pay it, and what it sells above its basis is a gain."""
    accounts = [_plan_account(1, "IRA"), _plan_account(2, "Brokerage", "taxable")]
    held = {1: 500_000_00, 2: 100_000_00}
    seen = []

    def tax(year, deferred, gains=0):
        seen.append(gains)
        return 10_000_00

    def plan(where):
        return retirement.plan_household_withdrawals(
            [2030], accounts, held, start_cents=50_000_00, tax_cents=tax,
            basis_cents={2: 40_000_00}, tax_paid_from=where).years[0]

    usual = plan("spending")
    assert usual.amounts == {1: 60_000_00, 2: 0}        # IRA pays it all
    first = plan("taxable")
    assert first.amounts == {1: 50_000_00, 2: 10_000_00}
    # $10,000 sold from $100,000 with a $40,000 basis: $6,000 of gain.
    assert first.gains_cents == 6_000_00 and seen[-1] == 6_000_00


def test_a_floor_larger_than_an_accounts_share_is_paid_by_the_others():
    """The account pays what it holds; the rest of the year comes from elsewhere.

    This is the "smart enough to switch" case in one year: the small account's
    required minimum takes all of it, and the household's total is still met.
    """
    accounts = [_plan_account(1, "Big IRA"), _plan_account(2, "Small IRA")]
    plan = retirement.plan_household_withdrawals(
        [2030], accounts, {1: 100_000_00, 2: 10_000_00},
        start_cents=30_000_00,
        floor_cents=lambda aid, yr: 10_000_00 if aid == 2 else 0,
    )
    year = plan.years[0]
    assert year.amounts == {1: 20_000_00, 2: 10_000_00}
    assert sum(year.amounts.values()) == year.target_cents
    assert plan.lasts


def test_a_required_minimum_never_exceeds_what_the_account_holds():
    """The law cannot make an empty IRA distribute, and a floor above the
    balance would drive it negative."""
    accounts = [_plan_account(1, "Spent IRA")]
    plan = retirement.plan_household_withdrawals(
        [2030], accounts, {1: 1_000_00},
        start_cents=500_00,
        floor_cents=lambda aid, yr: 9_999_00,
    )
    assert plan.years[0].amounts == {1: 1_000_00}


def test_the_roth_is_drawn_only_after_the_deferred_accounts_are_empty():
    """IRC 408A(c)(4): no lifetime minimum, and no reason to spend it first."""
    accounts = [_plan_account(1, "IRA"), _plan_account(2, "Roth", "roth")]
    plan = retirement.plan_household_withdrawals(
        [2030, 2031], accounts, {1: 5_000_00, 2: 100_000_00},
        start_cents=20_000_00,
    )
    first, second = plan.years
    assert first.amounts == {1: 5_000_00, 2: 15_000_00}
    # The IRA is empty now, so the whole of the next year is the Roth's.
    assert second.amounts == {1: 0, 2: 20_000_00}
    assert plan.lasts


def test_a_current_employer_plan_is_held_back_like_a_roth():
    """IRC 401(a)(9)(C)(i)(II): still working, so no minimum and no early draw."""
    accounts = [_plan_account(1, "IRA"),
                _plan_account(2, "401(k)", current_employer_plan=True)]
    plan = retirement.plan_household_withdrawals(
        [2030], accounts, {1: 8_000_00, 2: 50_000_00}, start_cents=10_000_00)
    assert plan.years[0].amounts == {1: 8_000_00, 2: 2_000_00}


def test_required_minimums_can_raise_a_years_total_above_what_was_asked():
    accounts = [_plan_account(1, "IRA")]
    plan = retirement.plan_household_withdrawals(
        [2030], accounts, {1: 500_000_00}, start_cents=10_000_00,
        floor_cents=lambda aid, yr: 50_000_00,
    )
    year = plan.years[0]
    assert year.drawn_cents == 50_000_00 > year.target_cents
    assert year.raised_by_floor and plan.floor_raised_years == [2030]
    assert plan.lasts                       # taking MORE is not running out


def test_an_empty_account_beside_a_funded_one_is_not_running_out():
    """The reported defect, reduced: one account at zero, household fine."""
    accounts = [_plan_account(1, "Spent IRA"), _plan_account(2, "Funded IRA")]
    plan = retirement.plan_household_withdrawals(
        [2030, 2031], accounts, {1: 0, 2: 500_000_00}, start_cents=40_000_00)
    assert plan.lasts and plan.depleted_year is None
    assert all(y.amounts[1] == 0 for y in plan.years)


def test_the_pool_running_out_names_the_year_and_what_was_left():
    accounts = [_plan_account(1, "IRA"), _plan_account(2, "Roth", "roth")]
    plan = retirement.plan_household_withdrawals(
        [2030, 2031], accounts, {1: 10_000_00, 2: 5_000_00},
        start_cents=20_000_00,
    )
    assert plan.lasts is False
    assert plan.depleted_year == 2030
    assert plan.depleted_pool_cents == 15_000_00
    assert plan.years[0].shortfall_cents == 5_000_00


def test_growth_and_conversions_move_the_balances_between_years():
    accounts = [_plan_account(1, "IRA"), _plan_account(2, "Roth", "roth")]
    plan = retirement.plan_household_withdrawals(
        [2030, 2031], accounts, {1: 100_000_00, 2: 0},
        start_cents=10_000_00,
        growth=lambda aid, yr: Decimal("1.10"),
        other_net_cents=lambda aid, yr: (-20_000_00 if aid == 1 else 20_000_00),
    )
    # 100,000 - 10,000 drawn - 20,000 converted = 70,000, grown 10% = 77,000.
    assert plan.years[1].balances[1] == 77_000_00
    assert plan.years[1].balances[2] == 22_000_00


def test_no_account_is_ever_driven_negative():
    accounts = [_plan_account(i, f"IRA {i}") for i in (1, 2, 3)]
    plan = retirement.plan_household_withdrawals(
        list(range(2030, 2070)), accounts,
        {1: 40_000_00, 2: 5_000_00, 3: 250_00},
        start_cents=12_000_00, increase_pct="3",
        floor_cents=lambda aid, yr: 2_000_00,
    )
    for year in plan.years:
        for account_id, cents in year.amounts.items():
            assert 0 <= cents <= year.balances[account_id]
        assert sum(year.amounts.values()) == year.drawn_cents


def test_the_household_plan_round_trips_through_the_database(conn):
    assert retirement.get_withdrawal_plan(conn).is_set is False
    retirement.set_withdrawal_plan(conn, 48_000_00, "2.5")
    stored = retirement.get_withdrawal_plan(conn)
    assert stored.start_cents == 48_000_00
    assert stored.increase_pct == Decimal("2.5")
    assert stored.is_set
    retirement.set_withdrawal_plan(conn, 50_000_00, 0)      # one row, replaced
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM retirement_withdrawal_plan"
    ).fetchone()["n"] == 1
    assert retirement.get_withdrawal_plan(conn).start_cents == 50_000_00
    retirement.clear_withdrawal_plan(conn)
    assert retirement.get_withdrawal_plan(conn).is_set is False


def test_retirement_year_is_the_year_the_planned_claim_age_is_reached():
    person = {"birth_year": 1965, "birth_month": 5, "planned_claim_age_months": 804}
    assert retirement.retirement_year(person) == 2032
    # 67 and 8 months, born in May: the claim age is reached the next January
    assert retirement.retirement_year(dict(person, planned_claim_age_months=812)) == 2033
    assert retirement.retirement_year(dict(person, planned_claim_age_months=None)) is None


def test_federal_tax_runs_taxable_income_through_the_indexed_brackets():
    """Reported: a total tax figure to compare scenarios by."""
    from decimal import Decimal
    ladder = retirement.tax_brackets("joint")
    # The whole first bracket at its rate, then the next dollar at the second.
    first = ladder[0]
    at_top = retirement.federal_tax_cents(first.upper_cents, "joint",
                                          retirement.TAX_TABLE_YEAR, 0)
    assert at_top == first.upper_cents * first.rate_percent // 100
    one_more = retirement.federal_tax_cents(first.upper_cents + 100_00, "joint",
                                            retirement.TAX_TABLE_YEAR, 0)
    assert one_more - at_top == 100_00 * ladder[1].rate_percent // 100
    assert retirement.federal_tax_cents(0, "joint", 2030, Decimal("2.2")) == 0
    # Indexed brackets tax the same income less in a later year.
    assert retirement.federal_tax_cents(200_000_00, "joint", 2040, Decimal("2.2")) <         retirement.federal_tax_cents(200_000_00, "joint", 2026, Decimal("2.2"))


def test_state_tax_is_a_flat_rate_on_agi_without_social_security():
    """Reported: the plan was federal-only."""
    tax = retirement.year_tax(100_000_00, 0, 32_200_00, "joint", 2026, 0,
                              state_pct="5", social_security_taxed_cents=20_000_00)
    assert tax.state == 4_000_00                  # 5% of 100,000 less 20,000 of SS
    both = retirement.year_tax(100_000_00, 0, 32_200_00, "joint", 2026, 0,
                               state_pct="5", social_security_taxed_cents=20_000_00,
                               state_taxes_ss=True)
    assert both.state == 5_000_00 and both.total == both.federal + 5_000_00


# ---------------------------------------------------------------------------
# audit fixes: spousal and survivor benefits, the earnings test, IRC 72(t),
# the senior deduction, the ACA credit slope
# ---------------------------------------------------------------------------
def test_a_spousal_benefit_is_half_the_workers_pia_less_own_reduced_for_age():
    """42 USC 402(b): half the worker's PIA less the person's own, reduced
    25/36% a month for 36 months and 5/12% beyond; nothing when their own is
    the larger, and no delayed credit for starting late."""
    # Born 1962 (FRA 67); starting at 62 is 60 months early: 25% + 10%.
    assert retirement.spousal_benefit_cents(2_000_00, 500_00, 1962, 62 * 12) == 325_00
    assert retirement.spousal_benefit_cents(2_000_00, 500_00, 1962, 67 * 12) == 500_00
    assert retirement.spousal_benefit_cents(2_000_00, 500_00, 1962, 70 * 12) == 500_00
    assert retirement.spousal_benefit_cents(2_000_00, 1_200_00, 1962, 67 * 12) == 0
    assert retirement.spousal_benefit_cents(2_000_00, 0, 1962, 67 * 12) == 1_000_00


def test_a_survivor_benefit_is_reduced_to_71_5_pct_at_60_and_floored_by_the_rib_lim():
    """42 USC 402(e), (q): 100% at the survivor FRA (67 for 1962+), 71.5% at
    60; delayed credits pass through; an early claim caps the survivor at the
    larger of the reduced benefit and 82.5% of the PIA."""
    assert retirement.survivor_full_retirement_age_months(1962) == 67 * 12
    assert retirement.survivor_full_retirement_age_months(1956) == 66 * 12
    assert retirement.survivor_factor(1962, 60 * 12) == Decimal("0.715")
    assert retirement.survivor_factor(1962, 67 * 12) == 1
    assert retirement.survivor_benefit_cents(2_000_00, 1, 1962, 67 * 12) == 2_000_00
    assert retirement.survivor_benefit_cents(2_000_00, 1, 1962, 60 * 12) == 1_430_00
    # Delayed credits (claimed at 70, 24% more) pass to the survivor.
    assert retirement.survivor_benefit_cents(2_000_00, Decimal("1.24"), 1962, 67 * 12) == 2_480_00
    # The deceased claimed at 62 (70%): a survivor at FRA gets max(70%, 82.5%).
    assert retirement.survivor_benefit_cents(2_000_00, Decimal("0.70"), 1962, 67 * 12) == 1_650_00
    # ...and at 60 the smaller of the age reduction and that floor.
    assert retirement.survivor_benefit_cents(2_000_00, Decimal("0.70"), 1962, 60 * 12) == 1_430_00


def test_the_earnings_test_withholds_one_for_two_then_one_for_three():
    """42 USC 403(b), (f): $1 per $2 over the lower exempt amount before the
    FRA year, $1 per $3 over the higher one in it, never more than the
    benefit; nothing at or under the exempt amount."""
    withheld = retirement.earnings_test_withheld_cents
    assert withheld(24_480_00, 20_000_00, 2026, 0, year_of_full_retirement=False) == 0
    assert withheld(44_480_00, 20_000_00, 2026, 0, year_of_full_retirement=False) == 10_000_00
    assert withheld(94_480_00, 20_000_00, 2026, 0, year_of_full_retirement=False) == 20_000_00
    assert withheld(74_160_00, 20_000_00, 2026, 0, year_of_full_retirement=True) == 3_000_00
    # The exempt amounts are indexed past the table year.
    assert withheld(44_480_00, 20_000_00, 2036, Decimal("2.2"),
                    year_of_full_retirement=False) < 10_000_00


def test_an_early_distribution_comes_last_and_pays_ten_percent_more():
    """IRC 72(t): an owner under 59 1/2 pays 10% on top; the plan spends the
    Roth first and touches the IRA only when nothing else is left."""
    accounts = [_plan_account(1, "IRA"), _plan_account(2, "Roth", "roth")]
    plan = retirement.plan_household_withdrawals(
        [2030, 2031], accounts, {1: 500_000_00, 2: 50_000_00},
        start_cents=80_000_00,
        early_ids=lambda year: {1} if year <= 2030 else set(),
    )
    first, second = plan.years
    # 2030: the Roth's 50,000 first, then the IRA for the rest plus its 10%:
    # 30,000 spent needs 30,000 / 0.9 = 33,333.33 out of it.
    assert first.amounts[2] == 50_000_00
    assert first.amounts[1] == 33_333_33
    assert first.penalty_cents == 3_333_33 == first.tax_cents
    assert first.target_cents == 80_000_00 + 3_333_33
    # 2031: 59 1/2 reached - no penalty, and the IRA is first again.
    assert second.penalty_cents == 0 and second.amounts[1] == 80_000_00
    assert retirement.early_distribution_last_year(1970, 3) == 2028   # 59 1/2 in 2029
    assert retirement.early_distribution_last_year(1970, 9) == 2029   # 59 1/2 in 2030
    assert retirement.early_distribution_last_year(1970, None) == 2029


def test_the_senior_deduction_runs_2025_to_2028_and_phases_out_at_six_percent():
    """Pub. L. 119-21 sec. 70103: $6,000 per person 65+, less 6% of MAGI over
    $75,000 / $150,000, on a joint or single return only."""
    senior = retirement.senior_deduction_cents
    assert senior("joint", 2, 100_000_00, 2026) == 12_000_00
    assert senior("joint", 2, 200_000_00, 2026) == 9_000_00
    assert senior("joint", 2, 350_000_00, 2026) == 0
    assert senior("single", 1, 100_000_00, 2028) == 4_500_00
    assert senior("single", 1, 100_000_00, 2029) == 0
    assert senior("joint", 0, 100_000_00, 2026) == 0
    assert senior("separate", 1, 10_000_00, 2026) == 0
    # It comes off taxable income in year_tax and in the chart's measure.
    with_it = retirement.year_tax(100_000_00, 0, 32_200_00, "joint", 2026, 0, seniors=2)
    without = retirement.year_tax(100_000_00, 0, 32_200_00, "joint", 2026, 0)
    assert without.ordinary - with_it.ordinary == 12_000_00 * 12 // 100
    assert retirement.taxable_ordinary_cents(100_000_00, 0, "joint", 0, 32_200_00,
                                             seniors=2, year=2026) == 55_800_00
    # The room under a top counts the deduction it takes away: 6 cents a dollar
    # once over the phase-out start, found by bisection.
    # At 160,000 the deduction is 11,400 and taxable 116,400; each dollar more
    # adds 1.06 of taxable income, so the room to 120,000 is 3,600 / 1.06.
    room = retirement.taxable_room_cents(120_000_00, 160_000_00, 0, "joint", 0,
                                         32_200_00, seniors=2, year=2026)
    assert retirement.taxable_ordinary_cents(160_000_00 + room, 0, "joint", 0, 32_200_00,
                                             seniors=2, year=2026) == 120_000_00
    assert 3_396_00 <= room <= 3_396_50                    # not the plain 3,600


def test_the_aca_credit_is_the_benchmark_less_a_rising_share_of_income():
    """IRC 36B(b)(3)(A) with the 2026 percentages: 2.10% of income at the
    poverty line, 9.96% from 300% to 400% of it, nothing outside 100-400%."""
    fpl = 21_640_00
    credit = retirement.aca_premium_credit_cents
    assert retirement.aca_applicable_pct(fpl, fpl) == Decimal("2.10")
    assert retirement.aca_applicable_pct(fpl * 4, fpl) == Decimal("9.96")
    assert retirement.aca_applicable_pct(fpl * 4 + 1, fpl) is None
    assert retirement.aca_applicable_pct(fpl - 1, fpl) is None
    assert credit(20_000_00, fpl, fpl) == 20_000_00 - 454_44          # 2.10% of 21,640
    assert credit(20_000_00, fpl * 3, fpl) == 20_000_00 - 6_466_03    # 9.96% of 64,920
    assert credit(20_000_00, fpl * 4 + 1, fpl) == 0                   # the cliff
    assert credit(20_000_00, 50_000_00, fpl) < credit(20_000_00, 40_000_00, fpl)
    assert credit(5_000_00, fpl * 3, fpl) == 0                        # never negative


# ---------------------------------------------------------------------------
# audit fixes, second round: inherited accounts, the divisor for a much younger
# spouse, the April 1 deferral, contribution caps, positions and losses, Roth
# layers, IRA aggregation, IRMAA details, Social Security small things
# ---------------------------------------------------------------------------
def test_an_inherited_account_follows_the_ten_year_rule():
    """IRC 401(a)(9)(H): a death in 2020 or later empties the account by the
    tenth year; annual minimums on the beneficiary's single life expectancy
    (set the year after, less one a year) only when the decedent had begun
    them. A pre-2020 inheritance stretches over that expectancy throughout."""
    floored = retirement.inherited_floored_in
    assert floored(2024, True, 2025) and not floored(2024, False, 2025)
    assert floored(2024, False, 2034) and not floored(2024, True, 2035)
    assert floored(2015, False, 2040)
    minimum = retirement.inherited_minimum_cents
    # Beneficiary born 1970 is 55 in 2025: 31.6, then 30.6.
    assert minimum(100_000_00, 1970, 2024, True, 2025) == 316_456
    assert minimum(100_000_00, 1970, 2024, True, 2026) == 326_797
    assert minimum(50_000_00, 1970, 2024, False, 2030) == 0
    assert minimum(50_000_00, 1970, 2024, False, 2034) == 50_000_00
    assert retirement.single_life_expectancy(0) == Decimal("84.6")
    assert retirement.single_life_expectancy(120) == Decimal("1.0")


def test_a_much_younger_spouse_lengthens_the_divisor():
    """Reg. 1.401(a)(9)-5: a spouse more than ten years younger brings the
    Joint and Last Survivor Table, stood in for by the spouse's own single
    life expectancy - never less than the joint figure, never under it."""
    assert retirement.rmd_divisor(75, 65) == retirement.uniform_lifetime_divisor(75)
    assert retirement.rmd_divisor(75, 60) == retirement.single_life_expectancy(60) == Decimal("27.1")
    alone = retirement.rmd(100_000_00, 1951, 6, 2026)
    assert retirement.rmd(100_000_00, 1951, 6, 2026, spouse_birth_year=1966) < alone
    assert retirement.rmd(100_000_00, 1951, 6, 2026, spouse_birth_year=1960) == alone


def test_the_first_minimum_can_wait_until_april_1():
    """IRC 401(a)(9)(C)(i): deferred, the first year takes nothing and the
    next takes two, the first from the balance the first year entered with."""
    minimum = retirement.owner_minimum_cents
    assert minimum(100_000_00, None, 1953, 6, 2026, first_year=2026) == retirement.rmd(
        100_000_00, 1953, 6, 2026)
    assert minimum(100_000_00, None, 1953, 6, 2026, first_year=2026, defer_first=True) == 0
    two = minimum(110_000_00, 100_000_00, 1953, 6, 2027, first_year=2026, defer_first=True)
    assert two == (retirement.rmd(100_000_00, 1953, 6, 2026)
                   + retirement.rmd(110_000_00, 1953, 6, 2027))
    assert minimum(110_000_00, 100_000_00, 1953, 6, 2028, first_year=2026,
                   defer_first=True) == retirement.rmd(110_000_00, 1953, 6, 2028)


def test_contributions_are_capped_and_a_high_earners_catch_up_is_roth():
    """IRC 402(g), 414(v), 415(c); SECURE 2.0 sec. 603."""
    capped = retirement.capped_contribution_cents
    assert capped(200_000_00, 20, 5, 55, 2026, 0, prior_wages_cents=200_000_00) == (
        24_500_00, 8_000_00, 10_000_00)
    assert capped(200_000_00, 20, 5, 55, 2026, 0, prior_wages_cents=100_000_00) == (
        32_500_00, 0, 10_000_00)
    assert capped(200_000_00, 20, 5, 45, 2026, 0) == (24_500_00, 0, 10_000_00)
    assert capped(60_000_00, 10, 3, 45, 2026, 0) == (6_000_00, 0, 1_800_00)
    assert capped(1_000_000_00, 2, 10, 45, 2026, 0) == (20_000_00, 0, 52_000_00)


def test_sales_take_the_cheapest_position_and_a_net_loss_offsets_and_carries():
    """IRC 1012, 1211(b): the loss position goes first, a net loss takes
    $3,000 off ordinary income and the rest carries into the next year's gains."""
    plan = retirement.plan_household_withdrawals(
        [2030, 2031], [_plan_account(1, "Brokerage", "taxable")], {1: 100_000_00},
        start_cents=30_000_00,
        basis_cents={1: [(40_000_00, 50_000_00), (60_000_00, 20_000_00)]},
    )
    first, second = plan.years
    # 30,000 out of the loss position: -7,500; 3,000 used, 4,500 carried.
    assert first.gains_cents == -3_000_00
    # The last 10,000 of it (-2,500), then 20,000 of the gain position
    # (+13,333.33), less the 4,500 carried.
    assert second.gains_cents == -2_500_00 + 1_333_333 - 4_500_00


def test_a_roths_young_conversions_and_earnings_cost_ten_percent_before_59_and_a_half():
    """IRC 408A(d)(3)(F), 408A(d)(2)(B): a conversion drawn within five years,
    and earnings, pay the 10% while the owner is under 59 1/2; earnings out
    of a Roth opened by conversion before its fifth year are taxed."""
    accounts = [_plan_account(1, "IRA"), _plan_account(2, "Roth", "roth")]
    plan = retirement.plan_household_withdrawals(
        [2029, 2030, 2031, 2032], accounts, {1: 500_000_00, 2: 0},
        start_cents=20_000_00,
        other_net_cents=lambda aid, yr: ({1: -50_000_00, 2: 50_000_00}.get(aid, 0)
                                         if yr == 2029 else 0),
        conversions_in=lambda aid, yr: 50_000_00 if aid == 2 and yr == 2029 else 0,
        early_ids=lambda yr: {1, 2},
        growth=lambda aid, yr: Decimal("1.5") if aid == 2 else Decimal(1),
    )
    y1, y2, y3, y4 = plan.years
    # 2029: the Roth is empty, so the IRA pays, plus 10% on every dollar.
    assert y1.amounts[2] == 0 and y1.penalty_cents == y1.amounts[1] * 10 // 100 > 0
    # 2030-2031: the Roth first (the IRA last), out of the 2029 conversion -
    # under five years old, so the same 10%; nothing taxed as earnings yet.
    assert y2.amounts[1] == 0 and y2.penalty_cents == y2.amounts[2] * 10 // 100 > 0
    assert y2.roth_taxable_cents == y3.roth_taxable_cents == 0
    # 2032: the conversion is used up; what follows is earnings, taxed and
    # penalized, since the Roth opened in 2029.
    assert y4.roth_taxable_cents > 0 and y4.penalty_cents > 0


def test_one_owners_ira_minimums_are_totaled_and_the_exempt_one_pays_from_the_other():
    """Reg. 1.408-8: an IRA the household leaves alone pays its minimum out
    of the owner's other IRAs."""
    accounts = [_plan_account(1, "IRA A", is_ira=True, owner_person_id=7),
                _plan_account(2, "IRA B", is_ira=True, owner_person_id=7)]
    plan = retirement.plan_household_withdrawals(
        [2030], accounts, {1: 100_000_00, 2: 100_000_00}, start_cents=0,
        floor_cents=lambda aid, yr: 4_000_00, exempt_ids={2})
    year = plan.years[0]
    assert year.floor_cents == 8_000_00 and year.amounts == {1: 8_000_00, 2: 0}


def test_irmaa_top_tier_is_frozen_through_2027_and_part_d_is_optional():
    pct = Decimal("2.2")
    # The cut is exclusive ("less than $500,000"), stored a cent under.
    assert retirement.irmaa_ceilings_cents("single", 2027, pct)[-1] == 49_999_999
    assert retirement.irmaa_ceilings_cents("single", 2028, pct)[-1] == (
        retirement.household_amount_cents(49_999_999, pct, 1))
    assert retirement.irmaa_ceilings_cents("single", 2027, pct)[0] > 109_000_00
    with_d = retirement.irmaa_surcharge_cents(150_000_00, "single", 2026)[1]
    without = retirement.irmaa_surcharge_cents(150_000_00, "single", 2026, part_d=False)[1]
    assert with_d - without == 37_50 * 12


def test_medicare_can_start_after_65_and_a_claim_at_62_pays_from_62_and_a_month():
    person = {"birth_year": 1961, "birth_month": 3, "born_on_the_first": 0}
    assert retirement.medicare_months(person, 2026) == 10
    assert retirement.medicare_months(person, 2026, start_override=(2028, 1)) == 0
    assert retirement.medicare_months(person, 2028, start_override=(2028, 1)) == 12
    assert retirement.medicare_months(person, 2026, start_override=(2025, 1)) == 10
    assert retirement.earliest_claim_months(person, 62 * 12) == 62 * 12 + 1
    assert retirement.earliest_claim_months({"born_on_the_first": 1}, 62 * 12) == 62 * 12
    assert retirement.earliest_claim_months(person, 63 * 12) == 63 * 12


def test_wage_indexing_is_projected_past_the_series_and_benefits_are_dimes():
    last = max(retirement.AWI_SERIES.rows)
    assert retirement.indexing_factor(last, last + 5 - 60) > 1     # turns 60 after the series
    assert retirement.indexing_factor(last, last - 60) == 1
    assert retirement.primary_insurance_amount(600_000, 2026) % 10 == 0
    assert retirement.benefit_from_pia_cents(266_580, Decimal("0.70")) == 186_600


def test_the_state_can_exempt_retirement_income_and_a_loss_comes_off_ordinary_income():
    tax = retirement.year_tax(100_000_00, 0, 32_200_00, "joint", 2026, 0,
                              state_pct="5", state_excluded_cents=60_000_00)
    assert tax.state == 2_000_00
    gross = retirement.gross_with_social_security_cents
    assert gross(50_000_00, 0, "joint", -10_000_00) == 47_000_00       # $3,000 of a loss
    assert gross(50_000_00, 0, "joint", 0, 10_000_00) == 50_000_00      # exempt interest
    # ...which still makes more of the benefit taxable (IRC 86(b)(2)(B)).
    assert gross(20_000_00, 20_000_00, "single", 0, 20_000_00) > gross(
        20_000_00, 20_000_00, "single")
