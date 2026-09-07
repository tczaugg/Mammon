"""Per-tag colors: storage, domain accessors, split-leg round-trip, and the UI
surfaces that render a tag's identity color (register cell, split dialog, By Tag
report).

A tag's color (``tags.color``, added in migration 54) is per-tag IDENTITY:
``mammon.ledger`` is the sole writer, and the register / split dialog / By Tag
report all read it through the single ``ledger.tag_colors`` accessor -- so a tag
keeps ONE color everywhere instead of color following a chart slice's rank. A
split leg's own single tag round-trips through ``ledger.set_splits`` (so editing a
split no longer drops a per-leg tag an import set), and the register cell shows the
UNION of a row's own tags and its legs' tags without double-counting. Synthetic
data only -- no PII.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "tagcolors.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


# ---- schema: the color column exists (committed in migration 54) ----------
def test_tags_color_column_exists(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tags)")}
    assert "color" in cols


# ---- normalize_tag_color --------------------------------------------------
def test_normalize_accepts_hex_and_lowercases():
    assert ledger.normalize_tag_color("#AABBCC") == "#aabbcc"
    assert ledger.normalize_tag_color("  #123abc ") == "#123abc"


def test_normalize_blank_and_none_clear():
    assert ledger.normalize_tag_color(None) is None
    assert ledger.normalize_tag_color("") is None
    assert ledger.normalize_tag_color("   ") is None


@pytest.mark.parametrize("bad", ["red", "#abc", "#12345g", "112233", "#1234567"])
def test_normalize_rejects_non_hex(bad):
    with pytest.raises(ValueError):
        ledger.normalize_tag_color(bad)


# ---- set / get / clear color + tag_colors map -----------------------------
def test_set_and_get_tag_color(conn):
    tid = ledger.tag_id(conn, "Vacation")
    ledger.set_tag_color(conn, tid, "#3B6EA5")
    assert ledger.get_tag(conn, tid)["color"] == "#3b6ea5"


def test_tag_colors_map_is_casefolded_and_only_colored(conn):
    v = ledger.tag_id(conn, "Vacation")
    ledger.tag_id(conn, "Reimbursable")            # no color
    ledger.set_tag_color(conn, v, "#112233")
    m = ledger.tag_colors(conn)
    assert m == {"vacation": "#112233"}
    assert "reimbursable" not in m


def test_clear_tag_color(conn):
    tid = ledger.tag_id(conn, "Vacation")
    ledger.set_tag_color(conn, tid, "#112233")
    ledger.set_tag_color(conn, tid, None)
    assert ledger.get_tag(conn, tid)["color"] is None
    assert ledger.tag_colors(conn) == {}


def test_set_color_rejects_bad_hex(conn):
    tid = ledger.tag_id(conn, "Vacation")
    with pytest.raises(ValueError):
        ledger.set_tag_color(conn, tid, "green")


def test_set_color_unknown_tag_raises(conn):
    with pytest.raises(KeyError):
        ledger.set_tag_color(conn, 999, "#112233")


# ---- list_tags ------------------------------------------------------------
def test_list_tags_includes_unused_with_usage_counts(conn, accounts):
    chk, _ = accounts
    ledger.tag_id(conn, "Groceries")
    ledger.tag_id(conn, "Spare")                    # never applied
    t = ledger.add_transaction(conn, chk, "2026-03-01", -50_00, payee="Store")
    ledger.set_tags(conn, t, "Groceries")
    rows = {r["name"]: r for r in ledger.list_tags(conn)}
    assert set(rows) == {"Groceries", "Spare"}
    assert rows["Groceries"]["usage"] == 1
    assert rows["Spare"]["usage"] == 0


# ---- rename_tag -----------------------------------------------------------
def test_rename_keeps_id_color_and_refreshes_cache(conn, accounts):
    chk, _ = accounts
    tid = ledger.tag_id(conn, "vac")
    ledger.set_tag_color(conn, tid, "#112233")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -50_00, payee="Store")
    ledger.set_tags(conn, t, "vac")
    ledger.rename_tag(conn, tid, "Vacation")
    assert ledger.get_tag(conn, tid)["name"] == "Vacation"     # same id
    assert ledger.get_tag(conn, tid)["color"] == "#112233"     # color survived
    # transactions.tag cache rebuilt to the new spelling
    assert ledger.get_transaction(conn, t)["tag"] == "Vacation"
    assert ledger.get_tags(conn, t) == ["Vacation"]


def test_rename_rejects_blank_comma_and_collision(conn):
    a = ledger.tag_id(conn, "Home")
    ledger.tag_id(conn, "Work")
    with pytest.raises(ValueError):
        ledger.rename_tag(conn, a, "  ")
    with pytest.raises(ValueError):
        ledger.rename_tag(conn, a, "a,b")
    with pytest.raises(ValueError):
        ledger.rename_tag(conn, a, "work")          # case-insensitive collision


def test_rename_allows_own_case_variant(conn):
    a = ledger.tag_id(conn, "home")
    ledger.rename_tag(conn, a, "Home")              # same tag, new case: OK
    assert ledger.get_tag(conn, a)["name"] == "Home"


# ---- delete_tag -----------------------------------------------------------
def test_delete_removes_junction_and_refreshes_cache(conn, accounts):
    chk, _ = accounts
    tid = ledger.tag_id(conn, "Doomed")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -50_00, payee="Store")
    ledger.set_tags(conn, t, "Doomed, Keep")
    ledger.delete_tag(conn, tid)
    assert ledger.get_tag(conn, tid) is None
    assert ledger.get_tags(conn, t) == ["Keep"]
    assert ledger.get_transaction(conn, t)["tag"] == "Keep"


def test_delete_nulls_split_leg_tag(conn, accounts):
    chk, _ = accounts
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    food = ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    ledger.set_splits(conn, t, [
        {"category_id": fuel, "amount": -60_00, "tag": "Trip"},
        {"category_id": food, "amount": -40_00},
    ])
    trip = ledger.tag_id(conn, "Trip")
    ledger.delete_tag(conn, trip)
    assert all(s["tag_id"] is None for s in ledger.get_splits(conn, t))


# ---- split leg tag round-trip through set_splits --------------------------
def test_set_splits_preserves_leg_tag_by_name(conn, accounts):
    chk, _ = accounts
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    food = ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    ledger.set_splits(conn, t, [
        {"category_id": fuel, "amount": -60_00, "tag": "Trip"},
        {"category_id": food, "amount": -40_00},
    ])
    legs = {s["category_id"]: s for s in ledger.get_splits(conn, t)}
    assert legs[fuel]["tag"] == "Trip"
    assert legs[food]["tag"] == ""


def test_set_splits_preserves_leg_tag_by_id(conn, accounts):
    chk, _ = accounts
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    food = ledger.resolve_category(conn, "Groceries")
    trip = ledger.tag_id(conn, "Trip")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    ledger.set_splits(conn, t, [
        {"category_id": fuel, "amount": -60_00, "tag_id": trip},
        {"category_id": food, "amount": -40_00},
    ])
    legs = {s["category_id"]: s for s in ledger.get_splits(conn, t)}
    assert legs[fuel]["tag_id"] == trip


def test_rebalance_splits_keeps_leg_tag(conn, accounts):
    chk, _ = accounts
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    food = ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    ledger.set_splits(conn, t, [
        {"category_id": fuel, "amount": -60_00, "tag": "Trip"},
        {"category_id": food, "amount": -40_00},
    ])
    # Changing the total rebalances via an uncategorized line; the tagged leg must
    # keep its tag across the delete+recreate rebuild.
    ledger.set_transaction_amount(conn, t, -120_00)
    tagged = [s["tag"] for s in ledger.get_splits(conn, t) if s["tag"]]
    assert tagged == ["Trip"]


# ---- split_leg_tags_by_txn ------------------------------------------------
def test_split_leg_tags_by_txn(conn, accounts):
    chk, _ = accounts
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    food = ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    ledger.set_splits(conn, t, [
        {"category_id": fuel, "amount": -60_00, "tag": "Trip"},
        {"category_id": food, "amount": -40_00, "tag": "Snacks"},
    ])
    assert ledger.split_leg_tags_by_txn(conn, chk)[t] == ["Trip", "Snacks"]


# ---- model TAG_COLORS_ROLE (register cell chips) --------------------------
def test_model_tag_swatches_map_row_tags_to_colors(qapp, conn, accounts):
    from mammon.ui.models import RegisterModel
    chk, _ = accounts
    v = ledger.tag_id(conn, "Vacation")
    ledger.set_tag_color(conn, v, "#112233")
    ledger.tag_id(conn, "Plain")                    # no color
    t = ledger.add_transaction(conn, chk, "2026-03-01", -50_00, payee="Store")
    ledger.set_tags(conn, t, "Vacation, Plain")
    model = RegisterModel(conn, chk)
    idx = model.index(model.row_for_txn(t), RegisterModel.TAG)
    swatches = model.data(idx, RegisterModel.TAG_COLORS_ROLE)
    assert swatches == [("Vacation", "#112233"), ("Plain", None)]


def test_model_tag_swatches_union_split_leg_colors(qapp, conn, accounts):
    from mammon.ui.models import RegisterModel
    chk, _ = accounts
    trip = ledger.tag_id(conn, "Trip")
    ledger.set_tag_color(conn, trip, "#aa0000")
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    food = ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    ledger.set_splits(conn, t, [
        {"category_id": fuel, "amount": -60_00, "tag": "Trip"},
        {"category_id": food, "amount": -40_00},
    ])
    model = RegisterModel(conn, chk)
    idx = model.index(model.row_for_txn(t), RegisterModel.TAG)
    swatches = model.data(idx, RegisterModel.TAG_COLORS_ROLE)
    # The split's per-leg tag surfaces on the collapsed parent row.
    assert ("Trip", "#aa0000") in swatches


def test_model_tag_swatches_dedup_parent_and_leg(qapp, conn, accounts):
    from mammon.ui.models import RegisterModel
    chk, _ = accounts
    trip = ledger.tag_id(conn, "Trip")
    ledger.set_tag_color(conn, trip, "#aa0000")
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    food = ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    ledger.set_tags(conn, t, "Trip")                # row-level Trip
    ledger.set_splits(conn, t, [                    # leg-level Trip too
        {"category_id": fuel, "amount": -60_00, "tag": "Trip"},
        {"category_id": food, "amount": -40_00},
    ])
    model = RegisterModel(conn, chk)
    idx = model.index(model.row_for_txn(t), RegisterModel.TAG)
    swatches = model.data(idx, RegisterModel.TAG_COLORS_ROLE)
    assert [s for s in swatches if s[0] == "Trip"] == [("Trip", "#aa0000")]  # once


# ---- split dialog: a tagged leg's row is tinted ---------------------------
def test_split_dialog_tints_tagged_leg_row(qapp, conn, accounts):
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog
    chk, _ = accounts
    trip = ledger.tag_id(conn, "Trip")
    ledger.set_tag_color(conn, trip, "#aa0000")
    fuel = ledger.resolve_category(conn, "Auto:Fuel")
    food = ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    ledger.set_splits(conn, t, [
        {"category_id": fuel, "amount": -60_00, "tag": "Trip"},
        {"category_id": food, "amount": -40_00},
    ])
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    try:
        tagged = [e for e in dlg._lines if e["tag"].text() == "Trip"]
        assert tagged, "the tagged leg was seeded"
        assert "#aa0000" in tagged[0]["frame"].styleSheet()
        untagged = [e for e in dlg._lines if not e["tag"].text()]
        assert untagged and untagged[0]["frame"].styleSheet() == ""
    finally:
        dlg.deleteLater()


def test_split_dialog_typing_tag_recolors_and_saves(qapp, conn, accounts):
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog
    chk, _ = accounts
    proj = ledger.tag_id(conn, "Project")
    ledger.set_tag_color(conn, proj, "#00aa00")
    ledger.resolve_category(conn, "Auto:Fuel")
    ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-03-01", -100_00, payee="Store")
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    try:
        dlg._lines[0]["cat"].setCurrentText("Auto:Fuel")
        dlg._lines[0]["amount"].setValue(-60.00)
        dlg._lines[0]["tag"].setText("Project")     # fires _recolor_line
        assert "#00aa00" in dlg._lines[0]["frame"].styleSheet()
        dlg._lines[1]["cat"].setCurrentText("Groceries")
        dlg._lines[1]["amount"].setValue(-40.00)
        lines = dlg.lines_cents()
    finally:
        dlg.deleteLater()
    ledger.set_splits(conn, t, lines)
    legs = {s["category_label"]: s for s in ledger.get_splits(conn, t)}
    assert legs["Auto:Fuel"]["tag"] == "Project"


# ---- By Tag report: rows colored by tag color -----------------------------
def test_by_tag_report_colors_tag_rows(qapp, conn, accounts):
    from mammon.ui.report_window import BY_TAG_SPEC, ReportWindow
    chk, _ = accounts
    trip = ledger.tag_id(conn, "Trip")
    ledger.set_tag_color(conn, trip, "#aa0000")
    ledger.tag_id(conn, "Plain")
    t1 = ledger.add_transaction(conn, chk, "2026-03-01", -60_00, payee="A")
    ledger.set_tags(conn, t1, "Trip")
    t2 = ledger.add_transaction(conn, chk, "2026-03-02", -40_00, payee="B")
    ledger.set_tags(conn, t2, "Plain")
    win = ReportWindow(conn, spec=BY_TAG_SPEC)
    try:
        win.filters.set_range("2026-01-01", "2026-12-31")
        win.refresh()
        labels = {win.table.item(i, 1).text(): i
                  for i in range(win.table.rowCount())}
        assert "Trip" in labels and "Plain" in labels
        assert not win.table.item(labels["Trip"], 1).icon().isNull()   # colored
        assert win.table.item(labels["Plain"], 1).icon().isNull()       # uncolored
        if "Total" in labels:
            assert win.table.item(labels["Total"], 1).icon().isNull()
    finally:
        win.close()
