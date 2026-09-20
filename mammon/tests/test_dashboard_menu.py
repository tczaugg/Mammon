"""View > Investment Dashboard actually opens the dashboard.

The regression this pins: the dashboard page shipped, but the View menu still
carried the disabled "Investment Center..." placeholder, so there was no way to
reach the page from the menu bar. These tests run against the REAL main window
(not a stub stack) because the bug lived in the wiring between the menu action,
the page instance and the home QStackedWidget -- a stub would have passed.

Index 0 of that stack is load-bearing: ``_drop_register`` falls back to
``setCurrentIndex(0)`` to land on the calendar when an open register goes away,
so appending the dashboard must not displace it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db
from mammon.tests import fresh_db


@pytest.fixture
def qapp():
    from PyQt5.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow
    conn = fresh_db(tmp_path / "dashboard_menu.db")
    sample_data(conn)
    w = MainWindow(conn)
    try:
        yield w
    finally:
        w.close()
        conn.close()


def _menu_actions(window):
    """Every action in every top-level menu, flattened."""
    out = []
    for menu_act in window.menuBar().actions():
        menu = menu_act.menu()
        if menu is None:
            continue
        out.extend(menu.actions())
    return out


def _view_action(window, needle):
    for act in _menu_actions(window):
        if needle in act.text():
            return act
    return None


def test_view_menu_offers_an_enabled_investment_dashboard(win):
    act = _view_action(win, "Investment Dashboard")
    assert act is not None, "no View action mentions Investment Dashboard"
    assert act.isEnabled(), "the Investment Dashboard action is still disabled"
    # The placeholder it replaced must be gone, not merely shadowed by a second
    # entry: two near-identical items is the confusing end state.
    stale = [a.text() for a in _menu_actions(win) if "Investment Center" in a.text()]
    assert stale == [], f"stale placeholder action(s) still present: {stale}"


def test_triggering_it_switches_the_home_stack_to_the_dashboard(win):
    from mammon.ui.investment_dashboard import InvestmentDashboardPage
    act = _view_action(win, "Investment Dashboard")
    act.trigger()
    assert isinstance(win.stack.currentWidget(), InvestmentDashboardPage)


def test_the_calendar_keeps_stack_index_zero(win):
    from mammon.ui.projection_dialogs import CalendarPanel
    _view_action(win, "Investment Dashboard").trigger()
    # _drop_register lands on index 0 by number, so this is the contract.
    assert isinstance(win.stack.widget(0), CalendarPanel)
    assert win.stack.widget(0) is win.calendar


def test_the_page_instance_is_reused_not_rebuilt(win):
    act = _view_action(win, "Investment Dashboard")
    act.trigger()
    first = win.stack.currentWidget()
    win.show_calendar()
    act.trigger()
    assert win.stack.currentWidget() is first


def test_a_write_marks_it_stale_and_re_entry_refreshes_it(win):
    act = _view_action(win, "Investment Dashboard")
    act.trigger()                       # first draw clears any initial staleness
    page = win.stack.currentWidget()
    win.show_calendar()                 # leave the page, as a user would

    page.mark_stale()
    seen = []
    original = page.refresh_if_stale

    def spy():
        did = original()
        seen.append(did)
        return did

    page.refresh_if_stale = spy
    try:
        act.trigger()
    finally:
        del page.refresh_if_stale

    assert seen == [True], (
        "re-entering the dashboard did not recompute a stale page; "
        f"refresh_if_stale returned {seen}")
