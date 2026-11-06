"""screen.look (bridge/voice/adapters/screen.py).

No real screencapture, no real OpenAI call: ``mac.run_exec`` is the capture seam, ``screen._downscale`` the PIL seam, and ``screen.httpx`` the network seam — same monkeypatch style as the spotify adapter tests.
"""

from __future__ import annotations

import io
import os
from types import SimpleNamespace

import pytest

from bridge.voice.adapters import mac, screen
from bridge.voice.verbs import VerbRegistry


@pytest.fixture
def reg():
    registry = VerbRegistry()
    screen.register(registry)
    return registry


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-eyes")
    monkeypatch.delenv("FREYJA_VOICE_LOOK_MODEL", raising=False)


class CaptureRecorder:
    """Stands in for mac.run_exec; on success writes a non-empty 'PNG'."""

    def __init__(self, ok=True, out="", empty=False):
        self.calls = []
        self.ok = ok
        self.out = out
        self.empty = empty

    async def __call__(self, argv, timeout=6.0):
        self.calls.append(list(argv))
        if self.ok:
            with open(argv[-1], "wb") as fh:
                if not self.empty:
                    fh.write(b"fake-png-bytes")
        return self.ok, self.out


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeHttpx:
    def __init__(self, response):
        self.posts = []
        outer = self

        class Client:
            def __init__(self, timeout=None):
                outer.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                outer.posts.append({"url": url, "headers": headers, "json": json})
                return response

        self.AsyncClient = Client


def _reply(content):
    return _FakeResponse(payload={"choices": [{"message": {"content": content}}]})


def _install_http(monkeypatch, response):
    fake = _FakeHttpx(response)
    monkeypatch.setattr(screen, "httpx", SimpleNamespace(AsyncClient=fake.AsyncClient))
    return fake


@pytest.fixture
def look_rig(monkeypatch, api_key):
    """Happy-path rig: recording capture + downscale + httpx fakes."""
    capture = CaptureRecorder()
    monkeypatch.setattr(mac, "run_exec", capture)
    downscale_calls = []

    def fake_downscale(path):
        downscale_calls.append(path)
        return b"downscaled-jpeg"

    monkeypatch.setattr(screen, "_downscale", fake_downscale)
    fake = _install_http(monkeypatch, _reply("Terminal, tests passing."))
    return SimpleNamespace(capture=capture, downscale_calls=downscale_calls, http=fake)


# ── capture failure ───────────────────────────────────────────────────────


async def test_capture_failure_or_empty_file_is_clean(reg, monkeypatch, api_key):
    capture = CaptureRecorder(ok=False, out="could not create image from display")
    monkeypatch.setattr(mac, "run_exec", capture)
    fake = _install_http(monkeypatch, _FakeResponse())
    res = await reg.get("screen.look").run({})
    assert not res.ok
    assert res.summary == "screen capture unavailable — needs Screen Recording permission"
    assert fake.posts == []  # never reached the vision call
    tmppath = capture.calls[0][-1]
    assert not os.path.exists(tmppath)  # cleaned up even on failure

    # A zero-byte capture (TCC wrote nothing) counts as the same failure.
    monkeypatch.setattr(mac, "run_exec", CaptureRecorder(empty=True))
    res = await reg.get("screen.look").run({})
    assert not res.ok
    assert "Screen Recording" in res.summary
    assert fake.posts == []


async def test_look_without_api_key(reg, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    called = CaptureRecorder()
    monkeypatch.setattr(mac, "run_exec", called)
    res = await reg.get("screen.look").run({})
    assert not res.ok and res.error == "no_api_key"
    assert called.calls == []  # no pointless screenshot


# ── happy path ────────────────────────────────────────────────────────────


async def test_look_happy_path(reg, look_rig):
    res = await reg.get("screen.look").run({})
    assert res.ok
    assert res.data == {"text": "Terminal, tests passing."}
    assert res.summary == "Terminal, tests passing."

    # screencapture invocation: silent, cursor included, into the tmp png.
    (argv,) = look_rig.capture.calls
    assert argv[:3] == ["screencapture", "-x", "-C"]
    tmppath = argv[3]
    assert tmppath.endswith(".png")

    # Downscale ran on the captured file; tmp is gone afterwards.
    assert look_rig.downscale_calls == [tmppath]
    assert not os.path.exists(tmppath)

    (post,) = look_rig.http.posts
    assert post["url"] == "https://api.openai.com/v1/chat/completions"
    assert post["headers"]["Authorization"].startswith("Bearer ")
    payload = post["json"]
    assert payload["model"] == "gpt-5-mini"
    system, user = payload["messages"]
    assert system == {"role": "system", "content": screen._SYSTEM_PROMPT}
    image_part, text_part = user["content"]
    # The downscaled JPEG rides as a data URI (base64 of b"downscaled-jpeg").
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,ZG93bnNjYWxlZC1qcGVn")
    assert text_part == {"type": "text", "text": "What matters on this screen right now?"}
    assert look_rig.http.timeout == 25.0


async def test_look_threads_question_and_model_env_and_truncates_summary(reg, look_rig, monkeypatch):
    monkeypatch.setenv("FREYJA_VOICE_LOOK_MODEL", "gpt-5.2")
    long_text = "long " * 40
    fake = _install_http(monkeypatch, _reply(long_text))
    res = await reg.get("screen.look").run({"question": "is the build green?"})
    assert res.ok
    (post,) = fake.posts
    assert post["json"]["model"] == "gpt-5.2"
    text_part = post["json"]["messages"][1]["content"][1]
    assert text_part == {"type": "text", "text": "is the build green?"}
    assert res.summary == long_text.strip()[:80]
    assert res.data["text"] == long_text.strip()


async def test_look_api_error_is_terse_and_cleans_up(reg, look_rig, monkeypatch):
    _install_http(monkeypatch, _FakeResponse(status_code=429, text="rate limited"))
    res = await reg.get("screen.look").run({})
    assert not res.ok
    assert res.summary == "look failed: OpenAI returned 429"
    tmppath = look_rig.capture.calls[0][-1]
    assert not os.path.exists(tmppath)


# ── the real downscale (PIL is a dependency — cheap to exercise) ──────────


def test_downscale_resizes_and_recompresses(tmp_path):
    from PIL import Image

    src = tmp_path / "big.png"
    Image.new("RGB", (3200, 1000), (10, 20, 30)).save(src, format="PNG")
    jpeg = screen._downscale(str(src))
    out = Image.open(io.BytesIO(jpeg))
    assert out.format == "JPEG"
    assert out.width == 1568
    assert out.height == round(1000 * 1568 / 3200)

    small = tmp_path / "small.png"
    Image.new("RGB", (400, 300), (10, 20, 30)).save(small, format="PNG")
    out_small = Image.open(io.BytesIO(screen._downscale(str(small))))
    assert (out_small.width, out_small.height) == (400, 300)  # never upscaled


# ── locate_in_image (vision grounding for AX-opaque clicking) ─────────────


@pytest.mark.parametrize(
    "content,expected",
    [
        ('{"found": true, "x": 0.42, "y": 0.6}', (0.42, 0.6)),
        ('{"found": false}', None),
        ('```json\n{"found":true,"x":0.1,"y":0.9}\n```', (0.1, 0.9)),  # tolerates a code fence
        ('{"found":true,"x":1.4,"y":-0.2}', (1.0, 0.0)),  # clamps out-of-range coords
    ],
)
async def test_locate_in_image_parses_model_reply(monkeypatch, api_key, content, expected):
    fake = _install_http(monkeypatch, _reply(content))
    assert await screen.locate_in_image(b"jpeg-bytes", "the Send button") == expected
    # the target rode in the user text
    assert "Send button" in fake.posts[0]["json"]["messages"][1]["content"][1]["text"]


async def test_locate_in_image_http_error_raises(monkeypatch, api_key):
    _install_http(monkeypatch, _FakeResponse(status_code=429))
    with pytest.raises(RuntimeError):
        await screen.locate_in_image(b"x", "thing")
