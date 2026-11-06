"""A message holding a lone surrogate still reaches the raw message log.

Text cut in JavaScript can end in half of a surrogate pair (the renderer's
summary of a session started before transcript persistence did, on
2026-09-03). JSON carries it as ``\\ud835``, and Python reads it back as a
lone surrogate, which UTF-8 cannot encode. The log writers dumped with
``ensure_ascii=False`` into a UTF-8 file, so the write raised, the bridge
logged it at debug, and the message never reached the log: in
sub_19d903fa801_5 the user turn is missing and the reply after it is there.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from engine.types import Message

TEXT = "a summary cut in the middle of \ud835"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


def _rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_raw_log_keeps_a_message_with_a_lone_surrogate(tmp_path):
    from bridge.transcript_persistence import append_subagent_raw_message, raw_messages_path

    append_subagent_raw_message("sub_s_1", Message(role="user", content=TEXT))
    append_subagent_raw_message("sub_s_1", Message(role="assistant", content="é and 🙂"))

    rows = _rows(raw_messages_path("sub_s_1"))
    assert [r["message"]["content"] for r in rows] == [TEXT, "é and 🙂"]
    # Text that UTF-8 can hold is written as it is.
    assert "é and 🙂" in raw_messages_path("sub_s_1").read_text(encoding="utf-8")


def test_the_compaction_logs_keep_a_summary_with_a_lone_surrogate(tmp_path):
    from bridge.freyja_bridge import _BridgeSession, _BridgeState
    from bridge.transcript_persistence import append_subagent_compaction, compactions_path

    append_subagent_compaction("sub_s_2", model="m", subtype="compaction_complete",
                               details={"summary_text": TEXT}, default_trigger="runtime")
    assert _rows(compactions_path("sub_s_2"))[0]["summary_text"] == TEXT

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("root-s", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    sess._append_compaction_log({"ts": 1.0, "subtype": "compaction_complete", "summary_text": TEXT})
    log = tmp_path / ".freyja" / "projects" / "root-s" / "compactions.jsonl"
    assert _rows(log)[0]["summary_text"] == TEXT
