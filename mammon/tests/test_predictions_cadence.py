"""One payee that is several interleaved bills (mammon.predictions sub-streams).

The calendar defect these cover: a phone carrier paid for four family members
in rotation posts about once a week, so read as a single series the payee is
"weekly" at one arbitrary amount -- four estimates a month that match no real
bill. Each line is actually billed every four weeks, so the payee's rows are
clustered into sub-streams (amount first, memo/category as tie-breakers) and a
cadence is fitted per sub-stream.

The regression that matters most is the other direction: an ordinary bill, a
bill whose amount drifts, and a bill whose amount went up for good must each
still produce exactly ONE prediction, with the cadence and amount they had
before sub-streams existed.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from mammon import db, ledger, predictions, projection, scheduled

TODAY = "2026-09-02"
PAYEE = "Talkline Mobile"          # invented: four family lines on one bill payee
ROTATION_START = "2026-05-01"


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "cadence.db")
    yield c
    c.close()


def _rotation(conn, aid, payee, amounts, *, start=ROTATION_START, cycles=4, memos=None):
    """Postings one week apart cycling through ``amounts``: every line is billed
    every fourth week, while the payee as a whole posts weekly."""
    d = _dt.date.fromisoformat(start)
    for i in range(cycles * len(amounts)):
        k = i % len(amounts)
        ledger.add_transaction(conn, aid, d.isoformat(), amounts[k], payee=payee,
                               memo=None if memos is None else memos[k])
        d += _dt.timedelta(days=7)


def _for(conn, payee, today=TODAY):
    return [p for p in predictions.predict_recurring(conn, today) if p.payee == payee]


# ---------------------------------------------------------------------------
# the rotation
# ---------------------------------------------------------------------------
def test_a_rotation_payee_is_read_as_one_stream_per_bill(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    _rotation(conn, chk, PAYEE, [-42_00, -48_00, -55_00, -63_00])
    dates = [r["date"] for r in conn.execute(
        "SELECT date FROM transactions WHERE payee=? ORDER BY date", (PAYEE,))]
    # The defect, stated: the payee's raw dates really do look weekly.
    assert predictions.fit_period(dates) == "weekly"

    preds = _for(conn, PAYEE)
    assert len(preds) == 4                       # one per line, not one weekly payment
    assert {p.frequency for p in preds} == {"monthly"}
    assert not any(p.frequency == "weekly" for p in preds)
    assert {p.count for p in preds} == {4}
    assert {p.varies for p in preds} == {False}   # each line's own steady amount
    assert [(p.amount, p.next_date) for p in sorted(preds, key=lambda p: p.amount)] == [
        (-63_00, "2026-09-14"), (-55_00, "2026-09-07"),
        (-48_00, "2026-09-30"), (-42_00, "2026-09-24")]

    # A month of the calendar: four estimates, one per real bill, each its own
    # amount. Read as one weekly series this month held FIVE estimates (the
    # weekly grid falls on the 2nd, 9th, 16th, 23rd and 30th) all at -42.00.
    events = projection.projected_events(conn, [chk], "2026-10-01", "2026-10-31", today=TODAY)
    assert [(e.date, e.amount) for e in events if e.source == projection.PREDICTED] == [
        ("2026-10-07", -55_00), ("2026-10-14", -63_00),
        ("2026-10-24", -42_00), ("2026-10-30", -48_00)]
    assert sum(e.amount for e in events if e.source == projection.PREDICTED) == \
        -(42_00 + 48_00 + 55_00 + 63_00)


def test_lines_that_cost_the_same_are_told_apart_by_the_memo(conn):
    """Amount alone cannot separate four lines on one plan, so memo (then
    category) breaks the tie: four monthly estimates on their own dates rather
    than one weekly one."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    memos = [f"line ANON-{n}" for n in (1, 2, 3, 4)]
    _rotation(conn, chk, PAYEE, [-45_00] * 4, memos=memos)
    preds = _for(conn, PAYEE)
    assert len(preds) == 4
    assert {(p.frequency, p.amount, p.count) for p in preds} == {("monthly", -45_00, 4)}
    assert {p.next_date for p in preds} == {"2026-09-07", "2026-09-14",
                                            "2026-09-24", "2026-09-30"}


def test_a_stream_with_too_few_occurrences_is_left_out_rather_than_guessed(conn):
    """Three of the four lines have a history; the fourth was added last month.
    The three are predicted on their own cadence and the newcomer is not
    predicted at all -- the minimum-occurrence bar still means something."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    amounts = [-42_00, -48_00, -55_00, -63_00]
    d = _dt.date.fromisoformat(ROTATION_START)
    for i in range(4 * len(amounts)):
        amt = amounts[i % len(amounts)]
        if amt != -63_00 or i == 15:      # the fourth line posted once, last month
            ledger.add_transaction(conn, chk, d.isoformat(), amt, payee=PAYEE)
        d += _dt.timedelta(days=7)

    preds = _for(conn, PAYEE)
    assert sorted(p.amount for p in preds) == [-55_00, -48_00, -42_00]
    assert {p.frequency for p in preds} == {"monthly"}
    assert {p.count for p in preds} == {4}


# ---------------------------------------------------------------------------
# the control: one payee, one series, unchanged
# ---------------------------------------------------------------------------
def test_ordinary_single_stream_bills_are_unchanged(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    util = ledger.resolve_category(conn, "Utilities")
    # A plain monthly bill of a constant amount.
    for month in (4, 5, 6, 7, 8):
        ledger.add_transaction(conn, chk, f"2026-{month:02d}-12", -110_00,
                               payee="Northshore Power", category_id=util)
    # A monthly bill whose amount drifts: clustering by amount makes singletons
    # of it, none of which earns a prediction, so it is read as one series.
    for date, amt in (("2026-06-03", -80_00), ("2026-07-02", -85_00), ("2026-08-04", -82_00)):
        ledger.add_transaction(conn, chk, date, amt, payee="Harbor Water")
    # A rent that went up for good: the old amount is a cluster too, but a
    # stale one, and must not be predicted beside the new one.
    for date, amt in (("2026-04-01", -1500_00), ("2026-05-01", -1500_00),
                      ("2026-06-01", -1500_00), ("2026-07-01", -1600_00),
                      ("2026-08-01", -1600_00)):
        ledger.add_transaction(conn, chk, date, amt, payee="Cedar Lane Rental")

    power, water, rent = (_for(conn, "Northshore Power"), _for(conn, "Harbor Water"),
                          _for(conn, "Cedar Lane Rental"))
    assert [(p.frequency, p.amount, p.count, p.category_id) for p in power] == \
        [("monthly", -110_00, 5, util)]
    assert [(p.frequency, p.amount, p.count) for p in water] == [("monthly", -82_00, 3)]
    assert [(p.frequency, p.amount, p.count) for p in rent] == [("monthly", -1600_00, 5)]
    # One estimate each in a projected month, on the expected day.
    events = projection.projected_events(conn, [chk], "2026-10-01", "2026-10-31", today=TODAY)
    assert [(e.date, e.payee, e.amount) for e in events
            if e.source == projection.PREDICTED] == [
        ("2026-10-01", "Cedar Lane Rental", -1600_00),
        ("2026-10-04", "Harbor Water", -82_00),
        ("2026-10-12", "Northshore Power", -110_00)]


def test_split_substreams_keeps_one_series_whole(conn):
    """The splitter itself: near-equal amounts cluster together, and a payee
    that is one series stays one list (the anchor is the amount that opened the
    cluster, so a ladder of nearly-equal amounts cannot chain into one)."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    for date, amt in (("2026-06-01", -3000_00), ("2026-07-01", -3000_25),
                      ("2026-08-01", -2999_80)):
        ledger.add_transaction(conn, chk, date, amt, payee="Bayfield Mortgage Svc")
    rows = list(conn.execute("SELECT * FROM transactions WHERE payee='Bayfield Mortgage Svc'"))
    assert [len(c) for c in predictions.split_substreams(rows)] == [3]
    assert predictions._near_amount(-3000_25, -3000_00)
    assert not predictions._near_amount(-4200, -4800)      # two different lines
    assert not predictions._near_amount(4200, -4200)       # a refund is not the bill


# ---------------------------------------------------------------------------
# end to end: the calendar month, beside a reminder for one of the lines
# ---------------------------------------------------------------------------
def test_projected_month_holds_one_event_per_real_bill_beside_a_reminder(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=2000_00)
    _rotation(conn, chk, PAYEE, [-42_00, -48_00, -55_00, -63_00])
    # The user made ONE of the four lines a definition, and its payee text
    # drifted from what the bank posts, so only the amount identifies it. The
    # prediction of that same line must be dropped at the merge point, and the
    # other three must survive.
    scheduled.add_scheduled(conn, chk, payee=PAYEE + " Autopay", amount=-55_00,
                            frequency="monthly", next_date="2026-10-07")
    events = projection.projected_events(conn, [chk], "2026-10-01", "2026-10-31", today=TODAY)
    assert [(e.date, e.amount, e.source) for e in events] == [
        ("2026-10-07", -55_00, projection.SCHEDULED),
        ("2026-10-14", -63_00, projection.PREDICTED),
        ("2026-10-24", -42_00, projection.PREDICTED),
        ("2026-10-30", -48_00, projection.PREDICTED)]
    assert len(events) == 4                      # one per real bill, no duplicate
