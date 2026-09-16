"""What an investment import could not settle is reported, and nothing else is
(reports/import_audit.py, SRD 6.2a).

Each finding shape is one a real Quicken export produced; each quiet case is one
an early draft of this report flagged wrongly. All data is synthetic.
"""
from __future__ import annotations

import pytest

from mammon import db, importers, instruments, investments, ledger, securities
from mammon.reports import import_audit


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "audit.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


def _rec(conn, acct, date, action, symbol, qty, price=None, amount=None):
    investments.record_investment(conn, acct, date, action, symbol=symbol, quantity=qty,
                                  price=price, amount=amount)


def _price(conn, symbol, date, close):
    investments.record_prices(conn, [(symbol, date, close, "qif")])


def _kinds(conn, acct, **kw):
    return [(f.kind, f.symbol, f.date) for f in import_audit.audit(conn, [acct], **kw)]


def test_a_removal_under_a_spelling_that_never_held_shares_is_reported(conn, acct):
    """A plan fund exported under two names: purchases under one, the fee
    removals under the other."""
    _rec(conn, acct, "2024-01-05", "Buy", "BOND INDEX", "100", "10", 100_000)
    _rec(conn, acct, "2025-01-07", "ShrsOut", "BOND INDEX(ANON)", "0.5")
    _price(conn, "BOND INDEX", "2025-01-07", "10")
    found = import_audit.audit(conn, [acct], as_of="2025-01-31")
    assert [(f.kind, f.symbol, f.date) for f in found] == [
        (import_audit.REMOVED_UNHELD, "BOND INDEX(ANON)", "2025-01-07")]
    assert "0.5 shares removed when 0 were held" in found[0].detail


def test_a_sale_recorded_before_the_same_days_purchase_is_not_a_shortfall(conn, acct):
    _rec(conn, acct, "2025-03-03", "Sell", "ACME", "50", "20", 100_000)
    _rec(conn, acct, "2025-03-03", "Buy", "ACME", "50", "19", 95_000)
    assert _kinds(conn, acct, as_of="2025-03-03") == []


def test_a_short_entered_as_sell_then_buy_is_not_reported(conn, acct):
    """Old broker exports wrote a short sale as a plain Sell covered by a later
    Buy. The position ends at zero; nothing is wrong with it."""
    _rec(conn, acct, "2001-01-03", "Sell", "ACME", "100", "20", 200_000)
    _rec(conn, acct, "2001-02-03", "Buy", "ACME", "100", "15", 150_000)
    assert _kinds(conn, acct, as_of="2001-02-03") == []


def test_a_written_option_is_not_a_removal_of_unheld_shares(conn, acct):
    sym = "ACME  260417C00045000"
    conn.execute("INSERT INTO securities(symbol, name) VALUES (?,?)", (sym, sym))
    securities.set_kinds(conn, [dict(
        symbol=sym, kind=instruments.Kind.OPTION.value, kind_source="user",
        multiplier="1", underlying="ACME", expiration="2026-04-17", strike="45",
        option_right="C")])
    _rec(conn, acct, "2026-03-02", "Sell", sym, "500", "4", 200_000)
    _price(conn, sym, "2026-03-02", "4")
    assert _kinds(conn, acct, as_of="2026-03-02") == []


def test_shares_arriving_with_no_price_or_amount_have_no_basis(conn, acct):
    """The new shares of a merger, as Quicken exports them."""
    _rec(conn, acct, "2022-05-23", "ShrsOut", "OLDCO", "0")
    _rec(conn, acct, "2022-05-23", "ShrsIn", "NEWCO", "56")
    _rec(conn, acct, "2022-06-01", "ShrsIn", "PAIDCO", "10", "5", 5_000)
    _price(conn, "NEWCO", "2022-06-01", "100")
    _price(conn, "PAIDCO", "2022-06-01", "5")
    assert _kinds(conn, acct, as_of="2022-06-01") == [
        (import_audit.ZERO_BASIS_ARRIVAL, "NEWCO", "2022-05-23")]


def test_a_holding_the_source_stopped_pricing_is_stale_the_rest_are_not(conn, acct):
    """Measured from the newest price in the ledger, not from today: every holding
    in an old export is old against the calendar."""
    _rec(conn, acct, "2019-01-02", "Buy", "PLAN FUND", "10", "10", 10_000)
    _rec(conn, acct, "2019-01-02", "Buy", "ACME", "10", "10", 10_000)
    _price(conn, "PLAN FUND", "2020-03-09", "11")
    _price(conn, "ACME", "2025-12-31", "30")
    assert _kinds(conn, acct) == [(import_audit.STALE_PRICE, "PLAN FUND", None)]


def test_a_closed_account_reports_no_stale_prices(conn, acct):
    _rec(conn, acct, "2019-01-02", "Buy", "PLAN FUND", "10", "10", 10_000)
    _price(conn, "PLAN FUND", "2019-01-02", "10")
    _price(conn, "ACME", "2025-12-31", "30")
    ledger.update_account(conn, acct, closed_flag=1)
    assert _kinds(conn, acct) == []


def test_an_option_whose_trades_do_not_settle_its_units_is_reported(conn, tmp_path):
    """Only a close with no total: nothing shows what quantity x price means."""
    text = (
        "!Type:Security\n"
        "NXYZ  270115C00020000 XYZ 15JAN27 20.0 C\nSXYZ  270115C00020000\nTOption\n^\n"
        "!Account\nNBrokerage\nTInvst\n^\n!Type:Invst\n"
        "D5/18'26\nNShtSell\nYXYZ  270115C00020000 XYZ 15JAN27 20.0 C\nI2\nQ5\n^\n"
    )
    path = tmp_path / "units.qif"
    path.write_text(text, encoding="utf-8")
    res = importers.import_file(conn, str(path), account="Brokerage", account_type="investment")
    assert res.investment_account_ids
    kinds = [f.kind for f in import_audit.audit(conn, res.investment_account_ids,
                                                as_of="2026-05-18")]
    assert import_audit.OPTION_UNITS in kinds


def test_format_findings_heads_each_kind_and_counts_the_rest(conn, acct):
    for i in range(3):
        _rec(conn, acct, "2022-05-23", "ShrsIn", f"NEWCO{i}", "10")
        _price(conn, f"NEWCO{i}", "2022-05-23", "1")
    lines = import_audit.format_findings(import_audit.audit(conn, [acct]), limit=2)
    assert lines[0] == "Shares that arrived with no cost basis (3):"
    assert lines[-1] == "  ...and 1 more"
