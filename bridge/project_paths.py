"""Shared path helpers for session-scoped Freyja project output."""

from __future__ import annotations

import re
from pathlib import Path


def safe_session_id(session_id: str | None) -> str:
    """Return a filesystem-safe session identifier."""

    raw = (session_id or "session").strip() or "session"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-")
    return cleaned or "session"


def project_output_dir(session_id: str | None) -> Path:
    """Default root for new files created on behalf of a session."""

    return Path.home() / ".freyja" / "projects" / safe_session_id(session_id)


def project_output_guidance(session_id: str | None, workspace: str | Path) -> str:
    """Concise prompt guidance for workspace vs. generated project output."""

    output_dir = project_output_dir(session_id)
    return (
        "Workspace and file output:\n"
        f"- The active workspace is `{workspace}`. Read and edit existing project files there when the task is about that project.\n"
        f"- Put new standalone files, generated assets, scratch files, reports, and handoff artifacts under `{output_dir}` unless the user gives another path.\n"
        "- For generated markdown/data artifacts, prefer `write_file` with `base=\"project\"`; if using shell redirection, set bash `working_dir` to the project output directory.\n"
        "- Use `artifacts` with action=`list` or action=`read` to inspect verified files produced by parent agents or sub-agents.\n"
        "- If the user explicitly asks to create or modify a file elsewhere, follow that instruction.\n"
    )
