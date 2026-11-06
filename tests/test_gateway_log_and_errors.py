"""Turn failures reach Slack, and the stdout log stays bounded.

Both pin lessons from one incident: a Slack thread pinned to a Gemini model on a daemon whose env had no GEMINI_API_KEY. Every turn in that thread died, and the operator saw nothing at all: the bridge emitted `{"type": "error", ...}` with NO sessionId, and emit()'s listener fan-out is keyed on exactly that field, so it reached no gateway listener. Meanwhile the log that recorded it had grown to 560 MB because launchd captures the daemon's stdout and nothing rotates it.
"""

from __future__ import annotations

import gzip
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


async def _run_slack_turn(fb, sess):
    """Run one turn of ``sess`` the way the gateway does, with a stream consumer on a fake Slack thread. Returns the consumer, the adapter, and the order the reply was sealed and notes posted."""
    from bridge.gateway.run import _attach_turn_consumer

    calls: list[str] = []

    class _Slack(StreamingFakeAdapter):
        async def send(self, chat_id, content, **kw):
            calls.append("note")
            return await super().send(chat_id, content, **kw)

        async def stop_stream(self, chat_id, ts, **kw):
            calls.append("reply sealed")
            return await super().stop_stream(chat_id, ts, **kw)

    adapter = _Slack()
    source = MessageSource(
        platform="slack", workspace_id="T1", chat_id="C1", chat_type="channel",
        user_id="U1", message_id="1.1", thread_id="1.1",
    )
    consumer = SlackStreamConsumer(adapter, source, session_key=sess.id, verbosity="all")
    fb._schedule_or_queue_turn(
        sess, "hi", on_turn_start=lambda: _attach_turn_consumer(sess, sess.id, consumer),
    )
    await sess.pending_task
    await consumer.wait_until_handled()
    fb.unregister_session_listener(sess.id, consumer.on_event)
    return consumer, adapter, calls


@pytest.mark.asyncio
async def test_a_failed_turn_closes_its_message_and_posts_the_failure(monkeypatch, tmp_path):
    """No turn_complete follows a failure, so the consumer has to seal the open stream and post the notice itself. It dropped turn_failed with the events it ignores, so through emit() the notice never posted and the reply stayed open until the next turn."""
    import bridge.freyja_bridge as fb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "sessions")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("session-slack")

    async def run_turn(_content, _attachments=None, **_kw):
        fb.emit({"type": "turn_start", "sessionId": sess.id})
        fb.emit({"type": "text_delta", "sessionId": sess.id, "text": "Looking into it."})
        raise RuntimeError("GEMINI_API_KEY is not set")

    sess.run_turn = run_turn
    consumer, adapter, calls = await _run_slack_turn(fb, sess)

    assert consumer.finalized
    assert calls == ["reply sealed", "note"]
    assert adapter.streamed == ["Looking into it."]
    notice = adapter.sent[0]
    assert "That turn failed" in notice and "GEMINI_API_KEY is not set" in notice
    assert "kimi-k3-fast" in notice and "~/.freyja/.env" in notice


@pytest.mark.asyncio
async def test_a_failure_the_turn_catches_itself_posts_why_under_the_reply(monkeypatch, tmp_path):
    """run_turn's own handler reports a failure as a session-scoped error, then turn_complete, and no turn_failed follows. The consumer dropped the error, so the reply just ended with no reason."""
    import bridge.freyja_bridge as fb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "sessions")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = await state.ensure_session("session-slack")
    await sess.initialize()

    async def run(_session, _message, **_kw):
        fb.emit({"type": "text_delta", "sessionId": sess.id, "text": "Looking into it."})
        raise RuntimeError("the connection reset")

    sess.runner.run = run
    consumer, adapter, calls = await _run_slack_turn(fb, sess)

    assert consumer.finalized
    assert calls == ["reply sealed", "note"]
    assert adapter.streamed == ["Looking into it."]
    assert "the connection reset" in adapter.sent[0]


@pytest.mark.asyncio
async def test_a_failure_reported_both_ways_posts_one_note_with_its_hint(monkeypatch, tmp_path):
    """A provider that cannot be built sends an error from initialize, then turn_failed from the turn queue. One note goes up, and it is turn_failed's, which says where the key goes."""
    import bridge.freyja_bridge as fb

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "sessions")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = await state.ensure_session("session-slack")
    consumer, adapter, calls = await _run_slack_turn(fb, sess)

    assert consumer.finalized
    assert calls == ["note"]
    assert "ANTHROPIC_API_KEY is not set" in adapter.sent[0]
    assert "~/.freyja/.env" in adapter.sent[0]


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
        assert rotate_log_if_large(log, max_bytes=1024) is True
        assert log.exists() and log.stat().st_size == 0
        os.write(fd, b"fresh\n")
    finally:
        os.close(fd)
    assert log.stat().st_ino == inode_before, (
        "the file must keep its identity — a new inode means launchd's "
        "open descriptor now points at an orphan"
    )
    assert log.read_bytes() == b"fresh\n"


def test_a_large_log_is_copied_whole_to_its_segment(tmp_path):
    """Each byte at the offset it had in the log, so the app's tail reads on there. The last 8 MB alone dropped the rest."""
    log = tmp_path / "gateway.log"
    log.write_bytes(b"".join(f"event-{i}\n".encode() for i in range(5000)))
    before = log.read_bytes()

    assert rotate_log_if_large(log, max_bytes=1024) is True

    assert (tmp_path / "gateway.log.1").read_bytes() == before


def test_older_segments_are_gzipped_and_no_line_is_dropped(tmp_path):
    """The user's rule: old logs are compressed, never deleted. Each rotation wrote over gateway.log.1."""
    log = tmp_path / "gateway.log"
    written = b""
    for round_ in range(3):
        lines = b"".join(f"round {round_} event-{i}\n".encode() for i in range(200))
        written += lines
        with log.open("ab") as f:
            f.write(lines)
        assert rotate_log_if_large(log, max_bytes=1024) is True
    with log.open("ab") as f:
        f.write(b"after\n")

    old = sorted(tmp_path.glob("gateway.log.*.gz"), key=lambda p: p.stat().st_mtime_ns)
    assert len(old) == 2
    kept = b"".join(
        [gzip.decompress(p.read_bytes()) for p in old]
        + [(tmp_path / "gateway.log.1").read_bytes(), log.read_bytes()]
    )
    assert kept == written + b"after\n"


async def test_the_gateway_rotates_its_logs_under_its_writers_locks(monkeypatch):
    """No line the gateway's log handlers or emit() write falls between the copy and the emptying."""
    import asyncio
    import logging

    import bridge.freyja_bridge as fb
    import bridge.gateway.pid as pid
    from bridge.gateway.run import GatewayDaemon

    rotated: list[tuple] = []

    def rotate(path, *, locks=()):
        rotated.append((path.name, tuple(locks)))
        if len(rotated) == 2:
            raise asyncio.CancelledError
        return False

    handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    monkeypatch.setattr(pid, "rotate_log_if_large", rotate)
    gateway = GatewayDaemon()
    gateway._LOG_ROTATE_INTERVAL_SEC = 0
    try:
        with pytest.raises(asyncio.CancelledError):
            await gateway._rotate_logs_periodically()
    finally:
        root.removeHandler(handler)

    assert [name for name, _ in rotated] == ["gateway.log", "gateway.err"]
    for _, locks in rotated:
        # The log handlers' locks, then emit()'s.
        assert locks[-1] is fb._EMIT_LOCK
        assert handler.lock in locks
