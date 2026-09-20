"""Tests for the price-history editor (mammon/ui/price_history_dialog.py).

Two defects are pinned here, because both made a price series UNREACHABLE rather
than merely awkward:

* a coin's prices are filed as ``{SYM}-USD``, so every lookup by the holding's
  own symbol found nothing and the app reported "no recorded price history" for
  a coin whose prices were sitting in the table;
* there was nowhere in the app to add or remove a price at all, so the only
  answer to a wrong or missing close was to leave it.

Every modal is an overridable seam, so nothing here opens a window that would
block forever under the offscreen platform.

All data is synthetic.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import crypto, db, investments, ledger
from mammon.ui.price_history_dialog import (
    MANUAL_SOURCE, PriceHistoryDialog, price_symbol_for,
)
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "prices.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    acct = ledger.create_account(conn, "Coinbase", "crypto")
    crypto.record_buy(conn, acct, "2025-02-01", "ETH", "10", 20_000_00)
    crypto.rebuild_holdings(conn, acct)
    investments.record_price(conn, crypto.pair_symbol("ETH"), "2025-06-01",
                             "3000", "yfinance")
    return acct


class _Dialog(PriceHistoryDialog):
    """The editor with its modals answered from the test."""

    confirm_answer = True
    next_price = None

    def __init__(self, *a, **kw):
        self.confirmed = []
        super().__init__(*a, **kw)

    def _confirm(self, title, text):
        self.confirmed.append((title, text))
        return self.confirm_answer

    def _warn(self, title, text):
        pass

    def _ask_price(self, row=None):
        return self.next_price


# --------------------------------------------------------------------------
# Finding the series at all
# --------------------------------------------------------------------------

def test_a_coin_resolves_to_its_pair_symbol(conn, wallet):
    """The defect: prices are filed as ETH-USD, so asking for ETH found none."""
    assert price_symbol_for(conn, "ETH") == "ETH-USD"


def test_a_security_keeps_its_own_ticker(conn):
    investments.record_price(conn, "VTI", "2025-06-01", "250", "yfinance")
    assert price_symbol_for(conn, "VTI") == "VTI"


def test_an_unpriced_coin_still_resolves_to_the_pair(conn, wallet):
    """A coin nobody has priced yet must still route to where a price WOULD go,
    or the editor would file the backfill under a symbol nothing reads."""
    investments.delete_price(conn, "ETH-USD", "2025-06-01")
    assert price_symbol_for(conn, "ETH") == "ETH-USD"


def test_an_unknown_symbol_is_left_alone(conn):
    assert price_symbol_for(conn, "NOSUCH") == "NOSUCH"
    assert price_symbol_for(conn, "") == ""


# --------------------------------------------------------------------------
# The editor
# --------------------------------------------------------------------------

def test_the_dialog_opens_on_the_coins_series(qapp, conn, wallet):
    dlg = _Dialog(conn, "ETH")
    assert dlg.symbol == "ETH-USD"
    assert [r[0] for r in dlg.table_rows()] == ["06/01/2025"]
    assert "1 price" in dlg.summary.text()
    dlg.close()


def test_adding_a_price_records_it_as_manual(qapp, conn, wallet):
    """Source matters beyond bookkeeping: the split adjustment treats a
    provider's closes as already back-adjusted and a manual one as as-traded."""
    dlg = _Dialog(conn, "ETH")
    dlg.next_price = ("2025-12-31", "3210.55")
    assert dlg.on_add()
    rows = investments.stored_prices(conn, "ETH-USD")
    added = [r for r in rows if r["date"] == "2025-12-31"][0]
    assert str(added["close_price"]) == "3210.55"
    assert added["source"] == MANUAL_SOURCE
    dlg.close()


def test_deleting_a_price_removes_it_and_is_confirmed(qapp, conn, wallet):
    dlg = _Dialog(conn, "ETH")
    dlg.table.selectRow(0)
    assert dlg.on_delete()
    assert investments.stored_prices(conn, "ETH-USD") == []
    assert dlg.confirmed and "06/01/2025" in dlg.confirmed[-1][1]
    assert dlg.table_rows() == []
    dlg.close()


def test_a_refused_delete_keeps_the_price(qapp, conn, wallet):
    dlg = _Dialog(conn, "ETH")
    dlg.confirm_answer = False
    dlg.table.selectRow(0)
    assert not dlg.on_delete()
    assert len(investments.stored_prices(conn, "ETH-USD")) == 1
    dlg.close()


def test_moving_a_price_to_another_date_leaves_no_ghost(qapp, conn, wallet):
    """The date is the series key, so an edit that moves one is an add plus a
    delete -- the old row would otherwise survive at the old date."""
    dlg = _Dialog(conn, "ETH")
    dlg.table.selectRow(0)
    dlg.next_price = ("2025-07-01", "3100")
    assert dlg.on_edit()
    dates = [r["date"] for r in investments.stored_prices(conn, "ETH-USD")]
    assert dates == ["2025-07-01"]
    dlg.close()


def test_replacing_an_existing_date_is_confirmed(qapp, conn, wallet):
    investments.record_price(conn, "ETH-USD", "2025-08-01", "2900", "yfinance")
    dlg = _Dialog(conn, "ETH")
    dlg.next_price = ("2025-08-01", "2950")
    assert dlg.on_add()
    assert dlg.confirmed and "Replace" in dlg.confirmed[-1][0]
    rows = {r["date"]: r for r in investments.stored_prices(conn, "ETH-USD")}
    assert str(rows["2025-08-01"]["close_price"]) == "2950"

    dlg.confirm_answer = False
    dlg.next_price = ("2025-08-01", "1")
    assert not dlg.on_add()
    rows = {r["date"]: r for r in investments.stored_prices(conn, "ETH-USD")}
    assert str(rows["2025-08-01"]["close_price"]) == "2950"   # untouched
    dlg.close()


def test_the_editor_shows_stored_prices_not_split_adjusted_ones(qapp, conn):
    """A round trip through this window must not restate the history: the chart
    divides by later splits, the editor must not."""
    acct = ledger.create_account(conn, "Brokerage", "investment")
    investments.record_investment(conn, acct, "2020-01-02", "Buy",
                                  symbol="AAPL", quantity="10", price="300",
                                  amount=-3_000_00)
    investments.record_price(conn, "AAPL", "2020-01-02", "300", "manual")
    investments.record_investment(conn, acct, "2020-08-31", "StkSplit",
                                  symbol="AAPL", quantity="40")   # a 4-for-1
    charted = dict(investments.price_history(conn, "AAPL"))
    dlg = _Dialog(conn, "AAPL")
    stored = {r[0]: r[1] for r in dlg.table_rows()}
    assert str(charted["2020-01-02"]) == "75"        # adjusted for the 4:1
    assert stored["01/02/2020"] == "300"             # as traded
    dlg.close()


def test_an_empty_series_says_what_that_means(qapp, conn):
    dlg = _Dialog(conn, "NOSUCH")
    assert dlg.table_rows() == []
    assert "unpriced" in dlg.summary.text()
    assert not dlg.edit_btn.isEnabled() and not dlg.delete_btn.isEnabled()
    dlg.close()


# --------------------------------------------------------------------------
# Reaching the series from a holding and from the register
# --------------------------------------------------------------------------
#
# These drive the GESTURE, not the helper. The helper was tested and passed while
# the menu that calls it raised ``'HoldingValue' object is not subscriptable`` on
# the first right-click: ``crypto.holding_values`` yields dataclasses and the row
# lookup indexed them like the dicts ``list_holdings`` returns.

@pytest.fixture
def wallet_window(qapp, conn, wallet):
    from mammon.ui.widgets import CryptoHoldingsDialog
    dlg = CryptoHoldingsDialog(conn, wallet)
    yield dlg
    dlg.close()


def test_a_holdings_row_reports_its_coin(wallet_window):
    assert wallet_window.symbol_at(0) == "ETH"


def test_the_cash_row_names_no_coin(wallet_window):
    """An exchange account's last row is CASH, which has no price."""
    last = wallet_window.table.rowCount() - 1
    if last > 0:                       # a wallet has no cash row to test
        assert wallet_window.symbol_at(last) == ""
    assert wallet_window.symbol_at(99) == ""
    assert wallet_window.symbol_at(-1) == ""


def test_right_clicking_a_holding_offers_both_price_actions(wallet_window,
                                                            monkeypatch):
    """The crash was here: the menu is built from the row lookup."""
    import mammon.ui.widgets as widgets

    seen = {}

    class _Action:
        def __init__(self, text):
            self.text = text

        def setEnabled(self, *_a):
            pass

    class _Menu:
        def __init__(self, *_a, **_k):
            self.actions = []

        def addAction(self, text):
            action = _Action(text)
            self.actions.append(action)
            return action

        def addSeparator(self):
            pass

        def exec_(self, *_a, **_k):
            seen["menu"] = [a.text for a in self.actions]
            return None

    monkeypatch.setattr(widgets, "QMenu", _Menu)
    table = wallet_window.table
    rect = table.visualItemRect(table.item(0, 0))
    wallet_window._row_menu(rect.center())      # the gesture, not the helper
    assert seen["menu"] == ["Price history: ETH…", "Edit price history: ETH…"]


def test_the_crypto_register_offers_the_coins_prices(qapp, conn, wallet,
                                                     monkeypatch):
    """The register NAMES the coin on every trade row, so it is a place a user
    reasonably looks -- and it offered nothing at all."""
    import mammon.ui.widgets as widgets

    seen = {}

    class _Action:
        def __init__(self, text):
            self.text = text

        def setEnabled(self, *_a):
            pass

    class _Menu:
        def __init__(self, *_a, **_k):
            self.actions = []

        def addAction(self, text):
            action = _Action(text)
            self.actions.append(action)
            return action

        def addSeparator(self):
            pass

        def exec_(self, *_a, **_k):
            seen["menu"] = [a.text for a in self.actions]
            return None

    reg = widgets.CryptoRegisterWidget(conn, wallet)
    row = next(i for i in range(reg.model.rowCount()) if reg.symbol_at(i))
    monkeypatch.setattr(widgets, "QMenu", _Menu)
    index = reg.model.index(row, 0)
    reg._context_menu(reg.view.visualRect(index).center())
    assert "Price history: ETH…" in seen["menu"]
    assert "Edit price history: ETH…" in seen["menu"]
    reg.close()


def test_a_register_row_naming_no_coin_offers_no_price_actions(qapp, conn,
                                                               wallet):
    from mammon.ui.widgets import CryptoRegisterWidget
    reg = CryptoRegisterWidget(conn, wallet)
    blank = reg.model.rowCount() - 1            # the quick-entry row
    assert reg.symbol_at(blank) == ""
    assert reg.symbol_at(-1) == ""
    reg.close()
