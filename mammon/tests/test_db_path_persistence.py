"""Durability of learned rules across app restarts, at the DB-resolution layer.

Payee-renaming and category learning were reported as "not retained across
sessions" even though every rule write commits (see test_rename_tree.py /
test_import_review_prediction.py, which prove the SQLite round-trip). The real
cause lived one level up, in ``mammon.app._resolve_db``: it resolved the default
database (``data/mammon.db`` and its ``mammon.db`` fallback) RELATIVE TO THE
CURRENT WORKING DIRECTORY. Launching the app from a different directory silently
opened a *different* -- often brand-new, empty -- database, so rules committed in
one session were invisible the next. The fix anchors the default DB to the Mammon
install root, independent of the CWD.

These tests pin that behaviour and prove an end-to-end two-session cycle: learn
in session 1 (launched from dir A) -> restart from dir B -> the persisted payee
AND category are auto-filled on a matching import.
"""
from __future__ import annotations

import os

import pytest

from mammon import app, category_rules, db, import_review, ledger, rename_tree


def _row(desc, *, tid="", amount="12.34", debit=True, date="2026-05-01"):
    return {
        "transactionId": tid,
        "postedDate": date,
        "amount": amount,
        "isDebit": debit,
        "statementDescription": desc,
    }


# ---------------------------------------------------------------------------
# _resolve_db: CWD-independence (the actual regression)
# ---------------------------------------------------------------------------
def test_resolve_db_explicit_arg_wins(tmp_path):
    assert app._resolve_db("/somewhere/custom.db", root=tmp_path) == "/somewhere/custom.db"


def test_resolve_db_is_cwd_independent(tmp_path):
    install = tmp_path / "install"
    (install / "data").mkdir(parents=True)
    anchored = install / "data" / "mammon.db"
    anchored.write_bytes(b"")  # pretend a real DB already lives at the anchor

    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    orig = os.getcwd()
    try:
        os.chdir(a)
        p1 = app._resolve_db(None, root=install)
        os.chdir(b)
        p2 = app._resolve_db(None, root=install)
    finally:
        os.chdir(orig)

    assert p1 == p2 == str(anchored)   # same file regardless of launch directory
    assert os.path.isabs(p1)           # anchored, not a bare relative "mammon.db"


def test_resolve_db_ignores_stray_cwd_db_when_anchored_exists(tmp_path):
    # A leftover mammon.db from the old CWD-relative behaviour must NOT hijack the
    # session away from the real anchored database.
    install = tmp_path / "install"
    (install / "data").mkdir(parents=True)
    (install / "data" / "mammon.db").write_bytes(b"")
    stray = tmp_path / "elsewhere"
    stray.mkdir()
    (stray / "mammon.db").write_bytes(b"")

    orig = os.getcwd()
    try:
        os.chdir(stray)
        p = app._resolve_db(None, root=install)
    finally:
        os.chdir(orig)

    assert p == str(install / "data" / "mammon.db")


def test_resolve_db_explicit_new_path_is_authoritative(tmp_path):
    # A not-yet-created explicit --db (relative OR absolute) must be returned as
    # given -- NEVER swapped for the anchored mammon.db just because it does not
    # exist yet. This is the reported bug: pointing --db at a fresh path silently
    # reverted to data/mammon.db.
    install = tmp_path / "install"
    (install / "data").mkdir(parents=True)
    (install / "data" / "mammon.db").write_bytes(b"")  # a real anchored default exists
    new_abs = tmp_path / "brand" / "new.db"            # does not exist yet
    assert app._resolve_db(str(new_abs), root=install) == str(new_abs)
    assert app._resolve_db("relative_new.db", root=install) == "relative_new.db"


def test_resolve_db_directory_arg_fails_loudly(tmp_path):
    # An explicit --db that names an existing directory cannot be a database
    # file: fail loudly instead of silently opening a different database.
    with pytest.raises(SystemExit) as exc:
        app._resolve_db(str(tmp_path), root=tmp_path)
    assert "directory" in str(exc.value)


def test_main_explicit_db_opens_exact_path_not_mammon(tmp_path, monkeypatch):
    # End-to-end through main(): an explicit --db opens/creates EXACTLY that file
    # and hands the GUI a connection to it -- never the anchored mammon.db.
    captured = {}

    def fake_launch(conn, db_path, account):
        captured["db_path"] = db_path
        captured["conn"] = conn
        return 0

    monkeypatch.setattr(app, "_launch_gui", fake_launch)
    monkeypatch.setattr(app.crashlog, "install_excepthook", lambda *a, **k: None)

    target = tmp_path / "sub" / "custom.db"  # new path, parent dir absent
    rc = app.main(["--db", str(target)])

    assert rc == 0
    assert target.exists()                        # created exactly where asked
    assert captured["db_path"] == str(target)     # GUI got the explicit path
    # The connection really points at the explicit file, not mammon.db.
    (real,) = captured["conn"].execute("PRAGMA database_list").fetchall()[:1]
    assert os.path.samefile(real[2], target)


def test_main_explicit_existing_db_opens_that_db(tmp_path, monkeypatch):
    # The reported bug, taken head-on: ``main(["--db", <existing db>])`` must open
    # THAT file (not the anchored mammon.db). Distinct from the new-path test above
    # (which proves creation) and the argv=None test below (which proves sys.argv
    # is read): here the DB already exists and --db is passed explicitly, so this
    # pins the "existing" half of "explicit --db is authoritative, existing OR new".
    existing = tmp_path / "already_here.db"
    seed = db.init_db(str(existing))
    ledger.create_account(seed, "Marker Checking", "checking")
    seed.commit()
    seed.close()

    captured = {}

    def fake_launch(conn, db_path, account):
        captured["db_path"] = db_path
        captured["conn"] = conn
        return 0

    monkeypatch.setattr(app, "_launch_gui", fake_launch)
    monkeypatch.setattr(app.crashlog, "install_excepthook", lambda *a, **k: None)

    rc = app.main(["--db", str(existing)])

    assert rc == 0
    assert captured["db_path"] == str(existing)          # exact file, not mammon.db
    (real,) = captured["conn"].execute("PRAGMA database_list").fetchall()[:1]
    assert os.path.samefile(real[2], existing)
    # Pre-existing data is intact -> it really is the SAME file, not a fresh one.
    names = [r[0] for r in captured["conn"].execute(
        "SELECT name FROM accounts").fetchall()]
    assert "Marker Checking" in names


def test_main_explicit_directory_errors_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_launch_gui", lambda *a, **k: 0)
    monkeypatch.setattr(app.crashlog, "install_excepthook", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        app.main(["--db", str(tmp_path)])       # tmp_path is a directory
    assert "directory" in str(exc.value)


def test_main_cli_argv_none_honours_explicit_db(tmp_path, monkeypatch):
    # THE regression: ``python -m mammon.app --db X`` reaches main() via
    # ``sys.exit(main())`` with argv=None. main() must then read sys.argv, NOT an
    # empty list -- the old ``parse_args([] if argv is None else argv)`` silently
    # dropped the real command line so a VALID existing --db was ignored and
    # startup fell back to the anchored mammon.db. Here we pre-create a real DB
    # with a marker account, point --db at it on sys.argv, and prove main() opens
    # THAT file (marker visible), never mammon.db.
    existing = tmp_path / "tom_existing.db"
    seed = db.init_db(str(existing))
    ledger.create_account(seed, "the user Checking", "checking")
    seed.commit()
    seed.close()

    captured = {}

    def fake_launch(conn, db_path, account):
        captured["db_path"] = db_path
        captured["conn"] = conn
        return 0

    monkeypatch.setattr(app, "_launch_gui", fake_launch)
    monkeypatch.setattr(app.crashlog, "install_excepthook", lambda *a, **k: None)
    # Simulate the real CLI: sys.argv carries --db and main() is called bare.
    monkeypatch.setattr("sys.argv", ["mammon.app", "--db", str(existing)])

    rc = app.main()  # argv=None -> must fall through to sys.argv

    assert rc == 0
    assert captured["db_path"] == str(existing)          # exact file, not mammon.db
    (real,) = captured["conn"].execute("PRAGMA database_list").fetchall()[:1]
    assert os.path.samefile(real[2], existing)
    # The pre-existing data is intact -> it really is the SAME file, not a fresh one.
    names = [r[0] for r in captured["conn"].execute(
        "SELECT name FROM accounts").fetchall()]
    assert "the user Checking" in names


def test_resolve_db_default_prefers_mammon_when_present(tmp_path):
    # The no-arg default is UNCHANGED: with an anchored mammon.db present it is
    # resolved (regression guard that the explicit-path fix left the default
    # fallback chain intact).
    install = tmp_path / "install"
    (install / "data").mkdir(parents=True)
    (install / "data" / "mammon.db").write_bytes(b"")
    assert app._resolve_db(None, root=install) == str(install / "data" / "mammon.db")


def test_resolve_db_ignores_cwd_relative_db(tmp_path):
    # The default is ANCHORED to the install root, so a database sitting in the
    # current working directory is never picked up. This is the guarantee that
    # keeps one launch from opening a different file than the next: the CWD
    # cannot change which database you get.
    install = tmp_path / "install"
    install.mkdir()                      # no data/mammon.db here yet
    cwd = tmp_path / "cwd"
    (cwd / "data").mkdir(parents=True)
    (cwd / "data" / "mammon.db").write_bytes(b"")   # a decoy under the CWD

    orig = os.getcwd()
    try:
        os.chdir(cwd)
        p = app._resolve_db(None, root=install)
    finally:
        os.chdir(orig)

    # the anchored path wins even though it does not exist yet
    assert p == str(install / "data" / "mammon.db")
    assert os.path.abspath(p) != os.path.abspath(cwd / "data" / "mammon.db")


# ---------------------------------------------------------------------------
# End-to-end: learning survives an app restart AND a change of working directory
# ---------------------------------------------------------------------------
def test_learning_survives_restart_across_working_dirs(tmp_path):
    """Session 1 (launched from dir A) learns a payee + category from a user edit;
    session 2 (fresh load, launched from dir B) auto-fills BOTH from the persisted
    rules. This is the two-load-cycle guarantee the user asked for, exercised through
    the real ``_resolve_db`` default-resolution path."""
    install = tmp_path / "install"
    (install / "data").mkdir(parents=True)
    cwd_a = tmp_path / "cwd_a"
    cwd_a.mkdir()
    cwd_b = tmp_path / "cwd_b"
    cwd_b.mkdir()
    orig = os.getcwd()

    def open_db_like_main(cwd):
        # Mirror mammon.app.main's DB-opening steps: resolve (CWD-independent),
        # ensure the parent dir exists, init/upgrade, return the path + conn.
        os.chdir(cwd)
        from pathlib import Path
        path = app._resolve_db(None, root=install)
        Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
        return path, db.init_db(path)

    try:
        # ---- Session 1: launched from cwd_a; user corrects payee + sets category.
        path1, c1 = open_db_like_main(cwd_a)
        acct = ledger.create_account(c1, "Checking", "checking")
        cid = ledger.resolve_category(c1, "Dining")

        m = import_review.map_row(_row("POS COFFEE SHOP 12", tid="C1"))
        _, pre_cat = import_review.predict_fields(c1, m)
        assert pre_cat is None  # nothing learned yet
        import_review.save_new(c1, acct, m, payee="Coffee Shop", category_id=cid)
        # Enough corrections to make the payee HIGH-CONFIDENCE, so it still
        # prefills after the restart rather than showing the raw text. Keyed to
        # the constant rather than a literal: the entropy-ranked tree raised the
        # floor from 2 to 4, and a test that hardcodes the old number reports the
        # rewrite as a persistence bug.
        for n in range(rename_tree.HIGH_CONFIDENCE_MIN_COUNT - 1):
            mb = import_review.map_row(
                _row("POS COFFEE SHOP %d" % (22 + n), tid="C1b%d" % n))
            import_review.save_new(c1, acct, mb, payee="Coffee Shop",
                                   category_id=cid)
        # committed to disk by save_new's self-committing rule writes
        assert rename_tree.list_nodes(c1)      # the rename tree learned a payee
        assert category_rules.list_rules(c1)   # a category rule row exists
        c1.close()

        # ---- Session 2: brand-new process, launched from a DIFFERENT directory.
        path2, c2 = open_db_like_main(cwd_b)
        assert path2 == path1  # the fix: same DB file despite the different CWD

        m2 = import_review.map_row(_row("POS COFFEE SHOP 34", tid="C2"))
        payee, cat = import_review.predict_fields(c2, m2)
        assert payee == "Coffee Shop"  # payee rename survived the restart
        assert cat == cid              # category learning survived the restart
        c2.close()
    finally:
        os.chdir(orig)
