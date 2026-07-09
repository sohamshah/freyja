"""Web page read verb (P2): web.read_page — read the frontmost browser tab.

The gap this closes: "what does this page say?", "summarize this" — the
operator is looking at a browser and wants Freyja to read it back. We pull
the active tab's URL + title + visible text straight out of the frontmost
browser over AppleScript, cap it, and hand it to the realtime model to
summarize (or answer a question against).

Two browser dialects:
  · Safari — ``URL``/``name`` of the front document are plain AppleScript
    properties, but the page TEXT needs ``do JavaScript`` on the document,
    which Safari gates behind Develop ▸ "Allow JavaScript from Apple
    Events". When that's off the JS errors; we degrade rather than dead-end.
  · Chromium (Chrome, Arc, Brave, Edge, …) — the URL/title are properties
    of the active tab, and the text comes from
    ``execute … javascript "document.body.innerText"``.

The frontmost browser is detected via System Events' frontmost process
name and mapped to the right dialect. If extraction fails for ANY reason —
a non-browser is frontmost, Safari's JS-from-Apple-Events is disabled, Arc
returns nothing (its tabs are often AppleScript-opaque) — the verb FALLS
BACK to the ``screen.look`` vision helper so it never dead-ends. The
missing-Automation-grant case degrades to the shared setup message.

Tests monkeypatch ``mac.run_osascript`` (the single AppleScript seam) and,
for the fallback path, ``screen._look``; no test drives a real browser.
"""

from __future__ import annotations

from typing import Any, Optional

from bridge.voice.adapters import mac, screen
from bridge.voice.adapters.mac import as_quoted
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

_AUTOMATION_SETUP = "automation"
_TEXT_CAP = 6000  # keep the model's read cheap; a page's gist fits easily
_FIELD_SEP = "\x1f"  # url US title US text — never appears in page text

# Frontmost-process names (lower-cased) → dialect. Chromium-family browsers
# all speak the same "active tab javascript" AppleScript; Safari is its own.
_CHROMIUM_BROWSERS = {
    "google chrome": "Google Chrome",
    "google chrome canary": "Google Chrome Canary",
    "chromium": "Chromium",
    "arc": "Arc",
    "brave browser": "Brave Browser",
    "microsoft edge": "Microsoft Edge",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "dia": "Dia",
}
_SAFARI_BROWSERS = {
    "safari": "Safari",
    "safari technology preview": "Safari Technology Preview",
}


async def _frontmost_process() -> tuple[Optional[str], Optional[VerbResult]]:
    """(frontmost process name, denied_result). On the Automation-grant
    failure the denied VerbResult is returned so the caller can surface the
    setup message; otherwise the name (possibly None) rides back."""
    ok, out = await mac.run_osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    )
    if not ok:
        if mac.automation_denied(out):
            return None, _denied_result("System Events", out)
        return None, None
    return out.strip(), None


def _denied_result(app: str, out: str) -> VerbResult:
    return VerbResult(
        ok=False,
        summary=(
            f"needs Automation permission for {app} — grant it in System "
            "Settings > Privacy & Security > Automation"
        ),
        data={"setup": _AUTOMATION_SETUP},
        error=out or "automation_denied",
    )


def _dialect(process_name: str) -> tuple[Optional[str], Optional[str]]:
    """(browser app name, kind) for a frontmost process, or (None, None)
    when it isn't a supported browser. ``kind`` is "safari" or "chromium"."""
    key = " ".join((process_name or "").lower().split())
    if key in _SAFARI_BROWSERS:
        return _SAFARI_BROWSERS[key], "safari"
    if key in _CHROMIUM_BROWSERS:
        return _CHROMIUM_BROWSERS[key], "chromium"
    return None, None


def _safari_lines(app: str) -> list[str]:
    # URL + name are plain properties; the visible text needs do JavaScript
    # (gated behind "Allow JavaScript from Apple Events"). All three ride
    # back unit-separated on one line so a single script yields the record.
    return [
        f"tell application {as_quoted(app)}",
        "set theURL to URL of front document",
        "set theTitle to name of front document",
        'set theText to (do JavaScript "document.body.innerText" in front document)',
        f'theURL & "{_FIELD_SEP}" & theTitle & "{_FIELD_SEP}" & theText',
        "end tell",
    ]


def _chromium_lines(app: str) -> list[str]:
    # Chromium browsers expose the active tab of the front window; innerText
    # comes back from executing JS in that tab.
    return [
        f"tell application {as_quoted(app)}",
        "set t to active tab of front window",
        "set theURL to URL of t",
        "set theTitle to title of t",
        'set theText to (execute t javascript "document.body.innerText")',
        f'theURL & "{_FIELD_SEP}" & theTitle & "{_FIELD_SEP}" & theText',
        "end tell",
    ]


def _parse_page(out: str) -> Optional[tuple[str, str, str]]:
    """Split a URL US title US text record; None when the shape is wrong
    (empty script result, or a browser that returned nothing)."""
    if not out or _FIELD_SEP not in out:
        return None
    parts = out.split(_FIELD_SEP, 2)
    url = parts[0].strip()
    title = parts[1].strip() if len(parts) > 1 else ""
    text = parts[2] if len(parts) > 2 else ""
    text = text.strip()[:_TEXT_CAP]
    if not text:
        return None
    return url, title, text


async def _vision_fallback(question: str, reason: str) -> VerbResult:
    """No text from the browser (non-browser frontmost, JS-from-Apple-Events
    off, Arc opaque) → let ``screen.look`` read the page as a picture. Never
    a dead-end: the vision helper owns its own capture/TCC failure wording."""
    look_q = question or "Summarize the main content of this page."
    res = await screen._look({"question": look_q})
    if not res.ok:
        # Bubble the vision helper's own failure (e.g. Screen Recording
        # permission) rather than invent one.
        return res
    caption = str(res.data.get("text") or res.summary or "").strip()
    return VerbResult(
        ok=True,
        summary=res.summary,
        data={"caption": caption, "via": "vision", "reason": reason},
    )


async def _read_page(args: dict[str, Any]) -> VerbResult:
    question = str(args.get("question") or "").strip()

    process_name, denied = await _frontmost_process()
    if denied is not None:
        return denied
    if not process_name:
        # Couldn't even read the frontmost process — try the eyes.
        return await _vision_fallback(question, "no_frontmost")

    app, kind = _dialect(process_name)
    if app is None:
        # A non-browser is frontmost → read the screen instead.
        return await _vision_fallback(question, "not_a_browser")

    lines = _safari_lines(app) if kind == "safari" else _chromium_lines(app)
    ok, out = await mac.run_osascript_lines(lines, timeout=12.0)
    if not ok:
        if mac.automation_denied(out):
            return _denied_result(app, out)
        # Safari JS-from-Apple-Events disabled, or an opaque browser error →
        # fall back to vision so "read this page" always answers something.
        return await _vision_fallback(question, "extract_failed")

    parsed = _parse_page(out)
    if parsed is None:
        # Script ran but yielded no usable text (empty tab, opaque Arc) →
        # vision.
        return await _vision_fallback(question, "empty_text")

    url, title, text = parsed
    return VerbResult(
        ok=True,
        summary=f"read {(title or url or 'page')[:40]}",
        data={"url": url, "title": title, "text": text},
    )


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="web.read_page",
            description="Read the current browser page (URL, title, text) to summarize or answer",
            params={
                "question": {
                    "type": "string",
                    "description": "what to find on the page; omit for a summary",
                }
            },
            required=[],
            tier="auto",
            run=_read_page,
        )
    )
