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
    adapter = _find_slack_adapter(ctx.state, spec.workspace_id)
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
            f"\n\n_— scheduled job: *{job.name}* "
            f"(`{job.id}`, run `{run.run_id}`)_"
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


def _find_slack_adapter(state: Any, workspace_id: str) -> Any | None:
    """Locate the Slack adapter on _BridgeState. The bridge owns one
    adapter per workspace; we walk the adapter list to find the match.
    Falls back to the first Slack adapter when workspace_id is empty
    (single-workspace install — the common case)."""
    adapters = (
        getattr(state, "platform_adapters", None)
        or getattr(state, "_platform_adapters", None)
        or []
    )
    for a in adapters:
        try:
            if getattr(a, "name", None) == "slack":
                ws = getattr(a, "workspace_id", None) or getattr(
                    a, "_workspace_id", None
                )
                if not workspace_id or ws == workspace_id:
                    return a
        except Exception:  # noqa: BLE001
            continue
    # Fall through — try the gateway runner reference if the bridge
    # state has one (the gateway exposes adapters under different
    # attrs depending on boot order).
    gw = getattr(state, "gateway_runner", None)
    if gw is not None:
        for a in getattr(gw, "_adapters", []) or []:
            try:
                if getattr(a, "name", None) == "slack":
                    return a
            except Exception:  # noqa: BLE001
                continue
    return None


def _build_banner(job: "JobRecord", run: "RunRecord") -> str:
    iter_suffix = f" · iteration {run.iteration}" if run.iteration else ""
    return (
        f"_:hourglass_flowing_sand: Scheduled job *{job.name}* running"
        f"{iter_suffix} — results below._"
    )
