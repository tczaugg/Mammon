"""Exchange-rate entry/refresh UI (SRD 5.4a) -- the surface over the existing
``mammon.fx`` dated rate store.

The store, the reader, and the net-worth fold already existed (``test_fx.py``,
``test_multicurrency_ui.py``); what these tests hold down is the UI that PUTS a
rate in, end to end: a rate entered through the dialog's Add flow, or refreshed
through its fetch seam, makes a foreign-currency account value correctly against
it. Every write here goes through :func:`mammon.fx.set_rate` (the store's single
writer -- ``fetch_rates`` funnels through it too); the dialog adds no second path
to ``fx_rates``.

Modal safety (CLAUDE.md, headless-modal hazard): the fetch goes through the
injected ``fx_source`` and the ``_notify`` seam is patched, so no test opens a
window that would block forever under the offscreen platform.

Synthetic data only -- no real account names, numbers, or amounts.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt5.QtCore import QDate
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from mammon import db, fx, ledger
from mammon.ui.fx_rates_dialog import FxRateEditor, FxRatesDialog
from mammon.ui.models import AccountsModel
from mammon.tests import fresh_db


@pytest.fixture(scope="module", autouse=True)
def qapp():
    """One QApplication for the module -- constructing a QWidget without one takes
    the interpreter down with no traceback."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "fx_ui.db")
    yield c
    c.close()


class _FakeFxSource:
    """An injected FX source: a fixed close for each requested pair, the shape
    :func:`mammon.fx.fetch_rates` expects (``get_rates(pairs) -> list[FxRate]``).
    A pair that is absent yields nothing, which is how a real source declines."""

    source_name = "fake"

    def __init__(self, rates, date="2026-02-01"):
        self.rates = rates            # {(base, quote): rate_text}
        self.date = date
        self.seen = []

    def get_rates(self, pairs):
        out = []
        for base, quote in pairs:
            self.seen.append((base, quote))
            r = self.rates.get((base, quote))
            if r is not None:
                out.append(fx.FxRate(self.date, base, quote, r, self.source_name))
        return out


def _two_accounts(conn):
    """A base-currency (USD) account and a EUR account, round synthetic balances."""
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Euro Cash", "cash",
                          opening_balance=50_00, currency="EUR")


# ---------------------------------------------------------------------------
# ENTER a rate through the dialog's Add flow -> the EUR account values against it
# ---------------------------------------------------------------------------
def test_entering_a_rate_makes_a_foreign_account_value_correctly(conn, monkeypatch):
    _two_accounts(conn)
    # No rate yet: the EUR 50 is UNCONVERTED, so it is left OUT of the total
    # rather than folded in at 1:1. Reporting 150.00 for a ledger holding USD 100
    # plus EUR 50 would overstate net worth by the whole foreign balance -- the
    # same shape of error as a double-counted transfer -- so the honest total
    # before any rate exists is the base currency alone (SRD 5.4a).
    # test_currency_networth pins the same rule from the domain side, where
    # 150_00 is named outright as "the 1:1-folded wrong answer".
    assert AccountsModel(conn).net_worth() == 100_00

    dlg = FxRatesDialog(conn)
    try:
        # Drive the real Add flow: pre-set the editor's fields and accept it,
        # exactly as a user filling the dialog would (asset_value_dialog idiom).
        def fake_exec(self):
            self.date.setDate(QDate(2026, 1, 1))
            self.base.setCurrentText("EUR")
            self.quote.setCurrentText("USD")
            self.rate.setText("1.10")
            return QDialog.Accepted

        monkeypatch.setattr(FxRateEditor, "exec_", fake_exec)
        dlg.on_add()

        # The rate landed in the store through set_rate, and the list shows it.
        assert fx.get_rate(conn, "EUR", "USD", "2026-01-01") is not None
        assert dlg.table.rowCount() == 1
        # End to end: the EUR account now values at 1.10 USD/EUR.
        # 100.00 USD + 50.00 EUR * 1.10 = 100.00 + 55.00 = 155.00
        assert AccountsModel(conn).net_worth() == 155_00
    finally:
        dlg.deleteLater()


# ---------------------------------------------------------------------------
# REFRESH rates through the dialog's fetch seam -> the EUR account values against it
# ---------------------------------------------------------------------------
def test_refreshing_rates_values_a_foreign_account(conn, monkeypatch):
    _two_accounts(conn)
    said = []
    source = _FakeFxSource({("EUR", "USD"): "1.20"})
    dlg = FxRatesDialog(conn, fx_source=source)
    monkeypatch.setattr(dlg, "_notify", lambda title, text: said.append(text))
    try:
        dlg.on_refresh()

        # The dialog asked for the EUR->USD pair its EUR account implies, and the
        # fetched rate was written through set_rate (fetch_rates funnels through it).
        assert ("EUR", "USD") in source.seen
        assert fx.get_rate(conn, "EUR", "USD") is not None
        assert dlg.table.rowCount() == 1
        # End to end: 100.00 USD + 50.00 EUR * 1.20 = 160.00
        assert AccountsModel(conn).net_worth() == 160_00
        assert said and "1.20" in said[0]
    finally:
        dlg.deleteLater()


def test_refresh_with_only_base_accounts_writes_nothing(conn, monkeypatch):
    """An all-USD ledger has no pair to refresh -- the dialog says so and calls no
    source, rather than inventing a USD->USD request."""
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    said = []
    source = _FakeFxSource({("EUR", "USD"): "1.20"})
    dlg = FxRatesDialog(conn, fx_source=source)
    monkeypatch.setattr(dlg, "_notify", lambda title, text: said.append(text))
    try:
        dlg.on_refresh()
        assert source.seen == []
        assert dlg.table.rowCount() == 0
        assert said and "No foreign-currency" in said[0]
    finally:
        dlg.deleteLater()


# ---------------------------------------------------------------------------
# the editor refuses a bad rate rather than writing a zero/nonsense one
# ---------------------------------------------------------------------------
def test_editor_refuses_bad_input_and_normalizes_good(conn, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a[2])))
    ed = FxRateEditor(conn)
    try:
        # same currency on both sides is refused (a self-rate is meaningless)
        ed.base.setCurrentText("EUR")
        ed.quote.setCurrentText("EUR")
        ed.rate.setText("1.1")
        ed._on_accept()
        assert ed.result() != QDialog.Accepted and warned
        warned.clear()

        # a non-positive rate is refused (it would zero the account out)
        ed.quote.setCurrentText("USD")
        ed.rate.setText("0")
        ed._on_accept()
        assert ed.result() != QDialog.Accepted and warned
        warned.clear()

        # a valid entry accepts and returns an upper-cased, trimmed tuple
        ed.base.setCurrentText(" eur ")
        ed.rate.setText("1.085")
        ed._on_accept()
        assert ed.result() == QDialog.Accepted
        assert not warned
        assert ed.values()[1:] == ("EUR", "USD", "1.085")
    finally:
        ed.deleteLater()
