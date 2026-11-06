"""Anthropic prompt-cache breakpoint placement.

Caching is prefix-ordered — tools, then system, then messages — so a volatile
value early in the request invalidates everything behind it. Two problems this
covers:

  1. The conversation tail was never a breakpoint, so message history after the
     latest compaction summary was re-processed at full input rate on every
     turn. That is the largest recurring cost in a long session and the reason
     a forked session could not reuse its parent's prefix.
  2. The runner tail-appends ``<system-reminder>`` guidance to a clone of the
     last user message on every request. A breakpoint placed on or after those
     blocks would write a cache entry whose prefix can never recur.

Also pins the day-resolution date block: a minute-resolution clock in the
system prompt made the FIRST breakpoint miss on nearly every turn, which made
all the others worthless too.
"""

from __future__ import annotations

from engine.anthropic_provider import (
    MAX_CACHE_BREAKPOINTS,
    _count_cache_breakpoints,
    _try_cache_compaction_summary,
    _try_cache_conversation_tail,
)

# ─── helpers ──────────────────────────────────────────────────────────


REMINDER = "<system-reminder>\nCurrent time: whenever\n</system-reminder>"


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _user(*blocks) -> dict:
    return {"role": "user", "content": list(blocks)}


def _marked(msg: dict) -> list[dict]:
    """Blocks in `msg` carrying a cache_control marker."""
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("cache_control")]


# ─── the tail breakpoint ──────────────────────────────────────────────


def test_the_tail_is_marked_whether_blocks_or_a_string() -> None:
    blocks = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        _user(_text("second")),
    ]
    _try_cache_conversation_tail(blocks)
    assert [b["text"] for b in _marked(blocks[-1])] == ["second"]

    # A string tail is promoted to a block so it can carry the marker.
    string = [{"role": "user", "content": "just a string"}]
    _try_cache_conversation_tail(string)
    content = string[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == "just a string"
    assert content[0]["cache_control"] == {"type": "ephemeral"}


def test_the_marker_goes_before_the_per_request_reminders() -> None:
    # This is the whole point: the reminder blocks change every request, so a
    # marker on or after them would be a cache write that can never be hit.
    msgs = [
        _user(
            _text("the real turn"),
            _text(REMINDER),
            _text("<system-reminder>\nledger\n</system-reminder>"),
        )
    ]
    _try_cache_conversation_tail(msgs)
    marked = _marked(msgs[-1])
    assert len(marked) == 1
    assert marked[0]["text"] == "the real turn"


def test_a_tool_result_tail_is_marked_on_the_tool_result() -> None:
    # Mid-tool-loop, the tail message is a tool_result batch plus reminders.
    msgs = [
        _user(
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            _text(REMINDER),
        )
    ]
    _try_cache_conversation_tail(msgs)
    marked = _marked(msgs[-1])
    assert len(marked) == 1
    assert marked[0]["type"] == "tool_result"


def test_no_marker_when_nothing_before_the_reminders_can_carry_it() -> None:
    # Defensive: older transcripts (and any caller that fuses the reminder into
    # the string) must not get a poisoned breakpoint.
    fused = [{"role": "user", "content": "turn text" + REMINDER}]
    _try_cache_conversation_tail(fused)
    assert isinstance(fused[-1]["content"], str)

    only_reminders = [_user(_text(REMINDER))]
    _try_cache_conversation_tail(only_reminders)
    assert _marked(only_reminders[-1]) == []

    for empty in ([], [{"role": "user", "content": ""}]):
        before = [dict(m) for m in empty]
        _try_cache_conversation_tail(empty)
        assert empty == before


# ─── budget ───────────────────────────────────────────────────────────


def test_tail_and_compaction_summary_coexist_within_budget() -> None:
    msgs = [
        _user(_text("[System context]: [Previous conversation summary]\nolder stuff")),
        {"role": "assistant", "content": "ack"},
        _user(_text("newest turn"), _text(REMINDER)),
    ]
    _try_cache_compaction_summary(msgs)
    _try_cache_conversation_tail(msgs)
    # 2 in-message markers + system + last tool = exactly Anthropic's cap.
    assert _count_cache_breakpoints(msgs) == 2
    assert _count_cache_breakpoints(msgs) + 2 == MAX_CACHE_BREAKPOINTS
    assert _marked(msgs[0])[0]["text"].startswith("[System context]")
    assert _marked(msgs[-1])[0]["text"] == "newest turn"


def test_the_message_budget_is_never_exceeded() -> None:
    # The tail is not marked twice when it is the summary itself.
    summary_only = [_user(_text("[System context]: [Previous conversation summary]\nrecap"))]
    _try_cache_compaction_summary(summary_only)
    _try_cache_conversation_tail(summary_only)
    assert _count_cache_breakpoints(summary_only) == 1

    # And no marker is added once the two in-message slots are spent.
    spent = [
        _user({"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}),
        _user({"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}}),
        _user(_text("c")),
    ]
    _try_cache_conversation_tail(spent)
    assert _marked(spent[-1]) == []


# ─── the date block ───────────────────────────────────────────────────


def test_the_clock_lives_in_the_reminder_not_the_system_date_block() -> None:
    from bridge.tools.coordination import current_date_block, current_time_reminder
    from engine.anthropic_provider import _EPHEMERAL_REMINDER_MARKER

    a = current_date_block()
    assert a == current_date_block()
    # No clock: the value must not move between two turns a minute apart.
    assert ":" not in a.split("timezone")[0]

    reminder = current_time_reminder()
    assert reminder.startswith("<system-reminder>")
    assert reminder.rstrip().endswith("</system-reminder>")
    assert "Current time:" in reminder
    # It has to be recognisable as a reminder so the tail breakpoint skips it.
    assert _EPHEMERAL_REMINDER_MARKER in reminder


# ─── the runner's tail-append shape ───────────────────────────────────


def test_runner_writes_the_reminders_into_the_transcript_message_once() -> None:
    # OpenAI resumes a cached prefix only where an earlier request ended. If
    # the turn's first request carried reminders the second did not repeat,
    # the second could never resume from the first and re-read everything
    # after the previous turn. So the block stays in the message, as its own
    # block (the provider can only place the breakpoint before the reminders
    # if they are separable), and a retry of the same request does not add
    # another.
    from engine.runner import AsyncAgentRunner
    from engine.types import Message, TextBlock

    runner = AsyncAgentRunner.__new__(AsyncAgentRunner)
    runner._current_pressure_ratio = lambda: 0.0  # type: ignore[method-assign]
    runner._build_pressure_note = lambda _r: None  # type: ignore[method-assign]
    builds: list[int] = []

    def _gather() -> str:
        builds.append(1)
        return REMINDER

    runner._gather_extra_reminders = _gather  # type: ignore[method-assign]

    original = Message(role="user", content="plain string turn")
    messages = [original]
    first = runner._augment_messages_with_pressure_note(messages)
    assert first[-1] is original
    assert [type(b) for b in original.content] == [TextBlock, TextBlock]
    assert [b.text for b in original.content] == ["plain string turn", REMINDER]

    retry = runner._augment_messages_with_pressure_note(messages)
    assert len(retry[-1].content) == 2
    assert builds == [1]

    # The turn's next request ends in a tool result: no reminders, and the
    # producers do not run, so a one-shot reminder is not built and lost.
    later = messages + [
        Message(role="assistant", content="", tool_calls=None),
        Message(role="tool_result", content="ok", tool_call_id="call_1"),
    ]
    assert runner._augment_messages_with_pressure_note(later) is later
    assert builds == [1]
    assert later[0].content[1].text == REMINDER
