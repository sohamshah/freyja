"""Slack delivery sink.

Posts the run's output into the target Slack channel/DM/thread. Uses
the existing Slack adapter's ``send`` path so retries, throttling, and
formatting (mrkdwn) all flow through the same code as interactive
turns. We post the prose body as a single message — the Slack stream
consumer fanciness (cards, deltas) is only useful when we have a live
LLM stream; scheduled outputs are already complete by delivery time,
so a single ``chat.postMessage`` is the right call.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bridge.scheduler.models import JobRecord, RunRecord, SlackSinkSpec
    from bridge.scheduler.sinks import RunOutput, SinkContext

logger = logging.getLogger("freyja.scheduler.sinks.slack")


async def deliver_slack(
    spec: "SlackSinkSpec",
    job: "JobRecord",
    run: "RunRecord",
    output: "RunOutput",
    ctx: "SinkContext",
) -> str | None:
    """Returns the Slack message ``ts`` on success, else raises."""
    adapter = _find_slack_adapter(ctx.state)
    if adapter is None:
        raise RuntimeError(
            f"no Slack adapter available for workspace {spec.workspace_id}"
        )

    # Optional pre-fire banner — useful for recurring agents posting
    # into busy channels. Suppressed when delivery is into a thread
    # where users already saw the scheduled-job context.
    if spec.include_banner:
        try:
            banner = _build_banner(job, run)
            await adapter.send(
                spec.chat_id,
                banner,
                thread_id=spec.thread_ts,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("scheduler banner send failed (non-fatal): %s", exc)

    # Build the body. Metadata footer if requested — one line that
    # makes the message's provenance unambiguous when it lands in a
    # channel separate from where the job was created.
    body = output.text or "_(scheduled run produced no output)_"
    if spec.include_metadata:
        footer = (
            f"\n\n— _scheduled job *{job.name}* "
            f"(`{job.id}`, run `{run.run_id}`). "
            f"Full record via `schedule(action='get_run', "
            f"job_id='{job.id}', run_id='{run.run_id}')`._"
        )
        body = body + footer

    result = await adapter.send(
        spec.chat_id,
        body,
        thread_id=spec.thread_ts,
    )
    if not getattr(result, "ok", False):
        raise RuntimeError(
            f"slack send returned not-ok: {getattr(result, 'error', '?')}"
        )

    ts = getattr(result, "message_id", None)
    return str(ts) if ts else None


def _find_slack_adapter(state: Any) -> Any | None:
    """Locate the Slack adapter through the GatewayDaemon that built
    this _BridgeState. The gateway process and the scheduler daemon
    both run one (the daemon's adapter is send-only); the desktop
    bridge has none. One adapter serves every authenticated workspace."""
    gw = getattr(state, "gateway_runner", None)
    for a in getattr(gw, "adapters", None) or []:
        if getattr(a, "name", None) == "slack":
            return a
    return None


def _build_banner(job: "JobRecord", run: "RunRecord") -> str:
    iter_suffix = f" · iteration {run.iteration}" if run.iteration else ""
    return (
        f"_:hourglass_flowing_sand: Scheduled job *{job.name}* running"
        f"{iter_suffix} — results below._"
    )
