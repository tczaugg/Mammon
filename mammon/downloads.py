"""Automated institution downloads that feed the import framework (SRD 7).

webSlinger INITIATES every download, so the user never has to log in and click
Export by hand. Per institution a recorded webSlinger script does ONE of two
things and hands the result to :mod:`mammon.importers`:

* EXPORT mode -- drive the site's own export button to download a QFX/OFX/CSV
  file, which Mammon then imports. Preferred when the export covers a useful date
  range.
* SCRAPE mode -- scrape the on-screen transaction list to canonical JSON
  (SRD 6.4) when the site's export is range-limited or absent (the fallback for
  any site whose own export cannot cover the range we need).

This module is the Mammon-side glue: an orchestrator that (1) runs the script
named on the ACCOUNT (``accounts.download_script``, with the inputs saved in
``download_config``) through an INJECTED runner -- so
Mammon stays decoupled from the webSlinger transport and the whole flow is
unit-testable without a browser -- (2) feeds the download to the importer, and
(3) reconciles the resulting account balance against the statement's OWN
reported balance.

The runner is any ``Callable[[str, dict], RunOutput]``; in production it wraps
the webSlinger MCP ``run_script`` tool (EXPORT scripts drop a file, SCRAPE
scripts return ``output_data`` records). Nothing here imports webSlinger, so a
test passes a fake runner that returns a fixture file or fixture records.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from mammon import download_log, importers, ledger
from mammon.importers.jsonimp import parse_json
from mammon.importers.record import (
    ImportResult,
    NormalizedTxn,
    dollars_to_cents,
    parse_date,
)

# ---------------------------------------------------------------------------
# Run outcome labels
# ---------------------------------------------------------------------------
# What a webSlinger run produced. There is deliberately NO central institution
# registry: each account names its own recorded script (``download_script``)
# and carries that script's saved inputs (``download_config``), and the branch
# taken below is decided by what the run ACTUALLY returned, never by a table.
EXPORT = "export"   # the site's own export button dropped a file
SCRAPE = "scrape"   # the script returned on-screen rows as records


class NoScriptError(RuntimeError):
    """Raised when the account has no webSlinger script configured -- name one
    in Account Details -> Download before downloading."""


class DownloadFailedError(RuntimeError):
    """The webSlinger run returned nothing to import -- no downloaded file AND no
    scraped records. Usually the automation did not complete: webSlinger signs in
    automatically using the credentials in the user's own browser session
    (managed by keyCocoon), so a stale/expired keyCocoon auth session is the most
    common cause. Surfaced to the UI so a failed run reads as a failure, not a
    silent "0 added" (SRD: a gated/failed Download is never silently nothing)."""


def _require_output(out: "RunOutput", script_name: str) -> None:
    """Guard against a degenerate run: one that produced neither a file nor any
    records (both ``None``) never fetched a statement. Distinguished from a
    genuinely empty result -- an EXPORT with zero transactions still yields a
    ``file_path``, and an empty SCRAPE yields ``records == []`` (an explicit
    empty list), neither of which trips this check."""
    if out.file_path is None and out.records is None:
        raise DownloadFailedError(
            f"webSlinger run for {script_name!r} returned no file and no records "
            "-- the automation did not complete. webSlinger signs in "
            "automatically using the credentials in your browser session "
            "(managed by keyCocoon); if this keeps happening, re-authenticate "
            "your keyCocoon session. Nothing was imported.")


# ---------------------------------------------------------------------------
# Runner contract
# ---------------------------------------------------------------------------
@dataclass
class RunOutput:
    """What a webSlinger run hands back to Mammon.

    EXPORT scripts download a file (``file_path``); SCRAPE scripts return
    extracted rows (``records``, canonical SRD-6.4 dicts). A production runner
    wraps the webSlinger ``run_script`` MCP tool and fills whichever applies; a
    test supplies either directly.
    """

    file_path: Optional[str] = None
    records: Optional[list] = None
    raw: Optional[dict] = None       # the runner's untouched result, for logging

    @property
    def is_file(self) -> bool:
        return bool(self.file_path)


Runner = Callable[[str, dict], RunOutput]


@dataclass
class DownloadResult:
    """Outcome of one institution download + import + reconcile pass."""

    institution: str
    mode: str
    import_result: Optional[ImportResult] = None
    account_id: Optional[int] = None
    account_name: Optional[str] = None
    ledger_balance: Optional[int] = None      # Mammon's computed balance (cents)
    statement_balance: Optional[int] = None   # the download's own reported balance
    as_of: Optional[str] = None
    reconciled: Optional[bool] = None         # None = no statement balance to check
    file_path: Optional[str] = None

    def summary(self) -> str:
        imp = self.import_result.summary() if self.import_result else "no import"
        if self.reconciled is None:
            rec = "reconcile: n/a"
        elif self.reconciled:
            rec = f"reconcile: OK ({_fmt(self.ledger_balance)} == statement)"
        else:
            rec = (f"reconcile: MISMATCH (ledger {_fmt(self.ledger_balance)} "
                   f"!= statement {_fmt(self.statement_balance)})")
        return f"{self.institution} [{self.mode}]: {imp}; {rec}"


@dataclass
class ReviewDownload:
    """What a *review-first* download produced, before anything is imported.

    A single webSlinger EXPORT run can drop MULTIPLE files (e.g. checking +
    savings + a card), so the EXPORT shapes are LISTS:

    * SCRAPE -- the run returned records; ``rows`` holds the RAW webSlinger
      flat-dict rows (``import_review.build_review`` maps them) and NOTHING has
      been written. The UI classifies + reviews them; only accept/save commits.
    * EXPORT, single-account files -- the run drove the site's own export button
      and dropped one or more single-account files (cash OR investment). NOTHING
      has been written: each path is listed in ``file_reviews`` so the UI routes
      EVERY one through the SAME import review queue a scraped batch uses (user:
      file data and download data must both land in review).
    * EXPORT, multi-account files -- a dropped file that is a multi-account QIF
      (both transfer legs / a securities master) is the ONE shape imported
      directly; each such import's :class:`DownloadResult` is listed in
      ``import_results``.

    A run may yield a MIX (several single-account files plus a multi-account
    QIF); ``file_reviews`` and ``import_results`` each carry their share. The
    singular ``file_path`` / ``import_result`` / ``needs_file_review`` accessors
    are kept as back-compat views of the FIRST entry.
    """

    rows: list = None
    mode: Optional[str] = None
    file_reviews: list = None      # single-account files -> import review queue
    import_results: list = None    # DownloadResults from DIRECT (multi-account) imports
    provider: str = "webSlinger"

    def __post_init__(self):
        if self.rows is None:
            self.rows = []
        if self.file_reviews is None:
            self.file_reviews = []
        if self.import_results is None:
            self.import_results = []

    @property
    def needs_review(self) -> bool:
        """A row-by-row SCRAPE review is pending (raw rows to classify)."""
        return bool(self.rows)

    @property
    def needs_file_review(self) -> bool:
        """One or more single-account EXPORT files await import review."""
        return bool(self.file_reviews)

    @property
    def file_path(self) -> Optional[str]:
        """Back-compat: the FIRST single-account file to review (``None`` if
        there are none). New callers should iterate ``file_reviews``."""
        return self.file_reviews[0] if self.file_reviews else None

    @property
    def import_result(self) -> Optional["DownloadResult"]:
        """Back-compat: the FIRST direct-import result (``None`` if no
        multi-account file was dropped). New callers iterate ``import_results``."""
        return self.import_results[0] if self.import_results else None


def _fmt(cents: Optional[int]) -> str:
    return "?" if cents is None else f"{cents / 100:.2f}"


# ---------------------------------------------------------------------------
# Orchestration seams (runner-callable). The UI's Download action uses
# download_rows_for_review below; these two are the direct-import cousins that
# the fixture tests drive.
# ---------------------------------------------------------------------------
def run_and_import(
    conn,
    runner: Runner,
    script: str,
    inputs: Optional[dict] = None,
    *,
    account: Optional[str] = None,
    account_type: Optional[str] = None,
    provider: Optional[str] = None,
) -> DownloadResult:
    """Run a webSlinger ``script`` via ``runner`` and import whatever it returns.

    ``account`` names the Mammon account the data belongs to (the import account
    and, for reconciliation, the balance that is checked); ``account_type`` is
    used only when the import has to create it. Records take the SCRAPE branch;
    a dropped file takes the EXPORT branch, where an OFX/QFX file's own
    ``<LEDGERBAL>`` is reconciled against Mammon's balance.
    """
    out = runner(script, dict(inputs or {}))
    _require_output(out, script)
    provider = provider or account or "webSlinger"
    if not out.is_file:
        records = _normalize_scraped(out.records or [], account)
        result = importers.import_records(
            conn, records, provider=provider,
            default_account=account, default_account_type=account_type,
        )
        return _finish_named(conn, provider, SCRAPE, result, account,
                             statement=None, path=None)
    return import_download_file(conn, out.file_path, account=account,
                                account_type=account_type, provider=provider)


def import_download_file(
    conn, path: str, *, account: Optional[str] = None,
    account_type: Optional[str] = None, provider: Optional[str] = None,
) -> DownloadResult:
    """Import an ALREADY-downloaded file (EXPORT mode) and reconcile it -- the
    path taken after a run has dropped a file, and the seam that lets the
    download flow be tested against a fixture file."""
    provider = provider or account or "webSlinger"
    result = importers.import_file(
        conn, path, provider=provider, account=account, account_type=account_type,
    )
    statement = None
    if str(path).lower().endswith((".ofx", ".qfx")):
        try:
            statement = ofx_reported_balance(_read_text(path))
        except OSError:
            statement = None
    return _finish_named(conn, provider, EXPORT, result, account,
                         statement=statement, path=path)


def default_downloads_dir(home: Optional[str] = None) -> str:
    """The current user's browser download folder (``~/Downloads``). Resolving
    the home directory this way yields ``C:\\Users\\<username>\\Downloads`` on
    Windows and the platform equivalent elsewhere."""
    return os.path.join(home or os.path.expanduser("~"), "Downloads")


# ---------------------------------------------------------------------------
# Download date-range helpers (pure: UI-independent so they are unit-testable)
# ---------------------------------------------------------------------------
_START_WORDS = ("start", "begin", "from", "since")
_END_WORDS = ("end", "until", "through", "thru")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y",
                 "%d/%m/%Y", "%Y/%m/%d")


def classify_date_inputs(schema):
    """Return ``(start_input, end_input)`` -- the ``ScriptInput``s carrying the
    download's start and end dates (or ``None`` for either the script omits).

    A date input is recognised via ``ScriptInput.is_date``; start vs end is
    decided by name/description keywords, falling back to declared order for any
    date inputs the keywords did not classify."""
    dates = [i for i in schema.inputs if i.is_date]
    start = end = None
    for inp in dates:
        blob = f"{inp.name} {inp.description}".lower()
        if start is None and any(w in blob for w in _START_WORDS):
            start = inp
        elif end is None and any(w in blob for w in _END_WORDS):
            end = inp
    remaining = [i for i in dates if i is not start and i is not end]
    if start is None and remaining:
        start = remaining.pop(0)
    if end is None and remaining:
        end = remaining.pop(0)
    return start, end


def date_format_from_example(example: Optional[str]) -> str:
    """Guess the ``strftime`` format from a sample date so the value Mammon sends
    matches what the script expects. Defaults to ISO (``%Y-%m-%d``)."""
    s = (example or "").strip()
    if s:
        for fmt in _DATE_FORMATS:
            try:
                datetime.datetime.strptime(s, fmt)
                return fmt
            except ValueError:
                continue
    return "%Y-%m-%d"


def _parse_config_date(value) -> Optional[datetime.date]:
    """Parse a stored date string (any known format) into a ``date`` or None."""
    if not value or not isinstance(value, str):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def default_download_dates(config, today=None, *, start_name=None,
                           end_name=None, default_span_days=30):
    """``(start_date, end_date)`` to prefill the Download prompt.

    ``end`` defaults to today; ``start`` defaults to the day AFTER the last
    download's saved end date (so consecutive downloads neither overlap nor gap),
    or ``today - default_span_days`` when there is no saved end. The last end is
    read from the canonical ``_last_end`` key first, then the script's own end
    input value."""
    config = config or {}
    today = today or datetime.date.today()
    saved_end = _parse_config_date(config.get("_last_end"))
    if saved_end is None and end_name:
        saved_end = _parse_config_date(config.get(end_name))
    if saved_end is not None:
        start = saved_end + datetime.timedelta(days=1)
    else:
        start = today - datetime.timedelta(days=default_span_days)
    if start > today:
        start = today
    return start, today


def build_download_input_data(schema, config, start_date, end_date):
    """Merge the account's saved ``config`` with the chosen date range into the
    ``input_data`` dict to run. Non-date inputs come from config (falling back to
    the schema template); the start/end date inputs are formatted to match the
    script's example format. The internal ``_last_end`` bookkeeping key is never
    sent to the script."""
    start_inp, end_inp = classify_date_inputs(schema)
    data = {k: v for k, v in (config or {}).items() if k != "_last_end"}
    for inp in schema.inputs:
        if inp.name not in data:
            tmpl = schema.input_template.get(inp.name)
            if tmpl not in (None, ""):
                data[inp.name] = tmpl
    if start_inp is not None:
        fmt = date_format_from_example(
            schema.input_template.get(start_inp.name) or start_inp.example)
        data[start_inp.name] = start_date.strftime(fmt)
    if end_inp is not None:
        fmt = date_format_from_example(
            schema.input_template.get(end_inp.name) or end_inp.example)
        data[end_inp.name] = end_date.strftime(fmt)
    return data


def remember_download_dates(config, schema, start_date, end_date):
    """Return an updated ``config`` dict persisting the chosen dates so the NEXT
    Download defaults start = this end + 1 day. Stores the dates under the
    script's own input names AND a canonical ISO ``_last_end`` watermark."""
    start_inp, end_inp = classify_date_inputs(schema)
    cfg = dict(config or {})
    if start_inp is not None:
        fmt = date_format_from_example(
            schema.input_template.get(start_inp.name) or start_inp.example)
        cfg[start_inp.name] = start_date.strftime(fmt)
    if end_inp is not None:
        fmt = date_format_from_example(
            schema.input_template.get(end_inp.name) or end_inp.example)
        cfg[end_inp.name] = end_date.strftime(fmt)
    cfg["_last_end"] = end_date.strftime("%Y-%m-%d")
    return cfg


def download_account(
    conn,
    client,
    script: str,
    input_data: Optional[dict] = None,
    *,
    account: Optional[str] = None,
    account_type: Optional[str] = None,
    provider: Optional[str] = None,
    downloads_dir: Optional[str] = None,
    now: Optional[float] = None,
    account_number: Optional[str] = None,
) -> DownloadResult:
    """Run an account's stored webSlinger ``script`` with its saved ``input_data``
    and import whatever it produces INTO that account.

    ``client`` is any object exposing ``describe_script(name)`` and
    ``run_script(name, inputs)`` (the webSlinger client, or a test fake) -- this
    module deliberately does NOT import :mod:`mammon.webslinger`, so it stays
    decoupled from the MCP transport and unit-testable without a browser. Mammon
    handles NO credentials on this path: the webSlinger MCP server logs into the
    site with the credentials living in the user's own browser.

    Two branches, decided by what the run ACTUALLY returned:

    * RETURNED DATA -- the run's result carries records; import them directly.
    * NO DATA -- assume the automation drove the site's own Export button, which
      dropped one or more files in the browser's download folder. Scan
      ``downloads_dir`` (default ``~/Downloads``) for EVERY ``.ofx/.qfx/.csv/.qif``
      modified AFTER the run STARTED (so a stale earlier download is never
      re-imported) and import them all, summing the counts. If none is found,
      raise :class:`DownloadFailedError`.

    ``describe_script`` is queried up front (the same mechanism Account Details
    uses to populate inputData) so Mammon knows the script's declared outputData
    shape before running it.
    """
    if not (script and str(script).strip()):
        raise NoScriptError(
            "no download script is configured for this account; set one in "
            "Account Details -> Download before downloading")
    # Query the outputData shape up front (spec: know the returned-data shape).
    client.describe_script(script)
    provider = provider or account or "webSlinger"

    started = float(now) if now is not None else time.time()
    sent_input = dict(input_data or {})
    # Everything below is wrapped so that EVERY outcome -- success, success with
    # MCP failed actions, or a raised failure -- is recorded to the download log
    # (mammon.download_log). This records the outcome ONLY; it does not decide it
    # (task 51582452 owns the success logic).
    result = None
    mode = None
    path = None
    imp = None
    dr = None
    log_items = []   # (path, ImportResult) pairs -> one download-log entry each
    try:
        result = client.run_script(script, sent_input)

        records = getattr(result, "records", None)
        if records:
            rows = _normalize_scraped(records, account)
            imp = importers.import_records(
                conn, rows, provider=provider,
                default_account=account,
                default_account_type=account_type or "checking",
            )
            mode = SCRAPE
            dr = _finish_named(conn, provider, SCRAPE, imp, account,
                               statement=None, path=None)
            log_items.append((None, imp))
        else:
            # No returned data -> assume a browser export. A single run can drop
            # SEVERAL files (e.g. checking + savings + a card), so import EVERY
            # new file that appeared AFTER the run started, not just the newest.
            ddir = downloads_dir or default_downloads_dir()
            paths = new_downloads(ddir, since=started)
            if not paths:
                msg = (
                    f"webSlinger run for {script!r} returned no data and no new "
                    f".ofx/.qfx/.csv/.qif file appeared in {ddir} after the run "
                    "started -- the automation may not have completed. Nothing "
                    "was imported.")
                # The keyCocoon re-auth advice is a red herring when login/MFA/
                # extractions all succeeded; only surface it when the run
                # actually reported an authentication failure.
                if _auth_failed(result):
                    msg += (
                        " The run looks like it failed to authenticate; "
                        "webSlinger signs in using the credentials in your "
                        "browser session (managed by keyCocoon), so "
                        "re-authenticate your keyCocoon session and try again.")
                raise DownloadFailedError(msg)
            mode = EXPORT
            imps = []
            statement = None
            for path in paths:      # ``path`` tracks the current file so the
                                    # failure log points at the one that raised
                fimp = importers.import_file(
                    conn, path, provider=provider,
                    account=account, account_type=account_type,
                )
                imps.append(fimp)
                log_items.append((path, fimp))
                # Reconcile against the last statement-bearing file (an OFX/QFX);
                # a run of several files reconciles once, on the one with balances.
                if str(path).lower().endswith((".ofx", ".qfx")):
                    try:
                        statement = ofx_reported_balance(_read_text(path))
                    except OSError:
                        pass
            imp = _merge_import_results(imps)
            # One aggregate DownloadResult with summed counts. ``file_path`` names
            # the single file, or every file joined when the run dropped several.
            path = paths[0] if len(paths) == 1 else ", ".join(paths)
            dr = _finish_named(conn, provider, EXPORT, imp, account,
                               statement=statement, path=path)
    except Exception as exc:
        _log_download_outcome(
            conn=conn, script=script, account=account,
            account_number=account_number, input_data=sent_input,
            result=result, success=False, mode=mode, path=path, imp=None,
            error=str(exc), started=started)
        raise
    # One log entry per file (or the single records/SCRAPE entry), so a multi-
    # file run is diagnosable file-by-file.
    for lpath, limp in log_items:
        _log_download_outcome(
            conn=conn, script=script, account=account,
            account_number=account_number, input_data=sent_input, result=result,
            success=True, mode=mode, path=lpath, imp=limp, error=None,
            started=started)
    return dr


def download_rows_for_review(
    conn,
    client,
    script: str,
    input_data: Optional[dict] = None,
    *,
    account: Optional[str] = None,
    account_type: Optional[str] = None,
    provider: Optional[str] = None,
    downloads_dir: Optional[str] = None,
    now: Optional[float] = None,
    account_number: Optional[str] = None,
) -> ReviewDownload:
    """Run an account's stored webSlinger ``script`` and return its result for
    IMPORT REVIEW instead of writing straight to the register.

    Same run mechanics as :func:`download_account` (describe up front, run, log
    every outcome), but NOTHING is written straight to the register:

    * SCRAPE (the run returned records) -- the raw rows are handed back
      UN-imported for :mod:`mammon.import_review` to classify.
    * EXPORT (the run drove the site's own export button and dropped one or MORE
      files) -- EACH single-account file (cash OR investment) is handed back for
      review (``file_reviews``), exactly like a scraped batch; only MULTI-account
      files (a multi-account QIF, which fans out across accounts) are imported
      directly here (``import_results``). This is what closes the investment /
      webSlinger EXPORT bypass: a broker's exported .qfx/.csv now lands in
      review, not the register.
    """
    if not (script and str(script).strip()):
        raise NoScriptError(
            "no download script is configured for this account; set one in "
            "Account Details -> Download before downloading")
    client.describe_script(script)
    provider = provider or account or "webSlinger"

    started = float(now) if now is not None else time.time()
    sent_input = dict(input_data or {})
    result = None
    mode = None
    path = None
    out = None
    log_items = []   # (path, ImportResult|None) pairs -> one log entry each
    try:
        result = client.run_script(script, sent_input)
        records = getattr(result, "records", None)
        if records:
            # Hand the RAW webSlinger rows back for review; do NOT import.
            mode = SCRAPE
            out = ReviewDownload(rows=list(records), mode=SCRAPE, provider=provider)
            log_items.append((None, None))
        else:
            # A single EXPORT run can drop SEVERAL files; route EVERY new one that
            # appeared after the run started (single-account -> review queue,
            # multi-account QIF -> direct import), not just the newest.
            ddir = downloads_dir or default_downloads_dir()
            paths = new_downloads(ddir, since=started)
            if not paths:
                msg = (
                    f"webSlinger run for {script!r} returned no data and no new "
                    f".ofx/.qfx/.csv/.qif file appeared in {ddir} after the run "
                    "started -- the automation may not have completed. Nothing "
                    "was imported.")
                if _auth_failed(result):
                    msg += (
                        " The run looks like it failed to authenticate; "
                        "webSlinger signs in using the credentials in your "
                        "browser session (managed by keyCocoon), so "
                        "re-authenticate your keyCocoon session and try again.")
                raise DownloadFailedError(msg)
            mode = EXPORT
            file_reviews = []
            import_results = []
            for path in paths:      # ``path`` tracks the current file for the log
                if importers.multi_account_file(path, account=account):
                    # Multi-account QIF (both transfer legs / a securities
                    # master): the ONE shape that imports directly, since it fans
                    # out across accounts. Everything else goes to review.
                    imp = importers.import_file(
                        conn, path, provider=provider,
                        account=account, account_type=account_type,
                    )
                    statement = None
                    if str(path).lower().endswith((".ofx", ".qfx")):
                        try:
                            statement = ofx_reported_balance(_read_text(path))
                        except OSError:
                            statement = None
                    dr = _finish_named(conn, provider, EXPORT, imp, account,
                                       statement=statement, path=path)
                    import_results.append(dr)
                    log_items.append((path, imp))
                else:
                    # Single-account file (cash OR investment): hand it back for
                    # the import review queue, like a scrape. NOTHING is written
                    # here -- logged with imp=None like the SCRAPE branch.
                    file_reviews.append(path)
                    log_items.append((path, None))
            out = ReviewDownload(rows=[], mode=EXPORT, file_reviews=file_reviews,
                                 import_results=import_results, provider=provider)
    except Exception as exc:
        _log_download_outcome(
            conn=conn, script=script, account=account,
            account_number=account_number, input_data=sent_input,
            result=result, success=False, mode=mode, path=path, imp=None,
            error=str(exc), started=started)
        raise
    # One log entry per file (or the single SCRAPE entry), so a multi-file run is
    # diagnosable file-by-file.
    for lpath, limp in log_items:
        _log_download_outcome(
            conn=conn, script=script, account=account,
            account_number=account_number, input_data=sent_input, result=result,
            success=True, mode=mode, path=lpath, imp=limp, error=None,
            started=started)
    return out


_AUTH_MARKERS = ("auth", "login", "log in", "sign in", "sign-in", "signin",
                 "mfa", "2fa", "credential", "password", "unauthorized",
                 "not logged in", "session expired")


def _auth_failed(result) -> bool:
    """True only when the run itself signalled an AUTHENTICATION failure.

    Used to gate the keyCocoon re-auth advice: when login/MFA/extractions all
    succeeded (data merely didn't come back as a file), that advice is a red
    herring, so it is suppressed. Scans the run's status/error text and raw
    payload for auth markers; a run with no such signal returns False."""
    if result is None:
        return False
    parts = [str(getattr(result, "status", "") or ""),
             str(getattr(result, "error", "") or "")]
    raw = getattr(result, "raw", None)
    if isinstance(raw, dict):
        try:
            parts.append(json.dumps(raw, default=str))
        except (TypeError, ValueError):
            pass
    text = " ".join(parts).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def _log_download_outcome(*, conn, script, account, account_number, input_data,
                          result, success, mode, path, imp, error, started):
    """Record ONE download attempt's full outcome to the download log.

    Deliberately captures BOTH the raw MCP run summary/error AND Mammon's final
    decision, so a run that "returned good data but reported an error" is
    diagnosable. Never raises -- logging must not break a download."""
    try:
        mammon_wait = round(max(0.0, time.time() - started), 3)
    except Exception:
        mammon_wait = None
    # Prefer the RUN's own reported start/duration (UTC) over Mammon's local
    # numbers; keep Mammon's own wall-clock wait as a separate, labelled field.
    run_started = getattr(result, "started_at", None)
    run_duration = getattr(result, "duration_seconds", None)
    # The account stores its input_data pre-coercion; the client records the
    # actual post-coercion payload it sent to MCP as result.sent_input -- log
    # that when present so subAccountName shows the real ['Share Savings']
    # array, not the "['Share Savings']" string.
    sent = getattr(result, "sent_input", None) or input_data
    if success:
        if mode == SCRAPE:
            source = "records"
            reason = f"run returned records; imported directly into {account!r}"
        else:
            source = "downloads_file"
            reason = ("run returned no records; imported Downloads file "
                      f"{path!r}")
        failed = getattr(result, "failed_actions", None)
        if failed:
            reason += (f" (SUCCESS despite {failed} failed MCP action(s) -- "
                       "Mammon judged by returned data, not error count)")
    else:
        source = None
        reason = f"download failed: {error}"
    entry = {
        "ts": run_started or time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "account": account,
        "account_number": account_number,
        "script": script,
        "input_data": sent,
        "mcp": {
            "status": getattr(result, "status", None),
            "success_flag": getattr(result, "success", None),
            "failed_actions": getattr(result, "failed_actions", None),
            "raw_error": getattr(result, "error", None),
            "run_started_utc": run_started,
            "run_duration_seconds": run_duration,
            "mammon_wait_seconds": mammon_wait,
        },
        "decision": "success" if success else "failure",
        "reason": reason,
        "source": source,
        "file_path": path,
        "imported": getattr(imp, "added", None),
        "duplicates": getattr(imp, "duplicates", None),
        "import_errors": getattr(imp, "errors", None),
        "error_text": error,
    }
    try:
        log_path = download_log.default_log_path(
            download_log.db_path_from_conn(conn))
        download_log.log_attempt(entry, log_path=log_path)
    except Exception:
        pass


def _normalize_scraped(rows: list, account: Optional[str]) -> list[NormalizedTxn]:
    """Turn webSlinger's scraped rows into NormalizedTxn. Dict rows are routed
    through the JSON importer (the canonical SRD-6.4 mapping); NormalizedTxn rows
    pass through untouched."""
    if not rows:
        return []
    if isinstance(rows[0], NormalizedTxn):
        return list(rows)
    return parse_json(json.dumps(rows), default_account=account)


def _finish_named(conn, institution, mode, result, account, statement,
                  path) -> DownloadResult:
    """Assemble a :class:`DownloadResult` with the account balance and (for an
    OFX/QFX statement) the reconcile check. ``institution`` is the provider
    label recorded on the import (the account name unless the caller supplied
    one)."""
    dr = DownloadResult(
        institution=institution, mode=mode,
        import_result=result, account_name=account, file_path=path,
    )
    acct = ledger.get_account_by_name(conn, account) if account else None
    if acct is not None:
        dr.account_id = acct["id"]
        if statement is not None:
            bal_cents, as_of = statement
            dr.statement_balance = bal_cents
            dr.as_of = as_of
            dr.ledger_balance = ledger.account_balance(conn, acct["id"], as_of)
            dr.reconciled = dr.ledger_balance == bal_cents
        else:
            dr.ledger_balance = ledger.account_balance(conn, acct["id"])
    return dr


# ---------------------------------------------------------------------------
# Reconciliation helpers
# ---------------------------------------------------------------------------
_LEDGERBAL = re.compile(r"<LEDGERBAL>(.*?)</LEDGERBAL>", re.I | re.S)
_BALAMT = re.compile(r"<BALAMT>\s*([^<\r\n]+)", re.I)
_DTASOF = re.compile(r"<DTASOF>\s*([^<\r\n]+)", re.I)


def ofx_reported_balance(text: str) -> Optional[tuple[int, Optional[str]]]:
    """Extract an OFX/QFX statement's reported ledger balance as ``(cents,
    as_of_iso)``, or ``None`` if the file carries no ``<LEDGERBAL>``.

    Reconciliation semantics: Mammon's balance for the account AS OF ``as_of``
    should equal this figure, which holds once the account already contains its
    history up to the statement date (the ongoing-download steady state). A
    first import into an empty account reconciles only if the download spans the
    account's whole life or the opening balance is set to bridge the gap.
    """
    block = _LEDGERBAL.search(text)
    scope = block.group(1) if block else text
    amt = _BALAMT.search(scope)
    if not amt:
        return None
    cents = dollars_to_cents(amt.group(1).strip())
    dt = _DTASOF.search(scope)
    as_of = None
    if dt:
        try:
            as_of = parse_date(dt.group(1).strip())
        except ValueError:
            as_of = None
    return cents, as_of


def reconcile_account(
    conn, account_id: int, statement_cents: int, as_of: Optional[str] = None,
) -> bool:
    """True when Mammon's balance for the account (as of ``as_of``) equals the
    statement's reported balance."""
    return ledger.account_balance(conn, account_id, as_of) == statement_cents


# ---------------------------------------------------------------------------
# Live-run helpers (used by the webSlinger-backed runner in the app)
# ---------------------------------------------------------------------------
def newest_download(
    downloads_dir: str, extensions=(".ofx", ".qfx", ".csv"), since: Optional[float] = None,
) -> Optional[str]:
    """The most recently modified file in ``downloads_dir`` matching one of
    ``extensions`` (and newer than ``since`` epoch seconds, if given). Used to
    locate the file a webSlinger EXPORT run just dropped in the browser's
    download folder."""
    best: Optional[str] = None
    best_mtime = -1.0
    for ext in extensions:
        for path in glob.glob(os.path.join(downloads_dir, f"*{ext}")):
            mtime = os.path.getmtime(path)
            if since is not None and mtime < since:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
    return best


def new_downloads(
    downloads_dir: str,
    since: Optional[float] = None,
    extensions=(".ofx", ".qfx", ".csv", ".qif"),
) -> list[str]:
    """EVERY file in ``downloads_dir`` matching one of ``extensions`` and modified
    at/after ``since`` epoch seconds (all matches if ``since`` is None), sorted
    oldest->newest (ties broken by path) for a deterministic import order.

    A webSlinger EXPORT run can drop MULTIPLE files in one go (e.g. checking +
    savings + a card, or several accounts), so Mammon must pick up ALL of them,
    not just the newest -- otherwise the rest are silently left in Downloads.
    ``.qif`` is included because a dropped multi-account QIF is the one direct-
    import format, and :func:`newest_download` never scanned for it."""
    found: list[tuple[float, str]] = []
    seen: set[str] = set()
    for ext in extensions:
        for path in glob.glob(os.path.join(downloads_dir, f"*{ext}")):
            if path in seen:        # a file can match only one extension, but a
                continue            # duplicate glob hit must not double-count
            seen.add(path)
            mtime = os.path.getmtime(path)
            if since is not None and mtime < since:
                continue
            found.append((mtime, path))
    found.sort(key=lambda t: (t[0], t[1]))
    return [p for _, p in found]


def _merge_import_results(results: list) -> Optional[ImportResult]:
    """Sum a run's per-file :class:`ImportResult`s into one aggregate (added /
    duplicates / errors / transfers / investments / matched summed; the list
    fields concatenated). Returns the sole result unchanged for a 1-file run, or
    ``None`` for an empty list."""
    results = [r for r in results if r is not None]
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    merged = ImportResult()
    for r in results:
        merged.added += r.added or 0
        merged.duplicates += r.duplicates or 0
        merged.errors += r.errors or 0
        merged.transfers += r.transfers or 0
        merged.investments += r.investments or 0
        merged.matched += r.matched or 0
        merged.payment_changes.extend(r.payment_changes or [])
        merged.unmapped_actions.extend(r.unmapped_actions or [])
        merged.position_discrepancies.extend(r.position_discrepancies or [])
    return merged


def _read_text(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")
