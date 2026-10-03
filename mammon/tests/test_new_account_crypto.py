"""The New Account dialog must offer the 'crypto' type, and creating an account
with that choice must land a row of type='crypto'.

The crypto track already groups 'crypto' under investing
(ledger.INVESTMENT_LIKE_TYPES) and routes it to CryptoRegisterWidget/Model, but
NewAccountDialog offered no such choice, so the user had no way to create one.
Account creation still funnels through the single writer (ledger.create_account)
exactly as on_new_account does -- the dialog only supplies values().
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


def test_new_account_dialog_offers_crypto_type(qapp):
    from mammon.ui.widgets import NewAccountDialog
    dlg = NewAccountDialog()
    try:
        choices = [dlg.type.itemText(i) for i in range(dlg.type.count())]
        assert "crypto" in choices
    finally:
        dlg.deleteLater()


def test_creating_crypto_account_yields_crypto_type(qapp, conn):
    """Drive the same path on_new_account uses: the dialog reports type='crypto'
    and the sole writer persists an account whose type is 'crypto' (so the track
    valuing it as investment-like via INVESTMENT_LIKE_TYPES picks it up)."""
    from mammon.ui.widgets import NewAccountDialog
    dlg = NewAccountDialog()
    try:
        dlg.name.setText("Coin Wallet")
        i = dlg.type.findText("crypto")
        assert i >= 0
        dlg.type.setCurrentIndex(i)
        v = dlg.values()
        assert v["type"] == "crypto"
        aid = ledger.create_account(conn, v["name"], v["type"],
                                    opening_balance=v["opening_balance"],
                                    opening_date=v["opening_date"])
    finally:
        dlg.deleteLater()

    acct = ledger.get_account(conn, aid)
    assert acct["type"] == "crypto"
    assert acct["type"] in ledger.INVESTMENT_LIKE_TYPES
