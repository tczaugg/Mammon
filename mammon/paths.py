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
2. A **packaged build** (PyInstaller sets ``sys.frozen``) uses a per-user folder
   -- ``~/Documents/Mammon``. This is not stylistic. An installed application
   lives somewhere like ``C:\\Program Files\\Mammon``, which a standard user
   cannot write to; Windows does not fail cleanly there either, it silently
   redirects the writes into a per-user VirtualStore copy, so the ledger appears
   to save and then appears to vanish. Documents is chosen over ``%LOCALAPPDATA%``
   deliberately: this application's whole promise is that you own the file, and a
   file you cannot find is not one you own. It is also already covered by
   whatever backs up Documents.
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


def is_frozen() -> bool:
    """True when running from a packaged build (PyInstaller and friends)."""
    return bool(getattr(sys, "frozen", False))


def install_root() -> Path:
    """The directory the ``mammon`` package sits in."""
    return Path(__file__).resolve().parent.parent


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
    if is_frozen():
        return user_data_dir()
    return install_root() / "data"


def default_db_path() -> Path:
    """The database file opened when ``--db`` is not given."""
    return data_dir() / "mammon.db"
