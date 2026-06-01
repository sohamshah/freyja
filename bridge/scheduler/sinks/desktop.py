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

    formatted = _format_with_meta(output.text or "", job, run)

    # Persist into the target session's transcript so the message
    # survives daemon restarts. Only valid when the target session
    # actually exists in memory (it must — otherwise sending an
    # assistant message into a session the bridge doesn't know
    # about would race transcript persistence).
    if sess is not None:
        try:
            inner = getattr(sess, "session", None)
            if inner is not None:
                inner.add_assistant_message(formatted)
                if hasattr(sess, "_save_transcript"):
                    sess._save_transcript()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            # Persistence failure is non-fatal — we still emit the
            # in-memory append below so the user sees the message in
            # the current renderer state.
            import logging
            logging.getLogger("freyja.scheduler.sinks.desktop").warning(
                "desktop transcript persist failed: %s", exc,
            )

    try:
        from bridge.freyja_bridge import emit
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"bridge.emit unavailable: {exc}")

    # The activity-feed system_event — small banner with metadata.
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

    # The in-chat assistant message — this is what users actually
    # expect when they ask Freyja to "deliver the result here." The
    # renderer's `message_appended` handler hard-appends to the
    # target session's transcript view; combined with the bridge-side
    # add_assistant_message above, both the in-memory view and the
    # persisted transcript stay in sync.
    emit({
        "type": "message_appended",
        "sessionId": target_id,
        "role": "assistant",
        "content": formatted,
        "messageId": f"sched-{run.run_id}",
        "createdAt": int(time.time() * 1000),
    })
    return target_id


def _format_with_meta(text: str, job: "JobRecord", run: "RunRecord") -> str:
    """Wrap the run output with an unambiguous header + footer so:
      · the user can visually tell this is from a scheduled job
      · the agent can recognize it later (e.g. when asked "what did
        the morning briefing find") and quote / summarize it
      · the schedule tool's `get_run` action is one click away
    """
    import datetime as _dt
    fired_at = _dt.datetime.fromtimestamp(run.started_at).strftime(
        "%Y-%m-%d %H:%M"
    )
    header = (
        f"📅 **Scheduled run** · *{job.name}* "
        f"(`{job.id}` · run `{run.run_id}` · fired {fired_at})\n"
        f"---\n\n"
    )
    footer = (
        f"\n\n---\n"
        f"_Full record: `schedule(action='get_run', job_id='{job.id}', "
        f"run_id='{run.run_id}')`_"
    )
    return header + (text or "_(no output produced)_") + footer
