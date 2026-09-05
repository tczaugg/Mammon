"""mammon.rebalance: a target asset mix and the drift from it (SRD 5.8f).

The contract:

* percentages are Decimal text, never floats, and an impossible one is refused;
* drift is measured against the target's SLEEVE, with property beside it as
  context and never inside the mix being corrected;
* the 5/25 rule fires on whichever band is tighter, and only the absolute one
  applies to a class targeted at zero;
* a target that does not sum to 100 is reported, never silently normalized;
* nothing here writes a transaction.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import asset_values, db, investments, ledger, portfolio, rebalance


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "rebalance.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """A brokerage 70/20/10 stock/bond/cash by value, plus a house that must
    stay OUT of the mix being rebalanced."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=120_000_00)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ledger.create_transfer(conn, chk, inv, "2026-01-02", 100_000_00, payee="Fund")
    investments.record_investment(conn, inv, "2026-01-03", "Buy", symbol="VTI",
                                  quantity="700", price="100", amount=-70_000_00)
    investments.record_investment(conn, inv, "2026-01-03", "Buy", symbol="BND",
                                  quantity="200", price="100", amount=-20_000_00)
    investments.record_price(conn, "VTI", "2026-06-30", "100")
    investments.record_price(conn, "BND", "2026-06-30", "100")
    investments.rebuild_holdings(conn, inv)
    portfolio.set_security(conn, "VTI", asset_class="domestic_stock")
    portfolio.set_security(conn, "BND", asset_class="bond")

    house = ledger.create_account(conn, "House", "asset", opening_balance=300_000_00)
    portfolio.set_account_asset_class(conn, house, "real_estate")
    return {"chk": chk, "inv": inv, "house": house}


AS_OF = "2026-06-30"


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------
def test_targets_are_named_with_one_active(conn):
    a = rebalance.create_target(conn, "Sixty forty",
                                lines={"domestic_stock": 60, "bond": 40})
    b = rebalance.create_target(conn, "Aggressive",
                                lines={"domestic_stock": 90, "bond": 10}, active=True)
    assert rebalance.active_target(conn)["id"] == b
    rebalance.set_active(conn, a)
    assert rebalance.active_target(conn)["id"] == a
    assert [int(t["active"]) for t in rebalance.list_targets(conn)].count(1) == 1
    rebalance.set_active(conn, None)
    assert rebalance.active_target(conn) is None

    assert rebalance.target_lines(conn, a) == {"domestic_stock": Decimal("60"),
                                               "bond": Decimal("40")}
    assert rebalance.target_total(conn, a) == Decimal("100")
    # Lines are replaceable and a zero clears rather than storing a dead line.
    rebalance.set_line(conn, a, "cash", 5)
    assert rebalance.target_total(conn, a) == Decimal("105")
    rebalance.set_line(conn, a, "cash", 0)
    assert "cash" not in rebalance.target_lines(conn, a)
    assert rebalance.delete_target(conn, b) is True
    assert len(rebalance.list_targets(conn)) == 1


def test_impossible_targets_are_refused(conn):
    tid = rebalance.create_target(conn, "T", lines={"bond": 10})
    for bad in (-1, 101, "abc"):
        with pytest.raises(ValueError):
            rebalance.set_line(conn, tid, "bond", bad)
    with pytest.raises(ValueError):
        rebalance.set_line(conn, tid, "crypto", 10)          # not an asset class
    with pytest.raises(ValueError):
        rebalance.create_target(conn, "  ")                  # needs a name
    with pytest.raises(ValueError):
        rebalance.create_target(conn, "X", sleeve="everything")   # not rebalanceable
    with pytest.raises(ValueError):
        rebalance.update_target(conn, tid, nonsense=1)


# ---------------------------------------------------------------------------
# the 5/25 rule
# ---------------------------------------------------------------------------
def test_the_5_25_rule_uses_whichever_band_is_tighter():
    D = Decimal
    # A big sleeve: the ABSOLUTE band governs (5pp of a 60% target is only 8.3%
    # relative, so the relative band never fires first).
    assert rebalance.in_band(D("4"), D("60"), D("5"), D("25")) is True
    assert rebalance.in_band(D("5"), D("60"), D("5"), D("25")) is False
    # A small sleeve: the RELATIVE band governs. A 4% target that has grown to
    # 5% is only 1pp off -- but that is 25% of it, and 5pp would never fire.
    assert rebalance.in_band(D("0.9"), D("4"), D("5"), D("25")) is True
    assert rebalance.in_band(D("1"), D("4"), D("5"), D("25")) is False
    # Underweight fires the same as overweight.
    assert rebalance.in_band(D("-1"), D("4"), D("5"), D("25")) is False
    # Targeted at zero: relative is undefined, so only the absolute band applies.
    assert rebalance.in_band(D("4.9"), D("0"), D("5"), D("25")) is True
    assert rebalance.in_band(D("5"), D("0"), D("5"), D("25")) is False


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------
def test_drift_measures_the_sleeve_and_keeps_the_house_as_context(conn, world):
    """The house is 300k of 400k owned -- if it were inside the mix, every
    class would read wildly underweight and no trade could ever fix it."""
    tid = rebalance.create_target(
        conn, "Sixty forty", lines={"domestic_stock": 60, "bond": 30, "cash": 10},
        active=True)
    r = rebalance.drift(conn, as_of=AS_OF)

    assert r.target_name == "Sixty forty" and r.sleeve == "investments"
    assert r.sleeve_total == 100_000_00            # brokerage only: 70k + 20k + 10k cash
    assert r.target_is_complete
    by_class = {row.asset_class: row for row in r.rows}
    assert by_class["domestic_stock"].current_pct == Decimal("70")
    assert by_class["domestic_stock"].drift_pct == Decimal("10")        # overweight
    assert by_class["domestic_stock"].move_cents == -10_000_00          # sell 10k
    assert by_class["bond"].drift_pct == Decimal("-10")
    assert by_class["bond"].move_cents == 10_000_00                     # buy 10k
    assert by_class["cash"].drift_pct == Decimal("0")
    assert by_class["cash"].move_cents == 0

    # The house is beside the mix, not in it.
    assert r.fixed_total == 300_000_00
    assert r.fixed_rows == [("Real estate", 300_000_00)]
    assert "real_estate" not in by_class

    # Both legs are out of band (10pp >= 5pp), and the trade is one 10k swap.
    assert r.needs_rebalance
    assert {row.asset_class for row in r.out_of_band} == {"domestic_stock", "bond"}
    assert r.to_move_cents == 10_000_00
    assert "out of band" in rebalance.describe(r) and "$10,000.00" in rebalance.describe(r)


def test_a_mix_inside_the_bands_reports_nothing_to_do(conn, world):
    rebalance.create_target(conn, "As held",
                            lines={"domestic_stock": 70, "bond": 20, "cash": 10},
                            active=True)
    r = rebalance.drift(conn, as_of=AS_OF)
    assert not r.needs_rebalance and r.out_of_band == []
    assert r.to_move_cents == 0
    assert "On target" in rebalance.describe(r)


def test_relative_band_catches_a_small_sleeve_the_absolute_one_misses(conn, world):
    """A 4%-target sleeve that has grown to 10% is 6pp off -- but the point is
    that even at 5% it would fire, where a 5pp-only rule never would."""
    rebalance.create_target(
        conn, "Small sleeve",
        lines={"domestic_stock": 66, "bond": 30, "cash": 4}, active=True)
    r = rebalance.drift(conn, as_of=AS_OF)
    cash = next(row for row in r.rows if row.asset_class == "cash")
    assert cash.current_pct == Decimal("10") and cash.drift_pct == Decimal("6")
    assert cash.drift_rel_pct == Decimal("150")
    assert cash.out_of_band


def test_an_untargeted_class_is_reported_and_a_partial_target_is_flagged(conn, world):
    """A class held but not targeted reads as 100% overweight of nothing, and
    a target summing to 90 is SHOWN rather than normalized into a plausible lie."""
    rebalance.create_target(conn, "Forgot cash",
                            lines={"domestic_stock": 60, "bond": 30}, active=True)
    r = rebalance.drift(conn, as_of=AS_OF)
    assert r.target_total_pct == Decimal("90") and not r.target_is_complete
    cash = next(row for row in r.rows if row.asset_class == "cash")
    assert cash.target_pct == Decimal("0") and cash.target_cents == 0
    assert cash.drift_rel_pct is None            # undefined, not infinite
    assert cash.out_of_band and cash.move_cents == -10_000_00


def test_drift_needs_a_target_and_survives_an_empty_sleeve(conn, world):
    with pytest.raises(ValueError, match="no allocation target"):
        rebalance.drift(conn, as_of=AS_OF)

    empty = db.init_db(":memory:")
    ledger.create_account(empty, "Brokerage", "investment", opening_balance=0)
    rebalance.create_target(empty, "T", lines={"domestic_stock": 100}, active=True)
    r = rebalance.drift(empty, as_of=AS_OF)
    # Nothing held is not "wildly off target"; it is nothing to measure.
    assert r.sleeve_total == 0 and not r.needs_rebalance
    assert "nothing to measure" in rebalance.describe(r)
    empty.close()


def test_with_cash_sleeve_pulls_the_chequing_account_in(conn, world):
    tid = rebalance.create_target(conn, "All liquid", sleeve="with_cash",
                                  lines={"domestic_stock": 60, "bond": 20, "cash": 20},
                                  active=True)
    r = rebalance.drift(conn, as_of=AS_OF)
    assert r.sleeve_total == 120_000_00                     # + 20k chequing
    cash = next(row for row in r.rows if row.asset_class == "cash")
    assert cash.current_cents == 30_000_00                  # 10k brokerage + 20k bank
    assert cash.current_pct == Decimal("25")
    assert r.fixed_total == 300_000_00                      # the house, still context


# ---------------------------------------------------------------------------
# seeding a target from what is held
# ---------------------------------------------------------------------------
def test_target_from_current_totals_exactly_one_hundred(conn, world):
    tid = rebalance.target_from_current(conn, "As held today", as_of=AS_OF, active=True)
    lines = rebalance.target_lines(conn, tid)
    assert lines == {"domestic_stock": Decimal("70"), "bond": Decimal("20"),
                     "cash": Decimal("10")}
    assert rebalance.target_total(conn, tid) == Decimal("100")
    # Seeded from today, today is on target.
    assert not rebalance.drift(conn, as_of=AS_OF).needs_rebalance


def test_target_from_current_forces_the_rounding_remainder_to_land(conn):
    """Three thirds round to 33 each and would total 99 -- a target nobody can
    ever be on. The remainder goes to the largest class."""
    inv = ledger.create_account(conn, "B", "investment", opening_balance=0)
    chk = ledger.create_account(conn, "C", "checking", opening_balance=0)
    ledger.create_transfer(conn, chk, inv, "2026-01-02", 30_000_00, payee="Fund")
    for sym, cls in (("AAA", "domestic_stock"), ("BBB", "bond"), ("CCC", "intl_stock")):
        investments.record_investment(conn, inv, "2026-01-03", "Buy", symbol=sym,
                                      quantity="100", price="100", amount=-10_000_00)
        investments.record_price(conn, sym, "2026-06-30", "100")
        portfolio.set_security(conn, sym, asset_class=cls)
    investments.rebuild_holdings(conn, inv)
    tid = rebalance.target_from_current(conn, "Thirds", as_of=AS_OF)
    assert rebalance.target_total(conn, tid) == Decimal("100")

    with pytest.raises(ValueError, match="nothing in the sleeve"):
        rebalance.target_from_current(db.init_db(":memory:"), "X")


def test_rebalance_never_writes_a_transaction(conn, world):
    """It proposes arithmetic; executing it stays the user's job."""
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    inv_before = conn.execute("SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
    rebalance.create_target(conn, "T", lines={"domestic_stock": 50, "bond": 50},
                            active=True)
    r = rebalance.drift(conn, as_of=AS_OF)
    assert r.needs_rebalance
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == inv_before


def test_market_moves_show_up_as_drift(conn, world):
    """The whole point: prices move, the mix drifts, the bands catch it."""
    rebalance.create_target(conn, "As held",
                            lines={"domestic_stock": 70, "bond": 20, "cash": 10},
                            active=True)
    assert not rebalance.drift(conn, as_of=AS_OF).needs_rebalance
    # Equities run up 40%; nothing was bought or sold.
    investments.record_price(conn, "VTI", "2026-12-31", "140")
    investments.record_price(conn, "BND", "2026-12-31", "100")
    r = rebalance.drift(conn, as_of="2026-12-31")
    stock = next(row for row in r.rows if row.asset_class == "domestic_stock")
    assert stock.current_cents == 98_000_00
    assert r.sleeve_total == 128_000_00
    assert stock.out_of_band and stock.move_cents < 0        # sell the winner
    bond = next(row for row in r.rows if row.asset_class == "bond")
    assert bond.move_cents > 0                               # buy the laggard


# ---------------------------------------------------------------------------
# hidden accounts and the cash rule
# ---------------------------------------------------------------------------
def test_hidden_accounts_contribute_no_value_or_holdings(conn, world):
    """Hiding is how a user says "these records are incomplete -- leave them out
    of my totals". A hidden account that leaked in would not merely inflate the
    total; its balance values as pure CASH, inventing a slice from nothing and
    skewing every other class's percentage. It must count for zero in BOTH the
    sleeve and the property context."""
    rebalance.create_target(
        conn, "Sixty forty", lines={"domestic_stock": 60, "bond": 30, "cash": 10},
        active=True)
    baseline = rebalance.drift(conn, as_of=AS_OF)

    # Real value the report must ignore: a hidden brokerage full of cash, and a
    # hidden second property.
    ira = ledger.create_account(conn, "Old 401k", "investment",
                                opening_balance=250_000_00)
    cabin = ledger.create_account(conn, "Cabin", "asset", opening_balance=150_000_00)
    portfolio.set_account_asset_class(conn, cabin, "real_estate")
    ledger.set_account_hidden(conn, ira, True)
    ledger.set_account_hidden(conn, cabin, True)

    after = rebalance.drift(conn, as_of=AS_OF)
    # Nothing moved: same sleeve, same cash line, same property context.
    assert after.sleeve_total == baseline.sleeve_total == 100_000_00
    assert after.sleeve_accounts == baseline.sleeve_accounts == ["Brokerage"]
    assert after.fixed_total == baseline.fixed_total == 300_000_00
    assert after.fixed_rows == baseline.fixed_rows == [("Real estate", 300_000_00)]
    cash = next(row for row in after.rows if row.asset_class == "cash")
    assert cash.current_cents == 10_000_00        # brokerage cash only; the 250k is gone

    # Prove the guard is load-bearing: unhide the 401k and its cash floods the
    # sleeve, tripling the total and the Cash line.
    ledger.set_account_hidden(conn, ira, False)
    flooded = rebalance.drift(conn, as_of=AS_OF)
    assert flooded.sleeve_total == 350_000_00
    flooded_cash = next(row for row in flooded.rows if row.asset_class == "cash")
    assert flooded_cash.current_cents == 260_000_00


def test_cash_is_never_sold(conn, world):
    """Cash cannot be sold: it is spent by BUYING securities and raised by
    SELLING them. So an overweight cash line proposes Invest (deploy the
    surplus), never Sell; an underweight one proposes Raise (sell securities to
    top it up), never Buy. Securities still buy and sell as usual."""
    # Brokerage cash is 10% of the sleeve. Target it BELOW that => overweight.
    tid = rebalance.create_target(
        conn, "Deploy cash",
        lines={"domestic_stock": 65, "bond": 30, "cash": 5}, active=True)
    r = rebalance.drift(conn, as_of=AS_OF)
    rows = {row.asset_class: row for row in r.rows}
    assert rows["cash"].move_cents < 0            # 10% held vs 5% target: overweight
    assert rows["cash"].action == "invest"        # deploy the surplus...
    assert rows["cash"].action != "sell"          # ...never sell cash
    # A security class is unaffected: still a plain Sell when overweight.
    assert rows["domestic_stock"].move_cents < 0
    assert rows["domestic_stock"].action == "sell"

    # Now target cash ABOVE what is held => underweight: raise it by selling.
    rebalance.set_line(conn, tid, "domestic_stock", 50)
    rebalance.set_line(conn, tid, "cash", 20)
    r2 = rebalance.drift(conn, as_of=AS_OF)
    cash2 = next(row for row in r2.rows if row.asset_class == "cash")
    assert cash2.move_cents > 0                    # 10% held vs 20% target: underweight
    assert cash2.action == "raise"                 # sell securities to raise cash
    assert cash2.action != "buy"

    # On target => nothing to do.
    rebalance.set_line(conn, tid, "cash", 10)
    r3 = rebalance.drift(conn, as_of=AS_OF)
    cash3 = next(row for row in r3.rows if row.asset_class == "cash")
    assert cash3.move_cents == 0 and cash3.action == "hold"
