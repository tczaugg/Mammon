"""The financial calendar's month title is pinned to the width of the widest
month name, so the ◀ / ▶ arrows never move under the user's cursor.

Before this, the title sized itself to the current month, and because the
arrow group sits after a stretch in the header row, "May" vs "September"
dragged the back arrow sideways every month - the user had to look up to find
it. These tests step a real CalendarPanel through all twelve months and assert
both the title width and prev_btn's x position are constant, and that the
pinned width still fits the longest rendered title (so a font change cannot
silently elide it)."""
from __future__ import annotations

import datetime as _dt
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger
from mammon.ui import prefs
from mammon.ui.projection_dialogs import CalendarPanel
from mammon.tests import fresh_db

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
    c = fresh_db(tmp_path / "caltitle.db")
    ledger.create_account(c, "Checking", "checking", opening_balance=1000_00)
    ledger.create_account(c, "Savings", "savings", opening_balance=500_00)
    yield c
    c.close()


@pytest.fixture
def panel(qapp, conn):
    p = CalendarPanel(conn, today=TODAY)
    p.resize(1200, 800)
    p.show()
    qapp.processEvents()
    p.layout().activate()
    qapp.processEvents()
    yield p
    p.hide()
    p.deleteLater()


def _prev_x(panel) -> int:
    return panel.prev_btn.mapTo(panel, panel.prev_btn.rect().topLeft()).x()


def _walk(panel, qapp, step):
    """Step through twelve months, sampling (title text, width, arrow x)."""
    seen = []
    for _ in range(12):
        qapp.processEvents()
        panel.layout().activate()
        qapp.processEvents()
        seen.append((panel.title.text(), panel.title.width(), _prev_x(panel)))
        step()
    return seen


def test_title_width_and_back_arrow_never_move(panel, qapp):
    for step in (panel.next_month, panel.prev_month):
        seen = _walk(panel, qapp, step)
        months = {t.split()[0] for t, _, _ in seen}
        assert len(months) == 12, months
        assert {"February", "September"} <= months
        widths = {w for _, w, _ in seen}
        xs = {x for _, _, x in seen}
        assert len(widths) == 1, f"title width moved: {seen}"
        assert len(xs) == 1, f"back arrow moved: {seen}"


def test_pinned_width_fits_the_longest_month(panel, qapp):
    fm = QFontMetrics(panel.title.font())
    longest = max(fm.width(_dt.date(2026, m, 1).strftime("%B %Y"))
                  for m in range(1, 13))
    assert panel.title.width() >= longest
    # And no month's text is elided at that width.
    for _ in range(12):
        panel.next_month()
        qapp.processEvents()
        text = panel.title.text()
        assert fm.width(text) <= panel.title.width()
        assert "…" not in text and text.endswith(str(panel.year))
