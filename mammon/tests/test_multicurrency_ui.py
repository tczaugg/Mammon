"""Per-account currency exposed in the UI (SRD 5.4a).

The per-account `accounts.currency` column and the whole `mammon.fx` domain layer
already existed (see test_fx.py) but nothing in the UI set, showed, or rendered a
non-base currency, and `ledger.create_account` -- the sole account writer -- had no
way to set it, so a currency chosen at creation was silently lost. These tests
cover the UI:

* `ledger.create_account` accepts and persists a native currency (normalized),
  read back through the domain layer -- currency is chosen ONCE, at creation.
* the New Account dialog offers a currency (default = base) that flows to the
  ledger; the Account Details dialog shows it READ-ONLY (immutable property).
* per-account amounts render in the account's own currency (bare for the base
  currency, tagged for a foreign one), and net worth folds foreign accounts to
  the base currency through `mammon.fx`; a non-zero foreign balance with no rate
  is surfaced UNCONVERTED and left out of the total, never folded in at 1:1.

Synthetic data only -- no real account names, numbers, or amounts.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, fx, ledger
from mammon.ui.models import (
    AccountsModel, RegisterModel, currency_symbol, fmt_amount_ccy, fmt_cents,
    fmt_money,
)
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mc_ui.db")
    yield c
    c.close()


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# ledger.create_account persists a native currency (the sole account writer)
# ---------------------------------------------------------------------------
def test_create_account_persists_non_base_currency(conn):
    aid = ledger.create_account(conn, "Euro Cash", "cash", currency="EUR")
    # read back through the domain layer, not by peeking at raw SQL
    assert fx.get_account_currency(conn, aid) == "EUR"
    assert ledger.get_account(conn, aid)["currency"] == "EUR"


def test_create_account_defaults_to_base_currency(conn):
    aid = ledger.create_account(conn, "Plain Checking", "checking")
    assert fx.get_account_currency(conn, aid) == fx.BASE_CURRENCY == "USD"


def test_create_account_normalizes_currency(conn):
    lower = ledger.create_account(conn, "Sterling", "savings", currency="  gbp ")
    assert fx.get_account_currency(conn, lower) == "GBP"
    blank = ledger.create_account(conn, "Blank Ccy", "cash", currency="")
    assert fx.get_account_currency(conn, blank) == "USD"
    none = ledger.create_account(conn, "None Ccy", "cash", currency=None)
    assert fx.get_account_currency(conn, none) == "USD"


# ---------------------------------------------------------------------------
# New Account dialog: offers a currency (default base) that flows to the ledger
# ---------------------------------------------------------------------------
def test_new_account_dialog_defaults_to_base_currency(qapp):
    from mammon.ui.widgets import NewAccountDialog
    dlg = NewAccountDialog()
    try:
        assert dlg.currency.currentText() == fx.BASE_CURRENCY
        assert dlg.values()["currency"] == "USD"
    finally:
        dlg.deleteLater()


def test_new_account_dialog_currency_flows_to_ledger(conn, qapp):
    from mammon.ui.widgets import NewAccountDialog
    dlg = NewAccountDialog()
    try:
        dlg.name.setText("Travel EUR")
        dlg.currency.setCurrentText("eur")            # editable + case-insensitive
        v = dlg.values()
        assert v["currency"] == "EUR"
        aid = ledger.create_account(conn, v["name"], v["type"],
                                    currency=v["currency"])
    finally:
        dlg.deleteLater()
    assert fx.get_account_currency(conn, aid) == "EUR"


# ---------------------------------------------------------------------------
# per-account amount rendering: bare for base, tagged for foreign
# ---------------------------------------------------------------------------
def test_currency_symbol_maps_and_falls_back():
    assert currency_symbol("USD") == "$"
    assert currency_symbol(None) == "$"
    assert currency_symbol("eur") == "€"
    # an unmapped ISO code is still labelled -- never silently shown as dollars
    assert currency_symbol("THB") == "THB "


def test_fmt_amount_ccy_base_is_bare_foreign_is_tagged():
    # base currency (and blank) render exactly like the classic bare formatter
    assert fmt_amount_ccy(1234_56, "USD") == fmt_cents(1234_56) == "1,234.56"
    assert fmt_amount_ccy(1234_56, None) == fmt_cents(1234_56)
    # a foreign account is tagged so its balance is never read as base dollars
    assert fmt_amount_ccy(1234_56, "EUR") == "€1,234.56"
    assert fmt_amount_ccy(-500_00, "EUR") == "-€500.00"
    assert fmt_amount_ccy(500_00, "THB") == "THB 500.00"


def test_fmt_money_currency_symbol_selectable():
    assert fmt_money(100_00) == "$100.00"                 # default base symbol
    assert fmt_money(100_00, currency="GBP") == "£100.00"


def test_accounts_model_carries_and_renders_currency(conn, qapp):
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Euro Cash", "cash",
                          opening_balance=50_00, currency="EUR")
    model = AccountsModel(conn)
    rows = {r["name"]: r for r in model.rows()}
    assert rows["Euro Cash"]["currency"] == "EUR"
    assert rows["US Checking"]["currency"] == "USD"

    def balance_text(name):
        for i, r in enumerate(model.rows()):
            if r["name"] == name:
                idx = model.index(i, AccountsModel.BALANCE)
                return model.data(idx)
        raise AssertionError(name)

    # foreign account tagged; base account bare (dense classic look preserved)
    assert balance_text("Euro Cash") == "€50.00"
    assert balance_text("US Checking") == "100.00"


# ---------------------------------------------------------------------------
# register: model exposes the currency and the header names a foreign one
# ---------------------------------------------------------------------------
def test_register_model_reports_account_currency(conn):
    aid = ledger.create_account(conn, "Euro Checking", "checking", currency="EUR")
    model = RegisterModel(conn, aid)
    assert model.account_currency() == "EUR"
    usd = ledger.create_account(conn, "US Checking", "checking")
    assert RegisterModel(conn, usd).account_currency() == "USD"


def test_register_header_names_foreign_currency(conn, qapp):
    from mammon.ui.widgets import RegisterWidget
    eur = ledger.create_account(conn, "Euro Checking", "checking", currency="EUR")
    usd = ledger.create_account(conn, "US Checking", "checking")
    weur = RegisterWidget(conn, eur)
    wusd = RegisterWidget(conn, usd)
    try:
        weur._refresh_header()
        wusd._refresh_header()
        assert "EUR" in weur.header.text()
        # a base-currency register keeps its plain name -- no currency clutter
        assert wusd.header.text() == "US Checking"
    finally:
        weur.deleteLater()
        wusd.deleteLater()


# ---------------------------------------------------------------------------
# Account Details dialog shows the currency READ-ONLY (immutable property)
# ---------------------------------------------------------------------------
def test_account_details_shows_currency_read_only(conn, qapp):
    from mammon.ui.widgets import AccountDetailsDialog
    aid = ledger.create_account(conn, "Euro Cash", "cash", currency="EUR")
    acct = ledger.get_account(conn, aid)
    dlg = AccountDetailsDialog(acct, conn=conn)
    try:
        assert dlg.currency_display.text() == "EUR"
        assert dlg.currency_display.isReadOnly()
        # currency is NOT written back from this dialog -- it is immutable here
        assert "currency" not in dlg.values()
    finally:
        dlg.deleteLater()


# ---------------------------------------------------------------------------
# net worth folds foreign accounts to the base currency through mammon.fx
# ---------------------------------------------------------------------------
def test_net_worth_folds_foreign_through_fx(conn, qapp):
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Euro Cash", "cash",
                          opening_balance=50_00, currency="EUR")
    fx.set_rate(conn, "2026-01-01", "EUR", "USD", "1.10")
    model = AccountsModel(conn)
    # 100.00 USD + 50.00 EUR * 1.10 = 100.00 + 55.00 = 155.00
    assert model.net_worth() == 155_00


def test_net_worth_excludes_foreign_without_rate(conn, qapp):
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Euro Cash", "cash",
                          opening_balance=50_00, currency="EUR")
    model = AccountsModel(conn)
    # no EUR->USD rate: the non-zero EUR balance is NEVER folded in at 1:1 (which
    # would overstate net worth by the whole 50.00). It is surfaced as an
    # unconverted line and left out, so the total is the honest USD-only figure.
    assert model.net_worth() == ledger.net_worth(conn) == 100_00
    nwc = fx.net_worth_currencies(conn)
    assert nwc.unconverted == ["EUR"]
    assert not nwc.is_complete
    # ...and the model names them, which is what the account bar renders.
    assert model.unconverted_currencies() == ["EUR"]


def test_net_worth_strip_warns_instead_of_printing_a_partial_total(conn, qapp):
    """A total that CANNOT be computed is shown as the warning triangle, never as
    a number.

    The per-currency subtotals are complete; only the roll-up is impossible. A
    number here would silently omit the EUR 50 -- the total would read as though
    the foreign balance did not exist -- so the strip shows the same warning mark
    the register uses for an unassigned split remainder, and the tooltip names
    both the missing currency and the two ways to supply a rate."""
    from mammon.ui.models import fmt_money
    from mammon.ui.widgets import AccountBar
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Euro Cash", "cash",
                          opening_balance=50_00, currency="EUR")
    bar = AccountBar(conn)
    try:
        assert bar.net_amount.text() == "", "a partial total was printed anyway"
        pm = bar.net_amount.pixmap()
        assert pm is not None and not pm.isNull(), "no warning mark was shown"
        tip = bar.net_amount.toolTip()
        assert "EUR" in tip                      # WHAT is missing
        assert "Exchange Rates" in tip           # WHERE to fix it
        assert "Add" in tip and "Refresh Rates" in tip   # BOTH ways to fix it
        assert bar.net_row.toolTip() == tip      # hovering the label works too

        # Once a rate exists the strip is a number again, with no stale tooltip.
        fx.set_rate(conn, "2026-01-01", "EUR", "USD", "1.10")
        bar.refresh()
        assert bar.net_amount.text() == fmt_money(155_00)
        assert bar.net_amount.toolTip() == "" and bar.net_row.toolTip() == ""
    finally:
        bar.deleteLater()
