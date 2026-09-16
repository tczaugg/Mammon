"""Smoke tests for mammon.db: schema builds, is idempotent, and enforces
the money/precision conventions we committed to."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from mammon import db, sqldriver

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "accounts",
    "categories",
    "payees",
    "imports",
    "transactions",
    "splits",
    "balance_checkpoints",
    "import_mappings",
    "transaction_matches",
    "holdings",
    "price_history",
    "investment_transactions",
    "reconciliations",
    "review_items",
    "rename_examples",
    "rename_stats",
    "rename_meta",
    "scheduled_payments",
}


def test_init_creates_all_tables(tmp_path):
    conn = db.init_db(tmp_path / "mammon.db")
    names = db.table_names(conn)
    assert EXPECTED_TABLES <= names
    # Migration 15 retired the old flat keyword rules table.
    assert "payee_rules" not in names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "mammon.db"
    db.init_db(path).close()
    conn = db.init_db(path)  # second call must not raise or re-run migrations
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_accounts_has_online_banking_columns(tmp_path):
    """Migration 6 adds the online-banking url/account_number columns and the
    hidden flag; hidden defaults to 0 for pre-existing rows."""
    conn = db.init_db(tmp_path / "mammon.db")
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(accounts)")}
    assert {"url", "account_number", "hidden"} <= set(cols)
    assert cols["hidden"]["dflt_value"] in ("0", 0)
    conn.execute("INSERT INTO accounts(name, type) VALUES ('A', 'checking')")
    row = conn.execute("SELECT url, account_number, hidden FROM accounts").fetchone()
    assert row["url"] is None and row["account_number"] is None
    assert row["hidden"] == 0


def test_accounts_has_cutover_date_column(tmp_path):
    """Migration 8 adds the per-account migration cutover watermark; it is NULL
    for pre-existing rows (closes gap G4 -- the QIF->live import seam)."""
    conn = db.init_db(tmp_path / "mammon.db")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    assert "cutover_date" in cols
    conn.execute("INSERT INTO accounts(name, type) VALUES ('A', 'checking')")
    assert conn.execute("SELECT cutover_date FROM accounts").fetchone()["cutover_date"] is None


def test_review_items_table_and_dedupe_index(tmp_path):
    """Migration 11 adds review_items with a per-account transaction_id dedupe:
    the same non-empty id inserts once; blank ids are exempt (always insert)."""
    conn = db.init_db(tmp_path / "mammon.db")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_items)")}
    assert {"account_id", "transaction_id", "state", "raw_json",
            "matched_txn_id", "accepted_txn_id"} <= cols
    conn.execute("INSERT INTO accounts(name, type) VALUES ('A', 'checking')")
    aid = conn.execute("SELECT id FROM accounts").fetchone()[0]
    # duplicate (account_id, transaction_id) with a non-empty id -> ignored
    conn.execute("INSERT OR IGNORE INTO review_items(account_id, transaction_id) "
                 "VALUES (?, 'T-1')", (aid,))
    conn.execute("INSERT OR IGNORE INTO review_items(account_id, transaction_id) "
                 "VALUES (?, 'T-1')", (aid,))
    # blank/NULL transaction_id is outside the partial unique index -> both stay
    conn.execute("INSERT OR IGNORE INTO review_items(account_id, transaction_id) "
                 "VALUES (?, NULL)", (aid,))
    conn.execute("INSERT OR IGNORE INTO review_items(account_id, transaction_id) "
                 "VALUES (?, NULL)", (aid,))
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM review_items WHERE transaction_id='T-1'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM review_items WHERE transaction_id IS NULL").fetchone()[0] == 2


def test_rename_example_log_replaces_the_tries(tmp_path):
    """Migration 15 added the online rename tries; migration 60 replaced them
    with the example log the decision tree is rebuilt from (see
    :mod:`mammon.rename_tree`). The tries' tables are gone, the tallies and
    meta stay, and ``payee_rules`` (retired in 15) stays gone. The seed of the
    log from existing accepted review rows is pinned in test_rename_tree."""
    conn = db.init_db(tmp_path / "mammon.db")
    names = db.table_names(conn)
    assert {"rename_examples", "rename_stats", "rename_meta"} <= names
    for gone in ("rename_nodes", "rename_node_payees", "rename_token_freq",
                 "action_nodes", "action_node_labels", "action_token_freq",
                 "payee_rules"):
        assert gone not in names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rename_examples)")}
    assert {"kind", "txn_id", "review_id", "text", "extra", "label", "created_at"} <= cols


def test_v19_backfills_dated_total_payment_for_legacy_loan(tmp_path):
    """Migration 19 adds the dated total-payment history (loan_payments) and
    backfills each existing loan's current payment_amount at its origination date,
    so a file created before the feature upgrades in place with the same schedule.
    A fresh init_db never exercises this backfill (no loan rows exist yet), so
    freeze a DB at v18, insert a legacy loan, THEN upgrade to trigger _V19."""
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    for i in range(18):                        # MIGRATIONS[:18] -> user_version 18
        conn.executescript(db.MIGRATIONS[i])
    conn.execute("PRAGMA user_version = 18")
    conn.commit()
    assert "loan_payments" not in db.table_names(conn)   # feature not yet present

    conn.execute("INSERT INTO accounts(name, type) VALUES ('Old Mortgage', 'liability')")
    aid = conn.execute("SELECT id FROM accounts").fetchone()["id"]
    conn.execute(
        "INSERT INTO loan_params(account_id, original_principal, origination_date, "
        "term_months, payment_amount, payment_interval) VALUES (?,?,?,?,?,?)",
        (aid, 200_000_00, "2020-05-01", 360, 1500_00, "monthly"))
    conn.commit()
    conn.close()

    conn = db.init_db(path)                     # in-place upgrade runs _V19
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute(
        "SELECT effective_date, amount FROM loan_payments WHERE account_id=?",
        (aid,)).fetchone()
    # baseline dated row = the loan's current total at its origination date
    assert row["effective_date"] == "2020-05-01" and row["amount"] == 1500_00
    conn.close()


def test_v20_unreconciles_auto_marked_transfer_legs(tmp_path):
    """Migration 20 resets transfer legs that were AUTO-marked reconciled (e.g. by
    migration 17, or an old QIF collapse) back to uncleared -- but ONLY in accounts
    the user never actually reconciled -- so they reappear in the reconcile dialog
    and can be reconciled/unmarked. A transfer leg in an account that WAS genuinely
    reconciled (has a reconciliations row), and any plain non-transfer row, are left
    untouched. A fresh init_db never exercises this (no rows yet), so freeze a DB at
    v19, insert legacy rows, THEN upgrade to trigger _V20."""
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    for i in range(19):                        # MIGRATIONS[:19] -> user_version 19
        conn.executescript(db.MIGRATIONS[i])
    conn.execute("PRAGMA user_version = 19")
    conn.commit()

    conn.executescript(
        "INSERT INTO accounts(id, name, type) VALUES "
        "(1,'Checking','checking'),(2,'Savings','savings'),(3,'Reconciled','checking');")
    # Auto-marked transfer between accounts 1 and 2 (NEITHER ever reconciled):
    # both legs must be reset to uncleared.
    conn.execute("INSERT INTO transactions(id, account_id, date, amount, cleared, "
                 "reconciled, transfer_account_id) VALUES (10,1,'2020-01-01',-4000,1,1,2)")
    conn.execute("INSERT INTO transactions(id, account_id, date, amount, cleared, "
                 "reconciled, transfer_account_id) VALUES (11,2,'2020-01-01',4000,1,1,1)")
    # A transfer leg in account 3, which the user GENUINELY reconciled -> preserved.
    conn.execute("INSERT INTO transactions(id, account_id, date, amount, cleared, "
                 "reconciled, transfer_account_id) VALUES (12,3,'2020-01-01',-4000,1,1,1)")
    conn.execute("INSERT INTO reconciliations(account_id, statement_date, statement_balance) "
                 "VALUES (3,'2020-01-31',0)")
    # A plain (non-transfer) reconciled row in account 1 -> must NOT be touched.
    conn.execute("INSERT INTO transactions(id, account_id, date, amount, cleared, "
                 "reconciled) VALUES (13,1,'2020-01-02',-1000,1,1)")
    conn.commit()
    conn.close()

    conn = db.init_db(path)                    # in-place upgrade runs _V20
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION

    def status(tid):
        r = conn.execute("SELECT cleared, reconciled FROM transactions WHERE id=?",
                         (tid,)).fetchone()
        return r["cleared"], r["reconciled"]

    # Auto-marked transfer legs (accounts never reconciled) -> reset to uncleared,
    # so they are reconcile candidates again.
    assert status(10) == (0, 0)
    assert status(11) == (0, 0)
    # Transfer leg in a genuinely-reconciled account -> status preserved.
    assert status(12) == (1, 1)
    # Plain reconciled row -> untouched (migration only affects transfer legs).
    assert status(13) == (1, 1)
    conn.close()


def test_v68_drops_holdings_snapshots_written_under_the_old_replay(tmp_path):
    """Year-end holdings snapshots store realized P/L and lots. Those written
    before short covers realized, and before a sale's commission stopped being
    charged twice, would keep serving the old figures for every earlier year, so
    migration 68 drops them; the next rebuild writes them under the new rules."""
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    for i in range(67):                        # MIGRATIONS[:67] -> user_version 67
        conn.executescript(db.MIGRATIONS[i])
    conn.execute("PRAGMA user_version = 67")
    conn.execute("INSERT INTO accounts(id, name, type) VALUES (1,'Brokerage','investment')")
    conn.execute("INSERT INTO holdings_checkpoints(account_id, year, symbol, quantity, "
                 "cost_basis, dividends, realized, ever_held, lots) "
                 "VALUES (1, 2025, 'ANON', '0', 0, 0, -12345, 1, '[]')")
    conn.commit()
    conn.close()

    conn = db.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM holdings_checkpoints").fetchone()[0] == 0
    conn.close()


def test_v72_drops_holdings_snapshots_summed_without_a_brokers_dividend_words(tmp_path):
    """"Dividend" and "Cash Dividend" count as dividends from migration 72 on, and
    the snapshots store each position's dividend total summed without them."""
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    for i in range(71):                        # MIGRATIONS[:71] -> user_version 71
        conn.executescript(db.MIGRATIONS[i])
    conn.execute("PRAGMA user_version = 71")
    conn.execute("INSERT INTO accounts(id, name, type) VALUES (1,'Brokerage','investment')")
    conn.execute("INSERT INTO holdings_checkpoints(account_id, year, symbol, quantity, "
                 "cost_basis, dividends, realized, ever_held, lots) "
                 "VALUES (1, 2026, 'ANON', '26', 984200, 0, 0, 1, '[]')")
    conn.commit()
    conn.close()

    conn = db.init_db(path)
    assert conn.execute("SELECT COUNT(*) FROM holdings_checkpoints").fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_invtxn_splits'").fetchone()
    conn.close()


def test_v69_drops_only_balance_snapshots_from_before_an_opening_date(tmp_path):
    """Snapshots for years ending before an account's opening date were written
    with the opening balance in them; later ones are right under both readings
    and are kept, as are those of an account with no opening date."""
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    for i in range(68):                        # MIGRATIONS[:68] -> user_version 68
        conn.executescript(db.MIGRATIONS[i])
    conn.execute("PRAGMA user_version = 68")
    conn.execute("INSERT INTO accounts(id, name, type, opening_balance, opening_date) "
                 "VALUES (1,'Mortgage','liability',-15000000,'2002-08-14'),"
                 "(2,'Checking','checking',10000,NULL)")
    conn.executemany("INSERT INTO balance_checkpoints(account_id, year, balance) VALUES (?,?,?)",
                     [(1, 2001, -15050000), (1, 2002, -15050000), (1, 2003, -14950000),
                      (2, 1999, 10000)])
    conn.commit()
    conn.close()

    conn = db.init_db(path)
    kept = conn.execute("SELECT account_id, year FROM balance_checkpoints ORDER BY 1, 2").fetchall()
    assert [tuple(r) for r in kept] == [(1, 2002), (1, 2003), (2, 1999)]
    conn.close()


def test_foreign_keys_enforced(tmp_path):
    conn = db.init_db(tmp_path / "mammon.db")
    with pytest.raises(sqldriver.IntegrityError):
        # account_id 999 does not exist
        conn.execute(
            "INSERT INTO transactions(account_id, date, amount) VALUES (999, '2026-01-01', -100)"
        )


def test_amounts_are_integer_cents(tmp_path):
    conn = db.init_db(tmp_path / "mammon.db")
    conn.execute("INSERT INTO accounts(name, type) VALUES ('Checking', 'checking')")
    acct_id = conn.execute("SELECT id FROM accounts").fetchone()[0]
    conn.execute(
        "INSERT INTO transactions(account_id, date, payee, amount) VALUES (?,?,?,?)",
        (acct_id, "2026-01-15", "Safeway", -1234),
    )
    conn.commit()
    amount = conn.execute("SELECT amount FROM transactions").fetchone()[0]
    assert amount == -1234
    assert isinstance(amount, int)


def test_share_quantity_is_decimal_text(tmp_path):
    conn = db.init_db(tmp_path / "mammon.db")
    conn.execute("INSERT INTO accounts(name, type) VALUES ('Brokerage', 'investment')")
    acct_id = conn.execute("SELECT id FROM accounts").fetchone()[0]
    conn.execute(
        "INSERT INTO holdings(account_id, symbol, quantity) VALUES (?,?,?)",
        (acct_id, "VTSAX", "123.4567"),
    )
    conn.commit()
    qty = conn.execute("SELECT quantity FROM holdings").fetchone()[0]
    assert qty == "123.4567"
    assert isinstance(qty, str)


def test_connection_uses_wal_and_strict_sync(tmp_path):
    """WAL always, FULL durability at rest.

    The two are independent and only one costs anything. WAL is the fix for
    fsync-bound imports (per-row commits made one yearly QIF spend 79% of its
    time inside commit()), and it cannot lose a commit -- so it is permanent, and
    asserted here because falling back to the rollback journal is a 5-14x
    slowdown with no other symptom. `synchronous=FULL` is the part that trades
    safety for speed, so at rest it stays STRICT: nothing the user types can be
    lost. Only a bulk import relaxes it (:func:`db.bulk_write`)."""
    conn = db.init_db(tmp_path / "mammon.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 2   # FULL
    finally:
        conn.close()


def test_bulk_write_relaxes_fsync_and_restores_it(tmp_path):
    """The import scope drops to NORMAL and puts back what it found.

    It restores the PRIOR value rather than assuming FULL, so nesting cannot leak
    a relaxed setting: the inner scope's exit must leave the outer one still
    relaxed, and only the outermost exit returns to FULL. A leak here would mean
    hand-entered transactions silently running without durability afterwards --
    invisible until a power cut."""
    conn = db.init_db(tmp_path / "mammon.db")
    try:
        def sync():
            return int(conn.execute("PRAGMA synchronous").fetchone()[0])

        assert sync() == 2                      # FULL at rest
        with db.bulk_write(conn):
            assert sync() == 1                  # NORMAL inside
            with db.bulk_write(conn):
                assert sync() == 1
            assert sync() == 1, "inner exit restored FULL inside the outer scope"
        assert sync() == 2, "the import scope leaked a relaxed setting"
    finally:
        conn.close()


def test_bulk_write_restores_fsync_even_when_the_body_raises(tmp_path):
    """A failed import must not leave the connection relaxed for the rest of the
    session -- the restore is in a finally for that reason."""
    conn = db.init_db(tmp_path / "mammon.db")
    try:
        with pytest.raises(ValueError):
            with db.bulk_write(conn):
                raise ValueError("import blew up")
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 2
    finally:
        conn.close()


def test_an_import_runs_relaxed_and_leaves_durability_restored(tmp_path):
    """End to end: importing through the real entry point relaxes fsync for the
    duration (that is the whole speed-up) and hands the connection back strict."""
    from mammon import importers
    conn = db.init_db(tmp_path / "mammon.db")
    try:
        seen = []
        rec = importers.NormalizedTxn(
            external_account="Checking", account_type="checking",
            date="2026-01-05", amount_cents=-2_500, payee="Safeway")
        real = db.bulk_write

        import contextlib

        @contextlib.contextmanager
        def spy(c):
            with real(c):
                seen.append(int(c.execute("PRAGMA synchronous").fetchone()[0]))
                yield c

        db.bulk_write = spy
        try:
            importers.import_records(conn, [rec], provider="t", source_format="qif")
        finally:
            db.bulk_write = real
        assert seen == [1], "the import did not run inside the relaxed scope"
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 2
    finally:
        conn.close()


def test_a_backup_taken_in_wal_mode_restores_a_complete_ledger(tmp_path):
    """WAL holds recent commits in a `-wal` sidecar, so a backup that copied only
    the .db file would produce a snapshot that opens cleanly and silently lacks
    the newest transactions. `backup.create_backup` uses the online backup API,
    which checkpoints into the copy -- pinned here because that failure mode is
    invisible: the bad snapshot is a perfectly valid ledger, just an older one."""
    from mammon import backup, ledger
    src = tmp_path / "mammon.db"
    conn = db.init_db(src)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        aid = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
        ledger.add_transaction(conn, aid, "2026-01-05", -2_500, payee="Safeway")
        # Deliberately NOT checkpointed: that row lives in the -wal sidecar, which
        # is exactly the state a naive file-copy backup would get wrong.
        snapshot = backup.create_backup(
            conn, db_path=src, tag="test", backup_dir=tmp_path / "backups")
    finally:
        conn.close()
    # A NEW path, so restore_backup's foreign-snapshot guard does not apply
    # (it only fires when the destination already exists).
    restored = backup.restore_backup(snapshot, tmp_path / "restored.db")
    out = db.connect(restored)
    try:
        rows = out.execute("SELECT payee, amount FROM transactions").fetchall()
        assert len(rows) == 1, "the WAL-resident row did not reach the snapshot"
        assert rows[0]["payee"] == "Safeway" and rows[0]["amount"] == -2_500
    finally:
        out.close()


def test_claude_md_states_the_real_schema_version():
    """CLAUDE.md tells a contributor which migration number is next. That is a
    promise the prose makes about code it cannot see, and it goes stale silently:
    it read 28 while MIGRATIONS held 29. An agent trusting it appends a `_V29`
    that collides with the real one, and the collision does not surface until a
    migration runs against somebody's real ledger."""
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    m = re.search(r"SCHEMA_VERSION = len\(MIGRATIONS\)` \(currently (\d+)\)", text)
    assert m, "CLAUDE.md no longer states the schema version in the expected form"
    assert int(m.group(1)) == db.SCHEMA_VERSION, (
        f"CLAUDE.md says schema version {m.group(1)}, "
        f"but db.SCHEMA_VERSION is {db.SCHEMA_VERSION}")


def test_v73_adds_holding_links_scoped_to_an_account(tmp_path):
    conn = db.init_db(tmp_path / "links.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(holding_links)")]
    assert cols == ["account_id", "from_symbol", "to_symbol", "date"]
    conn.close()
