"""Account Details: the current-employer-plan checkbox.

Everything here is synthetic - an "ANON" 401(k) and IRA in a throwaway ledger.
No real name, account number or balance belongs in this file.

The flag `accounts.current_employer_plan` decides one thing in the Retirement
Planner: whether a tax-deferred account carries the RMD floor (IRC
401(a)(9)(C)(i)(II) exempts the plan at the employer you still work for). It
existed as a column with no way to set it, so the only way to say so was
hand-written SQL. These tests pin the checkbox that fixes that: it is offered
only where it means something, it round-trips through the same
``ledger.update_account`` path the closed/hidden flags take, and what it writes
changes the plan.

Nothing here opens a modal: the dialog is constructed and inspected directly,
and persistence is done the way the callers do it (``dlg.values()`` ->
``ledger.update_account``). ``exec_()`` under the offscreen platform blocks
forever.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import ledger, rebalance
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "widgets.db")
    yield c
    c.close()


def _open_details(conn, account_id):
    """The dialog on one account, built the way its callers build it."""
    from mammon.ui.widgets import AccountDetailsDialog

    return AccountDetailsDialog(ledger.get_account(conn, account_id), conn=conn)


def _save(conn, dlg, account_id):
    """Persist exactly the way the accounts list does after OK."""
    ledger.update_account(conn, account_id, **dlg.values())


def _flag(conn, account_id) -> int:
    return conn.execute("SELECT current_employer_plan FROM accounts WHERE id=?",
                        (account_id,)).fetchone()[0]


@pytest.fixture
def work_401k(conn):
    account_id = ledger.create_account(conn, "ANON Employer 401(k)", "investment")
    rebalance.set_account_treatment(conn, account_id, "deferred")
    return account_id


def test_the_checkbox_persists_and_comes_back_checked(qapp, conn, work_401k):
    dlg = _open_details(conn, work_401k)
    try:
        assert dlg.current_employer_plan is not None
        assert not dlg.current_employer_plan.isChecked()
        assert not dlg.current_employer_plan.isHidden()   # offered on a 401(k)
        dlg.current_employer_plan.setChecked(True)
        dlg.accept()
        _save(conn, dlg, work_401k)
    finally:
        dlg.setParent(None)

    assert _flag(conn, work_401k) == 1

    reopened = _open_details(conn, work_401k)
    try:
        assert reopened.current_employer_plan.isChecked()
        # And unchecking it clears the flag again.
        reopened.current_employer_plan.setChecked(False)
        reopened.accept()
        _save(conn, reopened, work_401k)
    finally:
        reopened.setParent(None)

    assert _flag(conn, work_401k) == 0


def test_the_checkbox_is_offered_only_for_a_tax_deferred_account(
        qapp, conn, work_401k):
    """Shown for Tax-deferred on a retirement-capable type; hidden otherwise.

    A Roth has no lifetime RMD (IRC 408A(c)(4)) so the still-working exception
    buys nothing, and a taxable or unset account is not a plan at all.
    """
    dlg = _open_details(conn, work_401k)
    try:
        assert not dlg.current_employer_plan.isHidden()
        for treatment in ("roth", "taxable", "special", ""):
            i = dlg.tax_treatment.findData(treatment)
            dlg.tax_treatment.setCurrentIndex(i)
            assert dlg.current_employer_plan.isHidden(), treatment
        dlg.tax_treatment.setCurrentIndex(dlg.tax_treatment.findData("deferred"))
        assert not dlg.current_employer_plan.isHidden()
        # A type that cannot hold retirement money hides it too.
        dlg.type.setCurrentIndex(dlg.type.findText("asset"))
        assert dlg.current_employer_plan.isHidden()
    finally:
        dlg.setParent(None)


def test_a_flag_set_on_a_deferred_account_is_dropped_when_it_stops_applying(
        qapp, conn, work_401k):
    """Retagging the account Roth must not leave a flag nothing can show."""
    ledger.update_account(conn, work_401k, current_employer_plan=1)
    dlg = _open_details(conn, work_401k)
    try:
        assert dlg.current_employer_plan.isChecked()
        dlg.tax_treatment.setCurrentIndex(dlg.tax_treatment.findData("roth"))
        dlg.accept()
        _save(conn, dlg, work_401k)
    finally:
        dlg.setParent(None)

    assert _flag(conn, work_401k) == 0


def test_a_checking_account_with_no_treatment_never_offers_it(qapp, conn):
    account_id = ledger.create_account(conn, "ANON Checking", "checking")
    dlg = _open_details(conn, account_id)
    try:
        assert dlg.current_employer_plan.isHidden()
        assert dlg.values()["current_employer_plan"] == 0
    finally:
        dlg.setParent(None)
