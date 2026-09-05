"""The import-review UI offers to save a profile the first time it sees a new
tabular (CSV) format, and stays silent for a format it already knows -- so a
downloaded statement imports with no prompt on the second pull. Runs headlessly
(offscreen Qt), driving the choke point ``_ingest_file_via_review`` directly.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "qs"))


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "q.db")
    yield c
    c.close()


VENMO_CSV = (
    "Account Statement - (@SampleUser) ,,,,,,,,\n"
    "Account Activity,,,,,,,,\n"
    ",ID,Datetime,Type,Status,Note,From,To,Amount (total)\n"
    ",111,2026-06-04T15:21:49,Payment,Complete,Rent,Jamie Chen,Sam Rivera,\"+ $2,250.00\"\n"
    ",222,2026-06-20T20:19:38,Payment,Complete,Gift,Sam Rivera,Jamie Park,- $30.00\n"
    ",,,,,,,,\n"
)


def test_new_csv_format_offers_profile_then_silent(qapp, conn, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    p = tmp_path / "VenmoStatement_June_2026.csv"
    p.write_text(VENMO_CSV, encoding="utf-8")
    aid = ledger.create_account(conn, "Venmo", "checking")
    acct = {"id": aid, "name": "Venmo", "type": "checking"}

    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    asked = {"n": 0}

    def _question(*a, **k):
        asked["n"] += 1
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_question))

    win = MainWindow(conn)
    try:
        # First import of this format: the user is asked, says yes -> profile saved,
        # and nothing was written to the register (review only).
        win._ingest_file_via_review(aid, acct, str(p))
        assert asked["n"] == 1
        assert conn.execute("SELECT COUNT(*) FROM import_profiles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

        # Second import of the SAME format: known now, so no prompt.
        win._ingest_file_via_review(aid, acct, str(p))
        assert asked["n"] == 1
    finally:
        win.close()
