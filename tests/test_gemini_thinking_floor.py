"""gemini-3.1-pro-preview rejects thinking_level=MINIMAL (400 "Thinking level
MINIMAL is not supported for this model") — its lowest tier is LOW. The Google
provider previously sent MINIMAL whenever thinking was disabled or for
structured output, so the kanban/goal judge synthesis 400'd whenever the
cross-provider ensemble picked that model. The provider now floors the level
per model (verified live: pro→LOW, flash→MINIMAL).
"""

from __future__ import annotations

import pytest

from engine.google_provider import (
    GoogleConfig,
    GoogleProvider,
    _floor_thinking_level,
)
from engine.types import ThinkingConfig


def test_floor_thinking_level_detection():
    assert _floor_thinking_level("gemini-3.1-pro-preview") == "LOW"
    assert _floor_thinking_level("gemini-3.1-pro-preview-20260601") == "LOW"  # suffix-tolerant
    # 3.8-flash rejects MINIMAL per its model page; 3.6 still accepts.
    assert _floor_thinking_level("gemini-3.8-flash") == "LOW"
    # Gemini 4 Argon rejects MINIMAL too (probed live 2026-10-28; the
    # thinking docs list low/medium/high with medium as default). Carbon is
    # gated and unprobed, so it takes the safe floor.
    assert _floor_thinking_level("gemini-4-argon") == "LOW"
    assert _floor_thinking_level("gemini-4-carbon") == "LOW"
    assert _floor_thinking_level("gemini-3.6-flash") == "MINIMAL"
    assert _floor_thinking_level("gemini-3.5-flash-lite") == "MINIMAL"
    assert _floor_thinking_level("gemini-3.1-flash-lite") == "MINIMAL"
    assert _floor_thinking_level("") == "MINIMAL"


def _provider(model: str) -> GoogleProvider:
    # Dummy key — construction makes no network call, and we only exercise the
    # pure thinking-config builder below.
    return GoogleProvider(GoogleConfig(model=model, api_key="dummy-key"))


@pytest.mark.parametrize(
    "model,thinking,kwargs,level",
    [
        ("gemini-3.1-pro-preview", ThinkingConfig(enabled=False), {}, "LOW"),
        # complete_structured path: thinking None + force_include_thoughts=False.
        ("gemini-3.1-pro-preview", None, {"force_include_thoughts": False}, "LOW"),
        ("gemini-3.6-flash", ThinkingConfig(enabled=False), {}, "MINIMAL"),
        # Before Argon joined the floor list, the provider sent MINIMAL for
        # thinking-off and Argon answered 400 (reproduced live 2026-10-28
        # through complete_async with ThinkingConfig(enabled=False)).
        ("gemini-4-argon", ThinkingConfig(enabled=False), {}, "LOW"),
    ],
    ids=["pro-thinking-off", "pro-structured-output", "flash-thinking-off", "argon-thinking-off"],
)
def test_thinking_off_and_structured_calls_send_the_model_floor(model, thinking, kwargs, level):
    tc = _provider(model)._build_thinking_config(thinking, **kwargs)
    assert tc is not None and level in str(tc.thinking_level)


def test_default_output_cap_leaves_room_for_thinking():
    """On Gemini 4, max_output_tokens is shared with thinking: Argon at
    MEDIUM under a 48-token cap returned MAX_TOKENS and no text (probed
    2026-10-28). Callers that pass no max_tokens must not get a cap a few
    thousand tokens of thought can exhaust."""
    assert GoogleConfig().max_tokens >= 65_536
