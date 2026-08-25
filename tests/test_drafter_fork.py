"""The forked skill drafter.

Instead of spawning a fresh sub-agent and handing it an excerpt — every message
truncated to its first 1 000 chars — the drafter now forks the session under
review and appends its instructions as a user message. It reads the real
transcript, and its request prefix is byte-identical to the parent's so the
conversation comes out of the prompt cache.

That byte-identity is fragile, and it is what most of these tests defend:

  · the fork's tool array must match the parent's exactly — same entries, same
    order, same schema visibility — or Anthropic's prefix cache misses on
    everything behind it
  · read-only must be ENFORCED, not merely asserted in prose the way the
    sub-agent drafter's whitelist did with ``bash``
  · the decision block has to survive a SKILL.md body that contains its own
    code fences, which is most of them
"""

from __future__ import annotations

import pytest

from bridge.knowledge.learning.drafter_prompt import (
    build_forked_drafter_injection,
)
from bridge.knowledge.learning.fork_output import (
    MAX_BODY_CHARS,
    extract_block,
    parse_fork_decision,
)

# ─── the decision block ───────────────────────────────────────────────


def _block(payload: str, fence: str = "````") -> str:
    return f"Some reasoning first.\n\n{fence}skill-candidate\n{payload}\n{fence}\n"


def test_a_save_round_trips() -> None:
    decision = parse_fork_decision(
        _block(
            '{"decision":"save","rationale":"worth it","name":"ema-release-ops",'
            '"description":"When cutting a release","skill_type":"workflow",'
            '"triggers":["harness-push"],"tags":["release"],"body":"# Release\\n\\nDo it."}'
        )
    )
    assert decision is not None
    assert decision.is_save
    assert decision.name == "ema-release-ops"
    assert decision.skill_type == "workflow"
    assert decision.triggers == ["harness-push"]
    assert decision.body == "# Release\n\nDo it."


def test_a_body_with_its_own_code_fences_survives() -> None:
    # This is why the contract asks for FOUR backticks. Nearly every SKILL.md
    # body contains ``` fences; a three-backtick wrapper would end the block
    # partway through the body and truncate the skill silently.
    body = "# Deploy\\n\\n```bash\\nmake deploy\\n```\\n\\nThen verify."
    decision = parse_fork_decision(
        _block(
            '{"decision":"save","rationale":"r","name":"deploy-thing",'
            '"description":"d","body":"' + body + '"}'
        )
    )
    assert decision is not None
    assert decision.is_save
    assert "```bash" in decision.body
    assert decision.body.endswith("Then verify.")


def test_a_three_backtick_fence_still_parses() -> None:
    # The contract asks for four; accept three so a model that follows the
    # more familiar convention isn't silently discarded.
    decision = parse_fork_decision(
        _block('{"decision":"skip","rationale":"nothing here"}', fence="```")
    )
    assert decision is not None
    assert decision.decision == "skip"
    assert decision.rationale == "nothing here"


def test_the_last_block_wins() -> None:
    # A drafter that shows a draft, reconsiders, and emits a final version
    # meant the final one.
    text = (
        _block('{"decision":"save","rationale":"first","name":"aaa-first",'
               '"description":"d","body":"b"}')
        + _block('{"decision":"skip","rationale":"changed my mind"}')
    )
    decision = parse_fork_decision(text)
    assert decision is not None
    assert decision.decision == "skip"
    assert decision.rationale == "changed my mind"


def test_no_block_is_distinguishable_from_a_skip() -> None:
    # None means "the drafter emitted nothing we can act on" — it may have
    # published through propose_skill instead. A ForkDecision(skip) means it
    # explicitly declined. Collapsing the two is how the old keyword-scan
    # heuristic reported a published candidate as a skip.
    assert parse_fork_decision("I looked and there was nothing.") is None
    assert parse_fork_decision("") is None


def test_prose_after_the_block_is_ignored() -> None:
    text = _block('{"decision":"skip","rationale":"r"}') + "\nAnything else here."
    decision = parse_fork_decision(text)
    assert decision is not None and decision.decision == "skip"


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ('{"decision":"save","rationale":"r","description":"d","body":"b"}', "no name"),
        (
            '{"decision":"save","rationale":"r","name":"ok-name","description":"d","body":"   "}',
            "no body",
        ),
        (
            '{"decision":"save","rationale":"r","name":"ok-name","body":"b"}',
            "no description",
        ),
        (
            '{"decision":"save","rationale":"r","name":"Bad Name","description":"d","body":"b"}',
            "not a valid skill name",
        ),
        ('{"decision":"maybe","rationale":"r"}', "unknown decision"),
        ("{not json at all}", "not valid JSON"),
        ('["a","list"]', "not a JSON object"),
    ],
)
def test_a_malformed_save_reports_why(payload: str, fragment: str) -> None:
    # A silently-dropped candidate is indistinguishable from a rational skip,
    # which makes a broken drafter invisible. Every rejection carries a reason.
    decision = parse_fork_decision(_block(payload))
    assert decision is not None
    assert not decision.is_save
    assert fragment in decision.error


def test_an_oversized_body_is_rejected_with_its_size() -> None:
    body = "x" * (MAX_BODY_CHARS + 10)
    decision = parse_fork_decision(
        _block(
            '{"decision":"save","rationale":"r","name":"big-one","description":"d",'
            '"body":"' + body + '"}'
        )
    )
    assert decision is not None
    assert not decision.is_save
    assert "over the" in decision.error


def test_an_unknown_skill_type_falls_back_to_build() -> None:
    decision = parse_fork_decision(
        _block(
            '{"decision":"save","rationale":"r","name":"a-skill","description":"d",'
            '"skill_type":"nonsense","body":"b"}'
        )
    )
    assert decision is not None
    assert decision.skill_type == "build"


def test_triggers_and_tags_are_cleaned_and_capped() -> None:
    decision = parse_fork_decision(
        _block(
            '{"decision":"save","rationale":"r","name":"a-skill","description":"d",'
            '"body":"b","triggers":[" one ","",2,"two","3","4","5","6","7","8","9"],'
            '"tags":["  x  "]}'
        )
    )
    assert decision is not None
    assert decision.triggers[0] == "one"
    assert len(decision.triggers) == 8
    assert decision.tags == ["x"]


def test_extract_block_returns_none_without_the_label() -> None:
    assert extract_block("```json\n{}\n```") is None


# ─── the injected message ─────────────────────────────────────────────


def _injection(**over) -> str:
    kwargs = {
        "loaded_skill_names": ["ema-release-ops"],
        "all_skill_names": ["ema-release-ops", "gh-address-comments"],
        "negative_library_excerpt": "",
        "operator_guidance": "",
    }
    kwargs.update(over)
    return build_forked_drafter_injection(**kwargs)


def test_the_injection_switches_the_model_out_of_assistant_mode() -> None:
    # It arrives behind a system prompt that spent ~20 KB telling the model to
    # be the operator's assistant, so it has to say plainly that this turn is
    # not a continuation of the work.
    text = _injection()
    assert "MODE SWITCH" in text
    assert "do not continue the previous task" in text
    assert "The operator is not reading this turn" in text


def test_the_injection_does_not_paste_the_conversation() -> None:
    # The whole point of the fork: the transcript is already above.
    text = _injection()
    assert "[CONVERSATION]" not in text
    assert "conversation above" in text


def test_the_injection_carries_the_skill_landscape() -> None:
    text = _injection()
    assert "ema-release-ops" in text
    assert "[SKILLS ON DISK]" in text
    assert "[LOADED IN THIS SESSION]" in text
    assert "[RECENTLY REJECTED" in text


def test_the_injection_states_the_output_contract_last() -> None:
    text = _injection()
    assert "skill-candidate" in text
    assert text.rstrip().endswith("emit exactly one decision block.")
    # Four backticks, so a body with its own fences survives.
    assert "````skill-candidate" in text


def test_operator_guidance_is_included_only_when_given() -> None:
    assert "[OPERATOR GUIDANCE]" not in _injection()
    with_guidance = _injection(operator_guidance="focus on the deploy workflow")
    assert "[OPERATOR GUIDANCE]" in with_guidance
    assert "focus on the deploy workflow" in with_guidance


def test_the_injection_is_honest_about_the_read_only_enforcement() -> None:
    text = _injection()
    assert "READ-ONLY and that is enforced" in text


def test_an_empty_library_reads_as_empty_not_broken() -> None:
    text = _injection(loaded_skill_names=[], all_skill_names=[])
    assert "(none yet)" in text
    assert "(none — no skill was loaded in this session)" in text
    assert "(no recent rejections)" in text


def test_the_skill_list_is_capped() -> None:
    from bridge.knowledge.learning.constants import DRAFTER_MAX_LISTED_SKILLS

    names = [f"skill-{i:03d}" for i in range(DRAFTER_MAX_LISTED_SKILLS + 7)]
    text = _injection(all_skill_names=names, loaded_skill_names=[])
    assert "7 more not shown" in text
    assert names[DRAFTER_MAX_LISTED_SKILLS] not in text


# ─── the read-only fork registry ──────────────────────────────────────


class _FakeTool:
    def __init__(self, name: str, tier: str = "hot") -> None:
        from engine.tools import ToolDefinition, ToolTier

        self.definition = ToolDefinition(
            name=name,
            description=f"{name} does things",
            summary=name,
            parameters={"type": "object", "properties": {}},
            tier=ToolTier(tier),
        )
        self.calls: list[dict] = []

    async def execute(self, call_id: str, arguments: dict):
        from engine.types import ToolResult

        self.calls.append(arguments)
        return ToolResult(call_id=call_id, content="ran")


def _parent_registry():
    from engine.tools import ToolRegistry

    reg = ToolRegistry()
    for name in ("read_file", "write_file", "bash", "sub_agent", "grep", "load_skill"):
        reg.register(_FakeTool(name))
    reg.register(_FakeTool("propose_skill", tier="warm"))
    return reg


def test_the_fork_registry_matches_the_parent_exactly() -> None:
    # Same entries, same ORDER. Anthropic caches tools → system → messages by
    # prefix, so a reordered array is as invalidating as a changed one.
    from bridge.tools.fork_registry import build_read_only_fork_registry

    parent = _parent_registry()
    fork = build_read_only_fork_registry(parent)

    assert list(fork._tools.keys()) == list(parent._tools.keys())
    assert [d.name for d in fork.list_definitions()] == [
        d.name for d in parent.list_definitions()
    ]
    # And the definition OBJECTS are the parent's, not copies that might drift.
    for name in parent._tools:
        assert fork.get(name).definition is parent.get(name).definition


def test_a_promoted_deferred_tool_stays_promoted_in_the_fork() -> None:
    # tool_search can flip a WARM tool into the request mid-session. If the
    # fork rebuilt from static tiers it would silently un-promote it, changing
    # the tools array and costing the cache hit.
    from bridge.tools.fork_registry import build_read_only_fork_registry

    parent = _parent_registry()
    assert "propose_skill" not in [d.name for d in parent.list_definitions()]
    parent.promote_tool("propose_skill")
    assert "propose_skill" in [d.name for d in parent.list_definitions()]

    fork = build_read_only_fork_registry(parent)
    assert "propose_skill" in [d.name for d in fork.list_definitions()]


@pytest.mark.asyncio
async def test_read_tools_run_normally() -> None:
    from bridge.tools.fork_registry import build_read_only_fork_registry

    parent = _parent_registry()
    fork = build_read_only_fork_registry(parent)
    result = await fork.get("read_file").execute("c1", {"path": "x"})
    assert not result.is_error
    assert result.content == "ran"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["write_file", "bash", "sub_agent"])
async def test_mutating_tools_refuse_at_execution_time(name: str) -> None:
    # The sub-agent drafter "enforced" read-only with a tool_include whitelist
    # that contained bash, plus a system prompt asking it nicely. This is the
    # first version where the promise is code.
    from bridge.tools.fork_registry import build_read_only_fork_registry

    parent = _parent_registry()
    fork = build_read_only_fork_registry(parent)
    tool = fork.get(name)

    result = await tool.execute("c1", {"anything": True})
    assert result.is_error
    assert "not available in this review fork" in result.content
    # The real tool never saw the call.
    assert parent.get(name).calls == []
    # But its schema is still on the wire, byte-identical.
    assert tool.definition is parent.get(name).definition


@pytest.mark.asyncio
async def test_also_allow_opens_one_specific_tool() -> None:
    from bridge.tools.fork_registry import build_read_only_fork_registry

    parent = _parent_registry()
    fork = build_read_only_fork_registry(
        parent, also_allow=frozenset({"propose_skill"})
    )
    result = await fork.get("propose_skill").execute("c1", {"name": "x"})
    assert not result.is_error


def test_a_refused_tool_does_not_trigger_a_permission_prompt() -> None:
    from bridge.tools.fork_registry import build_read_only_fork_registry

    parent = _parent_registry()
    fork = build_read_only_fork_registry(parent)
    assert getattr(fork.get("write_file"), "requires_permission", False) is False


def test_an_unknown_future_tool_defaults_to_refused() -> None:
    # ALLOW-list, not deny-list: a tool added to the registry next month must
    # not silently hand a reviewer new powers.
    from bridge.tools.fork_registry import READ_ONLY_TOOLS, build_read_only_fork_registry
    from engine.tools import ToolRegistry

    parent = ToolRegistry()
    parent.register(_FakeTool("some_new_destructive_tool"))
    fork = build_read_only_fork_registry(parent)
    assert "some_new_destructive_tool" not in READ_ONLY_TOOLS
    assert type(fork.get("some_new_destructive_tool")).__name__ == "RefusedTool"


# ─── the bridge's fork run ────────────────────────────────────────────


class _FakeSubAgentTool:
    """Captures what the bridge asked spawn_fork for, and replies with a
    canned final message."""

    def __init__(self, response: str, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.call: dict | None = None

    async def spawn_fork(self, **kwargs):
        from types import SimpleNamespace

        self.call = kwargs
        record = SimpleNamespace(id="sub_fork_1", child_model="claude-opus-4-8")
        return record, self.response, self.error


def _fake_session(sid: str = "session-under-review"):
    from types import SimpleNamespace

    return SimpleNamespace(
        system_prompt="PARENT SYSTEM PROMPT",
        serialize_transcript=lambda: {"version": 1, "transcript": {"entries": [1, 2, 3]}},
    )


def _bridge_session(sub_tool, monkeypatch, emitted: list):
    """A _BridgeSession stub carrying only what _run_drafter_fork touches."""
    from types import SimpleNamespace

    from bridge import freyja_bridge as fb

    monkeypatch.setattr(fb, "emit", lambda payload: emitted.append(payload))

    sess = SimpleNamespace(
        id="session-under-review",
        current_turn_id="turn-9",
        reasoning_level="high",
        session=_fake_session(),
        tool_registry=SimpleNamespace(_tools={"sub_agent": sub_tool}),
    )
    sess._emit_drafter_pass = fb._BridgeSession._emit_drafter_pass.__get__(sess)
    sess._append_drafter_decision = fb._BridgeSession._append_drafter_decision.__get__(sess)
    sess._run_drafter_fork = fb._BridgeSession._run_drafter_fork.__get__(sess)
    return sess


@pytest.mark.asyncio
async def test_the_fork_is_handed_the_parents_state(monkeypatch) -> None:
    emitted: list = []
    sub = _FakeSubAgentTool(
        _block('{"decision":"skip","rationale":"nothing durable here"}')
    )
    sess = _bridge_session(sub, monkeypatch, emitted)

    await sess._run_drafter_fork(
        run_id="run1",
        loaded_skill_names=["a-skill"],
        all_skill_names=["a-skill"],
        operator_guidance="",
    )

    assert sub.call is not None
    # The parent's system prompt, verbatim — not an agent-type prompt.
    assert sub.call["system_prompt"] == "PARENT SYSTEM PROMPT"
    # The parent's transcript, not a rendered excerpt.
    assert sub.call["transcript_snapshot"]["transcript"]["entries"] == [1, 2, 3]
    assert sub.call["agent_type_name"] == "skill-drafter-fork"
    assert sub.call["source_session_id"] == "session-under-review"
    # propose_skill stays reachable as a secondary publish path.
    assert "propose_skill" in sub.call["also_allow"]
    # The instructions are the injected user message.
    assert "MODE SWITCH" in sub.call["injected_message"]
    # And the parent's reasoning level rides along: Anthropic invalidates a
    # cached prefix when the thinking budget changes, so a fork that switched
    # effort would pay full input rate for the transcript it forked to reuse.
    assert sub.call["thinking_effort"] == "high"


@pytest.mark.asyncio
async def test_a_skip_is_reported_with_its_rationale(monkeypatch) -> None:
    emitted: list = []
    sub = _FakeSubAgentTool(
        _block('{"decision":"skip","rationale":"one-shot fix, not a pattern"}')
    )
    sess = _bridge_session(sub, monkeypatch, emitted)
    await sess._run_drafter_fork(
        run_id="run1", loaded_skill_names=[], all_skill_names=[], operator_guidance="",
    )

    passes = [e for e in emitted if e["type"] == "skill_drafter_pass"]
    assert len(passes) == 1
    assert passes[0]["decision"] == "skip"
    assert passes[0]["rationale"] == "one-shot fix, not a pattern"
    # The model field used to be hardcoded empty, leaving the run detail's
    # model row permanently blank.
    assert passes[0]["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_a_save_publishes_and_names_the_candidate(monkeypatch) -> None:

    emitted: list = []
    published: list = []

    def _publish(**kwargs):
        published.append(kwargs)
        return ("cand_123", "safe", None)

    monkeypatch.setattr(
        "bridge.knowledge.learning.publish.publish_candidate", _publish
    )

    sub = _FakeSubAgentTool(
        _block(
            '{"decision":"save","rationale":"recurring release dance",'
            '"name":"ema-release-ops","description":"When cutting a release",'
            '"skill_type":"workflow","triggers":["harness-push"],"tags":["release"],'
            '"body":"# Release\\n\\nSteps."}'
        )
    )
    sess = _bridge_session(sub, monkeypatch, emitted)
    await sess._run_drafter_fork(
        run_id="run1", loaded_skill_names=[], all_skill_names=[], operator_guidance="",
    )

    assert len(published) == 1
    call = published[0]
    assert call["name"] == "ema-release-ops"
    assert call["skill_type"] == "workflow"
    # Provenance points at the conversation that produced it, not the fork.
    assert call["source_session_id"] == "session-under-review"
    assert call["source_turn_id"] == "turn-9"
    # drafter_model was previously always empty on every candidate on disk.
    assert call["drafter_model"] == "claude-opus-4-8"

    passes = [e for e in emitted if e["type"] == "skill_drafter_pass"]
    assert passes[0]["decision"] == "save"
    assert passes[0]["name"] == "ema-release-ops"
    assert passes[0]["candidateId"] == "cand_123"


@pytest.mark.asyncio
async def test_a_run_with_no_block_does_not_claim_to_know_what_happened(
    monkeypatch,
) -> None:
    # The old path keyword-scanned the final text for "propose_skill" and
    # reported save/skip from that, so a drafter that published and then said
    # "Done — added the release skill." was reported as a skip. Now the
    # skill_candidate event is the only claim about publishing.
    emitted: list = []
    sub = _FakeSubAgentTool("Done — added the release skill.")
    sess = _bridge_session(sub, monkeypatch, emitted)
    await sess._run_drafter_fork(
        run_id="run1", loaded_skill_names=[], all_skill_names=[], operator_guidance="",
    )

    passes = [e for e in emitted if e["type"] == "skill_drafter_pass"]
    assert passes[0]["decision"] == "skip"
    assert "no decision block" in passes[0]["rationale"] or passes[0]["rationale"]
    assert "candidateId" not in passes[0]


@pytest.mark.asyncio
async def test_a_malformed_block_is_an_error_not_a_silent_skip(monkeypatch) -> None:
    emitted: list = []
    sub = _FakeSubAgentTool(
        _block('{"decision":"save","rationale":"r","name":"ok-name","body":"b"}')
    )
    sess = _bridge_session(sub, monkeypatch, emitted)
    await sess._run_drafter_fork(
        run_id="run1", loaded_skill_names=[], all_skill_names=[], operator_guidance="",
    )

    passes = [e for e in emitted if e["type"] == "skill_drafter_pass"]
    assert passes[0]["decision"] == "error"
    assert "no description" in passes[0]["rationale"]


@pytest.mark.asyncio
async def test_a_runner_error_is_reported(monkeypatch) -> None:
    emitted: list = []
    sub = _FakeSubAgentTool("", error=RuntimeError("model unavailable"))
    sess = _bridge_session(sub, monkeypatch, emitted)
    await sess._run_drafter_fork(
        run_id="run1", loaded_skill_names=[], all_skill_names=[], operator_guidance="",
    )

    passes = [e for e in emitted if e["type"] == "skill_drafter_pass"]
    assert passes[0]["decision"] == "error"
    assert "model unavailable" in passes[0]["rationale"]


@pytest.mark.asyncio
async def test_the_run_links_to_the_fork_session_for_navigation(monkeypatch) -> None:
    emitted: list = []
    sub = _FakeSubAgentTool(_block('{"decision":"skip","rationale":"r"}'))
    sess = _bridge_session(sub, monkeypatch, emitted)
    await sess._run_drafter_fork(
        run_id="run1", loaded_skill_names=[], all_skill_names=[], operator_guidance="",
    )

    linked = [e for e in emitted if e["type"] == "skill_drafter_run_linked"]
    assert len(linked) == 1
    assert linked[0]["subagentSessionId"] == "sub_fork_1"
    assert linked[0]["runId"] == "run1"


@pytest.mark.asyncio
async def test_a_publish_refusal_is_surfaced(monkeypatch) -> None:
    emitted: list = []
    monkeypatch.setattr(
        "bridge.knowledge.learning.publish.publish_candidate",
        lambda **_k: (None, "dangerous", "skills guard: exfiltration pattern"),
    )
    sub = _FakeSubAgentTool(
        _block(
            '{"decision":"save","rationale":"r","name":"bad-skill",'
            '"description":"d","body":"b"}'
        )
    )
    sess = _bridge_session(sub, monkeypatch, emitted)
    await sess._run_drafter_fork(
        run_id="run1", loaded_skill_names=[], all_skill_names=[], operator_guidance="",
    )

    passes = [e for e in emitted if e["type"] == "skill_drafter_pass"]
    assert passes[0]["decision"] == "error"
    assert "exfiltration" in passes[0]["rationale"]


# ─── cadence telemetry ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cadence_state_is_emitted_in_milliseconds(monkeypatch) -> None:
    # The event was fully specified — declared in shared/events.ts, reduced in
    # the store, rendered by DrafterActivityStrip — and had no Python emitter,
    # so the strip's "next review in N turns" was permanently blank.
    #
    # `last_trip_ts` is already epoch MILLIseconds (review_scheduler writes it
    # with _now_ms()); scaling it again puts the timestamp 56 000 years out and
    # renders as a nonsense relative time.
    from types import SimpleNamespace

    from bridge import freyja_bridge as fb

    emitted: list = []
    monkeypatch.setattr(fb, "emit", lambda p: emitted.append(p))
    monkeypatch.setattr(
        "bridge.knowledge.learning.review_scheduler.read_cadence_state",
        lambda: {
            "turns_since_last_review": 3,
            "last_trip_ts": 1_787_653_742_261,
            "threshold": 10,
        },
    )

    sess = SimpleNamespace(id="s1")
    sess._emit_cadence_state = fb._BridgeSession._emit_cadence_state.__get__(sess)
    sess._emit_cadence_state()

    assert len(emitted) == 1
    ev = emitted[0]
    assert ev["type"] == "cadence_state"
    assert ev["turnsSinceLastReview"] == 3
    assert ev["turnsUntilTrip"] == 7
    assert ev["lastTrippedAt"] == 1_787_653_742_261


@pytest.mark.asyncio
async def test_cadence_state_falls_back_to_the_default_threshold(monkeypatch) -> None:
    from types import SimpleNamespace

    from bridge import freyja_bridge as fb
    from bridge.knowledge.learning.constants import CADENCE_DEFAULT_THRESHOLD

    emitted: list = []
    monkeypatch.setattr(fb, "emit", lambda p: emitted.append(p))
    # A cadence file that has never been written carries no threshold key.
    monkeypatch.setattr(
        "bridge.knowledge.learning.review_scheduler.read_cadence_state",
        lambda: {"turns_since_last_review": 0, "last_trip_ts": 0},
    )

    sess = SimpleNamespace(id="s1")
    sess._emit_cadence_state = fb._BridgeSession._emit_cadence_state.__get__(sess)
    sess._emit_cadence_state()

    assert emitted[0]["turnsUntilTrip"] == CADENCE_DEFAULT_THRESHOLD


@pytest.mark.asyncio
async def test_cadence_state_never_raises(monkeypatch) -> None:
    from types import SimpleNamespace

    from bridge import freyja_bridge as fb

    def _boom():
        raise OSError("cadence file unreadable")

    monkeypatch.setattr(fb, "emit", lambda _p: None)
    monkeypatch.setattr(
        "bridge.knowledge.learning.review_scheduler.read_cadence_state", _boom
    )
    sess = SimpleNamespace(id="s1")
    sess._emit_cadence_state = fb._BridgeSession._emit_cadence_state.__get__(sess)
    sess._emit_cadence_state()  # must not raise into the turn loop


# ─── fork isolation ───────────────────────────────────────────────────


def test_a_snapshot_does_not_share_metadata_with_its_session() -> None:
    # restore_transcript assigns the snapshot's metadata dict straight onto the
    # restored session. Handing out the LIVE dict meant a fork stamping its own
    # identity ("subagent_id", "agent_type", its model) wrote those fields into
    # the parent session it forked from.
    from engine.session import Session

    parent = Session.create(system_prompt="p", metadata={"model_id": "opus", "a": 1})
    snapshot = parent.serialize_transcript()
    snapshot["metadata"]["model_id"] = "sonnet"
    snapshot["metadata"]["subagent_id"] = "sub_1"

    assert parent.metadata["model_id"] == "opus"
    assert "subagent_id" not in parent.metadata


def test_a_restored_session_does_not_share_metadata_with_the_source() -> None:
    from engine.session import Session

    parent = Session.create(system_prompt="p", metadata={"model_id": "opus"})
    fork = Session.create(system_prompt="p", session_id="sub_1")
    fork.restore_transcript(parent.serialize_transcript())
    fork.metadata.update({"model_id": "sonnet", "subagent_id": "sub_1"})

    assert parent.metadata == {"model_id": "opus"}


def test_tool_search_in_a_fork_promotes_only_the_fork(monkeypatch) -> None:
    # The parent's ToolSearchTool closes over the PARENT registry. Left in
    # place, a fork calling it flips schema_visible on the parent's catalog —
    # changing the parent's tools array from inside a fork, which invalidates
    # the cached prefix the fork exists to reuse.
    import asyncio

    from bridge.tools.fork_registry import build_read_only_fork_registry
    from bridge.tools.tool_search_tool import ToolSearchTool

    parent = _parent_registry()
    parent.register(ToolSearchTool(parent))
    fork = build_read_only_fork_registry(parent)

    assert fork.get("tool_search") is not parent.get("tool_search")
    # Definition stays byte-identical, or the tools array would differ.
    assert fork.get("tool_search").definition.name == "tool_search"
    assert (
        fork.get("tool_search").definition.parameters
        == parent.get("tool_search").definition.parameters
    )

    before = [d.name for d in parent.list_definitions()]
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        fork.get("tool_search").execute("c1", {"tool_name": "propose_skill"})
    )
    assert [d.name for d in parent.list_definitions()] == before
    assert "propose_skill" in [d.name for d in fork.list_definitions()]


def test_crlf_output_still_parses() -> None:
    # re.MULTILINE's "$" matches before the "\n" but after the "\r", so a model
    # emitting Windows line endings produced no match at all and its decision
    # was discarded as "no block emitted".
    text = (
        "Reasoning.\r\n\r\n````skill-candidate\r\n"
        '{"decision": "skip", "rationale": "crlf"}\r\n'
        "````\r\n"
    )
    decision = parse_fork_decision(text)
    assert decision is not None
    assert decision.decision == "skip"
    assert decision.rationale == "crlf"


def test_leading_whitespace_before_the_fence_is_tolerated() -> None:
    text = "   ````skill-candidate\n" '{"decision": "skip", "rationale": "indented"}\n' "   ````\n"
    decision = parse_fork_decision(text)
    assert decision is not None
    assert decision.rationale == "indented"


def test_restore_copies_metadata_even_from_a_shared_dict() -> None:
    # Belt and braces: serialize copies on the way out, restore copies on the
    # way in. A caller that hand-builds a snapshot around a live dict — or any
    # future serializer that forgets — still cannot alias two sessions.
    from engine.session import Session

    shared = {"model_id": "opus", "project_session_id": "sub_parent"}
    fork = Session.create(system_prompt="p", session_id="sub_1")
    fork.restore_transcript({"version": 1, "metadata": shared, "transcript": None})
    fork.metadata["project_session_id"] = "sub_1"

    assert shared["project_session_id"] == "sub_parent"
