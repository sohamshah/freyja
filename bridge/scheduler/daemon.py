"""macOS LaunchAgent management for the Freyja scheduler daemon.

The daemon is the same ``bridge/freyja_bridge.py`` process, started in
``--headless --scheduler-only`` mode. This means jobs fire even when the
Electron desktop app is closed. The LaunchAgent is installed
automatically on first durable-job creation (per user spec: ease of use
trumps install ceremony), and uninstalled when the user explicitly asks.

Layout:

  ~/Library/LaunchAgents/com.freyja.scheduler.plist
      LaunchAgent definition. RunAtLoad=true, KeepAlive=true.

  ~/Library/Application Support/Freyja/scheduler-launcher
      Shell shim that resolves the current Python bundle + bridge
      script path. Decouples the .plist from where Freyja.app is
      installed — reinstalling Freyja to a new folder keeps the
      LaunchAgent valid.

  ~/.freyja/.locks/.bridge.lock
      Advisory single-writer lock. The Electron-attached bridge holds
      this while running; the daemon backs off when it's held, taking
      over only when the user closes the app.

Public surface:

  ensure_daemon_installed(reason)  — write plist + shim; load with
                                     launchctl. Idempotent. Used by
                                     the auto-install hook on first
                                     durable job.
  uninstall_daemon()              — stop + unload + delete plist.
  daemon_status()                 — installed?, running?, pid?, last_tick
  on_app_quit_handoff()           — called by Electron on quit to
                                     hand control to the daemon.
  on_app_start_takeover()         — called by Electron on start to
                                     stop the daemon and reclaim
                                     the bridge lock.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("freyja.scheduler.daemon")


LAUNCH_AGENT_LABEL = "com.freyja.scheduler"


def is_supported_platform() -> bool:
    """macOS only for now. Other platforms get the in-process scheduler
    but no background daemon — durable jobs only fire while the app
    is open."""
    return platform.system() == "Darwin"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return launch_agents_dir() / f"{LAUNCH_AGENT_LABEL}.plist"


def app_support_dir() -> Path:
    p = Path.home() / "Library" / "Application Support" / "Freyja"
    p.mkdir(parents=True, exist_ok=True)
    return p


def shim_path() -> Path:
    return app_support_dir() / "scheduler-launcher"


def freyja_home() -> Path:
    base = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def daemon_log_path() -> Path:
    p = freyja_home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p / "daemon.log"


def daemon_pid_path() -> Path:
    return freyja_home() / ".scheduler-daemon.pid"


def bridge_lock_path() -> Path:
    p = freyja_home() / ".locks"
    p.mkdir(parents=True, exist_ok=True)
    return p / ".bridge.lock"


# ─── Bridge / Python resolution ────────────────────────────────────────


def resolve_bridge_invocation() -> tuple[str, list[str]]:
    """Return ``(executable, args)`` to launch the bridge in headless
    scheduler-only mode. We resolve at install time so a fresh
    Freyja.app reinstall doesn't break the .plist.

    Three resolution paths in order:

      1. ``FREYJA_BRIDGE_INVOKE`` environment override — useful for
         packagers that ship their own launcher.
      2. The packaged app bundle's python + bridge — when running
         inside ``/Applications/Freyja.app``.
      3. ``sys.executable`` + this file's grandparent ``freyja_bridge.py``
         — for dev installs.
    """
    override = os.environ.get("FREYJA_BRIDGE_INVOKE")
    if override:
        parts = override.split()
        return parts[0], parts[1:]

    # Try the app bundle first.
    candidates = [
        Path("/Applications/Freyja.app/Contents/Resources/python-bundle/bin/python3"),
        Path("/Applications/Freyja.app/Contents/Resources/python-bundle/bin/python"),
    ]
    bundle_bridge = Path("/Applications/Freyja.app/Contents/Resources/bridge/freyja_bridge.py")
    for c in candidates:
        if c.exists() and bundle_bridge.exists():
            return str(c), [str(bundle_bridge), "--headless", "--scheduler-only"]

    # Dev install fallback.
    py = sys.executable
    here = Path(__file__).resolve()
    bridge_py = here.parents[2] / "freyja_bridge.py"
    if bridge_py.exists():
        return py, [str(bridge_py), "--headless", "--scheduler-only"]

    raise RuntimeError(
        "could not locate freyja_bridge.py — set FREYJA_BRIDGE_INVOKE "
        "or install Freyja.app"
    )


# ─── Install / uninstall ───────────────────────────────────────────────


_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{shim}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>FREYJA_HEADLESS</key>
        <string>1</string>
        <key>FREYJA_HOME</key>
        <string>{freyja_home}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{stdout}</string>
    <key>StandardErrorPath</key>
    <string>{stderr}</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
</dict>
</plist>
"""


_SHIM_TEMPLATE = """#!/bin/bash
# Freyja scheduler daemon launcher. Generated by bridge/scheduler/daemon.py.
# Edit this file if you reinstall Freyja to a different location — or run
#   `freyja daemon reinstall`
# from inside the Freyja app, which regenerates both this file and the
# LaunchAgent plist.
#
# The `cd` is load-bearing. The bundled Python's pyvenv.cfg uses a
# relative `home = python-bundle/bin` path (see scripts/bundle-python.sh)
# that only resolves correctly when cwd is the directory CONTAINING
# python-bundle. launchd starts agents with cwd=/, so without this `cd`
# the python interpreter fails to find its stdlib and aborts with:
#   Fatal Python error: Failed to import encodings module
# That bug produced 5,414 daemon crashes and a 37k-line daemon.log
# before it was caught. Don't remove the `cd`.

cd "{cwd}" || exit 1
exec "{python}" {args} >> "{log}" 2>&1
"""


def _state_file_path() -> Path:
    return app_support_dir() / "daemon-state.json"


def _write_state(state: dict[str, Any]) -> None:
    try:
        _state_file_path().write_text(json.dumps(state, indent=2))
    except OSError as exc:
        logger.warning("failed to write daemon state: %s", exc)


def _read_state() -> dict[str, Any]:
    p = _state_file_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def ensure_daemon_installed(*, reason: str = "auto") -> dict[str, Any]:
    """Install + register the LaunchAgent. Always rewrites the shim +
    plist from the current templates so a `npm run rebuild` / Freyja
    relaunch picks up template fixes without the operator having to
    manually `uninstall_daemon()` first.

    Prior behavior short-circuited when both files existed, which meant
    template bug fixes (like the cd-into-bundle-parent shim fix that
    stopped the daemon from crashing 5,414 times in a row) shipped in
    new app versions but the stale shim on disk kept running. Now we
    rewrite unconditionally — the templates render the same output for
    the same inputs, so rewriting when nothing changed is a no-op write.

    Returns a status dict suitable for system events / dashboard."""
    if not is_supported_platform():
        return {"installed": False, "reason": "platform_unsupported"}

    try:
        executable, args = resolve_bridge_invocation()
    except RuntimeError as exc:
        return {"installed": False, "reason": f"resolution_failed: {exc}"}

    # Always rewrite the shim. `cwd` is the directory containing
    # python-bundle (e.g. Contents/Resources for the .app install) —
    # the bundled Python's pyvenv.cfg uses a relative `home` path that
    # needs this cwd to resolve. See the _SHIM_TEMPLATE comment for
    # the full reasoning.
    args_str = " ".join(f'"{a}"' for a in args)
    shim = shim_path()
    shim_cwd = str(Path(executable).resolve().parent.parent.parent)
    new_shim_content = _SHIM_TEMPLATE.format(
        python=executable,
        args=args_str,
        log=str(daemon_log_path()),
        cwd=shim_cwd,
    )
    prior_shim = shim.read_text() if shim.exists() else None
    shim.write_text(new_shim_content)
    os.chmod(shim, 0o755)
    shim_changed = prior_shim != new_shim_content

    # Then the plist. Same rewrite-unconditionally story as the shim:
    # write the current template, compare to prior, reload launchd only
    # if something actually changed (or if the launchd job isn't loaded
    # at all). Writing identical content over an existing plist is a
    # cheap no-op.
    launch_agents_dir().mkdir(parents=True, exist_ok=True)
    plist = plist_path()
    new_plist_content = _PLIST_TEMPLATE.format(
        label=LAUNCH_AGENT_LABEL,
        shim=str(shim),
        freyja_home=str(freyja_home()),
        stdout=str(daemon_log_path()),
        stderr=str(daemon_log_path()),
    )
    prior_plist = plist.read_text() if plist.exists() else None
    plist.write_text(new_plist_content)
    plist_changed = prior_plist != new_plist_content

    # Decide whether to (re)load launchd:
    #   - plist changed → must unload+load so launchd re-reads it.
    #   - launchd doesn't have the job loaded → load it.
    #   - shim changed but plist didn't → no launchd action needed; the
    #     shim path is the same, launchd execs it fresh on every
    #     respawn so the next KeepAlive respawn (~10s) picks up the
    #     new content automatically. We could `launchctl stop` to
    #     hasten that, but the natural respawn cycle does the job.
    currently_loaded = _launchctl_print(LAUNCH_AGENT_LABEL) is not None
    if plist_changed and currently_loaded:
        _launchctl_unload()
        _launchctl_load()
    elif not currently_loaded:
        _launchctl_load()
    elif shim_changed:
        # Plist unchanged but shim was updated. Stop the running
        # process so launchd respawns it with the new shim instead of
        # waiting for the daemon to crash naturally.
        try:
            subprocess.run(
                ["launchctl", "stop", LAUNCH_AGENT_LABEL],
                check=False, capture_output=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    _write_state({
        "installed_at": time.time(),
        "install_reason": reason,
        "plist": str(plist),
        "shim": str(shim),
        "bridge_executable": executable,
        "bridge_args": args,
        "shim_rewritten": shim_changed,
        "plist_rewritten": plist_changed,
    })

    try:
        from bridge.freyja_bridge import emit, log

        log(
            "info",
            f"scheduler daemon ensured (reason={reason}, "
            f"shim_changed={shim_changed}, plist_changed={plist_changed})",
        )
        emit({
            "type": "system_event",
            "sessionId": "scheduler:global",
            "subtype": "scheduler_daemon_installed",
            "message": "Background scheduler daemon installed",
            "details": {
                "reason": reason,
                "plist": str(plist),
                "shimChanged": shim_changed,
                "plistChanged": plist_changed,
            },
        })
    except Exception:  # noqa: BLE001
        pass

    return {
        "installed": True,
        "reason": reason,
        "plist": str(plist),
        "shim": str(shim),
        "shim_changed": shim_changed,
        "plist_changed": plist_changed,
    }


def uninstall_daemon() -> dict[str, Any]:
    """Stop the daemon, unregister from launchd, delete the plist +
    shim. Does NOT delete persisted job state — those survive."""
    if not is_supported_platform():
        return {"uninstalled": False, "reason": "platform_unsupported"}

    _launchctl_unload()
    removed_files: list[str] = []
    for p in (plist_path(), shim_path()):
        if p.exists():
            try:
                p.unlink()
                removed_files.append(str(p))
            except OSError as exc:
                logger.warning("failed to remove %s: %s", p, exc)

    _write_state({"uninstalled_at": time.time(), "removed": removed_files})

    try:
        from bridge.freyja_bridge import emit

        emit({
            "type": "system_event",
            "sessionId": "scheduler:global",
            "subtype": "scheduler_daemon_uninstalled",
            "message": "Background scheduler daemon uninstalled",
            "details": {"removed": removed_files},
        })
    except Exception:  # noqa: BLE001
        pass

    return {"uninstalled": True, "removed": removed_files}


def daemon_status() -> dict[str, Any]:
    """Return a status dict for the dashboard."""
    if not is_supported_platform():
        return {"supported": False, "platform": platform.system()}
    installed = plist_path().exists() and shim_path().exists()
    running = False
    pid: int | None = None
    if installed:
        info = _launchctl_print(LAUNCH_AGENT_LABEL)
        running = info is not None and info.get("state", "") in ("running", "spawning")
        pid = info.get("pid") if info else None
    return {
        "supported": True,
        "installed": installed,
        "running": running,
        "pid": pid,
        "plist": str(plist_path()) if installed else None,
        "log": str(daemon_log_path()),
        "state_file": str(_state_file_path()),
    }


# ─── App ↔ Daemon coordination ─────────────────────────────────────────


def on_app_start_takeover() -> None:
    """Called by the Electron-attached bridge at start. Stops the
    daemon so this process becomes the active scheduler. Acquires the
    bridge lock (advisory).

    Why not run both? Two SchedulerService instances scanning the same
    ``~/.freyja/schedules/jobs/`` dir would race on next_fire_at
    persistence and fire the same job twice. The lock + takeover
    pattern enforces single-writer semantics.
    """
    if not is_supported_platform():
        return
    try:
        subprocess.run(
            ["launchctl", "stop", LAUNCH_AGENT_LABEL],
            check=False, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    _write_lock(os.getpid(), "electron_bridge")


def on_app_quit_handoff() -> None:
    """Called by Electron on quit. If durable jobs exist, restart the
    daemon so they continue to fire. Releases the bridge lock."""
    if not is_supported_platform():
        return
    _clear_lock()
    if plist_path().exists():
        try:
            subprocess.run(
                ["launchctl", "start", LAUNCH_AGENT_LABEL],
                check=False, capture_output=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


# ─── launchctl wrappers ────────────────────────────────────────────────


def _launchctl_load() -> bool:
    try:
        subprocess.run(
            ["launchctl", "unload", str(plist_path())],
            check=False, capture_output=True, timeout=5,
        )
        result = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path())],
            check=False, capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                "launchctl load failed: %s / %s",
                result.stdout.decode(errors="replace"),
                result.stderr.decode(errors="replace"),
            )
            return False
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("launchctl invocation failed: %s", exc)
        return False


def _launchctl_unload() -> bool:
    if not plist_path().exists():
        return True
    try:
        subprocess.run(
            ["launchctl", "stop", LAUNCH_AGENT_LABEL],
            check=False, capture_output=True, timeout=5,
        )
        result = subprocess.run(
            ["launchctl", "unload", "-w", str(plist_path())],
            check=False, capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _launchctl_print(label: str) -> dict[str, Any] | None:
    """Parse a tiny subset of ``launchctl print`` output to determine
    if the agent is loaded + its current state and pid.
    """
    try:
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            check=False, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.decode(errors="replace")
    info: dict[str, Any] = {}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("state ="):
            info["state"] = s.split("=", 1)[1].strip()
        elif s.startswith("pid ="):
            try:
                info["pid"] = int(s.split("=", 1)[1].strip())
            except ValueError:
                pass
    return info or {"state": "unknown"}


# ─── Bridge lock ───────────────────────────────────────────────────────


def _write_lock(pid: int, owner: str) -> None:
    try:
        bridge_lock_path().write_text(json.dumps({
            "pid": pid,
            "owner": owner,
            "acquired_at": time.time(),
        }))
    except OSError as exc:
        logger.warning("failed to write bridge lock: %s", exc)


def _clear_lock() -> None:
    p = bridge_lock_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def read_bridge_lock() -> dict[str, Any] | None:
    p = bridge_lock_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
