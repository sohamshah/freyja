"""Fire-time execution: resolve execution context → run the agent turn
→ capture output → fan out to sinks.

The runtime is the only place that touches ``_BridgeSession.run_turn``
and the engine's transcript. Everything else in the scheduler is
storage, schedule math, or sink delivery — none of which knows about
agents.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from bridge.scheduler.models import (
    BudgetSpec,
    ExistingSession,
    JobRecord,
    NewSession,
    PersistentJobSession,
    RunRecord,
    SelfPacedSchedule,
)
from bridge.scheduler.persistence import (
    save_run,
    update_job_record,
    write_run_output,
    append_event,
)
from bridge.scheduler.sinks import RunOutput, SinkContext, deliver_all

if TYPE_CHECKING:
    from bridge.scheduler.service import SchedulerService

logger = logging.getLogger("freyja.scheduler.runtime")


# Sessions allocated for a job's execution context get a stable key
# under this prefix. Picking a deterministic prefix means the user can
# spot scheduler-owned sessions in the activity panel.
_SCHED_SESSION_PREFIX = "scheduler:"

# A fire that starts this long after its slot tells the agent it is
# late. A tick is at most a second late, a takeover 15 s, and a run that
# waited a few minutes for the job's previous run is not stale.
_LATE_AFTER_SECONDS = 5 * 60


def scheduler_session_id(job_id: str) -> str:
    return f"{_SCHED_SESSION_PREFIX}{job_id}"


async def fire_job(
    service: "SchedulerService",
    job: JobRecord,
    *,
    iteration: int = 0,
    scheduled_for: float | None = None,
    attempt: int = 0,
) -> RunRecord:
    """Run one fire of ``job``. Returns the run record. The caller
    (SchedulerService.tick / run_job_now) is responsible for
    pre-advancing the next_fire_at and persisting BEFORE this call,
    so a crash mid-fire doesn't double-fire. The caller holds the job's
    run lock, not its record lock: the job may be edited while it runs,
    so the run goes on with ``job`` as claimed and writes to the record
    only what it owns, on the record as it is then.

    ``scheduled_for`` is the slot a scheduled fire is for; None for a
    manual run. ``attempt`` is which retry of it this is under the job's
    retry_policy; 0 for the slot's own run.
    """
    from bridge.scheduler.models import new_run_id

    run = RunRecord(
        run_id=new_run_id(),
        job_id=job.id,
        job_name=job.name,
        started_at=time.time(),
        fire_number=job.fire_count + 1,
        iteration=iteration,
        attempt=attempt,
        prompt=job.prompt,
        status="claimed",
    )
    save_run(run)
    append_event({
        "type": "scheduler_run_claimed",
        "job_id": job.id,
        "run_id": run.run_id,
        "fire_number": run.fire_number,
        "iteration": iteration,
        "scheduled_for": scheduled_for,
        "attempt": attempt,
    })
    service.emit_event(
        subtype="scheduler_run_claimed",
        message=f"Scheduled job '{job.name}' starting",
        details={"jobId": job.id, "runId": run.run_id, "fireNumber": run.fire_number},
    )

    try:
        sess, sess_id, owns_session = await _resolve_execution_session(service, job)
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.error = f"execution context resolution failed: {exc}"
        run.finished_at = time.time()
        run.duration_seconds = run.finished_at - run.started_at
        save_run(run)
        service.emit_event(
            subtype="scheduler_run_failed",
            message=f"Scheduled job '{job.name}' failed (execution context)",
            details={"jobId": job.id, "runId": run.run_id, "error": str(exc)},
        )
        return run

    run.execution_session_id = sess_id
    run.status = "running"
    save_run(run)

    # For self-paced loops, register the loop-control tools on this
    # session's tool registry for the duration of the fire. They record
    # the agent's decision, which is applied to the job once the turn
    # ends. We unregister after the turn completes so the session's
    # later turns don't send them.
    registered_loop_tools: list[str] = []
    loop_decision = None
    if isinstance(job.schedule, SelfPacedSchedule):
        try:
            from bridge.tools.schedule_tool import (
                LoopDecision,
                build_complete_loop_tool,
                build_continue_loop_tool,
            )
            # A session that has not run a turn yet (a new job session, or
            # any session since a reset) builds its tool registry at the
            # start of its first turn, after this point. Build it now, or
            # the loop tools are silently left out.
            initialize = getattr(sess, "initialize", None)
            if getattr(sess, "tool_registry", None) is None and callable(initialize):
                await initialize()
            reg = getattr(sess, "tool_registry", None)
            inner_reg = getattr(reg, "_underlying", None) or reg
            if inner_reg is None:
                logger.warning(
                    "job %s: session %s has no tool registry; the loop tools are missing",
                    job.id, sess_id,
                )
            else:
                loop_decision = LoopDecision()
                cont = build_continue_loop_tool(schedule=job.schedule, decision=loop_decision)
                comp = build_complete_loop_tool(schedule=job.schedule, decision=loop_decision)
                inner_reg.register(cont)
                inner_reg.register(comp)
                registered_loop_tools.extend([
                    cont.definition.name, comp.definition.name,
                ])
        except Exception as exc:  # noqa: BLE001
            logger.warning("job %s: loop tool registration failed: %s", job.id, exc)

    # Compose the fire-time prompt. We pre-load skills if the job
    # requested any, and inject a small system-style header that tells
    # the model this is a scheduled fire (so it doesn't ask clarifying
    # questions or expect a human in the loop).
    # Snapshot working notes BEFORE composing the prompt — the
    # composer reads them, and the post-turn diff needs the pre-state
    # to compute what the agent added.
    try:
        from bridge.scheduler.memory import snapshot_notes
        snapshot_notes(job, run.run_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("notes snapshot failed: %s", exc)
    prompt = _compose_fire_prompt(job, run, service, scheduled_for=scheduled_for)

    # ── Run the agent turn ────────────────────────────────────────────
    output_text = ""
    output_attachments: list[dict[str, Any]] = []
    iteration_count = 0
    entries_before: set[str] | None = None
    abort_job = False
    from bridge.freyja_bridge import register_session_listener, unregister_session_listener

    run_cost = _RunCost(run.run_id, job.budget)
    register_session_listener(sess_id, run_cost.on_event)
    try:
        from bridge.freyja_bridge import _schedule_or_queue_turn

        # _schedule_or_queue_turn returns True for immediate dispatch
        # and False for queued. Either way we'll await sess.pending_task
        # below to wait for completion.
        # Background work already running in this session (a fire into
        # the operator's own session) isn't this fire's: it neither waits
        # for it nor stops it on timeout.
        _running_work = getattr(sess, "_running_background_work", None)
        preexisting = {
            w.id for w in (_running_work() if callable(_running_work) else [])
        }
        entries_before = _entry_ids(sess)
        _schedule_or_queue_turn(
            sess, prompt, attachments=None, on_turn_start=run_cost.our_turn_starts,
        )
        pending = getattr(sess, "pending_task", None)
        # Detect the self-await trap. This fires when:
        #   (a) `run_now` is invoked from within a tool call — the
        #       calling agent's turn task is the same task that fire_job
        #       is now running in;
        #   (b) a scheduled fire targets a session whose own turn task
        #       happens to be the one driving this fire (rare, but
        #       possible inside nested tool flows).
        # In either case, the prompt has been queued via
        # _schedule_or_queue_turn and will run after the current turn
        # finishes. We treat this run record as a dispatch acknowledgement
        # rather than the actual execution — the agent's caller is told
        # the prompt is queued so they don't think it failed silently.
        current_task = asyncio.current_task()
        if pending is not None and pending is current_task:
            run.status = "queued"
            run.error = (
                "execution target is the calling session — prompt was "
                "queued to run after the current turn finishes; this "
                "run record represents the dispatch, not the eventual "
                "execution. Look at the session's transcript for the "
                "deferred turn's output."
            )
            run.finished_at = time.time()
            run.duration_seconds = run.finished_at - run.started_at
            save_run(run)
            return run
        if pending is not None:
            # Poll the run JSON for cancel_requested while we wait. A
            # peer process (e.g. the user clicked Cancel in the
            # dashboard while the daemon is firing) can set this flag
            # via persistence.request_run_cancellation; the firing
            # process notices and cancels its local turn task.
            timeout = job.timeout_seconds or (
                job.budget.wall_clock_timeout_seconds if job.budget else None
            )
            # Wait for the WHOLE job, not just the fire's own turn:
            # sub-agents run in the background, so the turn can end with
            # children still working and their memos then wake the session
            # for the turns that actually produce the result. Timeout and
            # cancel stop all of it (turn + the work this fire started).
            from bridge.freyja_bridge import (
                _force_cancel_session,
                _stop_background_work,
                wait_until_quiescent,
            )

            # With no job timeout, bound the wait on background work alone
            # so one hung child can't hold the job (and its lock) forever.
            quiet = asyncio.ensure_future(
                wait_until_quiescent(
                    sess,
                    background_wait_cap_s=(
                        None if timeout else BACKGROUND_WORK_WAIT_CAP_S
                    ),
                    ignore_ids=preexisting,
                )
            )

            def _abort_this_fire() -> None:
                _force_cancel_session(sess, scope="turn")
                _stop_background_work(sess, exclude=preexisting)

            cancel_outcome = await _await_pending_with_cancel_poll(
                quiet,
                job_id=job.id,
                run_id=run.run_id,
                timeout_seconds=timeout,
                on_abort=_abort_this_fire,
            )
            if cancel_outcome == "timed_out":
                run.status = "timed_out"
                run.error = f"wall-clock timeout after {timeout}s"
            elif cancel_outcome == "cancelled":
                run.status = "cancelled"
                run.error = "cancel_requested by peer process or user"

        # Read the most recent assistant message back out of the
        # transcript. This is the cleanest capture point: by the time
        # pending_task is done, the assistant's response has been
        # written to the session.
        output_text, iteration_count = _extract_last_assistant_text(sess)
        still_running = getattr(sess, "_running_background_work", None)
        leftover = [
            w for w in (still_running() if callable(still_running) else [])
            if w.id not in preexisting
        ]
        if leftover:
            names = ", ".join(
                getattr(w, "label", None) or getattr(w, "summary", None) or w.id
                for w in leftover[:5]
            )
            output_text = (
                f"{output_text}\n\n(Note: {len(leftover)} piece(s) of background "
                f"work were still running when this run was delivered — {names}. "
                "This result may be incomplete.)"
            ).strip()

        # Capture per-run usage telemetry (tokens + cost) from the run's
        # own calls. This is what populates the dashboard Metrics view and
        # the per-run cost in the filesystem sink's JSON output.
        run.input_tokens = run_cost.input_tokens
        run.output_tokens = run_cost.output_tokens
        run.cache_read_tokens = run_cost.cache_read_tokens
        run.cost_usd = run_cost.cost

        # Budget enforcement — if a per-run cap was set and the run blew
        # through it, mark the run budget_exceeded and, if
        # on_exceeded=abort_job, disable the job (with the run's other
        # writes to the record, below). Marked timed_out, it was retried
        # by a retry_policy for timeouts and spent the budget again. A run
        # over a cap was stopped at the call that took it over (_RunCost).
        if job.budget is not None:
            b = job.budget
            exceeded = (
                (b.max_tokens_per_run is not None
                 and (run.input_tokens + run.output_tokens) > b.max_tokens_per_run)
                or (b.max_cost_usd_per_run is not None
                    and run.cost_usd > b.max_cost_usd_per_run)
            )
            if exceeded:
                run.status = "budget_exceeded"
                run.error = (
                    f"budget exceeded: tokens={run.input_tokens + run.output_tokens}, "
                    f"cost=${run.cost_usd:.4f}"
                )
                abort_job = b.on_exceeded == "abort_job"
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.error = f"agent turn failed: {exc}"
    finally:
        unregister_session_listener(sess_id, run_cost.on_event)
        # Clean up loop-control tools so they don't dangle. unregister()
        # drops the catalog entry too: the request's tool list is built
        # from the catalog, so popping only the tool table left them in
        # every later request of the session.
        if registered_loop_tools:
            try:
                reg = getattr(sess, "tool_registry", None)
                inner_reg = getattr(reg, "_underlying", None) or reg
                if inner_reg is not None:
                    for name in registered_loop_tools:
                        try:
                            inner_reg.unregister(name)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                pass

    run.iterations = iteration_count
    run.output_text = output_text

    # Zero-work guard. A run that completed the normal path but made no
    # model calls and produced no text never actually executed — the
    # agent was frozen before its first iteration (classically: the
    # machine slept through a cron fire that landed during a maintenance
    # DarkWake). Marking it "succeeded" would let an empty run masquerade
    # as real output (e.g. a morning briefing that was never written) and
    # suppress any retry. Fail it explicitly instead so it's visible and
    # downstream recovery can re-fire.
    if run.status == "running" and iteration_count == 0 and not (output_text or "").strip():
        run.status = "failed"
        turn_error = getattr(sess, "last_turn_error", None)
        run.error = (
            f"the turn failed before the model replied — {turn_error}"
            if turn_error
            else "agent made 0 iterations and produced no output — the fire was "
            "interrupted before it could start (most often the machine slept "
            "through the scheduled time). Nothing was produced."
        )

    # A fire that nothing answered takes its prompt back out of the session's
    # history; the run record keeps it. Left in, each failed fire adds one:
    # a loop that failed 180 times put ~540k tokens of identical prompts
    # ahead of the operator's next message.
    if iteration_count == 0 and entries_before is not None:
        _drop_unanswered_prompt(sess, entries_before, prompt)

    # Capture what the agent added to its working notes during this
    # turn — diffed against the pre-turn snapshot. This delta is what
    # the NEXT fire injects under "What you added in recent runs."
    # Empty deltas don't pollute the dir (the helper cleans up on
    # zero-diff fires).
    try:
        from bridge.scheduler.memory import capture_notes_delta
        delta_text = capture_notes_delta(job, run.run_id)
        if delta_text:
            logger.info(
                "captured notes delta for job=%s run=%s (%d chars)",
                job.id, run.run_id, len(delta_text),
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("notes delta capture failed: %s", exc)

    # Persist outputs to disk regardless of sink success — the dashboard
    # uses these as the source of truth for "what did this run produce?"
    try:
        write_run_output(job.id, run.run_id, text=output_text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("write_run_output failed: %s", exc)

    # ── Deliver to sinks ──────────────────────────────────────────────
    # A cancelled run delivers nothing and stays cancelled: delivered, its
    # partial answer went out and the delivery overwrote its status.
    if run.status not in ("failed", "timed_out", "budget_exceeded", "cancelled"):
        run.status = "delivering"
        save_run(run)
        sink_ctx = SinkContext(state=service.state, outputs_dir=write_run_output, log=logger.info)
        try:
            run.delivery_reports = await deliver_all(
                job, run, RunOutput(text=output_text, attachments=output_attachments,
                                    run=run, job=job),
                sink_ctx,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("sink fanout crashed")
            run.error = (run.error or "") + f" | sink fanout: {exc}"

        any_ok = any(r.success for r in run.delivery_reports)
        any_fail = any(not r.success for r in run.delivery_reports)
        if not job.sinks:
            run.status = "succeeded"
        elif any_ok and any_fail:
            run.status = "partial_failure"
        elif any_ok:
            run.status = "succeeded"
        else:
            run.status = "failed"

    run.finished_at = time.time()
    run.duration_seconds = run.finished_at - run.started_at
    save_run(run)

    # ── Job-level bookkeeping ─────────────────────────────────────────
    # On the record as it is now: an edit made while the job ran stays.
    def _record_fire(fresh: JobRecord) -> None:
        if loop_decision is not None:
            _apply_loop_decision(service, fresh, loop_decision)
        if abort_job:
            # No next fire, like every stopped job: the one the claim set
            # was shown as a paused job's.
            fresh.enabled = False
            fresh.status = "disabled_budget_exhausted"
            fresh.next_fire_at = None
        fresh.fire_count += 1
        fresh.last_fire_at = run.started_at
        if fresh.max_fires is not None and fresh.fire_count >= fresh.max_fires:
            fresh.status = "disabled_max_fires"
            fresh.enabled = False
            fresh.next_fire_at = None

    await update_job_record(job.id, _record_fire)

    append_event({
        "type": f"scheduler_run_{run.status}",
        "job_id": job.id,
        "run_id": run.run_id,
        "duration_seconds": run.duration_seconds,
    })
    service.emit_event(
        subtype=f"scheduler_run_{run.status}",
        message=f"Scheduled job '{job.name}' {run.status}",
        details={
            "jobId": job.id,
            "runId": run.run_id,
            "duration": run.duration_seconds,
            "deliveryReports": [r.to_dict() for r in run.delivery_reports],
        },
    )
    return run


def _apply_loop_decision(service: "SchedulerService", job: JobRecord, decision: Any) -> None:
    """Set a self-paced job's next fire from what the agent decided this
    turn. With no decision, the next fire waits the maximum delay. (The
    pre-advance before the fire already set ``next_fire_at`` to the
    minimum delay, which is why a loop whose agent never decided used to
    fire at the minimum instead.)"""
    schedule = job.schedule
    if decision.kind == "complete":
        job.enabled = False
        job.status = "disabled_max_fires"  # reuse — "natural completion"
        job.next_fire_at = None
        append_event({
            "type": "scheduler_self_paced_completed",
            "job_id": job.id,
            "reason": decision.reason,
        })
        service.emit_event(
            subtype="scheduler_self_paced_completed",
            message=f"Self-paced job '{job.name}' completed",
            details={"jobId": job.id, "reason": decision.reason},
        )
        return
    if decision.kind == "continue":
        job.next_fire_at = time.time() + decision.delay_seconds
        append_event({
            "type": "scheduler_self_paced_continued",
            "job_id": job.id,
            "delay_seconds": decision.delay_seconds,
            "reason": decision.reason,
        })
        service.emit_event(
            subtype="scheduler_self_paced_continued",
            message=f"Self-paced job '{job.name}' continued",
            details={
                "jobId": job.id,
                "delaySeconds": decision.delay_seconds,
                "reason": decision.reason,
            },
        )
        return
    job.next_fire_at = time.time() + schedule.max_delay_seconds
    append_event({
        "type": "scheduler_self_paced_defaulted",
        "job_id": job.id,
        "delay_seconds": schedule.max_delay_seconds,
    })


# ─── Execution context resolution ──────────────────────────────────────


async def _resolve_execution_session(
    service: "SchedulerService",
    job: JobRecord,
) -> tuple[Any, str, bool]:
    """Returns (session, session_id, owns_session).

    ``owns_session`` is True when the runtime created the session for
    this fire (NewSession). The session is intentionally NOT torn down
    after the run — the renderer can still inspect transcripts of
    completed scheduled runs (the user said keep them). For
    ``NewSession`` we reuse a per-job ephemeral session id so successive
    NewSession fires don't accumulate hundreds of distinct sessions in
    the bridge — each fresh fire reset()s the session before run.
    """
    state = service.state
    spec = job.execution

    if isinstance(spec, ExistingSession):
        if not spec.session_id:
            raise ValueError("ExistingSession requires session_id")
        from bridge.freyja_bridge import _gateway_holds

        if _gateway_holds(spec.session_id):
            # Run here, it went on a copy of the thread saved over the
            # gateway's. Its scheduled fires go to the gateway
            # (service._fires_in_the_gateway).
            raise ValueError(
                "its Slack thread runs in the Slack gateway; its scheduled "
                "runs go there"
            )
        sessions = getattr(state, "sessions", {}) or {}
        sess = sessions.get(spec.session_id)
        if sess is None:
            # Try restoring from disk (the bridge will rehydrate persisted
            # state if a transcript exists).
            sess = await state.ensure_session(
                spec.session_id,
                coordination_strategy=job.coordination_strategy,
                model_id=job.model_id,
            )
            # A live session keeps its own tier; one this fire brings
            # back runs at the job's.
            _apply_job_tier(sess, job)
        return sess, spec.session_id, False

    if isinstance(spec, PersistentJobSession):
        if not spec.session_id:
            # First fire — allocate a deterministic id tied to the job.
            spec.session_id = scheduler_session_id(job.id)
            session_id = spec.session_id

            def _keep_session(fresh: JobRecord) -> None:
                if isinstance(fresh.execution, PersistentJobSession):
                    fresh.execution.session_id = fresh.execution.session_id or session_id

            await update_job_record(job.id, _keep_session)
        sess = await state.ensure_session(
            spec.session_id,
            coordination_strategy=spec.coordination_strategy,
            model_id=spec.model_id or job.model_id,
        )
        _apply_job_tier(sess, job)
        return sess, spec.session_id, False

    # NewSession — use a deterministic id but reset its transcript so
    # each fire starts clean. This is the right tradeoff vs. allocating
    # a fresh uuid per fire (which would litter the bridge with
    # short-lived sessions the renderer never sees).
    sess_id = scheduler_session_id(f"{job.id}.ephemeral")
    sess = await state.ensure_session(
        sess_id,
        coordination_strategy=spec.coordination_strategy,
        # A fresh session per fire runs on the current default, not on the
        # model an earlier fire's transcript was saved with.
        model_id=spec.model_id or job.model_id or getattr(state, "default_model", None),
    )
    try:
        # If the session already has prior content from earlier fires,
        # reset before this fire so the agent starts clean. Then build
        # the empty session: restoring the transcript here, as this did,
        # loaded the last fire's back from disk, and every briefing was
        # sent the previous ones.
        inner = getattr(sess, "session", None)
        if inner is not None and len(inner.transcript) > 0:
            sess.reset()
            await sess.initialize()
    except Exception:  # noqa: BLE001
        pass
    _apply_job_tier(sess, job)
    return sess, sess_id, True


def _apply_job_tier(sess: Any, job: JobRecord) -> None:
    """Run ``sess`` at the job's permission tier. Set on the session
    alone: the process-wide tier is what every other new session takes,
    and a run used to hold the job's tier there for its whole length."""
    tier = job.permission_snapshot
    if not tier:
        return
    sess.permission_tier = tier
    handler = getattr(sess, "permission_handler", None)
    if handler is not None:
        handler.set_policy(tier)


# ─── Prompt composition ────────────────────────────────────────────────


def _compose_fire_prompt(
    job: JobRecord,
    run: RunRecord,
    service: "SchedulerService",
    *,
    scheduled_for: float | None = None,
) -> str:
    parts: list[str] = []

    header = (
        f"[Scheduled run — fire {run.fire_number} of job \"{job.name}\""
        f" (id: {job.id})]\n"
        "This invocation is automated. There is no human in the loop "
        "right now — do not ask clarifying questions, just complete "
        "the task. If you cannot proceed, explain why so the user "
        "can fix it on the next fire."
    )
    parts.append(header)

    # Skill preloading — load each skill body inline so the agent
    # doesn't have to call load_skill itself before doing anything.
    if job.skills_to_load:
        skill_blocks = _load_skill_bodies(service, job.skills_to_load)
        if skill_blocks:
            parts.append("[Preloaded skills for this run:]")
            parts.extend(skill_blocks)

    # Working memory — accumulated notes + recent deltas. This is
    # what makes a recurring job feel like it's actually compounding:
    # the agent sees what it's learned over prior runs, what it's
    # tried that didn't work, style/voice decisions, etc.
    try:
        from bridge.scheduler.memory import notes_path_for, render_memory_for_prompt

        memory_block = render_memory_for_prompt(job)
        if memory_block:
            parts.append("# Memory across runs\n\n" + memory_block)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory render failed: %s", exc)

    # Artifact reference — the canonical thing this job's runs revolve
    # around. We give the agent the reference (a path, a URL — anything)
    # and trust it to use its existing tools (web_fetch, gh CLI, file
    # I/O, etc.) to read or update it. No scheduler-specific artifact
    # tools — the agent already has the full toolbox.
    if job.artifact:
        parts.append(_artifact_block(job))

    # Notes-maintenance contract — only when memory is enabled.
    # Tells the agent to capture learnings before finishing the turn.
    if getattr(job.memory, "enabled", True):
        try:
            from bridge.scheduler.memory import notes_path_for as _np
            notes_p = str(_np(job))
        except Exception:  # noqa: BLE001
            notes_p = "(memory path resolution failed)"
        parts.append(
            "# Notes contract\n\n"
            f"Your working notes file is at `{notes_p}`. "
            "Before finishing this turn, append any learnings that will "
            "help future runs of this job:\n"
            "- process refinements / shortcuts that worked\n"
            "- APIs, endpoints, or services that didn't work + why\n"
            "- decisions you made and the reasoning\n"
            "- style/voice/format conventions for the artifact\n"
            "- anything you wish you'd known at the start of this run\n\n"
            "Use `edit_file` / `write_file` / `bash` to update the file. "
            "Don't restate what's already in the notes — append only what's new. "
            "There is no length cap — be substantive when it matters."
        )

    # Self-paced loops get the continue/complete contract appended.
    if isinstance(job.schedule, SelfPacedSchedule):
        parts.append(_self_paced_contract(job, run))

    # Fire-time context preludes — data that changes per fire and so
    # can't live in the snapshotted prompt. The briefer uses this to
    # inject its deterministic recency shortlist; other jobs get nothing.
    # Guarded: a prelude failure must never break a fire.
    try:
        from bridge.briefing import fire_context_block

        prelude = fire_context_block(job)
        if prelude:
            parts.append(prelude)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire context prelude failed: %s", exc)

    # A late run says so, right before the task: a reminder that fires
    # hours after its time must not read as if it were on time.
    if scheduled_for is not None and run.started_at - scheduled_for >= _LATE_AFTER_SECONDS:
        due = datetime.fromtimestamp(scheduled_for).astimezone()
        parts.append(
            "# This run is late\n\n"
            f"It was due at {due:%Y-%m-%d %H:%M %Z} and starts "
            f"{_how_late(run.started_at - scheduled_for)} after that. "
            "Open your reply by saying that it is late and when it was due."
        )

    # The job's actual prompt goes last so it's what the model sees
    # most prominently.
    parts.append("# Your task\n\n" + job.prompt)
    return "\n\n".join(parts)


def _how_late(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} days {hours % 24} h"


def _artifact_block(job: JobRecord) -> str:
    """Render the artifact reference for the fire-time prompt. Free-form
    string — could be a local path, a URL, a github reference. We do
    one cheap heuristic: if it looks like a local file path that
    exists, include its current contents inline (small files only) so
    the agent doesn't have to do a tool call just to read it. For URLs
    or paths above the inline cap, we just tell the agent where it is
    and let it fetch.
    """
    import os as _os
    from pathlib import Path as _Path

    ref = (job.artifact or "").strip()
    inline_cap = 50_000  # ~12k tokens — beyond that the agent should fetch

    looks_local = (
        ref.startswith("/")
        or ref.startswith("~")
        or ref.startswith("./")
        or ref.startswith("../")
        or (len(ref) > 1 and ref[1] == ":")  # windows-ish
    )

    block_header = (
        "# Artifact\n\n"
        f"The canonical reference for this job is: `{ref}`\n\n"
        "Read it at fire start so you know the current state, then "
        "update it as part of completing your task. Use your existing "
        "tools (`read_file` / `edit_file` / `bash` / `gh` CLI / "
        "`web_fetch`, etc.) — whatever the artifact type requires."
    )

    if looks_local:
        try:
            p = _Path(_os.path.expanduser(_os.path.expandvars(ref)))
            if p.exists() and p.is_file():
                size = p.stat().st_size
                if size > 0 and size <= inline_cap:
                    try:
                        contents = p.read_text(encoding="utf-8")
                        return (
                            block_header
                            + f"\n\n## Current contents ({size} bytes)\n\n```\n{contents}\n```"
                        )
                    except (OSError, UnicodeDecodeError):
                        pass
                elif size > inline_cap:
                    return (
                        block_header
                        + f"\n\n_(File is {size} bytes — too large to inline. "
                        f"Read it with `read_file` or `bash`.)_"
                    )
                else:
                    return block_header + "\n\n_(File exists but is empty — write the first version.)_"
            elif p.is_dir():
                # Directory artifact (e.g. the briefer's briefing/ root):
                # list a few entries so the agent sees the current shape
                # without a tool call, newest first.
                try:
                    entries = sorted(
                        (e for e in p.iterdir() if not e.name.startswith(".")),
                        key=lambda e: e.stat().st_mtime,
                        reverse=True,
                    )[:12]
                    listing = "\n".join(
                        f"- {e.name}{'/' if e.is_dir() else ''}" for e in entries
                    ) or "(empty)"
                    return (
                        block_header
                        + f"\n\n_(Directory — current entries, newest first:)_\n{listing}"
                    )
                except OSError:
                    return block_header + "\n\n_(Directory — list it with `bash`.)_"
            elif not p.exists():
                return block_header + "\n\n_(File does not exist yet — create it.)_"
        except Exception:  # noqa: BLE001
            pass

    return block_header


def _self_paced_contract(job: JobRecord, run: RunRecord) -> str:
    from bridge.tools.schedule_tool import COMPLETE_LOOP_TOOL, CONTINUE_LOOP_TOOL

    schedule = job.schedule
    assert isinstance(schedule, SelfPacedSchedule)
    until = schedule.until_condition
    until_clause = (
        f"\nIf this condition is now satisfied — \"{until}\" — call "
        f"`{COMPLETE_LOOP_TOOL}` instead of continuing."
        if until else ""
    )
    return (
        f"[Self-paced loop — iteration {run.iteration + 1}]\n"
        f"Before finishing this turn, call EXACTLY ONE of:\n"
        f"  • `{CONTINUE_LOOP_TOOL}(delay_seconds, reason)` — schedule"
        f" the next iteration. delay_seconds must be in"
        f" [{schedule.min_delay_seconds}, {schedule.max_delay_seconds}].\n"
        f"  • `{COMPLETE_LOOP_TOOL}(reason)` — terminate the loop.\n"
        f"If you call neither, the next iteration waits the maximum "
        f"delay."
        f"{until_clause}"
    )


def _load_skill_bodies(service: "SchedulerService", skill_names: list[str]) -> list[str]:
    """Load skill bodies from the bridge's skill store. Returns one
    formatted block per loaded skill (silently skips missing ones —
    the dashboard surfaces the misses in run metadata)."""
    out: list[str] = []
    state = service.state
    # Use any available bridge session's skill store. We pick the
    # first session that has one — the store is process-global so
    # any reference works.
    sessions = getattr(state, "sessions", {}) or {}
    store = None
    for sess in sessions.values():
        store = getattr(sess, "skill_store", None) or getattr(sess, "_skill_store", None)
        if store is not None:
            break
    if store is None:
        return out
    for name in skill_names:
        try:
            rec = None
            if hasattr(store, "get"):
                rec = store.get(name)
            elif hasattr(store, "load"):
                rec = store.load(name)
            if rec is None:
                continue
            body = getattr(rec, "instructions", None) or getattr(rec, "body", None) or ""
            if body:
                out.append(f"## Skill: {name}\n{body}")
        except Exception:  # noqa: BLE001
            continue
    return out


# ─── Output capture ────────────────────────────────────────────────────


def _extract_last_assistant_text(sess: Any) -> tuple[str, int]:
    """Pull the agent's final response off the session transcript.

    Walks back from the tail until we find an assistant message. The
    text is concatenated content of all TextBlocks in that message.
    Returns ``(text, iteration_count)`` where iteration_count is the
    number of assistant messages emitted during the trailing scheduled
    fire (a proxy for runner iterations within this turn)."""
    inner = getattr(sess, "session", None)
    if inner is None:
        return "", 0
    try:
        entries = list(inner.transcript.entries)
    except Exception:  # noqa: BLE001
        return "", 0
    # Walk back through the trailing tool/assistant block until we hit
    # the user message that started this fire. Everything after that
    # is "this turn."
    assistant_count = 0
    final_text = ""
    for e in reversed(entries):
        msg = getattr(e, "message", None)
        if msg is None:
            continue
        role = getattr(msg, "role", "")
        if role == "assistant":
            assistant_count += 1
            if not final_text:
                final_text = _extract_text(msg)
        elif role == "user":
            break
    return final_text, assistant_count


class _RunCost:
    """What a run's own model calls cost and the tokens they used, added
    up from its session's llm_call_metric events: the calls of the turn
    that runs the fire's prompt, and of the turns after it that no
    message started (the wakes its sub-agents' reports start, a goal
    loop's continuations). A run's cost was how much the session's total
    grew while it ran, so a fire into the operator's session was billed
    the turns of the messages the operator sent while it waited. The
    calls of the sub-agents of its session that started while it ran are
    its cost too: they report under their own session ids, so neither of
    its budgets saw them.

    The call that takes the run over its ``budget``'s cost or token cap
    stops it. Checked only when the run ended, a run over its budget had
    spent the money by then. Its tokens were read from the transcript
    after its last user entry, which left out every turn before the last
    (a sub-agent's report wakes the session with a user message)."""

    def __init__(self, run_id: str, budget: BudgetSpec | None) -> None:
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self._run_id = run_id
        self._budget = budget
        self._started = time.time()
        # emit() also runs on worker threads (compaction's calls).
        self._loop = asyncio.get_running_loop()
        self._turns: set[str] = set()
        self._next_turn_is_ours = False

    def our_turn_starts(self) -> None:
        """The on_turn_start hook of the fire's prompt: the session's next
        turn is the fire's."""
        self._next_turn_is_ours = True

    def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "turn_start":
            if self._next_turn_is_ours or (self._turns and event.get("automatic")):
                self._turns.add(event.get("turnId"))
            self._next_turn_is_ours = False
        elif (
            (kind == "llm_call_metric" and event.get("turnId") in self._turns)
            or (kind == "subagent_llm_call"
                and float(event.get("subagentStartedAt") or 0.0) >= self._started)
        ):
            self.cost += float(event.get("costUsd") or 0.0)
            self.input_tokens += int(event.get("inputTokens") or 0)
            self.output_tokens += int(event.get("outputTokens") or 0)
            self.cache_read_tokens += int(event.get("cacheReadTokens") or 0)
            if self._over_budget():
                self._loop.call_soon_threadsafe(stop_run_now, self._run_id, "budget_exceeded")

    def _over_budget(self) -> bool:
        b = self._budget
        return b is not None and (
            (b.max_cost_usd_per_run is not None and self.cost > b.max_cost_usd_per_run)
            or (b.max_tokens_per_run is not None
                and self.input_tokens + self.output_tokens > b.max_tokens_per_run)
        )


def _entry_ids(sess: Any) -> set[str] | None:
    transcript = getattr(getattr(sess, "session", None), "transcript", None)
    if transcript is None:
        return None
    return {e.id for e in transcript.entries}


def _drop_unanswered_prompt(sess: Any, entries_before: set[str], prompt: str) -> bool:
    """Remove this fire's prompt from the session's history when no reply
    followed it, and save. Anything else added meanwhile stays."""
    transcript = getattr(getattr(sess, "session", None), "transcript", None)
    if transcript is None:
        return False
    added = [e for e in transcript.entries if e.id not in entries_before]
    if any(getattr(e.message, "role", "") == "assistant" for e in added):
        return False
    header = prompt.split("\n", 1)[0]
    ids = {
        e.id for e in added
        if getattr(e.message, "role", "") == "user" and header in _extract_text(e.message)
    }
    if not ids:
        return False
    transcript.remove_entries(ids)
    save = getattr(sess, "_save_transcript", None)
    if callable(save):
        save()
    return True


def _extract_text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


# ─── Cross-process cancellation polling ────────────────────────────────


def _run_abort(on_abort: Any) -> None:
    if on_abort is None:
        return
    try:
        on_abort()
    except Exception as exc:  # noqa: BLE001
        logger.debug("scheduler abort hook failed: %s", exc)


# With no job timeout, how long a fire waits on background work (sub-agents,
# backgrounded commands) after its own turns are done before delivering.
BACKGROUND_WORK_WAIT_CAP_S = 30 * 60


# How often to poll for the cancel flag while the agent turn runs.
# 1s gives the user sub-second-feel cancellation latency without
# burning CPU on a stat call.
_CANCEL_POLL_INTERVAL = 1.0

# Runs of this process waiting on their turn, by run id. Resolving one's
# future stops the run at once rather than at its next look at the flag.
_stop_now: dict[str, asyncio.Future] = {}


def stop_run_now(run_id: str, outcome: str = "cancelled") -> bool:
    """Stop ``run_id`` at once when it runs in this process and waits on
    its turn: it ends as ``outcome``, "cancelled" as if it saw its cancel
    flag, or "budget_exceeded". Returns True if it does. Cancelling the
    run's task would end the run where it stands, its record left
    "running" and the job's not updated."""
    stop = _stop_now.get(run_id)
    if stop is None or stop.done():
        return False
    stop.set_result(outcome)
    return True


async def _await_pending_with_cancel_poll(
    pending: asyncio.Future,
    *,
    job_id: str,
    run_id: str,
    timeout_seconds: float | None,
    on_abort: Any = None,
) -> str:
    """Await ``pending`` while also polling the run JSON for
    ``cancel_requested``.

    ``on_abort`` (zero-arg, sync) runs whenever we give up on ``pending``
    — timeout, cancel flag, or our own cancellation — for the work that
    cancelling ``pending`` alone would not stop (the session's turn and
    background sub-agents, when ``pending`` is a quiescence waiter).

    Returns one of:
      · "completed"  — turn finished on its own
      · "cancelled"  — another process set cancel_requested; we
        cancelled the local turn
      · "timed_out"  — wall-clock budget exceeded; we cancelled
      · "budget_exceeded" — the run's calls went over its cost cap
        (stop_run_now); we cancelled

    The poll uses a per-loop ``asyncio.wait`` with a 1s timeout so the
    scheduler tick / other tasks aren't starved.
    """
    from bridge.scheduler.persistence import read_run_cancel_requested

    started = time.time()
    stop_now = _stop_now[run_id] = asyncio.get_running_loop().create_future()
    try:
        while True:
            # If the turn finished while we slept, exit.
            if pending.done():
                return "completed"
            # Wall-clock timeout check.
            if timeout_seconds is not None and timeout_seconds > 0:
                elapsed = time.time() - started
                if elapsed >= timeout_seconds:
                    _run_abort(on_abort)
                    try:
                        pending.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                    # Give the cancellation a brief grace period before
                    # returning, so the runner has a chance to clean up
                    # its LLM stream before we mark the run timed_out.
                    try:
                        await asyncio.wait_for(asyncio.shield(pending), timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        pass
                    return "timed_out"
            # Cross-process cancel-flag check, or stop_run_now in this one.
            if stop_now.done() or read_run_cancel_requested(job_id, run_id):
                outcome = stop_now.result() if stop_now.done() else "cancelled"
                _run_abort(on_abort)
                try:
                    pending.cancel()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                return outcome
            # Sleep until next poll OR the task completes OR stop_run_now
            # — whichever comes first. ``asyncio.wait`` lets us race them.
            poll_window = _CANCEL_POLL_INTERVAL
            if timeout_seconds is not None and timeout_seconds > 0:
                remaining = timeout_seconds - (time.time() - started)
                poll_window = max(0.05, min(poll_window, remaining))
            try:
                done, _pending = await asyncio.wait(
                    {pending, stop_now},
                    timeout=poll_window,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending in done:
                    return "completed"
            except asyncio.CancelledError:
                # OUR task got cancelled (e.g. service.stop()). Pass it
                # on to the turn so the LLM stream stops cleanly.
                _run_abort(on_abort)
                try:
                    pending.cancel()
                except Exception:  # noqa: BLE001
                    pass
                raise
    finally:
        _stop_now.pop(run_id, None)
