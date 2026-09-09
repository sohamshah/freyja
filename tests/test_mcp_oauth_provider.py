"""FreyjaOAuthClientProvider wiring: provider defaults (Figma), humanized DCR
errors, redirect/port resolution precedence, client metadata, pre-registration
+ client-change invalidation, and build_httpx_auth's fast paths. No network."""

from __future__ import annotations

import json

import httpx2
import pytest

from bridge.mcp.config import McpConfigError, McpServerSpec
from bridge.mcp.oauth import callback as cb
from bridge.mcp.oauth import cimd
from bridge.mcp.oauth import provider as prov
from bridge.mcp.oauth.callback import CIMD_PORTS
from bridge.mcp.oauth.gates import OAuthNonInteractiveError, suppress_interactive_oauth
from bridge.mcp.oauth.provider import (
    FIGMA_DCR_CLIENT_NAME,
    FIGMA_DEFAULT_SCOPE,
    FreyjaOAuthClientProvider,
    apply_provider_defaults,
    build_client_metadata,
    build_httpx_auth,
    humanize_oauth_registration_error,
    is_figma_remote_mcp,
    maybe_preregister_client,
    resolve_callback_port,
    resolve_redirect_uri,
)
from bridge.mcp.oauth.settings import OAuthSettings
from bridge.mcp.oauth.storage import FreyjaTokenStorage, write_json


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "fake-home"))
    cb.reset_port_state_for_tests()
    yield
    cb.reset_port_state_for_tests()


def _spec(oauth=None, **kw) -> McpServerSpec:
    base = dict(name="acme", transport="http", url="https://mcp.example/mcp", auth="oauth")
    base.update(kw)
    return McpServerSpec(oauth=oauth, **base)


# ---------------------------------------------------------------------------
# Figma defaults + humanized errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,url,expected", [
    ("figma", "https://mcp.figma.com/mcp", True),
    ("design", "https://mcp.figma.com/mcp", True),
    ("x", "https://api.figma.com/mcp", True),
    ("figma-local", "http://127.0.0.1:3845/mcp", False),   # local Figma desktop server
    ("figma", "", True),                                    # name only, no url
    ("acme", "https://mcp.example/mcp", False),
    ("x", "https://notfigma.com/mcp", False),
])
def test_is_figma_remote_mcp(name, url, expected):
    assert is_figma_remote_mcp(name, url) is expected


def test_apply_provider_defaults_figma_fills_only_unset():
    s = apply_provider_defaults(OAuthSettings(), server_name="figma", server_url="https://mcp.figma.com/mcp")
    assert s.client_name == FIGMA_DCR_CLIENT_NAME == "Claude Code"
    assert s.scope == FIGMA_DEFAULT_SCOPE
    assert s.token_endpoint_auth_method == "client_secret_post"

    s = apply_provider_defaults(
        OAuthSettings(client_name="Codex", scope="files:read", token_endpoint_auth_method="none"),
        server_name="figma", server_url="https://mcp.figma.com/mcp",
    )
    assert s.client_name == "Codex"
    assert s.scope == "files:read"
    assert s.token_endpoint_auth_method == "none"


def test_apply_provider_defaults_noop_for_other_servers():
    s = apply_provider_defaults(OAuthSettings(), server_name="acme", server_url="https://mcp.example/mcp")
    assert s == OAuthSettings()
    assert s.effective_client_name == "Freyja"


def test_humanize_registration_error():
    assert humanize_oauth_registration_error("acme", RuntimeError("timeout")) is None
    assert humanize_oauth_registration_error("acme", "HTTP 403: Forbidden (resource)") is not None
    generic = humanize_oauth_registration_error("acme", "Registration failed: 403 Forbidden")
    assert "pre-approved OAuth clients" in generic
    assert "${VAR}" in generic
    figma = humanize_oauth_registration_error(
        "figma", "Registration failed: 403 Forbidden", server_url="https://mcp.figma.com/mcp"
    )
    assert "Claude Code" in figma and "/mcp login figma" in figma
    # a 403 from the resource itself (not registration) passes through
    assert humanize_oauth_registration_error("acme", "Tool call returned 403") is None


# ---------------------------------------------------------------------------
# Redirect / port resolution
# ---------------------------------------------------------------------------


def test_resolve_redirect_uri_variants():
    assert resolve_redirect_uri(OAuthSettings(), 4242) == "http://127.0.0.1:4242/callback"
    assert resolve_redirect_uri(OAuthSettings(redirect_host="localhost"), 1) == "http://localhost:1/callback"
    assert resolve_redirect_uri(OAuthSettings(redirect_uri="https://p/cb"), 1) == "https://p/cb"


def test_resolve_callback_port_explicit_config_wins(tmp_path):
    s = OAuthSettings(redirect_port=45678)
    assert resolve_callback_port(s, FreyjaTokenStorage("a", root=tmp_path)) == 45678
    assert s.resolved_port == 45678
    assert not cb.is_port_reserved(45678)  # fixed ports bind via reuse_address later


def test_resolve_callback_port_uses_cached_registration(tmp_path):
    storage = FreyjaTokenStorage("a", root=tmp_path)
    write_json(storage.client_info_path(), {
        "client_id": "c", "redirect_uris": ["http://127.0.0.1:41234/callback"],
    })
    s = OAuthSettings()
    assert resolve_callback_port(s, storage) == 41234


def test_resolve_callback_port_reserves_fresh_ephemeral(tmp_path):
    s = OAuthSettings()
    port = resolve_callback_port(s, FreyjaTokenStorage("a", root=tmp_path))
    assert port > 1024
    assert cb.is_port_reserved(port)


def test_resolve_callback_port_external_redirect(tmp_path):
    s = OAuthSettings()
    assert resolve_callback_port(s, None, external_redirect_uri="https://bridge/cb") == 0
    assert s.redirect_uri == "https://bridge/cb"
    s2 = OAuthSettings()
    storage = FreyjaTokenStorage("a", root=tmp_path)
    write_json(storage.client_info_path(), {"client_id": "c", "redirect_uris": ["https://old/cb"]})
    assert resolve_callback_port(s2, storage) == 0
    assert s2.redirect_uri == "https://old/cb"


def test_resolve_callback_port_cimd_pins_and_records_url(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: True)
    url = "https://me.example/oauth/cimd.json"
    s = OAuthSettings(client_metadata_url=url)
    port = resolve_callback_port(s, FreyjaTokenStorage("a", root=tmp_path))
    assert port in CIMD_PORTS
    assert s.cimd_url == url
    assert cimd.cimd_provider_kwargs(s) == {"client_metadata_url": url}


def test_cached_cimd_port_is_claimed_for_process(tmp_path):
    storage = FreyjaTokenStorage("a", root=tmp_path)
    write_json(storage.client_info_path(), {
        "client_id": "https://me/cimd.json",
        "redirect_uris": [f"http://127.0.0.1:{CIMD_PORTS[3]}/callback"],
    })
    assert resolve_callback_port(OAuthSettings(), storage) == CIMD_PORTS[3]
    assert cb.assigned_cimd_ports() == (CIMD_PORTS[3],)


# ---------------------------------------------------------------------------
# Client metadata + pre-registration
# ---------------------------------------------------------------------------


def test_build_client_metadata_requires_resolved_port():
    with pytest.raises(ValueError):
        build_client_metadata(OAuthSettings())


def test_build_client_metadata_public_vs_confidential():
    s = OAuthSettings()
    s.resolved_port = 4242
    md = build_client_metadata(s)
    assert md.client_name == "Freyja"
    assert [str(u) for u in md.redirect_uris] == ["http://127.0.0.1:4242/callback"]
    assert md.token_endpoint_auth_method == "none"
    assert md.application_type == "native"
    assert md.grant_types == ["authorization_code", "refresh_token"]
    assert md.scope is None

    s = OAuthSettings(client_secret="s", scope="a b", client_name="X")
    s.resolved_port = 1
    md = build_client_metadata(s)
    assert md.token_endpoint_auth_method == "client_secret_post"
    assert md.scope == "a b"
    assert md.client_name == "X"

    s = OAuthSettings(token_endpoint_auth_method="client_secret_basic")
    s.resolved_port = 1
    assert build_client_metadata(s).token_endpoint_auth_method == "client_secret_basic"


def test_maybe_preregister_client_writes_client_json_and_invalidates_on_change(tmp_path):
    storage = FreyjaTokenStorage("acme", root=tmp_path)
    s = OAuthSettings(client_id="pre-1", client_secret="sec", scope="x")
    s.resolved_port = 4242
    md = build_client_metadata(s)
    maybe_preregister_client(storage, s, md)
    raw = json.loads(storage.client_info_path().read_text())
    assert raw["client_id"] == "pre-1"
    assert raw["client_secret"] == "sec"
    assert raw["token_endpoint_auth_method"] == "client_secret_post"
    assert raw["redirect_uris"] == ["http://127.0.0.1:4242/callback"]
    assert raw["scope"] == "x"

    # Tokens minted under pre-1 must be dropped when the config switches to pre-2.
    write_json(storage.tokens_path(), {"access_token": "AT", "expires_at": 9e12})
    s2 = OAuthSettings(client_id="pre-2")
    s2.resolved_port = 4242
    maybe_preregister_client(storage, s2, build_client_metadata(s2))
    assert not storage.tokens_path().exists()
    raw = json.loads(storage.client_info_path().read_text())
    assert raw["client_id"] == "pre-2"
    assert raw["token_endpoint_auth_method"] == "none"  # public pre-registered client stays "none"
    assert "client_secret" not in raw

    # Same identity again: tokens preserved.
    write_json(storage.tokens_path(), {"access_token": "AT2", "expires_at": 9e12})
    maybe_preregister_client(storage, s2, build_client_metadata(s2))
    assert storage.tokens_path().exists()


def test_maybe_preregister_noop_without_client_id(tmp_path):
    storage = FreyjaTokenStorage("acme", root=tmp_path)
    s = OAuthSettings()
    s.resolved_port = 1
    maybe_preregister_client(storage, s, build_client_metadata(s))
    assert not storage.client_info_path().exists()


# ---------------------------------------------------------------------------
# build_httpx_auth fast paths
# ---------------------------------------------------------------------------


def test_build_httpx_auth_none_for_non_oauth(tmp_path):
    assert build_httpx_auth(_spec(auth=None), interactive=True, storage_root=tmp_path) is None
    assert build_httpx_auth(_spec(auth="bearer"), interactive=True, storage_root=tmp_path) is None
    assert build_httpx_auth(_spec(url=None), interactive=True, storage_root=tmp_path) is None


def test_build_httpx_auth_non_interactive_without_tokens_raises(tmp_path):
    with pytest.raises(OAuthNonInteractiveError) as ei:
        build_httpx_auth(_spec(), interactive=False, storage_root=tmp_path)
    assert "/mcp login acme" in str(ei.value)
    assert ei.value.server_name == "acme"
    # Suppression wins even with interactive=True
    with suppress_interactive_oauth():
        with pytest.raises(OAuthNonInteractiveError):
            build_httpx_auth(_spec(), interactive=True, storage_root=tmp_path)
    # Nothing was written to disk on the failure path
    assert not (tmp_path / "acme").exists()


def test_build_httpx_auth_non_interactive_with_tokens_builds_provider(tmp_path):
    storage = FreyjaTokenStorage("acme", root=tmp_path)
    write_json(storage.tokens_path(), {"access_token": "AT", "expires_at": 9e12})
    auth = build_httpx_auth(_spec(), interactive=False, storage_root=tmp_path)
    assert isinstance(auth, FreyjaOAuthClientProvider)
    assert isinstance(auth, httpx2.Auth)
    assert auth.server_name == "acme"
    assert auth.preregistered is False
    assert auth.token_user_agent == "Freyja"
    assert auth.context.client_metadata.client_name == "Freyja"


def test_build_httpx_auth_matches_manager_auth_factory_contract(tmp_path):
    """manager.py calls ``auth_factory(spec, interactive)`` positionally and
    type-checks the result against the SDK's own httpx module."""
    from bridge.mcp.connection import sdk_httpx

    storage = FreyjaTokenStorage("acme", root=tmp_path)
    write_json(storage.tokens_path(), {"access_token": "AT", "expires_at": 9e12})
    factory = lambda spec, interactive: build_httpx_auth(spec, interactive, storage_root=tmp_path)  # noqa: E731
    auth = factory(_spec(), False)
    assert isinstance(auth, sdk_httpx().Auth)
    with pytest.raises(OAuthNonInteractiveError) as ei:
        factory(_spec(name="other"), False)
    # connection.py routes exceptions whose class name mentions OAuth/NeedsAuth to needs-auth
    assert "OAuth" in type(ei.value).__name__
    assert any("NeedsAuth" in k.__name__ for k in type(ei.value).__mro__)


def test_build_httpx_auth_rejects_inline_secret(tmp_path):
    with pytest.raises(McpConfigError):
        build_httpx_auth(
            _spec({"client_secret": "sk-inline"}), interactive=True, storage_root=tmp_path
        )


def test_build_httpx_auth_preregistered_marks_provider(tmp_path):
    auth = build_httpx_auth(
        _spec({"client_id": "pre", "client_secret": "${ACME_SECRET}", "user_agent": "UA/1"}),
        interactive=True, storage_root=tmp_path, environ={"ACME_SECRET": "s"},
    )
    assert auth.preregistered is True
    assert auth.token_user_agent == "UA/1"
    raw = json.loads(FreyjaTokenStorage("acme", root=tmp_path).client_info_path().read_text())
    assert raw["client_id"] == "pre"


def test_build_httpx_auth_figma_defaults_flow_through(tmp_path):
    auth = build_httpx_auth(
        _spec(name="figma", url="https://mcp.figma.com/mcp"), interactive=True,
        storage_root=tmp_path,
    )
    md = auth.context.client_metadata
    assert md.client_name == "Claude Code"
    assert md.scope == "mcp:connect"
    assert md.token_endpoint_auth_method == "client_secret_post"


def test_build_httpx_auth_uses_semaphore_lock(tmp_path):
    import anyio

    auth = build_httpx_auth(_spec(), interactive=True, storage_root=tmp_path)
    assert isinstance(auth.context.lock, anyio.Semaphore)


def _capture_inner_generators(monkeypatch) -> list:
    """Record every SDK-level async_auth_flow generator so tests can check it
    was closed synchronously (``ag_frame is None``) rather than by GC."""
    from mcp.client.auth.oauth2 import OAuthClientProvider

    created: list = []
    original = OAuthClientProvider.async_auth_flow

    def wrapper(self, request):
        gen = original(self, request)
        created.append(gen)
        return gen

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", wrapper)
    return created


async def test_auth_flow_closed_mid_resource_request_releases_lock(tmp_path, monkeypatch):
    """httpx cancelling the flow while the resource request is in flight
    (streamable-HTTP GET teardown) must leave the semaphore free."""
    import asyncio

    storage = FreyjaTokenStorage("acme", root=tmp_path)
    write_json(storage.tokens_path(), {"access_token": "AT", "expires_at": 9e12})
    auth = build_httpx_auth(_spec(), interactive=False, storage_root=tmp_path)
    request = httpx2.Request("POST", "https://mcp.example/mcp")
    inner_gens = _capture_inner_generators(monkeypatch)

    gen = auth.async_auth_flow(request)
    outgoing = await gen.__anext__()
    assert outgoing is request
    assert outgoing.headers["Authorization"] == "Bearer AT"
    # Lock is released around the resource request...
    assert auth.context.lock.value == 1
    await gen.aclose()
    # ...the SDK generator is torn down deterministically (not left to GC)...
    assert len(inner_gens) == 1 and inner_gens[0].ag_frame is None
    # ...and the semaphore is balanced: a fresh acquire must not block.
    assert auth.context.lock.value == 1
    await asyncio.wait_for(auth.context.lock.acquire(), timeout=1)
    auth.context.lock.release()

    # And a second full flow on the same provider still works.
    gen2 = auth.async_auth_flow(request)
    assert (await gen2.__anext__()) is request
    await gen2.aclose()
    await asyncio.wait_for(auth.context.lock.acquire(), timeout=1)
    auth.context.lock.release()


async def test_auth_flow_closed_during_discovery_releases_lock(tmp_path, monkeypatch):
    """Closing while the SDK holds the lock for a *non-resource* request
    (discovery after a 401) must also release it exactly once."""
    import asyncio

    storage = FreyjaTokenStorage("acme", root=tmp_path)
    write_json(storage.tokens_path(), {"access_token": "AT", "expires_at": 9e12})
    auth = build_httpx_auth(_spec(), interactive=False, storage_root=tmp_path)
    request = httpx2.Request("POST", "https://mcp.example/mcp")
    inner_gens = _capture_inner_generators(monkeypatch)
    gen = auth.async_auth_flow(request)
    await gen.__anext__()
    # Feed a 401 so the SDK proceeds to discovery (lock held by the SDK).
    resp = httpx2.Response(401, request=request, headers={"WWW-Authenticate": "Bearer"})
    discovery = await gen.asend(resp)
    assert discovery is not request
    assert auth.context.lock.value == 0
    await gen.aclose()
    assert inner_gens[0].ag_frame is None
    assert auth.context.lock.value == 1
    await asyncio.wait_for(auth.context.lock.acquire(), timeout=1)
    auth.context.lock.release()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def test_same_endpoint_and_safe_error_fields():
    assert prov._same_endpoint("https://AS.example/token?x=1", "https://as.example/token/")
    assert not prov._same_endpoint("https://as.example/token", "https://as.example/register")
    assert not prov._same_endpoint("http://as.example/token", "https://as.example/token")
    body = b'{"error":"invalid_client","error_description":"gone","access_token":"AT"}'
    assert prov._safe_error_fields(body) == "invalid_client: gone"
    assert prov._safe_error_fields(b"<html>nope</html>") == ""
    assert prov._safe_error_fields(None) == ""
    assert prov._safe_error_fields(b"[1]") == ""
