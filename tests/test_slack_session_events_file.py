"""Only the gateway writes a Slack session's events file.

emit() appends every event with a session id to that session's
``.events.jsonl``, which the app replays to show a Slack session's activity.
A copy of a Slack session that a command builds in the app still emits as it
loads, and those lines went into the gateway's file: on Nov 6 the app wrote
skill_updated, session_switched and transcript_restored lines into two
Slack threads' files. The app sees its own events on stdout.
"""

from __future__ import annotations

import json

import pytest

import bridge.freyja_bridge as fb

SLACK = "freyja:slack:T1:channel:C1:1.0"


@pytest.fixture
def mirror(monkeypatch, tmp_path):
    monkeypatch.setattr(fb, "_DEBUG_LOG_ENABLED", False)
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_SESSION_EVENT_FILES", fb._OrderedDict())
    yield
    for sid in list(fb._SESSION_EVENT_FILES):
        fb._close_session_event_file(sid)


def _emit_load_events(sid: str) -> None:
    fb.emit({"type": "skill_updated", "sessionId": sid, "skill": {"name": "s"}})
    fb.emit({"type": "system_event", "sessionId": sid, "subtype": "transcript_restored", "message": "m"})


def _written(sid: str) -> list[str]:
    path = fb._session_event_path(sid)
    if not path.exists():
        return []
    return [json.loads(line)["type"] for line in path.read_text().splitlines()]


def test_the_app_writes_nothing_to_a_slack_sessions_events_file(mirror, monkeypatch, capsys):
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)

    _emit_load_events(SLACK)

    assert _written(SLACK) == []
    # The app's own view still gets them.
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(e["type"], e["sessionId"]) for e in out] == [("skill_updated", SLACK), ("system_event", SLACK)]


def test_the_gateway_writes_its_slack_sessions_events_file(mirror, monkeypatch):
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)

    _emit_load_events(SLACK)

    assert _written(SLACK) == ["skill_updated", "system_event"]


def test_the_app_still_writes_its_own_sessions_events_file(mirror, monkeypatch):
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)

    _emit_load_events("session-abc")

    assert _written("session-abc") == ["skill_updated", "system_event"]


def test_the_app_writes_nothing_to_a_slack_sub_agents_events_file(mirror, monkeypatch):
    # The gateway runs a Slack thread's sub-agents and writes their files;
    # the sub-agent's saved sidecar names its parent.
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    sub = "sub_19f0slackev_1"
    tp.save_subagent_state(sub, {"sessionId": sub, "parentSessionId": SLACK})

    _emit_load_events(sub)

    assert _written(sub) == []
