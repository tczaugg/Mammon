"""Tax lots in the investment replay (roadmap item 7): FIFO, LIFO, average
and specified lots relieve cost differently and book one realized gain per
lot; splits and returns of capital keep the lots consistent; and the year-end
snapshots carry the lots so snapshot+delta equals a from-inception replay."""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, investments, ledger, portfolio


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "lots.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


def _rec(conn, a, date, action, sym, qty=None, price=None, amount=None, **kw):
    return investments.record_investment(conn, a, date, action, symbol=sym, quantity=qty,
                                         price=price, amount=amount, **kw)


def _two_lots_then_sale(conn, a):
    b1 = _rec(conn, a, "2024-01-05", "Buy", "AAPL", "10", "100.00", -1000_00)
    b2 = _rec(conn, a, "2024-06-05", "Buy", "AAPL", "10", "120.00", -1200_00)
    s = _rec(conn, a, "2025-03-05", "Sell", "AAPL", "5", "130.00", 650_00)
    return b1, b2, s


def _pos(conn, a, sym="AAPL"):
    return investments._replay_positions(conn, a, use_snapshots=False)[sym]


def _lots(pos):
    return [(lot.date, str(lot.qty), lot.cost) for lot in pos.lots]


# ---------------------------------------------------------------------------
# the four ways a sale relieves cost
# ---------------------------------------------------------------------------
def test_average_is_the_default_and_matches_the_old_figures(conn, acct):
    _two_lots_then_sale(conn, acct)
    pos = _pos(conn, acct)
    assert investments.get_lot_method(conn, acct) == "average"
    assert (pos.qty, pos.cost, pos.realized) == (Decimal(15), 1650_00, 100_00)
    # every remaining share carries the average ($110): 5 + 10 shares
    assert _lots(pos) == [("2024-01-05", "5", 550_00), ("2024-06-05", "10", 1100_00)]
    [g] = pos.gains
    assert (g.acquired, g.sold, str(g.quantity), g.proceeds, g.basis, g.gain, g.term) == \
        ("2024-01-05", "2025-03-05", "5", 650_00, 550_00, 100_00, "long")


def test_fifo_relieves_the_oldest_lot(conn, acct):
    investments.set_lot_method(conn, acct, "fifo")
    _two_lots_then_sale(conn, acct)
    pos = _pos(conn, acct)
    assert (pos.cost, pos.realized) == (1700_00, 150_00)
    assert _lots(pos) == [("2024-01-05", "5", 500_00), ("2024-06-05", "10", 1200_00)]
    assert [(g.basis, g.term) for g in pos.gains] == [(500_00, "long")]


def test_lifo_relieves_the_newest_lot(conn, acct):
    investments.set_lot_method(conn, acct, "lifo")
    _two_lots_then_sale(conn, acct)
    pos = _pos(conn, acct)
    assert (pos.cost, pos.realized) == (1600_00, 50_00)
    assert _lots(pos) == [("2024-01-05", "10", 1000_00), ("2024-06-05", "5", 600_00)]
    assert [(g.basis, g.term) for g in pos.gains] == [(600_00, "short")]


def test_specified_lots_win_over_the_method(conn, acct):
    investments.set_lot_method(conn, acct, "fifo")
    b1, b2, s = _two_lots_then_sale(conn, acct)
    investments.assign_lots(conn, s, [(b2, "3"), (b1, "2")])
    assert investments.lot_assignments_for(conn, s) == [(b2, "3"), (b1, "2")]
    pos = _pos(conn, acct)
    assert pos.cost == 2200_00 - 360_00 - 200_00
    assert [(g.acquired, str(g.quantity), g.basis, g.term) for g in pos.gains] == \
        [("2024-06-05", "3", 360_00, "short"), ("2024-01-05", "2", 200_00, "long")]
    assert sum(g.proceeds for g in pos.gains) == 650_00
    # Guards: not a purchase of this security, too many shares, wrong row.
    with pytest.raises(ValueError):
        investments.assign_lots(conn, s, [(b1, "6")])
    with pytest.raises(ValueError):
        investments.assign_lots(conn, b1, [(b2, "1")])
    investments.assign_lots(conn, s, [])
    assert _pos(conn, acct).cost == 1700_00                  # back to FIFO


def test_a_sale_across_lots_books_one_gain_per_lot(conn, acct):
    investments.set_lot_method(conn, acct, "fifo")
    _rec(conn, acct, "2024-01-05", "Buy", "VTI", "10", "100.00", -1000_00)
    _rec(conn, acct, "2024-06-05", "Buy", "VTI", "10", "120.00", -1200_00)
    # The amount is the NET cash the sale brought in (15 x 130 less the $10
    # commission), as Quicken's total and an OFX <TOTAL> state it; the replay no
    # longer takes the commission off a second time (investments._proceeds_of).
    _rec(conn, acct, "2025-03-05", "Sell", "VTI", "15", "130.00", 1940_00, commission=10_00)
    pos = _pos(conn, acct, "VTI")
    assert (str(pos.qty), pos.cost) == ("5", 600_00)
    assert [(str(g.quantity), g.proceeds, g.basis, g.term) for g in pos.gains] == \
        [("10", 1293_33, 1000_00, "long"), ("5", 646_67, 600_00, "short")]
    assert pos.realized == 1940_00 - 1600_00


# ---------------------------------------------------------------------------
# corporate actions keep the lots consistent
# ---------------------------------------------------------------------------
def test_split_scales_every_lot_and_keeps_its_date(conn, acct):
    investments.set_lot_method(conn, acct, "fifo")
    _rec(conn, acct, "2024-01-05", "Buy", "NVDA", "10", "100.00", -1000_00)
    _rec(conn, acct, "2024-06-05", "Buy", "NVDA", "10", "200.00", -2000_00)
    _rec(conn, acct, "2024-08-01", "StkSplit", "NVDA", "20", split_num=2, split_den=1)
    _rec(conn, acct, "2025-09-01", "Sell", "NVDA", "25", "150.00", 3750_00)
    pos = _pos(conn, acct, "NVDA")
    assert (str(pos.qty), pos.cost) == ("15", 1500_00)
    assert _lots(pos) == [("2024-06-05", "15", 1500_00)]
    assert [(g.acquired, str(g.quantity), g.basis, g.term) for g in pos.gains] == \
        [("2024-01-05", "20", 1000_00, "long"), ("2024-06-05", "5", 500_00, "long")]


def test_return_of_capital_lowers_every_lot_in_proportion(conn, acct):
    investments.set_lot_method(conn, acct, "fifo")
    _rec(conn, acct, "2024-01-05", "Buy", "O", "10", "100.00", -1000_00)
    _rec(conn, acct, "2024-06-05", "Buy", "O", "10", "200.00", -2000_00)
    _rec(conn, acct, "2024-12-01", "RtrnCap", "O", amount=300_00)
    pos = _pos(conn, acct, "O")
    assert pos.cost == 2700_00
    assert _lots(pos) == [("2024-01-05", "10", 1350_00), ("2024-06-05", "10", 1350_00)]


# ---------------------------------------------------------------------------
# snapshots carry the lots
# ---------------------------------------------------------------------------
def _seed_years(conn, a):
    _rec(conn, a, "2020-01-05", "Buy", "AAPL", "10", "100.00", -1000_00)
    _rec(conn, a, "2020-06-05", "Buy", "MSFT", "5", "200.00", -1000_00)
    _rec(conn, a, "2021-02-05", "Buy", "AAPL", "10", "120.00", -1200_00)
    _rec(conn, a, "2021-09-09", "Div", "AAPL", amount=30_00)
    _rec(conn, a, "2022-03-05", "Sell", "AAPL", "5", "130.00", 650_00)
    _rec(conn, a, "2023-07-07", "ReinvDiv", "MSFT", "1", "250.00", 250_00)
    _rec(conn, a, "2024-04-04", "Sell", "MSFT", "2", "300.00", 600_00)


@pytest.mark.parametrize("method", ["average", "fifo", "lifo"])
def test_snapshot_plus_delta_equals_full_replay_lots_included(conn, acct, method):
    _seed_years(conn, acct)
    investments.set_lot_method(conn, acct, method)          # rebuilds the snapshots
    rows = conn.execute("SELECT year, symbol, lots FROM holdings_checkpoints "
                        "WHERE account_id=? ORDER BY year, symbol", (acct,)).fetchall()
    assert [r["year"] for r in rows][:2] == [2020, 2020] and all(r["lots"] for r in rows)
    snap = investments._replay_positions(conn, acct, use_snapshots=True)
    full = investments._replay_positions(conn, acct, use_snapshots=False)
    assert snap == full                     # dataclass equality: qty, cost, lots, ...
    assert all(_lots(snap[s]) == _lots(full[s]) for s in full)
    # The mid-history read (seeded from the 2021 snapshot) agrees too.
    assert investments._replay_positions(conn, acct, "2022-12-31", True) == \
        investments._replay_positions(conn, acct, "2022-12-31", False)
    assert investments.compute_holdings(conn, acct)["AAPL"].qty == 15


def test_capital_gains_are_read_from_inception_and_filtered_by_date(conn, acct):
    investments.set_lot_method(conn, acct, "fifo")
    _seed_years(conn, acct)
    investments.rebuild_holdings(conn, acct)
    all_gains = portfolio.capital_gains(conn, acct)
    assert [(g.symbol, g.sold, g.basis, g.gain, g.term) for g in all_gains] == [
        ("AAPL", "2022-03-05", 500_00, 150_00, "long"),
        ("MSFT", "2024-04-04", 400_00, 200_00, "long")]
    assert [g.symbol for g in portfolio.capital_gains(conn, acct, "2024-01-01", "2024-12-31")] == ["MSFT"]
    s = portfolio.gains_summary(all_gains)
    assert (s["long"]["gain"], s["long"]["lots"], s["short"]["lots"], s["total"]["proceeds"]) == \
        (350_00, 2, 0, 1250_00)
