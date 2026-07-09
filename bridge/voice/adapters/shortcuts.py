"""Shortcuts verbs: list + run the user's Shortcuts library (App Intents bridge).

The point of these two verbs is leverage: every shortcut the operator has
built — "Open Hacker News", "Text Last Image", "Play Focus Meditation" —
becomes a voice verb for free, and through App Intents a shortcut can
reach apps and system actions no AppleScript surface exposes. So the whole
library rides in behind a single ``shortcuts.run <name>``.

Unlike the Apple-app adapters, the ``shortcuts`` CLI needs NO Automation
TCC grant — it runs in a bare dev shell — so there is no denied path here;
failures are ordinary non-zero exits surfaced as terse one-liners.

``shortcuts list`` output (the shortcut names) is cached module-level for
60 s so a "run X" right after a "list" doesn't re-shell. ``shortcuts.run``
fuzzy-resolves its ``name`` against that cache with the same
exact→unique-substring→ask discipline the app resolver in ``system.py``
uses, then runs the shortcut; ``input`` is handed to the shortcut via a
temp ``--input-path`` file (``mac.run_exec`` has no stdin) and the output
is captured from stdout.

Tests monkeypatch ``mac.run_exec`` (the single subprocess seam) and reset
the cache; no test shells out to the real ``shortcuts`` binary.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any, Optional

from bridge.voice.adapters import mac
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

_LIST_TTL_SEC = 60.0
# {"expires": epoch, "names": [str, …]} — the last `shortcuts list`.
_list_cache: dict[str, Any] = {"expires": 0.0, "names": []}


def _reset_cache() -> None:
    """Test seam — drop the list cache."""
    _list_cache.update({"expires": 0.0, "names": []})


async def _fetch_names(force: bool = False) -> tuple[Optional[list[str]], Optional[str]]:
    """Cached shortcut names. Returns (names, error): on success error is
    None; on a `shortcuts list` failure names is None and error is the
    terse CLI message."""
    now = time.time()
    if not force and now < _list_cache["expires"] and _list_cache["names"]:
        return list(_list_cache["names"]), None
    ok, out = await mac.run_exec(["shortcuts", "list"])
    if not ok:
        return None, out or "shortcuts list failed"
    names = [line.strip() for line in out.splitlines() if line.strip()]
    _list_cache.update({"expires": now + _LIST_TTL_SEC, "names": names})
    return list(names), None


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def _resolve_shortcut(query: str, names: list[str]) -> tuple[Optional[str], list[str]]:
    """(matched name, near-miss suggestions), mirroring system._resolve_app:
    exact normalized match wins; else a UNIQUE substring match wins; a
    non-unique substring (or only weak word overlap) returns candidates so
    the caller asks rather than running the wrong shortcut."""
    q = _norm(query)
    if not q:
        return None, []
    by_norm = {_norm(n): n for n in names}
    if q in by_norm:
        return by_norm[q], []
    # Unique substring match — "hacker news" ⊂ "Open Hacker News".
    subs = [n for n in names if q in _norm(n)]
    if len(subs) == 1:
        return subs[0], []
    if len(subs) > 1:
        return None, sorted(subs, key=len)[:5]
    # No substring hit: offer any shortcut sharing a spoken word, shortest
    # first, as a "did you mean" — better than a bare not-found.
    q_tokens = set(q.split())
    weak = [n for n in names if q_tokens & set(_norm(n).split())]
    return None, sorted(weak, key=len)[:5]


async def _shortcuts_list(args: dict[str, Any]) -> VerbResult:
    names, err = await _fetch_names()
    if names is None:
        return VerbResult(ok=False, summary="couldn't list Shortcuts", error=err)
    n = len(names)
    return VerbResult(
        ok=True,
        summary=f"{n} shortcut{'s' if n != 1 else ''}",
        data={"shortcuts": names},
    )


async def _shortcuts_run(args: dict[str, Any]) -> VerbResult:
    name = str(args.get("name") or "").strip()
    if not name:
        return VerbResult(ok=False, summary="which shortcut?", error="missing_name")
    names, err = await _fetch_names()
    if names is None:
        return VerbResult(ok=False, summary="couldn't list Shortcuts", error=err)
    resolved, suggestions = _resolve_shortcut(name, names)
    if resolved is None:
        if suggestions:
            listing = ", ".join(suggestions)
            return VerbResult(
                ok=False,
                summary=f"which shortcut? {len(suggestions)} near {name}",
                data={"suggestions": suggestions},
                error=f"no exact shortcut named {name}; close matches: {listing}. Ask which one.",
            )
        return VerbResult(
            ok=False, summary=f"no shortcut named {name}", error="unknown_shortcut"
        )

    argv = ["shortcuts", "run", resolved]
    input_text = str(args.get("input") or "")
    tmppath: Optional[str] = None
    try:
        if input_text:
            # run_exec has no stdin, so the input rides in via a temp file
            # the shortcut reads through --input-path.
            fd, tmppath = tempfile.mkstemp(prefix="freyja-shortcut-in-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(input_text)
            argv += ["--input-path", tmppath]
        ok, out = await mac.run_exec(argv)
    finally:
        if tmppath is not None:
            try:
                os.unlink(tmppath)
            except OSError:
                pass
    if not ok:
        return VerbResult(ok=False, summary=f"{resolved} failed", error=out)
    data: dict[str, Any] = {"name": resolved}
    if out.strip():
        data["output"] = out.strip()
    return VerbResult(ok=True, summary=f"▷ ran {resolved}", data=data)


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="shortcuts.list",
            description="List the names of the user's Shortcuts",
            params={},
            required=[],
            tier="auto",
            run=_shortcuts_list,
        )
    )
    registry.register(
        Verb(
            name="shortcuts.run",
            description="Run a Shortcut by name (fuzzy-matched), optional text input",
            params={
                "name": {"type": "string"},
                "input": {"type": "string", "description": "text passed to the shortcut"},
            },
            required=["name"],
            tier="auto",
            run=_shortcuts_run,
        )
    )
