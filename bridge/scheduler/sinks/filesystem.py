"""Filesystem delivery sink — write the agent's output to a file.

Path template supports tokens:
  {job_name}, {job_id}, {run_id}, {date} (YYYY-MM-DD), {time} (HHMMSS),
  {iso} (ISO8601), {fire_number}

Tilde and env-var expansion are applied at write time so users can
type ``--to laptop:~/freyja-outputs/{date}.md`` without hand-resolving
the home directory.

Format:
  · markdown — body as Markdown, with a small YAML-ish header
  · json     — JSON-encoded {job_id, run_id, timestamp, text,
                attachments_meta}
  · text     — raw output body, no header
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridge.scheduler.models import FilesystemSinkSpec, JobRecord, RunRecord
    from bridge.scheduler.sinks import RunOutput, SinkContext


async def deliver_filesystem(
    spec: "FilesystemSinkSpec",
    job: "JobRecord",
    run: "RunRecord",
    output: "RunOutput",
    ctx: "SinkContext",
) -> str | None:
    template = spec.path_template
    if not template:
        raise ValueError("filesystem sink requires path_template")

    path = _resolve_template(template, job, run)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = _format_payload(output.text, spec.format, job, run)

    if spec.append and path.exists():
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n\n---\n\n")
            f.write(payload)
    else:
        path.write_text(payload, encoding="utf-8")

    # Copy attachments next to the file, if any.
    if output.attachments:
        att_dir = path.parent / f"{path.stem}.attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        for att in output.attachments:
            data = att.get("data")
            filename = att.get("filename") or att.get("name") or "attachment.bin"
            if isinstance(data, bytes):
                try:
                    (att_dir / filename).write_bytes(data)
                except OSError:
                    continue

    return str(path)


def _resolve_template(
    template: str,
    job: "JobRecord",
    run: "RunRecord",
) -> Path:
    now = datetime.now()
    s = os.path.expanduser(os.path.expandvars(template))
    tokens = {
        "{job_name}": _safe_filename(job.name) or job.id,
        "{job_id}": job.id,
        "{run_id}": run.run_id,
        "{date}": now.strftime("%Y-%m-%d"),
        "{time}": now.strftime("%H%M%S"),
        "{iso}": now.strftime("%Y-%m-%dT%H%M%S"),
        "{fire_number}": str(run.fire_number),
    }
    for k, v in tokens.items():
        s = s.replace(k, v)
    return Path(s).resolve()


def _safe_filename(s: str) -> str:
    return re.sub(r"[^\w\-_. ]", "_", s).strip()


def _format_payload(
    text: str,
    fmt: str,
    job: "JobRecord",
    run: "RunRecord",
) -> str:
    if fmt == "json":
        return json.dumps({
            "job_id": job.id,
            "job_name": job.name,
            "run_id": run.run_id,
            "fire_number": run.fire_number,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "text": text,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "cost_usd": run.cost_usd,
        }, indent=2, default=str)
    if fmt == "text":
        return text
    # markdown
    iso = datetime.fromtimestamp(run.started_at).isoformat(timespec="seconds")
    header = (
        f"# {job.name}\n"
        f"\n"
        f"- job: `{job.id}`\n"
        f"- run: `{run.run_id}`\n"
        f"- fired: {iso}\n"
        f"- fire_number: {run.fire_number}\n"
        f"\n"
        f"---\n"
        f"\n"
    )
    return header + text
