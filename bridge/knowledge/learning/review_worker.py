"""Post-turn-complete drafter spawn site.

The bridge calls :func:`spawn_drafter_review` at the end of a turn when
either the cadence counter trips OR an explicit ``/learn-this`` slash
command fires. This module is the sole site that wires a drafter pass
onto the bridge's asyncio loop — keeping the spawn surface tiny means
the lifecycle (logging, devnull redirect, in-flight tracking) lives in
one place.

Design constraints
──────────────────

  · **Non-blocking.** The drafter is an LLM round trip that can take
    20-60 s. It MUST NOT delay returning control to the user. We schedule
    it as an :class:`asyncio.Task` on the running loop and return
    immediately.

  · **Silent.** The Anthropic SDK used here is already quiet in normal
    operation. M3: a previous version wrapped the ``await run_drafter(...)``
    in ``contextlib.redirect_stdout`` / ``redirect_stderr`` to
    ``/dev/null``. ``contextlib.redirect_stdout`` mutates
    ``sys.stdout`` PROCESS-WIDE, and an ``await`` inside it yields
    control to other coroutines that THEN write to the redirected
    stdout — so the drafter's stdout sink silently captured Slack
    streams, scheduler logging, and every other awaitable producing
    output for 20-60 s. The redirect is removed; if a future provider
    regression starts printing, fix it at the provider.

  · **Best-effort.** A drafter failure (provider 500, guard reject,
    schema mismatch) must never bubble back into the turn loop. The
    done-callback logs the exception class and swallows it.

  · **Diagnosable.** A module-level :class:`set` of in-flight tasks
    lets ``/diag drafter`` (and tests via :func:`wait_for_drain`)
    inspect what's running. The set holds a strong reference so the
    asyncio task isn't GC-eligible mid-await (H4 — previously a
    ``weakref.WeakSet``, which let CPython collect orphan tasks mid-
    LLM-call). A done-callback discards entries when the task settles
    so the set never grows unboundedly.

What this is NOT
────────────────

  · Not the drafter itself — that lives in
    ``bridge.knowledge.learning.drafter.run_drafter``. We just spawn it.
  · Not the cadence counter — the bridge decides when to call us. From
    this module's perspective every call is "go".
  · Not the candidate-emit pipeline — once ``run_drafter`` returns a
    candidate_id, we fire ``on_candidate(candidate_id)`` and the bridge
    handles emitting the renderer event and (eventually) operator
    confirmation. This module never touches the renderer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


# Module-level registry of in-flight drafter tasks.
#
# H4: previously a ``weakref.WeakSet``. The caller discards the task
# handle, and CPython 3.11+ asyncio.create_task does NOT retain a
# strong reference of its own — the docs explicitly warn that orphan
# tasks may be garbage-collected mid-await. The 20-60 s drafter LLM
# call is the longest GC window in this whole loop. A weak reference
# is exactly the wrong primitive here.
#
# Switch to a plain set + done-callback discard. The done callback
# (_on_task_done) removes the entry once the task settles, so the
# set never grows unboundedly; in the meantime the set keeps a
# strong reference that prevents GC reaping mid-run.
_INFLIGHT: set["asyncio.Task[Any]"] = set()


def spawn_drafter_review(
    *,
    session_id: str,
    turn_id: str | None,
    conversation_excerpt: str,
    loaded_skill_names: list[str],
    all_skill_names: list[str],
    on_candidate: Callable[[str], None] | None = None,
) -> asyncio.Task[Any] | None:
    """Schedule a drafter review on the running event loop.

    Parameters
    ──────────

      · ``session_id`` / ``turn_id`` — identifiers used in the task name
        and for downstream candidate provenance. ``turn_id`` may be
        ``None`` when called from ``/learn-this`` mid-turn (no completed
        turn to anchor against).

      · ``conversation_excerpt`` — pre-rendered text the drafter reads.
        The caller is responsible for trimming this to a reasonable size
        (the drafter has its own cap, but we don't double the work).

      · ``loaded_skill_names`` — skills that were resident in the session
        at the time the review fires. The drafter uses this to bias
        toward updating known skills before proposing new ones.

      · ``all_skill_names`` — full library inventory, for de-dup checks
        inside the drafter.

      · ``on_candidate`` — invoked with the candidate id string when (and
        only when) ``run_drafter`` returns a non-None candidate. The
        bridge passes a closure that emits a
        ``skill_candidate_ready`` event to the renderer. May be ``None``
        in tests / headless runs.

    Returns
    ───────

      The :class:`asyncio.Task` so tests can ``await`` it. ``None`` if
      we couldn't schedule (no running loop, or failed import of the
      drafter module — both treated as non-fatal misconfiguration).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called from a sync context with no loop. Drafter is a
        # best-effort background pass; if there's no loop to host it,
        # silently drop. The bridge always calls us from within its
        # asyncio runtime in practice — hitting this branch usually
        # means a test or CLI tool invoked us directly.
        logger.debug(
            "review_worker: no running event loop; skipping drafter for session=%s turn=%s",
            session_id, turn_id,
        )
        return None

    coro = _run_with_redirects(
        session_id=session_id,
        turn_id=turn_id,
        conversation_excerpt=conversation_excerpt,
        loaded_skill_names=loaded_skill_names,
        all_skill_names=all_skill_names,
        on_candidate=on_candidate,
    )
    # Truncate session_id in the task name — names show up in error
    # logs and stack dumps; long opaque ids make those hard to scan.
    task_name = f"drafter-review:{session_id[:24]}"
    task = loop.create_task(coro, name=task_name)
    _INFLIGHT.add(task)
    task.add_done_callback(_on_task_done)
    logger.info(
        "review_worker: spawned drafter task name=%s session=%s turn=%s inflight=%d",
        task_name, session_id, turn_id, len(_INFLIGHT),
    )
    return task


async def _run_with_redirects(
    *,
    session_id: str,
    turn_id: str | None,
    conversation_excerpt: str,
    loaded_skill_names: list[str],
    all_skill_names: list[str],
    on_candidate: Callable[[str], None] | None,
) -> str | None:
    """Inner coroutine: import drafter lazily, run it, fire ``on_candidate``
    on success.

    Returns the candidate_id on success, ``None`` on every other path
    (no candidate, drafter unavailable, exception). The return is mostly
    informational — the side effect of firing ``on_candidate`` is what
    matters to the bridge.

    Naming preserved for the diagnostic trail and external callers; the
    actual ``redirect_stdout``/``redirect_stderr`` block was removed
    in M3 (see module docstring) because it mutated process-wide
    ``sys.stdout`` across an ``await`` and silently swallowed every
    other coroutine's output during the drafter's 20-60 s LLM call.
    """
    # Lazy import: ``drafter`` may not be fully wired during early MVP
    # phases. A missing import shouldn't crash the bridge — it just
    # means there's nothing to spawn yet.
    try:
        from bridge.knowledge.learning import drafter  # type: ignore[attr-defined]
    except Exception:
        logger.exception(
            "review_worker: drafter module unavailable; skipping review for session=%s",
            session_id,
        )
        return None

    run_drafter = getattr(drafter, "run_drafter", None)
    if run_drafter is None:
        logger.warning(
            "review_worker: drafter.run_drafter missing; skipping review for session=%s",
            session_id,
        )
        return None

    candidate_id: str | None = None
    try:
        # No process-wide stdout/stderr redirect — see M3 in the module
        # docstring. The Anthropic SDK is quiet by default; if it
        # regresses, the fix belongs at the provider, not in a wrapper
        # that mutes every concurrent coroutine for the duration of an
        # ``await``.
        result = await run_drafter(
            session_id=session_id,
            turn_id=turn_id,
            conversation_excerpt=conversation_excerpt,
            loaded_skill_names=loaded_skill_names,
            all_skill_names=all_skill_names,
        )
        # ``run_drafter`` returns either a candidate_id string, or
        # something falsy (None / "") meaning "nothing worth proposing
        # this pass." Both are normal outcomes — only the truthy branch
        # fires the callback.
        if result:
            candidate_id = str(result)
    except asyncio.CancelledError:
        # Propagate cancellation so the loop's shutdown semantics work
        # correctly. Don't log — cancellation is normal during session
        # reset / shutdown.
        raise
    except Exception:
        logger.exception(
            "review_worker: drafter raised for session=%s turn=%s",
            session_id, turn_id,
        )
        return None

    if candidate_id and on_candidate is not None:
        try:
            on_candidate(candidate_id)
        except Exception:
            # The bridge's emit callback shouldn't fail, but if it does,
            # we already produced the candidate file on disk — operator
            # can still see it via the candidates list. Log + swallow.
            logger.exception(
                "review_worker: on_candidate callback raised for candidate=%s session=%s",
                candidate_id, session_id,
            )

    if candidate_id:
        logger.info(
            "review_worker: drafter produced candidate=%s session=%s turn=%s",
            candidate_id, session_id, turn_id,
        )
    else:
        logger.debug(
            "review_worker: drafter produced no candidate session=%s turn=%s",
            session_id, turn_id,
        )
    return candidate_id


def _on_task_done(task: asyncio.Task[Any]) -> None:
    """Done-callback: log any exception that escaped the inner coroutine,
    swallow it, and drop the strong reference from ``_INFLIGHT``."""
    # H4: discard from the strong set here, mirroring the pattern
    # ``outcome_watcher._tasks`` uses. Without this, the set would
    # leak completed tasks forever.
    _INFLIGHT.discard(task)
    if task.cancelled():
        logger.debug("review_worker: task %s cancelled", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        # Anything that escapes ``_run_with_redirects`` is a bug in the
        # error handling there — log loudly so we notice, but never
        # re-raise (the loop's default would surface it to the bridge).
        logger.error(
            "review_worker: task %s ended with unhandled exception: %r",
            task.get_name(), exc,
        )
        return
    logger.debug("review_worker: task %s completed cleanly", task.get_name())


async def wait_for_drain(timeout: float = 60.0) -> None:
    """Block until every in-flight drafter task finishes.

    Used by tests + by graceful-shutdown paths that want to make sure no
    candidate is lost mid-flight to process exit. On timeout we log and
    return — we never raise, because the caller is usually already
    tearing down and can't usefully react.
    """
    # Snapshot under a list — iterating the live set while done-
    # callbacks discard entries is racy.
    tasks = [t for t in _INFLIGHT if not t.done()]
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        still = sum(1 for t in tasks if not t.done())
        logger.warning(
            "review_worker: drain timed out after %.1fs; %d tasks still in flight",
            timeout, still,
        )
