"""Data models for the scheduler service.

JobRecord is the durable record on disk. RunRecord is one execution of
a job. The schedule/execution/sink specs are discriminated unions so
adding a new kind never requires touching unrelated code.

All times stored as POSIX float seconds (timezone metadata lives
separately on schedule specs that care about wall-clock time).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# ─── Identity / Creator ────────────────────────────────────────────────


@dataclass
class CreatorRef:
    """Who/where this job was created from. Used for dashboard scoping
    and to resolve 'sink: here' shortcuts at create time. Never used
    for permissions — those are snapshotted separately."""

    surface: Literal["desktop", "slack", "api"]
    session_id: str
    user_id: str | None = None
    workspace_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    user_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CreatorRef:
        return cls(
            surface=d.get("surface", "desktop"),
            session_id=d.get("session_id", ""),
            user_id=d.get("user_id"),
            workspace_id=d.get("workspace_id"),
            chat_id=d.get("chat_id"),
            thread_id=d.get("thread_id"),
            user_name=d.get("user_name"),
        )


# ─── Schedule kinds ────────────────────────────────────────────────────


@dataclass
class OnceSchedule:
    kind: Literal["once"] = "once"
    at_iso: str = ""             # RFC3339, in `timezone`
    timezone: str = "UTC"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "once", "at_iso": self.at_iso, "timezone": self.timezone}


@dataclass
class IntervalSchedule:
    kind: Literal["interval"] = "interval"
    seconds: int = 0
    after_iso: str | None = None  # don't start before this
    timezone: str = "UTC"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "interval",
            "seconds": self.seconds,
            "after_iso": self.after_iso,
            "timezone": self.timezone,
        }


@dataclass
class CronSchedule:
    kind: Literal["cron"] = "cron"
    expression: str = ""          # 5-field standard cron
    timezone: str = "UTC"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "cron", "expression": self.expression, "timezone": self.timezone}


@dataclass
class SelfPacedSchedule:
    """Agent decides its own next wakeup via continue_loop/complete_loop
    tools that are auto-registered on each fire."""

    kind: Literal["self_paced"] = "self_paced"
    min_delay_seconds: int = 60
    max_delay_seconds: int = 3600
    until_condition: str | None = None  # NL condition; runtime polls model

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "self_paced",
            "min_delay_seconds": self.min_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "until_condition": self.until_condition,
        }


ScheduleSpec = OnceSchedule | IntervalSchedule | CronSchedule | SelfPacedSchedule


def schedule_from_dict(d: dict[str, Any]) -> ScheduleSpec:
    kind = d.get("kind", "")
    if kind == "once":
        return OnceSchedule(at_iso=d.get("at_iso", ""), timezone=d.get("timezone", "UTC"))
    if kind == "interval":
        return IntervalSchedule(
            seconds=int(d.get("seconds", 0)),
            after_iso=d.get("after_iso"),
            timezone=d.get("timezone", "UTC"),
        )
    if kind == "cron":
        return CronSchedule(
            expression=d.get("expression", ""),
            timezone=d.get("timezone", "UTC"),
        )
    if kind == "self_paced":
        return SelfPacedSchedule(
            min_delay_seconds=int(d.get("min_delay_seconds", 60)),
            max_delay_seconds=int(d.get("max_delay_seconds", 3600)),
            until_condition=d.get("until_condition"),
        )
    raise ValueError(f"unknown schedule kind: {kind!r}")


# ─── Execution context ─────────────────────────────────────────────────


@dataclass
class NewSession:
    """Each fire creates a fresh ephemeral session. No state across
    fires. Cheapest, most isolated. Default for one-shot reminders."""

    kind: Literal["new_session"] = "new_session"
    coordination_strategy: str = "bus"
    model_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PersistentJobSession:
    """First fire allocates a job-owned session id; subsequent fires
    queue into the same session. Agent's transcript grows over time —
    the recurring 'morning briefing' that remembers yesterday's takeaways
    lives here. Session id is auto-set on first fire and persisted on
    the JobRecord."""

    kind: Literal["persistent_job_session"] = "persistent_job_session"
    session_id: str | None = None       # auto-allocated on first fire
    coordination_strategy: str = "bus"
    model_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExistingSession:
    """Fire into a session that already exists. Useful for tying a
    scheduled job to a user-visible interactive session. If the target
    session disappears, the job goes to 'disabled_target_lost'."""

    kind: Literal["existing_session"] = "existing_session"
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ExecutionSpec = NewSession | PersistentJobSession | ExistingSession


def execution_from_dict(d: dict[str, Any]) -> ExecutionSpec:
    kind = d.get("kind", "new_session")
    if kind == "new_session":
        return NewSession(
            coordination_strategy=d.get("coordination_strategy", "bus"),
            model_id=d.get("model_id"),
        )
    if kind == "persistent_job_session":
        return PersistentJobSession(
            session_id=d.get("session_id"),
            coordination_strategy=d.get("coordination_strategy", "bus"),
            model_id=d.get("model_id"),
        )
    if kind == "existing_session":
        return ExistingSession(session_id=d.get("session_id", ""))
    raise ValueError(f"unknown execution kind: {kind!r}")


# ─── Sinks ─────────────────────────────────────────────────────────────


@dataclass
class SlackSinkSpec:
    kind: Literal["slack"] = "slack"
    workspace_id: str = ""
    chat_id: str = ""              # channel / DM
    thread_ts: str | None = None
    include_banner: bool = True
    include_metadata: bool = True  # adds "(scheduled: <name>)" suffix

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DesktopSinkSpec:
    kind: Literal["desktop"] = "desktop"
    session_id: str = ""
    render_mode: Literal["system_event", "assistant_message"] = "system_event"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FilesystemSinkSpec:
    """``path_template`` supports tokens: ``{job_name}``, ``{job_id}``,
    ``{run_id}``, ``{date}`` (YYYY-MM-DD), ``{time}`` (HHMMSS),
    ``{iso}`` (ISO8601). Tilde expansion and env-var expansion happen
    at write time. If ``append`` is true the new content is appended
    to an existing file (creating it if missing) — useful for log-style
    outputs."""

    kind: Literal["filesystem"] = "filesystem"
    path_template: str = ""
    format: Literal["markdown", "json", "text"] = "markdown"
    append: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionSinkSpec:
    """Output stays in the execution session's transcript — no
    external delivery. Useful for self-paced loops that build context
    over many iterations, or for PersistentJobSession agents whose
    transcript IS the deliverable (inspectable via dashboard)."""

    kind: Literal["session"] = "session"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WebhookSinkSpec:
    kind: Literal["webhook"] = "webhook"
    url: str = ""
    method: Literal["POST", "PUT", "PATCH"] = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body_template: str = "{output}"  # supports {output}, {run_id}, {job_id}, etc.
    content_type: str = "text/markdown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NoopSinkSpec:
    """Run records are still saved; nothing is published externally."""

    kind: Literal["noop"] = "noop"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SinkSpec = (
    SlackSinkSpec
    | DesktopSinkSpec
    | FilesystemSinkSpec
    | SessionSinkSpec
    | WebhookSinkSpec
    | NoopSinkSpec
)


def sink_from_dict(d: dict[str, Any]) -> SinkSpec:
    kind = d.get("kind", "")
    if kind == "slack":
        return SlackSinkSpec(
            workspace_id=d.get("workspace_id", ""),
            chat_id=d.get("chat_id", ""),
            thread_ts=d.get("thread_ts"),
            include_banner=bool(d.get("include_banner", True)),
            include_metadata=bool(d.get("include_metadata", True)),
        )
    if kind == "desktop":
        return DesktopSinkSpec(
            session_id=d.get("session_id", ""),
            render_mode=d.get("render_mode", "system_event"),
        )
    if kind == "filesystem":
        return FilesystemSinkSpec(
            path_template=d.get("path_template", ""),
            format=d.get("format", "markdown"),
            append=bool(d.get("append", False)),
        )
    if kind == "session":
        return SessionSinkSpec()
    if kind == "webhook":
        return WebhookSinkSpec(
            url=d.get("url", ""),
            method=d.get("method", "POST"),
            headers=dict(d.get("headers") or {}),
            body_template=d.get("body_template", "{output}"),
            content_type=d.get("content_type", "text/markdown"),
        )
    if kind == "noop":
        return NoopSinkSpec()
    raise ValueError(f"unknown sink kind: {kind!r}")


# ─── Budgets, retries ──────────────────────────────────────────────────


@dataclass
class BudgetSpec:
    """All None = unlimited (user explicitly chose 'no defaults'). The
    runtime checks before/after each LLM call inside a run."""

    max_tokens_per_run: int | None = None
    max_cost_usd_per_run: float | None = None
    max_cost_usd_per_day: float | None = None
    wall_clock_timeout_seconds: int | None = None
    on_exceeded: Literal["abort_run", "abort_job"] = "abort_run"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BudgetSpec:
        return cls(
            max_tokens_per_run=d.get("max_tokens_per_run"),
            max_cost_usd_per_run=d.get("max_cost_usd_per_run"),
            max_cost_usd_per_day=d.get("max_cost_usd_per_day"),
            wall_clock_timeout_seconds=d.get("wall_clock_timeout_seconds"),
            on_exceeded=d.get("on_exceeded", "abort_run"),
        )


@dataclass
class RetryPolicy:
    max_retries: int = 0
    backoff_seconds: int = 60
    backoff_multiplier: float = 2.0
    on: list[str] = field(default_factory=list)  # ["agent_error", "sink_error", "timeout"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RetryPolicy:
        return cls(
            max_retries=int(d.get("max_retries", 0)),
            backoff_seconds=int(d.get("backoff_seconds", 60)),
            backoff_multiplier=float(d.get("backoff_multiplier", 2.0)),
            on=list(d.get("on") or []),
        )


# ─── JobRecord ─────────────────────────────────────────────────────────


JobStatus = Literal[
    "active",
    "paused",
    "disabled_max_fires",
    "disabled_budget_exhausted",
    "disabled_target_lost",
    "disabled_error",
]


@dataclass
class JobRecord:
    """The full durable record. One file per job under
    ``~/.freyja/schedules/jobs/{job_id}.json``."""

    # Identity
    id: str
    name: str
    description: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    creator: CreatorRef = field(default_factory=lambda: CreatorRef(surface="api", session_id=""))

    # Schedule + state
    schedule: ScheduleSpec = field(default_factory=lambda: IntervalSchedule(seconds=3600))
    enabled: bool = True
    status: JobStatus = "active"
    max_fires: int | None = None
    fire_count: int = 0
    next_fire_at: float | None = None
    last_fire_at: float | None = None

    # Execution
    prompt: str = ""
    execution: ExecutionSpec = field(default_factory=NewSession)
    permission_snapshot: str = "low"   # tier string ("yolo" | "low" | "medium" | "high")
    model_id: str | None = None
    coordination_strategy: str = "bus"
    skills_to_load: list[str] = field(default_factory=list)
    budget: BudgetSpec | None = None

    # Delivery
    sinks: list[SinkSpec] = field(default_factory=list)

    # Behavior
    misfire_policy: Literal["skip", "fire_once", "fire_all"] = "skip"
    overlap_policy: Literal["queue", "skip_if_running", "cancel_running"] = "queue"
    timeout_seconds: int | None = None
    retry_policy: RetryPolicy | None = None

    # Recursion guard — scheduled jobs cannot spawn more by default.
    allow_creates_schedules: bool = False

    # Organization
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "creator": self.creator.to_dict(),
            "schedule": self.schedule.to_dict(),
            "enabled": self.enabled,
            "status": self.status,
            "max_fires": self.max_fires,
            "fire_count": self.fire_count,
            "next_fire_at": self.next_fire_at,
            "last_fire_at": self.last_fire_at,
            "prompt": self.prompt,
            "execution": self.execution.to_dict(),
            "permission_snapshot": self.permission_snapshot,
            "model_id": self.model_id,
            "coordination_strategy": self.coordination_strategy,
            "skills_to_load": list(self.skills_to_load),
            "budget": self.budget.to_dict() if self.budget else None,
            "sinks": [s.to_dict() for s in self.sinks],
            "misfire_policy": self.misfire_policy,
            "overlap_policy": self.overlap_policy,
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": self.retry_policy.to_dict() if self.retry_policy else None,
            "allow_creates_schedules": self.allow_creates_schedules,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JobRecord:
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", time.time())),
            creator=CreatorRef.from_dict(d.get("creator") or {}),
            schedule=schedule_from_dict(d.get("schedule") or {}),
            enabled=bool(d.get("enabled", True)),
            status=d.get("status", "active"),
            max_fires=d.get("max_fires"),
            fire_count=int(d.get("fire_count", 0)),
            next_fire_at=d.get("next_fire_at"),
            last_fire_at=d.get("last_fire_at"),
            prompt=d.get("prompt", ""),
            execution=execution_from_dict(d.get("execution") or {}),
            permission_snapshot=d.get("permission_snapshot", "low"),
            model_id=d.get("model_id"),
            coordination_strategy=d.get("coordination_strategy", "bus"),
            skills_to_load=list(d.get("skills_to_load") or []),
            budget=BudgetSpec.from_dict(d["budget"]) if d.get("budget") else None,
            sinks=[sink_from_dict(s) for s in (d.get("sinks") or [])],
            misfire_policy=d.get("misfire_policy", "skip"),
            overlap_policy=d.get("overlap_policy", "queue"),
            timeout_seconds=d.get("timeout_seconds"),
            retry_policy=RetryPolicy.from_dict(d["retry_policy"]) if d.get("retry_policy") else None,
            allow_creates_schedules=bool(d.get("allow_creates_schedules", False)),
            tags=list(d.get("tags") or []),
        )


# ─── RunRecord ─────────────────────────────────────────────────────────


RunStatus = Literal[
    "claimed",        # pre-advance + persist done; agent hasn't started yet
    "running",        # agent turn in flight
    "delivering",     # agent done, sinks in flight
    "succeeded",      # agent succeeded + all sinks succeeded
    "partial_failure",# agent succeeded; ≥1 sink failed
    "failed",         # agent errored OR all sinks failed
    "cancelled",      # user-requested cancellation
    "timed_out",      # wall-clock or budget timeout
]


@dataclass
class DeliveryReport:
    sink_index: int                  # which sink in JobRecord.sinks
    sink_kind: str
    success: bool
    delivered_at: float
    artifact_ref: str | None = None  # slack ts, file path, http status, etc.
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeliveryReport:
        return cls(**{k: d.get(k) for k in (
            "sink_index", "sink_kind", "success", "delivered_at",
            "artifact_ref", "error",
        )})


@dataclass
class RunRecord:
    run_id: str
    job_id: str
    job_name: str
    started_at: float
    finished_at: float | None = None
    status: RunStatus = "claimed"
    fire_number: int = 0             # nth fire of the job
    iteration: int = 0               # nth loop iteration (self-paced)

    # Inputs (snapshot — job may be edited later)
    prompt: str = ""
    execution_session_id: str | None = None

    # Outputs
    output_text: str = ""
    output_attachments: list[dict[str, Any]] = field(default_factory=list)

    # Telemetry
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    iterations: int = 0              # runner iterations within this run

    # Outcomes
    error: str | None = None
    delivery_reports: list[DeliveryReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["delivery_reports"] = [r.to_dict() for r in self.delivery_reports]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunRecord:
        reports = [DeliveryReport.from_dict(r) for r in (d.get("delivery_reports") or [])]
        copy = dict(d)
        copy.pop("delivery_reports", None)
        return cls(**copy, delivery_reports=reports)


# ─── Query / mutation helpers ──────────────────────────────────────────


@dataclass
class JobFilter:
    """Used by ``list_jobs``. None on a field means 'no filter'."""

    user_id: str | None = None
    surface: str | None = None
    status: str | None = None
    tag: str | None = None
    enabled: bool | None = None


@dataclass
class JobPatch:
    """Updateable fields on a JobRecord. None = leave unchanged. Used
    by ``update_job``. Identity, lifecycle, and creator-set metadata
    are intentionally NOT patchable here."""

    name: str | None = None
    description: str | None = None
    schedule: ScheduleSpec | None = None
    enabled: bool | None = None
    max_fires: int | None = None
    prompt: str | None = None
    execution: ExecutionSpec | None = None
    permission_snapshot: str | None = None
    model_id: str | None = None
    coordination_strategy: str | None = None
    skills_to_load: list[str] | None = None
    budget: BudgetSpec | None = None
    sinks: list[SinkSpec] | None = None
    misfire_policy: str | None = None
    overlap_policy: str | None = None
    timeout_seconds: int | None = None
    retry_policy: RetryPolicy | None = None
    tags: list[str] | None = None


@dataclass
class SchedulerMetrics:
    """Rollup of scheduler health, for dashboard."""

    total_jobs: int
    enabled_jobs: int
    paused_jobs: int
    disabled_jobs: int

    runs_24h: int
    succeeded_24h: int
    failed_24h: int
    avg_run_duration_seconds: float
    total_cost_usd_24h: float

    next_fire_at: float | None
    next_fire_job_id: str | None
    next_fire_job_name: str | None


# ─── IDs ───────────────────────────────────────────────────────────────


def new_job_id() -> str:
    return f"sched_{uuid.uuid4().hex[:12]}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"
