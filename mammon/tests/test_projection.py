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
