"""SchedulerService — long-running asyncio service that fires due
jobs and exposes CRUD over scheduled jobs.

Lifecycle:
  · ``start()`` — boot the run loop, load all jobs from disk, recompute
    next_fire_at for everyone, restore from disk.
  · ``stop()`` — graceful shutdown; pending fires finish.

The service is owned by ``_BridgeState``. Surfaces (tool, slash,
HTTP) all call into the same async API. Persistence is per-job;
events.jsonl is the audit log. At-most-once is achieved by
pre-advancing ``next_fire_at`` and persisting BEFORE firing.

Concurrency invariants:
  · One asyncio.Lock per job (created lazily) — prevents the same
    job firing twice in parallel.
  · The execution session has its own serialization via
    ``_schedule_or_queue_turn`` — concurrent fires of different jobs
    into the SAME session will queue, not race.
  · A global semaphore bounds total concurrent fires; default very
    high so we don't artificially throttle small loads.
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
    append_event,
    delete_job,
    ensure_dirs,
    load_all_jobs,
    load_job,
    load_recent_runs_global,
    load_runs_for_job,
    save_job,
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


class SchedulerService:
    """Process-level scheduler. One instance lives on ``_BridgeState``.

    Public API:

      ``await create_job(spec) -> JobRecord``
      ``await list_jobs(filter) -> list[JobRecord]``
      ``await get_job(job_id) -> JobRecord | None``
      ``await update_job(job_id, patch) -> JobRecord``
      ``await pause_job(job_id)``
      ``await resume_job(job_id)``
      ``await remove_job(job_id)``
      ``await run_job_now(job_id) -> RunRecord``
      ``await get_runs(job_id, limit) -> list[RunRecord]``
      ``await cancel_run(run_id)``
      ``await metrics() -> SchedulerMetrics``

    Loop control:

      ``await start()`` / ``await stop()``

    Internal hooks:

      ``advance_self_paced(job_id, delay_seconds)`` — used by the
      auto-registered ``continue_loop`` tool.
      ``complete_self_paced(job_id, reason)`` — used by
      ``complete_loop``.
    """

    def __init__(
        self,
        state: Any,
        *,
        max_concurrent_runs: int = _DEFAULT_MAX_CONCURRENT_RUNS,
    ) -> None:
        self.state = state
        self._jobs: dict[str, JobRecord] = {}
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._in_flight_runs: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._stopped = False
        self._loop_task: asyncio.Task | None = None
        self._concurrency = asyncio.Semaphore(max_concurrent_runs)
        self._daily_cost: dict[str, float] = {}  # job_id → running 24h cost
        # Optional callback for emitting daemon-install hints — set by
        # the bridge's main on first scheduled-job creation.
        self.on_durable_job_created: Any = None

    # ─── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        ensure_dirs()
        for job in load_all_jobs():
            self._jobs[job.id] = job
            # Recompute next_fire_at on every boot. Cron-style schedules
            # need this; one-shots in the past are honored per
            # misfire_policy below.
            self._recompute_next_fire(job, force=True)
        logger.info("scheduler booted: %d jobs", len(self._jobs))
        append_event({
            "type": "scheduler_started",
            "job_count": len(self._jobs),
        })
        self._loop_task = asyncio.create_task(self._run_loop(), name="scheduler-loop")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._loop_task is not None:
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
        append_event({"type": "scheduler_stopped"})

    # ─── Main loop ────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while not self._stopped:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("scheduler tick crashed: %s", exc)
            # Sleep until the next due time, or until wake.
            now = time.time()
            next_at = self._earliest_next_fire()
            if next_at is None:
                # No work to do — wake every hour to re-check (cheap).
                delay = 3600.0
            else:
                delay = max(0.1, next_at - now)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake.clear()

    async def _tick(self) -> None:
        now = time.time()
        due: list[JobRecord] = []
        for job in self._jobs.values():
            if not job.enabled or job.status != "active":
                continue
            nf = job.next_fire_at
            if nf is None:
                continue
            if nf <= now:
                due.append(job)
        if not due:
            return
        # Pre-advance + persist all due jobs BEFORE firing any of them.
        # This is the at-most-once invariant from Hermes.
        for job in due:
            self._recompute_next_fire(job, force=True, after_fire=True)
            save_job(job)
        # Fire concurrently, bounded by the global semaphore.
        for job in due:
            task = asyncio.create_task(
                self._fire_with_concurrency(job),
                name=f"scheduler-fire-{job.id}",
            )
            self._in_flight_runs[task.get_name()] = task

    async def _fire_with_concurrency(self, job: JobRecord) -> None:
        try:
            async with self._concurrency:
                await self._fire_with_lock(job)
        except Exception as exc:  # noqa: BLE001
            logger.exception("fire crashed (job=%s): %s", job.id, exc)
        finally:
            self._in_flight_runs.pop(asyncio.current_task().get_name(), None)

    async def _fire_with_lock(self, job: JobRecord) -> None:
        lock = self._job_locks.setdefault(job.id, asyncio.Lock())
        # Overlap policy gating. The lock guards "this job is currently
        # running"; if held when we arrive, we either queue (await the
        # lock), skip, or cancel the running fire.
        if job.overlap_policy == "skip_if_running" and lock.locked():
            append_event({
                "type": "scheduler_run_skipped",
                "job_id": job.id,
                "reason": "overlap_policy=skip_if_running",
            })
            self.emit_event(
                subtype="scheduler_run_skipped",
                message=f"Scheduled job '{job.name}' skipped (already running)",
                details={"jobId": job.id, "reason": "skip_if_running"},
            )
            return
        if job.overlap_policy == "cancel_running" and lock.locked():
            # Find any in-flight task for this job and cancel it.
            for name, task in list(self._in_flight_runs.items()):
                if name.startswith(f"scheduler-fire-{job.id}") and not task.done():
                    task.cancel()
        async with lock:
            from bridge.scheduler.runtime import fire_job
            run = await fire_job(self, job)
            # Retry policy. If the run failed and the user configured
            # retries, schedule a backoff fire. Subsequent retries
            # multiply the backoff by ``backoff_multiplier`` so quickly
            # ramping into the multi-minute range is the default.
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
                    job.next_fire_at = time.time() + float(delay)
                    save_job(job)
                    append_event({
                        "type": "scheduler_run_retrying",
                        "job_id": job.id,
                        "run_id": run.run_id,
                        "attempt": attempts,
                        "delay_seconds": delay,
                    })
            # If self-paced and the agent didn't set a next_fire_at,
            # default to max_delay_seconds. The runtime emits the
            # warning.
            if isinstance(job.schedule, SelfPacedSchedule) and job.enabled:
                if job.next_fire_at is None or job.next_fire_at <= time.time():
                    job.next_fire_at = time.time() + job.schedule.max_delay_seconds
                    save_job(job)
                    append_event({
                        "type": "scheduler_self_paced_defaulted",
                        "job_id": job.id,
                        "delay_seconds": job.schedule.max_delay_seconds,
                    })
            # Wake so we re-evaluate horizon (job's next_fire_at moved).
            self._wake.set()

    # ─── CRUD ─────────────────────────────────────────────────────────

    async def create_job(self, spec: JobRecord) -> JobRecord:
        """Persist a new job. Computes ``next_fire_at`` from schedule.
        Notifies durable-job hook if anyone subscribed (used by the
        bridge to lazy-install the LaunchAgent daemon)."""
        if not spec.id:
            spec.id = new_job_id()
        spec.created_at = spec.updated_at = time.time()
        self._recompute_next_fire(spec, force=True)
        self._jobs[spec.id] = spec
        save_job(spec)
        append_event({
            "type": "scheduler_job_created",
            "job_id": spec.id,
            "name": spec.name,
            "schedule": spec.schedule.to_dict(),
        })
        self.emit_event(
            subtype="scheduler_job_created",
            message=f"Scheduled job '{spec.name}' created",
            details={
                "jobId": spec.id,
                "cadence": cadence_label(spec.schedule),
                "nextFireAt": spec.next_fire_at,
            },
        )
        self._wake.set()
        # Daemon hint for durable jobs (anything non-ephemeral counts).
        try:
            if self.on_durable_job_created is not None:
                if asyncio.iscoroutinefunction(self.on_durable_job_created):
                    await self.on_durable_job_created(spec)
                else:
                    self.on_durable_job_created(spec)
        except Exception:  # noqa: BLE001
            logger.debug("on_durable_job_created hook failed", exc_info=True)
        return spec

    async def list_jobs(self, filt: JobFilter | None = None) -> list[JobRecord]:
        out = list(self._jobs.values())
        if filt is None:
            return sorted(out, key=lambda j: (j.next_fire_at or 1e18, j.name))
        return sorted(
            [j for j in out if _matches(j, filt)],
            key=lambda j: (j.next_fire_at or 1e18, j.name),
        )

    async def get_job(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id) or load_job(job_id)

    async def update_job(self, job_id: str, patch: JobPatch) -> JobRecord:
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        for field_name in (
            "name", "description", "enabled", "max_fires", "prompt",
            "permission_snapshot", "model_id", "coordination_strategy",
            "skills_to_load", "budget", "sinks", "misfire_policy",
            "overlap_policy", "timeout_seconds", "retry_policy", "tags",
        ):
            val = getattr(patch, field_name)
            if val is not None:
                setattr(job, field_name, val)
        if patch.schedule is not None:
            job.schedule = patch.schedule
            self._recompute_next_fire(job, force=True)
        if patch.execution is not None:
            job.execution = patch.execution
        self._jobs[job.id] = job
        save_job(job)
        append_event({
            "type": "scheduler_job_updated",
            "job_id": job.id,
        })
        self.emit_event(
            subtype="scheduler_job_updated",
            message=f"Scheduled job '{job.name}' updated",
            details={"jobId": job.id, "cadence": cadence_label(job.schedule)},
        )
        self._wake.set()
        return job

    async def pause_job(self, job_id: str) -> None:
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        job.enabled = False
        job.status = "paused"
        save_job(job)
        append_event({"type": "scheduler_job_paused", "job_id": job_id})
        self.emit_event(
            subtype="scheduler_job_paused",
            message=f"Scheduled job '{job.name}' paused",
            details={"jobId": job_id},
        )

    async def resume_job(self, job_id: str) -> None:
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        job.enabled = True
        job.status = "active"
        self._recompute_next_fire(job, force=True)
        save_job(job)
        append_event({"type": "scheduler_job_resumed", "job_id": job_id})
        self.emit_event(
            subtype="scheduler_job_resumed",
            message=f"Scheduled job '{job.name}' resumed",
            details={"jobId": job_id, "nextFireAt": job.next_fire_at},
        )
        self._wake.set()

    async def remove_job(self, job_id: str) -> bool:
        job = self._jobs.pop(job_id, None) or load_job(job_id)
        if job is None:
            return False
        deleted = delete_job(job_id)
        append_event({"type": "scheduler_job_removed", "job_id": job_id})
        self.emit_event(
            subtype="scheduler_job_removed",
            message=f"Scheduled job removed",
            details={"jobId": job_id, "name": getattr(job, "name", "")},
        )
        return deleted

    async def run_job_now(self, job_id: str) -> RunRecord:
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        # Doesn't touch next_fire_at — manual runs don't reset the
        # cadence.
        async with self._concurrency:
            from bridge.scheduler.runtime import fire_job
            return await fire_job(self, job)

    async def get_runs(self, job_id: str, *, limit: int = 50) -> list[RunRecord]:
        return load_runs_for_job(job_id, limit=limit)

    async def cancel_run(self, run_id: str) -> bool:
        for name, task in self._in_flight_runs.items():
            if run_id in name and not task.done():
                task.cancel()
                return True
        return False

    async def metrics(self) -> SchedulerMetrics:
        jobs = list(self._jobs.values())
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
        job = await self.get_job(job_id)
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
        self._wake.set()
        return {"delay_seconds": clamped, "next_fire_at": job.next_fire_at}

    async def complete_self_paced(
        self,
        job_id: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        job.enabled = False
        job.status = "disabled_max_fires"  # reuse — "natural completion"
        job.next_fire_at = None
        save_job(job)
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
            # One-shot already-fired or unparseable cron: disable.
            if isinstance(job.schedule, SelfPacedSchedule):
                return  # loop will set next_fire_at after fire
            job.enabled = False
            job.status = "disabled_max_fires"

    def _earliest_next_fire(self) -> float | None:
        ts: float | None = None
        for j in self._jobs.values():
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
