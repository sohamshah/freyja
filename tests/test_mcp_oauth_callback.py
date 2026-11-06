"""Loopback callback: TOCTOU-safe port reservation, cached redirect port,
CIMD pinned-port pool, paste parser, one-shot HTTP listener with state check."""

from __future__ import annotations

import asyncio
import logging
import socket
import urllib.error
import urllib.request

import pytest

from bridge.mcp.oauth import callback as cb
from bridge.mcp.oauth.callback import (
    CIMD_PORTS,
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
def _reset_ports(monkeypatch):
    cb.reset_port_state_for_tests()
    # The capture wait polls at this interval; tighten it so timeouts and
    # deliveries are observed in milliseconds rather than tenths of a second.
    monkeypatch.setattr(cb, "_POLL_INTERVAL_S", 0.005)
    yield
    cb.reset_port_state_for_tests()


def _get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _bind_ok(port: int, *, reuse: bool = False) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reuse:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# Port reservation
# ---------------------------------------------------------------------------


def test_port_reservation_parks_releases_and_blocks_theft():
    a = reserve_callback_port()
    b = reserve_callback_port()
    assert a != b
    assert cb.is_port_reserved(a) and cb.is_port_reserved(b)
    assert not _bind_ok(a)  # the parked socket holds it
    release_reserved_port(a)
    assert not cb.is_port_reserved(a)
    assert _bind_ok(a)  # now free
    assert reserve_fixed_port(a) is True
    assert reserve_fixed_port(a) is False  # our own parked socket holds it


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
    assert _bind_ok(port, reuse=True)


def test_fixed_port_binds_without_reservation_reports_in_use_and_is_idempotent():
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

    tmp = socket.socket()
    tmp.bind(("127.0.0.1", 0))
    port = tmp.getsockname()[1]
    tmp.close()
    server = LoopbackCallbackServer(port, server_name="acme")
    server.start()
    server.start()  # idempotent
    try:
        assert server.running
        assert _get(f"http://127.0.0.1:{port}/x")[0] == 404
    finally:
        server.close()
        server.close()  # safe twice
    assert not server.running


# ---------------------------------------------------------------------------
# CIMD pinned ports
# ---------------------------------------------------------------------------


def test_pick_cimd_port_walks_skips_wraps_and_records(monkeypatch):
    available = set(CIMD_PORTS)
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: port in available)
    picks = [pick_cimd_port() for _ in range(5)]
    assert picks == list(CIMD_PORTS)
    assert pick_cimd_port() == CIMD_PORTS[0]  # wraps rather than falling back to DCR
    assert cb.assigned_cimd_ports() == CIMD_PORTS

    cb.reset_port_state_for_tests()
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: port != CIMD_PORTS[0])
    assert pick_cimd_port() == CIMD_PORTS[1]  # taken by another process -> skipped
    cb.reset_port_state_for_tests()  # nothing assigned to us, nothing bindable -> None
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: False)
    assert pick_cimd_port() is None

    cb.reset_port_state_for_tests()
    note_assigned_cimd_port(40000)  # outside the pinned range: ignored
    note_assigned_cimd_port(CIMD_PORTS[2])
    note_assigned_cimd_port(CIMD_PORTS[2])
    assert cb.assigned_cimd_ports() == (CIMD_PORTS[2],)


# ---------------------------------------------------------------------------
# Cached redirect port / uri + URL helpers
# ---------------------------------------------------------------------------


def test_cached_redirect_port_uri_and_state_helpers(tmp_path):
    s = FreyjaTokenStorage("acme", root=tmp_path)
    assert cached_redirect_port(s) is None
    assert cached_redirect_port(None) is None
    write_json(s.client_info_path(), {
        "client_id": "c", "redirect_uris": ["http://127.0.0.1:41234/callback"],
    })
    assert cached_redirect_port(s) == 41234
    assert cached_redirect_uri(s) is None
    # localhost is accepted; an https redirect is reported as the external uri.
    write_json(s.client_info_path(), {
        "client_id": "c",
        "redirect_uris": ["https://proxy.example/cb", "http://localhost:5151/callback"],
    })
    assert cached_redirect_port(s) == 5151
    assert cached_redirect_uri(s) == "https://proxy.example/cb"
    # Foreign paths and garbage are ignored.
    write_json(s.client_info_path(), {
        "client_id": "c", "redirect_uris": ["http://127.0.0.1:5151/other", "garbage"],
    })
    assert cached_redirect_port(s) is None

    assert loopback_redirect_uri(1234) == "http://127.0.0.1:1234/callback"
    assert loopback_redirect_uri(1234, "localhost") == "http://localhost:1234/callback"
    url = "https://as.example/authorize?client_id=x&state=abc123&code_challenge=zz"
    assert state_from_authorization_url(url) == "abc123"
    assert state_from_authorization_url("https://as.example/authorize") is None


# ---------------------------------------------------------------------------
# Paste parser
# ---------------------------------------------------------------------------


def test_parse_callback_input_accepted_formats_error_skip_and_noise():
    for line in (
        "http://127.0.0.1:37949/callback?code=abc&state=xyz",
        "https://mcp.example.com/callback?code=abc&state=xyz&foo=bar",
        "?code=abc&state=xyz",
        "code=abc&state=xyz",
        "  code=abc&state=xyz#frag  ",
    ):
        cap = parse_callback_input(line)
        assert cap is not None, line
        assert cap.code == "abc" and cap.state == "xyz" and not cap.skipped
        assert cap.is_terminal

    cap = parse_callback_input(
        "?error=access_denied&error_description=nope&state=s&iss=https%3A%2F%2Fas"
    )
    assert cap.error == "access_denied"
    assert cap.error_description == "nope"
    assert cap.iss == "https://as"
    assert cap.is_terminal

    for tok in ("skip", "SKIP", "cancel", "s", "n", "no", "q", "quit"):
        cap = parse_callback_input(tok)
        assert cap is not None and cap.skipped and cap.is_terminal, tok

    for line in ("", "   ", "hello world", "http://x/y?foo=bar", None):
        assert parse_callback_input(line) is None, line


# ---------------------------------------------------------------------------
# Loopback server behaviour
# ---------------------------------------------------------------------------


async def test_success_redirect_is_captured_once_with_state_check_and_no_query_in_logs(caplog):
    port = reserve_callback_port()
    server = LoopbackCallbackServer(port, expected_state="good", server_name="acme")
    server.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="bridge.mcp.oauth.callback"):
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
                _get, f"http://127.0.0.1:{port}/callback?code=SECRETCODE&state=good&iss=https%3A%2F%2Fas"
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
        assert result.code == "SECRETCODE"
        assert result.state == "good"
        assert result.iss == "https://as"
        # The handler logs the path, never the query (it carries the code).
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "SECRETCODE" not in joined and "EVIL" not in joined
        assert "/callback" in joined
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
    server = LoopbackCallbackServer(reserve_callback_port(), server_name="acme")
    server.start()
    try:
        with pytest.raises(OAuthCallbackTimeoutError) as ei:
            await wait_for_authorization_response(server, timeout=0.05)
        assert "/mcp login acme" in str(ei.value)
        assert "Metadata Document" not in str(ei.value)
        with pytest.raises(OAuthCallbackTimeoutError) as ei:
            await wait_for_authorization_response(
                server, timeout=0.05, cimd_url="https://me.example/cimd.json"
            )
        assert "https://me.example/cimd.json" in str(ei.value)
        assert '"cimd": false' in str(ei.value)
    finally:
        server.close()


async def test_paste_source_wins_is_state_checked_and_failures_are_non_fatal():
    # Bad state, garbage and a raising source are ignored (timeout, nothing
    # consumed); the server keeps serving across them.
    server = LoopbackCallbackServer(
        reserve_callback_port(), expected_state="st", server_name="acme",
    )
    server.start()
    try:
        async def bad_paste():
            return "code=EVIL&state=wrong"

        async def garbage():
            return "not a redirect"

        async def broken():
            raise RuntimeError("stdin closed")

        for source in (bad_paste, garbage, broken):
            with pytest.raises(OAuthCallbackTimeoutError):
                await wait_for_authorization_response(server, timeout=0.05, paste_source=source)
            assert server.capture is None

        async def paste():
            return "http://127.0.0.1:1/callback?code=PASTED&state=st"

        result = await wait_for_authorization_response(server, timeout=2, paste_source=paste)
        assert result.code == "PASTED"
    finally:
        server.close()

    server = LoopbackCallbackServer(reserve_callback_port(), server_name="acme")
    server.start()
    try:
        async def skip():
            return "skip"

        with pytest.raises(OAuthUserSkippedError) as ei:
            await wait_for_authorization_response(server, timeout=2, paste_source=skip)
        assert "user_skipped" in str(ei.value)
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
            await asyncio.sleep(0.01)
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
