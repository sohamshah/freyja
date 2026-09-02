"""Every model the bridge advertises must be constructible.

Regression guard for the GLM-5.3 launch bug: the model was in
AVAILABLE_MODELS (so the picker offered it) but build_provider had no
branch for its family, so sending a message raised "Unknown model family
for glm-5.3". A family present in the catalog with no factory branch is
exactly the silent-miss failure mode docs/ADDING-A-MODEL.md warns about.
"""

from __future__ import annotations

import pytest

from bridge.freyja_bridge import AVAILABLE_MODELS, build_provider

# Fake keys for every provider family so construction gets past the
# env-var guard. No network calls happen at construction time.
_FAKE_KEYS = {
    "ANTHROPIC_API_KEY": "dummy",
    "OPENAI_API_KEY": "dummy",
    "CEREBRAS_API_KEY": "dummy",
    "FIREWORKS_API_KEY": "dummy",
    "ZAI_API_KEY": "dummy",
    "GEMINI_API_KEY": "dummy",
}


@pytest.mark.parametrize("model_id", [m["id"] for m in AVAILABLE_MODELS])
def test_every_advertised_model_builds_a_provider(model_id, monkeypatch):
    for key, value in _FAKE_KEYS.items():
        monkeypatch.setenv(key, value)
    provider = build_provider(model_id)
    assert provider is not None


def test_catalog_env_vars_are_covered_by_fake_keys():
    """If a new family adds a new env var, extend _FAKE_KEYS (and likely
    build_provider) rather than letting the param test fail obscurely."""
    advertised = {m["envVar"] for m in AVAILABLE_MODELS}
    assert advertised <= set(_FAKE_KEYS), advertised - set(_FAKE_KEYS)
