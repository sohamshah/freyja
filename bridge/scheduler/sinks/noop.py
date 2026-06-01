"""Noop sink — no external delivery. Run record + outputs/ still
written by the runtime, so the dashboard surfaces it."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridge.scheduler.models import JobRecord, NoopSinkSpec, RunRecord
    from bridge.scheduler.sinks import RunOutput, SinkContext


async def deliver_noop(
    spec: "NoopSinkSpec",
    job: "JobRecord",
    run: "RunRecord",
    output: "RunOutput",
    ctx: "SinkContext",
) -> str | None:
    return "noop"
