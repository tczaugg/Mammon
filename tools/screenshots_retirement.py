"""Regenerate the README's Retirement Planner and Investment Dashboard shots.

    python tools/screenshots_retirement.py [--out docs/images] [--theme dark|light|both]

Never point this at a real ledger. It builds its own household from
:func:`mammon.demo.build_retirement` - one person a year from retiring, $1.2M
across a traditional IRA, a Roth IRA and a brokerage account - so nothing in
``docs/images`` can be anything but invented. The built ledger is cached
(``--db``, default ``data/demo-retirement.db``, gitignored) and reused;
``--rebuild`` starts over. Open it yourself with ``--db data\\demo-retirement.db``.

The plan is applied here, not in the builder: the household lives on $84,000 a
year rising 2.5%, IRA draws held within the 12% bracket (the brokerage and the
Roth pay the rest) and the tax paid from the brokerage account; then the years
before required minimums are filled with Roth conversions to the 12%, 22% and
24% tops in turn, under the next IRMAA tier and over it, and the fill whose tax
total in today's dollars is lowest is the one kept and shown.

Runs headless (Qt's offscreen platform); ``--onscreen`` opts out.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPENDING_CENTS = 84_000_00
SPENDING_RISE_PCT = "2.5"
#: IRA draws held within the 12% bracket: the brokerage and the Roth pay the
#: rest of the spending, which leaves the room above for conversions.
DRAW_BRACKET = "12"
#: (bracket top, stay under the next IRMAA tier). The IRMAA surcharge is in the
#: total either way, so a fill that crosses a tier competes on equal terms.
FILL_CANDIDATES = (("12", True), ("22", True), ("22", False), ("24", False))
WINDOW = (1600, 1000)


def _settle(app, turns: int = 10) -> None:
    for _ in range(turns):
        app.processEvents()


def build_or_reuse(db_path: Path, rebuild: bool, today: _dt.date):
    from mammon import db, demo, ledger

    conn = db.init_db(str(db_path))
    existing = ledger.list_accounts(conn, include_closed=True)
    if rebuild and existing:
        conn.close()
        db_path.unlink()
        conn = db.init_db(str(db_path))
        existing = []
    if existing:
        by_name = {a["name"]: a["id"] for a in existing}
        wanted = {role: by_name.get(name)
                  for role, (name, *_rest) in demo.RETIREMENT_ACCOUNTS.items()}
        if None in wanted.values():
            raise SystemExit(f"{db_path} is not the retirement demo; pass --rebuild or --db")
        print(f"reusing {db_path}")
        return conn, wanted
    ids = demo.build_retirement(conn, today)
    print(f"built {db_path}")
    return conn, ids


def choose_fill(page, app, first: int, last: int) -> tuple[str, int]:
    """Fill the conversion years to each bracket top in turn and keep the one
    whose tax total in today's dollars is lowest. Returns (rate, total)."""
    page.confirm = lambda *_a, **_k: True       # the Clear button's yes/no, headless

    def fill(rate: str, under_irmaa: bool) -> int:
        page.clear_all_conversions()
        _settle(app)
        page.fill_years_to_bracket(first, last, rate, stay_under_irmaa=under_irmaa)
        _settle(app)
        page.refresh()
        _settle(app)
        _total, tax = page.tax_totals()
        _irmaa, irmaa = page.irmaa_totals()
        _left, _end, end = page.end_ira_tax()
        return tax + irmaa + end

    best: tuple[str, bool, int] | None = None
    for rate, under in FILL_CANDIDATES:
        total = fill(rate, under)
        print(f"  fill to {rate}%{' under IRMAA' if under else ''}: "
              f"{total / 100:,.0f} in today's dollars")
        if best is None or total < best[2]:
            best = (rate, under, total)
    assert best is not None
    if (best[0], best[1]) != FILL_CANDIDATES[-1]:
        fill(best[0], best[1])
    return best[0], best[2]


def shoot(out_dir: Path, theme: str, onscreen: bool, db_path: Path,
          rebuild: bool = False) -> list[Path]:
    if not onscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt5.QtWidgets import QApplication

    from mammon import retirement
    from mammon.ui import style, widgets

    app = QApplication.instance() or QApplication(sys.argv[:1])
    style.apply_theme(app, {"theme": theme})
    today = _dt.date.today()
    conn, ids = build_or_reuse(db_path, rebuild, today)

    win = widgets.MainWindow(conn, db_path=str(db_path))
    # Larger than the launch default: both pages stack two charts, and the
    # honest size for a chart is one where its labels can be read.
    win.resize(*WINDOW)
    win.show()
    _settle(app)

    page = win.show_retirement_planner()
    _settle(app)
    retire = today.year + 1
    person = retirement.get_person(conn, ids.get("person") or
                                   retirement.list_people(conn)[0]["id"])
    born = int(person["birth_year"])
    first_rmd = born + retirement.applicable_age(born)
    params = retirement.get_withdrawal_plan(conn)
    if not params.is_set:
        page.withdrawals.apply_household(SPENDING_CENTS, SPENDING_RISE_PCT,
                                         start_year=retire, bracket_rate=DRAW_BRACKET)
        _settle(app)
        rate, total = choose_fill(page, app, retire, first_rmd - 1)
        print(f"kept the fill to the {rate}% top: {total / 100:,.0f} in today's dollars")
    page.refresh()
    _settle(app)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def grab(name: str) -> None:
        path = out_dir / f"{name}-{theme}.png"
        win.grab().save(str(path))
        written.append(path)

    grab("retirement")
    dash = win.show_investment_dashboard()
    _settle(app)
    dash.what_if_bar.retirement.setChecked(True)
    # The plan was written after the dashboard first drew itself: redraw with
    # the plan's draws in the projection.
    dash.refresh()
    _settle(app, 20)
    grab("dashboard")

    win.close()
    conn.close()
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    ap.add_argument("--theme", default="dark", choices=("dark", "light", "both"))
    ap.add_argument("--onscreen", action="store_true")
    ap.add_argument("--db", default=str(ROOT / "data" / "demo-retirement.db"))
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    themes = ("dark", "light") if args.theme == "both" else (args.theme,)
    for theme in themes:
        for path in shoot(Path(args.out), theme, args.onscreen, Path(args.db), args.rebuild):
            print(f"wrote {path.relative_to(ROOT)}  ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
