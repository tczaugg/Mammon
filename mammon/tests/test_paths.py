"""Where the data lives, across the three ways Mammon runs.

This exists because the rule was previously implemented three times and the
copies disagreed: ``$MAMMON_DATA_DIR`` moved the backups and the download log
but not the database, so a test run or an alternate install wrote its ledger
into the real one. The property worth protecting is that all three answers come
from one place and therefore cannot drift apart again.
"""

import os
import sys
from pathlib import Path

import pytest

from mammon import app, backup, download_log, paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MAMMON_DATA_DIR", raising=False)
    monkeypatch.delenv("MAMMON_REPORT_DEFS_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)


def test_a_source_checkout_uses_the_data_dir_beside_the_package():
    """Unchanged behavior: this is how it has always run, and how it must keep
    running for everyone working from a clone."""
    assert paths.data_dir() == paths.install_root() / "data"
    assert paths.default_db_path().name == "mammon.db"


def test_the_env_var_moves_everything_together(tmp_path, monkeypatch):
    """THE regression. The database used to ignore MAMMON_DATA_DIR while the
    backups and the download log obeyed it."""
    monkeypatch.setenv("MAMMON_DATA_DIR", str(tmp_path))
    assert paths.data_dir() == tmp_path
    assert Path(app._resolve_db(None)).parent == tmp_path        # the database
    assert backup.default_backup_dir() == tmp_path / "backups"   # the snapshots
    assert Path(download_log.default_data_dir()) == tmp_path     # the log


def test_a_packaged_build_writes_under_the_users_home(monkeypatch):
    """An installed app cannot write beside itself: Program Files is read-only
    to a standard user, and Windows silently redirects the writes into a
    VirtualStore copy rather than failing, so the ledger appears to save and
    then appears to vanish."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    d = paths.data_dir()
    assert d != paths.install_root() / "data"
    assert paths.install_root() not in d.parents
    assert d.name == paths.APP_DIR_NAME
    assert Path.home() in d.parents


def test_the_env_var_still_wins_in_a_packaged_build(tmp_path, monkeypatch):
    """Precedence: an explicit override beats the frozen-build default, or a
    packaged build could never be pointed at a test directory."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("MAMMON_DATA_DIR", str(tmp_path))
    assert paths.data_dir() == tmp_path


def test_a_home_without_documents_does_not_invent_one(tmp_path, monkeypatch):
    """Redirected Windows profiles and plenty of Linux homes have no Documents.
    Falling back to ~/Mammon is better than creating a Documents folder the
    user never asked for."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert paths.user_data_dir() == tmp_path / paths.APP_DIR_NAME
    (tmp_path / "Documents").mkdir()
    assert paths.user_data_dir() == tmp_path / "Documents" / paths.APP_DIR_NAME


def test_resolving_a_path_creates_nothing(tmp_path, monkeypatch):
    """Importing or querying must never leave a stray folder behind; the
    callers that write are the ones that create."""
    target = tmp_path / "nothing-here"
    monkeypatch.setenv("MAMMON_DATA_DIR", str(target))
    paths.data_dir()
    paths.default_db_path()
    backup.default_backup_dir()
    assert not target.exists()


def test_the_installer_marker_makes_a_copy_packaged(tmp_path, monkeypatch):
    """THE installer regression. The installer runs the stock embeddable Python,
    which sets no sys.frozen, so with that as the only test an installed copy
    took itself for a source checkout and kept the ledger inside the install
    folder -- the folder the uninstaller deletes and every upgrade replaces."""
    root = tmp_path / "LocalAppData" / "Mammon"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "install_root", lambda: root)
    assert not paths.is_installed()
    assert paths.data_dir() == root / "data"          # a checkout, until marked

    (root / paths.INSTALL_MARKER).write_text("{}")
    assert paths.is_installed() and paths.is_packaged()
    assert paths.data_dir() == paths.user_data_dir()
    assert root not in paths.data_dir().parents


def test_a_directory_named_like_the_marker_is_not_an_install(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "install_root", lambda: tmp_path)
    (tmp_path / paths.INSTALL_MARKER).mkdir()
    assert not paths.is_installed()


def test_the_last_db_pointer_follows_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MAMMON_DATA_DIR", str(tmp_path))
    assert paths.last_db_record_path() == tmp_path / paths.LAST_DB_RECORD


def test_report_definitions_live_under_the_data_dir(tmp_path, monkeypatch):
    """A definition the user writes for his own return names his forms and his
    lines, so it is his data: it follows MAMMON_DATA_DIR with the ledger, and it
    is gitignored with the rest of data/."""
    assert paths.report_defs_dir() == paths.data_dir() / "report_defs"
    monkeypatch.setenv("MAMMON_DATA_DIR", str(tmp_path))
    assert paths.report_defs_dir() == tmp_path / "report_defs"


def test_the_report_definitions_dir_has_its_own_override(tmp_path, monkeypatch):
    """Its own variable, so a test (and a read-only install) can point the
    definitions somewhere without moving the ledger too."""
    monkeypatch.setenv("MAMMON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAMMON_REPORT_DEFS_DIR", str(tmp_path / "defs"))
    assert paths.report_defs_dir() == tmp_path / "defs"
    assert paths.data_dir() == tmp_path / "data"       # and only that


def test_asking_for_the_report_definitions_dir_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MAMMON_REPORT_DEFS_DIR", str(tmp_path / "defs"))
    paths.report_defs_dir()
    assert not (tmp_path / "defs").exists()


def test_an_explicit_db_argument_still_wins(tmp_path):
    """--db is authoritative and used verbatim, whatever the data dir says."""
    explicit = tmp_path / "elsewhere.db"
    assert app._resolve_db(str(explicit)) == str(explicit)
