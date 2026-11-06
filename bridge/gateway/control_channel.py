"""Desktop ↔ daemon control channels.

The desktop renderer needs to send commands *to* the gateway daemon —
most urgently, ``permission_response`` so the operator can approve or
deny a ``permission_request`` from the desktop UI without us needing
to stand up a socket between Electron and Python. The read path is
already file-coupled (the desktop tails ``~/.freyja/logs/gateway.log``,
where the daemon writes every event, to see live activity), so we
mirror that for the write path:

  · Desktop appends one JSON command per line to
    ``~/.freyja/control/commands.jsonl``.
  · Daemon tails that file, parses each line, and dispatches the
    command. Last-read byte offset is persisted to
    ``~/.freyja/control/commands.offset`` so a daemon restart picks up
    exactly where it left off (no replay, no skipped commands).

Each file has EXACTLY ONE reader. ``commands.jsonl`` is read by the
process that owns the gateway PID lock (see ``gateway/pid.py``) —
a second reader sharing the offset file silently steals commands
(observed: the scheduler daemon consuming ``skill_candidate_resolve``
commands meant for the gateway and failing with ``not_found``).

``desktop-commands.jsonl`` is the reverse direction: daemon → desktop
bridge. The desktop bridge process tails it (its own offset file) so
gateway-side agents can push ``talk_deliver`` messages to sessions
that live in the desktop process.

Command types today: ``permission_response``, ``set_permission_policy``,
``skill_candidate_resolve``, ``skill_learn_this``, ``talk_deliver``,
``artifact_note``, ``cancel_turn``, ``scheduler_fire``, and the
kanban board's commands (``kanban_operator_create`` and the rest).
The dispatch table is a plain dict so adding more is one line each.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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


def desktop_commands_path() -> Path:
    """Daemon → desktop direction. Read only by the desktop bridge."""
    return control_dir() / "desktop-commands.jsonl"


def desktop_commands_offset_path() -> Path:
    return control_dir() / "desktop-commands.offset"


def append_command(cmd: dict[str, Any], *, path: Path | None = None) -> None:
    """Append one command line to a control file.

    Single ``write()`` of a full line on an O_APPEND handle — atomic for
    the line sizes we send. Any process may write; only the file's one
    designated reader consumes.
    """
    dest = path or commands_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(cmd, ensure_ascii=False)
    with dest.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


# The reader takes at most this much per read and dispatches whole lines
# only, so a longer line is skipped unread (see ControlChannelReader._drain).
MAX_LINE_BYTES = 1024 * 1024

# Where a skipped line names its command and session (see _report_skipped).
_HEAD_FIELD = re.compile(rb'"(type|sessionId|to)"\s*:\s*"([^"\\]{1,200})"')


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

    def __init__(
        self,
        *,
        commands_file: Path | None = None,
        offset_file: Path | None = None,
    ) -> None:
        self._handlers: dict[str, CommandHandler] = {}
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        # Which file pair this reader tails. Defaults to the desktop →
        # daemon direction; the desktop bridge passes the
        # desktop-commands pair for the reverse direction.
        self._commands_file: Path = commands_file or commands_path()
        self._offset_file: Path = offset_file or commands_offset_path()
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
        path = self._commands_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # Restore persisted offset (or jump to current EOF on first run
        # so we don't replay history from a long-lived control file).
        offset_path = self._offset_file
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
        path = self._commands_file
        offset_path = self._offset_file
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
        def _read_chunk(off: int, end: int) -> tuple[bytes, int]:
            """The next batch from ``off``, and the length of its first line
            when that line is longer than MAX_LINE_BYTES (0 when it is not,
            -1 when its end has not been written yet)."""
            with path.open("rb") as fp:
                fp.seek(off)
                # Cap each batch to a sane upper bound so a runaway
                # writer can't pin the loop for tens of MB.
                chunk = fp.read(min(end - off, MAX_LINE_BYTES))
                if len(chunk) < MAX_LINE_BYTES or b"\n" in chunk:
                    return chunk, 0
                # A batch with no newline: the line goes on past it. Find
                # its end a batch at a time, never holding all of it.
                length = len(chunk)
                while True:
                    block = fp.read(min(end - off - length, MAX_LINE_BYTES))
                    if not block:
                        return chunk, -1
                    nl = block.find(b"\n")
                    if nl != -1:
                        return chunk, length + nl + 1
                    length += len(block)

        chunk, too_long = await asyncio.to_thread(_read_chunk, self._offset, size)
        if too_long:
            # Skip the line, so the commands after it still run. One that
            # is still being written is waited for: the offset (saved by the
            # loop) stays at its start until its end is known, so a restart
            # never resumes inside it.
            if too_long > 0:
                self._offset += too_long
                self._report_skipped(chunk, too_long)
            return
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

    def _report_skipped(self, head: bytes, length: int) -> None:
        """Log a skipped line, and tell the session it names: its command
        never ran. The desktop only shows gateway events that name a
        session, so one that names none is only logged."""
        fields: dict[str, str] = {}
        for key, value in _HEAD_FIELD.findall(head[:4096]):
            fields.setdefault(key.decode(), value.decode("utf-8", errors="replace"))
        ctype = fields.get("type") or "unknown"
        sid = fields.get("sessionId") or fields.get("to") or ""
        logger.warning(
            "control channel: skipped a %s command of %d bytes for %s (lines over %d bytes are not read)",
            ctype, length, sid or "no session", MAX_LINE_BYTES,
        )
        if not sid:
            return
        try:
            from bridge.freyja_bridge import emit

            emit(
                {
                    "type": "system_event",
                    "sessionId": sid,
                    "subtype": "control_command_skipped",
                    "message": (
                        f"A {ctype} command for this session was not run: it was "
                        f"{length / MAX_LINE_BYTES:.1f} MiB, and the control channel "
                        "reads lines up to 1 MiB."
                    ),
                    "details": {"command": ctype, "bytes": length, "chatVisible": True},
                }
            )
        except Exception:  # noqa: BLE001
            logger.debug("control channel: skip notice failed", exc_info=True)

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
