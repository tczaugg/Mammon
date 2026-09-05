"""The calendar's account filter grows progressively: it starts as a single
"+ account filter" slot, and each account chosen reveals exactly one more empty
"+" slot (only the next empty is ever shown, never a trailing row of them), up
to ten slots. The filtering result is otherwise identical to a fixed row."""
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
    c = db.init_db(tmp_path / "calfilter.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=500_00)
    visa = ledger.create_account(conn, "Visa", "credit", opening_balance=-200_00)
    cash = ledger.create_account(conn, "Wallet", "cash", opening_balance=40_00)
    return {"chk": chk, "sav": sav, "visa": visa, "cash": cash}


def _visible_boxes(slots) -> int:
    return sum(b.isVisibleTo(slots) for b in slots._boxes)


def test_starts_with_one_account_filter_slot(qapp, conn, world):
    slots = AccountSlots(conn)
    assert slots.account_ids() == [] and slots.describe() == ALL_SPENDING
    # Exactly one slot, labelled "+ account filter" -- not a row of empties.
    assert _visible_boxes(slots) == 1
    assert slots._labels[0].text() == AccountSlots.EMPTY_FIRST
    assert not any(c.isVisibleTo(slots) for c in slots._clears)
    slots.deleteLater()


def test_choosing_an_account_reveals_the_next_plus(qapp, conn, world):
    chk, sav = world["chk"], world["sav"]
    slots = AccountSlots(conn)
    assert _visible_boxes(slots) == 1
    # First choice: the slot fills and a fresh empty "+" appears beside it.
    slots.set_slot(0, chk)
    assert _visible_boxes(slots) == 2
    assert slots._labels[0].text() == "Checking"
    assert slots._labels[1].text() == AccountSlots.EMPTY_MORE   # just "+"
    assert slots._clears[0].isVisibleTo(slots)                  # filled has ×
    assert not slots._clears[1].isVisibleTo(slots)              # empty has none
    # Choosing in the revealed slot reveals the one after it, and so on.
    slots.set_slot(1, sav)
    assert _visible_boxes(slots) == 3
    assert slots._labels[2].text() == AccountSlots.EMPTY_MORE
    assert slots.account_ids() == [chk, sav]
    # Clearing a filled slot compacts the rest -- no gap, one fewer visible.
    slots.clear_slot(0)
    assert slots.account_ids() == [sav]
    assert _visible_boxes(slots) == 2
    assert slots._labels[0].text() == "Savings"
    assert slots._labels[1].text() == AccountSlots.EMPTY_MORE
    slots.deleteLater()


def test_caps_at_ten_slots(qapp, conn):
    ids = [ledger.create_account(conn, f"Acct{i:02d}", "checking", opening_balance=0)
           for i in range(11)]
    assert AccountSlots.MAX == 10
    slots = AccountSlots(conn)
    # Nine filled -> nine plus the one empty "+" that is the tenth slot.
    slots.set_account_ids(ids[:9])
    assert _visible_boxes(slots) == 10
    assert slots._labels[9].text() == AccountSlots.EMPTY_MORE
    # Offer eleven: only ten are kept and every slot is filled (no empty "+").
    slots.set_account_ids(ids)
    assert slots.account_ids() == ids[:10]
    assert _visible_boxes(slots) == 10
    assert all(slots._ids[i] is not None for i in range(AccountSlots.MAX))
    slots.deleteLater()


def test_filtering_result_unchanged(qapp, conn, world):
    chk, sav, visa = world["chk"], world["sav"], world["visa"]
    dlg = CalendarPanel(conn, year=2026, month=9, today=TODAY)
    assert dlg.projection.opening == 1000_00 + 500_00 - 200_00 + 40_00   # all spending
    assert dlg.summary.text().startswith(ALL_SPENDING)
    dlg.slots.set_slot(0, chk)
    dlg.slots.set_slot(1, visa)
    assert dlg.projection.opening == 1000_00 - 200_00
    assert dlg.summary.text().startswith("Checking, Visa:")
    dlg.deleteLater()
    # A new calendar opens on the same remembered slots.
    again = CalendarPanel(conn, year=2026, month=9, today=TODAY)
    assert again.slots.account_ids() == [chk, visa]
    assert again.projection.opening == 1000_00 - 200_00
    # An explicit account overrides them for that window without persisting.
    explicit = CalendarPanel(conn, year=2026, month=9, today=TODAY, account_id=sav)
    assert explicit.slots.account_ids() == [sav] and explicit.projection.opening == 500_00
    assert prefs.projection_slots() == [chk, visa]
    again.deleteLater()
    explicit.deleteLater()
