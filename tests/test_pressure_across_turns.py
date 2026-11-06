"""The context's measured size carries from one turn to the next.

run() zeroed the usage accumulator at every turn start, last-call values
included, and every pressure reader reads that accumulator. So the first call
of each turn saw 0%: it went out without the pressure note, a reply past 55%
raised a Channel 3 advisory claiming a crossing "from band 0", the bridge's
pressure telemetry logged a 0% row, and the pre-request check measured the
context by the tokenizer alone, though context_outlook (what the Slack notice
reads) predicted with the last call's size.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from engine.compaction import CompactionResult
from engine.providers import APIUsage, ProviderResponse, ToolCallResponse
from engine.runner import AsyncAgentRunner
from engine.session import Session
from engine.tools import ToolDefinition, ToolRegistry, ToolResult
from engine.types import AgentConfig

# 100k window, 50k reserved for the reply: 50k usable for input.
WINDOW = 100_000
RESERVED = 50_000


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


def _end(input_tokens: int) -> ProviderResponse:
    return ProviderResponse(content="done", stop_reason="end_turn", usage=APIUsage(input_tokens=input_tokens))


def _tools(call_id: str, input_tokens: int) -> ProviderResponse:
    return ProviderResponse(
        content="", stop_reason="tool_use", usage=APIUsage(input_tokens=input_tokens),
        tool_calls=[ToolCallResponse(id=call_id, name="read", arguments={})],
    )


class _Provider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = WINDOW

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests: list[list] = []

    async def stream_to_response(self, *, messages, tools=None, system_prompt=None,
                                 max_tokens=None, thinking=None, on_event=None):
        self.requests.append(list(messages))
        return self.steps.pop(0)


class _ReadTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="read", description="read a file", summary="read")

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        return ToolResult(call_id=call_id, content="contents", is_error=False)


class _Compactor:
    prompt_source = None

    def __init__(self):
        self.calls = 0

    def compact(self, transcript, provider, **_kw):
        self.calls += 1
        return CompactionResult(success=False, summary=None, tokens_before=0, tokens_after=0, error="stub")


def _runner(steps, **kw) -> tuple[AsyncAgentRunner, _Provider]:
    provider = _Provider(steps)
    registry = ToolRegistry()
    registry.register(_ReadTool())
    runner = AsyncAgentRunner(provider, config=AgentConfig(max_tokens_per_turn=RESERVED),
                              tool_registry=registry, **kw)
    return runner, provider


async def test_a_turn_that_starts_past_55_percent_raises_no_crossing():
    # The first turn ends at 60% (strong band); the second turn's first call
    # comes back at 62%. Nothing crossed.
    runner, _ = _runner([_end(30_000), _tools("c1", 31_000), _end(31_500)])
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")
    runner.consume_channel3_advisory()

    await runner.run(session, "second task")

    assert runner.consume_channel3_advisory() == ""


async def test_the_turns_first_request_carries_the_pressure_note():
    runner, provider = _runner([_end(30_000), _end(31_000)])
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    await runner.run(session, "second task")

    turn_message = provider.requests[1][-1]
    assert "Context window is at 60-69%" in turn_message.get_text()


async def test_each_call_is_reported_with_its_own_measure():
    # The bridge's pressure telemetry reads the accumulator from on_llm_call,
    # which fired before the accumulator took the call's usage: every
    # pressure_signal row showed the call before it.
    seen: list[int] = []
    runner, _ = _runner([_tools("c1", 10_000), _end(30_000), _end(31_000)],
                        on_llm_call=lambda _p: seen.append(runner.usage.effective_context_tokens()))
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    await runner.run(session, "second task")

    assert seen == [10_000, 30_000, 31_000]


async def test_the_outlook_that_says_compact_holds_at_the_turns_first_request():
    # The first turn ends at 76%: the next request compacts first, as
    # context_outlook (the Slack notice) predicts.
    compactor = _Compactor()
    runner, _ = _runner([_end(38_000), _end(39_000)], compaction_strategy=compactor)
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")
    assert runner.context_outlook(session).will_compact

    await runner.run(session, "second task")

    assert compactor.calls == 1


async def test_a_transcript_cut_since_the_last_call_is_measured_afresh():
    # An edit or a delete between turns cuts the transcript: the last call's
    # 76% no longer describes it, and nothing is compacted.
    compactor = _Compactor()
    runner, _ = _runner([_end(38_000), _end(1_000)], compaction_strategy=compactor)
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")
    session.transcript._entries = []  # noqa: SLF001

    await runner.run(session, "a different task")

    assert compactor.calls == 0


async def test_the_outlook_after_a_cut_reads_what_the_next_turn_will():
    # The Slack notice reads context_outlook before the turn. After a cut it
    # kept the last call's 76% and announced a compaction the turn never ran.
    runner, _ = _runner([_end(38_000)])
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")
    assert runner.context_outlook(session).will_compact
    session.transcript._entries = []  # noqa: SLF001

    outlook = runner.context_outlook(session)

    assert not outlook.will_compact
    assert outlook.used_tokens == session.estimate_tokens()


async def test_the_turn_after_a_manual_compaction_starts_from_the_size_it_left(monkeypatch, tmp_path):
    # A manual compaction runs between turns and sets the measure to the
    # size it left. The next turn saw a rewritten transcript and zeroed it,
    # so its first call read 0%, with no pressure note.
    import bridge.freyja_bridge as fb
    from bridge.freyja_bridge import _BridgeSession, _BridgeState
    from engine.compaction import SummaryCompaction

    monkeypatch.setenv("HOME", str(tmp_path))  # the session's project directory
    monkeypatch.setattr(fb, "emit", lambda _e: None)
    runner, provider = _runner([_end(38_000), _end(12_000)])
    seen: list[int] = []
    played = provider.stream_to_response

    async def _measured_at_call(**kw):
        seen.append(runner.usage.effective_context_tokens())
        return await played(**kw)

    provider.stream_to_response = _measured_at_call
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-manual", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)

    async def _initialize():
        sess.session, sess.provider, sess.runner = session, provider, runner

    sess.initialize = _initialize
    sess._mirror_compaction_event = lambda *_a, **_k: None
    sess._write_compaction_snapshot = lambda **_k: {}
    sess._save_transcript = lambda: None
    sess._ledger_ground_truth = lambda: None

    def _compact(_self, transcript, _provider, **_kw):
        transcript.append_compaction("We did the first task.", first_kept_id=transcript.entries[-1].id,
                                     tokens_before=38_000)
        return CompactionResult(success=True, summary="We did the first task.", tokens_before=38_000,
                                tokens_after=100)

    monkeypatch.setattr(SummaryCompaction, "compact", _compact)
    await sess.force_compact()
    left = session.estimate_tokens()

    await runner.run(session, "second task")

    assert seen == [0, left]
    assert left > 0


# ─── what the advisories promise ─────────────────────────────────────
# Nothing counts or rejects tool calls. Past 70% of the usable window the
# runtime compacts the history before its next request.


async def test_the_crossing_advisory_says_what_happens_past_70_percent():
    runner, _ = _runner([_tools("c1", 30_000), _end(31_000)])
    await runner.run(Session.create(system_prompt="t"), "a task")

    advisory = runner.consume_channel3_advisory()

    assert "crossed 60-69%" in advisory
    assert "rejected" not in advisory
    assert "Past 70% the runtime compacts the history before its next request" in advisory


def test_the_tag_past_70_percent_says_what_happens():
    from types import SimpleNamespace

    from bridge.freyja_bridge import _build_pressure_tag
    from engine.usage import UsageAccumulator

    usage = UsageAccumulator(last_input=36_000)
    runner = SimpleNamespace(provider=_Provider([]), config=AgentConfig(max_tokens_per_turn=RESERVED), usage=usage)

    tag = _build_pressure_tag(runner)

    assert tag.startswith("[ctx: 72% (36,000/50,000)")
    assert "rejected" not in tag
    assert "the runtime compacts the history before its next request" in tag
