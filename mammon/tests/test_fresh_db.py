"""The schema-template shortcut in mammon/tests/__init__.py.

`fresh_db` stands in for `db.init_db` several hundred times across the suite, so
what it hands back has to be indistinguishable from the real thing -- and it has
to KNOW when it cannot take the shortcut.
"""
from __future__ import annotations

import os

import pytest

from mammon import db
from mammon.tests import fresh_db


def _facts(conn):
    return (conn.execute("PRAGMA user_version").fetchone()[0],
            db.table_names(conn),
            conn.execute("PRAGMA foreign_keys").fetchone()[0])


def test_the_copy_is_indistinguishable_from_a_real_init(tmp_path):
    """Same schema version, same tables, same connection conventions."""
    real = db.init_db(tmp_path / "real.db")
    fast = fresh_db(tmp_path / "fast.db")
    try:
        assert _facts(fast) == _facts(real)
        assert _facts(fast)[0] == db.SCHEMA_VERSION
        assert fast.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        real.close(); fast.close()


def test_the_copy_is_writable_and_independent(tmp_path):
    """Two databases from one template must not share state."""
    a, b = fresh_db(tmp_path / "a.db"), fresh_db(tmp_path / "b.db")
    try:
        a.execute("INSERT INTO accounts (name, type) VALUES ('Only In A', 'checking')")
        a.commit()
        assert b.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        a.close(); b.close()


def test_the_template_leaves_no_wal_sidecar_to_copy(tmp_path):
    """db.connect opens WAL, and copying a database whose -wal still holds
    commits captures a stale file (db.connect's own docstring records the trap).
    The template is closed before it is copied, so the copy stands alone."""
    from mammon.tests import _migrated_template
    template = _migrated_template()
    assert not (template.parent / (template.name + "-wal")).exists()
    assert not (template.parent / (template.name + "-shm")).exists()


def test_an_existing_database_takes_the_real_upgrade_path(tmp_path):
    """The idempotent re-open is what several tests are ABOUT, so a file that
    already has content must never be overwritten by the template."""
    path = tmp_path / "existing.db"
    first = fresh_db(path)
    first.execute("INSERT INTO accounts (name, type) VALUES ('Keep Me', 'checking')")
    first.commit()
    first.close()
    again = fresh_db(path)
    try:
        assert [r["name"] for r in again.execute("SELECT name FROM accounts")] == ["Keep Me"]
    finally:
        again.close()


def test_an_in_memory_database_is_not_templated():
    conn = fresh_db(":memory:")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_an_encrypted_database_is_not_templated(tmp_path, monkeypatch):
    """A keyed database's pages cannot come from a plain template; the call must
    reach db.init_db with the key intact."""
    seen = {}

    def fake_init_db(path, key=None):
        seen["key"] = key
        return db.connect(":memory:")

    monkeypatch.setattr(db, "init_db", fake_init_db)
    fresh_db(tmp_path / "enc.db", key="hunter2").close()
    assert seen["key"] == "hunter2"


def test_the_template_is_rebuilt_when_the_schema_version_changes(tmp_path, monkeypatch):
    """A test that patches the migration list must not be served the schema this
    process cached earlier."""
    import mammon.tests as helpers
    fresh_db(tmp_path / "warm.db").close()          # prime the cache
    cached = helpers._TEMPLATE
    assert cached is not None and cached[0] == db.SCHEMA_VERSION
    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    try:
        helpers._migrated_template()
        assert helpers._TEMPLATE[0] == db.SCHEMA_VERSION
        assert helpers._TEMPLATE[1] != cached[1]
    finally:
        helpers._TEMPLATE = cached                  # leave the process as we found it
