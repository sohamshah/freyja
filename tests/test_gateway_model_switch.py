"""Gateway model defaults and `/model` switching.

Three bugs pinned here:

1. Slack sessions read ~/.freyja/gateway.yaml, NOT the desktop bridge's
   FREYJA_MODEL default, so changing the bridge default left Slack on
   whatever the yaml said. The code fallback had also drifted stale.
2. `/model X` issued while a turn was running was logged as "deferred"
   and then dropped, while Slack replied "Model set to <OLD model>" —
   a success message containing the wrong value.
3. `/model <typo>` was accepted verbatim; the failure surfaced one
   message later as a provider 404.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bridge.freyja_bridge import _apply_pending_config
from bridge.gateway.config import GatewayConfig
from engine.providers import MODEL_REGISTRY


# ── 1. defaults ──────────────────────────────────────────────────────

def test_gateway_default_model_is_registered_and_current():
    """The fallback a fresh install gets must be a model that exists."""
    assert GatewayConfig.default_model in MODEL_REGISTRY


def test_load_falls_back_to_the_class_default(tmp_path, monkeypatch):
    """The literal used to be duplicated in load(); a drifted copy is how
    the fallback went stale in the first place."""
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr("bridge.gateway.config.config_path", lambda: missing)
    assert GatewayConfig.load().default_model == GatewayConfig.default_model


def test_yaml_model_overrides_the_code_default(tmp_path, monkeypatch):
    cfg_file = tmp_path / "gateway.yaml"
    cfg_file.write_text("defaults:\n  model: claude-fable-5-1\n", encoding="utf-8")
    monkeypatch.setattr("bridge.gateway.config.config_path", lambda: cfg_file)
    loaded = GatewayConfig.load()
    assert loaded.default_model == "claude-fable-5-1"
    assert loaded.default_model != GatewayConfig.default_model


# ── 2. deferred config application ───────────────────────────────────

class _FakeSession:
    """Minimal surface _apply_pending_config touches."""

    def __init__(self, model_id: str):
        self.id = "freyja:slack:T1:channel:C1:1.0"
        self.model_id = model_id
        self.reasoning_level = "high"
        self.reasoning_level_explicit = False
        self.coordination_strategy = "bus"
        self.pending_config: dict | None = None
        self.reset_calls = 0
        self.restore_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1

    async def try_restore_transcript(self) -> None:
        self.restore_calls += 1


def test_parked_model_change_is_applied_after_the_turn():
    sess = _FakeSession("kimi-k3-fast")
    sess.pending_config = {"model_id": "glm-5.3-fireworks"}

    asyncio.run(_apply_pending_config(sess))

    assert sess.model_id == "glm-5.3-fireworks"
    assert sess.pending_config is None, "the parked request must be consumed"
    # A model swap has to rebuild the session, then reload from disk.
    assert sess.reset_calls == 1 and sess.restore_calls == 1


def test_model_swap_resets_reasoning_to_the_new_model_default():
    """glm-5.3's ladder is low/high/max, so a carried-over level from a
    model with different rungs must be renormalized, not preserved."""
    sess = _FakeSession("claude-sonnet-5")
    sess.reasoning_level = "xhigh"  # valid on sonnet-5, not on glm-5.3
    sess.pending_config = {"model_id": "glm-5.3-fireworks"}

    asyncio.run(_apply_pending_config(sess))

    assert sess.reasoning_level in MODEL_REGISTRY["glm-5.3-fireworks"]["reasoning_levels"]
    assert sess.reasoning_level_explicit is False


def test_noop_change_does_not_reset_the_session():
    """Re-selecting the model you are already on must not wipe and
    reload the transcript for nothing."""
    sess = _FakeSession("kimi-k3-fast")
    sess.pending_config = {"model_id": "kimi-k3-fast"}

    asyncio.run(_apply_pending_config(sess))

    assert sess.reset_calls == 0 and sess.restore_calls == 0
    assert sess.pending_config is None


def test_empty_pending_config_is_a_noop():
    sess = _FakeSession("kimi-k3-fast")
    asyncio.run(_apply_pending_config(sess))
    assert sess.reset_calls == 0


def test_apply_never_raises_into_the_turn_loop():
    """This runs inside _run_turn_queue; an exception here would strand
    every queued message behind it."""
    sess = _FakeSession("kimi-k3-fast")
    sess.pending_config = {"model_id": "glm-5.3-fireworks"}

    def boom() -> None:
        raise RuntimeError("disk gone")

    sess.reset = boom  # type: ignore[assignment]
    asyncio.run(_apply_pending_config(sess))  # must not propagate


# ── 3. validation ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["sonnet", "glm5.3", "claude-opus-99", ""])
def test_typos_are_not_valid_models(bad):
    assert bad not in MODEL_REGISTRY


def test_close_matches_exist_for_realistic_typos():
    """The rejection message is only useful if the suggestion lands."""
    import difflib

    for typo, expected in [
        ("kimi-k3-fst", "kimi-k3-fast"),
        ("glm-5.3-firewors", "glm-5.3-fireworks"),
    ]:
        matches = difflib.get_close_matches(typo, list(MODEL_REGISTRY), n=3, cutoff=0.4)
        assert expected in matches, (typo, matches)


def test_the_command_help_examples_are_real_models():
    """The old help text advertised claude-opus-4-7 / sonnet-4-6 / gpt-5.5;
    suggesting ids is only helpful if they resolve."""
    import inspect
    import re

    from bridge.gateway.run import GatewayDaemon

    src = inspect.getsource(GatewayDaemon._handle_model_command)
    quoted = set(re.findall(r"`([^`\s]+)`", src))
    advertised = {q for q in quoted if q.startswith(("kimi", "glm", "claude", "gpt"))}
    assert advertised, "no example model ids found in the help text"
    for model in advertised:
        assert model in MODEL_REGISTRY, model
