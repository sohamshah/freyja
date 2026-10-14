"""The environment server: serves each env's site, keeps per-episode state, and
records what the page reports, so rewards come from ground truth and never from
an agent's own account of what it did.

One threaded HTTP server on 127.0.0.1. The same server also answers on
`localhost` (a different origin), so an env can embed a cross-origin frame the
way third-party widgets are embedded.

Routes:
  GET  /__start/<ep>?to=<path>   set the episode cookie, then redirect to <path>
  GET  <a page in .pages>         a tool page, such as `serve`'s index of tasks
  GET  /<env>/<file>             static files of an env's site
  ANY  /<env>/api/<name>         the env backend (JSON), with the episode's state
  POST /event                    a page event (old jev_live fixtures, sendBeacon)
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

COOKIE = "cua_ep"


@dataclass
class Episode:
    id: str
    env: str
    config: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def log(self, event: str, **data: Any) -> None:
        with self.lock:
            self.events.append({"t": round(time.time() - self.started, 3), "event": event, **data})


@dataclass
class Response:
    status: int = 200
    body: Any = None  # dict/list -> JSON; str -> text/html; bytes -> raw
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0


# An env backend: (episode, method, api name, query, json body) -> Response.
Backend = Callable[[Episode, str, str, dict[str, str], Any], Response]


@dataclass
class EnvSpec:
    name: str
    site: Path
    backend: Backend | None = None
    # Called when an episode starts: fills episode.state from episode.config.
    init: Callable[[Episode], None] | None = None


class EnvServer:
    def __init__(self, envs: list[EnvSpec], *, port: int = 0) -> None:
        self.envs = {e.name: e for e in envs}
        self.episodes: dict[str, Episode] = {}
        self.fixture_events: list[dict[str, Any]] = []  # POST /event (keyed by "run")
        # Tool pages outside every env: path -> handler(query) -> Response.
        self.pages: dict[str, Callable[[dict[str, str]], Response]] = {}
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:  # noqa: N802
                server._handle(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                server._handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # ─── lifecycle ──────────────────────────────────────────────────

    def start(self) -> "EnvServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def other_origin(self) -> str:
        """The same server under another origin, for cross-origin frames."""
        return f"http://localhost:{self.port}"

    def new_episode(self, env: str, config: dict[str, Any] | None = None) -> Episode:
        spec = self.envs[env]
        ep = Episode(id=secrets.token_hex(6), env=env, config=dict(config or {}))
        if spec.init:
            spec.init(ep)
        with self._lock:
            self.episodes[ep.id] = ep
        return ep

    def start_url(self, ep: Episode, path: str) -> str:
        """The address that binds a fresh browser context to `ep` and opens `path`."""
        return f"{self.base}/__start/{ep.id}?to={urllib.parse.quote(path, safe='')}"

    def events_for_run(self, run: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.fixture_events if e.get("run") == run]

    # ─── request handling ───────────────────────────────────────────

    def _episode_for(self, h: BaseHTTPRequestHandler, query: dict[str, str]) -> Episode | None:
        ep_id = query.get("ep") or ""
        if not ep_id:
            c = SimpleCookie(h.headers.get("Cookie") or "")
            if COOKIE in c:
                ep_id = c[COOKIE].value
        return self.episodes.get(ep_id)

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urllib.parse.urlsplit(h.path)
        path = urllib.parse.unquote(parsed.path)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        try:
            if path.startswith("/__start/"):
                ep_id = path.split("/")[2]
                to = query.get("to") or "/"
                return self._send(
                    h,
                    Response(
                        302,
                        "",
                        {
                            "Location": to,
                            "Set-Cookie": f"{COOKIE}={ep_id}; Path=/; SameSite=Lax",
                        },
                    ),
                )
            if path in self.pages:
                return self._send(h, self.pages[path](query))
            if path == "/event" and method == "POST":
                try:
                    ev = json.loads(raw.decode("utf-8", "replace") or "{}")
                except ValueError:
                    ev = {"raw": raw.decode("utf-8", "replace")}
                ev["_t"] = time.time()
                with self._lock:
                    self.fixture_events.append(ev)
                ep = self._episode_for(h, query)
                if ep is not None:
                    ep.log("page", **{k: v for k, v in ev.items() if k != "_t"})
                return self._send(h, Response(204, b""))
            parts = [p for p in path.split("/") if p]
            if not parts or parts[0] not in self.envs:
                return self._send(h, Response(404, "not found"))
            spec = self.envs[parts[0]]
            if len(parts) >= 3 and parts[1] == "api":
                ep = self._episode_for(h, query)
                if ep is None or spec.backend is None:
                    return self._send(h, Response(400, {"error": "no episode"}))
                body: Any = None
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        body = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
                with ep.lock:
                    resp = spec.backend(ep, method, "/".join(parts[2:]), query, body)
                if resp.delay_s:
                    time.sleep(resp.delay_s)
                return self._send(h, resp)
            rel = "/".join(parts[1:]) or "index.html"
            f = (spec.site / rel).resolve()
            if spec.site.resolve() not in f.parents or not f.is_file():
                return self._send(h, Response(404, "not found"))
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            ep = self._episode_for(h, query)
            if ep is not None and f.suffix == ".html":
                ep.log("page_load", path="/" + "/".join(parts), host=h.headers.get("Host", ""))
            return self._send(h, Response(200, f.read_bytes(), {"Content-Type": ctype}))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            try:
                self._send(h, Response(500, {"error": f"{type(exc).__name__}: {exc}"}))
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, r: Response) -> None:
        body = r.body
        headers = dict(r.headers)
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode()
            headers.setdefault("Content-Type", "text/html; charset=utf-8")
        else:
            data = body or b""
        h.send_response(r.status)
        headers.setdefault("Cache-Control", "no-store")
        for k, v in headers.items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(data)
