"""The life of a pre-entry after it is generated: posting it by hand, a
payment made from another account once, a standing pre-entry restated by
Enter, Loan Setup changes reaching the rows already generated, downloads that
merge on a changed amount or on the payee, and a lender's own download meeting
the principal mirror. Each of these was a hole the first cut left open."""
from __future__ import annotations

import pytest

from mammon import db, importers, ledger, loans, loans_schedule, scheduled
from mammon.importers.record import NormalizedTxn

PAYMENT = 1268_99


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "life.db")
    yield c
    c.close()


@pytest.fixture
def house(conn):
    """Checking pays a mortgage the Quicken way (see test_loan_funding), plus a
    credit card and a savings account for the alternatives."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=10000_00)
    card = ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=5000_00)
    loan = ledger.create_account(conn, "Mortgage", "liability", opening_balance=-113394_32)
    ledger.resolve_category(conn, "Int Exp")
    ledger.resolve_category(conn, "Escrow")
    loans.set_loan_params(conn, loan, original_principal=113394_32,
                          origination_date="2025-12-01", term_months=357,
                          payment_amount=PAYMENT, interval="monthly",
                          rates=[("2025-12-01", "5.25")],
                          extras=[("Escrow", 250_00, "Escrow")],
                          interest_category="Int Exp")
    for date in ("2026-01-01", "2026-02-01"):
        split = loans.payment_split(conn, loan, date, PAYMENT)
        tid = ledger.add_transaction(conn, chk, date, -PAYMENT, payee="US Bank")
        ledger.set_splits(conn, tid, [
            {"category_id": ledger.resolve_category(conn, "Int Exp"), "amount": -split.interest},
            {"category_id": ledger.resolve_category(conn, "Escrow"), "amount": -split.escrow},
            {"transfer_account_id": loan, "amount": -split.principal}])
    return {"chk": chk, "card": card, "sav": sav, "loan": loan}


def _mirror(conn, tid):
    row = conn.execute("SELECT transfer_pair_id FROM splits WHERE transaction_id=? "
                       "AND transfer_pair_id IS NOT NULL", (tid,)).fetchone()
    return ledger.get_transaction(conn, row["transfer_pair_id"]) if row else None


def _leg_sum(conn, tid):
    return sum(s["amount"] for s in ledger.get_splits(conn, tid))


# ---------------------------------------------------------------------------
# posting a pre-entry by hand
# ---------------------------------------------------------------------------
def test_set_scheduled_moves_a_pre_entry_and_its_mirrors_together(conn, house):
    loan = house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    assert ledger.get_transaction(conn, pid)["scheduled"] == 1
    assert _mirror(conn, pid)["scheduled"] == 1
    ledger.set_scheduled(conn, pid, False)
    assert ledger.get_transaction(conn, pid)["scheduled"] == 0
    assert _mirror(conn, pid)["scheduled"] == 0
    ledger.set_scheduled(conn, pid, True)
    assert _mirror(conn, pid)["scheduled"] == 1
    # A plain transfer placeholder carries its pair along too.
    sav = house["sav"]
    sid = scheduled.add_scheduled(conn, house["chk"], payee="Auto-save", amount=-100_00,
                                  frequency="monthly", next_date="2026-03-05",
                                  transfer_account_id=sav)
    tid = scheduled.create_pending_from_definition(conn, sid)
    pair = ledger.get_transaction(conn, tid)["transfer_pair_id"]
    ledger.set_scheduled(conn, tid, False)
    assert ledger.get_transaction(conn, pair)["scheduled"] == 0


# ---------------------------------------------------------------------------
# Enter Payment: from another account once; restating a standing pre-entry
# ---------------------------------------------------------------------------
def test_enter_payment_from_a_card_once_does_not_change_the_default(conn, house):
    chk, card, loan = house["chk"], house["card"], house["loan"]
    assert loans.get_loan_params(conn, loan).funding_account_id is None   # inferred
    tid = loans_schedule.enter_payment(conn, loan, "2026-03-01", funding_account_id=card)
    row = ledger.get_transaction(conn, tid)
    assert (row["account_id"], row["amount"], row["scheduled"]) == (card, -PAYMENT, 0)
    assert _leg_sum(conn, tid) == -PAYMENT
    assert _mirror(conn, tid)["account_id"] == loan
    # The one-off pinned checking as the default; inference would now say Visa.
    assert loans.get_loan_params(conn, loan).funding_account_id == chk
    assert loans.infer_funding_account(conn, loan) == card
    assert loans.funding_account(conn, loan) == chk
    assert scheduled.list_loan_schedules(conn, on_or_after="2026-02-15")[0][
        "account_name"] == "Checking → Mortgage"
    assert loans_schedule.next_due_date(conn, loan, "2026-02-15") == "2026-04-01"


def test_enter_restates_a_standing_pre_entry_instead_of_doubling_it(conn, house):
    chk, card, loan = house["chk"], house["card"], house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    tid = loans_schedule.enter_payment(conn, loan, "2026-03-03", amount_cents=1300_00,
                                       payee="US Bank Mortgage")
    assert tid == pid
    row = ledger.get_transaction(conn, pid)
    assert (row["date"], row["amount"], row["payee"], row["scheduled"]) == \
        ("2026-03-03", -1300_00, "US Bank Mortgage", 0)
    assert _leg_sum(conn, pid) == -1300_00
    assert _mirror(conn, pid)["scheduled"] == 0
    assert loans_schedule.standing_pre_entries(conn, loan) == []
    # Standing on checking, paid from the card: the placeholder moves.
    pid2 = loans_schedule.create_pending_payment(conn, loan, "2026-04-01")
    tid2 = loans_schedule.enter_payment(conn, loan, "2026-04-01", funding_account_id=card)
    assert ledger.get_transaction(conn, tid2)["account_id"] == card   # (id may be reused)
    assert loans_schedule.standing_pre_entries(conn, loan) == []
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=? "
                        "AND date='2026-04-01'", (chk,)).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# Loan Setup changes reach the rows already generated
# ---------------------------------------------------------------------------
def test_realign_moves_standing_pre_entries_to_the_new_funder(conn, house):
    chk, sav, loan = house["chk"], house["sav"], house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    assert ledger.get_transaction(conn, pid)["account_id"] == chk
    loans.set_funding_account(conn, loan, sav)
    loans.add_payment_change(conn, loan, "2026-03-01", 1300_00)
    new = loans_schedule.realign_pending_payments(conn, loan)
    assert len(new) == 1
    row = ledger.get_transaction(conn, new[0])
    assert (row["account_id"], row["date"], row["amount"], row["scheduled"]) == \
        (sav, "2026-03-01", -1300_00, 1)
    assert _mirror(conn, new[0])["scheduled"] == 1
    assert loans_schedule.pending_payment(conn, loan)[2] == sav
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE scheduled=1").fetchone()["c"] == 2


# ---------------------------------------------------------------------------
# downloads meeting the pre-entries
# ---------------------------------------------------------------------------
def test_funding_download_with_a_changed_amount_merges_and_flags_the_change(conn, house):
    chk, loan = house["chk"], house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    res = importers.import_records(conn, [NormalizedTxn(
        external_account="Checking", account_type="checking", date="2026-03-02",
        amount_cents=-1300_00, payee="US BANK MTG PMT", fitid="M-3")], provider="test")
    assert (res.matched, res.added) == (1, 0)
    row = ledger.get_transaction(conn, pid)
    assert (row["amount"], row["date"], row["scheduled"], row["fitid"], row["payee"]) == \
        (-1300_00, "2026-03-02", 0, "M-3", "US Bank")
    assert _leg_sum(conn, pid) == -1300_00
    assert len(res.payment_changes) == 1
    ch = res.payment_changes[0]
    assert (ch.pending_id, ch.scheduled_date, ch.expected_amount, ch.actual_amount) == \
        (pid, "2026-03-01", PAYMENT, 1300_00)
    assert ch.delta == 1300_00 - PAYMENT
    assert loans_schedule.standing_pre_entries(conn, loan) == []


def test_bill_placeholder_merges_on_payee_when_the_amount_moves(conn, house):
    chk = house["chk"]
    cat = ledger.resolve_category(conn, "Utilities")
    scheduled.add_scheduled(conn, chk, payee="Power Co", amount=-80_00,
                            frequency="monthly", next_date="2026-03-05", category_id=cat)
    scheduled.generate_all_due(conn, "2026-03-01")
    placeholder = conn.execute(
        "SELECT id FROM transactions WHERE scheduled=1 AND payee='Power Co'").fetchone()["id"]
    res = importers.import_records(conn, [
        NormalizedTxn(external_account="Checking", account_type="checking", date="2026-03-06",
                      amount_cents=-93_27, payee="POWER CO ONLINE PMT", fitid="P-1"),
        NormalizedTxn(external_account="Checking", account_type="checking", date="2026-03-06",
                      amount_cents=-93_27, payee="Grocer", fitid="G-1")], provider="test")
    assert (res.matched, res.added) == (1, 1)
    row = ledger.get_transaction(conn, placeholder)
    assert (row["amount"], row["payee"], row["category_id"], row["scheduled"],
            row["cleared"], row["fitid"]) == (-93_27, "Power Co", cat, 0, 1, "P-1")
    # Only the loan pre-entry (and its mirror) generated alongside still stands.
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE scheduled=1 "
                        "AND payee='Power Co'").fetchone()["c"] == 0


def test_lenders_download_confirms_the_pending_mirror_not_a_second_payment(conn, house):
    chk, loan = house["chk"], house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    mirror = _mirror(conn, pid)
    principal = mirror["amount"]
    assert principal > 0 and mirror["scheduled"] == 1
    before = conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                          (loan,)).fetchone()["c"]
    res = importers.import_records(conn, [NormalizedTxn(
        external_account="Mortgage", account_type="liability", date="2026-03-01",
        amount_cents=principal, payee="PAYMENT - THANK YOU", fitid="L-3")], provider="test")
    assert (res.matched, res.added) == (1, 0)
    after = ledger.get_transaction(conn, mirror["id"])
    assert (after["scheduled"], after["cleared"], after["fitid"],
            after["transfer_account_id"]) == (0, 1, "L-3", chk)
    assert ledger.get_splits(conn, mirror["id"]) == []          # still one side of a transfer
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (loan,)).fetchone()["c"] == before
    # The checking side is still waiting for its own download, then posts.
    assert ledger.get_transaction(conn, pid)["scheduled"] == 1
    importers.import_records(conn, [NormalizedTxn(
        external_account="Checking", account_type="checking", date="2026-03-01",
        amount_cents=-PAYMENT, payee="US BANK MTG PMT", fitid="M-3")], provider="test")
    assert ledger.get_transaction(conn, pid)["scheduled"] == 0
    assert loans_schedule.standing_pre_entries(conn, loan) == []
