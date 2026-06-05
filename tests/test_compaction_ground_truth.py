"""The summarizer is seeded with the runtime ledger's confirmed actions, and
the summary template asks for a first-person 'Actions I performed' section."""

from __future__ import annotations

from types import SimpleNamespace

from engine.compaction import SummaryCompaction
from engine.session import TranscriptManager
from engine.types import Message


class _FakeProvider:
    name = "fake"
    model_id = "fake-model"

    def __init__(self) -> None:
        self.last_prompt: str | None = None

    def complete(self, messages, max_tokens, thinking=None):
        # The compactor sends a single user message holding the whole prompt.
        self.last_prompt = messages[0].content
        return SimpleNamespace(
            content=(
                "<analysis>ok</analysis><summary>Actions I performed: "
                "I created widget_tools.py (680 lines).</summary>"
            ),
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0
            ),
            model="fake-model",
        )


def _big_transcript(n: int = 24) -> TranscriptManager:
    tm = TranscriptManager()
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        tm.append_message(Message(role=role, content=f"message {i} " + ("lorem ipsum " * 40)))
    return tm


def test_compact_accepts_and_embeds_ground_truth():
    tm = _big_transcript()
    provider = _FakeProvider()
    compactor = SummaryCompaction()
    ground_truth = "- created widget_tools.py (680 lines)\n- edited runner_factory.py"

    result = compactor.compact(tm, provider, ground_truth=ground_truth)

    assert result.success, result.error
    assert provider.last_prompt is not None
    # The deterministic ledger seed is present in the summarizer prompt…
    assert "created widget_tools.py (680 lines)" in provider.last_prompt
    assert "Confirmed actions (ground truth" in provider.last_prompt
    # …and the template now asks for a first-person actions section.
    assert "Actions I performed" in provider.last_prompt


def test_compact_still_works_without_ground_truth():
    tm = _big_transcript()
    provider = _FakeProvider()
    compactor = SummaryCompaction()
    result = compactor.compact(tm, provider)  # no ground_truth
    assert result.success, result.error
    assert "Confirmed actions (ground truth" not in (provider.last_prompt or "")


def test_compaction_emits_working_memory_upserts():
    """End-to-end 2b: a <working_memory> block in the summarizer output is
    parsed and handed to the on_working_memory_upserts callback."""
    tm = _big_transcript()

    class _WMProvider(_FakeProvider):
        def complete(self, messages, max_tokens, thinking=None):
            self.last_prompt = messages[0].content
            return SimpleNamespace(
                content=(
                    "<analysis>a</analysis><summary>did the work</summary>"
                    '<working_memory>[{"type":"workstream","title":"Port",'
                    '"request":"ship widgets"},{"type":"finding",'
                    '"text":"MCP differs","workstream":"Port"}]</working_memory>'
                ),
                usage=SimpleNamespace(
                    input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0
                ),
                model="fake-model",
            )

    captured: list = []
    result = SummaryCompaction().compact(
        tm, _WMProvider(),
        on_working_memory_upserts=lambda ups: captured.append(ups),
    )
    assert result.success
    assert len(captured) == 1
    types = {u["type"] for u in captured[0]}
    assert types == {"workstream", "finding"}


def test_first_person_inject_framing_preserves_marker():
    """The reframed compaction inject still starts with the exact marker the
    provider's cache helper and the iterative-strip rely on."""
    tm = TranscriptManager()
    tm.append_message(Message(role="user", content="hi"))
    tm.append_compaction(summary="did stuff", first_kept_id=tm.entries[-1].id, tokens_before=100)
    tm.append_message(Message(role="assistant", content="continuing"))
    msgs = tm.get_messages()
    sys_msgs = [m for m in msgs if m.role == "system"]
    assert sys_msgs, "expected an injected summary system message"
    assert sys_msgs[0].content.startswith("[Previous conversation summary]")
    assert "your own earlier actions" in sys_msgs[0].content
