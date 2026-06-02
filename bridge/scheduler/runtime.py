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
    # Snapshot working notes BEFORE composing the prompt — the
    # composer reads them, and the post-turn diff needs the pre-state
    # to compute what the agent added.
    try:
        from bridge.scheduler.memory import snapshot_notes
        snapshot_notes(job, run.run_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("notes snapshot failed: %s", exc)
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
            # Poll the run JSON for cancel_requested while we wait. A
            # peer process (e.g. the user clicked Cancel in the
            # dashboard while the daemon is firing) can set this flag
            # via persistence.request_run_cancellation; the firing
            # process notices and cancels its local turn task.
            timeout = job.timeout_seconds or (
                job.budget.wall_clock_timeout_seconds if job.budget else None
            )
            cancel_outcome = await _await_pending_with_cancel_poll(
                pending,
                job_id=job.id,
                run_id=run.run_id,
                timeout_seconds=timeout,
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

    # The job's actual prompt goes last so it's what the model sees
    # most prominently.
    parts.append("# Your task\n\n" + job.prompt)
    return "\n\n".join(parts)


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
            elif not p.exists():
                return block_header + "\n\n_(File does not exist yet — create it.)_"
        except Exception:  # noqa: BLE001
            pass

    return block_header


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


# ─── Cross-process cancellation polling ────────────────────────────────


# How often to poll for the cancel flag while the agent turn runs.
# 1s gives the user sub-second-feel cancellation latency without
# burning CPU on a stat call.
_CANCEL_POLL_INTERVAL = 1.0


async def _await_pending_with_cancel_poll(
    pending: asyncio.Task,
    *,
    job_id: str,
    run_id: str,
    timeout_seconds: float | None,
) -> str:
    """Await ``pending`` while also polling the run JSON for
    ``cancel_requested``.

    Returns one of:
      · "completed"  — turn finished on its own
      · "cancelled"  — another process set cancel_requested; we
        cancelled the local turn
      · "timed_out"  — wall-clock budget exceeded; we cancelled

    The poll uses a per-loop ``asyncio.wait`` with a 1s timeout so the
    scheduler tick / other tasks aren't starved.
    """
    from bridge.scheduler.persistence import read_run_cancel_requested

    started = time.time()
    while True:
        # If the turn finished while we slept, exit.
        if pending.done():
            return "completed"
        # Wall-clock timeout check.
        if timeout_seconds is not None and timeout_seconds > 0:
            elapsed = time.time() - started
            if elapsed >= timeout_seconds:
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
        # Cross-process cancel-flag check.
        if read_run_cancel_requested(job_id, run_id):
            try:
                pending.cancel()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            return "cancelled"
        # Sleep until next poll OR the task completes — whichever
        # comes first. ``asyncio.wait`` lets us race the two.
        poll_window = _CANCEL_POLL_INTERVAL
        if timeout_seconds is not None and timeout_seconds > 0:
            remaining = timeout_seconds - (time.time() - started)
            poll_window = max(0.05, min(poll_window, remaining))
        try:
            done, _pending = await asyncio.wait(
                {pending},
                timeout=poll_window,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done:
                return "completed"
        except asyncio.CancelledError:
            # OUR task got cancelled (e.g. service.stop()). Pass it
            # on to the turn so the LLM stream stops cleanly.
            try:
                pending.cancel()
            except Exception:  # noqa: BLE001
                pass
            raise
