"""A switch_session / ensure_session must NEVER reset+restore a session that
has a turn in flight.

Incident this guards: an operator continued a Slack session inside the desktop
app, switched to another session, then switched back — and the whole
conversation vanished while new messages silently queued. Root cause:
``switch_session`` re-sends the renderer's persisted model / reasoning /
strategy, which ``ensure_session`` registered as a spurious "change". The
``if changed:`` branch then called ``existing.reset()`` (wiping the in-memory
transcript that held the entire running turn) + ``try_restore_transcript()``
(reloading the last *saved* disk snapshot). But the transcript is only
persisted at turn boundaries, so mid-turn that snapshot is stale — it's missing
the live turn. The reload clobbered the live conversation with old state.

The sibling no-change path (`_restore_persisted_transcript_if_empty`) already
short-circuited on an in-flight turn; this test pins the same protection onto
the reset/restore path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bridge.freyja_bridge import _BridgeState


class _LiveTask:
    """Stand-in for sess.pending_task while a turn is running."""

    def done(self) -> bool:
        return False


class _DoneTask:
    def done(self) -> bool:
        return True


def _make_existing(pending_task) -> SimpleNamespace:
    """A _BridgeSession-shaped stub. reset/try_restore flip flags so the test
    can assert whether the destructive path was taken."""
    flags = {"reset": False, "restore": False, "restore_if_empty": False}

    async def _try_restore() -> bool:
        flags["restore"] = True
        return True

    async def _restore_if_empty() -> bool:
        flags["restore_if_empty"] = True
        return False

    existing = SimpleNamespace(
        id="freyja_slack_T0_channel_C0_1781500298.140809",
        pending_task=pending_task,
        model_id="claude-opus-4-8",
        reasoning_level="high",
        reasoning_level_explicit=True,
        coordination_strategy="goal",
        runtime="native",
        harness_session_id="harness-abc",
        harness_adapter=None,
        reset=lambda: flags.__setitem__("reset", True),
        try_restore_transcript=_try_restore,
        _restore_persisted_transcript_if_empty=_restore_if_empty,
    )
    existing._flags = flags  # type: ignore[attr-defined]
    return existing


def _make_state(existing) -> SimpleNamespace:
    return SimpleNamespace(
        sessions={existing.id: existing},
        active_session_id="some-other-session",
    )


def test_inflight_turn_blocks_reset_and_restore():
    """The bug repro: a turn is running and the switch re-sends a *different*
    model. The session must NOT be reset/restored, and must become active."""
    existing = _make_existing(_LiveTask())
    state = _make_state(existing)

    result = asyncio.run(
        _BridgeState.ensure_session(
            state,
            existing.id,
            model_id="claude-sonnet-4-6",  # a genuine "change" mid-turn
            reasoning_level="max",
            coordination_strategy="kanban",
            runtime="harness",
        )
    )

    assert result is existing
    # The whole reconciliation was deferred — transcript untouched.
    assert existing._flags["reset"] is False
    assert existing._flags["restore"] is False
    assert existing._flags["restore_if_empty"] is False
    # Config was NOT mutated mid-turn (can't swap a streaming request).
    assert existing.model_id == "claude-opus-4-8"
    assert existing.coordination_strategy == "goal"
    assert existing.runtime == "native"
    # ...but the session still becomes the active view.
    assert state.active_session_id == existing.id


def test_no_inflight_turn_still_resets_on_real_change():
    """Regression guard: with NO turn in flight, a genuine model change must
    still reset + restore as before (the fix must not freeze normal swaps)."""
    existing = _make_existing(_DoneTask())
    state = _make_state(existing)

    result = asyncio.run(
        _BridgeState.ensure_session(
            state,
            existing.id,
            model_id="claude-sonnet-4-6",
        )
    )

    assert result is existing
    assert existing.model_id == "claude-sonnet-4-6"  # change applied
    assert existing._flags["reset"] is True  # destructive path taken
    assert existing._flags["restore"] is True
    assert state.active_session_id == existing.id


def test_no_inflight_no_change_uses_nondestructive_restore():
    """No turn, no config delta → only the gentle if-empty restore runs."""
    existing = _make_existing(_DoneTask())
    state = _make_state(existing)

    result = asyncio.run(_BridgeState.ensure_session(state, existing.id))

    assert result is existing
    assert existing._flags["reset"] is False
    assert existing._flags["restore"] is False
    assert existing._flags["restore_if_empty"] is True
    assert state.active_session_id == existing.id
