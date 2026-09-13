"""mammon.projection: projected balances from entered rows, reminder
occurrences and loan schedules, without double counting (roadmap item 5)."""
from __future__ import annotations

import pytest

from mammon import db, ledger, loans, loans_schedule, projection, scheduled


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "projection.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


def test_month_range():
    assert projection.month_range(2026, 2) == ("2026-02-01", "2026-02-28")
    assert projection.month_range(2026, 12) == ("2026-12-01", "2026-12-31")


def test_events_and_daily_balances(conn, accounts):
    chk, sav = accounts
    # History before the window is the opening balance; a placeholder inside
    # the window is an entered event (pending), not a second occurrence.
    ledger.add_transaction(conn, chk, "2026-01-05", -100_00, payee="Grocer")
    ledger.add_transaction(conn, chk, "2026-01-12", -40_00, payee="Cafe")     # entered, future
    sid = scheduled.add_scheduled(conn, chk, payee="Power Co", amount=-60_00,
                                  frequency="monthly", next_date="2026-01-10")
    scheduled.ensure_due_pre_entries(conn, sid, "2026-01-08")               # placeholder Jan 10
    scheduled.add_scheduled(conn, chk, payee="Employer", amount=2000_00,
                            frequency="semimonthly", next_date="2026-01-15")
    scheduled.add_scheduled(conn, chk, payee="Auto-save", amount=-500_00,
                            frequency="monthly", next_date="2026-01-20",
                            transfer_account_id=sav)
    p = projection.project(conn, [chk], "2026-01-08", "2026-01-31")
    assert p.opening == 900_00
    by = {d.date: d for d in p.days}
    assert [(e.payee, e.amount, e.source, e.pending) for e in by["2026-01-10"].events] == [
        ("Power Co", -60_00, "entered", True)]
    assert by["2026-01-10"].balance == 840_00
    assert by["2026-01-12"].balance == 800_00
    assert [(e.payee, e.source) for e in by["2026-01-15"].events] == [("Employer", "scheduled")]
    assert by["2026-01-15"].balance == 2800_00
    assert by["2026-01-20"].balance == 2300_00
    assert by["2026-01-31"].events[0].payee == "Employer"           # 15th and last day
    assert p.closing == 4300_00 and p.low == 800_00 and p.low_date == "2026-01-12"
    assert len(p.days) == 24
    # The next Power Co occurrence (Feb 10) is projected, not the Jan one again.
    feb = projection.project(conn, [chk], "2026-02-01", "2026-02-28")
    power = [e for e in feb.events() if e.payee == "Power Co"]
    assert [e.date for e in power] == ["2026-02-10"] and power[0].source == "scheduled"
    # The transfer lands on the savings side with the opposite sign.
    s = projection.project(conn, [sav], "2026-01-01", "2026-01-31")
    assert [(e.date, e.amount) for e in s.events()] == [("2026-01-20", 500_00)]
    # Both accounts together: the transfer nets to zero.
    both = projection.project(conn, [chk, sav], "2026-01-08", "2026-01-31")
    assert both.closing == 4300_00 + 500_00 and both.opening == 900_00


def test_loan_payments_project_on_the_loan_without_double_counting(conn, accounts):
    loan = ledger.create_account(conn, "Mortgage", "liability", opening_balance=0)
    loans.set_loan_params(conn, loan, original_principal=100000_00,
                          origination_date="2025-12-01", term_months=360,
                          payment_amount=600_00, interval="monthly",
                          rates=[("2025-12-01", "6")])
    p = projection.project(conn, [loan], "2026-01-01", "2026-02-28")
    assert [(e.date, e.source, e.amount) for e in p.events()] == [
        ("2026-01-01", "loan", 600_00), ("2026-02-01", "loan", 600_00)]
    # Once January's payment is pre-entered it is an entered event, not a loan one.
    loans_schedule.ensure_pending_payments(conn, loan, "2026-01-01", lead_days=0)
    p2 = projection.project(conn, [loan], "2026-01-01", "2026-02-28")
    assert [(e.date, e.source) for e in p2.events()] == [("2026-01-01", "entered"),
                                                         ("2026-02-01", "loan")]
    assert p2.closing == p.closing


def test_reversed_range_and_empty_accounts(conn, accounts):
    chk, _ = accounts
    p = projection.project(conn, [chk], "2026-01-10", "2026-01-01")
    assert p.start == "2026-01-01" and p.end == "2026-01-10" and len(p.days) == 10
    assert projection.projected_events(conn, [], "2026-01-01", "2026-01-31") == []


# --- a reminder and a prediction of the same bill are one event ------------
#
# Reported from the calendar: one entry for a card payment in the month that
# already had the payment posted, two in the next month. The definition's
# stored payee text no longer matches the bank text on the rows (a rename
# landed after the definition was written), so predict_recurring's "covered"
# guard misses it, and is_entered only hides a prediction in a month that has
# a posted row. projected_events reconciles the two sources at the merge.

def _drifted_card_world(conn):
    """A monthly card payment whose ledger rows and whose definition normalize
    to DIFFERENT payee keys, with history enough to be predicted."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=5000_00)
    for month in (6, 7, 8, 9, 10):
        ledger.add_transaction(conn, chk, f"2026-{month:02d}-01", -250_00,
                               payee="Examplebank Card Pmt 4821")
    # The definition the user wrote, under the renamed display payee.
    scheduled.add_scheduled(conn, chk, payee="Example Bank Card", amount=-250_00,
                            frequency="monthly", next_date="2026-11-01")
    return chk


def test_prediction_does_not_duplicate_a_reminder_whose_payee_drifted(conn):
    from mammon import predictions

    chk = _drifted_card_world(conn)
    today = "2026-10-15"
    # The guard that was supposed to prevent this really does miss: the
    # definition's key and the rows' key are different strings.
    assert predictions.payee_key("Example Bank Card") != \
        predictions.payee_key("Examplebank Card Pmt 4821")
    pred = [p for p in predictions.predict_recurring(conn, today, account_ids=[chk])
            if p.amount == -250_00]
    assert len(pred) == 1 and pred[0].next_date == "2026-11-01"

    # October: the payment is posted, so exactly one event -- this always held.
    oct_events = projection.projected_events(conn, [chk], "2026-10-01", "2026-10-31",
                                             today=today)
    card = [e for e in oct_events if e.amount == -250_00]
    assert [(e.date, e.source) for e in card] == [("2026-10-01", "entered")]

    # November: nothing posted yet. The reminder survives, the prediction of
    # the same bill on the same day is dropped (this used to be two rows).
    nov_events = projection.projected_events(conn, [chk], "2026-11-01", "2026-11-30",
                                             today=today)
    card = [e for e in nov_events if e.amount == -250_00]
    assert [(e.date, e.source) for e in card] == [("2026-11-01", "scheduled")]
    assert not [e for e in nov_events if e.source == "predicted"]


def test_prediction_near_a_reminder_survives_when_neither_payee_nor_amount_match(conn):
    """The negative control: two genuinely different bills on one day, and a
    prediction that merely shares an amount with a reminder a fortnight away."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=5000_00)
    # Predicted: a monthly membership on the 1st.
    for month in (6, 7, 8, 9, 10):
        ledger.add_transaction(conn, chk, f"2026-{month:02d}-01", -45_00,
                               payee="Neighborhood Gym")
    # Reminder: a different payee, a different amount, the same day.
    scheduled.add_scheduled(conn, chk, payee="Example Landlord", amount=-900_00,
                            frequency="monthly", next_date="2026-11-01")
    # Reminder: the SAME amount as the prediction, but three weeks away.
    scheduled.add_scheduled(conn, chk, payee="Example Storage", amount=-45_00,
                            frequency="monthly", next_date="2026-11-22")
    events = projection.projected_events(conn, [chk], "2026-11-01", "2026-11-30",
                                         today="2026-10-15")
    assert [(e.date, e.payee, e.amount, e.source) for e in events] == [
        ("2026-11-01", "Example Landlord", -900_00, "scheduled"),
        ("2026-11-01", "Neighborhood Gym", -45_00, "predicted"),
        ("2026-11-22", "Example Storage", -45_00, "scheduled"),
    ]
