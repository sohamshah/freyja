"""System + app verbs (contract §4): volume, open/focus/quit/frontmost.

`system.volume` reads the prior state first so its VerbResult carries an
undo closure restoring BOTH volume level and mute flag — "undo" after
"mute" must bring back the old level too, not just unmute.

`app.quit` is the only confirm-tier verb in slice 1 (never force-quit;
the service layer gates it behind a spoken confirmation token) and its
undo simply reopens the app.
"""

from __future__ import annotations

import os
import time
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


# ── app-name resolution ─────────────────────────────────────────────────────
# The model hears "open Arc" and reliably says "Arc Browser"; the bundle on
# disk is "Arc". A bare `open -a "Arc Browser"` fails, and the operator sees
# "couldn't open Arc Browser" — the exact live gap this closes. We resolve
# the spoken name against the ACTUALLY-INSTALLED apps (Spotlight, with a
# directory-scan fallback) and match forgivingly, so a reasonable name lands
# on the right app and a wrong one comes back with real suggestions.

_APP_CACHE_TTL_SEC = 60.0
# {normalized display name → canonical app name (no .app)}, newest wins.
_app_cache: dict[str, Any] = {"expires": 0.0, "by_name": {}}

_APP_DIRS = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    os.path.expanduser("~/Applications"),
)


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


async def _installed_apps() -> dict[str, str]:
    """normalized name → canonical app name (no .app extension). Cached
    60 s. Spotlight first (finds apps anywhere, ~80 ms); directory scan
    as the fallback when the index is unavailable."""
    now = time.time()
    if now < _app_cache["expires"] and _app_cache["by_name"]:
        return _app_cache["by_name"]
    paths: list[str] = []
    ok, out = await mac.run_exec(
        ["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"], timeout=4.0
    )
    if ok and out.strip():
        paths = [p for p in out.splitlines() if p.strip().endswith(".app")]
    if not paths:
        # Spotlight off / sandboxed — scan the usual bundle dirs.
        for d in _APP_DIRS:
            try:
                for entry in os.listdir(d):
                    if entry.endswith(".app"):
                        paths.append(os.path.join(d, entry))
            except OSError:
                continue
    by_name: dict[str, str] = {}
    for p in paths:
        canonical = os.path.basename(p)[: -len(".app")]
        key = _norm(canonical)
        if key:
            by_name.setdefault(key, canonical)
    _app_cache.update({"expires": now + _APP_CACHE_TTL_SEC, "by_name": by_name})
    return by_name


async def _resolve_app(query: str) -> tuple[Optional[str], list[str]]:
    """(canonical app name, near-miss suggestions).

    Scoring, best-first: exact normalized match, then one name being a
    prefix of the other ("Arc" ↔ "Arc Browser"), then a shared leading
    word, then any word overlap. A confident match (prefix or better)
    resolves; a weak one returns suggestions so the model can ask rather
    than open the wrong thing. Ties break toward the shortest name so
    "Arc" wins over "Arc Search Beta"."""
    apps = await _installed_apps()
    q = _norm(query)
    if not q:
        return None, []
    if q in apps:
        return apps[q], []
    q_tokens = set(q.split())
    scored: list[tuple[int, str]] = []  # (score, canonical)
    for key, canonical in apps.items():
        k_tokens = set(key.split())
        if key.startswith(q):
            score = 92  # spoken words are a leading prefix of the name — strong
        elif q_tokens and q_tokens.issubset(k_tokens):
            score = 82  # every spoken word appears in the name ("chrome" ⊂ Chrome)
        elif q.startswith(key):
            score = 80  # name is a prefix of a longer phrase ("Arc" ← "arc browser")
        elif key.split()[:1] == q.split()[:1]:
            score = 70  # shared leading word
        elif q_tokens & k_tokens:
            score = 50  # some word in common — weak
        else:
            continue
        scored.append((score, canonical))
    if not scored:
        return None, []
    top = max(s for s, _ in scored)
    winners = sorted({c for s, c in scored if s == top}, key=len)
    if top >= 70:
        # Decisive: the shortest (plainest) top-scorer is almost always the
        # one meant — "chrome" → Google Chrome, not …Canary. Only a genuine
        # tie (two equally short top matches) is worth a clarifying question.
        if len(winners) == 1 or len(winners[0]) < len(winners[1]):
            return winners[0], []
        return None, winners[:4]
    # Only weak overlaps — surface them as suggestions, shortest first.
    return None, sorted({c for _, c in scored}, key=len)[:4]


async def _open_by_name(query: str, verb_label: str) -> tuple[Optional[str], Optional[VerbResult]]:
    """Resolve + launch. Returns (canonical_name, error_result). On
    success error_result is None and canonical_name is the app actually
    opened (so focus/quit target the right bundle)."""
    canonical, suggestions = await _resolve_app(query)
    if canonical is None and suggestions:
        # The resolver had real opinions but no confident winner — ask
        # rather than blind-launching the raw phrase (which might open the
        # wrong app, or nothing).
        listing = ", ".join(suggestions)
        return None, VerbResult(
            ok=False,
            summary=f"which {query}?",
            data={"suggestions": suggestions},
            error=f"more than one app matches {query}: {listing}. Ask which one.",
        )
    target = canonical or query
    ok, out = await mac.run_exec(["open", "-a", target])
    if not ok:
        # `open -a` matches bundle name; AppleScript `activate` also matches
        # the scripting name — try it before giving up.
        ok, out = await mac.run_osascript(f"tell application {as_quoted(target)} to activate")
    if ok:
        return target, None
    # Launch failed and the resolver found nothing to suggest.
    return None, VerbResult(ok=False, summary=f"couldn't {verb_label} {query}", error=out)


async def _open_app(name: str) -> VerbResult:
    canonical, err = await _open_by_name(name, "open")
    if err is not None:
        return err
    return VerbResult(ok=True, summary=f"opened {canonical}")


async def _app_open(args: dict[str, Any]) -> VerbResult:
    name = _require_name(args)
    if name is None:
        return VerbResult(ok=False, summary="which app?", error="missing name")
    return await _open_app(name)


async def _app_focus(args: dict[str, Any]) -> VerbResult:
    name = _require_name(args)
    if name is None:
        return VerbResult(ok=False, summary="which app?", error="missing name")
    # Focus IS open-if-needed + activate, so resolution + the open path
    # cover both; a running app just comes forward.
    canonical, err = await _open_by_name(name, "focus")
    if err is not None:
        return err
    return VerbResult(ok=True, summary=f"focused {canonical}")


async def _app_quit(args: dict[str, Any]) -> VerbResult:
    name = _require_name(args)
    if name is None:
        return VerbResult(ok=False, summary="which app?", error="missing name")
    canonical, _sugg = await _resolve_app(name)
    target = canonical or name
    ok, out = await mac.run_osascript(f"tell application {as_quoted(target)} to quit")
    if not ok:
        return VerbResult(ok=False, summary=f"couldn't quit {target}", error=out)
    name = target

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
