"""Desktop → daemon control channel.

The desktop renderer needs to send commands *to* the gateway daemon —
most urgently, ``permission_response`` so the operator can approve or
deny a ``permission_request`` from the desktop UI without us needing
to stand up a socket between Electron and Python. Both directions are
already file-coupled (the desktop tails the daemon's per-session
``.events.jsonl`` to see live activity), so we mirror that for the
write path:

  · Desktop appends one JSON command per line to
    ``~/.freyja/control/commands.jsonl``.
  · Daemon tails that file, parses each line, and dispatches the
    command. Last-read byte offset is persisted to
    ``~/.freyja/control/commands.offset`` so a daemon restart picks up
    exactly where it left off (no replay, no skipped commands).

Initial command type: ``permission_response``. The dispatch table is a
plain dict so adding ``cancel_turn``, ``set_permission_policy``, etc.
later is one line each.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def control_dir() -> Path:
    base = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    p = Path(base) / "control"
    p.mkdir(parents=True, exist_ok=True)
    return p


def commands_path() -> Path:
    return control_dir() / "commands.jsonl"


def commands_offset_path() -> Path:
    return control_dir() / "commands.offset"


CommandHandler = Callable[[dict[str, Any]], Any]


class ControlChannelReader:
    """Polls the command file and dispatches new lines.

    Use as::

        reader = ControlChannelReader()
        reader.register("permission_response", _on_permission_response)
        await reader.start()
        # ... reader runs as a background task ...
        await reader.stop()

    Polling is deliberately simple — 250ms cadence is well under
    operator perception latency. No inotify / FSEvents glue needed,
    and we stay portable across Linux + macOS without extra deps.
    """

    POLL_INTERVAL_SEC: float = 0.25

    def __init__(self) -> None:
        self._handlers: dict[str, CommandHandler] = {}
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        # In-memory offset cache. Synced to disk after each successful
        # batch read so a crash mid-batch replays at worst the current
        # batch, never older state.
        self._offset: int = 0

    def register(self, command_type: str, handler: CommandHandler) -> None:
        self._handlers[command_type] = handler

    async def start(self) -> None:
        # Ensure the file exists so the first read doesn't trip on
        # FileNotFoundError. Touch + chmod 0600 since this is a local
        # IPC channel that shouldn't be world-readable.
        path = commands_path()
        path.touch(exist_ok=True)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # Restore persisted offset (or jump to current EOF on first run
        # so we don't replay history from a long-lived control file).
        offset_path = commands_offset_path()
        if offset_path.exists():
            try:
                self._offset = int(offset_path.read_text().strip() or "0")
            except (ValueError, OSError):
                self._offset = 0
        else:
            # First-ever start: skip whatever historical lines exist
            # (the desktop is the only writer; nothing it wrote before
            # the daemon was alive can possibly be answerable).
            self._offset = path.stat().st_size
            try:
                offset_path.write_text(str(self._offset))
            except OSError:
                pass
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="control-channel")
        logger.info(
            "control channel started — tailing %s from offset %d",
            path, self._offset,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        logger.info("control channel stopped")

    async def _run_loop(self) -> None:
        path = commands_path()
        offset_path = commands_offset_path()
        try:
            while not self._stop_event.is_set():
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    # File deleted out from under us — recreate and
                    # reset offset on next iteration. Don't lose the
                    # tailer because of an external mtime sweep.
                    path.touch(exist_ok=True)
                    self._offset = 0
                    try:
                        offset_path.write_text("0")
                    except OSError:
                        pass
                    await self._sleep_short()
                    continue
                if size < self._offset:
                    # File was truncated (rotated by an operator,
                    # cleared in development). Reset to zero.
                    self._offset = 0
                if size > self._offset:
                    await self._drain(path, size)
                    try:
                        offset_path.write_text(str(self._offset))
                    except OSError:
                        pass
                await self._sleep_short()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("control channel loop crashed")
            raise

    async def _sleep_short(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop_event.wait(), timeout=self.POLL_INTERVAL_SEC,
            )
        except asyncio.TimeoutError:
            pass

    async def _drain(self, path: Path, size: int) -> None:
        # Read in a thread to avoid blocking the loop on a slow disk.
        def _read_chunk(off: int, end: int) -> tuple[int, bytes]:
            with path.open("rb") as fp:
                fp.seek(off)
                # Cap each batch to a sane upper bound so a runaway
                # writer can't pin the loop for tens of MB.
                chunk = fp.read(min(end - off, 1024 * 1024))
            return off + len(chunk), chunk

        new_offset, chunk = await asyncio.to_thread(_read_chunk, self._offset, size)
        # If the chunk doesn't end on a newline, keep the trailing
        # partial bytes for the next cycle (writer hadn't flushed the
        # newline yet).
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            # No complete lines yet — leave offset where it was.
            return
        complete = chunk[: last_nl + 1]
        self._offset += last_nl + 1
        # Round to whole UTF-8 lines and dispatch.
        for raw in complete.splitlines():
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("control channel: dropping non-JSON line: %r", line[:200])
                continue
            await self._dispatch(cmd)

    async def _dispatch(self, cmd: dict[str, Any]) -> None:
        ctype = str(cmd.get("type") or "")
        handler = self._handlers.get(ctype)
        if handler is None:
            logger.debug("control channel: no handler for %r — ignoring", ctype)
            return
        try:
            result = handler(cmd)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception("control channel handler %s raised", ctype)
