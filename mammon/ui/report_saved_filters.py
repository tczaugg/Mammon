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
    getters. Every list is sorted so the serialization is stable and two equal
    selections compare equal.

    ``category_ids`` is the authoritative half and the only one
    :func:`apply_filter_state` reads when it has any: `ledger.rename_category`
    keeps the id, so a name-keyed set silently drops a category the moment the
    user renames it, and ids are also the only spelling that can say "Taxes:
    Federal but not Taxes:Property". ``categories`` -- the top-level NAMES -- is
    still written beside it so a set stays readable by anything that only
    understands names, and so a set written HERE still loads in a build that
    predates the id-keyed picker.

    The ``category_ids`` key is omitted entirely when the picker is in its
    no-filter state: there is nothing to key by id, and a no-filter set then
    serializes byte-for-byte as it did before the tree existed.
    """
    cats = bar.selected_categories()
    ids = bar.selected_category_ids()
    state = {
        "start": bar.start_iso(),
        "end": bar.end_iso(),
        "include_hidden": bool(bar.include_hidden()),
        "account_ids": bar.selected_account_ids(),
        "categories": None if cats is None else sorted(cats),
    }
    if ids is not None:
        state["category_ids"] = sorted(ids)
    return state


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
    # Ids win when the set has them; a set written before the picker went
    # id-keyed (or one whose names are not real categories) falls back to
    # resolving the NAMES against the tree, which is the back-compat path.
    ids = state.get("category_ids")
    if ids:
        _apply_category_ids(bar, ids)
    elif state.get("categories") is not None:
        _apply_categories(bar, state["categories"])
    elif "category_ids" in state or "categories" in state:
        _apply_category_ids(bar, None)


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


def _apply_category_ids(bar, ids) -> None:
    if bar.category_list is None:
        return
    if ids is None:
        bar.mark_categories()
        return
    bar.set_selected_category_ids(ids)


def _apply_categories(bar, names) -> None:
    """The legacy NAME-keyed path: tick the top-level rows so named together with
    their whole subtrees -- which is what ticking a top level has always meant --
    and leave everything else clear.

    Matching on NAME here, rather than resolving the names to ids first, is
    deliberate: a bar built with an explicit ``categories=`` list may be showing
    synthesized buckets that are not rows in ``categories`` at all, and resolving
    those through ids would tick nothing. The state left behind is still id-keyed
    -- every later read goes through
    :meth:`ReportFilterBar.selected_category_ids` -- so a set loaded this way is
    immune to the next rename even though it arrived as names."""
    if bar.category_list is None:
        return
    if names is None:
        bar.mark_categories()
        return
    bar.set_selected_category_names(names)


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
