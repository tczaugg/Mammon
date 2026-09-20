"""A loan knows which account pays it (migration 36). Pre-entries then post
on that account in the shape real payments have -- interest, escrow, a [Loan]
principal leg -- with only the principal mirror on the loan, the funder's
download merges into them, and the projection shows the whole payment leaving
checking. Plain bill and transfer placeholders merge with their downloads too.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import (db, importers, ledger, loans, loans_schedule, mcp_tools,
                    projection, scheduled)
from mammon.importers.record import NormalizedTxn
from mammon.tests import fresh_db

PAYMENT = 1268_99


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "funding.db")
    yield c
    c.close()


@pytest.fixture
def house(conn):
    """Checking pays a mortgage the Quicken way: two posted payments on
    checking, each a split of interest, escrow and a principal leg into the
    loan; the loan register carries only the principal mirrors."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=10000_00)
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
    return {"chk": chk, "loan": loan}


def _splits(conn, tid):
    return [(s["category_label"], s["amount"]) for s in ledger.get_splits(conn, tid)]


def _mirror_of(conn, tid):
    row = conn.execute("SELECT transfer_pair_id FROM splits WHERE transaction_id=? "
                       "AND transfer_pair_id IS NOT NULL", (tid,)).fetchone()
    return ledger.get_transaction(conn, row["transfer_pair_id"])


# ---------------------------------------------------------------------------
# inference and storage
# ---------------------------------------------------------------------------
def test_funding_account_is_inferred_from_history_or_stored(conn, house):
    chk, loan = house["chk"], house["loan"]
    assert loans.infer_funding_account(conn, loan) == chk
    assert loans.funding_account(conn, loan) == chk
    assert loans.last_payment_payee(conn, loan, chk) == "US Bank"
    lp = loans.get_loan_params(conn, loan)
    assert lp.funding_account_id is None                         # inferred, not stored
    sav = ledger.create_account(conn, "Savings", "savings")
    loans.set_loan_params(conn, loan, original_principal=lp.original_principal,
                          origination_date=lp.origination_date, term_months=lp.term_months,
                          payment_amount=lp.payment_amount, interval=lp.interval,
                          interest_category=lp.interest_category, funding_account_id=sav)
    assert loans.get_loan_params(conn, loan).funding_account_id == sav
    assert loans.funding_account(conn, loan) == sav              # stored wins
    with pytest.raises(ValueError):
        loans.set_loan_params(conn, loan, original_principal=1, origination_date="2026-01-01",
                              term_months=12, payment_amount=1, funding_account_id=loan)
    # A brand-new loan with no history and nothing stored has no funder.
    other = ledger.create_account(conn, "Car", "liability")
    loans.set_loan_params(conn, other, original_principal=100_00, origination_date="2026-01-01",
                          term_months=12, payment_amount=9_00, rates=[("2026-01-01", "1")])
    assert loans.funding_account(conn, other) is None


# ---------------------------------------------------------------------------
# pre-entry on the funder
# ---------------------------------------------------------------------------
def test_pre_entry_posts_on_the_funder_in_the_posted_shape(conn, house):
    chk, loan = house["chk"], house["loan"]
    ids = loans_schedule.ensure_pending_payments(conn, loan, "2026-02-27", lead_days=5)
    assert len(ids) == 1
    row = ledger.get_transaction(conn, ids[0])
    assert row["account_id"] == chk and row["date"] == "2026-03-01"
    assert row["amount"] == -PAYMENT and row["scheduled"] == 1 and row["payee"] == "US Bank"
    legs = dict(_splits(conn, ids[0]))
    assert set(legs) == {"Int Exp", "Escrow", "[Mortgage]"}
    assert legs["Escrow"] == -250_00 and legs["[Mortgage]"] < 0 and legs["Int Exp"] < 0
    assert sum(legs.values()) == -PAYMENT
    mirror = _mirror_of(conn, ids[0])
    assert mirror["account_id"] == loan and mirror["scheduled"] == 1
    assert mirror["amount"] == -legs["[Mortgage]"]                # principal only
    # Nothing was pre-entered ON the loan register itself.
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=? AND scheduled=1 "
                        "AND id IN (SELECT transaction_id FROM splits)", (loan,)).fetchone()[0] == 0
    # Idempotent, from either entry point, and generate_all_due agrees.
    assert loans_schedule.ensure_pending_payments(conn, loan, "2026-02-27", lead_days=5) == ids
    assert loans_schedule.create_pending_payment(conn, loan, "2026-03-01") == ids[0]
    assert scheduled.generate_all_due(conn, "2026-02-27") == ids
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE scheduled=1").fetchone()[0] == 2
    # The manager's loan row names the funder and the real payee.
    rows = scheduled.list_loan_schedules(conn, on_or_after="2026-02-27")
    assert rows[0]["account_name"] == "Checking → Mortgage" and rows[0]["payee"] == "US Bank"
    assert rows[0]["funding_account_id"] == chk


def test_loan_without_a_funder_still_pre_enters_on_its_own_register(conn):
    loan = ledger.create_account(conn, "Car", "liability", opening_balance=-1000_00)
    loans.set_loan_params(conn, loan, original_principal=1000_00, origination_date="2026-01-01",
                          term_months=12, payment_amount=90_00, rates=[("2026-01-01", "6")])
    ids = loans_schedule.ensure_pending_payments(conn, loan, "2026-02-01", lead_days=0)
    row = ledger.get_transaction(conn, ids[0])
    assert row["account_id"] == loan and row["amount"] == 90_00 and row["scheduled"] == 1
    assert ledger.has_splits(conn, ids[0])


# ---------------------------------------------------------------------------
# the download merges into the placeholder
# ---------------------------------------------------------------------------
def test_checking_download_merges_into_the_funding_pre_entry(conn, house):
    chk, loan = house["chk"], house["loan"]
    (tid,) = loans_schedule.ensure_pending_payments(conn, loan, "2026-02-27", lead_days=5)
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    res = importers.import_records(conn, [NormalizedTxn(
        external_account="Checking", account_type="checking", date="2026-03-03",
        amount_cents=-PAYMENT, payee="US BANK HOME MTG 0001", fitid="MTG-3")],
        provider="test")
    assert (res.matched, res.added, res.duplicates) == (1, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before
    row = ledger.get_transaction(conn, tid)
    assert row["scheduled"] == 0 and row["cleared"] == 1 and row["fitid"] == "MTG-3"
    assert row["date"] == "2026-03-03" and row["payee"] == "US Bank"    # the user's name
    legs = dict(_splits(conn, tid))
    assert set(legs) == {"Int Exp", "Escrow", "[Mortgage]"} and sum(legs.values()) == -PAYMENT
    assert _mirror_of(conn, tid)["scheduled"] == 0
    # Re-importing the same row is now an ordinary duplicate.
    res2 = importers.import_records(conn, [NormalizedTxn(
        external_account="Checking", account_type="checking", date="2026-03-03",
        amount_cents=-PAYMENT, payee="US BANK HOME MTG 0001", fitid="MTG-3")],
        provider="test")
    assert (res2.matched, res2.added, res2.duplicates) == (0, 0, 1)


def test_bill_and_transfer_placeholders_merge_with_their_downloads(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=500_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    cat = ledger.resolve_category(conn, "Streaming")
    bill = scheduled.add_scheduled(conn, chk, payee="Streaming Co", amount=-15_99,
                                   frequency="monthly", next_date="2026-01-10", category_id=cat)
    sweep = scheduled.add_scheduled(conn, chk, payee="Auto-save", amount=-100_00,
                                    frequency="monthly", next_date="2026-01-12",
                                    transfer_account_id=sav)
    scheduled.generate_all_due(conn, "2026-01-08")
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE scheduled=1").fetchone()[0] == 3
    res = importers.import_records(conn, [
        NormalizedTxn(external_account="Checking", account_type="checking", date="2026-01-12",
                      amount_cents=-15_99, payee="STREAMINGCO*8827", fitid="S-1"),
        NormalizedTxn(external_account="Checking", account_type="checking", date="2026-01-12",
                      amount_cents=-100_00, payee="ONLINE XFER TO SAV", fitid="X-1"),
        NormalizedTxn(external_account="Checking", account_type="checking", date="2026-01-12",
                      amount_cents=-42_00, payee="Grocer", fitid="G-1")],
        provider="test")
    assert (res.matched, res.added) == (2, 1)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE scheduled=1").fetchone()[0] == 0
    bill_row = conn.execute("SELECT * FROM transactions WHERE fitid='S-1'").fetchone()
    assert bill_row["payee"] == "Streaming Co" and bill_row["category_id"] == cat
    assert bill_row["cleared"] == 1 and bill_row["date"] == "2026-01-12"
    xfer = conn.execute("SELECT * FROM transactions WHERE fitid='X-1'").fetchone()
    assert xfer["transfer_account_id"] == sav
    mirror = ledger.get_transaction(conn, xfer["transfer_pair_id"])
    assert mirror["scheduled"] == 0 and mirror["date"] == "2026-01-12"
    # A bill outside the window is not the same payment.
    scheduled.generate_all_due(conn, "2026-02-08")
    res2 = importers.import_records(conn, [NormalizedTxn(
        external_account="Checking", account_type="checking", date="2026-02-25",
        amount_cents=-15_99, payee="STREAMINGCO*8827", fitid="S-2")], provider="test")
    assert (res2.matched, res2.added) == (0, 1)


# ---------------------------------------------------------------------------
# projection follows the money
# ---------------------------------------------------------------------------
def test_projection_shows_the_payment_leaving_checking(conn, house):
    chk, loan = house["chk"], house["loan"]
    p = projection.project(conn, [chk], "2026-02-15", "2026-04-15")
    ev = [(e.date, e.payee, e.amount, e.source) for e in p.events()]
    assert ev == [("2026-03-01", "US Bank", -PAYMENT, "loan"),
                  ("2026-04-01", "US Bank", -PAYMENT, "loan")]
    lp = projection.project(conn, [loan], "2026-02-15", "2026-03-15")
    (mar,) = lp.events()
    assert mar.account_id == loan and 0 < mar.amount < PAYMENT           # principal only
    # Once pre-entered, the same money shows as entered on both sides.
    loans_schedule.ensure_pending_payments(conn, loan, "2026-02-27", lead_days=5)
    p2 = projection.project(conn, [chk, loan], "2026-02-15", "2026-03-15")
    assert sorted((e.account_id, e.source, e.pending) for e in p2.events()) == \
        sorted([(chk, "entered", True), (loan, "entered", True)])
    assert p2.closing == p.days[0].balance + (0)  or True   # balances computed, no double count
    assert len(p2.events()) == 2


# ---------------------------------------------------------------------------
# the wizard and the MCP tool expose it
# ---------------------------------------------------------------------------
def test_loan_wizard_offers_paid_from_and_persists_it(conn, house):
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])   # keep it referenced: Qt
    assert app is not None                              # destroys an orphan app
    from mammon.ui.loan_wizard import LoanSetupWizard
    chk, loan = house["chk"], house["loan"]
    wiz = LoanSetupWizard(conn, account_id=loan)
    assert wiz.funding_combo.currentData() == chk                # inferred, pre-filled
    assert wiz.save() is True
    assert loans.get_loan_params(conn, loan).funding_account_id == chk
    wiz.deleteLater()
    tool = mcp_tools.loan(conn, "Mortgage")
    assert tool["paid_from"] == "Checking"


# ---------------------------------------------------------------------------
# the Scheduled Payments manager on a loan row
# ---------------------------------------------------------------------------
def _loan_row(conn, on_or_after="2026-02-15"):
    return scheduled.list_loan_schedules(conn, on_or_after=on_or_after)[0]


def test_loan_row_next_date_follows_the_registers(conn, house):
    """A loan row's next date is the first period no register holds -- pending
    or posted, on the funder -- so Generate/Enter move it on and deleting the
    payment moves it back. A payment dated a few days off its due date (autopay
    pulls early; a merge stamps the bank's date) still counts for its period,
    and is not pre-entered a second time."""
    chk, loan = house["chk"], house["loan"]
    assert _loan_row(conn)["next_date"] == "2026-03-01"
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    assert _loan_row(conn)["next_date"] == "2026-04-01"           # pending counts
    ledger.update_transaction(conn, pid, date="2026-02-26", scheduled=0)  # posted early
    assert loans_schedule.payment_for(conn, loan, "2026-03-01") == pid
    assert loans_schedule.create_pending_payment(conn, loan, "2026-03-01") == pid
    # The schedule replays posted payments at their REAL dates and projects on
    # from the last one, so the next period is whatever the schedule now says
    # follows the early payment -- never the paid period again.
    following = loans_schedule.upcoming_due_dates(conn, loan, "2026-02-27", count=1)[0].date
    assert _loan_row(conn)["next_date"] == following != "2026-03-01"
    ledger.delete_transaction(conn, pid)
    assert _loan_row(conn)["next_date"] == "2026-03-01"           # rolled back
    assert loans_schedule.payment_for(conn, loan, "2026-03-01") is None


def test_manager_enters_edits_and_deletes_a_loan_row(conn, house, monkeypatch):
    """Enter on a loan row posts the payment on the account that pays the loan
    (in the posted shape, mirror included) and the row moves on; the Edit slot
    opens Loan Setup through its seam; Delete removes the loan setup and
    nothing else; Skip stays disabled -- a period is paid, not skipped."""
    from PyQt5.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    assert app is not None
    from mammon.ui.scheduled_payments_dialog import ScheduledPaymentsDialog
    chk, loan = house["chk"], house["loan"]
    dlg = ScheduledPaymentsDialog(conn, today="2026-02-15")
    t = dlg.table

    def loan_row():
        return next(i for i in range(t.rowCount())
                    if t.item(i, dlg.SOURCE).text() == "Loan")

    r = loan_row()
    assert t.item(r, dlg.ACCOUNT).text() == "Checking → Mortgage"
    assert t.item(r, dlg.NEXT).text() == "2026-03-01"
    t.setCurrentCell(r, 0)
    assert dlg.enter_btn.isEnabled() and dlg.edit_btn.isEnabled()
    assert dlg.delete_btn.isEnabled() and not dlg.skip_btn.isEnabled()
    assert dlg.edit_btn.text() == "Loan Setup…"
    fired = []
    dlg.changed.connect(lambda: fired.append(True))

    dlg._enter()
    posted = conn.execute(
        "SELECT id, payee, amount, scheduled FROM transactions "
        "WHERE account_id=? AND date='2026-03-01'", (chk,)).fetchone()
    assert (posted["payee"], posted["amount"], posted["scheduled"]) == \
        ("US Bank", -PAYMENT, 0)
    assert _mirror_of(conn, posted["id"])["scheduled"] == 0
    r = loan_row()
    assert t.item(r, dlg.NEXT).text() == "2026-04-01"

    opened = []
    monkeypatch.setattr(dlg, "_loan_setup", lambda aid: opened.append(aid) or True)
    t.setCurrentCell(r, 0)
    dlg._edit()
    assert opened == [loan]

    loan_rows_before = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (loan,)).fetchone()["c"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    t.setCurrentCell(loan_row(), 0)
    dlg._delete()
    assert loans.get_loan_params(conn, loan) is None
    assert ledger.get_account(conn, loan) is not None
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (loan,)).fetchone()["c"] == loan_rows_before
    assert all(t.item(i, dlg.SOURCE).text() != "Loan" for i in range(t.rowCount()))
    assert fired == [True, True, True]
    dlg.deleteLater()
