"""Freyja MCP server — runs as a subprocess of the harness CLI.

Spawned by Claude Code (via `--mcp-config`) or Codex (via `-c
mcp_servers.freyja.command=...`). Exposes Freyja-specific tools to the
harness over the standard MCP stdio JSON-RPC 2.0 protocol.

This file is a THIN PROXY: every tools/call is forwarded over a Unix
socket back to the parent Freyja bridge, which holds all the session
state, native bindings, and event-bus context needed to actually
execute the tool. The path to the per-session socket is delivered in
the FREYJA_BRIDGE_SOCKET env var when the bridge spawns us.

Pure stdlib: json, socket, sys, os, signal, threading. Bundled into the
.app the same way every other bridge module is, but designed to run
with whatever Python the harness's PATH resolves first if needed.

MCP protocol implemented (subset sufficient for tool-only servers):

  ← initialize { protocolVersion, capabilities, clientInfo }
  → { protocolVersion, capabilities: { tools: {} }, serverInfo }
  ← notifications/initialized
  ← tools/list
  → { tools: [ {name, description, inputSchema}, ... ] }
  ← tools/call { name, arguments }
  → { content: [...], isError: bool }
  ← ping
  → {}
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from typing import Any, Optional

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "freyja", "version": "0.1.0"}

# Tool catalog. We expose Freyja-specific capabilities the harness
# doesn't already have. Coordinates are in the api_dims space that the
# parent bridge resolves on the first screenshot — same convention as
# Freyja's native sessions.
TOOL_CATALOG: list[dict[str, Any]] = [
    {
        "name": "freyja_screenshot",
        "description": (
            "Capture the user's desktop screen and return a PNG image. "
            "Use this to see what's on screen before clicking or typing. "
            "Coordinates returned by other tools (and accepted by click "
            "/ type_text / scroll) are pixels in THIS image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "integer",
                    "description": "Optional CGWindowID from freyja_list_windows()",
                },
                "display_id": {
                    "type": "integer",
                    "description": "Optional display id from freyja_list_displays()",
                },
            },
        },
    },
    {
        "name": "freyja_click",
        "description": (
            "Click at an absolute screen coordinate (in api_dims space). "
            "Screenshot first to find the right coordinate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                },
                "double": {"type": "boolean", "default": False},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "freyja_type_text",
        "description": (
            "Type text into whatever has keyboard focus. "
            "Focus the target first by clicking into it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "freyja_press_key",
        "description": (
            "Press a named key, optionally with modifiers. "
            "Examples: 'enter', 'tab', 'escape', 'cmd+c', 'cmd+shift+t'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key name or modifier+key combination",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "freyja_scroll",
        "description": (
            "Scroll by (dx, dy) clicks at a screen point. "
            "Positive dy = down, positive dx = right."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "dx": {"type": "integer", "default": 0},
                "dy": {"type": "integer", "default": 0},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "freyja_list_windows",
        "description": (
            "List every visible window on screen: pid, app name, title, "
            "window_id, bounds. Use the window_id with freyja_screenshot "
            "to capture a single app, or freyja_focus_window to bring "
            "it forward."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "freyja_focus_window",
        "description": (
            "Bring an application or window to the front so subsequent "
            "type_text / press_key land in it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_id": {"type": "integer"},
                "app_name": {"type": "string"},
            },
        },
    },
]


# ────────────────────────────────────────────────────────────────────
# Parent-bridge socket client
# ────────────────────────────────────────────────────────────────────


class BridgeSocketClient:
    """Synchronous wrapper around the parent-bridge Unix socket.

    Thread-safe — we serialize requests behind a lock so multiple MCP
    tool calls (the harness may parallelize) don't interleave responses
    on the socket."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._next_id = 0
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._buf = b""

    def _ensure(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(None)
        s.connect(self._path)
        self._sock = s
        return s

    def call(self, name: str, arguments: dict) -> dict:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            sock = self._ensure()
            payload = {
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                sock.sendall(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Reconnect once
                self._sock = None
                sock = self._ensure()
                sock.sendall(data)
            return self._read_response(sock)

    def _read_response(self, sock: socket.socket) -> dict:
        while True:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                try:
                    return json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"bridge sent invalid JSON: {exc}")
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("bridge socket closed unexpectedly")
            self._buf += chunk

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None


# ────────────────────────────────────────────────────────────────────
# MCP server (stdio JSON-RPC)
# ────────────────────────────────────────────────────────────────────


def _send(payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stdout.write(data)
    sys.stdout.flush()


def _respond_result(req_id: Any, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _respond_error(req_id: Any, code: int, message: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }
    )


def _handle_initialize(req_id: Any, _params: dict) -> None:
    _respond_result(
        req_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        },
    )


def _handle_tools_list(req_id: Any, _params: dict) -> None:
    _respond_result(req_id, {"tools": TOOL_CATALOG})


def _handle_tools_call(
    req_id: Any,
    params: dict,
    bridge: Optional[BridgeSocketClient],
) -> None:
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}

    if bridge is None:
        _respond_error(
            req_id,
            -32603,
            (
                "FREYJA_BRIDGE_SOCKET is not set; the Freyja MCP server "
                "needs to be spawned by the Freyja bridge for tool dispatch."
            ),
        )
        return

    try:
        resp = bridge.call(name, arguments)
    except Exception as exc:
        _respond_error(req_id, -32603, f"bridge call failed: {exc}")
        return

    if "error" in resp:
        # Bridge rejected the call — surface as a successful MCP response
        # with isError:true so the harness can react to it rather than
        # treating it as a protocol error.
        _respond_result(
            req_id,
            {
                "content": [
                    {"type": "text", "text": str(resp.get("error") or "")}
                ],
                "isError": True,
            },
        )
        return

    result = resp.get("result") or {}
    # Bridge dispatcher returns the MCP-shaped content already. If it
    # returned something else, wrap.
    if isinstance(result, dict) and "content" in result:
        _respond_result(req_id, result)
    else:
        _respond_result(
            req_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                ],
                "isError": False,
            },
        )


def main() -> int:
    socket_path = os.environ.get("FREYJA_BRIDGE_SOCKET", "").strip()
    bridge: Optional[BridgeSocketClient] = (
        BridgeSocketClient(socket_path) if socket_path else None
    )

    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {}

            if method == "initialize":
                _handle_initialize(req_id, params)
            elif method == "notifications/initialized":
                # No response for notifications.
                continue
            elif method == "tools/list":
                _handle_tools_list(req_id, params)
            elif method == "tools/call":
                _handle_tools_call(req_id, params, bridge)
            elif method == "ping":
                _respond_result(req_id, {})
            elif method == "shutdown":
                break
            else:
                # Methods we don't implement — return method-not-found
                # for requests (with id), silently ignore notifications.
                if req_id is not None:
                    _respond_error(req_id, -32601, f"method not implemented: {method}")
    finally:
        if bridge is not None:
            bridge.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
