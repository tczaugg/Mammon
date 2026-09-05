from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mammon import backup, db, ledger


@pytest.fixture
def src(tmp_path):
    """A small real database file to snapshot."""
    path = tmp_path / "mammon_2026.db"
    conn = db.init_db(path)
    ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    conn.commit()
    return conn, path


def _dt(sec=0):
    return datetime(2026, 8, 18, 12, 30, sec)


def test_backup_name_format():
    name = backup.backup_name("data/mammon_2026.db", "auto", _dt(5))
    assert name == "mammon_2026.db.auto.20260818_123005.bak"


def test_create_backup_from_connection_is_a_valid_copy(src, tmp_path):
    conn, path = src
    bdir = tmp_path / "backups"
    dest = backup.create_backup(conn, path, tag="manual", backup_dir=bdir, when=_dt())
    assert dest.exists()
    assert dest.name == "mammon_2026.db.manual.20260818_123000.bak"
    # The snapshot is a real, queryable database carrying the source's data.
    copy = db.connect(dest)
    names = {a["name"] for a in ledger.list_accounts(copy)}
    assert "Checking" in names
    copy.close()


def test_create_backup_from_path_opens_its_own_connection(src, tmp_path):
    conn, path = src
    conn.close()
    bdir = tmp_path / "backups"
    dest = backup.create_backup(str(path), tag="auto", backup_dir=bdir, when=_dt(1))
    assert dest.exists()
    assert dest.name.endswith(".auto.20260818_123001.bak")


def test_backup_from_connection_requires_db_path(src, tmp_path):
    conn, _ = src
    with pytest.raises(ValueError):
        backup.create_backup(conn, None, backup_dir=tmp_path)


def test_list_backups_filters_by_tag(src, tmp_path):
    conn, path = src
    bdir = tmp_path / "backups"
    backup.create_backup(conn, path, tag="auto", backup_dir=bdir, when=_dt(1))
    backup.create_backup(conn, path, tag="auto", backup_dir=bdir, when=_dt(2))
    backup.create_backup(conn, path, tag="manual", backup_dir=bdir, when=_dt(3))
    assert len(backup.list_backups(path, backup_dir=bdir)) == 3
    assert len(backup.list_backups(path, tag="auto", backup_dir=bdir)) == 2
    assert len(backup.list_backups(path, tag="manual", backup_dir=bdir)) == 1


def test_prune_keeps_newest_n_of_that_tag_only(src, tmp_path):
    conn, path = src
    bdir = tmp_path / "backups"
    # Five auto snapshots at increasing timestamps, one manual, plus a hand-made
    # checkpoint whose name uses a DIFFERENT middle token.
    for s in range(5):
        backup.create_backup(conn, path, tag="auto", backup_dir=bdir, when=_dt(s))
    backup.create_backup(conn, path, tag="manual", backup_dir=bdir, when=_dt(9))
    handmade = bdir / "mammon_2026.db.pre-rebuild.20260101_000000.bak"
    handmade.write_bytes(b"checkpoint")

    removed = backup.prune_backups(path, tag="auto", keep=2, backup_dir=bdir)
    assert len(removed) == 3
    kept = backup.list_backups(path, tag="auto", backup_dir=bdir)
    assert [k.name for k in kept] == [
        "mammon_2026.db.auto.20260818_123003.bak",
        "mammon_2026.db.auto.20260818_123004.bak",
    ]
    # Manual snapshot and the hand-made checkpoint are untouched.
    assert len(backup.list_backups(path, tag="manual", backup_dir=bdir)) == 1
    assert handmade.exists()


def test_create_backup_with_keep_rotates(src, tmp_path):
    conn, path = src
    bdir = tmp_path / "backups"
    for s in range(4):
        backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                             keep=2, when=_dt(s))
    assert len(backup.list_backups(path, tag="auto", backup_dir=bdir)) == 2


# ---------------------------------------------------------------------------
# Time-based retention: purge_auto_backups
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 8, 29, 12, 0, 0)


def _auto_file(bdir, when, name="mammon_2026.db", tag="auto"):
    """Fabricate a snapshot file (content irrelevant to pruning) with the given
    embedded timestamp, db-name prefix, and tag."""
    bdir.mkdir(parents=True, exist_ok=True)
    p = bdir / f"{name}.{tag}.{when.strftime('%Y%m%d_%H%M%S')}.bak"
    p.write_bytes(b"snapshot")
    return p


def test_purge_removes_stale_keeps_within_window(tmp_path):
    bdir = tmp_path / "backups"
    fresh = [_auto_file(bdir, _NOW - timedelta(hours=h)) for h in (0, 1, 12, 47)]
    stale = [_auto_file(bdir, _NOW - timedelta(days=d)) for d in (4, 10, 30)]

    removed = backup.purge_auto_backups(backup_dir=bdir, floor=0, now=_NOW)

    assert {r.name for r in removed} == {s.name for s in stale}
    assert all(f.exists() for f in fresh)
    assert not any(s.exists() for s in stale)


def test_purge_safety_floor_keeps_newest_even_when_all_stale(tmp_path):
    bdir = tmp_path / "backups"
    # Five snapshots, ALL older than the 3-day window (oldest first list order).
    files = [_auto_file(bdir, _NOW - timedelta(days=d)) for d in (30, 20, 15, 10, 5)]

    removed = backup.purge_auto_backups(backup_dir=bdir, floor=3, now=_NOW)

    # The newest 3 survive despite being stale; the folder is never emptied.
    survivors = sorted(p.name for p in bdir.glob("*.auto.*.bak"))
    assert survivors == sorted(f.name for f in files[-3:])
    assert {r.name for r in removed} == {f.name for f in files[:2]}
    assert survivors  # never left empty


def test_purge_only_touches_auto_files_in_this_folder(tmp_path):
    bdir = tmp_path / "backups"
    stale_auto = _auto_file(bdir, _NOW - timedelta(days=30))
    # Same folder, but NOT auto-backups: manual snapshot + hand-made checkpoint.
    manual = _auto_file(bdir, _NOW - timedelta(days=30), tag="manual")
    checkpoint = bdir / "mammon_2026.db.pre2001import.20200101_000000.bak"
    checkpoint.write_bytes(b"checkpoint")
    stray = bdir / "notes.txt"
    stray.write_bytes(b"unrelated")
    # A file OUTSIDE the auto-backup folder that merely looks like an auto-backup.
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside = _auto_file(outside_dir, _NOW - timedelta(days=30))

    removed = backup.purge_auto_backups(backup_dir=bdir, floor=0, now=_NOW)

    assert [r.name for r in removed] == [stale_auto.name]
    assert not stale_auto.exists()
    assert manual.exists() and checkpoint.exists() and stray.exists()
    assert outside.exists()  # nothing outside the target folder is touched


def test_purge_spans_all_db_name_prefixes(tmp_path):
    bdir = tmp_path / "backups"
    # Orphaned auto-backups from a since-renamed db (mammon.db) plus the current
    # db's fresh one. Retention is name-agnostic, so orphans get cleaned too.
    orphan_old = _auto_file(bdir, _NOW - timedelta(days=30), name="mammon.db")
    current_fresh = _auto_file(bdir, _NOW - timedelta(minutes=1), name="mammon_2026.db")

    removed = backup.purge_auto_backups(backup_dir=bdir, floor=0, now=_NOW)

    assert [r.name for r in removed] == [orphan_old.name]
    assert current_fresh.exists()


def test_purge_uses_embedded_timestamp_not_mtime(tmp_path):
    bdir = tmp_path / "backups"
    # Freshly written now (mtime ~ real now) but the NAME encodes a 30-day-old
    # stamp. If mtime governed it would survive; embedded timestamp marks it stale.
    aged = _auto_file(bdir, _NOW - timedelta(days=30))
    removed = backup.purge_auto_backups(backup_dir=bdir, floor=0, now=_NOW)
    assert [r.name for r in removed] == [aged.name]
    assert not aged.exists()


def test_purge_is_idempotent(tmp_path):
    bdir = tmp_path / "backups"
    _auto_file(bdir, _NOW - timedelta(days=30))
    _auto_file(bdir, _NOW - timedelta(hours=1))
    first = backup.purge_auto_backups(backup_dir=bdir, floor=0, now=_NOW)
    second = backup.purge_auto_backups(backup_dir=bdir, floor=0, now=_NOW)
    assert len(first) == 1
    assert second == []


def test_purge_missing_dir_returns_empty(tmp_path):
    assert backup.purge_auto_backups(backup_dir=tmp_path / "nope", now=_NOW) == []


def test_purge_defaults_use_named_constants(tmp_path):
    # The default window/floor come from the tunable module constants, not magic
    # numbers baked into the function.
    bdir = tmp_path / "backups"
    assert backup.AUTO_RETENTION_DAYS == 3
    assert backup.AUTO_RETENTION_FLOOR == 3
    # A file exactly inside the default window is retained under the defaults.
    inside = _auto_file(bdir, _NOW - timedelta(days=backup.AUTO_RETENTION_DAYS - 1))
    backup.purge_auto_backups(backup_dir=bdir, now=_NOW)
    assert inside.exists()


# ---------------------------------------------------------------------------
# Change detection: an idle session must not rewrite the database every minute.
#
# The auto-backup timer fires on a fixed interval regardless of activity. With a
# 40-year ledger that is ~12 MB per tick and, at DEFAULT_AUTO_KEEP snapshots,
# over a gigabyte of near-identical copies. The retention constants were chosen
# when the file was small; skipping an unchanged database is the actual fix.
# ---------------------------------------------------------------------------
def test_fingerprint_is_stable_while_nothing_changes(tmp_path):
    from mammon import db
    p = tmp_path / "m.db"
    conn = db.init_db(p)
    a = backup.db_fingerprint(conn, p)
    assert backup.db_fingerprint(conn, p) == a      # no write -> identical
    conn.close()


def test_fingerprint_moves_on_a_write_through_this_connection(tmp_path):
    from mammon import db, ledger
    p = tmp_path / "m.db"
    conn = db.init_db(p)
    a = backup.db_fingerprint(conn, p)
    ledger.create_account(conn, "Checking", "checking")
    assert backup.db_fingerprint(conn, p) != a
    conn.close()


def test_fingerprint_moves_on_a_write_from_another_connection(tmp_path):
    """total_changes alone would miss this; the file's mtime/size catches it."""
    import sqlite3
    from mammon import db, ledger
    p = tmp_path / "m.db"
    conn = db.init_db(p)
    a = backup.db_fingerprint(conn, p)
    other = sqlite3.connect(str(p))
    ledger.create_account(other, "Savings", "savings")
    other.close()
    assert backup.db_fingerprint(conn, p) != a
    conn.close()


def test_fingerprint_survives_a_missing_file(tmp_path):
    assert backup.db_fingerprint(None, tmp_path / "gone.db") == (-1, -1, 0, 0)


# ---------------------------------------------------------------------------
# Incremental (page-delta) snapshots
# ---------------------------------------------------------------------------
def _restored(path, tmp_path, name="restored.db"):
    """Materialise a snapshot and open it, so assertions run against a REAL
    database rather than against the delta's own bookkeeping."""
    import sqlite3
    out = backup.restore_backup(path, tmp_path / name)
    c = sqlite3.connect(str(out))
    c.row_factory = sqlite3.Row
    return c


def test_first_incremental_snapshot_is_full(src, tmp_path):
    """With no baseline to measure against there is nothing to delta, so the
    first incremental snapshot is an ordinary full .bak."""
    conn, path = src
    bdir = tmp_path / "backups"
    p = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                             incremental=True, when=_dt(0))
    assert p.suffix == backup.FULL_EXT
    assert not backup.is_delta(p)


def test_second_incremental_snapshot_is_a_delta_and_is_much_smaller(src, tmp_path):
    conn, path = src
    bdir = tmp_path / "backups"
    full = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                incremental=True, when=_dt(0))
    acct = ledger.list_accounts(conn)[0]["id"]
    ledger.add_transaction(conn, acct, "2026-08-18", -25_00, payee="Safeway")
    delta = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                 incremental=True, when=_dt(1))
    assert delta.suffix == backup.DELTA_EXT
    assert backup.is_delta(delta)
    assert delta.stat().st_size < full.stat().st_size
    h = backup.delta_header(delta)
    assert h["baseline"] == full.name
    assert 0 < len(h["pages"]) < h["page_count"]      # a subset, not the whole file


def test_delta_restores_to_the_exact_database_it_snapshotted(src, tmp_path):
    """The point of the whole scheme: a delta must rebuild the byte-exact file."""
    import hashlib
    conn, path = src
    bdir = tmp_path / "backups"
    backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                         incremental=True, when=_dt(0))
    acct = ledger.list_accounts(conn)[0]["id"]
    ledger.add_transaction(conn, acct, "2026-08-18", -25_00, payee="Safeway")
    conn.commit()
    # The image the delta is taken FROM, captured independently for comparison.
    reference = backup.create_backup(conn, path, tag="manual", backup_dir=bdir,
                                     when=_dt(1))
    delta = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                 incremental=True, when=_dt(2))
    assert delta.suffix == backup.DELTA_EXT
    assert (hashlib.sha256(backup.restore_bytes(delta)).hexdigest()
            == hashlib.sha256(reference.read_bytes()).hexdigest())


def test_each_delta_is_its_own_restore_point(src, tmp_path):
    """Successive deltas capture successive states -- restoring the first must
    not show work that only exists in the second."""
    conn, path = src
    bdir = tmp_path / "backups"
    acct = ledger.list_accounts(conn)[0]["id"]
    backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                         incremental=True, when=_dt(0))
    ledger.add_transaction(conn, acct, "2026-08-18", -10_00, payee="First")
    conn.commit()
    d1 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                              incremental=True, when=_dt(1))
    ledger.add_transaction(conn, acct, "2026-08-18", -20_00, payee="Second")
    conn.commit()
    d2 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                              incremental=True, when=_dt(2))

    c1 = _restored(d1, tmp_path, "r1.db")
    payees1 = {r["payee"] for r in c1.execute("SELECT payee FROM transactions")}
    assert "First" in payees1 and "Second" not in payees1
    assert c1.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c1.close()

    c2 = _restored(d2, tmp_path, "r2.db")
    payees2 = {r["payee"] for r in c2.execute("SELECT payee FROM transactions")}
    assert {"First", "Second"} <= payees2
    c2.close()


def test_deltas_reference_the_baseline_not_each_other(src, tmp_path):
    """No chains: every delta names the FULL snapshot, so one damaged delta can
    never invalidate the ones after it."""
    conn, path = src
    bdir = tmp_path / "backups"
    acct = ledger.list_accounts(conn)[0]["id"]
    full = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                incremental=True, when=_dt(0))
    deltas = []
    for s in range(1, 4):
        ledger.add_transaction(conn, acct, "2026-08-18", -100 * s, payee=f"P{s}")
        conn.commit()
        deltas.append(backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                           incremental=True, when=_dt(s)))
    assert all(backup.delta_header(d)["baseline"] == full.name for d in deltas)
    # Destroying the middle delta leaves the last one perfectly restorable.
    deltas[1].write_bytes(b"corrupted")
    c = _restored(deltas[2], tmp_path, "last.db")
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()


def test_restore_refuses_a_delta_whose_baseline_is_missing(src, tmp_path):
    conn, path = src
    bdir = tmp_path / "backups"
    full = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                incremental=True, when=_dt(0))
    acct = ledger.list_accounts(conn)[0]["id"]
    ledger.add_transaction(conn, acct, "2026-08-18", -25_00, payee="Safeway")
    delta = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                 incremental=True, when=_dt(1))
    full.unlink()
    with pytest.raises(FileNotFoundError, match="baseline"):
        backup.restore_bytes(delta)


def test_restore_refuses_a_delta_applied_to_a_damaged_baseline(src, tmp_path):
    """A wrong restore is worse than a failed one, so the recorded checksum is
    verified and a mismatch raises instead of returning a plausible file."""
    conn, path = src
    bdir = tmp_path / "backups"
    full = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                incremental=True, when=_dt(0))
    acct = ledger.list_accounts(conn)[0]["id"]
    ledger.add_transaction(conn, acct, "2026-08-18", -25_00, payee="Safeway")
    delta = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                 incremental=True, when=_dt(1))
    blob = bytearray(full.read_bytes())
    blob[100:200] = b"\x00" * 100                # damage a page the delta reuses
    full.write_bytes(bytes(blob))
    with pytest.raises(ValueError, match="did not restore cleanly"):
        backup.restore_bytes(delta)


def test_manual_backup_is_always_a_full_openable_file(src, tmp_path):
    """A user asking for a backup gets a real database, never a delta."""
    conn, path = src
    bdir = tmp_path / "backups"
    backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                         incremental=True, when=_dt(0))
    p = backup.create_backup(conn, path, tag="manual", backup_dir=bdir, when=_dt(1))
    assert p.suffix == backup.FULL_EXT and not backup.is_delta(p)


def test_rebase_when_too_much_of_the_file_changed(src, tmp_path, monkeypatch):
    """Past the re-base threshold a delta stops being worth it and a fresh full
    snapshot is written instead -- which also becomes the next baseline."""
    conn, path = src
    bdir = tmp_path / "backups"
    first = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                 incremental=True, when=_dt(0))
    monkeypatch.setattr(backup, "REBASE_FRACTION", 0.0)   # nothing is cheap enough
    acct = ledger.list_accounts(conn)[0]["id"]
    ledger.add_transaction(conn, acct, "2026-08-18", -25_00, payee="Safeway")
    second = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                  incremental=True, when=_dt(1))
    assert second.suffix == backup.FULL_EXT
    assert second != first
    assert backup.latest_full(path, tag="auto", backup_dir=bdir) == second


def test_prune_counts_deltas_and_never_orphans_a_live_baseline(src, tmp_path):
    """Rotation is the one way an incremental scheme can lose data: drop the
    baseline and every delta pointing at it becomes unrestorable. The baseline is
    held back past its turn while a surviving delta still needs it."""
    conn, path = src
    bdir = tmp_path / "backups"
    acct = ledger.list_accounts(conn)[0]["id"]
    full = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                                incremental=True, when=_dt(0))
    for s in range(1, 5):
        ledger.add_transaction(conn, acct, "2026-08-18", -100 * s, payee=f"P{s}")
        conn.commit()
        backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                             incremental=True, when=_dt(s))
    backup.prune_backups(path, tag="auto", keep=2, backup_dir=bdir)
    survivors = backup.list_backups(path, tag="auto", backup_dir=bdir)
    # keep=2 counts restore points; the baseline stays because they still need it.
    assert full.exists() and full in survivors
    assert sum(1 for p in survivors if p.suffix == backup.DELTA_EXT) == 2
    for d in [p for p in survivors if p.suffix == backup.DELTA_EXT]:
        assert backup.restore_bytes(d)          # still restorable after rotation


def test_purge_keeps_a_baseline_its_surviving_deltas_still_need(tmp_path):
    """Same protection on the time-based path: an old baseline outlives the
    retention window while a within-window delta still references it."""
    import json
    bdir = tmp_path / "backups"
    bdir.mkdir(parents=True)
    old_base = _auto_file(bdir, _NOW - timedelta(days=10))
    stamp = _NOW.strftime("%Y%m%d_%H%M%S")
    fresh = bdir / f"mammon_2026.db.auto.{stamp}.delta"
    fresh.write_bytes(backup.DELTA_MAGIC + b"\n"
                      + json.dumps({"baseline": old_base.name}).encode() + b"\n")
    for i in range(4):                       # push the pair past the safety floor
        _auto_file(bdir, _NOW - timedelta(minutes=i + 1))
    removed = backup.purge_auto_backups(backup_dir=bdir, now=_NOW)
    assert old_base.exists() and old_base not in removed


# ---------------------------------------------------------------------------
# One folder per database, and a restore that refuses a foreign snapshot
# ---------------------------------------------------------------------------
def test_snapshot_db_name_identifies_the_ledger_it_came_from():
    S = backup.snapshot_db_name
    assert S("mammon.db.auto.20260831_120000.bak") == "mammon.db"
    assert S("mammon2.db.manual.20260831_120000.delta") == "mammon2.db"
    # a hand-made checkpoint still names its database -- it is a real snapshot
    assert S("mammon.db.pre2001import.2.bak") == "mammon.db"
    assert S("mammon2.db.pre2001import.2.bak") == "mammon2.db"
    # a database whose name does not end in .db still parses by tag/stamp
    assert S("ledger.sqlite.auto.20260831_120000.bak") == "ledger.sqlite"
    # claims nothing rather than claiming wrongly
    assert S("mammon.db") is None
    assert S("random.txt") is None


def test_restoring_another_databases_backup_is_refused(tmp_path):
    """The incident this exists for: a scratch mammon2.db was open earlier in the
    day, and hours later its snapshot -- the newest-looking file in a shared
    folder -- was restored over the real ledger. It succeeds at the file level
    and leaves a valid, completely WRONG ledger in place."""
    bdir = tmp_path / "backups"
    real = tmp_path / "mammon.db"
    scratch = tmp_path / "mammon2.db"
    for path, payee in ((real, "Real Ledger"), (scratch, "Scratch")):
        conn = db.init_db(path)
        acct = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
        ledger.add_transaction(conn, acct, "2026-08-01", -100, payee=payee)
        conn.close()
    foreign = backup.create_backup(scratch, backup_dir=bdir)

    with pytest.raises(backup.ForeignSnapshotError) as err:
        backup.restore_backup(foreign, real)
    assert "mammon2.db" in str(err.value) and "mammon.db" in str(err.value)
    # ...and the real ledger is untouched
    conn = db.connect(real)
    assert conn.execute(
        "SELECT payee FROM transactions").fetchone()["payee"] == "Real Ledger"
    conn.close()

    # its OWN snapshot still restores
    own = backup.create_backup(real, backup_dir=bdir)
    assert backup.restore_backup(own, real) == real


def test_backups_land_in_a_folder_per_database(tmp_path):
    bdir = tmp_path / "backups"
    for name in ("mammon.db", "mammon2.db"):
        conn = db.init_db(tmp_path / name)
        conn.close()
        backup.create_backup(tmp_path / name, backup_dir=bdir)

    assert (bdir / "mammon.db").is_dir() and (bdir / "mammon2.db").is_dir()
    # each listing sees only its own database's snapshots
    mine = backup.list_backups(tmp_path / "mammon.db", backup_dir=bdir)
    assert len(mine) == 1
    assert all(p.parent.name == "mammon.db" for p in mine)
    assert backup.snapshot_db_name(mine[0]) == "mammon.db"


def test_legacy_flat_snapshots_are_still_found_and_organizable(tmp_path):
    """Per-database folders must not strand what is already on disk."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    src = tmp_path / "mammon.db"
    conn = db.init_db(src)
    acct = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    ledger.add_transaction(conn, acct, "2026-08-01", -100, payee="Old")
    conn.close()
    # a snapshot in the OLD flat layout, plus another database's
    legacy = bdir / "mammon.db.auto.20260101_000000.bak"
    legacy.write_bytes(src.read_bytes())
    (bdir / "mammon2.db.auto.20260101_000000.bak").write_bytes(src.read_bytes())

    found = backup.list_backups(src, backup_dir=bdir)
    assert [p.name for p in found] == [legacy.name]       # and not mammon2's

    moved = backup.organize_backups(backup_dir=bdir)
    assert {(s.name, d.parent.name) for s, d in moved} == {
        ("mammon.db.auto.20260101_000000.bak", "mammon.db"),
        ("mammon2.db.auto.20260101_000000.bak", "mammon2.db"),
    }
    assert not legacy.exists()
    still = backup.list_backups(src, backup_dir=bdir)
    assert [p.name for p in still] == ["mammon.db.auto.20260101_000000.bak"]
    assert still[0].parent.name == "mammon.db"


def test_a_delta_finds_a_baseline_left_in_the_legacy_folder(tmp_path):
    """A delta written into the new folder can name a baseline still sitting flat
    in the parent. Losing that lookup would cost a real restore point."""
    bdir = tmp_path / "backups"
    src = tmp_path / "mammon.db"
    conn = db.init_db(src)
    acct = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    ledger.add_transaction(conn, acct, "2026-08-01", -100, payee="One")
    conn.close()

    full = backup.create_backup(src, backup_dir=bdir, tag=backup.AUTO_TAG)
    conn = db.connect(src)
    ledger.add_transaction(conn, acct, "2026-08-02", -250, payee="Two")
    conn.close()
    delta = backup.create_backup(src, backup_dir=bdir, tag=backup.AUTO_TAG,
                                 incremental=True)
    assert delta.suffix == backup.DELTA_EXT

    # simulate the pre-folder layout for the baseline only
    legacy_baseline = bdir / full.name
    full.replace(legacy_baseline)
    out = backup.restore_backup(delta, tmp_path / "rebuilt.db")
    conn = db.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    conn.close()
