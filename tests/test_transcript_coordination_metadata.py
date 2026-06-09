"""A session's coordination strategy must persist into the transcript metadata,
so the desktop shows the real mode (goal/kanban) instead of defaulting to 'bus'.

This was the bug behind a Slack session started with `--mode=goal` showing a
'bus' badge: `_save_transcript` stamped model_id + reasoning_level into the
metadata but not coordination_strategy, so the desktop's transcript-
reconstruction path (persistence.ts) hit its `|| 'bus'` fallback.
"""

from __future__ import annotations

from types import SimpleNamespace

import bridge.transcript_persistence as tp
from bridge.freyja_bridge import _BridgeSession


def test_save_transcript_stamps_coordination_strategy(monkeypatch):
    captured: dict = {}
    # The import is `from bridge.transcript_persistence import save_transcript`
    # inside the method, so patching the module attribute intercepts it.
    monkeypatch.setattr(tp, "save_transcript", lambda sid, data: captured.update(data))

    stub = SimpleNamespace(
        session=SimpleNamespace(serialize_transcript=lambda: {"version": 1, "metadata": {}}),
        model_id="claude-opus-4-8",
        reasoning_level="high",
        coordination_strategy="goal",
        id="freyja-slack-T0-channel-C0-1781025596.714409",
        _save_goal_state=lambda: None,
    )

    # Call the unbound method against the stub — exercises exactly the stamping
    # logic without standing up a full _BridgeSession.
    _BridgeSession._save_transcript(stub)

    md = captured.get("metadata", {})
    assert md.get("coordination_strategy") == "goal"  # the fix
    # The pre-existing stamps still land (no regression).
    assert md.get("model_id") == "claude-opus-4-8"
    assert md.get("reasoning_level") == "high"


def test_save_transcript_preexisting_metadata_preserved(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(tp, "save_transcript", lambda sid, data: captured.update(data))

    stub = SimpleNamespace(
        session=SimpleNamespace(
            serialize_transcript=lambda: {
                "version": 1,
                "metadata": {"last_inbound_platform_ts": "1781039968.261729"},
            }
        ),
        model_id="claude-sonnet-4-6",
        reasoning_level="auto",
        coordination_strategy="kanban",
        id="sess-2",
        _save_goal_state=lambda: None,
    )

    _BridgeSession._save_transcript(stub)

    md = captured.get("metadata", {})
    assert md.get("coordination_strategy") == "kanban"
    # Existing metadata keys aren't clobbered.
    assert md.get("last_inbound_platform_ts") == "1781039968.261729"
