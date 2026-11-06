"""A cancelled run stays cancelled and delivers nothing.

The run was marked "cancelled" when its cancel flag was seen, then went
on to delivery like a finished one: its partial answer went to the
job's sinks, and the delivery outcome overwrote the status, so the
dashboard listed it as succeeded.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import load_runs_for_job, request_run_cancellation, save_job
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from engine.types import Message


class _Service:
    def __init__(self, sessions: dict) -> None:
        self.state = SimpleNamespace(permission_tier="low", sessions=sessions)

    def emit_event(self, **_kw) -> None:
        pass


def _session_with_a_long_turn(monkeypatch):
    """A session whose turn writes half a report, then works on; and the
    deliveries made."""
    import bridge.freyja_bridge as fb
    import bridge.scheduler.runtime as runtime

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        cumulative_cost=0.0,
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )
    sess.started = asyncio.Event()

    async def turn(prompt: str) -> None:
        transcript = sess.session.transcript
        transcript.append_message(Message(role="user", content=prompt))
        transcript.append_message(Message(role="assistant", content="Half of the report"))
        sess.started.set()
        await asyncio.sleep(60)

    def schedule_turn(s, prompt, attachments=None):
        s.pending_task = asyncio.ensure_future(turn(prompt))
        return True

    async def quiescent(s, **_kw):
        await s.pending_task

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    monkeypatch.setattr(fb, "wait_until_quiescent", quiescent)
    monkeypatch.setattr(fb, "_force_cancel_session", lambda s, scope: s.pending_task.cancel())
    monkeypatch.setattr(fb, "_stop_background_work", lambda s, exclude: None)
    delivered: list[str] = []

    async def deliver_all(job, run, output, ctx):
        delivered.append(output.text)
        return []

    monkeypatch.setattr(runtime, "deliver_all", deliver_all)
    return sess, delivered


def _report_job() -> JobRecord:
    job = JobRecord(id="sched_report", name="report", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the report", execution=ExistingSession(session_id="s1"),
                    sinks=[NoopSinkSpec()])
    save_job(job)
    return job


async def test_a_cancelled_run_is_not_delivered(monkeypatch):
    from bridge.scheduler.runtime import fire_job

    sess, delivered = _session_with_a_long_turn(monkeypatch)
    job = _report_job()

    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    await asyncio.wait_for(sess.started.wait(), 5)
    request_run_cancellation(job.id, load_runs_for_job(job.id, limit=1)[0].run_id)
    run = await asyncio.wait_for(fire, 10)

    assert run.status == "cancelled"
    assert delivered == []
    assert load_runs_for_job(job.id, limit=1)[0].status == "cancelled"
    assert time.time() - run.started_at < 10


async def test_a_run_asked_to_stop_while_its_session_loads_stops(monkeypatch):
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.runtime import fire_job

    sess, delivered = _session_with_a_long_turn(monkeypatch)
    loading = asyncio.Event()
    loaded = asyncio.Event()

    async def resolve(_service, _job):
        loading.set()
        await loaded.wait()
        return sess, "s1", False

    monkeypatch.setattr(runtime, "_resolve_execution_session", resolve)
    job = _report_job()

    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    await asyncio.wait_for(loading.wait(), 5)
    request_run_cancellation(job.id, load_runs_for_job(job.id, limit=1)[0].run_id)
    loaded.set()
    run = await asyncio.wait_for(fire, 10)

    assert run.status == "cancelled"
    assert delivered == []


# ─── a run asked to stop stays in its job's history ───────────────────


def _running_run(job_id: str = "sched_report"):
    from bridge.scheduler.models import RunRecord
    from bridge.scheduler.persistence import save_run

    run = RunRecord(run_id="run_live", job_id=job_id, job_name="report",
                    started_at=time.time(), status="running")
    save_run(run)
    return run


def test_a_run_asked_to_stop_is_still_listed():
    from bridge.scheduler.persistence import load_recent_runs_global, load_run

    run = _running_run()
    assert request_run_cancellation(run.job_id, run.run_id)

    assert [r.run_id for r in load_runs_for_job(run.job_id)] == [run.run_id]
    assert [r.run_id for r in load_recent_runs_global()] == [run.run_id]
    assert load_run(run.job_id, run.run_id) is not None


def test_every_key_a_run_record_is_saved_with_reads_back():
    import dataclasses
    import json

    from bridge.scheduler.models import DeliveryReport, RunRecord
    from bridge.scheduler.persistence import load_run, runs_dir, save_run

    run = _running_run()
    run.delivery_reports = [DeliveryReport(sink_index=0, sink_kind="noop", success=True,
                                           delivered_at=time.time())]
    save_run(run)
    request_run_cancellation(run.job_id, run.run_id)

    on_disk = json.loads((runs_dir(run.job_id) / f"{run.run_id}.json").read_text())
    loaded = load_run(run.job_id, run.run_id)
    assert set(on_disk) <= {f.name for f in dataclasses.fields(RunRecord)}
    assert loaded.to_dict() == {**run.to_dict(), "cancel_requested": True}


def test_a_run_record_from_a_newer_scheduler_still_reads():
    from bridge.scheduler.models import RunRecord

    run = RunRecord.from_dict({**_running_run().to_dict(), "a_later_field": 1})

    assert run.run_id == "run_live"
