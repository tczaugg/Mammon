"""Top-level crash handler (issue 5): a silent crash must leave a trace."""
from __future__ import annotations

import pathlib

import sys

from mammon import crashlog


def _boom():
    raise ValueError("kaboom-42")


def test_crash_log_path_anchored_to_db(tmp_path):
    db = tmp_path / "sub" / "mammon.db"
    assert crashlog.crash_log_path(db) == db.resolve().parent / crashlog.DEFAULT_CRASH_LOG


def test_crash_log_path_defaults_to_home():
    assert crashlog.crash_log_path(None).name == crashlog.DEFAULT_CRASH_LOG


def test_format_entry_contains_traceback_and_stamp():
    try:
        _boom()
    except ValueError:
        entry = crashlog.format_entry(*sys.exc_info())
    assert "Mammon crash" in entry
    assert "ValueError" in entry
    assert "kaboom-42" in entry
    assert "_boom" in entry            # the frame is in the traceback


def test_write_crash_creates_and_appends(tmp_path):
    log = tmp_path / "nested" / "mammon_crash.log"
    for _ in range(2):
        try:
            _boom()
        except ValueError:
            crashlog.write_crash(log, *sys.exc_info())
    assert log.exists()                # parent dir was created
    text = log.read_text(encoding="utf-8")
    assert text.count("Mammon crash") == 2   # appended, not overwritten
    assert "kaboom-42" in text


def test_install_excepthook_logs_and_chains(tmp_path, monkeypatch):
    log = tmp_path / "mammon_crash.log"
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append(a))
    hook = crashlog.install_excepthook(log)
    try:
        assert sys.excepthook is hook
        assert hook._crash_log == log
        try:
            _boom()
        except ValueError:
            info = sys.exc_info()
        sys.excepthook(*info)          # simulate an uncaught exception
        assert log.exists()
        assert "kaboom-42" in log.read_text(encoding="utf-8")
        assert len(seen) == 1          # chained to the previous hook
    finally:
        sys.excepthook = hook._previous


def test_logging_failure_never_masks_the_crash(tmp_path, monkeypatch):
    # A failure while WRITING the crash log must not raise out of the hook.
    log = tmp_path / "mammon_crash.log"
    hook = crashlog.install_excepthook(log, chain=False)
    monkeypatch.setattr(crashlog, "write_crash",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    try:
        try:
            _boom()
        except ValueError:
            sys.excepthook(*sys.exc_info())   # must not raise
    finally:
        sys.excepthook = hook._previous


# ---------------------------------------------------------------------------
# Hard (C-level) crashes.
#
# A Qt object used after deletion aborts the process without raising, so
# sys.excepthook never runs and the app vanishes leaving NOTHING in the log --
# which is exactly what a user reports as "terminates with no message".
# faulthandler is the only thing that sees those.
# ---------------------------------------------------------------------------
def test_faulthandler_is_armed_by_install_excepthook(tmp_path):
    import faulthandler
    from mammon import crashlog
    log = tmp_path / "crash.log"
    crashlog.install_excepthook(log, chain=False)
    assert faulthandler.is_enabled()
    assert "faulthandler armed" in log.read_text(encoding="utf-8")


def test_faulthandler_file_is_held_open(tmp_path):
    """faulthandler keeps only the file DESCRIPTOR, so letting the Python file
    object be collected would close the fd and silently disable the dump."""
    from mammon import crashlog
    crashlog.install_excepthook(tmp_path / "crash.log", chain=False)
    assert crashlog._FAULT_FILE is not None
    assert not crashlog._FAULT_FILE.closed


def test_a_real_segfault_lands_in_the_crash_log(tmp_path):
    """End to end, in a subprocess: a genuine access violation must be captured
    with the Python frame that caused it."""
    import subprocess, sys, textwrap
    log = tmp_path / "crash.log"
    script = textwrap.dedent(f"""
        import sys, ctypes
        sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[2])!r})
        from mammon import crashlog
        crashlog.install_excepthook({str(log)!r})
        ctypes.string_at(0)
    """)
    subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    text = log.read_text(encoding="utf-8")
    assert "faulthandler armed" in text
    assert ("access violation" in text.lower()
            or "segmentation fault" in text.lower()), text[:400]
    assert "string_at" in text          # the frame that faulted


def test_qt_warnings_reach_the_crash_log(tmp_path):
    """Qt's own diagnostics are often the only readable statement of what went
    wrong before a hard crash, and in a windowed app they go to a stderr that
    does not exist."""
    from PyQt5.QtCore import qWarning, qInstallMessageHandler
    from mammon import crashlog
    log = tmp_path / "crash.log"
    try:
        assert crashlog.install_qt_message_handler(log) is True
        qWarning(b"QTableView::edit: index was invalid")
        text = log.read_text(encoding="utf-8")
        assert "Qt WARNING" in text
        assert "index was invalid" in text
    finally:
        qInstallMessageHandler(None)      # don't leak the handler into other tests


def test_qt_message_handler_never_raises_on_a_bad_path(tmp_path):
    from PyQt5.QtCore import qWarning, qInstallMessageHandler
    from mammon import crashlog
    try:
        crashlog.install_qt_message_handler(tmp_path / "no" / "such" / "dir" / "c.log")
        qWarning(b"this must not blow up")   # unwritable target
    finally:
        qInstallMessageHandler(None)
