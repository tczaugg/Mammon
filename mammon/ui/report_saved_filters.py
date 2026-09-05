"""Named saved filter sets for the report window (QSettings-backed).

Persists named :class:`~mammon.ui.report_filters.ReportFilterBar` states -- the
date range plus any account/category selection and the include-hidden toggle --
so a user can recall a report configuration by name instead of re-ticking a
ninety-account check-list every time.

Following the :mod:`mammon.ui.prefs` convention, these are UI DISPLAY
preferences: they live in QSettings (an INI file in the user's scope), **never**
in the ledger database. There is no schema change and nothing is written into
the user's ``mammon.db``.

Two layers, deliberately split so the interesting part is testable without Qt's
settings machinery:

- **Pure serializers.** :func:`filter_state_to_dict` and
  :func:`apply_filter_state` read/write a live filter bar's widget state to/from
  a plain, JSON-able dict. No QSettings, no I/O, no SQL, no money logic -- just a
  projection of which dates/accounts/categories are selected. The round-trip is
  unit-testable on a bare bar.

- **Storage.** The whole name -> state mapping is kept as ONE JSON blob under a
  single QSettings key. Storing it as one value (rather than one key per name)
  sidesteps QSettings' treatment of ``/`` in a key as a group separator, so a
  filter-set name may contain any character the user likes.
"""
from __future__ import annotations

import json
from typing import Optional

from PyQt5.QtCore import Qt, QSettings

_ORG = "Mammon"
_APP = "Mammon"

# One key holds the entire {name: state} mapping as a JSON string. See the module
# docstring for why this is one blob rather than a key per name.
_FILTERS_KEY = "reports/saved_filters"


def _settings(settings: QSettings | None = None) -> QSettings:
    """The shared QSettings store (IniFormat/UserScope so tests can redirect it
    with ``QSettings.setPath``). Pass an explicit store to override (testing)."""
    if settings is not None:
        return settings
    return QSettings(QSettings.IniFormat, QSettings.UserScope, _ORG, _APP)


# ---------------------------------------------------------------------------
# Pure serializers (no QSettings, no I/O) -- the unit-testable core.
# ---------------------------------------------------------------------------
def filter_state_to_dict(bar) -> dict:
    """Capture a :class:`ReportFilterBar`'s current state as a JSON-able dict.

    Reads only the bar's public getters, so it stays a thin projection with no
    SQL and no money arithmetic. ``account_ids`` and ``categories`` are ``None``
    when the picker means "no filter" (every item checked), exactly mirroring the
    getters. Category names are sorted so the serialization is stable and two
    equal selections compare equal.
    """
    cats = bar.selected_categories()
    return {
        "start": bar.start_iso(),
        "end": bar.end_iso(),
        "include_hidden": bool(bar.include_hidden()),
        "account_ids": bar.selected_account_ids(),
        "categories": None if cats is None else sorted(cats),
    }


def apply_filter_state(bar, state) -> None:
    """Re-apply a captured ``state`` (see :func:`filter_state_to_dict`) to ``bar``.

    The inverse of the capture: set the date range, then the include-hidden
    toggle, then the account and category check-lists. The hidden toggle is
    applied **before** the account ticks because flipping it rebuilds the account
    list (preserving surviving ticks by id) -- setting account checks first would
    lose them. A key absent from ``state`` is left untouched; ``None`` for a list
    means "check everything" (the no-filter state). Falsy ``state`` is a no-op.
    """
    if not state:
        return
    start, end = state.get("start"), state.get("end")
    if start and end:
        bar.set_range(start, end)
    if bar.hidden_check is not None and "include_hidden" in state:
        bar.hidden_check.setChecked(bool(state.get("include_hidden")))
    if "account_ids" in state:
        _apply_account_ids(bar, state.get("account_ids"))
    if "categories" in state:
        _apply_categories(bar, state.get("categories"))


# Accept ``from_dict`` as an alias so callers can spell the inverse either way.
from_dict = apply_filter_state


def _apply_account_ids(bar, ids) -> None:
    lst = bar.account_list
    if lst is None:
        return
    if ids is None:
        bar.mark_accounts()
        return
    wanted = {int(i) for i in ids}
    for i in range(lst.count()):
        it = lst.item(i)
        it.setCheckState(
            Qt.Checked if int(it.data(Qt.UserRole)) in wanted else Qt.Unchecked)


def _apply_categories(bar, names) -> None:
    lst = bar.category_list
    if lst is None:
        return
    if names is None:
        bar.mark_categories()
        return
    wanted = set(names)
    for i in range(lst.count()):
        it = lst.item(i)
        it.setCheckState(Qt.Checked if it.text() in wanted else Qt.Unchecked)


# ---------------------------------------------------------------------------
# QSettings persistence (the {name: state} blob).
# ---------------------------------------------------------------------------
def _load_all(settings: QSettings | None = None) -> dict:
    """The whole {name: state} mapping (empty dict if unset or corrupt)."""
    raw = _settings(settings).value(_FILTERS_KEY, None)
    if raw is None:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _store_all(mapping: dict, settings: QSettings | None = None) -> None:
    s = _settings(settings)
    s.setValue(_FILTERS_KEY, json.dumps(mapping))
    s.sync()


def saved_filter_names(settings: QSettings | None = None) -> list[str]:
    """Every saved filter-set name, sorted case-insensitively (may be empty)."""
    return sorted(_load_all(settings).keys(), key=lambda n: n.lower())


def save_filter_set(name, state, settings: QSettings | None = None) -> None:
    """Persist ``state`` (a :func:`filter_state_to_dict` result) under ``name``.

    A blank name is ignored; an existing name is overwritten so re-saving under
    the same name updates it in place.
    """
    name = (name or "").strip()
    if not name:
        return
    mapping = _load_all(settings)
    mapping[name] = state
    _store_all(mapping, settings)


def load_filter_set(name, settings: QSettings | None = None) -> Optional[dict]:
    """The saved state for ``name``, or ``None`` when there is no such set."""
    name = (name or "").strip()
    if not name:
        return None
    val = _load_all(settings).get(name)
    return val if isinstance(val, dict) else None


def delete_filter_set(name, settings: QSettings | None = None) -> None:
    """Remove the saved filter set ``name`` (a no-op if it does not exist)."""
    name = (name or "").strip()
    if not name:
        return
    mapping = _load_all(settings)
    if name in mapping:
        del mapping[name]
        _store_all(mapping, settings)
