"""Run by build.py under the INSTALLED interpreter, never shipped.

    <install>\\python\\python.exe smoke_installed.py <path for a second ledger>

Proves the installed combination of interpreter, site-packages and package
actually works, which an import check alone does not (see build.py). It must be
given a redirected profile (build.py sets USERPROFILE, APPDATA and friends),
and it redirects QSettings itself: Qt finds the settings folder through the
Windows shell API, which ignores the environment variables.

Prints one JSON line of facts for build.py to check.
"""
import json
import os
import sys
from pathlib import Path


def check(condition, message):
    if not condition:
        print(f"SMOKE FAILED: {message}", file=sys.stderr, flush=True)
        os._exit(1)


def main() -> None:
    second = Path(sys.argv[1]).resolve()

    from mammon import app, last_db, paths
    check(paths.is_installed(), "the installed copy does not see its install marker")
    root, data = paths.install_root().resolve(), paths.data_dir().resolve()
    check(data != root and root not in data.parents,
          f"the data folder {data} is inside the install folder {root}")

    # Every required package, imported the way the app imports it.
    import sqlcipher3  # noqa: F401
    import yfinance  # noqa: F401
    from mcp.server.fastmcp import FastMCP  # noqa: F401
    from PyQt5.QtCore import QSettings
    from PyQt5.QtWidgets import QApplication
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(second.parent / "qsettings"))
    qapp = QApplication([])
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg  # noqa: F401
    from mammon.ui.widgets import MainWindow

    opened = app._resolve_db(None)
    conn = app._open_db(opened)
    window = MainWindow(conn, db_path=opened, on_database_opened=last_db.remember)
    window.show()
    qapp.processEvents()

    window.open_database(str(second))
    qapp.processEvents()
    check(last_db.remembered() == second, "File > Open was not remembered")
    check(app._resolve_db(None) == str(second),
          "the next plain launch would not open the database chosen last")
    window.close()
    window.conn.close()

    print(json.dumps({"opened": opened, "data_dir": str(data),
                      "app_id": app.APP_USER_MODEL_ID,
                      "python": sys.version.split()[0]}), flush=True)
    # Skip Qt's interpreter-exit teardown, which can fault offscreen after a
    # clean run and would turn a passing check into a failing exit code.
    os._exit(0)


if __name__ == "__main__":
    main()
