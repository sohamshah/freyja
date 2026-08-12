"""Thin AppleScript / subprocess shims for the voice verb adapters.

Everything the adapters do to the machine funnels through
`run_osascript` (or `run_exec` for the one non-AppleScript path,
`open -a`). Tests monkeypatch these two functions and assert on the
exact scripts, so no test ever touches the real Mac.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_osascript(script: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Run one AppleScript via `osascript -e`; returns (ok, stdout|stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "osascript not found"
    except OSError as exc:
        return False, f"osascript failed to start: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        # Kill AND reap — kill() alone leaves a zombie until the loop exits.
        proc.kill()
        await proc.wait()
        return False, "osascript timeout"
    if proc.returncode == 0:
        return True, stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip()
    return False, err or f"osascript exited {proc.returncode}"


async def run_osascript_lines(lines: list[str], timeout: float = 6.0) -> tuple[bool, str]:
    """Run a multi-line AppleScript as a single `-e` argument.

    Newline-joining (instead of one `-e` per line) keeps `run_osascript`
    the sole subprocess/monkeypatch point.
    """
    return await run_osascript("\n".join(lines), timeout=timeout)


async def run_exec(argv: list[str], timeout: float = 6.0) -> tuple[bool, str]:
    """Run a plain subprocess (e.g. `open -a Safari`); returns (ok, stdout|stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, f"{argv[0]}: command not found"
    except OSError as exc:
        return False, f"{argv[0]} failed to start: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"{argv[0]} timeout"
    if proc.returncode == 0:
        return True, stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip()
    return False, err or f"{argv[0]} exited {proc.returncode}"


def as_quoted(s: str) -> str:
    """AppleScript string literal with quotes/backslashes escaped."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Substrings that mean "the Automation TCC grant is missing" across the
# several shapes osascript uses (-1743 is the Apple-events auth code; the
# assistive-access line is the System-Events UI-scripting variant). Shared
# by every adapter that drives another app over AppleScript (apple.*, web.*)
# so the denied case degrades identically — a setup message, never a hang or
# a raw stderr. The Safari "Allow JavaScript from Apple Events" refusal is a
# different, JS-specific error (handled where it occurs), not this grant.
_AUTOMATION_DENIED_MARKERS = (
    "not authorized to send apple events",
    "-1743",
    "not allowed assistive access",
    "not permitted to send apple events",
)


def automation_denied(out: str) -> bool:
    """True when an osascript failure is the missing-Automation-grant one."""
    low = (out or "").lower()
    return any(marker in low for marker in _AUTOMATION_DENIED_MARKERS)
