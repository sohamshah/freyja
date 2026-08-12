"""web.read_page adapter behavior: Safari + Chromium dialects, the
vision fallback for every dead-end, and TCC-denied degradation.

``mac.run_osascript`` is the single AppleScript seam (monkeypatched for
every test) and ``screen._look`` is monkeypatched for the vision-fallback
paths; no test drives a real browser or takes a real screenshot.
"""

import pytest

from bridge.voice.adapters import mac, screen, web
from bridge.voice.verbs import VerbRegistry, VerbResult

_DENIED_MINUS_1743 = (
    "execution error: Not authorized to send Apple events to Safari. (-1743)"
)
# Safari's "Allow JavaScript from Apple Events" is OFF — a JS-specific error,
# NOT the -1743 Automation-grant refusal, so it must fall through to vision.
_JS_DISABLED = (
    "execution error: Safari is not allowed to run JavaScript from Apple "
    "Events in this document. (12)"
)


class ScriptRecorder:
    """Stands in for mac.run_osascript; replays canned (ok, out) replies."""

    def __init__(self, replies=None):
        self.scripts = []
        self.replies = list(replies or [])

    async def __call__(self, script, timeout=6.0):
        self.scripts.append(script)
        return self.replies.pop(0) if self.replies else (True, "")


@pytest.fixture
def osa(monkeypatch):
    rec = ScriptRecorder()
    # run_osascript_lines resolves run_osascript through module globals, so
    # one patch intercepts both entry points.
    monkeypatch.setattr(mac, "run_osascript", rec)
    return rec


@pytest.fixture
def look_calls(monkeypatch):
    """Capture screen._look invocations and return a canned VerbResult."""
    calls = []

    async def fake_look(args):
        calls.append(dict(args))
        return VerbResult(ok=True, summary="the eyes read it", data={"text": "vision says X"})

    monkeypatch.setattr(screen, "_look", fake_look)
    return calls


@pytest.fixture
def reg():
    # web.read_page is temporarily out of the default registry
    # (GALDR-BUILD §12.3), so register the adapter directly — the
    # extraction logic under test is unchanged and worth keeping green.
    registry = VerbRegistry()
    web.register(registry)
    return registry


def _frontmost(name):
    return (True, name)


# ── registration ──────────────────────────────────────────────────────────


def test_web_read_page_registered(reg):
    v = reg.get("web.read_page")
    assert v is not None and v.tier == "auto"


# ── Safari dialect ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safari_extracts_url_title_text(reg, osa):
    fs = web._FIELD_SEP
    osa.replies = [
        _frontmost("Safari"),
        (True, f"https://example.com{fs}Example Domain{fs}This is the page body."),
    ]
    res = await reg.get("web.read_page").run({})
    assert res.ok and res.summary == "read Example Domain"
    assert res.data == {
        "url": "https://example.com",
        "title": "Example Domain",
        "text": "This is the page body.",
    }
    # The Safari dialect: front document properties + do JavaScript.
    script = osa.scripts[1]
    assert 'tell application "Safari"' in script
    assert "URL of front document" in script
    assert "name of front document" in script
    assert 'do JavaScript "document.body.innerText" in front document' in script


@pytest.mark.asyncio
async def test_safari_caps_text(reg, osa):
    fs = web._FIELD_SEP
    body = "y" * 9000
    osa.replies = [
        _frontmost("Safari"),
        (True, f"https://x.com{fs}Big{fs}{body}"),
    ]
    res = await reg.get("web.read_page").run({})
    assert res.ok and len(res.data["text"]) == web._TEXT_CAP


# ── Chromium dialect (Arc, Chrome) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_chrome_dialect(reg, osa):
    fs = web._FIELD_SEP
    osa.replies = [
        _frontmost("Google Chrome"),
        (True, f"https://news.site{fs}Headlines{fs}Top story text"),
    ]
    res = await reg.get("web.read_page").run({})
    assert res.ok and res.data["title"] == "Headlines"
    script = osa.scripts[1]
    assert 'tell application "Google Chrome"' in script
    assert "active tab of front window" in script
    assert 'execute t javascript "document.body.innerText"' in script


@pytest.mark.asyncio
async def test_arc_dialect_uses_chromium_script(reg, osa):
    fs = web._FIELD_SEP
    osa.replies = [
        _frontmost("Arc"),
        (True, f"https://arc.page{fs}Arc{fs}Some visible text"),
    ]
    res = await reg.get("web.read_page").run({})
    assert res.ok
    script = osa.scripts[1]
    assert 'tell application "Arc"' in script
    assert "active tab of front window" in script
    assert "execute t javascript" in script


# ── vision fallback paths ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_browser_frontmost_falls_back_to_vision(reg, osa, look_calls):
    osa.replies = [_frontmost("Notes")]  # not a browser
    res = await reg.get("web.read_page").run({"question": "what does it say?"})
    assert res.ok and res.data["via"] == "vision"
    assert res.data["caption"] == "vision says X"
    assert res.data["reason"] == "not_a_browser"
    # The question is forwarded to the eyes; no browser script ever ran.
    assert look_calls == [{"question": "what does it say?"}]
    assert len(osa.scripts) == 1


@pytest.mark.asyncio
async def test_no_question_gives_vision_a_summary_prompt(reg, osa, look_calls):
    osa.replies = [_frontmost("Finder")]
    res = await reg.get("web.read_page").run({})
    assert res.ok and res.data["via"] == "vision"
    assert look_calls[0]["question"] == "Summarize the main content of this page."


@pytest.mark.asyncio
async def test_safari_js_disabled_falls_back_to_vision(reg, osa, look_calls):
    osa.replies = [
        _frontmost("Safari"),
        (False, _JS_DISABLED),  # JS-from-Apple-Events off (not -1743)
    ]
    res = await reg.get("web.read_page").run({"question": "summarize"})
    assert res.ok and res.data["via"] == "vision"
    assert res.data["reason"] == "extract_failed"
    assert look_calls[0]["question"] == "summarize"


@pytest.mark.asyncio
async def test_empty_text_falls_back_to_vision(reg, osa, look_calls):
    fs = web._FIELD_SEP
    # Arc-style opaque return: URL/title but no body text.
    osa.replies = [
        _frontmost("Arc"),
        (True, f"https://arc.page{fs}Arc{fs}"),
    ]
    res = await reg.get("web.read_page").run({})
    assert res.ok and res.data["via"] == "vision"
    assert res.data["reason"] == "empty_text"


@pytest.mark.asyncio
async def test_vision_failure_bubbles_up(reg, osa, monkeypatch):
    # When the browser is opaque AND the eyes fail (e.g. Screen Recording
    # denied), surface the vision helper's own failure — never a fake ok.
    osa.replies = [_frontmost("TextEdit")]

    async def failing_look(args):
        return VerbResult(
            ok=False, summary="screen capture unavailable — needs Screen Recording permission"
        )

    monkeypatch.setattr(screen, "_look", failing_look)
    res = await reg.get("web.read_page").run({})
    assert not res.ok and "Screen Recording" in res.summary


# ── TCC-denied ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_frontmost_automation_denied_setup_message(reg, osa, look_calls):
    # System Events itself is denied → the setup message, not a vision
    # fallback (we can't even see what's frontmost).
    osa.replies = [(False, _DENIED_MINUS_1743)]
    res = await reg.get("web.read_page").run({})
    assert not res.ok and res.data["setup"] == "automation"
    assert "Automation permission for System Events" in res.summary
    assert look_calls == []


@pytest.mark.asyncio
async def test_browser_automation_denied_setup_message(reg, osa, look_calls):
    # Frontmost read fine, but the browser tell is Automation-denied → the
    # setup message names the browser, and vision is NOT tried (the grant is
    # the actionable fix).
    osa.replies = [
        _frontmost("Safari"),
        (False, _DENIED_MINUS_1743),
    ]
    res = await reg.get("web.read_page").run({})
    assert not res.ok and res.data["setup"] == "automation"
    assert "Automation permission for Safari" in res.summary
    assert look_calls == []
