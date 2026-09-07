"""Mammon desktop entry point.

    python -m mammon.app [--db PATH] [--account ID] [--demo]

Opens the tabbed window: an Accounts overview (per-account balance + net worth)
and a classic register per account. A brand-new database (or --demo) is
seeded with a little sample data so the register is not empty on first run; an
existing file is never re-seeded.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mammon import crashlog, db, ledger


def sample_data(conn) -> None:
    """Seed a small, self-contained example: three accounts, a few categories,
    ordinary payments/deposits, and one transfer (to exercise every column).

    This is the MINIMAL fixture the test suite builds on; --demo seeds
    :func:`mammon.demo.build` instead. Changing what this produces breaks the
    tests that assert against these exact rows."""
    chk = ledger.create_account(conn, "Checking", "checking",
                                opening_balance=2_500_00, opening_date="2020-01-01")
    sav = ledger.create_account(conn, "Savings", "savings",
                                opening_balance=10_000_00, opening_date="2020-01-01")
    visa = ledger.create_account(conn, "Visa", "credit",
                                 opening_balance=0, opening_date="2020-01-01")

    salary = ledger.resolve_category(conn, "Income:Salary")
    groceries = ledger.resolve_category(conn, "Groceries")
    utilities = ledger.resolve_category(conn, "Bills:Utilities")
    dining = ledger.resolve_category(conn, "Dining")

    ledger.add_transaction(conn, chk, "2024-01-05", 5_000_00, payee="Employer",
                           category_id=salary, num="DEP", cleared=1)
    ledger.add_transaction(conn, chk, "2024-01-08", -85_32, payee="Whole Foods",
                           category_id=groceries, memo="weekly shop")
    ledger.add_transaction(conn, chk, "2024-01-15", -120_00, payee="City Power",
                           category_id=utilities, num="1201", memo="January")
    ledger.add_transaction(conn, visa, "2024-01-18", -45_99, payee="Venmo",
                           memo="concert tickets")
    ledger.add_transaction(conn, visa, "2024-01-20", -32_10, payee="Trattoria",
                           category_id=dining)
    ledger.create_transfer(conn, chk, sav, "2024-01-25", 1_000_00, memo="monthly savings")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mammon.app", description="Mammon register")
    p.add_argument("--db", default=None,
                   help="path to the Mammon database file "
                        "(default: <install root>/data/mammon.db)")
    p.add_argument("--account", type=int, default=None,
                   help="open this account's register on startup")
    p.add_argument("--demo", action="store_true",
                   help="seed sample data (only if the database has no accounts)")
    return p


def _ensure_seed(conn, demo: bool) -> None:
    # Seed the example accounts ONLY on an explicit --demo. Never fabricate a
    # "test set" into a real or empty database on first open (that made the
    # sample data masquerade as the user's real data).
    #
    # --demo builds the FULL synthetic ledger (mammon.demo), not sample_data
    # above: the account bar's group subtotals, the running balance, and the
    # Financial Calendar all need real history before they show anything.
    # sample_data stays as it is because ~40 tests use it as a minimal fixture
    # and assert its exact contents.
    if demo and not ledger.list_accounts(conn, include_closed=True):
        from mammon import demo as demo_mod
        demo_mod.build(conn)


def _resolve_db(arg, root=None) -> str:
    """Resolve the database file, INDEPENDENTLY of the current working directory.

    ``--db`` always wins. Otherwise the default database lives at a fixed
    location anchored to the Mammon install root (``root``, defaulting to the
    parent of this package), so every launch opens the SAME file no matter which
    directory the process was started from. This is deliberate: resolving the
    default relative to the CWD means starting the app from a different
    directory silently opens a *different* (often brand-new, empty) database.
    Learned payee/category rules committed in one session then appear "lost" the
    next session, even though every write succeeded -- they are simply in
    another file.

    An explicit ``--db`` is AUTHORITATIVE: it is used verbatim (existing OR a
    not-yet-created path, relative OR absolute) and NEVER falls back to the
    anchored default. main() creates the file and its parent dirs. The only
    exception is a path that cannot be a database file at all -- e.g. it names
    an existing directory -- which fails loudly here so the caller sees a clear
    error instead of silently opening a different database.
    """
    if arg:
        p = Path(arg)
        if p.is_dir():
            raise SystemExit(
                f"--db: {arg!r} is a directory, not a database file"
            )
        return arg
    if root is not None:                      # explicit root: tests pin one
        return str(Path(root) / "data" / "mammon.db")
    # The single, durable, CWD-independent default. Created on first run.
    # Resolved by mammon.paths, which also honours $MAMMON_DATA_DIR -- this
    # function used to ignore it while backups and the download log obeyed it,
    # so redirecting that variable moved everything EXCEPT the database.
    from mammon import paths
    return str(paths.default_db_path())


def _open_db(db_path):
    """Prepare and open the resolved database, mirroring what launch needs.

    Split out from :func:`main` so the DB-resolution/creation contract (an
    explicit ``--db`` opens/creates exactly that file, never mammon.db) can be
    exercised in tests without spinning up the Qt GUI.
    """
    # The default lives under an anchored data/ dir that may not exist on a
    # fresh checkout; sqlite3.connect() will not create it, so ensure it here.
    # An explicit --db pointing at an unwritable location must fail loudly with a
    # clear message rather than dying with an opaque OSError deep in sqlite.
    try:
        Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"--db: cannot create database at {db_path!r}: {exc}")
    # Install the top-level crash handler as early as possible so an otherwise
    # silent PyQt slot exception (which aborts with no usable trace) leaves a
    # diagnosable record next to the database instead of vanishing.
    crashlog.install_excepthook(crashlog.crash_log_path(db_path))
    conn = db.init_db(db_path)
    # The learned trees are NOT seeded here. Opening a ledger used to bootstrap
    # the rename tree from register history, replaying every posted row's
    # memo->payee pair as though it were an accepted rename. That premise only
    # holds for downloaded rows, where the memo IS the bank's text; for
    # hand-entered and QIF-imported history the memo is a note the USER typed. A
    # 1998 memo of "deposit" on a Foothill Place row therefore taught the tree
    # that MOBILE DEPOSIT means Foothill Place -- a payee never chosen in any
    # review -- in a ledger deliberately started fresh.
    #
    # Both trees now learn only from what the user actually accepts in review.
    # ``rename_tree.ensure_bootstrapped`` / ``category_tree.ensure_bootstrapped``
    # remain as callable API for an explicit "seed from history" action, but
    # nothing invokes them on open. (By request.)
    return conn


def _launch_gui(conn, db_path, account) -> int:
    # Import Qt lazily so headless tooling can import mammon.app without a display.
    from PyQt5.QtWidgets import QApplication
    from mammon.ui import prefs
    from mammon.ui.style import apply_theme
    from mammon.ui.widgets import MainWindow

    app = QApplication(sys.argv[:1])
    # Qt's own warnings go to a stderr a windowed app does not have, so the
    # message it prints immediately before misbehaving is lost. Route them into
    # the crash log next to the database, where they sit alongside the Python
    # traceback (or the faulthandler dump, when the fault was below Python).
    crashlog.install_qt_message_handler(crashlog.crash_log_path(db_path))
    # Apply the user's persisted Display Preferences (font/colors/shading); with
    # none saved this is exactly the default classic look.
    apply_theme(app, prefs.display_prefs())
    window = MainWindow(conn, db_path=db_path)
    if account is not None:
        window.open_register(account)
    window.show()
    return app.exec_()


def _launch_gui_locked(db_path, account=None) -> int:
    """Start the GUI for an ENCRYPTED database: ask, then open.

    Separate from :func:`_launch_gui` because the order is forced. The password
    dialog is a Qt widget, so a QApplication must exist before it can be shown --
    which means the application starts before the database is open, the reverse of
    the normal path. Cancelling exits without opening anything, and never falls
    back to opening the file unkeyed."""
    from PyQt5.QtWidgets import QApplication

    from mammon.ui import prefs
    from mammon.ui.password_dialog import ask_password
    from mammon.ui.style import apply_theme
    from mammon.ui.widgets import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_theme(app, prefs.display_prefs())
    key = ask_password(None, db_path)
    if key is None:
        return 1
    conn = db.init_db(db_path, key)
    crashlog.install_excepthook(crashlog.crash_log_path(db_path))
    crashlog.install_qt_message_handler(crashlog.crash_log_path(db_path))
    window = MainWindow(conn, db_path=db_path, db_key=key)
    if account is not None:
        window.open_register(account)
    window.show()
    return app.exec_()


def main(argv=None) -> int:
    # ``argv is None`` MUST fall through to argparse's default (``sys.argv[1:]``),
    # NOT an empty list. ``python -m mammon.app --db X`` reaches here via
    # ``sys.exit(main())`` with no argument, so forcing ``[]`` silently discarded
    # the real command line -- ``--db`` was dropped, ``args.db`` stayed ``None``,
    # and startup fell back to the anchored ``mammon.db`` even though a valid
    # ``--db`` was given. Passing ``argv`` straight through lets an explicit
    # ``--db`` win from the CLI exactly as it already does from tests.
    args = build_parser().parse_args(argv)
    db_path = _resolve_db(args.db)
    # An encrypted ledger needs its password before anything can be read from it,
    # and asking needs a QApplication -- so the prompt happens inside the GUI
    # launch, not here. A plaintext ledger (the default) opens exactly as before.
    from mammon import encryption
    if encryption.is_encrypted(db_path):
        return _launch_gui_locked(db_path, args.account)
    conn = _open_db(db_path)
    _ensure_seed(conn, args.demo)
    return _launch_gui(conn, db_path, args.account)


if __name__ == "__main__":
    sys.exit(main())
