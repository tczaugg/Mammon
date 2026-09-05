"""First-class, many-per-transaction tags.

Tags are stored relationally (the ``tags`` table + the ``transaction_tags``
junction, the authoritative store) and mirrored as a normalized comma-joined
string in ``transactions.tag`` (a cache the register cell, Find and the report
line loader keep reading). ``mammon.ledger`` is the sole writer of both; the
register edits ONE comma-separated slot and the domain owns the parse/join.

These tests pin: the parse/format round-trip, the storage round-trip and cache,
multi-tag storage, NOCASE collapse, that a transfer's mirror is never tagged from
its other side, exact filter-by-tag (scoped), the by-tag report's per-tag fan-out,
delete cascade, the legacy-tag data migration, and the register's single-slot edit.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.reports import tags as tag_report


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "tags.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


# --------------------------------------------------------------------------
# Pure parse / format
# --------------------------------------------------------------------------
def test_parse_splits_on_comma_only_trims_and_drops_empties():
    # Splits on commas only, so an internal space keeps one tag whole.
    assert ledger.parse_tags(" Ski Trip , vacation ,, ") == ["Ski Trip", "vacation"]
    assert ledger.parse_tags("") == []
    assert ledger.parse_tags(None) == []
    assert ledger.parse_tags("   ") == []


def test_parse_dedups_case_insensitively_keeping_first_spelling():
    assert ledger.parse_tags("Home, home, HOME, work") == ["Home", "work"]


def test_parse_format_round_trip_is_stable():
    text = "vacation, Business, Ski Trip"
    names = ledger.parse_tags(text)
    assert ledger.format_tags(names) == text
    # Re-parsing the formatted string is a fixed point.
    assert ledger.parse_tags(ledger.format_tags(names)) == names


# --------------------------------------------------------------------------
# Storage round-trip + cache
# --------------------------------------------------------------------------
def test_set_tags_text_round_trips_and_normalizes_the_cache(conn, accounts):
    chk, _ = accounts
    t = ledger.add_transaction(conn, chk, "2026-01-01", -50_00, payee="REI")
    ledger.set_tags_text(conn, t, "  vacation,  Business ,vacation ")
    assert ledger.get_tags(conn, t) == ["vacation", "Business"]
    assert ledger.tags_text(conn, t) == "vacation, Business"
    # transactions.tag is the normalized comma-joined cache the register shows.
    assert ledger.get_transaction(conn, t)["tag"] == "vacation, Business"


def test_clearing_tags_nulls_the_cache_and_empties_the_junction(conn, accounts):
    chk, _ = accounts
    t = ledger.add_transaction(conn, chk, "2026-01-01", -50_00, payee="REI", tag="vacation")
    assert ledger.get_tags(conn, t) == ["vacation"]
    ledger.set_tags_text(conn, t, "")
    assert ledger.get_tags(conn, t) == []
    assert ledger.get_transaction(conn, t)["tag"] is None


def test_add_transaction_tag_kwarg_populates_the_junction(conn, accounts):
    chk, _ = accounts
    t = ledger.add_transaction(conn, chk, "2026-01-01", -50_00, payee="REI",
                               tag="vacation, business")
    assert ledger.get_tags(conn, t) == ["vacation", "business"]
    assert ledger.get_transaction(conn, t)["tag"] == "vacation, business"


def test_second_transaction_reuses_the_tag_row_and_canonical_spelling(conn, accounts):
    chk, _ = accounts
    a = ledger.add_transaction(conn, chk, "2026-01-01", -10_00, payee="A", tag="Vacation")
    b = ledger.add_transaction(conn, chk, "2026-01-02", -20_00, payee="B", tag="vacation")
    # One tag row (NOCASE), and the second row renders the canonical first spelling.
    rows = conn.execute("SELECT COUNT(*) AS n FROM tags").fetchone()
    assert rows["n"] == 1
    assert ledger.get_tags(conn, b) == ["Vacation"]


# --------------------------------------------------------------------------
# Transfer mirror unaffected
# --------------------------------------------------------------------------
def test_tags_are_per_leg_and_never_mirror_across_a_transfer(conn, accounts):
    chk, sav = accounts
    a, b = ledger.create_transfer(conn, chk, sav, "2026-01-15", 500_00, payee="Stash")
    ledger.set_tags_text(conn, a, "reimbursable, travel")
    assert ledger.get_tags(conn, a) == ["reimbursable", "travel"]
    # The mirror leg is untouched: no tags leaked across the pair.
    assert ledger.get_tags(conn, b) == []
    assert ledger.get_transaction(conn, b)["tag"] is None


def test_editing_a_transfer_amount_leaves_the_tagged_leg_intact(conn, accounts):
    chk, sav = accounts
    a, b = ledger.create_transfer(conn, chk, sav, "2026-01-15", 500_00, payee="Stash")
    ledger.set_tags_text(conn, a, "travel")
    # Editing money mirrors amount to the other leg but must not disturb tags.
    ledger.update_transaction(conn, a, amount=-600_00)
    assert ledger.get_tags(conn, a) == ["travel"]
    assert ledger.get_tags(conn, b) == []
    legs = {r["id"]: r["amount"] for r in (
        ledger.get_transaction(conn, a), ledger.get_transaction(conn, b))}
    assert legs[a] == -600_00 and legs[b] == 600_00   # cents invariant: equal & opposite


# --------------------------------------------------------------------------
# Filter by tag
# --------------------------------------------------------------------------
def test_transactions_with_tag_is_exact_and_case_insensitive(conn, accounts):
    chk, _ = accounts
    t1 = ledger.add_transaction(conn, chk, "2026-01-01", -10_00, payee="A", tag="vacation")
    t2 = ledger.add_transaction(conn, chk, "2026-01-02", -20_00, payee="B",
                                tag="vacation, work")
    ledger.add_transaction(conn, chk, "2026-01-03", -30_00, payee="C", tag="work")
    # Payee text containing the word must NOT match -- this is an exact tag filter.
    ledger.add_transaction(conn, chk, "2026-01-04", -40_00, payee="vacation planning")
    got = ledger.transactions_with_tag(conn, "VACATION")   # NOCASE
    assert [r["id"] for r in got] == [t1, t2]


def test_transactions_with_tag_scopes_by_account(conn, accounts):
    chk, sav = accounts
    here = ledger.add_transaction(conn, chk, "2026-01-01", -10_00, payee="A", tag="shared")
    there = ledger.add_transaction(conn, sav, "2026-01-02", -20_00, payee="B", tag="shared")
    assert [r["id"] for r in ledger.transactions_with_tag(conn, "shared")] == [here, there]
    assert [r["id"] for r in ledger.transactions_with_tag(conn, "shared", account_id=chk)] == [here]


def test_all_tags_lists_only_used_tags_sorted_nocase(conn, accounts):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-01", -10_00, payee="A", tag="Zebra, apple")
    t = ledger.add_transaction(conn, chk, "2026-01-02", -20_00, payee="B", tag="orphan")
    ledger.set_tags_text(conn, t, "")   # "orphan" is no longer attached to anything
    assert ledger.all_tags(conn) == ["apple", "Zebra"]


# --------------------------------------------------------------------------
# Report by tag (per-tag fan-out)
# --------------------------------------------------------------------------
def test_spending_by_tag_fans_a_line_to_each_of_its_tags(conn, accounts):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-01", -100_00, payee="Dinner",
                           tag="vacation, reimbursable")
    ledger.add_transaction(conn, chk, "2026-01-02", -40_00, payee="Bus", tag="vacation")
    ledger.add_transaction(conn, chk, "2026-01-03", -25_00, payee="Coffee")   # untagged
    rep = tag_report.spending_by_tag(conn, "2026-01-01", "2026-01-31")
    rows = {r.name: (r.count, r.cents) for r in rep.rows}
    assert rows["vacation"] == (2, 140_00)
    assert rows["reimbursable"] == (1, 100_00)
    # Untagged money is NOT a row: "(no tag)" is not a tag, and on a real ledger
    # it swamps the report (97.8% of it, 158x the largest real tag) and sorts to
    # the top, burying every tag the report exists to show.
    assert "(no tag)" not in rows
    assert rep.total == 140_00 + 100_00        # tagged money only
    # Overlapping tags still fan out on purpose: the $100 dinner is counted
    # under both of its tags, so the total exceeds the money that moved.
    assert rep.total > 140_00


# --------------------------------------------------------------------------
# Delete cascade
# --------------------------------------------------------------------------
def test_deleting_a_transaction_retires_its_tag_links(conn, accounts):
    chk, _ = accounts
    t = ledger.add_transaction(conn, chk, "2026-01-01", -10_00, payee="A", tag="gone")
    assert conn.execute("SELECT COUNT(*) AS n FROM transaction_tags").fetchone()["n"] == 1
    ledger.delete_transaction(conn, t)
    assert conn.execute("SELECT COUNT(*) AS n FROM transaction_tags").fetchone()["n"] == 0


def test_deleting_one_transfer_leg_retires_both_legs_tag_links(conn, accounts):
    chk, sav = accounts
    a, b = ledger.create_transfer(conn, chk, sav, "2026-01-15", 500_00, payee="Stash")
    ledger.set_tags_text(conn, a, "travel")
    ledger.delete_transaction(conn, a)   # deletes both legs
    assert conn.execute("SELECT COUNT(*) AS n FROM transaction_tags").fetchone()["n"] == 0


# --------------------------------------------------------------------------
# Schema / migration
# --------------------------------------------------------------------------
def test_schema_version_matches_migrations(conn):
    assert db.SCHEMA_VERSION == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    have = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"tags", "transaction_tags"} <= have


def _tags_migration_index():
    """Where the tags migration sits in the list, found BY CONTENT.

    It was the last migration when this test was written, and `len(MIGRATIONS)
    - 1` encoded that. Migrations are appended, so the next unrelated feature
    silently turned this test's "before" database into an "after" one -- the
    table it asserts is absent had already been created."""
    for i, sql in enumerate(db.MIGRATIONS):
        if "CREATE TABLE tags" in sql:
            return i
    raise AssertionError("the tags migration is no longer in db.MIGRATIONS")


def _build_pre_tags_db(path):
    """A database migrated to the version just BEFORE the tags migration. Built
    with raw SQL so no tag-aware ledger code runs before the junction exists."""
    c = db.connect(path)
    for i in range(_tags_migration_index()):
        c.executescript(db.MIGRATIONS[i])
        c.execute(f"PRAGMA user_version = {i + 1}")
    c.commit()
    return c


def test_migration_carries_the_legacy_free_text_tag_forward(tmp_path):
    path = tmp_path / "legacy.db"
    c = _build_pre_tags_db(path)
    assert c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                     "AND name='tags'").fetchone() is None
    acct = ledger.create_account(c, "Checking", "checking", opening_balance=0)
    # Legacy single-column tags via raw SQL: padded, a case variant, and a blank.
    c.executemany(
        "INSERT INTO transactions(account_id, date, amount, payee, tag) VALUES (?,?,?,?,?)",
        [(acct, "2026-01-01", -100_00, "Store", "  Vacation  "),
         (acct, "2026-01-02", -200_00, "Shop", "vacation"),
         (acct, "2026-01-03", -300_00, "Blank", "   ")],
    )
    c.commit()
    c.close()

    c = db.init_db(path)   # applies the tags migration
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        names = [r["name"] for r in c.execute("SELECT name FROM tags").fetchall()]
        assert len(names) == 1 and names[0].lower() == "vacation"   # NOCASE collapse
        # Both real rows link to the one tag; the whitespace-only tag became NULL.
        both = ledger.transactions_with_tag(c, "vacation")
        assert {r["payee"] for r in both} == {"Store", "Shop"}
        assert c.execute("SELECT tag FROM transactions WHERE payee='Blank'").fetchone()["tag"] is None
        # The cache column is trimmed to the value it now stands for.
        assert c.execute("SELECT tag FROM transactions WHERE payee='Store'").fetchone()["tag"] == "Vacation"
    finally:
        c.close()


# --------------------------------------------------------------------------
# Register: the single comma-separated slot (thin projection)
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_register_tag_cell_edits_one_comma_separated_slot(qapp, conn, accounts):
    from PyQt5.QtCore import QCoreApplication, Qt
    from mammon.ui.models import RegisterModel

    chk, _ = accounts
    m = RegisterModel(conn, chk)
    m.add_from_values({"date": "2026-04-01", "payee": "REI", "payment": "50.00",
                       "category": "Recreation", "tag": "vacation, business"})
    txn = m.txn_at(0)
    # The one cell shows the joined string; the junction holds the split tags.
    assert m.data(m.index(0, RegisterModel.TAG), Qt.DisplayRole) == "vacation, business"
    assert ledger.get_tags(conn, txn["id"]) == ["vacation", "business"]
    assert [r["id"] for r in ledger.transactions_with_tag(conn, "business")] == [txn["id"]]

    # Inline-edit the same slot: parse/join happens in the domain, not in Qt.
    m.setData(m.index(0, RegisterModel.TAG), "solo", Qt.EditRole)
    QCoreApplication.processEvents()   # deferred inline-edit reload
    assert ledger.get_tags(conn, m.txn_at(0)["id"]) == ["solo"]
    assert m.data(m.index(0, RegisterModel.TAG), Qt.DisplayRole) == "solo"


def test_a_split_leg_is_counted_under_its_own_tag(tmp_path):
    """Per-leg attribution, the reason `splits.tag_id` exists (migration 54).

    One $250 parts order split $100/$150 across two projects. A report line for
    a split leg used to inherit the PARENT's tag, so a leg's own project was
    invisible; the alternative considered -- folding leg tags up onto the
    transaction -- would have credited the whole $250 to BOTH tags, reporting
    $500 of spending against a $250 payment.
    """
    from mammon import db
    from mammon.importers import import_file
    from mammon.reports.tags import spending_by_tag

    conn = db.init_db(str(tmp_path / "t.db"))
    path = tmp_path / "x.QIF"
    path.write_text(
        "!Type:Bank\nD2/15'18\nT-250.00\nPBig Order\n"
        "LBusiness:Research/Rig 8\n"
        "SBusiness:Research/Rig 8\n$-100.00\n"
        "SBusiness:Research/Rig 9\n$-150.00\n^\n", encoding="utf-8")
    import_file(conn, str(path), account="Checking")

    rows = {r.name: r.cents for r in
            spending_by_tag(conn, "2018-01-01", "2018-12-31").rows}
    assert rows == {"Rig 8": 100_00, "Rig 9": 150_00}
    assert sum(rows.values()) == 250_00


def test_a_row_tag_and_a_leg_tag_both_apply(tmp_path):
    """A transaction tagged 'reimbursable' whose legs carry projects: the leg is
    both. spending_by_tag counts a line under EVERY tag it carries, so the row
    tag reaches each leg's amount and the leg tag reaches only its own."""
    from mammon import db, ledger
    from mammon.reports.tags import spending_by_tag

    conn = db.init_db(str(tmp_path / "t.db"))
    aid = ledger.create_account(conn, "Checking", "checking")
    cat = ledger.resolve_category(conn, "Business:Research")
    txn = ledger.add_transaction(conn, aid, "2018-02-15", -250_00, payee="Big Order")
    ledger.set_splits(conn, txn, [
        {"category_id": cat, "amount": -100_00, "memo": ""},
        {"category_id": cat, "amount": -150_00, "memo": ""}])
    ledger.set_tags(conn, txn, ["reimbursable"])
    for leg, name in zip(ledger.get_splits(conn, txn), ("Rig 8", "Rig 9")):
        conn.execute("UPDATE splits SET tag_id=? WHERE id=?",
                     (ledger.tag_id(conn, name), leg["id"]))
    conn.commit()

    rows = {r.name: r.cents for r in
            spending_by_tag(conn, "2018-01-01", "2018-12-31").rows}
    assert rows == {"reimbursable": 250_00, "Rig 8": 100_00, "Rig 9": 150_00}
