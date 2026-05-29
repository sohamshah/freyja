"""Unix socket server that the harness's MCP subprocess connects to.

When a Freyja session uses an external harness (claude, codex), we spawn
an MCP server subprocess alongside the harness. That subprocess can't
reach into the bridge's process directly — it talks back over a
per-session Unix socket served by this module.

Wire protocol (newline-delimited JSON, both directions):

  Request:  {"id": <int>, "method": "tools/call", "params": {"name": ..., "arguments": {...}}}
  Response: {"id": <int>, "result": {"content": [{"type":"text","text":"..."}], "isError": false}}
  Error:    {"id": <int>, "error": "message"}

Dispatch is delegated to the calling _BridgeSession via a callable so we
don't import any heavy tool modules at startup — the bridge wires its
own session-aware dispatcher when it starts the socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Dispatcher: takes (tool_name, arguments_dict) → returns a result dict
# with keys {content: [...], isError: bool}. The MCP server projects
# this to the wire MCP shape.
ToolDispatcher = Callable[[str, dict], Awaitable[dict]]


def socket_path_for(session_id: str) -> str:
    """Per-session socket path. Limited to ~104 chars on macOS; we slice
    the session id to fit comfortably."""
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:40]
    return f"/tmp/freyja-mcp-{safe}.sock"


class HarnessToolSocketServer:
    """Per-session Unix socket server. One instance per harness session."""

    def __init__(
        self,
        *,
        session_id: str,
        dispatcher: ToolDispatcher,
    ) -> None:
        self._session_id = session_id
        self._dispatcher = dispatcher
        self._path = socket_path_for(session_id)
        self._server: Optional[asyncio.AbstractServer] = None
        self._connections: set[asyncio.Task] = set()

    @property
    def socket_path(self) -> str:
        return self._path

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start(self) -> str:
        """Start listening. Returns the absolute socket path."""
        if self._server is not None:
            return self._path
        # Stale socket from a prior crash will refuse bind; remove it.
        try:
            if os.path.exists(self._path):
                os.unlink(self._path)
        except OSError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._path,
        )
        # Restrict to the owner — the subprocess we spawn is the only
        # legitimate client, and macOS doesn't enforce socket permissions
        # the way Linux does, so this is best-effort.
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        logger.info(
            "harness tool socket listening at %s for session=%s",
            self._path,
            self._session_id,
        )
        return self._path

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:
            pass
        self._server = None
        # Cancel any in-flight connection handlers.
        for task in list(self._connections):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._connections.clear()
        try:
            if os.path.exists(self._path):
                os.unlink(self._path)
        except OSError:
            pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    logger.warning("harness socket bad json: %s", exc)
                    continue
                req_id = req.get("id")
                method = str(req.get("method") or "")
                params = req.get("params") or {}
                try:
                    if method == "tools/call":
                        name = str(params.get("name") or "")
                        args = params.get("arguments") or {}
                        if not isinstance(args, dict):
                            args = {}
                        result = await self._dispatcher(name, args)
                        await self._respond(writer, req_id, result=result)
                    elif method == "ping":
                        await self._respond(writer, req_id, result={"ok": True})
                    else:
                        await self._respond(
                            writer,
                            req_id,
                            error=f"unknown method: {method}",
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("harness socket dispatch raised")
                    await self._respond(writer, req_id, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("harness socket client handler crashed")
        finally:
            if task is not None:
                self._connections.discard(task)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        req_id: Any,
        *,
        result: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> None:
        payload: dict[str, Any] = {"id": req_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result or {}
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            writer.write(data)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            # Client gone; nothing to do.
            pass
