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


@pytest.mark.parametrize("scope", ["tool_results_only", "exploration_only"])
async def test_a_condensed_result_says_how_many_images_left_the_context(tmp_path, scope):
    # A result can be a list of blocks: its text and the images a tool
    # returned. Condensing keeps only a preview of the text, and the saved
    # file holds the text alone, so the images leave the model's context.
    from engine.types import ImageBlock, TextBlock

    session = _session("read_file")
    msg = next(m for m in session.get_messages() if m.role == "tool_result")
    original = msg.content
    msg.content = [
        TextBlock(text=original),
        ImageBlock.from_base64("aGVsbG8="),
        ImageBlock.from_base64("d29ybGQ="),
    ]

    result = await _tool(session).execute("sc1", {"scope": scope})

    assert not result.is_error, result.content
    (text,) = _results(session)
    assert isinstance(text, str)
    assert "2 images removed" in text
    assert "raw message log keeps" in text
    saved = Path(SAVED.search(text).group(1))
    assert saved.read_text(encoding="utf-8") == original


async def test_a_condensed_result_without_images_says_nothing_of_them(tmp_path):
    session = _session("grep")

    await _tool(session).execute("sc1", {"scope": "tool_results_only"})

    (text,) = _results(session)
    assert "image" not in text.split("· preview:")[0]
