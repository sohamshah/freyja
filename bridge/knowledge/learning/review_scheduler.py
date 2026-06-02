"""Workspace-global cadence counter for the skill-learning review nudge.

Lives on disk at ``~/.freyja/skills/.cadence.json`` so a bridge restart
doesn't reset the counter, and so that short sessions accumulate toward
a trip across session boundaries rather than each starting at 0.

The earlier in-memory per-session design caused the drafter to almost
never fire in real use: a typical operator session is 3–5 turns and the
threshold is 10, so a single session never trips. Going workspace-global
matches the operator-facing mental model — "every N turns of activity,
drafter runs" — and survives bridge restarts cleanly.

Trip rule
─────────

  · Increment once per *user turn* on any session in the workspace.
  · Trip when ``count >= threshold``. The caller (bridge) gates on
    "skills are available" before calling us; we only handle the count.
  · Reset ``count`` on trip — matches Hermes' "reset on trip, not on
    save" so a no-emit drafter pass still consumes one cadence slot.
  · ``force_trip()`` is the operator escape hatch (``/learn-this``):
    sets a one-shot flag that fires on the next tick regardless of
    count. Bypasses the disabled check too — operator override always
    works.
  · Operator can disable the automatic nudge by setting
    ``FREYJA_SKILL_NUDGE_INTERVAL <= 0``. ``force_trip()`` still works.

Concurrency
───────────
The file is read-modify-write under ``fcntl.flock(LOCK_EX)`` on POSIX.
On the rare path where two bridge processes (e.g. an old daemon still
running while the new one launches) write concurrently, the lock
serializes them. On Windows we fall back to a temp-file + replace
discipline — slightly more torn-write-prone but acceptable since multi-
bridge-process scenarios there are even rarer.

Threshold sourcing
──────────────────
The runtime threshold comes from the env var
``FREYJA_SKILL_NUDGE_INTERVAL`` (default 10) on each tick. We do not
trust the threshold stored in the file — env-var changes between
restarts should take effect immediately rather than being shadowed by
a stale persisted value. The file's ``threshold`` field is purely
informational for ``cat .cadence.json``.

API
───
:func:`make_counter(session_id)` returns a thin proxy whose
``on_turn_complete`` and ``force_trip`` methods read+write the global
file. The session_id is used only for the ``last_trip_session_id``
field in the persisted state (handy for "which session caused the
last trip" debugging).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from bridge.knowledge.learning.paths import cadence_path, ensure_loop_dirs

logger = logging.getLogger(__name__)


_ENV_VAR = "FREYJA_SKILL_NUDGE_INTERVAL"
_DEFAULT_THRESHOLD = 10


try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:
    fcntl = None  # type: ignore[assignment]


def _read_threshold_from_env() -> int:
    """Parse ``FREYJA_SKILL_NUDGE_INTERVAL`` with a safe fallback.

    Read on every tick so env-var changes between sessions in the same
    bridge take effect immediately. Cheap — os.environ.get + int().
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


def _read_state() -> dict[str, Any]:
    """Load the persisted state. Returns a fresh dict on any read error."""
    path = cadence_path()
    if not path.exists():
        return {"turns_since_last_review": 0, "last_trip_ts": 0, "last_trip_session_id": "", "forced": False}
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
            if not isinstance(data, dict):
                return {"turns_since_last_review": 0, "last_trip_ts": 0, "last_trip_session_id": "", "forced": False}
            data.setdefault("turns_since_last_review", 0)
            data.setdefault("last_trip_ts", 0)
            data.setdefault("last_trip_session_id", "")
            data.setdefault("forced", False)
            return data
    except (OSError, json.JSONDecodeError):
        return {"turns_since_last_review": 0, "last_trip_ts": 0, "last_trip_session_id": "", "forced": False}


def _write_state_atomic(state: dict[str, Any]) -> None:
    """Atomic write via tempfile + os.replace so a crash mid-write can't
    leave a half-written cadence file (which the reader would treat as
    "fresh state, counter at 0" — silently losing accumulated turns)."""
    ensure_loop_dirs()
    path = cadence_path()
    state.setdefault("threshold", _read_threshold_from_env())
    try:
        # Same directory so os.replace is atomic on the same filesystem.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=".cadence.",
            suffix=".tmp",
        ) as fp:
            json.dump(state, fp)
            fp.flush()
            os.fsync(fp.fileno())
            tmp_path = fp.name
        os.replace(tmp_path, path)
    except OSError:
        # Best-effort: a write failure means the next tick re-counts
        # from the last successful state — a single dropped tick, not
        # catastrophic.
        try:
            os.unlink(tmp_path)  # type: ignore[name-defined]
        except (OSError, NameError):
            pass


def _with_lock(callback):
    """Run ``callback(state) -> (new_state, return_value)`` under flock.

    Reads the file, calls the callback with the loaded state, writes
    back atomically, returns the callback's return value. The whole
    read-modify-write is bracketed by an exclusive flock so a partner
    bridge process can't read a stale state between our load + store.
    """
    ensure_loop_dirs()
    path = cadence_path()
    # Open (or create) the file for locking. We don't truncate — the
    # write path uses tempfile+replace, this handle is just for the
    # advisory lock.
    lock_fp = None
    try:
        # 'a+' creates if missing; doesn't truncate; positions at end.
        lock_fp = path.open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            except OSError:
                # Lock unavailable on this FS — proceed without it.
                # Window is short (a few ms) so collisions are rare.
                pass
        state = _read_state()
        new_state, return_value = callback(state)
        _write_state_atomic(new_state)
        return return_value
    finally:
        if lock_fp is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                lock_fp.close()
            except OSError:
                pass


@dataclass
class CadenceCounter:
    """Workspace-global cadence counter, backed by ``.cadence.json``.

    The session_id is recorded on trip so debugging can answer "which
    session caused the last trip." All read+write goes through the
    file; the dataclass instance itself holds no count state. Multiple
    sessions sharing the same workspace tick the same counter.
    """

    session_id: str

    @property
    def threshold(self) -> int:
        """Runtime threshold from the environment. Read on access so
        env-var changes between sessions take effect."""
        return _read_threshold_from_env()

    @property
    def count(self) -> int:
        """Current persisted turn count (read-only view)."""
        return int(_read_state().get("turns_since_last_review", 0))

    def is_disabled(self) -> bool:
        return self.threshold <= 0

    def on_turn_complete(self, *, had_user_message: bool) -> bool:
        """Increment the global counter; trip + reset when it reaches
        the threshold.

        Forced trip wins regardless of state (operator escape hatch
        always works even when the automatic nudge is disabled). After
        tripping, ``turns_since_last_review`` resets to 0 and the
        ``last_trip_ts`` / ``last_trip_session_id`` fields are updated
        for debugging.

        Returns ``True`` if the counter tripped this call. Best-effort:
        any I/O error logs and returns ``False`` (no spurious trip).
        """
        try:
            return bool(_with_lock(lambda s: self._tick(s, had_user_message)))
        except Exception:  # noqa: BLE001
            logger.exception("CadenceCounter[%s] tick failed", self.session_id)
            return False

    def _tick(self, state: dict[str, Any], had_user_message: bool) -> tuple[dict[str, Any], bool]:
        # Forced trip path: consume the flag, reset count, fire.
        if state.get("forced"):
            state["forced"] = False
            state["turns_since_last_review"] = 0
            state["last_trip_ts"] = _now_ms()
            state["last_trip_session_id"] = self.session_id
            logger.info("CadenceCounter[%s] forced trip", self.session_id)
            return state, True

        if self.is_disabled():
            return state, False

        if not had_user_message:
            return state, False

        threshold = self.threshold
        new_count = int(state.get("turns_since_last_review", 0)) + 1

        if new_count >= threshold:
            state["turns_since_last_review"] = 0
            state["last_trip_ts"] = _now_ms()
            state["last_trip_session_id"] = self.session_id
            logger.info(
                "CadenceCounter[%s] tripped at count=%d threshold=%d",
                self.session_id, new_count, threshold,
            )
            return state, True

        state["turns_since_last_review"] = new_count
        logger.debug(
            "CadenceCounter[%s] tick count=%d/%d",
            self.session_id, new_count, threshold,
        )
        return state, False

    def force_trip(self) -> None:
        """Arm a one-shot trip on the next tick. Persists the flag so
        a bridge restart between ``force_trip()`` and the next tick
        doesn't lose the operator's request.

        Note: ``/learn-this`` callers should prefer :meth:`reset_for_immediate_run`
        because they spawn the drafter inline rather than waiting for a
        tick. Mixing ``force_trip()`` + inline spawn fires the drafter
        twice (once now, once on the next tick when forced is consumed)."""
        try:
            def _set_forced(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
                state["forced"] = True
                return state, None
            _with_lock(_set_forced)
            logger.info("CadenceCounter[%s] force_trip armed", self.session_id)
        except Exception:  # noqa: BLE001
            logger.exception("CadenceCounter[%s] force_trip persist failed", self.session_id)

    def reset_for_immediate_run(self) -> None:
        """Mark a cadence cycle as consumed without firing on the next
        tick. For ``/learn-this`` callers that immediately spawn the
        drafter themselves: this resets the persisted count to 0 and
        clears any armed force_trip flag, so the next automatic trip
        is a full ``threshold`` away.

        Without this, the persistent cadence file would still trip on
        the next user turn even though the drafter just ran — wasting
        an Opus call and producing two near-simultaneous candidates."""
        try:
            def _reset(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
                state["turns_since_last_review"] = 0
                state["forced"] = False
                state["last_trip_ts"] = _now_ms()
                state["last_trip_session_id"] = self.session_id
                return state, None
            _with_lock(_reset)
            logger.debug(
                "CadenceCounter[%s] cadence reset for immediate /learn-this run",
                self.session_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("CadenceCounter[%s] reset persist failed", self.session_id)


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def make_counter(session_id: str) -> CadenceCounter:
    """Construct a counter proxy bound to this session's id.

    The counter itself is workspace-global on disk; the session_id is
    recorded into the persisted state on trip so "which session caused
    the last drafter run" is answerable from ``.cadence.json``.
    """
    return CadenceCounter(session_id=session_id)


def should_review_now(
    counter: CadenceCounter, *, had_user_message: bool
) -> bool:
    """Module-level convenience wrapper around
    :meth:`CadenceCounter.on_turn_complete`."""
    return counter.on_turn_complete(had_user_message=had_user_message)


# ── Backwards-compat / introspection ─────────────────────────────────


def read_cadence_state() -> dict[str, Any]:
    """Read-only snapshot of the persisted state. Used by ``/diag`` and
    the renderer's drafter activity strip to surface the current
    turns-since-last-review count without going through a CadenceCounter.
    """
    return _read_state()
