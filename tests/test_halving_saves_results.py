"""A halved tool result can be read back whole.

Under context pressure, on a cold cache, the runner halves old tool results:
all but the last three until 2026-11-06, all but the last ten of 2,000
characters or more since. The marker said "Request specific sections if
needed", but nothing was saved: the model could only run the tool again. In
one week of October halving cut 1,050 results in 91 passes.

The runner now saves each result whole, where the truncator saves the results
it cuts, before it halves it, and the marker names that file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from engine.runner import AsyncAgentRunner, RunnerContext
from engine.session import Session, TranscriptManager
from engine.types import AgentConfig, Message, ToolCall

LONG_RESULT = "".join(f"line {i} of the output\n" for i in range(400))
SAVED = re.compile(r"Full output saved to: (\S+) ")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    # The truncator's directory is read from HOME when a runner is built.
    monkeypatch.setenv("HOME", str(tmp_path))


class _Provider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = 100_000


def _session_with_results(n: int, content: str = LONG_RESULT) -> Session:
    session = Session.create(system_prompt="t")
    for i in range(n):
        call = ToolCall(id=f"c{i}", name="bash", arguments={})
        session.transcript.append_message(Message(role="assistant", content="", tool_calls=[call]))
        session.transcript.append_message(
            Message(role="tool_result", content=f"{i}: {content}", tool_call_id=call.id)
        )
    return session


def _results(session: Session) -> list[str]:
    return [m.content for m in session.get_messages() if m.role == "tool_result"]


async def test_a_halved_result_names_the_file_that_holds_it_whole(tmp_path):
    runner = AsyncAgentRunner(_Provider(), config=AgentConfig(max_tokens_per_turn=50_000))
    session = _session_with_results(13)
    originals = _results(session)
    runner.usage.last_input = 22_500  # 45% of the 50k usable: past the 40% where halving starts

    await runner._ensure_context_room(session, RunnerContext())

    halved = _results(session)
    assert halved[3:] == originals[3:]
    for text, original in zip(halved[:3], originals[:3]):
        assert len(text) < len(original)
        assert "Request specific sections" not in text
        saved = Path(SAVED.search(text).group(1))
        assert saved.parent == tmp_path / ".freyja" / "truncated"
        assert saved.name.startswith("bash_")
        assert saved.read_text(encoding="utf-8") == original


async def test_halving_starts_past_40_percent_and_keeps_the_newest_10_results_whole():
    provider = _Provider()
    provider.context_window = 1_050_000  # 1M usable: the measure, not the tokenizer, decides
    runner = AsyncAgentRunner(provider, config=AgentConfig(max_tokens_per_turn=50_000))
    session = _session_with_results(14)
    originals = _results(session)

    runner.usage.last_input = 350_000  # 35% of the 1M usable
    await runner._ensure_context_room(session, RunnerContext())
    assert _results(session) == originals

    runner.usage.last_input = 450_000  # 45%
    await runner._ensure_context_room(session, RunnerContext())
    halved = _results(session)
    assert halved[4:] == originals[4:]
    assert all(len(text) < len(original) for text, original in zip(halved[:4], originals[:4]))


def test_a_result_under_2000_characters_stays_whole():
    short, long = "s" * 1_990 + "\n", "l" * 2_000 + "\n"
    session = Session.create(system_prompt="t")
    for i, content in enumerate([short, long] + [LONG_RESULT] * 10):
        call = ToolCall(id=f"c{i}", name="bash", arguments={})
        session.transcript.append_message(Message(role="assistant", content="", tool_calls=[call]))
        session.transcript.append_message(Message(role="tool_result", content=content, tool_call_id=call.id))

    assert session.transcript.prune_old_tool_results(save_original=lambda *_a: "/x") == 1

    results = _results(session)
    assert results[0] == short
    assert results[1] != long


def test_a_result_that_could_not_be_saved_stays_whole():
    session = _session_with_results(6)
    originals = _results(session)

    assert session.transcript.prune_old_tool_results(keep_recent=3, save_original=lambda *_a: None) == 0

    assert _results(session) == originals


def test_a_result_halved_before_the_marker_named_a_file_is_not_halved_again():
    old = (
        LONG_RESULT[:1_000]
        + "\n\n[Content truncated - 9,000 total chars, middle removed. "
        "Request specific sections if needed.]\n\n" + LONG_RESULT[-1_000:]
    )
    session = _session_with_results(4, content=old)
    saved: list[str] = []

    pruned = session.transcript.prune_old_tool_results(
        keep_recent=3, save_original=lambda text, _name: saved.append(text) or "/x"
    )

    assert pruned == 0 and saved == []


def test_a_result_the_marker_would_not_shorten_stays_whole(monkeypatch):
    # Under the 2,000-character floor no result gets here; with a lower one,
    # the marker's own size keeps a short result whole.
    monkeypatch.setattr("engine.session.MIN_TOOL_RESULT_CHARS_TO_HALVE", 500)
    transcript = TranscriptManager()
    for i in range(4):
        call = ToolCall(id=f"c{i}", name="bash", arguments={})
        transcript.append_message(Message(role="assistant", content="", tool_calls=[call]))
        transcript.append_message(Message(role="tool_result", content="x" * 520, tool_call_id=call.id))

    assert transcript.prune_old_tool_results(keep_recent=3, save_original=lambda *_a: "/x") == 0
