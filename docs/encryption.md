# Database encryption

Mammon can encrypt its database file. This is **optional and off by default**: with no
password set, the ledger is an ordinary plain SQLite file that any SQL tool can open.

## What it uses

[SQLCipher](https://www.zetetic.net/sqlcipher/), through the `sqlcipher3` Python driver —
AES-256 page encryption with a per-page HMAC and PBKDF2 key derivation. The whole file is
covered: every table, every index, and the schema itself.

`sqlcipher3` is an optional dependency:

```
pip install -e ".[encryption]"
```

`mammon/sqldriver.py` selects the driver — `sqlcipher3` when it is installed, the standard
library's `sqlite3` when it is not — and everything in the application opens its
connections through it. Without the driver Mammon works exactly as before, minus the
ability to encrypt.

The two drivers define separate types. A SQLCipher connection is not an instance of
`sqlite3.Connection`, and its exceptions are not `sqlite3` exceptions, so `isinstance`
checks and `except` clauses take their classes from `mammon.sqldriver` rather than from
`sqlite3`.

## No password means plain SQLite

With no key set, SQLCipher writes a byte-identical ordinary SQLite file: the header is the
usual `SQLite format 3\000`, the standard library opens it, and so does any other tool.
Encryption begins only when a password is set.

`encryption.is_encrypted(path)` reports which kind a file is by reading its first 16 bytes,
so it works without the driver and without the password.

## Where you are asked for a password

Only when the file is actually encrypted. A plaintext ledger never prompts.

| When | What happens |
|---|---|
| Starting the app | `app._launch_gui_locked` asks before opening; cancelling exits |
| File → Open / New Database | `MainWindow.open_database` asks; cancelling leaves the current ledger open |
| File → Restore from Backup | The restored file opens through that same path, so it asks when that snapshot is encrypted |
| Settings → Database Password | Asks for the current password before changing it |

The prompt allows three attempts, then stops.

## Where the key lives

In memory, for the life of the session, on `MainWindow.db_key`. It is never written to
QSettings, to the database, or to any file, and closing the app discards it.

**There is no recovery.** A forgotten password means the data is unreadable.

## Setting, changing, and removing a password

Settings → **Database Password…** does all three. Leaving the new password blank removes
encryption.

Conversion is never done in place:

1. A backup of the current database is taken.
2. `sqlcipher_export()` writes a **new** file, and `PRAGMA user_version` is copied to it
   explicitly — the export does not carry it across, and Mammon reads that number to
   decide which migrations to run.
3. The new file is reopened, and its schema version and per-table row counts are compared
   against the source. A mismatch deletes it and aborts, leaving the original untouched.
4. The old file is renamed to `<name>.previous`, the new one takes its place, and the
   ledger is reopened. Only then is `.previous` deleted.

The same three operations are available without the GUI. Each returns the path it wrote
and leaves the source alone:

```python
from mammon import encryption
encryption.encrypt_database(path, password)      # -> <path>.encrypted
encryption.decrypt_database(path, password)      # -> <path>.plain
encryption.change_password(path, old, new)       # -> <path>.rekeyed
```

## Backups

A snapshot is written with the key the ledger currently uses, so backups of an encrypted
database are encrypted. `backup.create_backup(..., key=...)` must be given that key — the
online backup API refuses an unkeyed target for an encrypted source.

**A snapshot keeps the password it was written under.** Changing the ledger's password does
not reach back into existing snapshots. After a change from `OLD` to `NEW`:

| Snapshot | Opens with |
|---|---|
| Taken before the change | `OLD` |
| Taken after the change | `NEW` |

Restoring an older snapshot therefore means supplying the password that was current when it
was taken, and keeping a record of previous passwords for as long as snapshots written
under them are retained. To bring one forward, restore it and convert it individually:

```python
out = backup.restore_backup(snapshot, "restored.db")
encryption.change_password(out, "OLD", "NEW")
```

Snapshots taken before encryption was enabled are **plaintext**, including the safety
backup taken at the moment of the change. `backup.plaintext_snapshots(db_path)` lists them
(a `.delta` is judged by its baseline, since it has no SQLite header of its own). Enabling
encryption offers to delete them and defaults to keeping them: until the new password is
known to work, they are the only way back in.

## The MCP server

`mammon.mcp_server` connects without a key, so it cannot open an encrypted database. An
encrypted ledger and the MCP server are currently mutually exclusive.
