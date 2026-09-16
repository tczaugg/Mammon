"""mammon.security_mix: one security, several asset classes (SRD 5.8g).

The contract:

* a provider's positions map onto Mammon's classes, with the equity slice routed
  by the class the USER assigned -- never guessed into a hemisphere;
* weights are normalized to exactly 100 and a position is split into cents that
  sum to the position;
* a mixture WINS over the single class in `portfolio.allocation`, and by-security
  totals are unaffected;
* a fetch that returns nothing leaves any existing mixture alone;
* nothing here writes a transaction.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, investments, ledger, portfolio, rebalance, security_mix


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mix.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """A brokerage holding a target-date fund and an S&P index fund."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=200_000_00)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ledger.create_transfer(conn, chk, inv, "2026-01-02", 100_000_00, payee="Fund")
    investments.record_investment(conn, inv, "2026-01-03", "Buy", symbol="VTHRX",
                                  quantity="500", price="100", amount=-50_000_00)
    investments.record_investment(conn, inv, "2026-01-03", "Buy", symbol="FXAIX",
                                  quantity="400", price="100", amount=-40_000_00)
    for sym in ("VTHRX", "FXAIX"):
        investments.record_price(conn, sym, "2026-06-30", "100")
    investments.rebuild_holdings(conn, inv)
    portfolio.set_security(conn, "VTHRX", asset_class="domestic_stock")
    portfolio.set_security(conn, "FXAIX", asset_class="domestic_stock")
    return {"chk": chk, "inv": inv}


AS_OF = "2026-06-30"

# The real shapes yfinance returns (fractions), verified live 3 Sep 2026.
VTHRX_RAW = {"cashPosition": 0.0166, "stockPosition": 0.5843,
             "bondPosition": 0.398, "otherPosition": 0.0009}
VXUS_RAW = {"cashPosition": 0.0268, "stockPosition": 0.9714,
            "preferredPosition": 0.0001, "otherPosition": 0.0017}


class FakeSource:
    source_name = "fake"

    def __init__(self, by_symbol):
        self.by_symbol = dict(by_symbol)
        self.asked: list = []

    def get_mixtures(self, symbols):
        out = []
        for sym in symbols:
            self.asked.append(sym)
            entry = self.by_symbol.get(sym)
            if entry is None:
                continue
            positions, category = entry
            out.append(security_mix.SecurityMixture(
                symbol=sym, weights=positions, source=self.source_name,
                as_of="2026-06-30", category=category))
        return out


# ---------------------------------------------------------------------------
# mapping the provider's axis onto Mammon's
# ---------------------------------------------------------------------------
def test_positions_map_and_route_equity_by_the_users_own_class():
    mix = security_mix.map_positions(VTHRX_RAW, "domestic_stock")
    # The provider's own fractions sum to 0.9998, so 0.02 points are handed to
    # the largest class to make the mixture total exactly 100 (see normalize).
    assert mix == {"domestic_stock": Decimal("58.45"), "bond": Decimal("39.80"),
                   "cash": Decimal("1.66"), "other": Decimal("0.09")}
    assert sum(mix.values()) == Decimal("100")
    # The same fund held as an international sleeve routes its equity there.
    intl = security_mix.map_positions(VTHRX_RAW, "intl_stock")
    assert intl["intl_stock"] == Decimal("58.45") and "domestic_stock" not in intl
    # With no class assigned the equity is UNCLASSIFIED, visibly -- never guessed
    # into a hemisphere the data cannot name.
    unknown = security_mix.map_positions(VTHRX_RAW, None)
    assert unknown["unclassified"] == Decimal("58.45")


def test_percentages_and_fractions_are_both_accepted():
    """A silent 100x error in an allocation does not announce itself."""
    as_fractions = security_mix.map_positions(
        {"stockPosition": 0.6, "bondPosition": 0.4}, "domestic_stock")
    as_percents = security_mix.map_positions(
        {"stockPosition": 60, "bondPosition": 40}, "domestic_stock")
    assert as_fractions == as_percents == {"domestic_stock": Decimal("60"),
                                           "bond": Decimal("40")}


def test_hybrids_land_in_other_and_unknown_keys_are_dropped():
    mix = security_mix.map_positions(
        {"stockPosition": 0.90, "preferredPosition": 0.05,
         "convertiblePosition": 0.03, "somethingElse": 0.02}, "domestic_stock")
    # 90 stock + 5 preferred + 3 convertible; the unmodelled 2 is dropped and
    # the resulting 2-point shortfall lands on the largest class.
    assert mix == {"domestic_stock": Decimal("92"), "other": Decimal("8")}


def test_normalize_forces_exactly_one_hundred():
    """A provider's rounding gives 100.01; a mixture totalling 100.01 would
    allocate more than the position is worth."""
    out = security_mix.normalize({"bond": Decimal("98.36"), "cash": Decimal("1.64"),
                                  "other": Decimal("0.01")})
    assert sum(out.values()) == Decimal("100")
    assert out["bond"] == Decimal("98.35")            # remainder off the largest
    assert security_mix.normalize({}) == {}
    assert security_mix.normalize({"cash": Decimal("0")}) == {}


def test_stock_class_is_only_ever_suggested():
    assert security_mix.suggest_stock_class("Foreign Large Blend") == "intl_stock"
    assert security_mix.suggest_stock_class("Diversified Emerging Mkts") == "intl_stock"
    # A target-date fund holds both hemispheres; the data cannot say, so neither
    # does the suggestion.
    assert security_mix.suggest_stock_class("Target-Date 2030") is None
    assert security_mix.suggest_stock_class("Large Blend") is None
    assert security_mix.suggest_stock_class(None) is None


# ---------------------------------------------------------------------------
# splitting a position into cents
# ---------------------------------------------------------------------------
def test_split_value_is_exact_to_the_cent():
    """Naive rounding of each share loses or invents cents; the parts must sum
    to the position or the allocation quietly stops reconciling."""
    mix = {"domestic_stock": Decimal("58.43"), "bond": Decimal("39.80"),
           "cash": Decimal("1.66"), "other": Decimal("0.11")}
    for cents in (50_000_00, 1, 7, 99, 12_345_67, 999_999_99):
        parts = security_mix.split_value(cents, mix)
        assert sum(parts.values()) == cents, cents
    # A third each: 100 cents cannot divide evenly, and none may go missing.
    thirds = security_mix.split_value(
        100, {"cash": Decimal("33.33"), "bond": Decimal("33.33"),
              "other": Decimal("33.34")})
    assert sum(thirds.values()) == 100
    assert security_mix.split_value(1000, {}) == {}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def test_mixtures_round_trip_and_clear(conn, world):
    security_mix.set_mixture(conn, "VTHRX",
                             {"domestic_stock": 58.43, "bond": 39.8, "cash": 1.66,
                              "other": 0.11},
                             source="yfinance", as_of="2026-06-30")
    mix = security_mix.get_mixture(conn, "VTHRX")
    assert sum(mix.values()) == Decimal("100")
    assert security_mix.mixture_meta(conn, "VTHRX") == {"source": "yfinance",
                                                        "as_of": "2026-06-30"}
    assert list(security_mix.all_mixtures(conn)) == ["VTHRX"]
    assert "58.43% Domestic stock" in security_mix.describe(mix)

    # Re-setting replaces rather than accumulating.
    security_mix.set_mixture(conn, "VTHRX", {"bond": 100})
    assert security_mix.get_mixture(conn, "VTHRX") == {"bond": Decimal("100")}
    # An empty mapping clears it, back to the single class.
    security_mix.set_mixture(conn, "VTHRX", {})
    assert security_mix.get_mixture(conn, "VTHRX") == {}
    assert security_mix.mixture_meta(conn, "VTHRX") is None

    with pytest.raises(ValueError):
        security_mix.set_mixture(conn, "VTHRX", {"crypto": 100})
    with pytest.raises(ValueError):
        security_mix.set_mixture(conn, "  ", {"bond": 100})


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
def test_fetch_uses_the_assigned_class_and_flags_what_it_could_not_place(conn, world):
    src = FakeSource({"VTHRX": (VTHRX_RAW, "Target-Date 2030"),
                      "VXUS": (VXUS_RAW, "Foreign Large Blend")})
    # VXUS is held nowhere and has no assigned class -> equity unclassified.
    report = security_mix.fetch_mixtures(conn, ["VTHRX", "FXAIX", "VXUS"], source=src)
    assert [m.symbol for m in report.written] == ["VTHRX", "VXUS"]
    assert report.missing == [("FXAIX", "no fund composition published for this symbol")]
    # VTHRX was already domestic_stock, so its equity went there without asking.
    assert security_mix.get_mixture(conn, "VTHRX")["domestic_stock"] == Decimal("58.45")
    assert "VTHRX" not in [s for s, _ in report.needs_stock_class]
    # VXUS could not be placed, and the category only SUGGESTS where it goes.
    assert report.needs_stock_class == [("VXUS", "intl_stock")]
    assert "unclassified" in security_mix.get_mixture(conn, "VXUS")

    # Taking the suggestion re-routes it.
    security_mix.fetch_mixtures(conn, ["VXUS"], source=src,
                               stock_classes={"VXUS": "intl_stock"})
    mix = security_mix.get_mixture(conn, "VXUS")
    assert mix["intl_stock"] == Decimal("97.14") and "unclassified" not in mix


def test_a_fetch_that_finds_nothing_leaves_the_old_mixture_alone(conn, world):
    """A stale split is a far better answer than a wiped one."""
    security_mix.set_mixture(conn, "VTHRX", {"domestic_stock": 60, "bond": 40},
                             source="manual")
    report = security_mix.fetch_mixtures(conn, ["VTHRX"], source=FakeSource({}))
    assert report.written == [] and not report.ok
    assert security_mix.get_mixture(conn, "VTHRX") == {
        "domestic_stock": Decimal("60"), "bond": Decimal("40")}


# ---------------------------------------------------------------------------
# what the allocation and the drift see
# ---------------------------------------------------------------------------
def test_allocation_splits_a_fund_across_its_classes(conn, world):
    """The point of the whole feature: without the mixture the target-date fund
    counts as 50k of pure equity, which it is not."""
    before = portfolio.allocation(conn, as_of=AS_OF, scope="investments")
    assert dict((s.key, s.value) for s in before.by_class) == {
        "domestic_stock": 90_000_00, "cash": 10_000_00}

    security_mix.fetch_mixtures(
        conn, ["VTHRX"],
        source=FakeSource({"VTHRX": (VTHRX_RAW, "Target-Date 2030")}))
    after = portfolio.allocation(conn, as_of=AS_OF, scope="investments")
    by_class = dict((s.key, s.value) for s in after.by_class)
    # The 50k fund is now 58.45/39.80/1.66/0.09 of itself; FXAIX is untouched.
    assert by_class["domestic_stock"] == 40_000_00 + 29_225_00
    assert by_class["bond"] == 19_900_00
    assert by_class["cash"] == 10_000_00 + 830_00
    assert by_class["other"] == 45_00
    # Nothing was created or lost.
    assert sum(by_class.values()) == before.total == after.total
    # By SECURITY is unaffected -- a mixture splits what a holding is made of,
    # not how much of it there is.
    assert dict((s.key, s.value) for s in after.by_security) == \
        dict((s.key, s.value) for s in before.by_security)


def test_drift_is_wrong_without_the_mixture_and_right_with_it(conn, world):
    """A 60/40 target against a portfolio that IS roughly 60/40 once the fund is
    seen through -- but reads as 90/0 without it."""
    rebalance.create_target(conn, "Sixty forty",
                            lines={"domestic_stock": 60, "bond": 30, "cash": 10},
                            active=True)
    blind = rebalance.drift(conn, as_of=AS_OF)
    bond_blind = next(r for r in blind.rows if r.asset_class == "bond")
    assert bond_blind.current_pct == Decimal("0")          # 30 points underweight
    assert bond_blind.out_of_band and blind.needs_rebalance

    security_mix.fetch_mixtures(
        conn, ["VTHRX"],
        source=FakeSource({"VTHRX": (VTHRX_RAW, "Target-Date 2030")}))
    seeing = rebalance.drift(conn, as_of=AS_OF)
    bond_seeing = next(r for r in seeing.rows if r.asset_class == "bond")
    assert bond_seeing.current_cents == 19_900_00
    assert bond_seeing.current_pct == Decimal("19.9")
    # Still off target, but by 10 points rather than a phantom 30.
    assert bond_seeing.drift_pct == Decimal("-10.1")
    assert abs(bond_seeing.move_cents) < abs(bond_blind.move_cents)


def test_mixtures_never_write_a_transaction(conn, world):
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    inv_before = conn.execute("SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
    security_mix.fetch_mixtures(
        conn, ["VTHRX"],
        source=FakeSource({"VTHRX": (VTHRX_RAW, "Target-Date 2030")}))
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == inv_before


# ---------------------------------------------------------------------------
# the equity slice follows the security's CURRENT class (user-reported)
# ---------------------------------------------------------------------------
# "In Target & Drift when I expand Unclassified it shows [a fund] ... But the
# other funds in that account are listed under Domestic stock. They all are
# classified as mutual funds. What gives?" -- the fund was marked 'bond' when its
# composition was fetched, so its STOCK slice had no stock class to land in and
# froze as unclassified.
def test_the_equity_slice_follows_the_class_the_security_carries_now(conn):
    from mammon import portfolio, security_mix
    conn.execute("INSERT INTO securities(symbol, name) VALUES ('ANONSEL','ANON Select')")
    conn.commit()
    portfolio.set_security(conn, "ANONSEL", asset_class="bond")
    security_mix.set_mixture(conn, "ANONSEL", security_mix.map_positions(
        {"stockPosition": 97.72, "cashPosition": 2.28}, stock_class=None))
    stored = {r[0]: r[1] for r in conn.execute(
        "SELECT asset_class, pct FROM security_mixtures WHERE symbol='ANONSEL'")}
    assert "unclassified" in stored                     # as fetched, and stays so

    portfolio.set_security(conn, "ANONSEL", asset_class="domestic_stock")
    mix = security_mix.get_mixture(conn, "ANONSEL")
    assert mix == {"domestic_stock": Decimal("97.72"), "cash": Decimal("2.28")}
    assert security_mix.all_mixtures(conn)["ANONSEL"] == mix
    # The stored row is untouched, so un-setting the class puts it back.
    portfolio.set_security(conn, "ANONSEL", asset_class="")
    assert "unclassified" in security_mix.get_mixture(conn, "ANONSEL")


def test_a_non_stock_class_does_not_absorb_the_equity_slice(conn):
    """Only a STOCK class can take it: 'bond' was how the fund got here."""
    from mammon import portfolio, security_mix
    conn.execute("INSERT INTO securities(symbol, name) VALUES ('ANONSEL','ANON Select')")
    conn.commit()
    portfolio.set_security(conn, "ANONSEL", asset_class="bond")
    security_mix.set_mixture(conn, "ANONSEL", security_mix.map_positions(
        {"stockPosition": 90, "cashPosition": 10}, stock_class=None))
    assert security_mix.get_mixture(conn, "ANONSEL")["unclassified"] == Decimal("90")
