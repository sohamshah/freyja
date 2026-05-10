import pytest

from bridge.tools.agent_types import AgentType
from bridge.tools.base import ToolRegistry
from bridge.tools.coordination import STRATEGY_KANBAN
from bridge.tools.sub_agent_registry import SubAgentRegistry
from bridge.tools.sub_agent_tool import SubAgentSpec, SubAgentTool
from engine.session import Session


def test_subagent_terminal_transcript_is_persisted_for_followup(
    tmp_path,
    monkeypatch,
) -> None:
    from bridge import transcript_persistence

    monkeypatch.setattr(transcript_persistence, "SESSIONS_DIR", tmp_path)

    registry = SubAgentRegistry()
    record = registry.register(
        id="sub_test_1",
        label="Research child",
        task="Find the thing",
        mode="foreground",
    )
    record.artifact_path = str(tmp_path / "artifact.md")
    record.created_files = [record.artifact_path]

    spec = SubAgentSpec(
        parent_workspace=str(tmp_path),
        parent_model="claude-sonnet-4-6",
        build_provider=lambda *_args, **_kwargs: None,
        parent_registry=ToolRegistry(),
        registry=registry,
        emit_event=lambda _event: None,
        parent_session_id="session-parent",
        coordination_strategy=STRATEGY_KANBAN,
    )
    tool = SubAgentTool(spec)
    agent_type = AgentType(
        name="explore",
        description="Explore",
        usage_hint="Explore",
        model="parent",
        thinking_effort="medium",
    )
    session = Session.create(
        system_prompt="child prompt",
        session_id=record.id,
        metadata={"existing": "kept"},
    )
    session.add_user_message("Original child task")
    session.add_assistant_message("Original child answer")

    tool._persist_child_transcript(  # noqa: SLF001
        record,
        session,
        child_model="claude-sonnet-4-6",
        agent_type=agent_type,
        state="done",
    )

    restored = transcript_persistence.load_transcript(record.id)
    assert restored is not None
    assert restored["session_id"] == record.id
    assert len(restored["transcript"]["entries"]) == 2
    metadata = restored["metadata"]
    assert metadata["existing"] == "kept"
    assert metadata["model_id"] == "claude-sonnet-4-6"
    assert metadata["reasoning_level"] == "medium"
    assert metadata["parent_session_id"] == "session-parent"
    assert metadata["project_session_id"] == "session-parent"
    assert metadata["subagent_id"] == record.id
    assert metadata["subagent_state"] == "done"
    assert metadata["coordination_strategy"] == STRATEGY_KANBAN
    assert metadata["created_files"] == [record.artifact_path]


@pytest.mark.asyncio
async def test_existing_empty_runtime_reloads_persisted_child_transcript(
    tmp_path,
    monkeypatch,
) -> None:
    from bridge import transcript_persistence
    from bridge.freyja_bridge import _BridgeSession

    monkeypatch.setattr(transcript_persistence, "SESSIONS_DIR", tmp_path)

    persisted = Session.create(system_prompt="persisted", session_id="sub_existing")
    persisted.add_user_message("context that should survive")
    transcript_persistence.save_transcript(
        "sub_existing",
        persisted.serialize_transcript(),
    )

    bridge_session = _BridgeSession(
        "sub_existing",
        workspace=str(tmp_path),
        model_id="claude-sonnet-4-6",
        reasoning_level=None,
        coordination_strategy=None,
        state=type(
            "State",
            (),
            {"permission_tier": "low", "computer_enabled": False},
        )(),
    )
    bridge_session.session = Session.create(
        system_prompt="empty runtime",
        session_id="sub_existing",
    )
    bridge_session.runner = object()
    called = {"restore": False}

    async def fake_restore(self) -> bool:
        called["restore"] = True
        return True

    monkeypatch.setattr(_BridgeSession, "try_restore_transcript", fake_restore)

    restored = await bridge_session._restore_persisted_transcript_if_empty()  # noqa: SLF001

    assert restored is True
    assert called["restore"] is True


@pytest.mark.asyncio
async def test_legacy_context_summary_injects_before_followup(
    tmp_path,
    monkeypatch,
) -> None:
    from bridge import transcript_persistence
    from bridge.freyja_bridge import _BridgeSession, _inject_legacy_context_summary

    monkeypatch.setattr(transcript_persistence, "SESSIONS_DIR", tmp_path)

    bridge_session = _BridgeSession(
        "sub_legacy",
        workspace=str(tmp_path),
        model_id="claude-sonnet-4-6",
        reasoning_level=None,
        coordination_strategy=None,
        state=type(
            "State",
            (),
            {"permission_tier": "low", "computer_enabled": False},
        )(),
    )
    bridge_session.session = Session.create(
        system_prompt="empty runtime",
        session_id="sub_legacy",
    )
    bridge_session.runner = object()

    injected = await _inject_legacy_context_summary(
        bridge_session,
        "[ASSISTANT] I was verifying the generated file.",
    )

    assert injected is True
    assert len(bridge_session.session.transcript) == 2
    saved = transcript_persistence.load_transcript("sub_legacy")
    assert saved is not None
    assert len(saved["transcript"]["entries"]) == 2

    second = await _inject_legacy_context_summary(bridge_session, "new summary")
    assert second is False
    assert len(bridge_session.session.transcript) == 2
