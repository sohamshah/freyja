"""The request prefix stays fixed, so the provider's prompt cache keeps hitting.

OpenAI's GPT-5.6+ models resume a cached prefix only at the end of an earlier
request (or the end of the system prompt + tools). A change anywhere earlier
makes the next call re-read everything after it at the cache-write rate, 1.25x
input. Each test below pins one source of such changes, found in a GPT-6 Astra
Ultrafast session where re-reads were 42% of a $516 bill.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.types import ImageBlock, Message, TextBlock, ToolCall


# ─── the meter: OpenAI cache writes ───────────────────────────────────


def _openai_usage(*, input_tokens: int, cached: int, written: int | None, output: int = 10):
    details = {"cached_tokens": cached}
    if written is not None:
        details["cache_write_tokens"] = written
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output,
        input_tokens_details=SimpleNamespace(**details),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def test_openai_cache_writes_are_split_out_of_input_and_priced_as_writes() -> None:
    from engine.openai_provider import _api_usage_from_response_usage
    from engine.providers import compute_cost

    usage = _api_usage_from_response_usage(
        _openai_usage(input_tokens=130_000, cached=30_000, written=99_997)
    )
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (3, 30_000, 99_997)

    cost = compute_cost(
        "gpt-6-astra-ultrafast",
        input_tokens=usage.input_tokens,
        output_tokens=0,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    # $60 input, $6 cached, $75 cache write per MTok.
    assert cost == pytest.approx((3 * 60 + 30_000 * 6 + 99_997 * 75) / 1e6)


def test_models_without_cache_writes_keep_their_input() -> None:
    # gpt-5.5 reports cache_write_tokens: 0; older SDKs omit the field.
    from engine.openai_provider import _api_usage_from_response_usage

    for written in (0, None):
        usage = _api_usage_from_response_usage(
            _openai_usage(input_tokens=5_000, cached=1_000, written=written)
        )
        assert (usage.input_tokens, usage.cache_write_tokens) == (4_000, 0)


def test_sub_agent_records_report_cache_reads_and_writes() -> None:
    # Once the cache is warm, input_tokens is a few tokens per call; the
    # receipt adds the cache fields to show what the sub-agent read.
    from bridge.tools.sub_agent_registry import SubAgentRecord
    from bridge.tools.sub_agent_tool import _record_to_dict

    record = SubAgentRecord(id="sub_1", label="scan", task="scan", mode="background")
    record.input_tokens = 6
    record.cache_read_tokens = 15_481
    record.cache_write_tokens = 15_723
    fields = _record_to_dict(record)
    assert (fields["tokensIn"], fields["tokensCacheRead"], fields["tokensCacheWrite"]) == (
        6,
        15_481,
        15_723,
    )


# ─── screenshot pruning ───────────────────────────────────────────────


def _screenshot_transcript(n: int):
    from engine.session import TranscriptManager

    transcript = TranscriptManager()
    for i in range(n):
        call = ToolCall(id=f"call_{i}", name="screenshot", arguments={})
        transcript.append_message(Message(role="assistant", content="", tool_calls=[call]))
        transcript.append_message(
            Message(
                role="tool_result",
                tool_call_id=call.id,
                content=[
                    TextBlock(text=f"frame {i}"),
                    ImageBlock(source_type="base64", media_type="image/png", data="iVBORw0KGgo="),
                ],
            )
        )
    return transcript


def _images(transcript) -> list[int]:
    return [
        i
        for i, entry in enumerate(transcript.entries)
        if entry.message is not None
        and isinstance(entry.message.content, list)
        and any(isinstance(b, ImageBlock) for b in entry.message.content)
    ]


def test_screenshots_are_dropped_in_batches_not_one_per_step() -> None:
    transcript = _screenshot_transcript(12)
    assert not transcript.prune_old_tool_result_images(keep_recent=4, batch=8).changed
    assert len(_images(transcript)) == 12

    # The 13th screenshot crosses keep + batch: back to the latest 4 in one pass.
    more = _screenshot_transcript(13)
    stats = more.prune_old_tool_result_images(keep_recent=4, batch=8)
    assert stats.omitted_images == 9
    assert len(_images(more)) == 4

    # One more screenshot changes nothing until the window refills.
    call = ToolCall(id="call_x", name="screenshot", arguments={})
    more.append_message(Message(role="assistant", content="", tool_calls=[call]))
    more.append_message(
        Message(
            role="tool_result",
            tool_call_id=call.id,
            content=[ImageBlock(source_type="base64", media_type="image/png", data="iVBORw0KGgo=")],
        )
    )
    assert not more.prune_old_tool_result_images(keep_recent=4, batch=8).changed


def test_the_hard_image_limit_still_applies_on_every_call() -> None:
    transcript = _screenshot_transcript(10)
    stats = transcript.prune_old_tool_result_images(keep_recent=4, hard_limit=6, batch=8)
    assert stats.changed
    assert len(_images(transcript)) == 4


# ─── memories and skills: never in the system prompt ───────────────────


def _knowledge_home(tmp_path: Path, monkeypatch, *, memories: list[dict], skills: dict[str, str]):
    home = tmp_path / "home"
    (home / ".freyja" / "knowledge").mkdir(parents=True)
    (home / ".freyja" / "knowledge" / "memory.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in memories)
    )
    workspace = tmp_path / "ws"
    for name, description in skills.items():
        skill_dir = workspace / ".freyja" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\nBody of {name}.\n"
        )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    from bridge.knowledge.memory_store import MemoryStore
    from bridge.knowledge.skill_store import SkillStore

    return MemoryStore(workspace), SkillStore(workspace), home


def _memory(item_id: str, text: str, updated_at: int = 1) -> dict:
    return {"id": item_id, "scope": "user", "kind": "preference", "text": text, "updated_at": updated_at}


def test_knowledge_reminder_lists_only_items_not_shown_yet(tmp_path, monkeypatch) -> None:
    from bridge.knowledge.prompt import build_knowledge_reminder

    memory_store, skill_store, home = _knowledge_home(
        tmp_path,
        monkeypatch,
        memories=[_memory("m1", "deploys go through the release train")],
        skills={"release-train": "how deploys go out"},
    )
    reminder, keys = build_knowledge_reminder(
        memory_store=memory_store, skill_store=skill_store, query="deploys"
    )
    assert reminder.startswith("<system-reminder>") and reminder.endswith("</system-reminder>")
    assert "release train" in reminder and "release-train" in reminder
    assert len(keys) == 2

    again, again_keys = build_knowledge_reminder(
        memory_store=memory_store, skill_store=skill_store, query="deploys", shown=set(keys)
    )
    assert (again, again_keys) == ("", [])

    # An edited memory is a new listing.
    (home / ".freyja" / "knowledge" / "memory.jsonl").write_text(
        json.dumps(_memory("m1", "deploys go through the release train on Tuesdays", updated_at=2)) + "\n"
    )
    memory_store.refresh()
    edited, edited_keys = build_knowledge_reminder(
        memory_store=memory_store, skill_store=skill_store, query="deploys", shown=set(keys)
    )
    assert "Tuesdays" in edited
    assert "release-train" not in edited
    assert len(edited_keys) == 1


def _bridge_session(tmp_path, monkeypatch, memory_store, skill_store):
    import bridge.freyja_bridge as fb
    from engine.session import Session

    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    sess = fb._BridgeSession.__new__(fb._BridgeSession)
    sess.id = "session-test"
    sess.memory_store = memory_store
    sess.skill_store = skill_store
    sess._base_system_prompt = "BASE PROMPT"
    sess._system_prompt = ""
    sess._knowledge_shown = set()
    sess._knowledge_epoch = None
    sess._pending_knowledge = None
    sess.session = Session.create(system_prompt="", session_id="session-test")
    return sess, events


def test_the_system_prompt_is_the_same_whatever_the_turn_retrieves(tmp_path, monkeypatch) -> None:
    memory_store, skill_store, _home = _knowledge_home(
        tmp_path,
        monkeypatch,
        memories=[_memory("m1", "deploys go through the release train"), _memory("m2", "invoices are due monthly")],
        skills={"release-train": "how deploys go out", "invoicing": "how invoices are sent"},
    )
    sess, _events = _bridge_session(tmp_path, monkeypatch, memory_store, skill_store)

    sess._refresh_knowledge_context("how do deploys work")
    first_prompt = sess.session.system_prompt
    first = "\n".join(sess._build_extra_system_reminders())
    assert "BASE PROMPT" in first_prompt
    assert "Relevant Memory" not in first_prompt and "Available Skills" not in first_prompt
    assert "release train" in first

    sess.session.add_user_message("how do deploys work")
    sess._refresh_knowledge_context("deploys and invoices due")
    assert sess.session.system_prompt == first_prompt
    second = "\n".join(sess._build_extra_system_reminders())
    assert "invoices are due monthly" in second
    assert "release train" not in second  # already listed in the transcript

    # Delivered once: the next request in the same turn carries no copy.
    assert "Relevant Memory" not in "\n".join(sess._build_extra_system_reminders())


def test_compaction_lets_earlier_knowledge_be_listed_again(tmp_path, monkeypatch) -> None:
    memory_store, skill_store, _home = _knowledge_home(
        tmp_path,
        monkeypatch,
        memories=[_memory("m1", "deploys go through the release train")],
        skills={},
    )
    sess, _events = _bridge_session(tmp_path, monkeypatch, memory_store, skill_store)
    sess._refresh_knowledge_context("deploys")
    assert "release train" in "\n".join(sess._build_extra_system_reminders())
    sess.session.add_user_message("deploys")

    sess._refresh_knowledge_context("deploys")
    assert "release train" not in "\n".join(sess._build_extra_system_reminders())

    # The summary replaces the message that listed it.
    sess.session.transcript.append_compaction("summary of earlier work", first_kept_id="", tokens_before=1_000)
    sess._refresh_knowledge_context("deploys")
    assert "release train" in "\n".join(sess._build_extra_system_reminders())


def test_compaction_summaries_leave_out_runtime_reminders() -> None:
    from engine.compaction import SummaryCompaction

    strategy = SummaryCompaction.__new__(SummaryCompaction)
    text = strategy._format_conversation(
        [
            Message(
                role="user",
                content=[
                    TextBlock(text="ship the fix"),
                    TextBlock(text="\n\n<system-reminder>\nCurrent time: 10:00\n</system-reminder>"),
                ],
            )
        ]
    )
    assert "ship the fix" in text
    assert "Current time" not in text


# ─── tool_search: the tool list never changes mid-session ───────────────


class _EchoTool:
    def __init__(self, name: str, tier: str = "warm", permission_level: str | None = None) -> None:
        from bridge.tools.base import ToolDefinition

        self._definition = ToolDefinition(
            name=name,
            summary=f"{name} summary",
            description=f"{name} description",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            tier=tier,
        )
        self.calls: list[dict] = []
        if permission_level is not None:
            self.requires_permission = True
            self._level = permission_level

    @property
    def definition(self):
        return self._definition

    async def execute(self, call_id: str, arguments: dict):
        from bridge.tools.base import ToolResult

        self.calls.append(arguments)
        return ToolResult(call_id=call_id, content=f"echo {arguments.get('text')}")


class _GatedEchoTool(_EchoTool):
    async def permission_prompt(self, arguments: dict):
        return SimpleNamespace(prompt=f"run {self.definition.name}", level=self._level, details={})


def _registry(*tools, permission_handler=None):
    from bridge.tools.base import ToolRegistry
    from bridge.tools.tool_search_tool import CallToolTool, ToolSearchTool

    registry = ToolRegistry(permission_handler=permission_handler)
    for tool in tools:
        registry.register(tool)
    registry.register(ToolSearchTool(registry))
    registry.register(CallToolTool(registry))
    return registry


def test_tool_search_hands_back_the_schema_without_changing_the_tool_list() -> None:
    echo = _EchoTool("mcp__demo__echo")
    registry = _registry(echo)
    before = [d.name for d in registry.list_definitions()]
    assert "mcp__demo__echo" not in before

    found = asyncio.run(registry.get("tool_search").execute("s1", {"tool_name": "mcp__demo__echo"}))
    assert not found.is_error
    assert '"text"' in found.content and "call_tool" in found.content
    assert [d.name for d in registry.list_definitions()] == before

    ran = asyncio.run(
        registry.get("call_tool").execute(
            "c1", {"tool_name": "mcp__demo__echo", "arguments": {"text": "hi"}}
        )
    )
    assert (ran.call_id, ran.content, ran.is_error) == ("c1", "echo hi", False)
    assert echo.calls == [{"text": "hi"}]


def test_call_tool_takes_arguments_as_a_json_string_and_rejects_bad_ones() -> None:
    echo = _EchoTool("mcp__demo__echo")
    registry = _registry(echo)
    call_tool = registry.get("call_tool")

    ok = asyncio.run(call_tool.execute("c1", {"tool_name": "mcp__demo__echo", "arguments": '{"text": "x"}'}))
    assert ok.content == "echo x"
    bad = asyncio.run(call_tool.execute("c2", {"tool_name": "mcp__demo__echo", "arguments": "{nope"}))
    assert bad.is_error
    missing = asyncio.run(call_tool.execute("c3", {"tool_name": "mcp__demo__ech", "arguments": {}}))
    assert missing.is_error and "mcp__demo__echo" in missing.content
    nested = asyncio.run(call_tool.execute("c4", {"tool_name": "call_tool", "arguments": {}}))
    assert nested.is_error
    assert echo.calls == [{"text": "x"}]


def test_call_tool_keeps_the_tools_permission_prompt() -> None:
    asked: list[str] = []

    class _Handler:
        def request_permission(self, *, action, level, details):
            asked.append(action)
            return SimpleNamespace(approved=False)

    gated = _GatedEchoTool("mcp__demo__send", permission_level="write")
    registry = _registry(gated, permission_handler=_Handler())
    denied = asyncio.run(
        registry.get("call_tool").execute("c1", {"tool_name": "mcp__demo__send", "arguments": {"text": "x"}})
    )
    assert asked == ["run mcp__demo__send"]
    assert "denied" in denied.content
    assert gated.calls == []


# ─── sub-agents: standard tier, except forks ──────────────────────────


@pytest.mark.parametrize(
    ("model", "standard"),
    [
        ("gpt-6-astra-ultrafast", "gpt-6-astra"),
        ("gpt-6.1-sol-ultrafast", "gpt-6.1-sol"),
        ("claude-opus-5-5-fast", "claude-opus-5-5"),
        ("gpt-6-astra", "gpt-6-astra"),
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        # A separate Fireworks deployment, not a speed tier of kimi-k3.
        ("kimi-k3-fast", "kimi-k3-fast"),
    ],
)
def test_standard_tier_model_id(model: str, standard: str) -> None:
    from engine.providers import standard_tier_model_id

    assert standard_tier_model_id(model) == standard


def test_fresh_sub_agents_use_the_standard_tier_and_forks_keep_the_exact_model(monkeypatch) -> None:
    import bridge.tools.sub_agent_tool as sat
    from bridge.tools.agent_types import ModelResolution
    from bridge.tools.base import ToolRegistry
    from bridge.tools.sub_agent_registry import SubAgentRegistry

    resolved_from: list[str] = []

    def _resolve(agent_type, parent_model):
        resolved_from.append(parent_model)
        return ModelResolution(model=parent_model, policy="test", candidates=(parent_model,))

    async def _run_child(self, record):
        self._spec.registry.mark_done(record.id, "ok", sat.SubAgentState.DONE)
        return "ok"

    monkeypatch.setattr(sat, "resolve_model_choice", _resolve)
    monkeypatch.setattr(sat.SubAgentTool, "_run_child", _run_child)

    async def _emit(_event):
        return None

    spec = sat.SubAgentSpec(
        parent_workspace="/tmp",
        parent_model="gpt-6-astra-ultrafast",
        build_provider=lambda *a, **k: None,
        parent_registry=ToolRegistry(),
        registry=SubAgentRegistry(),
        emit_event=_emit,
        parent_session_id="parent-1",
    )
    assert spec.standard_tier_model == "gpt-6-astra"
    tool = sat.SubAgentTool(spec)

    async def _spawn_both():
        await tool.execute("call-1", {"label": "scan", "task": "scan the repo"})
        await tool.spawn_fork(
            agent_type_name="general",
            label="fork",
            injected_message="review this",
            transcript_snapshot={},
            system_prompt="BASE PROMPT",
            source_session_id="parent-1",
        )
        await asyncio.sleep(0.05)

    asyncio.run(_spawn_both())
    assert resolved_from[:2] == ["gpt-6-astra", "gpt-6-astra-ultrafast"]
