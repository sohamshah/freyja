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
    write to the job's record: a claim's pre-advance, an edit, a run's
    writes at its start and end. Two processes racing the same due
    job: whoever flocks first wins; the other skips.
  · ``.locks/job-{id}.run.lock`` is held for the whole of a run, so two
    runs of one job never overlap, while edits to the job still go
    through and apply from its next run.
  · ``.wake`` is touched on every CRUD; tick loops poll its mtime
    each iteration (1Hz cap) and re-scan on change. Wake latency is
    bounded by the poll interval; no cross-process asyncio
    primitives needed.
  · ``request_run_cancellation`` writes a flag into the run JSON
    that the firing process polls during its agent turn.

Crash safety: ``fcntl.flock`` is released automatically by the kernel
when the holder exits (clean OR crash). A dead process can never leave
a job permanently locked.

Self-paced loops: the fire registers the loop-control tools and applies
the agent's decision itself, with its other writes to the job's record
at the end of the run (``runtime._apply_loop_decision``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import time
from collections import deque
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
    OnceSchedule,
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
from bridge.scheduler.daemon import (
    always_on_scheduler_installed,
    always_on_scheduler_running,
)
from bridge.scheduler.machine import why_runs_wait
from bridge.scheduler.persistence import (
    FileLock,
    append_event,
    delete_job,
    ensure_dirs,
    iter_job_files,
    job_lock_path,
    job_run_lock_path,
    load_all_jobs,
    load_job,
    load_recent_runs_global,
    load_runs_for_job,
    owner_lock_path,
    read_run_cancel_requested,
    read_wake_mtime,
    request_run_cancellation,
    save_job,
    touch_wake,
    update_job_record,
)
from bridge.scheduler.scheduling import (
    cadence_label,
    compute_next_fire,
    parse_when,
)
from engine.errors import (
    is_api_internal_error,
    is_overloaded_error,
    is_timeout_error,
    is_transient_connection_error,
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

# How often a CRUD-only scheduler tries the owner lock again. When the
# owner exits and nothing restarts it, jobs wait at most this long.
_OWNER_RETRY_SECONDS = 15.0

# "fire_all" catches up at most this many missed slots, the latest ones,
# and none older than a week. Unbounded, an every-minute job missed over
# a weekend ran about 2,880 times in a row.
_CATCH_UP_MAX_RUNS = 10
_CATCH_UP_MAX_AGE_SECONDS = 7 * 24 * 3600

# How long stop() waits for in-flight runs before the process drops
# them. Below launchd's 20 s exit timeout, so a stopping gateway still
# gets to disconnect its adapters.
_STOP_RUN_GRACE_SECONDS = 5.0

# A job whose last runs all failed is stopped and says so where it
# reports. Without this a job that cannot run fires until max_fires,
# silently, since failed runs deliver nothing: one self-paced loop failed
# 180 times in six hours before anyone noticed.
MAX_FAILED_RUNS_IN_A_ROW = 3
_FAILED_RUN_STATUSES = frozenset({"failed", "timed_out"})

# A run that failed for a passing reason (no network, a timeout, an
# overloaded provider) says nothing about the job and does not count
# toward the three above: three runs in DarkWake stopped the Morning
# briefing for good. The job runs again 2 minutes later, then 4, 8 and so
# on up to an hour, never after its next slot. It is stopped only after
# this many failed runs in a row, about a day of retries.
MAX_PASSING_FAILURES_IN_A_ROW = 24
_PASSING_RETRY_FIRST_SECONDS = 120
_PASSING_RETRY_MAX_SECONDS = 3600

# A due run waits out DarkWake and a lost network (machine.py), at most
# this long counted while the Mac is awake: time.monotonic() stops while it
# sleeps. The DarkWake reading comes from an undocumented IOKit function;
# read wrong for good (a headless Mac mini that never sleeps), it held every
# run forever. A sleeping Mac is in DarkWake a few seconds at a time: 1 to
# 12 minutes in all from 06:00 to 09:00, at most 35 in a whole night (pmset,
# Oct 30 to Nov 5), so a run held from 06:00 until the Mac is opened still
# waits for it. One that runs anyway and fails for want of a network is
# retried without counting.
_RUNS_WAIT_AT_MOST_SECONDS = 30 * 60

# Run errors that pass by themselves, beyond those the engine already
# classifies: a provider's 5xx, and a host the network could not reach
# (a sink posting to Slack while offline).
_PASSING_ERROR = re.compile(
    r"error code:? 5\d\d\b|bad gateway|gateway time-?out|cannot connect to host"
    r"|nodename nor servname|name or service not known|name resolution"
    r"|network is unreachable|no route to host",
    re.IGNORECASE,
)


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

    Notes:

      · ``cancel_run`` always works cross-process: it writes
        ``cancel_requested=true`` into the run JSON. The owning
        process polls during its agent turn and cancels the local
        task. Same-process callers also get an immediate
        ``Task.cancel()`` for speed.
      · ``run_job_now`` flocks the job's run lock; if the job is
        already running, it raises ``RuntimeError`` instead of racing.
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
        # While CRUD-only: retries the owner lock until a peer frees it.
        self._claim_task: asyncio.Task | None = None
        # The desktop bridge owns only until a LaunchAgent can.
        self._always_on = True
        self._hand_over_task: asyncio.Task | None = None
        # Jobs that came due but could not fire yet. The loop does not
        # wake early for them: it looks again at its usual once a second.
        self._waiting_job_ids: set[str] = set()
        # Why due runs wait right now (DarkWake, offline), or None.
        self._runs_wait_for: str | None = None
        # When each due job began to wait for the Mac, by time.monotonic().
        self._waiting_since: dict[str, float] = {}
        # Wake-mtime watermark: peers bump ``.wake`` on every CRUD;
        # we use it to know whether to bother re-scanning even if our
        # sleep timed out early. Currently informational — the tick
        # always re-reads disk — but available for future optimisation.
        self._last_wake_mtime: float = 0.0
        # Optional callback for emitting daemon-install hints — set by
        # the bridge's main on first scheduled-job creation.
        self.on_durable_job_created: Any = None

    # ─── Lifecycle ────────────────────────────────────────────────────

    async def start(self, *, always_on: bool = True) -> None:
        """Boot the run loop. Acquires the cross-process owner lock; if
        another scheduler in another process already holds it, this
        instance runs in CRUD-only mode (no tick loop) and tries the
        lock again every ``_OWNER_RETRY_SECONDS``, so it takes over the
        tick when the owner exits.

        The desktop bridge passes ``always_on=False``: it lives only
        while the app is open, and a run it owns dies when the app
        quits. It takes the lock only when no LaunchAgent runs a
        scheduler, and never retries. Retrying, it took the lock while
        the gateway restarted, and the gateway came back CRUD-only. When
        it owns and a LaunchAgent gets installed (the first scheduled
        job installs one), it lets go once launchd runs it.

        A second call while started does nothing. It used to open
        owner.lock again: a flock belongs to the open file description,
        so our own first lock refused the second, the service dropped to
        CRUD-only and stopped its tick loop, and the first descriptor,
        no longer referenced, kept the lock from every peer."""
        if self._owner_lock is not None:
            return
        # A restart after stop(): the loop exits as soon as it sees this.
        self._stopped = False
        self._always_on = always_on
        ensure_dirs()
        # Try to claim ownership of the tick loop. If a peer (daemon
        # or another desktop bridge) already holds it, we still
        # service CRUD — disk + per-job flocks keep us safe — but we
        # don't run a tick. This avoids two processes burning CPU on
        # parallel disk scans for the same jobs.
        self._owner_lock = FileLock(owner_lock_path())
        leave_to_launch_agents = not always_on and always_on_scheduler_installed()
        if not leave_to_launch_agents and self._owner_lock.acquire():
            self._boot_as_owner(mode="owner")
            return
        logger.info(
            "scheduler started in CRUD-only mode (pid %d; %s)",
            os.getpid(),
            "a LaunchAgent runs the scheduler" if leave_to_launch_agents
            else f"peer holds owner lock at {owner_lock_path()}",
        )
        append_event({
            "type": "scheduler_started",
            "mode": "crud_only",
            "job_count": _count_jobs_on_disk(),
        })
        if always_on:
            self._claim_task = asyncio.create_task(
                self._claim_owner_lock_when_free(), name="scheduler-claim-owner",
            )

    async def _claim_owner_lock_when_free(self) -> None:
        """Retry the owner lock until it is ours, then run the tick."""
        while not self._stopped:
            await asyncio.sleep(_OWNER_RETRY_SECONDS)
            if self._stopped or self._owner_lock is None:
                return
            if not self._owner_lock.acquire():
                continue
            try:
                self._boot_as_owner(mode="took_over")
                return
            except Exception:  # noqa: BLE001
                # Holding the lock without a tick would stall every peer.
                logger.exception("scheduler takeover failed; lock released")
                self._is_owner = False
                self._owner_lock.release()

    async def _hand_over_to_launch_agent(self) -> None:
        """Desktop bridge as owner: release the owner lock once a
        LaunchAgent that runs a scheduler is installed and running. Its
        process, CRUD-only until then, takes the tick within 15 s. Held
        on, the lock stayed here until the app quit, and runs this
        process starts die with the app. Runs in flight here keep their
        run locks, so the new owner does not fire them again."""
        while not self._stopped and self._is_owner:
            await asyncio.sleep(_OWNER_RETRY_SECONDS)
            if self._stopped or not self._is_owner or self._owner_lock is None:
                return
            if not always_on_scheduler_installed():
                continue
            if not await asyncio.to_thread(always_on_scheduler_running):
                continue
            self._is_owner = False
            self._owner_lock.release()
            logger.info(
                "scheduler handed the tick to a LaunchAgent (pid %d is CRUD-only now)",
                os.getpid(),
            )
            append_event({"type": "scheduler_handed_over"})
            return

    def _boot_as_owner(self, *, mode: str) -> None:
        self._is_owner = True
        # Owner: recompute next_fire_at for every job and persist —
        # the prior process may have crashed mid-update, leaving stale
        # values. We hold each job's flock briefly while we rewrite.
        # A job not yet due keeps its next fire: recomputing it from now
        # pushed an interval job a whole interval out and dropped a
        # self-paced job's chosen delay on every change of owner.
        # A slot missed while no process ticked is left due when the
        # job's misfire policy catches up, so the tick fires it; under
        # "skip" the recompute moves past it. A slot that waited for the
        # Mac was not missed and stays due.
        boot_count = 0
        now = time.time()
        for job in load_all_jobs():
            with FileLock(job_lock_path(job.id)) as got:
                if not got:
                    # A peer is mutating this job concurrently — skip
                    # the boot recompute; the next tick will catch up
                    # via the normal scan path.
                    continue
                fresh = load_job(job.id) or job
                if _stopped_only_by_passing_failures(fresh):
                    # Stopped before such runs stopped counting: the
                    # Morning briefing and the Routing radar after three
                    # runs in DarkWake. A job the user paused stays paused.
                    fresh.enabled = True
                    fresh.status = "active"
                    logger.info(
                        "job %s (%s) runs again: only passing failures stopped it",
                        fresh.id, fresh.name,
                    )
                    append_event({
                        "type": "scheduler_job_resumed_after_passing_failures",
                        "job_id": fresh.id,
                    })
                if not _catches_up(fresh, now) and not _waits_for_the_mac(fresh, now):
                    self._recompute_next_fire(fresh)
                save_job(fresh)
                boot_count += 1
        touch_wake()
        logger.info(
            "scheduler booted as owner (%s, pid %d): %d jobs",
            mode, os.getpid(), boot_count,
        )
        append_event({
            "type": "scheduler_started",
            "mode": mode,
            "job_count": boot_count,
        })
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="scheduler-loop",
        )
        if not self._always_on:
            self._hand_over_task = asyncio.create_task(
                self._hand_over_to_launch_agent(), name="scheduler-hand-over",
            )

    async def stop(self) -> None:
        self._stopped = True
        for task in (self._claim_task, self._hand_over_task):
            if task is not None:
                task.cancel()
        self._claim_task = self._hand_over_task = None
        if self._loop_task is not None:
            # The loop checks _stopped on each iteration; up to 1s
            # before it notices. Wait up to 5s for clean exit.
            try:
                await asyncio.wait_for(self._loop_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._loop_task.cancel()
        logger.info("scheduler stopped (pid %d, owner=%s)", os.getpid(), self._is_owner)
        # Hand the tick to a peer before waiting on runs. Each in-flight
        # run holds its job's run lock, so the new owner cannot fire it
        # again. Released after the runs, the lock kept every peer idle
        # until the slowest run ended or launchd killed this process.
        if self._owner_lock is not None:
            self._owner_lock.release()
            self._owner_lock = None
            self._is_owner = False
        # Don't kill in-flight runs — they're agent turns and they
        # need to finish cleanly. Wait briefly, then let the process
        # drop them.
        if self._in_flight_runs:
            await asyncio.wait(
                list(self._in_flight_runs.values()),
                timeout=_STOP_RUN_GRACE_SECONDS,
            )
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
            next_at = self._earliest_next_fire_from_disk(now)
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
        due: list[JobRecord] = []
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
            due.append(job)
        if not candidates:
            self._waiting_since.clear()
            return
        # Started in DarkWake or offline, a run fails with "Connection
        # error". Due jobs stay due and start once the Mac is awake and
        # online, or once they have waited too long; a run that starts late
        # says so.
        reason = why_runs_wait()
        if reason is not None:
            if reason != self._runs_wait_for:
                logger.info("%d due job(s) wait: %s", len(candidates), reason)
                append_event({
                    "type": "scheduler_runs_waiting",
                    "reason": reason,
                    "job_ids": candidates,
                })
            self._runs_wait_for = reason
            _note_waiting_slots(due)
            candidates = self._waited_too_long(candidates)
            if not candidates:
                return
        elif self._runs_wait_for is not None:
            logger.info("due jobs start: the wait is over (%s)", self._runs_wait_for)
            self._runs_wait_for = None
            self._waiting_since.clear()
        for job_id in candidates:
            asyncio.create_task(
                self._try_claim_and_fire(job_id, waited_for=reason),
                name=f"scheduler-claim-{job_id}",
            )

    def _waited_too_long(self, job_ids: list[str]) -> list[str]:
        """Those of the due ``job_ids`` that have waited for the Mac for
        ``_RUNS_WAIT_AT_MOST_SECONDS``, and run anyway. The rest wait on."""
        now = time.monotonic()
        self._waiting_since = {j: self._waiting_since.get(j, now) for j in job_ids}
        overdue = [
            j for j in job_ids
            if now - self._waiting_since[j] >= _RUNS_WAIT_AT_MOST_SECONDS
        ]
        self._waiting_job_ids.update(j for j in job_ids if j not in overdue)
        return overdue

    async def _try_claim_and_fire(self, job_id: str, *, waited_for: str | None = None) -> None:
        """Acquire the job's record lock, re-read the job under it,
        verify still due, take its run lock, pre-advance + persist, then
        fire. If the record lock is already held (a peer process or our
        own concurrent claim, or an edit), skip — the next tick looks
        again. If the run lock is held, the job's last run is still going.
        ``waited_for`` is why the Mac cannot run jobs, when this one runs
        anyway because it waited too long.
        """
        lock = FileLock(job_lock_path(job_id))
        if not lock.acquire():
            # Another process / claim task got here first. Their
            # pre-advance + fire is canonical; we drop out.
            self._waiting_job_ids.add(job_id)
            return
        self._waiting_job_ids.discard(job_id)
        run_lock = FileLock(job_run_lock_path(job_id))
        try:
            # Re-read under the lock — the peer may have already
            # advanced next_fire_at past now.
            job = load_job(job_id)
            if job is None or not job.enabled or job.status != "active":
                return
            now = time.time()
            if job.next_fire_at is None or job.next_fire_at > now:
                return
            if not run_lock.acquire():
                self._came_due_while_running(job)
                return
            if _misfire_policy(job) == "fire_all":
                _bound_catch_up(job, now)
                if job.next_fire_at is None or job.next_fire_at > now:
                    # Every missed slot was too old to run.
                    save_job(job)
                    touch_wake()
                    return
            # The slot this fire is for: a run that starts long after it
            # says how late it is.
            slot = job.next_fire_at
            attempt = job.retry_attempt
            # Pre-advance + persist BEFORE firing — at-most-once.
            job.waiting_slot = None
            self._recompute_next_fire(job, force=True, after_fire=True)
            save_job(job)
            touch_wake()
            # Hand off to the fire path while STILL holding the run
            # lock — the fire path releases it after the run completes.
            # The record lock goes now, so the job can be edited while
            # it runs; the run keeps the record it was claimed with.
            task = asyncio.create_task(
                self._fire_with_concurrency(job, run_lock, slot, attempt),
                name=f"scheduler-fire-{job.id}",
            )
            self._in_flight_runs[task.get_name()] = task
            # Don't release run_lock here — _fire_with_concurrency owns it.
            run_lock = None
            if waited_for is not None:
                # A DarkWake or offline reading that is wrong shows here.
                waited = time.monotonic() - self._waiting_since.get(job.id, time.monotonic())
                logger.warning(
                    "job %s (%s) waited %d min (%s) and runs anyway",
                    job.id, job.name, waited // 60, waited_for,
                )
                append_event({
                    "type": "scheduler_run_waited_too_long",
                    "job_id": job.id,
                    "reason": waited_for,
                    "waited_seconds": round(waited),
                })
        finally:
            lock.release()
            if run_lock is not None:
                run_lock.release()

    def _came_due_while_running(self, job: JobRecord) -> None:
        """``job`` is due while its last run still goes on (the caller
        holds its record lock). Its overlap policy decides: queue fires
        once that run ends, skip_if_running drops the slot, and
        cancel_running asks that run to stop, then fires. A self-paced
        job's next fire is set when its run ends; the time it came due by
        is a placeholder, so it waits."""
        policy = _overlap_policy(job)
        if isinstance(job.schedule, SelfPacedSchedule):
            policy = "queue"
        if policy == "skip_if_running":
            slot = job.next_fire_at
            self._recompute_next_fire(job, force=True, after_fire=True)
            save_job(job)
            touch_wake()
            logger.info("job %s: skipped the slot at %s; its last run is still going", job.id, slot)
            append_event({"type": "scheduler_slot_skipped_running", "job_id": job.id, "slot": slot})
            return
        self._waiting_job_ids.add(job.id)
        if policy != "cancel_running":
            return
        # The newest run is the one going on: runs of a job never overlap.
        # Until it has a record, the next tick looks again.
        last = next(iter(load_runs_for_job(job.id, limit=1)), None)
        if last is None or last.finished_at is not None:
            return
        if read_run_cancel_requested(job.id, last.run_id):
            return
        request_run_cancellation(job.id, last.run_id)
        logger.info("job %s: due again; asked its run %s to stop", job.id, last.run_id)
        append_event({
            "type": "scheduler_run_cancelled_for_next",
            "job_id": job.id,
            "run_id": last.run_id,
        })

    async def _fire_with_concurrency(
        self, job: JobRecord, run_lock: FileLock, slot: float | None, attempt: int = 0,
    ) -> None:
        """Holds the job's run lock for the duration of the fire."""
        task_name = asyncio.current_task().get_name()
        try:
            async with self._concurrency:
                await self._fire_under_lock(job, slot, attempt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("fire crashed (job=%s): %s", job.id, exc)
        finally:
            run_lock.release()
            self._in_flight_runs.pop(task_name, None)

    async def _fire_under_lock(
        self, job: JobRecord, slot: float | None, attempt: int = 0,
    ) -> None:
        """Run the agent turn for ``job``'s ``slot``, retry ``attempt`` of
        it under the job's retry_policy. Our caller holds the job's run
        lock; the failure stop and retry writes take the record lock and
        apply to the record as it is now. A run that failed for a passing
        reason is retried by its own rule and does not use up a retry; a
        slot is counted toward the stop once its retries are spent."""
        from bridge.scheduler.runtime import fire_job
        run = await fire_job(self, job, scheduled_for=slot, attempt=attempt)
        passing = _failed_for_a_passing_reason(run)
        if not passing and await self._retry_by_policy(job, run):
            return
        if await self._stop_if_failing(job, run):
            return
        if passing:
            await self._retry_after_passing_failure(job, run)

    async def _retry_by_policy(self, job: JobRecord, run: RunRecord) -> bool:
        """Run ``job`` again after its failed ``run`` when its retry_policy
        says so: at most ``max_retries`` times per slot, ``backoff_seconds``
        after the slot's own run, ``backoff_multiplier`` times longer after
        each retry. Returns True if it will. The attempt used to be read
        from a run attribute nothing set, so every failure was the first:
        retried without end, always after ``backoff_seconds``."""
        retry = job.retry_policy
        if (
            retry is None
            or run.status not in _FAILED_RUN_STATUSES
            or run.attempt >= retry.max_retries
            or (retry.on and _retry_kind(run) not in retry.on)
        ):
            return False
        attempt = run.attempt + 1
        delay = retry.backoff_seconds * retry.backoff_multiplier ** (attempt - 1)
        if not await self._run_again_at(job, time.time() + delay, attempt):
            return False
        append_event({
            "type": "scheduler_run_retrying",
            "job_id": job.id,
            "run_id": run.run_id,
            "attempt": attempt,
            "delay_seconds": delay,
        })
        return True

    async def _run_again_at(self, job: JobRecord, retry_at: float, attempt: int) -> bool:
        """Set ``job``'s next fire to ``retry_at``, as retry ``attempt`` of
        its last slot: not when its next slot comes sooner, nor when it was
        paused or stopped meanwhile. A one-shot whose only slot fired runs
        again. Returns True if it will."""
        retried = False

        def _retry(fresh: JobRecord) -> None:
            nonlocal retried
            if fresh.enabled and fresh.status == "active":
                if fresh.next_fire_at is not None and fresh.next_fire_at <= retry_at:
                    return
            elif not _one_shot_used_up(fresh):
                # Paused or stopped meanwhile.
                return
            fresh.enabled = True
            fresh.status = "active"
            fresh.next_fire_at = retry_at
            fresh.retry_attempt = attempt
            retried = True

        await update_job_record(job.id, _retry)
        return retried

    async def _retry_after_passing_failure(self, job: JobRecord, run: RunRecord) -> None:
        """Run ``job`` again soon after its ``run`` failed for a passing
        reason: 2 minutes later, twice as long after each such failure in
        a row, at most an hour, and never after its next slot. It is the
        same retry of the slot as ``run`` was."""
        _, passing = _failures_in_a_row(
            load_runs_for_job(job.id, limit=MAX_PASSING_FAILURES_IN_A_ROW)
        )
        delay = min(
            _PASSING_RETRY_MAX_SECONDS,
            _PASSING_RETRY_FIRST_SECONDS * 2 ** (max(passing, 1) - 1),
        )
        if not await self._run_again_at(job, time.time() + delay, run.attempt):
            return
        logger.info(
            "job %s: run failed for a passing reason; runs again in %d s",
            job.id, delay,
        )
        append_event({
            "type": "scheduler_run_retrying",
            "job_id": job.id,
            "attempt": passing,
            "delay_seconds": delay,
            "reason": "passing_failure",
        })

    async def _stop_if_failing(self, job: JobRecord, run: RunRecord) -> bool:
        """Disable ``job`` when its last ``MAX_FAILED_RUNS_IN_A_ROW`` runs
        all failed, not counting runs that failed for a passing reason
        nor a slot's retries under retry_policy (or after
        ``MAX_PASSING_FAILURES_IN_A_ROW`` failed runs of any kind), and
        post why to its sinks. Returns True if stopped."""
        if run.status not in _FAILED_RUN_STATUSES:
            return False
        recent = load_runs_for_job(job.id, limit=MAX_PASSING_FAILURES_IN_A_ROW)
        if not _should_stop(recent):
            return False
        stopped = False

        def _stop(fresh: JobRecord) -> None:
            nonlocal stopped
            if not fresh.enabled:
                return
            fresh.enabled = False
            fresh.status = "disabled_error"
            fresh.next_fire_at = None
            stopped = True

        fresh = await update_job_record(job.id, _stop)
        if fresh is None or not stopped:
            return False
        error = (run.error or _sink_errors(run) or "no error was recorded")[:600]
        notice = (
            f"I stopped the scheduled job *{fresh.name}*: its last "
            f"{sum(_failures_in_a_row(recent))} runs failed. Last error: {error}"
        )
        append_event({
            "type": "scheduler_job_stopped_failing",
            "job_id": job.id,
            "run_id": run.run_id,
            "error": error,
        })
        self.emit_event(
            subtype="scheduler_job_stopped_failing",
            message=notice,
            details={"jobId": job.id, "runId": run.run_id, "error": error},
        )
        from bridge.scheduler.persistence import write_run_output
        from bridge.scheduler.sinks import RunOutput, SinkContext, deliver_all

        # A notice only in sinks nobody reads tells no one: the Morning
        # briefing's sink is noop. It also goes to the conversation the
        # job was made in.
        told = fresh
        creator_sink = _creator_conversation(fresh.creator)
        if creator_sink is not None and not any(
            isinstance(s, (SlackSinkSpec, DesktopSinkSpec)) for s in fresh.sinks
        ):
            told = dataclasses.replace(fresh, sinks=[*fresh.sinks, creator_sink])
        try:
            await deliver_all(
                told,
                run,
                RunOutput(text=notice, attachments=[], run=run, job=told),
                SinkContext(state=self.state, outputs_dir=write_run_output, log=logger.info),
            )
        except Exception:  # noqa: BLE001
            logger.exception("failure notice for job %s was not delivered", job.id)
        return True

    # ─── CRUD ─────────────────────────────────────────────────────────

    async def create_job(self, spec: JobRecord) -> JobRecord:
        """Persist a new job. Computes ``next_fire_at`` from schedule.
        Reads back from disk before returning so the caller gets the
        canonical (post-pre-advance, post-validation) record."""
        _check_misfire_policy(spec.misfire_policy)
        _check_overlap_policy(spec.overlap_policy)
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
        _check_misfire_policy(patch.misfire_policy)
        _check_overlap_policy(patch.overlap_policy)
        with FileLock(job_lock_path(job_id)) as got:
            if not got:
                # A peer is writing this job's record, for milliseconds
                # (a run holds only its run lock) — the caller can retry.
                # We could alternatively block here on flock; preferring
                # fail-fast keeps the surface simple.
                raise RuntimeError(
                    f"job {job_id} is locked by a peer; retry shortly"
                )
            job = load_job(job_id)
            if job is None:
                raise KeyError(job_id)
            for field_name in (
                "name", "description", "max_fires", "prompt",
                "permission_snapshot", "model_id", "coordination_strategy",
                "skills_to_load", "budget", "artifact", "memory", "sinks",
                "misfire_policy", "overlap_policy", "timeout_seconds",
                "retry_policy", "tags",
            ):
                val = getattr(patch, field_name)
                if val is not None:
                    setattr(job, field_name, val)
            # enabled pauses or resumes the job, as pause_job and
            # resume_job do. Set alone, a paused job kept status "paused",
            # which the tick skips, and never ran again.
            resumed = False
            if patch.enabled is not None and patch.enabled != (
                job.enabled and job.status == "active"
            ):
                job.enabled = patch.enabled
                job.status = "active" if patch.enabled else "paused"
                resumed = patch.enabled
            if patch.schedule is not None:
                job.schedule = patch.schedule
            if patch.schedule is not None or resumed:
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
        for jlp in (job_lock_path(job_id), job_run_lock_path(job_id)):
            try:
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
        """Fire a job immediately. Acquires the job's run lock so a
        concurrent scheduled tick won't start a second run. Doesn't touch
        ``next_fire_at`` — manual runs are independent of cadence."""
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        lock = FileLock(job_run_lock_path(job_id))
        if not lock.acquire():
            raise RuntimeError(
                f"job {job_id} is running; try again when its run ends"
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
        # A next fire from the schedule is a slot's own run, not a retry.
        job.retry_attempt = 0
        now = time.time()
        last = job.last_fire_at if after_fire else None
        # "fire_all" runs every missed slot: step on from the slot being
        # fired, not from now, so an overdue job stays due until each
        # missed slot has had its run (within _bound_catch_up's limits).
        if (
            after_fire
            and _misfire_policy(job) == "fire_all"
            and job.next_fire_at is not None
            and not isinstance(job.schedule, SelfPacedSchedule)
        ):
            now = last = job.next_fire_at
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

    def _earliest_next_fire_from_disk(self, now: float) -> float | None:
        """Scan disk for the minimum next_fire_at across all active
        jobs. O(n) in job count; called once per tick. At 1000 jobs
        and 1Hz ticks this is ~1ms — fine. A due job that could not fire
        yet is left out: counted, it kept the loop waking every 0.05 s
        until it could."""
        ts: float | None = None
        for j in load_all_jobs():
            if not j.enabled or j.status != "active":
                continue
            if j.next_fire_at is None:
                continue
            if j.next_fire_at <= now and j.id in self._waiting_job_ids:
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


def _check_misfire_policy(policy: str | None) -> None:
    """Refuse a misfire policy the scheduler does not know. Empty means
    unset. The dashboard offered "run_now", saved as is and read as skip."""
    if policy and policy not in ("skip", "fire_once", "fire_all"):
        raise ValueError(
            f"unknown misfire_policy {policy!r}: use skip, fire_once or fire_all"
        )


_OVERLAP_POLICIES = ("queue", "skip_if_running", "cancel_running")


def _check_overlap_policy(policy: str | None) -> None:
    """Refuse an overlap policy the scheduler does not know. Empty means
    unset (queue). The dashboard offered "skip", saved as is."""
    if policy and policy not in _OVERLAP_POLICIES:
        raise ValueError(
            f"unknown overlap_policy {policy!r}: use queue, skip_if_running "
            "or cancel_running"
        )


def _overlap_policy(job: JobRecord) -> str:
    """``job``'s overlap policy; unset is queue. "skip", which the
    dashboard saved before this was checked, meant skip_if_running."""
    policy = job.overlap_policy or "queue"
    if policy == "skip":
        return "skip_if_running"
    return policy if policy in _OVERLAP_POLICIES else "queue"


def _misfire_policy(job: JobRecord) -> str:
    """``job``'s misfire policy, or its default when never set. A one-shot
    defaults to fire_once: under skip, a reminder whose time passed while
    no scheduler ran was disabled without ever running or saying so."""
    if job.misfire_policy:
        return job.misfire_policy
    return "fire_once" if isinstance(job.schedule, OnceSchedule) else "skip"


def _catches_up(job: JobRecord, now: float) -> bool:
    """True when ``job`` missed a slot and its misfire policy runs it."""
    return (
        _misfire_policy(job) in ("fire_once", "fire_all")
        and job.enabled
        and job.status == "active"
        and job.next_fire_at is not None
        and job.next_fire_at <= now
    )


def _waits_for_the_mac(job: JobRecord, now: float) -> bool:
    """True when ``job`` is due on a slot that came due while a scheduler
    ran and waited for the Mac (``waiting_slot``), so it was not missed."""
    return (
        job.enabled
        and job.status == "active"
        and job.next_fire_at is not None
        and job.next_fire_at <= now
        and job.waiting_slot == job.next_fire_at
    )


def _note_waiting_slots(jobs: list[JobRecord]) -> None:
    """Note on each due job's record that its slot waits for the Mac. A
    gateway restart during the wait recomputed a "skip" job's next fire
    from now, and that day's run was dropped as missed."""
    for job in jobs:
        if job.waiting_slot == job.next_fire_at:
            continue
        with FileLock(job_lock_path(job.id)) as got:
            if not got:
                # Being claimed or edited; the next tick looks again.
                continue
            fresh = load_job(job.id)
            if fresh is None or fresh.next_fire_at != job.next_fire_at:
                continue
            fresh.waiting_slot = fresh.next_fire_at
            save_job(fresh)


def _failed_for_a_passing_reason(run: RunRecord) -> bool:
    """True when ``run`` failed for a reason that passes by itself: the
    network was down (the Mac asleep or in DarkWake, no Wi-Fi), a request
    timed out, or the provider was overloaded or failed. A wall-clock
    timeout or a budget overrun ends a run as timed_out, not failed: those
    are the job's own."""
    if run.status != "failed":
        return False
    errors = [run.error] if run.error else [
        r.error or "" for r in run.delivery_reports if not r.success
    ]
    return bool(errors) and all(_passing_error(e) for e in errors)


def _passing_error(error: str) -> bool:
    return bool(error) and (
        is_transient_connection_error(error)
        or is_timeout_error(error)
        or is_overloaded_error(error)
        or is_api_internal_error(error)
        or _PASSING_ERROR.search(error) is not None
    )


def _sink_errors(run: RunRecord) -> str:
    return "; ".join(r.error for r in run.delivery_reports if not r.success and r.error)


def _failures_in_a_row(runs: list[RunRecord]) -> tuple[int, int]:
    """``(real, passing)``: how many runs at the head of ``runs``
    (newest first) failed, by cause. A slot's failed retries under the
    job's retry_policy count as one with its own run: counted alone, a job
    allowing two retries was stopped after one bad slot."""
    real = passing = 0
    for r in runs:
        if r.status not in _FAILED_RUN_STATUSES:
            break
        if _failed_for_a_passing_reason(r):
            passing += 1
        elif r.attempt == 0:
            real += 1
    return real, passing


def _should_stop(runs: list[RunRecord]) -> bool:
    real, passing = _failures_in_a_row(runs)
    return (
        real >= MAX_FAILED_RUNS_IN_A_ROW
        or real + passing >= MAX_PASSING_FAILURES_IN_A_ROW
    )


def _stopped_only_by_passing_failures(job: JobRecord) -> bool:
    """True when ``job`` was stopped for failing, but its runs do not
    stop it now that passing failures do not count."""
    if job.enabled or job.status != "disabled_error":
        return False
    runs = load_runs_for_job(job.id, limit=MAX_PASSING_FAILURES_IN_A_ROW)
    return bool(runs) and _failed_for_a_passing_reason(runs[0]) and not _should_stop(runs)


def _one_shot_used_up(job: JobRecord) -> bool:
    """True when ``job`` is a one-shot that was disabled because its
    only slot fired (the pre-advance does it before the run)."""
    return (
        isinstance(job.schedule, OnceSchedule)
        and job.status == "disabled_max_fires"
        and (job.max_fires is None or job.fire_count < job.max_fires)
    )


def _creator_conversation(creator: CreatorRef) -> SinkSpec | None:
    """A sink to the desktop session or Slack thread a job was made in;
    None for a job no person made (the Morning briefing)."""
    if creator.surface == "slack" and creator.chat_id:
        return SlackSinkSpec(
            workspace_id=creator.workspace_id or "",
            chat_id=creator.chat_id,
            thread_ts=creator.thread_id,
        )
    if creator.surface == "desktop" and creator.session_id:
        return DesktopSinkSpec(session_id=creator.session_id)
    return None


def _bound_catch_up(job: JobRecord, now: float) -> None:
    """Move an overdue "fire_all" job past the missed slots it will not
    run: all but the latest ``_CATCH_UP_MAX_RUNS``, and any older than
    ``_CATCH_UP_MAX_AGE_SECONDS``. The event log records the dropped
    slots: every slot of the schedule from ``from`` until ``until``."""
    if isinstance(job.schedule, (OnceSchedule, SelfPacedSchedule)):
        return
    first = job.next_fire_at
    slot = first
    cutoff = now - _CATCH_UP_MAX_AGE_SECONDS
    if slot < cutoff:
        # Jump, rather than step through, slots too old to run.
        slot = compute_next_fire(job.schedule, now=cutoff, last_fire=first)
    kept: deque[float] = deque(maxlen=_CATCH_UP_MAX_RUNS)
    while slot is not None and slot <= now:
        kept.append(slot)
        slot = compute_next_fire(job.schedule, now=slot, last_fire=slot)
    until = kept[0] if kept else slot
    if until == first:
        return
    job.next_fire_at = until
    logger.info(
        "job %s (fire_all): dropped the missed slots from %s until %s; "
        "%d catch-up runs",
        job.id, first, until, len(kept),
    )
    append_event({
        "type": "scheduler_missed_slots_dropped",
        "job_id": job.id,
        "from": first,
        "until": until,
        "catch_up_runs": len(kept),
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
