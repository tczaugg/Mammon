"""Which database a plain launch opens: the one the user last chose.

A launch from the Start Menu, a taskbar pin or a desktop shortcut has no command
line to name a database, so before this existed a GUI-launched Mammon always
opened ``<data dir>/mammon.db``. Anyone keeping their ledger anywhere else (a
renamed file, a synced folder, a second household's book) had to go through
File > Open Database on every single launch, and nothing warned them when the
window came up on the wrong ledger.

The rule, in order (see :func:`startup_db`):

1. ``--db`` wins, and is NOT remembered. An explicit path is a one-off:
   developers, tests, the screenshot tool and automation agents all launch with
   ``--db <scratch>``, and if that became the default the next GUI launch would
   quietly open a scratch ledger instead of the real one.
2. The database last opened through the window: File > New, Open, Save As, or
   a restore. :class:`mammon.ui.widgets.MainWindow` reports each switch through
   its ``on_database_opened`` hook, and only the real launch in
   :mod:`mammon.app` connects that hook to :func:`remember`. A test that builds
   a MainWindow therefore never writes the pointer, without having to remember
   to redirect anything.
3. :func:`mammon.paths.default_db_path`, as before.

**A remembered file that is missing is never forgotten.** The launch falls back
to the default and says so (:func:`missing_remembered`), but the pointer is
left alone. The usual cause is temporary: a USB drive or network share that is
not mounted yet. Overwriting the pointer then would lose track of the real
ledger for good, and the user would be left with an empty default one.

The pointer is a small JSON file in the data directory
(:func:`mammon.paths.last_db_record_path`), so ``$MAMMON_DATA_DIR`` isolates it
along with everything else, and a source checkout and an installed copy each
keep their own. It is written atomically. A pointer that is unreadable,
malformed, or unwritable (read-only data folder) is treated as absent: it is a
convenience, and failing to remember must never stop a database from opening.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from mammon import paths


def remembered() -> Optional[Path]:
    """The database path last chosen in the window, whether or not it still
    exists; ``None`` when nothing usable is recorded."""
    try:
        data = json.loads(paths.last_db_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("path") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value)


def remember(db_path) -> bool:
    """Record ``db_path`` as the database the next plain launch opens.

    Returns False instead of raising when the pointer cannot be written."""
    record = paths.last_db_record_path()
    tmp = record.with_name(record.name + ".tmp")
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"path": str(Path(db_path).resolve())}, indent=2),
                       encoding="utf-8")
        os.replace(tmp, record)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def startup_db() -> Path:
    """The database a launch with no ``--db`` opens: the remembered one when it
    still exists as a file, otherwise the default."""
    last = remembered()
    if last is not None and last.is_file():
        return last
    return paths.default_db_path()


def missing_remembered() -> Optional[Path]:
    """The remembered database when it can NOT be opened (so the launch fell
    back to the default); ``None`` when there is nothing to report."""
    last = remembered()
    if last is None or last.is_file():
        return None
    return last
