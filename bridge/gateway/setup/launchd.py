"""launchd plist install / uninstall / start / stop for the gateway.

macOS-only. Generates ``~/Library/LaunchAgents/co.freyja.gateway.plist``
with ``RunAtLoad=true`` + ``KeepAlive.SuccessfulExit=false`` (restart
unless the daemon exited cleanly) so the gateway starts at login and
survives crashes.

Linux / Windows installers would live alongside in their own modules
(systemd / Task Scheduler respectively) and the CLI would dispatch to
the right one based on ``sys.platform``. v1 ships darwin only.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any  # noqa: F401  (re-exported via build_plist signature)

from bridge.gateway.pid import (
    freyja_home,
    gateway_err_path,
    gateway_log_path,
    get_running_pid,
)


LAUNCH_LABEL = "co.freyja.gateway"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def _resolve_freyja_binary() -> str:
    """Find the ``freyja`` console script for the launchd plist.

    Search order:
      1. Beside the current Python interpreter (most reliable in a
         venv — ``sys.executable`` is ``.venv/bin/python``, the
         console script lives in the same dir)
      2. ``shutil.which`` (PATH lookup)
      3. Fallback: ``sys.executable -m bridge.gateway.cli``

    The fallback is robust but ugly in the plist; prefer the binary
    when it exists so users see a clean ProgramArguments entry.
    """
    interp = Path(sys.executable)
    sibling = interp.with_name("freyja")
    if sibling.exists() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("freyja")
    if found:
        return found
    return f"{sys.executable} -m bridge.gateway.cli"


def build_plist(
    *,
    program_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the plist content as a Python dict (plistlib-compatible)."""
    if program_args is None:
        binary = _resolve_freyja_binary()
        # Split in case the binary is a "python -m foo" fallback.
        program_args = binary.split() + ["gateway", "run"]

    env: dict[str, str] = {
        "HOME": str(Path.home()),
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "FREYJA_HOME": str(freyja_home()),
    }
    # Carry through any LLM provider keys we know about so the daemon
    # has the same provider access the desktop bridge has. The .env
    # file is also re-loaded inside the process for additional keys.
    for k in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "FIREWORKS_API_KEY",
        "CEREBRAS_API_KEY",
    ):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if extra_env:
        env.update(extra_env)

    return {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        # Restart unless the process exited cleanly (exit code 0).
        # Mirrors systemd Restart=on-failure semantics. Combined with
        # the takeover marker in pid.py, planned restarts exit 0 and
        # crashes/kills auto-respawn.
        "KeepAlive": {
            "SuccessfulExit": False,
        },
        "StandardOutPath": str(gateway_log_path()),
        "StandardErrorPath": str(gateway_err_path()),
        "EnvironmentVariables": env,
        # ProcessType=Interactive so it doesn't get throttled by App Nap.
        "ProcessType": "Interactive",
    }


def write_plist() -> Path:
    """Write the plist to LaunchAgents/. Returns the path."""
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = build_plist()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(data, f, sort_keys=False)
    tmp.replace(path)
    return path


def install() -> Path:
    """Write the plist + load it. Returns the plist path."""
    path = write_plist()
    # `launchctl bootstrap gui/<uid> <plist>` is the modern API; the
    # older `launchctl load` still works on macOS 11+ and is simpler
    # to call. Fall back if bootstrap fails (e.g. already loaded).
    unload(silent=True)  # idempotent — remove any prior version first
    subprocess.run(
        ["launchctl", "load", "-w", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def unload(*, silent: bool = False) -> bool:
    """Unload the plist (stops the daemon)."""
    if not plist_path().exists():
        return False
    result = subprocess.run(
        ["launchctl", "unload", "-w", str(plist_path())],
        capture_output=True,
    )
    if result.returncode != 0 and not silent:
        return False
    return result.returncode == 0


def uninstall() -> bool:
    """Unload + delete the plist."""
    unload(silent=True)
    path = plist_path()
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return True


def is_installed() -> bool:
    return plist_path().exists()


def start_via_launchctl() -> bool:
    """`launchctl start` — kicks an already-loaded plist."""
    if not is_installed():
        return False
    result = subprocess.run(
        ["launchctl", "start", LAUNCH_LABEL],
        capture_output=True,
    )
    return result.returncode == 0


def stop_via_launchctl() -> bool:
    """`launchctl stop` — graceful SIGTERM to the daemon."""
    if not is_installed():
        return False
    result = subprocess.run(
        ["launchctl", "stop", LAUNCH_LABEL],
        capture_output=True,
    )
    return result.returncode == 0


def status() -> dict[str, Any]:
    """Snapshot of installed-ness + running pid."""
    return {
        "installed": is_installed(),
        "plist_path": str(plist_path()),
        "running_pid": get_running_pid(),
        "log_path": str(gateway_log_path()),
        "err_path": str(gateway_err_path()),
    }
