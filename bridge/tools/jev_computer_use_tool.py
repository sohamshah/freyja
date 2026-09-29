"""
`jev_computer_use` tool: drive a macOS app with the Jev decision model.

Unlike `computer_use`, which spawns an LLM sub-agent that reads
screenshots and calls the atomic tools one by one, this tool runs the
observe → decide → act loop in code (bridge/tools/jev_operator) and asks
Jev one batched question set per step. An LLM is called only through three
doors: composing text for a field, replanning when the loop is stuck or the
accessibility tree does not describe the screen, and judging the end state.
Typical steps take ~0.5 s instead of several seconds. See docs/jev-operator.md.

The run is registered as a child session so its frames and actions show
up in the swarm UI exactly like a `computer_use` run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from bridge.decisions.provider import TypeSafeProvider
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier
from bridge.tools.computer_tools import ComputerToolSpec
from bridge.tools.jev_operator.handoff import (
    LLMHelper,
    completer_from_provider,
    default_llm_model,
)
from bridge.tools.jev_operator.loop import Operator, OperatorConfig
from bridge.tools.sub_agent_registry import SubAgentState
from bridge.tools.sub_agent_tool import SubAgentSpec, _fire, _record_to_dict

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 40
# A run needs its app frontmost and owns the single keyboard and mouse, so it
# cannot share the screen with another computer-use session.
MAX_ACTIVE_COMPUTER_SESSIONS = 1


class JevComputerUseTool:
    def __init__(
        self, *, sub_spec: SubAgentSpec, enabled: bool = True, llm_model: str | None = None
    ) -> None:
        self._sub_spec = sub_spec
        self._enabled = enabled
        self._llm_model = llm_model
        self._counter = 0

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="jev_computer_use",
            summary="Drive a macOS app quickly via the accessibility tree and the Jev decision model",
            tier=ToolTier.HOT,
            description="""Complete a goal in one macOS application using a fast decision-model loop.

Each step reads the app's accessibility tree, asks Jev (a ~200 ms decision
model) which listed control to click, type into, or which key to press, acts,
and diffs the tree to confirm the effect. An LLM is consulted only to compose
text for a field, to replan when stuck, or to verify the end state, so typical
steps take well under a second. Prefer this over `computer_use` for tasks in
native apps with labelled controls (Finder, System Settings, Calculator,
Notes, Mail, Safari pages with real form controls, menus and dialogs). Fall
back to `computer_use` for custom-drawn or canvas UIs, games, or when this
tool returns `blocked` with a note that the accessibility tree was empty.

Parameters:
  * `goal`: one app-scoped goal with visible success criteria. Quote any text
    that must be typed verbatim (e.g. type "hello world").
  * `app`: bundle id or app name to operate on (launched if not running).
    Pass it. Without it the run uses the frontmost app, and stops if that is
    Freyja itself.
  * `max_steps`: cap on decision steps (default 40, max 120)
  * `allow_irreversible`: default false. Buttons, menu items, and links whose
    label reads delete, send, submit, pay, empty trash, etc. stop the run with
    status=needs_confirmation; re-run with true after the user confirms. True
    permits every such control for that whole run, not only the one reported.
  * `use_llm`: default true. Set false for a pure Jev run (no field text
    composition, no replanning, no end-state check, template summary).

Input is only sent while the target app is frontmost and owns the window under
the pointer. If another app takes focus or covers the control, the run stops
with status=blocked instead of clicking into the other app.
""",
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "App-scoped goal with visible success criteria",
                    },
                    "app": {
                        "type": "string",
                        "description": "Bundle id or app name; omit only to use the frontmost app",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": f"Cap on decision steps (default {DEFAULT_MAX_STEPS}, max 120)",
                    },
                    "allow_irreversible": {
                        "type": "boolean",
                        "description": "Permit controls that look irreversible (default false)",
                    },
                    "use_llm": {
                        "type": "boolean",
                        "description": "Allow LLM handoff doors (default true)",
                    },
                },
                "required": ["goal"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._enabled:
            return ToolResult(
                call_id=call_id,
                content="jev_computer_use: computer control is disabled in Settings.",
                is_error=True,
            )
        goal = (arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult(call_id=call_id, content="Error: `goal` is required", is_error=True)
        provider = TypeSafeProvider()
        if not provider.available:
            return ToolResult(
                call_id=call_id,
                content="jev_computer_use: TYPESAFE_AI_API_KEY is not set; use `computer_use` instead.",
                is_error=True,
            )
        try:
            import freyja_native as native  # noqa: PLC0415
        except ImportError as exc:
            return ToolResult(call_id=call_id, content=f"jev_computer_use: {exc}", is_error=True)

        running = sum(
            1
            for r in self._sub_spec.registry.list_all()
            if r.is_running and r.label.startswith(("computer:", "jev:"))
        )
        if running >= MAX_ACTIVE_COMPUTER_SESSIONS:
            return ToolResult(
                call_id=call_id,
                content="Error: another computer-use session is active; "
                "the screen cannot be driven in parallel.",
                is_error=True,
            )

        app = (arguments.get("app") or "").strip() or None
        try:
            max_steps = int(arguments.get("max_steps") or DEFAULT_MAX_STEPS)
        except (TypeError, ValueError):
            return ToolResult(
                call_id=call_id, content="Error: `max_steps` must be an integer", is_error=True
            )
        max_steps = max(1, min(max_steps, 120))
        allow_irreversible = bool(arguments.get("allow_irreversible", False))
        use_llm = bool(arguments.get("use_llm", True))

        self._counter += 1
        sub_id = f"jev_{int(time.time() * 1000):x}_{self._counter}"
        label = f"jev: {goal[:48]}{'…' if len(goal) > 48 else ''}"
        record = self._sub_spec.registry.register(
            id=sub_id, label=label, task=goal, mode="foreground"
        )
        record.agent_type_name = "computer"
        asyncio_cancel = asyncio.Event()
        record.asyncio_cancel = asyncio_cancel
        record.loop = asyncio.get_running_loop()

        emit = self._sub_spec.emit_event
        await _fire(emit, {"type": "subagent_spawn", "record": _record_to_dict(record)})
        await _fire(
            emit,
            {
                "type": "session_spawned",
                "sessionId": sub_id,
                "parentSessionId": self._sub_spec.parent_session_id,
                "title": label,
                "model": "jev-1.13.0",
                "reasoningLevel": "off",
                "task": goal,
                "mode": "foreground",
                "agentType": "computer",
                "workspace": self._sub_spec.parent_workspace,
                "createdAt": int(time.time() * 1000),
                "kind": "computer",
            },
        )
        await _fire(
            emit,
            {
                "type": "computer_session_start",
                "sessionId": sub_id,
                "parentSessionId": self._sub_spec.parent_session_id,
                "goal": goal,
                "targetApp": app,
                "maxSteps": max_steps,
            },
        )
        await _fire(emit, {"type": "turn_start", "sessionId": sub_id, "turnId": "turn-1"})

        async def bridge_cancel() -> None:
            while not asyncio_cancel.is_set():
                if record.cancel_event.is_set():
                    asyncio_cancel.set()
                    return
                await asyncio.sleep(0.1)

        bridge_task = asyncio.create_task(bridge_cancel())

        spec = ComputerToolSpec(
            session_id=sub_id,
            emit_event=emit,
            cancel_event=asyncio_cancel,
            enabled=True,
            owner="jev_computer_use",
        )

        llm: LLMHelper | None = None
        if use_llm:
            try:
                model = self._llm_model or default_llm_model()
                llm = LLMHelper(
                    completer_from_provider(self._sub_spec.build_provider(model, "off"))
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("jev_computer_use: LLM helper unavailable: %s", exc)

        async def say(text: str) -> None:
            await _fire(emit, {"type": "text_delta", "sessionId": sub_id, "text": text + "\n"})

        cfg = OperatorConfig(
            goal=goal,
            app=app,
            max_steps=max_steps,
            allow_irreversible=allow_irreversible,
            use_llm=llm is not None,
        )
        op = Operator(cfg, provider=provider, spec=spec, native=native, llm=llm, on_step=say)

        try:
            result = await op.run()
        except asyncio.CancelledError:
            bridge_task.cancel()
            self._sub_spec.registry.mark_done(record.id, "Cancelled", SubAgentState.CANCELLED)
            await self._emit_end(record, outcome="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            bridge_task.cancel()
            logger.exception("jev_computer_use failed")
            self._sub_spec.registry.mark_done(record.id, f"Error: {exc}", SubAgentState.FAILED)
            await self._emit_end(record, outcome="failed")
            return ToolResult(
                call_id=call_id, content=f"jev_computer_use failed: {exc}", is_error=True
            )
        bridge_task.cancel()

        text = result.summary + "\n\n" + result.footer()
        if result.pending_action:
            text += f"\npending_action: {result.pending_action}"
        # blocked / needs_confirmation / budget_exhausted are legitimate results the parent
        # acts on, so the child session is DONE; only a provider error is a failure.
        state = {
            "cancelled": SubAgentState.CANCELLED,
            "error": SubAgentState.FAILED,
        }.get(result.status, SubAgentState.DONE)
        outcome = {"done": "done", "cancelled": "cancelled", "error": "failed"}.get(
            result.status, "stuck"
        )
        record.input_tokens = result.llm_tokens[0]
        record.output_tokens = result.llm_tokens[1]
        record.tools_called = result.steps
        record.iterations = result.jev_calls
        self._sub_spec.registry.mark_done(
            record.id,
            text,
            state,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            iterations=record.iterations,
            tools_called=record.tools_called,
        )
        await self._emit_end(record, outcome=outcome)
        return ToolResult(
            call_id=call_id, content=text, is_error=result.status in ("error", "cancelled")
        )

    async def _emit_end(self, record: Any, *, outcome: str) -> None:
        emit = self._sub_spec.emit_event
        ok = outcome == "done"
        await _fire(
            emit,
            {
                "type": "usage",
                "sessionId": record.id,
                "contextTokens": record.input_tokens,
                "inputTokens": record.input_tokens,
                "outputTokens": record.output_tokens,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "cost": 0.0,
            },
        )
        await _fire(
            emit,
            {"type": "turn_complete", "sessionId": record.id, "turnId": "turn-1", "success": ok},
        )
        await _fire(
            emit,
            {
                "type": "session_completed",
                "sessionId": record.id,
                "success": ok,
                "elapsedMs": int(record.elapsed * 1000),
                "contextTokens": record.context_tokens,
                "inputTokens": record.input_tokens,
                "outputTokens": record.output_tokens,
                "toolsCalled": record.tools_called,
            },
        )
        await _fire(
            emit,
            {
                "type": "computer_session_end",
                "sessionId": record.id,
                "outcome": outcome,
                "summary": str(record.result or "")[:400],
            },
        )
        await _fire(
            emit,
            {
                "type": "subagent_done",
                "id": record.id,
                "result": str(record.result or ""),
                "elapsedMs": int(record.elapsed * 1000),
            },
        )
        await _fire(
            emit, {"type": "subagent_update", "id": record.id, "patch": _record_to_dict(record)}
        )
