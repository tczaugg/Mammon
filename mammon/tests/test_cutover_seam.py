"""Migration-seam dedup (gap G4).

The one-time full Quicken (QIF) history migration leaves every row with
fitid=NULL. The first live OFX/QFX pull for an account therefore cannot dedup
the overlap by fitid, and fuzzy/exact-tuple matching is not guaranteed to catch
it (the bank often reports a post date days off from Quicken's, or formats a
share quantity differently). Without a guard the overlap double-imports.

These tests assert that a per-account cutover (last-migrated) watermark, recorded
by the migration and enforced on later imports, drops the fitid-less overlap so
no duplicate transactions are created across the seam -- for a bank (cash)
account and for an investment account.
"""
from __future__ import annotations

import pytest

from mammon import db, importers, ledger
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


def _acct_id(conn, name):
    return conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()["id"]


def _n_txn(conn, aid):
    return conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (aid,)
    ).fetchone()["c"]


def _n_invtxn(conn, aid):
    return conn.execute(
        "SELECT COUNT(*) c FROM investment_transactions WHERE account_id=?", (aid,)
    ).fetchone()["c"]


def test_a_qif_set_defers_its_watermark_so_every_file_lands(conn):
    """The watermark is the migration's END, not a guard between the files of one
    set. A full history exported a year at a time is imported as a SET, and with
    ``set_cutover=False`` a file carrying EARLIER dates than one already imported
    still lands -- otherwise the first file to arrive would shut out the rest and
    report their rows as duplicates."""
    newer = [importers.NormalizedTxn(external_account="Checking",
                                     account_type="checking", date="1998-03-03",
                                     amount_cents=-1_200, payee="New Grocer")]
    older = [importers.NormalizedTxn(external_account="Checking",
                                     account_type="checking", date="1997-03-03",
                                     amount_cents=-1_000, payee="Old Grocer")]
    importers.import_records(conn, newer, provider="quicken", source_format="qif",
                             set_cutover=False)
    aid = _acct_id(conn, "Checking")
    # Deferred: nothing was watermarked, so the older file is not pre-empted.
    assert ledger.account_cutover_date(conn, aid) in (None, "")
    res = importers.import_records(conn, older, provider="quicken",
                                   source_format="qif", set_cutover=False)
    assert res.added == 1 and res.duplicates == 0
    assert _n_txn(conn, aid) == 2


def test_a_qif_set_imported_per_file_would_shut_out_the_rest(conn):
    """The reason the deferral exists, pinned as behaviour: with the watermark
    applied PER FILE (the default), a later year imported first makes every
    earlier row look already-migrated."""
    newer = [importers.NormalizedTxn(external_account="Checking",
                                     account_type="checking", date="1998-03-03",
                                     amount_cents=-1_200, payee="New Grocer")]
    older = [importers.NormalizedTxn(external_account="Checking",
                                     account_type="checking", date="1997-03-03",
                                     amount_cents=-1_000, payee="Old Grocer")]
    importers.import_records(conn, newer, provider="quicken", source_format="qif")
    aid = _acct_id(conn, "Checking")
    assert ledger.account_cutover_date(conn, aid) == "1998-03-03"
    res = importers.import_records(conn, older, provider="quicken", source_format="qif")
    assert res.added == 0 and res.duplicates == 1      # skipped as pre-cutover
    assert _n_txn(conn, aid) == 1


def test_cash_no_duplication_across_cutover_seam(conn):
    # --- migrate: full Quicken history for a checking account (fitid=NULL) ---
    migrated = [
        importers.NormalizedTxn(external_account="Checking", account_type="checking",
                                date="2024-06-05", amount_cents=200_000, payee="Paycheck"),
        importers.NormalizedTxn(external_account="Checking", account_type="checking",
                                date="2024-06-15", amount_cents=-8_000, payee="Groceries"),
    ]
    importers.import_records(conn, migrated, provider="quicken", source_format="qif")
    aid = _acct_id(conn, "Checking")

    # the migration recorded the newest migrated date as the cutover watermark
    assert ledger.account_cutover_date(conn, aid) == "2024-06-15"
    assert _n_txn(conn, aid) == 2

    # --- first LIVE pull: overlaps the migrated window, plus genuinely new rows.
    # The paycheck is the SAME transaction the bank posts 5 days later (outside
    # the +/-3d fuzzy window) with a different payee string -- exactly the row
    # fitid+fuzzy dedup cannot catch. It is <= cutover, so it must be dropped.
    live = [
        importers.NormalizedTxn(external_account="Checking", account_type="checking",
                                date="2024-06-10", amount_cents=200_000,
                                payee="PAYROLL ACH DEP", fitid="L1"),   # overlap -> skip
        importers.NormalizedTxn(external_account="Checking", account_type="checking",
                                date="2024-06-18", amount_cents=-4_000,
                                payee="Gas", fitid="L2"),               # new -> add
        importers.NormalizedTxn(external_account="Checking", account_type="checking",
                                date="2024-06-22", amount_cents=-3_000,
                                payee="Dining", fitid="L3"),            # new -> add
    ]
    res = importers.import_records(conn, live, provider="bank", source_format="ofx")

    assert res.added == 2
    assert res.duplicates == 1
    assert _n_txn(conn, aid) == 4                      # 2 migrated + 2 new, seam not doubled
    # the paycheck exists exactly once (the migrated row), not duplicated
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=? AND amount=?",
        (aid, 200_000),
    ).fetchone()["c"] == 1

    # a live pull is NOT a migration: it must not move the watermark
    assert ledger.account_cutover_date(conn, aid) == "2024-06-15"

    # re-running the same live pull stays idempotent across the seam
    res2 = importers.import_records(conn, list(live), provider="bank", source_format="ofx")
    assert res2.added == 0
    assert _n_txn(conn, aid) == 4


def test_investment_no_duplication_across_cutover_seam(conn):
    # --- migrate: full Quicken history for a brokerage account (fitid=NULL) ---
    migrated = [
        importers.NormalizedTxn(external_account="Brokerage", account_type="investment",
                                date="2024-03-01", action="Buy", symbol="VTSAX",
                                quantity="10", price="100", amount_cents=-100_000),
        importers.NormalizedTxn(external_account="Brokerage", account_type="investment",
                                date="2024-03-05", action="Div", symbol="VTSAX",
                                amount_cents=5_000),
        importers.NormalizedTxn(external_account="Brokerage", account_type="investment",
                                date="2024-03-10", action="Buy", symbol="VTSAX",
                                quantity="5", price="110", amount_cents=-55_000),
    ]
    importers.import_records(conn, migrated, provider="quicken", source_format="qif")
    aid = _acct_id(conn, "Brokerage")

    assert ledger.account_cutover_date(conn, aid) == "2024-03-10"
    assert _n_invtxn(conn, aid) == 3

    # first LIVE pull. The 03-10 buy is the SAME lot the broker reports with a
    # differently formatted quantity ("5.0000"), so the exact-tuple investment
    # dedup misses it and fitid cannot help (migrated row has none). It is
    # <= cutover, so it must be dropped; the 03-15 buy is genuinely new.
    live = [
        importers.NormalizedTxn(external_account="Brokerage", account_type="investment",
                                date="2024-03-10", action="Buy", symbol="VTSAX",
                                quantity="5.0000", price="110", amount_cents=-55_000,
                                fitid="I1"),                              # overlap -> skip
        importers.NormalizedTxn(external_account="Brokerage", account_type="investment",
                                date="2024-03-15", action="Buy", symbol="VTSAX",
                                quantity="2", price="120", amount_cents=-24_000,
                                fitid="I2"),                              # new -> add
    ]
    res = importers.import_records(conn, live, provider="broker", source_format="ofx")

    assert res.added == 1
    assert res.investments == 1
    assert res.duplicates == 1
    assert _n_invtxn(conn, aid) == 4                   # 3 migrated + 1 new, seam not doubled
    # the 03-10 buy exists exactly once, not duplicated
    assert conn.execute(
        "SELECT COUNT(*) c FROM investment_transactions "
        "WHERE account_id=? AND date=? AND amount=?",
        (aid, "2024-03-10", -55_000),
    ).fetchone()["c"] == 1

    assert ledger.account_cutover_date(conn, aid) == "2024-03-10"
