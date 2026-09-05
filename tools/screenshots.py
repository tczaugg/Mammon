"""Regenerate the README screenshots from the synthetic demo ledger.

    python tools/screenshots.py [--out docs/images] [--theme dark|light|both]

Never point this at a real ledger. It builds its own database from
:mod:`mammon.demo`, so what lands in ``docs/images`` cannot contain anything but
invented names -- which is the whole reason the demo module exists. The built
ledger is cached (``--db``, default ``data/demo.db``, gitignored) and reused, so
successive runs are fast and produce identical images; ``--rebuild`` starts over.

Runs headless (Qt's offscreen platform) so it needs no display and cannot pop a
window over whatever you are doing; ``--onscreen`` opts out if a native-looking
capture is wanted instead.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _settle(app, turns: int = 8) -> None:
    """Let deferred work run. The register reloads its model on a zero-timer
    (see RegisterModel._write), so a grab taken in the same turn as the open
    catches a half-populated table."""
    for _ in range(turns):
        app.processEvents()


def shoot(out_dir: Path, theme: str, onscreen: bool, db_path: Path,
          rebuild: bool = False) -> list[Path]:
    if not onscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt5.QtWidgets import QApplication

    from mammon import db, demo, ledger
    from mammon.ui import style, widgets

    app = QApplication.instance() or QApplication(sys.argv[:1])
    # Stock appearance only. apply_theme falls back to each theme's own palette
    # for anything left unset, so passing just the theme name renders what a new
    # install looks like. Inheriting the saved display preferences instead put
    # the DARK theme's row colours into the light screenshot -- dark text on
    # dark rows, a combination the app never actually shows anyone.
    style.apply_theme(app, {"theme": theme})

    # Build the demo ONCE and keep it. Regenerating per run cost ~7 seconds a
    # shot and, worse, re-drew every amount whenever the module changed, so two
    # screenshots taken minutes apart disagreed. The cached file lives under
    # data/ (gitignored) -- it is never committed; only the PNGs are.
    conn = db.init_db(str(db_path))
    existing = ledger.list_accounts(conn, include_closed=True)
    if rebuild and existing:
        conn.close()
        db_path.unlink()
        conn = db.init_db(str(db_path))
        existing = []
    if existing:
        ids = {"checking": None, "card": None, "brokerage": None}
        by_name = {a["name"]: a["id"] for a in existing}
        ids["checking"] = by_name.get("Everyday Checking")
        ids["card"] = by_name.get("Rewards Card")
        ids["brokerage"] = by_name.get("Brokerage")
        if None in ids.values():
            raise SystemExit(
                f"{db_path} is not a Mammon demo ledger; pass --rebuild or --db")
        print(f"reusing {db_path}")
    else:
        ids = demo.build(conn)
        print(f"built {db_path}")

    win = widgets.MainWindow(conn, db_path=str(db_path))
    # No resize here on purpose: the shot should show the window a user actually
    # gets on launch, so the screenshot stays honest about the default layout.
    win.show()
    # The calendar's account slots persist from whatever the last real run
    # chose, which for a fresh profile is the first account alphabetically --
    # Emergency Savings, whose only activity is monthly interest. The page then
    # shows an all-but-empty month and a chart of $20 bars. Point it at the
    # accounts the spending actually flows through.
    win.calendar.slots.set_account_ids([ids["checking"], ids["card"]], save=False)
    win.calendar.refresh()
    _settle(app)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def grab(name: str) -> None:
        path = out_dir / f"{name}-{theme}.png"
        win.grab().save(str(path))
        written.append(path)

    def show_register(account_id: int) -> None:
        """Open a register and land it on a whole row. Left where it opens, the
        table shows a half-height row clipped against the header -- correct
        behaviour, but it reads as a rendering fault in a still image."""
        win.open_register(account_id)
        _settle(app)
        view = getattr(win.stack.currentWidget(), "view", None)
        if view is not None:
            # Trim the window to a whole number of rows before parking at the
            # bottom. The view already scrolls per item, but scrollToBottom
            # guarantees the LAST row is fully visible, so a viewport that is
            # 22.5 rows tall pushes the leftover half-row up under the header --
            # correct on screen, but in a still image it reads as a rendering
            # fault. Losing a few pixels of height costs nothing here.
            row_h = view.rowHeight(0) or view.verticalHeader().defaultSectionSize()
            extra = view.viewport().height() % row_h if row_h else 0
            if extra:
                win.resize(win.width(), win.height() - extra)
                _settle(app)
            view.scrollToBottom()
        _settle(app)

    grab("home")                                  # the Financial Calendar
    show_register(ids["checking"])
    grab("register")
    show_register(ids["brokerage"])
    grab("investments")

    win.close()
    conn.close()
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    ap.add_argument("--theme", default="dark", choices=("dark", "light", "both"))
    ap.add_argument("--onscreen", action="store_true",
                    help="render on the real platform instead of offscreen")
    ap.add_argument("--db", default=str(ROOT / "data" / "demo.db"),
                    help="cached demo ledger (built on first use; gitignored)")
    ap.add_argument("--rebuild", action="store_true",
                    help="discard the cached demo ledger and build it again")
    args = ap.parse_args(argv)

    themes = ("dark", "light") if args.theme == "both" else (args.theme,)
    for theme in themes:
        for path in shoot(Path(args.out), theme, args.onscreen,
                          Path(args.db), args.rebuild):
            print(f"wrote {path.relative_to(ROOT)}  "
                  f"({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
