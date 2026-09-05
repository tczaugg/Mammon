"""Accepting a match fills a MISSING price and never replaces one that is there.

Accepting a MATCHING investment row used to stamp ``fitid`` and nothing else, so
a download carrying a price for a register row that had none simply threw it
away. For a plan fund quoted nowhere public that download is the only price that
will ever exist, and the result was a dormant 401(k) holding its last
contribution price for six years, then revaluing the whole position in one day
when a price finally landed.

The other half is the restraint: a match says "this register line IS that
download", not "restate it". Overwriting a price the user already has is the
same complaint people make about Quicken changing a date on accept, so empty
means missing -- never wrong.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, import_review, investments, ledger


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "fill.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Employer 401k", "investment")


def _existing(conn, account, **kw):
    """A register row the download will later match against."""
    fields = dict(date="2026-01-07", action="ShrsOut",
                  symbol="LARGE CAP EQUITY INDEX", quantity="-0.007",
                  amount=-101, memo="RECORDKEEPING FEE")
    fields.update(kw)
    return investments.record_investment(
        conn, account, fields.pop("date"), fields.pop("action"), **fields)


def _entry(conn, account, txn_id, price=None, date="2026-01-07"):
    """A MATCHING investment review entry pointing at ``txn_id``.

    Built through ``build_review`` (so the entry is the real shape) and then
    pointed at the matched row. The investment fields are set explicitly because
    a raw dict's key names are the download adapter's business, not this test's
    -- what is under test is what ``accept_match`` does with a MATCHING
    investment entry."""
    rows = [{"transactionId": "FEE-1", "date": date, "amount": "-1.01",
             "description": "RECORDKEEPING FEE"}]
    entry = import_review.build_review(conn, account, rows)[0]
    m = entry.mapped
    m.is_investment = True
    m.action = "ShrsOut"
    m.symbol = "LARGE CAP EQUITY INDEX"
    m.quantity = "-0.007"
    m.price = price or ""
    entry.matched_txn_id = txn_id
    entry.label = "MATCHING"
    return entry


# ---------------------------------------------------------------------------
def test_an_empty_price_is_filled_from_the_rows_own_value(conn, account):
    """0.007 shares for $1.01 -> $144.29, the only price this fund will get."""
    txn = _existing(conn, account)
    entry = _entry(conn, account, txn)
    import_review.accept_match(conn, entry)
    row = conn.execute("SELECT price FROM investment_transactions WHERE id=?",
                       (txn,)).fetchone()
    assert row["price"] == "144.285714"


def test_the_filled_price_reaches_price_history_with_its_interval(conn, account):
    txn = _existing(conn, account)
    import_review.accept_match(conn, _entry(conn, account, txn))
    (date, close, lo, hi), = investments.price_history_bounds(
        conn, "LARGE CAP EQUITY INDEX")
    assert (date, close) == ("2026-01-07", Decimal("144.285714"))
    assert lo is not None and hi is not None and lo < close < hi


def test_an_existing_price_is_NEVER_replaced(conn, account):
    """The Quicken-changes-your-date complaint. A match confirms a row; it does
    not restate figures the user already has."""
    txn = _existing(conn, account, price="150.00")
    import_review.accept_match(conn, _entry(conn, account, txn, price="144.285714"))
    row = conn.execute("SELECT price FROM investment_transactions WHERE id=?",
                       (txn,)).fetchone()
    # record_investment normalises Decimal text, so compare by value
    assert Decimal(row["price"]) == Decimal("150")


def test_nothing_is_filled_when_there_is_nothing_to_derive(conn, account):
    """No amount -> no quotient -> no invented price."""
    txn = _existing(conn, account, amount=None)
    import_review.accept_match(conn, _entry(conn, account, txn))
    row = conn.execute("SELECT price FROM investment_transactions WHERE id=?",
                       (txn,)).fetchone()
    assert row["price"] in (None, "")
    assert investments.price_history(conn, "LARGE CAP EQUITY INDEX") == []


def test_a_price_the_download_states_wins_over_the_quotient(conn, account):
    """A stated price is exact; deriving one when the source gave us a real one
    would substitute a rounded quotient for a fact."""
    txn = _existing(conn, account)
    import_review.accept_match(conn, _entry(conn, account, txn, price="170.16"))
    row = conn.execute("SELECT price FROM investment_transactions WHERE id=?",
                       (txn,)).fetchone()
    assert row["price"] == "170.16"
    (_d, _c, lo, hi), = investments.price_history_bounds(
        conn, "LARGE CAP EQUITY INDEX")
    assert lo is None and hi is None, "a stated price carries no interval"


def test_reverting_puts_a_filled_price_back_to_empty(conn, account):
    txn = _existing(conn, account)
    entry = _entry(conn, account, txn)
    prior = import_review.accept_match(conn, entry)
    assert conn.execute("SELECT price FROM investment_transactions WHERE id=?",
                        (txn,)).fetchone()["price"] == "144.285714"
    import_review.revert_match(conn, entry, prior)
    assert conn.execute("SELECT price FROM investment_transactions WHERE id=?",
                        (txn,)).fetchone()["price"] is None


def test_reverting_leaves_an_untouched_price_alone(conn, account):
    """Undo must restore what the accept changed and nothing else."""
    txn = _existing(conn, account, price="150.00")
    entry = _entry(conn, account, txn)
    prior = import_review.accept_match(conn, entry)
    import_review.revert_match(conn, entry, prior)
    assert Decimal(conn.execute(
        "SELECT price FROM investment_transactions WHERE id=?",
        (txn,)).fetchone()["price"]) == Decimal("150")


def test_accept_still_stamps_the_source_id(conn, account):
    """The behaviour that was already there must survive the addition."""
    txn = _existing(conn, account)
    import_review.accept_match(conn, _entry(conn, account, txn))
    row = conn.execute("SELECT fitid FROM investment_transactions WHERE id=?",
                       (txn,)).fetchone()
    assert row["fitid"] == "FEE-1"


def test_filling_closes_the_gap_that_caused_the_jump(conn, account):
    """The end-to-end point: with the fill, a year of accepted fee rows prices
    the fund through the period instead of leaving one cliff at the end."""
    investments.record_investment(conn, account, "2020-01-01", "Buy",
                                  symbol="LARGE CAP EQUITY INDEX",
                                  quantity="1000", price="54.82", amount=-5482000)
    for date, qty, cents in (("2024-01-05", "-0.010", -80),
                             ("2025-01-06", "-0.009", -90),
                             ("2026-01-07", "-0.007", -101)):
        txn = _existing(conn, account, date=date, quantity=qty, amount=cents)
        import_review.accept_match(conn, _entry(conn, account, txn, date=date))
    # Three accepted fee rows -> three prices spread through the gap, instead of
    # one cliff when a price finally appears at the end.
    dates = [d for d, _p in investments.price_history(conn, "LARGE CAP EQUITY INDEX")]
    assert dates == ["2024-01-05", "2025-01-06", "2026-01-07"]
    # and each carries its own interval, so the chart shows how well each is known
    rows = investments.price_history_bounds(conn, "LARGE CAP EQUITY INDEX")
    assert all(lo is not None and hi is not None for _d, _c, lo, hi in rows)
