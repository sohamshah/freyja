"""Loopback callback: TOCTOU-safe port reservation, cached redirect port,
CIMD pinned-port pool, paste parser, one-shot HTTP listener with state check."""

from __future__ import annotations

import asyncio
import socket
import urllib.error
import urllib.request

import pytest

from bridge.mcp.oauth import callback as cb
from bridge.mcp.oauth.callback import (
    CIMD_PORTS,
    CallbackCapture,
    CallbackGate,
    LoopbackCallbackServer,
    cached_redirect_port,
    cached_redirect_uri,
    loopback_redirect_uri,
    note_assigned_cimd_port,
    park_reserved_socket,
    parse_callback_input,
    pick_cimd_port,
    release_reserved_port,
    reserve_callback_port,
    reserve_fixed_port,
    state_from_authorization_url,
    wait_for_authorization_response,
)
from bridge.mcp.oauth.gates import (
    OAuthCallbackPortInUseError,
    OAuthCallbackTimeoutError,
    OAuthNeedsAuthError,
    OAuthUserSkippedError,
)
from bridge.mcp.oauth.storage import FreyjaTokenStorage, write_json


@pytest.fixture(autouse=True)
def _reset_ports():
    cb.reset_port_state_for_tests()
    yield
    cb.reset_port_state_for_tests()


def _get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------------------
# Port reservation
# ---------------------------------------------------------------------------


def test_reserve_callback_port_returns_distinct_ports_and_parks_sockets():
    a = reserve_callback_port()
    b = reserve_callback_port()
    assert a != b
    assert cb.is_port_reserved(a) and cb.is_port_reserved(b)


def test_reserved_port_cannot_be_stolen():
    port = reserve_callback_port()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_release_reserved_port_frees_it():
    port = reserve_callback_port()
    release_reserved_port(port)
    assert not cb.is_port_reserved(port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))  # now free
    finally:
        probe.close()


def test_reserve_fixed_port_true_then_false_when_taken():
    port = reserve_callback_port()
    release_reserved_port(port)
    assert reserve_fixed_port(port) is True
    assert reserve_fixed_port(port) is False  # our own parked socket holds it


def test_park_evicts_oldest_ephemeral_but_never_pinned(monkeypatch):
    monkeypatch.setattr(cb, "_MAX_RESERVED_SOCKETS", 3)
    pinned = CIMD_PORTS[0]
    pinned_sock = socket.socket()
    pinned_sock.bind(("127.0.0.1", 0))  # any port; we register it under the pinned key
    park_reserved_socket(pinned, pinned_sock)
    ports = [reserve_callback_port() for _ in range(4)]
    # Cap is 3: pinned + 2 newest ephemeral survive; the two oldest ephemeral evicted.
    assert cb.is_port_reserved(pinned)
    assert not cb.is_port_reserved(ports[0])
    assert not cb.is_port_reserved(ports[1])
    assert cb.is_port_reserved(ports[2]) and cb.is_port_reserved(ports[3])


def test_server_adopts_reserved_socket_closing_the_toctou_window():
    port = reserve_callback_port()
    server = LoopbackCallbackServer(port, server_name="acme")
    server.start()
    try:
        assert not cb.is_port_reserved(port)  # taken over by the server
        assert server.port == port
        status, body = _get(f"http://127.0.0.1:{port}/nope")
        assert status == 404
    finally:
        server.close()
    # After close the port is free again (SO_REUSEADDR: the served connection
    # leaves a TIME_WAIT entry, which is what the fixed-port path handles too).
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_server_reports_port_in_use_clearly():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        server = LoopbackCallbackServer(port, server_name="acme")
        with pytest.raises(OAuthCallbackPortInUseError) as ei:
            server.start()
        assert str(port) in str(ei.value)
        assert "/mcp login acme" in str(ei.value)
    finally:
        blocker.close()


def test_server_binds_fixed_port_without_reservation():
    tmp = socket.socket()
    tmp.bind(("127.0.0.1", 0))
    port = tmp.getsockname()[1]
    tmp.close()
    server = LoopbackCallbackServer(port, server_name="acme")
    server.start()
    try:
        assert _get(f"http://127.0.0.1:{port}/x")[0] == 404
    finally:
        server.close()


# ---------------------------------------------------------------------------
# CIMD pinned ports
# ---------------------------------------------------------------------------


def test_cimd_ports_are_below_ephemeral_floor():
    assert len(CIMD_PORTS) == 5
    assert all(1024 < p < 32768 for p in CIMD_PORTS)
    assert len(set(CIMD_PORTS)) == 5


def test_pick_cimd_port_walks_range_and_wraps(monkeypatch):
    available = set(CIMD_PORTS)

    def fake_reserve(port):
        return port in available

    monkeypatch.setattr(cb, "reserve_fixed_port", fake_reserve)
    picks = [pick_cimd_port() for _ in range(5)]
    assert picks == list(CIMD_PORTS)
    assert pick_cimd_port() == CIMD_PORTS[0]  # wraps rather than falling back to DCR
    assert cb.assigned_cimd_ports() == CIMD_PORTS


def test_pick_cimd_port_skips_ports_taken_by_another_process(monkeypatch):
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: port != CIMD_PORTS[0])
    assert pick_cimd_port() == CIMD_PORTS[1]


def test_pick_cimd_port_none_when_nothing_bindable(monkeypatch):
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: False)
    assert pick_cimd_port() is None


def test_note_assigned_cimd_port_only_pinned_range():
    note_assigned_cimd_port(40000)
    note_assigned_cimd_port(CIMD_PORTS[2])
    note_assigned_cimd_port(CIMD_PORTS[2])
    assert cb.assigned_cimd_ports() == (CIMD_PORTS[2],)


# ---------------------------------------------------------------------------
# Cached redirect port / uri
# ---------------------------------------------------------------------------


def test_cached_redirect_port_from_client_json(tmp_path):
    s = FreyjaTokenStorage("acme", root=tmp_path)
    assert cached_redirect_port(s) is None
    assert cached_redirect_port(None) is None
    write_json(s.client_info_path(), {
        "client_id": "c", "redirect_uris": ["http://127.0.0.1:41234/callback"],
    })
    assert cached_redirect_port(s) == 41234
    assert cached_redirect_uri(s) is None


def test_cached_redirect_port_accepts_localhost_and_ignores_https(tmp_path):
    s = FreyjaTokenStorage("acme", root=tmp_path)
    write_json(s.client_info_path(), {
        "client_id": "c",
        "redirect_uris": ["https://proxy.example/cb", "http://localhost:5151/callback"],
    })
    assert cached_redirect_port(s) == 5151
    assert cached_redirect_uri(s) == "https://proxy.example/cb"


def test_cached_redirect_port_ignores_foreign_paths(tmp_path):
    s = FreyjaTokenStorage("acme", root=tmp_path)
    write_json(s.client_info_path(), {
        "client_id": "c", "redirect_uris": ["http://127.0.0.1:5151/other", "garbage"],
    })
    assert cached_redirect_port(s) is None


def test_loopback_redirect_uri_and_state_extraction():
    assert loopback_redirect_uri(1234) == "http://127.0.0.1:1234/callback"
    assert loopback_redirect_uri(1234, "localhost") == "http://localhost:1234/callback"
    url = "https://as.example/authorize?client_id=x&state=abc123&code_challenge=zz"
    assert state_from_authorization_url(url) == "abc123"
    assert state_from_authorization_url("https://as.example/authorize") is None


# ---------------------------------------------------------------------------
# Paste parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "http://127.0.0.1:37949/callback?code=abc&state=xyz",
    "https://mcp.example.com/callback?code=abc&state=xyz&foo=bar",
    "?code=abc&state=xyz",
    "code=abc&state=xyz",
    "  code=abc&state=xyz#frag  ",
])
def test_parse_callback_input_variants(line):
    cap = parse_callback_input(line)
    assert cap is not None
    assert cap.code == "abc" and cap.state == "xyz" and not cap.skipped


def test_parse_callback_input_error_and_iss():
    cap = parse_callback_input(
        "?error=access_denied&error_description=nope&state=s&iss=https%3A%2F%2Fas"
    )
    assert cap.error == "access_denied"
    assert cap.error_description == "nope"
    assert cap.iss == "https://as"
    assert cap.is_terminal


@pytest.mark.parametrize("tok", ["skip", "SKIP", "cancel", "s", "n", "no", "q", "quit"])
def test_parse_callback_input_skip_tokens(tok):
    cap = parse_callback_input(tok)
    assert cap is not None and cap.skipped


@pytest.mark.parametrize("line", ["", "   ", "hello world", "http://x/y?foo=bar", None])
def test_parse_callback_input_rejects_noise(line):
    assert parse_callback_input(line) is None


# ---------------------------------------------------------------------------
# Loopback server behaviour
# ---------------------------------------------------------------------------


async def test_success_redirect_is_captured_once_with_state_check():
    port = reserve_callback_port()
    server = LoopbackCallbackServer(port, expected_state="good", server_name="acme")
    server.start()
    try:
        # Wrong state: failure page, NOT consumed.
        status, body = await asyncio.to_thread(
            _get, f"http://127.0.0.1:{port}/callback?code=EVIL&state=bad"
        )
        assert status == 400
        assert b"mismatch" in body.lower()
        assert server.capture is None
        assert server.rejected_state_count == 1

        # Missing code/error: 400, not consumed.
        status, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/callback?state=good")
        assert status == 400
        assert server.capture is None

        # Good state: success page, consumed.
        status, body = await asyncio.to_thread(
            _get, f"http://127.0.0.1:{port}/callback?code=GOOD&state=good&iss=https%3A%2F%2Fas"
        )
        assert status == 200
        assert b"Authorization Successful" in body
        assert b"Freyja" in body

        # Second delivery: 409, first result kept.
        status, body = await asyncio.to_thread(
            _get, f"http://127.0.0.1:{port}/callback?code=LATE&state=good"
        )
        assert status == 409

        result = await wait_for_authorization_response(server, timeout=2)
        assert result.code == "GOOD"
        assert result.state == "good"
        assert result.iss == "https://as"
    finally:
        server.close()


async def test_error_redirect_raises_needs_auth():
    port = reserve_callback_port()
    server = LoopbackCallbackServer(port, server_name="acme")
    server.start()
    try:
        status, body = await asyncio.to_thread(
            _get, f"http://127.0.0.1:{port}/callback?error=access_denied&error_description=User%20said%20no"
        )
        assert status == 200
        assert b"Authorization Failed" in body
        with pytest.raises(OAuthNeedsAuthError) as ei:
            await wait_for_authorization_response(server, timeout=2)
        assert "access_denied" in str(ei.value)
        assert "User said no" in str(ei.value)
        assert "/mcp login acme" in str(ei.value)
    finally:
        server.close()


async def test_timeout_raises_with_login_hint_and_cimd_hint():
    port = reserve_callback_port()
    server = LoopbackCallbackServer(port, server_name="acme")
    server.start()
    try:
        with pytest.raises(OAuthCallbackTimeoutError) as ei:
            await wait_for_authorization_response(server, timeout=0.2)
        assert "/mcp login acme" in str(ei.value)
        assert "Metadata Document" not in str(ei.value)
    finally:
        server.close()
    server2 = LoopbackCallbackServer(reserve_callback_port(), server_name="acme")
    server2.start()
    try:
        with pytest.raises(OAuthCallbackTimeoutError) as ei:
            await wait_for_authorization_response(
                server2, timeout=0.2, cimd_url="https://me.example/cimd.json"
            )
        assert "https://me.example/cimd.json" in str(ei.value)
        assert '"cimd": false' in str(ei.value)
    finally:
        server2.close()


async def test_paste_source_wins_race_and_is_state_checked():
    port = reserve_callback_port()
    server = LoopbackCallbackServer(port, expected_state="st", server_name="acme")
    server.start()
    try:
        async def paste():
            return "http://127.0.0.1:1/callback?code=PASTED&state=st"

        result = await wait_for_authorization_response(server, timeout=2, paste_source=paste)
        assert result.code == "PASTED"
    finally:
        server.close()

    server = LoopbackCallbackServer(
        reserve_callback_port(), expected_state="st", server_name="acme"
    )
    server.start()
    try:
        async def bad_paste():
            return "code=EVIL&state=wrong"

        with pytest.raises(OAuthCallbackTimeoutError):
            await wait_for_authorization_response(server, timeout=0.3, paste_source=bad_paste)
        assert server.capture is None
    finally:
        server.close()


async def test_paste_skip_token_raises_user_skipped():
    server = LoopbackCallbackServer(reserve_callback_port(), server_name="acme")
    server.start()
    try:
        async def paste():
            return "skip"

        with pytest.raises(OAuthUserSkippedError) as ei:
            await wait_for_authorization_response(server, timeout=2, paste_source=paste)
        assert "user_skipped" in str(ei.value)
    finally:
        server.close()


async def test_paste_source_none_or_garbage_is_ignored():
    server = LoopbackCallbackServer(reserve_callback_port(), server_name="acme")
    server.start()
    try:
        async def paste():
            return "not a redirect"

        with pytest.raises(OAuthCallbackTimeoutError):
            await wait_for_authorization_response(server, timeout=0.3, paste_source=paste)
        assert server.capture is None
    finally:
        server.close()


async def test_paste_source_exception_is_non_fatal():
    server = LoopbackCallbackServer(reserve_callback_port(), server_name="acme")
    server.start()
    try:
        async def paste():
            raise RuntimeError("stdin closed")

        with pytest.raises(OAuthCallbackTimeoutError):
            await wait_for_authorization_response(server, timeout=0.3, paste_source=paste)
    finally:
        server.close()


async def test_slow_paste_source_is_cancelled_when_http_wins():
    port = reserve_callback_port()
    server = LoopbackCallbackServer(port, server_name="acme")
    server.start()
    cancelled = asyncio.Event()
    try:
        async def paste():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return None

        async def browser():
            await asyncio.sleep(0.05)
            await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/callback?code=HTTP")

        task = asyncio.create_task(browser())
        result = await wait_for_authorization_response(server, timeout=5, paste_source=paste)
        await task
        assert result.code == "HTTP"
        assert cancelled.is_set()
    finally:
        server.close()


async def test_callback_gate_deliver_params_state_check_and_one_shot():
    gate = CallbackGate(expected_state="s1", server_name="acme")
    assert gate.deliver_params(code=None, state="s1") is False  # nothing terminal
    assert gate.deliver_params(code="c", state="wrong") is False
    assert gate.rejected_state_count == 1
    assert gate.deliver_params(code="c", state="s1") is True
    assert gate.deliver_params(code="c2", state="s1") is False  # one-shot
    result = await wait_for_authorization_response(gate, timeout=1)
    assert result.code == "c"
    gate.close()


def test_callback_capture_terminal_semantics():
    assert not CallbackCapture().is_terminal
    assert CallbackCapture(code="x").is_terminal
    assert CallbackCapture(error="e").is_terminal
    assert CallbackCapture(skipped=True).is_terminal


async def test_handler_log_lines_never_include_query(caplog):
    import logging

    port = reserve_callback_port()
    server = LoopbackCallbackServer(port, server_name="acme")
    server.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="bridge.mcp.oauth.callback"):
            await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/callback?code=SECRETCODE&state=s")
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "SECRETCODE" not in joined
        assert "/callback" in joined
    finally:
        server.close()


def test_server_start_is_idempotent_and_close_safe_twice():
    server = LoopbackCallbackServer(reserve_callback_port(), server_name="acme")
    server.start()
    server.start()
    assert server.running
    server.close()
    server.close()
    assert not server.running
