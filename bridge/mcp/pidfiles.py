"""PID-file bookkeeping for stdio MCP servers (design doc 4.3 layer 3).

Live layout: ``~/.freyja/mcp-run/<server>.pid`` — one JSON object per
server, written by the stdio watchdog at spawn::

    {"pid": <server pid>, "cmdline": "<server argv joined>",
     "owner_pid": <bridge pid>, "watchdog_pid": <watchdog pid>}

The run directory is always injectable (``McpManager(run_dir=...)``);
tests use tmp_path and the default is only applied by the live bridge
wiring, never by this module.

Sweep semantics (``sweep_stale_pidfiles``): a recorded pid is killed iff
  · the recorded owner bridge is dead (or is this process), AND
  · the pid is alive AND its current cmdline still matches the recorded
    one (guards against pid reuse).
Every examined file is removed: matched pids are killed first, stale
entries are simply dropped.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def pid_file_path(run_dir: Path | str, server: str) -> Path:
    return Path(run_dir) / f"{server}.pid"


def write_pid_file(
    path: Path | str,
    pid: int,
    cmdline: str,
    *,
    owner_pid: int | None = None,
    watchdog_pid: int | None = None,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"pid": int(pid), "cmdline": cmdline}
    if owner_pid is not None:
        record["owner_pid"] = int(owner_pid)
    if watchdog_pid is not None:
        record["watchdog_pid"] = int(watchdog_pid)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def read_pid_file(path: Path | str) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data


def remove_pid_file(path: Path | str) -> None:
    with contextlib.suppress(OSError):
        Path(path).unlink()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_cmdline(pid: int) -> str | None:
    """Current argv of *pid* as one string, or None if gone. Uses ``ps``
    (portable across macOS/Linux; /proc does not exist on macOS)."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or "").strip()
    return text or None


def _cmdline_matches(recorded: str, live: str) -> bool:
    recorded = " ".join(recorded.split())
    live = " ".join(live.split())
    if not recorded or not live:
        return False
    return recorded == live or recorded in live or live in recorded


def sweep_stale_pidfiles(run_dir: Path | str) -> list[int]:
    """Kill orphaned MCP server pids recorded under *run_dir*; remove
    every pid file examined. Returns the list of pids killed."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return []
    killed: list[int] = []
    for path in sorted(run_dir.glob("*.pid")):
        record = read_pid_file(path)
        if record is None:
            remove_pid_file(path)
            continue
        owner = record.get("owner_pid")
        if (
            isinstance(owner, int)
            and owner != os.getpid()
            and pid_alive(owner)
        ):
            # Another live bridge owns this server — not ours to sweep.
            continue
        pid = record["pid"]
        recorded_cmd = str(record.get("cmdline") or "")
        live_cmd = process_cmdline(pid)
        if live_cmd is not None and _cmdline_matches(recorded_cmd, live_cmd):
            logger.warning(
                "mcp sweep: killing stale server pid %d from %s (%s)",
                pid, path.name, recorded_cmd[:120],
            )
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGKILL)
            killed.append(pid)
            # Take the recorded watchdog with it if it survived.
            wd = record.get("watchdog_pid")
            if isinstance(wd, int) and wd != pid and pid_alive(wd):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(wd, signal.SIGKILL)
        remove_pid_file(path)
    return killed
