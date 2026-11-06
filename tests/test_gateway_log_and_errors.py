"""Turn failures reach Slack, and the stdout log stays bounded.

Both pin lessons from one incident: a Slack thread pinned to a Gemini model on a daemon whose env had no GEMINI_API_KEY. Every turn in that thread died, and the operator saw nothing at all: the bridge emitted `{"type": "error", ...}` with NO sessionId, and emit()'s listener fan-out is keyed on exactly that field, so it reached no gateway listener. Meanwhile the log that recorded it had grown to 560 MB because launchd captures the daemon's stdout and nothing rotates it.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from bridge.freyja_bridge import _emit_turn_failed
from bridge.gateway.pid import rotate_log_if_large
from bridge.gateway.platforms.base import MessageSource
from bridge.gateway.stream_consumer import SlackStreamConsumer
from tests.slack_fakes import StreamingFakeAdapter

# ── turn failures are session-scoped so listeners actually see them ──

def _capture_emit(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr("bridge.freyja_bridge.emit", lambda e: seen.append(e))
    return seen


def test_turn_failure_carries_a_session_id_and_always_a_message(monkeypatch):
    """The whole bug: without sessionId the event reaches no listener. And `str(exc)` is empty for e.g. a bare TimeoutError; ":warning: That turn failed: " with nothing after it helps nobody."""
    seen = _capture_emit(monkeypatch)
    sess = SimpleNamespace(id="freyja:slack:T1:channel:C1:1.0", model_id="gemini-3.8-flash")
    _emit_turn_failed(sess, RuntimeError("GEMINI_API_KEY is not set"))

    assert len(seen) == 1
    evt = seen[0]
    assert evt["type"] == "turn_failed"
    assert evt["sessionId"] == sess.id, "no sessionId means no listener fan-out"
    assert "GEMINI_API_KEY is not set" in evt["message"]
    assert evt["model"] == "gemini-3.8-flash"

    _emit_turn_failed(SimpleNamespace(id="s", model_id="m"), TimeoutError())
    assert seen[-1]["message"] == "TimeoutError"


def test_missing_key_failures_explain_where_the_key_goes_and_others_get_no_hint(monkeypatch):
    """The daemon reads ~/.freyja/.env, not the project .env; that distinction is the entire reason this took an hour to find."""
    seen = _capture_emit(monkeypatch)
    sess = SimpleNamespace(id="s", model_id="gemini-3.8-flash")
    _emit_turn_failed(sess, RuntimeError("GEMINI_API_KEY is not set"))

    hint = seen[-1]["hint"]
    assert "~/.freyja/.env" in hint
    assert "GEMINI_API_KEY" in hint
    assert "/model" in hint, "offer the other way out, not just the fix"

    _emit_turn_failed(SimpleNamespace(id="s", model_id="m"), ValueError("boom"))
    assert seen[-1]["hint"] == ""
    assert seen[-1]["message"] == "boom"


def test_reporting_a_failure_cannot_raise(monkeypatch):
    """This runs inside the turn loop's except handler. If it raised it would replace a reportable failure with an unreportable one."""

    def boom(_e):
        raise RuntimeError("emit is broken")

    monkeypatch.setattr("bridge.freyja_bridge.emit", boom)
    _emit_turn_failed(SimpleNamespace(id="s", model_id="m"), ValueError("x"))


@pytest.mark.asyncio
async def test_the_consumer_posts_the_failure_and_closes_the_stream():
    """No turn_complete follows a failure, so the consumer has to post the notice itself and seal the open stream; otherwise the card stays spinning and the bot simply stops answering."""
    adapter = StreamingFakeAdapter()
    source = MessageSource(
        platform="slack", workspace_id="T1", chat_id="C1", chat_type="channel",
        user_id="U1", message_id="1.1", thread_id="1.1",
    )
    consumer = SlackStreamConsumer(
        adapter, source, session_key="freyja:slack:T1:channel:C1:1.1", verbosity="all",
    )
    await consumer._handle_event_async({"type": "thinking_delta", "thinking": "hmm"})
    assert adapter.streams_started == 1
    await consumer._handle_event_async({
        "type": "turn_failed", "message": "GEMINI_API_KEY is not set",
        "hint": " Put it in ~/.freyja/.env.", "model": "gemini-3.8-flash",
    })
    assert consumer.finalized
    assert adapter.streams_stopped == ["stream-ts"]
    notice = adapter.sent[0]
    assert "That turn failed" in notice and "GEMINI_API_KEY is not set" in notice
    assert "gemini-3.8-flash" in notice and "~/.freyja/.env" in notice


# ── log rotation ─────────────────────────────────────────────────────

def test_small_or_missing_log_is_left_alone(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_bytes(b"line\n" * 100)
    before = log.read_bytes()
    assert rotate_log_if_large(log) is False
    assert log.read_bytes() == before
    assert rotate_log_if_large(tmp_path / "nope.log") is False


def test_large_log_is_truncated_in_place_under_an_open_appending_descriptor(tmp_path):
    """Truncate rather than rename: launchd holds this descriptor open for the daemon's lifetime, so a renamed file would keep receiving every subsequent write while the new one stayed empty. Rotation also depends on O_APPEND writing at 0 after truncation, leaving no sparse hole; if that ever stopped holding, rotation would 'work' while the file silently stayed huge."""
    log = tmp_path / "gateway.log"
    log.write_bytes(b"x" * 4096 + b"\n")
    inode_before = log.stat().st_ino
    fd = os.open(log, os.O_WRONLY | os.O_APPEND)
    try:
        assert rotate_log_if_large(log, max_bytes=1024, keep_bytes=512) is True
        assert log.exists() and log.stat().st_size == 0
        os.write(fd, b"fresh\n")
    finally:
        os.close(fd)
    assert log.stat().st_ino == inode_before, (
        "the file must keep its identity — a new inode means launchd's "
        "open descriptor now points at an orphan"
    )
    assert log.read_bytes() == b"fresh\n"


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
