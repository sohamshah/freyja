"""Post-load outcome watcher — persistent pending classifications.

Each skill load produces a record at
``~/.freyja/skills/.pending/<load_id>.json`` which accumulates the
post-load conversation window as turns happen. When the window has
absorbed ``DEFAULT_POST_LOAD_TURNS`` turns OR the session ends OR the
bridge restarts and the session is gone, the classifier runs against
whatever window we captured. Pending files survive bridge restart and
session end — losing one outcome event is acceptable; silently losing
*every* outcome in a short-session workflow (which is what the prior
in-memory design did) breaks the whole measurement loop.

Lifecycle
─────────
  · ``record_load`` writes a fresh pending record.
  · ``on_turn_complete`` rebuilds the live post-load window via the
    bridge's ``window_builder`` and overwrites the persisted record. On
    the Nth post-load turn, ``ready`` flips true and a classifier task
    spawns. On success the pending file is deleted and an ``outcome``
    event lands in ``.events.jsonl``.
  · ``on_session_end`` (graceful shutdown / session reset) flushes the
    live window and dispatches whatever's pending regardless of turn
    count — best-effort drain.
  · ``resume_pending_classifications`` (module function, called once
    at bridge startup) scans every pending file. Files with at least
    one captured post-load turn get classified immediately (their owning
    session is gone); empty files older than the TTL get deleted.

What did NOT change
───────────────────
  · Sub-agents still skip the loop (handled by the bridge's
    ``_tick_skill_learning_hooks`` short-circuit, not here).
  · `_classified` (per-session dedup) stays in-memory — same-session
    reload dedup is per-session-correct by definition.
  · Classifier failure path: leave the pending file in place so a future
    tick (or the next bridge startup) retries. M12-equivalent durable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from bridge.knowledge.learning import events
from bridge.knowledge.learning.outcome_classifier import classify
from bridge.knowledge.learning.paths import (
    ensure_loop_dirs,
    pending_dir,
    safe_skill_filename,
)

logger = logging.getLogger(__name__)


# Number of post-load turns to include in the classifier's window. Set
# small (3) so the classification is about what happened right after the
# skill arrived in context — not about everything that happened the rest
# of the day. Configurable for tuning.
DEFAULT_POST_LOAD_TURNS = 3
DEFAULT_MAX_WINDOW_CHARS = 12_000  # cap raw bytes for the classifier prompt

# A pending file with zero captured post-load turns that's older than
# this gets deleted at startup. The classifier has nothing to work with
# anyway; keeping it forever is just disk noise.
_EMPTY_PENDING_TTL_MS = 24 * 60 * 60 * 1000  # 24 hours


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── Persistence helpers ──────────────────────────────────────────────


def _pending_path(load_id: str) -> Path:
    return pending_dir() / f"{load_id}.json"


def _make_load_id(skill_name: str, session_id: str, load_ts: int) -> str:
    """Deterministic-ish id: skill + session shard + ts + random nibble.

    Including the skill name + session shard makes the file list
    human-skimmable; the random nibble guarantees uniqueness even if
    two loads of the same skill land in the same millisecond (rare but
    possible in tests)."""
    skill = safe_skill_filename(skill_name)
    sess_shard = safe_skill_filename(session_id)[:16]
    suffix = uuid.uuid4().hex[:6]
    return f"{skill}__{sess_shard}__{load_ts}_{suffix}"


def _write_pending_atomic(record: dict[str, Any]) -> None:
    """Atomic write via tempfile + os.replace. Skipped silently on I/O
    failure — the next tick re-renders the window from the live session,
    so a dropped write costs one tick's worth of context."""
    ensure_loop_dirs()
    path = _pending_path(record["load_id"])
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=".pending.",
            suffix=".tmp",
        ) as fp:
            json.dump(record, fp, ensure_ascii=False)
            fp.flush()
            try:
                os.fsync(fp.fileno())
            except OSError:
                pass
            tmp_path = fp.name
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)  # type: ignore[name-defined]
        except (OSError, NameError):
            pass


def _read_pending(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _delete_pending(load_id: str) -> None:
    try:
        _pending_path(load_id).unlink()
    except OSError:
        pass


def _iter_pending_files() -> Iterator[Path]:
    d = pending_dir()
    if not d.exists():
        return
    try:
        for p in d.iterdir():
            if p.suffix == ".json" and not p.name.startswith("."):
                yield p
    except OSError:
        return


# ── Per-session watcher ──────────────────────────────────────────────


@dataclass
class _LoadHandle:
    """In-session reference to a persisted pending record.

    The full record lives in ``.pending/<load_id>.json``; this is the
    handle the watcher uses to find it on each turn tick without
    re-walking the directory."""

    load_id: str
    skill_name: str
    load_turn_index: int


class SkillOutcomeWatcher:
    """One per session. Records loads, updates persisted windows on each
    tick, and dispatches the classifier when the post-load window closes.

    The watcher's lifetime is the session's lifetime, but the *pending
    records it produces* outlive the session — they sit on disk under
    ``.pending/`` until classified or TTL-deleted. That's the entire
    point of the rewrite: a 3-turn session that loaded a skill at
    turn 1 now produces a classification even if the session dies at
    turn 2 (resume at next bridge startup).
    """

    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        # Per-session dedup: classify each skill at most once per
        # session, so a same-session reload doesn't double-spend.
        self._classified: set[str] = set()
        # Live handles to pending files we own (created this session).
        self._handles: list[_LoadHandle] = []
        # In-flight classifier tasks. Kept so shutdown can wait on
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

        Logs the load event (so the rollup load_count reflects reality)
        and writes a fresh pending record to disk. A same-session
        reload of an already-classified or already-pending skill is
        logged but does NOT create a second pending record (preserves
        the per-skill-per-session dedup that the old in-memory design
        had).
        """
        if not skill_name:
            return
        load_ts = _now_ms()
        # Always log the load — it's our only signal that the skill
        # entered context this session, and the rollup's load_count
        # depends on it being honest about repeats.
        events.append_loaded(
            skill_name,
            self.session_id,
            extra={"turn_index": turn_index, "load_context": load_context},
        )
        # Dedup: don't queue a second classification if we've already
        # classified this skill in this session, or if a pending record
        # is already in flight for it.
        if skill_name in self._classified:
            return
        if any(h.skill_name == skill_name for h in self._handles):
            return
        load_id = _make_load_id(skill_name, self.session_id, load_ts)
        record = {
            "load_id": load_id,
            "skill_name": skill_name,
            "skill_body": (skill_body or "")[:64_000],  # safety cap; classifier truncates anyway
            "session_id": self.session_id,
            "load_ts": load_ts,
            "load_turn_index": turn_index,
            "load_context": load_context,
            "post_load_window": "",
            "post_load_turn_count": 0,
            "ready": False,
            "created_at": load_ts,
        }
        _write_pending_atomic(record)
        self._handles.append(_LoadHandle(
            load_id=load_id,
            skill_name=skill_name,
            load_turn_index=turn_index,
        ))
        logger.debug(
            "outcome_watcher[%s]: queued pending classification for %s (load_id=%s)",
            self.session_id, skill_name, load_id,
        )

    # ── turn-boundary trigger ──

    def on_turn_complete(
        self,
        *,
        current_turn_index: int,
        window_builder: "TurnWindowBuilder",
    ) -> None:
        """End-of-turn tick.

        For every live handle: re-render the post-load window from the
        session and persist it. When the window has absorbed
        ``DEFAULT_POST_LOAD_TURNS`` turns, mark ready and schedule the
        classifier. Persisting the window on each tick is the durability
        invariant: if the bridge crashes after this returns, the latest
        window is on disk.
        """
        if not self._handles:
            return
        max_turns = DEFAULT_POST_LOAD_TURNS
        still_live: list[_LoadHandle] = []
        for h in self._handles:
            if h.skill_name in self._classified:
                # Stale handle (classified by a parallel session sharing
                # the workspace, or by the resume path). Drop it.
                continue
            try:
                self._update_pending_window(h, current_turn_index, window_builder)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "outcome_watcher[%s]: failed to update pending window for %s",
                    self.session_id, h.skill_name,
                )
                still_live.append(h)
                continue
            elapsed = current_turn_index - h.load_turn_index
            if elapsed >= max_turns:
                self._mark_ready_and_dispatch(h)
            else:
                still_live.append(h)
        self._handles = still_live

    def on_session_end(self, *, window_builder: "TurnWindowBuilder") -> None:
        """Best-effort drain — flush the live window once more and
        schedule classification for every pending skill, regardless of
        whether the full window accumulated. Called on graceful session
        shutdown (operator /reset, app close, etc.)."""
        # Use a conservative "current turn" estimate: the highest
        # load_turn_index + max_turns so the renderer captures the tail.
        synthetic_now = max(
            (h.load_turn_index + DEFAULT_POST_LOAD_TURNS for h in self._handles),
            default=0,
        )
        for h in list(self._handles):
            try:
                self._update_pending_window(h, synthetic_now, window_builder)
            except Exception:  # noqa: BLE001
                pass
            self._mark_ready_and_dispatch(h)
        self._handles.clear()

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

    def _update_pending_window(
        self,
        h: _LoadHandle,
        current_turn_index: int,
        window_builder: "TurnWindowBuilder",
    ) -> None:
        record = _read_pending(_pending_path(h.load_id))
        if record is None:
            # File vanished — someone else deleted it (resume path?).
            # Drop the handle on the next sweep by raising; on_turn_complete
            # treats the exception as transient and re-adds to still_live.
            # That's fine — next tick we'll regenerate or stay quiet.
            return
        elapsed = max(0, current_turn_index - h.load_turn_index)
        # Render a window from load_turn_index covering elapsed+1 turns
        # (the load turn itself + every turn since). The +1 ensures the
        # load turn is in the window even when elapsed=0.
        try:
            window = window_builder.build_window(
                anchor_turn=h.load_turn_index,
                max_turns=elapsed + 1,
                max_chars=DEFAULT_MAX_WINDOW_CHARS,
            )
        except Exception:  # noqa: BLE001
            window = record.get("post_load_window", "")
        record["post_load_window"] = (window or "")[:DEFAULT_MAX_WINDOW_CHARS]
        record["post_load_turn_count"] = elapsed
        if elapsed >= DEFAULT_POST_LOAD_TURNS:
            record["ready"] = True
        _write_pending_atomic(record)

    def _mark_ready_and_dispatch(self, h: _LoadHandle) -> None:
        # M12: do NOT mark _classified here. Marking before the
        # classifier finishes means a provider failure permanently skips
        # the skill — the next reload won't re-enqueue (record_load
        # checks _classified). Mark only on success inside _classify_one.
        if h.skill_name in self._classified:
            return
        record = _read_pending(_pending_path(h.load_id))
        if record is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "outcome_watcher: no running loop — leaving %s on disk for next startup",
                h.skill_name,
            )
            return
        coro = self._classify_one(record)
        task = loop.create_task(coro, name=f"outcome-classify:{h.skill_name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _classify_one(self, record: dict[str, Any]) -> None:
        skill_name = record.get("skill_name", "")
        window = (record.get("post_load_window") or "").strip()
        if not window:
            # M10: empty window — drop the event entirely rather than
            # synthesizing a "clean" outcome. Leaving the pending file
            # in place is also wrong (it'd retry forever). Delete and
            # move on — the load event is already in events.jsonl so
            # the rollup still sees the load without an outcome.
            logger.debug(
                "outcome_watcher: empty post-load window for %s — deleting pending without classification",
                skill_name,
            )
            _delete_pending(record.get("load_id", ""))
            return
        try:
            outcome = await classify(
                skill_name=skill_name,
                skill_body=record.get("skill_body", ""),
                post_load_window=window,
                load_context=record.get("load_context", ""),
            )
        except Exception:
            logger.exception("outcome_watcher: classifier raised for %s", skill_name)
            outcome = None
        if outcome is None:
            # Provider error — leave pending file in place so the next
            # bridge startup retries via resume_pending_classifications.
            return
        # Success path: mark per-session dedup, emit the outcome event,
        # delete the pending file.
        self._classified.add(skill_name)
        events.append_outcome(
            skill_name,
            record.get("session_id", self.session_id),
            category=outcome.category,
            load_ts=int(record.get("load_ts", 0)),
            evidence=outcome.evidence,
        )
        _delete_pending(record.get("load_id", ""))


# ── Module-level resume path ─────────────────────────────────────────


async def resume_pending_classifications() -> None:
    """Drain pending records left behind by previous bridge runs.

    Called once at bridge startup (after the event loop is running but
    before any new sessions accept turns). For each pending file:

      · ``ready`` is true OR ``post_load_turn_count >= DEFAULT_POST_LOAD_TURNS``
        → classify immediately. The session is gone; we have everything
        the classifier needs already on disk.
      · ``post_load_turn_count >= 1`` (we have *some* post-load context)
        → classify with what we have. Better signal than zero.
      · ``post_load_turn_count == 0`` AND older than the TTL → delete.
        Empty pending records from dead sessions are dead weight.
      · ``post_load_turn_count == 0`` AND fresh → leave alone. A live
        session may resume and complete the window.

    Failures are silent; classification retries on next startup.
    """
    files = list(_iter_pending_files())
    if not files:
        return
    logger.info("outcome_watcher: scanning %d pending classification(s) at startup", len(files))
    now = _now_ms()
    tasks: list[asyncio.Task[Any]] = []
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("outcome_watcher: no event loop at resume — skipping")
        return
    for path in files:
        record = _read_pending(path)
        if record is None:
            continue
        turn_count = int(record.get("post_load_turn_count", 0))
        ready = bool(record.get("ready", False))
        created_at = int(record.get("created_at", 0))
        if not ready and turn_count == 0:
            if created_at and (now - created_at) > _EMPTY_PENDING_TTL_MS:
                logger.info(
                    "outcome_watcher: dropping stale empty pending %s (age %.1fh)",
                    record.get("load_id", "?"), (now - created_at) / 3_600_000,
                )
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        # Classify in the background. We don't await here — startup
        # shouldn't block on N classifier calls. Each task races to
        # completion; failures retry on the NEXT startup.
        coro = _resume_classify_one(record)
        tasks.append(loop.create_task(
            coro,
            name=f"outcome-resume:{record.get('skill_name', '?')}",
        ))
    if tasks:
        logger.info(
            "outcome_watcher: dispatched %d pending classifier task(s) at startup",
            len(tasks),
        )


async def _resume_classify_one(record: dict[str, Any]) -> None:
    """Classifier dispatch for the resume path. Identical to
    :meth:`SkillOutcomeWatcher._classify_one` minus the per-session
    dedup state — the owning session is gone, so the in-memory
    ``_classified`` set isn't relevant. Outcome lands in the event
    log; pending file is deleted on success."""
    skill_name = record.get("skill_name", "")
    window = (record.get("post_load_window") or "").strip()
    load_id = record.get("load_id", "")
    if not window:
        _delete_pending(load_id)
        return
    try:
        outcome = await classify(
            skill_name=skill_name,
            skill_body=record.get("skill_body", ""),
            post_load_window=window,
            load_context=record.get("load_context", ""),
        )
    except Exception:
        logger.exception("outcome_watcher: resume classifier raised for %s", skill_name)
        outcome = None
    if outcome is None:
        return  # leave pending file in place; next startup retries
    events.append_outcome(
        skill_name,
        record.get("session_id", ""),
        category=outcome.category,
        load_ts=int(record.get("load_ts", 0)),
        evidence=outcome.evidence,
    )
    _delete_pending(load_id)


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
