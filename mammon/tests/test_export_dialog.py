"""File ▸ Export Ledger…: the dialog's choices reach mammon.export unchanged."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger
from mammon.ui.export_dialog import ExportDialog, perform
from mammon.tests import fresh_db


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "exp.db")
    chk = ledger.create_account(c, "Checking", "checking", opening_balance=0)
    sav = ledger.create_account(c, "Savings", "savings", opening_balance=0)
    ledger.add_transaction(c, chk, "2026-01-05", -10_00, payee="Coffee")
    ledger.add_transaction(c, sav, "2026-02-05", 50_00, payee="Interest")
    yield c
    c.close()


def test_dialog_values_follow_format_scope_and_range(qapp, conn, tmp_path):
    dlg = ExportDialog(conn, db_path=str(tmp_path / "exp.db"))
    v = dlg.values()
    assert v["fmt"] == "qif" and v["path"].endswith("exp-export.qif")
    assert v["account_ids"] is None and v["start"] is None and not v["include_sensitive"]
    # Untick Savings, limit the dates.
    dlg.accounts.item(1).setCheckState(Qt.Unchecked)
    dlg.limit_dates.setChecked(True)
    dlg.start.setDate(dlg.start.date().__class__(2026, 1, 1))
    dlg.end.setDate(dlg.end.date().__class__(2026, 1, 31))
    v = dlg.values()
    assert v["account_ids"] == [dlg.accounts.item(0).data(Qt.UserRole)]
    assert (v["start"], v["end"]) == ("2026-01-01", "2026-01-31")
    # JSON is always the whole ledger; the sensitive tick applies only there.
    dlg.format.setCurrentIndex(dlg.format.findData("json"))
    dlg.include_sensitive.setChecked(True)
    v = dlg.values()
    assert v["account_ids"] is None and v["start"] is None and v["include_sensitive"]
    assert not dlg.accounts.isEnabled()
    dlg.format.setCurrentIndex(dlg.format.findData("csv"))
    assert not dlg.include_sensitive.isEnabled() and dlg.values()["include_sensitive"] is False
    assert not dlg.by_year.isEnabled() and dlg.values()["by_year"] is False
    dlg.format.setCurrentIndex(dlg.format.findData("qif"))
    dlg.by_year.setChecked(True)
    assert dlg.by_year.isEnabled() and dlg.values()["by_year"] is True
    dlg.by_year.setChecked(False)
    # The picker is a seam; a chosen file gets the format's extension.
    dlg.format.setCurrentIndex(dlg.format.findData("qif"))
    dlg._pick_path = lambda fmt, current: str(tmp_path / "chosen")
    dlg._browse()
    assert dlg.path.text() == str(tmp_path / "chosen.qif")
    # Nothing ticked is refused through the seam.
    dlg.accounts.item(0).setCheckState(Qt.Unchecked)
    warned = []
    dlg._warn = warned.append
    dlg.accept()
    assert warned == ["Tick at least one account."] and dlg.result() != dlg.Accepted
    dlg.deleteLater()


def test_perform_runs_the_export_the_dialog_described(qapp, conn, tmp_path):
    dlg = ExportDialog(conn, db_path=str(tmp_path / "exp.db"))
    dlg.path.setText(str(tmp_path / "out.qif"))
    dlg.accept()
    assert dlg.result() == dlg.Accepted
    msg = perform(conn, dlg.values())
    assert "2 accounts" in msg and (tmp_path / "out.qif").exists()
    dlg.format.setCurrentIndex(dlg.format.findData("csv"))
    dlg.path.setText(str(tmp_path / "regs"))
    msg = perform(conn, dlg.values())
    assert "2 register files" in msg and (tmp_path / "regs" / "Savings.csv").exists()
    dlg.deleteLater()
