"""Freyja scheduler — durable scheduled jobs that fire agent turns.

The scheduler is a long-running asyncio service owned by ``_BridgeState``.
It persists jobs under ``~/.freyja/schedules/`` (one file per job for
write isolation), fires due jobs through ``_schedule_or_queue_turn``
into a resolvable execution context (new session / persistent job
session / existing session), and routes the resulting output through a
list of pluggable delivery sinks (Slack, desktop, filesystem, webhook,
in-session, noop).

The three axes are independent:

  · Trigger surface — desktop UI, desktop tool, Slack slash, Slack tool.
  · Execution context — where the agent actually runs.
  · Delivery sinks — where the output is published.

Any trigger can target any execution with any delivery. The whole
service has one API; every surface is just a thin translator.

Public re-exports below are what callers outside ``bridge/scheduler/``
should import from.
"""

from __future__ import annotations

from bridge.scheduler.models import (
    BudgetSpec,
    CreatorRef,
    CronSchedule,
    DesktopSinkSpec,
    ExistingSession,
    FilesystemSinkSpec,
    IntervalSchedule,
    JobFilter,
    JobPatch,
    JobRecord,
    NewSession,
    NoopSinkSpec,
    OnceSchedule,
    PersistentJobSession,
    RetryPolicy,
    RunRecord,
    ScheduleSpec,
    SchedulerMetrics,
    SelfPacedSchedule,
    SessionSinkSpec,
    SinkSpec,
    SlackSinkSpec,
    WebhookSinkSpec,
)
from bridge.scheduler.service import SchedulerService

__all__ = [
    "BudgetSpec",
    "CreatorRef",
    "CronSchedule",
    "DesktopSinkSpec",
    "ExistingSession",
    "FilesystemSinkSpec",
    "IntervalSchedule",
    "JobFilter",
    "JobPatch",
    "JobRecord",
    "NewSession",
    "NoopSinkSpec",
    "OnceSchedule",
    "PersistentJobSession",
    "RetryPolicy",
    "RunRecord",
    "ScheduleSpec",
    "SchedulerMetrics",
    "SchedulerService",
    "SelfPacedSchedule",
    "SessionSinkSpec",
    "SinkSpec",
    "SlackSinkSpec",
    "WebhookSinkSpec",
]
