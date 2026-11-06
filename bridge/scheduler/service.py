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
import contextlib
import dataclasses
import datetime
import logging
import os
import re
import time
from collections import Counter, deque
from typing import TYPE_CHECKING, Any, AsyncIterator

from bridge.scheduler.models import (
    CLEARABLE_FIELDS,
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
    RetryPolicy,
    RunRecord,
    SchedulerMetrics,
    ScheduleSpec,
    SelfPacedSchedule,
    SessionSinkSpec,
    SinkSpec,
    SlackSinkSpec,
    new_job_id,
    new_run_id,
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
    load_run_job_ids,
    load_runs_for_job,
    load_runs_started_since,
    load_unfinished_runs,
    owner_lock_path,
    read_run_cancel_requested,
    read_wake_mtime,
    request_run_cancellation,
    save_job,
    save_run,
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
_FAILED_RUN_STATUSES = frozenset({"failed", "timed_out", "budget_exceeded"})
# The runs that did not complete, as the dashboard counts them
# (FAILED_RUN_STATUSES in ScheduledJobsDashboard.tsx): a run cut off by a
# crash or a restart too. That is no failure of the job's own, so the stop
# and the retries leave it out.
_DID_NOT_COMPLETE_STATUSES = _FAILED_RUN_STATUSES | {"interrupted"}

# A run that failed for a passing reason (no network, a timeout, an
# overloaded provider) says nothing about the job and does not count
# toward the three above: three runs in DarkWake stopped the Morning
# briefing for good. The job runs again 2 minutes later, then 4, 8 and so
# on up to an hour, never after its next slot. It is stopped only after
# this many failed runs in a row, about a day of retries.
MAX_PASSING_FAILURES_IN_A_ROW = 24
_PASSING_RETRY_FIRST_SECONDS = 120
_PASSING_RETRY_MAX_SECONDS = 3600

# The most retries a retry_policy gives a slot. The runs read to decide a
# stop hold three slots with all their retries and the passing failures
# between them: the last 24 runs held two slots of a job allowing 8
# retries, so a job that always failed was never stopped.
MAX_RETRIES = 10
_FAILURE_WINDOW_RUNS = (
    MAX_FAILED_RUNS_IN_A_ROW * (MAX_RETRIES + 1) + MAX_PASSING_FAILURES_IN_A_ROW
)

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

# A fire the gateway or the app refused, or that never reached it, is
# due again, and handed again on the loop's next look. Back again, it waits
# a second more, twice as long each time after, at most a minute. Handed
# again every second, a refusal that never cleared wrote a control-channel
# line and log lines each time.
_REFUSED_RETRY_FIRST_SECONDS = 1.0
_REFUSED_RETRY_MAX_SECONDS = 60.0

# A run now handed to the gateway or the app waits in its control file
# until that process reads it. Sent just before the process exited, it ran
# whenever the process next started, maybe days later. Read later than this
# after it was sent, it is recorded as expired and does not run.
_HANDED_RUN_NOW_EXPIRES_SECONDS = 5 * 60

# A run whose process exited before it ended (a crash, a quit app, a
# restart) is still "claimed", "running" or "delivering" on disk. Every
# scheduler start looks for such runs among all of each job's runs: one
# cut off while a later run held the run lock at each start, or before
# this look existed, is not among the newest.
_UNFINISHED_RUN_STATUSES = frozenset({"claimed", "running", "delivering"})

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
      ``await start_job_now(job_id) -> RunRecord``
      ``await get_runs(job_id, limit) -> list[RunRecord]``
      ``await cancel_run(run_id) -> bool``
      ``await metrics() -> SchedulerMetrics``

    Loop control:

      ``await start()`` / ``await stop()``

    Notes:

      · ``cancel_run`` always works cross-process: it writes
        ``cancel_requested=true`` into the run JSON. The owning
        process polls during its agent turn and cancels the local
        task. A run of the same process also stops at once
        (``runtime.stop_run_now``), without waiting for its next poll.
      · ``run_job_now`` and ``start_job_now`` flock the job's run lock;
        if the job is already running, they raise ``RuntimeError``
        instead of racing.
    """

    def __init__(
        self,
        state: Any,
        *,
        max_concurrent_runs: int = _DEFAULT_MAX_CONCURRENT_RUNS,
    ) -> None:
        self.state = state
        # In-flight runs owned by THIS process — used for graceful
        # shutdown. Peer-process runs are not visible here; they're
        # cancelled via the cancel_requested flag on the run JSON.
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
        # Of those, the ones whose session waits for the process that runs
        # it: the gateway for a Slack thread, the app for a desktop session.
        self._waiting_for_owner: set[str] = set()
        # The scheduler sessions that runs going on in this process load,
        # each counted once per run (holds_session).
        self._sessions_in_runs: Counter[str] = Counter()
        # Why due runs wait right now (DarkWake, offline), or None.
        self._runs_wait_for: str | None = None
        # When each due job began to wait for the Mac, by time.monotonic().
        self._waiting_since: dict[str, float] = {}
        # Each job's last fire handed to the gateway or the app: its slot,
        # how many times in a row that slot was handed, when it may be
        # handed again if it comes back due, by time.monotonic(), and when
        # it was handed, by time.time().
        self._handed: dict[str, tuple[float | None, int, float, float]] = {}
        # Wake-mtime watermark: peers bump ``.wake`` on every CRUD;
        # we use it to know whether to bother re-scanning even if our
        # sleep timed out early. Currently informational — the tick
        # always re-reads disk — but available for future optimisation.
        self._last_wake_mtime: float = 0.0
        # Optional callback for emitting daemon-install hints — set by
        # the bridge's main on first scheduled-job creation.
        self.on_durable_job_created: Any = None

    # ─── Lifecycle ────────────────────────────────────────────────────

    def holds_session(self, session_id: str) -> bool:
        """True when this process loads, runs and saves the scheduler
        session ``session_id``: while it runs the tick loop, which fires
        the jobs that run in one, and while a run here that loads it goes
        on after the tick went to another process (the app hands it to a
        LaunchAgent, stop() to a peer). That run's saves are the job's."""
        return self._is_owner or self._sessions_in_runs[session_id] > 0

    @contextlib.asynccontextmanager
    async def _runs_here(self, job: JobRecord) -> AsyncIterator[None]:
        """Count the scheduler session a run of ``job`` loads as this
        process's while the run goes on (holds_session)."""
        session = _scheduler_session_of(job)
        if session is not None:
            self._sessions_in_runs[session] += 1
        try:
            yield
        finally:
            if session is not None:
                self._sessions_in_runs[session] -= 1
                if not self.holds_session(session):
                    self._stop_writing_events_held_elsewhere()

    def _turn_on_a_job_session(self) -> bool:
        """True while a scheduler session loaded here has a turn going that
        no run here holds (a talk wake, a memo wake). Such a turn holds no
        run lock: with the tick handed over under it, the new owner could
        run the job on its own copy meanwhile, and the turn's saves were
        dropped (holds_session). Stopping it at the handover, with its
        input put back in the inbox, would run its tools again there."""
        from bridge.scheduler.runtime import scheduler_session_id

        prefix = scheduler_session_id("")
        for sid, sess in list((getattr(self.state, "sessions", None) or {}).items()):
            pending = getattr(sess, "pending_task", None)
            if (
                sid.startswith(prefix)
                and not self._sessions_in_runs[sid]
                and pending is not None
                and not pending.done()
            ):
                return True
        return False

    @staticmethod
    def _stop_writing_events_held_elsewhere() -> None:
        """Close this process's events files of the job sessions it no
        longer holds: emit() writes to one while it has it open."""
        from bridge.freyja_bridge import _close_event_files_held_elsewhere

        _close_event_files_held_elsewhere()

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
        # Before the boot below reads the jobs such runs used up.
        _settle_interrupted_runs()
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
        run locks, so the new owner does not fire them again. A turn on a
        job's session that is not a run of it holds the lock here until it
        ends (_turn_on_a_job_session)."""
        while not self._stopped and self._is_owner:
            await asyncio.sleep(_OWNER_RETRY_SECONDS)
            if self._stopped or not self._is_owner or self._owner_lock is None:
                return
            if not always_on_scheduler_installed():
                continue
            if not await asyncio.to_thread(always_on_scheduler_running):
                continue
            if self._turn_on_a_job_session():
                continue
            self._is_owner = False
            self._owner_lock.release()
            self._stop_writing_events_held_elsewhere()
            logger.info(
                "scheduler handed the tick to a LaunchAgent (pid %d is CRUD-only now)",
                os.getpid(),
            )
            append_event({"type": "scheduler_handed_over"})
            return

    def _boot_as_owner(self, *, mode: str) -> None:
        # Copies of job sessions loaded while another process held them
        # would count as ours now, with what that process ran since
        # missing from them.
        from bridge.freyja_bridge import _drop_job_session_copies

        _drop_job_session_copies(self.state)
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
        # Mac, the gateway, the app or a run of the job was not missed and
        # stays due, as does one whose fire did not run there, and a
        # one-shot's that was resumed or whose run was cut off.
        boot_count = 0
        now = time.time()
        aimed_at_sub_agents: list[tuple[JobRecord, str]] = []
        resumed: list[JobRecord] = []
        for job in load_all_jobs():
            with FileLock(job_lock_path(job.id)) as got:
                if not got:
                    # A peer is mutating this job concurrently — skip
                    # the boot recompute; the next tick will catch up
                    # via the normal scan path.
                    continue
                fresh = load_job(job.id) or job
                if _stopped_only_by_passing_failures(fresh):
                    resumed.append(fresh)
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
                why = _execution_refused(fresh) if fresh.enabled else None
                if why is not None:
                    # Made before such jobs were refused: every fire failed
                    # until three failures stopped it. Stopped once, here.
                    fresh.enabled = False
                    fresh.status = "disabled_error"
                    aimed_at_sub_agents.append((fresh, why))
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
        # Saved: the dashboard and the sidebar read a job back on its event.
        for job in resumed:
            self.emit_event(
                subtype="scheduler_job_resumed_after_passing_failures",
                message=f"Scheduled job '{job.name}' runs again: only passing failures stopped it",
                details={"jobId": job.id, "nextFireAt": job.next_fire_at},
            )
        for job, why in aimed_at_sub_agents:
            asyncio.create_task(
                self._tell_stopped_aimed_at_a_sub_agent(job, why),
                name=f"scheduler-stopped-{job.id}",
            )
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
            self._stop_writing_events_held_elsewhere()
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
            in_gateway = _fires_in_the_gateway(job)
            in_app = not in_gateway and _fires_in_the_app(job)
            if (in_gateway and not _gateway_runs()) or (in_app and not _app_runs()):
                # Its Slack thread runs only in the gateway, and its desktop
                # session only in the app: it stays due until that runs.
                if job.id not in self._waiting_for_owner:
                    owner = "the Slack gateway" if in_gateway else "the app"
                    logger.info("job %s (%s) waits for %s", job.id, job.name, owner)
                    append_event({
                        "type": "scheduler_run_waits_for_gateway" if in_gateway else "scheduler_run_waits_for_app",
                        "job_id": job.id,
                    })
                if job.waiting_slot != job.next_fire_at:
                    # Not missed: under skip, a new owner's boot moved past it.
                    job.waiting_slot = job.next_fire_at
                    save_job(job)
                self._waiting_for_owner.add(job.id)
                self._waiting_job_ids.add(job.id)
                return
            self._waiting_for_owner.discard(job.id)
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
            if (in_gateway or in_app) and self._hand_over_later(job, slot):
                self._waiting_job_ids.add(job.id)
                return
            # Pre-advance + persist BEFORE firing — at-most-once.
            job.waiting_slot = None
            self._recompute_next_fire(job, force=True, after_fire=True)
            save_job(job)
            touch_wake()
            if in_gateway or in_app:
                # The gateway or the app takes the run lock for the run.
                run_lock.release()
                run_lock = None
                self._note_handed(job, slot)
                if not _hand_over(job, slot, attempt, to_app=in_app) and _due_again(job, slot, attempt):
                    save_job(job)
                # Due again if not sent or refused: looked at again on the
                # loop's usual once a second, not every 0.05 s.
                self._waiting_job_ids.add(job.id)
                return
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

    def _hand_over_later(self, job: JobRecord, slot: float | None) -> bool:
        """True when the fire of ``job`` for ``slot`` was handed to the
        gateway or the app, came back due (refused, or not sent), and may
        not be handed again yet. A run of the job that started since it was
        last handed, or a resume since, ends that wait and its count: a
        one-shot keeps its only slot, and one refused many times, then run
        and resumed, waited out the old wait and a minute after each
        refusal."""
        handed = self._handed.get(job.id)
        if handed is None or handed[0] != slot:
            return False
        if (job.resumed_at or 0) >= handed[3] or load_runs_started_since(job.id, handed[3]):
            del self._handed[job.id]
            return False
        return time.monotonic() < handed[2]

    def _note_handed(self, job: JobRecord, slot: float | None) -> None:
        """Note that ``job``'s fire for ``slot`` is handed over now, and when
        it may be handed again should it come back due. Each time in a row
        is logged once."""
        last = self._handed.get(job.id)
        times = last[1] + 1 if last is not None and last[0] == slot else 1
        wait = 0.0 if times == 1 else min(
            _REFUSED_RETRY_MAX_SECONDS,
            _REFUSED_RETRY_FIRST_SECONDS * 2 ** (times - 2),
        )
        self._handed[job.id] = (slot, times, time.monotonic() + wait, time.time())
        if times > 1:
            logger.info(
                "job %s: the fire for slot %s came back %d times; handed again, "
                "and again in %d s if it comes back", job.id, slot, times - 1, wait,
            )

    def _came_due_while_running(self, job: JobRecord) -> None:
        """``job`` is due while its last run still goes on (the caller
        holds its record lock). Its overlap policy decides: queue fires
        once that run ends, skip_if_running drops the slot, and
        cancel_running asks that run to stop, then fires. A self-paced
        job's next fire is set when its run ends; the time it came due by
        is a placeholder, so it waits. A one-shot waits too: its only slot,
        skipped, was used up without running. A slot that waits is marked
        as not missed."""
        policy = _overlap_policy(job)
        if isinstance(job.schedule, SelfPacedSchedule):
            policy = "queue"
        elif isinstance(job.schedule, OnceSchedule) and policy == "skip_if_running":
            policy = "queue"
        if policy == "skip_if_running":
            slot = job.next_fire_at
            self._recompute_next_fire(job, force=True, after_fire=True)
            save_job(job)
            touch_wake()
            logger.info("job %s: skipped the slot at %s; its last run is still going", job.id, slot)
            append_event({"type": "scheduler_slot_skipped_running", "job_id": job.id, "slot": slot})
            return
        if job.waiting_slot != job.next_fire_at:
            # Not missed: under skip, a new owner's boot moved past it.
            job.waiting_slot = job.next_fire_at
            save_job(job)
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

    async def fire_handed_over(self, job_id: str, slot: float | None, attempt: int = 0) -> bool:
        """Fire a run of ``job_id`` that the scheduler owning the tick, in
        another process, claimed and handed to this one: the gateway, which
        runs the job's Slack thread (see ``_fires_in_the_gateway``), or the
        app's bridge, which runs its desktop session (``_fires_in_the_app``).
        The run is the one that scheduler would have made, under the job's
        run lock. Returns False when it cannot start; the slot is then due
        again."""
        job = load_job(job_id)
        if job is None:
            return False
        run_lock = FileLock(job_run_lock_path(job_id))
        if _fires_in_the_gateway(job) or _fires_in_the_app(job):
            refused = "this process does not run its session"
        elif not run_lock.acquire():
            refused = "a run of it goes on"
        else:
            task = asyncio.create_task(
                self._fire_with_concurrency(job, run_lock, slot, attempt),
                name=f"scheduler-fire-{job.id}",
            )
            self._in_flight_runs[task.get_name()] = task
            return True
        # Dropped, the run was missed with only a log line. Due again, it is
        # claimed on a later tick by the scheduler that owns it, which sees
        # again where it runs, and whose overlap policy decides while a run
        # of it goes on.
        await update_job_record(job_id, lambda fresh: _due_again(fresh, slot, attempt))
        logger.warning("job %s: handed a fire for slot %s, but %s; due again", job_id, slot, refused)
        append_event({
            "type": "scheduler_handed_fire_refused",
            "job_id": job_id,
            "slot": slot,
            "reason": refused,
        })
        return False

    async def _fire_with_concurrency(
        self, job: JobRecord, run_lock: FileLock, slot: float | None, attempt: int = 0,
    ) -> None:
        """Holds the job's run lock for the duration of the fire."""
        task_name = asyncio.current_task().get_name()
        started = time.time()
        try:
            async with self._runs_here(job), self._concurrency:
                await self._fire_under_lock(job, slot, attempt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("fire crashed (job=%s): %s", job.id, exc)
            await self._finish_crashed_run(job, started, exc, scheduled=True)
        finally:
            run_lock.release()
            self._in_flight_runs.pop(task_name, None)

    async def _finish_crashed_run(
        self, job: JobRecord, since: float, exc: Exception, *, scheduled: bool = False,
    ) -> None:
        """End the run of ``job`` that started at ``since`` or later and has
        not ended, as failed with ``exc``, the error its fire raised, and
        count it as a fire, as a failed run is (a self-paced loop waits its
        maximum delay). A ``scheduled`` fire's run then goes through what
        follows a scheduled run (_after_run). Our caller holds the job's run
        lock, so that run is the fire's own. Left so, it said "running"
        until the next scheduler start marked it interrupted; ended without
        the rest, it was not counted against max_fires nor retried, and a
        one-shot it used up said nothing."""
        from bridge.scheduler.runtime import count_failed_fire

        for run in load_runs_started_since(job.id, since):
            if run.finished_at is not None:
                continue
            run.status = "failed"
            run.error = f"the run crashed: {exc}"
            run.finished_at = time.time()
            run.duration_seconds = run.finished_at - run.started_at
            save_run(run)
            await update_job_record(job.id, lambda fresh: count_failed_fire(self, fresh, run))
            append_event({
                "type": "scheduler_run_failed",
                "job_id": job.id,
                "run_id": run.run_id,
                "duration_seconds": run.duration_seconds,
            })
            self.emit_event(
                subtype="scheduler_run_failed",
                message=f"Scheduled job '{job.name}' failed (crashed)",
                details={"jobId": job.id, "runId": run.run_id, "error": run.error},
            )
            if scheduled:
                await self._after_run(job, run)

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
        if await self._held_by_daily_budget(job, attempt):
            return
        run = await fire_job(self, job, scheduled_for=slot, attempt=attempt)
        await self._after_run(job, run)

    async def _after_run(self, job: JobRecord, run: RunRecord) -> None:
        """What follows ``run``, a scheduled run of ``job``, once it ended:
        the notice of a stop over its budget, a retry, the stop for
        failing, or the notice of a one-shot that did not complete."""
        if await self._tell_if_stopped_over_budget(job, run):
            return
        passing = _failed_for_a_passing_reason(run)
        if not passing and await self._retry_by_policy(job, run):
            return
        if await self._stop_if_failing(job, run):
            return
        if passing:
            await self._retry_after_passing_failure(job, run)
        else:
            await self._tell_if_one_shot_failed(job, run)

    async def _tell_if_one_shot_failed(self, job: JobRecord, run: RunRecord) -> None:
        """Post that ``job``, a one-shot, did not complete when its failed
        ``run`` was the last it may have: it is used up and runs no more.
        It never fails three runs in a row, so the stop notice never came,
        and nobody was told the reminder did not run."""
        if run.status not in _FAILED_RUN_STATUSES:
            return
        fresh = load_job(job.id)
        if (
            fresh is None
            or not isinstance(fresh.schedule, OnceSchedule)
            or fresh.enabled
            or fresh.status != "disabled_max_fires"
        ):
            return
        error = (run.error or _sink_errors(run) or "no error was recorded")[:600]
        failed = "its run failed" if run.attempt == 0 else (
            f"its run failed, and so did {run.attempt} "
            f"{'retry' if run.attempt == 1 else 'retries'}"
        )
        notice = (
            f"The one-time job *{fresh.name}* did not complete: {failed}, and "
            f"it will not run again by itself. Last error: {error}. To run it "
            "again, resume it: it runs once, right away."
        )
        append_event({
            "type": "scheduler_one_shot_failed",
            "job_id": job.id,
            "run_id": run.run_id,
            "error": error,
        })
        self.emit_event(
            subtype="scheduler_one_shot_failed",
            message=notice,
            details={"jobId": job.id, "runId": run.run_id, "error": error},
        )
        await self._tell_stopped(fresh, run, notice)

    async def _retry_by_policy(self, job: JobRecord, run: RunRecord) -> bool:
        """Run ``job`` again after its failed ``run`` when its retry_policy
        says so: at most ``max_retries`` times per slot, ``backoff_seconds``
        after the slot's own run, ``backoff_multiplier`` times longer after
        each retry, and never more than ``MAX_RETRIES`` times, whatever a
        record saved before that bound says. Returns True if it will. The
        attempt used to be read from a run attribute nothing set, so every
        failure was the first: retried without end, always after
        ``backoff_seconds``. A run over its budget is never retried: run
        again, it spends the budget again."""
        retry = job.retry_policy
        if (
            retry is None
            or run.status not in _FAILED_RUN_STATUSES
            or run.status == "budget_exceeded"
            or run.attempt >= min(retry.max_retries, MAX_RETRIES)
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
        if retried:
            # The run's own end event came before its next fire moved: the
            # dashboard and the sidebar read the job back on a job event.
            self.emit_event(
                subtype="scheduler_job_retrying",
                message=f"Scheduled job '{job.name}' runs again in {round(retry_at - time.time())} s",
                details={"jobId": job.id, "nextFireAt": retry_at},
            )
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
        ``MAX_PASSING_FAILURES_IN_A_ROW`` failed runs of any kind), all
        since it was last resumed, and post why to its sinks. Returns True
        if stopped."""
        if run.status not in _FAILED_RUN_STATUSES:
            return False
        # Resumed since the claim, the job counts from that resume.
        recent = _runs_since_resumed(load_job(job.id) or job)
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
            f"I stopped the scheduled job *{fresh.name}*: "
            f"{_failed_runs_said(recent)}. Last error: {error}"
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
        await self._tell_stopped(fresh, run, notice)
        return True

    async def _tell_if_stopped_over_budget(self, job: JobRecord, run: RunRecord) -> bool:
        """Post why to ``job``'s sinks when its ``run`` went over its
        budget and stopped it (on_exceeded abort_job). The run delivers
        nothing, so the job stopped and told no one. Returns True if it
        was stopped."""
        budget = job.budget
        if run.status != "budget_exceeded" or budget is None or budget.on_exceeded != "abort_job":
            return False
        fresh = load_job(job.id)
        if fresh is None or fresh.status != "disabled_budget_exhausted":
            return False
        await self._tell_stopped_over_budget(
            fresh, run, f"a run went over its budget ({_over_budget_said(budget, run)})",
        )
        return True

    async def _held_by_daily_budget(self, job: JobRecord, attempt: int) -> bool:
        """True when ``job`` has spent its max_cost_usd_per_day today (the
        Mac's local day), so its run does not start. With on_exceeded
        abort_run the slot is skipped and the job's next fire is its first
        slot on the next day (a self-paced loop at the start of that day);
        with abort_job the job is stopped and says why, as after a run
        over its per-run budget. Nothing read the daily budget: a job ran
        every slot, whatever it had spent."""
        budget = job.budget
        if budget is None or budget.max_cost_usd_per_day is None:
            return False
        spent = _spent_today(job.id)
        if not _over_daily_budget(budget, spent):
            return False
        said = (
            f"it spent ${spent:.2f} today, the budget is "
            f"${budget.max_cost_usd_per_day:.2f} a day"
        )
        if budget.on_exceeded != "abort_job":
            logger.info("job %s: skipped its slot; %s", job.id, said)
            append_event({
                "type": "scheduler_slot_skipped_over_budget",
                "job_id": job.id,
                "spent_today": spent,
                "max_cost_usd_per_day": budget.max_cost_usd_per_day,
            })
            # A loop's next fire after the claim is its minimum delay, a
            # placeholder its run replaces, and a cron or interval job's is
            # its next slot today: skipped, the job was claimed and skipped
            # again at each of them for the rest of the day. A one-shot the
            # claim used up did not run: left so, it was gone.
            def _wait_for_tomorrow(fresh: JobRecord) -> None:
                if _one_shot_used_up(fresh):
                    fresh.enabled = True
                    fresh.status = "active"
                if fresh.enabled and fresh.status == "active":
                    fresh.next_fire_at = _first_fire_tomorrow(fresh)

            fresh = await update_job_record(job.id, _wait_for_tomorrow)
            # The dashboard and the sidebar read the job back on a job
            # event: without one they showed the skipped slot as its next.
            self.emit_event(
                subtype="scheduler_job_skipped_over_budget",
                message=f"Scheduled job '{job.name}' skipped its slot: {said}",
                details={
                    "jobId": job.id,
                    "nextFireAt": fresh.next_fire_at if fresh is not None else None,
                },
            )
            return True
        # The stop is a run that went over its budget before it started:
        # the notice, the dashboard and the schedule tool point to it.
        now = time.time()
        run = RunRecord(
            run_id=new_run_id(), job_id=job.id, job_name=job.name,
            started_at=now, finished_at=now, status="budget_exceeded",
            fire_number=job.fire_count + 1, attempt=attempt, prompt=job.prompt,
            error=f"daily budget exceeded: {said}",
        )
        save_run(run)

        def _stop(fresh: JobRecord) -> None:
            fresh.enabled = False
            fresh.status = "disabled_budget_exhausted"
            fresh.next_fire_at = None

        fresh = await update_job_record(job.id, _stop)
        # The dashboard and the sidebar follow runs by these events. Sent
        # once the job is stopped, they read it back stopped.
        append_event({
            "type": "scheduler_run_budget_exceeded",
            "job_id": job.id,
            "run_id": run.run_id,
            "duration_seconds": run.duration_seconds,
        })
        self.emit_event(
            subtype="scheduler_run_budget_exceeded",
            message=f"Scheduled job '{job.name}' budget_exceeded",
            details={"jobId": job.id, "runId": run.run_id, "error": run.error},
        )
        await self._tell_stopped_over_budget(
            fresh or job, run, f"it reached its daily budget ({said})",
        )
        return True

    async def _tell_stopped_over_budget(self, job: JobRecord, run: RunRecord, why: str) -> None:
        """Post that ``job`` stopped over its budget, and ``why``."""
        notice = (
            f"I stopped the scheduled job *{job.name}*: {why}. To run it "
            "again, raise or remove the budget, then resume it."
        )
        append_event({
            "type": "scheduler_job_stopped_over_budget",
            "job_id": job.id,
            "run_id": run.run_id,
            "error": run.error,
        })
        self.emit_event(
            subtype="scheduler_job_stopped_over_budget",
            message=notice,
            details={"jobId": job.id, "runId": run.run_id, "error": run.error},
        )
        await self._tell_stopped(job, run, notice)

    async def _tell_stopped_aimed_at_a_sub_agent(self, job: JobRecord, why: str) -> None:
        """Post that the boot stopped ``job``, which fires into a sub-agent's
        session (``why``), and how to run it again. The stop is a failed run
        that did not start, as for a daily budget: the notice, the
        dashboard and the schedule tool point to it."""
        from bridge.freyja_bridge import _root_session_id

        now = time.time()
        run = RunRecord(
            run_id=new_run_id(), job_id=job.id, job_name=job.name,
            started_at=now, finished_at=now, status="failed",
            fire_number=job.fire_count + 1, prompt=job.prompt, error=why,
        )
        save_run(run)
        sub = job.execution.session_id
        root = _root_session_id(sub)
        where = f"its session {root}" if root != sub else "its parent session"
        notice = (
            f"I stopped the scheduled job *{job.name}*: it runs in {sub}, a "
            "sub-agent's session, where a scheduled job cannot run, so each "
            f"of its runs would fail. To run it again, point it at {where} or "
            "at a new session, then resume it."
        )
        logger.info("job %s (%s) stopped: it runs in sub-agent %s", job.id, job.name, sub)
        append_event({
            "type": "scheduler_job_stopped_aimed_at_sub_agent",
            "job_id": job.id,
            "run_id": run.run_id,
            "error": why,
        })
        self.emit_event(
            subtype="scheduler_job_stopped_failing",
            message=notice,
            details={"jobId": job.id, "runId": run.run_id, "error": why},
        )
        await self._tell_stopped(job, run, notice)

    async def _tell_stopped(self, job: JobRecord, run: RunRecord, notice: str) -> None:
        """Post ``notice`` of why ``job`` stopped, with its last ``run``,
        to its sinks."""
        from bridge.scheduler.persistence import write_run_output
        from bridge.scheduler.sinks import RunOutput, SinkContext, deliver_all

        # A notice only in sinks nobody reads tells no one: the Morning
        # briefing's sink is noop. It also goes to the conversation the
        # job was made in.
        told = job
        creator_sink = _creator_conversation(job.creator)
        if creator_sink is not None and not any(
            isinstance(s, (SlackSinkSpec, DesktopSinkSpec)) for s in job.sinks
        ):
            told = dataclasses.replace(job, sinks=[*job.sinks, creator_sink])
        try:
            await deliver_all(
                told,
                run,
                RunOutput(text=notice, attachments=[], run=run, job=told),
                SinkContext(state=self.state, outputs_dir=write_run_output, log=logger.info),
            )
        except Exception:  # noqa: BLE001
            logger.exception("stop notice for job %s was not delivered", job.id)

    # ─── CRUD ─────────────────────────────────────────────────────────

    async def create_job(self, spec: JobRecord) -> JobRecord:
        """Persist a new job. Computes ``next_fire_at`` from schedule.
        Reads back from disk before returning so the caller gets the
        canonical (post-pre-advance, post-validation) record."""
        _check_misfire_policy(spec.misfire_policy)
        _check_overlap_policy(spec.overlap_policy)
        _check_retry_policy(spec.retry_policy)
        _check_execution(spec.execution)
        if not spec.id:
            spec.id = new_job_id()
        _check_tool_filter(spec)
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
        _check_retry_policy(patch.retry_policy)
        _check_execution(patch.execution)
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
            # A job its daily budget holds waits for its first fire
            # tomorrow. When the patch raises or removes the budget so the
            # job can run today, its next fire is set from now (below):
            # left as it was, the job waited for the next day all the same.
            spent = held = None
            if patch.budget is not None or patch.clear_budget:
                spent = _spent_today(job.id)
                held = (
                    job.enabled and job.status == "active"
                    and _over_daily_budget(job.budget, spent)
                    and job.next_fire_at is not None
                    and job.next_fire_at == _first_fire_tomorrow(job)
                )
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
            for field_name in CLEARABLE_FIELDS:
                if getattr(patch, f"clear_{field_name}"):
                    setattr(job, field_name, None)
            if held and not _over_daily_budget(job.budget, spent):
                now = time.time()
                job.next_fire_at = compute_next_fire(
                    job.schedule, now=now, last_fire=job.last_fire_at,
                ) or (
                    # A one-shot held after its slot runs now.
                    now if isinstance(job.schedule, OnceSchedule) else job.next_fire_at
                )
            # A cap lowered to the fires the job ran stops it now, as the
            # end of the run that reaches the cap does. Nothing checks
            # max_fires when a run is claimed: left active, it fired again.
            if (
                patch.max_fires is not None
                and job.enabled and job.status == "active"
                and job.fire_count >= patch.max_fires
            ):
                job.enabled = False
                job.status = "disabled_max_fires"
                job.next_fire_at = None
            # enabled pauses or resumes the job, as pause_job and
            # resume_job do. Set alone, a paused job kept status "paused",
            # which the tick skips, and never ran again.
            switched = False
            if patch.enabled is not None and patch.enabled != (
                job.enabled and job.status == "active"
            ):
                if patch.enabled:
                    _check_fires_left(job)
                job.enabled = patch.enabled
                job.status = "active" if patch.enabled else "paused"
                if patch.enabled:
                    job.resumed_at = time.time()
                switched = True
            if patch.schedule is not None:
                job.schedule = patch.schedule
            if switched and patch.enabled:
                self._resume_next_fire(job)
            elif patch.schedule is not None or switched:
                self._recompute_next_fire(job, force=True)
            if patch.execution is not None:
                job.execution = patch.execution
                _check_tool_filter(job)
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
            # A paused job has no next fire, like every job that cannot
            # fire. The one it kept was shown, soon long past, wherever
            # the job was listed; resume_job sets a fresh one.
            self._recompute_next_fire(job)
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
            _check_fires_left(job)
            _check_execution(job.execution)
            if not (job.enabled and job.status == "active"):
                job.resumed_at = time.time()
            job.enabled = True
            job.status = "active"
            self._resume_next_fire(job)
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
        # next acquire. The run lock's file stays while a run of the job
        # goes on: by it, the start-up sweep tells that run from one cut
        # off (_settle_interrupted_runs).
        with FileLock(job_run_lock_path(job_id)) as free:
            for jlp in (job_lock_path(job_id), *([job_run_lock_path(job_id)] if free else [])):
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
        """Fire a job immediately and wait for its run. Acquires the job's
        run lock so a concurrent scheduled tick won't start a second run.
        Doesn't touch ``next_fire_at`` — manual runs are independent of
        cadence. A job whose session another process runs is handed to it
        (_hand_run_now), and its record is returned as that run starts."""
        handed = self._hand_run_now(job_id)
        if handed is not None:
            return handed
        job, lock = self._take_run_lock(job_id)
        return await self._run_now(job, lock)

    async def start_job_now(self, job_id: str) -> RunRecord:
        """Start a run of ``job_id`` now, as ``run_job_now`` does, and
        return its record as it starts, without waiting for it; the run's
        events tell how it goes. The app's bridge handles one command at a
        time: waiting for the run there held every other command until it
        ended, and the renderer gave up on the run after 30 s.

        A job whose session another process runs is handed to it
        (_hand_run_now)."""
        handed = self._hand_run_now(job_id)
        if handed is not None:
            return handed
        job, lock = self._take_run_lock(job_id)
        run = RunRecord(
            run_id=new_run_id(), job_id=job.id, job_name=job.name,
            started_at=time.time(), fire_number=job.fire_count + 1, prompt=job.prompt,
        )
        self._start_run_now(job, lock, run.run_id)
        return run

    def _start_run_now(self, job: JobRecord, lock: FileLock, run_id: str) -> None:
        """Run ``job`` now under its run ``lock`` as ``run_id``, without
        waiting for it."""
        name = f"scheduler-run-now-{job.id}"

        async def _run() -> None:
            try:
                await self._run_now(job, lock, run_id=run_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("run now crashed (job=%s): %s", job.id, exc)
            finally:
                self._in_flight_runs.pop(name, None)

        self._in_flight_runs[name] = asyncio.create_task(_run(), name=name)

    def _hand_run_now(self, job_id: str) -> RunRecord | None:
        """Hand a run now of ``job_id`` to the process that runs its
        session, as its scheduled fires are (_hand_over): the gateway a
        Slack thread's, the app a desktop session's. Run here, it went on a
        copy of the session, and failed with that reason. Returns the
        record that process runs it under (run_now_handed_over), or None
        when this process runs it. Refused when that process is not
        running."""
        job = load_job(job_id)
        if job is None:
            return None
        in_gateway = _fires_in_the_gateway(job)
        if not in_gateway and not _fires_in_the_app(job):
            return None
        if in_gateway and not _gateway_runs():
            raise ValueError(
                f"{job.name} runs in its Slack thread, and the Slack gateway "
                "is not running. Start the gateway, or let the job run at its "
                "next scheduled time"
            )
        if not in_gateway and not _app_runs():
            raise ValueError(
                f"{job.name} runs in a desktop session, and the app is not "
                "open. Open the app, or let the job run at its next scheduled "
                "time"
            )
        with FileLock(job_run_lock_path(job.id)) as free:
            if not free:
                raise RuntimeError(f"job {job.id} is running; try again when its run ends")
        run = RunRecord(
            run_id=new_run_id(), job_id=job.id, job_name=job.name,
            started_at=time.time(), fire_number=job.fire_count + 1, prompt=job.prompt,
        )
        if not _hand_over(job, None, 0, to_app=not in_gateway, run_id=run.run_id):
            owner = "the Slack gateway" if in_gateway else "the app"
            raise RuntimeError(f"{job.name} could not be handed to {owner}")
        return run

    async def run_now_handed_over(self, job_id: str, run_id: str, sent_at: float | None = None) -> bool:
        """Run ``job_id`` now as ``run_id``: a run now that another process
        handed to this one, which runs the job's session (_hand_run_now),
        at ``sent_at``. It is a manual run, as run_job_now makes: no slot,
        no retries, and the cadence is left alone. When it cannot start, or
        it was sent more than _HANDED_RUN_NOW_EXPIRES_SECONDS ago, its
        record says why, so the run the other process answered with does
        not stay "claimed" or vanish. Returns False then. Such a run never
        ran, so it is "not_started" or "expired", not "failed": saved as
        failed, two run nows pressed while a run went on, and that run
        failing, stopped the job for three failed runs in a row."""
        job = load_job(job_id)
        if job is None:
            logger.warning("job %s: handed a run now, but the job is gone", job_id)
            return False
        lock = FileLock(job_run_lock_path(job_id))
        status = "not_started"
        waited = time.time() - sent_at if sent_at is not None else 0.0
        if waited > _HANDED_RUN_NOW_EXPIRES_SECONDS:
            status = "expired"
            refused = (
                f"it was asked for {int(waited // 60)} min ago, and a run now "
                f"expires after {int(_HANDED_RUN_NOW_EXPIRES_SECONDS // 60)} min"
            )
        elif _fires_in_the_gateway(job) or _fires_in_the_app(job):
            refused = "this process does not run its session"
        elif not lock.acquire():
            refused = "a run of it goes on"
        else:
            self._start_run_now(job, lock, run_id)
            return True
        now = time.time()
        save_run(RunRecord(
            run_id=run_id, job_id=job.id, job_name=job.name,
            started_at=now, finished_at=now, status=status,
            fire_number=job.fire_count + 1, prompt=job.prompt,
            error=f"not run: {refused}",
        ))
        logger.warning("job %s: handed a run now, but %s", job_id, refused)
        append_event({
            "type": f"scheduler_run_{status}",
            "job_id": job.id,
            "run_id": run_id,
            "duration_seconds": 0.0,
            "reason": refused,
        })
        self.emit_event(
            subtype=f"scheduler_run_{status}",
            message=f"Scheduled job '{job.name}' did not run: {refused}",
            details={"jobId": job.id, "runId": run_id, "error": f"not run: {refused}"},
        )
        return False

    def _take_run_lock(self, job_id: str) -> tuple[JobRecord, FileLock]:
        """``job_id``'s record and its run lock, taken for a run now."""
        job = load_job(job_id)
        if job is None:
            raise KeyError(job_id)
        _check_runs_now_here(self, job)
        lock = FileLock(job_run_lock_path(job_id))
        if not lock.acquire():
            raise RuntimeError(
                f"job {job_id} is running; try again when its run ends"
            )
        return job, lock

    async def _run_now(self, job: JobRecord, lock: FileLock, **kw: Any) -> RunRecord:
        """Run ``job`` now under its run ``lock``, and release it."""
        started = time.time()
        try:
            async with self._runs_here(job), self._concurrency:
                from bridge.scheduler.runtime import fire_job
                # Re-read inside the lock for freshness.
                fresh = load_job(job.id) or job
                return await fire_job(self, fresh, **kw)
        except Exception as exc:
            await self._finish_crashed_run(job, started, exc)
            raise
        finally:
            lock.release()

    async def get_runs(self, job_id: str, *, limit: int = 50) -> list[RunRecord]:
        return load_runs_for_job(job_id, limit=limit)

    async def cancel_run(self, run_id: str) -> bool:
        """Cancel a run, possibly in another process. Mechanism:
        1. If the run waits on its turn in this process, stop it at
           once (fast path).
        2. Always also write ``cancel_requested=true`` into the run
           JSON. The firing process (whichever one) polls this flag
           during its agent turn and cancels its local task.
        """
        # Same-process fast path. Fire tasks are named for their job, not
        # the run, so looking the run id up in task names never matched.
        from bridge.scheduler.runtime import stop_run_now
        same_process_cancelled = stop_run_now(run_id)
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
        fail = sum(1 for r in recent_24h if r.status in _DID_NOT_COMPLETE_STATUSES)
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

    def _resume_next_fire(self, job: JobRecord) -> None:
        """Set the next fire of ``job``, just resumed: from its schedule,
        or for a one-shot whose time passed and that has not run, that
        time, so it runs once now and says how late it is. From the
        schedule, which has no slot left, it was marked done without
        running, and the resume said it worked."""
        slot = _missed_one_shot_slot(job)
        if slot is None:
            self._recompute_next_fire(job, force=True)
            return
        job.retry_attempt = 0
        job.next_fire_at = slot
        # Not missed: the user asked for it now. Under skip, a new owner's
        # boot moved past it, and the one-shot was used up without running.
        job.waiting_slot = slot

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


def _check_retry_policy(policy: RetryPolicy | None) -> None:
    """Refuse a retry policy whose max_retries is below 0 or above
    ``MAX_RETRIES``. Unbounded, a job that always failed retried each
    slot so often that it was never stopped for failing."""
    if policy is not None and not 0 <= policy.max_retries <= MAX_RETRIES:
        raise ValueError(
            f"retry_policy.max_retries is {policy.max_retries}: "
            f"use 0 to {MAX_RETRIES} retries per slot"
        )


def _check_execution(execution: Any) -> None:
    """Refuse a job that fires into a sub-agent's session. A sub-agent runs
    only under its parent, which gets its report. A run loads its session
    with ensure_session, which built a root session on the sub-agent's id:
    a second, parentless copy that saved over its transcript."""
    if isinstance(execution, ExistingSession) and execution.session_id.startswith("sub_"):
        raise ValueError(
            f"{execution.session_id} is a sub-agent: a scheduled job cannot run "
            "in a sub-agent's session. Run it in the sub-agent's parent "
            "session, or in a new session"
        )


def _check_tool_filter(job: JobRecord) -> None:
    """Refuse a job made under a gateway tool filter that would run where
    the filter does not reach. Its own sessions take the filter from the
    job (runtime.job_tool_filter), and the session it was made in has it
    already. Any other session, such as a desktop chat, has every tool."""
    from bridge.scheduler.runtime import scheduler_session_id, tool_filter_of

    tool_filter = tool_filter_of(job)
    if not tool_filter:
        return

    spec = job.execution
    if isinstance(spec, PersistentJobSession):
        reached = not spec.session_id or spec.session_id == scheduler_session_id(job.id)
    elif isinstance(spec, ExistingSession):
        reached = spec.session_id == job.creator.session_id
    else:
        reached = True
    if not reached:
        raise ValueError(
            f"this job was made with the {tool_filter.title()} tool filter "
            "on, so it runs only in a new session, its own job session or the "
            "session it was made in"
        )


def _execution_refused(job: JobRecord) -> str | None:
    """Why ``job``'s execution would be refused now (_check_execution), or
    None. A job saved before it was refused still fires into it."""
    try:
        _check_execution(job.execution)
    except ValueError as exc:
        return str(exc)
    return None


def _check_fires_left(job: JobRecord) -> None:
    """Refuse to resume ``job`` when it ran all its max_fires. Nothing
    checks them before a run: resumed, the job fired once more, and the
    end of that run stopped it again."""
    if job.max_fires is not None and job.fire_count >= job.max_fires:
        raise ValueError(
            f"it ran all {job.max_fires} of its max fires; raise or clear "
            "max fires first"
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
    ran and waited for the Mac, the gateway, the app or a run of the job
    that went on, that is due again because its fire did not run there, or
    that a one-shot runs late, resumed or after its run was cut off
    (``waiting_slot``), so it was not missed."""
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


def _fires_in_the_gateway(job: JobRecord) -> bool:
    """True when ``job`` fires into a Slack thread and this process is not
    the gateway. The gateway runs that thread's session and saves its
    files; a run here would build a copy of it and save the copy over
    them. The scheduler daemon often owns the tick, and did that."""
    spec = job.execution
    if not isinstance(spec, ExistingSession) or not spec.session_id:
        return False
    from bridge.freyja_bridge import _gateway_holds

    return _gateway_holds(spec.session_id)


def _fires_in_the_app(job: JobRecord) -> bool:
    """True when ``job`` fires into a saved desktop session and this
    process is not the app's bridge, which loads, runs and saves desktop
    sessions. A run here would build a copy of the session beside the
    app's, and each would save over the other. The scheduler daemon often
    owns the tick, and did that. A Slack thread is the gateway's, and a
    scheduler session is run by whichever scheduler fires its own job.

    While the app is closed the job waits for it, as a Slack thread's job
    waits for the gateway. Run here then, the run would need a lock that
    the app's bridge asks before it loads that session, and the app loads
    sessions as it boots."""
    spec = job.execution
    if not isinstance(spec, ExistingSession) or not spec.session_id:
        return False
    from bridge.freyja_bridge import _is_gateway_session_id, _root_session_id
    from bridge.gateway.pid import holds_desktop_lock
    from bridge.scheduler.runtime import scheduler_session_id
    from bridge.tools.talk_tool import _session_exists_on_disk

    if holds_desktop_lock():
        return False
    root = _root_session_id(spec.session_id)
    if _is_gateway_session_id(root) or root.startswith(scheduler_session_id("")):
        return False
    return _session_exists_on_disk(spec.session_id)


def _scheduler_session_of(job: JobRecord) -> str | None:
    """The scheduler session (``scheduler:…``) a run of ``job`` loads, as
    runtime._resolve_execution_session names it, or None when it runs in
    a desktop session or a Slack thread."""
    from bridge.scheduler.runtime import scheduler_session_id

    spec = job.execution
    if isinstance(spec, PersistentJobSession):
        return spec.session_id or scheduler_session_id(job.id)
    if isinstance(spec, NewSession):
        return scheduler_session_id(f"{job.id}.ephemeral")
    if (spec.session_id or "").startswith(scheduler_session_id("")):
        return spec.session_id
    return None


def _check_runs_now_here(service: SchedulerService, job: JobRecord) -> None:
    """Refuse a run now of ``job`` in a process whose scheduler does not
    hold the session the job keeps from run to run (a PersistentJobSession,
    or an existing scheduler session). The tick owner, in another process,
    loads and saves it, and keeps it loaded from earlier runs, so its next
    save drops a run made here. No route hands the run to it: the
    scheduler daemon reads no control channel, and nothing says whether it
    or the gateway owns the tick. A NewSession job runs here: each run
    starts from an empty session, which the owner resets at its next run
    and never reads back."""
    session = _scheduler_session_of(job)
    if session is None or isinstance(job.execution, NewSession) or service.holds_session(session):
        return
    raise ValueError(
        f"{job.name} cannot run now from here: it keeps one session from run "
        "to run, and Freyja's background scheduler, in another process, holds "
        "that session, so a run here would be lost from it. It runs at its "
        "next scheduled time"
    )


def _gateway_runs() -> bool:
    try:
        from bridge.gateway.pid import get_running_pid

        return get_running_pid() is not None
    except Exception:  # noqa: BLE001
        return False


def _app_runs() -> bool:
    try:
        from bridge.gateway.pid import desktop_bridge_runs

        return desktop_bridge_runs()
    except Exception:  # noqa: BLE001
        return False


def _hand_over(
    job: JobRecord, slot: float | None, attempt: int, *, to_app: bool, run_id: str | None = None,
) -> bool:
    """Send a claimed fire of ``job`` on its control channel to the process
    that runs its session: the gateway (``GatewayDaemon._on_scheduler_fire``)
    or the app's bridge (``_handle_scheduler_fire_command``). Each reads
    from where it left off, so a fire sent while it restarts runs when it
    is back. With ``run_id`` it is a run now, run under that id. Returns
    False when the fire was not sent."""
    from bridge.gateway.control_channel import append_command, desktop_commands_path

    owner = "the app" if to_app else "the Slack gateway"
    cmd: dict[str, Any] = {
        "type": "scheduler_fire",
        "job_id": job.id,
        "scheduled_for": slot,
        "attempt": attempt,
        "origin_pid": os.getpid(),
    }
    if run_id is not None:
        cmd["run_id"] = run_id
        cmd["sent_at"] = time.time()
    try:
        append_command(cmd, path=desktop_commands_path() if to_app else None)
    except Exception:  # noqa: BLE001
        logger.exception("job %s: the fire for slot %s did not reach %s", job.id, slot, owner)
        return False
    logger.info("job %s (%s): fire handed to %s", job.id, job.name, owner)
    append_event({
        "type": "scheduler_run_handed_to_app" if to_app else "scheduler_run_handed_to_gateway",
        "job_id": job.id,
        "slot": slot,
    })
    return True


def _fire_on_its_way(job_id: str) -> bool:
    """True when a fire of ``job_id`` was handed to the gateway or the app
    (``_hand_over``) and has not been read there yet."""
    from bridge.gateway.control_channel import (
        commands_offset_path,
        commands_path,
        desktop_commands_offset_path,
        desktop_commands_path,
        unread_commands,
    )

    return any(
        cmd.get("type") == "scheduler_fire" and cmd.get("job_id") == job_id
        for commands, offset in (
            (commands_path(), commands_offset_path()),
            (desktop_commands_path(), desktop_commands_offset_path()),
        )
        for cmd in unread_commands(commands, offset)
    )


def _failed_for_a_passing_reason(run: RunRecord) -> bool:
    """True when ``run`` failed for a reason that passes by itself: the
    network was down (the Mac asleep or in DarkWake, no Wi-Fi), a request
    timed out, or the provider was overloaded or failed. A wall-clock
    timeout ends a run as timed_out and a budget overrun as budget_exceeded,
    not failed: those are the job's own."""
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


def _runs_since_resumed(job: JobRecord) -> list[RunRecord]:
    """``job``'s latest runs, newest first, that count toward a stop for
    failing: those started since it was last resumed. With the runs that
    had stopped it, a resumed job was stopped again by one failure."""
    runs = load_runs_for_job(job.id, limit=_FAILURE_WINDOW_RUNS)
    if job.resumed_at is None:
        return runs
    return [r for r in runs if r.started_at >= job.resumed_at]


def _should_stop(runs: list[RunRecord]) -> bool:
    real, passing = _failures_in_a_row(runs)
    return (
        real >= MAX_FAILED_RUNS_IN_A_ROW
        or real + passing >= MAX_PASSING_FAILURES_IN_A_ROW
    )


def _failed_runs_said(runs: list[RunRecord]) -> str:
    """How many runs at the head of ``runs`` (newest first) failed, for
    the notice of a job they stopped, and how many of those were retries
    under its retry_policy. The count the stop goes by leaves such retries
    out: it said 3 runs failed when 6 had."""
    failed = retries = 0
    for r in runs:
        if r.status not in _FAILED_RUN_STATUSES:
            break
        failed += 1
        if r.attempt > 0:
            retries += 1
    said = f"its last {failed} runs failed"
    return f"{said}, {retries} of them retries under its retry policy" if retries else said


def _over_budget_said(budget: BudgetSpec, run: RunRecord) -> str:
    """What of its per-run ``budget`` ``run`` went over, for the notice
    of the stop."""
    said = []
    tokens = run.input_tokens + run.output_tokens
    if budget.max_tokens_per_run is not None and tokens > budget.max_tokens_per_run:
        said.append(f"it used {tokens:,} tokens, the budget is {budget.max_tokens_per_run:,} a run")
    if budget.max_cost_usd_per_run is not None and run.cost_usd > budget.max_cost_usd_per_run:
        said.append(
            f"it cost ${run.cost_usd:.2f}, the budget is "
            f"${budget.max_cost_usd_per_run:.2f} a run"
        )
    return "; ".join(said)


def _start_of_today() -> float:
    """When the Mac's local day began, in POSIX seconds."""
    return datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp()


def _start_of_tomorrow() -> float:
    """When the Mac's next local day begins, in POSIX seconds."""
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    return datetime.datetime.combine(tomorrow, datetime.time.min).timestamp()


def _spent_today(job_id: str) -> float:
    """What the runs of ``job_id`` started since local midnight cost."""
    return sum(r.cost_usd for r in load_runs_started_since(job_id, _start_of_today()))


def _over_daily_budget(budget: BudgetSpec | None, spent: float) -> bool:
    """True when ``spent`` today reaches ``budget``'s max_cost_usd_per_day."""
    return (
        budget is not None
        and budget.max_cost_usd_per_day is not None
        and spent >= budget.max_cost_usd_per_day
    )


def _first_fire_tomorrow(job: JobRecord) -> float | None:
    """``job``'s first fire on the Mac's next local day, when its daily
    budget starts over: a self-paced loop, or a one-shot held after its
    only slot, at the day's start, any other job at its first slot from
    then (on an interval job's grid of slots, which runs through its next
    fire)."""
    tomorrow = _start_of_tomorrow()
    if isinstance(job.schedule, (SelfPacedSchedule, OnceSchedule)):
        return tomorrow
    if job.next_fire_at is None or job.next_fire_at >= tomorrow:
        return job.next_fire_at
    # compute_next_fire gives the first slot after ``now``: from a second
    # before the day starts, a slot at midnight is one.
    return compute_next_fire(job.schedule, now=tomorrow - 1, last_fire=job.next_fire_at)


def _stopped_only_by_passing_failures(job: JobRecord) -> bool:
    """True when ``job`` was stopped for failing, but its runs do not
    stop it now that passing failures do not count."""
    if job.enabled or job.status != "disabled_error":
        return False
    runs = _runs_since_resumed(job)
    return bool(runs) and _failed_for_a_passing_reason(runs[0]) and not _should_stop(runs)


def _one_shot_used_up(job: JobRecord) -> bool:
    """True when ``job`` is a one-shot that was disabled because its
    only slot fired (the pre-advance does it before the run)."""
    return (
        isinstance(job.schedule, OnceSchedule)
        and job.status == "disabled_max_fires"
        and (job.max_fires is None or job.fire_count < job.max_fires)
    )


def _due_again(job: JobRecord, slot: float | None, attempt: int) -> bool:
    """Make ``job`` due at ``slot`` again, as retry ``attempt`` of it, when
    its claimed run did not start. The claim moved its next fire past the
    slot, and a one-shot's past its only one, which marked it done: left
    so, the run was missed. A job paused or stopped meanwhile stays so.
    The slot is marked as not missed: under skip, a new owner's boot
    before the next claim moved past it. Returns True if it did."""
    if _one_shot_used_up(job):
        job.enabled = True
        job.status = "active"
    elif not (job.enabled and job.status == "active"):
        return False
    job.next_fire_at = slot if slot is not None else time.time()
    job.waiting_slot = job.next_fire_at
    job.retry_attempt = attempt
    return True


def _settle_interrupted_runs() -> None:
    """Mark interrupted each run whose process exited before it ended: its
    record said "running" for good. A run still going, in any process,
    holds its job's run lock and is left alone. A one-shot that the claim
    for such a run used up, and that has not run since its time, is due at
    its time again, so it runs once, late: left used up, it never ran. The
    runs of a removed job are looked at too: its history stays."""
    now = time.time()
    settled = False
    jobs = load_all_jobs()
    for job in jobs:
        with FileLock(job_lock_path(job.id)) as got, FileLock(job_run_lock_path(job.id)) as free:
            if not got or not free:
                # Being edited, or a run of it goes on; the next start looks.
                continue
            cut = _interrupt_cut_off_runs(job.id, now)
            fresh = load_job(job.id) if cut else None
            if fresh is None or not _one_shot_used_up(fresh):
                continue
            slot = compute_next_fire(fresh.schedule, now=0)
            if slot is None or any(
                r.status in ("succeeded", "partial_failure")
                for r in load_runs_started_since(job.id, slot)
            ):
                continue
            _due_again(fresh, slot, cut[0].attempt)
            save_job(fresh)
            settled = True
            logger.info("job %s (%s): its run was cut off; due again", job.id, job.name)
    known = {job.id for job in jobs}
    for job_id in load_run_job_ids():
        if job_id in known or not load_unfinished_runs(job_id):
            continue
        # A run of it that goes on took the run lock before the job was
        # removed, and remove_job left that lock's file.
        with FileLock(job_run_lock_path(job_id)) as free:
            if free:
                _interrupt_cut_off_runs(job_id, now)
    if settled:
        touch_wake()


def _interrupt_cut_off_runs(job_id: str, now: float) -> list[RunRecord]:
    """Mark interrupted the runs of ``job_id`` that have not ended, and
    return them. Our caller holds the job's run lock: none goes on."""
    cut = [r for r in load_unfinished_runs(job_id) if r.status in _UNFINISHED_RUN_STATUSES]
    for run in cut:
        was = run.status
        run.status = "interrupted"
        run.finished_at = now
        run.error = f"cut off while {was}: the process that ran it exited, or the run crashed"
        save_run(run)
        logger.warning("job %s: run %s was cut off while %s", job_id, run.run_id, was)
        append_event({
            "type": "scheduler_run_interrupted",
            "job_id": job_id,
            "run_id": run.run_id,
            "status": was,
        })
    return cut


def _missed_one_shot_slot(job: JobRecord) -> float | None:
    """The time of ``job``, a one-shot, when it has passed and no run of
    it since then succeeded; None for any other job. Refuses a one-shot
    that ran, saying when, and one whose run goes on or is on its way:
    resumed during the run of its slot, it ran again after."""
    if not isinstance(job.schedule, OnceSchedule):
        return None
    # Its only slot: the first fire of its schedule from the epoch.
    slot = compute_next_fire(job.schedule, now=0)
    if slot is None or slot > time.time():
        return None
    with FileLock(job_run_lock_path(job.id)) as free:
        if not free:
            raise ValueError("a run of it is going on; try again when that run ends")
    if _fire_on_its_way(job.id):
        # The gateway or the app takes the run lock as it reads the fire.
        # Resumed before, the one-shot was claimed again and ran twice.
        raise ValueError("its run is starting; try again when that run ends")
    for run in load_runs_started_since(job.id, slot):
        if run.status in ("succeeded", "partial_failure"):
            ran = datetime.datetime.fromtimestamp(run.started_at)
            raise ValueError(
                f"it already ran at {ran:%Y-%m-%d %H:%M}, and a one-shot runs "
                "once; give it a new time first"
            )
    return slot


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
