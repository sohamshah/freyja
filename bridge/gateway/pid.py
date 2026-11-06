"""PID-file based single-instance enforcement for the gateway daemon.

Mirrors Hermes's ``gateway/status.py`` pattern. One gateway per
``HERMES_HOME`` (here: per ``~/.freyja``). The PID file at
``~/.freyja/.gateway.pid`` holds the current daemon's process id; on
boot the daemon either acquires the lock (file empty / process dead /
``--replace`` requested) or refuses to start (file holds a live
foreign PID, no replace).

The lock itself is an exclusive flock on ``~/.freyja/.gateway.lock``,
held for the daemon's life and let go by the kernel when it dies. The PID
file is what other processes read to find the gateway. Checked and then
written, the PID file alone let two daemons that started at the same
moment both take the lock.

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
from typing import Any

logger = logging.getLogger(__name__)


def freyja_home() -> Path:
    """Resolve the Freyja home directory, creating it if missing."""
    home = Path(os.environ.get("FREYJA_HOME") or (Path.home() / ".freyja"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def pid_path() -> Path:
    return freyja_home() / ".gateway.pid"


def gateway_lock_path() -> Path:
    return freyja_home() / ".gateway.lock"


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
    """Return the PID of a running gateway, or None if none. A reader
    leaves a dead gateway's PID file in place: a gateway taking over
    writes its own PID over it right after the reader read the old one,
    and a reader that deleted the file then took the new gateway's away.
    acquire_lock writes over a dead PID anyway."""
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
        return None
    return pid


# The process that took the lock in acquire_lock, until release_lock.
_lock_holder: int | None = None
# Its open gateway_lock_path, flocked, until release_lock.
_gateway_lock: Any = None


def holds_lock() -> bool:
    """True in the process that took the gateway lock and has not released
    it, whatever the PID file says now. Asked of the file, a gateway whose
    file was gone treated its own Slack sessions as copies and saved
    nothing."""
    return _lock_holder == os.getpid()


# The app's bridge (the Electron app's Python process) owns desktop
# sessions: it loads, runs and saves them, and reads the desktop control
# channel. It holds an exclusive flock on this file for its whole life, and
# the kernel lets go when it dies. Other processes ask desktop_bridge_runs.
_desktop_lock: Any = None


def desktop_lock_path() -> Path:
    return freyja_home() / ".desktop-bridge.lock"


def hold_desktop_lock() -> bool:
    """Take the app's bridge's lock for this process, if free. True when
    this process holds it."""
    global _desktop_lock
    if _desktop_lock is not None:
        return True
    import fcntl

    try:
        fp = desktop_lock_path().open("a")
    except OSError:
        return False
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fp.close()
        return False
    _desktop_lock = fp
    return True


def holds_desktop_lock() -> bool:
    """True in the app's bridge."""
    return _desktop_lock is not None


def desktop_bridge_runs() -> bool:
    """True while the app's bridge runs, in any process. The probe takes a
    shared lock for a moment, so it never blocks another probe."""
    if _desktop_lock is not None:
        return True
    import fcntl

    try:
        fp = desktop_lock_path().open("a")
    except OSError:
        return False
    with fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
    return False


def write_pid(pid: int) -> None:
    """Write the PID file whole: to a temp file, renamed over it. Written
    in place, the file was empty between the truncate and the write, and
    another process reading it then saw no gateway."""
    path = pid_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(str(pid), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


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


def _flock_gateway_lock(wait_seconds: float) -> Any:
    """The gateway lock file, open and flocked by this process, or None
    when another process holds it for ``wait_seconds``."""
    import fcntl

    try:
        fp = gateway_lock_path().open("a")
    except OSError:
        return None
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fp
        except OSError:
            if time.monotonic() >= deadline:
                fp.close()
                return None
            time.sleep(0.1)


def acquire_lock(*, replace: bool = False) -> bool:
    """Try to claim the gateway PID lock for the current process.

    Returns True on success; False if another live gateway already
    holds the lock and ``replace`` is False.

    When ``replace`` is True and an old daemon is running, we write a
    takeover marker addressed to that PID and send it SIGTERM. The old
    daemon's shutdown handler will see the marker and exit 0; launchd
    won't restart it. We wait up to 10s for it to actually exit, then
    write our own PID.

    The flock decides. A gateway that started at the same moment, and so
    is not in the PID file yet, holds it, and this one does not start.
    """
    global _lock_holder, _gateway_lock
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

    if _gateway_lock is None:
        # A gateway being replaced lets go as it exits.
        _gateway_lock = _flock_gateway_lock(5.0 if replace else 0.0)
        if _gateway_lock is None:
            logger.error(
                "another gateway holds %s — use `freyja gateway stop` or "
                "pass --replace to take over",
                gateway_lock_path(),
            )
            return False
    write_pid(os.getpid())
    _lock_holder = os.getpid()
    return True


def release_lock() -> None:
    """Clear the PID file if we own it, then let go of the flock. In that
    order, a gateway taking over writes its PID only after ours is gone."""
    global _lock_holder, _gateway_lock
    _lock_holder = None
    existing = get_running_pid()
    if existing == os.getpid():
        clear_pid()
    if _gateway_lock is not None:
        _gateway_lock.close()
        _gateway_lock = None
