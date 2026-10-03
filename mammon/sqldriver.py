"""The SQLite driver the application talks to — and the ONE place that choice is made.

Mammon opens its ledger through ``sqlcipher3`` when it is installed, and through the
standard library's ``sqlite3`` when it is not. Both are pysqlite-derived and behave
identically for everything this codebase does; the only difference is that the
SQLCipher build *can* encrypt a database when a key is set.

**With no key, SQLCipher writes an ordinary, byte-identical plain SQLite file** — the
standard-library module opens it, and so does any other SQL tool. That is what makes
this switch free: the default stays exactly what it has always been, encryption is one
configured value away, and no code branches on which mode is in use.

Why a shim instead of importing ``sqlcipher3`` directly at each call site: the two
modules define *separate* types, and mixing them fails in ways that are quiet or
misleading. Verified, not assumed:

- ``sqlite3.Row`` on a SQLCipher cursor raises
  ``TypeError: Row() argument 1 must be sqlite3.Cursor, not sqlcipher3.dbapi2.Cursor``.
- The exception hierarchies are disjoint: ``isinstance(err, sqlite3.Error)`` is **False**
  for an error raised by SQLCipher. An ``except sqlite3.Error`` written against the
  standard library silently stops catching anything, turning a handled error into a
  crash. That is the dangerous one, because nothing fails until something goes wrong.

So every connection, row factory and exception reference must come from the *same*
driver, and this module is where that is decided. Import what you need from here rather
than from ``sqlite3``.

One deliberate exception: type annotations elsewhere in the codebase still read
``sqlite3.Connection`` / ``sqlite3.Row``. Those are annotations only -- Python does not
evaluate them at runtime -- and both drivers are structurally identical, so rewriting
~130 of them would be churn without behavior. The authorizer constants
(``SQLITE_OK``, ``SQLITE_DENY``, ...) are ABI-stable integers defined by SQLite itself
and are identical in both modules.
"""

from __future__ import annotations

try:                                    # the encryption-capable build, when present
    from sqlcipher3 import dbapi2 as _driver
    HAVE_SQLCIPHER = True
except ImportError:                     # the stdlib build: no encryption, same behavior
    import sqlite3 as _driver
    HAVE_SQLCIPHER = False

DRIVER_NAME = "sqlcipher3" if HAVE_SQLCIPHER else "sqlite3"

# The DB-API surface this codebase uses, re-exported from whichever driver won.
connect = _driver.connect
Row = _driver.Row
Connection = _driver.Connection
Cursor = _driver.Cursor

Error = _driver.Error
DatabaseError = _driver.DatabaseError
IntegrityError = _driver.IntegrityError
OperationalError = _driver.OperationalError
ProgrammingError = _driver.ProgrammingError

sqlite_version = _driver.sqlite_version
sqlite_version_info = _driver.sqlite_version_info


def apply_key(conn, key: str) -> None:
    """Unlock ``conn`` with ``key``, and prove the key is right before returning.

    Two things here are not optional. ``PRAGMA key`` must run BEFORE anything
    else touches the database, because it configures the codec that reads page 1.
    And a pragma cannot take a bound parameter, so the key is quoted the way SQL
    quotes a literal -- single quotes, doubled inside. An apostrophe in a
    passphrase is ordinary, and mis-quoting one reports "wrong password" for a
    password that is right.

    The read at the end forces page 1 to be decrypted, so a wrong key raises here
    -- at the moment the caller can still ask again -- rather than at some later,
    unrelated query.
    """
    conn.execute("PRAGMA key = '%s'" % key.replace("'", "''"))
    conn.execute("SELECT count(*) FROM sqlite_master")


def driver_report() -> str:
    """One line naming the driver and its SQLite build, for diagnostics.

    Worth surfacing because the two builds ship *different* SQLite versions, and a
    schema question ("does this migration behave the same?") depends on which one is
    actually loaded."""
    return f"{DRIVER_NAME} (SQLite {sqlite_version}, encryption {'available' if HAVE_SQLCIPHER else 'unavailable'})"
