#!/usr/bin/env python3
"""Build the Windows installer: ``dist/Mammon-<version>-Setup.zip``.

    python installer\\build.py                 # package the committed HEAD
    python installer\\build.py --ref v0.2.0    # package a tag or commit
    python installer\\build.py --worktree      # package uncommitted work (testing only)

Modelled on webSlinger's installer, which replaced a PyInstaller .exe with the
same shape: the stock Python embeddable package, dependencies pip-installed
beside it, and a ``setup.bat`` that copies everything into ``%LOCALAPPDATA%``.
No freezing means no hidden-import and data-file lists to maintain for PyQt5,
matplotlib and sqlcipher3; the installed app runs exactly the code a clone
runs, on an ordinary interpreter.

What each step prevents:

* **A release is built from a commit, not the working tree** (``--ref``,
  default HEAD). A working tree can hold half-finished work, and an installer
  is the "known good version" people who do not clone rely on. ``--worktree``
  exists for testing the installer itself and stamps the build ``-dirty``.
* **The build interpreter must be 64-bit CPython 3.12 on Windows.** pip
  resolves wheels for the interpreter running it, and they must load in the
  embeddable 3.12 that ships. A pure-Python sdist dependency (yfinance has
  some) rules out resolving for a foreign platform with ``--platform``.
* **``._pth`` is written whole** (:func:`write_pth`). It puts the embeddable
  interpreter in isolated mode: ``PYTHONPATH`` is ignored and neither the
  script's folder nor the working directory is on ``sys.path``. The install
  root (``..``, for the ``mammon`` package) and ``../site-packages`` must be
  listed there, or ``python -m mammon.app`` cannot find the package. So must
  pywin32's folders (:data:`SEARCH_PATH`), because ``.pth`` files are not
  processed either.
* **Bytecode is precompiled as unchecked-hash .pyc.** Explorer's ZIP
  extraction rounds file times to two seconds, which invalidates ordinary
  timestamp-checked .pyc files. Half the package would then recompile on first
  launch. Installed sources are never edited in place (every upgrade replaces
  them), so there is nothing to check against.
* **The payload is installed and exercised before it is zipped**
  (:func:`verify`). webSlinger shipped a build whose MCP server died at import
  because nothing ran the SHIPPED combination of interpreter, packages and
  sources. Here the payload runs through setup (moving a synthetic ledger in),
  opens a MainWindow offscreen under the installed interpreter, answers an MCP
  handshake, shows the right shortcut identity, and then uninstalls. All of it
  runs with the profile folders redirected into a temp directory, so the build
  machine's Start Menu, registry, settings and ledgers are never touched. A
  build that fails any of it produces no ZIP.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

PYTHON_VERSION = "3.12.9"
EMBED_ZIP = f"python-{PYTHON_VERSION}-embed-amd64.zip"
EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{EMBED_ZIP}"

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CACHE = HERE / "cache"
BUILD = HERE / "build"
DIST = HERE / "dist"
STAGE_NAME = "Mammon_Setup"

#: In requirements.txt for the checkout, pointless in an installed copy: the
#: test suite is not shipped.
NOT_SHIPPED = {"pytest", "pytest-xdist"}
PAYLOAD_FILES = ("setup.bat", "install.py", "uninstall.bat", "mammon-mcp.bat")

VERIFY_TIMEOUT = 300

#: ``._pth`` entries after the stdlib, relative to python.exe. The last three
#: are what ``pywin32.pth`` would add if ``._pth`` mode processed .pth files at
#: all; it does not. mcp imports ``pywintypes`` on Windows, so without them the
#: MCP server dies at import. The first build's verification caught exactly
#: that, the same failure webSlinger's installer hit.
SEARCH_PATH = (
    "..",                          # the install root: the mammon package
    "../site-packages",
    "../site-packages/win32",
    "../site-packages/win32/lib",
    "../site-packages/Pythonwin",
)


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def fail(text: str) -> None:
    raise SystemExit(f"\nBUILD FAILED: {text}")


def run(cmd, **kw) -> subprocess.CompletedProcess:
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)
    if proc.returncode != 0:
        fail(f"{' '.join(str(c) for c in cmd)}\n{proc.stdout}\n{proc.stderr}")
    return proc


def load_install_module():
    spec = importlib.util.spec_from_file_location("mammon_install", HERE / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module             # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
def check_build_interpreter() -> None:
    want = tuple(int(x) for x in PYTHON_VERSION.split(".")[:2])
    if sys.platform != "win32" or sys.maxsize <= 2**32 or sys.version_info[:2] != want:
        fail(f"build with 64-bit CPython {want[0]}.{want[1]} on Windows (this is "
             f"{sys.version.split()[0]} on {sys.platform}); its wheels must load in "
             f"the embeddable {PYTHON_VERSION} that ships")


def export_source(dest: Path, ref: str | None) -> dict:
    """The ``mammon`` package (minus tests), requirements and version, taken
    from ``ref`` or, when ``ref`` is None, from the working tree."""
    dest.mkdir(parents=True)
    if ref is None:
        src = REPO
        commit = run(["git", "-C", REPO, "rev-parse", "HEAD"]).stdout.strip()
        dirty = run(["git", "-C", REPO, "status", "--porcelain", "--",
                     "mammon", "requirements.txt", "pyproject.toml"]).stdout.strip()
        commit += "-dirty" if dirty else ""
    else:
        commit = run(["git", "-C", REPO, "rev-parse", f"{ref}^{{commit}}"]).stdout.strip()
        archive = subprocess.run(
            ["git", "-C", str(REPO), "archive", "--format=tar", commit,
             "mammon", "requirements.txt", "pyproject.toml"], capture_output=True)
        if archive.returncode != 0:
            fail(archive.stderr.decode(errors="replace"))
        src = dest.parent / "exported"
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(src, filter="data")

    def ignore(folder, names):
        skip = {n for n in names if n == "__pycache__" or n.endswith(".pyc")}
        if Path(folder).resolve() == (src / "mammon").resolve():
            skip.add("tests")
        return skip

    shutil.copytree(src / "mammon", dest / "mammon", ignore=ignore)
    version = tomllib.loads((src / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    requirements = (src / "requirements.txt").read_text(encoding="utf-8")
    return {"version": version, "commit": commit, "requirements": requirements}


def shipped_requirements(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        spec = line.split("#", 1)[0].strip()
        if not spec:
            continue
        name = spec
        for sep in "<>=!~;[ ":
            name = name.split(sep, 1)[0]
        if name.lower() not in NOT_SHIPPED:
            out.append(spec)
    return out


def fetch_embeddable() -> Path:
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / EMBED_ZIP
    if not cached.exists():
        print(f"  downloading {EMBED_URL}")
        partial = cached.with_suffix(".partial")
        urllib.request.urlretrieve(EMBED_URL, partial)
        partial.replace(cached)
    return cached


def write_pth(python_dir: Path) -> Path:
    pths = list(python_dir.glob("python3*._pth"))
    if len(pths) != 1:
        fail(f"expected one python3*._pth in {python_dir}, found {pths}")
    stdlib_zip = pths[0].name.replace("._pth", ".zip")
    pths[0].write_text("\n".join([stdlib_zip, ".", *SEARCH_PATH, "import site"]) + "\n",
                       encoding="utf-8")
    return pths[0]


def install_dependencies(stage: Path, requirements: list[str]) -> None:
    target = stage / "site-packages"
    req = stage.parent / "requirements-shipped.txt"
    req.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    run([sys.executable, "-m", "pip", "install", "--target", target, "--no-user",
         "--disable-pip-version-check", "--no-warn-script-location", "--quiet",
         "-r", req])
    shutil.rmtree(target / "bin", ignore_errors=True)
    frozen = run([sys.executable, "-m", "pip", "list", "--path", target,
                  "--format=freeze", "--disable-pip-version-check"]).stdout
    (stage / "packages.txt").write_text(frozen, encoding="utf-8")


def precompile(stage: Path) -> None:
    proc = subprocess.run(
        [str(stage / "python" / "python.exe"), "-m", "compileall", "-q",
         "--invalidation-mode", "unchecked-hash",
         str(stage / "mammon"), str(stage / "site-packages")],
        capture_output=True, text=True)
    if proc.returncode != 0:
        # Some wheels ship files that are not valid Python 3 (templates, test
        # data); those are never imported, so failing to compile them is fine.
        print("  note: compileall skipped some files that are never imported")


# ---------------------------------------------------------------------------
def verify(stage: Path) -> None:
    install_mod = load_install_module()
    with tempfile.TemporaryDirectory(prefix="mammon-verify-") as tmp:
        tmp = Path(tmp)
        home = tmp / "home"
        (home / "Documents").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("MAMMON_") and k not in ("PYTHONPATH", "PYTHONHOME")}
        env.update({
            "USERPROFILE": str(home), "HOME": str(home),
            "APPDATA": str(tmp / "appdata"), "LOCALAPPDATA": str(tmp / "localappdata"),
            "MAMMON_INSTALL_DIR": str(tmp / "localappdata" / "Mammon"),
            "MAMMON_START_MENU": str(tmp / "startmenu"),
            "MAMMON_SKIP_REGISTRY": "1",
            "QT_QPA_PLATFORM": "offscreen",
        })
        install_dir = tmp / "localappdata" / "Mammon"
        data_dir = home / "Documents" / "Mammon"

        step("verify: a ledger from a source checkout, to move in")
        clone_data = tmp / "clone" / "data"
        clone_data.mkdir(parents=True)
        run([stage / "python" / "python.exe", "-B", "-m", "mammon.db",
             clone_data / "mammon.db"], env=env)
        (clone_data / "backups" / "mammon.db").mkdir(parents=True)
        (clone_data / "backups" / "mammon.db" / "mammon.db.manual.20260101_000000.bak").write_bytes(b"x")

        step("verify: setup, unattended")
        proc = run([stage / "python" / "python.exe", "-B", stage / "install.py",
                    "--unattended", "--no-registry", "--move-from", clone_data],
                   env=env, cwd=stage, timeout=VERIFY_TIMEOUT)
        print("  " + proc.stdout.strip().replace("\n", "\n  "))
        if not (data_dir / "mammon.db").is_file():
            fail("the ledger was not moved into Documents\\Mammon")
        if (clone_data / "mammon.db").exists():
            fail("the moved ledger was left behind at its source")
        if not (data_dir / "backups" / "mammon.db").is_dir():
            fail("the ledger's backups were not moved")

        step("verify: the installed copy runs (offscreen)")
        proc = run([install_dir / "python" / "python.exe", "-B",
                    HERE / "smoke_installed.py", str(tmp / "second.db")],
                   env=env, cwd=tmp, timeout=VERIFY_TIMEOUT)
        facts = json.loads(proc.stdout.strip().splitlines()[-1])
        print(f"  {facts}")
        if Path(facts["opened"]).resolve() != (data_dir / "mammon.db").resolve():
            fail(f"a plain launch opened {facts['opened']}, not the moved ledger")

        step("verify: MCP handshake under the installed interpreter")
        verify_mcp(install_dir, data_dir / "mammon.db", env)

        step("verify: the Start Menu shortcut")
        lnk = install_mod.read_shortcut(tmp / "startmenu" / install_mod.SHORTCUT_NAME)
        print(f"  {lnk}")
        if Path(lnk["target"]).resolve() != (install_dir / "python" / "pythonw.exe").resolve():
            fail(f"shortcut target is {lnk['target']}")
        if lnk["arguments"] != "-m mammon.app" or lnk["app_id"] != facts["app_id"]:
            fail(f"shortcut arguments/app id wrong: {lnk}")

        step("verify: uninstall")
        run(["cmd", "/c", install_dir / "uninstall.bat", "/quiet"], env=env,
            timeout=VERIFY_TIMEOUT)
        if install_dir.exists():
            fail(f"uninstall left {install_dir}: {sorted(p.name for p in install_dir.iterdir())}")
        if (tmp / "startmenu" / install_mod.SHORTCUT_NAME).exists():
            fail("uninstall left the Start Menu shortcut")
        if not (data_dir / "mammon.db").is_file():
            fail("uninstall removed the ledger")
        print("  program removed, ledger kept")


def verify_mcp(install_dir: Path, db_path: Path, env: dict) -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mammon-build-verify", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(
        ["cmd", "/c", str(install_dir / "mammon-mcp.bat"), "--db", str(db_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, encoding="utf-8")
    try:
        stdout, stderr = proc.communicate(
            "".join(json.dumps(m) + "\n" for m in messages), timeout=VERIFY_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        fail("the MCP server did not answer")
    tools = None
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            message = json.loads(line)
            if message.get("id") == 2:
                tools = message.get("result", {}).get("tools")
    if not tools:
        fail(f"the MCP server never listed its tools\nstdout: {stdout[:800]}\n"
             f"stderr: {stderr[:800]}")
    print(f"  {len(tools)} tools")


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--ref", default="HEAD", help="git ref to package (default HEAD)")
    src.add_argument("--worktree", action="store_true",
                     help="package the working tree, uncommitted changes included")
    ap.add_argument("--keep-build", action="store_true",
                    help="leave installer/build in place afterwards")
    args = ap.parse_args(argv)

    check_build_interpreter()
    if BUILD.exists():
        shutil.rmtree(BUILD)
    stage = BUILD / STAGE_NAME

    step(f"source: {'working tree' if args.worktree else args.ref}")
    source = export_source(stage, None if args.worktree else args.ref)
    print(f"  Mammon {source['version']} at {source['commit']}")

    step(f"Python {PYTHON_VERSION} (embeddable)")
    with zipfile.ZipFile(fetch_embeddable()) as z:
        z.extractall(stage / "python")
    print(f"  wrote {write_pth(stage / 'python').name}")

    step("dependencies")
    requirements = shipped_requirements(source["requirements"])
    print("  " + ", ".join(requirements))
    install_dependencies(stage, requirements)

    step("bytecode")
    precompile(stage)

    for name in PAYLOAD_FILES:
        shutil.copy2(HERE / name, stage / name)
    (stage / "build-info.json").write_text(json.dumps({
        "version": source["version"], "commit": source["commit"],
        "python": PYTHON_VERSION, "built_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")

    verify(stage)

    step("zip")
    DIST.mkdir(exist_ok=True)
    suffix = "-dirty" if source["commit"].endswith("-dirty") else ""
    out = DIST / f"Mammon-{source['version']}{suffix}-Setup.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                z.write(path, Path(STAGE_NAME) / path.relative_to(stage))
    print(f"  {out}  ({out.stat().st_size / 2**20:.0f} MB)")

    if not args.keep_build:
        shutil.rmtree(BUILD, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
