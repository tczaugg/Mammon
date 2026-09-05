"""Tests for mammon.downloads: the download -> importer -> ledger -> reconcile
flow (with an injected fake runner, no browser), OFX ledger-balance
reconciliation, and the account-driven download entry points. There is no
institution registry: each account names its own recorded script."""
from __future__ import annotations

import pathlib

import pytest

from mammon import db, downloads, importers, ledger
from mammon.downloads import (
    EXPORT,
    SCRAPE,
    DownloadFailedError,
    DownloadResult,
    NoScriptError,
    RunOutput,
)
from mammon.webslinger import FakeWebSlingerClient, RunResult


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
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
<NAME>Smiths Marketplace
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


# ---------------------------------------------------------------------------
# EXPORT flow: fake runner drops an OFX file -> import -> reconcile
# ---------------------------------------------------------------------------
def test_export_download_import_and_reconcile(conn, tmp_path):
    # An established account holding its history up to the statement window:
    # opening 1000.00 as of 2026-06-30, so after the +150 net it should read
    # 1150.00 as of 2026-07-10 -- exactly the OFX LEDGERBAL.
    acct = ledger.create_account(conn, "Anytown CU Checking", "checking",
                                 opening_balance=1000_00, opening_date="2026-06-30")

    ofx = tmp_path / "download.ofx"
    ofx.write_text(OFX_FIXTURE, encoding="utf-8")

    calls = {}

    def fake_runner(script, inputs):
        calls["script"] = script
        calls["inputs"] = inputs
        return RunOutput(file_path=str(ofx))

    dr = downloads.run_and_import(
        conn, fake_runner, "GetCheckingTransactionsForRange",
        {"userName": "1234567", "startDate": "7/1/2026",
         "endDate": "7/10/2026", "format": "OFX File (.OFX)"},
        account="Anytown CU Checking",
    )

    assert calls["script"] == "GetCheckingTransactionsForRange"
    assert calls["inputs"]["userName"] == "1234567"
    assert dr.import_result.added == 2
    assert dr.account_id == acct
    assert dr.statement_balance == 1150_00
    assert dr.ledger_balance == 1150_00
    assert dr.reconciled is True
    assert "reconcile: OK" in dr.summary()


def test_reconcile_mismatch_is_reported(conn, tmp_path):
    # Wrong opening balance -> ledger will not match the statement.
    ledger.create_account(conn, "Anytown CU Checking", "checking",
                          opening_balance=500_00, opening_date="2026-06-30")
    ofx = tmp_path / "download.ofx"
    ofx.write_text(OFX_FIXTURE, encoding="utf-8")

    dr = downloads.import_download_file(
        conn, str(ofx), account="Anytown CU Checking")
    assert dr.statement_balance == 1150_00
    assert dr.ledger_balance == 500_00 - 50_00 + 200_00      # 650.00
    assert dr.reconciled is False
    assert "MISMATCH" in dr.summary()


def test_run_and_import_passes_the_script_name_verbatim(conn, tmp_path):
    # The per-account Download slot passes the account's own script name; it
    # reaches the runner untouched -- there is no registry to look it up in.
    ledger.create_account(conn, "Anytown CU Checking", "checking",
                          opening_balance=1000_00, opening_date="2026-06-30")
    ofx = tmp_path / "d.ofx"
    ofx.write_text(OFX_FIXTURE, encoding="utf-8")
    seen = {}

    def fake_runner(script, inputs):
        seen["script"] = script
        return RunOutput(file_path=str(ofx))

    dr = downloads.run_and_import(
        conn, fake_runner, "SomeCustomScript", {"userName": "1234567"},
        account="Anytown CU Checking")
    assert seen["script"] == "SomeCustomScript"
    assert dr.import_result.added == 2
    assert dr.reconciled is True
    assert dr.ledger_balance == 1150_00


def test_run_and_import_reconciles_from_the_file_itself(conn, tmp_path):
    # Reconciliation needs no institution table: the OFX carries its own
    # LEDGERBAL, and any account can be checked against it.
    ledger.create_account(conn, "Some Bank", "checking",
                          opening_balance=1000_00, opening_date="2026-06-30")
    ofx = tmp_path / "d.ofx"
    ofx.write_text(OFX_FIXTURE, encoding="utf-8")

    def runner(script, inputs):
        return RunOutput(file_path=str(ofx))

    dr = downloads.run_and_import(conn, runner, "someScript", {}, account="Some Bank")
    assert dr.import_result.added == 2
    assert dr.reconciled is True
    assert dr.ledger_balance == 1150_00
    assert dr.institution == "Some Bank"   # provider label defaults to the account


def test_empty_run_raises_rather_than_importing_nothing(conn):
    """A webSlinger run that returns neither a file nor records (e.g. the bank
    MFA login timed out) must surface as a failure, not a silent '0 added'."""
    def dead_runner(script, inputs):
        return RunOutput(file_path=None, records=None)   # nothing came back

    with pytest.raises(DownloadFailedError):
        downloads.run_and_import(
            conn, dead_runner, "GetCheckingTransactionsForRange",
            {"userName": "1234567"}, account="AF Checking")


def test_empty_scrape_list_is_allowed(conn):
    """An explicit empty scrape (records == []) is a legitimate 'no new rows',
    distinct from the degenerate None/None run -- it imports zero, no error."""
    def empty_scrape(script, inputs):
        return RunOutput(records=[])

    dr = downloads.run_and_import(conn, empty_scrape, "someScrape", {},
                                  account="Any")
    assert dr.import_result.added == 0


# Wells Fargo credit-card CSV export (the real download shape): no LEDGERBAL, so
# it imports and self-computes a balance but does not reconcile to a statement.
WF_CSV = (
    '"DATE","DESCRIPTION","AMOUNT","CHECK #","STATUS"\n'
    '"08/06/2026","ANON MARKET M ANYTOWN UT","-18.90",,"Posted"\n'
    '"07/31/2026","ANON MOBILE ANON.COM GA","-9.36",,"Posted"\n'
    '"07/31/2026","ANON MOBILE ANON.COM GA","-9.36",,"Posted"\n'
    '"07/22/2026","AUTOMATIC PAYMENT - THANK YOU","1850.94",,"Posted"\n'
)


def test_wells_fargo_csv_export_import_credit(conn, tmp_path):
    path = tmp_path / "CreditCard.csv"
    path.write_text(WF_CSV, encoding="utf-8")
    dr = downloads.import_download_file(conn, str(path), account="WF Credit Card",
                                        account_type="credit")
    assert dr.import_result.added == 4
    acct = ledger.get_account_by_name(conn, "WF Credit Card")
    assert acct["type"] == "credit"                      # account_type honored on create
    assert dr.ledger_balance == -18_90 - 9_36 - 9_36 + 1850_94
    assert dr.reconciled is None                         # CSV has no statement balance

    # A re-pull of the overlapping window dedups exactly (stable synthetic ids).
    dr2 = downloads.import_download_file(conn, str(path), account="WF Credit Card",
                                         account_type="credit")
    assert dr2.import_result.added == 0
    assert dr2.import_result.duplicates == 4


# ---------------------------------------------------------------------------
# SCRAPE flow: webSlinger dict rows -> JSON importer -> ledger
# ---------------------------------------------------------------------------
def test_scrape_records_import(conn):
    rows = [
        {"date": "2026-07-05", "description": "Coffee Bar", "amount": "-4.50",
         "type": "debit", "fitid": "WF-1"},
        {"date": "2026-07-06", "description": "Refund", "amount": "12.00",
         "type": "credit", "fitid": "WF-2"},
    ]
    dr = downloads.run_and_import(
        conn, lambda script, inputs: RunOutput(records=rows), "scrapeWF", {},
        account="WF Checking")
    assert dr.import_result.added == 2
    assert dr.account_id is not None
    # -4.50 + 12.00 = 7.50 into a fresh (opening 0) account
    assert dr.ledger_balance == 7_50
    assert dr.reconciled is None       # scrape carries no statement balance


def test_runner_returning_records_takes_the_scrape_branch(conn):
    # A runner may hand back scraped rows instead of a file (out.is_file False);
    # run_and_import then routes them through the JSON importer.
    rows = [{"date": "2026-07-05", "description": "Store", "amount": "-9.99",
             "type": "debit", "fitid": "S-1"}]

    def fake_runner(script, inputs):
        return RunOutput(records=rows)

    dr = downloads.run_and_import(conn, fake_runner, "GetCheckingTransactionsForRange",
                                  {}, account="AF Checking")
    assert dr.import_result.added == 1
    assert dr.ledger_balance == -9_99
    assert dr.reconciled is None


# ---------------------------------------------------------------------------
# download_account: the credential-free entry point. Mammon runs the account's
# stored script through the webSlinger MCP client (mocked here by
# FakeWebSlingerClient) with its saved inputData, then imports EITHER the
# returned records OR the newest post-run file from ~/Downloads. describe_script
# is queried up front so Mammon knows the outputData shape; Mammon holds no login
# secret at all (credentials live in the user's own browser).
# ---------------------------------------------------------------------------
# A minimal describe_script payload; download_account only needs the call to
# succeed (the branch decision uses the ACTUAL run result, not the schema).
_SCRIPT_SCHEMA = {"script_name": "GetTxns", "display_name": "GetTxns",
                  "inputs": [{"name": "userName"}], "outputs": []}


def test_download_account_imports_returned_data(conn):
    """Returned-data branch: the run hands back records -> import them into THIS
    account via the JSON importer, with reconcile counts."""
    ledger.create_account(conn, "WF Checking", "checking")
    rows = [
        {"date": "2026-07-05", "description": "Coffee Bar", "amount": "-4.50",
         "type": "debit", "fitid": "WF-1"},
        {"date": "2026-07-06", "description": "Refund", "amount": "12.00",
         "type": "credit", "fitid": "WF-2"},
    ]
    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(records=rows)},
    )
    dr = downloads.download_account(
        conn, client, "GetTxns", {"userName": "1234567"},
        account="WF Checking", account_type="checking")

    assert dr.import_result.added == 2
    assert dr.import_result.duplicates == 0
    assert dr.mode == SCRAPE
    assert dr.account_id is not None
    assert dr.ledger_balance == 7_50            # -4.50 + 12.00
    assert dr.reconciled is None                # scrape carries no statement bal
    # describe_script queried up front; run got the saved inputData verbatim.
    assert ("describe", "GetTxns", None) in client.calls
    assert ("run", "GetTxns", {"userName": "1234567"}) in client.calls


def test_download_account_imports_newest_post_run_downloads_file(conn, tmp_path):
    """No-data branch: the run returns nothing (a browser export), so Mammon scans
    ~/Downloads for the NEWEST .ofx/.qfx/.csv modified AFTER the run started. A
    stale pre-run file with the same extension must be IGNORED."""
    import os
    ledger.create_account(conn, "Anytown CU Checking", "checking",
                          opening_balance=1000_00, opening_date="2026-06-30")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    # A stale download from a previous session (mtime BEFORE the run started).
    stale = ddir / "old.ofx"
    stale.write_text("<OFX></OFX>", encoding="utf-8")
    os.utime(stale, (1000, 1000))
    # The file this run actually dropped (mtime AFTER the run started).
    fresh = ddir / "download.ofx"
    fresh.write_text(OFX_FIXTURE, encoding="utf-8")
    os.utime(fresh, (9000, 9000))

    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(file_path=None, records=None)},
    )
    dr = downloads.download_account(
        conn, client, "GetTxns", {"userName": "1234567"},
        account="Anytown CU Checking", account_type="checking",
        downloads_dir=str(ddir), now=5000)          # run "started" at t=5000

    assert dr.file_path == str(fresh)               # newest post-run file, not stale
    assert dr.mode == EXPORT
    assert dr.import_result.added == 2
    assert dr.statement_balance == 1150_00          # OFX LEDGERBAL reconciled
    assert dr.ledger_balance == 1150_00
    assert dr.reconciled is True


def test_download_account_no_new_file_raises(conn, tmp_path):
    """No-data branch with no NEW file: only a stale pre-run download exists, so
    nothing is imported and the failure surfaces clearly."""
    import os
    ledger.create_account(conn, "Anytown CU Checking", "checking")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    stale = ddir / "old.ofx"                         # older than the run start
    stale.write_text(OFX_FIXTURE, encoding="utf-8")
    os.utime(stale, (1000, 1000))

    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(file_path=None, records=None)},
    )
    with pytest.raises(DownloadFailedError):
        downloads.download_account(
            conn, client, "GetTxns", {"userName": "1234567"},
            account="Anytown CU Checking",
            downloads_dir=str(ddir), now=5000)


def test_download_account_requires_a_script(conn):
    """No stored script -> NoScriptError before the client is ever touched."""
    client = FakeWebSlingerClient(schemas={}, results={})
    with pytest.raises(NoScriptError):
        downloads.download_account(conn, client, "", {"userName": "x"},
                                   account="Any")
    assert client.calls == []                        # never reached the MCP


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


def test_download_account_defaults_downloads_dir_to_home(conn, tmp_path, monkeypatch):
    """When no downloads_dir is passed, Mammon resolves the current user's
    ~/Downloads (C:\\Users\\<username>\\Downloads on Windows)."""
    import os
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    fresh = ddir / "d.ofx"
    fresh.write_text(OFX_FIXTURE, encoding="utf-8")
    os.utime(fresh, (9000, 9000))
    ledger.create_account(conn, "AF Checking", "checking")

    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(records=None)},
    )
    dr = downloads.download_account(conn, client, "GetTxns", {"userName": "x"},
                                    account="AF Checking", now=5000)
    assert dr.file_path == str(fresh)


def test_newest_download_picks_latest(tmp_path):
    import os, time
    a = tmp_path / "a.ofx"
    b = tmp_path / "b.ofx"
    a.write_text("x", encoding="utf-8")
    b.write_text("y", encoding="utf-8")
    # make b newer than a
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    assert downloads.newest_download(str(tmp_path)) == str(b)
    # a csv that is newest wins across extensions
    c = tmp_path / "c.csv"
    c.write_text("z", encoding="utf-8")
    os.utime(c, (3000, 3000))
    assert downloads.newest_download(str(tmp_path)) == str(c)
    # since-filter excludes older files
    assert downloads.newest_download(str(tmp_path), since=2500) == str(c)


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


def test_download_account_imports_every_new_file_and_logs_each(
        conn, tmp_path, monkeypatch):
    """download_account (direct path): a run that drops TWO new files imports BOTH
    (counts summed), ignores a stale pre-run file, and logs one entry per file."""
    import os
    download_log, _ = _redirect_log(monkeypatch, tmp_path)
    ledger.create_account(conn, "AF Checking", "checking",
                          opening_balance=1000_00, opening_date="2026-06-30")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    _drop(ddir, "stale.ofx", OFX_FIXTURE, mtime=1000)          # pre-run -> ignored
    _drop(ddir, "one.ofx", OFX_FIXTURE, mtime=6000)            # 2 rows
    # A second file with fully DISTINCT rows (dedup is content-based, so fitids
    # alone are not enough) -> both files' rows are added, summed to 4.
    ofx2 = (OFX_FIXTURE
            .replace("AF-1001", "AF-2001").replace("AF-1002", "AF-2002")
            .replace("-50.00", "-77.00").replace("200.00", "321.00")
            .replace("Smiths Marketplace", "Target")
            .replace("Payroll ACH", "Bonus")
            .replace("20260705", "20260715").replace("20260710", "20260716"))
    _drop(ddir, "two.ofx", ofx2, mtime=7000)                  # 2 more (distinct) rows
    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(file_path=None, records=None)})
    dr = downloads.download_account(
        conn, client, "GetTxns", {"userName": "x"},
        account="AF Checking", account_type="checking",
        downloads_dir=str(ddir), now=5000)
    assert dr.mode == EXPORT
    assert dr.import_result.added == 4                          # both files summed
    assert "one.ofx" in dr.file_path and "two.ofx" in dr.file_path
    # One download-log entry PER imported file (the stale one is never touched).
    entries = download_log.read_recent(log_path=None)
    assert len(entries) == 2
    assert {e["decision"] for e in entries} == {"success"}
    assert sorted(os.path.basename(e["file_path"]) for e in entries) == \
        ["one.ofx", "two.ofx"]


def test_download_account_no_new_file_error_lists_qif(conn, tmp_path):
    """The no-file failure message now advertises .qif alongside .ofx/.qfx/.csv."""
    import os
    ledger.create_account(conn, "AF Checking", "checking")
    ddir = tmp_path / "Downloads"
    ddir.mkdir()
    _drop(ddir, "old.ofx", OFX_FIXTURE, mtime=1000)            # stale only
    client = FakeWebSlingerClient(
        schemas={"GetTxns": _SCRIPT_SCHEMA},
        results={"GetTxns": RunResult(file_path=None, records=None)})
    with pytest.raises(DownloadFailedError) as exc:
        downloads.download_account(
            conn, client, "GetTxns", {"userName": "x"},
            account="AF Checking", downloads_dir=str(ddir), now=5000)
    assert ".qif" in str(exc.value)


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
    dr = downloads.import_download_file(conn, str(FIXTURES / filename),
                                        account=acct, account_type=atype)
    assert dr.import_result.errors == 0
    assert dr.import_result.added == 3
    # exact re-import of the same file dedups completely (fitid, or the csv
    # importer's synthesized stable id).
    dr2 = downloads.import_download_file(conn, str(FIXTURES / filename),
                                         account=acct, account_type=atype)
    assert dr2.import_result.added == 0
    assert dr2.import_result.duplicates == 3


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
    dr = downloads.import_download_file(conn, str(FIXTURES / filename),
                                        account=acct, account_type=atype)
    assert dr.import_result.duplicates == 2
    assert dr.import_result.added == 1
    assert _n_txn(conn, aid) == 3                        # 2 migrated + 1 new, seam not doubled
    # the -50.00 charge exists exactly once (the migrated row), not duplicated
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=? AND amount=?",
        (aid, -50_00),
    ).fetchone()["c"] == 1
    # a live pull is NOT a migration: it must not move the watermark
    assert ledger.account_cutover_date(conn, aid) == "2026-07-10"
    # re-pulling the same window stays idempotent across the seam
    dr2 = downloads.import_download_file(conn, str(FIXTURES / filename),
                                         account=acct, account_type=atype)
    assert dr2.import_result.added == 0
    assert _n_txn(conn, aid) == 3


def test_fidelity_fixture_imports_cleanly(conn):
    dr = downloads.import_download_file(
        conn, str(FIXTURES / "fidelity.csv"), account="Fidelity Brokerage",
        account_type="investment")
    assert dr.import_result.errors == 0
    assert dr.import_result.added == 3
    assert dr.import_result.investments == 3


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
    dr = downloads.import_download_file(
        conn, str(FIXTURES / "fidelity.csv"), account="Fidelity Brokerage",
        account_type="investment")
    assert dr.import_result.duplicates == 2
    assert dr.import_result.investments == 1
    assert dr.import_result.added == 1
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
    MCP actions. Mammon imports the data (decision=success), and the log entry
    captures BOTH the failed-action count/raw error AND the success decision, plus
    the actual inputData sent and the imported counts."""
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
    downloads.download_account(
        conn, client, "GetTxns", {"userName": "1234567"},
        account="WF Checking", account_type="checking",
        account_number="1234567")

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
    assert e["source"] == "records"                          # imported directly
    assert e["imported"] == 2 and e["duplicates"] == 0
    assert "despite 3 failed MCP action" in e["reason"]      # WHY it's still success
    assert download_log.last_error() is None                 # a success, not an error


def test_download_log_records_failure(conn, tmp_path, monkeypatch):
    """FAILURE: the run returns no data and no new Downloads file appears, so
    download_account raises DownloadFailedError. The log must STILL capture a
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
        downloads.download_account(
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
        return None  # download_account ignores the return value

    def run_script(self, name, inputs, source="user"):
        self.calls.append((name, dict(inputs)))
        result = RunResult.from_mcp(self._payload)
        result.sent_input = dict(inputs)   # what McpWebSlingerClient records
        return result


def test_download_account_imports_output_data_rows_and_dedupes(
        conn, tmp_path, monkeypatch):
    """An API script returns rows in output_data (under an array name, NOT the
    literal 'records' key) and drops NO file. Mammon must bind to output_data,
    import DIRECTLY into the account (no Downloads scan), reconcile on re-run,
    and log the real sent inputData + the run's own UTC start/duration with no
    keyCocoon note on success."""
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

    dr = downloads.download_account(
        conn, client, "GetAnytownCUTransactions", sent,
        account="Anytown CU Savings", account_type="savings",
        account_number="1234567", downloads_dir=str(empty_dl))

    # Direct import from output_data -- no Downloads scan (empty dir did not
    # raise), 2 rows in.
    assert dr.mode == SCRAPE
    assert dr.import_result.added == 2 and dr.import_result.duplicates == 0

    # Re-run the identical payload: reconcile/dedupe -> nothing added.
    dr2 = downloads.download_account(
        conn, client, "GetAnytownCUTransactions", sent,
        account="Anytown CU Savings", account_type="savings",
        account_number="1234567", downloads_dir=str(empty_dl))
    assert dr2.import_result.added == 0 and dr2.import_result.duplicates == 2

    entries = download_log.read_recent(log_path=None)
    e = entries[0]
    assert e["decision"] == "success" and e["source"] == "records"
    # The ACTUAL post-coercion inputData sent -- a real array, not a string.
    assert e["input_data"]["subAccountName"] == ["Share Savings"]
    # The RUN's own reported timing (UTC), not Mammon's local wall clock.
    assert e["ts"] == "2026-08-22T13:37:06Z"
    assert e["mcp"]["run_started_utc"] == "2026-08-22T13:37:06Z"
    assert e["mcp"]["run_duration_seconds"] == 209.5
    assert "mammon_wait_seconds" in e["mcp"]          # Mammon's wait, labelled apart
    # No keyCocoon re-auth red herring on a successful run.
    assert "keyCocoon" not in (e.get("reason") or "")
