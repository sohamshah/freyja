"""Cadence counter for the skill-learning review nudge.

One :class:`CadenceCounter` lives on each ``_BridgeSession`` (see
``bridge.freyja_bridge``). The bridge constructs it at session start and
ticks it via :meth:`CadenceCounter.on_turn_complete` at every
``turn_complete``. When the counter trips, the bridge spawns a
background skill-review pass.

Trip rule (modeled on Hermes' pattern — see
``docs/skill-learning-reference/artifacts/cadence_counters.txt``):

  · Increment once per user turn.
  · Trip when ``count >= threshold`` AND ``skill_manage``-style tools are
    available. We don't have a tool registry to inspect here, so the
    caller (the bridge) is responsible for the availability check —
    ``on_turn_complete`` only handles the count side. The bridge skips
    calling us at all if no skills are present in the system prompt.
  · Reset ``count`` on trip — even if the LLM ends up emitting no
    candidate, the cadence resets and the next ``threshold`` turns start
    fresh. This matches Hermes' "reset on trip, not on save" behavior.
  · Operator can disable by setting ``threshold <= 0``.

Unlike Hermes we have a persistent ``_BridgeSession`` per conversation,
so the gateway-hydration trick (back-filling count from conversation
history because a fresh agent gets spawned per inbound message) is not
needed here.

Configuration
─────────────
Default threshold is 10 user turns. Override with the
``FREYJA_SKILL_NUDGE_INTERVAL`` env var (read once at counter
construction via :func:`make_counter`). Set to 0 or negative to disable
the automatic nudge entirely — operators can still trigger a review by
hand via :meth:`CadenceCounter.force_trip` (the ``/learn-this`` slash
command).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Env var name and default threshold. Hermes' equivalent is
# `MEMORY_NUDGE_INTERVAL` / `SKILL_NUDGE_INTERVAL`; we collapse to a
# single skill-only knob since the MVP does not do memory review.
_ENV_VAR = "FREYJA_SKILL_NUDGE_INTERVAL"
_DEFAULT_THRESHOLD = 10


def _read_threshold_from_env() -> int:
    """Parse ``FREYJA_SKILL_NUDGE_INTERVAL`` with a safe fallback.

    Best-effort: a malformed value logs a warning and falls back to the
    default rather than crashing the session.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None or raw == "":
        return _DEFAULT_THRESHOLD
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r, falling back to default %d",
            _ENV_VAR,
            raw,
            _DEFAULT_THRESHOLD,
        )
        return _DEFAULT_THRESHOLD


@dataclass
class CadenceCounter:
    """Per-session counter that trips every ``threshold`` user turns.

    Attributes
    ──────────
    session_id:
        The owning ``_BridgeSession`` id. Used only for logging — the
        counter itself is local in-memory state.
    threshold:
        Number of user turns between trips. ``<= 0`` disables the
        automatic nudge (see :meth:`is_disabled`).
    count:
        Turns accumulated since the last trip. Reset to 0 on trip.
    """

    session_id: str
    threshold: int = _DEFAULT_THRESHOLD
    count: int = 0
    # One-shot "trip on next call regardless of count" flag set by
    # :meth:`force_trip`. Kept out of the public dataclass surface via
    # ``field(repr=False)`` so it doesn't leak into operator-facing
    # repr strings.
    _forced: bool = field(default=False, repr=False)

    def is_disabled(self) -> bool:
        """Operator-disabled when threshold <= 0.

        A disabled counter still honors :meth:`force_trip` — the
        operator escape hatch should keep working even if the automatic
        nudge is turned off.
        """
        return self.threshold <= 0

    def on_turn_complete(self, *, had_user_message: bool) -> bool:
        """Tick the counter for one ``turn_complete`` event.

        Parameters
        ──────────
        had_user_message:
            Whether this turn included a fresh user message. Tool-only
            turns or system-injected turns don't tick. This mirrors
            Hermes' choice to count user turns, not iterations.

        Returns
        ───────
        ``True`` if the counter tripped (and was reset). The caller
        spawns the background review on ``True``. ``False`` otherwise.
        """
        # Forced trip wins regardless of state — consume the flag, reset
        # the counter, and fire. This keeps `/learn-this` snappy: one
        # call, one trip, no off-by-one.
        if self._forced:
            self._forced = False
            self.count = 0
            logger.debug(
                "CadenceCounter[%s] forced trip", self.session_id
            )
            return True

        if self.is_disabled():
            return False

        if not had_user_message:
            return False

        self.count += 1
        if self.count >= self.threshold:
            logger.debug(
                "CadenceCounter[%s] tripped at count=%d threshold=%d",
                self.session_id,
                self.count,
                self.threshold,
            )
            self.count = 0
            return True
        return False

    def force_trip(self) -> None:
        """Arm a one-shot trip for the next :meth:`on_turn_complete` call.

        Used by the operator-issued ``/learn-this`` slash command to
        bypass the cadence and request a review immediately at the end
        of the current turn. Idempotent: calling it twice still trips
        once.
        """
        self._forced = True
        logger.debug(
            "CadenceCounter[%s] force_trip armed", self.session_id
        )


def make_counter(session_id: str) -> CadenceCounter:
    """Construct a counter, reading the threshold from the environment.

    Called once per ``_BridgeSession`` at session init. The env var is
    read at construction (not on every tick) so an operator changing it
    mid-session takes effect on the next session, which matches the
    rest of Freyja's config-on-startup convention.
    """
    return CadenceCounter(
        session_id=session_id,
        threshold=_read_threshold_from_env(),
    )


def should_review_now(
    counter: CadenceCounter, *, had_user_message: bool
) -> bool:
    """Module-level convenience wrapper around
    :meth:`CadenceCounter.on_turn_complete`.

    Provided so call sites that already pass the counter around as data
    don't have to import the class just to tick it. Semantically
    identical to ``counter.on_turn_complete(had_user_message=...)``.
    """
    return counter.on_turn_complete(had_user_message=had_user_message)
