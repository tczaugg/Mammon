"""Register edit-state fixes (issues 1, 3, 4).

  1. Delete/Backspace on a TAB-selected cell clears it (parity with click).
  3. The Split button on a NEW review row materialises it, then opens the split.
  4. Enter/commit preserves the edited row's selection instead of jumping to top.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, import_review, ledger
from mammon.ui import widgets
from mammon.ui.models import RegisterModel

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QDialog


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "reg.db")
    yield c
    c.close()


def _dl_row(desc, *, tid="X1", amount="40.00", date="2026-06-01"):
    return {"transactionId": tid, "postedDate": date, "amount": amount,
            "isDebit": True, "statementDescription": desc}


# ---------------------------------------------------------------------------
# issue 1: Delete/Backspace clears a tab-selected cell
# ---------------------------------------------------------------------------
def test_delete_clears_tab_selected_cell(qapp, conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    t = ledger.add_transaction(conn, chk, "2024-01-02", -12_00, payee="Whole Foods",
                               memo="weekly")
    reg = widgets.RegisterWidget(conn, chk)
    row = reg.model.row_for_txn(t)
    reg.view.setCurrentIndex(reg.model.index(row, RegisterModel.PAYEE))

    ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier)
    handled = reg.eventFilter(reg.view, ev)
    assert handled is True                                   # key consumed
    assert ledger.register_rows(conn, chk)[0]["payee"] in (None, "")   # cleared


def test_delete_ignores_blank_and_readonly(qapp, conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    ledger.add_transaction(conn, chk, "2024-01-02", -12_00, payee="P")
    reg = widgets.RegisterWidget(conn, chk)
    # Balance is display-only -> Delete must not act.
    reg.view.setCurrentIndex(reg.model.index(0, RegisterModel.BALANCE))
    ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier)
    assert reg.eventFilter(reg.view, ev) is False
    # The blank quick-entry row is never cleared either.
    blank = reg.model.rowCount() - 1
    reg.view.setCurrentIndex(reg.model.index(blank, RegisterModel.PAYEE))
    assert reg.eventFilter(reg.view, ev) is False


# ---------------------------------------------------------------------------
# issue 4: Enter/commit preserves the edited row's selection (no jump to top)
# ---------------------------------------------------------------------------
def test_commit_preserves_selection(qapp, conn):
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    ids = [ledger.add_transaction(conn, chk, "2024-01-%02d" % d, -d * 100,
                                  payee="P%d" % d) for d in range(1, 8)]
    win = MainWindow(conn)
    reg = win.open_register(chk)
    target = ids[-1]                          # a row well below the top
    row = reg.model.row_for_txn(target)
    assert row > 0
    reg.view.setCurrentIndex(reg.model.index(row, RegisterModel.PAYEE))

    # Simulate the inline payee edit's commit (what Enter does).
    reg.model.setData(reg.model.index(row, RegisterModel.PAYEE),
                      "Corrected Payee", Qt.EditRole)

    assert reg.model.row_for_txn(target) == row               # row order unchanged
    assert reg.view.currentIndex().row() == row               # NOT snapped to 0
    assert ledger.register_rows(conn, chk)[row]["payee"] == "Corrected Payee"


def test_refresh_all_excludes_committing_register(qapp, conn):
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    ledger.add_transaction(conn, chk, "2024-01-01", -100, payee="P")
    win = MainWindow(conn)
    reg = win.open_register(chk)
    calls = []
    reg.model.reload = lambda: calls.append("reload")   # spy on THIS model's reload
    win._refresh_all(exclude_account_id=chk)
    assert calls == []                                   # committing reg not reloaded
    win._refresh_all()                                   # default reloads everyone
    assert calls == ["reload"]


# ---------------------------------------------------------------------------
# issue 3: Split on a NEW review row materialises it, then opens the dialog
# ---------------------------------------------------------------------------
def test_split_on_pending_review_row_materialises_and_opens(qapp, conn, monkeypatch):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    reg = widgets.RegisterWidget(conn, chk)
    entries = import_review.build_review(conn, chk, [_dl_row("ACME HARDWARE 55")])
    assert entries and entries[0].is_new
    reg.review_panel.set_entries(entries)     # the panel must hold the entry to accept
    reg.review_panel.show()
    reg._show_pending(entries[0])             # open the editable pending register row
    prow = reg.model.pending_row()
    assert reg.model.is_pending_row(prow)

    opened = {}

    class _FakeSplit:
        def __init__(self, model, row, parent=None):
            opened["row"] = row
            opened["txn"] = model.txn_at(row)
        def exec_(self):
            return QDialog.Rejected

    monkeypatch.setattr(widgets, "SplitDialog", _FakeSplit)
    before = len(ledger.register_rows(conn, chk))
    reg.on_split()                                        # the toolbar Split button

    # The pending row was accepted into a REAL transaction and the split opened
    # on it (previously the button silently did nothing).
    assert opened.get("txn") is not None
    assert len(ledger.register_rows(conn, chk)) == before + 1


def test_split_ignores_blank_row(qapp, conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    reg = widgets.RegisterWidget(conn, chk)
    blank = reg.model.rowCount() - 1
    reg._split_row(blank)                                 # no crash, no dialog
