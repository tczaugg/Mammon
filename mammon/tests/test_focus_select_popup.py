"""Regression: typing a brand-new payee (or category) must not lose characters.

USER SYMPTOM: typing in the register's Payee field, after two or three characters
the typed text turns blue (selected) and the next keystroke replaces everything
typed so far -- keyboard-only, worst when entering a name the register has never
seen.

ROOT CAUSE: when the typed prefix matches no known payee, ``QCompleter`` hides its
popup and Qt then delivers a FocusIn carrying ``Qt.PopupFocusReason`` back to the
line edit. ``select_all_on_focus`` used to blacklist only the mouse, so it
select-all'd on that popup-close FocusIn; the next keystroke replaced the prefix.
The fix is a WHITELIST of reasons that genuinely mean the editor just opened
(``delegates._FRESH_OPEN``); ``PopupFocusReason`` is not one, so the caret and
selection are left alone and typing APPENDS.

The line edit under test is :class:`_FocusSelectLineEdit` -- shared by the Payee
editor and, through :class:`CategoryLineEdit`, by the Category editor -- so both
paths are exercised here. Under the offscreen platform the popup's own show/hide
focus round-trip is not reliably delivered, so after driving the real completer
with typed keys we deliver the ``PopupFocusReason`` FocusIn exactly as Qt does when
the popup closes, then assert the selection is gone and one more key appends.

Payee/category names here are SYNTHETIC (no PII): the repo is going open source.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon.ui import delegates


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _deliver_focus(widget, reason):
    """Deliver a FocusIn with a specific reason, as Qt would when an editor opens
    (TabFocusReason) or when a completer popup closes (PopupFocusReason)."""
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QFocusEvent
    widget.focusInEvent(QFocusEvent(QEvent.FocusIn, reason))


# ---------------------------------------------------------------------------
# Payee editor: _FocusSelectLineEdit + PayeeCompleter
# ---------------------------------------------------------------------------
def test_payee_never_seen_prefix_appends_after_popup_closes(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    le = delegates._FocusSelectLineEdit()
    le.setCompleter(delegates.PayeeCompleter(["Costco Gas", "Comcast"], le))

    # The editor opens fresh (Tab) -- empty text, so select-all highlights nothing.
    _deliver_focus(le, Qt.TabFocusReason)
    # Type a prefix that runs past the last matching payee: 'c'/'co' match, then
    # 'coz'/'cozz' match nothing, so the completer hides its popup.
    QTest.keyClicks(le, "cozz")
    assert le.text() == "cozz"

    # Qt delivers this when the empty popup closes; the fix must NOT re-select.
    _deliver_focus(le, Qt.PopupFocusReason)
    assert le.selectedText() == ""             # nothing highlighted -> no clobber

    # One further character APPENDS rather than replacing everything typed.
    QTest.keyClicks(le, "y")
    assert le.text() == "cozzy"                 # all chars survive, not just "y"


# ---------------------------------------------------------------------------
# Category editor: CategoryLineEdit (the same base class, via make_category_combo)
# ---------------------------------------------------------------------------
def test_category_never_seen_prefix_appends_after_popup_closes(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    combo = delegates.make_category_combo(None, ["Groceries", "Auto:Fuel"])
    le = combo.lineEdit()
    assert isinstance(le, delegates.CategoryLineEdit)

    _deliver_focus(le, Qt.TabFocusReason)
    # 'gro' matches "Groceries"; 'groz'/'grozz' match nothing -> popup closes.
    QTest.keyClicks(le, "grozz")
    assert le.text() == "grozz"

    _deliver_focus(le, Qt.PopupFocusReason)
    assert le.selectedText() == ""

    QTest.keyClicks(le, "y")
    assert le.text() == "grozzy"
