#!/usr/bin/env python3
"""
Freyja JSONL bridge.

Reads JSON commands from stdin, runs per-session AsyncAgentRunner instances,
emits JSON events to stdout. The Electron main process wires this up over a
subprocess pipe.

All events are one JSON object per line. The schema matches the BridgeEvent
union in src/shared/events.ts.

This version supports multiple concurrent sessions keyed by `sessionId`, so
the renderer can switch between prior sessions without losing transcripts.
Each session has its own AsyncAgentRunner + engine Session; the tool
registry and subagent wiring are rebuilt lazily on first use.

If anything goes wrong during import (missing env, missing deps, etc.), the
bridge prints a single `{"type":"error","message":"..."}` line and exits
non-zero so the Electron side can fall back to demo mode cleanly.

Run directly:
    python bridge/freyja_bridge.py < commands.jsonl

Or from Electron main via spawn().
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

# Make the desktop/ dir importable so `from bridge.tools import ...` works
# regardless of where Python was launched from.
_BRIDGE_DIR = Path(__file__).resolve().parent
_DESKTOP_DIR = _BRIDGE_DIR.parent
if str(_DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(_DESKTOP_DIR))

from engine.compaction import SummaryCompaction


# ─── Stdout helpers ─────────────────────────────────────────────────────────


# Diagnostic log for computer-use events. Everything we emit that has a
# type in this set also gets appended to a JSONL file, with pngBase64
# truncated so the file stays inspectable. Enable via the env var
# FREYJA_DEBUG_LOG=1 (on by default when computer control is on).
_COMPUTER_EVENT_TYPES = frozenset(
    {
        "computer_session_start",
        "computer_session_end",
        "screenshot_frame",
        "action_planned",
        "action_executed",
        "emergency_stop",
        "subagent_spawn",
        "subagent_done",
        "subagent_update",
        "session_spawned",
        "session_completed",
        "tool_use_start",
        "tool_input_end",
        "tool_result",
        "file_change_set",
        "text_delta",
    }
)

_DEBUG_LOG_PATH = Path.home() / ".freyja" / "bridge-events.jsonl"
_DEBUG_LOG_ENABLED = (
    os.environ.get("FREYJA_DEBUG_LOG", "1").lower() not in ("0", "false", "no")
)

SKILL_PRUNE_MIN_SKILLS = 5
SKILL_PRUNE_MIN_SKILL_TOKENS = 5_000
SKILL_PRUNE_SESSION_TOKEN_THRESHOLD = 50_000
SKILL_MAINTENANCE_MAX_TOKENS = 4_000


def _write_debug_log(event: dict[str, Any]) -> None:
    """Append a trimmed copy of the event to the diagnostic log file.

    Strips pngBase64 down to a length marker so the log stays
    human-readable and we can tail it during a session. Truncates the
    file on each new computer_session_start so only the most recent
    run is there.
    """
    if not _DEBUG_LOG_ENABLED:
        return
    etype = event.get("type")
    if etype not in _COMPUTER_EVENT_TYPES:
        return
    try:
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Start-of-session → wipe the file so we only keep the latest trace.
        if etype == "computer_session_start":
            _DEBUG_LOG_PATH.write_text("")
        trimmed = dict(event)
        if "pngBase64" in trimmed:
            trimmed["pngBase64"] = f"<{len(trimmed['pngBase64'])} b64 chars>"
        # Text deltas are noisy; truncate
        if "text" in trimmed and isinstance(trimmed["text"], str):
            trimmed["text"] = trimmed["text"][:60]
        if "preview" in trimmed and isinstance(trimmed["preview"], str):
            trimmed["preview"] = trimmed["preview"][:200]
        # Stamp each row with monotonic wall time
        trimmed["_t"] = time.time()
        with _DEBUG_LOG_PATH.open("a") as f:
            f.write(json.dumps(trimmed, ensure_ascii=False, default=str))
            f.write("\n")
    except Exception:  # noqa: BLE001
        # Never let debug logging take down the bridge
        pass


def emit(event: dict[str, Any]) -> None:
    """Emit a single JSON line to stdout and flush immediately."""
    try:
        _write_debug_log(event)
        sys.stdout.write(json.dumps(event, ensure_ascii=False, default=str))
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception:
        traceback.print_exc(file=sys.stderr)


def log(level: str, message: str) -> None:
    emit({"type": "log", "level": level, "message": message})


def emit_error(message: str, recoverable: bool = False) -> None:
    emit({"type": "error", "message": message, "recoverable": recoverable})


def _build_user_message_with_attachments(
    user_content: str,
    attachments: list[dict[str, Any]] | None,
) -> Any:
    """Convert renderer attachments into engine content blocks."""
    if not attachments:
        return user_content

    from engine.types import ImageBlock, TextBlock

    image_blocks: list[ImageBlock] = []
    for attachment in attachments:
        if attachment.get("type") != "image":
            continue

        data = str(attachment.get("dataBase64") or "").strip()
        if data.startswith("data:"):
            _, separator, payload = data.partition(",")
            if separator:
                data = payload.strip()

        if not data:
            continue

        media_type = str(attachment.get("mimeType") or "image/png")
        image_blocks.append(ImageBlock.from_base64(data, media_type))

    if not image_blocks:
        return user_content

    blocks: list[Any] = [*image_blocks]
    if user_content:
        blocks.append(TextBlock(text=user_content))
    return blocks


# ─── Entry point ───────────────────────────────────────────────────────────


async def _main() -> None:
    boot_session_id = f"desktop-{int(time.time() * 1000):x}"
    workspace = os.environ.get("FREYJA_WORKSPACE", os.getcwd())

    try:
        from engine.runner import AsyncAgentRunner  # noqa: F401
        from engine.session import Session  # noqa: F401
    except Exception as exc:
        emit_error(f"failed to import engine: {exc}", recoverable=False)
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)

    default_model = os.environ.get("FREYJA_MODEL", "claude-sonnet-4-6")
    emit(
        {
            "type": "ready",
            "sessionId": boot_session_id,
            "mode": "live",
            "capabilities": {
                "workspace": workspace,
                "model": default_model,
                "subagents": True,
                "skills": True,
                "images": True,
                "models": _annotate_models(AVAILABLE_MODELS),
            },
        }
    )
    log(
        "info",
        f"bridge started (boot={boot_session_id}, pid={os.getpid()}, "
        f"workspace={workspace}) — if stuck, kill this pid to recover",
    )

    state = _BridgeState(workspace=workspace, default_model=default_model)
    await state.ensure_session(boot_session_id)
    await _command_loop(state)


# ─── Model catalog ─────────────────────────────────────────────────────────


AVAILABLE_MODELS: list[dict[str, Any]] = [
    # ─── Anthropic (ANTHROPIC_API_KEY) ─────────────────────────────────
    {
        "id": "claude-opus-4-7",
        "family": "anthropic",
        "label": "Claude Opus 4.7",
        "tier": "max",
        "contextWindow": 1_000_000,
        "thinking": False,
        "envVar": "ANTHROPIC_API_KEY",
        "description": "Latest Opus. Best for hard coding and agentic tasks. Adaptive thinking, 128k output.",
    },
    {
        "id": "claude-opus-4-6",
        "family": "anthropic",
        "label": "Claude Opus 4.6",
        "tier": "max",
        "contextWindow": 1_000_000,
        "thinking": True,
        "envVar": "ANTHROPIC_API_KEY",
        "description": "Previous-gen Opus. Deep reasoning with extended thinking.",
    },
    {
        "id": "claude-sonnet-4-6",
        "family": "anthropic",
        "label": "Claude Sonnet 4.6",
        "tier": "balanced",
        "contextWindow": 1_000_000,
        "thinking": True,
        "envVar": "ANTHROPIC_API_KEY",
        "description": "Balanced default. Strong quality, sane latency.",
    },
    {
        "id": "claude-haiku-4-5",
        "family": "anthropic",
        "label": "Claude Haiku 4.5",
        "tier": "fast",
        "contextWindow": 200_000,
        "thinking": True,
        "envVar": "ANTHROPIC_API_KEY",
        "description": "Fastest Claude. Good for quick edits and fanout.",
    },
    {
        "id": "claude-opus-4-5",
        "family": "anthropic",
        "label": "Claude Opus 4.5",
        "tier": "max",
        "contextWindow": 200_000,
        "thinking": True,
        "envVar": "ANTHROPIC_API_KEY",
        "description": "Previous-gen Opus.",
    },
    {
        "id": "claude-sonnet-4-5",
        "family": "anthropic",
        "label": "Claude Sonnet 4.5",
        "tier": "balanced",
        "contextWindow": 1_000_000,
        "thinking": True,
        "envVar": "ANTHROPIC_API_KEY",
        "description": "Previous-gen Sonnet.",
    },
    # ─── OpenAI (OPENAI_API_KEY) ───────────────────────────────────────
    {
        "id": "gpt-5.5",
        "family": "openai",
        "label": "GPT-5.5",
        "tier": "max",
        "contextWindow": 1_050_000,
        "thinking": True,
        "envVar": "OPENAI_API_KEY",
        "description": "OpenAI's newest frontier model. Best for complex coding, reasoning, and computer use.",
    },
    {
        "id": "gpt-5.4",
        "family": "openai",
        "label": "GPT-5.4",
        "tier": "max",
        "contextWindow": 1_050_000,
        "thinking": True,
        "envVar": "OPENAI_API_KEY",
        "description": "Previous OpenAI flagship. Strong reasoning, vision, tool use.",
    },
    {
        "id": "gpt-5.4-mini",
        "family": "openai",
        "label": "GPT-5.4 Mini",
        "tier": "balanced",
        "contextWindow": 400_000,
        "thinking": True,
        "envVar": "OPENAI_API_KEY",
        "description": "Balanced OpenAI tier. Cheap per-turn, still reasons.",
    },
    {
        "id": "gpt-5.4-nano",
        "family": "openai",
        "label": "GPT-5.4 Nano",
        "tier": "fast",
        "contextWindow": 400_000,
        "thinking": True,
        "envVar": "OPENAI_API_KEY",
        "description": "Cheapest OpenAI tier. Good for fanout and high-volume subagents.",
    },
    {
        "id": "gpt-5.3-codex",
        "family": "openai",
        "label": "GPT-5.3 Codex",
        "tier": "balanced",
        "contextWindow": 400_000,
        "thinking": True,
        "envVar": "OPENAI_API_KEY",
        "description": "Agentic coding specialist. Powers GPT-5.4's coding capabilities.",
    },
    {
        "id": "zai-glm-4.7",
        "family": "cerebras",
        "label": "GLM 4.7 (Cerebras)",
        "tier": "fast",
        "contextWindow": 131_072,
        "thinking": False,
        "envVar": "CEREBRAS_API_KEY",
        "description": "~1000 tps on Cerebras. Great for subagents and fanout.",
    },
    # ─── Fireworks (FIREWORKS_API_KEY) ─────────────────────────────────
    {
        "id": "kimi-k2.5",
        "family": "fireworks",
        "label": "Kimi K2.5",
        "tier": "balanced",
        "contextWindow": 262_144,
        "thinking": False,
        "envVar": "FIREWORKS_API_KEY",
        "description": "Moonshot's Kimi K2.5 via Fireworks. Vision + 262k ctx.",
    },
    {
        "id": "kimi-k2.6",
        "family": "fireworks",
        "label": "Kimi K2.6",
        "tier": "max",
        "contextWindow": 262_144,
        "thinking": True,
        "envVar": "FIREWORKS_API_KEY",
        "description": "Moonshot's newer multimodal agentic model via Fireworks. Vision + 262k ctx.",
    },
    {
        "id": "deepseek-v4-pro",
        "family": "fireworks",
        "label": "DeepSeek V4 Pro",
        "tier": "max",
        "contextWindow": 1_048_576,
        "thinking": True,
        "envVar": "FIREWORKS_API_KEY",
        "description": "DeepSeek's frontier MoE reasoning model via Fireworks. 1M ctx, function calling.",
    },
    {
        "id": "glm-5.1",
        "family": "fireworks",
        "label": "GLM 5.1",
        "tier": "max",
        "contextWindow": 202_752,
        "thinking": True,
        "envVar": "FIREWORKS_API_KEY",
        "description": "Z.ai's newer GLM 5.1 via Fireworks. Agentic engineering, tool use, 202.8k ctx.",
    },
    {
        "id": "glm5",
        "family": "fireworks",
        "label": "GLM 5 (Fireworks)",
        "tier": "balanced",
        "contextWindow": 202_752,
        "thinking": False,
        "envVar": "FIREWORKS_API_KEY",
        "description": "Zhipu's GLM 5 via Fireworks.",
    },
    {
        "id": "minimax-m2.7",
        "family": "fireworks",
        "label": "MiniMax M2.7",
        "tier": "balanced",
        "contextWindow": 196_608,
        "thinking": True,
        "envVar": "FIREWORKS_API_KEY",
        "description": "MiniMax M2.7 via Fireworks. Agent harnesses, teams, skills, and dynamic tool search.",
    },
    {
        "id": "minimax-m2.5",
        "family": "fireworks",
        "label": "MiniMax M2.5",
        "tier": "fast",
        "contextWindow": 196_608,
        "thinking": True,
        "envVar": "FIREWORKS_API_KEY",
        "description": "MiniMax M2.5 via Fireworks. Fast and cheap.",
    },
    {
        "id": "deepseek-v3.2",
        "family": "fireworks",
        "label": "DeepSeek v3.2",
        "tier": "balanced",
        "contextWindow": 163_840,
        "thinking": True,
        "envVar": "FIREWORKS_API_KEY",
        "description": "DeepSeek v3.2 via Fireworks. Efficient reasoning and agent performance.",
    },
    {
        "id": "qwen3.6-plus",
        "family": "fireworks",
        "label": "Qwen3.6 Plus",
        "tier": "balanced",
        "contextWindow": 1_000_000,
        "thinking": True,
        "envVar": "FIREWORKS_API_KEY",
        "description": "Alibaba's Qwen3.6 Plus via Fireworks. Vision, function calling, preserved reasoning, 1M ctx.",
    },
]


MODEL_REASONING_META: dict[str, dict[str, Any]] = {
    "claude-opus-4-7": {
        "reasoningMode": "adaptive",
        "reasoningLevels": ["auto"],
        "reasoningDefault": "auto",
    },
    "claude-opus-4-6": {
        "reasoningMode": "effort",
        "reasoningLevels": ["low", "medium", "high", "max"],
        "reasoningDefault": "max",
    },
    "claude-sonnet-4-6": {
        "reasoningMode": "effort",
        "reasoningLevels": ["low", "medium", "high"],
        "reasoningDefault": "high",
    },
    "claude-haiku-4-5": {
        "reasoningMode": "effort",
        "reasoningLevels": ["low", "medium", "high"],
        "reasoningDefault": "high",
    },
    "claude-opus-4-5": {
        "reasoningMode": "budget",
        "reasoningLevels": ["low", "medium", "high"],
        "reasoningDefault": "high",
    },
    "claude-sonnet-4-5": {
        "reasoningMode": "budget",
        "reasoningLevels": ["low", "medium", "high"],
        "reasoningDefault": "high",
    },
    "gpt-5.5": {
        "reasoningMode": "effort",
        "reasoningLevels": ["low", "medium", "high", "xhigh"],
        "reasoningDefault": "high",
    },
    "gpt-5.4": {
        "reasoningMode": "effort",
        "reasoningLevels": ["low", "medium", "high", "xhigh"],
        "reasoningDefault": "high",
    },
    "gpt-5.4-mini": {
        "reasoningMode": "effort",
        "reasoningLevels": ["low", "medium", "high"],
        "reasoningDefault": "medium",
    },
    "gpt-5.4-nano": {
        "reasoningMode": "effort",
        "reasoningLevels": ["low", "medium"],
        "reasoningDefault": "low",
    },
    "gpt-5.3-codex": {
        "reasoningMode": "effort",
        "reasoningLevels": ["low", "medium", "high", "xhigh"],
        "reasoningDefault": "medium",
    },
    "deepseek-v4-pro": {
        "reasoningMode": "effort",
        "reasoningLevels": ["none", "low", "medium", "high", "max"],
        "reasoningDefault": "high",
        "reasoningHistory": ["interleaved"],
    },
    "glm-5.1": {
        "reasoningMode": "effort",
        "reasoningLevels": ["none", "low", "medium", "high"],
        "reasoningDefault": "high",
    },
    "kimi-k2.6": {
        "reasoningMode": "effort",
        "reasoningLevels": ["none", "low", "medium", "high"],
        "reasoningDefault": "high",
        "reasoningHistory": ["preserved"],
    },
    "minimax-m2.7": {
        "reasoningMode": "required",
        "reasoningLevels": ["low", "medium", "high"],
        "reasoningDefault": "medium",
        "reasoningHistory": ["interleaved"],
    },
    "minimax-m2.5": {
        "reasoningMode": "required",
        "reasoningLevels": ["low", "medium", "high"],
        "reasoningDefault": "medium",
        "reasoningHistory": ["interleaved"],
    },
    "deepseek-v3.2": {
        "reasoningMode": "binary",
        "reasoningLevels": ["none", "high"],
        "reasoningDefault": "high",
    },
    "qwen3.6-plus": {
        "reasoningMode": "effort",
        "reasoningLevels": ["none", "low", "medium", "high"],
        "reasoningDefault": "medium",
        "reasoningHistory": ["preserved"],
    },
}


def _annotate_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each model as `available` based on whether its env var is set."""
    result: list[dict[str, Any]] = []
    for m in models:
        env = m.get("envVar", "")
        reasoning_meta = MODEL_REASONING_META.get(
            m["id"],
            {
                "reasoningMode": "none",
                "reasoningLevels": [],
                "reasoningDefault": "none",
            },
        )
        result.append(
            {
                **m,
                **reasoning_meta,
                "available": bool(env and os.environ.get(env)),
            }
        )
    return result


def _family_for_model(model_id: str) -> str:
    for m in AVAILABLE_MODELS:
        if m["id"] == model_id:
            return m["family"]
    # Heuristics for unknown model ids
    if model_id.startswith("claude-"):
        return "anthropic"
    if model_id.startswith("gpt-"):
        return "openai"
    if model_id.startswith("zai-") or "glm-4" in model_id:
        return "cerebras"
    return "fireworks"


def _default_thinking_for_model(model_id: str) -> "Any":
    """Return the right ThinkingConfig for a model, enabled by default
    for models that support extended thinking. Opus 4.7 uses adaptive
    thinking (no budget_tokens) so thinking is left disabled here and
    the model handles it natively. Opus 4.6 gets effort='max'."""
    from engine.types import ThinkingConfig

    # Look up whether this model supports thinking
    model_entry = next((m for m in AVAILABLE_MODELS if m["id"] == model_id), None)
    supports_thinking = model_entry.get("thinking", False) if model_entry else False

    if not supports_thinking:
        # Includes claude-opus-4-7 (adaptive thinking only — no explicit budget needed)
        return ThinkingConfig()

    default_effort = MODEL_REASONING_META.get(model_id, {}).get("reasoningDefault")
    if isinstance(default_effort, str) and default_effort in {
        "low",
        "medium",
        "high",
        "max",
        "xhigh",
    }:
        return ThinkingConfig(enabled=True, effort=default_effort)

    # Opus 4.6 gets max effort; everything else gets high
    if model_id == "claude-opus-4-6":
        return ThinkingConfig(enabled=True, effort="max")
    return ThinkingConfig(enabled=True, effort="high")


def build_provider(model_id: str, thinking_level: str = "auto") -> Any:
    """Create a fresh provider for the given model id.

    Auto-detects the provider family, loads the right Config, and passes a
    ThinkingConfig only to providers that support it. Raises a clear
    ValueError if the required API key env var is missing.

    thinking_level:
      - "auto" (default): enable thinking for models that support it,
        with effort based on model tier (Opus 4.6 → max, others → high;
        Opus 4.7 uses adaptive thinking and needs no explicit config)
      - "off"/"none": disable thinking
      - "max"/"high"/"medium"/"low": explicit effort level
    """
    from engine.types import ThinkingConfig

    family = _family_for_model(model_id)

    if thinking_level in ("auto", ""):
        thinking = _default_thinking_for_model(model_id)
    elif thinking_level in ("off", "none"):
        thinking = ThinkingConfig()
    else:
        thinking = ThinkingConfig(enabled=True, effort=thinking_level)

    if family == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")
        from engine.anthropic_provider import (
            AnthropicConfig,
            AnthropicProvider,
        )

        return AnthropicProvider(config=AnthropicConfig(model=model_id, thinking=thinking))

    if family == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not set")
        from engine.openai_provider import OpenAIConfig, OpenAIProvider

        # OpenAI uses `reasoning=` (not `thinking=`)
        return OpenAIProvider(
            config=OpenAIConfig(model=model_id, reasoning=thinking)
        )

    if family == "cerebras":
        if not os.environ.get("CEREBRAS_API_KEY"):
            raise ValueError("CEREBRAS_API_KEY is not set")
        from engine.cerebras_provider import CerebrasConfig, CerebrasProvider

        return CerebrasProvider(config=CerebrasConfig(model=model_id))

    if family == "fireworks":
        if not os.environ.get("FIREWORKS_API_KEY"):
            raise ValueError("FIREWORKS_API_KEY is not set")
        from engine.fireworks_provider import (
            FireworksConfig,
            FireworksProvider,
        )

        return FireworksProvider(config=FireworksConfig(model=model_id, reasoning=thinking))

    raise ValueError(f"Unknown model family for {model_id}")


# ─── Desktop permission handler ────────────────────────────────────────────


class DesktopPermissionHandler:
    """Async-native permission handler that round-trips requests to the UI.

    ToolRegistry calls `request_permission(action, level, details)` inside
    its execute() flow. We create an asyncio.Future, emit a JSON
    `permission_request` event, and return the awaitable. When the renderer
    sends `permission_response`, `_handle_command` calls `resolve()` to
    settle the future with the user's answer.

    `auto_approve` is mutated live by `set_permission_policy` commands so
    changes to the settings modal apply immediately without recreating
    the session.
    """

    def __init__(self, session_id: str, initial_tier: str | None = None) -> None:
        self.session_id = session_id
        self._pending: dict[str, asyncio.Future] = {}
        tier = initial_tier or os.environ.get("FREYJA_PERMISSION_AUTO", "low")
        self._auto_approve = _parse_auto_approve(tier)

    def set_policy(self, tier: str) -> None:
        self._auto_approve = _parse_auto_approve(tier)

    def request_permission(
        self,
        action: str,
        reason: str | None = None,
        level: Any = None,
        details: str | None = None,
    ) -> Any:
        """Return either a coroutine (async awaited) or a HumanResponse."""
        from engine.permissions import HumanResponse, PermissionLevel

        level_name = getattr(level, "value", str(level or "medium"))
        if isinstance(level, str):
            level_name = level
        try:
            resolved_level = level if isinstance(level, PermissionLevel) else PermissionLevel(level_name)
        except Exception:
            resolved_level = PermissionLevel.MEDIUM

        if resolved_level in self._auto_approve:
            return HumanResponse(approved=True, response="auto-approved")

        async def awaiter() -> HumanResponse:
            request_id = uuid.uuid4().hex
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending[request_id] = fut
            emit(
                {
                    "type": "permission_request",
                    "sessionId": self.session_id,
                    "requestId": request_id,
                    "level": resolved_level.value,
                    "prompt": action,
                    "reason": reason or "",
                    "details": details or "",
                }
            )
            try:
                response = await fut
            except asyncio.CancelledError:
                raise
            finally:
                self._pending.pop(request_id, None)
            approved = bool(response.get("approved"))
            return HumanResponse(
                approved=approved,
                response=response.get("response") or ("allow" if approved else "deny"),
            )

        return awaiter()

    def resolve(self, request_id: str, approved: bool, response_text: str = "") -> bool:
        fut = self._pending.get(request_id)
        if not fut or fut.done():
            return False
        fut.set_result({"approved": approved, "response": response_text})
        return True

    def ask_human(self, *args: Any, **kwargs: Any) -> Any:
        # We don't currently surface ask_human in the desktop UI; always return
        # an empty response so tools that optionally call it don't block.
        from engine.permissions import HumanResponse

        return HumanResponse(approved=True, response="")


def _parse_auto_approve(value: str) -> set:
    """Parse FREYJA_PERMISSION_AUTO into a PermissionLevel set."""
    from engine.permissions import PermissionLevel

    tier = (value or "low").strip().lower()
    if tier == "none":
        return set()
    if tier == "low":
        return {PermissionLevel.LOW}
    if tier == "medium":
        return {PermissionLevel.LOW, PermissionLevel.MEDIUM}
    if tier == "high":
        return {
            PermissionLevel.LOW,
            PermissionLevel.MEDIUM,
            PermissionLevel.HIGH,
        }
    if tier == "yolo":
        # Yolo truly means yolo — auto-approve every level, including
        # DANGEROUS. If the user picks this tier, they have explicitly
        # opted out of all permission prompts.
        return {
            PermissionLevel.LOW,
            PermissionLevel.MEDIUM,
            PermissionLevel.HIGH,
            PermissionLevel.DANGEROUS,
        }
    return {PermissionLevel.LOW}


# ─── Tracing tool registry ──────────────────────────────────────────────────


def _truncate_preview(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n… [truncated {len(text) - limit} chars] …\n\n{tail}"


def _new_tracing_registry(base_registry, session_id: str, get_runner=None):
    """Wrap a ToolRegistry so each execute() call streams events to the UI.

    The runner has already emitted `tool_use_start` via on_stream. We inject
    `tool_input_end` with finalized arguments so the UI has a structured
    copy regardless of streaming deltas, and we emit `tool_result` with the
    measured duration and preview.

    When ``get_runner`` is provided, a ``usage`` event is emitted after each
    tool call so the activity panel shows live token/cost stats without
    waiting for the entire agent turn to finish.
    """
    original_execute = base_registry.execute

    async def traced_execute(call, **kwargs):
        start = time.monotonic()
        tool_name = getattr(call, "name", "")
        tool_id = getattr(call, "id", "")
        tool_args = getattr(call, "arguments", {}) or {}
        try:
            from bridge.file_changes import create_file_change_tracker

            file_change_tracker = create_file_change_tracker(
                call_id=tool_id,
                tool_name=tool_name,
                arguments=tool_args,
            )
        except Exception as exc:  # noqa: BLE001
            file_change_tracker = None
            log("debug", f"file-change tracker init failed: {exc}")

        try:
            emit(
                {
                    "type": "tool_input_end",
                    "sessionId": session_id,
                    "id": tool_id,
                    "arguments": tool_args,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log("debug", f"tool_input_end emit failed: {exc}")

        try:
            result = await original_execute(call, **kwargs)
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            emit(
                {
                    "type": "tool_result",
                    "sessionId": session_id,
                    "id": tool_id,
                    "preview": f"Tool raised: {exc}",
                    "isError": True,
                    "durationMs": duration_ms,
                }
            )
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        content = getattr(result, "content", "")
        if not isinstance(content, str):
            try:
                content = json.dumps(content, default=str)
            except Exception:
                content = repr(content)

        if file_change_tracker is not None:
            try:
                change_set = file_change_tracker.finish(
                    success=not bool(getattr(result, "is_error", False)),
                )
                if change_set:
                    emit(
                        {
                            "type": "file_change_set",
                            "sessionId": session_id,
                            "changeSet": change_set,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                log("debug", f"file-change emit failed: {exc}")

        emit(
            {
                "type": "tool_result",
                "sessionId": session_id,
                "id": tool_id,
                "preview": _truncate_preview(content),
                "isError": bool(getattr(result, "is_error", False)),
                "durationMs": duration_ms,
            }
        )

        # Emit a live usage snapshot after each tool call so the activity
        # panel updates in real time instead of waiting for the turn to end.
        if get_runner is not None:
            try:
                runner = get_runner()
                if runner is not None:
                    u = runner.usage
                    in_tok = int(getattr(u, "input", 0) or 0)
                    out_tok = int(getattr(u, "output", 0) or 0)
                    cr_tok = int(getattr(u, "cache_read", 0) or 0)
                    cw_tok = int(getattr(u, "cache_write", 0) or 0)
                    cost = (in_tok * 3 + out_tok * 15) / 1_000_000
                    emit(
                        {
                            "type": "usage",
                            "sessionId": session_id,
                            "inputTokens": in_tok,
                            "outputTokens": out_tok,
                            "cacheReadTokens": cr_tok,
                            "cacheWriteTokens": cw_tok,
                            "cost": cost,
                        }
                    )
            except Exception:  # noqa: BLE001
                pass

        return result

    base_registry.execute = traced_execute  # type: ignore[assignment]
    return base_registry


# ─── Per-session bridge state ──────────────────────────────────────────────


class _BridgeSession:
    """Owns the engine Session + Runner + tool registry for one id."""

    def __init__(
        self,
        session_id: str,
        *,
        workspace: str,
        model_id: str,
        state: "_BridgeState",
    ) -> None:
        self.id = session_id
        self.workspace = workspace
        self.model_id = model_id
        self.state = state
        self.session: Any | None = None
        self.runner: Any | None = None
        self.provider: Any | None = None
        self.tool_registry: Any | None = None
        self.subagent_registry: Any | None = None
        self.memory_store: Any | None = None
        self.skill_store: Any | None = None
        self.permission_handler: DesktopPermissionHandler | None = None
        # Track the effective permission tier for this session independently
        # of the handler, so a `set_permission_policy` that arrives before
        # initialize() still takes effect when the handler is finally built.
        self.permission_tier: str = state.permission_tier
        self.current_tool_id: str | None = None
        self.current_turn_id: str | None = None
        self.turn_counter = 0
        self.pending_task: asyncio.Task | None = None
        self.tool_start_at: dict[str, float] = {}
        # Message queue — when the user sends a message while a turn is
        # in progress, we queue it here instead of cancelling. The task
        # runner drains the queue after each turn completes.
        self.queued_messages: list[tuple[str, list[dict[str, Any]] | None]] = []
        # Shared cancel signal for computer-use tools. `computer.emergency_stop`
        # sets this; parent-tier computer tools poll it every action and
        # abort mid-flight. Rebuilt on reset() so a new session starts clean.
        self.computer_cancel: asyncio.Event = asyncio.Event()
        # Session-scoped message bus for inter-agent communication.
        from bridge.tools.message_bus import SessionMessageBus
        self.message_bus: SessionMessageBus = SessionMessageBus()
        self._tool_list = ""
        self._agent_types_section = ""
        self._base_system_prompt = ""
        self._system_prompt = ""
        self.loaded_skills: dict[str, dict[str, Any]] = {}
        self.skill_maintenance_done = False

    async def initialize(self) -> None:
        """Lazily build the runner + tool registry for this session."""
        if self.runner is not None:
            return
        from engine.runner import AsyncAgentRunner
        from engine.session import Session
        from engine.types import ThinkingConfig

        from bridge.tools import build_desktop_registry
        from bridge.tools.sub_agent_registry import SubAgentRegistry
        from bridge.knowledge import MemoryStore, SkillStore
        from bridge.knowledge.prompt import build_knowledge_prompt

        thinking = _default_thinking_for_model(self.model_id)
        try:
            provider = build_provider(self.model_id)
        except ValueError as exc:
            emit_error(str(exc), recoverable=True)
            raise
        self.provider = provider

        def _provider_factory(model_id: str, thinking_effort: str = "auto") -> Any:
            return build_provider(model_id, thinking_level=thinking_effort)

        async def _emit_subagent(event: dict[str, Any]) -> None:
            event.setdefault("sessionId", self.id)
            emit(event)

        sub_registry = SubAgentRegistry()
        self.subagent_registry = sub_registry
        self.permission_handler = DesktopPermissionHandler(
            session_id=self.id,
            initial_tier=self.permission_tier,
        )
        self.memory_store = MemoryStore(Path(self.workspace))
        self.skill_store = SkillStore(Path(self.workspace))

        async def _emit_memory_updated(item: Any, reason: str = "") -> None:
            emit(
                {
                    "type": "memory_updated",
                    "sessionId": self.id,
                    "memory": item.to_event(),
                    "reason": reason,
                }
            )

        async def _emit_skill_event(skill: Any, reason: str = "") -> None:
            event_type = "skill_loaded" if reason == "loaded" else "skill_retrieved"
            if reason == "loaded":
                self._record_loaded_skill(skill)
            emit(
                {
                    "type": event_type,
                    "sessionId": self.id,
                    "skill": skill.to_event(),
                    "reason": reason,
                }
            )

        # Closure: wrap a registry with tracing scoped to a specific
        # session id. Used by the parent session (for itself) and passed
        # through to sub_agent_tool so child sessions get their own
        # tracing namespace.
        def _wrap_child_registry(reg: Any, session_id: str) -> Any:
            return _new_tracing_registry(reg, session_id)

        registry = build_desktop_registry(
            workspace=Path(self.workspace),
            subagent_registry=sub_registry,
            subagent_provider_factory=_provider_factory,
            subagent_model=self.model_id,
            subagent_emit=_emit_subagent,
            subagent_parent_session_id=self.id,
            subagent_wrap_registry=_wrap_child_registry,
            permission_handler=self.permission_handler,
            include_computer=self.state.computer_enabled,
            computer_session_id=self.id,
            computer_cancel_event=self.computer_cancel,
            message_bus=self.message_bus,
            memory_store=self.memory_store,
            skill_store=self.skill_store,
            on_memory_updated=_emit_memory_updated,
            on_skill_event=_emit_skill_event,
        )
        tool_names = sorted(registry._tools.keys())  # noqa: SLF001
        self.tool_registry = _new_tracing_registry(
            registry, self.id, get_runner=lambda: self.runner
        )

        tool_list = "\n".join(
            f"- `{name}` — {registry._tools[name].definition.summary}"  # noqa: SLF001
            for name in tool_names
        )
        self._tool_list = tool_list

        from bridge.tools.agent_types import agent_types_for_prompt

        agent_types_section = agent_types_for_prompt(
            workspace=Path(self.workspace),
            parent_model=self.model_id,
        )
        self._agent_types_section = agent_types_section

        self._base_system_prompt = (
                "You are running inside Freyja.\n"
                "\n"
                f"You are operating in the workspace `{self.workspace}`.\n"
                "\n"
                "Available tools:\n"
                f"{tool_list}\n"
                "\n"
                f"{agent_types_section}\n"
                "\n"
                "Use real tool calls (no XML markers). Be concise and actionable. "
                "Prefer reading the codebase before answering questions that depend "
                "on it. Use fenced code blocks for code and inline backticks for "
                "identifiers. When presenting tabular data, use GitHub-style "
                "tables with `|` and `---`.\n"
                "\n"
                "INSTALLING DEPENDENCIES: if a tool call fails because a "
                "package or binary is missing, just install it yourself and "
                "retry. You do NOT need to ask permission — in the default "
                "yolo tier every package install is auto-approved. Prefer "
                "`uv pip install <pkg>` inside the project's venv over raw "
                "pip. Use `uv add <pkg>` only when the change should be "
                "persisted to pyproject.toml. For npm use `npm install`, "
                "for macOS system tools use `brew install`. Common Python "
                "import → package mappings: `import fitz` → `pymupdf`, "
                "`import cv2` → `opencv-python`, `import PIL` → `pillow`, "
                "`import yaml` → `pyyaml`, `import sklearn` → "
                "`scikit-learn`. On a ModuleNotFoundError, install the "
                "right package and retry the same code on the next turn — "
                "do not give up or switch to a worse approach."
            )
        knowledge_prompt = build_knowledge_prompt(
            memory_store=self.memory_store,
            skill_store=self.skill_store,
        )
        system_prompt = (
            self._base_system_prompt
            + ("\n\n" + knowledge_prompt if knowledge_prompt else "")
        )
        # Stash for the session export so training data includes the prompt
        self._system_prompt = system_prompt

        self.session = Session.create(
            system_prompt=system_prompt,
            tools=list(registry._tools.values()),  # noqa: SLF001
        )

        runner = AsyncAgentRunner(
            provider=provider,
            compaction_strategy=SummaryCompaction(),
            tool_registry=self.tool_registry,
            on_stream=self._on_stream,
            on_system_event=self._on_system_event,
            thinking=thinking,
        )
        self.runner = runner
        log(
            "info",
            f"session {self.id} ready (model={self.model_id}, tools={len(tool_names)})",
        )
        # Emit the system prompt so the renderer can include it in
        # session exports for training data. This is a one-time event
        # per session initialization (not per turn).
        emit(
            {
                "type": "system_event",
                "sessionId": self.id,
                "subtype": "system_prompt_set",
                "message": "System prompt configured",
                "details": {"systemPrompt": system_prompt},
            }
        )
        for item in self.memory_store.list_items(limit=50):
            emit(
                {
                    "type": "memory_updated",
                    "sessionId": self.id,
                    "memory": item.to_event(),
                    "reason": "session initialization",
                }
            )
        for skill in self.skill_store.list_skills()[:100]:
            emit(
                {
                    "type": "skill_updated",
                    "sessionId": self.id,
                    "skill": skill.to_event(),
                }
            )

    def reset(self) -> None:
        """Drop the runner so the next turn starts a fresh transcript."""
        self.session = None
        self.runner = None
        self.provider = None
        self.tool_registry = None
        self.subagent_registry = None
        self.memory_store = None
        self.skill_store = None
        self.permission_handler = None
        self.turn_counter = 0
        self.current_tool_id = None
        self.current_turn_id = None
        self.tool_start_at.clear()
        self.computer_cancel = asyncio.Event()
        self._tool_list = ""
        self._agent_types_section = ""
        self._base_system_prompt = ""
        self._system_prompt = ""

    async def try_restore_transcript(self) -> bool:
        """Attempt to restore engine transcript from disk.

        Called by ensure_session() when creating a _BridgeSession for a
        session id that isn't in memory but may have persisted state from
        a previous app run. If a transcript file exists:

        1. Initialize the session (builds provider, tools, runner).
        2. Deserialize the transcript into the engine Session.
        3. Handle cross-provider mismatch (strip thinking blocks).
        4. Handle context overflow (trigger compaction if needed).

        Returns True if transcript was restored, False otherwise.
        """
        from bridge.transcript_persistence import (
            load_transcript,
            provider_family,
        )

        data = load_transcript(self.id)
        if data is None:
            emit(
                {
                    "type": "system_event",
                    "sessionId": self.id,
                    "subtype": "transcript_not_found",
                    "message": "No persisted transcript — send context summary if available",
                    "details": {},
                }
            )
            return False

        transcript_data = data.get("transcript")
        if not transcript_data or not transcript_data.get("entries"):
            emit(
                {
                    "type": "system_event",
                    "sessionId": self.id,
                    "subtype": "transcript_not_found",
                    "message": "Persisted transcript is empty",
                    "details": {},
                }
            )
            return False

        log("info", f"restoring transcript for session {self.id}")

        # Step 1: Initialize (creates empty Session + tools + runner).
        await self.initialize()
        if self.session is None:
            return False

        # Step 2: Restore the transcript into the engine Session.
        try:
            self.session.restore_transcript(data)
        except Exception as exc:
            log("warn", f"transcript restore failed for {self.id}: {exc}")
            return False

        # Step 3: Detect provider family mismatch.
        persisted_model = data.get("metadata", {}).get("model_id", "")
        if persisted_model and provider_family(persisted_model) != provider_family(self.model_id):
            stripped = self.session.strip_thinking_blocks()
            if stripped:
                log(
                    "info",
                    f"stripped {stripped} thinking block(s) — provider changed "
                    f"({persisted_model} → {self.model_id})",
                )

        # Step 4: Check context fit, compact if needed.
        try:
            from engine.constants import (
                CONTEXT_COMPACTION_THRESHOLD,
                DEFAULT_CONTEXT_WINDOW,
                MODEL_CONTEXT_WINDOWS,
            )

            ctx_window = MODEL_CONTEXT_WINDOWS.get(
                self.model_id, DEFAULT_CONTEXT_WINDOW
            )
            estimated = self.session.estimate_tokens()
            if estimated > ctx_window * CONTEXT_COMPACTION_THRESHOLD:
                log(
                    "info",
                    f"restored transcript ({estimated} tokens) exceeds "
                    f"compaction threshold for {self.model_id} "
                    f"({ctx_window}), compacting",
                )
                from engine.compaction import SummaryCompaction

                compactor = SummaryCompaction()
                compactor.compact(self.session.transcript, self.provider)
                self.session.compaction_count += 1
        except Exception as exc:
            log("warn", f"post-restore compaction failed: {exc}")

        # Step 5: Backfill any orphaned tool_use blocks from the old session.
        try:
            _backfill_orphan_tool_results(self.session)
        except Exception as exc:
            log("warn", f"post-restore orphan backfill failed: {exc}")

        entry_count = len(self.session.transcript)
        log("info", f"transcript restored for {self.id}: {entry_count} entries")

        emit(
            {
                "type": "system_event",
                "sessionId": self.id,
                "subtype": "transcript_restored",
                "message": f"Session context restored ({entry_count} transcript entries)",
                "details": {
                    "entryCount": entry_count,
                    "estimatedTokens": self.session.estimate_tokens(),
                },
            }
        )
        return True

    def _save_transcript(self) -> None:
        """Persist the engine transcript to disk (fire-and-forget)."""
        if self.session is None:
            return
        try:
            from bridge.transcript_persistence import save_transcript

            data = self.session.serialize_transcript()
            # Stash the model id so cross-provider detection works on restore.
            data.setdefault("metadata", {})["model_id"] = self.model_id
            save_transcript(self.id, data)
        except Exception as exc:
            log("warn", f"failed to save transcript for {self.id}: {exc}")

    def _last_provider_context_tokens(self) -> int:
        """Return the last provider-reported request context size, if known."""
        if self.runner is None:
            return 0
        try:
            return int(self.runner.usage.effective_context_tokens())
        except Exception:  # noqa: BLE001
            return 0

    def _current_usage_fields(self) -> tuple[int, int, int, int, float]:
        """Best-effort cumulative runner usage for a usage_snapshot event."""
        if self.runner is None:
            return (0, 0, 0, 0, 0.0)
        try:
            usage = self.runner.usage
            in_tok = int(getattr(usage, "input", 0) or 0)
            out_tok = int(getattr(usage, "output", 0) or 0)
            cr_tok = int(getattr(usage, "cache_read", 0) or 0)
            cw_tok = int(getattr(usage, "cache_write", 0) or 0)
            cost = (in_tok * 3 + out_tok * 15) / 1_000_000
            return (in_tok, out_tok, cr_tok, cw_tok, cost)
        except Exception:  # noqa: BLE001
            return (0, 0, 0, 0, 0.0)

    def _mark_usage_compacted(self, context_tokens_after: int) -> None:
        """Clear stale provider context counters after transcript compaction."""
        if self.runner is None:
            return
        try:
            usage = self.runner.usage
            output = int(getattr(usage, "output", 0) or 0)
            usage.last_input = max(0, context_tokens_after - output)
            usage.last_cache_read = 0
            usage.last_cache_write = 0
            usage.cache_read = 0
            usage.cache_write = 0
        except Exception:  # noqa: BLE001
            return

    def _write_compaction_snapshot(
        self,
        *,
        phase: str,
        compactor: SummaryCompaction,
        request_tokens: int,
        provider_context_tokens: int = 0,
    ) -> dict[str, Any]:
        """Persist an inspectable copy of transcript state around compaction."""
        if self.session is None:
            return {}
        try:
            safe_id = "".join(
                c for c in self.id if c.isalnum() or c in ("-", "_", ".")
            )[:120]
            root = Path.home() / ".freyja" / "sessions" / "compactions"
            root.mkdir(parents=True, exist_ok=True)
            stamp = int(time.time() * 1000)
            base = root / f"{safe_id}-{stamp}-{phase}"

            messages = self.session.transcript.get_messages()
            preview = compactor._format_conversation(messages, max_chars=12_000)  # noqa: SLF001
            full_text = compactor._format_conversation(messages, max_chars=1_500_000)  # noqa: SLF001

            md_path = base.with_suffix(".md")
            md_path.write_text(
                "\n".join(
                    [
                        f"# Compaction {phase} snapshot",
                        "",
                        f"- session: `{self.id}`",
                        f"- model: `{self.model_id}`",
                        f"- request estimate: `{request_tokens}` tokens",
                        f"- last provider context: `{provider_context_tokens}` tokens",
                        f"- transcript entries: `{len(self.session.transcript.entries)}`",
                        "",
                        "```text",
                        full_text,
                        "```",
                    ]
                ),
                encoding="utf-8",
            )

            json_path = base.with_suffix(".json")
            json_path.write_text(
                json.dumps(
                    self.session.serialize_transcript(),
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                ),
                encoding="utf-8",
            )

            return {
                f"{phase}_snapshot_path": str(md_path),
                f"{phase}_snapshot_json_path": str(json_path),
                f"{phase}_preview": preview[:4_000],
                f"{phase}_preview_chars": len(preview),
            }
        except Exception as exc:  # noqa: BLE001
            log("warn", f"failed to write {phase} compaction snapshot: {exc}")
            return {}

    async def force_compact(self) -> None:
        """Force an LLM summary compaction for the current session."""
        await self.initialize()
        if self.session is None or self.provider is None:
            emit(
                {
                    "type": "system_event",
                    "sessionId": self.id,
                    "subtype": "compaction_skipped",
                    "message": "Manual compaction skipped: session is not ready",
                    "details": {"trigger": "manual", "chatVisible": True},
                }
            )
            return

        if self.pending_task and not self.pending_task.done():
            emit(
                {
                    "type": "system_event",
                    "sessionId": self.id,
                    "subtype": "compaction_skipped",
                    "message": "Manual compaction skipped: a turn is currently running",
                    "details": {
                        "trigger": "manual",
                        "reason": "turn_running",
                        "chatVisible": True,
                    },
                }
            )
            return

        compactor = SummaryCompaction()
        try:
            request_tokens_before = int(self.session.estimate_tokens())
        except Exception:  # noqa: BLE001
            request_tokens_before = 0
        try:
            transcript_tokens_before = int(self.session.transcript.estimate_tokens())
        except Exception:  # noqa: BLE001
            transcript_tokens_before = 0
        provider_context_before = self._last_provider_context_tokens()
        context_tokens_before = max(provider_context_before, request_tokens_before)
        entries_before = len(getattr(self.session.transcript, "entries", []))
        before_snapshot = self._write_compaction_snapshot(
            phase="before",
            compactor=compactor,
            request_tokens=request_tokens_before,
            provider_context_tokens=provider_context_before,
        )

        emit(
            {
                "type": "system_event",
                "sessionId": self.id,
                "subtype": "compaction_start",
                "message": (
                    "Manual compaction started "
                    f"({context_tokens_before:,} context tokens; "
                    f"{transcript_tokens_before:,} transcript tokens)"
                ),
                "details": {
                    "trigger": "manual",
                    "tokens_before": context_tokens_before,
                    "context_tokens_before": context_tokens_before,
                    "request_tokens_before": request_tokens_before,
                    "last_provider_context_tokens": provider_context_before,
                    "transcript_tokens_before": transcript_tokens_before,
                    "entries_before": entries_before,
                    "chatVisible": True,
                    **before_snapshot,
                },
            }
        )

        result = await asyncio.to_thread(
            compactor.compact,
            self.session.transcript,
            self.provider,
        )

        if not result.success:
            emit(
                {
                    "type": "system_event",
                    "sessionId": self.id,
                    "subtype": "compaction_skipped",
                    "message": f"Manual compaction skipped: {result.error or 'not enough history to compact'}",
                    "details": {
                        "trigger": "manual",
                        "reason": result.error or "unknown",
                        "tokens_before": context_tokens_before,
                        "tokens_after": context_tokens_before,
                        "context_tokens_before": context_tokens_before,
                        "context_tokens_after": context_tokens_before,
                        "request_tokens_before": request_tokens_before,
                        "request_tokens_after": request_tokens_before,
                        "last_provider_context_tokens": provider_context_before,
                        "transcript_tokens_before": transcript_tokens_before,
                        "transcript_tokens_after": result.tokens_after,
                        "chatVisible": True,
                        **before_snapshot,
                    },
                }
            )
            return

        self.session.compaction_count += 1
        try:
            request_tokens_after = int(self.session.estimate_tokens())
        except Exception:  # noqa: BLE001
            request_tokens_after = result.tokens_after
        try:
            transcript_tokens_after = int(self.session.transcript.estimate_tokens())
        except Exception:  # noqa: BLE001
            transcript_tokens_after = result.tokens_after
        context_tokens_after = request_tokens_after
        after_snapshot = self._write_compaction_snapshot(
            phase="after",
            compactor=compactor,
            request_tokens=request_tokens_after,
            provider_context_tokens=context_tokens_after,
        )
        self._mark_usage_compacted(context_tokens_after)
        self._save_transcript()
        emit(
            {
                "type": "system_event",
                "sessionId": self.id,
                "subtype": "compaction_complete",
                "message": (
                    f"Manual compaction complete: "
                    f"{context_tokens_before:,} -> {context_tokens_after:,} context tokens; "
                    f"{result.entries_removed} entries summarized"
                ),
                "details": {
                    "trigger": "manual",
                    "strategy": "llm_summary",
                    "tokens_before": context_tokens_before,
                    "tokens_after": context_tokens_after,
                    "context_tokens_before": context_tokens_before,
                    "context_tokens_after": context_tokens_after,
                    "request_tokens_before": request_tokens_before,
                    "request_tokens_after": request_tokens_after,
                    "last_provider_context_tokens": provider_context_before,
                    "transcript_tokens_before": transcript_tokens_before,
                    "transcript_tokens_after": transcript_tokens_after,
                    "entries_removed": result.entries_removed,
                    "messages_before": result.messages_before,
                    "messages_after": result.messages_after,
                    "images_before": result.images_before,
                    "images_after": result.images_after,
                    "summary_chars": len(result.summary or ""),
                    "summary_preview": (result.summary or "")[:6_000],
                    "chatVisible": True,
                    **before_snapshot,
                    **after_snapshot,
                },
            }
        )
        _cum_in, cum_out, _cum_cr, _cum_cw, cost = self._current_usage_fields()
        emit(
            {
                "type": "usage_snapshot",
                "sessionId": self.id,
                "inputTokens": context_tokens_after,
                "outputTokens": cum_out,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "cost": cost,
            }
        )

    def _refresh_knowledge_context(self, query: str) -> None:
        """Refresh dynamic memory/skill context for the next provider call."""
        if self.session is None or self.memory_store is None or self.skill_store is None:
            return
        try:
            from bridge.knowledge.prompt import build_knowledge_prompt

            knowledge_prompt = build_knowledge_prompt(
                memory_store=self.memory_store,
                skill_store=self.skill_store,
                query=query,
            )
            system_prompt = (
                self._base_system_prompt
                + ("\n\n" + knowledge_prompt if knowledge_prompt else "")
            )
            self.session.system_prompt = system_prompt
            self._system_prompt = system_prompt
            emit(
                {
                    "type": "system_event",
                    "sessionId": self.id,
                    "subtype": "knowledge_context_built",
                    "message": "Knowledge context refreshed",
                    "details": {
                        "memoryCount": len(self.memory_store.relevant(query, limit=8)),
                        "skillCount": len(self.skill_store.search(query, limit=12))
                        if query.strip()
                        else len(self.skill_store.list_skills()[:12]),
                    },
                }
            )
            for item in self.memory_store.relevant(query, limit=8):
                emit(
                    {
                        "type": "memory_retrieved",
                        "sessionId": self.id,
                        "memory": item.to_event(),
                        "reason": "turn context",
                    }
                )
            skill_matches = (
                self.skill_store.search(query, limit=12)
                if query.strip()
                else [(s, 0, "available") for s in self.skill_store.list_skills()[:12]]
            )
            for skill, _score, reason in skill_matches:
                emit(
                    {
                        "type": "skill_retrieved",
                        "sessionId": self.id,
                        "skill": skill.to_event(),
                        "reason": reason or "turn context",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log("warn", f"knowledge context refresh failed: {exc}")

    def _record_loaded_skill(self, skill: Any) -> None:
        name = getattr(skill, "name", "")
        if not name:
            return
        instructions = getattr(skill, "instructions", "") or ""
        token_count = max(1, int((len(instructions) + len(name)) / 4))
        self.loaded_skills[name] = {
            "turn": self.turn_counter,
            "tokens": token_count,
            "skill_type": getattr(skill, "skill_type", "build") or "build",
            "tool_call_id": self.current_tool_id,
            "skill": skill,
        }
        self.skill_maintenance_done = False

    def _loaded_skill_tokens(self) -> int:
        return sum(int(info.get("tokens") or 0) for info in self.loaded_skills.values())

    def _should_run_skill_maintenance(self, session_input_tokens: int) -> bool:
        if self.skill_maintenance_done:
            return False
        if len(self.loaded_skills) < SKILL_PRUNE_MIN_SKILLS:
            return False
        if self._loaded_skill_tokens() < SKILL_PRUNE_MIN_SKILL_TOKENS:
            return False
        if session_input_tokens < SKILL_PRUNE_SESSION_TOKEN_THRESHOLD:
            return False
        return True

    async def _run_skill_maintenance(self, session_input_tokens: int) -> None:
        if (
            self.session is None
            or self.runner is None
            or self.skill_store is None
            or not self._should_run_skill_maintenance(session_input_tokens)
        ):
            return

        loaded = dict(self.loaded_skills)
        inventory = "\n".join(
            f"- {name} (loaded turn {info.get('turn')}, ~{info.get('tokens')} tokens, type: {info.get('skill_type')})"
            for name, info in sorted(loaded.items())
        )
        total_tokens = self._loaded_skill_tokens()
        maintenance_msg = (
            "Review the skills currently loaded in your context. For each loaded skill, "
            "decide whether to KEEP it (still needed for the current task) or PRUNE it "
            "(no longer needed). Return one decision for every loaded skill.\n\n"
            f"Currently loaded skills:\n{inventory}\n\n"
            f"Total: {total_tokens} tokens in loaded skills."
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["decisions"],
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["skill_name", "action", "reason"],
                        "properties": {
                            "skill_name": {"type": "string"},
                            "action": {"type": "string", "enum": ["keep", "prune"]},
                            "reason": {
                                "type": "string",
                                "enum": [
                                    "actively_using",
                                    "needed_soon",
                                    "task_completed",
                                    "never_relevant",
                                    "superseded",
                                    "low_value",
                                    "causing_confusion",
                                ],
                            },
                        },
                    },
                }
            },
        }

        emit(
            {
                "type": "system_event",
                "sessionId": self.id,
                "subtype": "skill_maintenance_start",
                "message": f"Reviewing {len(loaded)} loaded skills for pruning",
                "details": {"skillCount": len(loaded), "skillTokens": total_tokens},
            }
        )

        try:
            from engine.types import Message, ThinkingConfig

            provider = (
                self.runner.fallback_chain.current
                if getattr(self.runner, "fallback_chain", None)
                else self.runner.provider
            )
            messages = self.session.get_messages()
            messages.append(Message(role="user", content=maintenance_msg))
            result = await provider.complete_structured(
                messages=messages,
                schema=schema,
                schema_name="review_skills",
                schema_description="Review loaded skills for pruning",
                system_prompt=self.session.system_prompt,
                max_tokens=SKILL_MAINTENANCE_MAX_TOKENS,
                strict=True,
                thinking=ThinkingConfig(enabled=False),
            )
            decisions = result.data.get("decisions", []) if result.success else []
        except Exception as exc:  # noqa: BLE001
            log("warn", f"skill maintenance failed: {exc}")
            return

        if not decisions:
            self.skill_maintenance_done = True
            return

        pruned_names = {
            str(d.get("skill_name") or "")
            for d in decisions
            if d.get("action") == "prune" and str(d.get("skill_name") or "") in loaded
        }
        stubs = self._prune_skill_results(pruned_names, decisions)
        for decision in decisions:
            name = str(decision.get("skill_name") or "")
            info = loaded.get(name)
            if not name or info is None:
                continue
            skill_type = str(info.get("skill_type") or "build")
            self.skill_store.record_review_decision(
                name=name,
                skill_type=skill_type,
                action=str(decision.get("action") or ""),
                reason=str(decision.get("reason") or ""),
            )

        for name in pruned_names:
            info = self.loaded_skills.pop(name, None)
            skill = info.get("skill") if info else None
            if skill is not None:
                emit(
                    {
                        "type": "skill_pruned",
                        "sessionId": self.id,
                        "skill": skill.to_event(),
                        "reason": next(
                            (
                                str(d.get("reason") or "")
                                for d in decisions
                                if d.get("skill_name") == name
                            ),
                            "pruned",
                        ),
                    }
                )

        self.skill_maintenance_done = True
        tokens_freed = sum(int(loaded[n].get("tokens") or 0) for n in pruned_names if n in loaded)
        emit(
            {
                "type": "system_event",
                "sessionId": self.id,
                "subtype": "skill_maintenance_complete",
                "message": f"Pruned {len(pruned_names)} skill(s), freed ~{tokens_freed} tokens",
                "details": {
                    "pruned": sorted(pruned_names),
                    "tokensFreed": tokens_freed,
                },
            }
        )

    def _prune_skill_results(
        self,
        pruned_names: set[str],
        decisions: list[dict[str, Any]],
    ) -> dict[str, str]:
        if self.session is None or not pruned_names:
            return {}
        reason_map = {
            str(d.get("skill_name") or ""): str(d.get("reason") or "pruned")
            for d in decisions
            if d.get("action") == "prune"
        }
        stubs: dict[str, str] = {}
        for entry in getattr(self.session.transcript, "_entries", []):
            msg = getattr(entry, "message", None)
            if msg is None or getattr(msg, "role", None) != "tool_result":
                continue
            content = getattr(msg, "content", "")
            if not isinstance(content, str) or not content.startswith("[Skill: "):
                continue
            header_end = content.find("]")
            if header_end < 0:
                continue
            header = content[len("[Skill: ") : header_end]
            skill_name = header.split("|")[0].strip()
            if skill_name not in pruned_names:
                continue
            reason = reason_map.get(skill_name, "pruned")
            stub = (
                f"[Skill: {skill_name} - PRUNED ({reason}). "
                f"Call load_skill('{skill_name}') to reload if needed.]"
            )
            msg.content = stub
            stubs[skill_name] = stub
        return stubs

    async def run_turn(
        self,
        user_content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        await self.initialize()
        if self.runner is None or self.session is None:
            emit_error("runner not initialized")
            return

        # Clear the session-wide computer cancel event at the start
        # of every turn. Without this reset, a previous turn's
        # emergency stop (or any prior cancel) leaves
        # `computer_cancel` latched to True, and every subsequent
        # computer tool call returns "cancelled by emergency stop"
        # forever until the bridge restarts. We clear in place (not
        # reassign) so existing ComputerToolSpec instances still
        # hold a reference to the same Event object and observe the
        # cleared state.
        try:
            self.computer_cancel.clear()
        except Exception:  # noqa: BLE001
            pass

        # Defensive cleanup: if the PREVIOUS turn died in a way that
        # left orphan tool_use blocks in the transcript (bridge crash,
        # subprocess kill, unexpected exception), patch them here
        # before we issue another LLM call. Otherwise Anthropic
        # returns HTTP 400 and the session can't be used at all.
        if self.session is not None:
            try:
                _backfill_orphan_tool_results(self.session)
            except Exception as be:  # noqa: BLE001
                log("warn", f"pre-turn orphan backfill failed: {be}")

        self._refresh_knowledge_context(user_content)

        self.turn_counter += 1
        self.current_turn_id = f"turn-{self.turn_counter}"
        emit({"type": "turn_start", "sessionId": self.id, "turnId": self.current_turn_id})

        message: Any = _build_user_message_with_attachments(user_content, attachments)

        try:
            result = await self.runner.run(self.session, message, stream=True)
            usage = self.runner.usage
            # We emit TWO numbers the UI cares about:
            #   - `inputTokens` = CURRENT request size (what a fresh
            #     API call would cost), so the ctx meter reflects
            #     reality instead of an ever-growing cumulative sum.
            #     Uses `effective_context_tokens()` which mirrors
            #     OpenClaw's "last value" pattern (last_input +
            #     last_cache_read + last_cache_write + output).
            #   - Cumulative in/out go to the cost math below so
            #     session total spend still accrues across every
            #     tool-use round trip.
            cum_in = int(getattr(usage, "input", 0) or 0)
            cum_out = int(getattr(usage, "output", 0) or 0)
            cum_cr = int(getattr(usage, "cache_read", 0) or 0)
            cum_cw = int(getattr(usage, "cache_write", 0) or 0)
            try:
                current_ctx = int(usage.effective_context_tokens())
            except Exception:  # noqa: BLE001
                current_ctx = cum_in
            # Also ground the ctx meter against the tokenizer-based
            # estimate when the accumulator hasn't seen a successful
            # API response yet (common for the very first request
            # after compaction, which hasn't reported fresh usage).
            try:
                estimated_ctx = int(self.session.estimate_tokens())
            except Exception:  # noqa: BLE001
                estimated_ctx = 0
            effective_ctx = max(current_ctx, estimated_ctx)
            cost = (cum_in * 3 + cum_out * 15) / 1_000_000
            emit(
                {
                    "type": "usage",
                    "sessionId": self.id,
                    "inputTokens": effective_ctx,
                    "outputTokens": cum_out,
                    "cacheReadTokens": cum_cr,
                    "cacheWriteTokens": cum_cw,
                    "cost": cost,
                }
            )
            await self._run_skill_maintenance(effective_ctx)
            emit(
                {
                    "type": "message_stop",
                    "sessionId": self.id,
                    "stopReason": getattr(result, "stop_reason", "end_turn"),
                }
            )
            emit(
                {
                    "type": "turn_complete",
                    "sessionId": self.id,
                    "turnId": self.current_turn_id,
                    "success": True,
                }
            )
            # Persist transcript after successful turn so session can
            # be resumed after app restart.
            self._save_transcript()
        except asyncio.CancelledError:
            # CRITICAL: backfill synthetic tool_results for any
            # tool_use blocks the runner emitted before the cancel
            # landed. Without this, the next turn's API request
            # fails with "tool_use ids were found without
            # tool_result blocks immediately after" (HTTP 400) and
            # the session is effectively bricked — the user has to
            # start fresh or manually edit the transcript.
            if self.session is not None:
                try:
                    _backfill_orphan_tool_results(self.session)
                except Exception as be:  # noqa: BLE001
                    log("warn", f"orphan backfill failed: {be}")
            emit(
                {
                    "type": "turn_complete",
                    "sessionId": self.id,
                    "turnId": self.current_turn_id,
                    "success": False,
                }
            )
            self._save_transcript()
            raise
        except Exception as exc:  # noqa: BLE001
            # Same cleanup on any non-cancel exception — the runner
            # may have added an assistant message with tool_use
            # blocks before the error propagated, and those need
            # paired results for the next turn to work.
            if self.session is not None:
                try:
                    _backfill_orphan_tool_results(self.session)
                except Exception as be:  # noqa: BLE001
                    log("warn", f"orphan backfill failed: {be}")
            emit_error(f"turn failed: {exc}", recoverable=True)
            traceback.print_exc(file=sys.stderr)
            emit(
                {
                    "type": "turn_complete",
                    "sessionId": self.id,
                    "turnId": self.current_turn_id,
                    "success": False,
                }
            )
            self._save_transcript()

    async def _on_stream(self, event: Any) -> None:
        try:
            etype = getattr(event, "type", None)
            if etype == "text_delta":
                emit(
                    {
                        "type": "text_delta",
                        "sessionId": self.id,
                        "text": getattr(event, "text", ""),
                    }
                )
            elif etype == "thinking_delta":
                emit(
                    {
                        "type": "thinking_delta",
                        "sessionId": self.id,
                        "thinking": getattr(event, "thinking", ""),
                    }
                )
            elif etype == "tool_use_start":
                tid = getattr(event, "id", "")
                name = getattr(event, "name", "")
                self.current_tool_id = tid
                self.tool_start_at[tid] = time.monotonic()
                emit(
                    {
                        "type": "tool_use_start",
                        "sessionId": self.id,
                        "id": tid,
                        "name": name,
                    }
                )
            elif etype == "tool_input_delta":
                emit(
                    {
                        "type": "tool_input_delta",
                        "sessionId": self.id,
                        "id": self.current_tool_id or "",
                        "partialJson": getattr(event, "partial_json", ""),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log("error", f"on_stream error: {exc}")

    async def _on_system_event(self, event: Any) -> None:
        try:
            emit(
                {
                    "type": "system_event",
                    "sessionId": self.id,
                    "subtype": getattr(event, "type", "unknown"),
                    "message": getattr(event, "message", ""),
                    "details": getattr(event, "details", {}) or {},
                }
            )
        except Exception as exc:  # noqa: BLE001
            log("error", f"on_system_event error: {exc}")


class _BridgeState:
    """Process-level state: workspace + session map + global policy."""

    def __init__(self, workspace: str, default_model: str) -> None:
        self.workspace = workspace
        self.default_model = default_model
        self.sessions: dict[str, _BridgeSession] = {}
        self.active_session_id: str | None = None
        # Global auto-approve tier. New sessions inherit this and live
        # sessions are updated in place on `set_permission_policy`.
        self.permission_tier: str = os.environ.get(
            "FREYJA_PERMISSION_AUTO", "low"
        )
        # Computer-use gate. Off by default — requires explicit enable
        # via the settings panel. New sessions inherit this on first
        # initialize(). Live sessions are rebuilt when this flips.
        self.computer_enabled: bool = (
            os.environ.get("FREYJA_COMPUTER_ENABLED", "").lower()
            in ("1", "true", "yes")
        )

    async def ensure_session(
        self, session_id: str, model_id: str | None = None
    ) -> _BridgeSession:
        existing = self.sessions.get(session_id)
        if existing is not None:
            if model_id and model_id != existing.model_id:
                existing.model_id = model_id
                existing.reset()
                # Re-restore from disk — the transcript was just wiped
                # by reset() but the file still has the prior state.
                await existing.try_restore_transcript()
            self.active_session_id = session_id
            return existing

        s = _BridgeSession(
            session_id,
            workspace=self.workspace,
            model_id=model_id or self.default_model,
            state=self,
        )
        self.sessions[session_id] = s
        self.active_session_id = session_id

        # Attempt transcript restoration from disk for persisted sessions.
        await s.try_restore_transcript()
        return s

    def get(self, session_id: str | None) -> _BridgeSession | None:
        if session_id:
            return self.sessions.get(session_id)
        if self.active_session_id:
            return self.sessions.get(self.active_session_id)
        return None


# ─── Command loop ──────────────────────────────────────────────────────────


async def _command_loop(state: _BridgeState) -> None:
    loop = asyncio.get_event_loop()
    # Default StreamReader limit is 64KB, which is smaller than a single
    # user message carrying a base64-encoded image attachment. Bump to
    # 32MB so image uploads (and other large inputs) don't blow up
    # `readline()` with `ValueError: Separator is not found`, which used
    # to cascade into the Python bridge exiting and the Electron main
    # process crashing with an EPIPE on the next write.
    reader = asyncio.StreamReader(limit=32 * 1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line = await reader.readline()
        except Exception as exc:
            log("error", f"stdin read error: {exc}")
            return
        if not line:
            log("info", "stdin closed — exiting")
            return
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            cmd = json.loads(text)
        except json.JSONDecodeError as exc:
            log("warn", f"invalid json command: {exc}")
            continue
        try:
            await _handle_command(state, cmd)
        except Exception as exc:
            log("error", f"command handler crashed: {exc}")
            traceback.print_exc(file=sys.stderr)


def _backfill_orphan_tool_results(session: Any) -> int:
    """Append synthetic tool_result messages for any dangling tool_use
    blocks in the transcript.

    A cancelled or crashed turn can leave the session in a state where
    the most recent assistant message contains `tool_use` blocks for
    which no `tool_result` messages ever landed (because the tool
    execution was interrupted). Anthropic's API is strict about this:
    "`tool_use` ids were found without `tool_result` blocks immediately
    after" → HTTP 400 on the NEXT turn, which bricks the whole
    conversation until the user manually discards the orphaned message.

    This helper scans the transcript for every tool_call that isn't
    followed by a matching tool_result and appends a synthetic
    "cancelled by user" tool_result for each. Idempotent — running it
    twice is a no-op on a clean transcript. Called from every cancel /
    error path in run_turn so the session can be resumed cleanly.

    Returns the number of synthetic results added (diagnostic only).
    """
    try:
        messages = session.get_messages()
    except Exception:  # noqa: BLE001
        return 0

    # Collect every tool_call id from assistant messages and every
    # tool_result id that's already been delivered. Anything in the
    # first set that isn't in the second needs backfilling. We ONLY
    # backfill orphans that live at the tail of the transcript (i.e.
    # after the last tool_result), because mid-transcript gaps should
    # never exist and patching them would hide a deeper bug.
    orphan_ids: list[str] = []
    for msg in messages:
        role = getattr(msg, "role", None)
        if role == "assistant":
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                tid = getattr(tc, "id", None)
                if tid:
                    orphan_ids.append(tid)
        elif role == "tool_result":
            tid = getattr(msg, "tool_call_id", None)
            if tid and tid in orphan_ids:
                orphan_ids.remove(tid)

    if not orphan_ids:
        return 0

    fired = 0
    for tid in orphan_ids:
        try:
            session.add_tool_result(
                tid,
                "cancelled by user (turn was interrupted before this tool finished)",
                is_error=True,
            )
            fired += 1
        except Exception as exc:  # noqa: BLE001
            log(
                "warn",
                f"failed to backfill tool_result for {tid}: {exc}",
            )
    if fired:
        log(
            "info",
            f"backfilled {fired} synthetic tool_result(s) for orphaned tool_use ids",
        )
    return fired


def _force_cancel_session(sess: "_BridgeSession") -> int:
    """Hard-cancel every in-flight operation for a session.

    Fires five signals in order of increasing bluntness so that
    whichever mechanism the running code is blocked on unwinds
    promptly:

      1. Set every running sub-agent's `cancel_event` (threading).
         The watchdog tasks in sub_agent_tool / computer_use_tool
         poll this and propagate it to their inner asyncio.Events.
      2. For sub-agents that registered `asyncio_cancel` directly
         (computer_use_tool does this), wake the asyncio.Event via
         `loop.call_soon_threadsafe(ac.set)` — zero-latency path.
      3. Set the session-wide `computer_cancel` event so any
         parent-tier computer tools that are currently mid-action
         abort at their next cancel check.
      4. Cancel `sess.pending_task` — which cascades
         `asyncio.CancelledError` into every await inside
         `run_turn`, including inside tool calls that are awaiting
         sub-agent runners.
      5. Belt-and-braces: enumerate asyncio.all_tasks() and
         directly .cancel() every task whose name matches the
         sub-agent runner naming pattern (`compuse-run-*`,
         `sub-run-*`, `compuse-watch-*`, `sub-watch-*`). This
         catches any child task that somehow escaped the
         hierarchical cancellation path (e.g. if an intermediate
         await is shielded or if the asyncio.wait wrapper isn't
         propagating).

    Returns the number of cancel signals fired (diagnostic only).
    """
    fired = 0
    if sess.subagent_registry is not None:
        for rec in sess.subagent_registry.list_all():
            if not rec.is_running:
                continue
            rec.cancel_event.set()
            fired += 1
            ac = getattr(rec, "asyncio_cancel", None)
            loop = getattr(rec, "loop", None)
            if ac is not None and loop is not None:
                try:
                    loop.call_soon_threadsafe(ac.set)
                except Exception:  # noqa: BLE001
                    pass
    if not sess.computer_cancel.is_set():
        sess.computer_cancel.set()
        fired += 1
    if sess.pending_task and not sess.pending_task.done():
        sess.pending_task.cancel()
        fired += 1

    # Direct-cancel any lingering runner / watchdog tasks by name.
    # This is the last-resort path — if everything above worked
    # these tasks are already done or about to be, and
    # cancelling them is a no-op.
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:
        tasks = set()
    for t in tasks:
        if t.done():
            continue
        name = t.get_name() or ""
        if name.startswith(("compuse-run-", "compuse-watch-", "sub-run-", "sub-watch-")):
            t.cancel()
            fired += 1
    return fired


async def _handle_command(state: _BridgeState, cmd: dict[str, Any]) -> None:
    ctype = cmd.get("type")
    session_id = cmd.get("sessionId") or state.active_session_id

    if ctype == "hello":
        return
    if ctype == "shutdown":
        log("info", "shutdown requested")
        sys.exit(0)

    if ctype == "cancel" or ctype == "force_cancel":
        sess = state.get(session_id)
        if sess:
            fired = _force_cancel_session(sess)
            log(
                "info",
                f"{ctype} fired {fired} signal(s) on session={sess.id}",
            )
            emit(
                {
                    "type": "system_event",
                    "sessionId": sess.id,
                    "subtype": "turn_cancelled",
                    "message": f"Cancelled {fired} in-flight operation(s)",
                    "details": {"fired": fired, "kind": ctype},
                }
            )
        return

    if ctype == "diagnose":
        # Dump every running asyncio task (name, current frame,
        # cancelled/done state) plus the sub-agent registry state
        # so we can see exactly where a stuck cancel is blocked.
        # This is the non-sudo equivalent of py-spy dump on the
        # bridge process itself.
        import io
        import traceback

        buf = io.StringIO()
        buf.write("=== BRIDGE DIAGNOSE ===\n")
        buf.write(f"pid: {os.getpid()}\n")
        buf.write(f"active_session: {state.active_session_id}\n")
        buf.write(f"permission_tier: {state.permission_tier}\n")
        buf.write(f"computer_enabled: {state.computer_enabled}\n\n")

        # Sub-agent registry state per session
        for sess_id, sess in state.sessions.items():
            buf.write(f"--- session {sess_id} ---\n")
            buf.write(f"  pending_task: ")
            if sess.pending_task:
                buf.write(
                    f"{sess.pending_task.get_name()} "
                    f"done={sess.pending_task.done()} "
                    f"cancelled={sess.pending_task.cancelled()}\n"
                )
            else:
                buf.write("None\n")
            buf.write(
                f"  computer_cancel.is_set={sess.computer_cancel.is_set()}\n"
            )
            if sess.subagent_registry:
                records = sess.subagent_registry.list_all()
                buf.write(f"  subagents: {len(records)}\n")
                for rec in records:
                    ac = getattr(rec, "asyncio_cancel", None)
                    buf.write(
                        f"    - id={rec.id} state={rec.state.name} "
                        f"label={rec.label!r}\n"
                    )
                    buf.write(
                        f"      cancel_event.is_set={rec.cancel_event.is_set()}\n"
                    )
                    buf.write(
                        f"      asyncio_cancel={'set=' + str(ac.is_set()) if ac else 'None'}\n"
                    )
            buf.write("\n")

        # All asyncio tasks with their current stack
        try:
            tasks = asyncio.all_tasks()
        except RuntimeError:
            tasks = set()
        buf.write(f"=== asyncio tasks: {len(tasks)} ===\n")
        for t in sorted(tasks, key=lambda x: x.get_name() or ""):
            try:
                name = t.get_name()
                done = t.done()
                cancelled = t.cancelled() if done else False
                buf.write(
                    f"\n--- task {name} done={done} cancelled={cancelled}\n"
                )
                # Current stack of the task's coroutine
                stack = t.get_stack(limit=20)
                if stack:
                    for frame in stack:
                        buf.write(
                            f"    {frame.f_code.co_filename}:"
                            f"{frame.f_lineno} in {frame.f_code.co_name}\n"
                        )
                else:
                    buf.write("    (no stack — task is done or not started)\n")
            except Exception as exc:  # noqa: BLE001
                buf.write(f"    (failed to inspect: {exc})\n")

        dump = buf.getvalue()
        # Write to a file so we don't lose it to log truncation
        try:
            dump_path = Path.home() / ".freyja" / "bridge-diagnose.txt"
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            dump_path.write_text(dump)
        except Exception:  # noqa: BLE001
            pass
        # Also log a summary line
        log(
            "info",
            f"diagnose: {len(state.sessions)} sessions, "
            f"{len(asyncio.all_tasks())} tasks, "
            f"dumped to ~/.freyja/bridge-diagnose.txt",
        )
        # And emit the full dump as a system event so the UI logs get
        # a chance to show it too.
        emit(
            {
                "type": "system_event",
                "subtype": "bridge_diagnose",
                "message": "Bridge diagnose dump",
                "details": {"dump": dump},
            }
        )
        return

    if ctype == "compact":
        sess = await state.ensure_session(
            session_id or f"desktop-{int(time.time() * 1000):x}",
            model_id=cmd.get("model"),
        )
        try:
            await sess.force_compact()
        except Exception as exc:  # noqa: BLE001
            log("warn", f"manual compaction failed: {exc}")
            emit(
                {
                    "type": "system_event",
                    "sessionId": sess.id,
                    "subtype": "compaction_skipped",
                    "message": f"Manual compaction failed: {exc}",
                    "details": {
                        "trigger": "manual",
                        "reason": str(exc),
                        "chatVisible": True,
                    },
                }
            )
        return

    if ctype == "set_model":
        new_model = cmd.get("model")
        if not new_model:
            return
        if session_id:
            sess = await state.ensure_session(session_id, model_id=new_model)
        else:
            state.default_model = new_model
            sess = None
        log("info", f"model set to {new_model} (session={session_id})")
        emit(
            {
                "type": "system_event",
                "sessionId": session_id,
                "subtype": "model_changed",
                "message": f"model changed to {new_model}",
                "details": {"model": new_model},
            }
        )
        return

    if ctype == "new_session":
        if not session_id:
            session_id = f"desktop-{int(time.time() * 1000):x}"
        model = cmd.get("model") or state.default_model
        # Drop any existing session with the same id so it really starts fresh.
        if session_id in state.sessions:
            del state.sessions[session_id]
        await state.ensure_session(session_id, model_id=model)
        log("info", f"new session {session_id} (model={model})")
        emit(
            {
                "type": "system_event",
                "sessionId": session_id,
                "subtype": "session_reset",
                "message": "Started a new session",
                "details": {"model": model},
            }
        )
        return

    if ctype == "switch_session":
        if not session_id:
            return
        sess = await state.ensure_session(
            session_id, model_id=cmd.get("model")
        )
        log("info", f"switched to session {sess.id}")
        emit(
            {
                "type": "system_event",
                "sessionId": sess.id,
                "subtype": "session_switched",
                "message": f"Switched to session {sess.id}",
                "details": {"model": sess.model_id},
            }
        )
        return

    if ctype == "restore_context":
        # Legacy fallback: renderer sends a text summary of the UI
        # conversation for sessions that predate transcript persistence.
        # Injected as a user message so the model has context for
        # follow-ups. Only effective if the session exists and has an
        # empty transcript.
        summary = cmd.get("summary", "")
        if not summary or not session_id:
            return
        sess = state.get(session_id)
        if sess is None:
            return
        await sess.initialize()
        if sess.session is None:
            return
        # Only inject if the transcript is truly empty — don't clobber
        # a restored or active transcript.
        if len(sess.session.transcript) > 0:
            return
        sess.session.add_user_message(
            f"[Previous conversation summary — this session was started "
            f"before transcript persistence was available. The summary "
            f"below was extracted from the UI message history.]\n\n"
            f"{summary}"
        )
        sess.session.add_assistant_message(
            "Understood. I have context from the previous conversation "
            "summary above. How can I help you continue?"
        )
        log(
            "info",
            f"injected legacy context summary for session {session_id} "
            f"({len(summary)} chars)",
        )
        emit(
            {
                "type": "system_event",
                "sessionId": session_id,
                "subtype": "context_restored_legacy",
                "message": f"Restored approximate context from UI history ({len(summary)} chars)",
                "details": {"summaryLength": len(summary)},
            }
        )
        return

    if ctype == "send_message":
        content = cmd.get("content", "") or ""
        attachments = cmd.get("attachments") or None
        if not content and not attachments:
            return
        sess = await state.ensure_session(
            session_id or f"desktop-{int(time.time() * 1000):x}",
            model_id=cmd.get("model"),
        )

        # If a turn is already running, QUEUE the message instead of
        # cancelling. The task runner drains the queue after each turn
        # completes, so the user's follow-up is processed as the next
        # turn without losing in-flight work (subagents, tool calls).
        if sess.pending_task and not sess.pending_task.done():
            sess.queued_messages.append((content, attachments))
            log(
                "info",
                f"queued message on session={sess.id} "
                f"(queue depth: {len(sess.queued_messages)})",
            )
            emit(
                {
                    "type": "system_event",
                    "sessionId": sess.id,
                    "subtype": "message_queued",
                    "message": f"Message queued — will send after current turn ({len(sess.queued_messages)} in queue)",
                    "details": {"queueDepth": len(sess.queued_messages)},
                }
            )
            return

        # Fire-and-forget: the command loop MUST NOT block on this
        # await. If it did, subsequent commands (including `cancel`
        # and further `send_message`s) would sit unread in stdin
        # until the current turn completed. We wrap the runner in a
        # small shim that logs termination reasons so failures don't
        # disappear silently.
        async def _run_and_log() -> None:
            try:
                await sess.run_turn(content, attachments)
            except asyncio.CancelledError:
                log("info", f"turn cancelled (session={sess.id})")
            except Exception as exc:  # noqa: BLE001
                log("error", f"turn failed (session={sess.id}): {exc}")
            # Drain the queue: process any messages the user sent while
            # this turn was running. Each queued message becomes its own
            # turn so the conversation stays well-structured.
            while sess.queued_messages:
                q_content, q_attachments = sess.queued_messages.pop(0)
                log(
                    "info",
                    f"processing queued message on session={sess.id} "
                    f"({len(sess.queued_messages)} remaining)",
                )
                try:
                    await sess.run_turn(q_content, q_attachments)
                except asyncio.CancelledError:
                    log("info", f"queued turn cancelled (session={sess.id})")
                    break
                except Exception as exc:  # noqa: BLE001
                    log("error", f"queued turn failed (session={sess.id}): {exc}")

        sess.pending_task = asyncio.create_task(
            _run_and_log(), name=f"turn-{sess.id}"
        )
        return

    if ctype == "list_tools":
        sess = state.get(session_id)
        if sess is None or sess.tool_registry is None:
            try:
                sess = await state.ensure_session(
                    session_id or f"desktop-{int(time.time() * 1000):x}"
                )
                await sess.initialize()
            except Exception as exc:  # noqa: BLE001
                log("warn", f"list_tools could not build runner: {exc}")
                return
        if sess is None or sess.tool_registry is None:
            return
        try:
            for name, tool in sorted(sess.tool_registry._tools.items()):  # noqa: SLF001
                definition = tool.definition
                emit(
                    {
                        "type": "tool_catalog_entry",
                        "sessionId": sess.id,
                        "tool": {
                            "name": name,
                            "summary": getattr(definition, "summary", ""),
                            "description": getattr(definition, "description", ""),
                            "tier": getattr(
                                getattr(definition, "tier", None), "value", "hot"
                            ),
                        },
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log("warn", f"list_tools failed: {exc}")
        return

    if ctype == "usage":
        sess = state.get(session_id)
        if sess and sess.runner is not None:
            u = sess.runner.usage
            in_tok = int(getattr(u, "input", 0) or 0)
            out_tok = int(getattr(u, "output", 0) or 0)
            cr_tok = int(getattr(u, "cache_read", 0) or 0)
            cw_tok = int(getattr(u, "cache_write", 0) or 0)
            try:
                context_tok = int(u.effective_context_tokens())
            except Exception:  # noqa: BLE001
                context_tok = in_tok
            try:
                estimate_tok = int(sess.session.estimate_tokens()) if sess.session else 0
            except Exception:  # noqa: BLE001
                estimate_tok = 0
            cost = (in_tok * 3 + out_tok * 15) / 1_000_000
            emit(
                {
                    "type": "usage_snapshot",
                    "sessionId": sess.id,
                    "inputTokens": max(context_tok, estimate_tok),
                    "outputTokens": out_tok,
                    "cacheReadTokens": cr_tok,
                    "cacheWriteTokens": cw_tok,
                    "cost": cost,
                }
            )
        return

    if ctype == "list_skills":
        try:
            sess = state.get(session_id)
            if sess is not None and sess.skill_store is not None:
                store = sess.skill_store
            else:
                from bridge.knowledge import SkillStore

                store = SkillStore(Path(state.workspace))
            for skill in store.list_skills():
                emit(
                    {
                        "type": "skill_updated",
                        "sessionId": session_id,
                        "skill": skill.to_event(),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log("warn", f"skill list failed: {exc}")
        return

    if ctype == "list_subagents":
        return

    if ctype == "permission_response":
        sess = state.get(session_id)
        if sess and sess.permission_handler:
            resolved = sess.permission_handler.resolve(
                cmd.get("requestId") or "",
                bool(cmd.get("approved")),
                cmd.get("response") or "",
            )
            if not resolved:
                log("warn", f"stale permission response: {cmd.get('requestId')}")
        return

    if ctype == "set_permission_policy":
        tier = (cmd.get("autoApprove") or "low").strip().lower()
        # Distinguish an explicit sessionId (scoped escalation) from the
        # fallback-to-active behavior of `session_id` above. Global updates
        # bypass the fallback so the SettingsModal (no sessionId) always
        # updates state.permission_tier for future sessions.
        explicit_session = cmd.get("sessionId")
        if explicit_session and explicit_session in state.sessions:
            sess = state.sessions[explicit_session]
            sess.permission_tier = tier
            if sess.permission_handler is not None:
                sess.permission_handler.set_policy(tier)
            log("info", f"session {explicit_session} policy → {tier}")
            emit(
                {
                    "type": "system_event",
                    "sessionId": explicit_session,
                    "subtype": "permission_policy_updated",
                    "message": f"permission policy → {tier}",
                    "details": {"tier": tier, "scope": "session"},
                }
            )
        else:
            # Global update: new sessions inherit this, and we also push it
            # down to every existing session so live runs pick it up.
            state.permission_tier = tier
            for sess in state.sessions.values():
                sess.permission_tier = tier
                if sess.permission_handler is not None:
                    sess.permission_handler.set_policy(tier)
            log("info", f"global permission policy → {tier}")
            emit(
                {
                    "type": "system_event",
                    "subtype": "permission_policy_updated",
                    "message": f"permission policy → {tier}",
                    "details": {"tier": tier, "scope": "global"},
                }
            )
        return

    if ctype == "list_files":
        query = (cmd.get("query") or "").strip().lower()
        limit = int(cmd.get("limit") or 40)
        matches = _search_workspace_files(Path(state.workspace), query, limit)
        emit(
            {
                "type": "file_matches",
                "sessionId": session_id,
                "query": query,
                "matches": matches,
            }
        )
        return

    if ctype == "set_computer_enabled":
        new_value = bool(cmd.get("enabled"))
        if state.computer_enabled == new_value:
            return
        state.computer_enabled = new_value
        # Rebuild every existing session so the tool registry picks up
        # (or drops) the computer tools. Safe to call reset() because
        # that only drops the runner — the transcript lives in the
        # renderer store.
        for sess in state.sessions.values():
            if sess.runner is not None:
                sess.reset()
        log(
            "info",
            f"computer control → {'enabled' if new_value else 'disabled'}",
        )
        emit(
            {
                "type": "system_event",
                "subtype": "computer_control_toggled",
                "message": (
                    "Computer control enabled"
                    if new_value
                    else "Computer control disabled"
                ),
                "details": {"enabled": new_value},
            }
        )
        return

    if ctype == "computer.emergency_stop":
        # Global scope force-cancel: same mechanism as per-session
        # cancel, applied to every session at once.
        stopped = 0
        for sess in state.sessions.values():
            stopped += _force_cancel_session(sess)
        log("warn", f"emergency stop fired — signaled {stopped} tasks")
        emit(
            {
                "type": "emergency_stop",
                "reason": cmd.get("reason") or "user",
                "stopped": stopped,
            }
        )
        return

    log("warn", f"unknown command type: {ctype}")


_FILE_IGNORE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "dist-main",
        "dist-preload",
        "dist-renderer",
        "out",
        ".next",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".playwright-mcp",
        ".claude-trace",
        ".ema-versions",
        "build",
        "target",
    }
)
_FILE_IGNORE_EXT = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".so",
        ".dylib",
        ".dll",
        ".lock",
        ".lockb",
        ".map",
        ".log",
    }
)


def _search_workspace_files(
    workspace: Path, query: str, limit: int
) -> list[dict[str, Any]]:
    """Walk the workspace returning up to `limit` matches for `query`.

    Matches by case-insensitive substring on the relative path. Returned in
    the order: exact basename match → prefix match → contains match → other.
    Walks breadth-first so top-level files appear before deeply nested ones.
    """
    query_norm = query.lower().strip()
    workspace = workspace.expanduser().resolve()
    results: list[tuple[int, str, str]] = []  # (rank, relpath, display)
    seen = 0

    def rank(rel: str, name: str) -> int:
        if not query_norm:
            return 2  # neutral
        name_l = name.lower()
        if name_l == query_norm:
            return 0
        if name_l.startswith(query_norm):
            return 1
        if query_norm in name_l:
            return 2
        if query_norm in rel.lower():
            return 3
        return 4

    for root, dirs, files in os.walk(workspace):
        # Prune ignored directories in place
        dirs[:] = [
            d for d in dirs if d not in _FILE_IGNORE_DIRS and not d.startswith(".")
        ]
        for fname in files:
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1]
            if ext in _FILE_IGNORE_EXT:
                continue
            full = Path(root) / fname
            try:
                rel = full.relative_to(workspace).as_posix()
            except ValueError:
                continue
            r = rank(rel, fname)
            if query_norm and r >= 4:
                continue
            results.append((r, rel, fname))
            seen += 1
            # Hard cap the initial walk so we don't blow past on huge repos
            if seen > 3000:
                break
        if seen > 3000:
            break

    results.sort(key=lambda x: (x[0], len(x[1]), x[1]))
    top = results[:limit]
    return [{"path": rel, "name": name} for (_, rel, name) in top]


# ─── Entrypoint ────────────────────────────────────────────────────────────


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        emit_error(f"bridge crashed: {exc}", recoverable=False)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
