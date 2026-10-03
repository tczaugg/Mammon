"""mammon.reports.saving: money saved or paid down, by destination account
(SRD 5.12d).

Pins the definition the Budget Planner's Savings & Pay Down rows rest on: a
transfer leg out of a cash-flow account (checking, credit, cash) into any other
kind of account is saving, split legs included; a credit card payment and a
move between two saving-side accounts are not; the figure is NET per
destination; and each transfer is counted once whichever side it was entered
from. Synthetic data only.
"""
from __future__ import annotations

import pytest

from mammon import ledger
from mammon.reports.saving import (cash_flow_account_ids, saving_by_account,
                                   saving_lines)
from mammon.tests import fresh_db

START, END = "2026-03-01", "2026-03-31"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "saving.db")
    yield c
    c.close()


@pytest.fixture
def accts(conn):
    return {
        "checking": ledger.create_account(conn, "Everyday Checking", "checking"),
        "card": ledger.create_account(conn, "Rewards Card", "credit"),
        "savings": ledger.create_account(conn, "Rainy Day Savings", "savings"),
        "k401": ledger.create_account(conn, "Employer 401K", "investment"),
        "loan": ledger.create_account(conn, "Home Loan", "liability"),
        "brokerage": ledger.create_account(conn, "Brokerage", "investment"),
    }


def _paycheck(conn, accts, date, deferral=450_00):
    """A paycheck split: salary in, tax out, and a 401(k) deferral leg that
    TRANSFERS to the retirement account -- the shape that started this."""
    salary = ledger.resolve_category(conn, "Salary")
    tax = ledger.resolve_category(conn, "Taxes:Federal")
    net = 4_000_00 - 600_00 - deferral
    tid = ledger.add_transaction(conn, accts["checking"], date, net,
                                 payee="Employer Inc")
    ledger.set_splits(conn, tid, [
        {"category_id": salary, "amount": 4_000_00},
        {"category_id": tax, "amount": -600_00},
        {"transfer_account_id": accts["k401"], "amount": -deferral},
    ])
    return tid


def test_a_paycheck_deferral_leg_is_saving(conn, accts):
    _paycheck(conn, accts, "2026-03-13")
    _paycheck(conn, accts, "2026-03-27")
    assert saving_by_account(conn, START, END) == {accts["k401"]: 900_00}


def test_a_mortgage_principal_leg_is_pay_down(conn, accts):
    interest = ledger.resolve_category(conn, "Interest Exp")
    tid = ledger.add_transaction(conn, accts["checking"], "2026-03-01", -1_200_00,
                                 payee="Home Lender")
    ledger.set_splits(conn, tid, [
        {"category_id": interest, "amount": -900_00},
        {"transfer_account_id": accts["loan"], "amount": -300_00},
    ])
    assert saving_by_account(conn, START, END) == {accts["loan"]: 300_00}


def test_checking_to_savings_is_saving(conn, accts):
    ledger.create_transfer(conn, accts["checking"], accts["savings"],
                           "2026-03-05", 500_00)
    assert saving_by_account(conn, START, END) == {accts["savings"]: 500_00}


def test_a_credit_card_payment_is_not_saving(conn, accts):
    """Both ends are cash-flow accounts: the card's purchases were the spending,
    and counting the payment as anything would count them twice."""
    ledger.create_transfer(conn, accts["checking"], accts["card"],
                           "2026-03-05", 750_00)
    assert saving_by_account(conn, START, END) == {}


def test_moving_saved_money_between_saving_accounts_is_not_new_saving(conn, accts):
    ledger.create_transfer(conn, accts["savings"], accts["brokerage"],
                           "2026-03-05", 2_000_00)
    assert saving_by_account(conn, START, END) == {}


def test_saving_is_net_of_withdrawals(conn, accts):
    ledger.create_transfer(conn, accts["checking"], accts["brokerage"],
                           "2026-03-02", 1_000_00)
    ledger.create_transfer(conn, accts["brokerage"], accts["checking"],
                           "2026-03-20", 1_500_00)
    assert saving_by_account(conn, START, END) == {accts["brokerage"]: -500_00}


def test_a_transfer_entered_from_the_far_side_is_counted_once(conn, accts):
    """Entered in the 401(k) register as money arriving from checking: the
    mirror row on checking is what is read, so it counts exactly once."""
    ledger.create_transfer(conn, accts["checking"], accts["k401"],
                           "2026-03-09", 250_00)
    ledger.create_transfer(conn, accts["checking"], accts["k401"],
                           "2026-03-10", 100_00)
    assert saving_by_account(conn, START, END) == {accts["k401"]: 350_00}


def test_saving_lines_carry_the_parent_payee(conn, accts):
    _paycheck(conn, accts, "2026-03-13")
    lines = saving_lines(conn, START, END)
    assert [(ln.payee, ln.amount, ln.transfer_account_id) for ln in lines] == [
        ("Employer Inc", -450_00, accts["k401"])]


def test_the_window_bounds_are_inclusive(conn, accts):
    ledger.create_transfer(conn, accts["checking"], accts["savings"],
                           "2026-02-28", 1_00)
    ledger.create_transfer(conn, accts["checking"], accts["savings"],
                           "2026-03-01", 10_00)
    ledger.create_transfer(conn, accts["checking"], accts["savings"],
                           "2026-03-31", 100_00)
    ledger.create_transfer(conn, accts["checking"], accts["savings"],
                           "2026-04-01", 1_000_00)
    assert saving_by_account(conn, START, END) == {accts["savings"]: 110_00}


def test_cash_flow_accounts_are_checking_credit_and_cash(conn, accts):
    cash = ledger.create_account(conn, "Wallet", "cash")
    assert set(cash_flow_account_ids(conn)) == {
        accts["checking"], accts["card"], cash}
