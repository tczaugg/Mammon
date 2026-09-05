"""Persistent, append-mode log of every download attempt's FULL outcome.

Why this exists
---------------
A download run can return good data yet still surface an error (webSlinger
reports failed actions, or Mammon's newest-Downloads-file scan is ambiguous).
When that happens the user only ever saw a transient ``QMessageBox`` and the
outcome was lost. This module records, for every attempt, BOTH the raw MCP run
summary/error AND Mammon's final success/failure decision, so the
"good data but reported error" discrepancy is diagnosable after the fact.

Scope discipline
-----------------
This module ONLY records what :func:`mammon.downloads.download_account` already
decided -- it contains NO success/failure logic (task 51582452 owns that). Each
attempt is written as one JSON object per line (JSON Lines), which stays
human-readable AND trivially parseable by the in-app viewer.

The log lives next to the database at ``<data dir>/download.log`` and is size
capped: when it crosses ``MAX_BYTES`` the current file is rolled to
``download.log.1`` (one generation kept) so it can never grow without bound.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any, Optional

# ~1 MB per generation, one rolled backup kept (download.log + download.log.1).
MAX_BYTES = 1_000_000


def default_data_dir() -> str:
    """The directory the log lives in. Honours ``MAMMON_DATA_DIR`` (so tests and
    alternate installs can redirect it) and otherwise defaults to the live
    install's data folder next to ``mammon.db``."""
    env = os.environ.get("MAMMON_DATA_DIR")
    if env:
        return env
    # Derived from where the package actually lives -- NEVER a hardcoded
    # absolute path, which pointed at one developer's install and made any
    # other checkout (or a test run) write into it.
    return str(Path(__file__).resolve().parent.parent / "data")


def db_path_from_conn(conn) -> Optional[str]:
    """The on-disk path of a sqlite connection's ``main`` database, or ``None``
    for an in-memory / unknown connection. Lets the log sit NEXT TO the database
    it describes -- the live ``mammon.db`` in production, a tmp DB under tests --
    so a test run never writes to the live data dir."""
    try:
        for _seq, name, filename in conn.execute("PRAGMA database_list"):
            if name == "main" and filename:
                return filename
    except Exception:
        return None
    return None


def default_log_path(db_path: Optional[str] = None) -> str:
    """Resolved path of the download log. Resolution order:

    1. ``MAMMON_DOWNLOAD_LOG`` env var -- an outright override (tests point this at
       a tmp file).
    2. ``download.log`` next to ``db_path`` when one is supplied.
    3. ``download.log`` in :func:`default_data_dir` (the live install default)."""
    override = os.environ.get("MAMMON_DOWNLOAD_LOG")
    if override:
        return override
    if db_path:
        folder = os.path.dirname(os.path.abspath(db_path))
        if folder:
            return os.path.join(folder, "download.log")
    return os.path.join(default_data_dir(), "download.log")


def _rotate_if_needed(path: str, max_bytes: int) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) >= max_bytes:
            backup = path + ".1"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
            except OSError:
                pass
            os.replace(path, backup)
    except OSError:
        # Rotation is best-effort; never let it break a download.
        pass


def log_attempt(entry: dict, *, log_path: Optional[str] = None,
                max_bytes: int = MAX_BYTES) -> str:
    """Append one structured ``entry`` (a dict) to the download log as a single
    JSON line and return the path written. Best-effort: any I/O error is
    swallowed so logging can never break a download."""
    path = log_path or default_log_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _rotate_if_needed(path, max_bytes)
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    return path


def read_recent(n: int = 50, *, log_path: Optional[str] = None) -> list[dict]:
    """Return up to the last ``n`` parsed entries, oldest-first. Lines that fail
    to parse are returned as ``{"_raw": <text>}`` so nothing is hidden."""
    path = log_path or default_log_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append({"_raw": ln})
    return out


def last_error(*, log_path: Optional[str] = None) -> Optional[dict]:
    """The most recent entry whose decision was a failure, or ``None``."""
    for entry in reversed(read_recent(500, log_path=log_path)):
        if entry.get("decision") == "failure":
            return entry
    return None


def format_entry(entry: dict) -> str:
    """Render one entry as a readable multi-line block for the viewer."""
    if "_raw" in entry:
        return entry["_raw"]
    mcp = entry.get("mcp") or {}
    acct = entry.get("account") or "?"
    num = entry.get("account_number")
    acct_line = f"{acct}" + (f" (#{num})" if num else "")
    lines = [
        f"[{entry.get('ts', '?')}]  {str(entry.get('decision', '?')).upper()}"
        f"  -- {acct_line}",
        f"  script      : {entry.get('script')}",
        f"  inputData   : {json.dumps(entry.get('input_data'), default=str)}",
        f"  MCP run     : status={mcp.get('status')} success={mcp.get('success_flag')}"
        f" failedActions={mcp.get('failed_actions')} elapsed={mcp.get('elapsed_seconds')}s",
        f"  MCP error   : {mcp.get('raw_error')}",
        f"  reason      : {entry.get('reason')}",
        f"  source      : {entry.get('source')}"
        + (f"  file={entry.get('file_path')}" if entry.get('file_path') else ""),
        f"  imported    : added={entry.get('imported')}"
        f" duplicates={entry.get('duplicates')} errors={entry.get('import_errors')}",
    ]
    if entry.get("error_text"):
        lines.append(f"  error_text  : {entry.get('error_text')}")
    return "\n".join(lines)
