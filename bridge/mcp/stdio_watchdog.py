"""Parent-death supervisor for stdio MCP servers (design doc 4.3 layer 1).

The bridge wraps every stdio server spawn as::

    <python> stdio_watchdog.py --parent-pid <bridge pid> [--poll-interval S]
             [--pid-file P] -- <server command> <args...>

The watchdog:
  · spawns the real server in its OWN process group
    (``start_new_session=True``) with inherited stdio, so the MCP SDK's
    pipes pass straight through to the server;
  · optionally records the server's pid + argv in a pid file (consumed
    by the startup sweep in bridge/mcp/pidfiles.py);
  · polls the bridge pid (default every 2s) and SIGKILLs the server's
    process group the moment the bridge dies. Required because Electron
    can kill the bridge without SIGTERM reaching cleanup handlers and
    macOS has no PDEATHSIG;
  · forwards SIGTERM/SIGINT (what the MCP SDK sends on clean shutdown)
    as a group kill, then exits;
  · exits with the server's return code when the server exits on its own.

Deliberately stdlib-only and importable as a plain script: the child env
is allowlist-filtered (PATH/HOME/LANG + declared keys), so the watchdog
must not depend on the bridge package being importable.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

DEFAULT_POLL_INTERVAL_S = 2.0

# Absolute path to this file, used by wrap_with_watchdog so the child
# needs no PYTHONPATH / importable package.
WATCHDOG_SCRIPT = Path(__file__).resolve()


def wrap_with_watchdog(
    command: str,
    args: list[str],
    *,
    parent_pid: int,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    pid_file: Path | str | None = None,
    python: str | None = None,
) -> tuple[str, list[str]]:
    """Return (command, args) that runs *command args* under the watchdog."""
    wrapped = [
        str(WATCHDOG_SCRIPT),
        "--parent-pid", str(int(parent_pid)),
        "--poll-interval", str(poll_interval_s),
    ]
    if pid_file is not None:
        wrapped += ["--pid-file", str(pid_file)]
    wrapped += ["--", command, *args]
    return (python or sys.executable), wrapped


# ---------------------------------------------------------------------------
# Script body (no bridge imports below this line).
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_group(child: "subprocess.Popen[bytes]") -> None:
    """SIGKILL the server's process group (it leads its own group)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(child.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        child.wait(timeout=5)


def _write_pid_file(path: str, child_pid: int, argv: list[str], owner_pid: int) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "pid": child_pid,
                    "cmdline": " ".join(argv),
                    "owner_pid": owner_pid,
                    "watchdog_pid": os.getpid(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # pid files are best-effort; never take the server down


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP stdio parent-death watchdog")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)
    parser.add_argument("--pid-file", default=None)
    parser.add_argument("cmd", nargs=argparse.REMAINDER,
                        help="-- followed by the real server command")
    ns = parser.parse_args(argv)

    cmd = list(ns.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        sys.stderr.write("stdio_watchdog: no server command given\n")
        return 2

    # Inherit stdio: the MCP SDK's pipes must reach the real server.
    try:
        child = subprocess.Popen(cmd, start_new_session=True)
    except FileNotFoundError as exc:
        sys.stderr.write(f"stdio_watchdog: {exc}\n")
        return 127

    if ns.pid_file:
        _write_pid_file(ns.pid_file, child.pid, cmd, ns.parent_pid)

    def _on_signal(signum: int, _frame: object) -> None:
        # Clean shutdown from the SDK (SIGTERM) or operator (SIGINT):
        # take the whole server group down with us.
        _kill_group(child)
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    poll = max(0.05, float(ns.poll_interval))
    while True:
        try:
            # wait(timeout=...) wakes IMMEDIATELY on child exit (no
            # poll-interval lag on clean shutdown) and doubles as the
            # parent-poll cadence.
            rc = child.wait(timeout=poll)
        except subprocess.TimeoutExpired:
            rc = None
        if rc is not None:
            # Server exited on its own; sweep any grandchildren it left
            # in the group, then mirror its exit code.
            _kill_group(child)
            return rc
        if not _pid_alive(ns.parent_pid):
            _kill_group(child)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
