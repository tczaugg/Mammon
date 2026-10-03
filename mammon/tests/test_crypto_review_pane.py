"""Regression: a crypto account's imported/downloaded rows must surface in the
register's import-review pane, and the ``Review...`` action must enable -- exactly
as a cash account does.

The bug: :class:`CryptoRegisterWidget` was a separate ``QWidget`` (not a subclass
of the cash :class:`RegisterWidget`) that never mounted an ``ImportReviewPanel``
and had no ``_sync_review_action``. So ``review_items`` written for a
``type='crypto'`` account lit the sidebar's red dot -- ``import_review.count_pending``
has no account-type filter -- but there was NO widget to show the rows, and the
``Review...`` toolbar/gear action, which is only ever re-enabled from
``_sync_review_action``, stayed permanently disabled from construction. Every
MainWindow import/download path guards on ``hasattr(reg, "show_review")`` /
``reopen_review``, so the crypto register silently fell through to nothing.

The crypto register now carries the same review surface the cash and investment
registers do. Because its grid is READ-ONLY (no in-place editable pending row), a
NEW row is accepted with the importer's mapped values as-is, posting through the
single ``import_review`` chokepoint into the account's cash sleeve (the fiat that
lives in ``transactions`` and that ``crypto.account_valuation`` folds in).

Offscreen Qt; synthetic ANON data only (no real wallet address, hash or amount).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import crypto, db, import_review, ledger
from mammon.ui.widgets import CryptoRegisterWidget, MainWindow
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    # review_visibility() reads QSettings; keep it out of the real profile.
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "crypto_review.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    # A crypto wallet with a funded cash sleeve (the fiat sits in `transactions`).
    return crypto.create_account(conn, "Hot Wallet", opening_balance=1_000_000_00)


# Synthetic Etherscan-shaped rows, expressed as the download-row dicts
# import_review.build_review consumes. Every address is an obvious ANON
# placeholder; no real wallet address, hash or amount appears here.
_ROWS = [
    {"transactionId": "0xabc001", "postedDate": "2024-01-05", "amount": "120.00",
     "isDebit": False, "statementDescription": "RECEIVE 0x1111 -> 0x2222"},
    {"transactionId": "0xabc002", "postedDate": "2024-01-06", "amount": "45.00",
     "isDebit": True, "statementDescription": "SEND 0x2222 -> 0x3333"},
    {"transactionId": "0xabc003", "postedDate": "2024-01-07", "amount": "5.00",
     "isDebit": True, "statementDescription": "GAS 0x2222"},
]


def _seed_review(conn, account_id, rows):
    """Build + persist review rows for ``account_id`` (what a crypto import does)
    and return the reloaded pending entries."""
    entries = import_review.build_review(conn, account_id, rows)
    import_review.persist_entries(conn, account_id, entries)
    return import_review.load_pending(conn, account_id)


def test_crypto_import_shows_review_pane_and_enables_action(qapp, conn, wallet):
    """After an import produces review_items for an open crypto register,
    show_review reveals the populated pane and the Review... action enables -- the
    exact pair that silently did nothing before."""
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        # Before any import: the panel loaded no persisted rows, so the pane is
        # hidden and the Review... action is disabled.
        assert reg.review_panel.isHidden()
        assert not reg.toolbar.act_review.isEnabled()
        # An import produces review_items and calls show_review with them (the
        # MainWindow import/download paths do exactly this).
        entries = _seed_review(conn, wallet, _ROWS)
        reg.show_review(entries)
        assert not reg.review_panel.isHidden()
        assert reg.review_panel.pending_count() == len(_ROWS)
        assert reg.review_panel.has_pending()
        assert reg.toolbar.act_review.isEnabled()
    finally:
        reg.deleteLater()


def test_crypto_review_reopens_from_menu(qapp, conn, wallet):
    """The persisted review survives reopening the register: with review_items
    already on disk, constructing the crypto register loads them (ImportReviewPanel
    seeds from persisted pending rows), so the Review... action is ENABLED --
    where the bug left it grayed -- and MainWindow's Review... action (a no-op for
    crypto before the fix, because CryptoRegisterWidget had no reopen_review) now
    re-shows the pane. This is the exact path the user's Review menu item drives."""
    _seed_review(conn, wallet, _ROWS)
    win = MainWindow(conn)
    try:
        reg = win.open_register(wallet)
        assert isinstance(reg, CryptoRegisterWidget)
        # Persisted pending review_items were loaded on construction: the action
        # is already enabled (the bug's core symptom, now fixed), while the pane
        # stays hidden until the user invokes Review...
        assert reg.toolbar.act_review.isEnabled()
        assert reg.review_panel.isHidden()
        # Menu / gear "Review..." -> MainWindow._reopen_review reveals the pane.
        win._reopen_review(wallet)
        assert not reg.review_panel.isHidden()
        assert reg.review_panel.pending_count() == len(_ROWS)
        assert reg.toolbar.act_review.isEnabled()
    finally:
        win.close()


def test_crypto_accept_new_posts_to_cash_sleeve(qapp, conn, wallet):
    """Accepting a NEW crypto review row commits through the single import_review
    chokepoint into the account's cash sleeve (``transactions``), moving the
    balance, and leaves the remaining rows pending. The read-only crypto grid has
    no editable pending line, so the row posts with the importer's mapped
    values."""
    start = ledger.account_balance(conn, wallet)
    entries = _seed_review(conn, wallet, _ROWS)
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        reg.show_review(entries)
        new_entry = next(e for e in reg.review_panel._entries if e.is_new)
        delta = new_entry.mapped.amount_cents
        txn_id = reg.review_panel.accept_new(new_entry, {})
        # Posted through ledger into this account's cash sleeve.
        assert ledger.account_balance(conn, wallet) == start + delta
        assert ledger.get_transaction(conn, txn_id)["account_id"] == wallet
        # One row consumed; the rest still await action, so the action stays on.
        assert reg.review_panel.pending_count() == len(_ROWS) - 1
        assert reg.toolbar.act_review.isEnabled()
    finally:
        reg.deleteLater()


def test_crypto_register_review_action_off_without_review(qapp, conn, wallet):
    """No imports -> nothing to review -> the Review... action is disabled and the
    pane stays hidden (the enable is genuinely gated, not always-on)."""
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        assert reg.review_panel.isHidden()
        assert not reg.review_panel.has_pending()
        assert not reg.toolbar.act_review.isEnabled()
    finally:
        reg.deleteLater()
