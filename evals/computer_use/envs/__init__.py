"""Environments with their own server state, one package each:

- `backend.py`: SITE (the pages), init (an episode's state from its config),
  backend (the page API) and view (the state a check reads)
- `tasks.py`: TASKS, and SOLVERS for the privileged oracle (task id -> steps)
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType

_HERE = Path(__file__).resolve().parent
# Every package here with a backend, the first one first. CUA_ENVS=a,b keeps only
# those, so an env being built cannot break runs of the others.
_ONLY = {n.strip() for n in os.environ.get("CUA_ENVS", "").split(",") if n.strip()}
NAMES = sorted(
    (
        p.name
        for p in _HERE.iterdir()
        if (p / "backend.py").is_file() and (not _ONLY or p.name in _ONLY)
    ),
    key=lambda n: (n != "airline", n),
)


def backend(name: str) -> ModuleType:
    return importlib.import_module(f"{__name__}.{name}.backend")


def tasks(name: str) -> ModuleType:
    return importlib.import_module(f"{__name__}.{name}.tasks")
