"""Post-load outcome watcher.

Responsibilities:

  · On skill-load: record a ``loaded`` event so the value rollup picks
    up the count and so we have a ``load_ts`` to pair the eventual
    outcome with.
  · On turn boundary: for every skill loaded in the last few turns of
    THIS session that doesn't have an outcome yet, build the post-load
    window and ask the classifier to assign a category.
  · Persist the outcome event so the value rollup re-derives V on next
    read.

Scope intentionally narrow for MVP:

  · We only classify for skills loaded by the live session — sub-agents
    inherit the parent's outcome budget by piggy-backing on the parent's
    watcher rather than running their own (avoids double-counting and
    keeps cost predictable).
  · The window is "load turn + next 3 turns OR end of session, whichever
    first." Beyond that we treat the outcome as cold and assume `clean`
    if no negative signal landed in the window.
  · Failures are silent — losing one outcome event is acceptable, but a
    crash here cannot break the live turn loop.

What this is NOT:

  · Not a sub-process. Runs as an ``asyncio.create_task`` from the
    bridge's turn-complete handler so it doesn't block response
    delivery but stays in-process for transcript access.
  · Not a curator. We never archive, demote, or modify the skill itself
    here — purely an observer. The decay model (Phase 4) consumes this
    output but is intentionally NOT wired up in MVP so we can validate
    classification quality before letting it move skills around.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from bridge.knowledge.learning import events
from bridge.knowledge.learning.outcome_classifier import classify

logger = logging.getLogger(__name__)


# Number of post-load turns to include in the classifier's window. Set
# small (3) so the classification is about what happened right after the
# skill arrived in context — not about everything that happened the rest
# of the day. Configurable for tuning.
DEFAULT_POST_LOAD_TURNS = 3
DEFAULT_MAX_WINDOW_CHARS = 12_000  # cap raw bytes for the classifier prompt


@dataclass
class _LoadRecord:
    """One outstanding skill load awaiting classification."""

    skill_name: str
    skill_body: str
    load_ts: int
    turn_index: int             # how many turns into the session at load time
    load_context: str = ""


class SkillOutcomeWatcher:
    """One per session. Tracks loaded skills + dispatches classification.

    Lifetime: created when the first skill is loaded in a session,
    destroyed on session reset / shutdown. The bridge owns the
    instance (attached to ``_BridgeSession.skill_outcome_watcher``).
    """

    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        self._pending: list[_LoadRecord] = []
        # Skills already classified this session (or scheduled for
        # classification) — avoids redundant LLM calls if a skill is
        # loaded twice in one session.
        self._classified: set[str] = set()
        # In-flight classification tasks. Kept so shutdown can wait on
        # them (best-effort) before the session goes away.
        self._tasks: set[asyncio.Task[Any]] = set()

    # ── load-side ──

    def record_load(
        self,
        *,
        skill_name: str,
        skill_body: str,
        turn_index: int,
        load_context: str = "",
    ) -> None:
        """Called when a skill is loaded into this session.

        Idempotent within a session — a skill loaded twice produces one
        outcome record. The first load wins as the anchor turn.
        """
        if not skill_name:
            return
        if skill_name in self._classified:
            return
        if any(p.skill_name == skill_name for p in self._pending):
            return
        load_ts = int(time.time() * 1000)
        self._pending.append(
            _LoadRecord(
                skill_name=skill_name,
                skill_body=skill_body or "",
                load_ts=load_ts,
                turn_index=turn_index,
                load_context=load_context,
            )
        )
        events.append_loaded(
            skill_name,
            self.session_id,
            extra={"turn_index": turn_index, "load_context": load_context},
        )

    # ── turn-boundary trigger ──

    def on_turn_complete(
        self,
        *,
        current_turn_index: int,
        window_builder: "TurnWindowBuilder",
    ) -> None:
        """Called at the end of every turn.

        Walks ``self._pending`` and schedules a classifier task for any
        skill whose post-load window has accumulated ``DEFAULT_POST_LOAD_TURNS``
        turns of follow-up content. Tasks run on the bridge's event loop —
        they don't block the user's response, but they share the loop so
        if they're slow they hold up other low-priority work.
        """
        if not self._pending:
            return
        max_turns = DEFAULT_POST_LOAD_TURNS
        still_pending: list[_LoadRecord] = []
        for rec in self._pending:
            if current_turn_index - rec.turn_index >= max_turns:
                self._schedule_classification(rec, window_builder)
            else:
                still_pending.append(rec)
        self._pending = still_pending

    def on_session_end(self, *, window_builder: "TurnWindowBuilder") -> None:
        """Best-effort drain — schedule classification for every pending
        skill, regardless of whether the full window accumulated. Called
        on graceful session shutdown (operator /reset, app close, etc.)."""
        for rec in list(self._pending):
            self._schedule_classification(rec, window_builder)
        self._pending.clear()

    async def wait_for_drain(self, timeout: float = 60.0) -> None:
        """Optionally block until every in-flight classification has
        finished. Used by tests + by shutdown code that wants to ensure
        we don't lose outcome events to process exit."""
        tasks = list(self._tasks)
        if not tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "outcome_watcher: drain timed out after %.1fs; %d tasks still in flight",
                timeout, sum(1 for t in tasks if not t.done()),
            )

    # ── internal ──

    def _schedule_classification(self, rec: _LoadRecord, window_builder: "TurnWindowBuilder") -> None:
        if rec.skill_name in self._classified:
            return
        self._classified.add(rec.skill_name)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("outcome_watcher: no running loop — dropping classification for %s", rec.skill_name)
            return
        coro = self._classify_one(rec, window_builder)
        task = loop.create_task(coro, name=f"outcome-classify:{rec.skill_name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _classify_one(self, rec: _LoadRecord, window_builder: "TurnWindowBuilder") -> None:
        # Wrap the whole call in a stdout/stderr redirect so any chatter
        # from the provider client doesn't leak into the user's chat
        # surface — same pattern Hermes uses for background_review (see
        # docs/skill-learning-reference/artifacts/fork_construction.txt).
        window = window_builder.build_window(
            anchor_turn=rec.turn_index,
            max_turns=DEFAULT_POST_LOAD_TURNS + 1,
            max_chars=DEFAULT_MAX_WINDOW_CHARS,
        )
        if not window.strip():
            # Nothing to classify against. Log a synthetic clean so the
            # rollup still picks up the load → outcome pairing, then
            # bail.
            events.append_outcome(
                rec.skill_name,
                self.session_id,
                category="clean",
                load_ts=rec.load_ts,
                evidence="no post-load conversation captured",
            )
            return
        try:
            with open(os.devnull, "w", encoding="utf-8") as devnull, \
                 contextlib.redirect_stdout(devnull), \
                 contextlib.redirect_stderr(devnull):
                outcome = await classify(
                    skill_name=rec.skill_name,
                    skill_body=rec.skill_body,
                    post_load_window=window,
                    load_context=rec.load_context,
                )
        except Exception:
            logger.exception("outcome_watcher: classifier raised for %s", rec.skill_name)
            outcome = None
        if outcome is None:
            return  # provider error path — caller logs nothing, don't pollute the rollup
        events.append_outcome(
            rec.skill_name,
            self.session_id,
            category=outcome.category,
            load_ts=rec.load_ts,
            evidence=outcome.evidence,
            secondary=outcome.secondary,
        )


# ── Window builder protocol ───────────────────────────────────────────


class TurnWindowBuilder:
    """Adapter the watcher uses to obtain rendered conversation windows.

    The bridge implements this against its own session.messages list.
    Tests implement it against a fake.

    The contract: ``build_window(anchor_turn, max_turns, max_chars)``
    returns a plain-text rendering of turns ``[anchor_turn, anchor_turn + max_turns)``
    capped at ``max_chars``. Format details (role labels, tool-call
    summaries) are the implementer's choice but should be consistent
    enough that the classifier reads similar shapes across builders.
    """

    def build_window(self, *, anchor_turn: int, max_turns: int, max_chars: int) -> str:
        raise NotImplementedError
