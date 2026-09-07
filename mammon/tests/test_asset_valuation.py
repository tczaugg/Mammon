"""mammon.tests.test_asset_valuation -- the zEstimate wiring, end to end.

``test_asset_values.py`` pins the domain layer in isolation; this file pins the
WIRING that the rest of the app depends on: a property asset fetched through the
INJECTED webSlinger runner (never the network), stored as a dated series in
signed integer cents, and reflected in net worth at its LATEST value.

The runner is a :class:`mammon.webslinger.FakeWebSlingerClient` handed canned
:class:`RunResult` payloads, so ``fetch_values`` drives the real
``default_value_source`` -> :class:`asset_values.ZillowValueSource` -> the
``GetZEstimate`` script end to end without ever opening Chrome. That full path
(fetch -> default source -> the recorded script's ``houseAddress``/``zestimate``
fields -> parse -> ``set_value`` -> net worth) is exactly the seam a network
test would exercise, and no single domain-layer test above covers it as one flow.

Synthetic data only: ANYTOWN addresses on invented streets, no PII.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from mammon import asset_values, db, investments, ledger
from mammon.webslinger import FakeWebSlingerClient, RunResult


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "valuation.db")
    yield c
    c.close()


def _house(conn, name="1400 Maple Ave", basis=200_000_00,
           address="1400 Maple Ave, ANYTOWN ST 00000"):
    """An open asset account carrying a confirmed address, the only kind a
    valuation source is ever asked to look up."""
    aid = ledger.create_account(conn, name, "asset", opening_balance=basis)
    asset_values.set_address(conn, aid, address)
    return aid


def _zestimate_runner(raw_value="425000", value_field="zestimate",
                      script="GetZEstimate"):
    """A fake webSlinger client that answers ``GetZEstimate`` with a canned
    Zestimate string -- the injected-runner pattern, so no browser, no network.
    A page returns whatever text it had; the source turns that into cents."""
    result = RunResult(success=True, status="completed",
                       raw={"output_data": {value_field: raw_value}})
    return FakeWebSlingerClient(results={script: result})


# ---------------------------------------------------------------------------
# recording a valuation through the injected runner
# ---------------------------------------------------------------------------
def test_fetch_records_a_valuation_through_the_injected_runner(conn):
    """fetch_values -> default_value_source -> ZillowValueSource -> injected
    runner -> parse -> a stored dated row, in signed integer cents."""
    aid = _house(conn)
    client = _zestimate_runner(raw_value="425000")

    report = asset_values.fetch_values(conn, [aid], client=client)

    # The recorded script was driven by name, with the stored address routed to
    # its declared ``houseAddress`` input -- proof the wiring, not a stub, ran.
    assert client.calls == [
        ("run", "GetZEstimate",
         {"houseAddress": "1400 Maple Ave, ANYTOWN ST 00000"})]
    assert report.ok and len(report.written) == 1

    stored = asset_values.value_at(conn, aid)
    assert stored is not None
    # "425000" dollars became integer cents, tagged with the source name.
    assert stored.value_cents == 425_000_00
    assert type(stored.value_cents) is int
    assert stored.source == "zillow"
    # Stored dated ISO YYYY-MM-DD (raises if the date is any other shape).
    assert _dt.date.fromisoformat(stored.date) is not None
    # The ledger (cost basis) is left untouched -- value and basis never merge.
    assert ledger.account_balance(conn, aid) == 200_000_00


# ---------------------------------------------------------------------------
# reading the series + selecting the latest value
# ---------------------------------------------------------------------------
def test_series_reads_oldest_first_and_latest_value_wins(conn):
    """A backfilled appraisal plus a fresh fetch form one ordered series; the
    newest is what market_value returns, and on-or-before still finds the older
    one rather than letting today's number leak backwards."""
    aid = _house(conn)
    # A hand-entered appraisal predating the app.
    asset_values.set_value(conn, aid, "2012-06-30", 150_000_00, source="manual")
    # Today's value, fetched through the injected runner.
    asset_values.fetch_values(conn, [aid], client=_zestimate_runner(raw_value="425000"))

    history = asset_values.value_history(conn, aid)
    assert len(history) == 2
    assert [v.date for v in history] == sorted(v.date for v in history)  # oldest first
    assert (history[0].date, history[0].value_cents) == ("2012-06-30", 150_000_00)
    assert history[-1].value_cents == 425_000_00 and history[-1].source == "zillow"

    # Latest value wins with no as_of.
    latest = asset_values.value_at(conn, aid)
    assert latest.value_cents == 425_000_00 and latest.source == "zillow"
    assert asset_values.market_value(conn, aid) == 425_000_00
    # On-or-before never leaks the fetched value backwards in time.
    assert asset_values.market_value(conn, aid, "2013-01-01") == 150_000_00
    assert asset_values.market_value(conn, aid, "2000-01-01") is None


# ---------------------------------------------------------------------------
# net worth reflects the latest valuation
# ---------------------------------------------------------------------------
def test_net_worth_uses_the_latest_valuation_not_cost_basis(conn):
    aid = _house(conn, basis=200_000_00)
    ledger.create_account(conn, "Checking", "checking", opening_balance=5_000_00)

    # Before any valuation, net worth is cost basis + cash.
    assert ledger.net_worth(conn) == 200_000_00 + 5_000_00

    # An older appraisal, then a newer fetched value: net worth must track the
    # LATEST, not the first recorded value and not the basis.
    asset_values.set_value(conn, aid, "2012-06-30", 150_000_00, source="manual")
    asset_values.fetch_values(conn, [aid], client=_zestimate_runner(raw_value="425000"))

    assert investments.display_balance(conn, aid) == 425_000_00
    assert ledger.net_worth(conn) == 425_000_00 + 5_000_00


def test_a_failed_fetch_leaves_net_worth_on_the_last_good_value(conn):
    """A runner that hands back an unparseable value writes NOTHING; net worth
    holds the prior value rather than dropping to zero or to cost basis."""
    aid = _house(conn, basis=200_000_00)
    asset_values.set_value(conn, aid, "2026-01-01", 400_000_00, source="manual")
    assert ledger.net_worth(conn) == 400_000_00

    junk = FakeWebSlingerClient(results={"GetZEstimate": RunResult(
        success=True, raw={"output_data": {"zestimate": "Not available"}})})
    report = asset_values.fetch_values(conn, [aid], client=junk)

    assert not report.ok and report.written == []
    assert "no usable value" in report.missing[0][2]
    assert asset_values.market_value(conn, aid) == 400_000_00
    assert ledger.net_worth(conn) == 400_000_00
