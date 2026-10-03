"""Projected Balances, the Financial Calendar, the reminder-aware Scheduled
Payments manager, and pre-entry at launch (roadmap item 5)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication, QLabel

from mammon import db, ledger, loans, scheduled
from mammon.ui import prefs
from mammon.ui.projection_dialogs import CalendarPanel, ProjectedBalancesDialog
from mammon.ui.scheduled_payments_dialog import (ScheduledPaymentEditor,
                                                 ScheduledPaymentsDialog)
from mammon.tests import fresh_db

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
    c = fresh_db(tmp_path / "dialogs.db")
    yield c
    c.close()


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
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
    conn = fresh_db(tmp_path / "launch.db")
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
    conn2 = fresh_db(tmp_path / "launch2.db")
    chk2 = ledger.create_account(conn2, "Checking", "checking", opening_balance=100_00)
    scheduled.add_scheduled(conn2, chk2, payee="Due Co", amount=-5_00, frequency="monthly",
                            next_date=today)
    win2 = MainWindow(conn2, webslinger=FakeWebSlingerClient())
    assert conn2.execute("SELECT COUNT(*) FROM transactions WHERE scheduled=1").fetchone()[0] == 0
    assert win2.act_scheduled.text() == "Scheduled Payments (1 due)…"
    win2.close()
    conn.close()
    conn2.close()


# -- budget burn-down mode (SRD 5.10e) ----------------------------------------
# The fixtures below are deliberately synthetic: one account, one envelope,
# three identical charges, so the arithmetic in every assertion is checkable by
# hand from the numbers written in the test.

@pytest.fixture
def budgeted(conn):
    """A checking account, a Groceries envelope for January, and a helper that
    fills it with N identical charges on consecutive days."""
    from mammon import budgets

    acct = ledger.create_account(conn, "Everyday", "checking", opening_balance=5000_00)
    cat = ledger.resolve_category(conn, "Groceries")
    b = budgets.create_budget(conn, "Household")

    def build(limit_cents, amounts, days=(2, 3, 4)):
        budgets.set_line(conn, b, cat, "2026-01", limit_cents)
        for day, amount in zip(days, amounts):
            ledger.add_transaction(conn, acct, f"2026-01-{day:02d}", -amount,
                                   payee="Market", category_id=cat)
        return b

    return {"acct": acct, "cat": cat, "budget": b, "build": build}


def test_budget_mode_marks_every_expense_past_the_category_limit(qapp, conn, budgeted):
    """150 envelope, three 90 charges: 90 fits, 180 does not, 270 does not.

    The rule is every expense past the limit, not just the one that crosses it,
    so days 3 and 4 each carry their OWN mark -- and each mark's tooltip names
    the category that blew, because a day can hold two of them.
    """
    budgeted["build"](150_00, (90_00, 90_00, 90_00))
    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY,
                        account_id=budgeted["acct"])
    dlg.budget_mode.setChecked(True)

    assert dlg.marks_on(2) == []
    assert len(dlg.marks_on(3)) == 1
    assert len(dlg.marks_on(4)) == 1
    # Each mark carries how far past the limit that expense LEAVES the
    # category: 180 - 150 = 30, then 270 - 150 = 120.
    assert [x.over_cents for x in dlg.marks_on(3)] == [30_00]
    assert [x.over_cents for x in dlg.marks_on(4)] == [120_00]

    # The mark is a rendered image, never a text glyph and never an emoji.
    from mammon.ui.projection_dialogs import MARK_FALLBACK, mark_html
    assert mark_html().startswith("<img src=\"file:///")
    assert ".png" in mark_html()
    assert mark_html() in dlg.cell_html(3)
    assert mark_html() in dlg.cell_html(4)
    assert mark_html() not in dlg.cell_html(2)
    assert MARK_FALLBACK in dlg.cell_text(3)
    assert MARK_FALLBACK not in dlg.cell_text(2)

    # The tooltip names the blown category, on the marked days only.
    assert "Groceries" in dlg.cell_tooltip(3)
    assert "Over budget" in dlg.cell_tooltip(3)
    assert "Over budget" not in dlg.cell_tooltip(2)

    # Back to balances and every mark goes away.
    dlg.budget_mode.setChecked(False)
    assert dlg.marks_on(3) == []
    assert mark_html() not in dlg.cell_html(3)
    dlg.deleteLater()


def test_the_over_budget_mark_is_inked_orange_red_in_both_themes():
    """The moneybag mark's ink is orange-red by design. It was a neutral gray
    pair once, and the
    comment above it argued FOR neutral ink, so a later session reading that
    reasoning could quietly put gray back. Assert the hue, not the literal
    strings: red dominant, blue least, and a hue angle in the orange-red wedge
    -- which no gray (all three channels equal, hue undefined) can satisfy.
    """
    from PyQt5.QtGui import QColor

    from mammon.ui.projection_dialogs import _MARK_COLOR

    assert len(_MARK_COLOR) == 2, "keep the (light, dark) pair shape"
    for hexcolor in _MARK_COLOR:
        c = QColor(hexcolor)
        assert c.isValid(), hexcolor
        r, g, b = c.red(), c.green(), c.blue()
        assert r > g > b, "%s is not red-dominant/blue-least" % hexcolor
        assert 5 <= c.hsvHue() <= 25, \
            "%s sits at hue %d, outside the orange-red wedge" % (hexcolor,
                                                                 c.hsvHue())
        assert c.value() > 128 and c.saturation() > 128, \
            "%s is too washed out to read as orange-red" % hexcolor


def test_budget_mode_marks_only_the_charges_that_actually_cross(qapp, conn, budgeted):
    """The same three 90 charges under a 200 envelope: 90 and 180 both fit, so
    only the third is past the limit, by 70. The rule marks what is over, which
    is not the same as marking everything after the first big day."""
    budgeted["build"](200_00, (90_00, 90_00, 90_00))
    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY,
                        account_id=budgeted["acct"])
    dlg.budget_mode.setChecked(True)

    assert dlg.marks_on(2) == []
    assert dlg.marks_on(3) == []
    assert [x.over_cents for x in dlg.marks_on(4)] == [70_00]
    assert [x.category_name for x in dlg.marks_on(4)] == ["Groceries"]
    dlg.deleteLater()


def test_budget_mode_swaps_the_day_figure_and_keeps_the_account_slots_live(qapp, conn, budgeted):
    """Burn-down shows what is LEFT of the envelope rather than a balance -- and
    the account slots stay alive and keep their plain label, because the user
    overruled the original "budget mode ignores the selector" default."""
    budgeted["build"](150_00, (90_00, 90_00, 90_00))
    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY,
                        account_id=budgeted["acct"])

    assert dlg.slots.isEnabled()
    assert "Bal " in dlg.cell_text(2)
    assert "Left " not in dlg.cell_text(2)

    dlg.budget_mode.setChecked(True)
    assert dlg.slots.isEnabled()                    # the selector is not taken away
    assert dlg.slots_label.text() == "Accounts"
    assert dlg.slots_label.toolTip() == ""
    assert "Bal " not in dlg.cell_text(2)
    assert "Left 60.00" in dlg.cell_text(2)         # 150 - 90
    assert "Left -30.00" in dlg.cell_text(3)        # 150 - 180
    assert "Left -120.00" in dlg.cell_text(4)       # 150 - 270

    # The summary swaps to the envelope, and keeps the projected low beside it
    # because burn-down ignores income and cannot see a cash-floor problem.
    assert "Household" in dlg.summary.text()
    assert "Allowance 150.00" in dlg.summary.text()
    assert "Left -120.00" in dlg.summary.text()
    assert "Lowest" in dlg.summary.text()
    # The one spending account IS the household here, so no subset caveat.
    assert "spend counted only" not in dlg.summary.text()

    dlg.budget_mode.setChecked(False)
    assert dlg.slots.isEnabled()
    assert "Opening" in dlg.summary.text()
    dlg.deleteLater()


def test_budget_mode_burns_down_only_the_selected_accounts(qapp, conn):
    """End to end: the slot selection drives the burn-down in budget mode, day
    figures and summary together, while the allowance stays household-wide.

    The design ruling. Balance mode and budget mode read ONE selector, so the
    month can be asked what a single card did to the plan -- and because the
    allowance is not scaled down with it, the summary has to say plainly that
    only some accounts' spend is counted, or "Left" reads as money to spend.
    """
    from mammon import budgets

    checking = ledger.create_account(conn, "Everyday", "checking",
                                     opening_balance=5000_00)
    card = ledger.create_account(conn, "Rewards Card", "credit")
    cat = ledger.resolve_category(conn, "Groceries")
    b = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, b, cat, "2026-01", 300_00)
    ledger.add_transaction(conn, checking, "2026-01-02", -100_00,
                           payee="Market", category_id=cat)
    ledger.add_transaction(conn, card, "2026-01-03", -50_00,
                           payee="Grocer", category_id=cat)

    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY)
    # save=False throughout: the slots persist to the real per-user QSettings.
    dlg.slots.set_account_ids([], save=False)
    dlg.budget_mode.setChecked(True)
    assert dlg.slots.isEnabled()

    # No slot filled: every spending account, so both charges burn the envelope.
    assert "Left 200.00" in dlg.cell_text(2)        # 300 - 100
    assert "Left 150.00" in dlg.cell_text(3)        # 300 - 150
    assert "Allowance 300.00" in dlg.summary.text()
    assert "Spent 150.00" in dlg.summary.text()
    assert "spend counted only" not in dlg.summary.text()

    # Narrow to the checking account: the card's charge stops counting, and the
    # day figures move with it. The allowance does NOT shrink.
    dlg.slots.set_account_ids([checking], save=False)
    assert dlg.slots.isEnabled()                    # still usable in budget mode
    assert dlg.account_ids() == [checking]
    assert "Left 200.00" in dlg.cell_text(2)
    assert "Left 200.00" in dlg.cell_text(3)        # the card's 50 is not counted
    assert "Allowance 300.00" in dlg.summary.text()
    assert "Spent 100.00" in dlg.summary.text()
    assert "Left 200.00" in dlg.summary.text()
    assert "spend counted only for Everyday" in dlg.summary.text()

    # Narrow to the card instead: the other charge, the same untouched allowance.
    dlg.slots.set_account_ids([card], save=False)
    assert "Left 300.00" in dlg.cell_text(2)        # checking's 100 is not counted
    assert "Left 250.00" in dlg.cell_text(3)        # 300 - 50
    assert "Spent 50.00" in dlg.summary.text()
    assert "spend counted only for Rewards Card" in dlg.summary.text()

    # Both accounts named is the whole household again: totals restored, no caveat.
    dlg.slots.set_account_ids([checking, card], save=False)
    assert "Left 150.00" in dlg.cell_text(3)
    assert "Spent 150.00" in dlg.summary.text()
    assert "spend counted only" not in dlg.summary.text()
    assert dlg.slots.isEnabled() and dlg.budget_mode.isChecked()
    dlg.deleteLater()


def test_budget_mode_shows_coverage_and_flags_a_sparse_budget(qapp, conn, budgeted):
    """The toggle carries the share of recent spending the budget has a line
    for, and a budget that covers almost nothing says so rather than quietly
    burning down a number nobody should trust."""
    from mammon import budgets

    budgeted["build"](150_00, (90_00, 90_00, 90_00))
    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY,
                        account_id=budgeted["acct"])
    assert dlg.coverage.text() == ""                 # balance mode says nothing

    dlg.budget_mode.setChecked(True)
    assert "covers 100% of recent spending" in dlg.coverage.text()
    assert not dlg.burn.low_coverage

    # Now spend most of the money somewhere the budget has no line at all.
    unbudgeted = ledger.resolve_category(conn, "Home Improvement")
    ledger.add_transaction(conn, budgeted["acct"], "2026-01-05", -900_00,
                           payee="Lumber Yard", category_id=unbudgeted)
    dlg.refresh()
    # 270 of 1170 budgeted -> 23.1%, well under the 60% floor.
    assert "covers 23% of recent spending" in dlg.coverage.text()
    assert dlg.burn.low_coverage
    from mammon.ui.projection_dialogs import mark_html
    assert mark_html() in dlg.coverage.text()
    assert "Home Improvement" in dlg.coverage.toolTip()
    assert str(budgets.COVERAGE_DAYS) in dlg.coverage.toolTip()
    dlg.deleteLater()


def test_budget_mode_explains_itself_when_no_budget_covers_the_month(qapp, conn, seeded):
    """No active budget is not an error and never a dialog: the month keeps its
    events and the summary says what is missing."""
    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY,
                        account_id=seeded["chk"])
    dlg.budget_mode.setChecked(True)
    assert dlg.burn is None
    assert "No active budget" in dlg.summary.text()
    assert dlg.coverage.text() == ""
    assert "Grocer" in dlg.cell_text(5)             # the month still draws
    dlg.deleteLater()


# ---------------------------------------------------------------------------
# the band under the calendar: trend chart, or a bar per budget item (SRD 5.12e)
# ---------------------------------------------------------------------------
@pytest.fixture
def three_envelopes(conn):
    """One checking account and a January plan with three envelopes in three
    different states: Fuel untouched, Groceries partly spent, Dining OVERSPENT."""
    from mammon import budgets

    acct = ledger.create_account(conn, "Everyday", "checking",
                                 opening_balance=5000_00)
    cat = {name: ledger.resolve_category(conn, name)
           for name in ("Groceries", "Dining", "Fuel")}
    b = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, b, cat["Groceries"], "2026-01", 300_00)
    budgets.set_line(conn, b, cat["Dining"], "2026-01", 100_00)
    budgets.set_line(conn, b, cat["Fuel"], "2026-01", 80_00)
    ledger.add_transaction(conn, acct, "2026-01-03", -120_00, payee="Market",
                           category_id=cat["Groceries"])
    ledger.add_transaction(conn, acct, "2026-01-04", -130_00, payee="Cafe",
                           category_id=cat["Dining"])
    return {"acct": acct, "budget": b, "cat": cat}


def test_budget_mode_replaces_the_spending_chart_with_a_bar_per_item(
        qapp, conn, three_envelopes):
    """Budget mode's band answers "which envelope is in trouble": one bar per
    budgeted category, green measuring what is LEFT, and an overspent envelope
    reading empty with its overrun in red OUTSIDE the bar's left edge. Every
    figure is the domain's; the widget only turns cents into pixels."""
    from mammon import budgets
    from mammon.ui.budget_bars import (OVER_GUTTER, OVER_PAD, BudgetBarGrid,
                                       BudgetItemBar)
    from mammon.ui.charts import SpendingBarCanvas

    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY,
                        account_id=three_envelopes["acct"])
    dlg.include_predictions.setChecked(False)
    assert isinstance(dlg.chart_widget, SpendingBarCanvas)

    dlg.budget_mode.setChecked(True)
    grid = dlg.chart_widget
    assert isinstance(grid, BudgetBarGrid)
    assert dlg.chart_box.indexOf(grid) >= 0          # it really is in the band
    assert not isinstance(grid, SpendingBarCanvas)

    bars = grid.bars()
    assert all(isinstance(bar, BudgetItemBar) for bar in bars)
    want = budgets.month_category_status(conn, three_envelopes["budget"],
                                        "2026-01", include_predictions=False,
                                        account_ids=dlg.account_ids(),
                                        today=TODAY)
    assert len(bars) == 3                            # one per budgeted category
    assert [bar.status for bar in bars] == list(want)

    by_name = {bar.status.category_name: bar for bar in bars}
    assert by_name["Fuel"].amount_text() == "80.00 left"
    assert by_name["Groceries"].amount_text() == "180.00 left"
    assert by_name["Dining"].amount_text() == "30.00 over"
    # Fixed by category path, not by what is left: the same sequence every month.
    assert [bar.status.category_name for bar in bars] == ["Dining", "Fuel",
                                                          "Groceries"]

    # -- the pixels the painter would use, at a known width --------------------
    fuel = by_name["Fuel"].bar_geometry(width=180)
    assert fuel.bar_x == OVER_GUTTER                 # the gutter the red uses
    assert fuel.bar_width == pytest.approx(180 - OVER_GUTTER - 10)
    assert fuel.fill_width == pytest.approx(fuel.bar_width)   # untouched: full
    assert fuel.over_width == 0

    groceries = by_name["Groceries"].bar_geometry(width=180)
    assert groceries.bar_x == fuel.bar_x             # bars share one left edge
    assert groceries.fill_width == pytest.approx(0.6 * groceries.bar_width)

    over = by_name["Dining"].bar_geometry(width=180)
    assert over.fill_width == 0                      # nothing left inside it
    assert over.over_width == pytest.approx(30_00 / over.cents_per_pixel)
    assert over.over_x == pytest.approx(over.bar_x - over.over_width)
    assert over.over_x < over.bar_x                  # to the LEFT of the bar
    assert not over.over_clipped

    # A catastrophic overrun is capped inside the gutter rather than drawn off
    # the widget -- and the figure is in the label, so the cap costs no number.
    huge = by_name["Dining"].bar_geometry(width=600)
    assert huge.over_clipped
    assert huge.over_width == pytest.approx(OVER_GUTTER - OVER_PAD)
    assert huge.over_x >= 0
    dlg.deleteLater()


def test_leaving_budget_mode_brings_the_spending_chart_back(
        qapp, conn, three_envelopes):
    """The bars belong to budget mode only: unchecking the box restores the
    trailing-year income and spending chart exactly as before."""
    from mammon.ui.budget_bars import BudgetBarGrid
    from mammon.ui.charts import SpendingBarCanvas

    dlg = CalendarPanel(conn, year=2026, month=1, today=TODAY,
                        account_id=three_envelopes["acct"])
    dlg.budget_mode.setChecked(True)
    assert isinstance(dlg.chart_widget, BudgetBarGrid)

    dlg.budget_mode.setChecked(False)
    assert isinstance(dlg.chart_widget, SpendingBarCanvas)
    assert dlg.chart_box.count() == 1                # one occupant, not two
    dlg.deleteLater()


def test_budget_bars_flow_into_three_or_four_columns_and_say_when_empty(qapp):
    """The band holds 3-4 columns of items at the widths it actually gets, laid
    out in reading order, and an empty plan says so instead of drawing nothing."""
    from mammon.budgets import CategoryStatus
    from mammon.ui.budget_bars import GRID_HEIGHT, BudgetBarGrid

    items = tuple(CategoryStatus(category_id=i, category_name=f"Item {i}",
                                 allowance_cents=100_00, spent_cents=i * 10_00)
                  for i in range(1, 8))
    grid = BudgetBarGrid(items)
    assert grid.column_count(900) == 4
    assert grid.column_count(650) == 3
    assert grid.column_count(300) == 1
    assert grid.column_count(4000) == 4              # never more than four
    assert grid.maximumHeight() == GRID_HEIGHT       # the chart's own band

    grid = BudgetBarGrid(items, columns=3)
    layout = grid.widget().layout()
    assert grid.columns() == 3
    # Row-major: the domain's order runs along the top row, left to right.
    positions = [layout.getItemPosition(layout.indexOf(bar))[:2]
                 for bar in grid.bars()]
    assert positions[:4] == [(0, 0), (0, 1), (0, 2), (1, 0)]

    empty = BudgetBarGrid(())
    assert empty.bars() == []
    assert "No budget items" in empty.widget().findChild(QLabel).text()
