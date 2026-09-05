"""File imports (CSV/OFX) go through the IMPORT REVIEW queue, never straight to
the register, with alias-based field mapping and investment share/price/total
derivation.

the user's bug: a CSV imported into an account landed directly in the register instead
of the review list that downloaded/scraped data uses. These tests lock in that a
file import now:

  * lands every row in review (build_review_from_records / import_single_account
    finalize=False) and writes NOTHING to ``transactions`` / ``investment_transactions``
    until a row is accepted;
  * maps date / payee / amount through header ALIASES (case-insensitive, trimmed),
    incl. separate Debit/Credit columns folded into one signed amount;
  * carries INVESTMENT rows with their security fields, deriving the third of
    (shares, price-per-share, total) from any two.

The header spellings used here mirror the REAL example files in the user's
Downloads (reported in the task result): the "Posted Date,Reference Number,Payee,
Address,Amount" bank export, the "DATE,DESCRIPTION,AMOUNT,CHECK #,STATUS" credit
card export, and the Fidelity NetBenefits 401k "Date,Investment,Transaction Type,
Shares/Unit,Amount ($)" download (BOM + leading blank lines + a trailing legal
footer).
"""
from __future__ import annotations

import pytest

from mammon import db, import_review, importers, ledger
from mammon.importers import csvimp
from mammon.importers.record import derive_investment_amounts


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Checking", "checking")


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _counts(conn):
    cash = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    inv = conn.execute("SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
    return cash, inv


# ---------------------------------------------------------------------------
# 1. CSV with Payee + Amount  (real header: Anytown CU "Posted Date,...")
# ---------------------------------------------------------------------------
CSV_PAYEE_AMOUNT = """Posted Date,Reference Number,Payee,Address,Amount
05/07/2026,10000000000000000000001,"BA ELECTRONIC PAYMENT","",49.92
04/11/2026,10000000000000000000002,"ANON GROCERY #0210 FUEL","ANYTOWN UT",-49.92
"""


def test_csv_payee_amount_lands_in_review_not_register(conn, account):
    recs = csvimp.parse_csv(CSV_PAYEE_AMOUNT, default_account="Checking")
    entries = import_review.build_review_from_records(conn, account, recs)

    assert _counts(conn) == (0, 0)                    # nothing written to register
    assert len(entries) == 2
    by_amt = {e.mapped.amount_cents: e.mapped for e in entries}
    assert by_amt[49_92].payee == "BA ELECTRONIC PAYMENT"
    assert by_amt[49_92].date == "2026-05-07"
    assert by_amt[-49_92].payee == "ANON GROCERY #0210 FUEL"
    assert all(e.is_new for e in entries)


# ---------------------------------------------------------------------------
# 2. CSV with separate Debit / Credit columns -> one signed amount
# ---------------------------------------------------------------------------
CSV_DEBIT_CREDIT = """Transaction Date,Description,Debit,Credit,Category
01/05/2026,Safeway,25.00,,Groceries
01/10/2026,Paycheck,,200.00,Salary
"""


def test_csv_debit_credit_split_signs_amount(conn, account):
    recs = csvimp.parse_csv(CSV_DEBIT_CREDIT, default_account="Checking")
    entries = import_review.build_review_from_records(conn, account, recs)

    assert _counts(conn) == (0, 0)
    amts = sorted(e.mapped.amount_cents for e in entries)
    assert amts == [-25_00, 200_00]                   # debit negative, credit positive
    debit = next(e.mapped for e in entries if e.mapped.amount_cents < 0)
    assert debit.payee == "Safeway" and debit.date == "2026-01-05"


# ---------------------------------------------------------------------------
# 3. Investment CSV with shares + price -> derive total; lands in review
# ---------------------------------------------------------------------------
CSV_INVEST_SHARES_PRICE = """Trade Date,Symbol,Shares,Price
02/01/2026,AAPL,10,150.00
"""


def test_investment_csv_derives_total_and_lands_in_review(conn):
    aid = ledger.create_account(conn, "Brokerage", "investment")
    recs = csvimp.parse_csv(CSV_INVEST_SHARES_PRICE, default_account="Brokerage")
    entries = import_review.build_review_from_records(conn, aid, recs)

    assert _counts(conn) == (0, 0)                    # nothing in either register yet
    assert len(entries) == 1
    m = entries[0].mapped
    assert m.is_investment
    assert m.symbol == "AAPL" and m.quantity == "10" and m.price == "150.00"
    assert m.amount_cents == 150_000                  # 10 * 150.00 derived


def test_accept_investment_review_writes_investment_transaction(conn):
    aid = ledger.create_account(conn, "Brokerage", "investment")
    recs = csvimp.parse_csv(CSV_INVEST_SHARES_PRICE, default_account="Brokerage")
    entries = import_review.build_review_from_records(conn, aid, recs)
    import_review.persist_entries(conn, aid, entries)

    accepted = import_review.accept_all(conn, aid)
    assert accepted == 1
    cash, inv = _counts(conn)
    assert cash == 0 and inv == 1                      # posted to invest table, NOT cash
    row = conn.execute(
        "SELECT symbol, quantity, price, amount FROM investment_transactions"
    ).fetchone()
    assert row["symbol"] == "AAPL" and row["quantity"] == "10"
    assert row["price"] == "150.00" and row["amount"] == 150_000

    # Re-import the same statement -> identity dedup classifies MATCHING (no dup).
    again = import_review.build_review_from_records(
        conn, aid, csvimp.parse_csv(CSV_INVEST_SHARES_PRICE, default_account="Brokerage"))
    assert [e.label for e in again] == [import_review.LABEL_MATCHING]


# ---------------------------------------------------------------------------
# 3b. Real Fidelity NetBenefits 401k download: BOM + leading blanks + Shares/Unit
#     header + a trailing legal footer; shares + amount -> derive per-unit price.
# ---------------------------------------------------------------------------
CSV_401K_REAL = (
    "﻿\n\n"
    "Date,Investment,Transaction Type,Shares/Unit,Amount ($)\n"
    "10/08/2025,TARGET 2030 FUND,RECORDKEEPING FEE,\"-0.102\",\"-3.05\"\n"
    "10/08/2025,TARGET 2030 FUND,Change in Market Value,0,1.85\n"
    "\n"
    "\"The data and information in this spreadsheet is provided to you solely for your use.\"\n"
    "Date downloaded 08/26/2026 08:58 pm\n"
)


def test_real_401k_header_parses_and_derives_price(conn):
    aid = ledger.create_account(conn, "NetBenefits", "investment")
    recs = csvimp.parse_csv(CSV_401K_REAL, default_account="NetBenefits")
    entries = import_review.build_review_from_records(conn, aid, recs)

    assert _counts(conn) == (0, 0)
    # BOTH data rows survive. The blank line + legal footer + "Date downloaded"
    # are skipped (unparseable date), not crashed on -- but a row with a real
    # date is never dropped for its ACTIVITY text, however non-transactional it
    # looks. A row the importer drops is one the user cannot see, and therefore
    # cannot correct or delete; "Change in Market Value" reaches review like
    # everything else and is discarded there, as it would be in Quicken.
    assert len(entries) == 2
    assert any("MARKET VALUE" in (e.mapped.action or "").upper() for e in entries)

    # A plan fee is MAPPED to ShrsOut, which is a guess about what this
    # institution means by "RECORDKEEPING FEE" -- the kind Quicken gets wrong
    # often enough that correcting it is routine. What makes the guess
    # survivable is that the row is visible and every field is editable before
    # accepting.
    # The file writes the fee SIGNED ("-0.102" / "-3.05"); the record layer
    # detects that convention and normalizes to the ledger's MAGNITUDES, with
    # ShrsOut carrying the direction (record.normalize_investment_signs) -- a
    # signed quantity on a ShrsOut would ADD shares on accept.
    fee = next(e.mapped for e in entries if e.mapped.symbol == "TARGET 2030 FUND"
               and e.mapped.amount_cents == 3_05)
    assert fee.action == "ShrsOut"
    assert fee.is_investment
    assert fee.quantity == "0.102"
    # price derived from |amount| / |shares| = 3.05 / 0.102.
    assert fee.price == "29.901961"


# ---------------------------------------------------------------------------
# 4. OFX -> lands in review (real structure: SGML 1.02 bank statement)
# ---------------------------------------------------------------------------
OFX_BANK = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<ACCTID>XXXX0000
<ACCTTYPE>CREDITLINE
</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260730090000
<TRNAMT>21.48
<FITID>20260730090001
<NAME>COSTCO WHSE #0001
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260807090000
<TRNAMT>-59.08
<FITID>20260807090002
<NAME>COSTCO GAS #0001
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""


def test_ofx_lands_in_review_not_register(conn, account):
    recs = importers.PARSERS["ofx"](OFX_BANK, default_account="Checking")
    entries = import_review.build_review_from_records(conn, account, recs)

    assert _counts(conn) == (0, 0)
    assert len(entries) == 2
    by_amt = {e.mapped.amount_cents: e.mapped for e in entries}
    assert by_amt[21_48].payee == "COSTCO WHSE #0001"
    assert by_amt[-59_08].date == "2026-08-07"
    # source FITID preserved for later dedupe
    assert by_amt[21_48].transaction_id == "20260730090001"


# ---------------------------------------------------------------------------
# 5. End-to-end via import_single_account(path=..., finalize=False): a file on
#    disk goes to review, writing NOTHING to the register.
# ---------------------------------------------------------------------------
def test_import_single_account_file_finalize_false_writes_nothing(conn, account, tmp_path):
    path = _write(tmp_path, "statement.csv", CSV_PAYEE_AMOUNT)
    entries = importers.import_single_account(
        conn, account="Checking", path=path, finalize=False)
    assert len(entries) == 2
    assert all(e.is_new for e in entries)
    assert _counts(conn) == (0, 0)                    # review only; register untouched


def test_import_single_account_ofx_file_finalize_false_writes_nothing(conn, account, tmp_path):
    path = _write(tmp_path, "statement.ofx", OFX_BANK)
    entries = importers.import_single_account(
        conn, account="Checking", path=path, finalize=False)
    assert len(entries) == 2
    assert _counts(conn) == (0, 0)


def test_investment_file_finalize_true_posts_via_review_pipeline(conn, tmp_path):
    """The headless investment path (used by the UI for investment accounts that
    have no interactive panel yet): a file goes through the review pipeline
    (classify + dedup + accept), posting into investment_transactions -- NOT the
    cash register -- and a re-import dedups instead of doubling."""
    ledger.create_account(conn, "Brokerage", "investment")
    path = _write(tmp_path, "inv.csv", CSV_INVEST_SHARES_PRICE)
    summary = importers.import_single_account(
        conn, account="Brokerage", account_type="investment", path=path)
    assert summary == {"added": 1, "matched": 0}
    cash, inv = _counts(conn)
    assert cash == 0 and inv == 1
    row = conn.execute("SELECT symbol, quantity, amount FROM investment_transactions").fetchone()
    assert row["symbol"] == "AAPL" and row["quantity"] == "10" and row["amount"] == 150_000

    # Re-import -> identity dedup, no second row.
    summary2 = importers.import_single_account(
        conn, account="Brokerage", account_type="investment", path=path)
    assert summary2 == {"added": 0, "matched": 1}
    assert _counts(conn) == (0, 1)


# ---------------------------------------------------------------------------
# Unit-level: derivation + alias behaviour
# ---------------------------------------------------------------------------
def test_derive_investment_amounts_any_two():
    # shares + price -> total
    assert derive_investment_amounts("10", "150", "") == ("10", "150", 150_000, True)
    # shares + total -> price
    q, p, cents, ok = derive_investment_amounts("0.102", "", "3.05")
    assert q == "0.102" and p == "29.901961" and cents == 3_05 and ok
    # price + total -> shares
    q, p, cents, ok = derive_investment_amounts("", "12.50", "500.00")
    assert q == "40" and p == "12.50" and cents == 500_00
    # all three present + consistent -> keep given (signed) total, flag consistent
    q, p, cents, ok = derive_investment_amounts("10", "100.00", "-1000.00")
    assert cents == -1000_00 and ok is True
    # all three present + INconsistent -> flagged
    _, _, _, ok = derive_investment_amounts("10", "100.00", "-1500.00")
    assert ok is False


def test_amount_alias_transactionAmount_and_value():
    # scrape field name still works; and a generic "Value" column maps to amount.
    rows = csvimp.parse_csv(
        "Date,Name,transactionAmount\n01/02/2026,Utility,-42.10\n",
        default_account="A")
    assert rows[0].amount_cents == -42_10 and rows[0].payee == "Utility"
    rows = csvimp.parse_csv(
        "Date,Description,Value\n01/02/2026,Refund,15.00\n", default_account="A")
    assert rows[0].amount_cents == 15_00 and rows[0].payee == "Refund"


def test_date_alias_and_payee_alias_case_insensitive():
    rows = csvimp.parse_csv(
        "POSTED,MEMO,AMOUNT\n2026-03-04,Coffee Shop,-4.50\n", default_account="A")
    assert rows[0].date == "2026-03-04"
    assert rows[0].payee == "Coffee Shop" and rows[0].amount_cents == -4_50
