"""Offline fake OAuth 2.1 authorization server + protected MCP resource.

Runs an ``http.server.ThreadingHTTPServer`` on 127.0.0.1:<random> in a daemon
thread. Implements just enough of the MCP authorization stack for the
bridge/mcp/oauth tests to exercise the real mcp 2.1.1 ``OAuthClientProvider``
end to end without network access:

- RFC 9728 protected-resource metadata
  (``/.well-known/oauth-protected-resource`` and the path-suffixed form)
- RFC 8414 authorization-server metadata
  (``/.well-known/oauth-authorization-server``)
- RFC 7591 dynamic client registration (``POST /register``)
- ``GET /authorize`` — auto-approves: validates client + redirect_uri +
  PKCE challenge, mints a code, 302s to ``redirect_uri?code&state[&iss]``
- ``POST /token`` — ``authorization_code`` (PKCE S256 verified) and
  ``refresh_token`` grants; configurable ``expires_in``, refresh-token
  rotation, ``client_secret_post`` enforcement, per-client
  ``invalid_client`` rejection (poison-recovery tests), CIMD acceptance /
  rejection
- ``POST|GET /mcp`` — the protected resource: 401 +
  ``WWW-Authenticate: Bearer resource_metadata="..."`` until a valid,
  unexpired bearer is presented; then a canned JSON-RPC result.

Every endpoint increments ``hits[<name>]`` so tests can assert e.g. that
AS-metadata discovery is skipped on a warm start. Nothing here logs request
lines (they carry ``code=``), so the "no secrets in logs" test can include
the fixture's logger.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

PRM_PATH = "/.well-known/oauth-protected-resource"
ASM_PATH = "/.well-known/oauth-authorization-server"
RESOURCE_PATH = "/mcp"


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@dataclass
class _Client:
    client_id: str
    client_secret: str | None
    redirect_uris: list[str]
    token_endpoint_auth_method: str | None
    client_name: str | None
    scope: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    is_cimd: bool = False


@dataclass
class _Code:
    client_id: str
    redirect_uri: str
    code_challenge: str
    scope: str | None
    issued_at: float


@dataclass
class _AccessToken:
    client_id: str
    scope: str | None
    expires_at: float


class FakeAuthorizationServer:
    """See module docstring. Use as a context manager or ``start()``/``stop()``."""

    def __init__(
        self,
        *,
        expires_in: int = 3600,
        rotate_refresh_tokens: bool = True,
        issue_client_secret: bool = False,
        omit_auth_method_in_registration: bool = False,
        require_client_secret_post: bool = False,
        cimd_supported: bool = False,
        reject_cimd: bool = False,
        cimd_rejection_mode: str = "token",
        include_iss: bool = True,
        advertise_iss_supported: bool = True,
        scopes_supported: list[str] | None = None,
        registration_status: int | None = None,
        registration_allowlist: list[str] | None = None,
        fixed_access_token: str | None = None,
        resource_override: str | None = None,
    ) -> None:
        self.expires_in = expires_in
        self.fixed_access_token = fixed_access_token
        """When set, every minted access token IS this value (lets an
        external protected resource — e.g. mcp_http_fixture_server.py in
        ``auth`` mode — accept the bearer without sharing state)."""
        self.resource_override = resource_override
        """PRM ``resource`` value when the protected resource lives on
        another server (the SDK checks it against the MCP server URL)."""
        self.rotate_refresh_tokens = rotate_refresh_tokens
        self.issue_client_secret = issue_client_secret or require_client_secret_post
        self.omit_auth_method_in_registration = omit_auth_method_in_registration
        self.require_client_secret_post = require_client_secret_post
        self.cimd_supported = cimd_supported
        self.reject_cimd = reject_cimd
        self.cimd_rejection_mode = cimd_rejection_mode
        self.include_iss = include_iss
        self.advertise_iss_supported = advertise_iss_supported
        self.scopes_supported = (
            scopes_supported if scopes_supported is not None else ["mcp:read", "mcp:write"]
        )
        self.registration_status = registration_status
        self.registration_allowlist = registration_allowlist

        self.hits: Counter[str] = Counter()
        self.clients: dict[str, _Client] = {}
        self.codes: dict[str, _Code] = {}
        self.access_tokens: dict[str, _AccessToken] = {}
        self.refresh_tokens: dict[str, tuple[str, str | None]] = {}  # rt -> (client_id, scope)
        self.invalid_client_ids: set[str] = set()
        self.issued_access_tokens: list[str] = []
        self.issued_refresh_tokens: list[str] = []
        self.issued_codes: list[str] = []
        self.token_requests: list[dict[str, Any]] = []
        self.registration_requests: list[dict[str, Any]] = []
        self.authorize_requests: list[dict[str, Any]] = []
        self.resource_requests: list[dict[str, Any]] = []

        self.resource_always_401 = False
        """When True the resource rejects even valid bearers (audience/scope bug simulation)."""

        self._lock = threading.RLock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "FakeAuthorizationServer":
        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # silence; never log query strings
                return None

            def do_GET(self) -> None:  # noqa: N802
                server_ref._dispatch(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                server_ref._dispatch(self, "POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
            name="fake-oauth-as", daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "FakeAuthorizationServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- URLs ---------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def issuer(self) -> str:
        return self.base_url

    @property
    def resource_url(self) -> str:
        return f"{self.base_url}{RESOURCE_PATH}"

    @property
    def prm_url(self) -> str:
        """Protected-resource metadata URL (the path-suffixed RFC 9728 form)."""
        return f"{self.base_url}{PRM_PATH}{RESOURCE_PATH}"

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.base_url}/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base_url}/token"

    @property
    def registration_endpoint(self) -> str:
        return f"{self.base_url}/register"

    # -- test controls -------------------------------------------------------

    def forget_client(self, client_id: str) -> None:
        """Simulate an IdP redeploy/DB wipe: the client is gone server-side, so
        the token endpoint answers ``invalid_client`` for it."""
        with self._lock:
            self.invalid_client_ids.add(client_id)
            self.clients.pop(client_id, None)

    def preregister(
        self,
        client_id: str,
        *,
        client_secret: str | None = None,
        redirect_uris: list[str],
        scope: str | None = None,
    ) -> None:
        """Add a statically registered client (the ``oauth.client_id`` case)."""
        with self._lock:
            self.invalid_client_ids.discard(client_id)
            self.clients[client_id] = _Client(
                client_id=client_id, client_secret=client_secret, redirect_uris=list(redirect_uris),
                token_endpoint_auth_method="client_secret_post" if client_secret else "none",
                client_name="(preregistered)", scope=scope,
            )

    def revoke_all_access_tokens(self) -> None:
        with self._lock:
            self.access_tokens.clear()

    def reset_hits(self) -> None:
        self.hits.clear()

    def token_for(self, access_token: str) -> _AccessToken | None:
        return self.access_tokens.get(access_token)

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = b""
        length = int(handler.headers.get("Content-Length") or 0)
        if length:
            body = handler.rfile.read(length)
        try:
            if path == PRM_PATH or path == PRM_PATH + RESOURCE_PATH:
                self.hits["prm"] += 1
                self._json(handler, 200, self._prm_document())
            elif path == ASM_PATH:
                self.hits["asm"] += 1
                self._json(handler, 200, self._asm_document())
            elif path == "/.well-known/openid-configuration":
                self.hits["oidc"] += 1
                self._json(handler, 404, {"error": "not_found"})
            elif path == "/register" and method == "POST":
                self.hits["register"] += 1
                self._handle_register(handler, body)
            elif path == "/authorize" and method == "GET":
                self.hits["authorize"] += 1
                self._handle_authorize(handler, query)
            elif path == "/token" and method == "POST":
                self.hits["token"] += 1
                self._handle_token(handler, body)
            elif path == RESOURCE_PATH:
                self.hits["resource"] += 1
                self._handle_resource(handler, method, body)
            else:
                self.hits["other"] += 1
                self._json(handler, 404, {"error": "not_found", "path": path})
        except Exception as exc:  # noqa: BLE001 — surface fixture bugs as 500s
            self._json(handler, 500, {"error": "server_error", "error_description": repr(exc)})

    # -- helpers -------------------------------------------------------------

    def _send(self, handler: BaseHTTPRequestHandler, status: int, body: bytes,
              headers: dict[str, str] | None = None) -> None:
        handler.send_response(status)
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if body:
            handler.wfile.write(body)

    def _json(self, handler: BaseHTTPRequestHandler, status: int, doc: dict[str, Any],
              headers: dict[str, str] | None = None) -> None:
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        self._send(handler, status, json.dumps(doc).encode("utf-8"), hdrs)

    def _prm_document(self) -> dict[str, Any]:
        return {
            "resource": self.resource_override or self.resource_url,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(self.scopes_supported),
            "bearer_methods_supported": ["header"],
        }

    def _asm_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            "scopes_supported": list(self.scopes_supported),
            "client_id_metadata_document_supported": bool(self.cimd_supported),
        }
        if self.advertise_iss_supported:
            doc["authorization_response_iss_parameter_supported"] = True
        return doc

    # -- registration --------------------------------------------------------

    def _handle_register(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        try:
            req = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            self._json(handler, 400, {"error": "invalid_client_metadata"})
            return
        with self._lock:
            self.registration_requests.append(req)
        if self.registration_status is not None:
            self._json(handler, self.registration_status,
                       {"error": "forbidden", "error_description": "client registration forbidden"})
            return
        allowlist = self.registration_allowlist
        if allowlist is not None and req.get("client_name") not in allowlist:
            self._json(handler, 403, {"error": "forbidden",
                                      "error_description": "client_name not allowlisted"})
            return
        redirect_uris = [str(u) for u in (req.get("redirect_uris") or [])]
        if not redirect_uris:
            self._json(handler, 400, {"error": "invalid_redirect_uri"})
            return
        client_id = "dcr-" + secrets.token_hex(6)
        client_secret = secrets.token_urlsafe(24) if self.issue_client_secret else None
        requested_method = req.get("token_endpoint_auth_method")
        if client_secret and requested_method in (None, "none"):
            effective_method: str | None = "client_secret_post"
        else:
            effective_method = requested_method or "none"
        client = _Client(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=redirect_uris,
            token_endpoint_auth_method=effective_method,
            client_name=req.get("client_name"),
            scope=req.get("scope"),
            metadata=req,
        )
        with self._lock:
            self.clients[client_id] = client
        resp: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": redirect_uris,
            "grant_types": req.get("grant_types") or ["authorization_code", "refresh_token"],
            "response_types": req.get("response_types") or ["code"],
        }
        if client.client_name:
            resp["client_name"] = client.client_name
        if client.scope:
            resp["scope"] = client.scope
        if client_secret:
            resp["client_secret"] = client_secret
            resp["client_secret_expires_at"] = 0
        if not self.omit_auth_method_in_registration:
            resp["token_endpoint_auth_method"] = effective_method
        self._json(handler, 201, resp)

    # -- authorize -----------------------------------------------------------

    def _lookup_client(self, client_id: str | None) -> _Client | None:
        if not client_id:
            return None
        with self._lock:
            if client_id in self.invalid_client_ids:
                return None
            client = self.clients.get(client_id)
        if client is not None:
            return client
        if self.cimd_supported and client_id.startswith("https://"):
            # The real AS fetches the document; we accept any https client_id
            # and trust loopback redirect URIs (the shipped document lists them).
            return _Client(
                client_id=client_id, client_secret=None, redirect_uris=[],
                token_endpoint_auth_method="none", client_name="(cimd)", scope=None,
                is_cimd=True,
            )
        return None

    def _handle_authorize(
        self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]
    ) -> None:
        params = {k: v[0] for k, v in query.items()}
        with self._lock:
            self.authorize_requests.append(params)
        client = self._lookup_client(params.get("client_id"))
        if client is None:
            self._json(handler, 400, {"error": "invalid_client",
                                      "error_description": "unknown client_id"})
            return
        if client.is_cimd and self.reject_cimd and self.cimd_rejection_mode == "authorize":
            self._json(handler, 400, {"error": "invalid_client",
                                      "error_description": "client metadata document rejected"})
            return
        redirect_uri = params.get("redirect_uri")
        if not redirect_uri:
            self._json(handler, 400, {"error": "invalid_request",
                                      "error_description": "redirect_uri"})
            return
        if client.is_cimd:
            parsed = urlparse(redirect_uri)
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
                self._json(handler, 400, {"error": "invalid_request",
                                          "error_description": "redirect_uri not in document"})
                return
        elif redirect_uri not in client.redirect_uris:
            self._json(handler, 400, {"error": "invalid_request",
                                      "error_description": "redirect_uri does not match"})
            return
        if params.get("response_type") != "code":
            self._json(handler, 400, {"error": "unsupported_response_type"})
            return
        challenge = params.get("code_challenge")
        if not challenge or params.get("code_challenge_method") != "S256":
            self._json(handler, 400, {"error": "invalid_request",
                                      "error_description": "PKCE S256 required"})
            return
        state = params.get("state")
        scope = params.get("scope") or client.scope or " ".join(self.scopes_supported)
        code = "code-" + secrets.token_urlsafe(16)
        with self._lock:
            self.codes[code] = _Code(
                client_id=client.client_id, redirect_uri=redirect_uri,
                code_challenge=challenge, scope=scope, issued_at=time.time(),
            )
            self.issued_codes.append(code)
        out: dict[str, str] = {"code": code}
        if state is not None:
            out["state"] = state
        if self.include_iss:
            out["iss"] = self.issuer
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}{urlencode(out)}"
        self._send(handler, 302, b"", {"Location": location})

    # -- token -----------------------------------------------------------------

    def _mint_tokens(self, client_id: str, scope: str | None) -> dict[str, Any]:
        access = self.fixed_access_token or ("at-" + secrets.token_urlsafe(24))
        refresh = "rt-" + secrets.token_urlsafe(24)
        with self._lock:
            self.access_tokens[access] = _AccessToken(
                client_id=client_id, scope=scope, expires_at=time.time() + self.expires_in,
            )
            self.refresh_tokens[refresh] = (client_id, scope)
            self.issued_access_tokens.append(access)
            self.issued_refresh_tokens.append(refresh)
        doc: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": refresh,
        }
        if scope:
            doc["scope"] = scope
        return doc

    def _authenticate_client(self, form: dict[str, str]) -> tuple[_Client | None, str | None]:
        client_id = form.get("client_id")
        with self._lock:
            if client_id and client_id in self.invalid_client_ids:
                return None, "invalid_client"
        client = self._lookup_client(client_id)
        if client is None:
            return None, "invalid_client"
        if client.is_cimd and self.reject_cimd and self.cimd_rejection_mode == "token":
            return None, "invalid_client"
        if client.client_secret or self.require_client_secret_post:
            if not client.is_cimd and form.get("client_secret") != client.client_secret:
                return None, "invalid_client"
        return client, None

    def _handle_token(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        with self._lock:
            self.token_requests.append({
                "grant_type": form.get("grant_type"),
                "client_id": form.get("client_id"),
                "has_client_secret": "client_secret" in form,
                "user_agent": handler.headers.get("User-Agent"),
            })
        client, err = self._authenticate_client(form)
        if client is None:
            self._json(handler, 401, {"error": err or "invalid_client",
                                      "error_description": "client authentication failed"})
            return
        grant = form.get("grant_type")
        if grant == "authorization_code":
            code = form.get("code")
            with self._lock:
                entry = self.codes.pop(code or "", None)
            if entry is None or entry.client_id != client.client_id:
                self._json(handler, 400, {"error": "invalid_grant",
                                          "error_description": "unknown code"})
                return
            if form.get("redirect_uri") != entry.redirect_uri:
                self._json(handler, 400, {"error": "invalid_grant",
                                          "error_description": "redirect_uri"})
                return
            verifier = form.get("code_verifier") or ""
            if _s256(verifier) != entry.code_challenge:
                self._json(handler, 400, {"error": "invalid_grant",
                                          "error_description": "PKCE mismatch"})
                return
            self._json(handler, 200, self._mint_tokens(client.client_id, entry.scope))
            return
        if grant == "refresh_token":
            rt = form.get("refresh_token") or ""
            with self._lock:
                entry_rt = self.refresh_tokens.get(rt)
                if entry_rt is not None and self.rotate_refresh_tokens:
                    del self.refresh_tokens[rt]
            if entry_rt is None or entry_rt[0] != client.client_id:
                self._json(handler, 400, {"error": "invalid_grant",
                                          "error_description": "unknown refresh_token"})
                return
            doc = self._mint_tokens(client.client_id, entry_rt[1])
            if not self.rotate_refresh_tokens:
                with self._lock:
                    self.refresh_tokens.pop(doc["refresh_token"], None)
                    self.issued_refresh_tokens.pop()
                doc.pop("refresh_token")
            self._json(handler, 200, doc)
            return
        self._json(handler, 400, {"error": "unsupported_grant_type"})

    # -- protected resource -------------------------------------------------------

    def _handle_resource(self, handler: BaseHTTPRequestHandler, method: str, body: bytes) -> None:
        auth = handler.headers.get("Authorization") or ""
        token = auth[7:] if auth.lower().startswith("bearer ") else None
        with self._lock:
            entry = self.access_tokens.get(token or "")
            self.resource_requests.append({"has_bearer": token is not None, "method": method})
        if entry is None or entry.expires_at <= time.time() or self.resource_always_401:
            self.hits["resource_401"] += 1
            self._json(
                handler, 401,
                {"error": "unauthorized"},
                {"WWW-Authenticate": (
                    f'Bearer resource_metadata="{self.base_url}{PRM_PATH}{RESOURCE_PATH}"'
                )},
            )
            return
        self.hits["resource_ok"] += 1
        self._json(handler, 200, {
            "jsonrpc": "2.0", "id": 1,
            "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "serverInfo": {"name": "fake-oauth-resource", "version": "1"}},
        })


# ---------------------------------------------------------------------------
# Browser simulation (urllib — deliberately NOT httpx, whose INFO logging
# would print the callback URL including ``code=`` and break the
# "no secrets in logs" assertion).
# ---------------------------------------------------------------------------


@dataclass
class BrowserVisit:
    status: int
    final_url: str
    location: str | None
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def visit_authorization_url(
    url: str, *, follow: bool = True, timeout: float = 10.0
) -> BrowserVisit:
    """GET *url* like a browser would. With ``follow=True`` the 302 to the
    loopback callback is followed (so the listener receives the code); with
    ``follow=False`` the redirect ``Location`` is returned for the caller to
    deliver by hand (external-redirect flows)."""
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as resp:
            return BrowserVisit(
                status=resp.status, final_url=resp.geturl(),
                location=resp.headers.get("Location"), body=resp.read(),
            )
    except urllib.error.HTTPError as exc:
        return BrowserVisit(
            status=exc.code, final_url=exc.geturl() or url,
            location=exc.headers.get("Location") if exc.headers else None,
            body=exc.read() if hasattr(exc, "read") else b"",
        )


def redirect_params(location: str) -> dict[str, str]:
    """``code``/``state``/``iss``/``error`` from a redirect Location URL."""
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


__all__ = [
    "ASM_PATH",
    "BrowserVisit",
    "FakeAuthorizationServer",
    "PRM_PATH",
    "RESOURCE_PATH",
    "redirect_params",
    "visit_authorization_url",
]
