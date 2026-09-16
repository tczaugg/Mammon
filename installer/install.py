"""Install Mammon for the current user. ``setup.bat`` runs this with the BUNDLED
interpreter from the extracted ZIP, so it may use the standard library only
(and ``mammon`` itself, which ``._pth`` puts on the path).

Everything lands where a standard user can write without elevation:

* ``%LOCALAPPDATA%\\Mammon``: the embeddable Python, its site-packages, the
  ``mammon`` package, ``uninstall.bat``, ``mammon-mcp.bat``, and the install
  marker. It is program files ONLY. Every upgrade replaces it and uninstall
  removes it, so nothing the user owns may ever live here.
* ``Documents\\Mammon``: the ledger, backups, logs. The installer never writes
  here, except to move a ledger the user explicitly hands it.
* ``Start Menu\\Programs\\Mammon.lnk``: carries the app's AppUserModelID, which
  is what makes a taskbar pin launch Mammon rather than a bare pythonw.exe.
* ``HKCU\\...\\Uninstall\\Mammon``: the Settings > Apps entry.

The order of the steps is load-bearing:

1. **The install marker is written before the package is copied.** Its
   presence is what sends an installed copy's data to Documents
   (``mammon.paths``). Written last, a failure part-way through an upgrade
   would leave a ``mammon`` package that believes it is a source checkout and
   keeps its ledger inside the folder the next upgrade deletes.
2. **The data directory is then asked of the INSTALLED interpreter**, not
   computed here. That is the only answer that cannot drift from
   ``mammon.paths``, and it doubles as proof that the installed Python can
   import the installed package. A copy that would keep data inside its own
   folder stops the install.
3. **A ledger is copied, verified, and only then deleted at its source**
   (:func:`move_ledger`). Its WAL and journal sidecars travel with it, because
   the WAL holds the newest commits. The source is renamed first, which fails
   while any process has the file open, so a ledger in use is never moved.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

PAYLOAD = Path(__file__).resolve().parent

#: Replaced wholesale on every install. Nothing else in the install folder is
#: touched, and uninstall.bat removes exactly these, never recursively
#: deleting anything it did not put there.
PROGRAM_DIRS = ("python", "site-packages", "mammon")
PROGRAM_FILES = ("uninstall.bat", "mammon-mcp.bat")
INSTALL_MARKER = "mammon-install.json"    # == mammon.paths.INSTALL_MARKER
BUILD_INFO = "build-info.json"

SHORTCUT_NAME = "Mammon.lnk"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Mammon"
DB_SIDECARS = ("-wal", "-shm", "-journal")
SQLITE_HEADER = b"SQLite format 3\x00"

#: Files whose Mark of the Web would make Windows prompt on launch. A ZIP
#: extracted by Explorer marks every file inside as downloaded, and the copy
#: carries the mark along, so without this every Start Menu launch of
#: pythonw.exe could raise an "Open File - Security Warning".
UNBLOCK_SUFFIXES = {".exe", ".dll", ".pyd", ".bat"}


class InstallError(Exception):
    """A step that must stop the install, with a message for the user."""


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
def default_install_dir() -> Path:
    env = os.environ.get("MAMMON_INSTALL_DIR")
    if env:
        return Path(env)
    return Path(os.environ["LOCALAPPDATA"]) / "Mammon"


def default_start_menu() -> Path:
    env = os.environ.get("MAMMON_START_MENU")
    if env:
        return Path(env)
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


# ---------------------------------------------------------------------------
# Program files
# ---------------------------------------------------------------------------
def check_payload(payload: Path) -> dict:
    """Refuse an incomplete payload (typically: setup.bat run from inside the
    ZIP viewer, which extracts that one file and nothing else)."""
    required = [payload / "python" / "python.exe", payload / "python" / "pythonw.exe",
                payload / "site-packages", payload / "mammon" / "app.py",
                payload / BUILD_INFO, *(payload / f for f in PROGRAM_FILES)]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise InstallError(
            "The installer is incomplete. Extract the WHOLE ZIP to a folder "
            "(right-click it, Extract All) and run setup.bat from there.\n\n"
            "Missing:\n  " + "\n  ".join(missing))
    return json.loads((payload / BUILD_INFO).read_text(encoding="utf-8"))


def ensure_not_running(install_dir: Path) -> None:
    """Fail when a running Mammon (or MCP server) holds the installed Python.

    Renaming a folder fails on Windows while any file inside it is open or
    loaded, which is exactly the condition under which replacing it would fail
    half-way through."""
    python_dir = install_dir / "python"
    if not python_dir.exists():
        return
    probe = install_dir / "python.inuse-probe"
    try:
        os.rename(python_dir, probe)
    except OSError:
        raise InstallError(
            "Mammon is running from " + str(install_dir) + ".\n"
            "Close it (and any MCP client using mammon-mcp.bat), then run setup again.")
    try:
        os.rename(probe, python_dir)
    except OSError as exc:                     # pragma: no cover - a race
        raise InstallError(f"Could not restore {python_dir} after checking it: {exc}")


def write_marker(install_dir: Path, build_info: dict) -> Path:
    marker = install_dir / INSTALL_MARKER
    install_dir.mkdir(parents=True, exist_ok=True)
    info = dict(build_info)
    info["installed_at"] = datetime.now().isoformat(timespec="seconds")
    marker.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return marker


def replace_program(payload: Path, install_dir: Path, build_info: dict) -> None:
    """Marker first (see the module docstring), then each program part."""
    write_marker(install_dir, build_info)
    for name in PROGRAM_DIRS:
        target = install_dir / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(payload / name, target)
    for name in PROGRAM_FILES:
        shutil.copy2(payload / name, install_dir / name)


def unblock(root: Path) -> int:
    """Strip the Mark of the Web from launchable files; returns how many had one."""
    count = 0
    for path in root.rglob("*"):
        if path.suffix.lower() in UNBLOCK_SUFFIXES and path.is_file():
            try:
                os.remove(str(path) + ":Zone.Identifier")
                count += 1
            except OSError:
                pass
    return count


def query_installed(install_dir: Path) -> dict:
    """Ask the INSTALLED interpreter and package the facts setup depends on."""
    code = ("import json; from mammon import app, db, paths; print(json.dumps({"
            "'data_dir': str(paths.data_dir()), 'installed': paths.is_installed(), "
            "'default_db': str(paths.default_db_path()), "
            "'last_db_record': str(paths.last_db_record_path()), "
            "'app_id': app.APP_USER_MODEL_ID, 'schema': db.SCHEMA_VERSION}))")
    python = install_dir / "python" / "python.exe"
    proc = subprocess.run([str(python), "-B", "-c", code], capture_output=True,
                          text=True, cwd=str(install_dir))
    if proc.returncode != 0:
        raise InstallError("The installed copy of Mammon failed to start:\n"
                           + (proc.stderr or proc.stdout).strip())
    facts = json.loads(proc.stdout.strip().splitlines()[-1])
    data_dir = Path(facts["data_dir"]).resolve()
    root = install_dir.resolve()
    if not facts["installed"] or data_dir == root or root in data_dir.parents:
        raise InstallError(
            f"The installed copy would keep its data inside {install_dir}, which "
            "every upgrade replaces. This build is broken; nothing was moved.")
    return facts


def remember_in_installed(install_dir: Path, db_path: Path) -> bool:
    python = install_dir / "python" / "python.exe"
    proc = subprocess.run(
        [str(python), "-B", "-c",
         "import sys; from mammon import last_db; "
         "sys.exit(0 if last_db.remember(sys.argv[1]) else 1)", str(db_path)],
        capture_output=True, text=True, cwd=str(install_dir))
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Moving an existing ledger in
# ---------------------------------------------------------------------------
@dataclass
class MoveResult:
    database: Path
    backups: Optional[Path] = None
    warnings: list = field(default_factory=list)


def parse_user_path(text: str) -> Optional[Path]:
    """A path typed or dragged onto the console. Dragging a file whose path has
    spaces pastes it quoted."""
    text = text.strip().strip('"').strip()
    if not text:
        return None
    return Path(os.path.expandvars(os.path.expanduser(text)))


def resolve_ledger_source(path: Path) -> Path:
    """A database file, or a data folder holding exactly one obvious one."""
    if path.is_file():
        return path
    if path.is_dir():
        if (path / "mammon.db").is_file():
            return path / "mammon.db"
        dbs = sorted(p for p in path.glob("*.db") if p.is_file())
        if len(dbs) == 1:
            return dbs[0]
        if dbs:
            raise InstallError(f"{path} holds several databases; drag the one to move.")
        raise InstallError(f"There is no database in {path}.")
    raise InstallError(f"{path} does not exist.")


def ledger_schema_version(path: Path) -> Optional[int]:
    """``PRAGMA user_version`` read without writing anything, or None when it
    cannot be read (an encrypted ledger, or a WAL file SQLite will not open
    read-only)."""
    try:
        with open(path, "rb") as fh:
            if fh.read(16) != SQLITE_HEADER:
                return None                    # encrypted, or not SQLite at all
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def move_ledger(src: Path, data_dir: Path, schema_version: int) -> MoveResult:
    """Move a ledger (with its sidecars and its backups folder) into ``data_dir``.

    Copy, verify by checksum, and only then delete the source, so a failure at
    any point leaves the original exactly where it was. Refuses a database in
    use, one whose name is already taken in ``data_dir``, and one written by a
    NEWER Mammon than this installer carries: ``init_db`` does not refuse a
    newer schema, so the older code would open it and misread it."""
    src = Path(src).resolve()
    data_dir = Path(data_dir)
    dest = data_dir / src.name
    if src.parent == data_dir.resolve():
        raise InstallError(f"{src} is already in {data_dir}.")
    taken = [p for p in (dest, *(dest.with_name(dest.name + s) for s in DB_SIDECARS))
             if p.exists()]
    if taken:
        raise InstallError(f"{taken[0]} already exists; nothing was moved.")

    result = MoveResult(database=dest)
    version = ledger_schema_version(src)
    if version is None:
        result.warnings.append(
            "Its schema version could not be read (it may be encrypted); it was "
            "moved without checking that this Mammon is new enough for it.")
    elif version > schema_version:
        raise InstallError(
            f"{src} was last opened by a newer Mammon (schema {version}; this "
            f"installer carries {schema_version}). Install a newer Mammon first; "
            "nothing was moved.")

    moving = src.with_name(src.name + ".moving")
    if moving.exists():
        raise InstallError(f"{moving} is in the way; nothing was moved.")
    try:
        os.rename(src, moving)
    except OSError:
        raise InstallError(f"{src} is in use. Close Mammon and try again; "
                           "nothing was moved.")

    sources = [(moving, dest)] + [
        (src.with_name(src.name + s), dest.with_name(dest.name + s))
        for s in DB_SIDECARS if src.with_name(src.name + s).exists()]
    created = []
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        partials = []
        for s, d in sources:
            partial = d.with_name(d.name + ".partial")
            created.append(partial)
            shutil.copy2(s, partial)
            if _sha256(s) != _sha256(partial):
                raise OSError(f"the copy of {s.name} does not match the original")
            partials.append((partial, d))
        # Sidecars take their final names before the database does: a database
        # must never appear at its destination without the WAL that holds its
        # newest commits.
        for partial, d in partials[1:] + partials[:1]:
            os.replace(partial, d)
            created.append(d)
    except OSError as exc:
        for p in created:
            try:
                p.unlink()
            except OSError:
                pass
        os.rename(moving, src)
        raise InstallError(f"Moving {src} failed ({exc}); it was left where it was.")

    for s, _ in sources[1:] + sources[:1]:
        try:
            s.unlink()
        except OSError as exc:
            result.warnings.append(f"The copy is complete, but {s} could not be "
                                   f"deleted ({exc}); delete it by hand.")

    src_backups = src.parent / "backups" / src.name
    dest_backups = data_dir / "backups" / dest.name
    if src_backups.is_dir():
        if dest_backups.exists():
            result.warnings.append(f"Its backups were left in {src_backups}: "
                                   f"{dest_backups} already exists.")
        else:
            try:
                shutil.copytree(src_backups, dest_backups)
                shutil.rmtree(src_backups)
                result.backups = dest_backups
            except OSError as exc:
                shutil.rmtree(dest_backups, ignore_errors=True)
                result.warnings.append(f"Its backups could not be moved ({exc}); "
                                       f"they are still in {src_backups}.")
    return result


# ---------------------------------------------------------------------------
# The Start Menu shortcut (COM through ctypes: no pywin32 in the payload)
# ---------------------------------------------------------------------------
CLSID_ShellLink = "00021401-0000-0000-C000-000000000046"
IID_IShellLinkW = "000214F9-0000-0000-C000-000000000046"
IID_IPersistFile = "0000010B-0000-0000-C000-000000000046"
IID_IPropertyStore = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"
FMTID_AppUserModel = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
PID_AppUserModel_ID = 5
VT_LPWSTR = 31


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    u = uuid.UUID(text)
    g = _GUID(u.time_low, u.time_mid, u.time_hi_version)
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    # vt + three reserved words, then the value union (pointer-sized members;
    # the second pointer pads the union to its real size).
    _fields_ = [("vt", ctypes.c_ushort), ("reserved1", ctypes.c_ushort),
                ("reserved2", ctypes.c_ushort), ("reserved3", ctypes.c_ushort),
                ("value", ctypes.c_void_p), ("value2", ctypes.c_void_p)]


def _call(obj, index, argtypes, *args):
    """Call vtable slot ``index`` of a COM interface pointer; raises OSError
    on a failing HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])(obj, *args)


def _release(obj) -> None:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


def _query(obj, iid: str):
    out = ctypes.c_void_p()
    g = _guid(iid)
    _call(obj, 0, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
          ctypes.byref(g), ctypes.byref(out))
    return out


class _Apartment:
    def __enter__(self):
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx.restype = ctypes.c_long
        self._ok = ole32.CoInitializeEx(None, 2) >= 0      # apartment-threaded
        return self

    def __exit__(self, *exc):
        if self._ok:
            ctypes.windll.ole32.CoUninitialize()


def _new_shell_link():
    ole32 = ctypes.windll.ole32
    ole32.CoCreateInstance.restype = ctypes.HRESULT
    out = ctypes.c_void_p()
    clsid, iid = _guid(CLSID_ShellLink), _guid(IID_IShellLinkW)
    ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,   # CLSCTX_INPROC_SERVER
                           ctypes.byref(iid), ctypes.byref(out))
    return out


def create_shortcut(lnk_path: Path, target: Path, arguments: str, working_dir: Path,
                    icon: Path, description: str, app_id: str) -> None:
    """Write a .lnk that carries ``app_id`` as its System.AppUserModel.ID.

    WScript.Shell's CreateShortcut (what webSlinger's setup.bat uses) cannot set
    that property, and without it a taskbar pin records the running process,
    pythonw.exe with no arguments, instead of this shortcut."""
    wstr = ctypes.c_wchar_p
    lnk_path = Path(lnk_path)
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    with _Apartment():
        link = _new_shell_link()
        try:
            _call(link, 20, [wstr], str(target))                 # SetPath
            _call(link, 11, [wstr], arguments)                   # SetArguments
            _call(link, 9, [wstr], str(working_dir))             # SetWorkingDirectory
            _call(link, 7, [wstr], description)                  # SetDescription
            _call(link, 17, [wstr, ctypes.c_int], str(icon), 0)  # SetIconLocation
            store = _query(link, IID_IPropertyStore)
            try:
                buf = ctypes.create_unicode_buffer(app_id)
                value = _PROPVARIANT()
                value.vt = VT_LPWSTR
                value.value = ctypes.cast(buf, ctypes.c_void_p)
                key = _PROPERTYKEY(_guid(FMTID_AppUserModel), PID_AppUserModel_ID)
                _call(store, 6, [ctypes.POINTER(_PROPERTYKEY),
                                 ctypes.POINTER(_PROPVARIANT)],
                      ctypes.byref(key), ctypes.byref(value))    # SetValue
                _call(store, 7, [])                              # Commit
            finally:
                _release(store)
            persist = _query(link, IID_IPersistFile)
            try:
                _call(persist, 6, [wstr, ctypes.c_int], str(lnk_path), 1)  # Save
            finally:
                _release(persist)
        finally:
            _release(link)


def read_shortcut(lnk_path: Path) -> dict:
    """Target, arguments and AppUserModelID of a .lnk (for verification)."""
    wstr = ctypes.c_wchar_p
    with _Apartment():
        link = _new_shell_link()
        try:
            persist = _query(link, IID_IPersistFile)
            try:
                _call(persist, 5, [wstr, ctypes.c_ulong], str(lnk_path), 0)  # Load
            finally:
                _release(persist)
            target = ctypes.create_unicode_buffer(1024)
            _call(link, 3, [wstr, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong],
                  target, 1024, None, 0)                          # GetPath
            args = ctypes.create_unicode_buffer(1024)
            _call(link, 10, [wstr, ctypes.c_int], args, 1024)     # GetArguments
            app_id = None
            store = _query(link, IID_IPropertyStore)
            try:
                key = _PROPERTYKEY(_guid(FMTID_AppUserModel), PID_AppUserModel_ID)
                value = _PROPVARIANT()
                _call(store, 5, [ctypes.POINTER(_PROPERTYKEY),
                                 ctypes.POINTER(_PROPVARIANT)],
                      ctypes.byref(key), ctypes.byref(value))    # GetValue
                if value.vt == VT_LPWSTR and value.value:
                    app_id = ctypes.wstring_at(value.value)
                ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
            finally:
                _release(store)
        finally:
            _release(link)
    return {"target": target.value, "arguments": args.value, "app_id": app_id}


# ---------------------------------------------------------------------------
# Settings > Apps
# ---------------------------------------------------------------------------
def _folder_kb(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total // 1024


def register_uninstall(install_dir: Path, build_info: dict, key: str = UNINSTALL_KEY) -> None:
    import winreg
    icon = install_dir / "mammon" / "ui" / "icons" / "mammon.ico"
    uninstaller = f'"{install_dir / "uninstall.bat"}"'
    values = {
        "DisplayName": (winreg.REG_SZ, "Mammon"),
        "DisplayVersion": (winreg.REG_SZ, str(build_info.get("version", ""))),
        "DisplayIcon": (winreg.REG_SZ, str(icon)),
        "InstallLocation": (winreg.REG_SZ, str(install_dir)),
        "UninstallString": (winreg.REG_SZ, uninstaller),
        "QuietUninstallString": (winreg.REG_SZ, uninstaller + " /quiet"),
        "InstallDate": (winreg.REG_SZ, datetime.now().strftime("%Y%m%d")),
        "EstimatedSize": (winreg.REG_DWORD, min(_folder_kb(install_dir), 0xFFFFFFFF)),
        "NoModify": (winreg.REG_DWORD, 1),
        "NoRepair": (winreg.REG_DWORD, 1),
    }
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_WRITE) as k:
        for name, (kind, value) in values.items():
            winreg.SetValueEx(k, name, 0, kind, value)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def offer_move(facts: dict, install_dir: Path) -> Optional[MoveResult]:
    """Ask for a ledger from a source checkout, up to three tries."""
    print()
    print("Have you been running Mammon from a source checkout (a git clone)?")
    print("To move that ledger here, drag its mammon.db file, or the data folder")
    print("holding it, onto this window and press Enter. Press Enter alone to")
    print("start with a new, empty ledger.")
    for _ in range(3):
        path = parse_user_path(_ask("\nLedger to move: "))
        if path is None:
            return None
        try:
            return move_ledger(resolve_ledger_source(path), Path(facts["data_dir"]),
                               facts["schema"])
        except InstallError as exc:
            print(f"\n{exc}")
    return None


def run(args) -> int:
    payload = PAYLOAD
    install_dir = Path(args.install_dir) if args.install_dir else default_install_dir()
    start_menu = Path(args.start_menu) if args.start_menu else default_start_menu()
    if payload.resolve() == install_dir.resolve():
        raise InstallError("Run setup.bat from the extracted ZIP, not from the "
                           "installed folder.")

    build_info = check_payload(payload)
    version = build_info.get("version", "?")
    print(f"Installing Mammon {version} into {install_dir}")

    ensure_not_running(install_dir)
    print("  Copying program files (this takes a minute)...")
    replace_program(payload, install_dir, build_info)
    unblock(install_dir)

    facts = query_installed(install_dir)
    data_dir = Path(facts["data_dir"])
    print(f"  Your ledgers and backups will live in {data_dir}")

    moved = None
    has_ledger = (Path(facts["default_db"]).exists()
                  or Path(facts["last_db_record"]).exists())
    if args.move_from:
        moved = move_ledger(resolve_ledger_source(Path(args.move_from)), data_dir,
                            facts["schema"])
    elif not has_ledger and not args.unattended:
        moved = offer_move(facts, install_dir)
    if moved is not None:
        print(f"\n  Moved your ledger to {moved.database}")
        if moved.backups is not None:
            print(f"  Moved its backups to {moved.backups}")
        for warning in moved.warnings:
            print(f"  Note: {warning}")
        if moved.database != Path(facts["default_db"]):
            if not remember_in_installed(install_dir, moved.database):
                print(f"  Note: open it once with File > Open Database: {moved.database}")

    pythonw = install_dir / "python" / "pythonw.exe"
    icon = install_dir / "mammon" / "ui" / "icons" / "mammon.ico"
    create_shortcut(start_menu / SHORTCUT_NAME, pythonw, "-m mammon.app", install_dir,
                    icon, "Mammon personal finance ledger", facts["app_id"])
    print("  Added Mammon to the Start Menu")
    if not args.no_registry:
        register_uninstall(install_dir, build_info)
        print("  Added Mammon to Settings > Apps (uninstall it from there)")

    print()
    print("Mammon is installed. To pin it, start it, right-click its taskbar")
    print("button and choose Pin to taskbar.")
    print(f"For an MCP client, register: {install_dir / 'mammon-mcp.bat'}")

    if not args.unattended and not args.no_launch:
        if _ask("\nStart Mammon now? [Y/n] ").strip().lower() in ("", "y", "yes"):
            subprocess.Popen([str(pythonw), "-m", "mammon.app"], cwd=str(install_dir),
                             creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW GROUP
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="setup.bat", description="Install Mammon.")
    ap.add_argument("--install-dir", help="default: %%LOCALAPPDATA%%\\Mammon")
    ap.add_argument("--start-menu", help="folder for Mammon.lnk "
                                         "(default: the per-user Start Menu)")
    ap.add_argument("--move-from", help="move this ledger (or data folder) in, "
                                        "without asking")
    ap.add_argument("--no-registry", action="store_true",
                    help="do not add the Settings > Apps entry")
    ap.add_argument("--no-launch", action="store_true",
                    help="do not offer to start Mammon at the end")
    ap.add_argument("--unattended", action="store_true",
                    help="ask nothing and do not wait for Enter at the end")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc = run(args)
    except InstallError as exc:
        print(f"\nSetup stopped: {exc}")
        rc = 1
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        rc = 1
    except Exception as exc:                   # show it rather than vanish
        import traceback
        traceback.print_exc()
        print(f"\nSetup failed unexpectedly: {exc}")
        rc = 1
    if not args.unattended:
        _ask("\nPress Enter to close this window.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
