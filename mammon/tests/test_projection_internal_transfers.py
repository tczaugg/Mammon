"""A transfer with BOTH ends inside the projected account set is not an event.

The user: "when both ends of the transfer are in the list of accounts we're
showing info for, we have both a positive and negative amount with the same
Payee. Since such a transfer is net-neutral, we shouldn't show either side.
Only show transfers into or out of the account set, not within."

So the rule is scoped to the set passed to :func:`mammon.projection.project` --
the same transfer keeps showing when only one of its ends is displayed, because
that is real money arriving or leaving. It is read-side filtering only, and
since a suppressed pair sums to zero the day-by-day balances must not move;
that invariant gets its own test below. All data here is synthetic.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger, loans, loans_schedule, projection, scheduled
from mammon.ui import prefs
from mammon.ui.projection_dialogs import CalendarPanel
from mammon.tests import fresh_db

TODAY = "2026-03-01"
START, END = "2026-03-01", "2026-03-31"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "internal.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Account One", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Account Two", "savings", opening_balance=500_00)
    out = ledger.create_account(conn, "Account Three", "checking", opening_balance=250_00)
    return chk, sav, out


def _events(conn, ids, **kw):
    return projection.projected_events(conn, ids, START, END,
                                       include_predictions=False, today=TODAY, **kw)


# -- entered rows -----------------------------------------------------------

def test_both_ends_displayed_hides_both_legs(conn, accounts):
    chk, sav, _ = accounts
    ledger.create_transfer(conn, chk, sav, "2026-03-10", 250_00, payee="Move Money")
    assert [e.payee for e in _events(conn, [chk, sav])] == []
    # The escape hatch still shows the pair, so the rule lives in one place.
    both = _events(conn, [chk, sav], show_internal_transfers=True)
    assert sorted(e.amount for e in both) == [-250_00, 250_00]


def test_one_end_displayed_still_shows_that_leg(conn, accounts):
    chk, sav, _ = accounts
    ledger.create_transfer(conn, chk, sav, "2026-03-10", 250_00, payee="Move Money")
    out_leg = _events(conn, [chk])
    assert [(e.account_id, e.payee, e.amount) for e in out_leg] == [(chk, "Move Money", -250_00)]
    in_leg = _events(conn, [sav])
    assert [(e.account_id, e.payee, e.amount) for e in in_leg] == [(sav, "Move Money", 250_00)]


def test_a_transfer_to_an_undisplayed_account_is_unaffected(conn, accounts):
    chk, sav, out = accounts
    ledger.create_transfer(conn, chk, out, "2026-03-10", 250_00, payee="Move Money")
    evs = _events(conn, [chk, sav])
    assert [(e.account_id, e.amount) for e in evs] == [(chk, -250_00)]


def test_an_ordinary_payment_is_never_suppressed(conn, accounts):
    chk, sav, _ = accounts
    ledger.add_transaction(conn, chk, "2026-03-09", -40_00, payee="Corner Store")
    assert [e.payee for e in _events(conn, [chk, sav])] == ["Corner Store"]


# -- the net-neutral invariant ----------------------------------------------

def test_balances_are_unchanged_by_the_suppression(conn, accounts):
    """Suppression is net-neutral by construction: adding an internal transfer
    to the database must not move a single projected number."""
    chk, sav, _ = accounts
    ledger.add_transaction(conn, chk, "2026-03-05", -120_00, payee="Corner Store")
    ledger.add_transaction(conn, sav, "2026-03-18", 60_00, payee="Interest")
    before = projection.project(conn, [chk, sav], START, END,
                                include_predictions=False, today=TODAY)
    ledger.create_transfer(conn, chk, sav, "2026-03-10", 250_00, payee="Move Money")
    # ... and one dated before the window, which lands in the opening balance.
    ledger.create_transfer(conn, sav, chk, "2026-02-14", 75_00, payee="Move Money")
    after = projection.project(conn, [chk, sav], START, END,
                               include_predictions=False, today=TODAY)

    assert (after.opening, after.closing, after.low, after.low_date) == \
           (before.opening, before.closing, before.low, before.low_date)
    assert [(d.date, d.balance) for d in after.days] == \
           [(d.date, d.balance) for d in before.days]
    # And the test is only meaningful because the transfer really is hidden.
    assert "Move Money" not in {e.payee for e in after.events()}
    assert "Move Money" in {e.payee for e in projection.project(
        conn, [chk, sav], START, END, include_predictions=False, today=TODAY,
        show_internal_transfers=True).events()}


# -- scheduled pre-entries ---------------------------------------------------

def test_scheduled_transfer_internal_to_the_set_shows_neither_leg(conn, accounts):
    chk, sav, _ = accounts
    scheduled.add_scheduled(conn, chk, payee="Auto Save", amount=-500_00,
                            frequency="monthly", next_date="2026-03-20",
                            transfer_account_id=sav)
    assert [e.payee for e in _events(conn, [chk, sav])] == []


def test_scheduled_transfer_with_one_end_displayed_still_shows(conn, accounts):
    chk, sav, _ = accounts
    scheduled.add_scheduled(conn, chk, payee="Auto Save", amount=-500_00,
                            frequency="monthly", next_date="2026-03-20",
                            transfer_account_id=sav)
    assert [(e.account_id, e.amount) for e in _events(conn, [chk])] == [(chk, -500_00)]
    assert [(e.account_id, e.amount) for e in _events(conn, [sav])] == [(sav, 500_00)]


# -- loan payments -----------------------------------------------------------

@pytest.fixture
def loan(conn, accounts):
    chk, _sav, _out = accounts
    acct = ledger.create_account(conn, "Term Loan", "liability", opening_balance=0)
    loans.set_loan_params(conn, acct, original_principal=100000_00,
                          origination_date="2025-12-01", term_months=360,
                          payment_amount=600_00, interval="monthly",
                          rates=[("2025-12-01", "6")])
    loans.set_funding_account(conn, acct, chk)
    return acct


def test_loan_payment_between_two_displayed_accounts_shows_neither_leg(conn, accounts, loan):
    chk, _sav, _out = accounts
    evs = _events(conn, [chk, loan])
    assert [e for e in evs if e.source == projection.LOAN] == []


def test_loan_payment_still_shows_when_only_the_funder_is_displayed(conn, accounts, loan):
    chk, _sav, _out = accounts
    legs = [e for e in _events(conn, [chk]) if e.source == projection.LOAN]
    assert legs and all(e.account_id == chk and e.amount == -600_00 for e in legs)
    # The loan register on its own keeps its principal leg, too.
    own = [e for e in _events(conn, [loan]) if e.source == projection.LOAN]
    assert own and all(e.account_id == loan and e.amount > 0 for e in own)


def test_a_pre_entered_loan_payment_keeps_both_legs(conn, accounts, loan):
    """The guard on the suppression: a pair is only hidden when it cancels.

    Pre-entering a loan payment writes a split on the funder (the whole
    payment, interest included) and a principal-only row on the loan, so the
    two do NOT cancel; hiding either one would understate what leaves the
    funder. Both stay, unlike the projected LOAN-source pair above."""
    chk, _sav, _out = accounts
    loans_schedule.ensure_pending_payments(conn, loan, "2026-02-25", lead_days=10)
    entered = [e for e in _events(conn, [chk, loan]) if e.source == projection.ENTERED]
    by_acct = {e.account_id: e.amount for e in entered}
    assert set(by_acct) == {chk, loan}
    assert by_acct[chk] == -600_00                       # payment + interest
    assert 0 < by_acct[loan] < 600_00                    # principal only


# -- the calendar itself -----------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    prefs.set_projection_slots([])
    yield
    prefs.set_projection_slots([])


def test_calendar_day_cell_drops_the_internal_transfer(qapp, conn, accounts):
    chk, sav, _ = accounts
    ledger.create_transfer(conn, chk, sav, "2026-03-10", 250_00, payee="Move Money")
    panel = CalendarPanel(conn, year=2026, month=3, today=TODAY)
    try:
        panel.include_predictions.setChecked(False)
        panel.slots.set_slot(0, chk)
        panel.slots.set_slot(1, sav)
        panel.refresh()
        assert panel.projection.account_ids == [chk, sav]
        assert "Move Money" not in panel.cell_text(10)
        assert panel.events_on(10) == []
        both_balances = [(d.date, d.balance) for d in panel.projection.days]

        # Only one end displayed: the leg is real money leaving, so it shows.
        panel.slots.clear_slot(1)
        panel.refresh()
        assert panel.projection.account_ids == [chk]
        assert "Move Money" in panel.cell_text(10)
        assert [e.amount for e in panel.events_on(10)] == [-250_00]

        # Hiding the pair did not move the two-account running balance.
        panel.slots.set_slot(1, sav)
        panel.refresh()
        assert [(d.date, d.balance) for d in panel.projection.days] == both_balances
        assert panel.projection.closing == 1000_00 + 500_00
    finally:
        panel.deleteLater()
