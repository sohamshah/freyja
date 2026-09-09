"""External MCP server support for the Freyja bridge (stdio, v1 lifecycle).

See /docs in the design doc: config.py (catalog), connection.py (per-
server lifecycle state machine), proxy_tool.py (Tool-protocol wrappers),
manager.py (orchestration + registry integration + status surface),
commands.py (mcp_command IPC / /mcp chat handling), stdio_watchdog.py
(parent-death supervisor), pidfiles.py (stale-pid sweep).
"""

from bridge.mcp.config import McpServerSpec  # noqa: F401
from bridge.mcp.manager import McpManager  # noqa: F401

__all__ = ["McpManager", "McpServerSpec"]
