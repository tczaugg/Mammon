"""Per-snapshot change summaries (see mammon.backup).

A snapshot's file name records only a moment and a tag, so a hundred one-minute
auto-backups are indistinguishable when picking a restore point. Each snapshot
now also carries a lightweight, read-only manifest of ledger totals plus a
one-line "what changed" summary diffing it against the previous snapshot of the
same tag. These tests prove the manifest is accurate, that both storage forms
(full-.bak sidecar and delta header) carry it, that the CLI listing surfaces it,
that retention removes a full snapshot's sidecar with it without orphaning a
baseline, and -- the load-bearing invariant -- that the extra header keys never
perturb a delta's reconstructed-image checksum.

Synthetic ledgers only: account names and payees below are invented, never real.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from mammon import backup, db, ledger


def _dt(sec=0):
    return datetime(2026, 8, 18, 12, 30, sec)


def _new_db(tmp_path, name="mammon_2026.db"):
    """A real on-disk ledger with one account opening at $100.00."""
    path = tmp_path / name
    conn = db.init_db(path)
    aid = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    conn.commit()
    return conn, path, aid


def test_summary_reports_added_txns_and_balance_delta(tmp_path):
    conn, path, aid = _new_db(tmp_path)
    bdir = tmp_path / "backups"
    # First snapshot: full baseline (no baseline yet -> stays .bak), empty account.
    b1 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                              incremental=True, when=_dt(1))
    assert b1.suffix == ".bak"

    ledger.add_transaction(conn, aid, "2026-08-02", -10_00, payee="Coffee")
    ledger.add_transaction(conn, aid, "2026-08-03", -20_00, payee="Books")
    ledger.add_transaction(conn, aid, "2026-08-04", 5_00, payee="Refund")
    conn.commit()
    b2 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                              incremental=True, when=_dt(2))
    assert backup.is_delta(b2)                       # small change -> delta

    meta = backup.read_meta(b2)
    m = meta["manifest"]
    assert m["total_txns"] == 3
    acct = next(a for a in m["accounts"] if a["name"] == "Checking")
    assert acct["txns"] == 3
    # opening 100.00 then -10.00 -20.00 +5.00 -> 75.00 (all integer cents)
    assert acct["balance_cents"] == 100_00 - 10_00 - 20_00 + 5_00
    assert acct["last_payee"] == "Refund"

    summary = meta["summary"]
    assert "+3 txns" in summary
    assert "Checking" in summary
    assert "100.00->75.00" in summary


def test_full_bak_sidecar_and_delta_header_both_carry_manifest(tmp_path):
    conn, path, aid = _new_db(tmp_path)
    bdir = tmp_path / "backups"

    # A full .bak carries its manifest/summary in a sidecar next to it.
    b1 = backup.create_backup(conn, path, tag="manual", backup_dir=bdir, when=_dt(1))
    assert b1.suffix == ".bak"
    side = backup._meta_path(b1)
    assert side.exists()
    data = json.loads(side.read_text(encoding="utf-8"))
    assert data["manifest"]["total_txns"] == 0
    assert "baseline / initial" in data["summary"]

    # A delta carries them inside its own JSON header, not a sidecar.
    b0 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir, when=_dt(2))
    ledger.add_transaction(conn, aid, "2026-08-03", -20_00, payee="Books")
    conn.commit()
    b2 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                              incremental=True, when=_dt(3))
    assert backup.is_delta(b2)
    header = backup.delta_header(b2)
    assert "manifest" in header and "summary" in header
    assert header["manifest"]["total_txns"] == 1
    assert not backup._meta_path(b2).exists()       # deltas need no sidecar


def test_cli_list_includes_the_summary(tmp_path, capsys):
    conn, path, aid = _new_db(tmp_path)
    bdir = tmp_path / "backups"
    backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                         incremental=True, when=_dt(1))
    ledger.add_transaction(conn, aid, "2026-08-02", -10_00, payee="Coffee")
    conn.commit()
    backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                         incremental=True, when=_dt(2))

    rc = backup._main(["list", "mammon_2026.db", "--dir", str(bdir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "baseline / initial" in out              # the first snapshot's line
    assert "+1 txns" in out                          # the delta's line


def test_prune_removes_a_full_snapshots_sidecar_with_it(tmp_path):
    conn, path, aid = _new_db(tmp_path)
    bdir = tmp_path / "backups"
    # Two independent full snapshots (no incremental), so pruning drops the older.
    b1 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir, when=_dt(1))
    ledger.add_transaction(conn, aid, "2026-08-02", -10_00, payee="Coffee")
    conn.commit()
    b2 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir, when=_dt(2))
    s1 = backup._meta_path(b1)
    assert s1.exists()

    removed = backup.prune_backups(path, tag="auto", keep=1, backup_dir=bdir)
    assert b1 in removed
    assert not b1.exists()
    assert not s1.exists()                           # sidecar deleted alongside
    assert b2.exists() and backup._meta_path(b2).exists()


def test_prune_never_orphans_a_baseline_or_its_sidecar(tmp_path):
    conn, path, aid = _new_db(tmp_path)
    bdir = tmp_path / "backups"
    b1 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                              incremental=True, when=_dt(1))   # full baseline
    assert b1.suffix == ".bak"
    ledger.add_transaction(conn, aid, "2026-08-02", -10_00, payee="Coffee")
    conn.commit()
    b2 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                              incremental=True, when=_dt(2))   # delta needs b1
    assert backup.is_delta(b2)
    s1 = backup._meta_path(b1)
    assert s1.exists()

    # keep=1 would retire b1 by age, but the surviving delta still needs it.
    removed = backup.prune_backups(path, tag="auto", keep=1, backup_dir=bdir)
    assert b1 not in removed
    assert b1.exists()                               # baseline held back...
    assert s1.exists()                               # ...and its sidecar with it
    assert b2.exists()


def test_purge_removes_sidecar_with_its_bak(tmp_path):
    # purge_auto_backups scans the flat root by *.auto.* glob, so fabricate a
    # root-level snapshot + sidecar the way test_backup._auto_file does.
    bdir = tmp_path / "backups"
    bdir.mkdir(parents=True)
    old = datetime(2020, 1, 1, 0, 0, 0)
    bak = bdir / f"mammon_2026.db.auto.{old.strftime('%Y%m%d_%H%M%S')}.bak"
    bak.write_bytes(b"snapshot")
    side = backup._meta_path(bak)
    side.write_text(json.dumps({"summary": "x", "manifest": {}}), encoding="utf-8")

    removed = backup.purge_auto_backups(backup_dir=bdir, floor=0, now=_dt(0))
    assert bak in removed
    assert not bak.exists()
    assert not side.exists()                         # sidecar purged too


def test_delta_with_manifest_still_restores_against_its_checksum(tmp_path):
    conn, path, aid = _new_db(tmp_path)
    bdir = tmp_path / "backups"
    backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                         incremental=True, when=_dt(1))
    for i, amt in enumerate((-10_00, -20_00, 5_00)):
        ledger.add_transaction(conn, aid, f"2026-08-0{i + 2}", amt, payee=f"P{i}")
    conn.commit()
    b2 = backup.create_backup(conn, path, tag="auto", backup_dir=bdir,
                              incremental=True, when=_dt(2))
    assert backup.is_delta(b2)
    assert "manifest" in backup.delta_header(b2)     # header carries the manifest

    # restore_backup verifies the reconstructed image against the sha256 recorded
    # when the delta was written; it raises on mismatch. The manifest keys live
    # only in the (unhashed) header, so this must still succeed.
    out = backup.restore_backup(b2, tmp_path / "restored.db")
    c = db.connect(out)
    try:
        assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3
    finally:
        c.close()
