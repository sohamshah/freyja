"""Loopback redirect handling for the OAuth authorization-code flow.

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) —
``tools/mcp_oauth.py`` lines 209-323 (port reservation, cached redirect
port), 731-792 (callback handler), 918-1156 (callback waiter + stdin paste
reader) and 1312-1404 (CIMD pinned-port pool).

What lives here:

* **TOCTOU-safe port reservation** (hermes #22161). ``reserve_callback_port``
  binds an ephemeral port and *keeps the socket bound but not listening* in
  ``_reserved_sockets`` until :class:`LoopbackCallbackServer` adopts it.
  Between "pick a free port" and "the browser redirect arrives" can be
  minutes; a closed probe socket would let any other process grab the port
  in that window. A bounded FIFO evicts stale ephemeral reservations so
  reconnect loops cannot leak fds; pinned CIMD ports are never evicted.
* **Cached redirect port** (hermes Summ bug). Providers bind a dynamically
  registered ``client_id`` to the exact redirect URI. On restart we read
  the port back out of ``client.json`` instead of picking a new one.
* **CIMD pinned-port pool** — five fixed ports below Linux's 32768 ephemeral
  floor that Freyja's published Client ID Metadata Document declares.
* **LoopbackCallbackServer** — a minimal ``http.server`` on 127.0.0.1 that
  captures exactly one ``code``/``error`` redirect, validates the ``state``
  parameter against the value embedded in the authorization URL (a request
  with the wrong state gets a failure page and does NOT consume the
  one-shot), renders a success/failure page, and hands the result to the
  asyncio side without blocking the bridge loop.
* **Paste/skip fallback** (hermes ``_paste_callback_reader``) exposed as a
  pure parser plus an optional ``paste_source`` coroutine hook, because
  Freyja has no stdin to read from — the desktop UI or Slack surface
  supplies the pasted redirect URL (or a skip token) through the hook.

SDK note (mcp 2.1.1): ``callback_handler`` must return
``mcp.shared.auth.AuthorizationCodeResult`` (mcp 2.0 replaced the
``tuple[str, str | None]`` contract) and the SDK reads ``.state`` / ``.iss``
off it; ``iss`` (RFC 9207) is forwarded because the SDK *rejects* a
response that omits it when the AS advertised
``authorization_response_iss_parameter_supported``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from bridge.mcp.oauth.gates import (
    OAuthCallbackPortInUseError,
    OAuthCallbackTimeoutError,
    OAuthNeedsAuthError,
    OAuthUserSkippedError,
    login_hint,
)

if TYPE_CHECKING:  # pragma: no cover
    from bridge.mcp.oauth.storage import FreyjaTokenStorage
    from mcp.shared.auth import AuthorizationCodeResult

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/callback"
LOOPBACK_BIND_HOST = "127.0.0.1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

DEFAULT_CALLBACK_TIMEOUT_S = 300.0
_POLL_INTERVAL_S = 0.05

# Skip tokens accepted at the paste prompt — exit OAuth without auth.
SKIP_TOKENS = frozenset({"skip", "cancel", "s", "n", "no", "q", "quit"})

# ---------------------------------------------------------------------------
# CIMD pinned ports
# ---------------------------------------------------------------------------

# Loopback callback ports declared in docs/oauth/client-metadata.json. The
# redirect URI in the authorization request must be an exact string match
# against a listed one (CIMD draft section 4.2), so a CIMD flow cannot use
# the ephemeral port we pick otherwise. These sit below Linux's 32768
# ephemeral floor, so the kernel never hands one to an unrelated process.
# Keep in sync with the document — tests/test_mcp_oauth_cimd.py enforces it.
CIMD_PORTS: tuple[int, ...] = (27890, 27891, 27892, 27893, 27894)

# Pinned ports this process has committed to, in the order they were taken.
# A provider is built once per configured OAuth server and keeps its port
# for the process lifetime, so assignments are never released. Includes a
# port restored from a cached client registration, so a sibling server is
# never handed a port another one is already registered on (hermes #34260).
_assigned_cimd_ports: list[int] = []


# ---------------------------------------------------------------------------
# Port reservation (TOCTOU-safe)
# ---------------------------------------------------------------------------

# Bound-but-not-listening sockets reserved for pending OAuth callback flows,
# keyed by port. Bounded FIFO so repeated build calls (reconnect loops)
# cannot leak fds.
_reserved_sockets: dict[int, socket.socket] = {}
_MAX_RESERVED_SOCKETS = 8
_reservation_lock = threading.Lock()


def park_reserved_socket(port: int, sock: socket.socket) -> None:
    """Hold *sock* bound to *port* until a :class:`LoopbackCallbackServer` adopts it.

    Pinned CIMD sockets are never evicted: the published metadata document
    only declares the pinned ports, so losing one mid-flow silently converts
    a pinned reservation back into a stealable window — the exact race the
    parking exists to prevent (#22161). The FIFO cap applies to ephemeral
    reservations only; the pinned range is already bounded by ``CIMD_PORTS``.
    """
    with _reservation_lock:
        while len(_reserved_sockets) >= _MAX_RESERVED_SOCKETS:
            stale_port = next((p for p in _reserved_sockets if p not in CIMD_PORTS), None)
            if stale_port is None:
                break  # only pinned sockets remain — never evict those
            stale = _reserved_sockets.pop(stale_port, None)
            if stale is None:
                continue
            try:
                stale.close()
            except OSError:
                pass
        previous = _reserved_sockets.pop(port, None)
        if previous is not None and previous is not sock:
            try:
                previous.close()
            except OSError:
                pass
        _reserved_sockets[port] = sock


def reserve_callback_port() -> int:
    """Pick an ephemeral callback port and keep its socket bound.

    Returns the port. The bound (not yet listening) socket is parked in
    ``_reserved_sockets`` so no other process can bind the port before the
    callback server adopts it. Adoption (or ``close``) owns the socket's
    lifetime from there.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOOPBACK_BIND_HOST, 0))
    except OSError:
        s.close()
        raise
    port = s.getsockname()[1]
    park_reserved_socket(port, s)
    return port


def reserve_fixed_port(port: int) -> bool:
    """Bind *port* and park the socket, or return False if it's taken.

    ``SO_REUSEADDR`` is set before binding (deviation from hermes, which bound
    bare): a pinned CIMD port that just served a callback sits in TIME_WAIT
    for up to a minute, and without the flag every re-login within that
    window would find it "in use", walk down the five-port range, and then
    silently fall back to DCR. The flag lets us reclaim a TIME_WAIT port
    while a port with a *listening* sibling (another profile mid-login)
    still refuses the bind, which is the contention we actually care about.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK_BIND_HOST, port))
    except OSError:
        sock.close()
        return False
    park_reserved_socket(port, sock)
    return True


def take_reserved_socket(port: int) -> socket.socket | None:
    """Remove and return the parked socket for *port* (None if none)."""
    with _reservation_lock:
        return _reserved_sockets.pop(port, None)


def is_port_reserved(port: int) -> bool:
    with _reservation_lock:
        return port in _reserved_sockets


def release_reserved_port(port: int) -> None:
    """Close and forget a parked socket (flow abandoned before adoption)."""
    sock = take_reserved_socket(port)
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


def note_assigned_cimd_port(port: int) -> None:
    """Claim *port* for this process when it belongs to the pinned range."""
    if port in CIMD_PORTS and port not in _assigned_cimd_ports:
        _assigned_cimd_ports.append(port)


def pick_cimd_port() -> int | None:
    """Reserve a pinned CIMD callback port, or None when none is usable.

    Holding the bound socket until the callback server adopts it does the
    same job here as ``reserve_callback_port`` does for ephemeral ports
    (#22161): a fixed port is just as stealable in the minutes between
    selection and the browser redirect arriving. It also makes contention
    cooperative — a second Freyja profile mid-login, or a sibling server in
    this process, finds the bind refused and moves down the range instead of
    racing us to the same listener.

    Once every pinned port belongs to this process the range wraps rather
    than falling back to DCR: a reused port only bites if both of its
    servers authorize at the same moment, and the callback server reports
    that collision clearly, whereas the DCR fallback would silently use a
    mechanism the server may not support at all.
    """
    for port in CIMD_PORTS:
        if port in _assigned_cimd_ports:
            continue
        if reserve_fixed_port(port):
            _assigned_cimd_ports.append(port)
            return port
    return _assigned_cimd_ports[0] if _assigned_cimd_ports else None


def assigned_cimd_ports() -> tuple[int, ...]:
    return tuple(_assigned_cimd_ports)


def reset_port_state_for_tests() -> None:
    """Close every parked socket and forget CIMD assignments (tests only)."""
    with _reservation_lock:
        for sock in _reserved_sockets.values():
            try:
                sock.close()
            except OSError:
                pass
        _reserved_sockets.clear()
    _assigned_cimd_ports.clear()


# ---------------------------------------------------------------------------
# Cached redirect port / URI from client.json
# ---------------------------------------------------------------------------


def cached_redirect_port(storage: "FreyjaTokenStorage | None") -> int | None:
    """Return the loopback callback port from cached client registration.

    OAuth providers bind a dynamically-registered ``client_id`` to the exact
    redirect URI that was registered with it. If Freyja restarts and chooses
    a new random callback port while reusing the stored ``client_id``,
    providers such as Summ reject the authorization request with
    ``redirect_uri does not match any registered URIs``. Reusing the cached
    redirect port keeps the authorization request consistent with the
    stored client registration.
    """
    if storage is None:
        return None
    try:
        data = storage.read_client_info_raw()
    except (AttributeError, TypeError, ValueError):
        return None
    if not data:
        return None
    for uri in data.get("redirect_uris") or []:
        try:
            parsed = urlparse(str(uri))
        except (TypeError, ValueError):
            continue
        if (
            parsed.scheme == "http"
            and parsed.hostname in LOOPBACK_HOSTS
            and parsed.path == CALLBACK_PATH
            and parsed.port is not None
        ):
            return int(parsed.port)
    return None


def cached_redirect_uri(storage: "FreyjaTokenStorage | None") -> str | None:
    """Return a cached non-loopback (https proxy) redirect URI, if one was registered."""
    if storage is None:
        return None
    try:
        data = storage.read_client_info_raw()
    except (AttributeError, TypeError, ValueError):
        return None
    for uri in (data or {}).get("redirect_uris") or []:
        try:
            parsed = urlparse(str(uri))
        except (TypeError, ValueError):
            continue
        if parsed.scheme == "https" and parsed.netloc:
            return str(uri)
    return None


def loopback_redirect_uri(port: int, host: str = LOOPBACK_BIND_HOST) -> str:
    return f"http://{host}:{port}{CALLBACK_PATH}"


def state_from_authorization_url(url: str) -> str | None:
    """Extract the ``state`` query parameter the SDK put in the authorize URL."""
    try:
        return parse_qs(urlparse(url).query).get("state", [None])[0]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Captured redirect
# ---------------------------------------------------------------------------


@dataclass
class CallbackCapture:
    """The parameters of one authorization response redirect."""

    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None
    iss: str | None = None
    skipped: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.code is not None or self.error is not None or self.skipped


def authorization_code_result(
    code: str, state: str | None, iss: str | None = None
) -> "AuthorizationCodeResult":
    """Package redirect parameters in the shape the installed SDK expects."""
    from mcp.shared.auth import AuthorizationCodeResult

    return AuthorizationCodeResult(code=code, state=state, iss=iss)


def parse_callback_input(line: str) -> CallbackCapture | None:
    """Parse a pasted redirect into a :class:`CallbackCapture`.

    Accepts any of:
      - Full redirect URL: ``http://127.0.0.1:37949/callback?code=...&state=...``
      - The provider's own callback URL: ``https://mcp.example.com/callback?code=...``
      - Just the query string: ``?code=...&state=...`` or ``code=...&state=...``
      - A skip token (``skip``, ``cancel``, ``s``, ``n``, ``no``, ``q``, ``quit``)
        — returns a capture with ``skipped=True`` so the caller can exit the
        OAuth flow cleanly without auth (non-fatal opt-out).

    Returns None when the input carries neither ``code=`` nor ``error=``.
    Nothing is logged here — the input contains the authorization code.
    """
    if line is None:
        return None
    text = line.strip()
    if not text:
        return None
    if text.lower() in SKIP_TOKENS:
        return CallbackCapture(skipped=True)

    query = text
    if "?" in text:
        query = text.split("?", 1)[1]
    if query.startswith("?"):
        query = query[1:]
    if "#" in query:
        query = query.split("#", 1)[0]
    try:
        params = parse_qs(query, keep_blank_values=False)
    except (ValueError, TypeError):
        return None

    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    error = params.get("error", [None])[0]
    if not code and not error:
        return None
    return CallbackCapture(
        code=code,
        state=state,
        error=error,
        error_description=params.get("error_description", [None])[0],
        iss=params.get("iss", [None])[0],
    )


# ---------------------------------------------------------------------------
# Loopback HTTP server
# ---------------------------------------------------------------------------


def _render_page(title: str, body: str, app_name: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:-apple-system,system-ui,sans-serif;margin:3rem auto;"
        "max-width:32rem;line-height:1.5;color:#222}h2{margin-bottom:.5rem}</style>"
        f"</head><body><h2>{html.escape(title)}</h2><p>{body}</p>"
        f"<p style='color:#666'>{html.escape(app_name)}</p></body></html>"
    ).encode("utf-8")


class CallbackGate:
    """Thread-safe one-shot capture of a single authorization response.

    Shared by the loopback HTTP listener, the paste fallback and external
    (flow-delivered, hermes ``DashboardOAuthFlow``-style) callbacks so every
    source goes through the same state check and first-writer-wins gate.
    ``wait()`` polls without blocking the bridge loop.
    """

    def __init__(self, *, expected_state: str | None = None, server_name: str = "") -> None:
        self.expected_state = expected_state
        self.server_name = server_name
        self._capture: CallbackCapture | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.rejected_state_count = 0

    def deliver(self, capture: CallbackCapture) -> bool:
        """Record *capture* if nothing has been captured yet. Returns True if it won."""
        if not capture.is_terminal:
            return False
        with self._lock:
            if self._capture is not None:
                return False
            self._capture = capture
        self._done.set()
        return True

    def deliver_params(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None = None,
        error_description: str | None = None,
        iss: str | None = None,
    ) -> bool:
        """Deliver raw redirect parameters with state validation.

        Returns False (and counts the rejection) on a state mismatch or when
        the parameters carry neither code nor error; the one-shot is left
        open for the genuine redirect.
        """
        capture = CallbackCapture(
            code=code, state=state, error=error, error_description=error_description, iss=iss
        )
        if not capture.is_terminal:
            return False
        if not self._state_matches(state):
            self.rejected_state_count += 1
            logger.warning(
                "mcp oauth '%s': callback with mismatched state ignored", self.server_name
            )
            return False
        return self.deliver(capture)

    def _state_matches(self, state: str | None) -> bool:
        if self.expected_state is None:
            return True
        if state is None:
            return False
        import secrets

        return secrets.compare_digest(state, self.expected_state)

    @property
    def capture(self) -> CallbackCapture | None:
        return self._capture

    @property
    def done(self) -> bool:
        return self._done.is_set()

    async def wait(self, timeout: float = DEFAULT_CALLBACK_TIMEOUT_S) -> CallbackCapture:
        """Await the one-shot capture without blocking the event loop."""
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while not self._done.is_set():
            if time.monotonic() >= deadline:
                raise OAuthCallbackTimeoutError(
                    "OAuth callback timed out — no authorization code received. "
                    "Ensure you completed the browser authorization flow. "
                    f"{login_hint(self.server_name)}",
                    server_name=self.server_name,
                )
            await asyncio.sleep(_POLL_INTERVAL_S)
        assert self._capture is not None
        return self._capture

    def close(self) -> None:  # pragma: no cover — no resources for a bare gate
        return None


class LoopbackCallbackServer(CallbackGate):
    """One-shot ``http://127.0.0.1:<port>/callback`` listener for one flow.

    Lifecycle: ``start()`` (adopts the parked socket or binds the port),
    ``await wait(timeout)`` (returns the capture), ``close()`` (always — the
    flow wrapper does it in a ``finally``). ``deliver()`` lets the paste
    fallback inject a capture through the same one-shot gate the HTTP
    handler uses.
    """

    def __init__(
        self,
        port: int,
        *,
        expected_state: str | None = None,
        server_name: str = "",
        app_name: str = "Freyja",
    ) -> None:
        super().__init__(expected_state=expected_state, server_name=server_name)
        self.port = port
        self.app_name = app_name
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- HTTP ---------------------------------------------------------------

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self._reply(404, _render_page(
                        "Not found", "This listener only accepts the OAuth callback.",
                        server_ref.app_name,
                    ))
                    return
                params = parse_qs(parsed.query)
                capture = CallbackCapture(
                    code=params.get("code", [None])[0],
                    state=params.get("state", [None])[0],
                    error=params.get("error", [None])[0],
                    error_description=params.get("error_description", [None])[0],
                    # RFC 9207 authorization-response issuer. mcp 2.0 validates
                    # it against the discovered metadata and *rejects* a
                    # response that omits it when the AS advertised support,
                    # so dropping it here would break login against those
                    # providers.
                    iss=params.get("iss", [None])[0],
                )
                if not capture.is_terminal:
                    self._reply(400, _render_page(
                        "Authorization Failed",
                        "The redirect did not include an authorization code or error.",
                        server_ref.app_name,
                    ))
                    return
                if not server_ref._state_matches(capture.state):
                    # Wrong/missing state: a stray or forged request. Do NOT
                    # consume the one-shot — the real redirect may still come.
                    server_ref.rejected_state_count += 1
                    logger.warning(
                        "mcp oauth '%s': loopback callback with mismatched state ignored",
                        server_ref.server_name,
                    )
                    self._reply(400, _render_page(
                        "Authorization Failed",
                        "State parameter mismatch — this response does not belong to "
                        "the pending login. Please retry the login.",
                        server_ref.app_name,
                    ))
                    return
                won = server_ref.deliver(capture)
                if not won:
                    self._reply(409, _render_page(
                        "Already Completed",
                        "This login already received its callback. You can close this tab.",
                        server_ref.app_name,
                    ))
                    return
                if capture.code:
                    body = _render_page(
                        "Authorization Successful",
                        f"You can close this tab and return to {html.escape(server_ref.app_name)}.",
                        server_ref.app_name,
                    )
                else:
                    detail = html.escape(capture.error or "unknown")
                    if capture.error_description:
                        detail += f": {html.escape(capture.error_description)}"
                    body = _render_page(
                        "Authorization Failed", f"Error: {detail}", server_ref.app_name
                    )
                self._reply(200, body)

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, fmt: str, *args: Any) -> None:
                # Never log the request line: it carries ``code=`` and
                # ``state=``. Path + status only.
                logger.debug(
                    "mcp oauth '%s': loopback callback request %s",
                    server_ref.server_name, urlparse(self.path).path,
                )

        return _Handler

    def start(self) -> None:
        """Bind/adopt the port and start serving in a daemon thread.

        Adopts the socket reserved at port-selection time when one exists
        (closing the TOCTOU window, #22161). ``allow_reuse_address`` is set
        BEFORE binding for the fixed/cached-port path so a lingering
        TIME_WAIT socket from a previous flow cannot block the next one
        (hermes #44590).
        """
        if self._httpd is not None:
            return
        handler_cls = self._make_handler()
        try:
            httpd = HTTPServer(
                (LOOPBACK_BIND_HOST, self.port), handler_cls, bind_and_activate=False
            )
            reserved = take_reserved_socket(self.port)
            if reserved is not None:
                httpd.socket.close()
                httpd.socket = reserved
                httpd.server_address = reserved.getsockname()
                httpd.server_activate()
            else:
                httpd.allow_reuse_address = True
                httpd.server_bind()
                httpd.server_activate()
        except OSError as exc:
            # The loopback callback port is genuinely in use: a concurrent
            # OAuth flow, a leftover listener, or a fixed `oauth.redirect_port`
            # that collided. Surface a clear, actionable error instead of a
            # misleading "timed out".
            raise OAuthCallbackPortInUseError(
                f"OAuth callback port {self.port} is already in use ({exc}). "
                "Close any other in-progress login, or set a free `oauth.redirect_port` "
                f"in the server config, then retry. {login_hint(self.server_name)}",
                server_name=self.server_name,
            ) from exc
        httpd.timeout = 0.5
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name=f"mcp-oauth-callback-{self.port}",
            daemon=True,
        )
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def redirect_uri(self, host: str = LOOPBACK_BIND_HOST) -> str:
        return loopback_redirect_uri(self.port, host)

    def close(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001 — shutdown before serve_forever started
            pass
        try:
            httpd.server_close()
        except OSError:
            pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Waiter: loopback server raced against the optional paste hook
# ---------------------------------------------------------------------------

PasteSource = Callable[[], Awaitable["str | None"]]


async def wait_for_authorization_response(
    server: CallbackGate,
    *,
    timeout: float = DEFAULT_CALLBACK_TIMEOUT_S,
    paste_source: PasteSource | None = None,
    cimd_url: str | None = None,
) -> "AuthorizationCodeResult":
    """Await the redirect on *server*, optionally racing a paste hook.

    ``paste_source`` (headless/SSH surfaces) is awaited concurrently; when it
    resolves to a string it is parsed with :func:`parse_callback_input` and
    delivered through the server's one-shot gate — whichever source fills
    it first wins. ``None`` / unparseable input is ignored and the loopback
    listener keeps waiting.

    ``cimd_url`` only tailors the timeout message: a server that fetches
    the document and refuses it aborts at the *authorization* endpoint, so
    no redirect ever reaches us and a bare "timed out" hides the real cause.

    Raises:
        OAuthUserSkippedError: the paste hook returned a skip token.
        OAuthCallbackTimeoutError: nothing arrived within *timeout*.
        OAuthNeedsAuthError: the AS redirected with ``error=``.
    """
    paste_task: asyncio.Task | None = None
    if paste_source is not None:
        paste_task = asyncio.create_task(_run_paste_source(server, paste_source))
    try:
        try:
            capture = await server.wait(timeout)
        except OAuthCallbackTimeoutError as exc:
            if cimd_url:
                raise OAuthCallbackTimeoutError(
                    str(exc) + " If the browser showed an invalid-client error instead of "
                    "an approval prompt, the authorization server rejected Freyja's Client ID "
                    f"Metadata Document ({cimd_url}); set \"cimd\": false under that server's "
                    "\"oauth\" block to authorize via dynamic client registration instead.",
                    server_name=server.server_name,
                ) from None
            raise
    finally:
        if paste_task is not None and not paste_task.done():
            paste_task.cancel()
            try:
                await paste_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    if capture.skipped:
        raise OAuthUserSkippedError(
            f"user_skipped: OAuth for '{server.server_name}' skipped by the user. "
            f"{login_hint(server.server_name)}",
            server_name=server.server_name,
        )
    if capture.error:
        detail = capture.error
        if capture.error_description:
            detail += f" ({capture.error_description})"
        raise OAuthNeedsAuthError(
            f"OAuth authorization failed: {detail}. {login_hint(server.server_name)}",
            server_name=server.server_name,
        )
    assert capture.code is not None
    return authorization_code_result(capture.code, capture.state, capture.iss)


async def _run_paste_source(server: CallbackGate, source: PasteSource) -> None:
    try:
        line = await source()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — best-effort fallback
        logger.debug("mcp oauth '%s': paste source failed (non-fatal): %s",
                     server.server_name, type(exc).__name__)
        return
    if not line:
        return
    capture = parse_callback_input(line)
    if capture is None:
        logger.info("mcp oauth '%s': pasted input did not contain code= or error= — ignoring",
                    server.server_name)
        return
    if capture.skipped:
        server.deliver(capture)
        return
    if not server._state_matches(capture.state):
        logger.warning("mcp oauth '%s': pasted redirect has mismatched state — ignoring",
                       server.server_name)
        return
    server.deliver(capture)


__all__ = [
    "CALLBACK_PATH",
    "CIMD_PORTS",
    "DEFAULT_CALLBACK_TIMEOUT_S",
    "LOOPBACK_HOSTS",
    "SKIP_TOKENS",
    "CallbackCapture",
    "CallbackGate",
    "LoopbackCallbackServer",
    "PasteSource",
    "assigned_cimd_ports",
    "authorization_code_result",
    "cached_redirect_port",
    "cached_redirect_uri",
    "is_port_reserved",
    "loopback_redirect_uri",
    "note_assigned_cimd_port",
    "park_reserved_socket",
    "parse_callback_input",
    "pick_cimd_port",
    "release_reserved_port",
    "reserve_callback_port",
    "reserve_fixed_port",
    "reset_port_state_for_tests",
    "state_from_authorization_url",
    "take_reserved_socket",
    "wait_for_authorization_response",
]
