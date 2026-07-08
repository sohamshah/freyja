"""System + app verbs (contract §4): volume, open/focus/quit/frontmost.

`system.volume` reads the prior state first so its VerbResult carries an
undo closure restoring BOTH volume level and mute flag — "undo" after
"mute" must bring back the old level too, not just unmute.

`app.quit` is the only confirm-tier verb in slice 1 (never force-quit;
the service layer gates it behind a spoken confirmation token) and its
undo simply reopens the app.
"""

from __future__ import annotations

from typing import Any, Optional

from bridge.voice.adapters import mac
from bridge.voice.adapters.mac import as_quoted
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

# The trailing bare expression is the script result osascript prints.
_READ_VOLUME_LINES = [
    "set s to get volume settings",
    "(output volume of s as string) & linefeed & (output muted of s as string)",
]


async def _read_volume() -> Optional[tuple[int, bool]]:
    """Current (output volume 0-100, muted) or None if unreadable."""
    ok, out = await mac.run_osascript_lines(_READ_VOLUME_LINES)
    if not ok:
        return None
    parts = out.split("\n")
    if len(parts) < 2:
        return None
    try:
        level = int(parts[0].strip())
    except ValueError:
        return None
    return level, parts[1].strip().lower() == "true"


def _make_volume_undo(prior_level: int, prior_muted: bool) -> Any:
    async def undo() -> VerbResult:
        ok, out = await mac.run_osascript_lines(
            [
                f"set volume output volume {prior_level}",
                f"set volume output muted {'true' if prior_muted else 'false'}",
            ]
        )
        if not ok:
            return VerbResult(ok=False, summary="couldn't restore volume", error=out)
        suffix = " (muted)" if prior_muted else ""
        return VerbResult(ok=True, summary=f"volume restored to {prior_level}%{suffix}")

    return undo


async def _volume(args: dict[str, Any]) -> VerbResult:
    level = args.get("level")
    delta = args.get("delta")
    mute = args.get("mute")
    if level is None and delta is None and mute is None:
        return VerbResult(
            ok=False, summary="no volume change requested", error="missing level/delta/mute"
        )

    prior = await _read_volume()

    if mute is not None:
        muted = bool(mute)
        ok, out = await mac.run_osascript(f"set volume output muted {'true' if muted else 'false'}")
        if not ok:
            return VerbResult(ok=False, summary="couldn't change mute", error=out)
        return VerbResult(
            ok=True,
            summary="muted" if muted else "unmuted",
            data={"muted": muted},
            undo=_make_volume_undo(*prior) if prior else None,
        )

    if delta is not None and level is None:
        # Relative change is meaningless without the current level.
        if prior is None:
            return VerbResult(
                ok=False, summary="couldn't read current volume", error="volume read failed"
            )
        target = prior[0] + int(delta)
    else:
        try:
            target = int(level)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return VerbResult(ok=False, summary="volume level must be a number", error=str(level))
    target = max(0, min(100, target))

    ok, out = await mac.run_osascript(f"set volume output volume {target}")
    if not ok:
        return VerbResult(ok=False, summary="couldn't set volume", error=out)
    if prior is not None:
        return VerbResult(
            ok=True,
            summary=f"volume {prior[0]}% → {target}%",
            data={"level": target},
            undo=_make_volume_undo(*prior),
        )
    # Prior read failed: the absolute set still went through, just not undoable.
    return VerbResult(ok=True, summary=f"volume → {target}%", data={"level": target})


def _require_name(args: dict[str, Any]) -> Optional[str]:
    name = str(args.get("name") or "").strip()
    return name or None


async def _open_app(name: str) -> VerbResult:
    ok, out = await mac.run_exec(["open", "-a", name])
    if not ok:
        # `open -a` resolves by bundle name only; AppleScript `activate`
        # also matches the app's scripting name, so try that before failing.
        ok, out = await mac.run_osascript(f"tell application {as_quoted(name)} to activate")
        if not ok:
            return VerbResult(ok=False, summary=f"couldn't open {name}", error=out)
    return VerbResult(ok=True, summary=f"opened {name}")


async def _app_open(args: dict[str, Any]) -> VerbResult:
    name = _require_name(args)
    if name is None:
        return VerbResult(ok=False, summary="which app?", error="missing name")
    return await _open_app(name)


async def _app_focus(args: dict[str, Any]) -> VerbResult:
    name = _require_name(args)
    if name is None:
        return VerbResult(ok=False, summary="which app?", error="missing name")
    ok, out = await mac.run_osascript(f"tell application {as_quoted(name)} to activate")
    if not ok:
        return VerbResult(ok=False, summary=f"couldn't focus {name}", error=out)
    return VerbResult(ok=True, summary=f"focused {name}")


async def _app_quit(args: dict[str, Any]) -> VerbResult:
    name = _require_name(args)
    if name is None:
        return VerbResult(ok=False, summary="which app?", error="missing name")
    ok, out = await mac.run_osascript(f"tell application {as_quoted(name)} to quit")
    if not ok:
        return VerbResult(ok=False, summary=f"couldn't quit {name}", error=out)

    async def undo() -> VerbResult:
        return await _open_app(name)

    return VerbResult(ok=True, summary=f"quit {name}", undo=undo)


async def _app_frontmost(args: dict[str, Any]) -> VerbResult:
    ok, out = await mac.run_osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    )
    if not ok:
        return VerbResult(ok=False, summary="couldn't read frontmost app", error=out)
    return VerbResult(ok=True, summary=f"frontmost: {out}", data={"app": out})


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="system.volume",
            description="Change output volume: absolute level, relative delta, or mute/unmute",
            params={
                "level": {"type": "integer", "minimum": 0, "maximum": 100},
                "delta": {"type": "integer", "description": "relative change, e.g. -10"},
                "mute": {"type": "boolean"},
            },
            required=[],
            tier="auto",
            run=_volume,
        )
    )
    registry.register(
        Verb(
            name="app.open",
            description="Open (launch) a Mac app by name",
            params={"name": {"type": "string"}},
            required=["name"],
            tier="auto",
            run=_app_open,
        )
    )
    registry.register(
        Verb(
            name="app.focus",
            description="Bring a running app to the front",
            params={"name": {"type": "string"}},
            required=["name"],
            tier="auto",
            run=_app_focus,
        )
    )
    registry.register(
        Verb(
            name="app.quit",
            description="Quit an app gracefully (never force-quits)",
            params={"name": {"type": "string"}},
            required=["name"],
            tier="confirm",
            run=_app_quit,
        )
    )
    registry.register(
        Verb(
            name="app.frontmost",
            description="Which app is frontmost right now",
            params={},
            required=[],
            tier="auto",
            run=_app_frontmost,
        )
    )
