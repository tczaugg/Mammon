"""BUG 3: NewAccountDialog's opening-date field honors the date-format preference
and round-trips to ISO (or None when blank).

It used to be a bare QLineEdit with a hardcoded 'YYYY-MM-DD' placeholder read raw,
so choosing a non-ISO display format changed every other date field in the app but
not this one, and any typed date had to be ISO. It now uses
ui.delegates.make_date_edit / date_edit_iso like ReconcileStartDialog: the DISPLAY
honors the setting while the stored/returned value stays ISO YYYY-MM-DD (or None).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon.ui import prefs
from mammon.ui.models import fmt_date, qt_date_format


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def date_pref():
    """Run under a non-ISO, non-default display format; restore it afterward so the
    preference change never leaks to other tests."""
    saved = prefs.date_format()
    prefs.set_date_format("DD/MM/YYYY")
    yield
    prefs.set_date_format(saved)


def test_new_account_date_defaults_to_blank_none(qapp, date_pref):
    from mammon.ui.widgets import NewAccountDialog
    dlg = NewAccountDialog()
    try:
        # Optional field left untouched -> None, NOT today's date.
        assert dlg.values()["opening_date"] is None
    finally:
        dlg.deleteLater()


def test_new_account_date_display_honors_pref_but_returns_iso(qapp, date_pref):
    from PyQt5.QtCore import QDate
    from mammon.ui.widgets import NewAccountDialog
    dlg = NewAccountDialog()
    try:
        edit = dlg.opening_date
        # A real date editor rendered in the chosen format, not a raw ISO line edit.
        assert edit.displayFormat() == qt_date_format("DD/MM/YYYY")
        edit.setDate(QDate(2025, 1, 15))
        shown = edit.date().toString(edit.displayFormat())
        assert shown == fmt_date("2025-01-15") == "15/01/2025"
        assert shown != "2025-01-15"                     # NOT the stored ISO string
        # Stored/returned value is ISO regardless of the display format.
        assert dlg.values()["opening_date"] == "2025-01-15"
    finally:
        dlg.deleteLater()


def test_new_account_date_roundtrips_under_us_format(qapp):
    """The default US display format also stores ISO (guards the general path, not
    just the DD/MM branch)."""
    from PyQt5.QtCore import QDate
    from mammon.ui.widgets import NewAccountDialog
    saved = prefs.date_format()
    prefs.set_date_format("MM/DD/YYYY")
    dlg = NewAccountDialog()
    try:
        dlg.opening_date.setDate(QDate(2025, 1, 15))
        assert dlg.opening_date.date().toString(dlg.opening_date.displayFormat()) \
            == "01/15/2025"
        assert dlg.values()["opening_date"] == "2025-01-15"
    finally:
        dlg.deleteLater()
        prefs.set_date_format(saved)
