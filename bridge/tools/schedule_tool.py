"""Model-callable ``schedule`` tool.

Single action-oriented surface — create | list | get | pause | resume |
remove | run_now | runs | metrics. Same JobRecord shape regardless of
whether the agent is in a desktop session or a Slack session: the
gateway_source field on the bridge session tells us the creator
context.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bridge.scheduler.models import (
    BudgetSpec,
    CronSchedule,
    IntervalSchedule,
    JobFilter,
    JobPatch,
    JobRecord,
    OnceSchedule,
    SelfPacedSchedule,
)
from bridge.scheduler.scheduling import cadence_label
from bridge.scheduler.service import (
    SchedulerService,
    build_creator_ref,
    build_execution,
    build_schedule,
    build_sinks,
)
from engine.tools import ToolDefinition, ToolTier
from engine.types import ToolResult

logger = logging.getLogger("freyja.tools.schedule")


class ScheduleTool:
    """Single typed tool covering the entire scheduler API.

    The tool reads the creating session's ``gateway_source`` and a
    ``current_session_id`` (passed by the registry on construction) to
    figure out the creator context. That keeps the model's call simple
    — no need to spell out workspace_id, channel_id, etc.
    """

    requires_permission = False  # creating jobs is a cheap CRUD op

    def __init__(
        self,
        *,
        service: SchedulerService,
        current_session_id: str,
        gateway_source_getter: Any | None = None,
        current_user_id: str | None = None,
    ) -> None:
        self._service = service
        self._session_id = current_session_id
        self._gw_source_getter = gateway_source_getter
        self._user_id = current_user_id

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="schedule",
            summary=(
                "Create, inspect, and manage scheduled jobs that fire "
                "agent turns at specified times. One tool, many actions."
            ),
            description=_DESCRIPTION,
            parameters=_PARAMETERS,
            tier=ToolTier.WARM,
        )

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        action = (arguments.get("action") or "").strip().lower()
        try:
            if action == "create":
                payload = await self._action_create(arguments)
            elif action == "list":
                payload = await self._action_list(arguments)
            elif action == "get":
                payload = await self._action_get(arguments)
            elif action == "update":
                payload = await self._action_update(arguments)
            elif action == "pause":
                payload = await self._action_simple("pause", arguments)
            elif action == "resume":
                payload = await self._action_simple("resume", arguments)
            elif action == "remove":
                payload = await self._action_simple("remove", arguments)
            elif action in ("run_now", "run"):
                payload = await self._action_run_now(arguments)
            elif action == "runs":
                payload = await self._action_runs(arguments)
            elif action in ("get_run", "run_detail"):
                payload = await self._action_get_run(arguments)
            elif action == "metrics":
                payload = await self._action_metrics()
            else:
                return _err(call_id, f"unknown action: {action!r}")
        except KeyError as exc:
            return _err(call_id, f"job not found: {exc}")
        except ValueError as exc:
            return _err(call_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("schedule tool action failed")
            return _err(call_id, f"action failed: {exc}")

        return ToolResult(
            call_id=call_id,
            content=_format_payload(payload),
            is_error=False,
        )

    # ─── Actions ─────────────────────────────────────────────────────

    async def _action_create(self, args: dict[str, Any]) -> dict[str, Any]:
        creator = self._creator_ref()
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("'prompt' is required")
        name = (args.get("name") or "").strip() or _autoname(prompt)
        timezone = args.get("timezone") or "UTC"

        schedule = build_schedule(
            args.get("when"),
            args.get("schedule"),
            timezone=timezone,
        )
        execution = build_execution(args.get("execution"), creator=creator)
        sinks = build_sinks(args.get("sinks"), creator=creator, state=self._service.state)

        budget = None
        if isinstance(args.get("budget"), dict):
            budget = BudgetSpec.from_dict(args["budget"])

        spec = JobRecord(
            id="",
            name=name,
            description=args.get("description", ""),
            creator=creator,
            schedule=schedule,
            enabled=bool(args.get("enabled", True)),
            max_fires=args.get("max_fires"),
            prompt=prompt,
            execution=execution,
            permission_snapshot=getattr(
                self._service.state, "permission_tier", "low",
            ),
            model_id=args.get("model_id"),
            coordination_strategy=args.get("coordination_strategy", "bus"),
            skills_to_load=list(args.get("skills_to_load") or []),
            budget=budget,
            sinks=sinks,
            misfire_policy=args.get("misfire_policy", "skip"),
            overlap_policy=args.get("overlap_policy", "queue"),
            timeout_seconds=args.get("timeout_seconds"),
            allow_creates_schedules=bool(args.get("allow_creates_schedules", False)),
            tags=list(args.get("tags") or []),
        )
        job = await self._service.create_job(spec)
        return {
            "action": "created",
            "job": _job_summary(job),
        }

    async def _action_list(self, args: dict[str, Any]) -> dict[str, Any]:
        filt = JobFilter(
            user_id=args.get("user_id"),
            surface=args.get("surface"),
            status=args.get("status"),
            tag=args.get("tag"),
            enabled=args.get("enabled"),
        )
        jobs = await self._service.list_jobs(filt)
        return {
            "action": "list",
            "count": len(jobs),
            "jobs": [_job_summary(j) for j in jobs],
        }

    async def _action_get(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = _require_job_id(args)
        job = await self._service.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return {"action": "get", "job": _job_summary(job)}

    async def _action_update(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = _require_job_id(args)
        patch = JobPatch()
        for k in (
            "name", "description", "enabled", "max_fires", "prompt",
            "permission_snapshot", "model_id", "coordination_strategy",
            "skills_to_load", "misfire_policy", "overlap_policy",
            "timeout_seconds", "tags",
        ):
            if k in args:
                setattr(patch, k, args[k])
        if "when" in args or "schedule" in args:
            patch.schedule = build_schedule(
                args.get("when"), args.get("schedule"),
                timezone=args.get("timezone", "UTC"),
            )
        if "execution" in args:
            patch.execution = build_execution(args["execution"], creator=self._creator_ref())
        if "sinks" in args:
            patch.sinks = build_sinks(
                args["sinks"], creator=self._creator_ref(),
                state=self._service.state,
            )
        if isinstance(args.get("budget"), dict):
            patch.budget = BudgetSpec.from_dict(args["budget"])
        job = await self._service.update_job(job_id, patch)
        return {"action": "updated", "job": _job_summary(job)}

    async def _action_simple(self, what: str, args: dict[str, Any]) -> dict[str, Any]:
        job_id = _require_job_id(args)
        if what == "pause":
            await self._service.pause_job(job_id)
        elif what == "resume":
            await self._service.resume_job(job_id)
        elif what == "remove":
            await self._service.remove_job(job_id)
        return {"action": what, "job_id": job_id}

    async def _action_run_now(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = _require_job_id(args)
        run = await self._service.run_job_now(job_id)
        return {
            "action": "run_now",
            "job_id": job_id,
            "run_id": run.run_id,
            "status": run.status,
            "preview": (run.output_text or "")[:300],
        }

    async def _action_runs(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = _require_job_id(args)
        limit = int(args.get("limit", 20))
        runs = await self._service.get_runs(job_id, limit=limit)
        return {
            "action": "runs",
            "job_id": job_id,
            "count": len(runs),
            "runs": [_run_summary(r) for r in runs],
            "hint": (
                "Use action='get_run' with job_id + run_id for full "
                "output_text and delivery details."
            ),
        }

    async def _action_get_run(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return the FULL run record for one run — full output_text,
        all delivery reports, tokens, cost. Use this when the user
        wants to see what a scheduled job actually produced."""
        from bridge.scheduler.persistence import load_run, outputs_dir

        job_id = _require_job_id(args)
        run_id = (args.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("'run_id' is required")
        run = load_run(job_id, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found for job {job_id}")
        output_dir = outputs_dir(job_id, run_id)
        return {
            "action": "get_run",
            "run": _run_full(run),
            "output_dir": str(output_dir),
        }

    async def _action_metrics(self) -> dict[str, Any]:
        m = await self._service.metrics()
        return {
            "action": "metrics",
            "metrics": {
                "total_jobs": m.total_jobs,
                "enabled_jobs": m.enabled_jobs,
                "paused_jobs": m.paused_jobs,
                "disabled_jobs": m.disabled_jobs,
                "runs_24h": m.runs_24h,
                "succeeded_24h": m.succeeded_24h,
                "failed_24h": m.failed_24h,
                "avg_run_duration_seconds": m.avg_run_duration_seconds,
                "total_cost_usd_24h": m.total_cost_usd_24h,
                "next_fire_at": m.next_fire_at,
                "next_fire_job_id": m.next_fire_job_id,
                "next_fire_job_name": m.next_fire_job_name,
            },
        }

    # ─── Helpers ─────────────────────────────────────────────────────

    def _creator_ref(self):
        src = None
        if self._gw_source_getter is not None:
            try:
                src = self._gw_source_getter()
            except Exception:  # noqa: BLE001
                src = None
        if src is not None:
            # Gateway source has .platform.value, .workspace_id, .chat_id, etc.
            plat = getattr(src, "platform", None)
            plat_name = getattr(plat, "value", None) or str(plat or "desktop")
            return build_creator_ref(
                surface=plat_name,
                session_id=self._session_id,
                user_id=getattr(src, "user_id", None) or self._user_id,
                workspace_id=getattr(src, "workspace_id", None),
                chat_id=getattr(src, "chat_id", None),
                thread_id=getattr(src, "thread_id", None),
                user_name=getattr(src, "user_name", None),
            )
        return build_creator_ref(
            surface="desktop",
            session_id=self._session_id,
            user_id=self._user_id,
        )


# ─── Output formatting ─────────────────────────────────────────────────


def _job_summary(j: JobRecord) -> dict[str, Any]:
    return {
        "id": j.id,
        "name": j.name,
        "description": j.description,
        "enabled": j.enabled,
        "status": j.status,
        "schedule": j.schedule.to_dict(),
        "cadence": cadence_label(j.schedule),
        "next_fire_at": j.next_fire_at,
        "last_fire_at": j.last_fire_at,
        "fire_count": j.fire_count,
        "max_fires": j.max_fires,
        "prompt_preview": j.prompt[:200],
        "execution": j.execution.to_dict(),
        "sinks": [s.to_dict() for s in j.sinks],
        "tags": list(j.tags),
        "creator_surface": j.creator.surface,
    }


def _run_summary(r) -> dict[str, Any]:
    return {
        "run_id": r.run_id,
        "job_id": r.job_id,
        "job_name": r.job_name,
        "status": r.status,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "duration_seconds": r.duration_seconds,
        "fire_number": r.fire_number,
        "iteration": r.iteration,
        "preview": (r.output_text or "")[:500],
        "output_size_chars": len(r.output_text or ""),
        "delivery_reports": [dr.to_dict() for dr in r.delivery_reports],
        "error": r.error,
    }


def _run_full(r) -> dict[str, Any]:
    """Full run payload — used by get_run. Includes the entire
    output_text so the agent can echo / quote / summarize it."""
    return {
        "run_id": r.run_id,
        "job_id": r.job_id,
        "job_name": r.job_name,
        "status": r.status,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "duration_seconds": r.duration_seconds,
        "fire_number": r.fire_number,
        "iteration": r.iteration,
        "iterations": r.iterations,
        "execution_session_id": r.execution_session_id,
        "output_text": r.output_text,
        "output_attachments": r.output_attachments,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cache_read_tokens": r.cache_read_tokens,
        "cost_usd": r.cost_usd,
        "delivery_reports": [dr.to_dict() for dr in r.delivery_reports],
        "error": r.error,
        "prompt": r.prompt,
    }


def _format_payload(p: dict[str, Any]) -> str:
    import json
    return json.dumps(p, indent=2, default=str)


def _err(call_id: str, msg: str) -> ToolResult:
    return ToolResult(call_id=call_id, content=f"Error: {msg}", is_error=True)


def _require_job_id(args: dict[str, Any]) -> str:
    job_id = (args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("'job_id' is required")
    return job_id


def _autoname(prompt: str) -> str:
    # First 8 words of the prompt, capped at 60 chars.
    words = prompt.split()[:8]
    name = " ".join(words)
    return (name[:60] + "…") if len(name) > 60 else name or "scheduled job"


# ─── Tool description + JSON schema ────────────────────────────────────


_DESCRIPTION = """Schedule agent work to run at a future time or on a recurring cadence.

Actions:
  · create   — register a new scheduled job. Returns the created
               JobRecord with a normalized schedule + next_fire_at.
  · list     — show all scheduled jobs (newest-first by next fire).
               Optional filters: tag, status, enabled.
  · get      — fetch a single job by id.
  · update   — modify fields on an existing job (schedule, prompt,
               sinks, etc.). Returns the updated record.
  · pause    — disable firing without deleting.
  · resume   — re-enable a paused job.
  · remove   — delete a job. Run history under the job id is
               retained for the dashboard.
  · run_now  — fire a job once immediately, regardless of schedule.
               Does NOT reset the recurring cadence.
  · runs     — list recent runs of a single job (preview only).
  · get_run  — full run record for one run_id: complete output_text,
               delivery reports, tokens, cost. Use this to retrieve
               or echo what a scheduled job actually produced.
  · metrics  — system-wide scheduler health.

Specifying when:
  Pass either:
    · `when` — natural language: "every weekday at 9am", "in 30 minutes",
               "tomorrow at 5pm", "every 5 minutes", "every monday at noon",
               "self-paced between 60s and 30m".
    · `schedule` — typed dict:
        {"kind":"interval","seconds":300}
        {"kind":"cron","expression":"0 9 * * 1-5","timezone":"America/Los_Angeles"}
        {"kind":"once","at_iso":"2026-06-15T14:00:00-07:00"}
        {"kind":"self_paced","min_delay_seconds":60,"max_delay_seconds":1800,
         "until_condition":"the deploy is green"}

Execution context (where the agent runs):
  · "new_session"             — fresh ephemeral session per fire (default)
  · "persistent_job_session"  — agent has memory across fires (e.g. a
                                 morning briefing that remembers
                                 yesterday's takeaways)
  · "existing_session" / "here" — fire into the current session
  · or a typed dict: {"kind":"persistent_job_session"}

Sinks (where the output goes):
  Omit to deliver wherever the job was created — the right default
  for most jobs. To override, pass an array of items. Each item is
  one of these strings:

    "here", "slack", "desktop", "session", "noop",
    "laptop:<path>"  (tokens: {date} {time} {run_id} {job_name}),
    "https://<url>"

  Or a typed dict — use only when a shortcut won't do (e.g. a
  specific Slack channel that isn't the creator's):
    {"kind":"slack",      "workspace_id":"T...", "chat_id":"C...",
                          "thread_ts": null}
    {"kind":"filesystem", "path_template":"/tmp/{date}.md",
                          "format":"markdown"|"json"|"text",
                          "append":false}
    {"kind":"webhook",    "url":"https://...", "method":"POST"}

  Must be a real JSON array — not a JSON-encoded string.

Self-paced loops:
  When the schedule is `self_paced`, two extra tools are auto-registered
  during each fire: `scheduler.continue_loop(delay_seconds, reason)` and
  `scheduler.complete_loop(reason)`. The agent must call exactly one
  before finishing the turn. If neither is called, the runtime defaults
  to the max delay.
"""


_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "create", "list", "get", "update",
                "pause", "resume", "remove",
                "run_now", "run", "runs", "get_run", "run_detail",
                "metrics",
            ],
            "description": "Which scheduler operation to perform.",
        },
        "job_id": {
            "type": "string",
            "description": "Job id. Required for get/update/pause/resume/remove/run_now/runs/get_run.",
        },
        "run_id": {
            "type": "string",
            "description": "Specific run id. Required for get_run.",
        },
        "name": {
            "type": "string",
            "description": "Human-readable label. Auto-derived from the prompt if omitted.",
        },
        "description": {"type": "string"},
        "prompt": {
            "type": "string",
            "description": (
                "The prompt the agent runs at fire time. Should be "
                "self-contained: scheduled runs have no human in the "
                "loop, so the agent must be able to act without "
                "clarifying questions."
            ),
        },
        "when": {
            "type": "string",
            "description": "Natural-language schedule (see description).",
        },
        "schedule": {
            "type": "object",
            "description": "Typed schedule spec. Either `when` or `schedule` is required for create.",
        },
        "timezone": {
            "type": "string",
            "description": "IANA timezone for schedule resolution. Defaults to UTC.",
        },
        "execution": {
            "description": (
                "Where the agent runs. String keyword "
                "(new/persistent/here) or typed object."
            ),
        },
        "sinks": {
            "type": "array",
            "items": {},
            "description": (
                "Delivery destinations. Omit to deliver wherever the "
                "job was created. Each item is a shortcut string "
                "(\"here\", \"slack\", \"desktop\", \"session\", \"noop\", "
                "\"laptop:<path>\", \"https://<url>\") or a typed sink "
                "dict. Must be a real array — not a JSON-encoded string."
            ),
        },
        "model_id": {"type": "string"},
        "coordination_strategy": {"type": "string"},
        "skills_to_load": {
            "type": "array",
            "items": {"type": "string"},
        },
        "budget": {
            "type": "object",
            "description": (
                "Optional caps: max_tokens_per_run, max_cost_usd_per_run, "
                "max_cost_usd_per_day, wall_clock_timeout_seconds, "
                "on_exceeded ('abort_run' | 'abort_job')."
            ),
        },
        "max_fires": {"type": "integer"},
        "misfire_policy": {
            "type": "string",
            "enum": ["skip", "fire_once", "fire_all"],
        },
        "overlap_policy": {
            "type": "string",
            "enum": ["queue", "skip_if_running", "cancel_running"],
        },
        "timeout_seconds": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "limit": {"type": "integer"},
        "user_id": {"type": "string"},
        "surface": {"type": "string"},
        "status": {"type": "string"},
        "tag": {"type": "string"},
        "enabled": {"type": "boolean"},
        "allow_creates_schedules": {"type": "boolean"},
    },
    "required": ["action"],
}


# ─── Continue/complete loop tools (auto-registered for self-paced fires) ──


class _LoopControlTool:
    """Base for continue_loop / complete_loop. Both share the same
    permission model + reach into SchedulerService."""

    requires_permission = False

    def __init__(
        self,
        *,
        service: SchedulerService,
        job_id: str,
        mode: str,
    ) -> None:
        self._service = service
        self._job_id = job_id
        self._mode = mode  # "continue" | "complete"

    @property
    def definition(self) -> ToolDefinition:
        if self._mode == "continue":
            name = "scheduler.continue_loop"
            summary = (
                "Schedule the next iteration of the current self-paced "
                "loop. Required before ending this turn."
            )
            description = (
                "Set the next wakeup of the current self-paced loop "
                "to `delay_seconds` from now (clamped to the job's "
                "configured min/max). MUST be called before ending "
                "the turn unless you call scheduler.complete_loop."
            )
            params = {
                "type": "object",
                "properties": {
                    "delay_seconds": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["delay_seconds"],
            }
        else:
            name = "scheduler.complete_loop"
            summary = "Terminate the current self-paced loop."
            description = (
                "Mark the current self-paced loop as completed. The "
                "job becomes disabled — no further fires. Provide a "
                "short reason for the dashboard."
            )
            params = {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": [],
            }
        return ToolDefinition(
            name=name,
            summary=summary,
            description=description,
            parameters=params,
            tier=ToolTier.HOT,
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if self._mode == "continue":
                delay = int(arguments.get("delay_seconds") or 0)
                reason = str(arguments.get("reason") or "")
                payload = await self._service.advance_self_paced(
                    self._job_id, delay_seconds=delay, reason=reason,
                )
            else:
                reason = str(arguments.get("reason") or "")
                payload = await self._service.complete_self_paced(
                    self._job_id, reason=reason,
                )
            import json
            return ToolResult(
                call_id=call_id,
                content=json.dumps(payload, default=str),
                is_error=False,
            )
        except Exception as exc:  # noqa: BLE001
            return _err(call_id, str(exc))


def build_continue_loop_tool(*, service, job_id: str):
    return _LoopControlTool(service=service, job_id=job_id, mode="continue")


def build_complete_loop_tool(*, service, job_id: str):
    return _LoopControlTool(service=service, job_id=job_id, mode="complete")
