"""Every model the bridge advertises must be constructible.

Regression guard for the GLM-5.3 launch bug: the model was in
AVAILABLE_MODELS (so the picker offered it) but build_provider had no
branch for its family, so sending a message raised "Unknown model family
for glm-5.3". A family present in the catalog with no factory branch is
exactly the silent-miss failure mode docs/ADDING-A-MODEL.md warns about.
"""

from __future__ import annotations

from bridge.freyja_bridge import AVAILABLE_MODELS, build_provider
from engine.providers import MODEL_REGISTRY

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


def test_every_advertised_model_builds_a_provider(monkeypatch):
    """One sweep over the catalog; the failure names every model that does
    not build. A new family that adds a new env var must extend _FAKE_KEYS
    (and likely build_provider) rather than fail obscurely here."""
    for key, value in _FAKE_KEYS.items():
        monkeypatch.setenv(key, value)
    advertised = {m["envVar"] for m in AVAILABLE_MODELS}
    assert advertised <= set(_FAKE_KEYS), advertised - set(_FAKE_KEYS)

    failed: dict[str, str] = {}
    for m in AVAILABLE_MODELS:
        try:
            provider = build_provider(m["id"])
        except Exception as exc:  # noqa: BLE001 — reported below, per model
            failed[m["id"]] = f"{type(exc).__name__}: {exc}"
            continue
        if provider is None:
            failed[m["id"]] = "build_provider returned None"
    assert failed == {}, f"advertised models that do not build: {failed}"


def test_catalog_and_registry_hold_the_same_models():
    """Drift either way is a silent miss. Registered but not advertised is
    runnable yet invisible in the picker (gpt-5.4-pro sat there: registered,
    priced, with a fallback chain, unselectable). Advertised but not
    registered is a picker entry that fails at send time."""
    catalog = {m["id"] for m in AVAILABLE_MODELS}
    registry = set(MODEL_REGISTRY)
    invisible = sorted(registry - catalog)
    assert invisible == [], f"registered but not in AVAILABLE_MODELS: {invisible}"
    unknown = sorted(catalog - registry)
    assert unknown == [], f"advertised but unregistered: {unknown}"
