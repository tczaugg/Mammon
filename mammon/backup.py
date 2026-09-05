"""mammon.backup -- timestamped, rotating snapshots of the SQLite database.

A snapshot is a consistent copy of the live database file made with SQLite's
online backup API (``sqlite3.Connection.backup``), so it is safe to take WHILE
the desktop app holds the connection open and is writing. Snapshots land in a
folder OF THEIR OWN DATABASE, ``data/backups/<db-file-name>/``, named::

    <db-file-name>.<tag>.<YYYYMMDD_HHMMSS>.bak      full, a plain SQLite file
    <db-file-name>.<tag>.<YYYYMMDD_HHMMSS>.delta    changed pages only

One folder per database, and a restore that REFUSES a foreign snapshot
-----------------------------------------------------------------------
Both exist because of one real incident. Every database's snapshots used to sit
together in a flat ``data/backups``, and the Restore picker opened there and
listed all of them. A scratch ``mammon2.db`` had been opened for a test earlier
in the day; hours later, rolling back a mistake, the newest-looking file from an
earlier session was picked -- a ``mammon2.db`` snapshot -- and it overwrote the
real ledger. The loss was not noticed for some time, because the restored file
is a perfectly valid ledger; it is simply the WRONG one. Weeks of investment
work had to be redone.

So:

* snapshots are written into a per-database subfolder, and the picker opens in
  THIS database's folder (:func:`backup_dir_for`), so a foreign snapshot is not
  in front of the user in the first place; and
* :func:`restore_backup` compares the snapshot's own ``<db-file-name>`` prefix
  against the file being restored ONTO and refuses a mismatch
  (:func:`snapshot_db_name`). Navigating up a folder, or a copied file, must not
  be enough to overwrite a ledger with a different one. Opening another database
  is what File > Open Database is for; Restore only ever rolls THIS one back.

Legacy flat snapshots are still found and still restorable -- :func:`list_backups`
reads both locations -- so nothing already on disk was stranded by the change.
``python -m mammon.backup organize`` moves them into their folders when the user
wants the tidy layout.

The zero-padded timestamp sorts chronologically, so "keep the newest N" is a
plain lexical sort. ``tag`` separates the two flows the UI drives:

* ``manual`` -- user picked "Back Up Database Now"; never auto-pruned. ALWAYS a
  full ``.bak``: when the user asks for a copy they get a real, openable file.
* ``auto``   -- the ~1-minute background timer; pruned to the newest ``keep``.

Pruning is scoped to one (db-name, tag) pair, matched by the exact
``<name>.<tag>.*`` glob, so it never touches the hand-made
``mammon.db.pre2001import.*.bak`` style checkpoints already in the folder, nor
the other tag's snapshots.

Why incremental, and why by PAGE
--------------------------------
A 40-year ledger is ~12 MB, and a full snapshot every minute filled the folder
with 1.5 GB -- of which, measured on the real data, 1.26 GB was byte-identical
copies (now prevented by :func:`db_fingerprint`) and the rest differed by a
median of **14 pages out of 2,999**. A minute of real work changes about half a
percent of the file.

So an auto snapshot stores only the pages that differ from a full BASELINE, and
the page is the right unit rather than the year or the account:

* SQLite does not lay rows out by year. A 1998 transaction and a 2026 one can
  share a page, so "just save the current year" means extracting rows and
  rebuilding a file -- a logical export, not a copy.
* Changes are not confined to transactions. A rename-tree node, an import
  mapping, a learned category rule, a review row or a scheduled payment moves no
  transaction at all, and during import review those change constantly. Paging
  captures every table automatically, with no schema knowledge and nothing to
  keep in sync when the schema grows.

Every delta references the baseline DIRECTLY, never the delta before it. That
costs a little space -- a delta accumulates the day's changes rather than just
the minute's -- but measured growth is sublinear (67 pages after two hours of
editing, because edits keep landing on the same hot pages), and it buys the
property that matters: no chains. One damaged delta costs exactly one restore
point instead of every point after it, and restoring is always a two-file
operation.

An operation log would be smaller still -- roughly 100 bytes for an edit that
costs ~16 KB as a page delta. It is not worth it: replaying operations makes the
correctness of a BACKUP depend on the correctness (and the version) of the code
that replays it, and a schema migration would strand every older log. At ~16 KB
a snapshot the whole retention window already fits in single-digit megabytes,
so there is nothing left to win.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3          # type annotations only; see mammon.sqldriver
from datetime import datetime, timedelta
import os
from pathlib import Path
from typing import Optional, Union

from mammon import sqldriver

# Backups live here by default (the location the user asked for). Tests pass an
# explicit ``backup_dir`` so they never write into the repo's real folder.
# Anchored to the INSTALL ROOT, never the CWD. A relative Path("data")/"backups"
# resolves against whatever directory the process happens to be started from,
# so a test run (or an app launched from elsewhere) silently wrote snapshots
# into an unrelated tree -- the same CWD trap app._resolve_db already avoids
# for the database itself. Overridable for tests via MAMMON_DATA_DIR.
def _install_data_dir() -> Path:
    env = os.environ.get("MAMMON_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data"


def default_backup_dir() -> Path:
    return _install_data_dir() / "backups"


# Module-level default, resolved once at import FROM THE INSTALL ROOT. Kept as a
# module attribute (not just the function) because callers read it and tests
# monkeypatch it to redirect snapshots into a tmp dir -- the seam that keeps a
# test run from ever writing into a real install's data folder.
DEFAULT_BACKUP_DIR = default_backup_dir()

MANUAL_TAG = "manual"
AUTO_TAG = "auto"

# How many automatic snapshots to retain (a 1/minute cadence would otherwise
# accumulate ~1440 files/day). ~2 hours of history at one per minute.
DEFAULT_AUTO_KEEP = 120

# Time-based retention for the auto-backup folder. The count cap above only
# bounds the CURRENT db-name's snapshots; auto-backups orphaned by a db rename
# (e.g. mammon.db -> mammon_2026.db) are never re-pruned and would accumulate
# forever. ``purge_auto_backups`` deletes any ``*.auto.*.bak`` older than this
# window, across all db-name prefixes. Tune here rather than at call sites.
AUTO_RETENTION_DAYS = 3

# Safety floor: always keep at least this many of the NEWEST auto-backups,
# even if they predate the retention window, so the folder is never left
# without a recent recovery point.
AUTO_RETENTION_FLOOR = 3

# The desktop auto-backup cadence, in milliseconds (~1 minute).
AUTO_INTERVAL_MS = 60_000

_STAMP = "%Y%m%d_%H%M%S"

# Full snapshots keep ``.bak`` (a plain SQLite file, openable by anything);
# incremental ones get their own extension so the two are never confused.
FULL_EXT = ".bak"
DELTA_EXT = ".delta"

# First line of a delta file. Versioned: a future format change bumps the number
# and old files still identify themselves rather than being misread.
DELTA_MAGIC = b"MAMMON-DELTA-1"

# Re-base when a delta would carry more than this fraction of the file. Past that
# the delta has stopped being cheap and a fresh baseline is both smaller and
# faster to restore. Deliberately generous: measured growth is sublinear, so in
# practice this triggers on bulk events (a large import, a VACUUM), not on
# ordinary editing.
REBASE_FRACTION = 0.25


def db_fingerprint(conn=None, db_path=None) -> tuple:
    """A cheap signature of "has this database changed?".

    The auto-backup timer fires on a fixed interval regardless of activity, so
    without this an idle session rewrites the WHOLE database every minute. On a
    40-year ledger that is ~12 MB per tick and, at ``DEFAULT_AUTO_KEEP``
    snapshots, over a gigabyte of near-identical copies -- the retention
    constants were chosen when the file was small. Comparing this signature
    first means a snapshot is written only when something actually changed.

    Three components, because no one of them is sufficient:

    * ``conn.total_changes`` -- rows inserted/updated/deleted through THIS
      connection. Catches the app's own edits immediately, even before SQLite
      has flushed them to disk (so the file's mtime may not have moved yet).
      It does NOT move for another connection's writes.
    * ``PRAGMA data_version`` -- bumped precisely when ANOTHER connection commits
      to this file. Exactly complementary to ``total_changes``, and the only
      reliable signal for that case: a one-row insert from a second session
      changes neither the file size (it lands in an existing page) nor,
      necessarily, the mtime, whose filesystem granularity can be coarser than
      the gap between two writes.
    * the file's ``(mtime_ns, size)`` -- backstop for a change made while no
      connection was open at all (a restore, a copy over the file).
    """
    changes = -1
    data_version = -1
    if conn is not None:
        try:
            changes = int(conn.total_changes)
            data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        except Exception:            # pragma: no cover - defensive
            pass
    try:
        st = os.stat(str(db_path))
        return (changes, data_version, st.st_mtime_ns, st.st_size)
    except OSError:
        return (changes, data_version, 0, 0)


def _dir(backup_dir) -> Path:
    return Path(backup_dir) if backup_dir is not None else Path(DEFAULT_BACKUP_DIR)


def backup_name(db_path, tag: str, when: datetime, ext: str = FULL_EXT) -> str:
    """The snapshot file name for a source db, tag, moment, and kind."""
    return f"{Path(db_path).name}.{tag}.{when.strftime(_STAMP)}{ext}"


def backup_dir_for(db_path, backup_dir=None) -> Path:
    """The folder holding ``db_path``'s snapshots: ``<backups>/<db-file-name>/``.

    One database per folder, so the Restore picker opens on this ledger's own
    snapshots and never puts another database's in front of the user (see the
    module docstring for the incident that motivated it)."""
    return _dir(backup_dir) / Path(db_path).name


def snapshot_db_name(path) -> Optional[str]:
    """The database file name a snapshot claims to be OF, or ``None``.

    Two readings, strictest first:

    1. everything before a ``.db`` segment -- which also names the database in a
       hand-made checkpoint like ``mammon.db.pre2001import.2.bak``. Those are
       real snapshots of a real ledger and must not be treated as anonymous;
    2. failing that, everything before the tag in
       ``<db-file-name>.<tag>.<YYYYMMDD_HHMMSS>.<ext>`` -- for a database whose
       file name does not end in ``.db``.

    Returns None when neither applies. That claims nothing rather than claiming
    wrongly, and a caller checking provenance then has to decide for itself.
    """
    name = Path(path).name
    if not name.endswith((FULL_EXT, DELTA_EXT)):
        return None
    marker = ".db."
    if marker in name:
        return name[:name.index(marker) + len(marker) - 1]
    parts = name.rsplit(".", 3)          # <db name>, <tag>, <stamp>, <ext>
    if len(parts) != 4:
        return None
    db_name, _tag, stamp, _ext = parts
    if not db_name or not stamp.replace("_", "").isdigit():
        return None
    return db_name


# ---------------------------------------------------------------------------
# page-level deltas
# ---------------------------------------------------------------------------
# Both drivers' Connection types. A SQLCipher connection is NOT an instance of
# sqlite3.Connection (the two modules define separate types), so a check against
# only the stdlib one silently mistook a live connection for a file path and
# tried to open it as a filename. Backups are the last line of defence; they do
# not get to be fussy about which driver the caller opened the ledger with.
_CONNECTION_TYPES = tuple({sqldriver.Connection, sqlite3.Connection})


def _connect_like(conn):
    """The ``connect`` belonging to the same driver as ``conn``."""
    return sqldriver.connect if isinstance(conn, sqldriver.Connection) else sqlite3.connect


def _page_size_of(blob: bytes) -> int:
    """The page size recorded in a SQLite file header (offset 16, big-endian).

    The stored value 1 means 65536 -- it will not fit in the header's two bytes,
    so SQLite spells it that way."""
    if len(blob) < 100:
        raise ValueError("not a SQLite database (file too short)")
    size = int.from_bytes(blob[16:18], "big")
    return 65536 if size == 1 else size


def _split_pages(blob: bytes, page_size: int) -> list[bytes]:
    return [blob[i:i + page_size] for i in range(0, len(blob), page_size)]


def _image(path) -> tuple[int, list[bytes], str]:
    """``(page_size, pages, sha256)`` for a full snapshot or database file."""
    blob = Path(path).read_bytes()
    page_size = _page_size_of(blob)
    return page_size, _split_pages(blob, page_size), hashlib.sha256(blob).hexdigest()


def is_delta(path) -> bool:
    """True when ``path`` is one of our delta files (checked by content, not by
    name, so a renamed file is still identified correctly)."""
    try:
        with open(path, "rb") as fh:
            return fh.readline().strip() == DELTA_MAGIC
    except OSError:
        return False


def delta_header(path) -> dict:
    """The JSON header of a delta file. Raises ``ValueError`` if it is not one."""
    with open(path, "rb") as fh:
        if fh.readline().strip() != DELTA_MAGIC:
            raise ValueError(f"not a Mammon delta file: {path}")
        return json.loads(fh.readline().decode("utf-8"))


def _write_delta(new_path, baseline_path, out_path, *, when) -> Optional[Path]:
    """Write ``new_path`` as a page delta against ``baseline_path``.

    Returns the delta's path, or ``None`` when a delta is not worth taking (a
    different page size, or too much of the file changed) -- the caller then
    keeps the full snapshot instead. Never raises for those cases: falling back
    to a full copy is always correct, just larger.
    """
    base_size, base_pages, base_sha = _image(baseline_path)
    new_size, new_pages, new_sha = _image(new_path)
    if base_size != new_size:
        return None                    # a VACUUM changed the page size; re-base
    # A page is "changed" when it differs from the baseline's, and every page
    # past the baseline's end counts as changed (the file grew).
    changed = [i for i in range(len(new_pages))
               if i >= len(base_pages) or new_pages[i] != base_pages[i]]
    if len(changed) > REBASE_FRACTION * max(len(new_pages), 1):
        return None
    header = {
        "baseline": Path(baseline_path).name,
        "baseline_sha256": base_sha,
        "page_size": new_size,
        "page_count": len(new_pages),   # authoritative: restore truncates to it
        "pages": changed,
        "sha256": new_sha,              # of the RECONSTRUCTED file, checked on restore
        "created": when.isoformat(timespec="seconds"),
    }
    body = gzip.compress(b"".join(new_pages[i] for i in changed), 6)
    tmp = Path(str(out_path) + ".part")
    with open(tmp, "wb") as fh:
        fh.write(DELTA_MAGIC + b"\n")
        fh.write(json.dumps(header).encode("utf-8") + b"\n")
        fh.write(body)
    tmp.replace(out_path)              # atomic: a reader never sees a half file
    return Path(out_path)


def restore_bytes(snapshot, *, baseline_dir=None) -> bytes:
    """Reconstruct the full database image a snapshot represents.

    A full ``.bak`` is returned verbatim. A delta is applied to its baseline,
    which is looked up BY NAME in ``baseline_dir`` (default: the delta's own
    folder), so a delta and its baseline travel together as a pair.

    Raises ``FileNotFoundError`` if the baseline is missing and ``ValueError`` if
    the result does not match the checksum recorded when the delta was written --
    a silently wrong restore is far worse than a loud failure.
    """
    snapshot = Path(snapshot)
    if not is_delta(snapshot):
        return snapshot.read_bytes()
    header = delta_header(snapshot)
    d = Path(baseline_dir) if baseline_dir is not None else snapshot.parent
    baseline = d / header["baseline"]
    if not baseline.exists():
        # Per-database folders arrived after some deltas were already written,
        # so a delta in the new folder can name a baseline still sitting flat in
        # the parent (and vice versa). Look there before giving up -- a delta
        # that cannot find its baseline is a lost restore point.
        alt = d.parent / header["baseline"]
        if alt.exists():
            baseline = alt
        else:
            raise FileNotFoundError(
                f"{snapshot.name} needs its baseline {header['baseline']}, "
                f"which is not in {d}")
    page_size = int(header["page_size"])
    page_count = int(header["page_count"])
    base_pages = _split_pages(baseline.read_bytes(), page_size)
    # Start from the baseline, trimmed or padded to the target length, then lay
    # the changed pages over it. Any page past the baseline's end is guaranteed
    # to be in ``pages`` (it counted as changed when the delta was written), so
    # no filler ever survives into the output.
    out = base_pages[:page_count]
    out.extend(b"\x00" * page_size for _ in range(page_count - len(out)))
    with open(snapshot, "rb") as fh:
        fh.readline(), fh.readline()               # magic + header
        body = gzip.decompress(fh.read())
    for n, i in enumerate(header["pages"]):
        out[i] = body[n * page_size:(n + 1) * page_size]
    blob = b"".join(out)
    got = hashlib.sha256(blob).hexdigest()
    if got != header["sha256"]:
        raise ValueError(
            f"{snapshot.name} did not restore cleanly: checksum {got[:12]} does "
            f"not match the recorded {header['sha256'][:12]} (baseline "
            f"{header['baseline']} may be damaged or is the wrong file)")
    return blob


class ForeignSnapshotError(ValueError):
    """A snapshot of one database was aimed at a different database file."""


def check_same_database(snapshot, out_path) -> None:
    """Raise :class:`ForeignSnapshotError` when ``snapshot`` belongs to a
    different database than ``out_path``.

    A snapshot names the database it was taken from, and restoring is by
    definition rolling ONE database back to its own earlier state. Restoring
    ``mammon2.db``'s snapshot over ``mammon.db`` succeeds at the file level and
    leaves a perfectly valid -- and completely wrong -- ledger in place, which is
    exactly how a day's work was lost once. A snapshot whose name claims nothing
    (a hand-made checkpoint, a plain ``.db``) is allowed through: it makes no
    claim to contradict.
    """
    claimed = snapshot_db_name(snapshot)
    target = Path(out_path).name
    if claimed is not None and claimed != target:
        raise ForeignSnapshotError(
            f"{Path(snapshot).name} is a backup of {claimed}, not of {target}. "
            f"Restoring it would replace {target} with a different database. "
            f"To work with {claimed}, use File > Open Database instead.")


def restore_backup(snapshot, out_path, *, baseline_dir=None,
                   allow_foreign: bool = False) -> Path:
    """Materialise ``snapshot`` as a standalone SQLite database at ``out_path``.

    Works for both kinds, so callers (the Restore dialog, the CLI) never have to
    care whether the user picked a full snapshot or a delta.

    Refuses a snapshot belonging to a DIFFERENT database
    (:func:`check_same_database`) when ``out_path`` ALREADY EXISTS -- that is the
    case where something is destroyed. Writing a snapshot out to a new file
    destroys nothing and is a normal thing to want (``python -m mammon.backup
    restore ... --out copy.db`` to inspect another ledger), so it is allowed.
    ``allow_foreign`` overrides the check outright."""
    if not allow_foreign and Path(out_path).exists():
        check_same_database(snapshot, out_path)
    blob = restore_bytes(snapshot, baseline_dir=baseline_dir)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out) + ".part")
    tmp.write_bytes(blob)
    tmp.replace(out)
    return out


def plaintext_snapshots(db_path, backup_dir=None) -> list:
    """Snapshots of ``db_path`` that are NOT encrypted, oldest first.

    Enabling encryption protects the ledger from that moment on; it does nothing
    for the snapshots already on disk. Those are full copies -- the safety backup
    this very change takes, plus up to DEFAULT_AUTO_KEEP auto-backups -- and they
    sit in the folder most likely to be swept into a cloud sync, which is the leak
    encryption was adopted to close. So the app has to be able to name them.

    A ``.delta`` carries no SQLite header of its own (it is gzipped pages behind a
    JSON header), so its state is its BASELINE's: pages copied from a plaintext
    database are plaintext, whatever the delta wraps them in.
    """
    from mammon import encryption
    out = []
    for path in list_backups(db_path, backup_dir=backup_dir):
        try:
            if is_delta(path):
                baseline = path.parent / delta_header(path)["baseline"]
                if baseline.exists() and not encryption.is_encrypted(baseline):
                    out.append(path)
            elif not encryption.is_encrypted(path):
                out.append(path)
        except Exception:                       # unreadable: not our business here
            continue
    return out


def create_backup(
    source: Union[sqlite3.Connection, str, Path],
    db_path=None,
    *,
    tag: str = MANUAL_TAG,
    backup_dir=None,
    keep: Optional[int] = None,
    key: Optional[str] = None,
    when: Optional[datetime] = None,
    incremental: bool = False,
) -> Path:
    """Write one consistent snapshot and return its path.

    ``source`` is either the live ``sqlite3.Connection`` (preferred: uses the
    online backup API, safe mid-session) or a path to the db file. ``db_path``
    names the file the snapshot is OF and drives the output name; it is required
    when ``source`` is a Connection and inferred from ``source`` otherwise.
    ``keep`` (when set) prunes older snapshots of the SAME (db-name, tag) to the
    newest ``keep`` after writing. ``when`` overrides the timestamp (tests).
    ``key`` is the encryption key of an encrypted ledger and MUST be supplied for
    one: without it the snapshot is written unencrypted (see below).

    With ``incremental``, the copy is compared against the newest full snapshot
    for this (db-name, tag) and stored as a page delta when that is materially
    smaller -- see the module docstring. The return value is the path actually
    written, which is a ``.delta`` in that case and a ``.bak`` otherwise (no
    baseline yet, too much changed, or anything at all went wrong). Callers that
    need a standalone database file from either kind use :func:`restore_backup`.
    """
    if isinstance(source, _CONNECTION_TYPES):
        if db_path is None:
            raise ValueError("db_path is required when backing up a live connection")
        conn = source
        own = False
    else:
        db_path = db_path if db_path is not None else source
        conn = sqldriver.connect(str(source))
        own = True

    when = when or datetime.now()
    dest_dir = backup_dir_for(db_path, backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / backup_name(db_path, tag, when)
    # Write through a temp name so a crash mid-copy can never leave a truncated
    # file sitting in the folder under a name that looks like a good snapshot.
    staged = dest_dir / (dest_path.name + ".part")

    try:
        # The target MUST come from the same driver as the source: the online
        # backup API copies between two connections of one module and rejects a
        # mixed pair. A caller may hand us either kind, so follow the source.
        target = _connect_like(conn)(str(staged))
        # ...and it must carry the SAME KEY, or a snapshot of an encrypted ledger
        # is written in plaintext. The backup API copies decrypted pages into
        # whatever the target's codec does with them, and an unkeyed target has
        # no codec -- so the one file most likely to be synced off the machine
        # would be the only unprotected copy of the data.
        if key:
            sqldriver.apply_key(target, key)
        try:
            conn.backup(target)
        finally:
            target.close()
    finally:
        if own:
            conn.close()

    result = dest_path
    if incremental:
        baseline = latest_full(db_path, tag=tag, backup_dir=backup_dir)
        if baseline is not None:
            delta = dest_dir / backup_name(db_path, tag, when, DELTA_EXT)
            try:
                if _write_delta(staged, baseline, delta, when=when) is not None:
                    result = delta
            except Exception:      # pragma: no cover - any trouble -> keep the full copy
                result = dest_path
    if result is dest_path or result == dest_path:
        staged.replace(dest_path)
    else:
        staged.unlink(missing_ok=True)

    if keep is not None:
        prune_backups(db_path, tag=tag, keep=keep, backup_dir=backup_dir)
    return result


def latest_full(db_path, tag: str = AUTO_TAG, backup_dir=None) -> Optional[Path]:
    """The newest FULL snapshot for this (db-name, tag), or ``None``.

    This is the baseline a new delta is measured against. Deltas always point at
    the newest full snapshot at the moment they are written, so a re-base simply
    starts a new generation -- older deltas keep naming their own baseline and
    stay restorable until retention removes them together."""
    fulls = [p for p in list_backups(db_path, tag=tag, backup_dir=backup_dir)
             if p.suffix == FULL_EXT]
    return fulls[-1] if fulls else None


def list_backups(db_path, tag: Optional[str] = None, backup_dir=None) -> list[Path]:
    """Snapshots for ``db_path`` (optionally only one ``tag``), oldest first.

    Both kinds, interleaved in time order: the timestamp sits before the
    extension in the name, so a plain lexical sort is still chronological."""
    name = Path(db_path).name
    stem = f"{name}.{tag}." if tag else f"{name}."
    found: dict = {}
    # This database's own folder first, then any legacy flat snapshots left in
    # the parent from before per-database folders existed. Keyed by file NAME so
    # a snapshot that has been moved between the two is not listed twice.
    for d in (backup_dir_for(db_path, backup_dir), _dir(backup_dir)):
        if not d.exists():
            continue
        for p in d.glob(stem + "*"):
            if p.suffix in (FULL_EXT, DELTA_EXT):
                found.setdefault(p.name, p)
    return [found[k] for k in sorted(found)]


def _baselines_in_use(surviving) -> set[str]:
    """Baseline file names that ``surviving`` deltas still need.

    A delta is useless without the full snapshot it was measured against, so
    retention must never delete a baseline out from under one that it is keeping.
    An unreadable delta is treated as claiming nothing: it is already beyond
    saving, and letting it pin a baseline forever would be worse."""
    needed: set[str] = set()
    for p in surviving:
        if p.suffix != DELTA_EXT:
            continue
        try:
            needed.add(delta_header(p)["baseline"])
        except Exception:            # pragma: no cover - unreadable delta
            continue
    return needed


def prune_backups(db_path, tag: str = AUTO_TAG, keep: int = DEFAULT_AUTO_KEEP,
                  backup_dir=None) -> list[Path]:
    """Delete the oldest snapshots of one (db-name, tag) so at most ``keep``
    remain. Returns the paths removed. ``keep<=0`` removes them all; the glob is
    tag-scoped so hand-made checkpoints and the other tag are never touched.

    ``keep`` counts RESTORE POINTS, full and incremental alike. A baseline that a
    surviving delta still depends on is held back even when its own age would
    have retired it -- otherwise rotation would quietly shred every delta of that
    generation, which is the one way an incremental scheme loses real data."""
    files = list_backups(db_path, tag=tag, backup_dir=backup_dir)
    removed: list[Path] = []
    if len(files) > keep:
        cut = len(files) - keep if keep > 0 else len(files)
        stale, surviving = files[:cut], files[cut:]
        protected = _baselines_in_use(surviving)
        for f in stale:
            if f.suffix == FULL_EXT and f.name in protected:
                continue             # a delta we are keeping still needs it
            f.unlink()
            removed.append(f)
    return removed


def _auto_stamp(path: Path) -> float:
    """The moment an auto-backup represents, as a POSIX timestamp.

    Prefer the timestamp embedded in ``<name>.auto.<YYYYMMDD_HHMMSS>.bak`` (stable
    even if the file is copied); fall back to the file's mtime when the name does
    not parse."""
    parts = path.name.split(".")
    if len(parts) >= 2 and parts[-1] in ("bak", "delta"):
        try:
            return datetime.strptime(parts[-2], _STAMP).timestamp()
        except ValueError:
            pass
    return path.stat().st_mtime


def purge_auto_backups(
    *,
    backup_dir=None,
    retention_days: int = AUTO_RETENTION_DAYS,
    floor: int = AUTO_RETENTION_FLOOR,
    now: Optional[datetime] = None,
) -> list[Path]:
    """Delete auto-backups older than the retention window; return removals.

    Time-based companion to ``prune_backups`` (which is count-scoped to one
    db-name). Scans ONLY the auto-backup folder and ONLY files matching the
    ``*.auto.*.bak`` glob, so manual snapshots, hand-made checkpoints, the live
    db, ``repair_qif_clr`` backups (which live elsewhere), and anything outside
    the folder are never touched. The glob spans ALL db-name prefixes, so
    auto-backups orphaned by a db rename get cleaned up too.

    A file is stale when its moment (embedded timestamp, else mtime) predates
    ``now - retention_days``. The newest ``floor`` auto-backups are ALWAYS kept
    regardless of age, so the folder never loses its most recent recovery point.
    A baseline still referenced by a delta that survives is kept too, for the
    same reason :func:`prune_backups` holds one back.
    Cheap and idempotent: a second run with nothing stale removes nothing."""
    d = _dir(backup_dir)
    if not d.exists():
        return []
    now = now or datetime.now()
    cutoff = (now - timedelta(days=retention_days)).timestamp()
    # (path, moment) pairs, newest first, so the floor is simply the head slice.
    entries = [(f, _auto_stamp(f)) for f in d.glob("*.auto.*")
               if f.suffix in (FULL_EXT, DELTA_EXT)]
    entries.sort(key=lambda e: e[1], reverse=True)
    doomed = [(p, m) for p, m in entries[max(0, floor):] if m < cutoff]
    kept = [p for p, _ in entries if p not in {q for q, _ in doomed}]
    protected = _baselines_in_use(kept)
    removed: list[Path] = []
    for path, _moment in doomed:
        if path.suffix == FULL_EXT and path.name in protected:
            continue
        try:
            path.unlink()
        except OSError:  # pragma: no cover - vanished/locked; skip, retry next tick
            continue
        removed.append(path)
    return removed


# ---------------------------------------------------------------------------
# CLI: inspect and restore snapshots without the GUI
# ---------------------------------------------------------------------------
def organize_backups(backup_dir=None, *, dry_run: bool = False) -> list:
    """Move legacy flat snapshots into their per-database subfolders.

    Returns ``[(source, destination)]`` for what moved (or would move, under
    ``dry_run``). Only files that NAME a database are touched
    (:func:`snapshot_db_name`); anything unrecognised is left exactly where it
    is, because guessing where an unidentified file belongs is how a backup
    folder loses a restore point.

    A delta and its baseline name the same database, so they always land in the
    same folder together and no delta is separated from what it needs.
    """
    d = _dir(backup_dir)
    if not d.exists():
        return []
    moved: list = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix not in (FULL_EXT, DELTA_EXT):
            continue
        owner = snapshot_db_name(p)
        if not owner:
            continue
        dest = d / owner / p.name
        if dest.exists():
            continue
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            p.replace(dest)
        moved.append((p, dest))
    return moved


def _describe(path: Path) -> str:
    """One listing line: kind, size, and what a delta depends on."""
    size = path.stat().st_size
    if path.suffix != DELTA_EXT:
        return f"{path.name:<52} full   {size/1024/1024:>8.2f} MB"
    try:
        h = delta_header(path)
        dep = f"{len(h['pages'])} pages of {h['page_count']} <- {h['baseline']}"
    except Exception as exc:                # pragma: no cover - damaged file
        dep = f"UNREADABLE ({exc})"
    return f"{path.name:<52} delta  {size/1024:>8.1f} KB  {dep}"


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m mammon.backup",
        description="List, verify, and restore Mammon database snapshots.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ls = sub.add_parser("list", help="list snapshots for a database")
    p_ls.add_argument("db", help="the database the snapshots are OF (name is enough)")
    p_ls.add_argument("--tag", default=None, help="only this tag (auto/manual)")
    p_ls.add_argument("--dir", default=None, help="backup folder (default: data/backups)")

    p_rs = sub.add_parser("restore", help="write a snapshot out as a real .db file")
    p_rs.add_argument("snapshot", help="the .bak or .delta to restore")
    p_rs.add_argument("--out", required=True, help="path for the restored database")

    p_og = sub.add_parser(
        "organize",
        help="move legacy flat snapshots into per-database subfolders")
    p_og.add_argument("--dir", default=None, help="backup folder")
    p_og.add_argument("--dry-run", action="store_true",
                      help="report what would move, move nothing")

    p_vf = sub.add_parser("verify", help="check that snapshots restore cleanly")
    p_vf.add_argument("db", help="the database the snapshots are OF")
    p_vf.add_argument("--tag", default=None)
    p_vf.add_argument("--dir", default=None)

    a = ap.parse_args(argv)

    if a.cmd == "list":
        files = list_backups(a.db, tag=a.tag, backup_dir=a.dir)
        if not files:
            print("no snapshots found")
            return 0
        total = sum(f.stat().st_size for f in files)
        for f in files:
            print(_describe(f))
        print(f"\n{len(files)} snapshots, {total/1024/1024:.1f} MB total")
        return 0

    if a.cmd == "organize":
        moved = organize_backups(backup_dir=a.dir, dry_run=a.dry_run)
        for src, dest in moved:
            print(f"{'would move' if a.dry_run else 'moved'} {src.name} -> "
                  f"{dest.parent.name}/")
        print(f"\n{len(moved)} snapshot(s) "
              f"{'would be organized' if a.dry_run else 'organized'}")
        return 0

    if a.cmd == "restore":
        # A new --out is allowed whatever the snapshot is OF (nothing is
        # destroyed); an --out that already exists is guarded like any other
        # replace.
        try:
            out = restore_backup(a.snapshot, a.out)
        except (ValueError, FileNotFoundError) as exc:
            print(f"restore failed: {exc}")
            return 1
        print(f"restored {Path(a.snapshot).name} -> {out} "
              f"({out.stat().st_size/1024/1024:.2f} MB)")
        return 0

    files = list_backups(a.db, tag=a.tag, backup_dir=a.dir)
    bad = 0
    for f in files:
        try:
            restore_bytes(f)
            print(f"ok    {f.name}")
        except Exception as exc:
            bad += 1
            print(f"FAIL  {f.name}: {exc}")
    print(f"\n{len(files) - bad}/{len(files)} snapshots restore cleanly")
    return 1 if bad else 0


if __name__ == "__main__":       # pragma: no cover - CLI entry point
    raise SystemExit(_main())
