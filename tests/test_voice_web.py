"""web.read_page adapter behavior: Safari + Chromium dialects, the vision fallback for every dead-end, and TCC-denied degradation.

``mac.run_osascript`` is the single AppleScript seam (monkeypatched for every test) and ``screen._look`` is monkeypatched for the vision-fallback paths; no test drives a real browser or takes a real screenshot.
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
_FS = web._FIELD_SEP


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


# ── Safari dialect ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safari_extracts_url_title_text_and_caps(reg, osa):
    osa.replies = [
        _frontmost("Safari"),
        (True, f"https://example.com{_FS}Example Domain{_FS}This is the page body."),
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

    osa.replies = [_frontmost("Safari"), (True, f"https://x.com{_FS}Big{_FS}{'y' * 9000}")]
    res = await reg.get("web.read_page").run({})
    assert res.ok and len(res.data["text"]) == web._TEXT_CAP


# ── Chromium dialect (Arc, Chrome) ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("browser", ["Google Chrome", "Arc"])
async def test_chromium_dialect(reg, osa, browser):
    osa.replies = [
        _frontmost(browser),
        (True, f"https://news.site{_FS}Headlines{_FS}Top story text"),
    ]
    res = await reg.get("web.read_page").run({})
    assert res.ok and res.data["title"] == "Headlines"
    script = osa.scripts[1]
    assert f'tell application "{browser}"' in script
    assert "active tab of front window" in script
    assert 'execute t javascript "document.body.innerText"' in script


# ── vision fallback paths ─────────────────────────────────────────────────


_SUMMARY_PROMPT = "Summarize the main content of this page."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replies,args,reason,question",
    [
        # Not a browser in front: no browser script ever runs.
        ([_frontmost("Notes")], {"question": "what?"}, "not_a_browser", "what?"),
        # JS-from-Apple-Events off (not -1743): fall through, don't nag about Automation.
        ([_frontmost("Safari"), (False, _JS_DISABLED)], {"question": "sum"}, "extract_failed", "sum"),
        # Arc-style opaque return: URL/title but no body text; no question → summary prompt.
        ([_frontmost("Arc"), (True, f"https://arc.page{_FS}Arc{_FS}")], {}, "empty_text", _SUMMARY_PROMPT),
    ],
)
async def test_opaque_page_falls_back_to_vision(
    reg, osa, look_calls, replies, args, reason, question
):
    osa.replies = list(replies)
    res = await reg.get("web.read_page").run(args)
    assert res.ok and res.data["via"] == "vision"
    assert res.data["caption"] == "vision says X"
    assert res.data["reason"] == reason
    # The question (or the default prompt) is forwarded to the eyes.
    assert look_calls == [{"question": question}]
    assert len(osa.scripts) == len(replies)


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
@pytest.mark.parametrize(
    "replies,app",
    [
        # System Events itself is denied → we can't even see what's frontmost.
        ([(False, _DENIED_MINUS_1743)], "System Events"),
        # Frontmost read fine, but the browser tell is Automation-denied → the
        # setup message names the browser.
        ([_frontmost("Safari"), (False, _DENIED_MINUS_1743)], "Safari"),
    ],
)
async def test_automation_denied_names_the_app_and_skips_vision(reg, osa, look_calls, replies, app):
    # The grant is the actionable fix, so vision is NOT tried.
    osa.replies = list(replies)
    res = await reg.get("web.read_page").run({})
    assert not res.ok and res.data["setup"] == "automation"
    assert f"Automation permission for {app}" in res.summary
    assert look_calls == []
