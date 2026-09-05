"""Top-level crash handler: log otherwise-silent crashes to a file.

TWO kinds of crash, and they need different machinery:

* A Python exception escaping a Qt slot. ``sys.excepthook`` catches these and
  writes a normal traceback.
* A HARD abort inside Qt's C++ layer -- a widget used after Qt deleted it, an
  editor destroyed mid-commit. The interpreter never raises, so excepthook is
  never called and the app simply disappears with no message at all. Only
  :mod:`faulthandler` sees these; it writes a C-level dump naming the Python
  frame that was executing.

A crash that leaves NOTHING in this log is itself diagnostic: it means the fault
was below Python, which narrows it to object lifetime rather than logic.

PyQt aborts (or silently swallows) exceptions raised inside slots without
printing a usable trace, so an unexpected error can vanish leaving the user with
a dead window and nothing to diagnose from. Installing a :data:`sys.excepthook`
that appends the full traceback -- with a timestamp -- to a durable crash-log
file leaves a diagnosable trace, then chains to the previous hook so normal
console reporting still happens.

The writer is deliberately fail-safe: a failure while logging the crash must
never mask the original exception.
"""
from __future__ import annotations

import datetime as _dt
import faulthandler
import sys
import traceback
from pathlib import Path

__all__ = [
    "DEFAULT_CRASH_LOG",
    "_enable_faulthandler",
    "crash_log_path",
    "format_entry",
    "write_crash",
    "install_excepthook",
    "install_qt_message_handler",
]

DEFAULT_CRASH_LOG = "mammon_crash.log"


def crash_log_path(db_path=None) -> Path:
    """Where the crash log lives: next to the open database when known, else in
    the user's home directory. Anchoring it to the DB keeps the trace beside the
    data it belongs to (and CWD-independent, like the DB itself)."""
    if db_path:
        return Path(db_path).resolve().parent / DEFAULT_CRASH_LOG
    return Path.home() / DEFAULT_CRASH_LOG


def format_entry(exc_type, exc, tb, *, when=None) -> str:
    """A single crash-log record: a timestamped banner plus the full traceback."""
    stamp = (when or _dt.datetime.now()).isoformat(timespec="seconds")
    body = "".join(traceback.format_exception(exc_type, exc, tb))
    return f"\n===== Mammon crash {stamp} =====\n{body}"


def write_crash(path, exc_type, exc, tb, *, when=None) -> None:
    """Append one crash record to ``path`` (creating parent dirs as needed)."""
    entry = format_entry(exc_type, exc, tb, when=when)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(entry)


_FAULT_FILE = None          # kept alive: faulthandler writes to the raw fd


def _enable_faulthandler(path) -> bool:
    """Also capture crashes BELOW Python (segfault / abort in Qt's C++).

    ``sys.excepthook`` cannot see those -- the process dies without raising, so
    the app vanishes leaving no entry at all. faulthandler writes a stack dump
    naming the Python frame that was running, which is usually enough to place a
    Qt lifetime bug. The file object is held in a module global because
    faulthandler keeps only the file DESCRIPTOR: letting it be garbage collected
    would close the fd and silently disable the dump."""
    global _FAULT_FILE
    try:
        _FAULT_FILE = open(Path(path), "a", buffering=1, encoding="utf-8")
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        _FAULT_FILE.write(f"\n===== faulthandler armed {stamp} =====\n")
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
        return True
    except Exception:       # pragma: no cover - diagnostics must never block startup
        _FAULT_FILE = None
        return False


def install_qt_message_handler(path) -> bool:
    """Route Qt's OWN diagnostics into the crash log.

    Qt writes warnings to stderr, which a windowed app does not have -- so the
    warning it emits immediately before misbehaving is lost. Those messages
    ("index out of range", "QWidget: Cannot ...", a fatal about a deleted
    object) are frequently the only human-readable statement of what went wrong,
    because a hard access violation leaves no Python frame worth reading.

    Safe to call more than once; never raises."""
    try:
        from PyQt5.QtCore import (QtCriticalMsg, QtFatalMsg, QtWarningMsg,
                                  qInstallMessageHandler)
    except Exception:       # pragma: no cover - Qt not present (headless tooling)
        return False

    names = {QtWarningMsg: "WARNING", QtCriticalMsg: "CRITICAL", QtFatalMsg: "FATAL"}
    target = Path(path)

    def _handler(mode, context, message):
        if mode not in names:
            return                      # debug/info chatter is not worth keeping
        try:
            stamp = _dt.datetime.now().isoformat(timespec="seconds")
            where = ""
            if getattr(context, "file", None):
                where = f" [{context.file}:{context.line}]"
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== Qt {names[mode]} {stamp} ====={where}\n"
                         f"{message}\n")
        except Exception:   # pragma: no cover - diagnostics must never crash
            pass

    qInstallMessageHandler(_handler)
    return True


def install_excepthook(path, *, chain=True):
    """Route uncaught exceptions to the crash log at ``path``, then chain.

    Returns the installed hook (whose ``_previous`` / ``_crash_log`` attributes
    the tests inspect). Logging failures are swallowed so they can never mask the
    exception that was actually being reported."""
    path = Path(path)
    previous = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            write_crash(path, exc_type, exc, tb)
        except Exception:  # never let a logging failure hide the real crash
            pass
        if chain and previous is not None:
            previous(exc_type, exc, tb)

    _hook._previous = previous
    _hook._crash_log = path
    _enable_faulthandler(path)
    sys.excepthook = _hook
    return _hook
