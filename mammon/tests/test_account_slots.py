"""The calendar's account slots: up to ten accounts the month is summed over,
chosen per slot, cleared with the ×, remembered between sessions. (The
progressive reveal of empty slots lives in test_calendar_account_filter.py.)"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger
from mammon.ui import prefs
from mammon.ui.projection_dialogs import ALL_SPENDING, AccountSlots, CalendarPanel

TODAY = "2026-09-02"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    prefs.set_projection_slots([])
    yield
    prefs.set_projection_slots([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "slots.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=500_00)
    visa = ledger.create_account(conn, "Visa", "credit", opening_balance=-200_00)
    cash = ledger.create_account(conn, "Wallet", "cash", opening_balance=40_00)
    ledger.create_account(conn, "House", "asset", opening_balance=900000_00)
    ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    return {"chk": chk, "sav": sav, "visa": visa, "cash": cash}


def test_slots_choose_clear_and_persist(qapp, conn, world):
    chk, sav, visa = world["chk"], world["sav"], world["visa"]
    slots = AccountSlots(conn)
    assert slots.account_ids() == [] and slots.describe() == ALL_SPENDING
    assert slots._labels[0].text() == AccountSlots.EMPTY_FIRST
    assert not any(c.isVisibleTo(slots) for c in slots._clears)
    fired = []
    slots.changed.connect(lambda: fired.append(True))
    slots.set_slot(0, chk)
    slots.set_slot(1, sav)
    assert slots.account_ids() == [chk, sav] and slots.describe() == "Checking, Savings"
    assert slots._labels[0].text() == "Checking" and slots._labels[1].text() == "Savings"
    assert slots._clears[0].isVisibleTo(slots) and slots._clears[1].isVisibleTo(slots)
    assert prefs.projection_slots() == [chk, sav]                # remembered
    slots.set_slot(0, sav)                                       # one account, one slot
    assert slots.account_ids() == [sav]                          # sav left its old slot
    assert slots._labels[0].text() == "Savings"
    slots.clear_slot(0)
    assert slots.account_ids() == [] and prefs.projection_slots() == []
    assert len(fired) == 4
    # Only spending accounts, at most ten, stale ids dropped, order kept.
    house = conn.execute("SELECT id FROM accounts WHERE name='House'").fetchone()["id"]
    slots.set_account_ids([house, 999, visa, chk, sav, world["cash"], chk])
    assert slots.account_ids() == [visa, chk, sav, world["cash"]]
    slots.deleteLater()


def test_calendar_sums_the_filled_slots_and_remembers_them(qapp, conn, world):
    chk, sav, visa, cash = world["chk"], world["sav"], world["visa"], world["cash"]
    dlg = CalendarPanel(conn, year=2026, month=9, today=TODAY)
    assert dlg.projection.opening == 1000_00 + 500_00 - 200_00 + 40_00   # all spending
    assert dlg.summary.text().startswith(ALL_SPENDING)
    dlg.slots.set_slot(0, chk)
    dlg.slots.set_slot(1, visa)
    assert dlg.projection.opening == 1000_00 - 200_00
    assert dlg.summary.text().startswith("Checking, Visa:")
    dlg.slots.clear_slot(0)
    assert dlg.projection.opening == -200_00
    dlg.deleteLater()
    # A new calendar opens on the same slots; an explicit account overrides
    # them for that window without changing the memory.
    again = CalendarPanel(conn, year=2026, month=9, today=TODAY)
    assert again.slots.account_ids() == [visa] and again.projection.opening == -200_00
    again.deleteLater()
    explicit = CalendarPanel(conn, year=2026, month=9, today=TODAY, account_id=sav)
    assert explicit.slots.account_ids() == [sav] and explicit.projection.opening == 500_00
    assert prefs.projection_slots() == [visa]
    explicit.deleteLater()
