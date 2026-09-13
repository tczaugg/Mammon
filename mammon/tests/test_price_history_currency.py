"""Price-history charts state the currency of the ACCOUNT the holding lives in.

A security carries no currency of its own -- the account does (`fx.get_account_currency`),
so a fund held in a CAD brokerage has CAD prices. The chart used to stamp a bare
'$' on every price, so a CAD series read as USD. These tests pin the fix at the
real entry points: the account id is threaded from the (single-account) window
into `widgets._chart_price_history`, which labels the y axis and the title with
the currency and drops the dollar sign for anything but USD.

Headless: QT_QPA_PLATFORM=offscreen, and the modal is never exec_()-ed -- the
tests patch `charts.ChartDialog.exec_`, the same seam
test_ui.test_holdings_dialog_double_click_charts_price_history uses.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import crypto, db, fx, investments, ledger


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "pricecur.db")
    yield c
    c.close()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _seed_fund_account(conn, currency="USD", name="ANON Brokerage"):
    """An investment account holding one synthetic fund with three prices."""
    acct = ledger.create_account(conn, name, "investment", opening_balance=0,
                                 currency=currency)
    investments.record_investment(conn, acct, "2020-01-05", "Buy",
                                  symbol="ANONFUND", quantity="10",
                                  price="100", amount=-1_000_00)
    investments.record_price(conn, "ANONFUND", "2020-01-05", "100")
    investments.record_price(conn, "ANONFUND", "2020-02-05", "110")
    investments.record_price(conn, "ANONFUND", "2020-03-05", "105.50")
    return acct


def _capture_charts(monkeypatch):
    """Patch the modal away and collect the dialogs that WOULD have been shown."""
    from mammon.ui import charts
    shown = []
    # Record the title WHILE the dialog is alive: nothing holds a reference once
    # the (patched) exec_ returns, so a stashed dialog is a deleted C++ object
    # by the time the assertions run. The canvas keeps its own Figure alive.
    monkeypatch.setattr(
        charts.ChartDialog, "exec_",
        lambda self: shown.append((self.windowTitle(), self.canvas)))
    return shown


def _tick_texts(canvas):
    """The y-axis tick strings as the user would read them."""
    ax = canvas.figure.axes[0]
    fmt = ax.yaxis.get_major_formatter()
    return [fmt(v, i) for i, v in enumerate(ax.get_yticks())]


# --------------------------------------------------------------------------
# (1) full life-cycle: a CAD account's chart says CAD and shows no '$'
# --------------------------------------------------------------------------
def test_cad_account_price_history_is_labelled_cad(qapp, conn, monkeypatch):
    from mammon.ui.widgets import HoldingsDialog

    acct = _seed_fund_account(conn, currency="CAD", name="ANON Canada")
    shown = _capture_charts(monkeypatch)

    HoldingsDialog(conn, acct).show_price_history("ANONFUND")

    assert len(shown) == 1
    title, canvas = shown[0]
    ax = canvas.figure.axes[0]
    assert "CAD" in ax.get_ylabel()                 # the axis itself says CAD
    assert "CAD" in ax.get_title()                  # and so does the plot title
    assert "CAD" in title                           # and the frame around it
    ticks = _tick_texts(canvas)
    assert ticks                                    # a real, formatted axis
    assert not any("$" in t for t in ticks)         # never a bare dollar sign
    assert all("CAD" in t for t in ticks)


# --------------------------------------------------------------------------
# (2) a USD account renders exactly as before
# --------------------------------------------------------------------------
def test_usd_account_price_history_keeps_dollar_formatting(qapp, conn,
                                                           monkeypatch):
    from mammon.ui.widgets import HoldingsDialog

    acct = _seed_fund_account(conn, currency="USD")
    shown = _capture_charts(monkeypatch)

    HoldingsDialog(conn, acct).show_price_history("ANONFUND")

    ax = shown[0][1].figure.axes[0]
    assert ax.get_ylabel() == "Price (USD)"
    assert "ANONFUND" in ax.get_title() and "USD" in ax.get_title()
    ticks = _tick_texts(shown[0][1])
    assert ticks and all(t.startswith("$") for t in ticks)


# --------------------------------------------------------------------------
# (3) the crypto holdings window is a single-account window too
# --------------------------------------------------------------------------
def test_crypto_account_price_history_uses_its_own_currency(qapp, conn,
                                                            monkeypatch):
    # The crypto holdings window has no price-history entry point of its own
    # today, but it is scoped to ONE account exactly like HoldingsDialog, so it
    # charts through the same shared helper -- pinned here so a coin chart can
    # never be drawn without the wallet's currency on it.
    from mammon.ui import widgets
    from mammon.ui.widgets import CryptoHoldingsDialog

    wallet = crypto.create_account(conn, "ANON Wallet CA")
    fx.set_account_currency(conn, wallet, "CAD")
    crypto.record_buy(conn, wallet, "2024-01-05", "ANONCOIN", "2", 4_000_00)
    crypto.rebuild_holdings(conn, wallet)
    investments.record_price(conn, "ANONCOIN", "2024-02-01", "2100")
    investments.record_price(conn, "ANONCOIN", "2024-03-01", "2500")

    shown = _capture_charts(monkeypatch)
    dlg = CryptoHoldingsDialog(conn, wallet)
    widgets._chart_price_history(dlg, conn, "ANONCOIN", dlg.account_id)

    ax = shown[0][1].figure.axes[0]
    assert "CAD" in ax.get_ylabel() and "CAD" in ax.get_title()
    assert not any("$" in t for t in _tick_texts(shown[0][1]))


# --------------------------------------------------------------------------
# (4) backward compatibility: currency is optional on the canvas
# --------------------------------------------------------------------------
def test_canvas_without_currency_still_formats_as_usd(qapp):
    from decimal import Decimal
    from mammon.ui.charts import PriceHistoryCanvas

    points = [("2020-01-31", Decimal("100.00")),
              ("2020-02-28", Decimal("110.00")),
              ("2020-03-31", Decimal("105.50"))]
    canvas = PriceHistoryCanvas("ANONFUND", points)      # positional, 2 args
    assert canvas.currency == "USD"
    ax = canvas.figure.axes[0]
    assert ax.get_ylabel() == "Price (USD)"
    ticks = _tick_texts(canvas)
    assert ticks and all(t.startswith("$") for t in ticks)

    # An empty series is still a placeholder, not a crash.
    assert PriceHistoryCanvas("ANONFUND", []).figure.axes


# --------------------------------------------------------------------------
# (5) an account with no currency recorded falls back to USD
# --------------------------------------------------------------------------
def test_account_with_no_currency_falls_back_to_usd(qapp, conn, monkeypatch):
    from mammon.ui.widgets import HoldingsDialog

    acct = _seed_fund_account(conn, name="ANON Legacy")
    # No currency recorded. The column is NOT NULL, so "unset" is the blank
    # string; fx.get_account_currency normalizes blank/missing to the base.
    conn.execute("UPDATE accounts SET currency='' WHERE id=?", (acct,))
    conn.commit()
    assert fx.get_account_currency(conn, acct) == "USD"

    shown = _capture_charts(monkeypatch)
    HoldingsDialog(conn, acct).show_price_history("ANONFUND")

    ax = shown[0][1].figure.axes[0]
    assert ax.get_ylabel() == "Price (USD)"
    assert all(t.startswith("$") for t in _tick_texts(shown[0][1]))
