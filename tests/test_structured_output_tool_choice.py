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
    assert _model_rejects_forced_tool_choice("claude-fable-5")
    assert _model_rejects_forced_tool_choice("claude-fable-5-20260601")  # suffix-tolerant
    assert not _model_rejects_forced_tool_choice("claude-opus-4-8")
    assert not _model_rejects_forced_tool_choice("claude-sonnet-4-6")
    assert not _model_rejects_forced_tool_choice("")


@pytest.mark.asyncio
async def test_structured_uses_auto_for_fable(monkeypatch):
    p = _provider(monkeypatch, "claude-fable-5")
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
    assert seen == [{"type": "auto"}]  # never attempted forced
    assert res.data == {"x": "hi"}


@pytest.mark.asyncio
async def test_structured_forces_for_supported_models(monkeypatch):
    p = _provider(monkeypatch, "claude-opus-4-8")
    seen: list = []

    async def fake(**kw):
        seen.append(kw.get("tool_choice"))
        return _resp()

    p.complete_async = fake  # type: ignore[assignment]
    await p.complete_structured(
        messages=[Message(role="user", content="x")],
        schema=SCHEMA,
        schema_name="working_memory",
    )
    assert seen == [{"type": "tool", "name": "working_memory"}]  # forced


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
