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


def test_every_registered_model_is_selectable():
    """The other direction of drift: a model can be fully wired in the
    engine (registry + pricing + fallbacks) yet missing from the bridge
    catalog, which makes it runnable but invisible in the picker.

    gpt-5.4-pro sat in exactly that state — registered, priced, with a
    fallback chain, and unselectable — because the parametrized test
    above iterates the CATALOG, so a model absent from it is simply
    never checked.
    """
    from engine.providers import MODEL_REGISTRY

    catalog = {m["id"] for m in AVAILABLE_MODELS}
    missing = sorted(set(MODEL_REGISTRY) - catalog)
    assert not missing, (
        f"registered but not in AVAILABLE_MODELS (invisible in the picker): {missing}"
    )


def test_catalog_models_are_all_registered():
    """And the inverse: advertising a model the engine cannot resolve
    gives the operator a picker entry that fails at send time."""
    from engine.providers import MODEL_REGISTRY

    catalog = {m["id"] for m in AVAILABLE_MODELS}
    unknown = sorted(catalog - set(MODEL_REGISTRY))
    assert not unknown, f"advertised but unregistered: {unknown}"


def test_every_catalog_model_has_reasoning_metadata():
    """Missing metadata leaves the reasoning selector empty or wrong —
    gpt-5.4-pro was missing this too."""
    from bridge.freyja_bridge import MODEL_REASONING_META

    thinking_models = {m["id"] for m in AVAILABLE_MODELS if m.get("thinking")}
    missing = sorted(thinking_models - set(MODEL_REASONING_META))
    assert not missing, f"thinking models with no reasoning metadata: {missing}"
