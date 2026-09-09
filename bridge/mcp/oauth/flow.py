"""Login flow orchestration: how the authorization URL reaches a human and how
the redirect gets back to the SDK.

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) —
``tools/mcp_oauth.py`` lines 384-400 (``_can_open_browser``), 800-894
(``_make_redirect_handler``), 918-1070 (``_make_callback_waiter``),
``tools/mcp_dashboard_oauth.py`` (``DashboardOAuthFlow``: the "inject an
alternate flow object" pattern) and ``hermes_cli/mcp_config.py`` lines
810-890 (``_reauth_oauth_server``: wipe -> probe -> verify a token landed).

The SDK's ``OAuthClientProvider`` takes two coroutines:

* ``redirect_handler(authorization_url)`` — show the URL to the user;
* ``callback_handler() -> AuthorizationCodeResult`` — wait for the redirect.

Both are built per provider by :func:`make_flow_handlers` (closure-scoped,
so concurrent servers never share callback state — hermes #44588/#34260)
and delegate the *human* half to a :class:`LoginFlow`:

* :class:`BrowserFlow` opens the URL locally (macOS ``open`` / ``webbrowser``)
  and waits on the loopback listener.
* :class:`HandoffFlow` hands the URL to an async callback (the bridge emits a
  desktop event / posts a Slack link) and waits on the loopback listener —
  or, when constructed with an external ``redirect_uri``, waits for the
  surface to call :meth:`HandoffFlow.deliver_callback` (hermes dashboard
  pattern: the callback lands on an already-authenticated web route
  instead of 127.0.0.1).

Both accept an optional ``paste_source`` coroutine — the headless/SSH
fallback: it resolves to a pasted redirect URL / ``?code=...&state=...``
query, or a skip token, and races the listener.

Ordering detail that differs from hermes: the loopback listener is started
*before* the URL is presented, not lazily in the callback handler. A browser
(or a test) that follows the redirect instantly would otherwise hit a
bound-but-not-listening socket and get "connection refused".

Freyja adaptation: no stderr/TTY output. All user-facing text is carried in
exceptions (``OAuthNeedsAuthError`` and subclasses) for the caller to render.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

import httpx2

from bridge.mcp.config import McpConfigError, McpServerSpec
from bridge.mcp.oauth.callback import (
    CallbackGate,
    LoopbackCallbackServer,
    PasteSource,
    state_from_authorization_url,
    wait_for_authorization_response,
)
from bridge.mcp.oauth.gates import (
    OAuthNeedsAuthError,
    force_interactive_oauth,
    login_hint,
    raise_if_non_interactive,
)
from bridge.mcp.oauth.storage import FreyjaTokenStorage
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.shared.auth import AuthorizationCodeResult

logger = logging.getLogger(__name__)

DEFAULT_LOGIN_TIMEOUT_S = 300.0


# ---------------------------------------------------------------------------
# Flow protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LoginFlow(Protocol):
    """How the authorization URL reaches a human.

    Implementations may also expose:

    * ``paste_source: PasteSource | None`` — headless paste/skip hook;
    * ``redirect_uri: str | None`` — an external (https) redirect the flow
      receives itself; when set, ``wait_for_callback(state, timeout)`` must
      be implemented and the loopback listener is not started.
    """

    async def on_authorize_url(self, url: str, *, server_name: str, redirect_uri: str) -> None:
        """Present *url* to the user (open a browser, emit an event, post a link)."""
        ...


def can_open_browser() -> bool:
    """Return True if opening a browser is likely to work."""
    # Explicit SSH session -> no local display
    if os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"):
        return False
    # macOS and Windows usually have a display
    if os.name == "nt":
        return True
    try:
        if os.uname().sysname == "Darwin":
            return True
    except AttributeError:
        pass
    # Linux/other posix: need DISPLAY or WAYLAND_DISPLAY
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


def default_open_url(url: str) -> bool:
    """Open *url* in the user's browser. macOS ``open`` first (it respects the
    default-browser setting and works from a launchd/Electron child where
    ``webbrowser`` sometimes can't find a controller), then ``webbrowser``."""
    if not can_open_browser():
        return False
    try:
        if os.uname().sysname == "Darwin" and shutil.which("open"):
            subprocess.Popen(
                ["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, close_fds=True,
            )
            return True
    except (AttributeError, OSError):
        pass
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 — webbrowser raises odd things on odd desktops
        return False


class BrowserFlow:
    """Open the authorization URL locally and await the loopback callback.

    ``opener`` (sync, returns bool) is injectable for tests. ``fallback`` is
    an optional async callback that receives the URL when the browser could
    not be opened (e.g. to surface it in the UI); without it, a failed open
    logs a warning and the flow still waits on the loopback listener / paste
    hook so a user who copies the URL by other means can finish.
    """

    def __init__(
        self,
        *,
        opener: Callable[[str], bool] | None = None,
        fallback: Callable[[str], Awaitable[None]] | None = None,
        paste_source: PasteSource | None = None,
    ) -> None:
        self._opener = opener or default_open_url
        self._fallback = fallback
        self.paste_source = paste_source
        self.redirect_uri: str | None = None
        self.last_url: str | None = None
        self.opened: bool | None = None

    async def on_authorize_url(self, url: str, *, server_name: str, redirect_uri: str) -> None:
        self.last_url = url
        try:
            self.opened = bool(await asyncio.to_thread(self._opener, url))
        except Exception:  # noqa: BLE001
            self.opened = False
        if self.opened:
            logger.info("mcp oauth '%s': browser opened for authorization", server_name)
            return
        logger.warning(
            "mcp oauth '%s': could not open a browser automatically — the authorization "
            "URL must be opened manually", server_name,
        )
        if self._fallback is not None:
            await self._fallback(url)


class HandoffFlow:
    """Hand the authorization URL to the bridge instead of opening a browser.

    ``on_authorize_url(url)`` is the bridge's coroutine (emit a desktop event,
    post a Slack link, ...). The redirect is then awaited on the loopback
    listener like BrowserFlow — unless ``redirect_uri`` is given, in which
    case the surface owns the redirect and must call
    :meth:`deliver_callback` with the parameters it received (hermes
    ``DashboardOAuthFlow`` pattern). ``paste_source`` is the headless
    paste/skip hook.
    """

    def __init__(
        self,
        on_authorize_url: Callable[[str], Awaitable[None]],
        *,
        paste_source: PasteSource | None = None,
        redirect_uri: str | None = None,
    ) -> None:
        self._on_authorize_url = on_authorize_url
        self.paste_source = paste_source
        self.redirect_uri = redirect_uri
        self.last_url: str | None = None
        self.server_name: str | None = None
        self.status: str = "starting"
        self._gate: CallbackGate | None = None

    async def on_authorize_url(self, url: str, *, server_name: str, redirect_uri: str) -> None:
        self.last_url = url
        self.server_name = server_name
        self.status = "authorization_required"
        await self._on_authorize_url(url)

    # -- external-redirect support (only when redirect_uri is set) ---------

    def _bind_gate(self, gate: CallbackGate) -> None:
        self._gate = gate

    def deliver_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None = None,
        error_description: str | None = None,
        iss: str | None = None,
    ) -> bool:
        """Feed redirect parameters received on the external redirect URI.

        Returns True when accepted. False means state mismatch, no pending
        flow, or the flow already completed.
        """
        gate = self._gate
        if gate is None:
            return False
        return gate.deliver_params(
            code=code, state=state, error=error, error_description=error_description, iss=iss
        )

    async def wait_for_callback(
        self, gate: CallbackGate, timeout: float
    ) -> AuthorizationCodeResult:
        return await wait_for_authorization_response(
            gate, timeout=timeout, paste_source=self.paste_source
        )


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


@dataclass
class _FlowSession:
    """Per-provider state shared by the redirect and callback closures."""

    gate: CallbackGate | None = None
    attempts: int = 0
    started_at: float | None = None


def make_flow_handlers(
    flow: LoginFlow,
    *,
    server_name: str,
    port: int,
    redirect_uri: str | None,
    interactive: bool | None,
    timeout: float = DEFAULT_LOGIN_TIMEOUT_S,
    cimd_url: str | None = None,
    app_name: str = "Freyja",
) -> tuple[
    Callable[[str], Awaitable[None]],
    Callable[[], Awaitable[AuthorizationCodeResult]],
]:
    """Build the SDK's ``redirect_handler`` / ``callback_handler`` pair for one provider.

    Closure-scoped so concurrent providers never share a listener or result
    (hermes #44588 / #34260). Both handlers re-check interactivity: a
    cached-but-unusable token makes the SDK fall through to the
    authorization-code flow even though the build-time token-file guard
    passed, and without this we would start a browser flow no operator can
    complete, then block for the full timeout (hermes #57836).
    """
    session = _FlowSession()
    external_redirect = getattr(flow, "redirect_uri", None)
    paste_source: PasteSource | None = getattr(flow, "paste_source", None)

    async def _redirect_handler(authorization_url: str) -> None:
        raise_if_non_interactive(
            "MCP OAuth requires browser authorization but no interactive session is "
            "available (non-interactive/background context).",
            server_name=server_name,
            explicit=interactive,
        )
        state = state_from_authorization_url(authorization_url)
        # Close a listener left over from an aborted earlier attempt.
        if session.gate is not None:
            session.gate.close()
            session.gate = None
        session.attempts += 1
        session.started_at = time.monotonic()

        if external_redirect:
            gate: CallbackGate = CallbackGate(expected_state=state, server_name=server_name)
            binder = getattr(flow, "_bind_gate", None)
            if callable(binder):
                binder(gate)
        else:
            server = LoopbackCallbackServer(
                port, expected_state=state, server_name=server_name, app_name=app_name
            )
            # Start listening BEFORE presenting the URL so an immediate
            # redirect is never refused (see module docstring).
            server.start()
            gate = server
        session.gate = gate

        if redirect_uri:
            effective_redirect = redirect_uri
        elif external_redirect:
            effective_redirect = str(external_redirect)
        else:
            effective_redirect = gate.redirect_uri()  # type: ignore[attr-defined]
        try:
            await flow.on_authorize_url(
                authorization_url, server_name=server_name, redirect_uri=effective_redirect
            )
        except BaseException:
            gate.close()
            session.gate = None
            raise

    async def _callback_handler() -> AuthorizationCodeResult:
        raise_if_non_interactive(
            "OAuth callback requires an interactive session but none is available "
            "(non-interactive/background context); skipping browser authorization "
            "without binding a callback listener.",
            server_name=server_name,
            explicit=interactive,
        )
        gate = session.gate
        if gate is None:
            raise OAuthFlowError("OAuth callback requested before the redirect handler ran")
        try:
            waiter = getattr(flow, "wait_for_callback", None)
            if external_redirect and callable(waiter):
                return await waiter(gate, timeout)
            return await wait_for_authorization_response(
                gate, timeout=timeout, paste_source=paste_source, cimd_url=cimd_url
            )
        finally:
            gate.close()
            session.gate = None

    return _redirect_handler, _callback_handler


# ---------------------------------------------------------------------------
# run_login
# ---------------------------------------------------------------------------


@dataclass
class LoginResult:
    """Outcome of :func:`run_login`."""

    ok: bool
    error: str | None = None
    expires_at: float | None = None
    scopes: list[str] = field(default_factory=list)
    server_name: str = ""
    client_id: str | None = None
    skipped: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


def _probe_request_body() -> dict[str, Any]:
    """A minimal MCP ``initialize`` request. The server must answer 401 when
    unauthenticated (which triggers the SDK flow) and anything-but-401 once a
    valid bearer is attached. We never open a session — the httpx round trip
    is all the login needs."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "freyja", "version": "login-probe"},
        },
    }


async def run_login(
    spec: McpServerSpec,
    *,
    flow: LoginFlow,
    storage_root: str | os.PathLike[str] | None = None,
    timeout: float = DEFAULT_LOGIN_TIMEOUT_S,
    fresh: bool = True,
    environ: dict[str, str] | None = None,
    probe_timeout_s: float = 30.0,
) -> LoginResult:
    """Drive a complete (re)authorization for one ``auth: oauth`` server.

    Mirrors hermes ``_reauth_oauth_server``: snapshot + wipe the stored OAuth
    state (``fresh=True``) so the flow starts from discovery, force the
    interactive gate, send one probe request through the provider (its 401
    triggers the SDK flow, which calls back into *flow*), then verify a
    token actually landed on disk before reporting success. A clean HTTP
    response is NOT proof of authentication — some servers answer
    ``initialize`` without auth — so the on-disk token is the source of
    truth. On failure the previous state is restored (unless newer state
    appeared meanwhile) so a still-valid token isn't destroyed by a botched
    re-auth.
    """
    from bridge.mcp.oauth.provider import build_httpx_auth, humanize_oauth_registration_error

    if spec.auth != "oauth" or not spec.url:
        return LoginResult(
            ok=False, server_name=spec.name,
            error=f"server '{spec.name}' is not configured for OAuth (auth={spec.auth!r})",
        )

    storage = FreyjaTokenStorage(spec.name, root=storage_root)
    snapshot = storage.snapshot() if fresh else {}
    if fresh:
        storage.remove()

    def _restore() -> None:
        if fresh and snapshot:
            storage.restore(snapshot, only_if_absent=True)

    try:
        with force_interactive_oauth():
            auth = build_httpx_auth(
                spec, interactive=True, flow=flow, storage_root=storage_root,
                timeout=timeout, environ=environ,
            )
            if auth is None:  # pragma: no cover — guarded above
                return LoginResult(ok=False, server_name=spec.name, error="not an OAuth server")
            async with httpx2.AsyncClient(
                auth=auth, timeout=httpx2.Timeout(probe_timeout_s), follow_redirects=True
            ) as client:
                response = await client.post(
                    spec.url,
                    json=_probe_request_body(),
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                )
    except OAuthNeedsAuthError as exc:
        _restore()
        skipped = "user_skipped" in str(exc)
        return LoginResult(ok=False, server_name=spec.name, error=str(exc), skipped=skipped)
    except (OAuthFlowError, McpConfigError) as exc:
        _restore()
        humanized = humanize_oauth_registration_error(spec.name, exc, server_url=spec.url)
        return LoginResult(ok=False, server_name=spec.name, error=humanized or str(exc))
    except httpx2.HTTPError as exc:
        _restore()
        return LoginResult(
            ok=False, server_name=spec.name,
            error=f"could not reach {spec.url}: {type(exc).__name__}",
        )

    tokens = await storage.get_tokens()
    if tokens is None:
        _restore()
        return LoginResult(
            ok=False, server_name=spec.name,
            error=(
                f"server responded ({response.status_code}) but no OAuth token was obtained — "
                "authentication did not complete. Some providers do not support automatic "
                "client registration; add oauth.client_id / oauth.client_secret "
                f"(\"${{VAR}}\" reference) to the server config, then re-run "
                f"/mcp login {spec.name}."
            ),
            detail={"status_code": response.status_code},
        )
    if response.status_code == 401:
        return LoginResult(
            ok=False, server_name=spec.name,
            error=(
                f"authorization completed but '{spec.name}' still answered 401 to the "
                f"authenticated probe. {login_hint(spec.name)}"
            ),
            expires_at=storage.expires_at(),
            detail={"status_code": 401},
        )

    client_info = storage.read_client_info_raw() or {}
    return LoginResult(
        ok=True,
        server_name=spec.name,
        expires_at=storage.expires_at(),
        scopes=(tokens.scope or "").split(),
        client_id=client_info.get("client_id"),
        detail={"status_code": response.status_code},
    )


__all__ = [
    "DEFAULT_LOGIN_TIMEOUT_S",
    "BrowserFlow",
    "HandoffFlow",
    "LoginFlow",
    "LoginResult",
    "can_open_browser",
    "default_open_url",
    "make_flow_handlers",
    "run_login",
]
