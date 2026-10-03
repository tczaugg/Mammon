"""Tests for the crypto import path (phase 3): an Etherscan-style native-ETH
by-address CSV -> crypto events on a ``type='crypto'`` account.

Covers the pure parser (text -> CryptoRecord, no DB) and the DB-facing core that
funnels EVERY write through ``mammon.crypto``'s event writers (never a second
writer of the crypto_* tables), exercising the four import rules the real 2020
ETH export forced:

  1. gas is a same-coin fee_* leg booked ONLY when From == the user's own wallet;
  2. sign derives from the two unsigned Value_IN / Value_OUT columns;
  3. FMV comes from ``Historical $Price/Eth``, never the export-time CurrentValue;
  4. a row is an own-wallet transfer only when the OTHER address is a registered
     Mammon crypto account; otherwise SEND / RECEIVE.

Plus: FIFO lots + realized gain from a real disposal, and tx_hash exact-dedup so
a re-import is a no-op.

Synthetic data only -- every address and tx hash below is an obvious ANON
placeholder; no real wallet address, hash or amount appears here or in the
fixture (real exports are PII and stay in the gitignored data/ dir).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from mammon import crypto, db, ledger
from mammon.importers import crypto_csv, crypto_core
from mammon.tests import fresh_db

FIXTURE = Path(__file__).parent / "fixtures" / "etherscan_eth_2020.csv"

WALLET = "0x1111111111111111111111111111111111111111"
COUNTERPARTY = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"
OTHER_WALLET = "0x4444444444444444444444444444444444444444"

HEADER = ("Txhash,Blockno,UnixTimestamp,DateTime,From,To,ContractAddress,"
          "Value_IN(ETH),Value_OUT(ETH),CurrentValue @ $1500.00/Eth,"
          "TxnFee(ETH),TxnFee(USD),Historical $Price/Eth,Status,ErrCode")


def _row(txhash, unix, frm, to, *, vin="0", vout="0", fee_eth="0.001",
         hist="100.00", status="", err=""):
    return ",".join([txhash, "1", str(unix), "1/1/2020 0:00", frm, to, "",
                     vin, vout, "0.00", fee_eth, "0.00", hist, status, err])


def _csv(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "crypto_import.db")
    yield c
    c.close()


def _wallet(conn, addr=WALLET, name="ETH Wallet", method="fifo"):
    aid = crypto.create_account(conn, name, wallet_address=addr)
    ledger.update_account(conn, aid, lot_method=method)
    return aid


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------
def test_parser_roundtrips_fixture_into_records():
    recs = crypto_csv.parse_etherscan(FIXTURE.read_text())
    assert len(recs) == 3

    r0, r1, r2 = recs
    # Rule 2: sign derives from the two columns.
    assert (r0.direction, Decimal(r0.quantity)) == ("in", Decimal(2))
    assert (r1.direction, Decimal(r1.quantity)) == ("in", Decimal(2))
    assert (r2.direction, Decimal(r2.quantity)) == ("out", Decimal(1))
    # Unix stamp -> ISO date (UTC), not the ambiguous DateTime string.
    assert [r.date for r in recs] == ["2020-02-15", "2020-03-15", "2020-06-15"]
    # Rule 3: the parser carries the HISTORICAL price, never CurrentValue.
    assert [Decimal(r.price) for r in recs] == [Decimal("200"), Decimal("400"), Decimal("500")]
    # Addresses are preserved (lower-cased) so the core can attribute gas.
    assert (r0.from_addr, r0.to_addr) == (COUNTERPARTY, WALLET)
    assert (r2.from_addr, r2.to_addr) == (WALLET, RECIPIENT)
    # The parser is dumb about attribution: it records EVERY row's raw gas.
    assert r0.fee_symbol == "ETH" and Decimal(r0.fee_quantity) == Decimal("0.001")
    assert r2.fee_symbol == "ETH" and Decimal(r2.fee_quantity) == Decimal("0.002")


def test_looks_like_etherscan():
    assert crypto_csv.looks_like_etherscan(FIXTURE.read_text())
    assert not crypto_csv.looks_like_etherscan("Date,Payee,Amount\n2020-01-01,X,5\n")


# ---------------------------------------------------------------------------
# Core: RECEIVE / SEND events via crypto.py writers
# ---------------------------------------------------------------------------
def test_import_books_receive_and_send_events(conn):
    wallet = _wallet(conn)
    result = crypto_core.import_etherscan_file(conn, FIXTURE, wallet)
    assert (result.imported, result.duplicates) == (3, 0)

    events = crypto.list_events(conn, wallet)
    actions = [e["action"] for e in events]
    assert actions == ["RECEIVE", "RECEIVE", "SEND"]
    # Inbound rows are income at FMV; the outbound row is a disposal.
    assert [e["quantity"] for e in events] == ["2", "2", "-1"]


def test_gas_booked_only_when_from_is_user_wallet(conn):
    """Rule 1: gas is a same-coin fee_* leg only on the row the user SENT."""
    wallet = _wallet(conn)
    crypto_core.import_etherscan_file(conn, FIXTURE, wallet)
    events = {e["action"]: e for e in crypto.list_events(conn, wallet)}

    send = events["SEND"]
    assert send["fee_symbol"] == "ETH"
    assert send["fee_quantity"] == "0.002"
    # Valued at the row's HISTORICAL price ($500/ETH * 0.002 = $1.00), NOT the
    # export-time CurrentValue rate that fills the TxnFee(USD) column.
    assert send["fee_amount"] == 1_00

    # The 23-inbound-row lesson: the counterparty's gas must not debit the user.
    for recv in crypto.list_events(conn, wallet):
        if recv["action"] == "RECEIVE":
            assert recv["fee_symbol"] is None
            assert recv["fee_quantity"] is None
            assert recv["fee_amount"] is None


def test_fmv_from_historical_price_not_currentvalue(conn):
    """Rule 3: basis/proceeds come from Historical $Price/Eth. The fixture's
    CurrentValue column ($1500/ETH) is deliberately divergent, so a wrong basis
    would be unmistakable."""
    wallet = _wallet(conn)
    crypto_core.import_etherscan_file(conn, FIXTURE, wallet)
    receives = [e for e in crypto.list_events(conn, wallet) if e["action"] == "RECEIVE"]
    # 2 ETH @ $200 and 2 ETH @ $400 -- from Historical, not 2 ETH @ $1500.
    assert sorted(e["basis"] for e in receives) == [400_00, 800_00]


# ---------------------------------------------------------------------------
# FIFO lots + realized gain from the disposal
# ---------------------------------------------------------------------------
def test_fifo_lots_and_realized_gain(conn):
    wallet = _wallet(conn, method="fifo")
    crypto_core.import_etherscan_file(conn, FIXTURE, wallet)

    gains = crypto.realized_gains(conn, wallet)
    assert len(gains) == 1
    g = gains[0]
    # FIFO relieves the FIRST lot (2 ETH @ $200), so 1 ETH out carries $200 basis.
    # Proves FIFO: LIFO would relieve the $400 lot (gain $100); average -> $200 basis
    # at $300/ETH avg (gain $200). FIFO gain = $500 - $200 = $300.
    assert (g.proceeds, g.basis, g.gain) == (500_00, 200_00, 300_00)

    # Remaining: 4 ETH in - 1 ETH sent - 0.002 ETH gas = 2.998 ETH.
    # Basis: $1200 - $200 (send, FIFO lot1) - $0.40 (gas, FIFO lot1) = $999.60.
    holdings = crypto.rebuild_holdings(conn, wallet)
    assert holdings == [{"symbol": "ETH", "quantity": "2.998", "cost_basis": 999_60}]


# ---------------------------------------------------------------------------
# tx_hash dedup: re-import is a no-op
# ---------------------------------------------------------------------------
def test_reimport_is_noop_via_tx_hash(conn):
    wallet = _wallet(conn)
    first = crypto_core.import_etherscan_file(conn, FIXTURE, wallet)
    assert first.imported == 3

    before = _txn_count(conn)
    second = crypto_core.import_etherscan_file(conn, FIXTURE, wallet)
    assert (second.imported, second.duplicates) == (0, 3)
    assert _txn_count(conn) == before  # not one row added


def _txn_count(conn):
    return conn.execute("SELECT COUNT(*) FROM crypto_transactions").fetchone()[0]


# ---------------------------------------------------------------------------
# Rule 4: own-wallet transfer only when both addresses are registered accounts
# ---------------------------------------------------------------------------
def test_own_wallet_transfer_when_recipient_is_a_registered_account(conn):
    a = _wallet(conn, addr=WALLET, name="Wallet A")
    b = _wallet(conn, addr=OTHER_WALLET, name="Wallet B")
    text = _csv(
        # fund A from an external counterparty
        _row("0xaa01", 1581724800, COUNTERPARTY, WALLET, vin="5", hist="100.00"),
        # A -> B : both are the user's own wallets => mirror transfer, no gain
        _row("0xaa02", 1592179200, WALLET, OTHER_WALLET, vout="1", fee_eth="0.002",
             hist="300.00"),
    )
    result = crypto_core.import_crypto_records(
        conn, crypto_csv.parse_etherscan(text), a)
    assert result.imported == 2

    a_events = {e["action"]: e for e in crypto.list_events(conn, a)}
    b_events = crypto.list_events(conn, b)
    assert "TRANSFER_OUT" in a_events and "SEND" not in a_events
    assert [e["action"] for e in b_events] == ["TRANSFER_IN"]

    out_leg, in_leg = a_events["TRANSFER_OUT"], b_events[0]
    assert out_leg["transfer_account_id"] == b
    assert in_leg["transfer_account_id"] == a
    assert out_leg["transfer_pair_id"] == in_leg["id"]
    # A transfer books NO realized gain -- basis rides to the other wallet.
    assert crypto.realized_gains(conn, a) == []
    # Gas (From == A's own wallet) still rides the OUT leg.
    assert out_leg["fee_symbol"] == "ETH" and out_leg["fee_quantity"] == "0.002"


def test_external_recipient_is_a_send_not_a_transfer(conn):
    """The mirror-image of rule 4: an unregistered recipient stays a SEND."""
    a = _wallet(conn, addr=WALLET, name="Wallet A")
    text = _csv(
        _row("0xbb01", 1581724800, COUNTERPARTY, WALLET, vin="5", hist="100.00"),
        _row("0xbb02", 1592179200, WALLET, RECIPIENT, vout="1", fee_eth="0.002",
             hist="300.00"),
    )
    crypto_core.import_crypto_records(conn, crypto_csv.parse_etherscan(text), a)
    actions = [e["action"] for e in crypto.list_events(conn, a)]
    assert "SEND" in actions and "TRANSFER_OUT" not in actions


# ---------------------------------------------------------------------------
# Failed on-chain transactions move no value
# ---------------------------------------------------------------------------
def test_failed_transaction_is_skipped(conn):
    wallet = _wallet(conn)
    text = _csv(
        _row("0xcc01", 1581724800, COUNTERPARTY, WALLET, vin="1", hist="100.00",
             status="Error", err="execution reverted"),
    )
    result = crypto_core.import_crypto_records(
        conn, crypto_csv.parse_etherscan(text), wallet)
    assert (result.imported, result.skipped_failed) == (0, 1)
    assert crypto.list_events(conn, wallet) == []


def test_import_rejects_a_non_crypto_account(conn):
    cash = ledger.create_account(conn, "Checking", "checking")
    with pytest.raises(ValueError):
        crypto_core.import_crypto_records(conn, [], cash)
