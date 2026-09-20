# CLAUDE.md

Guidance for Claude Code in this repository. This is an INDEX, deliberately
lean: the rules that are load-bearing on every call, plus pointers. It is
re-read on every api call of every session in this project, so subsystem detail
belongs in `docs/SRD.md`, not here.

## What this is

Mammon is a cross-platform desktop personal-finance application (Python 3.12 +
PyQt5) with a classic, dense account register, storing everything in one SQLite
file the user owns. Its concrete goal is absorbing ~40 years of existing
financial history and taking over ongoing transaction download from the user's
institutions.

Developed on Windows; macOS and Linux are supported in principle (no
platform-specific code beyond one shlex branch in `webslinger.py` and the
per-platform default font in `ui/style.py`) but are untested - do not assume a
change works there without saying so.

Packaging: `requirements.txt` and `pyproject.toml` list the SAME required set
(PyQt5, matplotlib, yfinance, mcp, sqlcipher3, PyYAML, pytest, pytest-xdist) and
there
are NO optional-dependency extras - an absent Python package is a broken install,
not a configuration. Everything runs from the repo root against the ambient
interpreter with no install step; the heavy imports stay lazy, so an offline
session never reaches the network. End users get `installer/` instead (SRD 9.2):
an embeddable Python plus those same requirements, installed per-user, with data
in `Documents\Mammon` because the install carries a `mammon-install.json` marker.

The ONE optional component is **webSlinger** (an external MCP tool, not a Python
package). Running without it is supported and tested: manual file import, manual
asset valuations, manual balances; the Account Details download fields stay empty
and Download explains what is missing. Never make a code path REQUIRE it.

## Where the detail lives

`docs/SRD.md` is the requirements document of record - read the compartment you
are working in before designing anything. Each is mostly self-contained: its
requirements, then an "Implementation notes" section on how it is built and
what each shape prevents.

| | Compartment | Read it when you are touching |
|---|---|---|
| A | Register, entry and editing | register rows, sorting/filtering, undo, category or date entry |
| B | Accounts, transfers and account types | account kinds, the transfer mirror model |
| C | Multi-currency | per-account currency, FX rates, home-currency rollup |
| D | Investments | securities, holdings, lots, cost basis, valuation, allocation |
| E | Cryptocurrency | crypto accounts and their two import shapes |
| F | Import and the review queue | parsers, the column map, dedup, the review panel, tags |
| G | Learned rules | payee/category learning - the five cooperating engines |
| H | Scheduled pre-entries, reminders, calendar | recurring bills, loan payments, projections |
| I | Reconciliation | reconciling against a statement, the audit log |
| J | Reports, charts and customization | aggregations, the customization bar, budgets, export |
| K | Database, backups and encryption | schema, migrations, checkpoints, snapshots, paths |
| L | Downloads, webSlinger and MCP | getting data from institutions, the LLM tool surface |
| M | Platform, architecture and open items | technology decisions, non-functional requirements |

Section numbers in the SRD are STABLE ANCHORS - around fifty comments in live
code cite them (`SRD 5.8f`). Keep a section's number wherever it moves.

## Commands

```powershell
# Run the app. The database is anchored to the install root, NOT the CWD.
python -m mammon.app
python -m mammon.app --db data\scratch.db --demo   # --demo seeds an empty DB only
python -m mammon.app --account 3                   # open a register on startup

# Tests. No conftest.py, no pytest.ini - invoke by path from the repo root.
python -m pytest mammon/tests/test_smoke.py -q        # ALWAYS: the app still runs
python -m pytest mammon/tests/test_ledger.py -q       # + the file(s) you changed
python -m pytest mammon/tests -q -n auto              # pre-push only (~16 workers)

# Create/inspect a database (prints schema version + table list)
python -m mammon.db data\scratch.db

# Windows installer ZIP from a commit (installs, runs, uninstalls itself first)
python installer\build.py [--ref TAG | --worktree]
```

A plain launch opens the database last opened in the window
(`mammon/last_db.py`, SRD 5.8y); `--db` is a one-off and never becomes the
default.

Qt tests set `QT_QPA_PLATFORM=offscreen` themselves at import - no display
needed. **12 acceptance tests are opt-in** and skip unless
`$MAMMON_ACCEPTANCE_DB` names a real ledger; they deliberately do not probe for
`data/mammon.db`, because anything that opens a real ledger by accident is one
migration away from modifying it.

## Layering

Strict, and the tests depend on it staying strict:

```
mammon/db.py          schema + migrations only (no business logic)
   |
mammon/ledger.py      domain: accounts, transactions, transfers, balances. UI-free.
mammon/investments.py holdings, price history, valuation (parallel domain layer)
mammon/loans.py       amortization + payment splitting (pure domain, no Qt)
   |
mammon/importers/     parsers -> NormalizedTxn -> core.py's single insert/dedup path
mammon/reports/       pure read-only aggregations returning plain data structures
   |
mammon/ui/            PyQt5 models/widgets - a thin projection, no SQL, no money logic
mammon/mcp_tools.py   the read-only LLM tool surface: functions over a connection -> JSON
mammon/mcp_server.py  binds those tools to MCP (the `mcp` SDK is imported only here)
```

## Rules

- **`mammon.ledger` is the only writer of transaction rows.** Importers, the
  review queue, the loan engine and the UI all funnel through it, so transfer
  invariants are enforced in exactly one place. Adding a second write path is
  the main way to break this codebase. The UI layer holds four raw SQL
  statements in total; keep it that way.
- **Money and precision are locked; do not deviate.** Cash amounts are signed
  integer cents everywhere (negative = money out), no floats ever. Share
  quantities and per-share prices are Decimal-encoded TEXT. Money rounds
  `ROUND_HALF_UP` at the cents boundary.
- **Dates are ISO `YYYY-MM-DD` in storage, the domain layer and download
  scripts.** What the user sees is one preference (`ui/prefs.date_format`) with
  two chokepoints: `ui/models.fmt_date` and `ui/models.parse_date`. Build every
  date field with `ui/delegates.make_date_edit` and read it with
  `date_edit_iso` - never hardcode a display format or accept a date as free
  text.
- **Never edit an existing migration.** `db.py` holds an ordered `MIGRATIONS`
  list; index *i* upgrades the DB from version *i* to *i+1*, tracked in
  `PRAGMA user_version`, with `SCHEMA_VERSION = len(MIGRATIONS)` (currently 75).
  Append a new `_Vn` and add it to the list - real databases have already
  applied the existing ones. `init_db()` is idempotent and safe on new and
  existing files. (That number is pinned by
  `test_db.test_claude_md_states_the_real_schema_version`: bump it in the same
  commit as the migration, or the suite fails.)
- **`mammon/paths.py` is the only place that decides where data lives.** Never
  resolve a data path anywhere else; any new default path must go through it
  and be test-overridable. Three modules once answered this separately and the
  copies drifted.
- **The app holds no credentials at all.** Login, MFA and secret storage belong
  entirely to the webSlinger/keyCocoon side. Nothing on the download path gates
  on a secret, and no credential vault belongs in this repo.
- **The MCP boundary is read-only.** Its connection sets `PRAGMA query_only`,
  refuses a database whose schema is not exactly the code's, and never returns
  `accounts.account_number`, `url` or `download_config`. Money crosses it as
  decimal dollar strings, never floats.
- **`data/` holds real financial history and is gitignored**, along with every
  `*.db`, `*.bak`, `*.qif`, `*.ofx`, `*.qfx`. The only exception is
  `mammon/tests/fixtures/`, which is synthetic. Never commit a real ledger, an
  account number, or a person's name.
- **Modals inside `setModelData` corrupt the heap.** A delegate's
  `setModelData` runs while Qt is destroying the editor, so a modal there opens
  a nested event loop that frees the editor the frame still holds -
  `0xc0000374`, no Python traceback. Defer with `QTimer.singleShot(0, ...)`,
  parent to the VIEW (never the editor), and write back through a
  `QPersistentModelIndex`. Same root cause as `RegisterModel._write`'s
  `defer_reload`.
- **Headless-modal hazard:** a `QMessageBox`/`QDialog` built and `exec_()`-ed
  directly blocks forever under the offscreen platform. Route user choices
  through `QMessageBox.question` (the seam tests patch) or a small overridable
  method, and bound any refine loop.
- Module docstrings carry the *rationale* - why a design was chosen and what
  bug the current shape prevents. They are load-bearing; when you change
  behavior, update the reasoning rather than deleting it.
- **American English, in identifiers and in prose.** center, color, behavior,
  gray, license, normalize - never the British spelling. This is not a style
  preference to weigh against matching nearby code: an LLM writing this codebase
  drifts to British spellings wherever no external API pins the token (it once
  wrote `color=colour` on ONE line - American where matplotlib forced it, British
  where the name was its own), and every later session then matched the drift
  because that is what reading like the surrounding code means. 358 occurrences
  across 86 identifiers accumulated that way before anyone said so. If you find
  one, it is a defect: fix it rather than matching it.
- **Tests open a database with `fresh_db`, not `db.init_db`.** `from
  mammon.tests import fresh_db` - it copies a per-process, already-migrated
  template instead of replaying all 75 migrations per test, which took the
  suite from 332s to 55s. It falls back to the real `init_db` for an in-memory
  or encrypted database and for a file that already has content (the idempotent
  upgrade path), so the six tests whose SUBJECT is the schema, backups or
  encryption still call `db.init_db` directly.
- Every behavioral fix lands with a regression test in `mammon/tests/`. Test
  files mirror module names - so run `test_smoke.py` plus `test_<module>.py` for
  what you changed, and NOT the whole suite (that is a pre-push step).
- New features touching requirements get reflected back into the right
  `docs/SRD.md` compartment.
