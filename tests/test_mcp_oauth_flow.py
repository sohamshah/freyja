"""End-to-end OAuth flows against the offline fake authorization server
(tests/fixtures/fake_oauth_as.py) driving the real mcp 2.1.1 provider.

Covers: full login (DCR -> PKCE authorize -> token -> 200), persistence +
0600, expires_at-driven refresh on cold start, metadata reuse (no discovery
on warm start), refresh-token rotation/carry-forward, invalid_client poison
recovery, pre-registered clients (not poisoned; client-change invalidation),
non-interactive gates (typed errors + login hint), Supabase-style
client_secret_post coercion, token UA stamping, BrowserFlow/HandoffFlow/
external-redirect/paste/skip/timeout, CIMD accept + reject fallback,
state-mismatch attack, concurrent servers, 401-after-fresh-token typed
error, run_login restore-on-failure, and a caplog "no secrets logged"
assertion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import sys
import time
from pathlib import Path

import httpx2
import pytest
from mcp.client.auth.exceptions import OAuthTokenError

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from fake_oauth_as import (  # noqa: E402
    FakeAuthorizationServer,
    redirect_params,
    visit_authorization_url,
)

from bridge.mcp.config import McpServerSpec  # noqa: E402
from bridge.mcp.oauth import callback as cb  # noqa: E402
from bridge.mcp.oauth.callback import CIMD_PORTS  # noqa: E402
from bridge.mcp.oauth.flow import (  # noqa: E402
    BrowserFlow,
    HandoffFlow,
    LoginFlow,
    LoginResult,
    run_login,
)
from bridge.mcp.oauth.gates import (  # noqa: E402
    OAuthNeedsAuthError,
    OAuthNonInteractiveError,
    suppress_interactive_oauth,
)
from bridge.mcp.oauth.provider import FreyjaOAuthClientProvider, build_httpx_auth  # noqa: E402
from bridge.mcp.oauth.storage import FreyjaTokenStorage, read_json, write_json  # noqa: E402

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "fake-home"))
    cb.reset_port_state_for_tests()
    yield
    cb.reset_port_state_for_tests()


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "mcp-tokens"


@pytest.fixture
def fake():
    with FakeAuthorizationServer() as server:
        yield server


def _spec(fake: FakeAuthorizationServer, oauth=None, name="acme", **kw) -> McpServerSpec:
    base = dict(name=name, transport="http", url=fake.resource_url, auth="oauth")
    base.update(kw)
    return McpServerSpec(oauth=oauth or {}, **base)


class AutoBrowser:
    """A HandoffFlow whose on_authorize_url behaves like a real browser: it
    follows the authorize 302 to the loopback callback in a background thread."""

    def __init__(self, *, delay: float = 0.0):
        self.urls: list[str] = []
        self.tasks: list[asyncio.Task] = []
        self.delay = delay
        self.flow = HandoffFlow(self._present)

    async def _present(self, url: str) -> None:
        self.urls.append(url)

        async def go():
            if self.delay:
                await asyncio.sleep(self.delay)
            return await asyncio.to_thread(visit_authorization_url, url)

        self.tasks.append(asyncio.create_task(go()))

    async def drain(self):
        for t in self.tasks:
            try:
                await t
            except Exception:  # noqa: BLE001
                pass


def _expire_tokens_on_disk(storage: FreyjaTokenStorage, seconds_ago: float = 60.0) -> None:
    raw = read_json(storage.tokens_path())
    assert raw is not None
    raw["expires_at"] = time.time() - seconds_ago
    write_json(storage.tokens_path(), raw)


async def _authed_post(auth, url: str, **kw) -> httpx2.Response:
    async with httpx2.AsyncClient(auth=auth, timeout=10) as client:
        return await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, **kw)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# Full login
# ---------------------------------------------------------------------------


async def test_full_login_roundtrip_persists_state_with_0600(fake, root):
    browser = AutoBrowser()
    result = await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)
    await browser.drain()
    assert result.ok, result.error
    assert result.server_name == "acme"
    assert result.client_id and result.client_id.startswith("dcr-")
    assert result.scopes == ["mcp:read", "mcp:write"]
    assert result.expires_at and result.expires_at > time.time() + 3000
    assert result.detail["status_code"] == 200

    assert fake.hits["register"] == 1
    assert fake.hits["authorize"] == 1
    assert fake.hits["token"] == 1
    assert fake.hits["resource_ok"] == 1
    assert len(browser.urls) == 1
    assert "code_challenge=" in browser.urls[0]

    storage = FreyjaTokenStorage("acme", root=root)
    for p in (storage.tokens_path(), storage.client_info_path(), storage.meta_path()):
        assert p.exists(), p
        assert _mode(p) == 0o600
    assert _mode(storage.server_dir) == 0o700
    assert _mode(root) == 0o700
    tokens = json.loads(storage.tokens_path().read_text())
    assert tokens["access_token"] == fake.issued_access_tokens[-1]
    assert "expires_at" in tokens
    meta = storage.load_oauth_metadata()
    assert str(meta.token_endpoint) == fake.token_endpoint
    client = read_json(storage.client_info_path())
    assert client["redirect_uris"][0].startswith("http://127.0.0.1:")
    assert client["client_name"] == "Freyja"
    # Registration declared application_type native (SEP-837)
    assert fake.registration_requests[0]["application_type"] == "native"
    assert fake.registration_requests[0]["token_endpoint_auth_method"] == "none"


async def test_login_reuses_cached_redirect_port_on_reauth(fake, root):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    storage = FreyjaTokenStorage("acme", root=root)
    first_uri = read_json(storage.client_info_path())["redirect_uris"][0]

    # Non-fresh re-auth (tokens revoked at the AS) keeps the registered redirect URI.
    fake.revoke_all_access_tokens()
    fake.refresh_tokens.clear()
    browser2 = AutoBrowser()
    result = await run_login(
        _spec(fake), flow=browser2.flow, storage_root=root, timeout=10, fresh=False
    )
    await browser2.drain()
    assert result.ok, result.error
    assert fake.hits["register"] == 1  # no second registration
    assert f"redirect_uri={first_uri}" in browser2.urls[0].replace("%3A", ":").replace("%2F", "/")


# ---------------------------------------------------------------------------
# Cold start: expires_at + metadata reuse
# ---------------------------------------------------------------------------


async def test_expired_by_expires_at_refreshes_before_first_request(fake, root):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    storage = FreyjaTokenStorage("acme", root=root)
    old_access = json.loads(storage.tokens_path().read_text())["access_token"]
    old_refresh = json.loads(storage.tokens_path().read_text())["refresh_token"]

    # "Restart hours later": expires_in on disk still says 3600 but expires_at is past.
    _expire_tokens_on_disk(storage)
    fake.reset_hits()

    auth = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
    resp = await _authed_post(auth, fake.resource_url)
    assert resp.status_code == 200
    assert fake.hits["token"] == 1
    assert fake.hits["resource_401"] == 0  # stale bearer never hit the resource
    assert fake.hits["authorize"] == 0  # no browser
    assert fake.token_requests[-1]["grant_type"] == "refresh_token"
    new = json.loads(storage.tokens_path().read_text())
    assert new["access_token"] != old_access
    assert new["refresh_token"] != old_refresh  # rotation persisted
    assert new["expires_at"] > time.time() + 3000
    assert new["scope"] == "mcp:read mcp:write"  # carried forward


async def test_warm_start_skips_discovery_when_metadata_cached(fake, root):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    storage = FreyjaTokenStorage("acme", root=root)
    _expire_tokens_on_disk(storage)
    fake.reset_hits()

    auth = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
    assert (await _authed_post(auth, fake.resource_url)).status_code == 200
    assert fake.hits["asm"] == 0
    assert fake.hits["prm"] == 0
    assert fake.hits["token"] == 1

    # Without meta.json the provider prefetches discovery (still no browser)
    # so refresh goes to the real token endpoint rather than {server_url}/token.
    _expire_tokens_on_disk(storage)
    storage.meta_path().unlink()
    fake.reset_hits()
    auth = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
    assert (await _authed_post(auth, fake.resource_url)).status_code == 200
    assert fake.hits["asm"] == 1
    assert fake.hits["prm"] >= 1
    assert fake.hits["authorize"] == 0
    assert storage.meta_path().exists()  # re-persisted


async def test_refresh_without_rotation_carries_refresh_token_forward(root):
    with FakeAuthorizationServer(rotate_refresh_tokens=False) as fake:
        browser = AutoBrowser()
        assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
        await browser.drain()
        storage = FreyjaTokenStorage("acme", root=root)
        rt = json.loads(storage.tokens_path().read_text())["refresh_token"]
        _expire_tokens_on_disk(storage)
        auth = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
        assert (await _authed_post(auth, fake.resource_url)).status_code == 200
        assert json.loads(storage.tokens_path().read_text())["refresh_token"] == rt


# ---------------------------------------------------------------------------
# Non-interactive gates
# ---------------------------------------------------------------------------


async def test_non_interactive_refresh_failure_raises_typed_error_with_hint(fake, root):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    storage = FreyjaTokenStorage("acme", root=root)
    _expire_tokens_on_disk(storage)
    fake.refresh_tokens.clear()  # refresh token revoked server-side -> invalid_grant
    fake.reset_hits()

    auth = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
    with pytest.raises(OAuthNonInteractiveError) as ei:
        await _authed_post(auth, fake.resource_url)
    assert "/mcp login acme" in str(ei.value)
    assert ei.value.server_name == "acme"
    assert fake.hits["authorize"] == 0
    assert fake.hits["register"] == 0  # cached registration reused, no re-register


async def test_suppression_gate_blocks_browser_even_if_flag_true(fake, root):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    storage = FreyjaTokenStorage("acme", root=root)
    _expire_tokens_on_disk(storage)
    fake.refresh_tokens.clear()
    browser2 = AutoBrowser()
    auth = build_httpx_auth(_spec(fake), interactive=True, flow=browser2.flow, storage_root=root)
    with suppress_interactive_oauth():
        with pytest.raises(OAuthNonInteractiveError):
            await _authed_post(auth, fake.resource_url)
    assert browser2.urls == []


# ---------------------------------------------------------------------------
# invalid_client poison recovery
# ---------------------------------------------------------------------------


async def test_invalid_client_poisons_registration_and_reregisters(fake, root, caplog):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    storage = FreyjaTokenStorage("acme", root=root)
    old_client = read_json(storage.client_info_path())["client_id"]

    fake.forget_client(old_client)  # IdP wiped its client DB
    _expire_tokens_on_disk(storage)
    fake.reset_hits()

    browser2 = AutoBrowser()
    auth = build_httpx_auth(_spec(fake), interactive=True, flow=browser2.flow, storage_root=root)
    with caplog.at_level(logging.WARNING):
        resp = await _authed_post(auth, fake.resource_url)
    await browser2.drain()
    assert resp.status_code == 200
    assert isinstance(auth, FreyjaOAuthClientProvider) and auth.last_poisoned
    new_client = read_json(storage.client_info_path())["client_id"]
    assert new_client != old_client
    backup = storage.client_info_path().with_name("client.json.bak")
    assert json.loads(backup.read_text())["client_id"] == old_client
    assert fake.hits["register"] == 1
    assert fake.hits["authorize"] == 1
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "invalid_client" in joined
    for secret in fake.issued_access_tokens + fake.issued_refresh_tokens:
        assert secret not in joined


async def test_preregistered_client_is_never_poisoned(fake, root):
    port = _free_port()
    fake.preregister("pre-1", client_secret="pre-secret",
                     redirect_uris=[f"http://127.0.0.1:{port}/callback"])
    oauth = {"client_id": "pre-1", "client_secret": "${ACME_SECRET}", "redirect_port": port}
    env = {"ACME_SECRET": "pre-secret"}
    browser = AutoBrowser()
    result = await run_login(
        _spec(fake, oauth), flow=browser.flow, storage_root=root, timeout=10, environ=env
    )
    await browser.drain()
    assert result.ok, result.error
    assert fake.hits["register"] == 0
    assert result.client_id == "pre-1"
    assert fake.token_requests[-1]["has_client_secret"] is True
    storage = FreyjaTokenStorage("acme", root=root)

    fake.forget_client("pre-1")
    _expire_tokens_on_disk(storage)
    auth = build_httpx_auth(_spec(fake, oauth), interactive=False, storage_root=root, environ=env)
    with pytest.raises(OAuthNonInteractiveError):
        await _authed_post(auth, fake.resource_url)
    assert storage.client_info_path().exists()  # config-supplied identity untouched
    assert not storage.client_info_path().with_name("client.json.bak").exists()
    assert auth.last_poisoned is False


async def test_client_id_change_in_config_drops_tokens(fake, root):
    port = _free_port()
    fake.preregister("pre-1", redirect_uris=[f"http://127.0.0.1:{port}/callback"])
    fake.preregister("pre-2", redirect_uris=[f"http://127.0.0.1:{port}/callback"])
    browser = AutoBrowser()
    assert (await run_login(
        _spec(fake, {"client_id": "pre-1", "redirect_port": port}), flow=browser.flow,
        storage_root=root, timeout=10,
    )).ok
    await browser.drain()
    storage = FreyjaTokenStorage("acme", root=root)
    assert storage.has_cached_tokens()

    # Operator edits mcp.json to pre-2: build (non-interactive is fine, tokens
    # exist at check time) must invalidate the pre-1 tokens.
    browser2 = AutoBrowser()
    auth = build_httpx_auth(
        _spec(fake, {"client_id": "pre-2", "redirect_port": port}), interactive=True,
        flow=browser2.flow, storage_root=root,
    )
    assert not storage.has_cached_tokens()
    assert read_json(storage.client_info_path())["client_id"] == "pre-2"
    resp = await _authed_post(auth, fake.resource_url)
    await browser2.drain()
    assert resp.status_code == 200
    assert fake.authorize_requests[-1]["client_id"] == "pre-2"


# ---------------------------------------------------------------------------
# Provider quirks: Supabase secret coercion, UA
# ---------------------------------------------------------------------------


async def test_supabase_style_secret_without_method_uses_client_secret_post(root):
    with FakeAuthorizationServer(require_client_secret_post=True,
                                 omit_auth_method_in_registration=True) as fake:
        browser = AutoBrowser()
        result = await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)
        await browser.drain()
        assert result.ok, result.error
        assert all(r["has_client_secret"] for r in fake.token_requests)
        storage = FreyjaTokenStorage("acme", root=root)
        client = read_json(storage.client_info_path())
        assert client["token_endpoint_auth_method"] == "client_secret_post"

        # refresh path also sends the secret
        _expire_tokens_on_disk(storage)
        auth = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
        assert (await _authed_post(auth, fake.resource_url)).status_code == 200
        assert fake.token_requests[-1]["grant_type"] == "refresh_token"
        assert fake.token_requests[-1]["has_client_secret"] is True


async def test_token_requests_carry_user_agent(fake, root):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    assert fake.token_requests[-1]["user_agent"] == "Freyja"
    storage = FreyjaTokenStorage("acme", root=root)
    _expire_tokens_on_disk(storage)
    auth = build_httpx_auth(_spec(fake, {"user_agent": "Freyja/2.0 (+test)"}), interactive=False,
                            storage_root=root)
    assert (await _authed_post(auth, fake.resource_url)).status_code == 200
    assert fake.token_requests[-1]["user_agent"] == "Freyja/2.0 (+test)"


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


async def test_browser_flow_with_injected_opener(fake, root):
    loop = asyncio.get_running_loop()
    opened: list[str] = []
    pending: list[asyncio.Future] = []

    def opener(url: str) -> bool:
        opened.append(url)
        # like a browser: fetch asynchronously (opener runs in a worker thread)
        pending.append(asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(visit_authorization_url, url), loop
        ))
        return True

    flow = BrowserFlow(opener=opener)
    result = await run_login(_spec(fake), flow=flow, storage_root=root, timeout=10)
    for f in pending:
        f.result(timeout=5)
    assert result.ok, result.error
    assert flow.opened is True
    assert flow.last_url == opened[0]
    assert isinstance(flow, LoginFlow)


async def test_browser_flow_fallback_when_browser_cannot_open(fake, root):
    fallback_urls: list[str] = []
    tasks: list[asyncio.Task] = []

    async def fallback(url: str) -> None:
        fallback_urls.append(url)
        tasks.append(asyncio.create_task(asyncio.to_thread(visit_authorization_url, url)))

    flow = BrowserFlow(opener=lambda url: False, fallback=fallback)
    result = await run_login(_spec(fake), flow=flow, storage_root=root, timeout=10)
    for t in tasks:
        await t
    assert result.ok, result.error
    assert flow.opened is False
    assert len(fallback_urls) == 1


async def test_default_opener_is_no_op_in_ssh_session(monkeypatch):
    from bridge.mcp.oauth.flow import can_open_browser, default_open_url

    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    assert can_open_browser() is False
    assert default_open_url("https://example.invalid") is False


async def test_paste_fallback_completes_login_without_loopback_hit(fake, root):
    """Headless: the surface fetches the authorize URL elsewhere and pastes the
    redirect back. The loopback listener never receives a request."""
    pasted: asyncio.Future = asyncio.get_running_loop().create_future()

    async def present(url: str) -> None:
        visit = await asyncio.to_thread(visit_authorization_url, url, follow=False)
        assert visit.status == 302
        pasted.set_result(visit.location)

    async def paste_source() -> str | None:
        return await pasted

    flow = HandoffFlow(present, paste_source=paste_source)
    result = await run_login(_spec(fake), flow=flow, storage_root=root, timeout=10)
    assert result.ok, result.error
    assert flow.status == "authorization_required"


async def test_paste_skip_token_yields_skipped_result(fake, root):
    async def present(url: str) -> None:
        return None

    async def paste_source() -> str | None:
        return "skip"

    flow = HandoffFlow(present, paste_source=paste_source)
    result = await run_login(_spec(fake), flow=flow, storage_root=root, timeout=10)
    assert not result.ok
    assert result.skipped
    assert "user_skipped" in result.error
    assert not FreyjaTokenStorage("acme", root=root).has_cached_tokens()


async def test_external_redirect_handoff_flow_deliver_callback(fake, root):
    """Hermes dashboard pattern: the redirect lands on the bridge's own https
    route; the surface forwards the parameters via deliver_callback()."""
    external = "https://bridge.example/oauth/callback/acme"
    flow_holder: dict[str, HandoffFlow] = {}

    async def present(url: str) -> None:
        visit = await asyncio.to_thread(visit_authorization_url, url, follow=False)
        assert visit.status == 302 and visit.location.startswith(external)
        params = redirect_params(visit.location)
        # forged first (wrong state) -> rejected; then the real one
        assert flow_holder["flow"].deliver_callback(code="forged", state="nope") is False
        assert flow_holder["flow"].deliver_callback(
            code=params["code"], state=params.get("state"), iss=params.get("iss")
        ) is True

    flow = HandoffFlow(present, redirect_uri=external)
    flow_holder["flow"] = flow
    assert flow.deliver_callback(code="x", state="y") is False  # no pending flow yet
    result = await run_login(_spec(fake), flow=flow, storage_root=root, timeout=10)
    assert result.ok, result.error
    storage = FreyjaTokenStorage("acme", root=root)
    assert read_json(storage.client_info_path())["redirect_uris"] == [external]
    assert fake.authorize_requests[-1]["redirect_uri"] == external


async def test_state_mismatch_attack_is_ignored_and_real_callback_wins(fake, root):
    async def present(url: str) -> None:
        params = redirect_params(url)
        port = int(params["redirect_uri"].rsplit(":", 1)[1].split("/")[0])
        forged = f"http://127.0.0.1:{port}/callback?code=FORGED&state=not-the-state"
        visit = await asyncio.to_thread(visit_authorization_url, forged)
        assert visit.status == 400
        await asyncio.to_thread(visit_authorization_url, url)

    flow = HandoffFlow(present)
    result = await run_login(_spec(fake), flow=flow, storage_root=root, timeout=10)
    assert result.ok, result.error
    # The forged code never reached the token endpoint.
    assert all(c in fake.issued_codes for c in [fake.issued_codes[-1]])
    assert fake.hits["token"] == 1


async def test_callback_timeout_restores_previous_state(fake, root):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    storage = FreyjaTokenStorage("acme", root=root)
    before = storage.snapshot()

    async def never(url: str) -> None:
        return None

    result = await run_login(_spec(fake), flow=HandoffFlow(never), storage_root=root, timeout=0.3)
    assert not result.ok
    assert "timed out" in result.error
    assert "/mcp login acme" in result.error
    assert storage.snapshot() == before  # rollback


async def test_two_servers_login_concurrently_with_isolated_callbacks(root):
    with FakeAuthorizationServer() as fake_a, FakeAuthorizationServer() as fake_b:
        ba, bb = AutoBrowser(delay=0.05), AutoBrowser()
        ra, rb = await asyncio.gather(
            run_login(_spec(fake_a, name="alpha"), flow=ba.flow, storage_root=root, timeout=10),
            run_login(_spec(fake_b, name="beta"), flow=bb.flow, storage_root=root, timeout=10),
        )
        await ba.drain()
        await bb.drain()
        assert ra.ok, ra.error
        assert rb.ok, rb.error
        pa = redirect_params(ba.urls[0])["redirect_uri"]
        pb = redirect_params(bb.urls[0])["redirect_uri"]
        assert pa != pb
        assert fake_a.hits["token"] == 1 and fake_b.hits["token"] == 1


# ---------------------------------------------------------------------------
# CIMD
# ---------------------------------------------------------------------------

CIMD_URL = "https://me.example/freyja/oauth/client-metadata.json"


async def test_cimd_login_when_server_supports_it(root):
    with FakeAuthorizationServer(cimd_supported=True) as fake:
        browser = AutoBrowser()
        result = await run_login(
            _spec(fake, {"client_metadata_url": CIMD_URL}), flow=browser.flow,
            storage_root=root, timeout=10,
        )
        await browser.drain()
        assert result.ok, result.error
        assert fake.hits["register"] == 0
        assert result.client_id == CIMD_URL
        assert fake.authorize_requests[-1]["client_id"] == CIMD_URL
        port = int(redirect_params(browser.urls[0])["redirect_uri"].rsplit(":", 1)[1].split("/")[0])
        assert port in CIMD_PORTS


async def test_cimd_rejection_marks_and_falls_back_to_dcr(root, caplog):
    with FakeAuthorizationServer(cimd_supported=True, reject_cimd=True) as fake:
        spec = _spec(fake, {"client_metadata_url": CIMD_URL})
        browser = AutoBrowser()
        auth = build_httpx_auth(spec, interactive=True, flow=browser.flow, storage_root=root)
        assert auth.context.client_metadata_url == CIMD_URL
        with caplog.at_level(logging.WARNING):
            with pytest.raises(OAuthTokenError) as ei:
                await _authed_post(auth, fake.resource_url)
        await browser.drain()
        assert "invalid_client" in str(ei.value)
        storage = FreyjaTokenStorage("acme", root=root)
        assert storage.cimd_rejected()
        assert auth.context.client_metadata_url is None
        assert "Metadata Document" in "\n".join(r.getMessage() for r in caplog.records)

        # Second attempt: DCR path, succeeds.
        browser2 = AutoBrowser()
        auth2 = build_httpx_auth(spec, interactive=True, flow=browser2.flow, storage_root=root)
        assert auth2.context.client_metadata_url is None
        resp = await _authed_post(auth2, fake.resource_url)
        await browser2.drain()
        assert resp.status_code == 200
        assert fake.hits["register"] == 1
        assert read_json(storage.client_info_path())["client_id"].startswith("dcr-")


async def test_cimd_default_off_uses_dcr_even_when_supported(root):
    with FakeAuthorizationServer(cimd_supported=True) as fake:
        browser = AutoBrowser()
        result = await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)
        await browser.drain()
        assert result.ok
        assert fake.hits["register"] == 1
        assert result.client_id.startswith("dcr-")


# ---------------------------------------------------------------------------
# Typed 401-after-fresh-token + run_login error paths
# ---------------------------------------------------------------------------


async def test_401_after_fresh_token_raises_needs_auth(fake, root):
    fake.resource_always_401 = True
    browser = AutoBrowser()
    auth = build_httpx_auth(_spec(fake), interactive=True, flow=browser.flow, storage_root=root)
    with pytest.raises(OAuthNeedsAuthError) as ei:
        await _authed_post(auth, fake.resource_url)
    await browser.drain()
    assert "freshly issued access token" in str(ei.value)
    assert "/mcp login acme" in str(ei.value)
    # The authorization itself did complete and tokens were stored.
    assert FreyjaTokenStorage("acme", root=root).has_cached_tokens()
    # run_login surfaces the same condition as a failed LoginResult
    fake.reset_hits()
    browser2 = AutoBrowser()
    result = await run_login(_spec(fake), flow=browser2.flow, storage_root=root, timeout=10)
    await browser2.drain()
    assert not result.ok and "freshly issued" in result.error


async def test_run_login_rejects_non_oauth_and_unreachable(root):
    result = await run_login(
        McpServerSpec(name="x", transport="http", url="http://127.0.0.1:9/mcp", auth="bearer"),
        flow=HandoffFlow(lambda url: asyncio.sleep(0)), storage_root=root,
    )
    assert not result.ok and "not configured for OAuth" in result.error
    port = _free_port()
    result = await run_login(
        McpServerSpec(name="x", transport="http", url=f"http://127.0.0.1:{port}/mcp", auth="oauth"),
        flow=HandoffFlow(lambda url: asyncio.sleep(0)), storage_root=root, timeout=2,
    )
    assert not result.ok and "could not reach" in result.error
    assert isinstance(result, LoginResult)


async def test_registration_403_is_humanized(root):
    with FakeAuthorizationServer(registration_status=403) as fake:
        result = await run_login(_spec(fake), flow=HandoffFlow(lambda url: asyncio.sleep(0)),
                                 storage_root=root, timeout=5)
        assert not result.ok
        assert "pre-approved OAuth clients" in result.error
    with FakeAuthorizationServer(registration_allowlist=["Claude Code"]) as fake:
        # Default name is refused by the allowlist...
        result = await run_login(_spec(fake), flow=HandoffFlow(lambda url: asyncio.sleep(0)),
                                 storage_root=root, timeout=5)
        assert not result.ok and "oauth.client_name" in result.error
        assert fake.registration_requests[-1]["client_name"] == "Freyja"
        # ...an allowlisted oauth.client_name gets through.
        browser = AutoBrowser()
        result = await run_login(_spec(fake, {"client_name": "Claude Code"}),
                                 flow=browser.flow, storage_root=root, timeout=5)
        await browser.drain()
        assert result.ok, result.error
        assert fake.registration_requests[-1]["client_name"] == "Claude Code"


async def test_run_login_missing_secret_env_is_needs_auth(fake, root):
    result = await run_login(
        _spec(fake, {"client_id": "pre", "client_secret": "${NOPE}"}),
        flow=HandoffFlow(lambda url: asyncio.sleep(0)), storage_root=root, environ={},
    )
    assert not result.ok
    assert "NOPE" in result.error and "/mcp login acme" in result.error


# ---------------------------------------------------------------------------
# No secrets in logs
# ---------------------------------------------------------------------------


async def test_no_secret_values_appear_in_any_log_line(root, caplog):
    caplog.set_level(logging.DEBUG)
    with FakeAuthorizationServer(issue_client_secret=True) as fake:
        browser = AutoBrowser()
        result = await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)
        await browser.drain()
        assert result.ok, result.error
        storage = FreyjaTokenStorage("acme", root=root)
        _expire_tokens_on_disk(storage)
        auth = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
        assert (await _authed_post(auth, fake.resource_url)).status_code == 200
        # and a failure path with an error body
        fake.forget_client(read_json(storage.client_info_path())["client_id"])
        _expire_tokens_on_disk(storage)
        auth = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
        with pytest.raises(OAuthNonInteractiveError):
            await _authed_post(auth, fake.resource_url)

        secrets = set(fake.issued_access_tokens) | set(fake.issued_refresh_tokens)
        secrets |= set(fake.issued_codes)
        secrets |= {c.client_secret for c in fake.clients.values() if c.client_secret}
        backup = json.loads(storage.client_info_path().with_name("client.json.bak").read_text())
        secrets |= {backup.get("client_secret")} - {None}
        assert len(secrets) >= 6
        lines = [r.getMessage() for r in caplog.records]
        assert lines, "expected log output to inspect"
        for line in lines:
            for secret in secrets:
                assert secret not in line, f"secret leaked in log line: {line[:120]}"


# ---------------------------------------------------------------------------
# Cross-process refresh (desktop bridge + gateway + scheduler daemon share
# tokens.json; each holds its own provider). With rotating refresh tokens the
# first process to refresh used to revoke every other process's in-memory
# refresh token -> invalid_grant -> needs-auth despite a valid tokens.json.
# ---------------------------------------------------------------------------


async def _two_warm_processes(fake, root):
    browser = AutoBrowser()
    assert (await run_login(_spec(fake), flow=browser.flow, storage_root=root, timeout=10)).ok
    await browser.drain()
    a = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
    b = build_httpx_auth(_spec(fake), interactive=False, storage_root=root)
    assert (await _authed_post(a, fake.resource_url)).status_code == 200
    assert (await _authed_post(b, fake.resource_url)).status_code == 200
    # Time passes: both processes' in-memory access tokens are now expired.
    for p in (a, b):
        p.context.token_expiry_time = time.time() - 1
    _expire_tokens_on_disk(FreyjaTokenStorage("acme", root=root))
    fake.reset_hits()
    return a, b


async def test_second_process_adopts_rotated_tokens_instead_of_refreshing(fake, root):
    a, b = await _two_warm_processes(fake, root)

    assert (await _authed_post(a, fake.resource_url)).status_code == 200
    assert fake.hits["token"] == 1  # A refreshed and rotated the refresh token

    # B still holds the now-revoked refresh token in memory. It must pick up
    # A's tokens from disk, not spend the dead refresh token.
    assert (await _authed_post(b, fake.resource_url)).status_code == 200
    assert fake.hits["token"] == 1
    assert fake.hits["authorize"] == 0
    assert b.context.current_tokens.access_token == a.context.current_tokens.access_token


async def test_concurrent_refresh_across_processes_spends_refresh_token_once(fake, root):
    a, b = await _two_warm_processes(fake, root)

    ra, rb = await asyncio.gather(
        _authed_post(a, fake.resource_url), _authed_post(b, fake.resource_url)
    )
    assert (ra.status_code, rb.status_code) == (200, 200)
    assert fake.hits["token"] == 1  # the flock serialized them; loser adopted
    assert fake.hits["authorize"] == 0
    storage = FreyjaTokenStorage("acme", root=root)
    fd = await storage.acquire_refresh_lock(timeout=0.2)
    assert fd is not None  # nothing left holding the lock
    storage.release_refresh_lock(fd)


async def test_rejected_refresh_adopts_newer_disk_tokens_instead_of_clearing(fake, root):
    """An unlocked writer (e.g. an older build) rotated tokens.json between
    our disk check and our refresh: recover from disk rather than wipe."""
    a, b = await _two_warm_processes(fake, root)
    assert (await _authed_post(a, fake.resource_url)).status_code == 200
    storage = FreyjaTokenStorage("acme", root=root)

    # Skip B's pre-flow sync so it spends its revoked refresh token.
    async def _no_sync(_owner):
        return None

    b._sync_before_flow = _no_sync  # type: ignore[method-assign]
    fake.reset_hits()

    resp = await _authed_post(b, fake.resource_url)
    assert resp.status_code == 200
    assert fake.hits["token"] == 1  # the one rejected refresh
    assert fake.hits["authorize"] == 0
    assert read_json(storage.tokens_path())["access_token"] == a.context.current_tokens.access_token
