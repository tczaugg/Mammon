"""The Reports menu is a hand-written sequence of ``addAction`` calls in
``widgets._build_menu`` -- there is no registry -- so a report wired only to the
place it was born in stays invisible to anyone who does not already know where
to look. That is exactly what happened to Capital Gains and Taxes: it shipped
reachable ONLY from the Investment Dashboard's corner launcher.

These tests pin both halves of the fix against the REAL main window (a stub
would have passed the regression, which lived entirely in the menu wiring):
the action is on the menu, it opens the shared report window on the same
``CAPITAL_GAINS_SPEC`` object the dashboard opens, and triggering it a second
time raises the window already on screen instead of stacking a duplicate on
top of it.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db

from PyQt5.QtCore import QCoreApplication


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    """Redirect QSettings (used by mammon.ui.prefs) to a per-test temp dir, so
    the window/report preferences never touch the developer's real settings."""
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def main_win(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow
    conn = db.init_db(tmp_path / "reports_menu.db")
    sample_data(conn)
    win = MainWindow(conn)
    try:
        yield win
    finally:
        for report in list(getattr(win, "_report_windows", [])):
            report.close()
        win.close()
        conn.close()


def _reports_menu(window):
    for menu_act in window.menuBar().actions():
        menu = menu_act.menu()
        if menu is not None and "Reports" in menu_act.text():
            return menu
    return None


def _capital_gains_action(window):
    reports = _reports_menu(window)
    assert reports is not None, "no Reports menu on the menu bar"
    hits = [a for a in reports.actions() if "Capital Gains" in a.text()]
    assert hits, ("the Reports menu does not name Capital Gains: "
                  f"{[a.text() for a in reports.actions()]}")
    assert len(hits) == 1, f"duplicate entries: {[a.text() for a in hits]}"
    return hits[0]


def _capital_gains_windows(window):
    from mammon.ui.report_window import CAPITAL_GAINS_SPEC
    return [w for w in getattr(window, "_report_windows", [])
            if getattr(w, "spec", None) is CAPITAL_GAINS_SPEC]


def test_reports_menu_offers_capital_gains(main_win):
    act = _capital_gains_action(main_win)
    assert act.isEnabled()


def test_triggering_it_opens_the_capital_gains_report_window(main_win):
    from mammon.ui.report_window import CAPITAL_GAINS_SPEC, ReportWindow

    assert _capital_gains_windows(main_win) == []
    _capital_gains_action(main_win).trigger()   # modeless: show(), never exec_()
    QCoreApplication.processEvents()

    opened = _capital_gains_windows(main_win)
    assert len(opened) == 1, f"expected one report window, got {len(opened)}"
    win = opened[0]
    assert isinstance(win, ReportWindow)
    assert win.spec is CAPITAL_GAINS_SPEC
    assert win.windowTitle() == CAPITAL_GAINS_SPEC.title
    assert win.isVisible()


def test_triggering_it_twice_raises_the_same_window(main_win):
    """Two ways in (this menu and the dashboard corner) share one window, so a
    second trip through either must not bury the first under a fresh copy."""
    act = _capital_gains_action(main_win)
    act.trigger()
    QCoreApplication.processEvents()
    first = _capital_gains_windows(main_win)
    assert len(first) == 1

    act.trigger()
    QCoreApplication.processEvents()
    again = _capital_gains_windows(main_win)
    assert len(again) == 1, f"a second window was opened: {len(again)} total"
    assert again[0] is first[0]


def test_the_dashboard_corner_shares_the_menus_window(main_win):
    """The dashboard launcher and the menu name the same spec object, which is
    what lets one window serve both -- checked without building the Qt page, so
    no corner launcher's modal loop is entered."""
    from mammon.ui.investment_dashboard import InvestmentDashboardPage
    from mammon.ui.report_window import CAPITAL_GAINS_SPEC

    class _Recorder:
        opened = None

        def _open_report(self, spec):
            self.opened = spec
            return spec

    rec = _Recorder()
    InvestmentDashboardPage.open_capital_gains(rec)
    assert rec.opened is CAPITAL_GAINS_SPEC
