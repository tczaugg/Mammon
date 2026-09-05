"""mammon.reports.payees: money by payee and by tag, in three directions, with
splits attributed to the parent's payee/tag and transactions (not lines)
counted (roadmap item 4)."""
from __future__ import annotations

import pytest

from mammon import db, ledger
from mammon.reports import payees


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "payees.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    g = ledger.resolve_category(conn, "Groceries")
    h = ledger.resolve_category(conn, "Household")
    ledger.add_transaction(conn, chk, "2026-01-01", 3000_00, payee="Employer")
    for month in ("01", "02", "03"):
        ledger.add_transaction(conn, chk, f"2026-{month}-02", -1000_00, payee="Landlord",
                               tag="home")
    ledger.add_transaction(conn, chk, "2026-01-10", -200_00, payee="Grocer", category_id=g)
    split = ledger.add_transaction(conn, chk, "2026-02-10", -180_00, payee="Grocer",
                                   tag="food")
    ledger.set_splits(conn, split, [(g, -150_00, ""), (h, -30_00, "")])
    ledger.add_transaction(conn, chk, "2026-02-12", 20_00, payee="Grocer", category_id=g)
    ledger.add_transaction(conn, chk, "2026-03-15", -40_00)                 # no payee
    ledger.create_transfer(conn, chk, sav, "2026-01-15", 500_00, payee="Stash")
    return {"chk": chk, "sav": sav}


def _rows(rep):
    return [(r.name, r.count, r.cents) for r in rep.rows]


def test_by_payee_out_in_net(conn, seeded):
    out = payees.by_payee(conn, "2026-01-01", "2026-03-31")
    assert _rows(out) == [("Landlord", 3, 3000_00), ("Grocer", 2, 380_00),
                          ("(no payee)", 1, 40_00)]
    assert out.total == 3420_00 and out.key == "payee" and out.direction == "out"
    inn = payees.by_payee(conn, "2026-01-01", "2026-03-31", direction="in")
    assert _rows(inn) == [("Employer", 1, 3000_00), ("Grocer", 1, 20_00)]
    net = payees.by_payee(conn, "2026-01-01", "2026-03-31", direction="net")
    assert dict((n, c) for n, _k, c in _rows(net))["Grocer"] == -360_00
    assert net.total == 3000_00 - 3000_00 - 360_00 - 40_00
    # The transfer never appears under its payee.
    assert all(n != "Stash" for n, _k, _c in _rows(net))
    with pytest.raises(ValueError):
        payees.by_payee(conn, "2026-01-01", "2026-03-31", direction="sideways")


def test_by_tag_inherits_the_parents_tag_on_split_lines(conn, seeded):
    rep = payees.by_tag(conn, "2026-01-01", "2026-03-31")
    assert _rows(rep) == [("home", 3, 3000_00), ("(no tag)", 2, 240_00), ("food", 1, 180_00)]
    only_feb = payees.by_tag(conn, "2026-02-01", "2026-02-28", account_ids=[seeded["chk"]])
    assert _rows(only_feb) == [("home", 1, 1000_00), ("food", 1, 180_00)]


# ---- regression: a paycheck belongs to the employer, not its legs ----------

def test_by_payee_bills_a_split_to_its_payee_at_the_parents_amount(conn):
    """A paycheck is a split: salary in, tax legs out, a 401(k) deferral
    transferring to the retirement account. The by-payee report used to read the
    LEGS, and the shared extraction drops transfer legs -- which unbalances the
    split, so the employer totalled to the withholding. Reported against a real
    ledger: an employer who had deposited $60,000 showed as $12,000.

    Synthetic figures, same shape."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    plan = ledger.create_account(conn, "Plan 401K", "savings", opening_balance=0)
    salary = ledger.resolve_category(conn, "Salary")
    tax = ledger.resolve_category(conn, "Fed")

    # 4,000.00 gross - 900.00 tax - 300.00 deferral = 2,800.00 deposited.
    pay = ledger.add_transaction(conn, chk, "2026-01-15", 2800_00, payee="Acme Corp.")
    ledger.set_splits(conn, pay, [
        {"category_id": salary, "amount": 4000_00},
        {"category_id": tax, "amount": -900_00},
        {"transfer_account_id": plan, "amount": -300_00},
    ])

    inn = payees.by_payee(conn, "2026-01-01", "2026-01-31", direction="in")
    assert _rows(inn) == [("Acme Corp.", 1, 2800_00)]
    net = payees.by_payee(conn, "2026-01-01", "2026-01-31", direction="net")
    assert _rows(net) == [("Acme Corp.", 1, 2800_00)]

    # You paid your employer nothing: the withholding is a category fact, not a
    # payment to them. The old behaviour reported 900.00 here.
    out = payees.by_payee(conn, "2026-01-01", "2026-01-31")
    assert _rows(out) == []
    assert out.total == 0

    # The deferral's mirror in the plan account repeats the payee, and is a plain
    # transfer -- excluded, so the employer is never counted twice.
    assert conn.execute(
        "SELECT payee FROM transactions WHERE account_id = ?",
        (plan,)).fetchone()["payee"] == "Acme Corp."

    # A tag report still reads the LEGS: payroll tax is a real expense whatever
    # the paycheck nets out to, which is a category-shaped question.
    by_tag = payees.by_tag(conn, "2026-01-01", "2026-01-31")
    assert dict((n, c) for n, _k, c in _rows(by_tag)).get("(no tag)") == 900_00


def test_by_payee_totals_a_plain_split_exactly_as_before(conn, seeded):
    """Collapsing must not change a split with no transfer leg: the parent's
    amount is the sum of its legs (ledger.set_splits guarantees it), so the
    Grocer's 180.00 split still totals 180.00 and counts as one transaction."""
    out = payees.by_payee(conn, "2026-02-01", "2026-02-28")
    assert dict((n, c) for n, _k, c in _rows(out))["Grocer"] == 180_00
    assert dict((n, k) for n, k, _c in _rows(out))["Grocer"] == 1


def test_by_payee_net_is_signed_both_ways_for_a_payee_on_both_sides(conn):
    """The rule as the user stated it: if I pay someone $200 and later they pay
    me $50, the report should show -$150 against them as a Payee. Outflows are
    negative, inflows positive, and a payee on both sides nets."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    ledger.add_transaction(conn, chk, "2026-02-01", -200_00, payee="Jordan Lee")
    ledger.add_transaction(conn, chk, "2026-03-01", 50_00, payee="Jordan Lee")
    ledger.add_transaction(conn, chk, "2026-03-05", -85_32, payee="Blue Diner")

    net = payees.by_payee(conn, "2026-01-01", "2026-12-31", direction="net")
    assert _rows(net) == [("Jordan Lee", 2, -150_00), ("Blue Diner", 1, -85_32)]
    # The report total is signed too, not a magnitude.
    assert net.total == -235_32
