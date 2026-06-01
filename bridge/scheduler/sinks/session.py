"""In-session sink — no-op delivery; output already lives in the
execution session's transcript.

Returning the execution session id as the artifact_ref makes the
delivery report informative (the dashboard can deep-link into that
session). Useful for self-paced loops and PersistentJobSession agents
that build up context across many fires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridge.scheduler.models import JobRecord, RunRecord, SessionSinkSpec
    from bridge.scheduler.sinks import RunOutput, SinkContext


async def deliver_session(
    spec: "SessionSinkSpec",
    job: "JobRecord",
    run: "RunRecord",
    output: "RunOutput",
    ctx: "SinkContext",
) -> str | None:
    return run.execution_session_id
