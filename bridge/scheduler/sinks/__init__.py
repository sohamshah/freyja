"""Sink registry. Each sink is a small async callable that accepts a
``RunOutput`` and a typed config. The scheduler's runtime fans out
across all sinks in parallel and collects per-sink reports."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from bridge.scheduler.models import (
    DeliveryReport,
    DesktopSinkSpec,
    FilesystemSinkSpec,
    JobRecord,
    NoopSinkSpec,
    RunRecord,
    SessionSinkSpec,
    SinkSpec,
    SlackSinkSpec,
    WebhookSinkSpec,
)
from bridge.scheduler.sinks.desktop import deliver_desktop
from bridge.scheduler.sinks.filesystem import deliver_filesystem
from bridge.scheduler.sinks.noop import deliver_noop
from bridge.scheduler.sinks.session import deliver_session
from bridge.scheduler.sinks.slack import deliver_slack
from bridge.scheduler.sinks.webhook import deliver_webhook

logger = logging.getLogger("freyja.scheduler.sinks")


@dataclass
class RunOutput:
    """Normalized agent output handed to every sink."""

    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    run: RunRecord | None = None
    job: JobRecord | None = None


@dataclass
class SinkContext:
    """Runtime services a sink can use. Held by the scheduler service
    and passed through on every delivery."""

    state: Any                 # _BridgeState (avoid circular import)
    outputs_dir: Any           # Callable[[str, str], Path]
    log: Any                   # callable(level, message)


async def deliver_all(
    job: JobRecord,
    run: RunRecord,
    output: RunOutput,
    ctx: SinkContext,
) -> list[DeliveryReport]:
    """Dispatch the run output to every configured sink concurrently.
    Each sink reports independently; one sink's failure does not block
    the others. Returns the per-sink reports."""

    async def _one(idx: int, spec: SinkSpec) -> DeliveryReport:
        kind = spec.kind
        started = time.time()
        try:
            if isinstance(spec, SlackSinkSpec):
                ref = await deliver_slack(spec, job, run, output, ctx)
            elif isinstance(spec, DesktopSinkSpec):
                ref = await deliver_desktop(spec, job, run, output, ctx)
            elif isinstance(spec, FilesystemSinkSpec):
                ref = await deliver_filesystem(spec, job, run, output, ctx)
            elif isinstance(spec, SessionSinkSpec):
                ref = await deliver_session(spec, job, run, output, ctx)
            elif isinstance(spec, WebhookSinkSpec):
                ref = await deliver_webhook(spec, job, run, output, ctx)
            elif isinstance(spec, NoopSinkSpec):
                ref = await deliver_noop(spec, job, run, output, ctx)
            else:
                raise ValueError(f"unknown sink kind: {kind!r}")
            return DeliveryReport(
                sink_index=idx,
                sink_kind=kind,
                success=True,
                delivered_at=started,
                artifact_ref=ref,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("sink %s/%s delivery failed", idx, kind)
            return DeliveryReport(
                sink_index=idx,
                sink_kind=kind,
                success=False,
                delivered_at=started,
                error=str(exc),
            )

    if not job.sinks:
        return []
    return await asyncio.gather(*[_one(i, s) for i, s in enumerate(job.sinks)])
