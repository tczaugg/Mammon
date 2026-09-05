"""Tests for mammon.scheduled: generalized recurring-payment definitions, their
pre-entry generation, that loan schedules surface in the manager, and -- the
crux -- that definitions AND their generated pre-entries survive a QIF re-import
(neither duplicated nor lost)."""
from __future__ import annotations

import pytest

from mammon import db, importers, ledger, loans, scheduled


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


QIF_BANK = """!Account
NChecking
TBank
^
!Type:Bank
D01/05'26
T-25.00
PSafeway
LGroceries
^
D01/10'26
T200.00
PPaycheck
LSalary
^
"""


def _checking(conn):
    return conn.execute(
        "SELECT id FROM accounts WHERE name='Checking'").fetchone()["id"]


def _scheduled_txn_count(conn, account_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=? AND scheduled=1",
        (account_id,)).fetchone()["c"]


# ---------------------------------------------------------------------------
# frequency arithmetic
# ---------------------------------------------------------------------------
def test_advance_date_by_frequency():
    assert scheduled.advance_date("2026-01-15", "weekly") == "2026-01-22"
    assert scheduled.advance_date("2026-01-15", "biweekly") == "2026-01-29"
    assert scheduled.advance_date("2026-01-15", "monthly") == "2026-02-15"
    assert scheduled.advance_date("2026-01-15", "quarterly") == "2026-04-15"
    assert scheduled.advance_date("2026-01-15", "annual") == "2027-01-15"
    # month-end clamps: Jan 31 + 1 month -> Feb 28 (2026 is not a leap year)
    assert scheduled.advance_date("2026-01-31", "monthly") == "2026-02-28"
    with pytest.raises(ValueError):
        scheduled.advance_date("2026-01-15", "hourly")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def test_crud_roundtrip(conn):
    acct = ledger.create_account(conn, "Checking", "cash")
    cat = ledger.resolve_category(conn, "Subscriptions")
    sid = scheduled.add_scheduled(
        conn, acct, payee="Netflix", amount=-15_99, frequency="monthly",
        next_date="2026-02-01", category_id=cat, memo="streaming")

    d = scheduled.get_scheduled(conn, sid)
    assert d["payee"] == "Netflix" and d["amount"] == -15_99
    assert d["frequency"] == "monthly" and d["next_date"] == "2026-02-01"
    assert d["category_label"] == "Subscriptions" and d["active"] is True
    assert d["source"] == "manual"

    scheduled.update_scheduled(conn, sid, amount=-17_99, active=False)
    d = scheduled.get_scheduled(conn, sid)
    assert d["amount"] == -17_99 and d["active"] is False

    assert [r["id"] for r in scheduled.list_scheduled(conn)] == [sid]
    assert scheduled.list_scheduled(conn, active_only=True) == []

    scheduled.delete_scheduled(conn, sid)
    assert scheduled.get_scheduled(conn, sid) is None
    assert scheduled.list_scheduled(conn) == []


def test_unknown_frequency_rejected(conn):
    acct = ledger.create_account(conn, "Checking", "cash")
    with pytest.raises(ValueError):
        scheduled.add_scheduled(conn, acct, payee="X", amount=-1_00,
                                frequency="whenever", next_date="2026-02-01")


# ---------------------------------------------------------------------------
# pre-entry generation
# ---------------------------------------------------------------------------
def test_create_pending_is_idempotent(conn):
    acct = ledger.create_account(conn, "Checking", "cash")
    sid = scheduled.add_scheduled(conn, acct, payee="Cable", amount=-89_00,
                                  frequency="monthly", next_date="2026-02-01")
    pid = scheduled.create_pending_from_definition(conn, sid)
    row = ledger.get_transaction(conn, pid)
    assert row["scheduled"] == 1 and row["cleared"] == 0 and row["amount"] == -89_00
    assert row["payee"] == "Cable"
    # a second call for the same account+date+amount returns the same row
    assert scheduled.create_pending_from_definition(conn, sid, "2026-02-01") == pid
    assert _scheduled_txn_count(conn, acct) == 1


def test_ensure_due_advances_next_date_and_is_idempotent(conn):
    acct = ledger.create_account(conn, "Checking", "cash")
    sid = scheduled.add_scheduled(conn, acct, payee="Gym", amount=-40_00,
                                  frequency="monthly", next_date="2026-02-01")
    ids = scheduled.ensure_due_pre_entries(conn, sid, "2026-01-28", lead_days=7)
    assert len(ids) == 1
    assert _scheduled_txn_count(conn, acct) == 1
    # next_date rolled forward to the following month
    assert scheduled.get_scheduled(conn, sid)["next_date"] == "2026-03-01"
    # re-running over the same window creates no new pre-entries
    scheduled.ensure_due_pre_entries(conn, sid, "2026-01-28", lead_days=7)
    assert _scheduled_txn_count(conn, acct) == 1


def test_inactive_definition_generates_nothing(conn):
    acct = ledger.create_account(conn, "Checking", "cash")
    sid = scheduled.add_scheduled(conn, acct, payee="Old", amount=-5_00,
                                  frequency="monthly", next_date="2026-02-01",
                                  active=False)
    assert scheduled.ensure_due_pre_entries(conn, sid, "2026-02-01") == []
    assert _scheduled_txn_count(conn, acct) == 0


# ---------------------------------------------------------------------------
# loans surface in the manager list + generate_all_due covers them
# ---------------------------------------------------------------------------
def _setup_loan(conn):
    aid = ledger.create_account(conn, "Home Mortgage", "liability",
                                opening_balance=-300_000_00)
    loans.set_loan_params(
        conn, aid, original_principal=300_000_00, term_months=360,
        payment_amount=1998_65, origination_date="2024-01-01", interval="monthly",
        rates=[("2024-02-01", "6.0")], extras=[("Escrow", 200_00, "Taxes")])
    return aid


def test_loan_schedule_surfaces_as_readonly_row(conn):
    aid = _setup_loan(conn)
    rows = scheduled.list_loan_schedules(conn, on_or_after="2024-01-15")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "loan" and row["account_id"] == aid
    assert row["payee"] == "Home Mortgage Payment"
    assert row["amount"] == 1998_65 and row["frequency"] == "monthly"
    assert row["next_date"] == "2024-02-01"


def test_generate_all_due_covers_manual_and_loans(conn):
    chk = ledger.create_account(conn, "Checking", "cash")
    scheduled.add_scheduled(conn, chk, payee="Cable", amount=-89_00,
                            frequency="monthly", next_date="2024-02-01")
    loan = _setup_loan(conn)
    ids = scheduled.generate_all_due(conn, "2024-01-28", lead_days=7)
    assert len(ids) == 2
    assert _scheduled_txn_count(conn, chk) == 1
    assert _scheduled_txn_count(conn, loan) == 1


# ---------------------------------------------------------------------------
# THE crux: definitions + generated pre-entries survive a QIF re-import
# ---------------------------------------------------------------------------
def test_scheduled_definitions_survive_qif_reimport(conn, tmp_path):
    qif = _write(tmp_path, "bank.qif", QIF_BANK)
    importers.import_file(conn, qif)          # creates Checking + posts 2 rows
    chk = _checking(conn)

    # A generalized (non-loan) recurring bill + one generated pending pre-entry.
    sid = scheduled.add_scheduled(conn, chk, payee="Netflix", amount=-15_99,
                                  frequency="monthly", next_date="2026-02-01")
    scheduled.create_pending_from_definition(conn, sid)
    assert len(scheduled.list_scheduled(conn)) == 1
    assert _scheduled_txn_count(conn, chk) == 1
    posted_before = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE scheduled=0").fetchone()["c"]

    # Re-import the SAME file: posted rows dedup; the DEFINITION and its pending
    # pre-entry must be untouched (importers never read/write scheduled_payments,
    # and _find_dup_cash ignores scheduled=1 rows).
    res2 = importers.import_file(conn, qif)
    assert res2.added == 0 and res2.duplicates >= 1

    defs = scheduled.list_scheduled(conn)
    assert len(defs) == 1                              # not duplicated, not lost
    assert defs[0]["id"] == sid and defs[0]["payee"] == "Netflix"
    assert defs[0]["amount"] == -15_99
    assert _scheduled_txn_count(conn, chk) == 1        # pre-entry not duplicated
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE scheduled=0"
    ).fetchone()["c"] == posted_before                 # no double-counted posts


def test_loan_params_survive_qif_reimport(conn, tmp_path):
    _setup_loan(conn)
    qif = _write(tmp_path, "bank.qif", QIF_BANK)
    importers.import_file(conn, qif)
    importers.import_file(conn, qif)
    assert conn.execute(
        "SELECT COUNT(*) c FROM loan_params").fetchone()["c"] == 1


def test_nonloan_positive_pre_entry_does_not_crash_import(conn):
    """A generalized POSITIVE scheduled pre-entry on a non-loan account must
    never reach the LOAN merge path (which calls loans.payment_split and would
    crash for lack of loan_params). Regression for the import gate. It IS
    merged by the plain-placeholder path: the deposit posts INTO the pre-entry,
    keeping the definition's payee, and no duplicate row appears."""
    chk = ledger.create_account(conn, "Checking", "cash")
    sid = scheduled.add_scheduled(conn, chk, payee="Rent Reimb", amount=200_00,
                                  frequency="monthly", next_date="2026-02-01")
    scheduled.create_pending_from_definition(conn, sid)
    res = importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Checking", date="2026-02-02", amount_cents=200_00,
        payee="Deposit", fitid="DEP-1")], provider="test")
    assert res.added == 0 and res.matched == 1
    assert _scheduled_txn_count(conn, chk) == 0          # the pre-entry became the deposit
    row = conn.execute(
        "SELECT payee, fitid, cleared, date FROM transactions WHERE account_id=?",
        (chk,)).fetchone()
    assert (row["payee"], row["fitid"], row["cleared"], row["date"]) == \
        ("Rent Reimb", "DEP-1", 1, "2026-02-02")
