"""The application icon, and the one place its location is resolved.

The icon ships as a multi-resolution ``.ico`` (every size Windows asks for) plus
a PNG set for Linux icon themes. Both are generated from one master by
``tools/make_icon.py`` -- regenerate rather than editing a size by hand, or the
sizes drift apart.

Resolved from the package directory, never the working directory: the app is
launched from a shortcut, from a shell, and from an installed location, and only
the package path is the same in all three (see CLAUDE.md, "Paths").
"""

from __future__ import annotations

from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent


def icon_path(preferred: str = "mammon.ico") -> Path:
    """Best available icon file. Falls back to the 512px PNG when the .ico is
    missing, which is what a source checkout with a partial build looks like."""
    ico = ICON_DIR / preferred
    if ico.exists():
        return ico
    png = ICON_DIR / "mammon-512.png"
    return png if png.exists() else ICON_DIR / "mammon.png"


def app_icon():
    """A ``QIcon`` for the window and the taskbar, or ``None`` when the files
    are absent. Imports Qt lazily so this module stays usable without it."""
    from PyQt5.QtGui import QIcon
    path = icon_path()
    return QIcon(str(path)) if path.exists() else None
