"""A job that runs in a new session per fire starts each fire empty.

Before each fire the runtime reset the job's session and then restored
its transcript from disk, which put the last fire's whole transcript
back: every Morning briefing run was sent the previous runs (fires 140,
141 and 142 in one transcript, about 117k tokens of context, before the
day's own work). The briefer is meant to carry over only its notes.
"""

from __future__ import annotations

from types import SimpleNamespace

from bridge.scheduler.models import CronSchedule, JobRecord, NewSession, NoopSinkSpec
from bridge.scheduler.runtime import _resolve_execution_session
from engine.session import TranscriptManager
from engine.types import Message


class _BridgeSession:
    """Models the bridge session: reset() drops the transcript, the next
    initialize() builds an empty one, and try_restore_transcript() loads
    the last saved transcript from disk."""

    def __init__(self, saved: list[Message]) -> None:
        self.saved = saved
        self.session = None
        self.runner = None
        self.permission_tier = "low"
        self.permission_handler = None

    async def initialize(self) -> None:
        if self.session is None:
            self.session = SimpleNamespace(transcript=TranscriptManager())

    async def try_restore_transcript(self) -> bool:
        await self.initialize()
        for message in self.saved:
            self.session.transcript.append_message(message)
        return True

    def reset(self) -> None:
        self.session = None


async def test_each_fire_of_a_new_session_job_starts_with_an_empty_transcript():
    last_fire = [
        Message(role="user", content="[Scheduled run — fire 142 of job \"Morning briefing\"]"),
        Message(role="assistant", content="Wrote today's briefing."),
    ]
    sess = _BridgeSession(last_fire)

    async def ensure_session(session_id, **_kw):
        # A session not in memory is restored from disk, as the bridge does.
        if sess.session is None:
            await sess.try_restore_transcript()
        return sess

    service = SimpleNamespace(state=SimpleNamespace(ensure_session=ensure_session))
    job = JobRecord(id="sched_briefer", name="Morning briefing", schedule=CronSchedule(expression="0 6 * * *"),
                    prompt="brief me", execution=NewSession(), sinks=[NoopSinkSpec()])

    got, _sid, owns = await _resolve_execution_session(service, job)

    assert owns
    assert len(got.session.transcript) == 0
