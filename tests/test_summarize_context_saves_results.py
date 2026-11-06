"""A result the cheap summarize_context scopes condense can be read back whole.

``tool_results_only`` and ``exploration_only`` replace each result with a
one-line preview and make no model call. They saved nothing, so the model
could only run the tool again. Each result now goes whole to the truncator's
store (~/.freyja/truncated) first, as halving does, and the preview names
that file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bridge.tools.summarize_context_tool import SummarizeContextTool
from engine.compaction import SummaryCompaction
from engine.session import Session
from engine.types import Message, ToolCall

LONG_RESULT = "".join(f"line {i} of the output\n" for i in range(400))
SAVED = re.compile(r"full output saved to: (\S+) ")


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    # The truncator's directory is read from HOME when it is built.
    monkeypatch.setenv("HOME", str(tmp_path))


class _Provider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = 100_000


def _session(*tools: str) -> Session:
    session = Session.create(system_prompt="t")
    session.add_user_message("look around")
    for i, name in enumerate(tools):
        call = ToolCall(id=f"c{i}", name=name, arguments={})
        session.transcript.append_message(Message(role="assistant", content="", tool_calls=[call]))
        session.transcript.append_message(
            Message(role="tool_result", content=f"{i}: {LONG_RESULT}", tool_call_id=call.id)
        )
    return session


def _results(session: Session) -> list[str]:
    return [m.content for m in session.get_messages() if m.role == "tool_result"]


def _tool(session: Session) -> SummarizeContextTool:
    return SummarizeContextTool(
        get_session=lambda: session,
        get_provider=_Provider,
        get_compactor=SummaryCompaction,
    )


@pytest.mark.parametrize(
    ("scope", "condensed"),
    [("tool_results_only", [0, 1]), ("exploration_only", [0])],
)
async def test_a_condensed_result_names_the_file_that_holds_it_whole(tmp_path, scope, condensed):
    session = _session("grep", "bash")
    originals = _results(session)

    result = await _tool(session).execute("sc1", {"scope": scope})

    assert not result.is_error, result.content
    after = _results(session)
    for i, (text, original) in enumerate(zip(after, originals)):
        if i not in condensed:
            assert text == original
            continue
        assert len(text) < len(original)
        saved = Path(SAVED.search(text).group(1))
        assert saved.parent == tmp_path / ".freyja" / "truncated"
        assert saved.name.startswith(("grep_", "bash_")[i])
        assert saved.read_text(encoding="utf-8") == original
        assert "grep or sed" in text


async def test_a_result_that_could_not_be_saved_stays_whole(monkeypatch):
    session = _session("grep")
    originals = _results(session)
    tool = _tool(session)
    monkeypatch.setattr(tool, "_save_original", lambda *_a: None)

    result = await tool.execute("sc1", {"scope": "tool_results_only"})

    assert result.is_error
    assert _results(session) == originals


async def test_a_condensed_result_is_not_condensed_again(tmp_path):
    session = _session("grep")
    tool = _tool(session)
    await tool.execute("sc1", {"scope": "tool_results_only"})
    once = _results(session)

    result = await tool.execute("sc2", {"scope": "tool_results_only"})

    assert result.is_error
    assert _results(session) == once
    assert len(list((tmp_path / ".freyja" / "truncated").iterdir())) == 1
