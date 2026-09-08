"""Turn failures reach Slack, and the stdout log stays bounded.

Both pin lessons from one incident: a Slack thread pinned to a Gemini
model on a daemon whose env had no GEMINI_API_KEY. Every turn in that
thread died, and the operator saw nothing at all — the bridge emitted
`{"type": "error", ...}` with NO sessionId, and emit()'s listener
fan-out is keyed on exactly that field, so it reached no gateway
listener. Meanwhile the log that recorded it had grown to 560 MB
because launchd captures the daemon's stdout and nothing rotates it.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from bridge.gateway.pid import (
    GATEWAY_LOG_KEEP_BYTES,
    GATEWAY_LOG_MAX_BYTES,
    rotate_log_if_large,
)


# ── turn failures are session-scoped so listeners actually see them ──

def _capture_emit(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr("bridge.freyja_bridge.emit", lambda e: seen.append(e))
    return seen


def test_turn_failure_carries_a_session_id(monkeypatch):
    """The whole bug: without sessionId the event reaches no listener."""
    from bridge.freyja_bridge import _emit_turn_failed

    seen = _capture_emit(monkeypatch)
    sess = SimpleNamespace(id="freyja:slack:T1:channel:C1:1.0", model_id="gemini-3.7-flash")
    _emit_turn_failed(sess, RuntimeError("GEMINI_API_KEY is not set"))

    assert len(seen) == 1
    evt = seen[0]
    assert evt["type"] == "turn_failed"
    assert evt["sessionId"] == sess.id, "no sessionId means no listener fan-out"
    assert "GEMINI_API_KEY is not set" in evt["message"]
    assert evt["model"] == "gemini-3.7-flash"


def test_missing_key_failures_explain_where_the_key_goes(monkeypatch):
    """The daemon reads ~/.freyja/.env, not the project .env — that
    distinction is the entire reason this took an hour to find."""
    from bridge.freyja_bridge import _emit_turn_failed

    seen = _capture_emit(monkeypatch)
    sess = SimpleNamespace(id="s", model_id="gemini-3.7-flash")
    _emit_turn_failed(sess, RuntimeError("GEMINI_API_KEY is not set"))

    hint = seen[0]["hint"]
    assert "~/.freyja/.env" in hint
    assert "GEMINI_API_KEY" in hint
    assert "/model" in hint, "offer the other way out, not just the fix"


def test_unrelated_failures_get_no_key_hint(monkeypatch):
    from bridge.freyja_bridge import _emit_turn_failed

    seen = _capture_emit(monkeypatch)
    _emit_turn_failed(SimpleNamespace(id="s", model_id="m"), ValueError("boom"))
    assert seen[0]["hint"] == ""
    assert seen[0]["message"] == "boom"


def test_empty_exception_still_reports_something(monkeypatch):
    """`str(exc)` is empty for e.g. bare TimeoutError; ":warning: That
    turn failed: " with nothing after it helps nobody."""
    from bridge.freyja_bridge import _emit_turn_failed

    seen = _capture_emit(monkeypatch)
    _emit_turn_failed(SimpleNamespace(id="s", model_id="m"), TimeoutError())
    assert seen[0]["message"] == "TimeoutError"


def test_reporting_a_failure_cannot_raise(monkeypatch):
    """This runs inside the turn loop's except handler. If it raised it
    would replace a reportable failure with an unreportable one."""
    from bridge.freyja_bridge import _emit_turn_failed

    def boom(_e):
        raise RuntimeError("emit is broken")

    monkeypatch.setattr("bridge.freyja_bridge.emit", boom)
    _emit_turn_failed(SimpleNamespace(id="s", model_id="m"), ValueError("x"))


def test_consumer_handles_turn_failed():
    """The Slack consumer dispatches on event type; turn_failed has to be
    a branch there or the event arrives and is dropped."""
    import inspect

    from bridge.gateway.stream_consumer import SlackStreamConsumer

    src = inspect.getsource(SlackStreamConsumer)
    assert '"turn_failed"' in src
    assert "_handle_turn_failed" in src
    # It must close the stream too — no turn_complete is coming.
    handler = inspect.getsource(SlackStreamConsumer._handle_turn_failed)
    assert "finalize" in handler


# ── log rotation ─────────────────────────────────────────────────────

def test_small_log_is_left_alone(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_bytes(b"line\n" * 100)
    before = log.read_bytes()
    assert rotate_log_if_large(log) is False
    assert log.read_bytes() == before


def test_large_log_is_truncated_in_place(tmp_path):
    """Truncate rather than rename: launchd holds this descriptor open
    for the daemon's lifetime, so a renamed file would keep receiving
    every subsequent write while the new one stayed empty."""
    log = tmp_path / "gateway.log"
    log.write_bytes(b"x" * 4096 + b"\n")
    inode_before = log.stat().st_ino

    assert rotate_log_if_large(log, max_bytes=1024, keep_bytes=512) is True

    assert log.exists() and log.stat().st_size == 0
    assert log.stat().st_ino == inode_before, (
        "the file must keep its identity — a new inode means launchd's "
        "open descriptor now points at an orphan"
    )


def test_rotation_preserves_a_readable_tail(tmp_path):
    log = tmp_path / "gateway.log"
    lines = [f"event-{i}\n".encode() for i in range(5000)]
    log.write_bytes(b"".join(lines))

    assert rotate_log_if_large(log, max_bytes=1024, keep_bytes=4096) is True

    archive = tmp_path / "gateway.log.1"
    kept = archive.read_bytes()
    assert kept, "the tail is the only diagnosable part after truncation"
    # Must start on a line boundary, not mid-record.
    assert kept.startswith(b"event-")
    # And must be the END of the log, which is the part worth keeping.
    assert kept.rstrip().endswith(b"event-4999")


def test_append_after_truncate_starts_at_zero(tmp_path):
    """The O_APPEND property rotation depends on: after truncation an
    already-open appending descriptor writes at 0, leaving no sparse
    hole. If this ever stopped holding, rotation would 'work' while the
    file silently stayed huge."""
    log = tmp_path / "gateway.log"
    log.write_bytes(b"y" * 4096)
    fd = os.open(log, os.O_WRONLY | os.O_APPEND)
    try:
        assert rotate_log_if_large(log, max_bytes=1024, keep_bytes=512) is True
        os.write(fd, b"fresh\n")
    finally:
        os.close(fd)
    assert log.read_bytes() == b"fresh\n"


def test_missing_file_is_not_an_error(tmp_path):
    assert rotate_log_if_large(tmp_path / "nope.log") is False


def test_caps_are_sane():
    assert GATEWAY_LOG_KEEP_BYTES < GATEWAY_LOG_MAX_BYTES
    assert GATEWAY_LOG_MAX_BYTES <= 256 * 1024 * 1024
