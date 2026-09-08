"""PID-file based single-instance enforcement for the gateway daemon.

Mirrors Hermes's ``gateway/status.py`` pattern. One gateway per
``HERMES_HOME`` (here: per ``~/.freyja``). The PID file at
``~/.freyja/.gateway.pid`` holds the current daemon's process id; on
boot the daemon either acquires the lock (file empty / process dead /
``--replace`` requested) or refuses to start (file holds a live
foreign PID, no replace).

A "takeover marker" lets the new daemon politely terminate a running
predecessor without that predecessor's launchd restarting it: the
incoming daemon writes the marker file before sending SIGTERM, the
outgoing daemon checks for the marker in its shutdown handler and
exits 0 (which tells launchd "intentional, don't restart") instead of
the default non-zero (which would trigger ``KeepAlive``).
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def freyja_home() -> Path:
    """Resolve the Freyja home directory, creating it if missing."""
    home = Path(os.environ.get("FREYJA_HOME") or (Path.home() / ".freyja"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def pid_path() -> Path:
    return freyja_home() / ".gateway.pid"


def takeover_marker_path() -> Path:
    return freyja_home() / ".gateway.takeover"


def logs_dir() -> Path:
    p = freyja_home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def gateway_log_path() -> Path:
    return logs_dir() / "gateway.log"


def gateway_err_path() -> Path:
    return logs_dir() / "gateway.err"


# Cap for gateway.log / gateway.err. The gateway's stdout IS the log:
# launchd redirects it via StandardOutPath, and bridge.emit() writes
# every session event there (the desktop tails the file to mirror
# gateway-owned sessions). Streaming events dominate the volume —
# tool_input_delta alone was ~34% of lines in a 560 MB sample — so
# without a cap the file grows without bound.
GATEWAY_LOG_MAX_BYTES = 64 * 1024 * 1024
# How much of the tail to preserve into the `.1` archive on rotation.
GATEWAY_LOG_KEEP_BYTES = 8 * 1024 * 1024


def rotate_log_if_large(
    path: Path,
    *,
    max_bytes: int = GATEWAY_LOG_MAX_BYTES,
    keep_bytes: int = GATEWAY_LOG_KEEP_BYTES,
) -> bool:
    """Copytruncate ``path`` when it exceeds ``max_bytes``.

    Truncation in place is the only rotation that works here. launchd
    opened this file itself and holds the descriptor for the daemon's
    lifetime, so renaming it would leave every subsequent write going
    to the renamed inode and the fresh file would stay empty forever.
    Truncating keeps that descriptor valid: the redirect is O_APPEND,
    so the next write lands at the new end of file rather than leaving
    a sparse hole.

    The last ``keep_bytes`` are copied to ``<name>.1`` first, so a crash
    right before rotation is still diagnosable. Returns True if it
    rotated.
    """
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return False
        archive = path.with_suffix(path.suffix + ".1")
        with path.open("rb") as src:
            size = path.stat().st_size
            src.seek(max(0, size - keep_bytes))
            # Drop a partial first line so the archive starts clean.
            src.readline()
            tail = src.read()
        archive.write_bytes(tail)
        os.truncate(path, 0)
        return True
    except Exception:  # noqa: BLE001
        # Log hygiene must never take the daemon down.
        return False


def _process_alive(pid: int) -> bool:
    """Check whether ``pid`` is a live process we can signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        # ESRCH = no such process; EPERM = process exists but we can't
        # signal it (still counts as alive).
        return exc.errno == errno.EPERM
    return True


def get_running_pid() -> int | None:
    """Return the PID of a running gateway, or None if none."""
    path = pid_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        pid = int(raw)
    except (OSError, ValueError):
        return None
    if not _process_alive(pid):
        # Stale; clean up so future acquire calls don't see it.
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return pid


def write_pid(pid: int) -> None:
    pid_path().write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    try:
        pid_path().unlink()
    except OSError:
        pass


def write_takeover_marker(target_pid: int) -> None:
    """Tell ``target_pid`` that the SIGTERM coming next is a planned
    takeover, not a crash — so its shutdown handler exits 0 and
    launchd's ``KeepAlive=SuccessfulExit=false`` doesn't auto-restart
    it into a flap loop against the new instance."""
    takeover_marker_path().write_text(str(target_pid), encoding="utf-8")


def consume_takeover_marker() -> bool:
    """Read + clear the takeover marker. Returns True if the marker
    targeted us (so the shutdown handler should exit 0)."""
    path = takeover_marker_path()
    if not path.exists():
        return False
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    try:
        path.unlink()
    except OSError:
        pass
    try:
        target = int(raw)
    except ValueError:
        return False
    return target == os.getpid()


def terminate_pid(pid: int, *, force: bool = False, wait_seconds: float = 10.0) -> bool:
    """Send SIGTERM (or SIGKILL if ``force``) to ``pid``, waiting up
    to ``wait_seconds`` for it to die. Returns True if the process is
    gone by the deadline."""
    if not _process_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return True
        logger.warning("could not signal pid %d: %s", pid, exc)
        return False

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.1)
    return not _process_alive(pid)


def acquire_lock(*, replace: bool = False) -> bool:
    """Try to claim the gateway PID lock for the current process.

    Returns True on success; False if another live gateway already
    holds the lock and ``replace`` is False.

    When ``replace`` is True and an old daemon is running, we write a
    takeover marker addressed to that PID and send it SIGTERM. The old
    daemon's shutdown handler will see the marker and exit 0; launchd
    won't restart it. We wait up to 10s for it to actually exit, then
    write our own PID.
    """
    existing = get_running_pid()
    if existing is not None and existing != os.getpid():
        if not replace:
            logger.error(
                "gateway already running (pid %d) — use `freyja gateway stop` "
                "or pass --replace to take over",
                existing,
            )
            return False
        logger.info("taking over from existing gateway pid %d", existing)
        write_takeover_marker(existing)
        if not terminate_pid(existing, force=False, wait_seconds=10.0):
            logger.warning(
                "old gateway pid %d did not exit cleanly — sending SIGKILL",
                existing,
            )
            terminate_pid(existing, force=True, wait_seconds=2.0)

    write_pid(os.getpid())
    return True


def release_lock() -> None:
    """Clear the PID file if we own it."""
    existing = get_running_pid()
    if existing == os.getpid():
        clear_pid()
