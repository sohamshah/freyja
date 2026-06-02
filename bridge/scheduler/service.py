"""SchedulerService — long-running asyncio service that fires due
jobs and exposes CRUD over scheduled jobs.

Multi-process safe. The service has NO in-memory job table — every
read hits disk, every mutation takes a per-job ``fcntl.flock`` before
re-reading + writing. Two processes can run a SchedulerService
concurrently (e.g. the Electron-attached bridge + the LaunchAgent
daemon) without corrupting state or double-firing jobs.

The mechanism:

  · ``jobs/{id}.json`` is the canonical record. Atomic writes
    (temp + rename) prevent partial-state reads.
  · ``.locks/job-{id}.lock`` is acquired (non-blocking) before any
    state-mutating operation, including pre-advance + fire dispatch.
    Two processes racing the same due job: whoever flocks first
    wins; the other skips.
  · ``.wake`` is touched on every CRUD; tick loops poll its mtime
    each iteration (1Hz cap) and re-scan on change. Wake latency is
    bounded by the poll interval; no cross-process asyncio
    primitives needed.
  · ``request_run_cancellation`` writes a flag into the run JSON
    that the firing process polls during its agent turn.

Crash safety: ``fcntl.flock`` is released automatically by the kernel
when the holder exits (clean OR crash). A dead process can never leave
a job permanently locked.

Internal hooks:

  ``advance_self_paced(job_id, delay_seconds)`` — used by the
  auto-registered ``continue_loop`` tool.
  ``complete_self_paced(job_id, reason)`` — used by ``complete_loop``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from bridge.scheduler.models import (
    BudgetSpec,
    CreatorRef,
    DesktopSinkSpec,
    ExistingSession,
    JobFilter,
    JobPatch,
    JobRecord,
    NewSession,
    PersistentJobSession,
    RunRecord,
    SchedulerMetrics,
    ScheduleSpec,
    SelfPacedSchedule,
    SessionSinkSpec,
    SinkSpec,
    SlackSinkSpec,
    new_job_id,
)
from bridge.scheduler.persistence import (
    FileLock,
    append_event,
    delete_job,
    ensure_dirs,
    iter_job_files,
    job_lock_path,
    load_all_jobs,
    load_job,
    load_recent_runs_global,
    load_runs_for_job,
    owner_lock_path,
    read_wake_mtime,
    request_run_cancellation,
    save_job,
    touch_wake,
)
from bridge.scheduler.scheduling import (
    cadence_label,
    compute_next_fire,
    parse_when,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger("freyja.scheduler.service")


# Default concurrency cap — very high so small users see no throttling,
# but a runaway "fire 10k jobs at the same instant" won't OOM the box.
_DEFAULT_MAX_CONCURRENT_RUNS = 256

# Tick cadence ceiling. The loop sleeps min(horizon, _TICK_CAP_SECONDS)
# so wake events from peers are noticed promptly. At 1Hz a cross-process
# create_job becomes visible within ~1s, which matches user expectations
# without burning CPU.
_TICK_CAP_SECONDS = 1.0


class SchedulerService:
    """Process-level scheduler. Multiple instances may coexist; disk +
    flock are the coordination layer.

    Public API:

      ``await create_job(spec) -> JobRecord``
      ``await list_jobs(filter) -> list[JobRecord]``
      ``await get_job(job_id) -> JobRecord | None``
      ``await update_job(job_id, patch) -> JobRecord``
      ``await pause_job(job_id) -> JobRecord``
      ``await resume_job(job_id) -> JobRecord``
      ``await remove_job(job_id) -> bool``
      ``await run_job_now(job_id) -> RunRecord``
      ``await get_runs(job_id, limit) -> list[RunRecord]``
      ``await cancel_run(run_id) -> bool``
      ``await metrics() -> SchedulerMetrics``

    Loop control:

      ``await start()`` / ``await stop()``

    Internal hooks:

      ``advance_self_paced(job_id, delay_seconds)`` — used by the
      auto-registered ``continue_loop`` tool.
      ``complete_self_paced(job_id, reason)`` — used by ``complete_loop``.

    Notes:

      · ``cancel_run`` always works cross-process: it writes
        ``cancel_requested=true`` into the run JSON. The owning
        process polls during its agent turn and cancels the local
        task. Same-process callers also get an immediate
        ``Task.cancel()`` for speed.
      · ``run_job_now`` flocks the per-job lock; if a peer is
        already firing, it raises ``RuntimeError`` instead of racing.
    """

    def __init__(
        self,
        state: Any,
        *,
        max_concurrent_runs: int = _DEFAULT_MAX_CONCURRENT_RUNS,
    ) -> None:
        self.state = state
        # In-flight runs owned by THIS process — used for same-process
        # cancel and graceful shutdown. Peer-process runs are not
        # visible here; they're cancelled via the cancel_requested
        # flag on the run JSON.
        self._in_flight_runs: dict[str, asyncio.Task] = {}
        self._stopped = False
        self._loop_task: asyncio.Task | None = None
        self._concurrency = asyncio.Semaphore(max_concurrent_runs)
        # The owner-lock holder runs the tick loop. The other process
        # still services CRUD via disk + per-job flocks.
        self._owner_lock: FileLock | None = None
        self._is_owner: bool = False
        # Wake-mtime watermark: peers bump ``.wake`` on every CRUD;
        # we use it to know whether to bother re-scanning even if our
        # sleep timed out early. Currently informational — the tick
        # always re-reads disk — but available for future optimisation.
        self._last_wake_mtime: float = 0.0
        # Optional callback for emitting daemon-install hints — set by
        # the bridge's main on first scheduled-job creation.
        self.on_durable_job_created: Any = None

    # ─── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Boot the run loop. Acquires the cross-process owner lock; if
        another scheduler in another process already holds it, this
        instance runs in CRUD-only mode (no tick loop)."""
        ensure_dirs()
        # Try to claim ownership of the tick loop. If a peer (daemon
        # or another desktop bridge) already holds it, we still
        # service CRUD — disk + per-job flocks keep us safe — but we
        # don't run a tick. This avoids two processes burning CPU on
        # parallel disk scans for the same jobs.
        self._owner_lock = FileLock(owner_lock_path())
        self._is_owner = self._owner_lock.acquire()
        if not self._is_owner:
            logger.info(
                "scheduler started in CRUD-only mode "
                "(peer holds owner lock at %s)",
                owner_lock_path(),
            )
            append_event({
                "type": "scheduler_started",
                "mode": "crud_only",
                "job_count": _count_jobs_on_disk(),
            })
            return

        # Owner: recompute next_fire_at for every job and persist —
        # the prior process may have crashed mid-update, leaving stale
        # values. We hold each job's flock briefly while we rewrite.
        boot_count = 0
        for job in load_all_jobs():
            with FileLock(job_lock_path(job.id)) as got:
                if not got:
                    # A peer is mutating this job concurrently — skip
                    # the boot recompute; the next tick will catch up
                    # via the normal scan path.
                    continue
                fresh = load_job(job.id) or job
                self._recompute_next_fire(fresh, force=True)
                save_job(fresh)
                boot_count += 1
        touch_wake()
        logger.info("scheduler booted as owner: %d jobs", boot_count)
        append_event({
            "type": "scheduler_started",
            "mode": "owner",
            "job_count": boot_count,
        })
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="scheduler-loop",
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._loop_task is not None:
            # The loop checks _stopped on each iteration; up to 1s
            # before it notices. Wait up to 5s for clean exit.
            try:
                await asyncio.wait_for(self._loop_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._loop_task.cancel()
        # Don't kill in-flight runs — they're agent turns and they
        # need to finish cleanly. Wait briefly, then let the process
        # drop them.
        if self._in_flight_runs:
            await asyncio.gather(
                *self._in_flight_runs.values(),
                return_exceptions=True,
            )
        if self._owner_lock is not None:
            self._owner_lock.release()
            self._owner_lock = None
            self._is_owner = False
        append_event({"type": "scheduler_stopped"})

    # ─── Main loop ────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        # Only the owner runs the tick loop. If we somehow lost
        # ownership (shouldn't happen — kernel releases flocks only on
        # process exit), bail.
        while not self._stopped and self._is_owner:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("scheduler tick crashed: %s", exc)
            # Sleep until the next due time, capped at _TICK_CAP_SECONDS
            # so cross-process wake-touches surface within ~1s. We
            # always re-scan disk on the next tick regardless.
            now = time.time()
            next_at = self._earliest_next_fire_from_disk()
            if next_at is None:
                delay = _TICK_CAP_SECONDS
            else:
                delay = max(0.05, min(next_at - now, _TICK_CAP_SECONDS))
            self._last_wake_mtime = read_wake_mtime()
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """Scan jobs from disk, find due, attempt to claim each with a
        per-job flock + atomic pre-advance, fire winners. Multi-process
        safe: two ticking schedulers compete via the flock, so each
        due job fires exactly once across the fleet."""
        now = time.time()
        # Cheap read: enumerate file paths only. No JSON parse yet.
        candidates: list[str] = []
        for job_id, _path, _mtime in iter_job_files():
            # We could optimise by stat-only checks here, but the
            # parsed JobRecord is needed to know next_fire_at anyway.
            # On a tick path, this is fine.
            job = load_job(job_id)
            if job is None or not job.enabled or job.status != "active":
                continue
            if job.next_fire_at is None or job.next_fire_at > now:
                continue
            candidates.append(job_id)
        for job_id in candidates:
            asyncio.create_task(
                self._try_claim_and_fire(job_id),
                name=f"scheduler-claim-{job_id}",
            )

    async def _try_claim_and_fire(self, job_id: str) -> None:
        """Acquire the per-job flock, re-read the job under it, verify
        still due, pre-advance + persist, then fire. If the flock is
        already held (by a peer process or our own concurrent claim),
        skip — the holder is firing this job already.
        """
        lock = FileLock(job_lock_path(job_id))
        if not lock.acquire():
            # Another process / claim task got here first. Their
            # pre-advance + fire is canonical; we drop out.
            return
        try:
            # Re-read under the lock — the peer may have already
            # advanced next_fire_at past now.
            job = load_job(job_id)
            if job is None or not job.enabled or job.status != "active":
                return
            now = time.time()
            if job.next_fire_at is None or job.next_fire_at > now:
                return
            # Pre-advance + persist BEFORE firing — at-most-once.
            self._recompute_next_fire(job, force=True, after_fire=True)
            save_job(job)
            touch_wake()
            # Hand off to the fire path while STILL holding the lock —
            # the fire path is responsible for releasing it after the
            # run completes (so cancel_run flag polling sees the same
            # owner).
            task = asyncio.create_task(
                self._fire_with_concurrency(job, lock),
                name=f"scheduler-fire-{job.id}",
            )
            self._in_flight_runs[task.get_name()] = task
            # Don't release lock here — _fire_with_concurrency owns it.
            lock = None
        finally:
            if lock is not None:
                lock.release()

    async def _fire_with_concurrency(self, job: JobRecord, fire_lock: FileLock) -> None:
        """Owns the per-job flock for the duration of the fire."""
        task_name = asyncio.current_task().get_name()
        try:
            async with self._concurrency:
                await self._fire_under_lock(job)
        except Exception as exc:  # noqa: BLE001
            logger.exception("fire crashed (job=%s): %s", job.id, exc)
        finally:
            fire_lock.release()
            self._in_flight_runs.pop(task_name, None)

    async def _fire_under_lock(self, job: JobRecord) -> None:
        """Run the agent turn for ``job``. The per-job flock is already
        held by our caller, so retry-policy + self-paced default
        writes are race-free."""
        from bridge.scheduler.runtime import fire_job
        run = await fire_job(self, job)
        # Retry policy. If the run failed and the user configured
        # retries, schedule a backoff fire.
        retry = job.retry_policy
        if (
            retry is not None
            and run.status in {"failed", "timed_out"}
            and retry.max_retries > 0
            and (not retry.on or _retry_kind(run) in retry.on)
        ):
            attempts = getattr(run, "_attempts", 0) + 1
            if attempts <= retry.max_retries:
                delay = retry.backoff_seconds * (
                    retry.backoff_multiplier ** (attempts - 1)
                )
                # Re-read job under our held lock so we don't clobber
                # any concurrent update_job from a peer (also locked,
                # but ordered before us).
                fresh = load_job(job.id) or job
                fresh.next_fire_at = time.time() + float(delay)
                save_job(fresh)
                touch_wake()
                append_event({
                    "type": "scheduler_run_retrying",
                    "job_id": job.id,
                    "run_id": run.run_id,
                    "attempt": attempts,
                    "delay_seconds": delay,
                })
        # Self-paced jobs whose agent didn't set a next_fire_at get
        # the configured max_delay as a safety net.
        if isinstance(job.schedule, SelfPacedSchedule):
            fresh = load_job(job.id) or job
            if fresh.enabled and (
                fresh.next_fire_at is None
                or fresh.next_fire_at <= time.time()
            ):
                fresh.next_fire_at = time.time() + job.schedule.max_delay_seconds
                save_job(fresh)
                touch_wake()
                append_event({
                    "type": "scheduler_self_paced_defaulted",
                    "job_id": job.id,
                    "delay_seconds": job.schedule.max_delay_seconds,
                })

    # ─── CRUD ─────────────────────────────────────────────────────────

    async def create_job(self, spec: JobRecord) -> JobRecord:
        """Persist a new job. Computes ``next_fire_at`` from schedule.
        Reads back from disk before returning so the caller gets the
        canonical (post-pre-advance, post-validation) record."""
        if not spec.id:
            spec.id = new_job_id()
        spec.created_at = spec.updated_at = time.time()
        # Take the per-job lock to align with the rest of the CRUD
        # surface (so concurrent create-vs-update on a known id can't
        # race), then compute + persist.
        with FileLock(job_lock_path(spec.id)) as got:
            if not got:
                # The id is in use AND being mutated — astronomically
                # unlikely for a fresh uuid, but defensive.
                raise RuntimeError(
                    f"job id collision: {spec.id} is locked by a peer"
                )
            self._recompute_next_fire(spec, force=True)
            save_job(spec)
        touch_wake()
        # Re-load from disk so the returned record matches what peers
        # would see if they queried — not the just-mutated in-memory
        # object.
        canonical = load_job(spec.id) or spec
        append_event({
            "type": "scheduler_job_created",
            "job_id": canonical.id,
            "name": canonical.name,
            "schedule": canonical.schedule.to_dict(),
        })
        self.emit_event(
            subtype="scheduler_job_created",
            message=f"Scheduled job '{canonical.name}' created",
            details={
                "jobId": canonical.id,
                "cadence": cadence_label(canonical.schedule),
                "nextFireAt": canonical.next_fire_at,
            },
        )
        try:
            if self.on_durable_job_created is not None:
                if asyncio.iscoroutinefunction(self.on_durable_job_created):
                    await self.on_durable_job_created(canonical)
                else:
                    self.on_durable_job_created(canonical)
        except Exception:  # noqa: BLE001
            logger.debug("on_durable_job_created hook failed", exc_info=True)
        return canonical

    async def list_jobs(self, filt: JobFilter | None = None) -> list[JobRecord]:
        """Read all jobs from disk and apply filter. O(n) in job count,
        which is fine at our scale (1-10000 jobs). No in-memory cache
        means cross-process writes are visible immediately."""
        jobs = load_all_jobs()
        if filt is not None:
            jobs = [j for j in jobs if _matches(j, filt)]
        return sorted(jobs, key=lambda j: (j.next_fire_at or 1e18, j.name))

    async def get_job(self, job_id: str) -> JobRecord | None:
        """Always read from disk — never trust an in-memory cache."""
        return load_job(job_id)

    async def update_job(self, job_id: str, patch: JobPatch) -> JobRecord:
        """Take the per-job flock, re-read under it, apply the patch,
        save. Eliminates lost-update against concurrent peer writes."""
        with FileLock(job_lock_path(job_id)) as got:
            if not got:
                # A peer is firing this job. Updates wait for the next
                # opportunity — the caller can retry. We could
                # alternatively block here on flock; preferring fail-
                # fast keeps the surface simple.
                raise RuntimeError(
                    f"job {job_id} is locked by a peer; retry shortly"
                )
            job = load_job(job_id)
            if job is None:
                raise KeyError(job_id)
            for field_name in (
                "name", "description", "enabled", "max_fires", "prompt",
                "permission_snapshot", "model_id", "coordination_strategy",
                "skills_to_load", "budget", "artifact", "memory", "sinks",
                "misfire_policy", "overlap_policy", "timeout_seconds",
                "retry_policy", "tags",
            ):
                val = getattr(patch, field_name)
                if val is not None:
                    setattr(job, field_name, val)
            if patch.schedule is not None:
                job.schedule = patch.schedule
                self._recompute_next_fire(job, force=True)
            if patch.execution is not None:
                job.execution = patch.execution
            save_job(job)
        touch_wake()
        canonical = load_job(job_id) or job
        append_event({
            "type": "scheduler_job_updated",
            "job_id": canonical.id,
        })
        self.emit_event(
            subtype="scheduler_job_updated",
            message=f"Scheduled job '{canonical.name}' updated",
            details={"jobId": canonical.id, "cadence": cadence_label(canonical.schedule)},
        )
        return canonical

    async def pause_job(self, job_id: str) -> JobRecord:
        with FileLock(job_lock_path(job_id)) as got:
            if not got:
                raise RuntimeError(f"job {job_id} is locked by a peer; retry shortly")
            job = load_job(job_id)
            if job is None:
                raise KeyError(job_id)
            job.enabled = False
            job.status = "paused"
            save_job(job)
        touch_wake()
        append_event({"type": "scheduler_job_paused", "job_id": job_id})
        canonical = load_job(job_id) or job
        self.emit_event(
            subtype="scheduler_job_paused",
            message=f"Scheduled job '{canonical.name}' paused",
            details={"jobId": job_id},
        )
        return canonical

    async def resume_job(self, job_id: str) -> JobRecord:
        with FileLock(job_lock_path(job_id)) as got:
            if not got:
                raise RuntimeError(f"job {job_id} is locked by a peer; retry shortly")
            job = load_job(job_id)
            if job is None:
                raise KeyError(job_id)
            job.enabled = True
            job.status = "active"
            self._recompute_next_fire(job, force=True)
            save_job(job)
        touch_wake()
        append_event({"type": "scheduler_job_resumed", "job_id": job_id})
        canonical = load_job(job_id) or job
        self.emit_event(
            subtype="scheduler_job_resumed",
            message=f"Scheduled job '{canonical.name}' resumed",
            details={"jobId": job_id, "nextFireAt": canonical.next_fire_at},
        )
        return canonical

    async def remove_job(self, job_id: str) -> bool:
        with FileLock(job_lock_path(job_id)) as got:
            if not got:
                raise RuntimeError(f"job {job_id} is locked by a peer; retry shortly")
            job = load_job(job_id)
            if job is None:
                return False
            deleted = delete_job(job_id)
        touch_wake()
        # Best-effort cleanup of the lock file. Safe to leave behind
        # if removal fails — next iter_job_files won't return the
        # deleted job anyway, and FileLock recreates the file on
        # next acquire.
        try:
            jlp = job_lock_path(job_id)
            if jlp.exists():
                jlp.unlink()
        except OSError:
            pass
        append_event({"type": "scheduler_job_removed", "job_id": job_id})
        self.emit_event(
            subtype="scheduler_job_removed",
            message=f"Scheduled job removed",
            details={"jobId": job_id, "name": getattr(job, "name", "")},
        )
        return deleted

    async def run_job_now(self, job_id: str) -> RunRecord:
        """Fire a job immediately. Acquires the per-job flock so a
        concurrent scheduled tick won't double-fire. Doesn't touch
        ``next_fire_at`` — manual runs are independent of cadence."""
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        lock = FileLock(job_lock_path(job_id))
        if not lock.acquire():
            raise RuntimeError(
                f"job {job_id} is currently firing in another process; "
                f"try again when the current fire completes"
            )
        try:
            async with self._concurrency:
                from bridge.scheduler.runtime import fire_job
                # Re-read inside the lock for freshness.
                fresh = load_job(job_id) or job
                return await fire_job(self, fresh)
        finally:
            lock.release()

    async def get_runs(self, job_id: str, *, limit: int = 50) -> list[RunRecord]:
        return load_runs_for_job(job_id, limit=limit)

    async def cancel_run(self, run_id: str) -> bool:
        """Cancel a run, possibly in another process. Mechanism:
        1. If we have the run task in-process, cancel it directly
           (fast path).
        2. Always also write ``cancel_requested=true`` into the run
           JSON. The firing process (whichever one) polls this flag
           during its agent turn and cancels its local task.
        """
        # Same-process fast path.
        same_process_cancelled = False
        for name, task in list(self._in_flight_runs.items()):
            if run_id in name and not task.done():
                task.cancel()
                same_process_cancelled = True
        # Cross-process flag — we need the job_id to find the run
        # file. Scan recent runs across all jobs and find the match.
        try:
            from bridge.scheduler.persistence import load_recent_runs_global
            for run in load_recent_runs_global(limit=200):
                if run.run_id == run_id:
                    request_run_cancellation(run.job_id, run_id)
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("cancel_run flag-write failed: %s", exc)
        return same_process_cancelled

    async def metrics(self) -> SchedulerMetrics:
        jobs = load_all_jobs()
        enabled = sum(1 for j in jobs if j.enabled and j.status == "active")
        paused = sum(1 for j in jobs if j.status == "paused")
        disabled = sum(1 for j in jobs if j.status.startswith("disabled"))

        recent = load_recent_runs_global(limit=500)
        cutoff = time.time() - 86400
        recent_24h = [r for r in recent if r.started_at >= cutoff]
        ok = sum(1 for r in recent_24h if r.status in ("succeeded", "partial_failure"))
        fail = sum(1 for r in recent_24h if r.status in ("failed", "timed_out"))
        avg_dur = (
            sum(r.duration_seconds for r in recent_24h) / len(recent_24h)
            if recent_24h else 0.0
        )
        cost = sum(r.cost_usd for r in recent_24h)

        next_at = None
        next_job: JobRecord | None = None
        for j in jobs:
            if j.enabled and j.status == "active" and j.next_fire_at:
                if next_at is None or j.next_fire_at < next_at:
                    next_at = j.next_fire_at
                    next_job = j

        return SchedulerMetrics(
            total_jobs=len(jobs),
            enabled_jobs=enabled,
            paused_jobs=paused,
            disabled_jobs=disabled,
            runs_24h=len(recent_24h),
            succeeded_24h=ok,
            failed_24h=fail,
            avg_run_duration_seconds=avg_dur,
            total_cost_usd_24h=cost,
            next_fire_at=next_at,
            next_fire_job_id=next_job.id if next_job else None,
            next_fire_job_name=next_job.name if next_job else None,
        )

    # ─── Self-paced loop hooks ────────────────────────────────────────

    async def advance_self_paced(
        self,
        job_id: str,
        *,
        delay_seconds: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """Called by the auto-registered ``continue_loop`` tool when
        the agent declares the next wakeup."""
        with FileLock(job_lock_path(job_id)) as got:
            if not got:
                raise RuntimeError(
                    f"job {job_id} is locked by a peer; cannot advance"
                )
            job = load_job(job_id)
            if job is None:
                raise KeyError(job_id)
            if not isinstance(job.schedule, SelfPacedSchedule):
                raise ValueError("continue_loop is only valid for self-paced jobs")
            clamped = max(
                job.schedule.min_delay_seconds,
                min(job.schedule.max_delay_seconds, int(delay_seconds)),
            )
            job.next_fire_at = time.time() + clamped
            save_job(job)
        touch_wake()
        append_event({
            "type": "scheduler_self_paced_continued",
            "job_id": job_id,
            "delay_seconds": clamped,
            "reason": reason,
        })
        self.emit_event(
            subtype="scheduler_self_paced_continued",
            message=f"Self-paced job '{job.name}' continued",
            details={"jobId": job_id, "delaySeconds": clamped, "reason": reason},
        )
        return {"delay_seconds": clamped, "next_fire_at": job.next_fire_at}

    async def complete_self_paced(
        self,
        job_id: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        with FileLock(job_lock_path(job_id)) as got:
            if not got:
                raise RuntimeError(
                    f"job {job_id} is locked by a peer; cannot complete"
                )
            job = load_job(job_id)
            if job is None:
                raise KeyError(job_id)
            job.enabled = False
            job.status = "disabled_max_fires"  # reuse — "natural completion"
            job.next_fire_at = None
            save_job(job)
        touch_wake()
        append_event({
            "type": "scheduler_self_paced_completed",
            "job_id": job_id,
            "reason": reason,
        })
        self.emit_event(
            subtype="scheduler_self_paced_completed",
            message=f"Self-paced job '{job.name}' completed",
            details={"jobId": job_id, "reason": reason},
        )
        return {"completed": True}

    # ─── Utility ──────────────────────────────────────────────────────

    def _recompute_next_fire(
        self,
        job: JobRecord,
        *,
        force: bool = False,
        after_fire: bool = False,
    ) -> None:
        if not job.enabled or job.status != "active":
            job.next_fire_at = None
            return
        if not force and job.next_fire_at and job.next_fire_at > time.time():
            return
        now = time.time()
        last = job.last_fire_at if after_fire else None
        try:
            job.next_fire_at = compute_next_fire(
                job.schedule,
                now=now,
                last_fire=last,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("compute_next_fire failed for %s: %s", job.id, exc)
            job.next_fire_at = None
        if job.next_fire_at is None:
            if isinstance(job.schedule, SelfPacedSchedule):
                return
            job.enabled = False
            job.status = "disabled_max_fires"

    def _earliest_next_fire_from_disk(self) -> float | None:
        """Scan disk for the minimum next_fire_at across all active
        jobs. O(n) in job count; called once per tick. At 1000 jobs
        and 1Hz ticks this is ~1ms — fine."""
        ts: float | None = None
        for j in load_all_jobs():
            if not j.enabled or j.status != "active":
                continue
            if j.next_fire_at is None:
                continue
            if ts is None or j.next_fire_at < ts:
                ts = j.next_fire_at
        return ts

    def emit_event(
        self,
        *,
        subtype: str,
        message: str,
        details: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        """Forward a SystemEvent through the bridge's emit() channel so
        the renderer / activity log picks it up. session_id defaults to
        a synthetic id so renderer dashboards can filter all scheduler
        events into one stream."""
        try:
            from bridge.freyja_bridge import emit
        except Exception:  # noqa: BLE001
            return
        emit({
            "type": "system_event",
            "sessionId": session_id or "scheduler:global",
            "subtype": subtype,
            "message": message,
            "details": details or {},
        })


def _count_jobs_on_disk() -> int:
    """Cheap job-count for telemetry — pure readdir, no JSON parse."""
    n = 0
    for _ in iter_job_files():
        n += 1
    return n


def _retry_kind(run: Any) -> str:
    """Map a RunRecord's status to its retry category."""
    s = getattr(run, "status", "")
    if s == "timed_out":
        return "timeout"
    if s == "failed":
        # Heuristic — if any delivery_report failed, classify as sink_error.
        # Otherwise it's the agent that failed.
        try:
            for r in run.delivery_reports:
                if not r.success:
                    return "sink_error"
        except Exception:  # noqa: BLE001
            pass
        return "agent_error"
    return ""


def _matches(j: JobRecord, f: JobFilter) -> bool:
    if f.user_id is not None and j.creator.user_id != f.user_id:
        return False
    if f.surface is not None and j.creator.surface != f.surface:
        return False
    if f.status is not None and j.status != f.status:
        return False
    if f.tag is not None and f.tag not in j.tags:
        return False
    if f.enabled is not None and bool(j.enabled) != bool(f.enabled):
        return False
    return True


# ─── High-level build helpers (used by tool + slash handlers) ──────────


def build_creator_ref(
    *,
    surface: str,
    session_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    user_name: str | None = None,
) -> CreatorRef:
    return CreatorRef(
        surface=surface,            # type: ignore[arg-type]
        session_id=session_id,
        user_id=user_id,
        workspace_id=workspace_id,
        chat_id=chat_id,
        thread_id=thread_id,
        user_name=user_name,
    )


def build_schedule(
    when: str | None,
    schedule_dict: dict[str, Any] | None,
    *,
    timezone: str = "UTC",
) -> ScheduleSpec:
    """Resolve either ``when`` (NL) or ``schedule`` (typed dict) into a
    typed ScheduleSpec. ``schedule`` wins if both are passed."""
    if schedule_dict:
        from bridge.scheduler.models import schedule_from_dict
        return schedule_from_dict(schedule_dict)
    if not when:
        raise ValueError("either 'when' or 'schedule' is required")
    return parse_when(when, timezone=timezone)


def build_sinks(
    sink_list: Any,
    *,
    creator: CreatorRef,
    state: Any,
) -> list[SinkSpec]:
    """Convert a list of mixed sink specs (strings like ``"here"``,
    ``"slack:current"``, ``"laptop:/path/to/file"``, or full dicts) into
    typed SinkSpecs. Resolves ``current``/``here`` shortcuts using the
    creator context.

    Robust to several model-friendly input shapes:
      · None / [] / ""           → default sink for creator
      · ["here"]                  → typed list (the canonical form)
      · "here"                    → single sink as a string
      · "[\"here\", \"slack\"]"   → JSON-encoded array (some providers
                                    coerce lists to strings)
      · "here,slack:current"      → comma-separated string
      · {"kind": "slack", ...}    → single sink dict
    """
    # Normalize whatever the caller passed into a Python list of items.
    if sink_list is None:
        return [_default_sink_for_creator(creator)]
    if isinstance(sink_list, str):
        s = sink_list.strip()
        if not s:
            return [_default_sink_for_creator(creator)]
        # JSON array → real list
        if s.startswith("[") and s.endswith("]"):
            import json as _json
            try:
                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    sink_list = parsed
                else:
                    sink_list = [parsed]
            except _json.JSONDecodeError:
                sink_list = [s]
        elif "," in s:
            sink_list = [p.strip() for p in s.split(",") if p.strip()]
        else:
            sink_list = [s]
    elif isinstance(sink_list, dict):
        sink_list = [sink_list]
    elif not isinstance(sink_list, list):
        raise ValueError(
            f"'sinks' must be a list or a string (got {type(sink_list).__name__})"
        )

    if not sink_list:
        # Default: deliver wherever the job was created.
        return [_default_sink_for_creator(creator)]
    out: list[SinkSpec] = []
    for item in sink_list:
        if isinstance(item, dict):
            from bridge.scheduler.models import sink_from_dict
            out.append(sink_from_dict(item))
            continue
        s = str(item).strip()
        if s in ("here", "current"):
            out.append(_default_sink_for_creator(creator))
        elif s in ("slack", "slack:current", "slack:here"):
            if creator.surface != "slack":
                raise ValueError("'slack:current' requires Slack creator context")
            out.append(SlackSinkSpec(
                workspace_id=creator.workspace_id or "",
                chat_id=creator.chat_id or "",
                thread_ts=creator.thread_id,
            ))
        elif s in ("desktop", "desktop:current", "desktop:here"):
            if creator.surface != "desktop":
                raise ValueError("'desktop:current' requires desktop creator context")
            out.append(DesktopSinkSpec(session_id=creator.session_id))
        elif s in ("session", "in_session"):
            out.append(SessionSinkSpec())
        elif s.startswith(("file:", "laptop:", "fs:", "filesystem:")):
            _, _, path = s.partition(":")
            from bridge.scheduler.models import FilesystemSinkSpec
            out.append(FilesystemSinkSpec(path_template=path))
        elif s.startswith(("webhook:", "http:", "https:")):
            from bridge.scheduler.models import WebhookSinkSpec
            url = s if s.startswith(("http:", "https:")) else s.split(":", 1)[1]
            out.append(WebhookSinkSpec(url=url))
        elif s == "noop":
            from bridge.scheduler.models import NoopSinkSpec
            out.append(NoopSinkSpec())
        else:
            raise ValueError(f"unknown sink spec: {item!r}")
    return out


def _default_sink_for_creator(creator: CreatorRef) -> SinkSpec:
    if creator.surface == "slack":
        return SlackSinkSpec(
            workspace_id=creator.workspace_id or "",
            chat_id=creator.chat_id or "",
            thread_ts=creator.thread_id,
        )
    return DesktopSinkSpec(session_id=creator.session_id)


def build_execution(
    execution_value: Any,
    *,
    creator: CreatorRef,
    job_default_session_id: str | None = None,
):
    """Coerce a free-form 'execution' input (string keyword or dict)
    into a typed ExecutionSpec. Defaults to NewSession when None."""
    if execution_value is None:
        return NewSession()
    if isinstance(execution_value, dict):
        from bridge.scheduler.models import execution_from_dict
        return execution_from_dict(execution_value)
    s = str(execution_value).strip().lower()
    if s in ("new", "new_session", "ephemeral"):
        return NewSession()
    if s in ("persistent", "persistent_job_session", "job_session"):
        return PersistentJobSession(session_id=job_default_session_id)
    if s in ("here", "current", "existing", "existing_session"):
        return ExistingSession(session_id=creator.session_id)
    raise ValueError(f"unknown execution kind: {execution_value!r}")
