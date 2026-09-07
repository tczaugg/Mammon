"""webSlinger client: schema parsing, gating, and the runner adaptor.

The describe_script fixture below is the VERBATIM shape the webSlinger MCP server
returns for the Anytown CU checking script ``GetCheckingTransactionsForRange``
-- account number, a start/end date range, and an export format. The dynamic
Download form is built from exactly this, so the tests pin the parsing to the
real payload.
"""
from __future__ import annotations

from mammon import downloads, webslinger
from mammon.webslinger import (
    FakeWebSlingerClient, RunResult, ScriptSchema, download_readiness, mcp_runner,
    preflight_download,
)

# Real describe_script result (source: webSlinger MCP, Anytown CU checking).
CHECKING_SCHEMA = {
    "success": True,
    "script_name": "GetCheckingTransactionsForRange.js",
    "display_name": "GetCheckingTransactionsForRange",
    "description": ("Login to bank (MFA) go to checking account, in export widget "
                    "enter startDate, endDate, and format, then submit."),
    "inputs": [
        {"description": "account number", "example": "1234567",
         "name": "userName", "type": ""},
        {"description": "Start date for transaction export", "example": "1/1/2026",
         "name": "startDate", "type": ""},
        {"description": "End date for transaction export", "example": "7/31/2026",
         "name": "endDate", "type": ""},
        {"description": "Export file format (e.g., CSV, OFX)",
         "example": "OFX File (.OFX)", "name": "format", "type": ""},
    ],
    "internal_inputs": [],
    "outputs": [],
    "input_template": {
        "userName": "1234567", "startDate": "1/1/2026",
        "endDate": "7/31/2026", "format": "OFX File (.OFX)",
    },
    "target_website": "https://anytowncu.example",
    "created_date": "2026-08-08T06:17:01Z",
}


def test_schema_parses_real_describe_payload():
    schema = ScriptSchema.from_mcp(CHECKING_SCHEMA)
    assert schema.name == "GetCheckingTransactionsForRange"
    assert schema.target_website == "https://anytowncu.example"
    assert [i.name for i in schema.inputs] == [
        "userName", "startDate", "endDate", "format"]
    assert schema.input_template["format"] == "OFX File (.OFX)"
    # description + example feed the field hint
    start = next(i for i in schema.inputs if i.name == "startDate")
    assert start.is_date is True
    assert "1/1/2026" in start.hint


def test_schema_flags_secret_inputs():
    payload = {"inputs": [{"name": "totpCode", "description": "MFA code"}]}
    schema = ScriptSchema.from_mcp(payload)
    assert schema.inputs[0].is_secret is True


def test_fake_client_describe_and_run():
    client = FakeWebSlingerClient(
        schemas={"GetCheckingTransactionsForRange": CHECKING_SCHEMA},
        results={"GetCheckingTransactionsForRange": RunResult(file_path="/tmp/x.ofx")},
    )
    assert client.available() is True
    schema = client.describe_script("GetCheckingTransactionsForRange")
    assert len(schema.inputs) == 4
    out = client.run_script("GetCheckingTransactionsForRange", {"userName": "1"})
    assert out.file_path == "/tmp/x.ofx"
    assert ("run", "GetCheckingTransactionsForRange", {"userName": "1"}) in client.calls


def test_schema_parses_outputs_shape():
    """describe_script's ``outputs`` (the outputData shape) is parsed so Mammon
    knows the returned-data shape. Entries may be dicts or bare strings."""
    payload = {"script_name": "s",
               "outputs": [{"name": "date"}, {"name": "amount"}, "memo", {}]}
    schema = ScriptSchema.from_mcp(payload)
    assert schema.outputs == ("date", "amount", "memo")
    assert schema.returns_data is True
    # a script with no declared outputs drives a browser download instead
    assert ScriptSchema.from_mcp({"script_name": "x"}).returns_data is False


def test_gate_open_when_everything_present():
    client = FakeWebSlingerClient(available=True)
    ok, reason = download_readiness(
        client=client, script_name="GetCheckingTransactionsForRange",
        input_data={"userName": "1234567"})
    assert ok is True


def test_gate_blocks_each_missing_prereq():
    up = FakeWebSlingerClient(available=True)
    down = FakeWebSlingerClient(available=False)
    filled = {"userName": "1234567"}
    # webSlinger unavailable
    ok, why = download_readiness(client=down, script_name="s", input_data=filled)
    assert not ok and "available" in why.lower()
    # NO credential/api-key gate: a reachable MCP + a script + saved inputs must
    # PASS with Mammon holding no secret whatsoever (creds live in the browser).
    ok, why = download_readiness(client=up, script_name="s", input_data=filled)
    assert ok is True
    assert "credential" not in why.lower() and "api key" not in why.lower()
    # no script
    ok, why = download_readiness(client=up, script_name="", input_data=filled)
    assert not ok and "script" in why.lower()
    # a script but its inputData is not filled in (missing or all-blank)
    ok, why = download_readiness(client=up, script_name="s", input_data={})
    assert not ok and "input" in why.lower()
    ok, why = download_readiness(client=up, script_name="s",
                                 input_data={"userName": "   "})
    assert not ok and "input" in why.lower()


def test_preflight_returns_structured_code_per_branch():
    up = FakeWebSlingerClient(available=True)
    down = FakeWebSlingerClient(available=False)
    filled = {"userName": "1234567"}

    def code(**kw):
        pf = preflight_download(**kw)
        assert pf.title and pf.message           # always user-facing
        return pf

    # priority order: client -> script -> inputData (NO credential gate)
    assert code(client=down, script_name="s", input_data=filled).code == "no_client"
    assert code(client=up, script_name="", input_data=filled).code == "no_script"
    assert code(client=up, script_name="s", input_data={}).code == "no_input"
    ready = code(client=up, script_name="GetCheckingTransactionsForRange",
                 input_data=filled)
    assert ready.ok is True and ready.code == "ok"


def test_mcp_runner_adapts_to_runoutput():
    client = FakeWebSlingerClient(
        results={"s": RunResult(file_path="/tmp/f.ofx", records=None, raw={"a": 1})})
    runner = mcp_runner(client)
    out = runner("s", {})
    assert isinstance(out, downloads.RunOutput)
    assert out.file_path == "/tmp/f.ofx"
    assert out.is_file is True


def test_discover_mcp_command_from_claude_config(tmp_path, monkeypatch):
    """Mammon discovers the MCP launch command from the SAME registry Claude uses
    (~/.claude.json ``mcpServers.webslinger``), so no Mammon-specific setup is
    needed once webSlinger works for Claude."""
    import json
    from mammon.webslinger import (McpWebSlingerClient, discover_mcp_command,
                                  _load_claude_mcp)
    monkeypatch.delenv("MAMMON_WEBSLINGER_MCP_CMD", raising=False)
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"webslinger": {
        "type": "stdio", "command": "python",
        "args": ["D:\\webslinger\\taskSpinner\\mcp_server.py"],
        "env": {"WS_TOKEN": "abc"}}}}), encoding="utf-8")
    assert discover_mcp_command(config_path=str(cfg)) == [
        "python", "D:\\webslinger\\taskSpinner\\mcp_server.py"]
    # the entry's env is threaded through for auth
    argv, env = _load_claude_mcp(config_path=str(cfg))
    assert argv[0] == "python" and env == {"WS_TOKEN": "abc"}
    # a client built from that config path is available WITHOUT the env var
    client = McpWebSlingerClient(config_path=str(cfg))
    assert client.available() is True


def test_discover_reads_project_scoped_entry(tmp_path, monkeypatch):
    """A per-project ``projects.<dir>.mcpServers`` block is honored too."""
    import json
    from mammon.webslinger import discover_mcp_command
    monkeypatch.delenv("MAMMON_WEBSLINGER_MCP_CMD", raising=False)
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"projects": {"D:/webslinger": {"mcpServers": {
        "webslinger": {"command": "py", "args": ["mcp.py"]}}}}}), encoding="utf-8")
    assert discover_mcp_command(config_path=str(cfg)) == ["py", "mcp.py"]


def test_env_var_overrides_claude_config(tmp_path, monkeypatch):
    """$MAMMON_WEBSLINGER_MCP_CMD wins over the config file."""
    import os
    from mammon.webslinger import discover_mcp_command
    cfg = tmp_path / ".claude.json"
    cfg.write_text('{"mcpServers":{"webslinger":{"command":"python","args":["a.py"]}}}',
                   encoding="utf-8")
    override = r"python C:\ws\mcp.py" if os.name == "nt" else "python /ws/mcp.py"
    monkeypatch.setenv("MAMMON_WEBSLINGER_MCP_CMD", override)
    argv = discover_mcp_command(config_path=str(cfg))
    assert argv[-1].endswith("mcp.py") and "a.py" not in argv


def test_unconfigured_client_and_actionable_hint(tmp_path, monkeypatch):
    """No env var and no config entry -> available() is False, and config_hint
    names BOTH the config file and the env var (never a dead end)."""
    from mammon.webslinger import McpWebSlingerClient, config_hint
    monkeypatch.delenv("MAMMON_WEBSLINGER_MCP_CMD", raising=False)
    missing = tmp_path / "nope.json"
    assert McpWebSlingerClient(config_path=str(missing)).available() is False
    # present file but no webslinger entry -> still unconfigured
    empty = tmp_path / ".claude.json"
    empty.write_text('{"mcpServers": {}}', encoding="utf-8")
    assert McpWebSlingerClient(config_path=str(empty)).available() is False
    msg = config_hint()
    assert "mcpServers" in msg and ".claude.json" in msg
    assert "MAMMON_WEBSLINGER_MCP_CMD" in msg


def test_split_command_parses_launch_command():
    """The MCP launch command in $MAMMON_WEBSLINGER_MCP_CMD must round-trip. On
    Windows the natural path uses backslashes, which POSIX shlex would eat."""
    import os
    from mammon.webslinger import McpWebSlingerClient, _split_command
    assert _split_command("   ") is None
    assert McpWebSlingerClient(command="").available() is False
    if os.name == "nt":
        assert _split_command(r"python D:\webslinger\taskSpinner\mcp_server.py") == [
            "python", r"D:\webslinger\taskSpinner\mcp_server.py"]
        # a quoted path-with-spaces yields a clean, unquoted token
        assert _split_command('python "C:\\Program Files\\ws\\mcp.py"') == [
            "python", r"C:\Program Files\ws\mcp.py"]
    else:
        assert _split_command("python /opt/ws/mcp.py") == ["python", "/opt/ws/mcp.py"]


# ---------------------------------------------------------------------------
# Live-validation fixes: list inputData, async polling, real-success check
# ---------------------------------------------------------------------------
import datetime

import pytest

from mammon.webslinger import McpWebSlingerClient, WebSlingerError, _coerce_input_data


class _StubMcp(McpWebSlingerClient):
    """McpWebSlingerClient with the subprocess JSON-RPC transport stubbed: each
    tool name maps to a canned response (a dict, a callable, or a LIST consumed
    one call at a time to simulate polling). A tool with no entry raises
    WebSlingerError, exactly as a real server error would."""

    def __init__(self, responses):
        super().__init__(command=["noop"], poll_interval=0)
        self._responses = responses
        self.calls = []

    def _call(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if tool not in self._responses:
            raise WebSlingerError(f"no stub for {tool!r}")
        resp = self._responses[tool]
        if isinstance(resp, list):
            return resp.pop(0)
        if callable(resp):
            return resp(arguments)
        return resp


def test_coerce_input_data_upgrades_json_list_and_object():
    coerced = _coerce_input_data({
        "subAccountName": '["Checking","Savings"]',
        "opts": '{"a": 1}',
        "userName": "1234567",     # numeric-looking string is NOT a JSON array
        "note": "not json",
        "already": ["x"],
    })
    assert coerced["subAccountName"] == ["Checking", "Savings"]
    assert coerced["opts"] == {"a": 1}
    assert coerced["userName"] == "1234567"
    assert coerced["note"] == "not json"
    assert coerced["already"] == ["x"]


def test_run_script_sends_list_input_as_json_array():
    """The subAccountName bug: a list typed as text must reach start_run as a
    real array, not a stringified one."""
    stub = _StubMcp({
        "start_run": {"success": True, "run_id": "r1"},
        "get_run_status": {"success": True, "status": "completed",
                           "failed_actions": 0, "output_data": {"records": []}},
    })
    stub.run_script("S", {"subAccountName": '["A","B"]'})
    start_args = next(a for (t, a) in stub.calls if t == "start_run")
    assert start_args["input_data"]["subAccountName"] == ["A", "B"]


@pytest.mark.parametrize("entered", [
    ["Share Savings"],       # already a real list (e.g. reloaded from persistence)
    '["Share Savings"]',     # JSON-array text
    "Share Savings",         # a lone plain value typed into the field
    "['Share Savings']",     # Python-repr text (single quotes -- json.loads rejects it)
])
def test_run_script_coerces_array_typed_field_to_json_array(entered):
    """A field the script DECLARES as an array (via describe_script) must reach
    start_run as a real JSON list for every form the value arrives in -- covering
    the subAccountName bug where a lone value or a repr'd list was sent as a
    string and the script iterated its characters."""
    stub = _StubMcp({
        "describe_script": {"success": True, "inputs": [
            {"name": "subAccountName", "description": "sub-accounts",
             "type": "array"},
            {"name": "userName", "description": "account number", "type": ""},
        ]},
        "start_run": {"success": True, "run_id": "r1"},
        "get_run_status": {"success": True, "status": "completed",
                           "failed_actions": 0, "output_data": {"records": []}},
    })
    stub.run_script("GetCheckingTransactionsForRange",
                    {"subAccountName": entered, "userName": "1234567"})
    start_args = next(a for (t, a) in stub.calls if t == "start_run")
    sub = start_args["input_data"]["subAccountName"]
    assert isinstance(sub, list) and sub == ["Share Savings"]
    # a scalar (non-array) field is left untouched, not wrapped in a list.
    assert start_args["input_data"]["userName"] == "1234567"


def test_run_script_polls_start_run_to_completion():
    stub = _StubMcp({
        "start_run": {"success": True, "run_id": "r1"},
        "get_run_status": [
            {"status": "running", "elapsed_seconds": 1},
            {"success": True, "status": "completed", "failed_actions": 0,
             "records": [{"a": 1}]},
        ],
    })
    result = stub.run_script("S", {})
    assert result.success is True
    assert result.records == [{"a": 1}]
    # run_script first best-effort-asks describe_script for array-typed fields;
    # here that lookup finds no stub and is ignored -- assert the RUN sequence.
    tools = [t for (t, _a) in stub.calls if t != "describe_script"]
    assert tools == ["start_run", "get_run_status", "get_run_status"]


def test_run_script_falls_back_to_blocking_when_no_run_id():
    stub = _StubMcp({
        "start_run": {"success": True},   # server has no async variant: no run_id
        "run_script": {"success": True, "status": "completed", "failed_actions": 0,
                       "output_data": {"file_path": "/tmp/x.ofx"}},
    })
    result = stub.run_script("S", {})
    assert result.file_path == "/tmp/x.ofx"
    tools = [t for (t, _a) in stub.calls if t != "describe_script"]
    assert tools == ["start_run", "run_script"]


_ROW = {"date": "2026-01-15", "amount": "1.00", "fitid": "X-1"}
_ROW2 = {"date": "2026-02-15", "amount": "2.00", "fitid": "X-2"}


@pytest.mark.parametrize("output_data, expected", [
    ({"records": [_ROW, _ROW2]}, [_ROW, _ROW2]),        # classic wrapper
    ([_ROW, _ROW2], [_ROW, _ROW2]),                     # bare list
    ({"shareSavings": [_ROW, _ROW2]}, [_ROW, _ROW2]),   # array-name key
    ({"jan": [_ROW], "feb": [_ROW2]}, [_ROW, _ROW2]),   # per-extraction dict
    # metadata lists must never be mistaken for rows
    ({"failed_actions": [{"step": 1}], "txns": [_ROW]}, [_ROW]),
    ({"file_path": "/tmp/x.ofx"}, None),                # export script: no rows
])
def test_from_mcp_extracts_records_from_varied_output_data(output_data, expected):
    """RunResult.from_mcp must find transaction rows wherever an API script put
    them in output_data (regression: rows under a non-'records' key were seen as
    empty, so Mammon wrongly fell through to the Downloads scan)."""
    from mammon.webslinger import RunResult
    payload = {"success": True, "status": "completed", "output_data": output_data}
    assert RunResult.from_mcp(payload).records == expected


def test_from_mcp_captures_run_reported_timing():
    from mammon.webslinger import RunResult
    r = RunResult.from_mcp({"success": True, "status": "completed",
                            "started_at": "2026-08-22T13:37:06Z",
                            "duration_seconds": 209.5,
                            "output_data": {"records": [_ROW]}})
    assert r.started_at == "2026-08-22T13:37:06Z"
    assert r.duration_seconds == 209.5


def test_run_script_accepts_data_despite_failed_actions():
    """(a) Data present WITH errors -> the run is accepted, not rejected. Incidental
    failed actions are surfaced as a warning; the returned range data is kept so the
    caller imports it (ACU 'Anytown CU Pat' 0000000: range data + errors)."""
    stub = _StubMcp({
        "start_run": {"success": True, "run_id": "r1"},
        "get_run_status": {"success": True, "status": "completed",
                           "failed_actions": 17, "records": [{"a": 1}]},
    })
    result = stub.run_script("S", {})
    assert result.records == [{"a": 1}]     # data-with-errors -> import proceeds
    assert result.failed_actions == 17      # count preserved for the warning
    assert result.has_failures is True


def test_run_script_returns_empty_result_when_no_data_despite_report_errors():
    """(b) Data ABSENT (even with report errors) -> run_script does NOT raise; it
    returns an empty result so download_account can fall through to the Downloads
    scan. The full report is still consulted to surface the failure count."""
    stub = _StubMcp({
        "start_run": {"success": True, "run_id": "r1"},
        "get_run_status": {"success": True, "status": "completed"},
        "get_run_report": {"actions": [{"success": True}, {"success": False}]},
    })
    result = stub.run_script("S", {})
    assert result.records is None and result.file_path is None
    assert result.failed_actions == 1        # report consulted for the count
    assert any(t == "get_run_report" for (t, _a) in stub.calls)


def test_run_script_still_raises_on_launch_failure_with_no_data():
    """(c) A genuine run failure (success is False) still raises -- only the
    error-count-with-data case was relaxed, not real launch/protocol failures."""
    stub = _StubMcp({
        "start_run": {"success": True, "run_id": "r1"},
        "get_run_status": {"success": False, "status": "failed",
                           "error": "session expired"},
    })
    with pytest.raises(WebSlingerError):
        stub.run_script("S", {})


def test_run_script_surfaces_start_run_launch_failure():
    stub = _StubMcp({"start_run": {"success": False, "status": "busy",
                                   "error": "already running"}})
    with pytest.raises(WebSlingerError):
        stub.run_script("S", {})


# ---------------------------------------------------------------------------
# Download date-range helpers (fix 2)
# ---------------------------------------------------------------------------
def _checking_schema():
    return ScriptSchema.from_mcp(CHECKING_SCHEMA)


def test_classify_date_inputs_finds_start_and_end():
    start, end = downloads.classify_date_inputs(_checking_schema())
    assert start.name == "startDate"
    assert end.name == "endDate"


def test_default_download_dates_without_saved_end():
    today = datetime.date(2026, 8, 22)
    start, end = downloads.default_download_dates({}, today, end_name="endDate")
    assert end == today
    assert start == today - datetime.timedelta(days=30)


def test_default_download_dates_uses_last_end_plus_one():
    today = datetime.date(2026, 8, 22)
    start, end = downloads.default_download_dates(
        {"_last_end": "2026-08-10"}, today, end_name="endDate")
    assert start == datetime.date(2026, 8, 11)
    assert end == today


def test_build_download_input_data_formats_and_drops_bookkeeping():
    schema = _checking_schema()
    cfg = {"userName": "1234567", "_last_end": "2026-08-10"}
    data = downloads.build_download_input_data(
        schema, cfg, datetime.date(2026, 8, 11), datetime.date(2026, 8, 22))
    assert data["startDate"] == "08/11/2026"        # matches example %m/%d/%Y
    assert data["endDate"] == "08/22/2026"
    assert data["userName"] == "1234567"
    assert data["format"] == "OFX File (.OFX)"      # filled from template
    assert "_last_end" not in data                  # bookkeeping never sent


def test_remember_download_dates_chains_next_default():
    schema = _checking_schema()
    cfg = downloads.remember_download_dates(
        {}, schema, datetime.date(2026, 8, 11), datetime.date(2026, 8, 22))
    assert cfg["_last_end"] == "2026-08-22"
    assert cfg["endDate"] == "08/22/2026"
    start, _end = downloads.default_download_dates(
        cfg, datetime.date(2026, 9, 1), end_name="endDate")
    assert start == datetime.date(2026, 8, 23)       # day after this end


def test_date_format_from_example_variants():
    assert downloads.date_format_from_example("7/31/2026") == "%m/%d/%Y"
    assert downloads.date_format_from_example("2026-07-31") == "%Y-%m-%d"
    assert downloads.date_format_from_example("") == "%Y-%m-%d"
    assert downloads.date_format_from_example("garbage") == "%Y-%m-%d"


def test_download_failed_error_mentions_keycocoon():
    """fix 6: the failure message no longer claims we wait on a browser MFA
    approval -- webSlinger signs in via keyCocoon, so that is what we surface."""
    with pytest.raises(downloads.DownloadFailedError) as exc:
        downloads._require_output(downloads.RunOutput(), "S")
    assert "keyCocoon" in str(exc.value)


# ---------------------------------------------------------------------------
# payload extraction: a script's OTHER arrays are not transaction rows
# ---------------------------------------------------------------------------
def test_a_lookup_table_beside_the_rows_is_not_gathered():
    """Regression (the user, on a fresh reload): eleven blank lines at the top
    of the review list.

    The America First script's own description says "The subAccountList maps
    shortName to accountId", and it returns one transaction array per
    sub-account. The gather-every-list fallback concatenated the lookup table
    with the rows, so 11 ``{"id": ..., "shortName": ...}`` entries became 11
    review rows with no date, no amount and no text. The key name is per-bank,
    so only the row SHAPE can be tested.
    """
    payload = {"output_data": {
        "subAccountList": [{"id": 6239395, "shortName": "Household Checking"},
                           {"id": 5896620, "shortName": "Checking"}],
        "Checking": [
            {"transactionId": "A1", "postedDate": "2026-09-02",
             "amount": "42.10", "isDebit": True,
             "statementDescription": "COSTCO WHSE #1118"},
        ]}}
    rows = webslinger._extract_records(payload)
    assert [r["transactionId"] for r in rows] == ["A1"]
    assert all("shortName" not in r for r in rows)


def test_a_payload_of_only_a_lookup_table_yields_no_records():
    """...and a run that returned ONLY the lookup table has no data at all,
    rather than looking like eleven successful rows."""
    assert webslinger._extract_records(
        {"output_data": {"subAccountList": [{"id": 1, "shortName": "Checking"}]}}
    ) is None


def test_named_row_arrays_are_still_gathered():
    """The fallback must keep doing its job: a script that named its array gets
    its rows, and several such arrays are still concatenated."""
    payload = {"output_data": {
        "shareSavings": [{"postedDate": "2026-09-01", "amount": "1.00",
                          "statementDescription": "DIVIDEND"}],
        "checking": [{"postedDate": "2026-09-02", "amount": "2.00",
                      "statementDescription": "POS PURCHASE"}]}}
    rows = webslinger._extract_records(payload)
    assert len(rows) == 2
    assert {r["statementDescription"] for r in rows} == {"DIVIDEND", "POS PURCHASE"}
