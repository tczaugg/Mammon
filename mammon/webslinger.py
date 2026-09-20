"""webSlinger MCP client for Mammon's automated Download feature.

Mammon never drives a browser itself: a recorded **webSlinger** automation does,
and Mammon talks to it through the webSlinger **MCP server** -- ``describe_script``
to learn a script's inputs, ``run_script`` to execute it. This module wraps that
server behind a tiny client so:

  * the Account-details *Download* section can render a script's input fields
    DYNAMICALLY from ``describe_script`` (the fields vary per institution -- a
    date range, an export file format, an account number/username, and sometimes
    a bank TOTP the user set up for MFA), and
  * the per-account *Download* action can run the script and hand the resulting
    file to the existing import pipeline (see ``downloads.run_and_import``).

The concrete :class:`McpWebSlingerClient` speaks newline-delimited JSON-RPC over
stdio to the MCP server named by ``$MAMMON_WEBSLINGER_MCP_CMD`` (or an explicit
command). When nothing is configured it reports ``available() is False`` so the
UI disables Download with a clear reason -- being unconfigured is never an error.

Tests use :class:`FakeWebSlingerClient`, whose canned schemas/results are shaped
exactly like the live server's -- see the ``describe_script`` payload for the
Anytown CU checking script ``GetCheckingTransactionsForRange`` (renamed
2026-08-08 from the older ``downloadCheckingForRange``/``DownloadBanking...``):
``inputs`` is a list of ``{name, description, example, type}`` and
``input_template`` a name->example dict, which is precisely what
:meth:`ScriptSchema.from_mcp` consumes.

The MCP launch command (``$MAMMON_WEBSLINGER_MCP_CMD``) is parsed by
:func:`_split_command`, which keeps Windows backslash paths intact -- e.g.
``python D:\\webslinger\\taskSpinner\\mcp_server.py``.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, NamedTuple, Optional

_log = logging.getLogger("mammon.webslinger")


class WebSlingerError(RuntimeError):
    """A webSlinger MCP call failed (transport error, or the server returned
    ``success: false``). The UI catches this and reports it; it is NOT raised
    merely because webSlinger is unconfigured -- that surfaces as
    ``available() is False`` instead."""


# ---------------------------------------------------------------------------
# Script schema (what describe_script reports)
# ---------------------------------------------------------------------------
_DATE_HINTS = ("date", "start", "end", "from", "to", "since", "until")
_SECRET_HINTS = ("totp", "otp", "mfa", "password", "passcode", "secret", "pin")


@dataclass(frozen=True)
class ScriptInput:
    """One declared input of a webSlinger script."""

    name: str
    description: str = ""
    example: str = ""
    type: str = ""

    @property
    def label(self) -> str:
        return self.name

    @property
    def is_date(self) -> bool:
        blob = f"{self.name} {self.description}".lower()
        return any(h in blob for h in _DATE_HINTS)

    @property
    def is_secret(self) -> bool:
        blob = f"{self.name} {self.description}".lower()
        return any(h in blob for h in _SECRET_HINTS)

    @property
    def is_array(self) -> bool:
        """True when the script declares this input as a list/array (e.g. the
        ``subAccountName`` set). Such a value MUST reach the MCP run as a real
        JSON array, never a stringified/repr'd one -- see :func:`_as_list`."""
        t = (self.type or "").lower()
        return "array" in t or "list" in t

    @property
    def hint(self) -> str:
        """A one-line tooltip: the description, then the example if any."""
        parts = []
        if self.description:
            parts.append(self.description)
        if self.example:
            parts.append(f"e.g. {self.example}")
        return " — ".join(parts)


@dataclass(frozen=True)
class ScriptSchema:
    """A script's callable shape, normalized from the MCP ``describe_script``
    payload. Drives the dynamic input form."""

    name: str
    description: str = ""
    target_website: str = ""
    inputs: tuple[ScriptInput, ...] = ()
    input_template: dict = field(default_factory=dict)
    outputs: tuple[str, ...] = ()

    @property
    def returns_data(self) -> bool:
        """True when the script DECLARES an outputData shape -- i.e. it hands
        records back through the MCP result rather than dropping a file in the
        browser's download folder. The Download flow uses this only as a hint;
        it still branches on what the run ACTUALLY returned."""
        return bool(self.outputs)

    @classmethod
    def from_mcp(cls, payload: dict) -> "ScriptSchema":
        """Build from a raw ``describe_script`` result dict. Tolerant of missing
        keys so a thin/older server payload still yields a usable schema."""
        raw_inputs = payload.get("inputs") or []
        inputs = tuple(
            ScriptInput(
                name=str(i.get("name", "")),
                description=str(i.get("description", "") or ""),
                example=str(i.get("example", "") or ""),
                type=str(i.get("type", "") or ""),
            )
            for i in raw_inputs
            if i.get("name")
        )
        template = payload.get("input_template")
        if not isinstance(template, dict):
            template = {i.name: i.example for i in inputs}
        # outputData shape: each entry may be a dict ({"name": ...}) or a bare
        # string. Keep just the field names so Mammon knows the returned shape.
        outputs = []
        for o in payload.get("outputs") or []:
            if isinstance(o, dict) and o.get("name"):
                outputs.append(str(o["name"]))
            elif isinstance(o, str) and o.strip():
                outputs.append(o.strip())
        return cls(
            name=str(payload.get("display_name")
                     or payload.get("script_name")
                     or payload.get("name", "")),
            description=str(payload.get("description", "") or ""),
            target_website=str(payload.get("target_website", "") or ""),
            inputs=inputs,
            input_template=dict(template),
            outputs=tuple(outputs),
        )


def _as_list(value) -> list:
    """Coerce a single webSlinger LIST-input value to a real list of strings,
    tolerating every form it reaches us in:

      * already a list -> its items, stringified and stripped;
      * JSON text ``["Checking","Savings"]`` -> parsed;
      * Python-repr text ``['Share Savings']`` (single quotes, which
        :func:`json.loads` rejects) -> recovered via ``ast.literal_eval``;
      * plain text ``Share Savings`` (or a newline/comma-separated set) -> split.

    An empty/blank value yields ``[]``."""
    if isinstance(value, list):
        return [str(x).strip() for x in value]
    s = str(value if value is not None else "").strip()
    if not s:
        return []
    if s[:1] == "[" and s[-1:] == "]":
        for loader in (json.loads, ast.literal_eval):
            try:
                parsed = loader(s)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed]
    parts: list = []
    for line in s.replace("\r", "\n").split("\n"):
        parts.extend(line.split(","))
    return [p.strip() for p in parts if p.strip()]


def _coerce_input_data(inputs, array_fields=None) -> dict:
    """Return a copy of ``inputs`` with list/object-valued fields sent as REAL
    JSON structures rather than stringified.

    webSlinger LIST inputs (e.g. a ``subAccountName`` array) are frequently
    captured from a plain text field as the literal text ``["Checking","Savings"]``.
    Left as a string they reach the MCP server as a JSON *string*, so the script's
    per-item loop iterates the characters (or does nothing) and only one/zero
    sub-accounts download. Any value whose text parses to a list/dict is upgraded
    to that structure; scalars -- including numeric-looking strings such as an
    account number -- are left untouched so they are not mangled.

    ``array_fields`` names the inputs the script DECLARES as arrays (from
    ``describe_script``). Those are ALWAYS coerced to a real list via
    :func:`_as_list` -- so a lone ``Share Savings`` becomes ``["Share Savings"]``
    and a Python-repr ``['Share Savings']`` (which ``json.loads`` cannot parse) is
    recovered -- rather than relying on the value already looking like JSON."""
    array_fields = set(array_fields or ())
    out = {}
    for key, value in dict(inputs or {}).items():
        if key in array_fields:
            out[key] = _as_list(value)
            continue
        if isinstance(value, (list, dict)):
            out[key] = value
            continue
        if isinstance(value, str):
            s = value.strip()
            if s[:1] in ("[", "{") and s[-1:] in ("]", "}"):
                try:
                    parsed = json.loads(s)
                except ValueError:
                    parsed = None
                if isinstance(parsed, (list, dict)):
                    out[key] = parsed
                    continue
        out[key] = value
    return out


def _extract_run_id(payload) -> Optional[str]:
    """Pull the run id an async ``start_run`` handed back, tolerating both
    snake/camel case and a nested ``output_data``."""
    if not isinstance(payload, dict):
        return None
    for key in ("run_id", "runId"):
        val = payload.get(key)
        if val:
            return str(val)
    out = payload.get("output_data")
    if isinstance(out, dict):
        for key in ("run_id", "runId"):
            val = out.get(key)
            if val:
                return str(val)
    return None


def _failed_action_count(payload) -> Optional[int]:
    """Best-effort count of actions that FAILED in a run summary or full report.

    Returns ``None`` when the payload carries no failure signal at all, so a thin
    summary that simply omits the field is never misread as "all actions passed".
    Understands an explicit ``failed_actions``/``failedActions`` (int or list),
    the same nested inside a ``summary``/``report``/``output_data`` container, or
    a full report's ``actions`` list (counting entries with ``success is False``
    or a failed/error ``status``)."""
    if not isinstance(payload, dict):
        return None
    for key in ("failed_actions", "failedActions"):
        val = payload.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            return val
        if isinstance(val, list):
            return len(val)
    for key in ("summary", "execution_summary", "executionSummary",
                "report", "output_data"):
        sub = payload.get(key)
        if isinstance(sub, dict):
            nested = _failed_action_count(sub)
            if nested is not None:
                return nested
    actions = payload.get("actions")
    if isinstance(actions, list) and actions:
        bad = 0
        for act in actions:
            if not isinstance(act, dict):
                continue
            if (act.get("success") is False
                    or str(act.get("status", "")).lower() in ("failed", "error")):
                bad += 1
        return bad
    return None


# Keys inside output_data that hold RUN METADATA, never transaction rows -- the
# gather-any-list fallback in ``_rows_from`` must skip them so it never mistakes
# an action/error list for records.
_NON_RECORD_KEYS = frozenset({
    "actions", "failed_actions", "failedactions", "errors", "error", "logs",
    "log", "validation", "validations", "warnings", "steps", "screenshots",
    "datastorage", "data_storage", "summary", "execution_summary",
})

# Field names that make a dict a TRANSACTION row, mirroring what
# ``import_review.map_row`` actually reads (``_map_date`` / ``_signed_cents`` /
# the description vocabulary). A blacklist of metadata KEY NAMES cannot cover
# this: a script's lookup table is named whatever that bank calls it, and the
# next bank names it something else. Testing the SHAPE of the rows does.
_ROW_DATE_KEYS = ("posteddate", "posted", "postdate", "date", "transactiondate",
                  "effectivedate")
_ROW_AMOUNT_KEYS = ("amount", "transactionamount", "amount_usd")
_ROW_TEXT_KEYS = ("statementdescription", "transactiondescription",
                  "description", "memo", "name")


def _looks_like_rows(vals: list) -> bool:
    """True when ``vals`` holds dicts that could be transactions.

    A script may return several arrays and only some of them are rows. The
    America First script declares "The subAccountList maps shortName to
    accountId", so its ``output_data`` carries a lookup table of
    ``{"id": 1234567, "shortName": "Checking"}`` beside the per-sub-account
    transaction arrays. Gathered blindly, those 11 lookup entries became 11
    review rows with no date, no amount and no text -- blank lines at the top of
    the user's review list, and a tell that the payload was being flattened
    rather than read. A dict with none of a date, an amount or a description is
    not a transaction, whatever array it came from.
    """
    for row in vals:
        if not isinstance(row, dict):
            continue
        keys = {str(k).lower() for k in row}
        if (keys.intersection(_ROW_DATE_KEYS)
                or keys.intersection(_ROW_AMOUNT_KEYS)
                or keys.intersection(_ROW_TEXT_KEYS)):
            return True
    return False


# Key names that hold a script's row array, searched at EVERY depth (see
# _rows_from): the one thing that survives the generator reorganizing
# output_data is that the rows still sit under a name like "transactions".
_ROW_ARRAY_KEYS = ("records", "transactions", "rows", "items", "results",
                   "result", "data", "output", "extractions")


def _rows_in_list(vals, *, wrappers_only: bool = False) -> Optional[list]:
    """Rows out of a LIST value: the dicts themselves when they ARE rows, else
    whatever each one holds ONE LEVEL DOWN.

    A list of dicts is one of two things, and only the shape tells them apart:
    the rows, or a list of per-account WRAPPERS that carry the rows inside
    (``{"accountName": "Checking", "transactions": [...]}``). Descending is what
    makes the reader indifferent to which the script's author chose -- see
    :func:`_rows_from`.

    ``wrappers_only`` is the trust dial. On the named-key path (``transactions``
    and friends) the key already vouches for the list, so dicts that merely fail
    the shape test are still returned -- a script whose rows use vocabulary
    :func:`_looks_like_rows` does not know must not be thrown away. On the
    gather-any-list fallback there is no such vouching, so a list that is
    neither rows nor a wrapper of rows is rejected: that is the
    ``subAccountList`` lookup-table guard.
    """
    dicts = [v for v in vals if isinstance(v, dict)]
    if not dicts:
        return None
    if _looks_like_rows(dicts):
        return dicts
    gathered = []
    for d in dicts:
        nested = _rows_from(d)
        if nested:
            gathered.extend(nested)
    if gathered:
        return gathered
    return None if wrappers_only else dicts


def _rows_from(out) -> Optional[list]:
    """Pull a flat list of row dicts out of a script's ``output_data`` value.

    API-style scripts return rows in a shape that varies by script: a bare list
    of row dicts, a dict wrapping them under a name
    (``records``/``transactions``/an array name the demo declared), a dict
    keyed per extraction whose values are row lists (the '2/2 extractions'
    case), or a LIST of per-account wrapper objects each holding its own rows.

    The shape is not stable even for one institution, because the script is
    GENERATED: re-recording America First on 2026-09-19 turned
    ``{"Checking": {"transactions": [...]}}`` into
    ``{"accounts": [{"accountName": "Checking", "transactions": [...]}]}``, and
    a reader that only recursed through dict VALUES skipped the list whole and
    reported an empty run -- 8 transactions silently not imported. What survived
    the rewrite, and what every such shape has in common, is the ``transactions``
    KEY; so the named-key scan below runs at every depth, through dicts and
    through the dicts inside lists, rather than only at the top. Returns a flat
    list of dict rows, or None."""
    if isinstance(out, list):
        return _rows_in_list(out)
    if not isinstance(out, dict):
        return None
    for key in _ROW_ARRAY_KEYS:
        rows = _rows_from(out.get(key))
        if rows:
            return rows
    # Fall back to every non-metadata value, so a script that named its array
    # (e.g. "shareSavings"), returned one list per extraction, or nested the
    # rows under a wrapper dict/list still yields them. An array that holds
    # neither transaction-SHAPED dicts nor wrappers around them is skipped
    # rather than concatenated: a script that returns a lookup table beside its
    # rows (America First's ``subAccountList``) would otherwise contribute blank
    # review rows, and the key name it uses is per-bank so only the shape can be
    # tested (:func:`_looks_like_rows`).
    gathered = []
    for key, val in out.items():
        if str(key).lower() in _NON_RECORD_KEYS:
            continue
        if isinstance(val, list):
            rows = _rows_in_list(val, wrappers_only=True)
            if rows:
                gathered.extend(rows)
        elif isinstance(val, dict):
            nested = _rows_from(val)
            if nested:
                gathered.extend(nested)
    return gathered or None


def _extract_records(payload: dict) -> Optional[list]:
    """Find the transaction rows a run returned, wherever the script put them.
    Only ``file_path`` export scripts legitimately return none."""
    if not isinstance(payload, dict):
        return None
    top = payload.get("records")
    if isinstance(top, list):
        rows = [r for r in top if isinstance(r, dict)]
        if rows:
            return rows
    return _rows_from(payload.get("output_data"))


def _run_timing(payload) -> tuple:
    """Best-effort (started_at, duration_seconds) AS REPORTED BY THE RUN itself
    -- distinct from Mammon's own wall-clock wait. webSlinger reports these in
    UTC; Mammon must log the run's own numbers, not a local re-derivation."""
    if not isinstance(payload, dict):
        return None, None
    started = None
    for key in ("started_at", "startedAt", "start_time", "startTime",
                "start", "started"):
        val = payload.get(key)
        if val:
            started = str(val)
            break
    dur = None
    for key in ("duration_seconds", "durationSeconds", "duration",
                "elapsed_seconds", "elapsedSeconds"):
        val = payload.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            dur = float(val)
            break
    if started is None or dur is None:
        for key in ("summary", "execution_summary", "executionSummary",
                    "report", "output_data"):
            sub = payload.get(key)
            if isinstance(sub, dict):
                s2, d2 = _run_timing(sub)
                started = started or s2
                dur = dur if dur is not None else d2
    return started, dur


@dataclass
class RunResult:
    """What a run returned. EXPORT scripts drop a ``file_path``; SCRAPE/API
    scripts return ``records``. ``raw`` keeps the untouched server payload for
    logging. ``sent_input`` is the POST-COERCION input_data actually handed to
    MCP; ``started_at``/``duration_seconds`` are the run's OWN reported timing
    (UTC), not Mammon's local wait."""

    success: bool = True
    status: str = "ok"
    file_path: Optional[str] = None
    records: Optional[list] = None
    error: Optional[str] = None
    raw: Optional[dict] = None
    failed_actions: Optional[int] = None
    sent_input: Optional[dict] = None
    started_at: Optional[str] = None
    duration_seconds: Optional[float] = None

    @property
    def is_file(self) -> bool:
        return bool(self.file_path)

    @property
    def has_failures(self) -> bool:
        """True when the run reported one or more failed actions -- a completed
        status is NOT proof of success (a bank page can change and an action
        silently fail while the run still 'completes'). ``None`` (no failure
        signal in the payload) is treated as no known failure."""
        return bool(self.failed_actions)

    @classmethod
    def from_mcp(cls, payload: dict) -> "RunResult":
        ok = bool(payload.get("success", True))
        out = payload.get("output_data") if isinstance(payload.get("output_data"), dict) else {}
        file_path = (payload.get("file_path")
                     or payload.get("download_path")
                     or out.get("file_path")
                     or out.get("download_path"))
        records = _extract_records(payload)
        started_at, duration_seconds = _run_timing(payload)
        return cls(
            success=ok,
            status=str(payload.get("status", "ok" if ok else "failed")),
            file_path=file_path,
            records=records,
            error=payload.get("error"),
            raw=payload,
            failed_actions=_failed_action_count(payload),
            started_at=started_at,
            duration_seconds=duration_seconds,
        )


# ---------------------------------------------------------------------------
# Client interface
# ---------------------------------------------------------------------------
class WebSlingerClient:
    """Interface Mammon depends on. Implementations: :class:`McpWebSlingerClient`
    (production) and :class:`FakeWebSlingerClient` (tests)."""

    def available(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe_script(self, name: str, source: str = "user") -> ScriptSchema:  # pragma: no cover
        raise NotImplementedError

    def run_script(self, name: str, inputs: dict, source: str = "user") -> RunResult:  # pragma: no cover
        raise NotImplementedError


def _split_command(command: str) -> Optional[list]:
    """Split a shell command string into an argv list for :class:`subprocess`.

    On Windows the command usually carries a native path with backslashes
    (``python D:\\webslinger\\taskSpinner\\mcp_server.py``); POSIX ``shlex``
    treats ``\\`` as an escape and would silently eat them, so parse in
    ``posix=False`` mode there and strip any surrounding quotes so a quoted
    path-with-spaces (``python "C:\\Program Files\\ws\\mcp.py"``) still yields a
    clean token. Returns ``None`` for a blank command (Download stays disabled).
    """
    stripped = command.strip()
    if not stripped:
        return None
    if os.name == "nt":
        return [tok.strip('"') for tok in shlex.split(stripped, posix=False)]
    return shlex.split(stripped)


# ---------------------------------------------------------------------------
# Runtime MCP-server discovery -- THE concrete connection mechanism
# ---------------------------------------------------------------------------
# Mammon reaches the webSlinger MCP server by SPAWNING it as a subprocess and
# speaking JSON-RPC over stdio (see McpWebSlingerClient). The launch command and
# any auth env come from ONE of two places, checked in this order:
#   1. $MAMMON_WEBSLINGER_MCP_CMD -- an explicit launch command (shell-split);
#      an escape hatch / override.
#   2. The ``mcpServers.webslinger`` entry in ~/.claude.json -- the SAME registry
#      Claude / Claude Code use to launch MCP servers, so once webSlinger works
#      there it works here with no extra Mammon-specific setup. That entry looks
#      like ``{"command": "python",
#              "args": ["D:\\webslinger\\taskSpinner\\mcp_server.py"],
#              "env": {...}}`` and becomes the argv ``[command, *args]`` plus env.
# When neither is present the client is ``available() is False`` and the UI
# shows :func:`config_hint` -- exactly what to add and where.
WEBSLINGER_MCP_SERVER = "webslinger"


def _claude_config_path(config_path: Optional[str] = None) -> str:
    """The Claude MCP config file Mammon reads to discover the server."""
    return config_path or os.path.join(os.path.expanduser("~"), ".claude.json")


def _iter_mcp_server_maps(data: dict):
    """Yield every ``mcpServers`` mapping in a parsed ~/.claude.json: the global
    one first, then each per-project block (``projects.<dir>.mcpServers``)."""
    if not isinstance(data, dict):
        return
    top = data.get("mcpServers")
    if isinstance(top, dict):
        yield top
    projects = data.get("projects")
    if isinstance(projects, dict):
        for proj in projects.values():
            if isinstance(proj, dict) and isinstance(proj.get("mcpServers"), dict):
                yield proj["mcpServers"]


def _load_claude_mcp(server: str = WEBSLINGER_MCP_SERVER,
                     config_path: Optional[str] = None):
    """Return ``(argv, env)`` for ``server`` from the Claude MCP config, or
    ``(None, None)`` when the file is missing/unreadable or has no such entry.
    ``argv`` is ``[command, *args]``; ``env`` is the entry's ``env`` dict if
    non-empty (so credentials configured there flow to the spawned server)."""
    path = _claude_config_path(config_path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    for servers in _iter_mcp_server_maps(data):
        entry = servers.get(server)
        if isinstance(entry, dict) and entry.get("command"):
            argv = [str(entry["command"])]
            args = entry.get("args")
            if isinstance(args, list):
                argv.extend(str(a) for a in args)
            env = entry.get("env")
            env = dict(env) if isinstance(env, dict) and env else None
            return argv, env
    return None, None


def discover_mcp_command(config_path: Optional[str] = None) -> Optional[list]:
    """The webSlinger MCP launch argv Mammon will spawn, or ``None`` if nothing is
    configured. ``$MAMMON_WEBSLINGER_MCP_CMD`` wins; otherwise the
    ``mcpServers.webslinger`` entry in ~/.claude.json is used. Pure: no process
    is started here -- this is just the ``available()`` probe's data source."""
    env_cmd = os.environ.get("MAMMON_WEBSLINGER_MCP_CMD")
    if env_cmd and env_cmd.strip():
        return _split_command(env_cmd)
    argv, _env = _load_claude_mcp(config_path=config_path)
    return argv


def config_hint(config_path: Optional[str] = None) -> str:
    """The precise, actionable message shown when webSlinger is unconfigured:
    it names BOTH supported config locations (the exact file path and the env
    var) and gives a copy-pasteable example -- never a dead end."""
    path = _claude_config_path(config_path)
    return (
        "webSlinger is not configured. Mammon reaches the webSlinger MCP server by "
        "launching it and talking JSON-RPC over stdio. Configure it in EITHER "
        "place, then reopen this dialog:\n"
        f'  1. Add a "webslinger" entry under "mcpServers" in {path} :\n'
        '       "webslinger": {"command": "python",\n'
        '                      "args": ["D:\\\\webslinger\\\\taskSpinner\\\\mcp_server.py"]}\n'
        "  2. Or set the MAMMON_WEBSLINGER_MCP_CMD environment variable to the "
        "launch command, e.g.\n"
        "       python D:\\webslinger\\taskSpinner\\mcp_server.py"
    )


class McpWebSlingerClient(WebSlingerClient):
    """Talks to the webSlinger MCP server over stdio JSON-RPC.

    The server launch command is resolved by :func:`discover_mcp_command` when
    ``command`` is omitted: ``$MAMMON_WEBSLINGER_MCP_CMD`` first, then the
    ``mcpServers.webslinger`` entry in ~/.claude.json (whose ``env`` also flows
    to the spawned process). With nothing configured, :meth:`available` is
    ``False`` and the client makes no subprocess calls. Each call spawns the
    server, performs the MCP ``initialize`` handshake, invokes one tool, and
    tears the process down -- simple and stateless; webSlinger runs are
    infrequent. ``config_path`` overrides the config file location (tests).
    """

    def __init__(self, command: Optional[str | list] = None, *,
                 env: Optional[dict] = None, timeout: float = 900.0,
                 poll_interval: float = 2.0,
                 config_path: Optional[str] = None):
        if command is None:
            env_cmd = os.environ.get("MAMMON_WEBSLINGER_MCP_CMD")
            if env_cmd and env_cmd.strip():
                command = _split_command(env_cmd)
            else:
                command, cfg_env = _load_claude_mcp(config_path=config_path)
                if env is None:
                    env = cfg_env
        if isinstance(command, str):
            command = _split_command(command)
        self._command = list(command) if command else None
        self._env = dict(env) if env else None
        self._timeout = timeout
        self._poll_interval = poll_interval
        # Session cache of describe_script schemas keyed by (name, source). Each
        # describe_script is an MCP round trip -- spawn subprocess, do the
        # initialize handshake, one tools/call, tear down (see _call). Script
        # schemas don't change within a session, so caching turns the dialog's
        # "Load fields" auto-load, the Download button's date-input probe, and the
        # redundant _array_field_names probe inside run_script into ONE handshake
        # instead of several. Only successful results are cached (errors raise
        # before the store), so a transient failure is retried on the next call.
        self._schema_cache: dict = {}

    def available(self) -> bool:
        return bool(self._command)

    def describe_script(self, name: str, source: str = "user") -> ScriptSchema:
        cache_key = (name, source)
        cached = self._schema_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self._call("describe_script",
                             {"script_name": name, "script_source": source})
        if not payload.get("success", True):
            raise WebSlingerError(
                payload.get("error")
                or f"describe_script failed ({payload.get('status', 'error')})")
        schema = ScriptSchema.from_mcp(payload)
        self._schema_cache[cache_key] = schema
        return schema

    def _array_field_names(self, name: str, source: str) -> tuple:
        """Best-effort: ask ``describe_script`` which inputs the script declares as
        arrays, so their values can be coerced to real JSON lists before the run.
        Any failure (older/absent server, transport error) falls back to content
        sniffing only -- it must never block a run."""
        try:
            schema = self.describe_script(name, source)
        except Exception:
            return ()
        fields = []
        for inp in schema.inputs:
            if inp.is_array or isinstance(schema.input_template.get(inp.name), list):
                fields.append(inp.name)
        return tuple(fields)

    def run_script(self, name: str, inputs: dict, source: str = "user") -> RunResult:
        inputs = _coerce_input_data(inputs, self._array_field_names(name, source))
        payload = self._run_and_wait(name, inputs, source)
        result = RunResult.from_mcp(payload)
        # Record the POST-coercion inputData actually sent, so the download log
        # reflects the real array (e.g. ['Share Savings']) instead of the raw
        # pre-coercion string the account stored.
        result.sent_input = dict(inputs)
        if not result.success:
            raise WebSlingerError(result.error or f"run_script {result.status}")
        # Judge success by whether usable DATA came back for the requested range,
        # NOT by the run's error count. A bank automation routinely logs incidental
        # action errors (page tweaks, stray retries) while still returning the
        # transactions asked for; the old gate rejected such runs and threw away
        # valid data (ACU 'Anytown CU Pat' 0000000: range data + 17 errors was
        # wrongly rejected). So failed actions are now a SURFACED WARNING, never a
        # rejection. An empty result (no records, no export file) is returned as-is
        # so download_account can fall through to the Downloads-file scan and, only
        # if nothing is found there either, report failure.
        if result.has_failures:
            has_data = bool(result.records) or bool(result.file_path)
            _log.warning(
                "run_script for %r finished with status %r and %s failed "
                "action(s); %s",
                name, result.status, result.failed_actions,
                "usable data was returned, importing anyway (errors surfaced as a "
                "warning)" if has_data
                else "no usable data was returned -- caller will scan Downloads")
        return result

    def _run_and_wait(self, name: str, input_data: dict, source: str) -> dict:
        """Start the run and poll it to completion instead of blocking one
        synchronous ``run_script`` MCP call for up to its 600s timeout. Uses the
        async ``start_run`` + ``get_run_status`` pair; when the run finishes, the
        full ``get_run_report`` is fetched to surface per-action failures the
        terse status summary omits (see :func:`_failed_action_count`). Falls back
        to the blocking ``run_script`` tool when the server hands back no run id
        (older servers with no async variant)."""
        started = self._call("start_run",
                             {"script_name": name,
                              "input_data": input_data,
                              "script_source": source})
        if not started.get("success", True):
            return started  # launch_failed/busy/etc. -- let from_mcp surface it
        run_id = _extract_run_id(started)
        if not run_id:
            return self._call("run_script",
                              {"script_name": name,
                               "input_data": input_data,
                               "script_source": source})
        deadline = time.monotonic() + self._timeout
        while True:
            status = self._call("get_run_status", {"run_id": run_id})
            if str(status.get("status", "")).lower() != "running":
                self._attach_failed_actions(run_id, status)
                return status
            if time.monotonic() >= deadline:
                raise WebSlingerError(
                    f"webSlinger run {run_id} for {name!r} did not finish within "
                    f"{int(self._timeout)}s")
            time.sleep(self._poll_interval)

    def _attach_failed_actions(self, run_id: str, status: dict) -> None:
        """When the terminal status summary carries no per-action failure signal,
        fetch the full report to fill it in. Never raises -- a missing/errored
        report just leaves the summary as-is."""
        if _failed_action_count(status) is not None:
            return
        try:
            report = self._call("get_run_report", {"run_id": run_id})
        except WebSlingerError:
            return
        count = _failed_action_count(report)
        if count is not None:
            status["failed_actions"] = count

    # ---- stdio JSON-RPC ---------------------------------------------------
    def _call(self, tool: str, arguments: dict) -> dict:
        if not self._command:
            raise WebSlingerError("webSlinger MCP is not configured")
        popen_env = {**os.environ, **self._env} if self._env else None
        try:
            proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                env=popen_env,
            )
        except OSError as exc:
            raise WebSlingerError(f"could not launch webSlinger MCP: {exc}") from exc
        try:
            self._send(proc, 1, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mammon", "version": "1.0"},
            })
            self._read_result(proc, 1)
            self._notify(proc, "notifications/initialized")
            self._send(proc, 2, "tools/call",
                       {"name": tool, "arguments": arguments})
            result = self._read_result(proc, 2)
        finally:
            self._shutdown(proc)
        return _tool_payload(result)

    def _send(self, proc, msg_id, method, params) -> None:
        self._write(proc, {"jsonrpc": "2.0", "id": msg_id,
                           "method": method, "params": params})

    def _notify(self, proc, method) -> None:
        self._write(proc, {"jsonrpc": "2.0", "method": method})

    def _write(self, proc, obj) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _read_result(self, proc, msg_id) -> dict:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                raise WebSlingerError("webSlinger MCP closed the connection")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # a stray log line -- skip
            if msg.get("id") != msg_id:
                continue  # a notification or another response
            if "error" in msg:
                raise WebSlingerError(str(msg["error"]))
            return msg.get("result", {})

    def _shutdown(self, proc) -> None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=self._timeout)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()


def _tool_payload(result: dict) -> dict:
    """Pull the JSON object a webSlinger tool returned out of an MCP tools/call
    result. The server may hand it back as ``structuredContent`` or as a text
    content block whose text is JSON."""
    if isinstance(result.get("structuredContent"), dict):
        sc = result["structuredContent"]
        # Some servers wrap the payload as {"result": {...}}.
        return sc.get("result", sc) if "result" in sc and isinstance(sc["result"], dict) else sc
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return json.loads(block.get("text", ""))
            except ValueError:
                continue
    return result if isinstance(result, dict) else {}


class FakeWebSlingerClient(WebSlingerClient):
    """In-memory client for tests. ``schemas`` maps script name -> a raw
    describe_script payload dict (or a :class:`ScriptSchema`); ``results`` maps
    script name -> a :class:`RunResult`. ``available`` is settable so gating can
    be exercised in every state."""

    def __init__(self, *, schemas=None, results=None, available=True):
        self._schemas = dict(schemas or {})
        self._results = dict(results or {})
        self._available = available
        self.calls: list = []          # (kind, name, inputs) for assertions

    def available(self) -> bool:
        return self._available

    def describe_script(self, name: str, source: str = "user") -> ScriptSchema:
        self.calls.append(("describe", name, None))
        if name not in self._schemas:
            raise WebSlingerError(f"script {name!r} not found")
        schema = self._schemas[name]
        if isinstance(schema, ScriptSchema):
            return schema
        return ScriptSchema.from_mcp(schema)

    def run_script(self, name: str, inputs: dict, source: str = "user") -> RunResult:
        self.calls.append(("run", name, dict(inputs or {})))
        if name not in self._results:
            raise WebSlingerError(f"no canned result for {name!r}")
        return self._results[name]


# ---------------------------------------------------------------------------
# Gate + runner adaptor used by the UI / downloads pipeline
# ---------------------------------------------------------------------------
class DownloadPreflight(NamedTuple):
    """Structured result of the Download preflight. ``ok`` is the go/no-go;
    ``code`` names WHICH prerequisite is missing (one of ``ok``, ``no_client``,
    ``no_script``, ``no_input``) so callers can branch without string-matching;
    ``title`` is a short dialog heading and ``message`` the full user-facing
    sentence naming the missing item and the concrete step to fix it. It is also
    used as the Download tooltip."""
    ok: bool
    code: str
    title: str
    message: str


def _has_input_data(input_data) -> bool:
    """True when the account's saved inputData actually carries at least one
    non-blank value. An empty dict, ``None``, or a dict of only blank strings
    counts as "not filled in"."""
    if not isinstance(input_data, dict) or not input_data:
        return False
    return any(str(v).strip() for v in input_data.values())


def preflight_download(*, client: Optional[WebSlingerClient],
                       script_name: Optional[str],
                       input_data=None) -> DownloadPreflight:
    """Check the three Download prerequisites in priority order and return the
    FIRST that is missing as a :class:`DownloadPreflight` (``ok=False``), or an
    ``ok=True`` result when all pass.

    Order (matches the task brief): (1) webSlinger MCP reachable/available,
    (2) a download script is configured for this account, (3) that script's
    inputData is filled in. This never raises and never touches the network
    beyond the client's cheap ``available()`` probe -- it exists so the Download
    button can stay clickable in every state and always tell the user exactly
    what to do next.

    Credentials are DELIBERATELY not a gate here: the webSlinger MCP server logs
    into the site with the credentials living in the user's own browser, so Mammon
    neither holds nor checks any login secret of its own.
    """
    if client is None or not client.available():
        return DownloadPreflight(
            False, "no_client", "webSlinger unavailable",
            "webSlinger is not available. Configure the webSlinger MCP server: "
            'add a "webslinger" entry under "mcpServers" in ~/.claude.json, or set '
            "MAMMON_WEBSLINGER_MCP_CMD to its launch command "
            r"(e.g. python D:\webslinger\taskSpinner\mcp_server.py).")
    if not (script_name and str(script_name).strip()):
        return DownloadPreflight(
            False, "no_script", "No download script",
            "No download script is configured for this account. "
            "Set one in Account Details → Download.")
    if not _has_input_data(input_data):
        return DownloadPreflight(
            False, "no_input", "Download inputs not filled in",
            "This account's download script has no saved inputs. "
            "Fill in its input fields in Account Details → Download.")
    return DownloadPreflight(
        True, "ok", "Ready", "Download the latest statement via webSlinger.")


def download_readiness(*, client: Optional[WebSlingerClient],
                       script_name: Optional[str], input_data=None) -> tuple[bool, str]:
    """Back-compat ``(enabled, reason)`` view over :func:`preflight_download`,
    used where only the tooltip string is needed."""
    pf = preflight_download(client=client, script_name=script_name,
                            input_data=input_data)
    return pf.ok, pf.message


def mcp_runner(client: WebSlingerClient) -> Callable[[str, dict], object]:
    """Adapt a :class:`WebSlingerClient` to the ``downloads.Runner`` contract
    (``Callable[[script, inputs], downloads.RunOutput]``) so a webSlinger run can
    feed straight into ``downloads.run_and_import``."""
    from mammon import downloads  # local import: downloads is heavier

    def _run(script: str, inputs: dict):
        result = client.run_script(script, inputs)
        return downloads.RunOutput(
            file_path=result.file_path,
            records=result.records,
            raw=result.raw,
        )

    return _run


def default_client() -> WebSlingerClient:
    """The app's client -- MCP-backed. Its launch command is auto-discovered by
    :func:`discover_mcp_command` (``$MAMMON_WEBSLINGER_MCP_CMD``, then the
    ``mcpServers.webslinger`` entry in ~/.claude.json), so it is available as
    soon as webSlinger is registered for Claude -- no Mammon-specific setup. When
    nothing is configured it reports ``available() is False`` and the UI shows
    :func:`config_hint`."""
    return McpWebSlingerClient()
