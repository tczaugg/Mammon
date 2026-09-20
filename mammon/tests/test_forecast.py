"""Tests for mammon.forecast -- the closed-form projection engine.

The point of these is that the engine is ANALYTIC: every expectation below is
an independently derived formula (a double sum, an annuity due, a single
lognormal), not a tolerance around a simulation. If someone ever replaces the
recursion with sampling, test 1 and test 3 stop passing at 1e-12 and start
passing "most of the time", which is the signal.
"""
from __future__ import annotations

import math

import pytest

from mammon import forecast, portfolio


# ---------------------------------------------------------------------------
# 1. The recursion against the O(N^2) double sum
# ---------------------------------------------------------------------------

def _double_sum_second_moment(start: float, c: float, m: float, q: float,
                              n: int) -> float:
    """E[W_N^2] the slow, obviously-correct way.

        E[W_N^2] = sum_k sum_l a_k a_l m^|s_k - s_l| q^(N - max(s_k,s_l) + 1)

    with a_0 = W_0 and a_k = c_k. Two overlapping products share every period
    from max(s_k, s_l) onward, where E[R^2] = q applies, and differ over
    |s_k - s_l| periods, where only E[R] = m applies. Because a contribution
    is made at the START of period k it is exposed to R_k as well, so its
    first exposed period s_k is k itself (and s_0 = 1 for the opening
    balance).
    """
    a = [start] + [c] * n
    s = [1] + list(range(1, n + 1))
    total = 0.0
    for k in range(n + 1):
        for l in range(n + 1):
            total += (a[k] * a[l] * m ** abs(s[k] - s[l])
                      * q ** (n - max(s[k], s[l]) + 1))
    return total


@pytest.mark.parametrize("n, mu, sigma, c, start", [
    (12, 0.07, 0.150, 100.0, 1000.0),
    (40, 0.04, 0.080, 0.0, 5000.0),
    (25, 0.09, 0.200, 250.0, 0.0),
    (7, 0.03, 0.010, 10.0, 100.0),
    (60, 0.07, 0.159, 500.0, 25000.0),
])
def test_moment_recursion_matches_double_sum(n, mu, sigma, c, start):
    m, q = forecast.period_factors(mu, sigma)
    series = forecast.moments(start, [forecast.Step(c, m, q)] * n)

    assert len(series) == n + 1
    assert series[0] == (start, start * start)

    # Every intermediate horizon, not just the last one: one pass has to give
    # the whole fan.
    for t in range(1, n + 1):
        first, second = series[t]
        expected_first = start * m ** t + c * sum(m ** (t - k + 1)
                                                  for k in range(1, t + 1))
        assert first == pytest.approx(expected_first, rel=1e-9)
        assert second == pytest.approx(
            _double_sum_second_moment(start, c, m, q, t), rel=1e-9)


# ---------------------------------------------------------------------------
# 2. Zero volatility: the fan collapses to the annuity due
# ---------------------------------------------------------------------------

def test_zero_volatility_collapses_to_the_annuity_due():
    start_cents = 1_000_000          # $10,000
    annual_cents = 600_000           # $6,000/yr
    years, per_year = 15, 12
    m, _q = forecast.period_factors(0.05, 0.0, periods_per_year=per_year)

    points = forecast.fan(start_cents, annual_cents, 0.05, 0.0, years,
                          periods_per_year=per_year)

    for year, point in enumerate(points):
        n = year * per_year
        c = (annual_cents / 100.0) / per_year
        # Annuity DUE: the contribution is made at the start of the period, so
        # it earns that period's return too -- hence the leading m.
        closed = (start_cents / 100.0) * m ** n
        if n:
            closed += c * m * (m ** n - 1.0) / (m - 1.0)
        want = round(closed * 100)
        assert point.p05 == point.p25 == point.p50 == point.p75 == point.p95
        assert point.p50 == pytest.approx(want, abs=1)


# ---------------------------------------------------------------------------
# 3. Zero contribution: a single lognormal is matched by itself
# ---------------------------------------------------------------------------

def test_zero_contribution_is_lossless():
    start, mu, sigma = 12_345.67, 0.07, 0.15
    years, per_year = 30, 12
    n = years * per_year
    m, _q = forecast.period_factors(mu, sigma, periods_per_year=per_year)
    _mu_l, sigma_l2 = forecast.log_params(mu, sigma)

    series = forecast.moments(
        start, forecast.steps(0.0, mu, sigma, years, periods_per_year=per_year))
    first, second = series[-1]

    assert first == pytest.approx(start * m ** n, rel=1e-12)

    # With no contributions W_N IS lognormal, so Fenton-Wilkinson is exact:
    # S^2 = N sigma_L^2 dt, with no approximation error at all.
    _M, S = forecast.lognormal_params(first, second)
    assert S * S == pytest.approx(n * sigma_l2 / per_year, rel=1e-11)

    # ...and the median is then exp(M) = W_0 exp(N mu_L dt).
    mu_l, _ = forecast.log_params(mu, sigma)
    point = forecast.fan(int(round(start * 100)), 0, mu, sigma, years)[-1]
    assert point.p50 == pytest.approx(
        round(start * math.exp(n * mu_l / per_year) * 100), rel=1e-9)


# ---------------------------------------------------------------------------
# 4. Shape of the fan
# ---------------------------------------------------------------------------

def test_fan_is_ordered_widening_and_right_skewed():
    start_cents, annual_cents = 5_000_000, 2_400_000
    mu, sigma, years = 0.0614, 0.1138, 30
    points = forecast.fan(start_cents, annual_cents, mu, sigma, years)
    series = forecast.moments(
        start_cents / 100.0, forecast.steps(annual_cents / 100.0, mu, sigma, years))

    assert [p.year for p in points] == [float(y) for y in range(years + 1)]
    assert points[0].p50 == start_cents

    previous_width = -1.0
    for year, point in enumerate(points):
        assert point.p05 <= point.p25 <= point.p50 <= point.p75 <= point.p95

        # The fan widens with the horizon, in relative terms (the absolute
        # spread of a growing balance would widen for boring reasons).
        width = point.p95 / point.p05
        assert width >= previous_width
        if year:
            assert width > previous_width
        previous_width = width

        # Right skew: for any positive sigma the MEAN of a lognormal sits
        # above its median, and below the 75th percentile for any horizon
        # where S < 2 * z_75.
        mean_cents = series[year * 12][0] * 100.0
        assert point.p50 <= mean_cents <= point.p75

    # Deflating by a deterministic factor is exact, so the real fan is the
    # nominal one divided by (1+pi)^years.
    real = forecast.fan(start_cents, annual_cents, mu, sigma, years,
                        inflation=forecast.DEFAULT_INFLATION)
    deflator = (1.0 + forecast.DEFAULT_INFLATION) ** years
    # (within one cent: each fan rounds its own bands to cents independently)
    assert real[-1].p50 == pytest.approx(points[-1].p50 / deflator, abs=1)
    assert real[0].p50 == points[0].p50


# ---------------------------------------------------------------------------
# 5. portfolio_moments against hand arithmetic
# ---------------------------------------------------------------------------

def test_portfolio_moments_two_and_three_classes():
    assert forecast.ASSET_CLASSES is portfolio.ASSET_CLASSES

    # 50% domestic stock (7.0%, 15.1%) + 50% bonds (4.5%, 6.2%), rho = 0.10.
    #   mu  = .5(.070) + .5(.045) = .0575
    #   var = .25(.151^2) + .25(.062^2) + 2(.5)(.5)(.10)(.151)(.062)
    #       = .00570025 + .000961 + .0004681 = .00712935
    mu, sigma = forecast.portfolio_moments(
        {"domestic_stock": 0.5, "bond": 0.5})
    assert mu == pytest.approx(0.0575, abs=1e-12)
    assert sigma == pytest.approx(math.sqrt(0.00712935), rel=1e-12)
    assert sigma == pytest.approx(0.08443548, abs=5e-9)

    # 60% domestic + 30% international (7.0%, 18.8%) + 10% cash (3.3%, 1.1%),
    # rho(dom,intl) = .85 and cash correlates with neither.
    #   mu  = .6(.070) + .3(.070) + .1(.033) = .0663
    #   var = .36(.151^2) + .09(.188^2) + .01(.011^2)
    #         + 2(.6)(.3)(.85)(.151)(.188)
    #       = .00820836 + .00318096 + .00000121 + .008686728 = .020077258
    mu3, sigma3 = forecast.portfolio_moments(
        {"domestic_stock": 0.6, "intl_stock": 0.3, "cash": 0.1})
    assert mu3 == pytest.approx(0.0663, abs=1e-12)
    assert sigma3 == pytest.approx(math.sqrt(0.020077258), rel=1e-12)
    assert sigma3 == pytest.approx(0.14169424, abs=5e-9)

    # Weights are normalized, so the same mix stated in percent agrees.
    assert forecast.portfolio_moments(
        {"domestic_stock": 60, "intl_stock": 30, "cash": 10}
    ) == pytest.approx((mu3, sigma3))

    # Unclassified money is folded into "other" rather than dropped: it must
    # not make the portfolio look risk-free.
    folded = forecast.normalize_weights({"cash": 0.5, "unclassified": 0.5})
    assert folded["other"] == pytest.approx(0.5)
    assert folded["cash"] == pytest.approx(0.5)
    assert forecast.portfolio_moments({"cash": 0.5, "unclassified": 0.5})[1] > 0.0


# ---------------------------------------------------------------------------
# 6. The risk ladder, pinned
# ---------------------------------------------------------------------------

#: level -> (mean %, volatility %), the published thermometer table.
_LADDER_TABLE = [
    (0, 3.30, 1.10), (1, 3.78, 2.01), (2, 4.23, 3.55), (3, 4.66, 5.14),
    (4, 5.07, 6.73), (5, 5.45, 8.30), (6, 5.81, 9.84), (7, 6.14, 11.38),
    (8, 6.45, 12.90), (9, 6.74, 14.43), (10, 7.00, 15.95),
]


def test_risk_ladder_is_pinned_and_invertible():
    assert len(forecast.RISK_LADDER) == 11

    means, sigmas = [], []
    for (level, weights), (want_level, want_mu, want_sigma) in zip(
            forecast.RISK_LADDER, _LADDER_TABLE):
        assert level == float(want_level)
        assert sum(weights.values()) == pytest.approx(1.0)
        assert all(w >= 0.0 for w in weights.values())
        mu, sigma = forecast.portfolio_moments(weights)
        assert round(mu * 100, 2) == want_mu
        assert round(sigma * 100, 2) == want_sigma
        means.append(mu)
        sigmas.append(sigma)

    # Strictly increasing in both columns is what makes the thermometer a
    # meaningful single axis.
    assert means == sorted(means) and len(set(means)) == 11
    assert sigmas == sorted(sigmas) and len(set(sigmas)) == 11

    # Endpoints: all cash at the bottom, all stocks at the top.
    assert forecast.RISK_LADDER[0][1]["cash"] == pytest.approx(1.0)
    top = forecast.RISK_LADDER[-1][1]
    assert top["domestic_stock"] == pytest.approx(0.6)
    assert top["intl_stock"] == pytest.approx(0.4)
    assert top["cash"] == pytest.approx(0.0)
    assert top["bond"] == pytest.approx(0.0)

    # The slider is continuous and the round trip closes. It is exact away
    # from the bottom rung; within the first tenth of a level the inverse is
    # ambiguous, because a sliver of near-uncorrelated bond and stock
    # genuinely lowers the sigma of an all-cash portfolio.
    for i in range(0, 101):
        x = i / 10.0
        back = forecast.risk_for_mix(forecast.mix_for_risk(x))
        assert back == pytest.approx(x, abs=1e-6 if x >= 0.1 else 0.1)

    # Clamping outside the ladder's sigma range, not extrapolation.
    assert forecast.risk_for_mix({"cash": 1.0}) == 0.0
    assert forecast.risk_for_mix({"real_estate": 1.0}) == 10.0
    assert forecast.mix_for_risk(-3.0) == forecast.mix_for_risk(0.0)
    assert forecast.mix_for_risk(99.0) == forecast.mix_for_risk(10.0)


# ---------------------------------------------------------------------------
# 7. A glide path is just a different sequence through the same loop
# ---------------------------------------------------------------------------

def test_glide_path_narrows_the_fan():
    start_cents, annual_cents = 10_000_000, 1_800_000
    years, per_year = 40, 12
    n = years * per_year
    contribution = (annual_cents / 100.0) / per_year

    # Equity share falls from the top of the ladder to the bottom over the
    # horizon; nothing about the recursion changes, only (m_t, q_t).
    glide = []
    for t in range(n):
        level = 10.0 * (1.0 - t / n)
        mu_t, sigma_t = forecast.portfolio_moments(forecast.mix_for_risk(level))
        m, q = forecast.period_factors(mu_t, sigma_t, periods_per_year=per_year)
        glide.append(forecast.Step(contribution, m, q))

    assert glide[0].mean > glide[-1].mean      # early periods are the risky ones
    assert glide[0].second > glide[-1].second

    top_mu, top_sigma = forecast.portfolio_moments(forecast.mix_for_risk(10.0))
    fixed = forecast.fan(start_cents, annual_cents, top_mu, top_sigma, years,
                         periods_per_year=per_year)
    glided = forecast.fan_from_steps(start_cents, glide,
                                     periods_per_year=per_year)

    assert len(glided) == len(fixed) == years + 1
    assert glided[-1].p95 / glided[-1].p05 < fixed[-1].p95 / fixed[-1].p05
    assert glided[-1].p50 < fixed[-1].p50       # and it gives up return for it
    assert glided[-1].p05 > fixed[-1].p05       # while lifting the bad case
    for point in glided:
        assert point.p05 <= point.p25 <= point.p50 <= point.p75 <= point.p95


# ---------------------------------------------------------------------------
# 8. Crypto is its own class: far wilder than equity, and it must not fold
# ---------------------------------------------------------------------------

def test_crypto_is_its_own_class_and_is_wilder_than_equity():
    crypto = forecast.DEFAULT_ASSUMPTIONS[forecast.CRYPTO_CLASS]
    assert forecast.CRYPTO_CLASS == "crypto"

    # The whole reason the class exists: ETH-scale realized volatility is
    # multiples of equity's, so forecasting a coin as a stock understates it.
    equity = forecast.DEFAULT_ASSUMPTIONS["domestic_stock"]
    intl = forecast.DEFAULT_ASSUMPTIONS["intl_stock"]
    assert crypto.volatility > equity.volatility
    assert crypto.volatility > intl.volatility
    assert crypto.volatility > max(
        a.volatility for cls, a in forecast.DEFAULT_ASSUMPTIONS.items()
        if cls != forecast.CRYPTO_CLASS)
    assert crypto.volatility == pytest.approx(0.750)

    # And it is not cash either -- the other way to get this wrong.
    assert crypto.volatility > 50 * forecast.DEFAULT_ASSUMPTIONS["cash"].volatility

    # Conservative mu: above equity because it is riskier, nowhere near the
    # realized 15%/yr CAGR the comment refuses to extrapolate.
    assert equity.mean_return < crypto.mean_return < 0.15
    assert crypto.mean_return == pytest.approx(0.100)
    assert crypto.source                       # sourced, like every other row

    # A crypto weight is carried, NOT folded into "other" the way an unknown
    # key is. Folding would price it at "other"'s 12%.
    w = forecast.normalize_weights({"cash": 0.5, "crypto": 0.5})
    assert w["crypto"] == pytest.approx(0.5)
    assert w["other"] == pytest.approx(0.0)
    assert forecast.CRYPTO_CLASS in forecast.PROJECTION_CLASSES
    assert forecast.CRYPTO_CLASS not in portfolio.ASSET_CLASSES  # portfolio's tuple is untouched

    # sigma of half cash / half crypto: the two are uncorrelated, so
    #   var = .25(.75^2) + .25(.011^2) = .140625 + .000030250 = .14065525
    _mu, sigma = forecast.portfolio_moments({"cash": 0.5, "crypto": 0.5})
    assert sigma == pytest.approx(math.sqrt(0.14065525), rel=1e-12)

    # It is off the top of the thermometer: the ladder tops out at all-stock.
    assert forecast.risk_for_mix({"crypto": 1.0}) == 10.0
    for _level, weights in forecast.RISK_LADDER:
        assert weights.get(forecast.CRYPTO_CLASS, 0.0) == 0.0


def test_a_slice_of_crypto_widens_the_terminal_wealth_band():
    start_cents, annual_cents, years = 100_000_00, 12_000_00, 20

    without = {"domestic_stock": 0.60, "bond": 0.30, "cash": 0.10}
    # The same portfolio with a tenth of it moved into crypto, pro rata.
    with_crypto = {cls: w * 0.90 for cls, w in without.items()}
    with_crypto["crypto"] = 0.10
    assert sum(with_crypto.values()) == pytest.approx(1.0)

    mu_a, sigma_a = forecast.portfolio_moments(without)
    mu_b, sigma_b = forecast.portfolio_moments(with_crypto)
    assert sigma_b > sigma_a

    plain = forecast.fan(start_cents, annual_cents, mu_a, sigma_a, years)
    crypto = forecast.fan(start_cents, annual_cents, mu_b, sigma_b, years)

    end_plain, end_crypto = plain[-1], crypto[-1]
    # Wider both in dollars and as a ratio -- the ratio is the one that cannot
    # be explained away by the mix simply being richer.
    assert end_crypto.p95 - end_crypto.p05 > end_plain.p95 - end_plain.p05
    assert end_crypto.p95 / end_crypto.p05 > end_plain.p95 / end_plain.p05
    # The bad case is genuinely worse, which is the point of showing it.
    assert end_crypto.p05 < end_plain.p05
    for point in crypto:
        assert point.p05 <= point.p25 <= point.p50 <= point.p75 <= point.p95

    # Had crypto folded into "other" instead, a tenth of the portfolio would
    # read as 12% volatility and the whole mix would look a THIRD calmer.
    folded = forecast.portfolio_moments(
        {cls: w for cls, w in with_crypto.items() if cls != "crypto"}
        | {"other": 0.10})
    assert sigma_b > folded[1] * 1.4


def test_adding_crypto_left_every_existing_assumption_alone():
    """Real projections are already anchored on these; only crypto is new."""
    pinned = {
        "domestic_stock": (0.070, 0.151),
        "intl_stock": (0.070, 0.188),
        "bond": (0.045, 0.062),
        "cash": (0.033, 0.011),
        "real_estate": (0.060, 0.180),
        "other": (0.045, 0.120),
    }
    for cls, (mu, sigma) in pinned.items():
        row = forecast.DEFAULT_ASSUMPTIONS[cls]
        assert (row.mean_return, row.volatility) == pytest.approx((mu, sigma)), cls
    assert set(forecast.DEFAULT_ASSUMPTIONS) == set(pinned) | {"crypto"}
    assert set(portfolio.ASSET_CLASSES) == set(pinned)

    for (a, b), rho in {
        ("domestic_stock", "intl_stock"): 0.85,
        ("domestic_stock", "bond"): 0.10,
        ("intl_stock", "bond"): 0.10,
        ("domestic_stock", "real_estate"): 0.65,
        ("bond", "cash"): 0.20,
        ("other", "domestic_stock"): 0.50,
        ("other", "intl_stock"): 0.50,
        ("other", "real_estate"): 0.50,
    }.items():
        assert forecast.correlation(a, b) == pytest.approx(rho), (a, b)

    # Crypto against bonds and cash is left at the table's 0.0 default.
    assert forecast.correlation("crypto", "bond") == 0.0
    assert forecast.correlation("crypto", "cash") == 0.0

    # And the published thermometer is bit-for-bit what section 6 pins.
    for (level, weights), (_want_level, want_mu, want_sigma) in zip(
            forecast.RISK_LADDER, _LADDER_TABLE):
        mu, sigma = forecast.portfolio_moments(weights)
        assert (round(mu * 100, 2), round(sigma * 100, 2)) == (want_mu, want_sigma), level


def test_forecast_module_is_pure_domain_and_has_no_simulator():
    """No Qt, no SQL, no connection argument, and above all no sampling."""
    import inspect

    source = inspect.getsource(forecast).lower()
    for banned in ("random", "monte", "sqlite3", "pyqt5", "numpy"):
        assert banned not in source, banned

    for name, obj in vars(forecast).items():
        if not callable(obj) or getattr(obj, "__module__", None) != forecast.__name__:
            continue
        if not inspect.isfunction(obj):
            continue
        params = inspect.signature(obj).parameters
        assert "conn" not in params, name
