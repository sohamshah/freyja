"""Helpers for spawning child processes from the Freyja bridge.

The Electron app launches the bridge with these env vars set so the
bundled CPython can find its standard library:

    PYTHONHOME=/Applications/Freyja.app/Contents/Resources/python-bundle
    PYTHONPATH=/Applications/Freyja.app/Contents/Resources:...
    VIRTUAL_ENV=/Applications/Freyja.app/Contents/Resources/python-bundle

These are CORRECT for the bridge itself. They are POISON for any other
Python the bridge spawns — system `/usr/bin/python3`, a user's `uv`-
managed venv, a sub-agent's MCP server, the gateway daemon's children,
any `python` invoked from the agent's bash tool — because the inherited
PYTHONHOME points those interpreters at Freyja's bundle as their
stdlib root, where `encodings`, `os`, etc. are not in the layout the
foreign interpreter expects, and they abort at startup with:

    ModuleNotFoundError: No module named 'encodings'
    Fatal Python error: Failed to import encodings module

Documented incident (Jun 1 2026): the gateway daemon's `daemon.log`
accumulated 37,674 lines of this exact crash because every child it
spawned inherited the bundle's PYTHONHOME. The agent's bash tool also
exhibited it whenever the model tried to run `/usr/bin/python3` for
quick scripting.

Rule: when spawning ANY subprocess from the bridge, use `child_env()`.
"""

from __future__ import annotations

import os

# Env vars that point at the Freyja Python bundle. Stripping them means
# any spawned interpreter falls back to its own embedded path config —
# correct for system pythons, venvs the user controls, harness CLIs
# that spawn their own MCP children, etc.
_LEAKED_PYTHON_ENV_VARS = frozenset({
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
})


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of `os.environ` with Freyja-specific Python vars
    removed and `extra` overrides applied on top.

    Use this for every `create_subprocess_*` / `subprocess.run` /
    `subprocess.Popen` call in the bridge. The contract:

      * PYTHONHOME / PYTHONPATH / VIRTUAL_ENV / PYTHONSTARTUP /
        PYTHONEXECUTABLE are stripped so non-bundle pythons don't
        crash on launch.
      * Everything else is inherited verbatim (PATH, HOME, LANG, the
        agent-specified working directory's normal env, etc.).
      * `extra` is applied last so call-site overrides win (e.g. a
        harness adapter setting `NO_COLOR=1`, `TERM=dumb`).

    If you genuinely want the bundle's PYTHONHOME (e.g. you're spawning
    the bridge's own python to run a Freyja helper script), pass it
    back in via `extra`.
    """
    env = {k: v for k, v in os.environ.items() if k not in _LEAKED_PYTHON_ENV_VARS}
    if extra:
        env.update(extra)
    return env
