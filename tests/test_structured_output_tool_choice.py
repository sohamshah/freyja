"""complete_structured must not hard-depend on forced tool_choice: claude-fable-5
400s on it ("tool_choice forces tool use is not compatible with this model")
but calls a single tool reliably under "auto". Verified empirically against the
live API; these tests lock in the selection + retry logic with mocks.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.anthropic_provider import (
    AnthropicConfig,
    AnthropicProvider,
    _model_rejects_forced_tool_choice,
)
from engine.providers import ProviderError
from engine.types import Message

SCHEMA = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}


def _resp(name: str = "working_memory", args: dict | None = None):
    return SimpleNamespace(
        tool_calls=[SimpleNamespace(name=name, arguments=args or {"x": "hi"})],
        content="",
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        model="m",
    )


def _provider(monkeypatch, model: str) -> AnthropicProvider:
    # Construction creates SDK clients but makes no network call; a dummy key is
    # enough, and we replace complete_async below so nothing hits the API.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    return AnthropicProvider(AnthropicConfig(model=model))


def test_model_rejects_forced_tool_choice_detection():
    """Substring match: dated, `-fast` and point-release ids inherit the
    quirk (a miss is a 400 on every structured-output call), while
    "claude-sonnet-5-5" must not catch the still-supported "claude-sonnet-5"."""
    for model in (
        "claude-fable-5",
        "claude-fable-5-1",
        "claude-fable-5-20260601",
        "claude-opus-5-5-fast",
        "claude-sonnet-5-5",
    ):
        assert _model_rejects_forced_tool_choice(model), model
    for model in ("claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6", ""):
        assert not _model_rejects_forced_tool_choice(model), model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-fable-5", [{"type": "auto"}]),
        ("claude-opus-4-8", [{"type": "tool", "name": "working_memory"}]),
    ],
    ids=["fable-goes-auto-first-time", "opus-is-forced"],
)
async def test_structured_picks_tool_choice_per_model(monkeypatch, model, expected):
    p = _provider(monkeypatch, model)
    seen: list = []

    async def fake(**kw):
        seen.append(kw.get("tool_choice"))
        return _resp()

    p.complete_async = fake  # type: ignore[assignment]
    res = await p.complete_structured(
        messages=[Message(role="user", content="x")],
        schema=SCHEMA,
        schema_name="working_memory",
    )
    assert seen == expected
    assert res.data == {"x": "hi"}


@pytest.mark.asyncio
async def test_structured_retries_auto_on_forced_rejection(monkeypatch):
    # A model NOT pre-listed that still rejects forced use → retry once with auto.
    p = _provider(monkeypatch, "claude-opus-4-8")
    seen: list = []
    n = {"calls": 0}

    async def fake(**kw):
        seen.append(kw.get("tool_choice"))
        n["calls"] += 1
        if n["calls"] == 1:
            raise ProviderError(
                "Error code: 400 - tool_choice forces tool use is not "
                "compatible with this model."
            )
        return _resp()

    p.complete_async = fake  # type: ignore[assignment]
    res = await p.complete_structured(
        messages=[Message(role="user", content="x")],
        schema=SCHEMA,
        schema_name="working_memory",
    )
    assert seen == [{"type": "tool", "name": "working_memory"}, {"type": "auto"}]
    assert res.data == {"x": "hi"}


@pytest.mark.asyncio
async def test_structured_unrelated_provider_error_propagates(monkeypatch):
    p = _provider(monkeypatch, "claude-opus-4-8")

    async def fake(**kw):
        raise ProviderError("Error code: 529 - overloaded")

    p.complete_async = fake  # type: ignore[assignment]
    with pytest.raises(ProviderError):
        await p.complete_structured(
            messages=[Message(role="user", content="x")],
            schema=SCHEMA,
            schema_name="working_memory",
        )
