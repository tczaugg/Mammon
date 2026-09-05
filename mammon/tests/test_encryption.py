"""Encryption: the driver shim, and converting a ledger between plaintext and encrypted.

The conversion tests skip unless ``sqlcipher3`` is installed, like the MCP and quote
tests do for their own extras. The shim tests always run -- the point of the shim is
that the application behaves the same either way, and that has to hold on the machine
running the suite, whichever driver that is.
"""

import sqlite3

import pytest

from mammon import db, encryption, ledger, sqldriver

needs_cipher = pytest.mark.skipif(
    not encryption.available(),
    reason="needs the sqlcipher3 driver (pip install mammon[encryption])")


# --- the shim -------------------------------------------------------------

def test_the_whole_app_opens_through_one_driver():
    """Whichever driver is loaded, db.connect must hand back its Row type. Mixing
    them is not a style question: sqlite3.Row on a SQLCipher cursor raises."""
    assert sqldriver.Row is not None
    assert sqldriver.DRIVER_NAME in ("sqlite3", "sqlcipher3")


def test_row_factory_matches_the_connections_own_driver(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    row = conn.execute("SELECT 1 AS n").fetchone()
    assert row["n"] == 1                      # keyed access, i.e. a working Row


def test_a_fresh_database_is_plain_sqlite(tmp_path):
    """The default must stay a file any SQL tool can open -- including the standard
    library, even when the app itself is running on SQLCipher."""
    p = tmp_path / "plain.db"
    db.init_db(str(p)).close()
    assert not encryption.is_encrypted(p)
    with sqlite3.connect(str(p)) as c:        # stdlib, not the shim
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_is_encrypted_reads_the_header_not_the_driver(tmp_path):
    missing = tmp_path / "nope.db"
    assert encryption.is_encrypted(missing) is False
    (tmp_path / "empty.db").write_bytes(b"")
    assert encryption.is_encrypted(tmp_path / "empty.db") is False


def test_conversion_without_the_driver_explains_itself(tmp_path, monkeypatch):
    """A missing optional dependency is a setup step, not a crash."""
    monkeypatch.setattr(encryption.sqldriver, "HAVE_SQLCIPHER", False)
    p = tmp_path / "x.db"
    db.init_db(str(p)).close()
    with pytest.raises(encryption.EncryptionUnavailable) as exc:
        encryption.encrypt_database(p, "pw")
    assert "sqlcipher3" in str(exc.value)


# --- conversion -----------------------------------------------------------

def _seeded(path) -> int:
    conn = db.init_db(str(path))
    acct = ledger.create_account(conn, "Checking", "checking", opening_balance=1_000_00)
    ledger.add_transaction(conn, acct, "2026-01-05", -25_00, payee="Northwind Market")
    conn.commit()
    conn.close()
    return acct


@needs_cipher
def test_encrypting_preserves_the_schema_version(tmp_path):
    """THE regression this module exists for. sqlcipher_export() leaves the copy at
    user_version 0; a ledger reporting 0 has every migration replayed against an
    already-migrated schema the next time it is opened."""
    src = tmp_path / "ledger.db"
    _seeded(src)
    out = encryption.encrypt_database(src, "correct horse")
    conn = encryption._open(out, "correct horse")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()


@needs_cipher
def test_encrypting_hides_the_contents(tmp_path):
    src = tmp_path / "ledger.db"
    _seeded(src)
    out = encryption.encrypt_database(src, "pw")
    assert encryption.is_encrypted(out)
    assert b"Northwind Market" not in out.read_bytes()
    assert b"Northwind Market" in src.read_bytes()      # the original, untouched
    # sqlite3.DatabaseError, NOT sqldriver.DatabaseError: the connection below is a
    # stdlib one, and the two hierarchies are disjoint -- which is the whole reason
    # mammon.sqldriver exists. Catching the wrong one here passes silently until it
    # doesn't, exactly as it would in application code.
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(out)).execute("SELECT 1 FROM accounts").fetchone()


@needs_cipher
def test_the_original_is_never_touched(tmp_path):
    src = tmp_path / "ledger.db"
    _seeded(src)
    before = src.read_bytes()
    encryption.encrypt_database(src, "pw")
    assert src.read_bytes() == before


@needs_cipher
def test_conversion_refuses_to_overwrite(tmp_path):
    src = tmp_path / "ledger.db"
    _seeded(src)
    dest = tmp_path / "out.db"
    dest.write_bytes(b"something already here")
    with pytest.raises(FileExistsError):
        encryption.encrypt_database(src, "pw", dest=dest)
    assert dest.read_bytes() == b"something already here"


@needs_cipher
def test_round_trip_returns_the_same_ledger(tmp_path):
    src = tmp_path / "ledger.db"
    _seeded(src)
    enc = encryption.encrypt_database(src, "pw")
    back = encryption.decrypt_database(enc, "pw", dest=tmp_path / "back.db")
    assert not encryption.is_encrypted(back)
    conn = db.connect(str(back))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute(
            "SELECT payee FROM transactions WHERE payee IS NOT NULL"
        ).fetchone()["payee"] == "Northwind Market"
    finally:
        conn.close()


@needs_cipher
def test_the_wrong_password_is_rejected_not_misread(tmp_path):
    """A wrong key must fail loudly. A partial or garbage read that looked like data
    would be far worse than an error on a financial file."""
    src = tmp_path / "ledger.db"
    _seeded(src)
    enc = encryption.encrypt_database(src, "right")
    with pytest.raises(sqldriver.DatabaseError):
        conn = encryption._open(enc, "wrong")
        try:
            conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
        finally:
            conn.close()


@needs_cipher
def test_password_change_keeps_the_data_and_the_version(tmp_path):
    src = tmp_path / "ledger.db"
    _seeded(src)
    enc = encryption.encrypt_database(src, "old pw")
    new = encryption.change_password(enc, "old pw", "new pw")
    conn = encryption._open(new, "new pw")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    finally:
        conn.close()


@needs_cipher
def test_a_password_with_a_quote_still_works(tmp_path):
    """PRAGMA key cannot be parameterised, so the password is quoted by hand. An
    apostrophe is ordinary in a passphrase, and mis-quoting it reads as 'wrong
    password' for a password that is right."""
    src = tmp_path / "ledger.db"
    _seeded(src)
    pw = "it's a 'quoted' one"
    enc = encryption.encrypt_database(src, pw)
    conn = encryption._open(enc, pw)
    try:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    finally:
        conn.close()
