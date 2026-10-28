"""freyja_native sends the per-launch proxy token that Electron main puts in
the bridge's env. The capture and input proxies refuse a request without it
(src/main/proxyToken.ts). Only a local fake server is called; no input is
sent and nothing is captured."""

import http.server
import json
import threading

import pytest

freyja_native = pytest.importorskip("freyja_native")


@pytest.fixture
def fake_proxy():
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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
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
