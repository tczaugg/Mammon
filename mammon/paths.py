"""Where Mammon keeps its data -- and the ONE place that question is answered.

Three modules used to answer it separately (``backup._install_data_dir``,
``download_log.default_data_dir``, and ``app._resolve_db``), which is how they
came to disagree: the first two honoured ``$MAMMON_DATA_DIR`` and the third did
not, so pointing that variable at a scratch directory moved the backups and the
download log but left the database itself in the install. Everything now routes
through :func:`data_dir`.

The rule, in order of precedence:

1. ``$MAMMON_DATA_DIR`` wins outright. Tests and alternate installs use it, and
   it must move the database along with everything else.
2. A **packaged build** uses a per-user folder -- ``~/Documents/Mammon``. There
   are two ways to be one: the Windows installer (``installer/``) writes
   :data:`INSTALL_MARKER` beside the package, and a frozen executable
   (PyInstaller and friends) sets ``sys.frozen``. The marker is the one that
   ships. The installer runs the stock embeddable Python, which sets no
   ``sys.frozen``, so when ``sys.frozen`` was the only test, an installed copy
   took itself for a source checkout and kept the ledger INSIDE the install
   folder, which is the folder the uninstaller deletes.

   Keeping data out of the install folder is not stylistic, wherever that folder
   is. The installer uses ``%LOCALAPPDATA%\\Mammon``, which is replaced wholesale
   on every upgrade and removed on uninstall. A system-wide install would use
   ``C:\\Program Files\\Mammon``, which a standard user cannot write to; Windows
   does not fail cleanly there either, it silently redirects the writes into a
   per-user VirtualStore copy, so the ledger appears to save and then appears to
   vanish. Documents is chosen over ``%LOCALAPPDATA%`` deliberately: this
   application's whole promise is that you own the file, and a file you cannot
   find is not one you own. It is also already covered by whatever backs up
   Documents.
3. Otherwise -- a source checkout, which is how it has always run -- the
   ``data`` directory beside the package, resolved from the package location and
   never from the working directory (see CLAUDE.md, "Paths").

Nothing here creates directories. Callers that write do that themselves, so
merely importing this module can never leave a stray folder behind.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Folder name used under the user's home in a packaged build.
APP_DIR_NAME = "Mammon"

#: Written by the Windows installer into the install root, beside the package.
#: Its presence is what makes a copy "installed"; its content (version, commit,
#: install time) is informational only.
INSTALL_MARKER = "mammon-install.json"

#: The pointer to the database the user last chose (see :mod:`mammon.last_db`).
LAST_DB_RECORD = "last_database.json"


def is_frozen() -> bool:
    """True when running from a frozen executable (PyInstaller and friends)."""
    return bool(getattr(sys, "frozen", False))


def install_root() -> Path:
    """The directory the ``mammon`` package sits in."""
    return Path(__file__).resolve().parent.parent


def is_installed() -> bool:
    """True when this copy was put in place by the Windows installer."""
    return (install_root() / INSTALL_MARKER).is_file()


def is_packaged() -> bool:
    """True for any copy that must NOT keep data beside itself."""
    return is_frozen() or is_installed()


def user_data_dir() -> Path:
    """``~/Documents/Mammon``, with a fallback for a home without Documents.

    ``Path.home()`` is honoured rather than a Windows API call so the same code
    serves macOS and Linux, where ``~/Documents`` is also conventional. A home
    directory with no Documents folder (some Linux setups, and redirected
    Windows profiles) falls back to ``~/Mammon`` rather than creating a
    Documents folder the user never asked for."""
    home = Path.home()
    documents = home / "Documents"
    return (documents if documents.is_dir() else home) / APP_DIR_NAME


def data_dir() -> Path:
    """The directory holding ``mammon.db``, ``backups/`` and the download log."""
    env = os.environ.get("MAMMON_DATA_DIR")
    if env:
        return Path(env)
    if is_packaged():
        return user_data_dir()
    return install_root() / "data"


def cache_dir() -> Path:
    """Disposable, regenerable artifacts -- NOT user data.

    Today this holds the theme's generated tree-arrow PNGs (see
    ``mammon/ui/branch_icons.py``). Deleting it costs nothing: everything in it
    is rebuilt on demand. It lives under ``data_dir()`` so a packaged build and
    a source checkout each keep it beside the ledger they already own, and so
    ``MAMMON_DATA_DIR`` relocates it too; ``MAMMON_CACHE_DIR`` overrides it on
    its own for a read-only install."""
    env = os.environ.get("MAMMON_CACHE_DIR")
    if env:
        return Path(env)
    return data_dir() / "cache"


def default_db_path() -> Path:
    """The database file a launch falls back to when nothing else names one:
    no ``--db``, and no remembered last-used database (:mod:`mammon.last_db`)."""
    return data_dir() / "mammon.db"


def last_db_record_path() -> Path:
    """Where the last-used-database pointer is kept.

    In the data directory, so it follows ``$MAMMON_DATA_DIR`` (a test run
    can never read or overwrite the real pointer), and a source checkout and an
    installed copy each keep their own. They should: the two may be different
    versions of the code, and sharing one pointer would have each open whatever
    the other last used."""
    return data_dir() / LAST_DB_RECORD
