"""Tests for mammon.ui.report_filters -- the ONE report period dropdown and its
resolver.

These lock the property the report-unification work broke and then restored: the
Period dropdown offers the UNION of the presets it has ever shown. An earlier
change replaced the original calendar ranges (This/Last Month, This/Last Year,
Year-to-Date) with only the newer rolling ranges (rolling 7/30-day windows,
rolling 12 months, this/last quarter, earliest to date). Both sets must remain
reachable, every option must resolve to a concrete range, and no previously
supported option may quietly disappear again.
"""
import os
import datetime as _dt

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mammon import db, ledger
from mammon.ui.report_filters import (
    PERIOD_PRESETS, PERIOD_DEFAULT, NET_WORTH_PERIOD_DEFAULT, resolve_period,
)

# The two families the dropdown unifies. Neither may be dropped from the option
# set again -- this is the regression the union restores.
_ROLLING_KEYS = {
    "last_7_days", "last_30_days", "last_12_months",
    "this_quarter", "last_quarter", "earliest",
}
_CALENDAR_KEYS = {
    "this_month", "last_month", "this_year", "last_year", "ytd",
}


def _keys():
    return [key for _label, key in PERIOD_PRESETS]


def test_period_presets_are_the_union_of_both_families():
    """Every rolling AND every calendar preset is present, plus 'custom'."""
    keys = set(_keys())
    missing_rolling = _ROLLING_KEYS - keys
    missing_calendar = _CALENDAR_KEYS - keys
    assert not missing_rolling, f"rolling presets dropped: {missing_rolling}"
    assert not missing_calendar, f"calendar presets dropped: {missing_calendar}"
    assert "custom" in keys


def test_custom_is_last_and_unique():
    """'Custom' (which opens the gear dialog) is the final option, and appears
    exactly once -- the union de-duplicates the shared entry."""
    keys = _keys()
    assert keys[-1] == "custom"
    assert keys.count("custom") == 1


def test_no_duplicate_keys():
    keys = _keys()
    assert len(keys) == len(set(keys))


def test_default_is_present_and_ytd():
    """Report and chart windows default to Year-to-Date; Net Worth Over Time is
    the one exception and keeps the whole-ledger 'earliest' default. Both must be
    real preset keys, or a window would open with nothing selected."""
    assert PERIOD_DEFAULT == "ytd"
    assert PERIOD_DEFAULT in _keys()
    assert NET_WORTH_PERIOD_DEFAULT == "earliest"
    assert NET_WORTH_PERIOD_DEFAULT in _keys()


def test_resolve_every_option_yields_a_concrete_range(tmp_path):
    """resolve_period turns every dropdown key -- both families -- into an
    inclusive (start, end) ISO range, except 'custom' which defers to the gear
    dialog and returns None."""
    conn = db.init_db(tmp_path / "rf.db")
    try:
        acct = ledger.create_account(conn, "Checking", "asset")
        ledger.add_transaction(conn, account_id=acct, date="2020-01-15",
                               amount=10000, payee="Opening")
        ledger.add_transaction(conn, account_id=acct, date="2024-06-30",
                               amount=-2500, payee="Later")
        today = _dt.date(2024, 9, 3)
        for _label, key in PERIOD_PRESETS:
            rng = resolve_period(key, conn, today)
            if key == "custom":
                assert rng is None, "custom must defer to the gear dialog"
                continue
            assert rng is not None, f"{key} did not resolve"
            start, end = rng
            _dt.date.fromisoformat(start)   # valid ISO dates
            _dt.date.fromisoformat(end)
            assert start <= end, f"{key} produced an inverted range {rng}"
    finally:
        conn.close()


def test_earliest_spans_the_whole_ledger(tmp_path):
    """'earliest' starts at the ledger's first transaction and reaches at least
    'today', so a freshly opened report covers everything the ledger holds."""
    conn = db.init_db(tmp_path / "rf.db")
    try:
        acct = ledger.create_account(conn, "Checking", "asset")
        ledger.add_transaction(conn, account_id=acct, date="2001-03-04",
                               amount=100, payee="Old")
        today = _dt.date(2024, 9, 3)
        start, end = resolve_period("earliest", conn, today)
        assert start == "2001-03-04"
        assert end >= "2024-09-03"
    finally:
        conn.close()


def test_earliest_on_empty_ledger_falls_back(tmp_path):
    """With no transactions there are no bounds; 'earliest' falls back to a
    concrete recent window rather than crashing or returning None."""
    conn = db.init_db(tmp_path / "rf.db")
    try:
        today = _dt.date(2024, 9, 3)
        rng = resolve_period("earliest", conn, today)
        assert rng is not None
        start, end = rng
        _dt.date.fromisoformat(start)
        _dt.date.fromisoformat(end)
        assert start <= end
    finally:
        conn.close()
