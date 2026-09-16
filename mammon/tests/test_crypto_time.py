"""A crypto event keeps the time of day its source states, and a day's events
are ordered by it (migration 70, SRD 5.8j).

The register orders by date, then time, then cash high to low (so on a tie the
money arrives before it is spent), then entry order. The replay follows real
chronology. Rows imported before the column existed get their time back from a
re-import of the same export. All data is synthetic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mammon import crypto, db, import_review, ledger
from mammon.importers import coinbase_csv, crypto_core, crypto_csv
from mammon.importers.record import stamp_time

FIXTURE = Path(__file__).parent / "fixtures" / "coinbase_history.csv"
WALLET = "0x1111111111111111111111111111111111111111"
COUNTERPARTY = "0x2222222222222222222222222222222222222222"
HEADER = ("Txhash,Blockno,UnixTimestamp,DateTime,From,To,ContractAddress,"
          "Value_IN(ETH),Value_OUT(ETH),CurrentValue @ $1500.00/Eth,"
          "TxnFee(ETH),TxnFee(USD),Historical $Price/Eth,Status,ErrCode")
MIDNIGHT = 1581724800                     # 2020-02-15 00:00:00 UTC


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "time.db")
    yield c
    c.close()


def _etherscan(*rows):
    lines = [HEADER]
    for txhash, unix, vin in rows:
        lines.append(",".join([txhash, "1", str(unix), "2/15/2020 0:00", COUNTERPARTY,
                               WALLET, "", vin, "0", "0.00", "0.001", "0.00", "100.00",
                               "", ""]))
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("value, expected", [
    (str(MIDNIGHT + 14 * 3600 + 5 * 60 + 7), "14:05:07"),     # unix seconds, UTC
    (str((MIDNIGHT + 60) * 1000), "00:01:00"),                # unix milliseconds
    ("2021-03-01 14:05:07 UTC", "14:05:07"),
    ("2021-03-01T14:05:07.250Z", "14:05:07"),
    ("2/15/2020 2:05 PM", "14:05:00"),
    ("12/1/2020 12:30 AM", "00:30:00"),
    ("2021-03-01", None),
    ("3/1/2021", None),
    ("2021-03-01 25:00", None),
    ("", None),
])
def test_stamp_time_reads_each_source_shape(value, expected):
    assert stamp_time(value) == expected


def test_both_exports_keep_the_time_they_state():
    recs = crypto_csv.parse_etherscan(_etherscan(("0xa", MIDNIGHT + 3 * 3600, "1")))
    assert (recs[0].date, recs[0].time) == ("2020-02-15", "03:00:00")
    first = coinbase_csv.parse_coinbase(FIXTURE.read_text(encoding="utf-8"))[0]
    assert (first.date, first.time) == ("2021-02-01", "10:00:00")


def test_a_day_shows_in_time_order_then_cash_high_to_low(conn):
    ex = crypto.create_account(conn, "Exchange", kind=crypto.CRYPTO_KIND_EXCHANGE)
    day = "2026-04-01"
    crypto.record_cash(conn, ex, day, 1_000_00, time="10:00:00", memo="deposit")
    crypto.record_buy(conn, ex, day, "ETH", "1", 500_00, time="09:00:00", memo="early buy")
    crypto.record_cash(conn, ex, day, -50_00, time="12:00:00", memo="withdraw")
    crypto.record_cash(conn, ex, day, 200_00, time="12:00:00", memo="same-second deposit")
    crypto.record_cash(conn, ex, day, 5_00, memo="no time")
    memos = [r["memo"] for r in crypto.register_rows(conn, ex)]
    assert memos == ["no time", "early buy", "deposit", "same-second deposit", "withdraw"]


def test_the_replay_follows_the_time_not_the_entry_order(conn):
    ex = crypto.create_account(conn, "Exchange", kind=crypto.CRYPTO_KIND_EXCHANGE)
    late = crypto.record_cash(conn, ex, "2026-04-01", 10_00, time="15:00:00")
    early = crypto.record_cash(conn, ex, "2026-04-01", 10_00, time="08:30:00")
    ids = [r["id"] for r in crypto._list_txns_in_range(conn, ex, None, None)]
    assert ids == [early, late]


def test_a_time_not_in_hh_mm_ss_is_refused(conn):
    """Stored as text and sorted as text: 9:05 would sort after 14:00."""
    ex = crypto.create_account(conn, "Exchange", kind=crypto.CRYPTO_KIND_EXCHANGE)
    with pytest.raises(ValueError):
        crypto.record_cash(conn, ex, "2026-04-01", 10_00, time="9:05")


def test_a_reimport_fills_the_time_on_rows_imported_without_one(conn):
    wallet = crypto.create_account(conn, "ETH Wallet", wallet_address=WALLET)
    text = _etherscan(("0xa", MIDNIGHT + 3 * 3600, "1"), ("0xb", MIDNIGHT + 7200, "2"))
    recs = crypto_csv.parse_etherscan(text)
    for r in recs:
        r.time = ""                                   # as imported before migration 70
    crypto_core.import_crypto_records(conn, recs, wallet)
    assert {r["time"] for r in crypto.list_events(conn, wallet)} == {None}

    conn.execute("UPDATE crypto_transactions SET time='23:59:59' WHERE tx_hash='0xb'")
    conn.commit()
    result = crypto_core.import_crypto_records(conn, crypto_csv.parse_etherscan(text), wallet)
    assert result.imported == 0 and result.duplicates == 2
    times = {r["tx_hash"]: r["time"] for r in crypto.list_events(conn, wallet)}
    assert times == {"0xa": "03:00:00", "0xb": "23:59:59"}   # a recorded time stays


def test_the_review_queue_carries_the_time_to_the_register(conn):
    ex = crypto.create_account(conn, "ANON Exchange", kind=crypto.CRYPTO_KIND_EXCHANGE)
    recs = coinbase_csv.parse_coinbase(FIXTURE.read_text(encoding="utf-8"))
    entries = import_review.build_exchange_review(conn, ex, recs[:1])
    import_review.persist_entries(conn, ex, entries)
    [pending] = import_review.load_pending(conn, ex)
    assert pending.mapped.time == "10:00:00"
    txn_id = import_review.save_new(conn, ex, pending.mapped, review_id=pending.review_id)
    assert crypto.get_event(conn, txn_id)["time"] == "10:00:00"


def test_a_reimport_through_review_fills_posted_and_pending_rows(conn):
    ex = crypto.create_account(conn, "ANON Exchange", kind=crypto.CRYPTO_KIND_EXCHANGE)
    recs = coinbase_csv.parse_coinbase(FIXTURE.read_text(encoding="utf-8"))
    for r in recs[:2]:
        r.time = ""
    entries = import_review.build_exchange_review(conn, ex, recs[:2])
    import_review.persist_entries(conn, ex, entries)
    first = import_review.load_pending(conn, ex)[0]
    posted = import_review.save_new(conn, ex, first.mapped, review_id=first.review_id)

    fresh = coinbase_csv.parse_coinbase(FIXTURE.read_text(encoding="utf-8"))[:2]
    assert import_review.fill_crypto_times(conn, ex, fresh) == 2
    assert crypto.get_event(conn, posted)["time"] == fresh[0].time
    [still_pending] = import_review.load_pending(conn, ex)
    assert still_pending.mapped.time == fresh[1].time
