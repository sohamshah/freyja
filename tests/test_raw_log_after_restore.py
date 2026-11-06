"""A restored session keeps writing its raw message log.

The bridge builds each session with ``on_message_appended``, which appends
every message to ``raw_messages.jsonl``: the record exports and ``recall``
read, which compaction and halving never touch. Restoring a saved transcript
(an app or gateway restart, a reload) swapped in a new TranscriptManager
without that callback, so nothing appended after a restore reached the log.
On 2026-11-06, 53 of the 77 sessions active in the last three weeks had
messages that are in their live transcript but not in their raw log.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from engine.session import Session


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    # A session's project directory is read from HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


def test_a_restored_transcript_still_reports_each_message_it_appends():
    saved = Session.create(system_prompt="t")
    saved.add_user_message("before the restart")
    appended: list[str] = []
    session = Session.create(system_prompt="t", on_message_appended=lambda m: appended.append(m.content))

    session.restore_transcript(saved.serialize_transcript())
    session.add_user_message("after the restart")

    assert appended == ["after the restart"]


async def test_a_bridge_session_logs_the_messages_after_its_restore(tmp_path):
    import bridge.transcript_persistence as tp
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    saved = Session.create(system_prompt="t")
    saved.add_user_message("before the restart")
    tp.save_transcript("test-raw-log", {**saved.serialize_transcript(), "metadata": {}})

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-raw-log", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)

    async def _initialize():
        # What initialize gives every native session.
        sess.session = Session.create(system_prompt="t", session_id=sess.id,
                                      on_message_appended=sess._append_raw_message_log)
        sess.provider = SimpleNamespace(name="anthropic", model_id="claude-sonnet-5-5",
                                        context_window=200_000)
        sess._bridge_loop = asyncio.get_running_loop()

    sess.initialize = _initialize
    sess._restore_goal_state = lambda: False
    sess._restore_inbox = lambda: None

    with patch("bridge.freyja_bridge.emit"):
        assert await sess.try_restore_transcript()
    sess.session.add_user_message("after the restart")

    log = tmp_path / ".freyja" / "projects" / "test-raw-log" / "raw_messages.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["message"]["content"] for r in rows] == ["after the restart"]
    assert rows[0]["session_id"] == "test-raw-log"
