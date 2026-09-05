"""Named saved filter sets for the report window (roadmap item 9).

Two halves, matching the module split:

* the PURE serializers ``filter_state_to_dict`` / ``apply_filter_state`` --
  captured/re-applied on a bare :class:`ReportFilterBar` with no QSettings, so
  the round-trip is verified in isolation; and
* the QSettings-backed save/load/delete, exercised under a temporary settings
  scope (``QSettings.setPath``) so nothing touches the real user store -- and, as
  the whole point of the feature, never the ledger database.

Synthetic data only -- no PII. Offscreen-safe.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt, QSettings
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger
from mammon.ui.report_filters import ReportFilterBar
from mammon.ui.report_saved_filters import (
    apply_filter_state,
    delete_filter_set,
    filter_state_to_dict,
    from_dict,
    load_filter_set,
    save_filter_set,
    saved_filter_names,
)

CATEGORIES = ["Groceries", "Rent", "Salary"]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    """Redirect QSettings to a throwaway dir so save/load/delete never touch the
    real user store (and, by construction, never the ledger DB)."""
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "sf.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    a = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    b = ledger.create_account(conn, "Savings", "savings", opening_balance=500_00)
    return {"conn": conn, "a": a, "b": b}


def _bar(conn, start="2026-01-01", end="2026-03-31"):
    return ReportFilterBar(conn, start, end, show_accounts=True,
                           categories=CATEGORIES, show_hidden_toggle=True)


def _uncheck_account(bar, account_id):
    for i in range(bar.account_list.count()):
        it = bar.account_list.item(i)
        if int(it.data(Qt.UserRole)) == int(account_id):
            it.setCheckState(Qt.Unchecked)


def _uncheck_category(bar, name):
    for i in range(bar.category_list.count()):
        it = bar.category_list.item(i)
        if it.text() == name:
            it.setCheckState(Qt.Unchecked)


# ---- pure: serialize shape ------------------------------------------------

def test_all_checked_state_is_no_filter(qapp, world):
    """Every box checked -> account_ids / categories are None ('no filter'),
    mirroring the bar's getters."""
    bar = _bar(world["conn"])
    state = filter_state_to_dict(bar)
    assert state == {
        "start": "2026-01-01",
        "end": "2026-03-31",
        "include_hidden": False,
        "account_ids": None,
        "categories": None,
    }


def test_state_captures_partial_selection(qapp, world):
    bar = _bar(world["conn"])
    _uncheck_account(bar, world["b"])
    _uncheck_category(bar, "Rent")
    state = filter_state_to_dict(bar)
    assert state["account_ids"] == [world["a"]]
    # Categories are sorted for a stable serialization.
    assert state["categories"] == ["Groceries", "Salary"]


# ---- pure: round-trip -----------------------------------------------------

def test_round_trip_reproduces_partial_selection(qapp, world):
    conn = world["conn"]
    src = _bar(conn)
    _uncheck_account(src, world["b"])
    _uncheck_category(src, "Rent")
    src.hidden_check.setChecked(True)
    state = filter_state_to_dict(src)

    # A fresh bar with a different range and everything checked...
    dst = _bar(conn, start="2099-01-01", end="2099-12-31")
    apply_filter_state(dst, state)

    # ...ends up an exact match after re-applying the captured state.
    assert filter_state_to_dict(dst) == state
    assert dst.start_iso() == "2026-01-01"
    assert dst.end_iso() == "2026-03-31"
    assert dst.include_hidden() is True
    assert dst.selected_account_ids() == [world["a"]]
    assert dst.selected_categories() == {"Groceries", "Salary"}


def test_apply_none_marks_everything(qapp, world):
    """Applying a 'no filter' (None) state re-checks a previously narrowed bar."""
    conn = world["conn"]
    narrowed = _bar(conn)
    _uncheck_account(narrowed, world["b"])
    _uncheck_category(narrowed, "Salary")
    assert narrowed.selected_account_ids() is not None

    apply_filter_state(narrowed, {"account_ids": None, "categories": None})
    assert narrowed.selected_account_ids() is None
    assert narrowed.selected_categories() is None


def test_from_dict_is_apply_alias():
    assert from_dict is apply_filter_state


def test_apply_empty_state_is_noop(qapp, world):
    bar = _bar(world["conn"])
    before = filter_state_to_dict(bar)
    apply_filter_state(bar, None)
    apply_filter_state(bar, {})
    assert filter_state_to_dict(bar) == before


# ---- QSettings: save / load / delete --------------------------------------

def test_save_load_delete_round_trip():
    assert saved_filter_names() == []

    state = {
        "start": "2026-01-01", "end": "2026-03-31",
        "include_hidden": False, "account_ids": [1, 2],
        "categories": ["Groceries"],
    }
    save_filter_set("Q1 Groceries", state)

    assert saved_filter_names() == ["Q1 Groceries"]
    assert load_filter_set("Q1 Groceries") == state

    delete_filter_set("Q1 Groceries")
    assert saved_filter_names() == []
    assert load_filter_set("Q1 Groceries") is None


def test_names_sorted_case_insensitively():
    save_filter_set("zebra", {"start": "a", "end": "b"})
    save_filter_set("Apple", {"start": "c", "end": "d"})
    save_filter_set("mango", {"start": "e", "end": "f"})
    assert saved_filter_names() == ["Apple", "mango", "zebra"]


def test_resaving_same_name_overwrites():
    save_filter_set("View", {"start": "2020-01-01", "end": "2020-12-31"})
    save_filter_set("View", {"start": "2026-01-01", "end": "2026-12-31"})
    assert saved_filter_names() == ["View"]
    assert load_filter_set("View")["start"] == "2026-01-01"


def test_blank_and_missing_names_are_safe():
    save_filter_set("", {"start": "x"})
    save_filter_set("   ", {"start": "y"})
    assert saved_filter_names() == []
    assert load_filter_set("nope") is None
    # Deleting something absent must not raise.
    delete_filter_set("nope")
    delete_filter_set("")


def test_name_with_slash_survives():
    """One JSON blob (not a key per name) means a '/' in the name is data, not a
    QSettings group separator."""
    save_filter_set("2026/Q1", {"start": "2026-01-01", "end": "2026-03-31"})
    assert saved_filter_names() == ["2026/Q1"]
    assert load_filter_set("2026/Q1")["end"] == "2026-03-31"


# ---- window: end-to-end save / apply / delete -----------------------------

def _window(conn):
    from mammon.ui.report_window import ReportWindow
    return ReportWindow(conn)


def test_window_save_apply_delete(qapp, world):
    win = _window(world["conn"])
    try:
        # Only the blank placeholder to start.
        assert win.saved_combo.count() == 1
        default_start = win.filters.start_iso()

        assert win.save_current_filters("Default") is True
        assert "Default" in saved_filter_names()
        assert win.saved_combo.currentText() == "Default"

        # Move the range, then recall the saved set to restore it.
        win.filters.set_range("2099-01-01", "2099-01-02")
        assert win.filters.start_iso() == "2099-01-01"
        assert win.apply_saved_filters("Default") is True
        assert win.filters.start_iso() == default_start

        # Unknown set -> False, no change.
        assert win.apply_saved_filters("ghost") is False

        assert win.delete_saved_filters("Default") is True
        assert saved_filter_names() == []
        assert win.saved_combo.count() == 1
    finally:
        win.close()


def test_window_combo_selection_applies(qapp, world):
    win = _window(world["conn"])
    try:
        win.save_current_filters("Base")
        default_start = win.filters.start_iso()
        win.filters.set_range("2099-01-01", "2099-01-02")

        idx = win.saved_combo.findText("Base")
        win._on_saved_selected(idx)
        assert win.filters.start_iso() == default_start
    finally:
        win.close()


def test_window_save_dialog_seam_no_modal(qapp, world, monkeypatch):
    """The name prompt is an overridable seam; patching it drives the Save button
    handler without opening a modal QInputDialog."""
    win = _window(world["conn"])
    try:
        monkeypatch.setattr(win, "_prompt_filter_name", lambda: "FromSeam")
        win._save_filters_dialog()
        assert "FromSeam" in saved_filter_names()

        # A cancelled prompt (None) saves nothing.
        monkeypatch.setattr(win, "_prompt_filter_name", lambda: None)
        win._save_filters_dialog()
        assert saved_filter_names() == ["FromSeam"]
    finally:
        win.close()


def test_window_blank_name_saves_nothing(qapp, world):
    win = _window(world["conn"])
    try:
        assert win.save_current_filters("   ") is False
        assert saved_filter_names() == []
        assert win.delete_saved_filters("") is False
    finally:
        win.close()
