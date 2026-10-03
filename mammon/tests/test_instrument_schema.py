"""Schema-level tests for the instrument-taxonomy columns on `securities`.

Migration _V67 makes instrument kind and option terms STORABLE and does nothing
else: it classifies no row, backfills no value and rewrites no identity. These
tests pin that promise, because the cheap way to get this wrong is to "helpfully"
default `kind` to 'equity' during the migration -- which would silently assert a
classification the user never made about forty years of history.
"""
from __future__ import annotations

from mammon import db
from mammon.tests import fresh_db

# The seven columns _V67 adds, in the order the migration adds them.
NEW_COLUMNS = [
    "kind",
    "multiplier",
    "underlying",
    "expiration",
    "strike",
    "option_right",
    "kind_source",
]


def _columns(conn, table: str) -> dict[str, str]:
    """{column name: declared type} for `table`."""
    return {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({table})")}


def _user_version(conn) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def test_fresh_db_is_version_67_with_all_seven_columns(tmp_path):
    """A freshly initialized database is at the current version (67 or later)
    and carries the full column set."""
    conn = fresh_db(tmp_path / "fresh.db")
    try:
        assert db.SCHEMA_VERSION >= 67
        assert _user_version(conn) == db.SCHEMA_VERSION

        cols = _columns(conn, "securities")
        for name in NEW_COLUMNS:
            assert name in cols, f"securities is missing the {name!r} column"
            # Everything here is TEXT: multiplier and strike are Decimal-encoded
            # text like every other quantity/price in Mammon, never floats.
            assert cols[name] == "TEXT", f"{name} should be TEXT, got {cols[name]!r}"

        # The pre-existing columns are untouched.
        for name in ("symbol", "name", "sec_type", "asset_class"):
            assert name in cols
    finally:
        conn.close()


def test_column_is_option_right_not_the_sqlite_keyword(tmp_path):
    """`RIGHT` is a SQLite keyword (right joins, 3.39+). Naming a column that
    parses today but is a latent failure in some untested statement context is
    exactly the kind of trap this migration was written to avoid."""
    conn = fresh_db(tmp_path / "fresh.db")
    try:
        cols = _columns(conn, "securities")
        assert "option_right" in cols
        assert "right" not in {c.lower() for c in cols}
        # And it is usable unquoted in the contexts that matter.
        conn.execute(
            "INSERT INTO securities (symbol, option_right) VALUES ('AAPL240119C', 'C')")
        row = conn.execute(
            "SELECT option_right FROM securities "
            "WHERE option_right = 'C' ORDER BY option_right").fetchone()
        assert row[0] == "C"
    finally:
        conn.close()


def test_v67_upgrades_a_v66_db_and_leaves_every_row_unclassified(tmp_path):
    """A real ledger frozen at 66 migrates forward with all its securities
    intact and every one of them NULL-kind. NULL means UNCLASSIFIED, not
    equity: an existing row must behave after the migration exactly as it did
    before it."""
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    for i in range(66):                       # MIGRATIONS[:66] -> user_version 66
        conn.executescript(db.MIGRATIONS[i])
    conn.execute("PRAGMA user_version = 66")

    legacy = [
        ("VTSAX", "Vanguard Total Stock Market", "MUTUAL", "US Equity"),
        ("AAPL", "Apple Inc", "STOCK", "US Equity"),
        ("AAPL  240119C00190000", "Apple Jan 24 190 Call", "OPTION", None),
    ]
    conn.executemany(
        "INSERT INTO securities (symbol, name, sec_type, asset_class) "
        "VALUES (?, ?, ?, ?)", legacy)
    conn.commit()

    cols_before = _columns(conn, "securities")
    assert "kind" not in cols_before          # the feature is genuinely absent
    conn.close()

    conn = fresh_db(path)                   # in-place upgrade runs _V67 onward
    try:
        assert _user_version(conn) == db.SCHEMA_VERSION
        cols = _columns(conn, "securities")
        for name in NEW_COLUMNS:
            assert name in cols

        rows = conn.execute(
            "SELECT symbol, name, sec_type, asset_class, kind, multiplier, "
            "underlying, expiration, strike, option_right, kind_source "
            "FROM securities ORDER BY symbol").fetchall()
        assert len(rows) == len(legacy), "the migration lost or added rows"

        by_symbol = {r[0]: r for r in rows}
        for symbol, name, sec_type, asset_class in legacy:
            row = by_symbol[symbol]
            assert (row[1], row[2], row[3]) == (name, sec_type, asset_class), (
                f"{symbol} had its identity rewritten by the migration")
            # All seven new values are NULL -- including on the row whose
            # sec_type already says OPTION. Classification is Item 4's job.
            assert all(v is None for v in row[4:]), (
                f"{symbol} was classified at migration time: {row[4:]}")
    finally:
        conn.close()


def test_init_db_is_idempotent_at_67(tmp_path):
    """Running init_db twice is a no-op the second time: same version, same
    columns, same rows, and no duplicate-column error from re-running _V67."""
    path = tmp_path / "twice.db"
    conn = fresh_db(path)
    conn.execute(
        "INSERT INTO securities (symbol, name, sec_type) "
        "VALUES ('AAPL', 'Apple Inc', 'STOCK')")
    conn.commit()
    first_cols = _columns(conn, "securities")
    conn.close()

    conn = fresh_db(path)                   # second run, same file
    try:
        assert _user_version(conn) == db.SCHEMA_VERSION >= 67
        assert _columns(conn, "securities") == first_cols
        # connect() installs a Row factory, so compare as plain tuples.
        rows = [tuple(r) for r in conn.execute(
            "SELECT symbol, name, sec_type, kind, kind_source FROM securities")]
        assert rows == [("AAPL", "Apple Inc", "STOCK", None, None)]
    finally:
        conn.close()
