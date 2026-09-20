"""The Windows installer's own logic (installer/install.py, build.py, uninstall.bat).

The full payload is exercised end to end by ``python installer\\build.py``, which
installs, runs and uninstalls it before zipping. These cover the parts whose
failure destroys or strands data and is cheap to provoke here: moving a ledger
in (copy, verify, then delete; never a file in use or from newer code), the
order that keeps an installed copy's data out of the install folder, and an
uninstaller that removes only what setup put there.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mammon import app, db, ledger
from mammon.tests import fresh_db

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "installer"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"installer_{name}", INSTALLER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module             # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


inst = _load("install")


def _ledger(path: Path, marker: str = "Checking") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = fresh_db(path)
    ledger.create_account(conn, marker, "checking")
    conn.commit()
    conn.close()
    return path


def _accounts(path: Path) -> list:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM accounts")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Moving a ledger in
# ---------------------------------------------------------------------------
def test_a_dragged_path_is_unquoted():
    assert inst.parse_user_path('  "C:\\My Files\\mammon.db" \n') == Path("C:\\My Files\\mammon.db")
    assert inst.parse_user_path("   ") is None


def test_a_data_folder_resolves_to_its_ledger(tmp_path):
    data = tmp_path / "data"
    _ledger(data / "mammon.db")
    _ledger(data / "scratch.db")
    assert inst.resolve_ledger_source(data) == data / "mammon.db"

    only = tmp_path / "only"
    _ledger(only / "family.db")
    assert inst.resolve_ledger_source(only) == only / "family.db"

    several = tmp_path / "several"
    _ledger(several / "a.db")
    _ledger(several / "b.db")
    with pytest.raises(inst.InstallError, match="several"):
        inst.resolve_ledger_source(several)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(inst.InstallError, match="no database"):
        inst.resolve_ledger_source(empty)
    with pytest.raises(inst.InstallError, match="does not exist"):
        inst.resolve_ledger_source(tmp_path / "nowhere")


def test_moving_a_ledger_carries_its_wal_and_backups_and_clears_the_source(tmp_path):
    clone = tmp_path / "clone" / "data"
    src = _ledger(clone / "mammon.db", "Moved Checking")
    wal = clone / "mammon.db-wal"
    wal.write_bytes(b"newest commits live here")
    snap = clone / "backups" / "mammon.db" / "mammon.db.manual.20260101_000000.bak"
    snap.parent.mkdir(parents=True)
    snap.write_bytes(b"snapshot")
    original = src.read_bytes()

    dest_dir = tmp_path / "Documents" / "Mammon"
    result = inst.move_ledger(src, dest_dir, db.SCHEMA_VERSION)

    assert result.database == dest_dir / "mammon.db"
    assert result.database.read_bytes() == original
    assert (dest_dir / "mammon.db-wal").read_bytes() == b"newest commits live here"
    assert (dest_dir / "backups" / "mammon.db" / snap.name).read_bytes() == b"snapshot"
    assert result.backups == dest_dir / "backups" / "mammon.db"
    assert result.warnings == []
    assert not src.exists() and not wal.exists() and not snap.parent.exists()
    assert not list(dest_dir.glob("*.partial")) and not list(clone.glob("*.moving"))


def test_a_ledger_from_newer_code_is_refused_and_left_alone(tmp_path):
    src = _ledger(tmp_path / "clone" / "mammon.db")
    conn = sqlite3.connect(src)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(inst.InstallError, match="newer Mammon"):
        inst.move_ledger(src, tmp_path / "dest", db.SCHEMA_VERSION)
    assert src.is_file()
    assert not (tmp_path / "dest" / "mammon.db").exists()


def test_a_taken_destination_is_refused_and_nothing_moves(tmp_path):
    src = _ledger(tmp_path / "clone" / "mammon.db", "Source")
    dest = _ledger(tmp_path / "dest" / "mammon.db", "Already There")
    with pytest.raises(inst.InstallError, match="already exists"):
        inst.move_ledger(src, dest.parent, db.SCHEMA_VERSION)
    assert _accounts(src) == ["Source"]
    assert _accounts(dest) == ["Already There"]


@windows_only
def test_a_ledger_in_use_is_not_moved(tmp_path):
    src = _ledger(tmp_path / "clone" / "mammon.db")
    with open(src, "rb"):                       # held open, like a running Mammon
        with pytest.raises(inst.InstallError, match="in use"):
            inst.move_ledger(src, tmp_path / "dest", db.SCHEMA_VERSION)
    assert src.is_file()
    assert not (tmp_path / "dest" / "mammon.db").exists()


def test_a_failed_copy_puts_the_source_back_exactly(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    src = _ledger(clone / "mammon.db", "Keep Me")
    (clone / "mammon.db-wal").write_bytes(b"wal")
    real_copy = inst.shutil.copy2
    calls = []

    def copy_then_fail(s, d, *a, **k):
        calls.append(s)
        if len(calls) == 2:                     # the database copied, the WAL fails
            raise OSError("disk full")
        return real_copy(s, d, *a, **k)

    monkeypatch.setattr(inst.shutil, "copy2", copy_then_fail)
    dest = tmp_path / "dest"
    with pytest.raises(inst.InstallError, match="left where it was"):
        inst.move_ledger(src, dest, db.SCHEMA_VERSION)
    # The WAL first: opening the database read-write (as _accounts does) checkpoints
    # and deletes it on close.
    assert (clone / "mammon.db-wal").read_bytes() == b"wal"
    assert not (clone / "mammon.db.moving").exists()
    assert _accounts(src) == ["Keep Me"]
    assert list(dest.iterdir()) == []


def test_an_unreadable_schema_moves_with_a_warning(tmp_path):
    src = tmp_path / "clone" / "secret.db"
    src.parent.mkdir()
    src.write_bytes(os.urandom(4096))           # what an encrypted ledger looks like
    result = inst.move_ledger(src, tmp_path / "dest", db.SCHEMA_VERSION)
    assert result.database == tmp_path / "dest" / "secret.db"
    assert any("could not be read" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Program files
# ---------------------------------------------------------------------------
def _payload(root: Path) -> Path:
    for rel in ("python/python.exe", "python/pythonw.exe", "site-packages/pkg.py",
                "mammon/app.py", "uninstall.bat", "mammon-mcp.bat"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    (root / "build-info.json").write_text('{"version": "9.9.9", "commit": "abc"}')
    return root


def test_an_incomplete_payload_says_to_extract_the_zip(tmp_path):
    payload = _payload(tmp_path / "payload")
    (payload / "site-packages" / "pkg.py").unlink()
    (payload / "site-packages").rmdir()
    with pytest.raises(inst.InstallError, match="Extract the WHOLE ZIP"):
        inst.check_payload(payload)
    assert inst.check_payload(_payload(tmp_path / "ok"))["version"] == "9.9.9"


def _source_checkout(root: Path) -> Path:
    """What GitHub's green "Code -> Download ZIP" leaves on disk: the repo, whose
    installer/ folder holds the seven scripts and none of the built payload."""
    installer = root / "Mammon-main" / "installer"
    installer.mkdir(parents=True)
    for name in ("build.py", "install.py", "setup.bat", "uninstall.bat",
                 "mammon-mcp.bat", "smoke_installed.py", "README.md"):
        (installer / name).write_text(name)
    for name in ("pyproject.toml", "requirements.txt"):
        (installer.parent / name).write_text(name)
    return installer


def test_the_source_archive_is_not_reported_as_a_half_extracted_zip(tmp_path):
    """Regression (a first-time installer, reported 2026-09-20): downloading the
    repo instead of the Setup ZIP makes every required file missing at once,
    which the old message blamed on a partial extraction -- so the advice was
    "Extract All", which he had already done, and could never fix. Name the real
    cause and point at Releases instead.
    """
    with pytest.raises(inst.InstallError) as exc:
        inst.check_payload(_source_checkout(tmp_path))
    msg = str(exc.value)
    assert "SOURCE CODE" in msg
    assert inst.RELEASES_URL in msg
    assert "Extract the WHOLE ZIP" not in msg      # the advice that loops


def test_a_real_partial_extraction_still_gets_the_extract_advice(tmp_path):
    """The two diagnoses must not bleed: a payload with no build.py beside it and
    no repo above it is a genuinely broken extraction, whatever is missing."""
    payload = _payload(tmp_path / "payload")
    shutil.rmtree(payload / "python")
    with pytest.raises(inst.InstallError, match="Extract the WHOLE ZIP"):
        inst.check_payload(payload)


def test_a_complete_payload_is_never_called_a_source_checkout(tmp_path):
    """build.py alone must not trip it -- only build.py AND the repo one level up,
    which a built payload never has."""
    payload = _payload(tmp_path / "ok")
    (payload / "build.py").write_text("stray")
    assert not inst.looks_like_source_checkout(payload)
    assert inst.check_payload(payload)["version"] == "9.9.9"


def test_the_marker_is_written_before_the_package_arrives(tmp_path, monkeypatch):
    """If an upgrade dies part-way, whatever copy of mammon is left must still
    send its data outside the folder the next upgrade deletes."""
    payload = _payload(tmp_path / "payload")
    install = tmp_path / "install"
    real_copytree = inst.shutil.copytree

    def copytree(src, dst, *a, **k):
        if Path(src).name == "mammon":
            assert (install / inst.INSTALL_MARKER).is_file()
            raise OSError("interrupted")
        return real_copytree(src, dst, *a, **k)

    monkeypatch.setattr(inst.shutil, "copytree", copytree)
    with pytest.raises(OSError):
        inst.replace_program(payload, install, {"version": "9.9.9"})
    assert (install / inst.INSTALL_MARKER).is_file()


def test_an_upgrade_replaces_program_folders_and_nothing_else(tmp_path):
    payload = _payload(tmp_path / "payload")
    install = tmp_path / "install"
    (install / "mammon").mkdir(parents=True)
    (install / "mammon" / "removed_in_new_version.py").write_text("stale")
    (install / "notes.txt").write_text("the user's own file")
    inst.replace_program(payload, install, {"version": "9.9.9"})
    assert not (install / "mammon" / "removed_in_new_version.py").exists()
    assert (install / "mammon" / "app.py").is_file()
    assert (install / "notes.txt").read_text() == "the user's own file"
    assert (install / "uninstall.bat").is_file()


@windows_only
def test_a_running_copy_stops_the_install(tmp_path):
    python_dir = tmp_path / "install" / "python"
    python_dir.mkdir(parents=True)
    held = python_dir / "python312.dll"
    held.write_bytes(b"x")
    with open(held, "rb"):
        with pytest.raises(inst.InstallError, match="running"):
            inst.ensure_not_running(tmp_path / "install")
    assert python_dir.is_dir()
    inst.ensure_not_running(tmp_path / "install")        # released: fine
    assert python_dir.is_dir() and not (tmp_path / "install" / "python.inuse-probe").exists()


@windows_only
def test_the_mark_of_the_web_is_stripped_from_launchable_files(tmp_path):
    exe = tmp_path / "python" / "pythonw.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    with open(str(exe) + ":Zone.Identifier", "w") as fh:
        fh.write("[ZoneTransfer]\nZoneId=3\n")
    assert inst.unblock(tmp_path) == 1
    assert not os.path.exists(str(exe) + ":Zone.Identifier")
    assert exe.read_bytes() == b"MZ"


# ---------------------------------------------------------------------------
# Start Menu and Settings > Apps
# ---------------------------------------------------------------------------
@windows_only
def test_the_shortcut_carries_the_apps_taskbar_identity(tmp_path):
    """Without the AppUserModelID a taskbar pin records pythonw.exe with no
    arguments, and clicking it later starts a bare Python."""
    target = tmp_path / "python" / "pythonw.exe"
    target.parent.mkdir()
    target.write_bytes(b"MZ")
    lnk = tmp_path / "Start Menu" / "Mammon.lnk"
    inst.create_shortcut(lnk, target, "-m mammon.app", tmp_path, tmp_path / "mammon.ico",
                         "Mammon", app.APP_USER_MODEL_ID)
    back = inst.read_shortcut(lnk)
    assert Path(back["target"]) == target
    assert back["arguments"] == "-m mammon.app"
    assert back["app_id"] == app.APP_USER_MODEL_ID


@windows_only
def test_the_settings_entry_points_at_the_uninstaller(tmp_path):
    import winreg
    key = r"Software\MammonInstallerTest_" + os.urandom(4).hex()
    install = tmp_path / "Mammon"
    install.mkdir()
    try:
        inst.register_uninstall(install, {"version": "9.9.9"}, key=key)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            assert winreg.QueryValueEx(k, "DisplayVersion")[0] == "9.9.9"
            assert winreg.QueryValueEx(k, "UninstallString")[0] == f'"{install / "uninstall.bat"}"'
            assert winreg.QueryValueEx(k, "QuietUninstallString")[0].endswith(" /quiet")
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)


# ---------------------------------------------------------------------------
# uninstall.bat
# ---------------------------------------------------------------------------
def _fake_install(root: Path) -> Path:
    for rel in ("python/python.exe", "site-packages/pkg/__init__.py", "mammon/app.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x")
    (root / inst.INSTALL_MARKER).write_text("{}")
    (root / "mammon-mcp.bat").write_text("@echo off")
    (root / "uninstall.bat").write_bytes((INSTALLER / "uninstall.bat").read_bytes())
    return root


def _uninstall(tmp_path, install: Path):
    start_menu = tmp_path / "startmenu"
    start_menu.mkdir(exist_ok=True)
    (start_menu / "Mammon.lnk").write_text("shortcut")
    env = dict(os.environ, MAMMON_START_MENU=str(start_menu), MAMMON_SKIP_REGISTRY="1",
               TEMP=str(tmp_path), TMP=str(tmp_path))
    subprocess.run(["cmd", "/c", str(install / "uninstall.bat"), "/quiet"], env=env,
                   capture_output=True, text=True, timeout=60)
    return start_menu


@windows_only
def test_uninstall_removes_what_setup_put_there(tmp_path):
    install = _fake_install(tmp_path / "Mammon")
    start_menu = _uninstall(tmp_path, install)
    assert not install.exists()
    assert not (start_menu / "Mammon.lnk").exists()
    assert not (tmp_path / "mammon-uninstall.bat").exists()     # temp copy cleaned up


@windows_only
def test_uninstall_never_deletes_what_setup_did_not_put_there(tmp_path):
    install = _fake_install(tmp_path / "Mammon")
    _ledger(install / "data" / "mammon.db", "Somebody's Ledger")
    _uninstall(tmp_path, install)
    assert _accounts(install / "data" / "mammon.db") == ["Somebody's Ledger"]
    assert not (install / "mammon").exists() and not (install / "python").exists()


@windows_only
def test_uninstall_refuses_a_folder_without_the_marker(tmp_path):
    install = _fake_install(tmp_path / "NotMammon")
    (install / inst.INSTALL_MARKER).unlink()
    start_menu = _uninstall(tmp_path, install)
    assert (install / "python" / "python.exe").is_file()
    assert (start_menu / "Mammon.lnk").exists()


@windows_only
def test_uninstall_leaves_a_running_copy_alone(tmp_path):
    install = _fake_install(tmp_path / "Mammon")
    with open(install / "python" / "python.exe", "rb"):
        start_menu = _uninstall(tmp_path, install)
    assert (install / "mammon" / "app.py").is_file()
    assert (start_menu / "Mammon.lnk").exists()


# ---------------------------------------------------------------------------
# build.py
# ---------------------------------------------------------------------------
build = _load("build")


def test_the_pth_puts_the_package_and_site_packages_on_the_isolated_path(tmp_path):
    (tmp_path / "python312._pth").write_text("python312.zip\n.\n#import site\n")
    pth = build.write_pth(tmp_path)
    lines = pth.read_text().splitlines()
    assert lines[:4] == ["python312.zip", ".", "..", "../site-packages"]
    assert lines[-1] == "import site"
    # pywin32's own .pth is never processed in ._pth mode, and mcp imports
    # pywintypes on Windows: the first real build died on exactly this.
    for folder in ("../site-packages/win32", "../site-packages/win32/lib",
                   "../site-packages/Pythonwin"):
        assert folder in lines


def test_the_test_runner_is_not_shipped_but_everything_else_is():
    text = (REPO / "requirements.txt").read_text(encoding="utf-8")
    shipped = build.shipped_requirements(text)
    names = [s.split(">")[0].split("<")[0].split("=")[0].strip() for s in shipped]
    assert "pytest" not in names and "pytest-xdist" not in names
    for required in ("PyQt5", "matplotlib", "yfinance", "mcp", "sqlcipher3"):
        assert required in names
    assert any(s.startswith("mcp") and "<2" in s for s in shipped)
