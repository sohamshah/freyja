"""Desktop delivery sink.

Injects the run output into a target desktop session as either:

  · ``system_event``  — a banner card in the session's activity stream
    (recommended default; clearly distinguishes scheduled output from
    in-session messages)
  · ``assistant_message`` — synthesized assistant turn appended to the
    transcript (looks like the agent posted in the session). Useful when
    you want the scheduled output to appear inline with the live
    conversation.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bridge.scheduler.models import DesktopSinkSpec, JobRecord, RunRecord
    from bridge.scheduler.sinks import RunOutput, SinkContext


async def deliver_desktop(
    spec: "DesktopSinkSpec",
    job: "JobRecord",
    run: "RunRecord",
    output: "RunOutput",
    ctx: "SinkContext",
) -> str | None:
    """Returns the target session id on success, else raises."""
    target_id = spec.session_id
    if not target_id:
        raise ValueError("desktop sink requires session_id")

    # Look up the session. If it's not in memory, we still emit the
    # event — the renderer will buffer it for that session id and
    # surface it when the session is opened.
    sessions = getattr(ctx.state, "sessions", {}) or {}
    sess = sessions.get(target_id)

    if spec.render_mode == "assistant_message" and sess is not None:
        # Append to the transcript as if Freyja posted in the session.
        # The renderer will pick this up via the normal save path.
        try:
            inner = getattr(sess, "session", None)
            if inner is not None:
                inner.add_assistant_message(_format_with_meta(output.text, job, run))
                if hasattr(sess, "_save_transcript"):
                    sess._save_transcript()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"assistant_message inject failed: {exc}")

    # Always emit a system_event so the activity stream reflects the run.
    try:
        from bridge.freyja_bridge import emit
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"bridge.emit unavailable: {exc}")

    emit({
        "type": "system_event",
        "sessionId": target_id,
        "subtype": "scheduler_run_delivered",
        "message": f"Scheduled job '{job.name}' completed",
        "details": {
            "jobId": job.id,
            "jobName": job.name,
            "runId": run.run_id,
            "renderMode": spec.render_mode,
            "preview": (output.text or "")[:500],
            "ts": time.time(),
        },
    })
    return target_id


def _format_with_meta(text: str, job: "JobRecord", run: "RunRecord") -> str:
    return (
        f"{text}\n\n"
        f"_— from scheduled job **{job.name}** (`{job.id}`, run `{run.run_id}`)_"
    )
