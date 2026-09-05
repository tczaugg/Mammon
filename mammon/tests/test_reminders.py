"""Scheduled definitions as REMINDERS (roadmap item 5, mammon.scheduled):
the unified frequency list, occurrences, due status, Enter and Skip, transfer
and income definitions, remind-only definitions, and a per-definition lead."""
from __future__ import annotations

import pytest

from mammon import db, ledger, scheduled


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "reminders.db")
    yield c
    c.close()


def _placeholders(conn, account_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=? AND scheduled=1",
        (account_id,)).fetchone()["c"]


def test_semimonthly_and_semiannual_advance():
    adv = scheduled.advance_date
    assert adv("2026-01-01", "semimonthly") == "2026-01-16"
    assert adv("2026-01-16", "semimonthly") == "2026-02-01"
    assert adv("2026-01-15", "semimonthly") == "2026-01-31"     # 15th and last day
    assert adv("2026-01-31", "semimonthly") == "2026-02-15"
    assert adv("2026-02-15", "semimonthly") == "2026-02-28"
    assert adv("2026-02-28", "semimonthly") == "2026-03-15"
    assert adv("2026-01-10", "semimonthly") == "2026-01-25"
    assert adv("2026-01-25", "semimonthly") == "2026-02-10"
    assert adv("2026-01-31", "semiannual") == "2026-07-31"
    assert adv("2026-08-31", "semiannual") == "2027-02-28"
    assert set(scheduled.FREQUENCIES) >= {"semimonthly", "semiannual"}


def test_occurrences_status_and_days_until():
    occ = scheduled.occurrences("2026-01-05", "monthly", "2026-02-01", "2026-04-30")
    assert occ == ["2026-02-05", "2026-03-05", "2026-04-05"]
    assert scheduled.occurrences("2026-06-01", "weekly", "2026-01-01", "2026-05-31") == []
    assert scheduled.days_until("2026-01-10", "2026-01-07") == 3
    assert scheduled.days_until("2026-01-05", "2026-01-07") == -2
    st = scheduled.reminder_status
    assert st("2026-01-05", "2026-01-07") == scheduled.OVERDUE
    assert st("2026-01-07", "2026-01-07") == scheduled.DUE_TODAY
    assert st("2026-01-12", "2026-01-07") == scheduled.DUE_SOON          # within 5
    assert st("2026-01-13", "2026-01-07") == scheduled.UPCOMING
    assert st("2026-01-13", "2026-01-07", lead_days=10) == scheduled.DUE_SOON


def test_list_reminders_and_due_counts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=500_00)
    scheduled.add_scheduled(conn, chk, payee="Late Co", amount=-10_00, frequency="monthly",
                            next_date="2026-01-01")
    scheduled.add_scheduled(conn, chk, payee="Soon Co", amount=-20_00, frequency="monthly",
                            next_date="2026-01-10")
    scheduled.add_scheduled(conn, chk, payee="Far Co", amount=-30_00, frequency="monthly",
                            next_date="2026-02-01", lead_days=30)          # its own lead
    scheduled.add_scheduled(conn, chk, payee="Off Co", amount=-40_00, frequency="monthly",
                            next_date="2026-01-08", active=False)
    rem = scheduled.list_reminders(conn, "2026-01-07")
    assert [(r["payee"], r["status"], r["days_until"]) for r in rem] == [
        ("Late Co", "overdue", -6), ("Soon Co", "due_soon", 3), ("Far Co", "due_soon", 25)]
    assert rem[2]["lead"] == 30 and rem[1]["lead"] == scheduled.DEFAULT_LEAD_DAYS
    assert scheduled.due_counts(conn, "2026-01-07") == {
        "overdue": 1, "due_today": 0, "due_soon": 2, "upcoming": 0}


def test_skip_and_enter_next(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=500_00)
    cat = ledger.resolve_category(conn, "Utilities")
    sid = scheduled.add_scheduled(conn, chk, payee="Power Co", amount=-60_00,
                                  frequency="monthly", next_date="2026-01-10",
                                  category_id=cat, memo="electric")
    assert scheduled.skip_next(conn, sid) == "2026-02-10"
    assert _placeholders(conn, chk) == 0
    tid = scheduled.enter_next(conn, sid)                          # posted, not a placeholder
    row = ledger.get_transaction(conn, tid)
    assert (row["date"], row["amount"], row["scheduled"], row["category_id"]) == \
        ("2026-02-10", -60_00, 0, cat)
    assert scheduled.get_scheduled(conn, sid)["next_date"] == "2026-03-10"
    tid2 = scheduled.enter_next(conn, sid, placeholder=True, date="2026-03-12")
    assert ledger.get_transaction(conn, tid2)["scheduled"] == 1
    assert ledger.get_transaction(conn, tid2)["date"] == "2026-03-12"


def test_transfer_definition_pre_enters_both_legs(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=500_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    cat = ledger.resolve_category(conn, "Ignored")
    sid = scheduled.add_scheduled(conn, chk, payee="Auto-save", amount=-100_00,
                                  frequency="monthly", next_date="2026-01-15",
                                  category_id=cat, transfer_account_id=sav)
    d = scheduled.get_scheduled(conn, sid)
    assert d["kind"] == "transfer" and d["category_id"] is None
    assert d["category_label"] == "[Savings]" and d["transfer_account_name"] == "Savings"
    ids = scheduled.ensure_due_pre_entries(conn, sid, "2026-01-12")
    assert len(ids) == 1
    own = ledger.get_transaction(conn, ids[0])
    assert own["account_id"] == chk and own["amount"] == -100_00 and own["scheduled"] == 1
    mirror = ledger.get_transaction(conn, own["transfer_pair_id"])
    assert mirror["account_id"] == sav and mirror["amount"] == 100_00 and mirror["scheduled"] == 1
    # Idempotent, and the definition moved on.
    assert scheduled.ensure_due_pre_entries(conn, sid, "2026-01-12") == []
    assert scheduled.get_scheduled(conn, sid)["next_date"] == "2026-02-15"
    # Income the other way: positive amount = money INTO the definition's account.
    sid2 = scheduled.add_scheduled(conn, chk, payee="From savings", amount=50_00,
                                   frequency="monthly", next_date="2026-01-20",
                                   transfer_account_id=sav)
    tid = scheduled.enter_next(conn, sid2)
    own = ledger.get_transaction(conn, tid)
    assert own["account_id"] == chk and own["amount"] == 50_00 and own["scheduled"] == 0
    with pytest.raises(ValueError):
        scheduled.add_scheduled(conn, chk, payee="x", amount=-1, frequency="monthly",
                                next_date="2026-01-01", transfer_account_id=chk)


def test_income_definition_and_kind(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    sid = scheduled.add_scheduled(conn, chk, payee="Employer", amount=2500_00,
                                  frequency="semimonthly", next_date="2026-01-15")
    assert scheduled.get_scheduled(conn, sid)["kind"] == "income"
    ids = scheduled.ensure_due_pre_entries(conn, sid, "2026-01-28", lead_days=5)
    assert [ledger.get_transaction(conn, t)["date"] for t in ids] == ["2026-01-15", "2026-01-31"]


def test_remind_only_definitions_are_never_pre_entered(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    sid = scheduled.add_scheduled(conn, chk, payee="Manual Co", amount=-9_00,
                                  frequency="monthly", next_date="2026-01-05",
                                  auto_enter=False)
    assert scheduled.ensure_due_pre_entries(conn, sid, "2026-01-05") == []
    assert scheduled.generate_all_due(conn, "2026-01-05") == []
    assert scheduled.get_scheduled(conn, sid)["next_date"] == "2026-01-05"   # still waiting
    scheduled.update_scheduled(conn, sid, auto_enter=True, lead_days=0)
    assert len(scheduled.generate_all_due(conn, "2026-01-05")) == 1


def test_per_definition_lead_days_governs_generation(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    sid = scheduled.add_scheduled(conn, chk, payee="Early Co", amount=-9_00,
                                  frequency="monthly", next_date="2026-01-20",
                                  lead_days=20)
    assert len(scheduled.ensure_due_pre_entries(conn, sid, "2026-01-02")) == 1   # 18 days out
    sid2 = scheduled.add_scheduled(conn, chk, payee="Late Co", amount=-9_00,
                                   frequency="monthly", next_date="2026-01-20")
    assert scheduled.ensure_due_pre_entries(conn, sid2, "2026-01-02") == []      # default 5
