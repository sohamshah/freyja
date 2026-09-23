"""``FreyjaOAuthClientProvider`` — the MCP SDK provider with real-world fixes —
and ``build_httpx_auth``, the entry point connection.py uses.

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) —
``tools/mcp_oauth.py`` lines 1164-1265 (``_HermesOAuthClientProvider``),
1538-1622 (port/redirect resolution), 1625-1736 (Figma defaults + client
metadata), 1794-1957 (pre-registration, humanized errors,
``build_oauth_auth``) and ``tools/mcp_oauth_manager.py`` lines 105-600
(``HermesMCPOAuthProvider``: cold-load expiry seeding, metadata prefetch +
persistence, ``invalid_client`` poison detection, bidirectional
``async_auth_flow`` bridge with lock narrowing).

SDK adaptation notes (mcp 2.1.1 vs the hermes code paths):

* The SDK is built on **httpx2** (``pip install httpx2``, a fork with the
  same public API). ``OAuthClientProvider`` subclasses ``httpx2.Auth``; the
  ``AsyncClient`` you hand to ``streamable_http_client(http_client=...)``
  must be an ``httpx2.AsyncClient``. ``build_httpx_auth`` therefore returns
  an ``httpx2.Auth``.
* ``OAuthClientProvider.__init__`` has no ``timeout`` kwarg; the browser
  round-trip budget lives in the callback waiter (flow.py).
* ``callback_handler`` returns ``AuthorizationCodeResult`` (callback.py).
* ``OAuthClientMetadata.application_type`` exists natively ("native" default,
  SEP-837) — no retry-without-field fallback needed.
* ``_handle_refresh_response`` in 2.1.1 carries the previous ``scope`` and
  ``refresh_token`` forward when the AS omits them (RFC 6749 section 6);
  our sanitized override preserves that behaviour.
* ``_handle_token_response`` in 2.1.1 fills a missing ``scope`` from
  ``client_metadata.scope`` (RFC 6749 section 5.1); preserved too.
* ``context.lock`` is an ``anyio.Lock`` owned by the task that acquired it;
  streamable HTTP's session-long GET would hold it for the session lifetime
  and block every concurrent POST. Like hermes we swap in an
  ``anyio.Semaphore(1)`` and release it only around the resource request.

Freyja adaptations: interactivity is explicit (``interactive=``), the
``oauth`` block is parsed by settings.py, and a 401 that survives a freshly
minted token raises :class:`OAuthNeedsAuthError` instead of returning the
response, so connection.py can park the server in NEEDS_AUTH without
pattern-matching error text.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio
import httpx2
from pydantic import AnyUrl, ValidationError

from bridge.mcp.config import McpServerSpec
from bridge.mcp.oauth.callback import (
    cached_redirect_port,
    cached_redirect_uri,
    loopback_redirect_uri,
    note_assigned_cimd_port,
    reserve_callback_port,
)
from bridge.mcp.oauth.cimd import cimd_provider_kwargs, maybe_use_cimd
from bridge.mcp.oauth.gates import (
    OAuthNeedsAuthError,
    OAuthNonInteractiveError,
    is_interactive,
    login_hint,
)
from bridge.mcp.oauth.settings import OAuthSettings, parse_oauth_settings
from bridge.mcp.oauth.storage import FreyjaTokenStorage
from mcp.client.auth.exceptions import OAuthTokenError
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

if TYPE_CHECKING:  # pragma: no cover
    from bridge.mcp.oauth.flow import LoginFlow

logger = logging.getLogger(__name__)

OAUTH_AUTH_VALUE = "oauth"

_PREFETCH_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _same_endpoint(a: str, b: str) -> bool:
    """Return True if two URLs target the same endpoint (ignoring query/fragment).

    Compares scheme, host (case-insensitive), and path. Used to confirm a
    rejected response actually came from the OAuth token endpoint before we
    act on an ``invalid_client`` body.
    """
    try:
        pa, pb = urlsplit(a), urlsplit(b)
    except ValueError:  # pragma: no cover — malformed URL
        return False
    return (
        pa.scheme == pb.scheme
        and pa.netloc.lower() == pb.netloc.lower()
        and pa.path.rstrip("/") == pb.path.rstrip("/")
    )


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(url: str, domain: str) -> bool:
    host = _hostname(url)
    return host == domain or host.endswith("." + domain)


def _safe_error_fields(body: bytes | None) -> str:
    """Render only the RFC 6749 ``error`` / ``error_description`` fields of a
    token-endpoint error body — never the whole body (which could echo
    credentials or a token)."""
    if not body:
        return ""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    parts = []
    err = doc.get("error")
    if isinstance(err, str) and err:
        parts.append(err[:64])
    desc = doc.get("error_description")
    if isinstance(desc, str) and desc:
        parts.append(desc[:200])
    return ": ".join(parts)


# ---------------------------------------------------------------------------
# Provider-specific defaults (Figma)
# ---------------------------------------------------------------------------

# Figma's remote MCP (https://mcp.figma.com/mcp) implements RFC 7591 DCR as a
# *name allowlist*, not open registration. POST /v1/oauth/mcp/register returns
# 403 Forbidden for any client_name outside a short fixed set. Empirically (as
# of 2026-07, verified by hermes against api.figma.com):
#   "Claude Code" -> 200
#   "Codex"       -> 200
#   "Hermes Agent" / "Hermes" / "Cursor" / "VS Code" / "Freyja" -> 403
# Register under an allowlisted name so the browser flow can start. The user
# can still pin a different name via oauth.client_name if Figma admits one.
FIGMA_DCR_CLIENT_NAME = "Claude Code"
FIGMA_DEFAULT_SCOPE = "mcp:connect"


def is_figma_remote_mcp(server_name: str | None = None, server_url: str | None = None) -> bool:
    """True when this MCP server is Figma's hosted remote endpoint."""
    url = (server_url or "").lower()
    name = (server_name or "").lower()
    if _host_matches(url, "mcp.figma.com") or (_host_matches(url, "figma.com") and "/mcp" in url):
        return True
    # Name-only match only when the URL isn't some other host called figma-*.
    if "figma" in name and (not url or "figma" in _hostname(url)):
        return True
    return False


def apply_provider_defaults(
    settings: OAuthSettings,
    *,
    server_name: str = "",
    server_url: str | None = None,
) -> OAuthSettings:
    """Mutate *settings* with provider-specific OAuth workarounds. Returns it.

    Call this before :func:`build_client_metadata` /
    :func:`maybe_preregister_client`. Only fills keys the user left unset —
    an explicit ``oauth.client_name`` / ``oauth.scope`` always wins.
    """
    if is_figma_remote_mcp(server_name, server_url):
        if not settings.client_name:
            settings.client_name = FIGMA_DCR_CLIENT_NAME
            logger.info(
                "mcp oauth '%s': Figma DCR allowlist — registering as client_name=%r "
                "(override via oauth.client_name)",
                server_name or server_url, FIGMA_DCR_CLIENT_NAME,
            )
        if not settings.scope:
            settings.scope = FIGMA_DEFAULT_SCOPE
        # Figma's register response advertises token_endpoint_auth_method=none
        # *and* returns a client_secret — then the token endpoint rejects the
        # exchange with "Client secret is required". Request confidential-
        # client registration so the SDK includes client_secret on the token
        # POST (auth method client_secret_post).
        if not settings.token_endpoint_auth_method:
            settings.token_endpoint_auth_method = "client_secret_post"
    return settings


def humanize_oauth_registration_error(
    server_name: str,
    exc: BaseException | str,
    *,
    server_url: str | None = None,
) -> str | None:
    """Turn a Dynamic Client Registration refusal into a useful next step.

    Returns a humanized message when the error is a registration
    403/Forbidden, else ``None`` so the caller keeps the original text.
    """
    msg = str(exc)
    lowered = msg.lower()
    if "403" not in msg and "forbidden" not in lowered:
        return None
    looks_like_registration = (
        "regist" in lowered
        or "client registration" in lowered
        or "dcr" in lowered
        or "dynamic client" in lowered
        or lowered.strip() in {"forbidden", "403 forbidden", "http 403: forbidden"}
        or ("403" in msg and "forbidden" in lowered)
    )
    if not looks_like_registration:
        return None

    if is_figma_remote_mcp(server_name, server_url):
        return (
            f"'{server_name}' is Figma's remote MCP — DCR is allowlisted by exact "
            f"client_name (\"{FIGMA_DCR_CLIENT_NAME}\" and \"Codex\" work; most other "
            f"names 403). Freyja defaults to client_name: {FIGMA_DCR_CLIENT_NAME!r} "
            "automatically. If you set oauth.client_name yourself, change it to one of "
            f"those, or clear it and re-run: /mcp login {server_name}"
        )
    return (
        f"'{server_name}' only allows pre-approved OAuth clients — it rejected client "
        "registration (403), so no browser flow can start. Options: set oauth.client_name "
        "to a name the provider allowlists, add a pre-registered client "
        "(oauth: {client_id: ..., client_secret: \"${VAR}\"}), or use the provider's "
        "stdio / API-key / local server instead."
    )


# ---------------------------------------------------------------------------
# Port / redirect resolution + client metadata
# ---------------------------------------------------------------------------


def resolve_redirect_uri(settings: OAuthSettings, port: int) -> str:
    """Resolve the OAuth callback URL: configured ``redirect_uri`` or loopback.

    A configured ``redirect_uri`` lets the callback go through a proxy (e.g.
    a Tailscale Funnel exposing a public HTTPS URL that forwards to
    localhost); otherwise we default to ``http://<redirect_host>:<port>/callback``.
    Both the client metadata and any pre-registered client info must derive
    the redirect_uri here so they stay identical — a mismatch makes the
    authorization server reject the callback.

    ``redirect_host`` (default ``127.0.0.1``) tweaks only the hostname of
    the loopback callback. Some providers' WAFs (e.g. Reclaim.ai's AWS API
    Gateway) reject any authorize request whose query string contains a
    literal ``127.0.0.1``; ``redirect_host: localhost`` works around that.
    The callback listener still binds ``127.0.0.1`` either way.
    """
    if settings.redirect_uri:
        return settings.redirect_uri
    return loopback_redirect_uri(port, settings.redirect_host or "127.0.0.1")


def resolve_callback_port(
    settings: OAuthSettings,
    storage: FreyjaTokenStorage | None = None,
    *,
    external_redirect_uri: str | None = None,
) -> int:
    """Pick or validate the OAuth callback port; stores it in ``settings.resolved_port``.

    Port choice precedence:
    1. an external (flow-supplied or cached https) redirect URI -> port 0
    2. a pinned CIMD port, when the flow is CIMD-eligible (also records
       ``settings.cimd_url``)
    3. explicit ``oauth.redirect_port`` config
    4. cached client registration redirect URI port
    5. newly reserved free port (TOCTOU-safe, see callback.py)
    """
    if external_redirect_uri:
        settings.redirect_uri = settings.redirect_uri or external_redirect_uri
        settings.resolved_port = 0
        return 0
    cached_uri = cached_redirect_uri(storage)
    if not settings.redirect_uri and cached_uri:
        settings.redirect_uri = cached_uri
        settings.resolved_port = 0
        return 0
    cimd = maybe_use_cimd(settings, storage)
    if cimd is not None:
        settings.cimd_url, port = cimd
        settings.resolved_port = port
        return port
    requested = int(settings.redirect_port or 0)
    # Precedence: explicit config port -> cached client-registration port ->
    # fresh ephemeral port. The cached port keeps re-auth consistent with
    # the redirect URI pinned at dynamic client registration (providers
    # reject a mismatched URI). Only a truly fresh ephemeral pick goes
    # through reserve_callback_port(), which keeps the socket bound until
    # the callback server adopts it — closing the select->bind TOCTOU race
    # (#22161). Explicit and cached ports are fixed, known values and bind
    # via the reuse_address path instead.
    port = requested or cached_redirect_port(storage) or reserve_callback_port()
    # A cached port can be one of the pinned CIMD ports, left behind by an
    # earlier CIMD login for this server. Claim it so a sibling server's
    # pick_cimd_port doesn't hand the same port out a second time.
    note_assigned_cimd_port(port)
    settings.resolved_port = port
    return port


def build_client_metadata(settings: OAuthSettings) -> OAuthClientMetadata:
    """Build OAuthClientMetadata from the resolved settings.

    Requires ``settings.resolved_port`` (see :func:`resolve_callback_port`).
    """
    port = settings.resolved_port
    if port is None:
        raise ValueError("resolve_callback_port() must be called before build_client_metadata()")
    redirect_uri = resolve_redirect_uri(settings, port)

    # Default public client; confidential only when a secret is already
    # known or the provider (e.g. Figma) needs confidential-style token posts.
    auth_method = settings.token_endpoint_auth_method
    if not auth_method:
        auth_method = "client_secret_post" if settings.client_secret else "none"

    metadata_kwargs: dict[str, Any] = {
        "client_name": settings.effective_client_name,
        "redirect_uris": [AnyUrl(redirect_uri)],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
        # SEP-837 (2026-07-28 spec): clients MUST declare an application_type
        # during registration so OIDC-strict authorization servers stop
        # rejecting loopback redirect_uris. Freyja is a desktop app
        # redirecting to 127.0.0.1/localhost — exactly "native".
        "application_type": settings.application_type or "native",
    }
    if settings.scope:
        metadata_kwargs["scope"] = settings.scope
    return OAuthClientMetadata.model_validate(metadata_kwargs)


def maybe_preregister_client(
    storage: FreyjaTokenStorage,
    settings: OAuthSettings,
    client_metadata: OAuthClientMetadata,
) -> None:
    """If settings carry a pre-registered client_id, persist it to storage."""
    client_id = settings.client_id
    if not client_id:
        return
    storage.invalidate_on_client_change(client_id, settings.client_secret)
    port = settings.resolved_port or 0
    redirect_uri = resolve_redirect_uri(settings, port)

    info_dict: dict[str, Any] = {
        "client_id": client_id,
        "redirect_uris": [redirect_uri],
        "grant_types": client_metadata.grant_types,
        "response_types": client_metadata.response_types,
        "token_endpoint_auth_method": client_metadata.token_endpoint_auth_method,
    }
    if settings.client_secret:
        info_dict["client_secret"] = settings.client_secret
    if settings.client_name:
        info_dict["client_name"] = settings.client_name
    if settings.scope:
        info_dict["scope"] = settings.scope

    client_info = OAuthClientInformationFull.model_validate(info_dict)
    # Bypass set_client_info's coercion: a pre-registered public client
    # (no secret) legitimately uses "none".
    from bridge.mcp.oauth.storage import write_json

    write_json(storage.client_info_path(), client_info.model_dump(mode="json", exclude_none=True))
    logger.debug("mcp oauth '%s': pre-registered client_id=%s", storage.server_name, client_id)


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class FreyjaOAuthClientProvider(OAuthClientProvider):
    """OAuth provider with pragmatic fixes for real-world MCP providers.

    * ``_coerce_client_secret_post``: Supabase MCP dynamic registration
      returns ``client_secret`` but omits ``token_endpoint_auth_method``;
      the SDK treats that as ``none`` and omits the secret from the token
      request. Coerced in-memory right before every exchange/refresh.
    * ``token_user_agent``: stamped onto token-endpoint requests only —
      some authorization servers / WAFs reject httpx's default UA.
    * Sanitized token/refresh error handling: never echoes response bodies.
    * Cold-load: seeds ``token_expiry_time`` from the reloaded token, restores
      AS metadata from disk, prefetches it when absent, persists it after a
      lazy discovery.
    * ``invalid_client`` on the token endpoint -> poison the cached
      registration (and record CIMD refusal) so the next flow re-registers.
    * Narrows the SDK's context lock to exclude the resource request.
    * A 401 that survives a fresh authorization raises OAuthNeedsAuthError.
    """

    def __init__(
        self,
        *args: Any,
        server_name: str = "",
        preregistered: bool = False,
        token_user_agent: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # mcp 2.x uses a task-owned anyio.Lock and holds it across the
        # yielded resource request. A session-long GET therefore blocks every
        # concurrent POST, and httpx may later close the auth-flow generator
        # from a different task than the lock owner. A binary semaphore
        # preserves mutual exclusion without task ownership; async_auth_flow
        # below narrows its scope around resource I/O.
        self.context.lock = anyio.Semaphore(1, max_value=1)  # type: ignore[assignment]
        self.server_name = server_name
        # When the client_id comes from config (pre-registered), an
        # invalid_client rejection means the *config* is wrong — deleting
        # client.json would just be re-seeded from config and re-running
        # registration can't help. Only auto-heal dynamically-registered
        # clients.
        self.preregistered = preregistered
        self.token_user_agent = token_user_agent
        self.last_poisoned = False
        self.performed_authorization = False
        # Cross-process token sync (see FreyjaTokenStorage.acquire_refresh_lock).
        self._disk_mtime_ns: int | None = None
        self._refresh_lock_fd: int | None = None
        # Identity of the auth flow holding the flock, so one flow's cleanup
        # never releases a lock a concurrent flow took.
        self._refresh_lock_owner: object | None = None

    # -- request shaping ----------------------------------------------------

    def _stamp_token_user_agent(self, request: httpx2.Request) -> httpx2.Request:
        if self.token_user_agent:
            request.headers["User-Agent"] = self.token_user_agent
        return request

    def _coerce_client_secret_post(self) -> None:
        info = getattr(self.context, "client_info", None)
        if not info or not getattr(info, "client_secret", None):
            return
        method = getattr(info, "token_endpoint_auth_method", None)
        if method not in (None, "none", ""):
            return
        data = info.model_dump(mode="json", exclude_none=True)
        data["token_endpoint_auth_method"] = "client_secret_post"
        self.context.client_info = OAuthClientInformationFull.model_validate(data)

    async def _exchange_token_authorization_code(self, *args: Any, **kwargs: Any) -> httpx2.Request:
        self._coerce_client_secret_post()
        request = await super()._exchange_token_authorization_code(*args, **kwargs)
        return self._stamp_token_user_agent(request)

    async def _refresh_token(self) -> httpx2.Request:
        self._coerce_client_secret_post()
        request = await super()._refresh_token()
        return self._stamp_token_user_agent(request)

    # -- response handling (sanitized) ---------------------------------------

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        """Accept any 2xx token response and avoid leaking token bodies in errors."""
        if 200 <= response.status_code < 300:
            try:
                content = await response.aread()
                token_response = OAuthToken.model_validate_json(content)
            except (httpx2.HTTPError, ValidationError, ValueError):
                raise OAuthTokenError("Invalid token response") from None
            # RFC 6749 section 5.1: an omitted scope means the granted scope
            # equals the requested scope (mcp 2.1.1 behaviour, preserved).
            if token_response.scope is None:
                token_response.scope = self.context.client_metadata.scope
            self.context.current_tokens = token_response
            self.context.update_token_expiry(token_response)
            await self.context.storage.set_tokens(token_response)
            return

        detail = ""
        try:
            detail = _safe_error_fields(await response.aread())
        except httpx2.HTTPError:
            pass
        message = f"Token exchange failed ({response.status_code})"
        if detail:
            message += f": {detail}"
        raise OAuthTokenError(message)

    async def _handle_refresh_response(self, response: httpx2.Response) -> bool:
        try:
            return await self._handle_refresh_response_locked(response)
        finally:
            # The refresh lock taken in _sync_before_flow covers exactly one
            # "refresh → write tokens.json" — release once the write landed.
            self._release_refresh_lock()

    async def _handle_refresh_response_locked(self, response: httpx2.Response) -> bool:
        """Accept any 2xx refresh response and avoid logging token bodies."""
        if not (200 <= response.status_code < 300):
            detail = ""
            try:
                detail = _safe_error_fields(await response.aread())
            except httpx2.HTTPError:
                pass
            logger.warning(
                "mcp oauth '%s': token refresh failed: %s%s",
                self.server_name, response.status_code, f" ({detail})" if detail else "",
            )
            return await self._recover_failed_refresh()
        try:
            content = await response.aread()
            token_response = OAuthToken.model_validate_json(content)
        except (httpx2.HTTPError, ValidationError, ValueError):
            # Slack's token endpoint reports errors as HTTP 200 + ok:false,
            # which lands here rather than in the status branch above.
            logger.warning("mcp oauth '%s': invalid refresh response: %s",
                           self.server_name, response.status_code)
            return await self._recover_failed_refresh()
        # RFC 6749 section 6: a refresh response may omit scope (unchanged)
        # and refresh_token (the AS does not rotate). Carry both forward so
        # the persisted token stays self-describing and the next expiry can
        # still refresh instead of forcing a full re-authorization.
        prior = self.context.current_tokens
        if token_response.scope is None and prior is not None:
            token_response.scope = prior.scope
        if token_response.refresh_token is None and prior is not None:
            token_response.refresh_token = prior.refresh_token
        self.context.current_tokens = token_response
        self.context.update_token_expiry(token_response)
        await self.context.storage.set_tokens(token_response)
        return True

    # -- cold load ---------------------------------------------------------

    def _freyja_storage(self) -> FreyjaTokenStorage | None:
        storage = self.context.storage
        return storage if isinstance(storage, FreyjaTokenStorage) else None

    # -- cross-process token sync --------------------------------------------

    def _seed_expiry(self, tokens: OAuthToken) -> None:
        if tokens.expires_in is None:
            return
        self.context.update_token_expiry(tokens)
        if int(tokens.expires_in) <= 0 and self.context.token_expiry_time is not None:
            # Already expired by wall clock: make is_token_valid() False
            # deterministically rather than relying on sub-second drift.
            self.context.token_expiry_time -= 1.0

    async def _adopt_disk_tokens(self, *, force: bool = False) -> bool:
        """Swap in tokens.json when another process rewrote it.

        Returns True if the in-memory tokens changed. Cheap when nothing
        changed: a stat, no read, unless ``force``.
        """
        storage = self._freyja_storage()
        if storage is None:
            return False
        mtime = storage.tokens_mtime_ns()
        if not force and mtime == self._disk_mtime_ns:
            return False
        self._disk_mtime_ns = mtime
        disk = await storage.get_tokens()
        if disk is None:
            return False
        cur = self.context.current_tokens
        if (
            cur is not None
            and cur.access_token == disk.access_token
            and cur.refresh_token == disk.refresh_token
        ):
            return False
        self.context.current_tokens = disk
        self.context.token_expiry_time = None
        self._seed_expiry(disk)
        logger.info(
            "mcp oauth '%s': adopted tokens refreshed by another process", self.server_name
        )
        return True

    async def _sync_before_flow(self, owner: object) -> None:
        """Before the SDK decides whether to refresh: pick up tokens another
        process wrote, and if a refresh is still due, take the cross-process
        refresh lock so exactly one process spends the refresh token.

        Never waits on the flock while holding ``context.lock`` — a flow in
        this process that already holds the flock needs ``context.lock`` to
        finish its refresh.
        """
        storage = self._freyja_storage()
        if storage is None:
            return
        async with self.context.lock:
            if not self._initialized:
                await self._initialize()
            await self._adopt_disk_tokens()
            due = not self.context.is_token_valid() and self.context.can_refresh_token()
        if not due:
            return
        fd = await storage.acquire_refresh_lock()
        async with self.context.lock:
            # Whoever held the lock before us may have just refreshed.
            await self._adopt_disk_tokens(force=True)
            still_due = not self.context.is_token_valid() and self.context.can_refresh_token()
            if still_due and fd is not None and self._refresh_lock_fd is None:
                self._refresh_lock_fd = fd
                self._refresh_lock_owner = owner
                return
        storage.release_refresh_lock(fd)

    def _release_refresh_lock(self, owner: object | None = None) -> None:
        """Release the flock; with ``owner``, only if that flow holds it."""
        if owner is not None and owner is not self._refresh_lock_owner:
            return
        fd, self._refresh_lock_fd = self._refresh_lock_fd, None
        self._refresh_lock_owner = None
        FreyjaTokenStorage.release_refresh_lock(fd)

    async def _recover_failed_refresh(self) -> bool:
        """A refresh was rejected. If tokens.json holds a different token
        set (a process without the lock — e.g. an older build — rotated it),
        adopt it instead of wiping credentials that are still good."""
        if await self._adopt_disk_tokens(force=True):
            return self.context.is_token_valid()
        self.context.clear_tokens()
        return False

    async def _initialize(self) -> None:
        """Load stored tokens + client info AND seed ``token_expiry_time``.

        The SDK's base ``_initialize`` populates ``current_tokens`` but does
        NOT call ``update_token_expiry``, so ``token_expiry_time`` stays
        ``None`` and ``is_token_valid()`` returns True for any loaded token
        regardless of actual age. After a process restart this ships stale
        Bearer tokens to the server. Seeding it from the reloaded token
        (whose ``expires_in`` storage.py already rewrote from the absolute
        ``expires_at``) makes ``async_auth_flow`` take the
        ``can_refresh_token()`` branch and refresh before the first request.

        Also restores OAuth AS metadata from disk (and prefetches it when
        we have tokens but no cache) so ``_refresh_token`` has the correct
        ``token_endpoint`` instead of the SDK's guessed ``{server_url}/token``.
        """
        storage = self._freyja_storage()
        if storage is not None:
            self._disk_mtime_ns = storage.tokens_mtime_ns()
        await super()._initialize()
        tokens = self.context.current_tokens
        if tokens is not None:
            self._seed_expiry(tokens)

        if storage is not None and self.context.oauth_metadata is None:
            meta = storage.load_oauth_metadata()
            if meta is not None:
                self.context.oauth_metadata = meta
                logger.debug(
                    "mcp oauth '%s': restored AS metadata from disk (token_endpoint=%s)",
                    self.server_name, meta.token_endpoint,
                )

        if tokens is not None and self.context.oauth_metadata is None:
            try:
                await self._prefetch_oauth_metadata()
            except Exception as exc:  # noqa: BLE001 — defensive, non-fatal
                logger.debug(
                    "mcp oauth '%s': pre-flight metadata discovery failed (non-fatal): %s",
                    self.server_name, type(exc).__name__,
                )

    async def _prefetch_oauth_metadata(self) -> None:
        """Fetch PRM + ASM from the well-known endpoints, cache on context + disk.

        Mirrors the SDK's 401-branch discovery but runs before the first
        request instead of inside the httpx auth_flow generator. Uses the
        SDK's own URL builders and response handlers so we track whatever
        the pinned SDK version expects.
        """
        from mcp.client.auth.utils import (
            build_oauth_authorization_server_metadata_discovery_urls,
            build_protected_resource_metadata_discovery_urls,
            create_oauth_metadata_request,
            handle_auth_metadata_response,
            handle_protected_resource_response,
        )

        server_url = self.context.server_url
        async with httpx2.AsyncClient(timeout=_PREFETCH_TIMEOUT_S) as client:
            for url in build_protected_resource_metadata_discovery_urls(None, server_url):
                try:
                    resp = await client.send(create_oauth_metadata_request(url))
                except httpx2.HTTPError as exc:
                    logger.debug("mcp oauth '%s': PRM discovery failed: %s",
                                 self.server_name, type(exc).__name__)
                    continue
                prm = await handle_protected_resource_response(resp)
                if prm:
                    self.context.protected_resource_metadata = prm
                    if prm.authorization_servers:
                        self.context.auth_server_url = str(prm.authorization_servers[0])
                    break

            for url in build_oauth_authorization_server_metadata_discovery_urls(
                self.context.auth_server_url, server_url
            ):
                try:
                    resp = await client.send(create_oauth_metadata_request(url))
                except httpx2.HTTPError as exc:
                    logger.debug("mcp oauth '%s': ASM discovery failed: %s",
                                 self.server_name, type(exc).__name__)
                    continue
                ok, asm = await handle_auth_metadata_response(resp)
                if not ok:
                    break
                if asm:
                    self.context.oauth_metadata = asm
                    storage = self._freyja_storage()
                    if storage is not None:
                        storage.save_oauth_metadata(asm)
                    logger.debug("mcp oauth '%s': pre-flight ASM discovered token_endpoint=%s",
                                 self.server_name, asm.token_endpoint)
                    break

    def _persist_oauth_metadata_if_changed(self) -> None:
        """Persist discovered OAuth metadata for future process restarts.

        Called after the SDK's normal 401-branch auth flow completes so
        metadata discovered via the lazy path (not pre-flight) is also
        saved. No-op when nothing to persist or metadata hasn't changed.
        """
        meta = self.context.oauth_metadata
        if meta is None:
            return
        storage = self._freyja_storage()
        if storage is None:
            return
        existing = storage.load_oauth_metadata()
        if existing is None or existing.model_dump(mode="json") != meta.model_dump(mode="json"):
            storage.save_oauth_metadata(meta)

    # -- invalid_client detection -------------------------------------------

    async def _maybe_flag_poisoned_client(self, response: Any) -> None:
        """Detect a dead client registration and force re-registration.

        Conservative by construction — acts ONLY when all hold:
          * status is 400/401,
          * the request hit the discovered ``token_endpoint`` (the only
            request carrying our ``client_id``), and
          * the body carries the ``invalid_client`` error code
            (word-boundary match, so RFC 7591's ``invalid_client_metadata``
            registration error does not trip it).
        Pre-registered (config-supplied) clients are never poisoned. Fully
        best-effort: any failure here is swallowed so a detection miss never
        breaks the live auth flow.
        """
        try:
            if self.preregistered:
                return
            status = getattr(response, "status_code", None)
            if status not in (400, 401):
                return
            meta = self.context.oauth_metadata
            token_endpoint = (
                str(meta.token_endpoint) if meta is not None and meta.token_endpoint else None
            )
            req = getattr(response, "request", None)
            req_url = str(req.url) if req is not None else None
            if not token_endpoint or not req_url:
                return
            if not _same_endpoint(req_url, token_endpoint):
                return
            body = await response.aread()
            if not re.search(rb"\binvalid_client\b", body.lower()):
                return

            storage = self._freyja_storage()

            # When the rejected client_id was our Client ID Metadata Document
            # URL, re-presenting it next flow would loop: the server has
            # already fetched that document and refused it. Dropping the URL
            # sends the retry down the DCR branch instead, and the marker on
            # disk keeps the next process from walking back into the same
            # refusal. `/mcp login` clears the marker.
            cimd_url = self.context.client_metadata_url
            rejected_id = getattr(self.context.client_info, "client_id", None)
            if cimd_url and rejected_id == cimd_url:
                logger.warning(
                    "mcp oauth '%s': authorization server rejected our Client ID Metadata "
                    "Document (%s) with invalid_client — falling back to dynamic client "
                    "registration.",
                    self.server_name, cimd_url,
                )
                self.context.client_metadata_url = None
                if storage is not None:
                    storage.mark_cimd_rejected()

            if storage is not None:
                storage.poison_client_registration()
            # Drop the in-memory client so the SDK re-registers next flow.
            self.context.client_info = None
            self._initialized = False
            self.last_poisoned = True
        except Exception as exc:  # noqa: BLE001 — defensive, must not throw
            logger.debug("mcp oauth '%s': invalid_client detection failed (non-fatal): %s",
                         self.server_name, type(exc).__name__)

    # -- auth flow bridge ----------------------------------------------------

    async def async_auth_flow(self, request: httpx2.Request):  # type: ignore[override]
        """Bidirectional bridge over the SDK generator.

        httpx's auth driver calls ``auth_flow.asend(response)`` to feed HTTP
        responses back into the generator. A naive wrapper using ``async for
        item in inner: yield item`` DISCARDS those values and resumes the
        inner generator with None, so the SDK's ``response = yield request``
        sees ``None`` and crashes. The bridge below forwards each ``asend``
        value into the inner generator, preserving the contract, while
        sniffing responses for ``invalid_client`` and releasing the context
        lock around the resource request.
        """
        flow_id = object()
        await self._sync_before_flow(flow_id)
        inner = super().async_auth_flow(request)
        resource_lock_released = False
        sent_access_token: str | None = None
        retry_after_concurrent_auth = False
        resource_round_trips = 0
        retry_rejected_status: int | None = None
        try:
            outgoing = await inner.__anext__()
            while True:
                is_resource = outgoing is request
                if is_resource:
                    tokens = self.context.current_tokens
                    sent_access_token = tokens.access_token if tokens is not None else None
                    # The SDK holds context.lock for its entire generator,
                    # including while httpx waits on the actual MCP request.
                    # Release it only for that request. Discovery, refresh,
                    # registration and token exchange stay serialized.
                    self.context.lock.release()
                    resource_lock_released = True
                incoming = yield outgoing
                if resource_lock_released:
                    await self.context.lock.acquire()
                    resource_lock_released = False
                status = getattr(incoming, "status_code", None)
                if is_resource:
                    resource_round_trips += 1
                    if resource_round_trips >= 2 and status == 401:
                        # A second resource round trip happens only after
                        # the SDK completed a full authorization (or a 403
                        # step-up). The server rejected the fresh token.
                        retry_rejected_status = status
                # A different request — in this process, or another Freyja
                # process via tokens.json — may have completed refresh or
                # full authorization while this resource request was in
                # flight. Retry with that token instead of starting a
                # duplicate OAuth transition from the stale 401/403.
                if is_resource and status in (401, 403):
                    await self._adopt_disk_tokens(force=True)
                tokens = self.context.current_tokens
                if (
                    is_resource
                    and status in (401, 403)
                    and self.context.is_token_valid()
                    and tokens is not None
                    and tokens.access_token != sent_access_token
                ):
                    self._add_auth_header(request)
                    await inner.aclose()
                    retry_after_concurrent_auth = True
                    break
                # Sniff the response for a dead-client-registration signal
                # before handing it back to the SDK (best-effort).
                await self._maybe_flag_poisoned_client(incoming)
                if is_resource and status == 401 and resource_round_trips == 1:
                    self.performed_authorization = True
                outgoing = await inner.asend(incoming)
        except StopAsyncIteration:
            # Persist any metadata the SDK discovered lazily during the 401
            # branch so a subsequent cold-load skips discovery.
            self._persist_oauth_metadata_if_changed()
            if retry_rejected_status is not None:
                raise OAuthNeedsAuthError(
                    f"MCP server '{self.server_name}' rejected a freshly issued access token "
                    f"({retry_rejected_status}). The authorization completed but the resource "
                    f"still refuses it — check scopes/audience. {login_hint(self.server_name)}",
                    server_name=self.server_name,
                ) from None
            return
        finally:
            with anyio.CancelScope(shield=True):
                if resource_lock_released:
                    # Balance the SDK's surrounding ``async with`` even when
                    # httpx cancels or closes the flow while the resource
                    # request is still in flight.
                    await self.context.lock.acquire()
                # Tear down the inner SDK generator so its ``async with
                # context.lock`` exit actually runs and releases the semaphore
                # (hermes left this to "a separate cleanup PR"; without it a
                # cancelled streamable-HTTP GET wedges every later request).
                # No-op when the generator already finished or was closed.
                try:
                    await inner.aclose()
                except Exception:  # noqa: BLE001 — teardown must never mask the cause
                    pass
                # A flow cancelled mid-refresh must not wedge every other
                # Freyja process's refresh behind a held flock.
                self._release_refresh_lock(flow_id)

        if retry_after_concurrent_auth:
            yield request
            self._persist_oauth_metadata_if_changed()
            return


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_httpx_auth(
    spec: McpServerSpec,
    interactive: bool = False,
    *,
    flow: "LoginFlow | None" = None,
    storage_root: str | Any = None,
    timeout: float | None = None,
    environ: dict[str, str] | None = None,
) -> httpx2.Auth | None:
    """Build the ``httpx2.Auth`` OAuth handler for an ``auth: oauth`` server.

    Returns None for servers whose ``auth`` is not ``"oauth"`` (or that
    have no URL) so callers can unconditionally pass the result through.
    The signature matches manager.py's ``auth_factory(spec, interactive)``
    contract, so the function can be passed straight in as the factory.

    Args:
        spec: The catalog entry. ``spec.oauth`` is parsed by settings.py.
        interactive: Whether a human can complete a browser flow right now
            (``/mcp login`` -> True; background connect -> False). The
            ``suppress_interactive_oauth()`` ContextVar still wins.
        flow: How the authorization URL reaches the user (BrowserFlow default,
            HandoffFlow for desktop/Slack surfaces).
        storage_root: Token directory root (tests); default ``~/.freyja/mcp-tokens``.
        timeout: Browser round-trip budget; default ``oauth.timeout_s`` (300s).
        environ: Environment for ``${VAR}`` expansion (tests); default os.environ.

    Raises:
        OAuthNonInteractiveError: non-interactive and no cached tokens exist
            — the flow can only end in a browser prompt nobody can answer,
            so fail fast with the ``/mcp login`` hint.
        McpConfigError: malformed ``oauth`` block (inline secret, bad port).
    """
    if spec.auth != OAUTH_AUTH_VALUE or not spec.url:
        return None

    settings = parse_oauth_settings(spec, environ)
    apply_provider_defaults(settings, server_name=spec.name, server_url=spec.url)
    storage = FreyjaTokenStorage(spec.name, root=storage_root)

    if not is_interactive(interactive) and not storage.has_cached_tokens():
        raise OAuthNonInteractiveError(
            f"MCP OAuth for '{spec.name}': non-interactive context and no cached tokens "
            "found. The OAuth flow requires browser authorization. "
            f"{login_hint(spec.name)}",
            server_name=spec.name,
        )

    from bridge.mcp.oauth.flow import BrowserFlow, make_flow_handlers

    active_flow = flow if flow is not None else BrowserFlow()
    external_redirect = getattr(active_flow, "redirect_uri", None)

    resolve_callback_port(settings, storage, external_redirect_uri=external_redirect)
    client_metadata = build_client_metadata(settings)
    maybe_preregister_client(storage, settings, client_metadata)

    redirect_handler, callback_handler = make_flow_handlers(
        active_flow,
        server_name=spec.name,
        port=settings.resolved_port or 0,
        redirect_uri=(
            str(client_metadata.redirect_uris[0]) if client_metadata.redirect_uris else None
        ),
        interactive=interactive,
        timeout=float(timeout if timeout is not None else settings.timeout_s),
        cimd_url=settings.cimd_url,
        app_name=settings.effective_client_name,
    )

    provider = FreyjaOAuthClientProvider(
        server_url=spec.url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        server_name=spec.name,
        preregistered=settings.preregistered,
        token_user_agent=settings.effective_user_agent,
        **cimd_provider_kwargs(settings),
    )
    logger.debug(
        "mcp oauth '%s': provider built (port=%s, cimd=%s, preregistered=%s, interactive=%s)",
        spec.name, settings.resolved_port, bool(settings.cimd_url), settings.preregistered,
        is_interactive(interactive),
    )
    return provider


__all__ = [
    "FIGMA_DCR_CLIENT_NAME",
    "FIGMA_DEFAULT_SCOPE",
    "OAUTH_AUTH_VALUE",
    "FreyjaOAuthClientProvider",
    "apply_provider_defaults",
    "build_client_metadata",
    "build_httpx_auth",
    "humanize_oauth_registration_error",
    "is_figma_remote_mcp",
    "maybe_preregister_client",
    "resolve_callback_port",
    "resolve_redirect_uri",
]
