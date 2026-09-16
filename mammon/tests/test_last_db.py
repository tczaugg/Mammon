"""A launch with no --db opens the database the user last chose in the window.

A GUI launch (Start Menu, taskbar pin) has no command line, so this is the only
way a ledger kept anywhere other than <data dir>/mammon.db opens by itself. The
properties worth pinning are the ones whose failure is silent: an explicit
--db must not become the default (scratch ledgers would replace the real one),
a missing remembered file must not be forgotten (an unmounted drive would lose
track of the ledger for good), and a test-built window must never write the
pointer.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import app, db, last_db, mcp_server, paths


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("MAMMON_DATA_DIR", str(d))
    return d


@pytest.fixture
def ledger_file(tmp_path):
    p = tmp_path / "elsewhere" / "household.db"
    p.parent.mkdir()
    db.init_db(p).close()
    return p


def test_with_nothing_remembered_a_launch_opens_the_default(data_dir):
    assert last_db.remembered() is None
    assert last_db.startup_db() == paths.default_db_path()
    assert app._resolve_db(None) == str(data_dir / "mammon.db")
    assert last_db.missing_remembered() is None


def test_a_remembered_database_is_what_the_next_launch_opens(ledger_file):
    assert last_db.remember(ledger_file) is True
    assert app._resolve_db(None) == str(ledger_file.resolve())
    assert last_db.missing_remembered() is None


def test_the_pointer_lives_in_the_data_dir_so_the_env_var_isolates_it(
        data_dir, ledger_file):
    last_db.remember(ledger_file)
    record = data_dir / paths.LAST_DB_RECORD
    assert record.is_file()
    assert json.loads(record.read_text(encoding="utf-8")) == {
        "path": str(ledger_file.resolve())}
    assert not record.with_name(record.name + ".tmp").exists()


def test_an_explicit_db_wins_and_is_never_remembered(
        tmp_path, data_dir, ledger_file, monkeypatch):
    """Developers, tests and agents launch with --db <scratch>. If that became
    the default, the next Start Menu launch would open the scratch ledger."""
    last_db.remember(ledger_file)
    scratch = tmp_path / "scratch.db"
    seen = {}

    def fake_launch(conn, db_path, account):
        seen["db_path"] = db_path
        conn.close()
        return 0

    monkeypatch.setattr(app, "_launch_gui", fake_launch)
    monkeypatch.setattr(app.crashlog, "install_excepthook", lambda *a, **k: None)
    assert app.main(["--db", str(scratch)]) == 0
    assert seen["db_path"] == str(scratch)
    assert last_db.remembered() == ledger_file.resolve()


def test_a_plain_launch_opens_the_remembered_ledger_end_to_end(
        ledger_file, monkeypatch):
    last_db.remember(ledger_file)
    seen = {}

    def fake_launch(conn, db_path, account):
        seen["db_path"] = db_path
        conn.close()
        return 0

    monkeypatch.setattr(app, "_launch_gui", fake_launch)
    monkeypatch.setattr(app.crashlog, "install_excepthook", lambda *a, **k: None)
    assert app.main([]) == 0
    assert seen["db_path"] == str(ledger_file.resolve())


def test_a_missing_remembered_file_falls_back_but_is_not_forgotten(
        data_dir, ledger_file, monkeypatch):
    """The usual cause is a drive that is not mounted yet. Overwriting the
    pointer with the fallback would lose track of the real ledger for good."""
    last_db.remember(ledger_file)
    gone = ledger_file.with_name("moved-away.db")
    ledger_file.rename(gone)

    assert last_db.startup_db() == data_dir / "mammon.db"
    assert last_db.missing_remembered() == ledger_file.resolve()

    seen = {}

    def fake_launch(conn, db_path, account, missing_last_db=None):
        seen["db_path"] = db_path
        seen["missing"] = missing_last_db
        conn.close()
        return 0

    monkeypatch.setattr(app, "_launch_gui", fake_launch)
    monkeypatch.setattr(app.crashlog, "install_excepthook", lambda *a, **k: None)
    assert app.main([]) == 0
    assert seen["db_path"] == str(data_dir / "mammon.db")
    assert seen["missing"] == ledger_file.resolve()
    # Still remembered: bring the drive back and the next launch finds it.
    gone.rename(ledger_file)
    assert last_db.startup_db() == ledger_file.resolve()


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"path": ""}',
                                     '{"path": 7}', '{"other": "x"}'])
def test_an_unreadable_pointer_counts_as_nothing_remembered(data_dir, content):
    data_dir.mkdir()
    (data_dir / paths.LAST_DB_RECORD).write_text(content, encoding="utf-8")
    assert last_db.remembered() is None
    assert last_db.startup_db() == data_dir / "mammon.db"


def test_failing_to_remember_never_raises(tmp_path, monkeypatch, ledger_file):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the data folder should be")
    monkeypatch.setenv("MAMMON_DATA_DIR", str(blocker))
    assert last_db.remember(ledger_file) is False


def test_the_mcp_server_serves_the_ledger_the_app_would_open(ledger_file):
    last_db.remember(ledger_file)
    assert mcp_server.default_db_path() == str(ledger_file.resolve())


# ---------------------------------------------------------------------------
# The window reports switches only through its hook.
# ---------------------------------------------------------------------------
@pytest.fixture
def qapp(tmp_path):
    from PyQt5.QtCore import QSettings
    from PyQt5.QtWidgets import QApplication
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(tmp_path / "qsettings"))
    return QApplication.instance() or QApplication([])


def test_open_database_reports_the_switch_to_the_hook(qapp, tmp_path, ledger_file):
    from mammon.ui.widgets import MainWindow
    first = tmp_path / "first.db"
    conn = db.init_db(first)
    opened = []
    win = MainWindow(conn, db_path=str(first), on_database_opened=opened.append)
    try:
        win.open_database(str(ledger_file))
        assert opened == [str(ledger_file)]
    finally:
        win.close()
        win.conn.close()


def test_a_window_without_the_hook_writes_no_pointer(qapp, tmp_path, data_dir,
                                                     ledger_file):
    """Every test builds MainWindow this way. None of them may move the default."""
    from mammon.ui.widgets import MainWindow
    first = tmp_path / "first.db"
    conn = db.init_db(first)
    win = MainWindow(conn, db_path=str(first))
    try:
        win.open_database(str(ledger_file))
    finally:
        win.close()
        win.conn.close()
    assert not (data_dir / paths.LAST_DB_RECORD).exists()
    assert last_db.remembered() is None


def test_a_failing_hook_does_not_stop_the_switch(qapp, tmp_path, ledger_file):
    from mammon.ui.widgets import MainWindow
    first = tmp_path / "first.db"
    conn = db.init_db(first)

    def broken(path):
        raise OSError("disk full")

    win = MainWindow(conn, db_path=str(first), on_database_opened=broken)
    try:
        win.open_database(str(ledger_file))
        assert win.db_path == str(ledger_file)
    finally:
        win.close()
        win.conn.close()
