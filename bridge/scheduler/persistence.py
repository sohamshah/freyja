"""Atomic, per-file persistence for the scheduler.

Layout under ``~/.freyja/schedules/``:

  jobs/{job_id}.json              one file per job, atomic write per change
  runs/{job_id}/{run_id}.json     per-run metadata + delivery reports
  outputs/{job_id}/{run_id}/      run artifacts (response.md, attachments/…)
  events.jsonl                    global audit log, append-only
  metrics/                        rolled-up daily metrics
  .locks/job-{job_id}.lock        per-job fire/mutate flock target
  .wake                           bumped via os.utime on every CRUD;
                                  peers' tick loops poll mtime

Per-file persistence means every operation is bounded by a single small
file write regardless of how many jobs exist. The atomic-write pattern
(temp file → fsync → rename) prevents partial writes from corrupting
state.

This module also provides cross-process coordination primitives:

  · ``FileLock(path)`` — non-blocking exclusive fcntl.flock on a lock
    file. The kernel releases the lock automatically when the holding
    process exits (even via SIGKILL), so a crash never leaves a job
    permanently jammed.

  · ``touch_wake() / read_wake_mtime()`` — peers tick at ~1s intervals
    and notice "something changed" by stat-ing ``.wake``. Cheap and
    robust; no fs watchers, no listener-liveness problem.

  · ``append_event_locked()`` — flock-protected ``events.jsonl``
    appends so multi-process audit lines don't tear when payloads
    exceed PIPE_BUF (~4 KB).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from bridge.scheduler.models import (
    JobRecord,
    RunRecord,
)

logger = logging.getLogger("freyja.scheduler.persistence")


def schedules_root() -> Path:
    """Root directory for all scheduler state."""
    base = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    root = Path(base) / "schedules"
    return root


def jobs_dir() -> Path:
    return schedules_root() / "jobs"


def runs_dir(job_id: str) -> Path:
    return schedules_root() / "runs" / job_id


def outputs_dir(job_id: str, run_id: str) -> Path:
    return schedules_root() / "outputs" / job_id / run_id


def events_path() -> Path:
    return schedules_root() / "events.jsonl"


def metrics_dir() -> Path:
    return schedules_root() / "metrics"


def locks_dir() -> Path:
    return schedules_root() / ".locks"


def wake_path() -> Path:
    """Bumped via ``touch_wake`` on every state-mutating operation. Peers
    stat this file's mtime on each tick to short-circuit a full
    delta-scan when nothing has changed."""
    return schedules_root() / ".wake"


def job_lock_path(job_id: str) -> Path:
    """One lock file per job, used to coordinate concurrent fires +
    mutations across processes."""
    return locks_dir() / f"job-{job_id}.lock"


def owner_lock_path() -> Path:
    """Held by the process that currently owns the scheduler tick loop.
    First-to-flock wins. Other processes can still CRUD via disk +
    flock, but they don't run a tick loop."""
    return locks_dir() / "owner.lock"


def ensure_dirs() -> None:
    """Create the scheduler tree if it doesn't exist. Idempotent."""
    for d in (jobs_dir(), schedules_root() / "runs", schedules_root() / "outputs",
              metrics_dir(), locks_dir()):
        d.mkdir(parents=True, exist_ok=True)
    # Ensure the wake sentinel exists so peers' mtime stat doesn't
    # raise on a fresh install.
    wp = wake_path()
    if not wp.exists():
        try:
            wp.touch()
        except OSError:
            pass


# ─── Cross-process coordination ────────────────────────────────────────


class FileLock:
    """Non-blocking exclusive ``fcntl.flock`` on a per-job lock file.

    Usage::

        with FileLock(job_lock_path(job_id)) as got:
            if not got:
                # another process holds it
                return
            # critical section: re-read job from disk, mutate, save_job

    The kernel releases the lock automatically on process exit (clean
    OR crash), so we never need a recovery-from-stale-lock path. The
    blocking variant is intentionally omitted: schedulers should never
    block on a peer; they should skip and try again next tick.
    """

    __slots__ = ("path", "_fd", "_acquired")

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self._acquired = False

    def acquire(self) -> bool:
        """Attempt to take the lock. Returns True on success, False if
        another holder has it. Never raises on contention."""
        if self._acquired:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(
                str(self.path),
                os.O_RDWR | os.O_CREAT,
                0o644,
            )
        except OSError as exc:
            logger.warning("FileLock open(%s) failed: %s", self.path, exc)
            return False
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._acquired = True
            return True
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(self._fd)
                self._fd = None
                return False
            os.close(self._fd)
            self._fd = None
            logger.warning("FileLock flock(%s) failed: %s", self.path, exc)
            return False

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        self._acquired = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_a: Any) -> None:
        self.release()


@contextlib.contextmanager
def with_job_lock(job_id: str) -> Iterator[bool]:
    """Convenience wrapper for the common ``with FileLock(job_lock_path(id))``
    pattern. Yields True iff the lock was acquired."""
    lock = FileLock(job_lock_path(job_id))
    got = lock.acquire()
    try:
        yield got
    finally:
        if got:
            lock.release()


def touch_wake() -> None:
    """Bump the ``.wake`` mtime so peer tick loops notice something
    changed and re-scan. Best-effort — silent on failure."""
    ensure_dirs()
    p = wake_path()
    try:
        # Use os.utime to bump mtime without rewriting the file.
        # `times=None` => current time on both atime/mtime.
        if p.exists():
            os.utime(p, None)
        else:
            p.touch()
    except OSError as exc:
        logger.debug("touch_wake failed: %s", exc)


def read_wake_mtime() -> float:
    """Return ``.wake`` mtime, or 0 when missing."""
    try:
        return wake_path().stat().st_mtime
    except OSError:
        return 0.0


def jobs_dir_mtime() -> float:
    """Return ``jobs/`` directory mtime, or 0 when missing. Used as a
    cheap "did anything change?" gate before re-scanning per-file
    mtimes."""
    try:
        return jobs_dir().stat().st_mtime
    except OSError:
        return 0.0


# ─── Atomic JSON writes ────────────────────────────────────────────────


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Temp file → flush → fsync → atomic rename. Survives crashes
    mid-write. The caller's old data is never lost — either the new
    file lands fully or it doesn't land at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
    data = json.dumps(payload, indent=2, default=str)
    try:
        with open(tmp, "w") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync can fail on some filesystems (network mounts);
                # still better than skipping the atomic rename.
                pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ─── Jobs ──────────────────────────────────────────────────────────────


def save_job(job: JobRecord) -> None:
    """Persist a job record. Updates ``updated_at`` to wall-clock now."""
    ensure_dirs()
    job.updated_at = time.time()
    _atomic_write_json(jobs_dir() / f"{job.id}.json", job.to_dict())


def load_job(job_id: str) -> JobRecord | None:
    path = jobs_dir() / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return JobRecord.from_dict(json.load(f))
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to load job %s: %s", job_id, exc)
        return None


def delete_job(job_id: str) -> bool:
    """Remove the job file. Run history under runs/{job_id} and
    outputs/{job_id}/ is preserved so the dashboard can still show
    historical runs — pruning that is a separate concern."""
    path = jobs_dir() / f"{job_id}.json"
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("failed to delete job %s: %s", job_id, exc)
        return False


def load_all_jobs() -> list[JobRecord]:
    """Scan jobs_dir/. Skips malformed files with a warning rather than
    aborting the whole scheduler boot."""
    ensure_dirs()
    out: list[JobRecord] = []
    for path in sorted(jobs_dir().glob("*.json")):
        try:
            with open(path) as f:
                out.append(JobRecord.from_dict(json.load(f)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed job file %s: %s", path.name, exc)
    return out


def iter_job_files() -> Iterator[tuple[str, Path, float]]:
    """Yield ``(job_id, path, mtime)`` triples for every job file. Cheap
    — pure readdir + stat, no JSON parse. Use this when you need to
    know "which jobs exist" or "did any change" without paying the
    parse cost for unchanged files."""
    ensure_dirs()
    d = jobs_dir()
    if not d.exists():
        return
    for path in d.glob("*.json"):
        try:
            st = path.stat()
        except OSError:
            continue
        # filename is f"{job_id}.json"
        yield path.stem, path, st.st_mtime


# ─── Run cancellation flag ─────────────────────────────────────────────
#
# A run can be cancelled from another process. The signaller sets
# ``cancel_requested=true`` directly in the run JSON file; the owning
# process polls during its agent turn and cancels the local task.
# Avoids needing a separate IPC channel for what is, semantically, a
# state mutation on the run record.


def request_run_cancellation(job_id: str, run_id: str) -> bool:
    """Set ``cancel_requested=true`` on a run's JSON file. Returns False
    when the run file doesn't exist (already finished, or never
    started). The owner process polls via ``read_run_cancel_requested``."""
    path = runs_dir(job_id) / f"{run_id}.json"
    if not path.exists():
        return False
    try:
        with open(path) as f:
            data = json.load(f)
        data["cancel_requested"] = True
        _atomic_write_json(path, data)
        return True
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("request_run_cancellation failed %s/%s: %s", job_id, run_id, exc)
        return False


def read_run_cancel_requested(job_id: str, run_id: str) -> bool:
    """Cheap poll for the cancel flag. Returns False on any read error
    so we never spuriously cancel."""
    path = runs_dir(job_id) / f"{run_id}.json"
    if not path.exists():
        return False
    try:
        with open(path) as f:
            data = json.load(f)
        return bool(data.get("cancel_requested"))
    except (OSError, json.JSONDecodeError):
        return False


# ─── Runs ──────────────────────────────────────────────────────────────


def save_run(run: RunRecord) -> None:
    ensure_dirs()
    path = runs_dir(run.job_id) / f"{run.run_id}.json"
    _atomic_write_json(path, run.to_dict())


def load_run(job_id: str, run_id: str) -> RunRecord | None:
    path = runs_dir(job_id) / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return RunRecord.from_dict(json.load(f))
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to load run %s/%s: %s", job_id, run_id, exc)
        return None


def load_runs_for_job(job_id: str, *, limit: int = 50) -> list[RunRecord]:
    d = runs_dir(job_id)
    if not d.exists():
        return []
    # Sort by mtime DESC so newest first; cap by limit.
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[RunRecord] = []
    for path in files[:limit]:
        try:
            with open(path) as f:
                out.append(RunRecord.from_dict(json.load(f)))
        except Exception:  # noqa: BLE001
            continue
    return out


def load_recent_runs_global(limit: int = 100) -> list[RunRecord]:
    """All runs across all jobs, newest first. Used by the global Run
    History view."""
    base = schedules_root() / "runs"
    if not base.exists():
        return []
    all_files: list[Path] = []
    for job_dir in base.iterdir():
        if job_dir.is_dir():
            all_files.extend(job_dir.glob("*.json"))
    all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[RunRecord] = []
    for path in all_files[:limit]:
        try:
            with open(path) as f:
                out.append(RunRecord.from_dict(json.load(f)))
        except Exception:  # noqa: BLE001
            continue
    return out


# ─── Run outputs ───────────────────────────────────────────────────────


def write_run_output(
    job_id: str,
    run_id: str,
    *,
    text: str,
    attachments: Iterable[tuple[str, bytes]] = (),
) -> Path:
    """Write the agent's response and any attachments to the per-run
    outputs directory. Returns the directory path so sinks can
    reference it."""
    out = outputs_dir(job_id, run_id)
    out.mkdir(parents=True, exist_ok=True)
    response_path = out / "response.md"
    try:
        response_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning("failed to write run output %s: %s", response_path, exc)
    att_dir = out / "attachments"
    for filename, blob in attachments:
        try:
            att_dir.mkdir(parents=True, exist_ok=True)
            (att_dir / filename).write_bytes(blob)
        except OSError as exc:
            logger.warning("failed to write attachment %s: %s", filename, exc)
    return out


# ─── Events log ────────────────────────────────────────────────────────


def append_event(event: dict[str, Any]) -> None:
    """Append a single JSON line to events.jsonl. Multi-process safe:
    wraps the append in fcntl.flock so payloads larger than PIPE_BUF
    (~4 KB) don't tear when two processes race the writer. Best-effort
    — persistence failures here are logged but don't break the
    scheduler tick."""
    ensure_dirs()
    payload = dict(event)
    payload.setdefault("ts", time.time())
    line = json.dumps(payload, default=str) + "\n"
    path = events_path()
    try:
        with open(path, "a") as f:
            try:
                # Brief flock — release the moment the line lands. We
                # use blocking acquire here (LOCK_EX without LOCK_NB)
                # because the critical section is microseconds; the
                # contention surface is the events file alone, not the
                # scheduler tick.
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                # On platforms without flock, fall back to a plain
                # append. Tears are possible but rare on small lines.
                f.write(line)
    except OSError as exc:
        logger.warning("failed to append scheduler event: %s", exc)


def read_recent_events(limit: int = 200) -> list[dict[str, Any]]:
    p = events_path()
    if not p.exists():
        return []
    try:
        with open(p) as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
