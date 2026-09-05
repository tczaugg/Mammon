# Mammon

A personal-finance ledger for people who want to own their own data.

Mammon is a Python desktop application (PyQt5) with a classic, dense, keyboard-
friendly account register. Everything it knows lives in one SQLite file on your
disk — a file you can copy, back up, query, and read with any tool that speaks
SQL. There is no cloud, no account, and no subscription.

It exists because financial history outlives the software that recorded it.

## Why the name

Mammon is the name I used for my financial database for 40 years. I chose it 
as a continual reminder that money is not my master.

## What it does

- **Register** — per-account ledger with inline editing, splits, running
  balances, cleared/reconciled flags, and transfers modelled as two linked
  rows so net worth never double-counts.
- **Import** — QIF, OFX/QFX, CSV, and JSON all converge on one normalized
  record and a single insert path that deduplicates (exact by `FITID`, then a
  fuzzy amount/date/payee match) and collapses both legs of a transfer.
- **Import review** — downloaded rows are classified NEW or MATCHING and wait
  for you to accept them. Nothing reaches the register unreviewed.
- **Learning** — Mammon learns payee renaming, categories, and transfer targets
  from your corrections, so the tenth statement needs far less work than the
  first.
- **Investments** — holdings, cost basis, price history, and account valuation,
  with share quantities kept at `Decimal` precision.
- **Loans** — amortization with a full interest-rate history, and payments split
  into principal / interest / escrow.
- **Scheduled payments** — recurring bills pre-entered as placeholders that the
  real transaction merges into when it posts.
- **Reports** — spending by category, itemized income and expense, net worth
  over time, with charts.

## Install

Python 3.12 or newer. From a clone of the repository:

```
pip install -r requirements.txt        # PyQt5 and matplotlib: the app itself
```

Optional extras, installed as a package from the same clone:

```
pip install -e .[mcp,quotes,dev]       # the MCP server, quote download, the tests
```

An editable install (`pip install -e .`) also puts `mammon` and `mammon-mcp`
commands on your path; `python -m mammon.app` from the repository root works
without installing anything beyond the requirements.

## Running

```
python -m mammon.app
```

The database defaults to `data/mammon.db`, anchored to the install directory —
so the app opens the same file no matter which directory you launch it from.
Point it somewhere else with `--db`:

```
python -m mammon.app --db /path/to/ledger.db
python -m mammon.app --demo          # seed sample data into an empty database
```

## Ask an LLM about your finances (MCP server)

Mammon can serve the ledger to a language model over the Model Context
Protocol, read-only. Every tool returns aggregates first (income vs. expense,
cash flow, spending by category or payee, balances over time, holdings,
upcoming bills, loan terms), with a bounded transaction listing, free-text
search and a read-only SQL tool for the long tail. Account numbers and login
details are never returned.

```
python -m mammon.mcp_server                      # the app's default database, stdio
python -m mammon.mcp_server --db path\to\ledger.db
python -m mammon.mcp_server --transport streamable-http --port 8765
```

Requires the `mcp` package (`pip install mcp`); the application itself does
not need it. Point any MCP-capable client at that command. For a setup where
no row leaves your machine, use a client that hosts a **local model** (for
example LM Studio or Open WebUI) and give it this server; a hosted model works
the same way and is your choice. The server refuses a database whose schema
is older than the code (open it in Mammon once to migrate) or newer.

## Tests

```
python -m pytest mammon/tests -q
```

Qt tests run headless (`QT_QPA_PLATFORM=offscreen`), so no display is needed.
A handful of acceptance tests run only when a real ledger is present; point
`$MAMMON_ACCEPTANCE_DB` at one to include them, otherwise they skip.

## Design notes

Money is stored as signed integer cents — never floats. Share quantities and
per-share prices are `Decimal`-precision text. The domain layer
(`mammon/ledger.py`) is the only writer of transaction rows and holds no Qt, so
the invariants that matter are enforced in one place and tested headless. See
`docs/SRD.md` for the full requirements and data model.

## Status

Working and in daily use, but young. The schema is versioned and migrated
forward automatically (`PRAGMA user_version`); back up your database before
upgrading.

## License

GPL-3.0. See [LICENSE](LICENSE).
