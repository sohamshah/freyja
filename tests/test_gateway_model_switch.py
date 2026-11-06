"""Gateway model defaults and `/model` switching.

Three bugs pinned here:

1. Slack sessions read ~/.freyja/gateway.yaml, NOT the desktop bridge's FREYJA_MODEL default, so changing the bridge default left Slack on whatever the yaml said. The code fallback had also drifted stale.
2. `/model X` issued while a turn was running was logged as "deferred" and then dropped, while Slack replied "Model set to <OLD model>", a success message containing the wrong value.
3. `/model <typo>` was accepted verbatim; the failure surfaced one message later as a provider 404.
"""

from __future__ import annotations

import asyncio

import pytest

from bridge.freyja_bridge import _apply_pending_config
from bridge.gateway.config import GatewayConfig
from engine.providers import MODEL_REGISTRY

# ── 1. defaults ──────────────────────────────────────────────────────

def test_gateway_default_model_is_registered_and_current():
    """The fallback a fresh install gets must be a model that exists."""
    assert GatewayConfig.default_model in MODEL_REGISTRY


def test_load_uses_the_yaml_model_and_falls_back_to_the_class_default(tmp_path, monkeypatch):
    """The fallback literal used to be duplicated in load(); a drifted copy is how it went stale in the first place."""
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr("bridge.gateway.config.config_path", lambda: missing)
    assert GatewayConfig.load().default_model == GatewayConfig.default_model

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
    """glm-5.3's ladder is low/high/max, so a carried-over level from a model with different rungs must be renormalized, not preserved."""
    sess = _FakeSession("claude-sonnet-5")
    sess.reasoning_level = "xhigh"  # valid on sonnet-5, not on glm-5.3
    sess.pending_config = {"model_id": "glm-5.3-fireworks"}

    asyncio.run(_apply_pending_config(sess))

    assert sess.reasoning_level in MODEL_REGISTRY["glm-5.3-fireworks"]["reasoning_levels"]
    assert sess.reasoning_level_explicit is False


def test_noop_or_empty_pending_config_does_not_reset_the_session():
    """Re-selecting the model you are already on must not wipe and reload the transcript for nothing."""
    sess = _FakeSession("kimi-k3-fast")
    sess.pending_config = {"model_id": "kimi-k3-fast"}

    asyncio.run(_apply_pending_config(sess))

    assert sess.reset_calls == 0 and sess.restore_calls == 0
    assert sess.pending_config is None

    asyncio.run(_apply_pending_config(sess))  # nothing parked at all
    assert sess.reset_calls == 0


def test_apply_never_raises_into_the_turn_loop():
    """This runs inside _run_turn_queue; an exception here would strand every queued message behind it."""
    sess = _FakeSession("kimi-k3-fast")
    sess.pending_config = {"model_id": "glm-5.3-fireworks"}

    def boom() -> None:
        raise RuntimeError("disk gone")

    sess.reset = boom  # type: ignore[assignment]
    asyncio.run(_apply_pending_config(sess))  # must not propagate


async def test_a_model_picked_in_the_app_during_a_running_turn_is_reported_as_waiting(monkeypatch, tmp_path):
    """The app's own sessions: set_model said "model changed to X" with the reasoning level of the model the turn still ran on, as /model once did in Slack."""
    import bridge.freyja_bridge as fb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("session-desktop")
    turn = asyncio.get_running_loop().create_future()
    sess.pending_task = turn

    await fb._handle_command(
        state, {"type": "set_model", "sessionId": "session-desktop", "model": "glm-5.3-fireworks", "reasoningLevel": "max"},
    )
    assert sess.model_id == "kimi-k3-fast"
    (held,) = [e for e in events if e.get("subtype") == "model_changed"]
    assert held["message"] == "model will change to glm-5.3-fireworks (max) when the running turn ends"
    assert held["details"] == {"model": "glm-5.3-fireworks", "reasoningLevel": "max"}

    events.clear()
    turn.set_result(None)
    await fb._drain_after_turn(sess)
    assert (sess.model_id, sess.reasoning_level) == ("glm-5.3-fireworks", "max")
    (applied,) = [e for e in events if e.get("subtype") == "config_applied"]
    assert applied["details"]["model"] == "glm-5.3-fireworks"


async def test_a_retired_model_picked_in_the_app_during_a_running_turn_is_reported_as_waiting(monkeypatch, tmp_path):
    """ensure_session parks the replacement of a retired id; set_model compared what was parked with the retired id, and said the change was made."""
    import bridge.freyja_bridge as fb
    from engine.providers import RETIRED_MODEL_REPLACEMENTS

    assert RETIRED_MODEL_REPLACEMENTS["glm-5.2"] == "glm-5.3-fireworks"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("session-desktop")
    sess.pending_task = asyncio.get_running_loop().create_future()

    await fb._handle_command(
        state, {"type": "set_model", "sessionId": "session-desktop", "model": "glm-5.2", "reasoningLevel": "max"},
    )
    assert sess.model_id == "kimi-k3-fast"
    assert sess.pending_config["model_id"] == "glm-5.3-fireworks"
    (held,) = [e for e in events if e.get("subtype") == "model_changed"]
    assert held["message"] == "model will change to glm-5.3-fireworks (max) when the running turn ends"
    assert held["details"] == {"model": "glm-5.3-fireworks", "reasoningLevel": "max"}


async def _session_with_turns(monkeypatch, tmp_path, count: int):
    """A real bridge session whose turns run until their end is set:
    ``ends[i]`` ends turn i, and ``ran`` is the model each turn ran on."""
    import bridge.freyja_bridge as fb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "emit", lambda _event: None)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("session-desktop")
    ends = [asyncio.get_running_loop().create_future() for _ in range(count)]
    ran: list[str] = []

    async def run_turn(_content, _attachments=None, **_kw):
        ran.append(sess.model_id)
        await ends[len(ran) - 1]

    sess.run_turn = run_turn
    return fb, state, sess, ends, ran


async def _pick_model(fb, state, model: str) -> None:
    await fb._handle_command(state, {"type": "set_model", "sessionId": "session-desktop", "model": model})


async def _until(done) -> None:
    for _ in range(200):
        if done():
            return
        await asyncio.sleep(0)
    raise AssertionError("never happened")


async def test_a_model_picked_during_a_queued_turn_is_made_when_that_turn_ends(monkeypatch, tmp_path):
    """The drain made a held change before each queued turn and never after the last one, so the change waited through the next turn, which ran on the old model."""
    fb, state, sess, ends, ran = await _session_with_turns(monkeypatch, tmp_path, 3)
    fb._schedule_or_queue_turn(sess, "first")
    fb._schedule_or_queue_turn(sess, "second")  # queued behind the first
    ends[0].set_result(None)
    await _until(lambda: len(ran) == 2)
    await _pick_model(fb, state, "glm-5.3-fireworks")  # held for the queued turn
    ends[1].set_result(None)
    await sess.pending_task
    assert sess.model_id == "glm-5.3-fireworks" and not sess.pending_config
    fb._schedule_or_queue_turn(sess, "third")
    await _until(lambda: len(ran) == 3)
    ends[2].set_result(None)
    await sess.pending_task
    assert ran == ["kimi-k3-fast", "kimi-k3-fast", "glm-5.3-fireworks"]


async def test_a_model_picked_with_no_turn_running_is_not_overridden_by_an_older_held_one(monkeypatch, tmp_path):
    """The held change outlived the drain. A model picked after it, between turns, was made at once, and the older held change was made over it when the next turn ended."""
    fb, state, sess, ends, ran = await _session_with_turns(monkeypatch, tmp_path, 3)
    fb._schedule_or_queue_turn(sess, "first")
    fb._schedule_or_queue_turn(sess, "second")
    ends[0].set_result(None)
    await _until(lambda: len(ran) == 2)
    await _pick_model(fb, state, "glm-5.3-fireworks")
    ends[1].set_result(None)
    await sess.pending_task
    await _pick_model(fb, state, "claude-opus-5-5")  # no turn running: made at once
    assert sess.model_id == "claude-opus-5-5"
    fb._schedule_or_queue_turn(sess, "third")
    await _until(lambda: len(ran) == 3)
    ends[2].set_result(None)
    await sess.pending_task
    assert sess.model_id == "claude-opus-5-5"


async def test_a_model_picked_while_the_drain_makes_a_held_change_is_made_too(monkeypatch, tmp_path):
    """Making a held change awaits (the transcript load, the Slack note, which waits for the reply's seal). A change picked then was held, and left held when the drain ended."""
    fb, state, sess, ends, ran = await _session_with_turns(monkeypatch, tmp_path, 1)

    async def note(_sess, _text):
        if sess.model_id == "glm-5.3-fireworks":
            await _pick_model(fb, state, "claude-opus-5-5")

    state.thread_notice = note
    fb._schedule_or_queue_turn(sess, "first")
    await _until(lambda: len(ran) == 1)
    await _pick_model(fb, state, "glm-5.3-fireworks")
    ends[0].set_result(None)
    await sess.pending_task
    assert sess.model_id == "claude-opus-5-5" and not sess.pending_config


async def _stop(fb, state) -> None:
    await fb._handle_command(state, {"type": "cancel", "sessionId": "session-desktop", "scope": "turn"})


@pytest.mark.parametrize("turns", [1, 2], ids=["after-the-turn", "after-the-last-queued-turn"])
async def test_a_model_picked_while_a_stop_cuts_the_drain_short_is_made_too(monkeypatch, tmp_path, turns):
    """A stop while the drain made a held change (its Slack note waits for the reply's seal) ended the drain there, and a change picked meanwhile stayed held into the next turn. A change held during a queued turn is made as the drain ends, and a stop there was the same."""
    fb, state, sess, ends, ran = await _session_with_turns(monkeypatch, tmp_path, turns)
    posting = asyncio.Event()

    async def note(_sess, text):
        if "glm-5.3-fireworks" in text:
            posting.set()
            await asyncio.sleep(5)  # the reply is still being sealed

    state.thread_notice = note
    for n in range(turns):
        fb._schedule_or_queue_turn(sess, f"turn {n}")
    for end in ends[:-1]:
        end.set_result(None)
    await _until(lambda: len(ran) == turns)
    await _pick_model(fb, state, "glm-5.3-fireworks")
    ends[-1].set_result(None)
    await asyncio.wait_for(posting.wait(), timeout=2)
    await _pick_model(fb, state, "claude-opus-5-5")  # held: the drain is running
    await _stop(fb, state)
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    assert sess.model_id == "claude-opus-5-5" and not sess.pending_config


async def test_a_stop_while_the_drain_makes_a_held_change_between_queued_turns_still_runs_them(monkeypatch, tmp_path):
    """The drain makes a held change before each queued turn. A stop there ended the drain: the turn it had taken off the queue never ran, and the turns behind it waited for the next message. The stop is for the turn that ended, as a stop of a queued turn is."""
    fb, state, sess, ends, ran = await _session_with_turns(monkeypatch, tmp_path, 4)
    posting = asyncio.Event()

    async def note(_sess, text):
        if "glm-5.3-fireworks" in text:
            posting.set()
            await asyncio.sleep(5)  # the reply is still being sealed

    state.thread_notice = note
    for n in range(4):
        fb._schedule_or_queue_turn(sess, f"turn {n}")
    ends[0].set_result(None)
    await _until(lambda: len(ran) == 2)
    await _pick_model(fb, state, "glm-5.3-fireworks")  # held for the next queued turn
    for end in ends[1:]:
        end.set_result(None)
    await asyncio.wait_for(posting.wait(), timeout=2)
    await _stop(fb, state)  # between turn 1 and turn 2
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    assert ran == ["kimi-k3-fast", "kimi-k3-fast", "glm-5.3-fireworks", "glm-5.3-fireworks"]
    assert sess.queued_messages == []


async def test_a_stop_while_the_drain_makes_a_held_change_before_late_follow_ups_hands_them_back(monkeypatch, tmp_path):
    """Follow-ups that land after a turn's last boundary run as the next turn. The drain promoted them (the app shows them as sent), then made the held change. A stop there ended the drain: they never ran and were not handed back. The change is made first, so the stop hands them back to the composer, as it does every follow-up still waiting."""
    fb, state, sess, ends, ran = await _session_with_turns(monkeypatch, tmp_path, 2)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    posting = asyncio.Event()

    async def note(_sess, text):
        if "glm-5.3-fireworks" in text:
            posting.set()
            await asyncio.sleep(5)  # the reply is still being sealed

    state.thread_notice = note
    fb._schedule_or_queue_turn(sess, "first")
    await _until(lambda: len(ran) == 1)
    sess.push_operator_followup("and the tests", None, client_id="c1")
    ends[0].set_result(None)
    await _until(lambda: len(ran) == 2)  # the follow-up's turn
    await _pick_model(fb, state, "glm-5.3-fireworks")  # held for the next turn
    sess.push_operator_followup("and the docs", None, client_id="c2")  # after its last boundary
    ends[1].set_result(None)
    await asyncio.wait_for(posting.wait(), timeout=2)
    await _stop(fb, state)
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    assert ran == ["kimi-k3-fast", "kimi-k3-fast"]
    promoted = [i["clientId"] for e in events if e["type"] == "followups_promoted" for i in e["items"]]
    assert "c2" not in promoted
    (back,) = [e for e in events if e["type"] == "followup_withdrawn"]
    assert (back["clientId"], back["content"], back["restore"]) == ("c2", "and the docs", True)


async def test_a_model_picked_while_a_stopped_session_waits_to_wake_is_made_when_stopped_again(monkeypatch, tmp_path):
    """After a stop the drain waits a moment before waking for an agent's message. A second stop there ended the drain, and a change picked during the wait stayed held into the next turn."""
    from bridge.inbox import InboxMessage, new_message_id

    fb, state, sess, ends, ran = await _session_with_turns(monkeypatch, tmp_path, 1)
    monkeypatch.setattr(fb, "TALK_WAKE_AFTER_STOP_S", 5)
    fb._schedule_or_queue_turn(sess, "first")
    await _until(lambda: len(ran) == 1)
    sess.inbox.push(InboxMessage(
        id=new_message_id(), from_session="sub_x", from_label="worker",
        from_role="agent", content="tests pass",
    ))
    await _stop(fb, state)
    await asyncio.sleep(0.05)  # the drain is waiting to wake
    await _pick_model(fb, state, "glm-5.3-fireworks")  # held: the drain is running
    await _stop(fb, state)
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    assert sess.model_id == "glm-5.3-fireworks" and not sess.pending_config
    assert ran == ["kimi-k3-fast"]


# ── 3. validation ────────────────────────────────────────────────────

def test_the_command_help_examples_are_real_models():
    """The old help text advertised claude-opus-4-7 / sonnet-4-6 / gpt-5.5; suggesting ids is only helpful if they resolve."""
    import inspect
    import re

    from bridge.gateway.run import GatewayDaemon

    src = inspect.getsource(GatewayDaemon._handle_model_command)
    quoted = set(re.findall(r"`([^`\s]+)`", src))
    advertised = {q for q in quoted if q.startswith(("kimi", "glm", "claude", "gpt"))}
    assert advertised, "no example model ids found in the help text"
    for model in advertised:
        assert model in MODEL_REGISTRY, model
