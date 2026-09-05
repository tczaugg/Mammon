"""Converting a ledger between plaintext and encrypted, and changing its password.

Encryption itself needs no code: :mod:`mammon.sqldriver` opens the database through
SQLCipher when it is installed, and a database is encrypted exactly when a key was set
on the connection. This module is the *migration* between the two states -- what runs
when a user first sets a password on a ledger that already exists.

Three things here are load-bearing, and the first was found by testing rather than
reading documentation.

**``sqlcipher_export()`` does not carry ``user_version`` across.** It copies the schema
and every row faithfully, indexes included, and leaves the copy reporting schema
version **0**. Mammon decides which migrations to run from that number
(``db.SCHEMA_VERSION``, currently 50), so an encrypted copy made without restoring it
would have every migration replayed against an already-migrated schema the next time it
was opened. Each converting function therefore copies the version explicitly and
verifies it afterwards.

**Nothing is converted in place.** Each function writes a NEW file and returns its path,
leaving the original untouched; swapping them is the caller's decision, made after the
new file has been verified. A conversion that overwrites the only copy of a financial
ledger has no safe failure mode.

**Every conversion is verified before it is returned.** The new file is reopened with
the key it was written under and its schema version and table row counts are compared
against the source. A conversion that silently produced an empty or partial database
would be indistinguishable from a successful one until the user went looking for a
transaction, which is exactly the moment a backup is least likely to still exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mammon import sqldriver

# The first 16 bytes of any ordinary SQLite file. SQLCipher replaces them with random
# salt, so this is the cheap, dependency-free way to ask which kind of file this is.
SQLITE_MAGIC = b"SQLite format 3\x00"


class EncryptionUnavailable(RuntimeError):
    """Raised when a conversion is attempted without an encryption-capable driver."""


def available() -> bool:
    """True when the loaded driver can encrypt (i.e. ``sqlcipher3`` is installed)."""
    return sqldriver.HAVE_SQLCIPHER


def _require_driver() -> None:
    if not available():
        raise EncryptionUnavailable(
            "encryption needs the sqlcipher3 driver: pip install mammon[encryption] "
            f"(currently running {sqldriver.driver_report()})")


def is_encrypted(path: str | Path) -> bool:
    """True when ``path`` is NOT a plain SQLite file.

    Read from the file header rather than by trying to open it, so this answers for a
    database whose password is unknown, and needs no driver at all."""
    p = Path(path)
    if not p.exists() or p.stat().st_size < len(SQLITE_MAGIC):
        return False
    with open(p, "rb") as fh:
        return fh.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC


def _fingerprint(conn) -> tuple:
    """(schema version, [(table, row count), ...]) -- enough to catch a partial copy."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = [(t, conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables]
    return version, counts


def _open(path: str | Path, key: Optional[str]):
    conn = sqldriver.connect(str(path))
    if key:
        # A pragma cannot be parameterised, so the key is quoted the way SQL quotes a
        # literal: single quotes, doubled inside. Passwords with an apostrophe are not
        # exotic, and getting this wrong turns into "wrong password" for a right one.
        conn.execute("PRAGMA key = '%s'" % key.replace("'", "''"))
    return conn


def _export(src_conn, dest: Path, key: Optional[str], version: int) -> None:
    """``sqlcipher_export`` from an open connection into ``dest``, restoring version."""
    quoted = key.replace("'", "''") if key else ""
    src_conn.execute("ATTACH DATABASE ? AS target KEY '%s'" % quoted, (str(dest),))
    try:
        src_conn.execute("SELECT sqlcipher_export('target')")
        # THE line this module exists for: without it the copy reports schema 0 and
        # every migration replays on next open. See the module docstring.
        src_conn.execute("PRAGMA target.user_version = %d" % int(version))
    finally:
        src_conn.execute("DETACH DATABASE target")


def _convert(source: str | Path, dest: Optional[str | Path], src_key: Optional[str],
             dest_key: Optional[str], suffix: str) -> Path:
    _require_driver()
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(str(src))
    out = Path(dest) if dest is not None else src.with_name(src.name + suffix)
    if out.exists():
        raise FileExistsError(
            f"{out} already exists; conversion never overwrites an existing file")

    conn = _open(src, src_key)
    try:
        before = _fingerprint(conn)
        _export(conn, out, dest_key, before[0])
    finally:
        conn.close()

    check = _open(out, dest_key)
    try:
        after = _fingerprint(check)
    finally:
        check.close()
    if after != before:
        out.unlink(missing_ok=True)
        raise RuntimeError(
            f"conversion of {src.name} did not verify (schema/row counts differ); "
            "the original is untouched and the partial copy was removed")
    return out


def encrypt_database(source, password: str, dest=None) -> Path:
    """Write an encrypted copy of the plaintext ledger ``source``. Returns its path.

    The original is left alone. Swap it in only after you are satisfied -- and keep it
    until then, because a forgotten password is unrecoverable by design."""
    if not password:
        raise ValueError("a password is required to encrypt")
    return _convert(source, dest, None, password, ".encrypted")


def decrypt_database(source, password: str, dest=None) -> Path:
    """Write a plaintext copy of the encrypted ledger ``source``. Returns its path.

    This is the debugging and escape hatch: whatever else happens, the data can always
    be got back out into a file any SQL tool can read."""
    if not password:
        raise ValueError("the current password is required to decrypt")
    return _convert(source, dest, password, None, ".plain")


def change_password(source, old_password: str, new_password: str, dest=None) -> Path:
    """Write a copy of ``source`` re-encrypted under ``new_password``.

    Done as an export rather than ``PRAGMA rekey`` so the original stays readable under
    the old password until the caller swaps files -- a rekey that fails partway leaves
    a database openable by neither password."""
    if not old_password or not new_password:
        raise ValueError("both the current and the new password are required")
    return _convert(source, dest, old_password, new_password, ".rekeyed")
