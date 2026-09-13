"""The DB-facing half of the holdings-snapshot import: read a snapshot CSV with
the pure parser, then hand the rows to :mod:`mammon.investments` to apply.

WHY this is two lines of glue and nothing more:

- **No second writer.** ``mammon/investments.py`` is the only writer of
  ``price_history`` and of the share-reconcile drafts, exactly as
  ``mammon/ledger.py`` is the only writer of transaction rows (CLAUDE.md). This
  module therefore runs no SQL at all: it parses, it calls
  :func:`mammon.investments.apply_holdings_snapshot`, and it reports.

- **A snapshot is not a transaction file.** Nothing here reaches
  ``importers/core.py``'s insert/dedup path, and nothing here can create a
  transaction. Re-importing the same statement is a no-op beyond re-stating the
  same price and the same reconcile target, because both are keyed by
  ``(symbol, date)`` / ``(account, symbol)`` -- there is no history to duplicate.

The value of a separate entry point is that the caller (the share reconcile
dialog, a script, a test) gets one call for "this file, this account" while the
parser stays a pure ``text -> records`` function that a test can exercise with
no database at all.
"""
from __future__ import annotations

from typing import Optional

from mammon import investments
from mammon.importers.holdings_csv import (
    looks_like_holdings_csv,
    parse_holdings_csv,
    parse_holdings_file,
)

__all__ = [
    "import_holdings_csv",
    "import_holdings_file",
    "looks_like_holdings_csv",
    "summarize_snapshot",
]


def import_holdings_csv(conn, account_id: int, text: str, *,
                        as_of: Optional[str] = None,
                        source: str = investments.SNAPSHOT_PRICE_SOURCE,
                        write_prices: bool = True,
                        seed_drafts: bool = True) -> list:
    """Parse ``text`` as a holdings snapshot and apply it to ``account_id``.

    Returns the per-line result dicts of
    :func:`mammon.investments.apply_holdings_snapshot` -- including the lines it
    could NOT match to a security, which are reported rather than guessed at."""
    records = parse_holdings_csv(text, as_of=as_of)
    return investments.apply_holdings_snapshot(
        conn, account_id, records, as_of=as_of, source=source,
        write_prices=write_prices, seed_drafts=seed_drafts)


def import_holdings_file(conn, account_id: int, path, *,
                         as_of: Optional[str] = None,
                         source: str = investments.SNAPSHOT_PRICE_SOURCE,
                         write_prices: bool = True,
                         seed_drafts: bool = True) -> list:
    """:func:`import_holdings_csv` over a file path (UTF-8, BOM tolerated)."""
    records = parse_holdings_file(path, as_of=as_of)
    return investments.apply_holdings_snapshot(
        conn, account_id, records, as_of=as_of, source=source,
        write_prices=write_prices, seed_drafts=seed_drafts)


def summarize_snapshot(results) -> dict:
    """A one-line tally for the UI: how many lines were applied, how many are
    unmatched, how many prices were recorded, and the statement date(s) seen."""
    matched = [r for r in results if r.get("matched_by")]
    unmatched = [r for r in results if not r.get("matched_by")]
    return {
        "lines": len(results),
        "matched": len(matched),
        "unmatched": len(unmatched),
        "unmatched_names": [r.get("name") or r.get("symbol") or ""
                            for r in unmatched],
        "prices_recorded": sum(1 for r in results if r.get("price_recorded")),
        "drafts_saved": sum(1 for r in results if r.get("draft_saved")),
        "dates": sorted({r.get("date", "") for r in results if r.get("date")}),
    }
