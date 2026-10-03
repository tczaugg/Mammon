"""Year-end balance snapshots (the performance twin of a from-inception replay).

These tests pin the core contract of the snapshot design:

* A read served from ``balance_checkpoints`` / ``holdings_checkpoints`` (previous
  year's snapshot + current-year delta) is IDENTICAL to a full from-inception
  replay -- for cash balances AND for investment positions.
* A back-dated edit recomputes that year's snapshot and cascades to every later
  year, so post-edit reads still match the full replay.

The from-inception oracles are ``ledger._account_balance_full`` and
``investments._replay_positions(..., use_snapshots=False)``.
"""
from __future__ import annotations

import pytest

from mammon import db, investments, ledger
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "snap.db")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
_AS_OF = [
    "2019-06-30", "2020-01-01", "2020-12-31", "2021-07-15", "2021-12-31",
    "2022-03-03", "2022-12-31", "2023-12-31", "2024-06-01", "2024-12-31",
    "2099-12-31",
]


def _assert_cash_matches(conn, acct):
    """account_balance (checkpoint path) == _account_balance_full (oracle)."""
    assert ledger.account_balance(conn, acct) == ledger._account_balance_full(conn, acct)
    for d in _AS_OF:
        assert ledger.account_balance(conn, acct, d) == \
            ledger._account_balance_full(conn, acct, d), f"cash mismatch at {d}"


def _assert_positions_match(conn, acct):
    """Snapshot+delta replay == from-inception replay, at every year boundary
    and open-ended."""
    assert investments._replay_positions(conn, acct, use_snapshots=True) == \
        investments._replay_positions(conn, acct, use_snapshots=False)
    for d in _AS_OF:
        snap = investments._replay_positions(conn, acct, d, use_snapshots=True)
        full = investments._replay_positions(conn, acct, d, use_snapshots=False)
        assert snap == full, f"positions mismatch at {d}"


# ---------------------------------------------------------------------------
# cash: snapshot vs full replay
# ---------------------------------------------------------------------------
def test_cash_snapshot_equals_full_replay(conn):
    acct = ledger.create_account(conn, "Checking", "bank", opening_balance=100_00)
    ledger.add_transaction(conn, acct, "2020-02-01", 250_00)
    ledger.add_transaction(conn, acct, "2020-11-15", -40_00)
    ledger.add_transaction(conn, acct, "2021-05-05", 500_00)
    ledger.add_transaction(conn, acct, "2022-08-08", -125_00)
    ledger.add_transaction(conn, acct, "2024-01-20", 999_00)
    ledger.rebuild_checkpoints(conn, acct)

    # A checkpoint cache now exists and the read path must serve from it.
    assert ledger._has_checkpoints(conn, acct)
    _assert_cash_matches(conn, acct)


def test_cash_read_matches_without_any_checkpoints(conn):
    # With no checkpoint rows the reader degrades to the same full sum.
    acct = ledger.create_account(conn, "Savings", "bank", opening_balance=0)
    conn.execute("INSERT INTO transactions(account_id, date, amount) VALUES (?,?,?)",
                 (acct, "2021-03-03", 700_00))
    conn.commit()
    assert not ledger._has_checkpoints(conn, acct)
    _assert_cash_matches(conn, acct)


# ---------------------------------------------------------------------------
# cash: back-dated edit cascades
# ---------------------------------------------------------------------------
def test_cash_backdated_edit_cascades(conn):
    acct = ledger.create_account(conn, "Checking", "bank", opening_balance=100_00)
    ledger.add_transaction(conn, acct, "2020-02-01", 250_00)
    ledger.add_transaction(conn, acct, "2021-05-05", 500_00)
    ledger.add_transaction(conn, acct, "2023-09-09", 300_00)
    ledger.rebuild_checkpoints(conn, acct)

    cp_2024_before = ledger.balance_via_checkpoint(conn, acct, "2024-12-31")

    # Back-date a NEW transaction into 2021; add_transaction must cascade the
    # checkpoint recompute to 2021..latest because a cache already exists.
    ledger.add_transaction(conn, acct, "2021-01-15", 77_00)

    # Later-year snapshots shifted by the inserted amount ...
    assert ledger.balance_via_checkpoint(conn, acct, "2024-12-31") == cp_2024_before + 77_00
    # ... and every read still equals the from-inception oracle.
    _assert_cash_matches(conn, acct)


def test_cash_backdated_update_and_delete_cascade(conn):
    acct = ledger.create_account(conn, "Checking", "bank", opening_balance=0)
    ledger.add_transaction(conn, acct, "2020-02-01", 250_00)
    tid = ledger.add_transaction(conn, acct, "2021-05-05", 500_00)
    ledger.add_transaction(conn, acct, "2023-01-01", 100_00)
    ledger.rebuild_checkpoints(conn, acct)

    # Edit the amount of the 2021 row -> cascade forward.
    ledger.update_transaction(conn, tid, amount=600_00)
    _assert_cash_matches(conn, acct)

    # Back-date the same row into 2019 -> cascade from the earlier year.
    ledger.update_transaction(conn, tid, date="2019-06-06")
    _assert_cash_matches(conn, acct)

    # Delete it -> cascade again.
    ledger.delete_transaction(conn, tid)
    _assert_cash_matches(conn, acct)


# ---------------------------------------------------------------------------
# investments: snapshot vs full replay
# ---------------------------------------------------------------------------
def _seed_investments(conn):
    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2020-01-05", "Buy", symbol="AAPL",
                                  quantity="10", price="100.00", amount=-1000_00)
    investments.record_investment(conn, acct, "2020-06-05", "Buy", symbol="MSFT",
                                  quantity="5", price="200.00", amount=-1000_00)
    investments.record_investment(conn, acct, "2021-02-05", "Buy", symbol="AAPL",
                                  quantity="10", price="120.00", amount=-1200_00)
    investments.record_investment(conn, acct, "2021-09-09", "Div", symbol="AAPL",
                                  amount=30_00)
    investments.record_investment(conn, acct, "2022-03-05", "Sell", symbol="AAPL",
                                  quantity="5", price="130.00", amount=650_00)
    investments.record_investment(conn, acct, "2023-07-07", "ReinvDiv", symbol="MSFT",
                                  quantity="1", price="250.00", amount=250_00)
    investments.record_investment(conn, acct, "2024-04-04", "Sell", symbol="MSFT",
                                  quantity="2", price="300.00", amount=600_00)
    return acct


def test_investment_snapshot_equals_full_replay(conn):
    acct = _seed_investments(conn)
    investments.rebuild_holdings(conn, acct)      # builds holdings_checkpoints

    # Snapshots exist for each active year.
    yrs = [r[0] for r in conn.execute(
        "SELECT DISTINCT year FROM holdings_checkpoints WHERE account_id=? ORDER BY year",
        (acct,)).fetchall()]
    assert yrs == [2020, 2021, 2022, 2023, 2024]
    _assert_positions_match(conn, acct)

    # compute_holdings (the qty+cost projection) agrees too.
    snap = investments.compute_holdings(conn, acct)
    full = {s: investments._Lot(p.qty, p.cost) for s, p in
            investments._replay_positions(conn, acct, use_snapshots=False).items()}
    assert snap == full


# ---------------------------------------------------------------------------
# investments: back-dated edit cascades
# ---------------------------------------------------------------------------
def test_investment_backdated_edit_cascades(conn):
    acct = _seed_investments(conn)
    investments.rebuild_holdings(conn, acct)

    pos_2022_before = investments._replay_positions(conn, acct, "2022-12-31",
                                                    use_snapshots=True)["AAPL"]

    # Back-date an extra AAPL buy into 2021 (raw write invalidates snapshots
    # from 2021 on); the UI rebuild recomputes the cascade.
    investments.record_investment(conn, acct, "2021-03-03", "Buy", symbol="AAPL",
                                  quantity="10", price="90.00", amount=-900_00)
    investments.rebuild_holdings(conn, acct)

    # The 2022 snapshot reflects the back-dated shares (quantity rose by 10) ...
    pos_2022_after = investments._replay_positions(conn, acct, "2022-12-31",
                                                   use_snapshots=True)["AAPL"]
    assert pos_2022_after.qty == pos_2022_before.qty + 10
    # ... and the whole snapshot+delta path still equals the full replay.
    _assert_positions_match(conn, acct)


def test_investment_cascade_helper_matches_full_rebuild(conn):
    acct = _seed_investments(conn)
    investments.rebuild_holdings(conn, acct)

    # A direct, targeted cascade from 2021 must land on the same snapshots as a
    # from-scratch rebuild.
    def _dump():
        return conn.execute(
            "SELECT year, symbol, quantity, cost_basis, dividends, realized, ever_held "
            "FROM holdings_checkpoints WHERE account_id=? ORDER BY year, symbol",
            (acct,)).fetchall()

    baseline = [tuple(r) for r in _dump()]
    investments.recompute_holdings_checkpoints_from_year(conn, acct, 2021)
    assert [tuple(r) for r in _dump()] == baseline
