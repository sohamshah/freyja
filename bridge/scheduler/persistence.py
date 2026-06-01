"""Atomic, per-file persistence for the scheduler.

Layout under ``~/.freyja/schedules/``:

  jobs/{job_id}.json              one file per job, atomic write per change
  runs/{job_id}/{run_id}.json     per-run metadata + delivery reports
  outputs/{job_id}/{run_id}/      run artifacts (response.md, attachments/…)
  events.jsonl                    global audit log, append-only
  metrics/                        rolled-up daily metrics
  .locks/                         advisory file locks

Per-file persistence means every operation is bounded by a single small
file write regardless of how many jobs exist. The atomic-write pattern
(temp file → fsync → rename) prevents partial writes from corrupting
state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

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


def ensure_dirs() -> None:
    """Create the scheduler tree if it doesn't exist. Idempotent."""
    for d in (jobs_dir(), schedules_root() / "runs", schedules_root() / "outputs",
              metrics_dir(), locks_dir()):
        d.mkdir(parents=True, exist_ok=True)


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
    """Append a single JSON line to events.jsonl. Best-effort —
    persistence failures here are logged but don't break the
    scheduler tick."""
    ensure_dirs()
    payload = dict(event)
    payload.setdefault("ts", time.time())
    line = json.dumps(payload, default=str)
    try:
        with open(events_path(), "a") as f:
            f.write(line + "\n")
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
