"""HTTP webhook sink. POST/PUT/PATCH the agent's output to a URL.

Body template supports tokens (``{output}``, ``{run_id}``, ``{job_id}``,
``{job_name}``, ``{fire_number}``, ``{iso}``). If the template is the
default ``{output}``, we send the raw text body (no JSON wrapper) so
existing webhook receivers don't need to know about Freyja's
internals."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridge.scheduler.models import JobRecord, RunRecord, WebhookSinkSpec
    from bridge.scheduler.sinks import RunOutput, SinkContext

logger = logging.getLogger("freyja.scheduler.sinks.webhook")


async def deliver_webhook(
    spec: "WebhookSinkSpec",
    job: "JobRecord",
    run: "RunRecord",
    output: "RunOutput",
    ctx: "SinkContext",
) -> str | None:
    if not spec.url:
        raise ValueError("webhook sink requires url")
    body = _interpolate(
        spec.body_template or "{output}",
        output_text=output.text,
        job=job,
        run=run,
    )
    headers = dict(spec.headers or {})
    headers.setdefault("Content-Type", spec.content_type or "text/markdown")
    headers.setdefault("X-Freyja-Job-Id", job.id)
    headers.setdefault("X-Freyja-Run-Id", run.run_id)
    headers.setdefault("X-Freyja-Fire-Number", str(run.fire_number))

    # httpx is the standard HTTP client in this codebase; fall back to
    # urllib in the unlikely case it's missing so the sink never
    # silently swallows config errors.
    try:
        import httpx  # type: ignore[import-not-found]

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                spec.method,
                spec.url,
                content=body if isinstance(body, str) else str(body),
                headers=headers,
            )
        if 200 <= resp.status_code < 300:
            return f"{spec.method} {spec.url} → {resp.status_code}"
        raise RuntimeError(f"{resp.status_code} {resp.reason_phrase}: {resp.text[:300]}")
    except ImportError:
        import urllib.request

        req = urllib.request.Request(
            spec.url,
            data=body.encode("utf-8"),
            headers=headers,
            method=spec.method,
        )
        resp = urllib.request.urlopen(req, timeout=30.0)  # noqa: S310
        return f"{spec.method} {spec.url} → {resp.status}"


def _interpolate(
    template: str,
    *,
    output_text: str,
    job: "JobRecord",
    run: "RunRecord",
) -> str:
    iso = datetime.fromtimestamp(run.started_at).isoformat(timespec="seconds")
    return (
        template
        .replace("{output}", output_text)
        .replace("{job_id}", job.id)
        .replace("{job_name}", job.name)
        .replace("{run_id}", run.run_id)
        .replace("{fire_number}", str(run.fire_number))
        .replace("{iso}", iso)
    )
