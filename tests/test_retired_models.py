"""A retired model id keeps working wherever something remembered it.

On 2026-10-28 the registry dropped eighteen ids: dead ones (nine Fireworks
404s, Cerebras' archived zai-glm-4.7, Google's gemini-3.1-flash) and
deprecated ones with a shutdown date (claude-sonnet-4-5, gpt-5.3-codex,
gpt-5.4-nano, the routed gemini-3.7/3.5-flash, the access-limited 2.5s).
Sessions on disk, scheduler jobs, Slack bindings, sub-agent profiles and
FREYJA_MODEL may still name any of them. Each entry point maps the id to
its replacement instead of failing at the first request.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.providers import MODEL_REGISTRY, RETIRED_MODEL_REPLACEMENTS, resolve_retired_model


# ── bridge: a saved session resumes on the replacement ───────────────────


def test_saved_session_on_a_retired_model_restores_on_its_replacement(monkeypatch):
    import bridge.freyja_bridge as fb
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(
        tp,
        "load_transcript",
        lambda sid: {"metadata": {"model_id": "gemini-3.7-flash", "reasoning_level": "medium"}},
    )
    model, level = fb._saved_model("s1")
    assert model == "gemini-3.8-flash"
    assert level == "medium"


def test_saved_session_on_an_unknown_model_falls_to_the_default(monkeypatch):
    import bridge.freyja_bridge as fb
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(
        tp, "load_transcript", lambda sid: {"metadata": {"model_id": "never-a-model"}}
    )
    assert fb._saved_model("s2") == (None, None)


def test_live_model_id_logs_the_swap(monkeypatch):
    import bridge.freyja_bridge as fb

    lines: list[str] = []
    monkeypatch.setattr(fb, "log", lambda level, msg, *a, **k: lines.append(f"{level}:{msg}"))
    assert fb._live_model_id("kimi-k2.6", where="test") == "kimi-k3"
    assert any("kimi-k2.6" in line and "kimi-k3" in line for line in lines)
    lines.clear()
    assert fb._live_model_id("kimi-k3", where="test") == "kimi-k3"
    assert lines == []


def test_gated_catalog_entry_is_listed_but_not_available(monkeypatch):
    import bridge.freyja_bridge as fb

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    rows = {m["id"]: m for m in fb._annotate_models(fb.AVAILABLE_MODELS)}
    carbon = rows["gemini-4-carbon"]
    assert carbon["available"] is False
    assert "gated" in carbon["unavailableReason"].lower()
    assert rows["gemini-4-argon"]["available"] is True
    assert "unavailableReason" not in rows["gemini-4-argon"]


# ── sub-agent profiles ───────────────────────────────────────────────────


def test_profile_naming_a_retired_model_resolves_to_the_replacement(monkeypatch):
    from bridge.tools.agent_types import AgentType, resolve_model_choice

    monkeypatch.setenv("FIREWORKS_API_KEY", "k")
    profile = AgentType(
        name="old",
        description="a profile saved before the retirement",
        usage_hint="",
        model="kimi-k2.6",
        thinking_effort="medium",
        model_fallbacks=("zai-glm-4.7",),
    )
    resolution = resolve_model_choice(profile, parent_model="claude-opus-5-5")
    assert resolution.model == "kimi-k3"
    assert "kimi-k2.6" not in resolution.candidates
    assert "zai-glm-4.7" not in resolution.candidates
    assert "glm-5.3-flash-fireworks" in resolution.candidates


def test_profile_list_dedupes_after_the_swap(monkeypatch):
    from bridge.tools.agent_types import AgentType, _model_candidates

    profile = AgentType(
        name="dup",
        description="two retired ids that map to the same model",
        usage_hint="",
        model=["kimi-k2.6", "kimi-k2.7-code", "kimi-k3"],
        thinking_effort="medium",
    )
    assert _model_candidates(profile, parent_model="x") == ["kimi-k3"]


# ── gateway ──────────────────────────────────────────────────────────────


def test_slack_inline_model_flag_accepts_a_retired_id():
    from bridge.gateway.session_router import parse_inline_session_flags

    model, _mode, _cleaned, errors, _show = parse_inline_session_flags(
        "--model gemini-2.5-flash hello"
    )
    assert errors == []
    assert model == "gemini-3.8-flash"


def test_slack_inline_model_flag_still_rejects_nonsense():
    from bridge.gateway.session_router import parse_inline_session_flags

    model, _mode, _cleaned, errors, _show = parse_inline_session_flags("--model banana hello")
    assert model is None
    assert errors and "banana" in errors[0]


# ── engine ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("old,new", sorted(RETIRED_MODEL_REPLACEMENTS.items()))
def test_every_replacement_is_registered(old, new):
    assert old not in MODEL_REGISTRY
    assert resolve_retired_model(old) in MODEL_REGISTRY


def test_create_provider_runs_the_replacement(monkeypatch):
    import engine.providers as providers

    built: list[str] = []

    def fake_single(model, **kwargs):
        built.append(model)
        return SimpleNamespace(model_id=model, name="fake")

    monkeypatch.setattr(providers, "_create_single_provider", fake_single)
    provider = providers.create_provider("gpt-5.3-codex")
    assert provider.model_id == "gpt-6.1-sol"
    assert built == ["gpt-6.1-sol"]
