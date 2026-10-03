"""Shared helpers for the test suite.

This project has NO conftest.py (CLAUDE.md): tests are invoked by path and each
file carries its own fixtures. That rule keeps collection explicit, but it also
means a helper several hundred test files want has nowhere to live. This module
is that place -- an ordinary import, not a pytest magic file.

Why `fresh_db` exists
---------------------
`db.init_db()` on a new file replays every migration in `db.MIGRATIONS` -- 75 of
them, measured at 259 ms. 181 test files call it across 320 sites, most of them
from a per-test fixture, so the suite spent roughly 826 seconds of CPU (about 52
seconds of wall clock at 16 xdist workers) rebuilding the SAME empty schema
thousands of times. Migrating once per process and copying the result costs
1.1 ms, and the copy is byte-identical: same 61 tables, same user_version,
`integrity_check` ok.

Copying a Mammon database is only safe on a CLEANLY CLOSED one. `db.connect`
opens WAL, and its own docstring records the trap: a copy of just the `.db`
file, taken while a `-wal` sidecar still holds commits, captures a stale
database. A clean `close()` checkpoints and removes the sidecars, so the
template is built, closed, and then verified to have left none behind before it
is ever copied.
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

import pytest

from mammon import db

def skip_under_xdist(reason: str):
    """Mark a test to run SERIALLY only.

    For the handful of Qt dialog tests that are sound on their own and fall over
    under ``-n auto``: they drive a modeless dialog through ``processEvents``
    while widgets are being created and destroyed, and under sixteen workers
    that reliably crashes one. Every assertion in them still runs on the serial
    suite, which is the correctness gate; what this removes is noise from the
    pre-push parallel run, where a gate that cries wolf one run in two is a gate
    people learn to ignore.

    Keyed off PYTEST_XDIST_WORKER, which xdist sets in each worker process.
    NOT a blanket skip: delete the mark and the test runs again the moment the
    underlying fault is fixed.
    """
    import os
    return pytest.mark.skipif(bool(os.environ.get("PYTEST_XDIST_WORKER")),
                              reason=reason)


#: (schema version, path) of this PROCESS's template. Keyed on the version so a
#: test that patches `db.MIGRATIONS` cannot be served a stale schema; each xdist
#: worker is its own process and so builds its own.
_TEMPLATE: tuple[int, Path] | None = None


def _migrated_template() -> Path:
    """A fully migrated, cleanly closed, empty database -- built once per process."""
    global _TEMPLATE
    if _TEMPLATE is None or _TEMPLATE[0] != db.SCHEMA_VERSION:
        folder = Path(tempfile.mkdtemp(prefix="mammon-schema-template-"))
        atexit.register(shutil.rmtree, folder, ignore_errors=True)
        path = folder / "template.db"
        db.init_db(path).close()          # checkpoints WAL away; see module docstring
        stale = sorted(p.name for p in folder.iterdir() if p.name != "template.db")
        if stale:                         # never copy a database with live sidecars
            raise RuntimeError(
                f"the schema template still has WAL sidecars {stale}; copying it "
                "would capture a stale database")
        _TEMPLATE = (db.SCHEMA_VERSION, path)
    return _TEMPLATE[1]


def fresh_db(path, key=None):
    """Drop-in for ``db.init_db(path)`` that copies the migrated template.

    Returns an open connection configured exactly as `db.init_db` would: the
    copy is handed straight back to `db.init_db`, which finds `user_version`
    already at `SCHEMA_VERSION`, applies no migration, and opens the connection
    through the usual `db.connect` conventions (row factory, foreign keys, WAL).

    Falls back to the real `db.init_db` whenever the shortcut would change
    meaning: an in-memory database, an encrypted one (its pages cannot come from
    a plain template), or a file that already exists with content -- which is
    the idempotent upgrade path, and the whole point of several tests.
    """
    if key is not None or str(path) == ":memory:":
        return db.init_db(path, key)
    target = Path(path)
    if target.exists() and target.stat().st_size:
        return db.init_db(path, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_migrated_template(), target)
    return db.init_db(target)
