"""Projected Balances, the Financial Calendar, the reminder-aware Scheduled
Payments manager, and pre-entry at launch (roadmap item 5)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger, loans, scheduled
from mammon.ui import prefs
from mammon.ui.projection_dialogs import CalendarPanel, ProjectedBalancesDialog
from mammon.ui.scheduled_payments_dialog import (ScheduledPaymentEditor,
                                                 ScheduledPaymentsDialog)

TODAY = "2026-01-08"


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    prefs.set_auto_enter_on_launch(True)
    yield
    prefs.set_auto_enter_on_launch(True)


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "dialogs.db")
    yield c
    c.close()


def _luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_today_highlight_is_near_white_in_light_mode(monkeypatch):
    """Reported defect: the calendar's 'today' square was too dark in LIGHT
    mode. Its light highlight must now be only a shade darker than white (a
    gentle tint), clearly lighter than the earlier #e4efe8 green -- while the
    dark-mode highlight is left unchanged."""
    from mammon.ui import projection_dialogs as pd
    from mammon.ui import style

    monkeypatch.setattr(style, "theme", lambda: "light")
    light = pd.today_background()
    # Near-white: high luminance, and lighter than the too-dark old green.
    assert _luminance(light) >= 0.93
    assert _luminance(light) > _luminance("#e4efe8")

    monkeypatch.setattr(style, "theme", lambda: "dark")
    # Dark highlight unchanged (still the deeper fill visible on the dark grid).
    assert pd.today_background() == pd._TODAY_BG[1]


@pytest.fixture
def seeded(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ledger.add_transaction(conn, chk, "2026-01-05", -100_00, payee="Grocer")
    ledger.add_transaction(conn, chk, "2026-01-12", -40_00, payee="Cafe")
    power = scheduled.add_scheduled(conn, chk, payee="Power Co", amount=-60_00,
                                    frequency="monthly", next_date="2026-01-10")
    scheduled.ensure_due_pre_entries(conn, power, TODAY)            # placeholder Jan 10
    pay = scheduled.add_scheduled(conn, chk, payee="Employer", amount=2000_00,
                                  frequency="semimonthly", next_date="2026-01-15")
    sweep = scheduled.add_scheduled(conn, chk, payee="Auto-save", amount=-500_00,
                                    frequency="monthly", next_date="2026-01-20",
                                    transfer_account_id=sav)
    late = scheduled.add_scheduled(conn, chk, payee="Late Co", amount=-10_00,
                                   frequency="monthly", next_date="2026-01-02",
                                   auto_enter=False)
    return {"chk": chk, "sav": sav, "inv": inv, "power": power, "pay": pay,
            "sweep": sweep, "late": late}


# ---------------------------------------------------------------------------
# Projected Balances
# ---------------------------------------------------------------------------
def test_projected_balances_dialog_lists_events_with_running_balance(qapp, conn, seeded):
    dlg = ProjectedBalancesDialog(conn, today=TODAY)
    assert dlg.account.currentText() == "All spending accounts"
    assert [dlg.account.itemText(i) for i in range(dlg.account.count())] == [
        "All spending accounts", "Checking", "Savings"]          # no investment account
    assert dlg.horizon.currentText() == "30 days"
    t = dlg.table
    rows = [(t.item(r, dlg.PAYEE).text(), t.item(r, dlg.AMOUNT).text(),
             t.item(r, dlg.BALANCE).text(), t.item(r, dlg.SOURCE).text())
            for r in range(t.rowCount())]
    # Both accounts summed: the sweep is internal to the set, so NEITHER leg is
    # listed (see test_projection_internal_transfers) and the balances below --
    # which never saw it, it netted out -- are untouched.
    assert rows[0] == ("Late Co", "-10.00", "890.00", "Scheduled")
    assert rows[1] == ("Power Co", "-60.00", "830.00", "Entered (pending)")
    assert rows[2] == ("Cafe", "-40.00", "790.00", "Entered")
    assert rows[3] == ("Employer", "2,000.00", "2,790.00", "Scheduled")
    assert [r for r in rows if r[0] == "Auto-save"] == []
    assert "lowest 790.00" in dlg.summary.text()
    assert dlg.chart is not None
    # One account: only checking's side of the sweep, and the low point moves.
    dlg.account.setCurrentIndex(dlg.account.findText("Savings"))
    rows = [(t.item(r, dlg.PAYEE).text(), t.item(r, dlg.AMOUNT).text())
            for r in range(t.rowCount())]
    assert rows == [("Auto-save", "500.00")]
    assert dlg.projection.opening == 0 and dlg.projection.closing == 500_00
    dlg.horizon.setCurrentIndex(0)                               # 7 days: before the sweep
    assert dlg.table.rowCount() == 0
    dlg.deleteLater()


# ---------------------------------------------------------------------------
# Financial Calendar
# ---------------------------------------------------------------------------
def test_calendar_panel_cells_and_month_navigation(qapp, conn, seeded):
    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY,
                         account_id=seeded["chk"])
    assert dlg.title.text() == "January 2026"
    cell = dlg.cell_text(10)
    assert cell.startswith("10\n") and "Power Co -60.00" in cell and "Bal 830.00" in cell
    assert "· Employer 2,000.00" in dlg.cell_text(15)             # not yet entered
    assert "· Employer" in dlg.cell_text(31)                       # 15th and last day
    assert "Auto-save -500.00" in dlg.cell_text(20)
    assert dlg.cell_text(5).startswith("5\nGrocer -100.00")        # a past, entered day
    assert "Lowest" in dlg.summary.text()
    dlg.next_month()
    assert dlg.title.text() == "February 2026"
    assert "· Power Co -60.00" in dlg.cell_text(10)               # projected occurrence
    dlg.prev_month()
    dlg.prev_month()
    assert dlg.title.text() == "December 2025"
    assert dlg.cell_text(25).startswith("25")
    dlg.deleteLater()


def test_calendar_is_a_page_of_the_register_area_not_a_modal(qapp, conn, seeded):
    """The window opens on the calendar where a register goes, Tools comes back
    to it, and a write elsewhere leaves the month stale until it is shown."""
    from PyQt5.QtCore import QDate

    from mammon.ui.projection_dialogs import CalendarPanel
    from mammon.ui.widgets import MainWindow
    from mammon.webslinger import FakeWebSlingerClient

    now = QDate.currentDate().toString("yyyy-MM-dd")
    win = MainWindow(conn, webslinger=FakeWebSlingerClient())
    assert isinstance(win.calendar, CalendarPanel)
    assert win.stack.currentWidget() is win.calendar        # the startup page
    assert win.stack.widget(0) is win.calendar              # and the fallback page

    reg = win.open_register(seeded["chk"])
    assert win.stack.currentWidget() is reg
    assert win.show_calendar() is win.calendar
    assert win.stack.currentWidget() is win.calendar

    # A write does not recompute a month nobody is looking at...
    win.open_register(seeded["chk"])
    before = win.calendar.projection
    ledger.add_transaction(conn, seeded["chk"], now, -25_00, payee="Corner Store")
    win._refresh_all()
    assert win.calendar.projection is before and win.calendar._stale
    # ...but coming back to it redraws the month with that row on today.
    win.show_calendar()
    assert win.calendar.projection is not before and not win.calendar._stale
    assert "Corner Store -25.00" in win.calendar.cell_text(int(now[8:10]))
    win.close()


# ---------------------------------------------------------------------------
# the manager: status, Enter, Skip, the launch preference
# ---------------------------------------------------------------------------
def test_manager_shows_status_and_enters_or_skips(qapp, conn, seeded):
    dlg = ScheduledPaymentsDialog(conn, today=TODAY)
    t = dlg.table
    by_payee = {t.item(r, dlg.PAYEE).text(): r for r in range(t.rowCount())}
    assert t.item(by_payee["Late Co"], dlg.STATUS).text() == "Overdue 6d"
    assert t.item(by_payee["Employer"], dlg.STATUS).text() == "Upcoming"
    assert t.item(by_payee["Power Co"], dlg.STATUS).text() == "Upcoming"   # moved to Feb 10
    assert t.item(by_payee["Auto-save"], dlg.SOURCE).text() == "Transfer"
    assert t.item(by_payee["Employer"], dlg.SOURCE).text() == "Income"
    assert t.item(by_payee["Auto-save"], dlg.CATEGORY).text() == "[Savings]"
    t.setCurrentCell(by_payee["Late Co"], 0)
    assert dlg.enter_btn.isEnabled() and dlg.skip_btn.isEnabled()
    fired = []
    dlg.changed.connect(lambda: fired.append(True))
    dlg._enter()
    row = conn.execute("SELECT date, amount, scheduled FROM transactions "
                       "WHERE payee='Late Co'").fetchone()
    assert (row["date"], row["amount"], row["scheduled"]) == ("2026-01-02", -10_00, 0)
    assert scheduled.get_scheduled(conn, seeded["late"])["next_date"] == "2026-02-02"
    by_payee = {t.item(r, dlg.PAYEE).text(): r for r in range(t.rowCount())}
    t.setCurrentCell(by_payee["Employer"], 0)
    dlg._skip()
    assert scheduled.get_scheduled(conn, seeded["pay"])["next_date"] == "2026-01-31"
    assert fired == [True, True]
    # The launch preference is the checkbox.
    assert dlg.auto_launch.isChecked()
    dlg.auto_launch.setChecked(False)
    assert prefs.auto_enter_on_launch() is False
    dlg.deleteLater()


def test_editor_round_trips_transfer_lead_and_auto_enter(qapp, conn, seeded):
    ed = ScheduledPaymentEditor(conn)
    ed.account.setCurrentIndex(ed.account.findData(seeded["chk"]))
    ed.payee.setText("Card payment")
    ed.amount.setText("-250")
    ed.frequency.setCurrentIndex(ed.frequency.findData("semimonthly"))
    ed.transfer.setCurrentIndex(ed.transfer.findData(seeded["sav"]))
    assert not ed.category.isEnabled()
    ed.lead_days.setValue(10)
    ed.auto_enter.setChecked(False)
    v = ed.values()
    assert v["transfer_account_id"] == seeded["sav"] and v["category_id"] is None
    assert v["lead_days"] == 10 and v["auto_enter"] is False and v["frequency"] == "semimonthly"
    sid = scheduled.add_scheduled(conn, **v)
    entry = scheduled.get_scheduled(conn, sid)
    ed2 = ScheduledPaymentEditor(conn, entry=entry)
    assert ed2.transfer.currentData() == seeded["sav"] and ed2.lead_days.value() == 10
    assert not ed2.auto_enter.isChecked() and not ed2.category.isEnabled()
    ed2.lead_days.setValue(-1)
    assert ed2.values()["lead_days"] is None
    ed.deleteLater()
    ed2.deleteLater()


def test_main_window_pre_enters_due_payments_on_launch(qapp, tmp_path):
    from mammon.ui.widgets import MainWindow
    from mammon.webslinger import FakeWebSlingerClient
    conn = db.init_db(tmp_path / "launch.db")
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    from PyQt5.QtCore import QDate
    today = QDate.currentDate().toString("yyyy-MM-dd")
    scheduled.add_scheduled(conn, chk, payee="Due Co", amount=-5_00, frequency="monthly",
                            next_date=today)
    scheduled.add_scheduled(conn, chk, payee="Remind Co", amount=-7_00, frequency="monthly",
                            next_date=today, auto_enter=False)
    win = MainWindow(conn, webslinger=FakeWebSlingerClient())
    placeholders = conn.execute(
        "SELECT payee FROM transactions WHERE scheduled=1").fetchall()
    assert [r["payee"] for r in placeholders] == ["Due Co"]         # remind-only waits
    assert win.act_scheduled.text() == "Scheduled Payments (1 due)…"
    win.close()
    # With the preference off, launch enters nothing.
    prefs.set_auto_enter_on_launch(False)
    conn2 = db.init_db(tmp_path / "launch2.db")
    chk2 = ledger.create_account(conn2, "Checking", "checking", opening_balance=100_00)
    scheduled.add_scheduled(conn2, chk2, payee="Due Co", amount=-5_00, frequency="monthly",
                            next_date=today)
    win2 = MainWindow(conn2, webslinger=FakeWebSlingerClient())
    assert conn2.execute("SELECT COUNT(*) FROM transactions WHERE scheduled=1").fetchone()[0] == 0
    assert win2.act_scheduled.text() == "Scheduled Payments (1 due)…"
    win2.close()
    conn.close()
    conn2.close()
