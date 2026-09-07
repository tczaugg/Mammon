"""Tests for mammon.crypto -- the cryptocurrency domain layer (phase 2 + 3).

Covers buy/sell + realized gain, coin-for-coin swaps (linked SWAP_OUT/SWAP_IN
via swap_group_id), wallet-to-wallet transfers (the mirror model in coin: two
legs, edit-syncs-both, delete-deletes-both, basis rides along, no gain), gas/
network fees as a same-coin plain expense, in-kind income (REWARD accrues at
FMV), the per-year holdings-checkpoint machinery matching the full-replay oracle,
valuation via an injected fake quote source under the '{SYM}-USD' pair, and the
wei-scale high-precision summation guard.

Synthetic data only -- no real wallet addresses, tx hashes, amounts or PII.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import crypto, db, investments, ledger
from mammon.investments import Quote


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "crypto.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    # Fund the account with a cash sleeve so a buy nets to zero cash movement.
    return crypto.create_account(conn, "Hot Wallet", opening_balance=1_000_000_00)


class _FakeSource:
    """The injected quote seam: given BARE coin symbols, returns Quotes already
    keyed by the '{SYM}-USD' pair -- exactly what CryptoQuoteSource yields."""
    source_name = "fake"

    def __init__(self, quotes):
        self._quotes = quotes
        self.asked = None

    def get_quotes(self, symbols):
        self.asked = list(symbols)
        return self._quotes


# ---------------------------------------------------------------------------
# Buy / sell / realized gain
# ---------------------------------------------------------------------------
def test_buy_establishes_lot_and_debits_cash_sleeve(conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "BTC", "0.5", 30_000_00)
    holdings = crypto.rebuild_holdings(conn, wallet)
    assert holdings == [{"symbol": "BTC", "quantity": "0.5", "cost_basis": 30_000_00}]
    # Fiat left the internal cash sleeve; total cash unchanged by the conversion.
    assert crypto.crypto_cash(conn, wallet) == -30_000_00


def test_sell_books_realized_gain_and_reduces_holding(conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "2", 4_000_00)
    crypto.record_sell(conn, wallet, "2024-06-01", "ETH", "1", 3_000_00)
    holdings = crypto.rebuild_holdings(conn, wallet)
    assert holdings == [{"symbol": "ETH", "quantity": "1", "cost_basis": 2_000_00}]
    gains = crypto.realized_gains(conn, wallet)
    assert len(gains) == 1
    # Sold 1 ETH for $3000; relieved half the $4000 average-cost basis = $2000.
    assert gains[0].proceeds == 3_000_00
    assert gains[0].basis == 2_000_00
    assert gains[0].gain == 1_000_00


# ---------------------------------------------------------------------------
# Coin-for-coin swap (SWAP_OUT + SWAP_IN linked by swap_group_id)
# ---------------------------------------------------------------------------
def test_swap_disposes_out_leg_and_establishes_in_leg_at_fmv(conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "BTC", "1", 10_000_00)
    out_id, in_id = crypto.record_swap(
        conn, wallet, "2024-03-01", "BTC", "1", "ETH", "20", 12_000_00)

    out_row = crypto.get_event(conn, out_id)
    in_row = crypto.get_event(conn, in_id)
    # Two single-asset legs, one economic event, linked by a shared group id.
    assert out_row["action"] == "SWAP_OUT" and in_row["action"] == "SWAP_IN"
    assert out_row["swap_group_id"] == in_row["swap_group_id"] == out_id

    holdings = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, wallet)}
    assert "BTC" not in holdings                          # fully disposed
    assert holdings["ETH"]["quantity"] == "20"
    assert holdings["ETH"]["cost_basis"] == 12_000_00      # new lot basis = FMV

    gains = crypto.realized_gains(conn, wallet)
    assert len(gains) == 1
    assert gains[0].proceeds == 12_000_00 and gains[0].basis == 10_000_00
    assert gains[0].gain == 2_000_00


def test_deleting_one_swap_leg_deletes_both(conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "BTC", "1", 10_000_00)
    out_id, in_id = crypto.record_swap(
        conn, wallet, "2024-03-01", "BTC", "1", "ETH", "20", 12_000_00)
    assert crypto.delete_event(conn, in_id) is True
    assert crypto.get_event(conn, out_id) is None
    assert crypto.get_event(conn, in_id) is None
    # The BTC lot is back to whole after the swap is unwound.
    holdings = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, wallet)}
    assert holdings["BTC"]["quantity"] == "1"


# ---------------------------------------------------------------------------
# Wallet-to-wallet transfer (the mirror model, in coin quantity)
# ---------------------------------------------------------------------------
def test_wallet_transfer_mirrors_and_rides_basis(conn, wallet):
    other = crypto.create_account(conn, "Cold Wallet")
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "2", 4_000_00)
    out_id, in_id = crypto.record_wallet_transfer(
        conn, wallet, other, "2024-02-01", "ETH", "1")

    out_row = crypto.get_event(conn, out_id)
    in_row = crypto.get_event(conn, in_id)
    assert out_row["action"] == "TRANSFER_OUT" and in_row["action"] == "TRANSFER_IN"
    # Cross-linked: each leg's pair points at the other row and account.
    assert out_row["transfer_pair_id"] == in_id
    assert in_row["transfer_pair_id"] == out_id
    assert out_row["transfer_account_id"] == other
    assert in_row["transfer_account_id"] == wallet
    # No fiat moved; the basis rides along (half the $4000 average cost).
    assert out_row["basis"] == in_row["basis"] == 2_000_00

    src = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, wallet)}
    dst = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, other)}
    assert src["ETH"]["quantity"] == "1" and src["ETH"]["cost_basis"] == 2_000_00
    assert dst["ETH"]["quantity"] == "1" and dst["ETH"]["cost_basis"] == 2_000_00
    # A transfer is not a disposal: no realized gain on either side.
    assert crypto.realized_gains(conn, wallet) == []
    assert crypto.realized_gains(conn, other) == []


def test_editing_one_transfer_leg_syncs_the_other(conn, wallet):
    other = crypto.create_account(conn, "Cold Wallet")
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "2", 4_000_00)
    out_id, in_id = crypto.record_wallet_transfer(
        conn, wallet, other, "2024-02-01", "ETH", "1")

    # Edit the OUT leg's date and quantity; the IN leg mirrors (date same,
    # quantity negated).
    crypto.update_event(conn, out_id, date="2024-03-15", quantity="-1.5")
    in_row = crypto.get_event(conn, in_id)
    assert in_row["date"] == "2024-03-15"
    assert Decimal(in_row["quantity"]) == Decimal("1.5")


def test_deleting_one_transfer_leg_deletes_both(conn, wallet):
    other = crypto.create_account(conn, "Cold Wallet")
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "2", 4_000_00)
    out_id, in_id = crypto.record_wallet_transfer(
        conn, wallet, other, "2024-02-01", "ETH", "1")
    assert crypto.delete_event(conn, out_id) is True
    assert crypto.get_event(conn, out_id) is None
    assert crypto.get_event(conn, in_id) is None


# ---------------------------------------------------------------------------
# Gas / network fee (same-coin, plain expense)
# ---------------------------------------------------------------------------
def test_same_coin_gas_fee_reduces_holding_as_plain_expense(conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "2", 4_000_00)
    # Send 1 ETH at FMV $2500 with 0.01 ETH gas (ETH gas on an ETH move).
    crypto.record_send(conn, wallet, "2024-06-01", "ETH", "1", 2_500_00,
                       fee_symbol="ETH", fee_quantity="0.01", fee_amount=25_00)
    holdings = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, wallet)}
    # 2 held - 1 sent - 0.01 gas = 0.99 ETH remaining.
    assert holdings["ETH"]["quantity"] == "0.99"
    # The SEND is a disposal (books gain); the gas is a plain expense (no gain).
    gains = crypto.realized_gains(conn, wallet)
    assert len(gains) == 1
    assert gains[0].proceeds == 2_500_00


# ---------------------------------------------------------------------------
# In-kind income (staking reward), accrues to the checkpoint income total
# ---------------------------------------------------------------------------
def test_reward_income_adds_lot_at_fmv_and_accrues_income(conn, wallet):
    crypto.record_income(conn, wallet, "2024-04-01", "REWARD", "ETH", "0.5", 1_200_00)
    holdings = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, wallet)}
    assert holdings["ETH"]["quantity"] == "0.5"
    assert holdings["ETH"]["cost_basis"] == 1_200_00     # basis = FMV at receipt
    # Income accrues to the checkpoint income column (the crypto 'dividends').
    year = int("2024")
    row = conn.execute(
        "SELECT income FROM crypto_holdings_checkpoints "
        "WHERE account_id=? AND year=? AND symbol='ETH'", (wallet, year)).fetchone()
    assert row["income"] == 1_200_00


# ---------------------------------------------------------------------------
# Checkpoint path == full-replay oracle (mirrors test_year_end_snapshots)
# ---------------------------------------------------------------------------
def test_checkpoint_replay_matches_full_replay_oracle(conn, wallet):
    crypto.record_buy(conn, wallet, "2022-02-10", "BTC", "1", 10_000_00)
    crypto.record_buy(conn, wallet, "2023-03-15", "BTC", "0.5", 6_000_00)
    crypto.record_income(conn, wallet, "2023-07-01", "REWARD", "BTC", "0.1", 1_200_00)
    crypto.record_sell(conn, wallet, "2024-05-20", "BTC", "0.4", 8_000_00)
    crypto.rebuild_holdings_checkpoints(conn, wallet)

    # Seeded-from-snapshot replay must equal a from-inception replay, exactly.
    via_snapshots = crypto._replay_positions(conn, wallet, use_snapshots=True)
    from_inception = crypto._replay_positions(conn, wallet, use_snapshots=False)
    assert via_snapshots == from_inception

    # And a mid-history recompute must equal a full rebuild.
    crypto.recompute_holdings_checkpoints_from_year(conn, wallet, 2023)
    rebuilt = crypto._replay_positions(conn, wallet, use_snapshots=True)
    assert rebuilt == from_inception


def test_checkpoint_survives_out_of_order_edit(conn, wallet):
    crypto.record_buy(conn, wallet, "2022-02-10", "BTC", "1", 10_000_00)
    crypto.record_buy(conn, wallet, "2024-05-20", "BTC", "1", 20_000_00)
    crypto.rebuild_holdings_checkpoints(conn, wallet)
    # An edit to the 2022 row invalidates 2022+ snapshots; a fresh replay is
    # still correct (checkpoints are a cache, never the source of truth).
    ev = crypto.list_events(conn, wallet)[0]
    crypto.update_event(conn, ev["id"], amount=-12_000_00, basis=12_000_00)
    holdings = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, wallet)}
    assert holdings["BTC"]["quantity"] == "2"
    assert holdings["BTC"]["cost_basis"] == 32_000_00


# ---------------------------------------------------------------------------
# Valuation + price history via the '{SYM}-USD' pair
# ---------------------------------------------------------------------------
def test_fetch_quotes_stores_under_pair_symbol(conn, wallet):
    src = _FakeSource([Quote("ETH-USD", "2024-06-01", "2500.00", "fake"),
                       Quote("BTC-USD", "2024-06-01", "60000.00", "fake")])
    quotes = crypto.fetch_quotes(conn, ["ETH", "BTC", " ", "ETH"], source=src)
    assert len(quotes) == 2
    # The source is asked for BARE symbols (deduped), stores under the pair.
    assert src.asked == ["ETH", "BTC"]
    assert crypto.latest_price(conn, "ETH") == Decimal("2500.00")
    assert crypto.latest_price(conn, "BTC") == Decimal("60000.00")
    # Stored under the pair, never the bare ticker (avoids stock/coin collision).
    assert investments.latest_price(conn, "ETH-USD") == Decimal("2500.00")


def test_holding_values_and_account_valuation(conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "2", 4_000_00)
    crypto.rebuild_holdings(conn, wallet)
    hvs = crypto.holding_values(conn, wallet, prices={"ETH": "2500"})
    assert len(hvs) == 1
    assert hvs[0].market_value == 5_000_00           # 2 * $2500
    assert hvs[0].gain == 1_000_00                   # $5000 - $4000 cost

    val = crypto.account_valuation(conn, wallet, prices={"ETH": "2500"})
    # cash = opening 1,000,000.00 + sleeve (-4,000.00); securities = 5,000.00
    assert val.cash == 1_000_000_00 - 4_000_00
    assert val.securities == 5_000_00
    assert val.total == val.cash + val.securities


def test_display_balance_delegates_from_investments(conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "2", 4_000_00)
    crypto.rebuild_holdings(conn, wallet)
    src = _FakeSource([Quote("ETH-USD", "2024-06-01", "2500.00", "fake")])
    crypto.fetch_quotes(conn, ["ETH"], source=src)

    direct = crypto.display_balance(conn, wallet)
    # The app's single valuation entry point must route a crypto account to the
    # crypto domain layer (not value it as an equity brokerage with 0 coins).
    via_investments = investments.display_balance(conn, wallet)
    assert direct == via_investments
    assert direct == crypto.account_valuation(
        conn, wallet, crypto.valuation_as_of(conn)).total


# ---------------------------------------------------------------------------
# Precision: wei-scale storage AND high-precision summation
# ---------------------------------------------------------------------------
def test_wei_scale_quantity_round_trips_through_replay(conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH",
                      "1.234567890123456789", 5_000_00)
    holdings = crypto.rebuild_holdings(conn, wallet)
    assert holdings[0]["quantity"] == "1.234567890123456789"


def test_summation_survives_wei_drop_under_high_precision(conn, wallet):
    # Adding one wei to a large balance would be dropped under the default
    # 28-significant-digit context; the replay runs under prec>=40, so it holds.
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "1e11", 100)
    crypto.record_buy(conn, wallet, "2024-01-06", "ETH", "1e-18", 1)
    positions = crypto.compute_holdings(conn, wallet)
    assert positions["ETH"].qty == Decimal("100000000000.000000000000000001")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def test_record_event_rejects_unknown_action(conn, wallet):
    with pytest.raises(ValueError):
        crypto.record_event(conn, wallet, "2024-01-05", "HODL", symbol="BTC",
                            quantity="1")


def test_wallet_transfer_rejects_same_account(conn, wallet):
    with pytest.raises(ValueError):
        crypto.record_wallet_transfer(conn, wallet, wallet, "2024-02-01", "ETH", "1")
