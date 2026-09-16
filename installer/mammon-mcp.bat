@echo off
rem Mammon's read-only MCP server over stdio. Register THIS file as the command
rem in an MCP client (Claude Desktop, Claude Code). Nothing here may print:
rem the client reads JSON-RPC frames from this process's stdout. With no --db it
rem serves the ledger Mammon itself would open.
"%~dp0python\python.exe" -B -m mammon.mcp_server %*
