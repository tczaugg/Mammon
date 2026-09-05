# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Mammon is a Windows desktop personal-finance application (Python 3.12 + PyQt5) with a classic,
dense account register, storing everything in one SQLite file the user owns. Its concrete goal is
absorbing ~40 years of existing financial history and taking over ongoing transaction download
from the user's institutions.

`docs/SRD.md` is the requirements document of record — read it before designing anything new; it
states the data model, the import strategy, the download strategy, and which decisions are locked.
`docs/qif_import_research.md` covers reading Quicken's own export formats.

Packaging is minimal: `requirements.txt` (PyQt5, matplotlib) and a setuptools `pyproject.toml`
whose extras are `mcp` (the MCP server), `quotes` (`yfinance`) and `dev` (`pytest`). Everything
still runs from the repo root against the ambient interpreter with no install step. `yfinance` and
`mcp` are imported lazily where used, so the app never needs them; investment quote tests use
injected fakes.

## Commands

```powershell
# Run the app. The database is anchored to the install root, NOT the CWD.
python -m mammon.app
python -m mammon.app --db data\scratch.db --demo   # --demo seeds sample data into an empty DB only
python -m mammon.app --account 3                   # open a register on startup

# Tests. No conftest.py, no pytest.ini — invoke by path from the repo root.
python -m pytest mammon/tests -q
python -m pytest mammon/tests/test_ledger.py -q
python -m pytest mammon/tests/test_ledger.py::test_transfer_creates_both_sides -q
python -m pytest mammon/tests -q -k transfer

# Create/inspect a database (prints schema version + table list)
python -m mammon.db data\scratch.db
```

Qt tests set `QT_QPA_PLATFORM=offscreen` themselves at module import — no display needed, no setup
before `pytest`.

**12 acceptance tests are opt-in** and skip unless `$MAMMON_ACCEPTANCE_DB` names a real ledger.
They deliberately do not probe for `data/mammon.db`: they previously triggered on mere file
existence, ran against an unrelated database, and failed confusingly. Anything that opens a real
ledger by accident is one migration away from modifying it.

## Architecture

Strict layering, and the tests depend on it staying strict:

```
mammon/db.py          schema + migrations only (no business logic)
   ↓
mammon/ledger.py      the domain layer: accounts, transactions, transfers, balances. UI-free.
mammon/investments.py holdings, price history, valuation (parallel domain layer)
mammon/loans.py       amortization + payment splitting (pure domain, no Qt)
   ↓
mammon/importers/     parsers → NormalizedTxn → core.py's single insert/dedup path
mammon/reports/       pure read-only aggregations returning plain data structures
   ↓
mammon/ui/            PyQt5 models/widgets — a thin projection, holds no SQL and no money logic
mammon/mcp_tools.py   the read-only LLM tool surface: plain functions over a connection → JSON
mammon/mcp_server.py  binds those tools to MCP (the `mcp` SDK is imported only here)
```

The MCP server's connection has `PRAGMA query_only` set and refuses a database whose schema is
not exactly the code's; it never writes and never returns `accounts.account_number`, `url` or
`download_config` (the SQL tool runs under an authorizer that blanks them). Money crosses that
boundary as decimal dollar strings, never floats.

**`mammon.ledger` is the only writer of transaction rows.** Importers, the review queue, the loan
engine, and the UI all funnel through it, so transfer invariants are enforced in exactly one place.
Adding a second write path is the main way to break this codebase. The UI layer currently contains
four raw SQL statements in total; keep it that way.

### Money and precision conventions (locked; do not deviate)

- Cash amounts are **signed integer cents** everywhere. Negative = money out. No floats, ever.
- Share quantities and per-share prices are **Decimal-encoded TEXT**, so fractional shares and
  multi-decimal prices keep full precision. Money rounds `ROUND_HALF_UP` at the cents boundary.
- Dates are ISO `YYYY-MM-DD` strings **in storage, the domain layer and download scripts**.
  What the user sees and types is governed by one preference (`ui/prefs.date_format`) with two
  chokepoints: `ui/models.fmt_date` renders ISO to that format, `ui/models.parse_date` converts
  back. Build every date field with `ui/delegates.make_date_edit` (typing + calendar) and read it
  with `date_edit_iso` — never hardcode a display format or accept a date as free text.

### Transfers (the mirror model)

A transfer is **two** linked transactions, one per account, equal and opposite, cross-linked by
`transfer_pair_id` with each row's `transfer_account_id` pointing at the other account. Editing
amount/date on one side syncs the other; deleting one deletes both. The "category" of a transfer is
virtual — it renders as `[Other Account]` and consumes no category row. Every layer that touches
transactions must exclude or collapse transfers explicitly, or net worth double-counts.

This model is also why the app needs no third-party-payee field: give an intermediary (Venmo,
PayPal) its own **account**, and money moving to and from it is an ordinary transfer with the real
payee and memo living on that account's own register rows. A vestigial `third_party` column was
dropped in migration 28.

### Schema migrations

`mammon/db.py` holds an ordered `MIGRATIONS` list; index *i* upgrades the DB from version *i* to
*i+1*, tracked in `PRAGMA user_version`, with `SCHEMA_VERSION = len(MIGRATIONS)` (currently 50).
**Append a new `_Vn` and add it to the list — never edit an existing migration**, since real
databases have already applied them. `init_db()` is idempotent and safe on new and existing files.

### Balance checkpoints

Running balances use per-`(account, year)` rows in `balance_checkpoints` so a 40-year multi-account
file opens and scrolls fast. Any write that changes history calls `_touch_checkpoints(...)`, which
cascades a recompute from the earliest year touched forward. `ledger.account_balance` reads through
the checkpoint path; the full-sum version is retained as the oracle it must match, asserted by
`test_year_end_snapshots.py`. Investments have the analogous `holdings_checkpoints`.

### Two ingestion paths

1. **Files** — `importers.import_file(conn, path, ...)` infers the format and dispatches to a parser
   (`qif`, `ofx`/`qfx`, `csv`, `json`). Every parser is a pure function
   `parse(text, default_account=None) -> list[NormalizedTxn]` with no database access.
   `importers/core.py` owns everything database-facing: account/category resolution, dedup (exact by
   `fitid`, else a fuzzy amount/date/payee score recorded in `transaction_matches`), collapsing
   transfer mirrors into one `ledger.create_transfer`, and writing the run to `imports`.
2. **Downloads** — `mammon/downloads.py` runs a recorded webSlinger script through an *injected*
   runner, producing a downloaded file (EXPORT mode) or scraped rows (SCRAPE mode). Rows do **not**
   go straight into the register: `import_review.build_review` classifies each as `NEW` or
   `MATCHING`, stores them in `review_items`, and `ui/import_review_widget.py` renders a review panel
   below the register. Nothing enters the ledger until the user accepts or saves a row, and
   `mammon/import_review.py` is the sole writer for that flow.

`NormalizedTxn` (`importers/record.py`, SRD §6.4) is the waist of the hourglass: parsers stay
ignorant of the database, and `core.py` stays ignorant of file formats.

### Delimited (CSV-ish) imports and the column map

`importers/tabular.py` detects a delimited file's real header (skipping preamble/footer), sniffs the
delimiter (`, ; tab |` — so `.csv`, `.tsv`, `.tab`, `.txt` all work), and infers which column feeds
each role. `ui/import_mapping.py` is the wizard that lets the user overrule it, reachable from the
new-format prompt (Save / Adjust / Cancel). An accepted map is saved as a profile fingerprinted by
header signature, so that format never prompts again.

Two things here are load-bearing and easy to undo by accident:

- **A user-confirmed map must win.** `parse_csv` routes on header vocabulary, and a recognized date
  column sends it down the legacy path. Passing `roles_authoritative=True` overrides that for cash
  sources. Without it, a saved profile was silently discarded whenever the header merely *looked*
  conventional — exactly when an override is needed, since in `Date,Description,Amount,Balance` both
  money columns are recognizable and equally money-shaped.
- **Previews must name the source column.** A wrong column still produces plausible output (a
  balance column parses as money just like an amount column), so values alone cannot tell a user
  whether the mapping is right. Both the prompt and the wizard render `Role <- source column`.

### Learned rules (four cooperating engines)

Raw bank `statementDescription` text is gobbledygook, so the app learns from corrections:

- `rename_tree.py` — payee renaming AND investment-action mapping via an online discriminative
  **trie** per domain that grows only where it must to tell two labels apart. Tokens are ranked by
  **label entropy** (purest first — measured, not assumed: frequency was the wrong axis), and
  pure-numeric, very-short, and unseen mixed letter+digit tokens (ISINs, auth codes) are dropped
  during normalization (the overfitting guards). A supplied payee field is ranking *evidence*, not
  a gate; only a high-confidence pure rename overrides it. `RANKING_VERSION` forces a one-time
  rebuild from history when the ranking algorithm changes — a trie built under one ranking is
  unreachable under another.
- `category_rules.py`, `transfer_rules.py` — `keyword -> category_id` / `keyword -> account_id`,
  sharing their tokenizer with each other via `keywords.py` so they stay in lock-step.
- `categorize.py` — payee-level `import_mappings` keyed on the *normalized* payee, with a `source`
  ranking where an explicit user choice outranks an inferred one.

### Scheduled pre-entries vs. imports

Loan payments (`loans_schedule.py`) and generic recurring bills (`scheduled.py`) are pre-entered as
`scheduled=1` placeholder rows before the bank transaction arrives; the real import then *merges
into* the placeholder. These definitions are app-generated and never present in any QIF/OFX/CSV —
importers neither read nor write them, and `core._find_dup_cash` only matches `scheduled=0` rows, so
definitions and placeholders both survive a re-import. Regression:
`test_scheduled.py::test_scheduled_definitions_survive_qif_reimport`.

### Backups are incremental, by page

`backup.py` writes two kinds of snapshot into `data/backups/<db-file-name>/` — **one folder per
database**:

- `<db>.<tag>.<stamp>.bak` — a full copy, a plain SQLite file anything can open. Manual backups are
  always this.
- `<db>.<tag>.<stamp>.delta` — only the 4 KB pages that differ from the newest full snapshot,
  gzipped, behind a JSON header naming its baseline and the checksum of the reconstructed file.

Two independent guards keep the 1/minute auto-backup cheap, and both are load-bearing:
`db_fingerprint` skips the write entirely when nothing changed, and the delta stops the writes that
*do* happen from copying 12 MB to record a 15 KB change. Measured on the real ledger, a minute of
work touches ~14 pages of 2,999.

Four invariants:

- **A restore never crosses databases.** `restore_backup` refuses a snapshot whose own
  `<db-file-name>` prefix disagrees with the file being replaced (`snapshot_db_name` /
  `check_same_database`), and the Restore picker opens in this database's own folder. This is not
  hypothetical: a scratch `mammon2.db` snapshot was once restored over the real ledger from the old
  shared folder, and the result is a perfectly valid, completely wrong ledger — the loss went
  unnoticed for hours. The check applies when the target EXISTS; writing a snapshot out to a new
  file destroys nothing and is allowed. Legacy flat snapshots are still listed and restorable;
  `python -m mammon.backup organize` moves them into their folders.


- **Deltas reference the baseline directly, never each other.** No chains — one damaged delta costs
  one restore point, not every point after it. Growth against a fixed baseline is sublinear, so this
  costs almost nothing.
- **Retention must never orphan a baseline.** `prune_backups` and `purge_auto_backups` hold a full
  snapshot back past its turn while any surviving delta names it. Dropping it is the one way this
  scheme loses real data.
- **A rebuilt delta is checksum-verified** against the hash taken when it was written; a mismatch
  raises rather than handing back a plausible-looking database.

Anything needing a real file from either kind calls `backup.restore_backup(snapshot, out)`.
`python -m mammon.backup list|verify|restore` does the same from a terminal, so a delta is never a
dead end without the GUI. Rejected: year/account-scoped backups (SQLite doesn't lay rows out by
year, and rename-tree/mapping/review changes touch no transaction at all) and an operation log
(makes backup correctness depend on the code version that replays it, to save megabytes that no
longer exist).

## Paths, and why they are the way they are

Every path in this app resolves from the **install root**, never the current working directory. This
is not stylistic — each of these was a real bug:

- `app._resolve_db` — `--db` is authoritative and used verbatim; otherwise the default is
  `<install root>/data/mammon.db`. Resolving relative to the CWD meant launching from a different
  directory silently opened a *different*, empty database, and learned rules looked lost.
- `backup.DEFAULT_BACKUP_DIR` — anchored at import from the install root, overridable via
  `$MAMMON_DATA_DIR` and monkeypatched by tests. It was `Path("data")/"backups"`, so snapshots
  landed wherever the process happened to start.
- `download_log.default_data_dir()` — derived from the package location. It hardcoded one
  developer's absolute install path.

The last two are how a test run once reached a real ledger and migrated it. **Any new default path
must be anchored and test-overridable**; a test that can write outside `tmp_path` will eventually
write somewhere that matters.

## Configuration

- **The app holds no credentials at all.** Login, MFA, and secret storage belong entirely to the
  webSlinger/keyCocoon side — the MCP server drives the site using the credentials in the user's own
  browser session. Nothing on the download path gates on a secret, and no credential vault belongs
  in this repo. Don't reintroduce one.
- UI display preferences go to QSettings, never to the database (`ui/prefs.py`). This includes
  the PER-ACCOUNT one/two-line register layout (`account_view_mode`) and the
  transaction-accepted sound.
- Env vars: `MAMMON_WEBSLINGER_MCP_CMD` (MCP launch command), `MAMMON_DATA_DIR`,
  `MAMMON_DOWNLOAD_LOG`, `MAMMON_ACCEPTANCE_DB` (opt into the real-ledger acceptance tests).
- `crashlog.py` installs a `sys.excepthook` early in startup because PyQt otherwise swallows
  exceptions raised inside slots; crashes land in a log next to the database.

## Working in this repo

- **`data/` holds real financial history and is gitignored**, along with every `*.db`, `*.bak`,
  `*.qif`, `*.ofx`, `*.qfx`. The only exception is `mammon/tests/fixtures/`, which is synthetic.
  Never commit a real ledger, an account number, or a person's name.
- Module docstrings carry the *rationale* — why a design was chosen and what bug the current shape
  prevents. They are load-bearing; when you change behavior, update the reasoning rather than
  deleting it.
- Every behavioral fix lands with a regression test in `mammon/tests/`. Test files mirror module
  names.
- **Modals inside `setModelData` corrupt the heap.** A delegate's `setModelData` runs while Qt is
  destroying the editor, so a modal there opens a nested event loop that lets the teardown finish and
  frees the editor the frame still holds — `0xc0000374`, no Python traceback. Defer any such dialog
  with `QTimer.singleShot(0, ...)`, parent it to the VIEW (never the editor), and write back through a
  `QPersistentModelIndex`. Same root cause as `RegisterModel._write`'s `defer_reload`.
- **Headless-modal hazard:** a `QMessageBox`/`QDialog` built and `exec_()`-ed directly blocks forever
  under the offscreen platform. Route user choices through `QMessageBox.question` (the seam tests
  patch) or a small overridable method, and bound any refine loop — anything that can run unattended
  must terminate on its own.
- New features touching requirements should be reflected back into `docs/SRD.md`.
- Tests should be added along with new features that properly test them.
