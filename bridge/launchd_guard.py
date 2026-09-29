"""Decide whether this process may change Freyja's launchd jobs.

launchd keeps one registry of jobs per macOS user, keyed by label,
whatever HOME a process runs with. Tests and the e2e harness run Freyja
with a temporary HOME or FREYJA_HOME. When one of those runs installed the
scheduler LaunchAgent, it replaced the real ``com.freyja.scheduler`` job
with one that pointed into its temporary folder. The folder was deleted a
few minutes later, so launchd could no longer start the job, and the
``launchctl kickstart`` in ``npm run rebuild`` then waited forever.

Only a process running as the real user, with the real home and a
permanent Freyja home, may install, reload or remove a job.
"""

from __future__ import annotations

import os
import pwd
import tempfile
from pathlib import Path


def _temp_roots() -> list[Path]:
    roots = {Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/folders")}
    return [r.resolve() for r in roots]


def _inside_temp_dir(path: Path) -> bool:
    return any(path == root or root in path.parents for root in _temp_roots())


def launchd_changes_blocked() -> str | None:
    """Why this process must not change a launchd job, or None if it may."""
    if os.environ.get("FREYJA_NO_LAUNCHD", "").strip().lower() in ("1", "true", "yes", "on"):
        return "FREYJA_NO_LAUNCHD is set"
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    home = Path.home().resolve()
    if home != account_home:
        return f"HOME ({home}) is not this account's home ({account_home})"
    freyja_home = Path(os.environ.get("FREYJA_HOME") or account_home / ".freyja").resolve()
    if _inside_temp_dir(freyja_home):
        return f"FREYJA_HOME ({freyja_home}) is a temporary directory"
    return None
