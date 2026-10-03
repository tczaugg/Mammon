# Mammon's Windows installer

Builds `dist/Mammon-<version>-Setup.zip`: Mammon plus a private Python, for
people who will never clone the repository. The user extracts it and runs
`setup.bat`. No administrator rights, no Python of their own, no git.

The design follows webSlinger's own installer, which
dropped a PyInstaller `.exe` for this same shape. The reasoning for each
decision lives in the module docstrings of `build.py` and `install.py`. This
file covers how to use it.

## Building a release

```powershell
python installer\build.py                # packages the committed HEAD
python installer\build.py --ref v0.2.0   # packages a tag or commit
python installer\build.py --worktree     # uncommitted work, stamped -dirty: testing only
```

Run it with **64-bit CPython 3.12 on Windows**. pip resolves wheels for the
interpreter running it, and they must load in the embeddable Python 3.12.9 that
ships. The first build downloads that package into `installer/cache/`. Every
build pip-installs the runtime requirements from `requirements.txt` (minus
pytest) and takes a few minutes.

The build **installs what it just built, runs it, and uninstalls it** before
writing the ZIP, with the profile folders redirected into a temp directory so
the build machine is not touched. It moves a synthetic ledger in through setup,
opens a MainWindow offscreen under the installed interpreter, completes an MCP
handshake through `mammon-mcp.bat`, checks the Start Menu shortcut's target and
AppUserModelID, then uninstalls and checks the ledger survived. Any failure
means no ZIP.

To publish: bump `version` in `pyproject.toml`, commit, tag, build from the
tag, and attach the ZIP to a GitHub Release. GitHub counts downloads of release
assets (`gh api repos/tczaugg/Mammon/releases`).

## What ships

```
Mammon_Setup\
  setup.bat          checks the ZIP was extracted, then runs install.py
  install.py         the installer (stdlib only, run by the bundled Python)
  uninstall.bat      copied into the install; Settings > Apps runs it
  mammon-mcp.bat     the MCP server launcher, for MCP clients
  build-info.json    version, commit, Python version, build time
  packages.txt       exact versions of every bundled package
  python\            Python 3.12.9 embeddable, ._pth rewritten
  site-packages\     runtime dependencies, precompiled
  mammon\            the package, without tests, precompiled
```

## What setup does

1. Refuses to run if the ZIP was not fully extracted, or if Mammon is running
   from the install folder.
2. Writes `%LOCALAPPDATA%\Mammon\mammon-install.json`, **then** replaces
   `python\`, `site-packages\` and `mammon\` wholesale. The marker is what makes
   `mammon.paths` keep data in `Documents\Mammon`; writing it first means an
   interrupted upgrade can never leave a copy that keeps data in the install
   folder.
3. Strips the downloaded-file mark from executables, so a Start Menu launch does
   not raise a security prompt every time.
4. Asks the **installed** interpreter where the data lives, and stops if the
   answer is inside the install folder.
5. If there is no ledger there yet, offers to move one from a source checkout
   (the user drags `mammon.db` onto the window). The ledger is copied, verified
   by checksum, and only then deleted at its source, together with its `-wal`
   and `-journal` sidecars and its `backups\<name>\` folder. It refuses a ledger
   that is open, one whose name is already taken, and one from a newer schema
   than the installer carries.
6. Creates `Start Menu\Programs\Mammon.lnk` → `pythonw.exe -m mammon.app` with
   AppUserModelID `Mammon.Desktop` (`mammon.app.APP_USER_MODEL_ID`). The app sets
   the same ID at startup when installed; that pairing is what makes a taskbar
   pin launch Mammon rather than a bare `pythonw.exe`.
7. Registers `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Mammon`.

`setup.bat --help` lists the switches: `--unattended`, `--move-from PATH`,
`--install-dir`, `--start-menu`, `--no-registry`, `--no-launch`.

## Uninstalling

From Settings > Apps, or by running `uninstall.bat` in the install folder
(`/quiet` skips the question). It removes the three program folders, its own
files, the shortcut and the registry entry, and then the install folder only if
it is empty. It never deletes anything setup did not put there, and never
touches `Documents\Mammon`.

## Known limits

- **Not code-signed.** SmartScreen says "Windows protected your PC" on
  `setup.bat` from a downloaded ZIP; the user clicks More info, then Run anyway.
  A signing certificate would remove this.
- **No automatic updates.** A new version is a new ZIP and another `setup.bat`.
- **Windows only.** macOS and Linux users run from a clone.
- **Pinning is verified at the property level, not by clicking.** The build
  checks that the shortcut carries the ID the app claims. Whether Explorer
  honors that pairing was not click-tested when this was written.
