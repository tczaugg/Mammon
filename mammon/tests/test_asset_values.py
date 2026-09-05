"""mammon.asset_values: market value for an asset account, separate from the
cost basis in its ledger (SRD 5.8e).

The contract these tests pin down:

* cost basis and market value are two numbers and never collapse into one;
* a value is read on-or-BEFORE a date, so a valuation taken today cannot rewrite
  what the house was worth in 2012;
* an address is stored and confirmed, never derived from an account name;
* a CLOSED account is never valued -- a sold property keeps its history;
* a source that fails, returns nothing, or returns junk writes NOTHING, and the
  previous value stands;
* net worth and allocation both read market value where there is one;
* a loan names the asset it is secured by, MANY loans to one property, and
  exposure reads the debt from those links rather than guessing at names.
"""
from __future__ import annotations

import pytest

from mammon import asset_values, db, ledger, portfolio


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "assets.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """The user's ANYTOWN position: one house owned outright, one with a
    small mortgage left, and one sold years ago."""
    cedar = ledger.create_account(conn, "120 Cedar Ln", "asset",
                                      opening_balance=185_000_00)
    birch = ledger.create_account(conn, "240 Birch", "asset",
                                      opening_balance=140_000_00)
    sold = ledger.create_account(conn, "ANYTOWN House", "asset",
                                 opening_balance=120_000_00)
    loan = ledger.create_account(conn, "240 Birch Loan", "liability",
                                 opening_balance=-50_000_00)
    ledger.update_account(conn, sold, closed_flag=1)
    asset_values.set_address(conn, cedar, "120 Cedar Ln, ANYTOWN ST")
    asset_values.set_address(conn, birch, "240 Birch, ANYTOWN ST")
    return {"cedar": cedar, "birch": birch, "sold": sold, "loan": loan}


# ---------------------------------------------------------------------------
# liens
# ---------------------------------------------------------------------------
def test_many_loans_point_at_one_asset_and_debt_sums_them(conn, world):
    """A first mortgage and its refinance both sit against one house, which is
    why the link lives on the loan."""
    house = world["birch"]
    second = ledger.create_account(conn, "240 Birch Loan2", "liability",
                                   opening_balance=-15_000_00)
    asset_values.set_lien(conn, world["loan"], house)
    asset_values.set_lien(conn, second, house)
    assert asset_values.lien_of(conn, world["loan"]) == house
    assert [a["name"] for a in asset_values.loans_against(conn, house)] == \
        ["240 Birch Loan", "240 Birch Loan2"]
    assert asset_values.debt_against(conn, house) == 65_000_00

    asset_values.set_lien(conn, second, None)
    assert asset_values.debt_against(conn, house) == 50_000_00
    assert asset_values.lien_of(conn, second) is None


def test_a_lien_is_validated_not_trusted(conn, world):
    """A loan secured by a chequing account is a typo, and it would corrupt
    every leverage figure downstream."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1_000_00)
    with pytest.raises(ValueError, match="secured by an asset"):
        asset_values.set_lien(conn, world["loan"], chk)
    with pytest.raises(ValueError, match="only liability"):
        asset_values.set_lien(conn, world["cedar"], world["birch"])
    with pytest.raises(KeyError):
        asset_values.set_lien(conn, 999, world["birch"])
    assert asset_values.lien_of(conn, world["loan"]) is None


def test_an_overpaid_loan_never_reads_as_negative_debt(conn, world):
    """A liability swung positive contributes nothing, rather than reporting
    leverage below 1.0 -- which would read as the house being worth more than
    it is."""
    house = world["birch"]
    asset_values.set_lien(conn, world["loan"], house)
    ledger.add_transaction(conn, world["loan"], "2026-01-05", 60_000_00,
                           payee="Overpayment")
    assert ledger.account_balance(conn, world["loan"]) == 10_000_00
    assert asset_values.debt_against(conn, house) == 0


class FakeSource:
    """Canned values by address; records what it was asked for."""
    source_name = "fake"

    def __init__(self, by_address, date="2026-09-03"):
        self.by_address = dict(by_address)
        self.date = date
        self.asked: list = []

    def get_values(self, requests):
        out = []
        for r in requests:
            self.asked.append(r.address)
            if r.address in self.by_address:
                out.append(asset_values.AssetValue(
                    r.account_id, self.date, self.by_address[r.address],
                    self.source_name, r.address))
        return out


# ---------------------------------------------------------------------------
# the series
# ---------------------------------------------------------------------------
def test_market_value_is_separate_from_cost_basis(conn, world):
    a = world["cedar"]
    assert ledger.account_balance(conn, a) == 185_000_00       # what it cost
    assert asset_values.market_value(conn, a) is None          # no opinion yet

    asset_values.set_value(conn, a, "2026-09-03", 360_400_00, source="zillow")
    assert asset_values.market_value(conn, a) == 360_400_00
    # The ledger is untouched: basis survives for the eventual capital gain.
    assert ledger.account_balance(conn, a) == 185_000_00


def test_value_reads_on_or_before_never_backwards(conn, world):
    a = world["cedar"]
    asset_values.set_value(conn, a, "2012-06-30", 150_000_00, source="manual")
    asset_values.set_value(conn, a, "2026-09-03", 360_400_00, source="zillow")
    assert asset_values.market_value(conn, a, "2012-12-31") == 150_000_00
    assert asset_values.market_value(conn, a) == 360_400_00
    # Before any valuation there is NO opinion -- today's number must not leak
    # backwards and rewrite a decade of net-worth history.
    assert asset_values.market_value(conn, a, "2010-01-01") is None
    assert [v.date for v in asset_values.value_history(conn, a)] == \
        ["2012-06-30", "2026-09-03"]
    # Re-recording a date replaces it rather than duplicating.
    asset_values.set_value(conn, a, "2026-09-03", 361_000_00, source="manual")
    assert len(asset_values.value_history(conn, a)) == 2
    assert asset_values.market_value(conn, a) == 361_000_00
    assert asset_values.delete_value(conn, a, "2026-09-03") is True
    assert asset_values.market_value(conn, a) == 150_000_00
    with pytest.raises(ValueError):
        asset_values.set_value(conn, a, "09/03/2026", 1_00)


def test_addresses_are_stored_and_only_suggested_from_names(conn, world):
    assert asset_values.get_address(conn, world["cedar"]) == \
        "120 Cedar Ln, ANYTOWN ST"
    # A name is a STARTING POINT at best, and often not even that.
    assert asset_values.suggest_address("120 Cedar Ln") == "120 Cedar Ln"
    assert asset_values.suggest_address("Condo (Asset)") == ""
    assert asset_values.suggest_address("House (Asset)") == ""
    assert asset_values.suggest_address("ANYTOWN House") == ""
    asset_values.set_address(conn, world["cedar"], "  ")
    assert asset_values.get_address(conn, world["cedar"]) is None


def test_parse_value_refuses_to_invent_a_number():
    assert asset_values.parse_value("871900") == 871_900_00
    assert asset_values.parse_value("$871,900") == 871_900_00
    assert asset_values.parse_value("360,400.00") == 360_400_00
    assert asset_values.parse_value(871900) == 871_900_00
    for junk in (None, "", "   ", "Not available", "--", True):
        assert asset_values.parse_value(junk) is None


# ---------------------------------------------------------------------------
# which accounts get valued
# ---------------------------------------------------------------------------
def test_a_sold_property_is_never_valued(conn, world):
    """The account outlives the sale, so walking every asset account would add
    a house the user no longer owns to net worth -- quarterly, silently."""
    names = [a["name"] for a in asset_values.valuable_accounts(conn)]
    assert names == ["120 Cedar Ln", "240 Birch"]
    assert "ANYTOWN House" not in [
        a["name"] for a in asset_values.valuable_accounts(conn, include_unaddressed=True)]

    src = FakeSource({"120 Cedar Ln, ANYTOWN ST": 360_400_00})
    report = asset_values.fetch_values(conn, [world["sold"]], source=src)
    assert src.asked == []                        # never even looked it up
    assert report.written == [] and not report.ok
    assert "closed" in report.missing[0][2]


def test_an_account_without_a_confirmed_address_is_reported_not_guessed(conn, world):
    condo = ledger.create_account(conn, "Condo (Asset)", "asset", opening_balance=90_000_00)
    src = FakeSource({"120 Cedar Ln, ANYTOWN ST": 360_400_00})
    report = asset_values.fetch_values(conn, source=src)
    assert src.asked == ["120 Cedar Ln, ANYTOWN ST", "240 Birch, ANYTOWN ST"]
    assert [(m[1], "address" in m[2]) for m in report.missing if m[0] == condo] == \
        [("Condo (Asset)", True)]
    assert asset_values.market_value(conn, condo) is None


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
def test_fetch_writes_values_and_reports_what_it_could_not_value(conn, world):
    src = FakeSource({"120 Cedar Ln, ANYTOWN ST": 360_400_00,
                      "240 Birch, ANYTOWN ST": 289_000_00})
    report = asset_values.fetch_values(conn, source=src)
    assert report.ok and len(report.written) == 2
    assert asset_values.market_value(conn, world["cedar"]) == 360_400_00
    assert asset_values.market_value(conn, world["birch"]) == 289_000_00
    assert asset_values.value_at(conn, world["cedar"]).source == "fake"


def test_a_failed_fetch_leaves_the_last_good_value_standing(conn, world):
    """A wrong valuation is worse than a stale one, so a source that returns
    nothing must write nothing."""
    a = world["cedar"]
    asset_values.set_value(conn, a, "2026-06-01", 355_000_00, source="manual")

    silent = FakeSource({})                      # answers for nothing
    report = asset_values.fetch_values(conn, [a], source=silent)
    assert report.written == [] and not report.ok
    assert "no usable value" in report.missing[0][2]
    assert asset_values.market_value(conn, a) == 355_000_00       # unchanged

    class Exploding:
        source_name = "boom"

        def get_values(self, requests):
            raise RuntimeError("Zillow changed its layout")

    with pytest.raises(RuntimeError):
        asset_values.fetch_values(conn, [a], source=Exploding())
    assert asset_values.market_value(conn, a) == 355_000_00       # still unchanged


def test_zillow_source_parses_the_script_output_and_skips_junk(conn, world):
    """The real source over a fake webSlinger client: one run per address, and
    anything unparseable yields no value rather than a zero."""
    from mammon.webslinger import FakeWebSlingerClient, RunResult

    good = RunResult(success=True, status="completed",
                     raw={"output_data": {"zestimate": "360400"}})
    client = FakeWebSlingerClient(results={"GetZEstimate": good})
    src = asset_values.ZillowValueSource(client)
    [v] = src.get_values([asset_values.ValueRequest(world["cedar"],
                                                    "120 Cedar Ln, ANYTOWN ST")])
    assert v.value_cents == 360_400_00 and v.source == "zillow"
    assert client.calls[0][1] == "GetZEstimate"
    assert client.calls[0][2] == {"houseAddress": "120 Cedar Ln, ANYTOWN ST"}

    # An undeclared output goal still binds when it is the only field...
    lone = RunResult(success=True, raw={"output_data": {"value": "$289,000"}})
    src2 = asset_values.ZillowValueSource(
        FakeWebSlingerClient(results={"GetZEstimate": lone}))
    assert src2.get_values(
        [asset_values.ValueRequest(world["birch"], "x")])[0].value_cents == 289_000_00
    # ...but a page that gave back nothing usable yields NO value.
    for payload in ({"output_data": {}}, {"output_data": {"zestimate": "n/a"}},
                    {"output_data": {"zestimate": "0"}}, {}):
        empty = RunResult(success=True, raw=payload)
        s = asset_values.ZillowValueSource(
            FakeWebSlingerClient(results={"GetZEstimate": empty}))
        assert s.get_values([asset_values.ValueRequest(world["cedar"], "x")]) == []


# ---------------------------------------------------------------------------
# what the rest of the app sees
# ---------------------------------------------------------------------------
def test_net_worth_and_allocation_use_market_value(conn, world):
    from mammon import investments

    a = world["cedar"]
    basis_worth = ledger.net_worth(conn)
    asset_values.set_value(conn, a, "2026-09-03", 360_400_00, source="zillow")
    assert investments.display_balance(conn, a) == 360_400_00
    assert ledger.net_worth(conn) == basis_worth + (360_400_00 - 185_000_00)
    # An unvalued asset still counts, at the only number the file has.
    assert investments.display_balance(conn, world["birch"]) == 140_000_00

    portfolio.set_account_asset_class(conn, a, "real_estate")
    alloc = portfolio.allocation(conn, as_of="2026-09-03", scope="everything")
    by_class = dict((s.key, s.value) for s in alloc.by_class)
    assert by_class["real_estate"] == 360_400_00
    # ...and the debt is still not part of an allocation.
    assert "240 Birch Loan" not in [s.label for s in alloc.by_account]


def test_exposure_reports_leverage_against_the_debt_it_is_given(conn, world):
    """Gross, debt, net and leverage -- the number a pie cannot show."""
    asset_values.set_value(conn, world["cedar"], "2026-09-03", 360_400_00)
    asset_values.set_value(conn, world["birch"], "2026-09-03", 289_000_00)

    owned = asset_values.exposure(conn, world["cedar"], "2026-09-03")
    assert (owned.gross, owned.debt, owned.net) == (360_400_00, 0, 360_400_00)
    assert owned.leverage == pytest.approx(1.0)
    assert owned.basis == 185_000_00                    # cost survives alongside

    # The debt comes from the LINK, with no caller having to supply it.
    asset_values.set_lien(conn, world["loan"], world["birch"])
    levered = asset_values.exposure(conn, world["birch"], "2026-09-03")
    assert (levered.gross, levered.debt, levered.net) == (289_000_00, 50_000_00, 239_000_00)
    assert levered.leverage == pytest.approx(289_000 / 239_000, rel=1e-6)
    # An explicit figure still overrides it.
    assert asset_values.exposure(conn, world["birch"], "2026-09-03",
                                 debt_cents=-10_000_00).debt == 10_000_00

    # An unvalued asset falls back to basis rather than vanishing.
    condo = ledger.create_account(conn, "Condo (Asset)", "asset", opening_balance=90_000_00)
    assert asset_values.exposure(conn, condo).gross == 90_000_00
    # Underwater: a leverage ratio there is meaningless, not merely large.
    assert asset_values.exposure(conn, condo, debt_cents=120_000_00).leverage is None


def test_exposure_report_ranks_and_excludes_the_sold_house(conn, world):
    asset_values.set_value(conn, world["cedar"], "2026-09-03", 360_400_00)
    asset_values.set_value(conn, world["birch"], "2026-09-03", 289_000_00)
    asset_values.set_value(conn, world["sold"], "2026-09-03", 412_700_00)
    asset_values.set_lien(conn, world["loan"], world["birch"])
    rows = asset_values.exposure_report(conn, "2026-09-03")
    assert [(e.name, e.gross, e.debt) for e in rows] == [
        ("120 Cedar Ln", 360_400_00, 0),
        ("240 Birch", 289_000_00, 50_000_00)]
    assert sum(e.net for e in rows) == 599_400_00
    # Only when explicitly asked does the sold house appear at all.
    assert len(asset_values.exposure_report(conn, "2026-09-03", include_closed=True)) == 3
