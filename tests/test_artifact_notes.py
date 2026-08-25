"""Operator comments on artifacts, routed back to the producing session.

The desktop's artifact browser lets the operator highlight lines in any file
any session ever wrote and comment on them. The comment arrives at the bridge
as an ``artifact_note`` command and has to reach the session that most recently
touched that file — as a real user turn, not as a note in a file nobody reads.

Covers:
  · the agent-facing wording (it must name the file, quote the highlight, and
    tell the agent to re-read before editing)
  · anchor parsing, including the malformed payloads a UI can produce
  · delivery to a live session (push + wake)
  · the cold-load path, which is what makes a note on an old artifact actually
    do something — and its promise not to switch the operator's active session
  · the malformed-command and failure paths, which must never raise into the
    command loop
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.artifact_notes import (
    MAX_QUOTE_CHARS,
    NoteAnchor,
    note_label,
    render_note_message,
)

# ─── wording ──────────────────────────────────────────────────────────


def test_render_names_the_file_and_carries_the_comment() -> None:
    text = render_note_message(
        artifact_path="/Users/x/.freyja/projects/s1/report.md",
        body="  This section contradicts the benchmark.  ",
        filename="report.md",
    )
    assert "report.md" in text
    assert "/Users/x/.freyja/projects/s1/report.md" in text
    assert "This section contradicts the benchmark." in text
    # The comment is stripped, not pasted with the operator's stray spaces.
    assert "  This section" not in text


def test_render_quotes_the_highlighted_range() -> None:
    anchor = NoteAnchor(start_line=12, end_line=14, quote="a\nb\nc")
    text = render_note_message(
        artifact_path="/tmp/x.md", body="fix this", anchor=anchor, filename="x.md"
    )
    assert "lines 12-14" in text
    assert "```\na\nb\nc\n```" in text


def test_render_says_line_singular_for_one_line() -> None:
    anchor = NoteAnchor(start_line=7, end_line=7, quote="only this")
    text = render_note_message(artifact_path="/tmp/x.md", body="?", anchor=anchor)
    assert "line 7" in text
    assert "lines 7" not in text


def test_render_truncates_an_enormous_quote() -> None:
    anchor = NoteAnchor(start_line=1, end_line=9000, quote="x" * (MAX_QUOTE_CHARS * 3))
    text = render_note_message(artifact_path="/tmp/x.md", body="hm", anchor=anchor)
    assert "…[truncated]" in text
    assert len(text) < MAX_QUOTE_CHARS * 2


def test_render_tells_the_agent_to_reread_first() -> None:
    # The note may be months older than the file. Trusting the quoted snippet
    # is how an agent "fixes" a paragraph that no longer exists.
    text = render_note_message(artifact_path="/tmp/x.md", body="tighten this")
    assert "Read the file before you change anything" in text
    assert "answer" in text  # a question gets answered, not edited


def test_render_falls_back_to_the_path_basename() -> None:
    text = render_note_message(artifact_path="/a/b/deep/name.md", body="hi")
    assert "`name.md`" in text


def test_render_survives_an_empty_body() -> None:
    text = render_note_message(artifact_path="/tmp/x.md", body="   ")
    assert "(empty)" in text


def test_note_label_identifies_the_source() -> None:
    assert note_label("report.md") == "Operator (comment on report.md)"
    assert note_label("") == "Operator"


# ─── anchor parsing ───────────────────────────────────────────────────


def test_anchor_parses_a_well_formed_payload() -> None:
    anchor = NoteAnchor.from_payload({"startLine": 3, "endLine": 5, "quote": "hi"})
    assert anchor is not None
    assert (anchor.start_line, anchor.end_line) == (3, 5)


def test_anchor_normalizes_a_reversed_range() -> None:
    anchor = NoteAnchor.from_payload({"startLine": 9, "endLine": 2, "quote": "hi"})
    assert anchor is not None
    # A reversed range would render as "lines 9-2"; clamp instead.
    assert anchor.end_line == 9


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        "not a dict",
        {"startLine": 0, "endLine": 0, "quote": "x"},
        {"startLine": 1, "endLine": 2, "quote": "   "},
        {"startLine": "abc", "endLine": 2, "quote": "x"},
    ],
)
def test_anchor_rejects_malformed_payloads(payload: object) -> None:
    assert NoteAnchor.from_payload(payload) is None


# ─── delivery ─────────────────────────────────────────────────────────


class _Inbox:
    def __init__(self) -> None:
        self.pushed: list = []

    def push(self, msg) -> None:  # noqa: ANN001
        self.pushed.append(msg)


def _live_session(session_id: str, wake_log: list) -> SimpleNamespace:
    sess = SimpleNamespace(id=session_id, inbox=_Inbox(), pending_task=None)
    sess.wake_for_inbox = lambda: (wake_log.append(session_id), "woke it")[1]
    return sess


def _cmd(**over) -> dict:
    base = {
        "type": "artifact_note",
        "sessionId": "session-alpha",
        "noteId": "note_1",
        "artifactPath": "/tmp/proj/report.md",
        "artifactFilename": "report.md",
        "body": "This paragraph is wrong.",
        "anchor": {"startLine": 4, "endLine": 4, "quote": "the wrong line"},
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_live_session_gets_the_note_pushed_and_woken(monkeypatch) -> None:
    from bridge import freyja_bridge as fb

    wake_log: list = []
    sess = _live_session("session-alpha", wake_log)
    state = SimpleNamespace(sessions={"session-alpha": sess}, active_session_id="other")

    monkeypatch.setattr(fb, "_install_talk_wake_hook", lambda *_a, **_k: None)
    emitted: list = []
    monkeypatch.setattr(fb, "emit", lambda payload: emitted.append(payload))

    await fb._handle_artifact_note_command(state, _cmd())

    assert len(sess.inbox.pushed) == 1
    msg = sess.inbox.pushed[0]
    assert msg.from_role == "operator"
    assert msg.id == "note_1"
    assert "report.md" in msg.content
    assert "This paragraph is wrong." in msg.content
    assert "the wrong line" in msg.content
    # Pushing is not enough — an idle session never reaches an iteration
    # boundary on its own.
    assert wake_log == ["session-alpha"]
    # And the operator's view is not switched out from under them.
    assert state.active_session_id == "other"

    events = [e for e in emitted if e.get("subtype") == "artifact_note"]
    assert len(events) == 1
    assert events[0]["details"]["noteId"] == "note_1"


@pytest.mark.asyncio
async def test_cold_session_is_loaded_and_woken(monkeypatch) -> None:
    # The artifacts worth commenting on are usually old, so their sessions are
    # almost always cold. Queueing to a sidecar that only drains when the
    # operator reopens that session would make most notes inert.
    from bridge import freyja_bridge as fb

    wake_log: list = []
    loaded = _live_session("session-cold", wake_log)
    ensured: list = []

    async def _ensure(sid: str, *_a, **_k):
        ensured.append(sid)
        state.sessions[sid] = loaded
        state.active_session_id = sid  # what the real ensure_session does
        return loaded

    state = SimpleNamespace(sessions={}, active_session_id="session-visible")
    state.ensure_session = _ensure

    monkeypatch.setattr(fb, "_is_gateway_session_id", lambda _sid: False)
    monkeypatch.setattr(fb, "_resolve_archived_subagent", lambda _sid: None)
    monkeypatch.setattr(fb, "_install_talk_wake_hook", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "bridge.transcript_persistence.load_transcript", lambda _sid: {"version": 1}
    )

    from bridge.inbox import InboxMessage

    status = await fb.deliver_artifact_note(
        state,
        "session-cold",
        InboxMessage(
            id="n", from_session="operator", from_label="Operator",
            from_role="operator", content="body",
        ),
    )

    assert ensured == ["session-cold"]
    assert len(loaded.inbox.pushed) == 1
    assert wake_log == ["session-cold"]
    assert status == "woke it"
    # ensure_session makes what it loads active; the operator asked to comment
    # on a file, not to be moved into another conversation.
    assert state.active_session_id == "session-visible"


@pytest.mark.asyncio
async def test_session_with_no_transcript_falls_back_to_the_sidecar(monkeypatch) -> None:
    from bridge import freyja_bridge as fb

    state = SimpleNamespace(sessions={}, active_session_id=None)
    fallback: list = []

    async def _local(_state, sid, _msg):
        fallback.append(sid)
        return "queued to inbox sidecar"

    monkeypatch.setattr(fb, "_is_gateway_session_id", lambda _sid: False)
    monkeypatch.setattr(fb, "_resolve_archived_subagent", lambda _sid: None)
    monkeypatch.setattr(fb, "deliver_talk_message_locally", _local)
    monkeypatch.setattr(
        "bridge.transcript_persistence.load_transcript", lambda _sid: None
    )

    from bridge.inbox import InboxMessage

    status = await fb.deliver_artifact_note(
        state,
        "ghost-session",
        InboxMessage(
            id="n", from_session="operator", from_label="Operator",
            from_role="operator", content="body",
        ),
    )
    assert fallback == ["ghost-session"]
    assert "sidecar" in status


@pytest.mark.asyncio
async def test_gateway_session_uses_the_shared_talk_path(monkeypatch) -> None:
    # Gateway ids and archived sub-agents already have bespoke handling inside
    # deliver_talk_message_locally; don't duplicate it.
    from bridge import freyja_bridge as fb

    seen: list = []

    async def _local(_state, sid, _msg):
        seen.append(sid)
        return "delivered"

    monkeypatch.setattr(fb, "_is_gateway_session_id", lambda sid: sid.startswith("freyja:"))
    monkeypatch.setattr(fb, "_resolve_archived_subagent", lambda _sid: None)
    monkeypatch.setattr(fb, "deliver_talk_message_locally", _local)

    from bridge.inbox import InboxMessage

    await fb.deliver_artifact_note(
        SimpleNamespace(sessions={}, active_session_id=None),
        "freyja:slack:T1:C1:123",
        InboxMessage(
            id="n", from_session="operator", from_label="Operator",
            from_role="operator", content="body",
        ),
    )
    assert seen == ["freyja:slack:T1:C1:123"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"sessionId": ""},
        {"body": "   "},
        {"artifactPath": ""},
    ],
)
async def test_malformed_commands_are_dropped_quietly(monkeypatch, bad: dict) -> None:
    from bridge import freyja_bridge as fb

    called: list = []

    async def _deliver(*_a, **_k):
        called.append(1)
        return "delivered"

    monkeypatch.setattr(fb, "deliver_artifact_note", _deliver)
    monkeypatch.setattr(fb, "emit", lambda _p: None)

    state = SimpleNamespace(sessions={}, active_session_id=None)
    await fb._handle_artifact_note_command(state, _cmd(**bad))
    assert called == []


@pytest.mark.asyncio
async def test_a_delivery_failure_never_raises_into_the_command_loop(monkeypatch) -> None:
    from bridge import freyja_bridge as fb

    async def _boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    emitted: list = []
    monkeypatch.setattr(fb, "deliver_artifact_note", _boom)
    monkeypatch.setattr(fb, "emit", lambda payload: emitted.append(payload))

    state = SimpleNamespace(sessions={}, active_session_id=None)
    await fb._handle_artifact_note_command(state, _cmd())

    events = [e for e in emitted if e.get("subtype") == "artifact_note"]
    assert len(events) == 1
    assert "RuntimeError" in events[0]["details"]["status"]


@pytest.mark.asyncio
async def test_a_note_without_an_anchor_is_a_whole_file_comment(monkeypatch) -> None:
    from bridge import freyja_bridge as fb

    wake_log: list = []
    sess = _live_session("session-alpha", wake_log)
    state = SimpleNamespace(sessions={"session-alpha": sess}, active_session_id=None)
    monkeypatch.setattr(fb, "_install_talk_wake_hook", lambda *_a, **_k: None)
    monkeypatch.setattr(fb, "emit", lambda _p: None)

    await fb._handle_artifact_note_command(state, _cmd(anchor=None))

    content = sess.inbox.pushed[0].content
    assert "They highlighted" not in content
    assert "This paragraph is wrong." in content
