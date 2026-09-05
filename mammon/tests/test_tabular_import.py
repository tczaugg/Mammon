"""Generic tabular-import engine (mammon.importers.tabular).

Locks in the behaviour the user asked for: a downloaded/scraped delimited file whose
structure the alias guesser can't read (a preamble above the header, a footer
below the data, unfamiliar column names, split From/To counterparties, ``+ $`` /
``- $`` money) now imports correctly -- real transactions with correct signed
amounts, dates, payees and descriptions, NO blank rows -- through the REVIEW
queue, and a new/changed format is previewed + resolved via a wizard and saved as
a profile so it never prompts again. OFX/QIF are untouched (their own parsers).

The Venmo statement CSV (the in-flight adapter) is subsumed here as a saved-
profile regression fixture: two title rows, a blank first column, ``+ $`` / ``- $``
amounts, an ISO datetime, and a multi-line disclaimer footer.
"""
from __future__ import annotations

import pytest

from mammon import db, import_review, importers, ledger
from mammon.importers import csvimp, tabular


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _counts(conn):
    cash = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    inv = conn.execute("SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
    return cash, inv


# ---------------------------------------------------------------------------
# scalar parsers
# ---------------------------------------------------------------------------
def test_currency_parser_all_shapes():
    p = tabular.parse_money
    assert p("$1,234.56") == 123456
    assert p("+ $2,250.00") == 225000          # Venmo positive
    assert p("- $30.00") == -3000              # Venmo negative
    assert p("(30.00)") == -3000               # accounting parens
    assert p("100.00 CR") == 10000             # credit -> positive
    assert p("100.00 DR") == -10000            # debit -> negative
    assert p("DR 45.10") == -4510
    assert p("") == 0 and p(None) == 0
    assert p("junk") == 0                       # never raises


def test_flexible_date_parser():
    d = tabular.parse_date_flex
    assert d("2026-06-04T15:21:49") == "2026-06-04"   # ISO datetime
    assert d("06/04/2026") == "2026-06-04"            # US
    assert d("June 26, 2026") == "2026-06-26"         # spelled month
    assert d("26 Jun 2026") == "2026-06-26"           # day-month
    assert d("25/12/2026") == "2026-12-25"            # unambiguous EU (day>12)
    assert d("") is None and d("not a date") is None
    assert d("113-1234567-7654321") is None           # order number, not a date


# ---------------------------------------------------------------------------
# structure detection: preamble + footer + blank rows
# ---------------------------------------------------------------------------
PREAMBLE_FOOTER_CSV = (
    "My Bank -- Downloaded Statement\n"                 # preamble line 1
    "Account 1234, generated 2026-07-01\n"              # preamble line 2
    "\n"                                                # blank
    "Txn Date,Details,Money\n"                          # REAL header (unknown vocab)
    "2026-06-01,Coffee Shop,-4.50\n"
    "\n"                                                # interior blank row -> dropped
    "2026-06-02,Paycheck,2000.00\n"
    "\n"
    "Totals and disclaimers below -- not transactions\n"  # footer 1
    "Please retain for your records.\n"                   # footer 2
)


def test_preamble_and_footer_and_blank_rows_skipped():
    frame = tabular.locate_and_frame(PREAMBLE_FOOTER_CSV)
    assert frame.header == ["Txn Date", "Details", "Money"]
    assert frame.preamble_rows == 3           # 2 title lines + 1 blank above header
    assert frame.footer_rows == 3             # 1 blank + 2 trailing non-data lines
    recs = csvimp.parse_csv(PREAMBLE_FOOTER_CSV, default_account="Chk")
    assert [(r.date, r.amount_cents, r.payee) for r in recs] == [
        ("2026-06-01", -4_50, "Coffee Shop"),
        ("2026-06-02", 2000_00, "Paycheck"),
    ]
    # No blank rows leaked (the interior blank line produced no record).
    assert all(r.amount_cents or r.payee for r in recs)


def test_non_financial_scrape_yields_no_rows_not_blanks():
    # A headline scrape has no date column -> zero records, never blank ones.
    text = "headline\nRetirees face a new challenge\nMusk floored by dividend\n"
    assert csvimp.parse_csv(text, default_account="X") == []


# ---------------------------------------------------------------------------
# amount: signed single column vs separate debit/credit
# ---------------------------------------------------------------------------
def test_signed_single_amount_inferred():
    # unknown vocabulary (no "Date"/"Amount" alias) -> inference by value.
    text = "Trans Date,Who,Net\n2026-01-05,Store A,-25.00\n2026-01-06,Refund,15.00\n"
    recs = csvimp.parse_csv(text, default_account="A")
    assert sorted(r.amount_cents for r in recs) == [-25_00, 15_00]
    assert {r.payee for r in recs} == {"Store A", "Refund"}


def test_separate_debit_credit_folded_to_signed():
    text = ("When,Who,Debit,Credit\n"
            "2026-01-05,Groceries,25.00,\n"
            "2026-01-10,Salary,,200.00\n")
    recs = csvimp.parse_csv(text, default_account="A")
    by = {r.payee: r.amount_cents for r in recs}
    assert by == {"Groceries": -25_00, "Salary": 200_00}


# ---------------------------------------------------------------------------
# preview -> wizard -> accept -> save profile LOOP
# ---------------------------------------------------------------------------
# A rewards export whose single money column is POSITIVE for spend (sign inverted
# vs a ledger) and whose merchant is split across two columns.
REWARDS_CSV = (
    "Activity Date,Store,City,Spend\n"
    "2026-03-01,BLUE BOTTLE,SEATTLE,4.50\n"
    "2026-03-02,COSTCO,ANYTOWN,120.00\n"
)


def test_preview_wizard_profile_save_loop(conn):
    plan = tabular.plan_tabular(conn, REWARDS_CSV, default_account="Card",
                                account_type="credit")
    # First sight of a new format: flagged new, with a preview to show the user.
    assert plan.is_new is True
    assert len(plan.preview) == 2
    assert plan.preview[0]["date"] == "2026-03-01"

    # The user runs the wizard: the money column is spend (invert the sign) and the
    # payee lives INSIDE the description (Store + City concatenated), no payee col.
    plan2 = tabular.apply_wizard_answers(plan, {
        "amount_mode": "signed", "amount_col": "Spend", "invert_amount": True,
        "payee_mode": "in_description", "description_cols": ["Store", "City"],
    })
    assert plan2.is_new is True                     # not saved until accepted
    got = [(r.date, r.amount_cents, r.payee, r.memo) for r in plan2.records]
    assert got == [
        ("2026-03-01", -4_50, "BLUE BOTTLE SEATTLE", "BLUE BOTTLE SEATTLE"),
        ("2026-03-02", -120_00, "COSTCO ANYTOWN", "COSTCO ANYTOWN"),
    ]

    # Accept -> profile saved. Re-planning the SAME format now imports with no
    # prompt and reuses the wizard's mapping.
    tabular.accept_profile(conn, plan2, name="My Rewards Card")
    plan3 = tabular.plan_tabular(conn, REWARDS_CSV, default_account="Card")
    assert plan3.is_new is False
    assert plan3.name == "My Rewards Card"
    assert [r.amount_cents for r in plan3.records] == [-4_50, -120_00]
    assert plan3.records[0].payee == "BLUE BOTTLE SEATTLE"


def test_fingerprint_stable_across_exports():
    a = tabular.locate_and_frame(REWARDS_CSV)
    b = tabular.locate_and_frame(
        "Activity Date,Store,City,Spend\n2026-09-09,TARGET,ANYTOWN,9.99\n")
    assert tabular.fingerprint(a.header) == tabular.fingerprint(b.header)
    # A changed layout gets a different signature (so it re-prompts).
    c = tabular.locate_and_frame("Date,Store,Spend\n2026-09-09,TARGET,9.99\n")
    assert tabular.fingerprint(a.header) != tabular.fingerprint(c.header)


# ---------------------------------------------------------------------------
# Venmo statement -- saved-profile regression fixture
# ---------------------------------------------------------------------------
VENMO_CSV = (
    "Account Statement - (@SampleUser) ,,,,,,,,,,,,,,,,,,,,,\n"
    "Account Activity,,,,,,,,,,,,,,,,,,,,,\n"
    ",ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (tip),"
    "Amount (tax),Amount (fee),Tax Rate,Tax Exempt,Funding Source,Destination,"
    "Beginning Balance,Ending Balance,Statement Period Venmo Fees,"
    "Terminal Location,Year to Date Venmo Fees,Disclaimer\n"
    ",,,,,,,,,,,,,,,,$0.00,,,,,\n"
    ",1000000000000000001,2026-06-04T15:21:49,Payment,Complete,June 2026 Rent,"
    "Jamie Chen,Sam Rivera,\"+ $2,250.00\",,0,,0,,,Venmo balance,,,,,Venmo,\n"
    ",1000000000000000002,2026-06-05T18:57:28,Standard Transfer,Issued,,,,"
    "\"- $2,250.00\",,,,,,,ANYTOWN FEDERAL CREDIT UNION *0000,,,,,Venmo,\n"
    ",1000000000000000003,2026-06-20T20:19:38,Payment,Complete,Bridal shower,"
    "Sam Rivera,Jamie Park,- $30.00,,0,,0,,,,,,,,Venmo,\n"
    ",,,,,,,,,,,,,,,,,$0.00,$0.00,,$0.00,\"In case of errors or questions about\n"
    "        your electronic transfers, telephone 855-812-4430.\n"
    "        \"\n"
)


def _venmo_by_amount(recs):
    return {r.amount_cents: r for r in recs}


def test_venmo_parses_signed_directional_no_blanks():
    recs = csvimp.parse_csv(VENMO_CSV, default_account="Venmo")
    assert len(recs) == 3
    assert all(r.date.startswith("2026-06") for r in recs)
    assert sorted(r.amount_cents for r in recs) == [-2250_00, -30_00, 2250_00]
    by = _venmo_by_amount(recs)
    # incoming payment -> counterparty is the From person; outgoing -> the To person
    assert by[2250_00].payee == "Jamie Chen"
    assert by[-30_00].payee == "Jamie Park"
    # a bank transfer with no From/To names itself from its Type
    assert by[-2250_00].payee == "Standard Transfer"
    # Note becomes the description; no blank rows, no bypassed amount
    assert by[2250_00].memo == "June 2026 Rent"
    assert all(r.amount_cents != 0 for r in recs)


def test_venmo_imports_via_saved_profile_through_review(conn, tmp_path):
    aid = ledger.create_account(conn, "Venmo", "checking")

    # First import: format looks new; accept it to save the profile.
    plan = tabular.plan_tabular(conn, VENMO_CSV, default_account="Venmo")
    assert plan.is_new is True
    tabular.accept_profile(conn, plan, name="Venmo statement")

    # Second import goes through the REAL review path via a file on disk, using the
    # saved profile with no prompt -- and nothing hits the register until accepted.
    p = _write(tmp_path, "VenmoStatement_June_2026.csv", VENMO_CSV)
    entries = importers.import_single_account(
        conn, account="Venmo", path=p, finalize=False)
    assert _counts(conn) == (0, 0)                    # review-only, no bypass
    assert len(entries) == 3
    amts = sorted(e.mapped.amount_cents for e in entries)
    assert amts == [-2250_00, -30_00, 2250_00]
    # profile is now known (no re-prompt)
    assert tabular.plan_tabular(conn, VENMO_CSV, default_account="Venmo").is_new is False


def test_venmo_lands_in_review_and_accepts_to_register(conn):
    aid = ledger.create_account(conn, "Venmo", "checking")
    recs = csvimp.parse_csv(VENMO_CSV, default_account="Venmo")
    entries = import_review.build_review_from_records(conn, aid, recs)
    assert _counts(conn) == (0, 0)
    import_review.persist_entries(conn, aid, entries)
    accepted = import_review.accept_all(conn, aid)
    assert accepted == 3
    assert ledger.account_balance(conn, aid) == 2250_00 - 2250_00 - 30_00


# ---------------------------------------------------------------------------
# saved profile can override auto-inference (e.g. a corrected sign)
# ---------------------------------------------------------------------------
def test_saved_profile_roles_override_inference(conn):
    text = "Trans Date,Who,Net\n2026-01-05,Store A,25.00\n"
    # auto: positive
    assert csvimp.parse_csv(text, default_account="A")[0].amount_cents == 25_00
    # a saved/wizard profile that inverts the sign is honoured by parse_csv
    roles = tabular.infer_cash_roles(*_frame_hr(text))
    roles.invert_amount = True
    assert csvimp.parse_csv(text, default_account="A", roles=roles)[0].amount_cents == -25_00


def _frame_hr(text):
    f = tabular.locate_and_frame(text)
    return f.header, f.rows


# ---------------------------------------------------------------------------
# OFX/QIF are exempt -- the engine never touches them
# ---------------------------------------------------------------------------
def test_ofx_qif_untouched_by_tabular_engine():
    # locate_and_frame is CSV/tabular only; an OFX/QIF blob has no delimited table
    # of dated rows, so the engine declines it (returns None) rather than mangling.
    ofx = "<OFX><BANKMSGSRSV1><STMTTRN><DTPOSTED>20260601</DTPOSTED></STMTTRN>"
    assert tabular.locate_and_frame(ofx) is None
