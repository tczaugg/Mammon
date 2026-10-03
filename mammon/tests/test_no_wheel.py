"""No dropdown, spin box or date field changes value on a mouse wheel.

Rolling the wheel to scroll a page must never silently re-pick a category, a
frequency, an account type or a year, with nothing on screen to say so.
The fix is one shape, already in ``mammon/ui/delegates.py``: a ``NoWheel*``
subclass whose ``wheelEvent`` calls ``event.ignore()``. Ignoring (rather than
accepting-and-swallowing) matters twice over -- the value cannot change, AND the
wheel still reaches the enclosing scroll area, so a page under the cursor does
not feel dead.

Two tests, because either one alone rots:

* behavioral -- send a real ``QWheelEvent`` at real dropdowns, spin boxes and
  date fields in two real dialogs and assert nothing moved and the event was
  left unaccepted, plus one scroll-area case proving the page still scrolls;
* a source guard -- no file in ``mammon/ui`` may instantiate a bare
  ``QComboBox``/``QSpinBox``/``QDoubleSpinBox``/``QDateEdit``/``QFontComboBox``
  again, so the next dropdown someone adds gets the treatment too.

Synthetic data only.
"""
from __future__ import annotations

import datetime as _dt
import os
import pathlib
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QPoint, QPointF, Qt
from PyQt5.QtWidgets import (
    QApplication, QScrollArea, QVBoxLayout, QWidget,
)

from mammon.tests import fresh_db
from mammon.ui import delegates


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wheel_event(widget, notches: int = -3):
    """A wheel event over ``widget``, as Qt delivers one from a real mouse.

    ``notches`` is in Qt's eighth-of-a-degree units times 15 per notch; the sign
    only decides which way a value WOULD have moved, which is the point.
    """
    from PyQt5.QtGui import QWheelEvent
    delta = QPoint(0, 15 * 8 * notches)
    center = QPointF(widget.rect().center())
    return QWheelEvent(center, widget.mapToGlobal(widget.rect().center()),
                       delta, delta, Qt.NoButton, Qt.NoModifier,
                       Qt.NoScrollPhase, False)


def send_wheel(widget, notches: int = -3) -> bool:
    """Deliver a wheel event to ``widget``; return whether it ended ACCEPTED.

    False means it was ignored and therefore free to propagate to whatever
    scroll area encloses the widget -- the behavior we want everywhere.
    """
    event = wheel_event(widget, notches)
    QApplication.sendEvent(widget, event)
    return event.isAccepted()


# -- behavioral ------------------------------------------------------------

def test_account_dialog_pickers_ignore_the_wheel(app):
    """A dropdown, a cash spin box and a date field in one real dialog."""
    from mammon.ui.widgets import NewAccountDialog
    dlg = NewAccountDialog()
    try:
        combo = dlg.type
        assert combo.count() > 1, "need a second item for the wheel to land on"
        combo.setCurrentIndex(1)
        before = combo.currentIndex()
        assert send_wheel(combo) is False
        assert send_wheel(combo, notches=+3) is False
        assert combo.currentIndex() == before

        spin = dlg.opening
        spin.setValue(12.34)
        assert send_wheel(spin) is False
        assert spin.value() == pytest.approx(12.34)

        date = dlg.opening_date if hasattr(dlg, "opening_date") else None
        if date is None:  # find the dialog's date editor whatever it is named
            date = dlg.findChild(delegates.NoWheelDateEdit)
        assert date is not None, "the dialog should build its date via make_date_edit"
        iso_before = delegates.date_edit_iso(date)
        assert send_wheel(date) is False
        assert delegates.date_edit_iso(date) == iso_before
    finally:
        dlg.deleteLater()


def test_budgets_dialog_combo_ignores_the_wheel(app, tmp_path):
    """The Budgets dialog's start-month dropdown (mammon/ui/budget_page.py)."""
    from mammon.ui.budget_page import BudgetsDialog
    conn = fresh_db(tmp_path / "budgets.db")
    try:
        dlg = BudgetsDialog(conn, today=_dt.date(2024, 6, 15))
        try:
            combo = dlg.start_combo
            assert combo.count() > 1
            before = combo.currentIndex()
            assert send_wheel(combo) is False
            assert send_wheel(combo, notches=+1) is False
            assert combo.currentIndex() == before
        finally:
            dlg.deleteLater()
    finally:
        conn.close()


def scrollable_page(combo_factory):
    """A scrollable page with one dropdown near the top of it."""
    area = QScrollArea()
    inner = QWidget()
    box = QVBoxLayout(inner)
    combo = combo_factory()
    combo.addItems([f"item {i}" for i in range(10)])
    box.addWidget(combo)
    filler = QWidget()
    filler.setMinimumHeight(4000)        # force a scroll range
    box.addWidget(filler)
    area.setWidget(inner)
    area.resize(300, 200)
    area.show()
    QApplication.processEvents()
    return area, combo


def test_ignored_wheel_leaves_the_page_free_to_scroll(app):
    """The whole reason the event is IGNORED and not accepted: a dropdown must
    not be a dead spot on a scrollable page.

    Qt walks an unaccepted wheel event up the parent chain only on the real
    (spontaneous) delivery path, which a headless test cannot forge, so this
    checks the two halves it can: our combo leaves the event unaccepted, and
    that same event scrolls the page once it reaches the enclosing viewport.
    A plain ``QComboBox`` is built alongside as the baseline -- it accepts the
    event and changes value, which is the bug being fixed.
    """
    from PyQt5.QtWidgets import QComboBox  # the baseline, deliberately bare

    bare_area, bare_combo = scrollable_page(QComboBox)
    area, combo = scrollable_page(delegates.NoWheelComboBox)
    try:
        bare_combo.setCurrentIndex(3)
        assert send_wheel(bare_combo) is True, "baseline: Qt's combo eats the wheel"
        assert bare_combo.currentIndex() != 3, "baseline: and re-picks an item"

        bar = area.verticalScrollBar()
        assert bar.maximum() > 0, "the test page must actually be scrollable"
        bar.setValue(0)
        combo.setCurrentIndex(3)
        assert send_wheel(combo) is False  # unaccepted: free to propagate
        assert combo.currentIndex() == 3
        assert bar.value() == 0, "the combo itself must not scroll the page"

        # What propagation then does: the page moves, the selection does not.
        assert send_wheel(area.viewport()) is True
        assert bar.value() > 0, "the wheel should scroll the page it reaches"
        assert combo.currentIndex() == 3
    finally:
        for widget in (area, bare_area):
            widget.hide()
            widget.deleteLater()


# -- source guard ----------------------------------------------------------

#: Widgets whose value a mouse wheel changes, and the NoWheel replacement.
BARE = {
    "QComboBox": "delegates.NoWheelComboBox",
    "QFontComboBox": "delegates.NoWheelFontComboBox",
    "QSpinBox": "delegates.NoWheelSpinBox",
    "QDoubleSpinBox": "delegates.NoWheelDoubleSpinBox",
    "QDateEdit": "delegates.make_date_edit",
}
_BARE_RE = re.compile(r"\b(" + "|".join(BARE) + r")\(")

#: The only lines allowed to name a bare Qt picker with a paren: the NoWheel
#: subclass declarations themselves, which must of course name their base class.
#: There is no other allowlist -- every other picker in the UI is a NoWheel one.
_ALLOWED_RE = re.compile(r"^class NoWheel\w+\(Q\w+\):")


def test_no_bare_wheel_sensitive_widget_in_mammon_ui():
    ui = pathlib.Path(__file__).resolve().parents[1] / "ui"
    offenders = []
    for path in sorted(ui.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ALLOWED_RE.match(line):
                continue
            match = _BARE_RE.search(line)
            if match:
                bare = match.group(1)
                offenders.append(
                    f"mammon/ui/{path.name}:{lineno} instantiates a bare {bare} -- "
                    f"use {BARE[bare]} instead, so a mouse wheel over it cannot "
                    f"change the value: {line.strip()}")
    assert not offenders, (
        "Wheel-sensitive widgets must come from mammon.ui.delegates so scrolling "
        "a page cannot change a selection:\n  " + "\n  ".join(offenders))


def test_the_nowheel_classes_ignore_rather_than_swallow(app):
    """Pin the semantics the guard is protecting: ignore, never accept."""
    for factory in (delegates.NoWheelComboBox, delegates.NoWheelSpinBox,
                    delegates.NoWheelDoubleSpinBox, delegates.NoWheelFontComboBox,
                    delegates.NoWheelDateEdit):
        widget = factory()
        try:
            assert send_wheel(widget) is False, f"{factory.__name__} accepted a wheel"
        finally:
            widget.deleteLater()
