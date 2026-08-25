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


def test_tail_of_a_plain_conversation_is_marked() -> None:
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        _user(_text("second")),
    ]
    _try_cache_conversation_tail(msgs)
    assert len(_marked(msgs[-1])) == 1
    assert _marked(msgs[-1])[0]["text"] == "second"


def test_string_content_is_promoted_to_a_marked_block() -> None:
    msgs = [{"role": "user", "content": "just a string"}]
    _try_cache_conversation_tail(msgs)
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == "just a string"
    assert content[0]["cache_control"] == {"type": "ephemeral"}


def test_the_marker_goes_before_the_per_request_reminders() -> None:
    # This is the whole point: the reminder block changes every request, so a
    # marker on it would be a cache write that can never be hit.
    msgs = [_user(_text("the real turn"), _text(REMINDER))]
    _try_cache_conversation_tail(msgs)
    marked = _marked(msgs[-1])
    assert len(marked) == 1
    assert marked[0]["text"] == "the real turn"


def test_multiple_trailing_reminders_are_all_skipped() -> None:
    msgs = [
        _user(
            _text("the real turn"),
            _text(REMINDER),
            _text("<system-reminder>\nledger\n</system-reminder>"),
        )
    ]
    _try_cache_conversation_tail(msgs)
    assert _marked(msgs[-1])[0]["text"] == "the real turn"


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


def test_a_string_tail_that_still_contains_a_reminder_is_left_alone() -> None:
    # Defensive: older transcripts (and any caller that fuses the reminder into
    # the string) must not get a poisoned breakpoint.
    msgs = [{"role": "user", "content": "turn text" + REMINDER}]
    _try_cache_conversation_tail(msgs)
    assert isinstance(msgs[-1]["content"], str)


def test_a_tail_of_nothing_but_reminders_gets_no_marker() -> None:
    msgs = [_user(_text(REMINDER))]
    _try_cache_conversation_tail(msgs)
    assert _marked(msgs[-1]) == []


def test_empty_message_list_is_a_no_op() -> None:
    msgs: list[dict] = []
    _try_cache_conversation_tail(msgs)
    assert msgs == []


def test_empty_string_tail_is_a_no_op() -> None:
    msgs = [{"role": "user", "content": ""}]
    _try_cache_conversation_tail(msgs)
    assert msgs[-1]["content"] == ""


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


def test_the_tail_is_not_double_marked_when_it_is_the_summary() -> None:
    msgs = [
        _user(_text("[System context]: [Previous conversation summary]\nrecap")),
    ]
    _try_cache_compaction_summary(msgs)
    _try_cache_conversation_tail(msgs)
    assert _count_cache_breakpoints(msgs) == 1


def test_no_marker_is_added_once_the_message_budget_is_spent() -> None:
    msgs = [
        _user({"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}),
        _user({"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}}),
        _user(_text("c")),
    ]
    _try_cache_conversation_tail(msgs)
    assert _marked(msgs[-1]) == []


# ─── the date block ───────────────────────────────────────────────────


def test_the_system_date_block_is_stable_within_a_day() -> None:
    from bridge.tools.coordination import current_date_block

    a = current_date_block()
    b = current_date_block()
    assert a == b
    # No clock: the value must not move between two turns a minute apart.
    assert ":" not in a.split("timezone")[0]


def test_the_precise_clock_moved_to_the_reminder_seam() -> None:
    from bridge.tools.coordination import current_time_reminder

    reminder = current_time_reminder()
    assert reminder.startswith("<system-reminder>")
    assert reminder.rstrip().endswith("</system-reminder>")
    assert "Current time:" in reminder
    # It has to be recognisable as a reminder so the tail breakpoint skips it.
    from engine.anthropic_provider import _EPHEMERAL_REMINDER_MARKER

    assert _EPHEMERAL_REMINDER_MARKER in reminder


def test_the_minute_resolution_block_still_exists_for_one_shot_calls() -> None:
    # Compaction and summarizer prompts have no prefix worth preserving and
    # genuinely want the clock inline.
    from bridge.tools.coordination import current_datetime_block

    assert "The current date and time is:" in current_datetime_block()


# ─── the runner's tail-append shape ───────────────────────────────────


def test_runner_appends_reminders_as_their_own_block() -> None:
    # The provider can only place the breakpoint before the reminders if they
    # are separable, so a string tail must be split rather than concatenated.
    from engine.runner import AsyncAgentRunner
    from engine.types import Message, TextBlock

    runner = AsyncAgentRunner.__new__(AsyncAgentRunner)
    runner._current_pressure_ratio = lambda: 0.0  # type: ignore[method-assign]
    runner._build_pressure_note = lambda _r: None  # type: ignore[method-assign]
    runner._gather_extra_reminders = lambda: REMINDER  # type: ignore[method-assign]

    out = runner._augment_messages_with_pressure_note(
        [Message(role="user", content="plain string turn")]
    )
    content = out[-1].content
    assert isinstance(content, list)
    assert [type(b) for b in content] == [TextBlock, TextBlock]
    assert content[0].text == "plain string turn"
    assert content[1].text == REMINDER


def test_runner_leaves_the_transcript_message_untouched() -> None:
    from engine.runner import AsyncAgentRunner
    from engine.types import Message

    runner = AsyncAgentRunner.__new__(AsyncAgentRunner)
    runner._current_pressure_ratio = lambda: 0.0  # type: ignore[method-assign]
    runner._build_pressure_note = lambda _r: None  # type: ignore[method-assign]
    runner._gather_extra_reminders = lambda: REMINDER  # type: ignore[method-assign]

    original = Message(role="user", content="plain string turn")
    out = runner._augment_messages_with_pressure_note([original])
    assert original.content == "plain string turn"
    assert out[-1] is not original
