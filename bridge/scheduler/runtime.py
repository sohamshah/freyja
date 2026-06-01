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
from typing import TYPE_CHECKING, Any

from bridge.scheduler.models import (
    ExistingSession,
    JobRecord,
    NewSession,
    PersistentJobSession,
    RunRecord,
    SelfPacedSchedule,
)
from bridge.scheduler.persistence import (
    save_job,
    save_run,
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


def scheduler_session_id(job_id: str) -> str:
    return f"{_SCHED_SESSION_PREFIX}{job_id}"


async def fire_job(
    service: "SchedulerService",
    job: JobRecord,
    *,
    iteration: int = 0,
) -> RunRecord:
    """Run one fire of ``job``. Returns the run record. The caller
    (SchedulerService.tick / run_job_now) is responsible for
    pre-advancing the next_fire_at and persisting BEFORE this call,
    so a crash mid-fire doesn't double-fire.
    """
    from bridge.scheduler.models import new_run_id

    run = RunRecord(
        run_id=new_run_id(),
        job_id=job.id,
        job_name=job.name,
        started_at=time.time(),
        fire_number=job.fire_count + 1,
        iteration=iteration,
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
    })
    service.emit_event(
        subtype="scheduler_run_claimed",
        message=f"Scheduled job '{job.name}' starting",
        details={"jobId": job.id, "runId": run.run_id, "fireNumber": run.fire_number},
    )

    # Snapshot tier for this job onto the bridge tier global. The bridge
    # session inherits this when it initializes; restoring afterwards
    # keeps interactive sessions untouched.
    original_tier = getattr(service.state, "permission_tier", "low")
    service.state.permission_tier = job.permission_snapshot or original_tier

    try:
        sess, sess_id, owns_session = await _resolve_execution_session(service, job)
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.error = f"execution context resolution failed: {exc}"
        run.finished_at = time.time()
        run.duration_seconds = run.finished_at - run.started_at
        save_run(run)
        service.state.permission_tier = original_tier
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
    # session's tool registry for the duration of the fire. They
    # mutate next_fire_at on this exact job. We unregister after the
    # turn completes so other jobs sharing the session don't see
    # stale references.
    registered_loop_tools: list[str] = []
    if isinstance(job.schedule, SelfPacedSchedule):
        try:
            from bridge.tools.schedule_tool import (
                build_complete_loop_tool,
                build_continue_loop_tool,
            )
            reg = getattr(sess, "tool_registry", None)
            inner_reg = getattr(reg, "_underlying", None) or reg
            if inner_reg is not None:
                cont = build_continue_loop_tool(service=service, job_id=job.id)
                comp = build_complete_loop_tool(service=service, job_id=job.id)
                inner_reg.register(cont)
                inner_reg.register(comp)
                registered_loop_tools.extend([
                    cont.definition.name, comp.definition.name,
                ])
        except Exception as exc:  # noqa: BLE001
            logger.debug("loop tool registration failed: %s", exc)

    # Compose the fire-time prompt. We pre-load skills if the job
    # requested any, and inject a small system-style header that tells
    # the model this is a scheduled fire (so it doesn't ask clarifying
    # questions or expect a human in the loop).
    prompt = _compose_fire_prompt(job, run, service)

    # ── Run the agent turn ────────────────────────────────────────────
    output_text = ""
    output_attachments: list[dict[str, Any]] = []
    iteration_count = 0
    try:
        from bridge.freyja_bridge import _schedule_or_queue_turn

        # _schedule_or_queue_turn returns True for immediate dispatch
        # and False for queued. Either way we'll await sess.pending_task
        # below to wait for completion.
        _schedule_or_queue_turn(sess, prompt, attachments=None)
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
            service.state.permission_tier = original_tier
            return run
        if pending is not None:
            try:
                # Apply the wall-clock timeout if configured.
                timeout = job.timeout_seconds or (
                    job.budget.wall_clock_timeout_seconds if job.budget else None
                )
                if timeout and timeout > 0:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=timeout)
                else:
                    await pending
            except asyncio.TimeoutError:
                # Cancel the in-flight turn so the runner stops the LLM
                # stream cleanly; the run is marked timed_out.
                try:
                    pending.cancel()
                except Exception:  # noqa: BLE001
                    pass
                run.status = "timed_out"
                run.error = f"wall-clock timeout after {timeout}s"

        # Read the most recent assistant message back out of the
        # transcript. This is the cleanest capture point: by the time
        # pending_task is done, the assistant's response has been
        # written to the session.
        output_text, iteration_count = _extract_last_assistant_text(sess)

        # Capture per-run usage telemetry (tokens + cost) from the
        # session's recently-modified messages. This is what populates
        # the dashboard Metrics view and the per-run cost in the
        # filesystem sink's JSON output.
        try:
            in_tok, out_tok, cache_tok, cost = _extract_run_usage(sess, run.started_at)
            run.input_tokens = in_tok
            run.output_tokens = out_tok
            run.cache_read_tokens = cache_tok
            run.cost_usd = cost
        except Exception:  # noqa: BLE001
            pass

        # Budget enforcement (post-hoc) — if a per-run cap was set and
        # the run blew through it, mark the run timed_out/aborted and,
        # if on_exceeded=abort_job, disable the job.
        if job.budget is not None:
            b = job.budget
            exceeded = (
                (b.max_tokens_per_run is not None
                 and (run.input_tokens + run.output_tokens) > b.max_tokens_per_run)
                or (b.max_cost_usd_per_run is not None
                    and run.cost_usd > b.max_cost_usd_per_run)
            )
            if exceeded:
                run.status = "timed_out"
                run.error = (
                    f"budget exceeded: tokens={run.input_tokens + run.output_tokens}, "
                    f"cost=${run.cost_usd:.4f}"
                )
                if b.on_exceeded == "abort_job":
                    job.enabled = False
                    job.status = "disabled_budget_exhausted"
                    save_job(job)
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.error = f"agent turn failed: {exc}"
    finally:
        service.state.permission_tier = original_tier
        # Clean up loop-control tools so they don't dangle.
        if registered_loop_tools:
            try:
                reg = getattr(sess, "tool_registry", None)
                inner_reg = getattr(reg, "_underlying", None) or reg
                if inner_reg is not None:
                    for name in registered_loop_tools:
                        try:
                            inner_reg._tools.pop(name, None)  # noqa: SLF001
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                pass

    run.iterations = iteration_count
    run.output_text = output_text

    # Persist outputs to disk regardless of sink success — the dashboard
    # uses these as the source of truth for "what did this run produce?"
    try:
        write_run_output(job.id, run.run_id, text=output_text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("write_run_output failed: %s", exc)

    # ── Deliver to sinks ──────────────────────────────────────────────
    if run.status not in ("failed", "timed_out"):
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
    job.fire_count += 1
    job.last_fire_at = run.started_at
    if job.max_fires is not None and job.fire_count >= job.max_fires:
        job.status = "disabled_max_fires"
        job.enabled = False
        job.next_fire_at = None
    save_job(job)

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
        return sess, spec.session_id, False

    if isinstance(spec, PersistentJobSession):
        if not spec.session_id:
            # First fire — allocate a deterministic id tied to the job.
            spec.session_id = scheduler_session_id(job.id)
            save_job(job)
        sess = await state.ensure_session(
            spec.session_id,
            coordination_strategy=spec.coordination_strategy,
            model_id=spec.model_id or job.model_id,
        )
        return sess, spec.session_id, False

    # NewSession — use a deterministic id but reset its transcript so
    # each fire starts clean. This is the right tradeoff vs. allocating
    # a fresh uuid per fire (which would litter the bridge with
    # short-lived sessions the renderer never sees).
    sess_id = scheduler_session_id(f"{job.id}.ephemeral")
    sess = await state.ensure_session(
        sess_id,
        coordination_strategy=spec.coordination_strategy,
        model_id=spec.model_id or job.model_id,
    )
    try:
        # If the session already has prior content from earlier fires,
        # reset before this fire so the agent starts clean.
        inner = getattr(sess, "session", None)
        if inner is not None and len(inner.transcript) > 0:
            sess.reset()
            await sess.try_restore_transcript()  # no-op since we just reset
    except Exception:  # noqa: BLE001
        pass
    return sess, sess_id, True


# ─── Prompt composition ────────────────────────────────────────────────


def _compose_fire_prompt(
    job: JobRecord,
    run: RunRecord,
    service: "SchedulerService",
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

    # Self-paced loops get the continue/complete contract appended.
    if isinstance(job.schedule, SelfPacedSchedule):
        parts.append(_self_paced_contract(job, run))

    # The job's actual prompt goes last so it's what the model sees
    # most prominently.
    parts.append(job.prompt)
    return "\n\n".join(parts)


def _self_paced_contract(job: JobRecord, run: RunRecord) -> str:
    schedule = job.schedule
    assert isinstance(schedule, SelfPacedSchedule)
    until = schedule.until_condition
    until_clause = (
        f"\nIf this condition is now satisfied — \"{until}\" — call "
        "`scheduler.complete_loop` instead of continuing."
        if until else ""
    )
    return (
        f"[Self-paced loop — iteration {run.iteration + 1}]\n"
        f"Before finishing this turn, call EXACTLY ONE of:\n"
        f"  • `scheduler.continue_loop(delay_seconds, reason)` — schedule"
        f" the next iteration. delay_seconds must be in"
        f" [{schedule.min_delay_seconds}, {schedule.max_delay_seconds}].\n"
        f"  • `scheduler.complete_loop(reason)` — terminate the loop.\n"
        f"If you call neither, the runtime defaults to the maximum "
        f"delay and emits a warning."
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


def _extract_run_usage(sess: Any, run_started_at: float) -> tuple[int, int, int, float]:
    """Walk back through the transcript adding up token counts on
    assistant messages whose timestamp is at or after ``run_started_at``.
    Cost is approximated via the runner's cumulative_cost delta when
    available; otherwise we leave it at 0 and let the dashboard rely
    on token counts alone."""
    inner = getattr(sess, "session", None)
    if inner is None:
        return 0, 0, 0, 0.0
    in_tok = 0
    out_tok = 0
    cache_tok = 0
    try:
        entries = list(inner.transcript.entries)
    except Exception:  # noqa: BLE001
        return 0, 0, 0, 0.0
    for e in reversed(entries):
        msg = getattr(e, "message", None)
        if msg is None:
            continue
        # Stop when we cross back into a user turn (we only want the
        # tail belonging to this fire).
        if getattr(msg, "role", "") == "user":
            break
        if getattr(msg, "role", "") != "assistant":
            continue
        in_tok += int(getattr(msg, "input_tokens", 0) or 0)
        out_tok += int(getattr(msg, "output_tokens", 0) or 0)
        cache_tok += int(getattr(msg, "cache_read_tokens", 0) or 0)
    # Best-effort cost. The runner tracks cumulative_cost on the
    # session; we can't easily slice "just this run" without a delta
    # snapshot, so leave cost at 0 unless the session exposes a
    # per-run hook later.
    cost = 0.0
    cumulative = getattr(sess, "cumulative_cost", None)
    if isinstance(cumulative, (int, float)):
        # Approximate by attributing the latest delta — caller is
        # responsible for snapshotting before/after if precision
        # matters.
        cost = float(cumulative)
    return in_tok, out_tok, cache_tok, cost


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
