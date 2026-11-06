"""freyja_native sends the per-launch proxy token that Electron main puts in
the bridge's env. The capture and input proxies refuse a request without it
(src/main/proxyToken.ts), and when they refuse a call the caller hears why.
Only a local fake server is called; no input is sent and nothing is captured.

The wrapper is loaded from this checkout over a stub _native, under its own
name, so these tests need no built extension and cannot reach real input."""

import http.server
import importlib.util
import json
import sys
import threading
import types
import urllib.parse
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "native" / "freyja_native" / "python" / "freyja_native" / "__init__.py"
)


def _load_wrapper():
    name = "freyja_native_proxy_test"
    sys.modules[f"{name}._native"] = types.ModuleType(f"{name}._native")
    spec = importlib.util.spec_from_file_location(name, WRAPPER, submodule_search_locations=[])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


freyja_native = _load_wrapper()


@pytest.fixture
def fake_proxy(request):
    """Answers every POST with (status, body): 200 ok, or the test's param."""
    status, answer = getattr(request, "param", (200, b'{"ok": true}'))
    seen: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            token = self.headers.get("X-Freyja-Token")
            seen.append({"path": self.path, "token": token, "body": body})

        def do_GET(self):  # noqa: N802
            self._record()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(b"\xff\xd8jpegbytes")

        def do_POST(self):  # noqa: N802
            self._record()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *a):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    # A short poll so shutdown() returns at once instead of after the 0.5 s default.
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()


def test_input_proxy_call_sends_the_input_token(fake_proxy, monkeypatch):
    url, seen = fake_proxy
    monkeypatch.setenv("FREYJA_INPUT_URL", url)
    monkeypatch.setenv("FREYJA_INPUT_TOKEN", "input-secret")
    monkeypatch.setenv("FREYJA_CAPTURE_TOKEN", "capture-secret")
    freyja_native._proxy_input("move_mouse", x=1, y=2)
    assert seen[0]["path"] == "/input" and seen[0]["token"] == "input-secret"
    assert json.loads(seen[0]["body"]) == {"action": "move_mouse", "x": 1, "y": 2}


def test_capture_proxy_call_sends_the_capture_token(fake_proxy, monkeypatch):
    url, seen = fake_proxy
    monkeypatch.setenv("FREYJA_CAPTURE_TOKEN", "capture-secret")
    monkeypatch.setenv("FREYJA_INPUT_TOKEN", "input-secret")
    frame = freyja_native._proxy_screenshot(
        url, display_id=None, max_dim=None, format="jpeg", quality=80
    )
    assert frame.png == b"\xff\xd8jpegbytes"
    assert seen[0]["path"].startswith("/capture?") and seen[0]["token"] == "capture-secret"


def test_no_token_in_env_sends_no_header(fake_proxy, monkeypatch):
    url, seen = fake_proxy
    monkeypatch.setenv("FREYJA_INPUT_URL", url)
    monkeypatch.delenv("FREYJA_INPUT_TOKEN", raising=False)
    monkeypatch.delenv("FREYJA_CAPTURE_TOKEN", raising=False)
    freyja_native._proxy_input("key_up", key="shift")
    freyja_native._proxy_screenshot(url, display_id=None, max_dim=None, format="jpeg", quality=80)
    assert [s["token"] for s in seen] == [None, None]


@pytest.mark.parametrize(
    ("fake_proxy", "message"),
    [
        (
            (400, b'{"ok": false, "error": "x must be a number"}'),
            "input proxy 400: x must be a number",
        ),
        (
            (500, b'{"ok": false, "error": "RuntimeError: no display"}'),
            "input proxy 500: RuntimeError: no display",
        ),
        ((502, b"Bad Gateway"), "input proxy 502: Bad Gateway"),
    ],
    indirect=["fake_proxy"],
)
def test_a_refused_input_call_says_why(fake_proxy, monkeypatch, message):
    url, _ = fake_proxy
    monkeypatch.setenv("FREYJA_INPUT_URL", url)
    with pytest.raises(RuntimeError) as err:
        freyja_native._proxy_input("move_mouse", x=1, y=2)
    assert str(err.value) == message


@pytest.fixture
def display_proxy(monkeypatch):
    """A capture proxy that knows displays 1 and 2, as Electron main answers
    one, and a stub _native with no Screen Recording that records captures.
    Returns the native captures made."""
    unknown = (
        b"display 99 is not connected (it may have been unplugged, or the id is from an old list); "
        b"connected display ids: 1, 2"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            display = query.get("display_id", ["1"])[0]
            answers = {"1": (200, b"\xff\xd8one"), "2": (500, b"error: boom")}
            status, body = answers.get(display, (404, unknown))
            self.send_response(status)
            self.send_header("Content-Type", "image/jpeg" if status == 200 else "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    native_captures: list = []

    def capture_screen(display_id, max_dim, format, quality):
        native_captures.append(display_id)
        return b"\xff\xd8filtered"

    native = freyja_native._native
    monkeypatch.setattr(native, "check_screen_recording_permission", lambda: False, raising=False)
    monkeypatch.setattr(native, "capture_screen", capture_screen, raising=False)
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True).start()
    monkeypatch.setenv("FREYJA_CAPTURE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield native_captures
    finally:
        server.shutdown()


def test_a_display_that_is_not_connected_is_an_error_that_names_the_others(display_proxy):
    assert freyja_native.screenshot(display_id=1, format="jpeg").png == b"\xff\xd8one"
    with pytest.raises(freyja_native.CaptureProxyError) as err:
        freyja_native.screenshot(display_id=99, format="jpeg")
    assert err.value.status == 404
    assert str(err.value) == (
        "capture proxy 404: display 99 is not connected (it may have been unplugged, "
        "or the id is from an old list); connected display ids: 1, 2"
    )
    # The native path is not tried for it: it cannot capture the display either.
    assert display_proxy == []


def test_other_proxy_failures_still_fall_back_to_the_native_capture(display_proxy):
    assert freyja_native.screenshot(display_id=2, format="jpeg").png == b"\xff\xd8filtered"
    assert display_proxy == [2]
