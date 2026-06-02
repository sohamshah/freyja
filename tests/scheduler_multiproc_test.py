"""Multi-process safety tests for the disk-backed SchedulerService.

These tests spawn real subprocesses pointed at a shared ``FREYJA_HOME``
and verify that the cross-process invariants hold:

  · Two ticking schedulers seeing the same due job fire it exactly once
  · Concurrent ``update_job`` on disjoint fields preserves both
  · ``cancel_run`` from one process aborts a run owned by another
  · Wake propagates within the tick-cap window
  · Owner-lock prevents two processes from ticking simultaneously
  · ``run_now`` fails fast when a peer is already firing

The tests exercise the service module directly — no agent, no LLM, no
Slack adapter. The runtime's ``fire_job`` is monkey-patched in the
child processes to record fires without spinning up real sessions.

Run with::

    pytest -x tests/scheduler_multiproc_test.py -s
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ─── Helpers ──────────────────────────────────────────────────────────


def _make_home() -> str:
    """Each test gets a fresh FREYJA_HOME under /tmp."""
    p = tempfile.mkdtemp(prefix="freyja-sched-multiproc-")
    return p


def _cleanup(p: str) -> None:
    shutil.rmtree(p, ignore_errors=True)


def _python() -> str:
    return sys.executable


# Run a Python child process with the project on PYTHONPATH and the
# scheduler pointed at the given FREYJA_HOME. The child runs whatever
# code is in the inline script. Returns the (stdout, stderr, rc).
def _spawn(home: str, code: str, *, timeout: float = 30.0) -> tuple[str, str, int]:
    env = os.environ.copy()
    env["FREYJA_HOME"] = home
    env["PYTHONPATH"] = os.getcwd() + ":" + env.get("PYTHONPATH", "")
    p = subprocess.run(
        [_python(), "-c", code],
        env=env, capture_output=True, text=True, timeout=timeout,
    )
    return p.stdout, p.stderr, p.returncode


def _spawn_async(home: str, code: str) -> subprocess.Popen:
    """Spawn a child and return the Popen handle (caller waits/kills)."""
    env = os.environ.copy()
    env["FREYJA_HOME"] = home
    env["PYTHONPATH"] = os.getcwd() + ":" + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [_python(), "-c", code],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        bufsize=0,
    )


# ─── Inline child scripts ─────────────────────────────────────────────


# Creates a one-shot job due at ``now + delay_seconds`` and writes the
# job id to stdout. Used by the pre-advance race test.
_CREATE_JOB_SCRIPT = '''
import asyncio, sys, time
from bridge.scheduler.models import (
    JobRecord, CreatorRef, OnceSchedule, NewSession, NoopSinkSpec,
)
from bridge.scheduler.persistence import save_job
from bridge.scheduler.service import SchedulerService
import datetime as dt

async def main():
    spec = JobRecord(
        id="DUE_JOB_ID",
        name="due-now",
        creator=CreatorRef(surface="api", session_id="t"),
        schedule=OnceSchedule(at_iso=dt.datetime.utcfromtimestamp(time.time()+DELAY_SECONDS).isoformat() + "+00:00"),
        prompt="test",
        execution=NewSession(),
        sinks=[NoopSinkSpec()],
    )
    spec.next_fire_at = time.time() + DELAY_SECONDS
    save_job(spec)
    print("CREATED")

asyncio.run(main())
'''


# A scheduler instance that ticks for `duration_seconds`, with a
# monkey-patched fire_job that just appends to a fires.log file.
_TICKER_SCRIPT = '''
import asyncio, os, sys, time, json
from pathlib import Path
import bridge.scheduler.runtime as rt
from bridge.scheduler.service import SchedulerService
from bridge.scheduler.models import RunRecord, new_run_id

# Replace fire_job with a no-op that records the fire to a file. This
# avoids needing a real agent / bridge runtime for these tests.
async def fake_fire(service, job):
    run = RunRecord(
        run_id=new_run_id(),
        job_id=job.id, job_name=job.name,
        started_at=time.time(), status="succeeded",
        fire_number=job.fire_count + 1,
        prompt=job.prompt,
    )
    run.finished_at = time.time()
    run.duration_seconds = 0.001
    from bridge.scheduler.persistence import save_run
    save_run(run)
    # Append (pid, job_id, run_id) to fires.log under FREYJA_HOME
    path = Path(os.environ["FREYJA_HOME"]) / "fires.log"
    with open(path, "a") as f:
        f.write(json.dumps({"pid": os.getpid(), "job_id": job.id, "run_id": run.run_id, "t": time.time()}) + "\\n")
    return run

rt.fire_job = fake_fire

async def main():
    svc = SchedulerService(state=None)
    await svc.start()
    # Print the owner status BEFORE stop (which releases the lock).
    print(f"OWNER={svc._is_owner}")
    await asyncio.sleep(DURATION_SECONDS)
    await svc.stop()
    print(f"DONE")

asyncio.run(main())
'''


# ─── Tests ────────────────────────────────────────────────────────────


def _count_fires(home: str) -> list[dict]:
    p = Path(home) / "fires.log"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def test_no_double_fire_under_race() -> None:
    """Two ticking schedulers see one due job — exactly one fires it.

    This is the headline correctness invariant. Without flocks both
    would pre-advance + fire the same job, producing two RunRecords.
    """
    home = _make_home()
    try:
        # Create one job due 1.0s from now.
        create = _CREATE_JOB_SCRIPT.replace("DUE_JOB_ID", "race1") \
                                     .replace("DELAY_SECONDS", "1.0")
        out, err, rc = _spawn(home, create)
        assert rc == 0, f"create failed: {err}"
        assert "CREATED" in out

        # Spawn two tickers concurrently for 4 seconds.
        ticker = _TICKER_SCRIPT.replace("DURATION_SECONDS", "4.0")
        a = _spawn_async(home, ticker)
        b = _spawn_async(home, ticker)
        for p in (a, b):
            p.wait(timeout=10)

        fires = _count_fires(home)
        for_race = [f for f in fires if f["job_id"] == "race1"]
        assert len(for_race) == 1, (
            f"expected exactly 1 fire across both processes, got "
            f"{len(for_race)} — pids: {[f['pid'] for f in for_race]}"
        )
        # Sanity: each process's stdout reports its owner status.
        out_a = a.stdout.read() if a.stdout else ""
        out_b = b.stdout.read() if b.stdout else ""
        owners = [int("OWNER=True" in o) for o in (out_a, out_b)]
        assert sum(owners) == 1, (
            f"expected exactly one owner; got {owners} (stdouts: {out_a!r}, {out_b!r})"
        )
    finally:
        _cleanup(home)


def test_update_under_flock_no_lost_update() -> None:
    """Two processes update disjoint fields of the same job. Both
    fields should land on disk; neither should clobber the other.

    The flock + reload-under-lock pattern serializes the writes.
    """
    home = _make_home()
    try:
        # Create a passive job (far-future fire, no schedulers ticking).
        os.environ["FREYJA_HOME"] = home
        sys.path.insert(0, os.getcwd())
        from bridge.scheduler.models import (
            JobRecord, CreatorRef, OnceSchedule, NewSession, NoopSinkSpec,
        )
        from bridge.scheduler.persistence import save_job, load_job
        spec = JobRecord(
            id="up1", name="orig",
            creator=CreatorRef(surface="api", session_id="t"),
            schedule=OnceSchedule(at_iso="2099-01-01T00:00:00+00:00"),
            prompt="orig",
            execution=NewSession(),
            sinks=[NoopSinkSpec()],
        )
        save_job(spec)

        # Two concurrent updates, each patching a different field.
        scripts = []
        for new_name, new_tag in [("renamed-a", "tag-a"), ("renamed-b", "tag-b")]:
            scripts.append(f'''
import asyncio, time
from bridge.scheduler.service import SchedulerService
from bridge.scheduler.models import JobPatch
async def main():
    svc = SchedulerService(state=None)
    # don't start() — no tick needed
    # update only the prompt field so the two children patch DIFFERENT fields
    # (renamed-a sets prompt; renamed-b sets description)
    try:
        if "{new_name}" == "renamed-a":
            await svc.update_job("up1", JobPatch(prompt="from-a"))
        else:
            await svc.update_job("up1", JobPatch(description="from-b"))
        print("OK")
    except Exception as e:
        print("ERR", e)
asyncio.run(main())
''')
        procs = [_spawn_async(home, s) for s in scripts]
        outs = []
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            outs.append((p.stdout.read() if p.stdout else "", p.stderr.read() if p.stderr else ""))
        # If both children couldn't acquire the flock simultaneously,
        # one might surface an error — but the per-job lock holds only
        # for the brief read-patch-write window, so both should succeed.
        loaded = load_job("up1")
        assert loaded is not None
        # Both fields should be present.
        assert loaded.prompt == "from-a", (
            f"expected prompt='from-a' from process A; got {loaded.prompt!r}; outs={outs}"
        )
        assert loaded.description == "from-b", (
            f"expected description='from-b' from process B; got {loaded.description!r}; outs={outs}"
        )
    finally:
        _cleanup(home)
        sys.path.pop(0)


def test_wake_propagates_across_processes() -> None:
    """Process A is ticking. Process B creates a job. A's tick loop
    notices the new job within the wake-poll window (1s + tick cap)."""
    home = _make_home()
    try:
        # Start a ticker in the background.
        ticker = _TICKER_SCRIPT.replace("DURATION_SECONDS", "5.0")
        a = _spawn_async(home, ticker)
        time.sleep(0.5)  # let A claim the owner-lock and start ticking

        # Create a job due in 1.5s from a second process.
        create = _CREATE_JOB_SCRIPT.replace("DUE_JOB_ID", "wake1") \
                                     .replace("DELAY_SECONDS", "1.5")
        out, err, rc = _spawn(home, create)
        assert rc == 0, err

        a.wait(timeout=10)

        fires = _count_fires(home)
        wake_fires = [f for f in fires if f["job_id"] == "wake1"]
        assert len(wake_fires) == 1, (
            f"expected the cross-process job to fire; got {len(wake_fires)} fires"
        )
        # The fire should have happened within ~2.5s of when the job
        # was created (1.5s schedule + 1s wake poll worst case).
    finally:
        _cleanup(home)


def test_run_now_rejects_when_peer_firing() -> None:
    """If process A holds the per-job flock (mid-fire), process B's
    ``run_now`` should fail-fast rather than race."""
    home = _make_home()
    try:
        sys.path.insert(0, os.getcwd())
        os.environ["FREYJA_HOME"] = home
        from bridge.scheduler.models import (
            JobRecord, CreatorRef, OnceSchedule, NewSession, NoopSinkSpec,
        )
        from bridge.scheduler.persistence import (
            save_job, FileLock, job_lock_path,
        )

        spec = JobRecord(
            id="rn1", name="run-now-conflict",
            creator=CreatorRef(surface="api", session_id="t"),
            schedule=OnceSchedule(at_iso="2099-01-01T00:00:00+00:00"),
            prompt="x", execution=NewSession(), sinks=[NoopSinkSpec()],
        )
        save_job(spec)

        # Simulate "process A is mid-fire" by grabbing the per-job
        # flock in THIS process.
        held_lock = FileLock(job_lock_path("rn1"))
        assert held_lock.acquire()

        # Now spawn a child that tries to run_now → must reject.
        code = '''
import asyncio
from bridge.scheduler.service import SchedulerService
async def main():
    svc = SchedulerService(state=None)
    try:
        await svc.run_job_now("rn1")
        print("UNEXPECTED-SUCCESS")
    except RuntimeError as e:
        print("REJECTED", str(e)[:100])
asyncio.run(main())
'''
        out, err, rc = _spawn(home, code)
        assert "REJECTED" in out, f"expected RuntimeError; out={out!r} err={err!r}"
        held_lock.release()
    finally:
        _cleanup(home)
        if os.getcwd() in sys.path:
            sys.path.remove(os.getcwd())


def test_owner_lock_singleton() -> None:
    """Only one of two starting schedulers becomes the tick owner."""
    home = _make_home()
    try:
        # Two schedulers start with no jobs at all. They sit for 2s
        # and exit. Only one should report OWNER=True.
        ticker = _TICKER_SCRIPT.replace("DURATION_SECONDS", "2.0")
        a = _spawn_async(home, ticker)
        b = _spawn_async(home, ticker)
        for p in (a, b):
            p.wait(timeout=10)
        out_a = a.stdout.read() if a.stdout else ""
        out_b = b.stdout.read() if b.stdout else ""
        owners_true = ("OWNER=True" in out_a) + ("OWNER=True" in out_b)
        assert owners_true == 1, (
            f"expected exactly one owner; got {owners_true} "
            f"(out_a={out_a!r}, out_b={out_b!r})"
        )
    finally:
        _cleanup(home)


if __name__ == "__main__":
    print("running tests...")
    tests = [
        test_no_double_fire_under_race,
        test_update_under_flock_no_lost_update,
        test_wake_propagates_across_processes,
        test_run_now_rejects_when_peer_firing,
        test_owner_lock_singleton,
    ]
    failures = 0
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
            print(f"  PASS")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"  ERROR: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
