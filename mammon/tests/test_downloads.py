"""Tests for mammon.downloads: the review-first download entry point (with a
fake webSlinger client, no browser), OFX ledger-balance extraction, the
Downloads-folder scan, the download log, and the per-institution fixtures
through the real importer. There is no institution registry: each account
names its own recorded script."""
from __future__ import annotations

import pathlib

import pytest

from mammon import downloads, importers, ledger
from mammon.downloads import (
    EXPORT,
    SCRAPE,
    DownloadFailedError,
    NoScriptError,
)
from mammon.webslinger import FakeWebSlingerClient, RunResult
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


# An OFX 1.x (SGML, unclosed leaf tags) statement with a LEDGERBAL, like a real
# Anytown CU / bank download. Two cash transactions and a reported balance.
OFX_FIXTURE = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><ACCTID>1234567<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260701<DTEND>20260710
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260705
<TRNAMT>-50.00
<FITID>AF-1001
<NAME>Corner Market
<MEMO>groceries
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260710
<TRNAMT>200.00
<FITID>AF-1002
<NAME>Payroll ACH
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1150.00<DTASOF>20260710</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


# ---------------------------------------------------------------------------
# OFX reported-balance extraction + reconciliation math
# ---------------------------------------------------------------------------
def test_ofx_reported_balance():
    cents, as_of = downloads.ofx_reported_balance(OFX_FIXTURE)
    assert cents == 1150_00
    assert as_of == "2026-07-10"


def test_ofx_reported_balance_absent():
    assert downloads.ofx_reported_balance("<OFX></OFX>") is None


# Wells Fargo credit-card CSV export (the real download shape): no LEDGERBAL,
# so the importer self-computes a balance.
WF_CSV = (
    '"DATE","DESCRIPTION","AMOUNT","CHECK #","STATUS"\n'
    '"08/06/2026","ANON MARKET M ANYTOWN ST","-18.90",,"Posted"\n'
    '"07/31/2026","ANON MOBILE ANON.COM GA","-9.36",,"Posted"\n'
    '"07/31/2026","ANON MOBILE ANON.COM GA","-9.36",,"Posted"\n'
    '"07/22/2026","AUTOMATIC PAYMENT - THANK YOU","1850.94",,"Posted"\n'
)


def test_wells_fargo_csv_export_import_credit(conn, tmp_path):
    path = tmp_path / "CreditCard.csv"
    path.write_text(WF_CSV, encoding="utf-8")
    imp = importers.import_file(conn, str(path), provider="WF Credit Card",
                                account="WF Credit Card", account_type="credit")
    assert imp.added == 4
    acct = ledger.get_account_by_name(conn, "WF Credit Card")
    assert acct["type"] == "credit"                      # account_type honored on create
    assert ledger.account_balance(conn, acct["id"]) == \
        -18_90 - 9_36 - 9_36 + 1850_94

    # A re-pull of the overlapping window dedups exactly (stable synthetic ids).
    imp2 = importers.import_file(conn, str(path), provider="WF Credit Card",
                                 account="WF Credit Card", account_type="credit")
    assert imp2.added == 0
    assert imp2.duplicates == 4


# A minimal describe_script payload; download_rows_for_review only needs the
# call to succeed (the branch decision uses the ACTUAL run result, not the
# schema).
_SCRIPT_SCHEMA = {"script_name": "GetTxns", "display_name": "GetTxns",
                  "inputs": [{"name": "userName"}], "outputs": []}


# ---------------------------------------------------------------------------
# download_rows_for_review: NOTHING is written straight to the register. The
# SCRAPE branch hands rows back; the EXPORT branch now hands a SINGLE-account
# file back for review too, importing directly ONLY a multi-account QIF.
# ---------------------------------------------------------------------------
def _drop(ddir, name, text, mtime=9000):
    import os
    p = ddir / name
    p.write_text(text, encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def test_download_rows_for_review_export_single_account_file_routes_to_review(
        conn, tmp_path):
    """EXPORT branch, single-account file: the run returned no records and dropped
    a bank OFX. It must be flagged for review (needs_file_review) with NOTHING
    imported -- the webSlinger EXPORT bypass is closed."""
    ledger.create_account(conn, "Anytown CU Checking", "checking")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    _drop(ddir, "download.ofx", OFX_FIXTURE)
    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(file_path=None, records=None)})
    rd = downloads.download_rows_for_review(
        conn, client, "GetTxns", {"userName": "x"},
        account="Anytown CU Checking", account_type="checking",
        downloads_dir=str(ddir), now=5000)
    assert rd.mode == EXPORT
    assert rd.needs_file_review is True and rd.needs_review is False
    assert rd.file_path.endswith("download.ofx")
    assert rd.import_result is None                   # nothing imported here
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_download_rows_for_review_export_investment_file_routes_to_review(
        conn, tmp_path):
    """EXPORT branch with an INVESTMENT export (buy/sell/div): still routed to
    review, never straight to the register."""
    inv_ofx = OFX_FIXTURE.replace(
        "<BANKMSGSRSV1><STMTTRNRS><STMTRS>",
        "<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>")  # crude marker; parse still ok
    ledger.create_account(conn, "Broker", "investment")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    _drop(ddir, "broker.csv",
          "Trade Date,Symbol,Shares,Price\n02/01/2026,AAPL,10,150.00\n")
    client = FakeWebSlingerClient(
        schemas={"GetInv": _SCRIPT_SCHEMA},
        results={"GetInv": RunResult(file_path=None, records=None)})
    rd = downloads.download_rows_for_review(
        conn, client, "GetInv", {"userName": "x"},
        account="Broker", account_type="investment",
        downloads_dir=str(ddir), now=5000)
    assert rd.needs_file_review is True
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == 0


def test_multi_account_file_decision_reserves_direct_for_multi_account_qif(tmp_path):
    """The routing rule the EXPORT branch consults: only a MULTI-account file (or
    a securities-master QIF) imports directly; every single-account file -- cash
    OR investment, any format -- routes through review. (A bank EXPORT only ever
    drops .ofx/.qfx/.csv, all single-account, so in practice the EXPORT branch
    always routes to review; this locks in the underlying decision.)"""
    multi = _drop(
        tmp_path, "both.qif",
        "!Account\nNChk\nTBank\n^\n!Type:Bank\nD01/05'26\nT-40.00\nL[Sav]\n^\n"
        "!Account\nNSav\nTBank\n^\n!Type:Bank\nD01/05'26\nT40.00\nL[Chk]\n^\n")
    assert importers.multi_account_file(str(multi)) is True

    single_cash = _drop(tmp_path, "chk.csv",
                        "Date,Payee,Amount\n01/05/2026,Store,-9.99\n")
    assert importers.multi_account_file(str(single_cash)) is False

    single_ofx = _drop(tmp_path, "stmt.ofx", OFX_FIXTURE)
    assert importers.multi_account_file(str(single_ofx)) is False

    single_inv = _drop(tmp_path, "inv.csv",
                       "Trade Date,Symbol,Shares,Price\n02/01/2026,AAPL,10,150.00\n")
    assert importers.multi_account_file(str(single_inv)) is False


# ---------------------------------------------------------------------------
# new_downloads / multi-file EXPORT runs: a webSlinger EXPORT that drops N files
# must import/queue ALL N (not just the newest), ignore stale pre-run files, and
# now discover a dropped .qif too.
# ---------------------------------------------------------------------------
_MULTI_QIF = (
    "!Account\nNChk\nTBank\n^\n!Type:Bank\nD01/05'26\nT-40.00\nL[Sav]\n^\n"
    "!Account\nNSav\nTBank\n^\n!Type:Bank\nD01/05'26\nT40.00\nL[Chk]\n^\n")


def test_new_downloads_lists_every_new_file_and_finds_qif(tmp_path):
    """new_downloads returns EVERY post-``since`` file (across .ofx/.qfx/.csv AND
    .qif), oldest->newest, skipping a stale pre-run file."""
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    stale = _drop(ddir, "old.csv", "Date,Payee,Amount\n", mtime=1000)
    a = _drop(ddir, "chk.csv",
              "Date,Payee,Amount\n01/05/2026,Store,-9.99\n", mtime=6000)
    b = _drop(ddir, "broker.qfx", OFX_FIXTURE, mtime=7000)
    c = _drop(ddir, "both.qif", _MULTI_QIF, mtime=8000)
    got = downloads.new_downloads(str(ddir), since=5000)
    assert got == [str(a), str(b), str(c)]            # sorted oldest -> newest
    assert str(stale) not in got                      # stale pre-run file skipped
    assert any(p.endswith(".qif") for p in got)       # .qif is now discovered


def test_download_rows_for_review_processes_every_new_file(conn, tmp_path):
    """download_rows_for_review: a run that drops several files routes EACH single-
    account file to review (file_reviews) and imports the MULTI-account QIF
    directly (import_results); a stale pre-run file is ignored."""
    import os
    ledger.create_account(conn, "AF Checking", "checking")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    _drop(ddir, "stale.csv", "Date,Payee,Amount\n01/01/2026,Old,-1.00\n", mtime=1000)
    _drop(ddir, "chk.csv", "Date,Payee,Amount\n01/05/2026,Store,-9.99\n", mtime=6000)
    _drop(ddir, "stmt.ofx", OFX_FIXTURE, mtime=7000)
    multi = _drop(ddir, "both.qif", _MULTI_QIF, mtime=8000)
    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(file_path=None, records=None)})
    rd = downloads.download_rows_for_review(
        conn, client, "GetTxns", {"userName": "x"},
        account="AF Checking", account_type="checking",
        downloads_dir=str(ddir), now=5000)
    assert rd.mode == EXPORT
    # Both single-account files (cash CSV + bank OFX) are queued for review.
    assert rd.needs_file_review is True
    assert sorted(os.path.basename(p) for p in rd.file_reviews) == \
        ["chk.csv", "stmt.ofx"]
    # The multi-account QIF was imported DIRECTLY, not routed to review.
    assert len(rd.import_results) == 1
    assert str(multi) not in rd.file_reviews
    # back-compat singular views point at the FIRST of each list.
    assert rd.file_path in rd.file_reviews
    assert rd.import_result is rd.import_results[0]
    # The stale pre-run file was ignored entirely.
    assert all("stale" not in os.path.basename(p) for p in rd.file_reviews)


def test_download_rows_for_review_requires_a_script(conn):
    """No stored script -> NoScriptError before the client is ever touched."""
    client = FakeWebSlingerClient(schemas={}, results={})
    with pytest.raises(NoScriptError):
        downloads.download_rows_for_review(conn, client, "", {"userName": "x"},
                                           account="Any")
    assert client.calls == []                        # never reached the MCP


def test_download_rows_for_review_no_new_file_raises(conn, tmp_path):
    """Review path: no NEW file (only a stale pre-run download) -> DownloadFailedError."""
    ledger.create_account(conn, "AF Checking", "checking")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    _drop(ddir, "old.ofx", OFX_FIXTURE, mtime=1000)            # stale only
    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(file_path=None, records=None)})
    with pytest.raises(DownloadFailedError):
        downloads.download_rows_for_review(
            conn, client, "GetTxns", {"userName": "x"},
            account="AF Checking", downloads_dir=str(ddir), now=5000)


# ---------------------------------------------------------------------------
# Anonymized per-institution fixtures (tests/fixtures/): each imports cleanly
# through the real importer, and a first live pull does NOT duplicate history
# across the one-time Quicken-migration seam (accounts.cutover_date watermark).
# All cash fixtures share a date scheme -- rows on 2026-07-05 and 2026-07-08
# (<= cutover 2026-07-10) plus 2026-07-15 (> cutover) -- so one seam spec fits.
# ---------------------------------------------------------------------------
FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# (fixture filename, account name, account type)
CASH_FIXTURES = [
    ("anytown_cu_checking.ofx", "Anytown CU Checking", "checking"),
    ("anytown_cu_savings.ofx", "Anytown CU Savings", "savings"),
    ("chase.qfx", "Chase Amazon Visa", "credit"),
    ("citibank.qfx", "Citi Costco", "credit"),
    ("bank_of_america.qfx", "BoA Checking", "checking"),
    ("wells_fargo.csv", "WF Credit Card", "credit"),
    ("discover.csv", "Discover Card", "credit"),
]


def _import_fixture(conn, filename, acct, atype):
    """Import one fixture the way a reviewed download file lands: the real
    importer, the account name as the provider label."""
    return importers.import_file(conn, str(FIXTURES / filename), provider=acct,
                                 account=acct, account_type=atype)


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


@pytest.mark.parametrize("filename,acct,atype", CASH_FIXTURES)
def test_institution_fixture_imports_cleanly(conn, filename, acct, atype):
    # The anonymized sample parses through the real importer with no errors and
    # its three rows land as new transactions.
    imp = _import_fixture(conn, filename, acct, atype)
    assert imp.errors == 0
    assert imp.added == 3
    # exact re-import of the same file dedups completely (fitid, or the csv
    # importer's synthesized stable id).
    imp2 = _import_fixture(conn, filename, acct, atype)
    assert imp2.added == 0
    assert imp2.duplicates == 3


@pytest.mark.parametrize("filename,acct,atype", CASH_FIXTURES)
def test_institution_fixture_no_duplication_across_cutover_seam(conn, filename, acct, atype):
    # --- migrate: fitid-less Quicken history for this account. It carries the
    # SAME -50.00 charge the bank re-reports (fixture row A) but on an earlier
    # date with a different payee -- exactly what fitid + fuzzy dedup cannot
    # catch -- plus a sentinel row on 2026-07-10 that fixes the cutover watermark.
    migrated = [
        importers.NormalizedTxn(external_account=acct, account_type=atype,
                                date="2026-07-01", amount_cents=-50_00,
                                payee="MIGRATED SAME CHARGE"),
        importers.NormalizedTxn(external_account=acct, account_type=atype,
                                date="2026-07-10", amount_cents=-321_00,
                                payee="Migrated Sentinel"),
    ]
    importers.import_records(conn, migrated, provider="quicken", source_format="qif")
    aid = _acct_id(conn, acct)
    assert ledger.account_cutover_date(conn, aid) == "2026-07-10"
    assert _n_txn(conn, aid) == 2

    # --- first LIVE pull of the fixture: rows on 07-05 and 07-08 are <= cutover
    # (already covered by migrated history) and must be dropped; only the 07-15
    # row is genuinely new. The bank's fitids cannot dedup the fitid-less
    # migrated rows -- the cutover watermark is what prevents the double-import.
    imp = _import_fixture(conn, filename, acct, atype)
    assert imp.duplicates == 2
    assert imp.added == 1
    assert _n_txn(conn, aid) == 3                        # 2 migrated + 1 new, seam not doubled
    # the -50.00 charge exists exactly once (the migrated row), not duplicated
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=? AND amount=?",
        (aid, -50_00),
    ).fetchone()["c"] == 1
    # a live pull is NOT a migration: it must not move the watermark
    assert ledger.account_cutover_date(conn, aid) == "2026-07-10"
    # re-pulling the same window stays idempotent across the seam
    imp2 = _import_fixture(conn, filename, acct, atype)
    assert imp2.added == 0
    assert _n_txn(conn, aid) == 3


def test_fidelity_fixture_imports_cleanly(conn):
    imp = _import_fixture(conn, "fidelity.csv", "Fidelity Brokerage", "investment")
    assert imp.errors == 0
    assert imp.added == 3
    assert imp.investments == 3


def test_fidelity_fixture_no_duplication_across_cutover_seam(conn):
    # migrate fitid-less brokerage history: the SAME 07-05 lot the broker reports
    # (fixture row A) shows up here on 07-01 (different date), which exact-tuple
    # investment dedup misses; a sentinel buy on 07-10 fixes the cutover watermark.
    migrated = [
        importers.NormalizedTxn(external_account="Fidelity Brokerage",
                                account_type="investment", date="2026-07-01",
                                action="Buy", symbol="VTI", quantity="10",
                                price="100", amount_cents=-1000_00),
        importers.NormalizedTxn(external_account="Fidelity Brokerage",
                                account_type="investment", date="2026-07-10",
                                action="Buy", symbol="VTI", quantity="1",
                                price="321", amount_cents=-321_00),
    ]
    importers.import_records(conn, migrated, provider="quicken", source_format="qif")
    aid = _acct_id(conn, "Fidelity Brokerage")
    assert ledger.account_cutover_date(conn, aid) == "2026-07-10"
    assert _n_invtxn(conn, aid) == 2

    # first LIVE pull: 07-05 and 07-08 buys are <= cutover -> dropped; the 07-15
    # buy is genuinely new. Seam is not doubled.
    imp = _import_fixture(conn, "fidelity.csv", "Fidelity Brokerage", "investment")
    assert imp.duplicates == 2
    assert imp.investments == 1
    assert imp.added == 1
    assert _n_invtxn(conn, aid) == 3                     # 2 migrated + 1 new
    # the -1000.00 lot exists exactly once (the migrated row), not duplicated
    assert conn.execute(
        "SELECT COUNT(*) c FROM investment_transactions WHERE account_id=? AND amount=?",
        (aid, -1000_00),
    ).fetchone()["c"] == 1
    assert ledger.account_cutover_date(conn, aid) == "2026-07-10"


# ---------------------------------------------------------------------------
# download log: every attempt is recorded with BOTH the raw MCP run summary/
# error AND Mammon's final success/failure decision, so a run that "returned good
# data but reported an error" is diagnosable after its transient dialog is gone.
# The log path is redirected via MAMMON_DOWNLOAD_LOG so nothing touches the live
# <install root>/data/download.log during tests. Logging never touches success LOGIC
# (task 51582452 owns that); these tests only assert the OUTCOME is recorded.
# ---------------------------------------------------------------------------
def _redirect_log(monkeypatch, tmp_path):
    from mammon import download_log
    path = str(tmp_path / "download.log")
    monkeypatch.setenv("MAMMON_DOWNLOAD_LOG", path)
    return download_log, path


def test_download_log_records_success_with_mcp_errors(conn, tmp_path, monkeypatch):
    """SUCCESS-WITH-ERRORS: the run returns usable records AND reports 3 failed
    MCP actions. Mammon takes the data for review (decision=success), and the log
    entry captures BOTH the failed-action count/raw error AND the success
    decision, plus the actual inputData sent."""
    download_log, _ = _redirect_log(monkeypatch, tmp_path)
    ledger.create_account(conn, "WF Checking", "checking")
    rows = [
        {"date": "2026-07-05", "description": "Coffee Bar", "amount": "-4.50",
         "type": "debit", "fitid": "WF-1"},
        {"date": "2026-07-06", "description": "Refund", "amount": "12.00",
         "type": "credit", "fitid": "WF-2"},
    ]
    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(records=rows, status="completed",
                                      failed_actions=3, error="3 actions failed")},
    )
    rd = downloads.download_rows_for_review(
        conn, client, "GetTxns", {"userName": "1234567"},
        account="WF Checking", account_type="checking",
        account_number="1234567")
    assert rd.mode == SCRAPE and len(rd.rows) == 2       # handed back for review

    entries = download_log.read_recent(log_path=None)   # env override in effect
    assert len(entries) == 1
    e = entries[0]
    assert e["decision"] == "success"
    assert e["account"] == "WF Checking"
    assert e["account_number"] == "1234567"
    assert e["script"] == "GetTxns"
    assert e["input_data"] == {"userName": "1234567"}        # actual inputData sent
    assert e["mcp"]["failed_actions"] == 3                   # raw MCP error signal
    assert e["mcp"]["raw_error"] == "3 actions failed"       # raw MCP error text
    assert e["mcp"]["status"] == "completed"
    assert e["source"] == "records"
    assert e["imported"] is None                             # review imports later
    assert "despite 3 failed MCP action" in e["reason"]      # WHY it's still success
    assert download_log.last_error() is None                 # a success, not an error


def test_download_log_records_failure(conn, tmp_path, monkeypatch):
    """FAILURE: the run returns no data and no new Downloads file appears, so
    download_rows_for_review raises DownloadFailedError. The log must STILL capture a
    failure entry with the raw error text, retrievable via last_error()."""
    import os
    download_log, _ = _redirect_log(monkeypatch, tmp_path)
    ledger.create_account(conn, "Anytown CU Checking", "checking")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    stale = ddir / "old.ofx"                                 # older than run start
    stale.write_text("<OFX></OFX>", encoding="utf-8")
    os.utime(stale, (1000, 1000))

    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(file_path=None, records=None,
                                      status="failed")},
    )
    with pytest.raises(DownloadFailedError):
        downloads.download_rows_for_review(
            conn, client, "GetTxns", {"userName": "1234567"},
            account="Anytown CU Checking", account_number="1234567",
            downloads_dir=str(ddir), now=5000)

    entries = download_log.read_recent(log_path=None)
    assert len(entries) == 1
    e = entries[0]
    assert e["decision"] == "failure"
    assert e["account"] == "Anytown CU Checking"
    assert e["account_number"] == "1234567"
    assert e["error_text"] and "returned no data" in e["error_text"]
    assert e["mcp"]["status"] == "failed"                    # raw MCP summary
    assert e["imported"] is None                             # nothing imported
    last = download_log.last_error()                         # retrievable
    assert last is not None and last["decision"] == "failure"


class _McpShapeClient:
    """A client whose run_script parses a canned MCP TERMINAL PAYLOAD through
    RunResult.from_mcp -- exercising the REAL output_data binding, unlike
    FakeWebSlingerClient which hands back a pre-built RunResult. Mirrors an
    API-style script that returns transaction rows in output_data and NEVER
    drops a file."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def available(self):
        return True

    def describe_script(self, name):
        return None  # download_rows_for_review ignores the return value

    def run_script(self, name, inputs, source="user"):
        self.calls.append((name, dict(inputs)))
        result = RunResult.from_mcp(self._payload)
        result.sent_input = dict(inputs)   # what McpWebSlingerClient records
        return result


def test_download_binds_output_data_rows_and_logs_the_run_timing(
        conn, tmp_path, monkeypatch):
    """An API script returns rows in output_data (under an array name, NOT the
    literal 'records' key) and drops NO file. Mammon must bind to output_data,
    hand the rows to review (no Downloads scan), and log the real sent inputData
    + the run's own UTC start/duration with no keyCocoon note on success."""
    download_log, _ = _redirect_log(monkeypatch, tmp_path)
    ledger.create_account(conn, "Anytown CU Savings", "savings")
    empty_dl = tmp_path / "Downloads"           # empty: a scan here would fail
    empty_dl.mkdir()
    payload = {
        "success": True,
        "status": "completed",
        "failed_actions": 1,                    # incidental error, must not block
        "started_at": "2026-08-22T13:37:06Z",   # the run's OWN UTC start
        "duration_seconds": 209.5,              # the run's OWN duration
        "output_data": {
            "shareSavings": [                   # array name, not "records"
                {"date": "2026-01-15", "description": "Deposit",
                 "amount": "100.00", "type": "credit", "fitid": "AF-1"},
                {"date": "2026-02-15", "description": "Dividend",
                 "amount": "0.42", "type": "credit", "fitid": "AF-2"},
            ],
        },
    }
    client = _McpShapeClient(payload)
    sent = {"userName": "1234567", "subAccountName": ["Share Savings"]}

    rd = downloads.download_rows_for_review(
        conn, client, "GetAnytownCUTransactions", sent,
        account="Anytown CU Savings", account_type="savings",
        account_number="1234567", downloads_dir=str(empty_dl))

    # Bound from output_data -- no Downloads scan (the empty dir did not raise),
    # 2 rows handed back and nothing written.
    assert rd.mode == SCRAPE
    assert [r["fitid"] for r in rd.rows] == ["AF-1", "AF-2"]
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    entries = download_log.read_recent(log_path=None)
    e = entries[0]
    assert e["decision"] == "success" and e["source"] == "records"
    # The ACTUAL post-coercion inputData sent -- a real array, not a string.
    assert e["input_data"]["subAccountName"] == ["Share Savings"]
    # The RUN's own reported timing (UTC), not Mammon's local wall clock.
    assert e["ts"] == "2026-08-22T13:37:06Z"
    assert e["mcp"]["run_started_utc"] == "2026-08-22T13:37:06Z"
    assert e["mcp"]["run_duration_seconds"] == 209.5
    assert "mammon_wait_seconds" in e["mcp"]          # Mammon's wait, labeled apart
    # No keyCocoon re-auth red herring on a successful run.
    assert "keyCocoon" not in (e.get("reason") or "")


def test_download_failure_names_keycocoon_only_on_an_auth_failure(
        conn, tmp_path, monkeypatch):
    """A run that dropped nothing raises DownloadFailedError. When the run itself
    reported an AUTHENTICATION failure the message points at keyCocoon (webSlinger
    signs in with the browser's credentials); when it did not, that advice would
    be a red herring and is left out."""
    _redirect_log(monkeypatch, tmp_path)
    ddir = tmp_path / "Downloads"
    ddir.mkdir()

    def failure(status):
        client = FakeWebSlingerClient(
            schemas={"GetTxns": _SCRIPT_SCHEMA},
            results={"GetTxns": RunResult(file_path=None, records=None,
                                          status=status)})
        with pytest.raises(DownloadFailedError) as exc:
            downloads.download_rows_for_review(
                conn, client, "GetTxns", {"userName": "x"},
                account="AF Checking", downloads_dir=str(ddir), now=5000)
        return str(exc.value)

    assert "keyCocoon" in failure("login failed")
    assert "keyCocoon" not in failure("completed")
