"""mammon.forecast -- closed-form projection of an investing portfolio.

Pure domain, like :mod:`mammon.loans`: no Qt, no SQL, no database connection in
any signature. Nothing here reads or writes the ledger; it turns a starting
balance, a contribution schedule and an asset mix into a percentile fan.

WHY CLOSED FORM, AND WHY THERE IS NO SIMULATION HERE
----------------------------------------------------
Annual gross returns are modeled as i.i.d. lognormal and contributions are
periodic, so terminal wealth is

    W_N = W_0 * prod_{j<=N} R_j  +  sum_k c_k * prod_{j>k} R_j

-- a *sum* of lognormals, because every contribution compounds over its own
remaining horizon. A sum of lognormals has no closed-form distribution, which
is the usual excuse for reaching for a simulator. But its first two moments
ARE exactly closed form (``moments`` below: two multiply-adds per period,
O(N), exact), and a lognormal matched to those two moments (Fenton-Wilkinson,
``lognormal_params``) gives every percentile as a single ``exp()``. A 50-year
monthly fan is 600 recursion steps and a few thousand exponentials --
microseconds, so a chart can redraw live under a slider.

WHEN MONEY COMES OUT: MOMENTS WITH A FLOOR
-----------------------------------------
The plain recursion is only sound while every flow goes IN. A withdrawal makes
W a DIFFERENCE of lognormals: it can reach zero, and the plain recursion does
not know that - it keeps drawing from a ruined fund and compounding the debt,
while the fitted lognormal (which cannot be zero) keeps a positive median.
Reported: the Retirement Planner's fund line showed a large balance in the very
year its own account-by-account check said the money ran out.

So the state carried from period to period is not two numbers but a small
MIXTURE: ``p0``, the probability the fund is already empty, and one or more
funded PARTS, each a mass with its own mean and second moment
(:class:`FundState`). A withdrawal of d floors each part at zero - a household
that ran dry stops withdrawing - and the floored moments are closed form for
the lognormal fitted to that part:

    E[(X-d)+]   = e^(M+S^2/2) N(k+S) - d N(k)                k = (M - ln d)/S
    E[(X-d)+^2] = e^(2M+2S^2) N(k+2S) - 2d e^(M+S^2/2) N(k+S) + d^2 N(k)
    P(X <= d)   = N(-k)

The emptied mass moves into ``p0``. A CONTRIBUTION after that lifts the empty
mass off zero, but only to the contribution itself: it becomes a second, small
part rather than being folded into the funded one. Folding was the earlier
shape, and it was wrong in exactly the state a retirement plan produces - a
year of Social Security covering the spending after the fund has half run dry
- because one lognormal fitted to the moments of a two-humped distribution puts
its median between the humps (an audit measured the median overstated tens of
times over at half ruin). Percentiles come from the mixture: zero below
``p0``, then the parts' curves summed. Parts are capped at :data:`MAX_PARTS`,
the lightest two merged by moment matching beyond that; a restarted part is
small and is usually emptied again within a year, so the cap is rarely reached.
Checked once against a scratch sampling of the same model (never shipped): the
median tracks within about 5-10% until the last years before the fund runs dry,
where any single-curve fit is least exact. :class:`TrackedFund` carries the same
recursion a year at a time for the planner's household check. Returns
multiply, so an empty fund stays empty until money goes back in. With no
withdrawals ``p0`` stays 0, there is one part, and every step is exactly
:func:`moments`, so a contribution-only fan is unchanged. Still two
multiply-adds and a few ``erf`` calls per period and part.

A PLAN IS KEYED TO CALENDAR YEARS, AND THE FIRST ONE IS PARTIAL
---------------------------------------------------------------
On the 26th of September the current year has three months left, so today's
balance grows three months to reach the first of January, not twelve:
:func:`steps_from_annual` takes ``first_year_periods`` and builds the first
year from that many steps, and :func:`fan_from_steps` and :class:`TrackedFund`
report the first boundary at that fractional horizon. Growing today's balance
a whole year before the next one overstated every later year by nine months
of growth (found in the audit of 2026-09-26).

The first year's flow is an AMOUNT STILL TO HAPPEN, spread over the months
left - not a yearly rate taken for them. A conversion or a planned draw for
this year is money that has to move before December 31 whatever the date, and
the household plan moves all of it. Taken as a rate, a quarter of it moved
(reported: the conversion cap then saw an IRA the plan had emptied as still
holding a third of a million, a fill split its conversion by that, and the
plan cut it back short of the line). A CONTRIBUTION is a stream, so its
caller prorates it to the months left before handing it here
(``retirement_planner.planned_contributions``, the dashboard's measured
inflows). A projection in plain "years from now" (the dashboard with the plan
off) passes nothing and keeps whole years.

There is deliberately no sampling and no generator of pseudo-chance in this
module, and there must never be one. A simulator would be slower, would jitter
between redraws for no informational gain, and would hide the fact that the
answer is analytic. A test greps this file to keep it that way.

WHAT THIS MODULE REFUSES TO DO
------------------------------
It does not optimize. The risk ladder and the correlation matrix are
hand-written, documented, editable constants -- conventional round numbers, not
measurements, and never estimated from the user's own holdings. SRD 5.8f keeps
correlation work diagnostic rather than prescriptive; a mean-variance optimizer
at runtime would be both prescriptive and wildly sensitive to inputs that are
themselves guesses. Every number in ``DEFAULT_ASSUMPTIONS``, ``CORRELATIONS``
and the ladder is a DEFAULT the user may overwrite, not a fact.

MONEY AND FLOATS
----------------
CLAUDE.md locks ledger money to signed integer cents, no floats. A projection
is not ledger money: it is a model output that is never stored, never summed
with a real balance and never written anywhere. So this module takes cents at
its boundary, converts to float exactly once, does the maths in float, and
rounds back to cents with ROUND_HALF_UP in :class:`FanPoint`. The exception is
deliberate and confined to this file; the alternative -- ``Decimal.exp`` over
thousands of evaluations per redraw -- buys precision that a 15% volatility
assumption makes meaningless.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from mammon import portfolio

# Re-exported, never redefined: the classes a projection speaks in are exactly
# the classes the user already assigns per security and that
# ``portfolio.allocation`` already produces.
ASSET_CLASSES = portfolio.ASSET_CLASSES

#: Crypto is not an equity and it is emphatically not cash, and folding it into
#: "other" (12% volatility) understates it by a factor of six. This module
#: carried the class alone for a while, ahead of the day
#: ``portfolio.ASSET_CLASSES`` gained the column; that day came on 2026-09-21,
#: so the name is kept only because several call sites spell it.
CRYPTO_CLASS = "crypto"

#: What a weight dict handed to this module may legally name. Now exactly the
#: stored classes: upstream can finally EMIT every key the assumptions table
#: prices, so the two vocabularies are one.
PROJECTION_CLASSES: tuple[str, ...] = tuple(ASSET_CLASSES)

# The class every unrecognized or unclassified weight is folded into. 4.2's
# "other" is deliberately mediocre, so an unclassified holding is never
# flattered by the projection -- the fix is to classify it.
_FALLBACK_CLASS = "other"


@dataclass(frozen=True)
class ClassAssumption:
    """One editable row of the capital-market assumptions table."""

    name: str
    mean_return: float  # annual arithmetic, 0.07 == 7%
    volatility: float  # annual stdev of the arithmetic return
    source: str  # one line, shown in the assumptions editor


#: Defaults only. Every cell is editable; two are openly uncited placeholders.
DEFAULT_ASSUMPTIONS: dict[str, ClassAssumption] = {
    "domestic_stock": ClassAssumption(
        "domestic_stock", 0.070, 0.151,
        "Damodaran 9.94% geometric haircut toward VCMM's 2.8-4.8% 10yr range; "
        "VCMM U.S. equity volatility.",
    ),
    "intl_stock": ClassAssumption(
        "intl_stock", 0.070, 0.188,
        "VCMM global ex-U.S. unhedged 4.9-6.9% / 18.8%. Return held level with "
        "domestic: a deliberate refusal to forecast a regional winner.",
    ),
    "bond": ClassAssumption(
        "bond", 0.045, 0.062,
        "VCMM U.S. aggregate 3.8-4.8% / 6.2%; Damodaran 10yr Treasuries 4.50%.",
    ),
    "cash": ClassAssumption(
        "cash", 0.033, 0.011,
        "VCMM U.S. cash 2.9-3.9% / 1.1%; Damodaran T-bills 3.31%.",
    ),
    "real_estate": ClassAssumption(
        "real_estate", 0.060, 0.180,
        "UNCITED placeholder: return anchored on Damodaran's 4.23% and raised "
        "for a REIT rather than owner-occupied proxy; no public REIT "
        "volatility assumption was obtained.",
    ),
    # Crypto. Sigma is a MEASUREMENT, mu deliberately is not:
    #   sigma  ETH-USD daily log returns, Yahoo Finance closes, annualized by
    #          sqrt(365): 0.843 over 2018-01-01..2026-09-18 (3183 closes),
    #          0.693 over the trailing five years, 0.664 over the trailing
    #          three. 0.75 sits between the post-2020 regime and the full
    #          sample rather than betting that the calm-down is permanent. It
    #          is ~4x domestic equity's 0.151, which is the whole point of the
    #          class existing.
    #   mu     No coupon, no dividend, no earnings -- there is no VCMM or
    #          Damodaran building block to discount, so there is nothing to
    #          haircut honestly. Extrapolating the realized 15.0%/yr CAGR of
    #          that same window (a window that merely starts and ends where it
    #          does) would forecast the run-up, so instead: cash 3.3% plus
    #          about 1.5x the ~4.5pt premium domestic equity is assumed to earn
    #          over it. That is above stocks and far below history.
    # Note the consequence, which is intended: at sigma 0.75 the variance drag
    # (sigma^2/2 ~ 0.28) sinks the projected MEDIAN well below a 10% mean, so a
    # crypto-heavy mix shows a wide fan whose middle falls. A projection that
    # made crypto look like a better-paying stock would be the bug.
    "crypto": ClassAssumption(
        "crypto", 0.100, 0.750,
        "Realized ETH-USD volatility (Yahoo Finance daily closes): 84% since "
        "2018, 69% trailing five years. Return is cash + ~1.5x the equity "
        "premium -- NOT the 15%/yr realized CAGR.",
    ),
    "other": ClassAssumption(
        "other", 0.045, 0.120,
        "UNCITED placeholder: bond-like return with equity-like uncertainty, "
        "so leaving a holding unclassified is never flattering.",
    ),
}

#: Conventional round numbers, not measurements, and the UI must say so.
#: Applied to WEIGHTS, not to draws -- one closed-form line, no Cholesky.
#: Any pair not listed here (and not a class with itself) is 0.0; notably
#: everything against cash except bonds.
CORRELATIONS: dict[tuple[str, str], float] = {
    ("domestic_stock", "intl_stock"): 0.85,
    ("domestic_stock", "bond"): 0.10,
    ("intl_stock", "bond"): 0.10,
    ("domestic_stock", "real_estate"): 0.65,
    ("bond", "cash"): 0.20,
    ("other", "domestic_stock"): 0.50,
    ("other", "intl_stock"): 0.50,
    ("other", "real_estate"): 0.50,
    # ETH-USD vs SPY daily log returns ran 0.33 over 2018-2026 and 0.41 over
    # the trailing three years; 0.40 is the round number in that range, and
    # erring high is the cautious side (a diversification claim is the
    # dangerous error). Crypto against bonds and cash stays 0.0.
    ("crypto", "domestic_stock"): 0.40,
    ("crypto", "intl_stock"): 0.40,
    ("crypto", "other"): 0.40,
}

#: Default annual inflation, used only when a caller asks for real dollars.
DEFAULT_INFLATION = 0.030  # Damodaran, 1928-2024

#: Standard-normal quantiles for the five fan bands. Not looked up at runtime
#: and not approximated: a fan band is a constant times S.
Z_05 = -1.6448536269514722
Z_25 = -0.6744897501960817
Z_50 = 0.0
Z_75 = 0.6744897501960817
Z_95 = 1.6448536269514722


def correlation(a: str, b: str,
                correlations: Mapping[tuple[str, str], float] | None = None) -> float:
    """Correlation between two asset classes, order-insensitive.

    A class with itself is 1.0; an unlisted pair is 0.0. Callers pass a full
    or partial override dict; missing pairs still fall back to the defaults'
    rule rather than to whatever happens to be in the override.
    """
    if a == b:
        return 1.0
    table = CORRELATIONS if correlations is None else correlations
    if (a, b) in table:
        return float(table[(a, b)])
    if (b, a) in table:
        return float(table[(b, a)])
    return 0.0


def normalize_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """A weight dict over exactly PROJECTION_CLASSES, summing to 1 (or zero).

    Unknown keys -- including ``portfolio.allocation``'s "unclassified" -- fold
    into "other" rather than being dropped, so an unclassified holding still
    carries risk instead of silently vanishing from the mix. "crypto" is a key
    in its own right and must NOT fold: at "other"'s 12% volatility it would
    read as a sleepy bond substitute.
    """
    out = {cls: 0.0 for cls in PROJECTION_CLASSES}
    for key, value in weights.items():
        cls = key if key in out else _FALLBACK_CLASS
        out[cls] += float(value)
    total = sum(out.values())
    if total > 0 and abs(total - 1.0) > 1e-12:
        out = {cls: value / total for cls, value in out.items()}
    return out


def portfolio_moments(
    weights: Mapping[str, float],
    assumptions: Mapping[str, ClassAssumption] | None = None,
    correlations: Mapping[tuple[str, str], float] | None = None,
) -> tuple[float, float]:
    """(mu_p, sigma_p) for a weight dict over PROJECTION_CLASSES.

        mu_p        = sum_i w_i mu_i
        sigma_p^2   = sum_i sum_j w_i w_j sigma_i sigma_j rho_ij
    """
    table = DEFAULT_ASSUMPTIONS if assumptions is None else assumptions
    w = normalize_weights(weights)
    classes = [cls for cls in PROJECTION_CLASSES if w[cls]]
    mu = 0.0
    for cls in classes:
        mu += w[cls] * table[cls].mean_return
    var = 0.0
    for i in classes:
        for j in classes:
            var += (w[i] * w[j] * table[i].volatility * table[j].volatility
                    * correlation(i, j, correlations))
    return mu, math.sqrt(max(0.0, var))


def _ladder_weights(equity: float) -> dict[str, float]:
    """4.5's stated rule: all cash at the bottom, all stocks at the top.

        cash = (1-e)^2   bond = (1-e)e   domestic = 0.6e   intl = 0.4e

    The 60/40 domestic/international split is a convention, editable with the
    rest of the assumptions. The weights sum to 1 for every e in [0, 1].
    """
    e = float(equity)
    # Built FROM ASSET_CLASSES, not as a literal of the four the rule names:
    # mix_for_risk interpolates rung dicts key by key over ASSET_CLASSES, so a
    # dict missing a class raises KeyError the moment one is added -- which is
    # exactly what adding `crypto` did. The ladder itself is unchanged; the
    # classes the rule does not use are explicitly zero.
    out = {cls: 0.0 for cls in ASSET_CLASSES}
    out.update({
        "domestic_stock": 0.6 * e,
        "intl_stock": 0.4 * e,
        "bond": (1.0 - e) * e,
        "cash": (1.0 - e) ** 2,
    })
    return out


MAX_RISK_LEVEL = 10

#: The thermometer: 11 rungs, level 0 (all cash) through level 10 (all
#: stocks). Both mu_p and sigma_p are strictly increasing across the rungs,
#: which is what makes the thermometer a meaningful single axis. Generated
#: from one stated rule rather than eleven hand-typed rows, but pinned by a
#: test so the displayed numbers cannot drift silently.
RISK_LADDER: tuple[tuple[float, dict[str, float]], ...] = tuple(
    (float(k), _ladder_weights(k / MAX_RISK_LEVEL))
    for k in range(MAX_RISK_LEVEL + 1)
)

_LADDER_SIGMA: tuple[float, ...] = tuple(
    portfolio_moments(mix)[1] for _level, mix in RISK_LADDER
)


def mix_for_risk(level: float) -> dict[str, float]:
    """Thermometer level in [0, 10] -> weights, interpolated between rungs.

    The slider is continuous, so between rungs the WEIGHTS are interpolated and
    (mu_p, sigma_p) recomputed from them -- never the other way round, so the
    mix shown and the maths displayed beside it can never disagree.
    """
    x = min(max(float(level), 0.0), float(MAX_RISK_LEVEL))
    low = int(math.floor(x))
    high = min(low + 1, MAX_RISK_LEVEL)
    frac = x - low
    lo_mix = RISK_LADDER[low][1]
    hi_mix = RISK_LADDER[high][1]
    return {cls: lo_mix[cls] * (1.0 - frac) + hi_mix[cls] * frac
            for cls in ASSET_CLASSES}


def sigma_for_risk(level: float) -> float:
    """The volatility the thermometer shows at ``level``."""
    return portfolio_moments(mix_for_risk(level))[1]


def risk_for_mix(
    weights: Mapping[str, float],
    assumptions: Mapping[str, ClassAssumption] | None = None,
    correlations: Mapping[tuple[str, str], float] | None = None,
) -> float:
    """Inverse: weights -> the level with matching sigma_p, clamped to [0, 10].

    Matching on sigma rather than on equity share means a bond-heavy but
    volatile portfolio lands where it belongs. Outside the ladder's sigma
    range the needle clamps, and the caller is expected to say so.

    The inversion bisects the CONTINUOUS ladder curve (``sigma_for_risk``),
    not the eleven rung sigmas. Interpolating rung sigmas instead looks
    simpler but is wrong by up to a fifth of a level, because between rungs
    the weights are interpolated and sigma is not linear in them -- which
    would make the needle disagree with the mix the slider shows at the same
    position. Bisection is deterministic and converges in a fixed 60 steps.

    The rung sigmas are strictly increasing, but the continuous curve dips by
    ~0.002 percentage points over roughly the first twentieth of a level: a
    sliver of near-uncorrelated bond and stock genuinely lowers the risk of an
    all-cash portfolio. So the inverse is ambiguous by <0.1 of a level at the
    very bottom of the ladder, and exact everywhere else. Pinned by a test.
    """
    sigma = portfolio_moments(weights, assumptions, correlations)[1]
    if sigma <= _LADDER_SIGMA[0]:
        return 0.0
    if sigma >= _LADDER_SIGMA[-1]:
        return float(MAX_RISK_LEVEL)
    lo, hi = 0.0, float(MAX_RISK_LEVEL)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if sigma_for_risk(mid) < sigma:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# --------------------------------------------------------------------------
# The projection itself
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """One period of the recursion.

    ``contribution`` is added at the START of the period and the period's
    return is applied after it, so a period's contribution compounds for the
    whole period (an annuity due).
    """

    contribution: float  # dollars added at the START of this period
    mean: float  # m, this period's E[R]
    second: float  # q, this period's E[R^2]


def log_params(mu: float, sigma: float) -> tuple[float, float]:
    """Annual arithmetic (mean, stdev) -> annual lognormal (mu_L, sigma_L^2).

        sigma_L^2 = ln(1 + s^2 / (1 + a)^2)
        mu_L      = ln(1 + a) - sigma_L^2 / 2
    """
    growth = 1.0 + float(mu)
    if growth <= 0:
        raise ValueError("mean return must be greater than -100%")
    sigma_l2 = math.log(1.0 + (float(sigma) ** 2) / (growth * growth))
    mu_l = math.log(growth) - sigma_l2 / 2.0
    return mu_l, sigma_l2


def period_factors(mu: float, sigma: float, *,
                   periods_per_year: int = 12) -> tuple[float, float]:
    """One period's (m, q) = (E[R], E[R^2]) for an annual (mean, volatility).

    A glide path (a time-varying mix) builds its own ``Step`` list by calling
    this once per period with that period's mix; the recursion itself never
    cares whether the factors change.
    """
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    mu_l, sigma_l2 = log_params(mu, sigma)
    dt = 1.0 / periods_per_year
    m = math.exp(mu_l * dt + sigma_l2 * dt / 2.0)
    q = math.exp(2.0 * mu_l * dt + 2.0 * sigma_l2 * dt)
    return m, q


def steps(annual_contribution: float, mu: float, sigma: float, years: int,
          *, periods_per_year: int = 12) -> list[Step]:
    """Constant-mix case. A glide path builds its own list."""
    if years < 0:
        raise ValueError("years must not be negative")
    m, q = period_factors(mu, sigma, periods_per_year=periods_per_year)
    per_period = float(annual_contribution) / periods_per_year
    return [Step(per_period, m, q)] * (int(years) * periods_per_year)


def first_year_periods(today, *, periods_per_year: int = 12) -> int:
    """How many of this year's periods are still ahead on ``today``: the months
    after the current one, so September gives three (the module notes). Takes a
    ``date`` or an ISO ``YYYY-MM-DD`` string. Never less than one: in December
    the rest of the year is one period, because a conversion still planned for
    it has to happen somewhere."""
    if periods_per_year != 12:
        raise ValueError("first_year_periods is defined for monthly periods")
    month = int(str(today)[5:7])
    if not 1 <= month <= 12:
        raise ValueError(f"not a date: {today!r}")
    return max(1, 12 - month)


def steps_from_annual(annual_flows: Sequence[float], mu: float, sigma: float,
                      *, periods_per_year: int = 12,
                      first_year_periods: int | None = None) -> list[Step]:
    """Constant mix, but a flow that CHANGES from year to year.

    :func:`steps` is the one-number case; a retirement plan is the other one -
    it moves a different amount every year, and a projection built from any
    single year of it is a projection of a plan nobody has. Each year's flow is
    spread evenly across that year's periods, exactly as :func:`steps` spreads
    the constant. A NEGATIVE entry is a withdrawal: the recursion in
    :func:`moments` never looks at the sign.

    ``first_year_periods`` makes the first year PARTIAL (the module notes):
    that many steps, the first year's WHOLE flow spread across them - it is an
    amount still to happen this year, not a rate. A contribution stream is
    prorated by its caller first. Every later year is whole. None keeps whole
    years throughout.

    One function rather than one per caller, because two callers now hand a
    per-year series to the same math - the Retirement Planner's fund line and
    the Investment Dashboard's projection fan - and they must not be allowed to
    drift into disagreeing about the same plan.

    Flows are in DOLLARS, matching ``Step.contribution`` and :func:`steps`;
    callers holding cents divide by 100 first.
    """
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if first_year_periods is not None and not 1 <= int(first_year_periods) <= periods_per_year:
        raise ValueError("first_year_periods must be one to a whole year")
    m, q = period_factors(mu, sigma, periods_per_year=periods_per_year)
    plan: list[Step] = []
    for index, annual in enumerate(annual_flows):
        count = periods_per_year
        if index == 0 and first_year_periods is not None:
            count = int(first_year_periods)
        plan.extend([Step(float(annual) / count, m, q)] * count)
    return plan


def _year_boundaries(steps_total: int, periods_per_year: int,
                     first_year_periods: int | None) -> list[int]:
    """The step indices a fan reports: year 0, then every year boundary, then
    the end if a trailing partial year is left. With a partial first year the
    boundaries are offset by its length."""
    if first_year_periods is None:
        indices = list(range(0, steps_total + 1, periods_per_year))
    else:
        indices = [0] + list(range(int(first_year_periods), steps_total + 1,
                                   periods_per_year))
    if indices[-1] != steps_total:
        indices.append(steps_total)
    return indices


def moments(start: float, steps: Sequence[Step]) -> list[tuple[float, float]]:
    """The exact recursion: (E[W_t], E[W_t^2]) after every period.

        E[W_t]   = (E[W_{t-1}] + c_t) m_t
        E[W_t^2] = (E[W_{t-1}^2] + 2 c_t E[W_{t-1}] + c_t^2) q_t

    valid because R_t is independent of W_{t-1}. Two multiply-adds per period,
    exact, O(N), and it accepts different (m_t, q_t) every period.

    Index t holds the moments after t periods, so index 0 is the starting
    state and the result has ``len(steps) + 1`` entries. One pass to the far
    horizon therefore yields every intermediate horizon's fan for free.
    """
    w = float(start)
    w2 = w * w
    out = [(w, w2)]
    for step in steps:
        c = step.contribution
        w2 = (w2 + 2.0 * c * w + c * c) * step.second
        w = (w + c) * step.mean
        out.append((w, w2))
    return out


def lognormal_params(first: float, second: float) -> tuple[float, float]:
    """Fenton-Wilkinson: match LN(M, S^2) to exact moments (E[W], E[W^2]).

        S^2 = ln(E[W^2] / E[W]^2)      M = ln(E[W]) - S^2 / 2

    A sum of lognormals is not lognormal, so this is an approximation -- but a
    deterministic one, and lossless in the degenerate cases (no contributions,
    or no volatility) that the tests pin.
    """
    if first <= 0:
        return float("-inf"), 0.0
    ratio = second / (first * first)
    # Floating point can land a hair below 1 when S is genuinely 0.
    s2 = math.log(ratio) if ratio > 1.0 else 0.0
    return math.log(first) - s2 / 2.0, math.sqrt(s2)


def _to_cents(dollars: float) -> int:
    """Round model dollars back to integer cents, ROUND_HALF_UP."""
    if not math.isfinite(dollars):
        raise ValueError("projection overflowed to a non-finite value")
    return int((Decimal(dollars) * 100).quantize(Decimal(1),
                                                 rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class FanPoint:
    """The five bands at one horizon, in cents."""

    year: float
    p05: int
    p25: int
    p50: int
    p75: int
    p95: int


def _fan_point(year: float, first: float, second: float,
               deflator: float) -> FanPoint:
    M, S = lognormal_params(first, second)
    if M == float("-inf"):
        return FanPoint(year, 0, 0, 0, 0, 0)
    def band(z: float) -> int:
        return _to_cents(math.exp(M + S * z) / deflator)
    return FanPoint(year, band(Z_05), band(Z_25), band(Z_50),
                    band(Z_75), band(Z_95))


def _step_log_params(step: Step) -> tuple[float, float]:
    """One period's (a, b) = (mean, variance) of the log return, recovered from
    its moments: m = exp(a + b/2) and q = exp(2a + 2b)."""
    b = max(0.0, math.log(step.second) - 2.0 * math.log(step.mean))
    return math.log(step.mean) - b / 2.0, b


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF by bisection on ``erf``: exact to 1e-12,
    and only called a handful of times per fan point."""
    lo, hi = -10.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _floored(first: float, second: float, d: float) -> tuple[float, float, float]:
    """Moments of max(X - d, 0) for X lognormal with moments (first, second),
    and P(X <= d). ``d`` > 0 is the withdrawal."""
    M, S = lognormal_params(first, second)
    if M == float("-inf"):
        return 0.0, 0.0, 1.0
    if S == 0.0:                                # no spread: deterministic
        left = max(0.0, math.exp(M) - d)
        return left, left * left, (0.0 if left > 0 else 1.0)
    k = (M - math.log(d)) / S
    e1 = math.exp(M + S * S / 2.0)
    e2 = math.exp(2.0 * M + 2.0 * S * S)
    one = e1 * _norm_cdf(k + S) - d * _norm_cdf(k)
    two = (e2 * _norm_cdf(k + 2.0 * S) - 2.0 * d * e1 * _norm_cdf(k + S)
           + d * d * _norm_cdf(k))
    return max(0.0, one), max(0.0, two), _norm_cdf(-k)


#: The most funded parts a :class:`FundState` carries (module notes). Beyond
#: it the two lightest are merged by moment matching.
MAX_PARTS = 3


@dataclass(frozen=True)
class FundState:
    """The floored recursion's state: the probability the fund is empty, and
    the funded mass as parts ``(mass, E[W | part], E[W^2 | part])`` whose masses
    sum to ``1 - p0``. One part and ``p0 == 0`` is the plain recursion."""

    p0: float
    parts: tuple

    @property
    def first(self) -> float:
        """E[W] over the whole mixture."""
        return sum(mass * m1 for mass, m1, _m2 in self.parts)

    @property
    def second(self) -> float:
        """E[W^2] over the whole mixture."""
        return sum(mass * m2 for mass, _m1, m2 in self.parts)


def _merged(parts: Sequence[tuple[float, float, float]]) -> tuple:
    """Drop empty parts and cap the count, merging the two lightest."""
    kept = [p for p in parts if p[0] > 0.0]
    while len(kept) > MAX_PARTS:
        kept.sort(key=lambda p: p[0])
        (ma, a1, a2), (mb, b1, b2) = kept[0], kept[1]
        mass = ma + mb
        kept = [(mass, (ma * a1 + mb * b1) / mass, (ma * a2 + mb * b2) / mass)] + kept[2:]
    return tuple(kept)


def _advance(state: FundState, step: Step) -> FundState:
    """One period of the floored recursion.

    A contribution shifts every part by ``c`` and applies the return - exactly
    :func:`moments`' two lines per part - and lifts the empty mass off zero as
    a NEW part worth the contribution alone (module notes). A withdrawal is
    floored part by part, each part's emptied mass joining ``p0``.
    """
    c = step.contribution
    m, q = step.mean, step.second
    if c >= 0:
        parts = [(mass, (m1 + c) * m, (m2 + 2.0 * c * m1 + c * c) * q)
                 for mass, m1, m2 in state.parts]
        p0 = state.p0
        if c > 0 and p0 > 0.0:
            parts.append((p0, c * m, c * c * q))
            p0 = 0.0
        return FundState(p0, _merged(parts))
    d = -c
    p0 = state.p0
    parts = []
    for mass, m1, m2 in state.parts:
        one, two, empty = _floored(m1, m2, d)
        p0 += mass * empty
        left = 1.0 - empty
        if left > 1e-15 and one > 0.0:
            parts.append((mass * left, one / left * m, two / left * q))
    return FundState(min(1.0, p0), _merged(parts))


def _initial_state(start: float) -> FundState:
    start = float(start)
    return FundState(0.0, ((1.0, start, start * start),))


def floored_moments(start: float, steps: Sequence[Step]) -> list[FundState]:
    """The :class:`FundState` after every period, the fund floored at zero."""
    state = _initial_state(start)
    out = [state]
    for step in steps:
        state = _advance(state, step)
        out.append(state)
    return out


class TrackedFund:
    """A fund carried one YEAR at a time by the floored recursion.

    For a caller whose next year's flow depends on this year's answer - the
    Retirement Planner's household check asks "how much is there?" (the median),
    decides what to draw, and only then steps the year. Stepping it through a
    series of annual nets lands on exactly the medians :func:`fan_from_steps`
    draws for that series, so the check and the chart cannot disagree.

    ``first_year_periods`` makes the FIRST ``step_year`` partial, as
    :func:`steps_from_annual` does: only the months left are stepped, and the
    whole net handed to it is spread across them.
    """

    def __init__(self, start_cents: int, mu: float, sigma: float, *,
                 periods_per_year: int = 12,
                 first_year_periods: int | None = None):
        self._m, self._q = period_factors(mu, sigma,
                                          periods_per_year=periods_per_year)
        self._periods = int(periods_per_year)
        self._first = None if first_year_periods is None else int(first_year_periods)
        self.state = _initial_state(start_cents / 100.0)

    def median_cents(self) -> int:
        return _mixture_point(0.0, self.state, 1.0).p50

    def step_year(self, net_cents: int) -> None:
        count = self._periods if self._first is None else self._first
        self._first = None
        step = Step(float(net_cents) / 100.0 / count, self._m, self._q)
        for _ in range(count):
            self.state = _advance(self.state, step)


def _mixture_point(year: float, state: FundState, deflator: float) -> FanPoint:
    """Percentiles of "empty with probability p0, else one of the parts"."""
    fitted = []
    for mass, m1, m2 in state.parts:
        M, S = lognormal_params(m1, m2)
        if mass > 0.0 and M != float("-inf"):
            fitted.append((mass, M, S))
    funded = sum(mass for mass, _M, _S in fitted)
    p0 = max(0.0, 1.0 - funded)
    if funded <= 1e-12:
        return FanPoint(year, 0, 0, 0, 0, 0)
    if p0 <= 0.0 and len(fitted) == 1:
        return _fan_point(year, state.first, state.second, deflator)
    if len(fitted) == 1:
        _mass, M, S = fitted[0]

        def band(z: float) -> int:
            q = _norm_cdf(z)
            if q <= p0:
                return 0
            return _to_cents(math.exp(M + S * _norm_ppf((q - p0) / funded)) / deflator)
        return FanPoint(year, band(Z_05), band(Z_25), band(Z_50),
                        band(Z_75), band(Z_95))

    # Several parts: the mixture's CDF is the parts' curves summed, inverted
    # by bisection on log x. Monotone, so the bisection cannot miss.
    lo = min(M - 12.0 * S - 1e-9 for _m, M, S in fitted)
    hi = max(M + 12.0 * S + 1e-9 for _m, M, S in fitted)

    def cdf(x: float) -> float:
        total = p0
        for mass, M, S in fitted:
            if S <= 0.0:
                total += mass if x >= M else 0.0
            else:
                total += mass * _norm_cdf((x - M) / S)
        return total

    def band(z: float) -> int:
        q = _norm_cdf(z)
        if q <= p0:
            return 0
        a, b = lo, hi
        for _ in range(100):
            mid = (a + b) / 2.0
            if cdf(mid) < q:
                a = mid
            else:
                b = mid
        return _to_cents(math.exp((a + b) / 2.0) / deflator)
    return FanPoint(year, band(Z_05), band(Z_25), band(Z_50),
                    band(Z_75), band(Z_95))


def cut_after_ruin(points: Sequence[FanPoint], draws_cents: Sequence[int]
                   ) -> list[FanPoint]:
    """The plan's own reading of "the money is gone": from the first year whose
    MEDIAN is no more than that year's draw, every later year is zero.

    The recursion itself keeps a thin funded tail alive after such a year -
    the spread of outcomes that were luckier than the median - and the
    Retirement Planner's line used to creep back up on it while its household
    plan drew nothing (reported). The rule lives here so the planner's fund
    line and the Investment Dashboard's fan cut the same picture at the same
    year; ``draws_cents[i]`` is what year ``i`` takes out. The run-out year
    itself keeps its value: it is the year that draws what is left.
    """
    out = list(points)
    for index, draw in enumerate(draws_cents):
        if index >= len(out):
            break
        if int(draw) > 0 and 0 < out[index].p50 <= int(draw):
            return out[:index + 1] + [FanPoint(p.year, 0, 0, 0, 0, 0)
                                      for p in out[index + 1:]]
    return out


def fan_from_steps(start_cents: int, plan: Sequence[Step], *,
                   periods_per_year: int = 12,
                   inflation: float | None = None,
                   first_year_periods: int | None = None) -> list[FanPoint]:
    """The percentile fan for an arbitrary ``Step`` list, one point per year.

    This is the glide-path entry point (4.6): the caller supplies a sequence
    whose (m_t, q_t) vary, and gets the same fan by the same math.

    Year 0 is included, so a chart has a point to start the bands from. A
    trailing partial year is reported at its true fractional horizon rather
    than dropped, and so is a partial FIRST year (``first_year_periods``, the
    same value the plan was built with): the boundaries then fall at
    ``first_year_periods`` steps and every year after.
    """
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    infl = 0.0 if inflation is None else float(inflation)
    indices = _year_boundaries(len(plan), periods_per_year, first_year_periods)
    if any(step.contribution < 0 for step in plan):
        # Money comes out: carry the floor and the empty mass too (module notes).
        tracked = floored_moments(start_cents / 100.0, plan)
        return [_mixture_point(idx / periods_per_year, tracked[idx],
                               (1.0 + infl) ** (idx / periods_per_year))
                for idx in indices]
    series = moments(start_cents / 100.0, plan)
    points: list[FanPoint] = []
    for idx in indices:
        year = idx / periods_per_year
        first, second = series[idx]
        # Deflating quantiles by a deterministic factor is exact; it does not
        # disturb the moment match.
        points.append(_fan_point(year, first, second, (1.0 + infl) ** year))
    return points


def fan(start_cents: int, annual_contribution_cents: int, mu: float,
        sigma: float, years: int, *, periods_per_year: int = 12,
        inflation: float | None = None) -> list[FanPoint]:
    """Top-level: the whole percentile fan, one point per year.

    Money crosses this boundary as integer cents and comes back as integer
    cents; ``mu`` and ``sigma`` are annual arithmetic mean and volatility
    (0.07 == 7%), as produced by :func:`portfolio_moments`. Pass ``inflation``
    to get today's dollars instead of nominal ones.
    """
    plan = steps(annual_contribution_cents / 100.0, mu, sigma, years,
                 periods_per_year=periods_per_year)
    return fan_from_steps(start_cents, plan,
                          periods_per_year=periods_per_year,
                          inflation=inflation)
