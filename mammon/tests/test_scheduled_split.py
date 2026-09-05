"""A scheduled-payment definition can carry a SPLIT TEMPLATE learned from a
predicted entry's history, and reproduces it as real split lines on every
pre-entry it generates.

The concrete flow: the financial calendar predicts a recurring paycheck/bill
from history; that history is itself split (a paycheck's gross/taxes, a utility
bill's electric+water). Right-clicking the predicted entry and choosing
"Schedule <payee>" must LEARN that split -- the same lines the register's "Copy
from previous <payee> split" copies -- persist it on the definition
(``scheduled_splits``), and re-enter it as split lines whenever the placeholder
transaction is created. All split rows are still written through
``mammon.ledger.set_splits`` (the sole writer of transaction/split rows)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger, scheduled
from mammon.ui import style
from mammon.ui.projection_dialogs import CalendarPanel

TODAY = "2026-09-02"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "sched_split.db")
    yield c
    c.close()


def _split_paycheck(conn, chk, salary, tax, date):
    """A +2400.00 paycheck deposit split into +3000.00 salary and -600.00
    withholding (sums to the deposit total)."""
    tid = ledger.add_transaction(conn, chk, date, 2400_00, payee="Acme Payroll")
    ledger.set_splits(conn, tid, [
        {"category_id": salary, "amount": 3000_00, "memo": "gross"},
        {"category_id": tax, "amount": -600_00, "memo": "withholding"},
    ])
    return tid


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    salary = ledger.resolve_category(conn, "Salary")
    tax = ledger.resolve_category(conn, "Taxes")
    # Three monthly split paychecks -- enough for the predictor to recur it.
    for date in ("2026-06-15", "2026-07-15", "2026-08-15"):
        _split_paycheck(conn, chk, salary, tax, date)
    return {"chk": chk, "salary": salary, "tax": tax}


# ---------------------------------------------------------------------------
# The domain: storing a template and reproducing it on a pre-entry
# ---------------------------------------------------------------------------
def test_definition_stores_template_and_enters_split_lines(conn, world):
    chk, salary, tax = world["chk"], world["salary"], world["tax"]
    # The learned split is exactly what "Copy from previous <payee> split" copies.
    learned = ledger.previous_split_for_payee(conn, "Acme Payroll")
    assert [(s["category_id"], s["amount"]) for s in learned] == \
        [(salary, 3000_00), (tax, -600_00)]

    sid = scheduled.add_scheduled(
        conn, chk, payee="Acme Payroll", amount=2400_00, frequency="monthly",
        next_date="2026-09-15", category_id=salary, splits=learned)

    # The definition remembers the template.
    stored = scheduled.get_scheduled_splits(conn, sid)
    assert [(s["category_id"], s["amount"], s["memo"]) for s in stored] == \
        [(salary, 3000_00, "gross"), (tax, -600_00, "withholding")]

    # The generated placeholder is a real split, summing to the pre-entry total.
    tid = scheduled.create_pending_from_definition(conn, sid, "2026-09-15")
    txn = ledger.get_transaction(conn, tid)
    assert txn["scheduled"] == 1 and txn["amount"] == 2400_00
    assert ledger.has_splits(conn, tid)
    got = ledger.get_splits(conn, tid)
    assert [(s["category_id"], s["amount"]) for s in got] == \
        [(salary, 3000_00), (tax, -600_00)]
    # The split reconciles to the total: nothing left uncategorized.
    assert ledger.uncategorized_split_amount(conn, tid) == 0


def test_posted_enter_also_reproduces_the_split(conn, world):
    """Quicken's Enter (a POSTED row, not a placeholder) reproduces it too."""
    chk, salary, tax = world["chk"], world["salary"], world["tax"]
    learned = ledger.previous_split_for_payee(conn, "Acme Payroll")
    sid = scheduled.add_scheduled(
        conn, chk, payee="Acme Payroll", amount=2400_00, frequency="monthly",
        next_date="2026-09-15", category_id=salary, splits=learned)
    tid = scheduled.enter_next(conn, sid)          # posts, advances next_date
    txn = ledger.get_transaction(conn, tid)
    assert txn["scheduled"] == 0
    assert [(s["category_id"], s["amount"]) for s in ledger.get_splits(conn, tid)] == \
        [(salary, 3000_00), (tax, -600_00)]


def test_no_split_definition_stays_plain(conn, world):
    """A definition given no template pre-enters a plain single-line row."""
    chk, salary = world["chk"], world["salary"]
    sid = scheduled.add_scheduled(
        conn, chk, payee="Rent", amount=-1500_00, frequency="monthly",
        next_date="2026-09-15", category_id=salary)
    assert scheduled.get_scheduled_splits(conn, sid) == []
    tid = scheduled.create_pending_from_definition(conn, sid, "2026-09-15")
    assert not ledger.has_splits(conn, tid)


# ---------------------------------------------------------------------------
# The calendar: "Schedule <payee>" on a split PREDICTED entry learns the split
# ---------------------------------------------------------------------------
def test_calendar_schedule_prediction_learns_split(qapp, conn, world, monkeypatch):
    chk, salary, tax = world["chk"], world["salary"], world["tax"]
    monkeypatch.setattr(style, "theme", lambda: "light")
    dlg = CalendarPanel(conn, year=2026, month=9, today=TODAY)

    # The paycheck is predicted for 2026-09-15.
    day = next(d for d in range(1, 31)
               if any(e.payee == "Acme Payroll" for e in dlg.predictions_on(d)))
    [pay] = [e for e in dlg.predictions_on(day) if e.payee == "Acme Payroll"]

    # The editor is a seam; echo the pre-filled entry back unchanged (the user
    # accepts the predicted paycheck as-is).
    def fake_editor(entry):
        return {"account_id": entry["account_id"], "payee": entry["payee"],
                "amount": entry["amount"], "frequency": entry["frequency"],
                "next_date": entry["next_date"], "category_id": entry["category_id"]}

    dlg._edit_definition = fake_editor
    sid = dlg.schedule_prediction(pay)
    assert sid is not None

    # The definition LEARNED the split from the predicted payee's history...
    stored = scheduled.get_scheduled_splits(conn, sid)
    assert [(s["category_id"], s["amount"]) for s in stored] == \
        [(salary, 3000_00), (tax, -600_00)]

    # ...and reproduces it as split lines on the pre-entry.
    tid = scheduled.create_pending_from_definition(conn, sid, "2026-09-15")
    assert ledger.has_splits(conn, tid)
    assert [(s["category_id"], s["amount"]) for s in ledger.get_splits(conn, tid)] == \
        [(salary, 3000_00), (tax, -600_00)]
    dlg.deleteLater()
