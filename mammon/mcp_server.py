"""mammon.mcp_server -- expose the ledger to an LLM over MCP (roadmap item 3).

    python -m mammon.mcp_server                      # the app's default database, stdio
    python -m mammon.mcp_server --db path\\to\\ledger.db
    python -m mammon.mcp_server --transport streamable-http --port 8765

The server binds the read-only tool surface in :mod:`mammon.mcp_tools` to the
Model Context Protocol through the ``mcp`` SDK, which is imported only here,
so the application itself keeps no dependency on it. Two properties are
load-bearing:

* **The connection cannot write.** ``PRAGMA query_only`` is set on it, so a
  bug in a tool -- or a SELECT that turns out not to be one -- fails with a
  read-only error instead of touching the ledger. There is exactly one writer
  of transaction rows and this process is not it. When the LLM should CHANGE
  something, the intended path is a proposal into the review queue that the
  user accepts in the app, not a write from here.
* **The schema must match.** A database older than this code needs migrating,
  which only the app does (deliberately: nothing that opens a real ledger by
  accident may migrate it); a newer one belongs to a newer app. Either way
  the server refuses to start rather than read a shape it does not know.

Local models: stdio is what desktop MCP hosts speak (an MCP-capable local
client such as LM Studio or Open WebUI launches this command and talks to a
model on the same machine, so no row leaves it). The streamable-HTTP
transport is for clients that connect over a local port instead. Whichever
model is on the other end is the user's choice; the tools return aggregates
first and never return account numbers, so what the model sees stays small.
"""
from __future__ import annotations

import argparse
import functools
import inspect
import sqlite3
import sys
import typing
from typing import Optional

from mammon import db, mcp_tools


class SchemaMismatch(RuntimeError):
    """The database's schema version is not the one this code was built for."""


def open_readonly(path: str) -> sqlite3.Connection:
    """A connection that can only read. Refuses a database whose schema is
    older (open it in Mammon first to migrate) or newer than this code."""
    conn = db.connect(path)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < db.SCHEMA_VERSION:
        conn.close()
        raise SchemaMismatch(
            f"{path} is at schema {version}, this code expects {db.SCHEMA_VERSION}: "
            "open it in Mammon once to migrate it, then start the server")
    if version > db.SCHEMA_VERSION:
        conn.close()
        raise SchemaMismatch(
            f"{path} is at schema {version}, newer than this code ({db.SCHEMA_VERSION}); "
            "update Mammon")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _bind(fn, conn):
    """A copy of ``fn`` with ``conn`` bound and dropped from the signature, so
    the MCP SDK builds the tool's input schema from the remaining parameters.
    Domain errors come back as a plain message the model can act on rather
    than a traceback."""
    sig = inspect.signature(fn)
    # mcp_tools postpones its annotations (PEP 563), so resolve them HERE, in
    # the tool module's own namespace, and hand the SDK real types: a string
    # annotation on a function defined in another module cannot be evaluated
    # by pydantic and fails schema generation.
    hints = typing.get_type_hints(fn)
    hints.pop("conn", None)
    params = [p.replace(annotation=hints.get(p.name, p.annotation))
              for p in list(sig.parameters.values())[1:]]

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(conn, *args, **kwargs)
        except (ValueError, LookupError, KeyError, sqlite3.Error) as exc:
            return {"error": str(exc)}

    wrapper.__signature__ = sig.replace(
        parameters=params, return_annotation=hints.get("return", sig.return_annotation))
    wrapper.__annotations__ = hints
    del wrapper.__wrapped__          # keep inspect from following back to fn's signature
    return wrapper


def build_server(conn: sqlite3.Connection, name: str = "mammon", **kwargs):
    """The MCP server object with every tool in :data:`mcp_tools.TOOLS` bound
    to ``conn``. Imports the SDK here so the app never needs it."""
    from mcp.server.fastmcp import FastMCP
    server = FastMCP(name, instructions=mcp_tools.INSTRUCTIONS, **kwargs)
    for tool_name, fn in mcp_tools.TOOLS.items():
        server.add_tool(_bind(fn, conn), name=tool_name,
                        description=inspect.getdoc(fn) or tool_name)
    return server


def default_db_path() -> str:
    from mammon.app import _resolve_db
    return _resolve_db(None)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m mammon.mcp_server",
        description="Serve the Mammon ledger to an LLM over MCP (read-only).")
    ap.add_argument("--db", help="database file (default: the app's own)")
    ap.add_argument("--transport", choices=("stdio", "streamable-http", "sse"),
                    default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    path = args.db or default_db_path()
    try:
        conn = open_readonly(path)
    except SchemaMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"cannot open {path}: {exc}", file=sys.stderr)
        return 2
    server = build_server(conn, host=args.host, port=args.port)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":     # pragma: no cover - CLI entry
    sys.exit(main())
