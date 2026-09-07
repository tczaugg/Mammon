"""BUG 2: a Tab/keyboard-focused register editor selects-all so typing REPLACES;
a caret-placing mouse click appends.

Before this the click-to-edit path select-alled (typing replaced) but a Tab into
the same field left the caret at the end (typing appended), so Tab and click
disagreed on a pending review row's Category / Payee / Tag / Memo fields. The
choice is now a pure function of the Qt focus reason
(:func:`mammon.ui.delegates.select_all_on_focus`), applied by the editors'
focus-in, so it is testable headless without real focus.
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


def _focus(widget, reason):
    """Deliver a focus-in with a specific reason, as Qt would when the editor is
    opened by Tab (TabFocusReason) or by a mouse click (MouseFocusReason)."""
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QFocusEvent
    widget.focusInEvent(QFocusEvent(QEvent.FocusIn, reason))


# ---------------------------------------------------------------------------
# the pure decision
# ---------------------------------------------------------------------------
def test_select_all_on_focus_is_true_for_every_reason_but_a_mouse_click():
    from PyQt5.QtCore import Qt
    assert delegates.select_all_on_focus(Qt.TabFocusReason) is True
    assert delegates.select_all_on_focus(Qt.BacktabFocusReason) is True
    assert delegates.select_all_on_focus(Qt.ShortcutFocusReason) is True
    assert delegates.select_all_on_focus(Qt.OtherFocusReason) is True
    # The ONE case that appends: a click that places the caret with no selection.
    assert delegates.select_all_on_focus(Qt.MouseFocusReason) is False


# ---------------------------------------------------------------------------
# the shared Payee / Memo / Tag editor
# ---------------------------------------------------------------------------
def test_focus_select_line_edit_tab_replaces(qapp):
    from PyQt5.QtCore import Qt
    le = delegates._FocusSelectLineEdit()
    le.setText("August rent")
    _focus(le, Qt.TabFocusReason)
    assert le.hasSelectedText() is True
    assert le.selectedText() == "August rent"          # all highlighted
    le.insert("X")                                       # a keystroke over it...
    assert le.text() == "X"                              # ...REPLACES


def test_focus_select_line_edit_click_appends(qapp):
    from PyQt5.QtCore import Qt
    le = delegates._FocusSelectLineEdit()
    le.setText("August rent")
    _focus(le, Qt.MouseFocusReason)
    assert le.hasSelectedText() is False                 # nothing highlighted
    assert le.cursorPosition() == len("August rent")     # caret at the end
    le.insert("X")
    assert le.text() == "August rentX"                   # ...APPENDS


# ---------------------------------------------------------------------------
# the Category editor (CategoryLineEdit, inside make_category_combo)
# ---------------------------------------------------------------------------
def test_category_editor_tab_replaces(qapp):
    from PyQt5.QtCore import Qt
    combo = delegates.make_category_combo(None, ["Auto:Fuel", "Groceries"])
    le = combo.lineEdit()
    assert isinstance(le, delegates.CategoryLineEdit)
    le.setText("Groceries")
    _focus(le, Qt.TabFocusReason)
    assert le.hasSelectedText() is True
    assert le.selectedText() == "Groceries"
    le.insert("A")
    assert le.text() == "A"                               # replaces


def test_category_editor_click_appends(qapp):
    from PyQt5.QtCore import Qt
    combo = delegates.make_category_combo(None, ["Auto:Fuel", "Groceries"])
    le = combo.lineEdit()
    le.setText("Groceries")
    _focus(le, Qt.MouseFocusReason)
    assert le.hasSelectedText() is False
    assert le.cursorPosition() == len("Groceries")
    le.insert("A")
    assert le.text() == "GroceriesA"                      # appends


# ---------------------------------------------------------------------------
# wiring: the two-line delegate builds focus-aware editors for Memo/Tag
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["memo", "tag"])
def test_two_line_memo_tag_editor_is_focus_aware(qapp, field):
    from PyQt5.QtCore import QModelIndex
    from PyQt5.QtWidgets import QStyleOptionViewItem
    d = delegates.PayeeTwoLineDelegate()
    d.two_line = True
    d._active_field = field
    editor = d.createEditor(None, QStyleOptionViewItem(), QModelIndex())
    assert isinstance(editor, delegates._FocusSelectLineEdit)
