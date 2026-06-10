"""gemini-3.1-pro-preview rejects thinking_level=MINIMAL (400 "Thinking level
MINIMAL is not supported for this model") — its lowest tier is LOW. The Google
provider previously sent MINIMAL whenever thinking was disabled or for
structured output, so the kanban/goal judge synthesis 400'd whenever the
cross-provider ensemble picked that model. The provider now floors the level
per model (verified live: pro→LOW, flash→MINIMAL).
"""

from __future__ import annotations

from engine.google_provider import (
    GoogleConfig,
    GoogleProvider,
    _floor_thinking_level,
)
from engine.types import ThinkingConfig


def test_floor_thinking_level_detection():
    assert _floor_thinking_level("gemini-3.1-pro-preview") == "LOW"
    assert _floor_thinking_level("gemini-3.1-pro-preview-20260601") == "LOW"  # suffix-tolerant
    assert _floor_thinking_level("gemini-3.5-flash") == "MINIMAL"
    assert _floor_thinking_level("gemini-2.5-flash") == "MINIMAL"
    assert _floor_thinking_level("") == "MINIMAL"


def _provider(model: str) -> GoogleProvider:
    # Dummy key — construction makes no network call, and we only exercise the
    # pure thinking-config builder below.
    return GoogleProvider(GoogleConfig(model=model, api_key="dummy-key"))


def test_disabled_thinking_floors_to_low_for_pro():
    p = _provider("gemini-3.1-pro-preview")
    tc = p._build_thinking_config(ThinkingConfig(enabled=False))
    assert tc is not None and "LOW" in str(tc.thinking_level)


def test_structured_output_floors_to_low_for_pro():
    # complete_structured path: thinking None + force_include_thoughts=False.
    p = _provider("gemini-3.1-pro-preview")
    tc = p._build_thinking_config(None, force_include_thoughts=False)
    assert tc is not None and "LOW" in str(tc.thinking_level)


def test_disabled_thinking_stays_minimal_for_flash():
    p = _provider("gemini-3.5-flash")
    tc = p._build_thinking_config(ThinkingConfig(enabled=False))
    assert tc is not None and "MINIMAL" in str(tc.thinking_level)
