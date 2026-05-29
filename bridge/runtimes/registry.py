"""Catalog of available execution runtimes.

A "runtime" is the thing that actually advances a turn — either Freyja's own
loop (`native`) or an external CLI agent spawned as a subprocess
(`claude_code_acp`, `codex_app_server`). The renderer's ready event reads
this catalog so the Harnesses section of the model picker can render
without baking the list into the UI.

The runtime IS the agent's identity, not a prompt. When the operator picks
"Claude Code", we spawn `claude --acp --stdio` and let it drive the turn —
there's no "Freyja pretending to be Claude Code" anymore. See §27 of the
Hermes deep-dive for the architectural reasoning.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSpec:
    """Static description of one runtime, used by both the registry and the
    serialized capabilities payload sent to the renderer."""

    id: str
    label: str
    description: str
    # CLI command spawned for this runtime. Empty for "native".
    command: str = ""
    # Default args for the subprocess. ACP harnesses get ["--acp", "--stdio"].
    args: tuple[str, ...] = ()
    # Env var the user can set to override `command` (e.g. point at a
    # custom Claude Code build).
    command_env_var: str | None = None
    # Args env var override (parsed shlex-style by the adapter).
    args_env_var: str | None = None

    def resolved_command(self) -> str:
        """The command string after env-var overrides."""
        if self.command_env_var:
            override = os.environ.get(self.command_env_var, "").strip()
            if override:
                return override
        return self.command


_NATIVE = RuntimeSpec(
    id="native",
    label="Freyja",
    description="Freyja's own agent loop — direct provider calls.",
)

_CLAUDE_CODE_ACP = RuntimeSpec(
    id="claude_code_acp",
    label="Claude Code",
    description=(
        "Anthropic's official terminal coding agent, driven via the ACP "
        "protocol over stdio. Real `claude` CLI is the execution engine; "
        "Freyja is the frame around it."
    ),
    command="claude",
    args=("--acp", "--stdio"),
    command_env_var="FREYJA_CLAUDE_CODE_COMMAND",
    args_env_var="FREYJA_CLAUDE_CODE_ARGS",
)

_RUNTIMES: dict[str, RuntimeSpec] = {
    _NATIVE.id: _NATIVE,
    _CLAUDE_CODE_ACP.id: _CLAUDE_CODE_ACP,
}


def normalize_runtime(value: str | None) -> str:
    """Coerce arbitrary input to a known runtime id, defaulting to 'native'.

    Unknown values are logged and fall back to native — never silently
    accept a runtime we don't know how to drive."""
    if not value:
        return "native"
    v = value.strip().lower()
    if v in _RUNTIMES:
        return v
    logger.warning("unknown runtime %r — falling back to native", value)
    return "native"


def get_runtime(value: str | None) -> RuntimeSpec:
    return _RUNTIMES[normalize_runtime(value)]


def list_runtimes() -> list[RuntimeSpec]:
    """All registered runtimes in deterministic order, native first."""
    return [_NATIVE] + [r for r in _RUNTIMES.values() if r.id != _NATIVE.id]


def probe_availability(spec: RuntimeSpec) -> tuple[bool, str | None]:
    """Light pre-flight: does the runtime's CLI exist on PATH?

    Returns (available, reason_when_unavailable). Native is always
    available. For external CLIs, we just check shutil.which — auth /
    version checks happen at session-spawn time so we don't burn IPC on
    the renderer's ready event."""
    if spec.id == "native":
        return True, None
    cmd = spec.resolved_command()
    if not cmd:
        return False, "no command configured"
    resolved = shutil.which(cmd)
    if not resolved:
        env_hint = (
            f" (set {spec.command_env_var} to override the path)"
            if spec.command_env_var
            else ""
        )
        return False, f"`{cmd}` not found on PATH{env_hint}"
    return True, None


def capabilities_payload() -> list[dict[str, Any]]:
    """Serializable form for the ready-event `capabilities.harnesses` key.

    Includes a live availability probe per runtime so the renderer can
    grey out / surface install hints without making a second IPC call."""
    out: list[dict[str, Any]] = []
    for spec in list_runtimes():
        available, reason = probe_availability(spec)
        item: dict[str, Any] = {
            "id": spec.id,
            "label": spec.label,
            "command": spec.resolved_command(),
            "description": spec.description,
            "available": available,
        }
        if not available and reason:
            item["unavailableReason"] = reason
        out.append(item)
    return out
