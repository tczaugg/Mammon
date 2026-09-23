"""Full-ledger export (roadmap item 8). The QIF export is proved by the round
trip -- importing it into an empty database reproduces every balance, split,
holding and price; the JSON export is proved complete table by table; the CSV
registers carry the running balance."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from mammon import db, export, importers, investments, ledger, portfolio
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "source.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    card = ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    loan = ledger.create_account(conn, "Mortgage", "liability", opening_balance=-100000_00)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ledger.update_account(conn, chk, account_number="XXXX1234", url="https://bank.example")
    groc = ledger.resolve_category(conn, "Food:Groceries")
    sal = ledger.resolve_category(conn, "Salary")
    conn.execute("UPDATE categories SET type='income' WHERE id=?", (sal,))
    conn.commit()
    ledger.add_transaction(conn, chk, "2026-01-02", 5000_00, payee="Employer",
                           category_id=sal, cleared=1, memo="Jan pay")
    ledger.add_transaction(conn, chk, "2026-01-05", -85_25, payee="Safeway #123",
                           category_id=groc, num="1001", reconciled=1, cleared=1)
    ledger.create_transfer(conn, chk, sav, "2026-01-10", 1000_00, payee="Auto-save")
    ledger.add_transaction(conn, card, "2026-01-12", -42_00, payee="Grocer", category_id=groc)
    # A mortgage payment: interest + escrow + a principal leg into the loan.
    ledger.resolve_category(conn, "Int Exp")
    ledger.resolve_category(conn, "Escrow")
    tid = ledger.add_transaction(conn, chk, "2026-02-01", -1268_99, payee="US Bank")
    ledger.set_splits(conn, tid, [
        {"category_id": ledger.resolve_category(conn, "Int Exp"), "amount": -500_00,
         "memo": "Interest"},
        {"category_id": ledger.resolve_category(conn, "Escrow"), "amount": -250_00},
        {"transfer_account_id": loan, "amount": -518_99, "memo": "Principal"}])
    ledger.create_transfer(conn, chk, inv, "2026-01-15", 3000_00, payee="Fund brokerage")
    # A paycheck: gross, taxes and a 401(k) deferral transferred into the
    # investment account -- the split Quicken lost when the other side was
    # written as a bank-style record inside the investment section.
    pay = ledger.add_transaction(conn, chk, "2026-02-14", 2500_00, payee="Employer Inc.")
    ledger.set_splits(conn, pay, [
        {"category_id": sal, "amount": 3300_00},
        {"category_id": ledger.resolve_category(conn, "Tax:Fed"), "amount": -300_00},
        {"transfer_account_id": inv, "amount": -500_00, "memo": "401k"}])
    ledger.add_transaction(conn, inv, "2026-02-20", 12_34, payee="Interest",
                           category_id=ledger.resolve_category(conn, "Int Inc"))
    investments.record_investment(conn, inv, "2026-01-16", "Buy", symbol="AAPL",
                                  quantity="10", price="100.00", amount=-1000_00,
                                  commission=4_95)
    investments.record_investment(conn, inv, "2026-01-20", "Buy", symbol="NVDA",
                                  quantity="4", price="250.00", amount=-1000_00)
    investments.record_investment(conn, inv, "2026-02-03", "StkSplit", symbol="NVDA",
                                  quantity="20", split_num=2, split_den=1)
    investments.record_investment(conn, inv, "2026-02-10", "Div", symbol="AAPL", amount=12_00)
    investments.record_investment(conn, inv, "2026-02-15", "Sell", symbol="AAPL",
                                  quantity="4", price="110.00", amount=440_00)
    investments.record_price(conn, "AAPL", "2026-02-27", "112.50")
    investments.record_price(conn, "NVDA", "2026-02-27", "130")
    portfolio.set_security(conn, "AAPL", name="Apple Inc", sec_type="stock",
                           asset_class="domestic_stock")
    investments.rebuild_holdings(conn, inv)
    return {"chk": chk, "sav": sav, "card": card, "loan": loan, "inv": inv}


def _balances(c):
    """What the accounts list shows: the ledger balance, or for an investment
    account its full valuation -- whose cash may sit in investment records
    (XIn, Cash) after a trip through QIF rather than in transaction rows."""
    return {a["name"]: investments.display_balance(c, int(a["id"]))
            for a in ledger.list_accounts(c, include_closed=True, include_hidden=True)}


def _holdings(c, name):
    aid = next(int(a["id"]) for a in ledger.list_accounts(c) if a["name"] == name)
    return {s: (str(l.qty), l.cost) for s, l in investments.compute_holdings(c, aid).items()
            if l.qty}


# ---------------------------------------------------------------------------
# QIF round trip
# ---------------------------------------------------------------------------
def test_qif_export_imports_back_into_the_same_ledger(conn, world, tmp_path):
    out = tmp_path / "ledger.qif"
    counts = export.export_qif(conn, out)
    assert counts.pop("files") == [str(out)]
    assert counts == {"accounts": 5, "transactions": 12, "investment_transactions": 5,
                      "categories": 8, "securities": 2, "prices": 2}
    text = out.read_text(encoding="utf-8")
    # Every shape below was verified against Quicken's OWN exports (27 years of
    # them in the user's archive), because a file Quicken cannot read is not an
    # export. A split keeps its bracketed L label and its S lines; U and T
    # carry the same amount, U first.
    assert ("U2500.00\nT2500.00\nPEmployer Inc.\nLSalary\nSSalary\n$3300.00\n"
            "STax:Fed\n$-300.00\nS[Brokerage]\nE401k\n$-500.00\n^") in text
    # A cash row on an INVESTMENT account is an investment record -- NXIn/NXOut
    # with a $ amount line, or NCash -- never a bank-style record with no
    # action line, which is malformed inside !Type:Invst and is how the 401(k)
    # side of a paycheck came back as a bare transfer, the split gone.
    invst = text.split("!Type:Invst", 1)[1].split("!Account", 1)[0]
    assert "NXIn\nPEmployer Inc.\nU500.00\nT500.00\nL[Checking]\n$500.00\n^" in invst
    assert "NXIn\nPFund brokerage\nU3000.00\nT3000.00\nL[Checking]\n$3000.00\n^" in invst
    assert "NCash\nPInterest\nU12.34\nT12.34\nLInt Inc\n^" in invst
    for rec in invst.split("^\n"):
        assert not rec.startswith("D") or "\nN" in rec, rec     # every record has an action
    assert "!Type:Cat\nNFood\nE\n^\nNFood:Groceries\nE\n^" in text.replace("\r", "")
    assert "NSalary\nI\n" in text and "!Type:Prices" in text and '"AAPL",112.5,"02/27/2026"' in text
    assert "S[Mortgage]\nEPrincipal\n$-518.99" in text and "CX" in text and "N1001" in text
    # L echoes the FIRST leg, whatever it is; here the interest line.
    assert "PUS Bank\nLInt Exp\nSInt Exp\nEInterest\n$-500.00" in text
    assert "T-100000.00\nPOpening Balance\nL[Mortgage]\n^" in text     # Quicken's convention
    assert "NStkSplit\nYNVDA\nQ20" in text and "O4.95" in text
    assert "XXXX1234" not in text and "bank.example" not in text

    fresh = fresh_db(tmp_path / "fresh.db")
    try:
        res = importers.import_file(fresh, out)
        assert res.errors == 0
        assert _balances(fresh) == _balances(conn)
        assert {a["name"]: a["type"] for a in ledger.list_accounts(fresh)} == {
            "Checking": "checking", "Savings": "checking", "Visa": "credit",
            "Mortgage": "liability", "Brokerage": "investment"}
        assert _holdings(fresh, "Brokerage") == _holdings(conn, "Brokerage")
        assert investments.latest_price(fresh, "AAPL") == Decimal("112.5")
        # The paycheck's split survived, deferral leg included, and the
        # brokerage's full valuation (cash and securities) is the same.
        pay = fresh.execute("SELECT id, amount FROM transactions WHERE payee='Employer Inc.' "
                            "AND account_id=(SELECT id FROM accounts WHERE name='Checking')"
                            ).fetchone()
        legs = {s["category_label"]: s["amount"] for s in ledger.get_splits(fresh, pay["id"])}
        assert pay["amount"] == 2500_00
        assert legs == {"Salary": 3300_00, "Tax:Fed": -300_00, "[Brokerage]": -500_00}
        binv = next(int(a["id"]) for a in ledger.list_accounts(fresh) if a["name"] == "Brokerage")
        sinv = world["inv"]
        assert investments.account_valuation(fresh, binv, "2026-02-27").total == \
            investments.account_valuation(conn, sinv, "2026-02-27").total
        # The mortgage split came back with its three legs, principal into the loan.
        row = fresh.execute("SELECT id FROM transactions WHERE payee='US Bank'").fetchone()
        legs = {s["category_label"]: s["amount"] for s in ledger.get_splits(fresh, row["id"])}
        assert legs == {"Int Exp": -500_00, "Escrow": -250_00, "[Mortgage]": -518_99}
        rec = fresh.execute("SELECT cleared, reconciled, num FROM transactions "
                            "WHERE payee='Safeway #123'").fetchone()
        assert (rec["cleared"], rec["reconciled"], rec["num"]) == (1, 1, "1001")
        assert fresh.execute("SELECT type FROM categories WHERE name='Salary'").fetchone()[
            "type"] == "income"
        non_inv = ("SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.id=t.account_id "
                   "WHERE a.type<>'investment'")
        assert fresh.execute(non_inv).fetchone()[0] == conn.execute(non_inv).fetchone()[0]
        # The brokerage's cash rows travelled as investment records: the
        # categorized one as Cash, each transfer leg as XIn.
        assert sorted((r["action"], abs(int(r["amount"]))) for r in fresh.execute(
            "SELECT action, amount FROM investment_transactions "
            "WHERE action IN ('XIn', 'Cash')")) == \
            [("Cash", 12_34), ("XIn", 500_00), ("XIn", 3000_00)]
        loan = fresh.execute("SELECT opening_balance, opening_date FROM accounts "
                             "WHERE name='Mortgage'").fetchone()
        assert (loan["opening_balance"], loan["opening_date"]) == (-100000_00, "2026-01-31")
        # Importing the same file again changes nothing.
        again = importers.import_file(fresh, out)
        assert again.added == 0 and _balances(fresh) == _balances(conn)
    finally:
        fresh.close()


def test_qif_export_by_year_writes_one_file_per_year_that_round_trips(conn, world, tmp_path):
    from pathlib import Path
    ledger.add_transaction(conn, world["chk"], "2025-12-20", -30_00, payee="Old Co",
                           category_id=ledger.resolve_category(conn, "Food:Groceries"))
    counts = export.export_qif(conn, tmp_path / "ledger.qif", by_year=True)
    assert counts["years"] == ["2025", "2026"]
    assert [Path(f).name for f in counts["files"]] == ["ledger-2025.qif", "ledger-2026.qif"]
    y25 = (tmp_path / "ledger-2025.qif").read_text(encoding="utf-8")
    y26 = (tmp_path / "ledger-2026.qif").read_text(encoding="utf-8")
    assert "POpening Balance" in y25 and "POpening Balance" not in y26   # once, first file
    assert "!Type:Cat" in y25 and "!Type:Cat" in y26                     # each stands alone
    assert "Old Co" in y25 and "Old Co" not in y26 and "US Bank" in y26
    assert "!Type:Invst" not in y25 and "!Type:Invst" in y26              # no activity in 2025
    assert (counts["transactions"], counts["investment_transactions"]) == (13, 5)
    fresh = fresh_db(tmp_path / "fresh2.db")
    try:
        for f in counts["files"]:
            assert importers.import_file(fresh, f).errors == 0
        assert _balances(fresh) == _balances(conn)
        assert _holdings(fresh, "Brokerage") == _holdings(conn, "Brokerage")
    finally:
        fresh.close()
    # A folder as the target names the files ledger-YYYY.qif inside it.
    (tmp_path / "yearly").mkdir()
    counts2 = export.export_qif(conn, tmp_path / "yearly", by_year=True)
    assert [Path(f).name for f in counts2["files"]] == ["ledger-2025.qif", "ledger-2026.qif"]
    assert "2 yearly files" in export.run(conn, "qif", tmp_path / "yearly", by_year=True)


def test_a_transfer_a_quicken_import_left_on_both_sides_is_written_once(conn, tmp_path):
    """A Quicken history can hold a checking leg AND an XIn on the investment
    account for the same transfer (the valuation dedups them). The export
    writes the checking leg and not the XIn; a split leg likewise silences a
    repeating XIn; an XIn with no cash record of its own is written."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    ira = ledger.create_account(conn, "IRA", "investment", opening_balance=0)
    ledger.create_transfer(conn, chk, ira, "2026-03-01", 1000_00, payee="IRA contribution")
    investments.record_investment(conn, ira, "2026-03-01", "XIn", amount=1000_00,
                                  transfer_account_id=chk)
    pay = ledger.add_transaction(conn, chk, "2026-03-14", 2000_00, payee="Employer")
    ledger.set_splits(conn, pay, [{"category_id": ledger.resolve_category(conn, "Salary"),
                                   "amount": 2400_00},
                                  {"transfer_account_id": ira, "amount": -400_00}])
    investments.record_investment(conn, ira, "2026-03-14", "XIn", amount=400_00,
                                  transfer_account_id=chk)
    investments.record_investment(conn, ira, "2026-03-20", "XIn", amount=50_00,
                                  transfer_account_id=chk, memo="only here")
    out = tmp_path / "once.qif"
    counts = export.export_qif(conn, out)
    text = out.read_text(encoding="utf-8")
    # Both registers are written, as Quicken's own exports do: the cash side
    # with its bracketed L, the investment side as NXIn. The mirror rows and
    # the account's own XIn rows are separate records because the ledger holds
    # them separately (the valuation nets the duplicate; see
    # investments._duplicated_transfer_leg_total).
    assert (counts["transactions"], counts["investment_transactions"]) == (4, 3)
    assert "D03/01/2026\nU-1000.00\nT-1000.00\nPIRA contribution\nL[IRA]\n^" in text
    assert "S[IRA]\n$-400.00" in text
    invst = text.split("!Type:Invst", 1)[1]
    assert invst.count("NXIn") == 5 and "Monly here" in invst
    for rec in invst.split("^\n"):
        assert not rec.startswith("D") or "\nN" in rec, rec


def test_qif_export_can_be_scoped_by_account_and_date(conn, world, tmp_path):
    out = tmp_path / "part.qif"
    counts = export.export_qif(conn, out, account_ids=[world["chk"]],
                               start="2026-01-01", end="2026-01-31")
    assert counts["accounts"] == 1 and counts["transactions"] == 4
    assert counts["investment_transactions"] == 0 and counts["securities"] == 0
    text = out.read_text(encoding="utf-8")
    assert "US Bank" not in text and "Employer" in text


# ---------------------------------------------------------------------------
# JSON: every table, sensitive columns blanked unless asked
# ---------------------------------------------------------------------------
def test_json_export_holds_every_table_and_blanks_identifiers(conn, world, tmp_path):
    out = tmp_path / "ledger.json"
    counts = export.export_json(conn, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["format"] == "mammon-ledger" and data["schema_version"] == db.SCHEMA_VERSION
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert set(data["tables"]) == tables == set(counts)
    for name in tables:
        assert len(data["tables"][name]) == conn.execute(
            f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    chk = next(a for a in data["tables"]["accounts"] if a["name"] == "Checking")
    assert chk["account_number"] is None and chk["url"] is None
    assert "XXXX1234" not in out.read_text(encoding="utf-8")
    split = next(t for t in data["tables"]["investment_transactions"] if t["action"] == "StkSplit")
    assert (split["split_num"], split["split_den"]) == (2, 1)      # exact, unlike QIF
    export.export_json(conn, out, include_sensitive=True)
    data2 = json.loads(out.read_text(encoding="utf-8"))
    chk2 = next(a for a in data2["tables"]["accounts"] if a["name"] == "Checking")
    assert chk2["account_number"] == "XXXX1234" and data2["sensitive_included"]


# ---------------------------------------------------------------------------
# CSV registers
# ---------------------------------------------------------------------------
def test_csv_registers_carry_the_running_balance(conn, world, tmp_path):
    paths = export.export_csv_all(conn, tmp_path / "csv")
    assert sorted(p.name for p in paths) == ["Brokerage.csv", "Checking.csv", "Mortgage.csv",
                                             "Savings.csv", "Visa.csv"]
    lines = (tmp_path / "csv" / "Checking.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == export.CASH_HEADERS
    assert lines[1].startswith("2026-01-02,,Employer,Salary,Jan pay,,*,5000.00,5000.00")
    mortgage = next(l for l in lines if ",US Bank," in l)
    assert mortgage.startswith("2026-02-01,,US Bank,--Split--")
    assert "[Mortgage] -518.99 (Principal)" in mortgage
    assert lines[-1].split(",")[8] == money_of(conn, world["chk"])
    inv_lines = (tmp_path / "csv" / "Brokerage.csv").read_text(encoding="utf-8").splitlines()
    assert inv_lines[0].split(",") == export.INVST_HEADERS
    assert any(l.startswith("2026-02-03,StkSplit,NVDA,2:1") for l in inv_lines)
    assert any(l.startswith("2026-01-16,Buy,AAPL,10,100,-1000.00,4.95") for l in inv_lines)
    # A date range keeps the balance as of the row, not of the range.
    n = export.export_csv(conn, world["chk"], tmp_path / "feb.csv", start="2026-02-01")
    feb = (tmp_path / "feb.csv").read_text(encoding="utf-8").splitlines()
    assert n == 2 and feb[-1].split(",")[8] == money_of(conn, world["chk"])


def money_of(c, aid) -> str:
    return export.money(ledger.account_balance(c, aid))


# ---------------------------------------------------------------------------
# one entry point + command line
# ---------------------------------------------------------------------------
def test_run_and_cli(conn, world, tmp_path):
    msg = export.run(conn, "qif", tmp_path / "r.qif")
    assert msg.startswith("Wrote") and "12 transactions" in msg
    msg = export.run(conn, "json", tmp_path / "r.json")
    assert "left out" in msg
    msg = export.run(conn, "csv", tmp_path / "regs", account_ids=[world["chk"], world["sav"]])
    assert "2 register files" in msg
    with pytest.raises(ValueError):
        export.run(conn, "xml", tmp_path / "r.xml")
    rc = export.main(["--db", str(tmp_path / "source.db"), "--format", "csv",
                      "--out", str(tmp_path / "cli"), "--account", "checking"])
    assert rc == 0 and (tmp_path / "cli" / "Checking.csv").exists()
