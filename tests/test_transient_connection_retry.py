"""Connection-level transient disconnects (the peer/proxy dropped the connection
mid-request — httpx RemoteProtocolError, reset, broken pipe, SSL EOF) must be
retryable. Otherwise a passing network/server blip on a long streaming LLM call
escapes the runner's `except ProviderError` retry path raw and fails the turn.
"""

from __future__ import annotations

from engine.errors import is_retryable_error, is_transient_connection_error

# The exact error that failed a turn in production.
REMOTE_PROTOCOL = (
    "RemoteProtocolError: peer closed connection without sending complete "
    "message body (incomplete chunked read)"
)


def test_remote_protocol_error_is_retryable():
    assert is_transient_connection_error(REMOTE_PROTOCOL)
    assert is_retryable_error(REMOTE_PROTOCOL)


def test_other_connection_drops_are_retryable():
    for msg in [
        "httpx.RemoteProtocolError: Server disconnected without sending a response.",
        "ConnectionResetError: [Errno 54] Connection reset by peer",
        "('Connection aborted.', ConnectionResetError(...))",
        "BrokenPipeError: [Errno 32] Broken pipe",
        "httpx.ConnectError: All connection attempts failed",
        "ssl.SSLError: [SSL] EOF occurred in violation of protocol",
        "incomplete chunked read",
    ]:
        assert is_retryable_error(msg), msg


def test_non_connection_errors_unaffected():
    # Genuinely non-retryable failures must stay non-retryable.
    assert not is_transient_connection_error("authentication_error: invalid x-api-key")
    assert not is_retryable_error("authentication_error: invalid x-api-key")
    assert not is_retryable_error("")
    # A clean success-ish string isn't a connection error.
    assert not is_transient_connection_error("model emitted 3 tool calls")
