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
from bridge.tools.computer_use_tool import _SCREEN_DRIVERS
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


def render_result(result: Any) -> str:
    """Summary, footer, pending action, and (for runs that did not finish cleanly) the handoff."""
    text = result.summary + "\n\n" + result.footer()
    if result.pending_action:
        text += f"\npending_action: {result.pending_action}"
    handoff = result.handoff()
    if handoff:
        text += "\n\n" + handoff
    return text


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

Choose the lightest route that works, in this order:
  1. Do it without the UI: bash, osascript, `open`, a URL, an API.
  2. `jev_computer_use` (this tool) for apps with labelled native controls:
     Finder, System Settings, Calculator, Notes, Mail, Safari pages with real
     form controls, menus and dialogs.
  3. `computer_use` (screenshot-based) for custom-drawn or canvas UIs, games,
     and apps whose accessibility tree is empty or unreadable. Use it when this
     tool returns `blocked` with a note that the tree could not be read.

Each step reads the app's accessibility tree, asks Jev (a ~200 ms decision
model) which listed control to click, type into, or which key to press, acts,
and diffs the tree to confirm the effect. An LLM is consulted only to compose
text for a field, to replan when stuck, or to verify the end state.

It runs in the BACKGROUND by default: the call returns at once with a run id,
and the result (summary plus a handoff block when it did not finish cleanly)
arrives in your inbox. While it runs it owns the screen, so do not use your own
click/type/scroll tools. Pass `wait=true` only when you must block for the
result. A returned `done` is a hint, not proof: check it against the returned
text or a screenshot before telling the person it worked.

Write the goal well. Put the person's exact wording and every literal value
(names, numbers, paths, text to type) in the goal, quoting text that must be
typed verbatim (e.g. type "hello world"). Merge consecutive micro-steps into
one goal ("open Notes, create a note titled "Plan" with the body ...") instead
of making one call per click.

Parameters:
  * `goal`: one app-scoped goal with visible success criteria
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
  * `wait`: default false. True blocks until the run ends and returns its
    result directly.

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
                    "wait": {
                        "type": "boolean",
                        "description": "Block until the run ends instead of running in the background (default false)",
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
        wait = bool(arguments.get("wait", False))

        self._counter += 1
        sub_id = f"jev_{int(time.time() * 1000):x}_{self._counter}"
        label = f"jev: {goal[:48]}{'…' if len(goal) > 48 else ''}"
        record = self._sub_spec.registry.register(
            id=sub_id, label=label, task=goal, mode="foreground" if wait else "background"
        )
        record.agent_type_name = "computer"
        if not wait:
            record.notify_parent = True
            record.parent_session_id = self._sub_spec.parent_session_id or ""
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
                "mode": record.mode,
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

        # Parent-tier computer tools refuse to act while this run holds the screen.
        _SCREEN_DRIVERS[sub_id] = label
        run_kwargs = dict(
            goal=goal,
            app=app,
            max_steps=max_steps,
            allow_irreversible=allow_irreversible,
            use_llm=use_llm,
            provider=provider,
            native=native,
        )
        if wait:
            try:
                return await self._run(call_id, record, **run_kwargs)
            finally:
                _SCREEN_DRIVERS.pop(sub_id, None)
        record.bg_task = asyncio.create_task(
            self._run_background(record, **run_kwargs), name=f"jevuse-bg-{sub_id}"
        )
        return ToolResult(
            call_id=call_id,
            content=(
                f"jev_computer_use `{label}` started in the background (id={sub_id}). "
                "It owns the screen until it finishes: don't use your own click/type/scroll "
                "tools meanwhile. The result (summary, and a handoff block if it did not "
                "finish cleanly) will arrive as a memo in your inbox; don't wait or poll."
            ),
            is_error=False,
        )

    async def _run_background(self, record: Any, **kwargs: Any) -> None:
        """Run the operator, release the screen, and memo the parent."""
        from bridge.tools.background_shell import CURRENT_TOOL_CONTEXT  # noqa: PLC0415

        CURRENT_TOOL_CONTEXT.set(None)  # not the parent's session (see sub_agent_tool)
        try:
            await self._run(None, record, **kwargs)
        except asyncio.CancelledError:
            _SCREEN_DRIVERS.pop(record.id, None)
            await self._notify_terminal(record)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("jev_computer_use run %s failed", record.id)
            if record.is_running:
                self._sub_spec.registry.mark_done(record.id, f"Error: {exc}", SubAgentState.FAILED)
        finally:
            _SCREEN_DRIVERS.pop(record.id, None)
        await self._notify_terminal(record)

    async def _notify_terminal(self, record: Any) -> None:
        cb = self._sub_spec.on_child_terminal
        if cb is None or not record.notify_parent or record.is_running:
            return
        try:
            result = cb(record)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception("on_child_terminal failed for %s", record.id)

    async def _run(
        self,
        call_id: str | None,
        record: Any,
        *,
        goal: str,
        app: str | None,
        max_steps: int,
        allow_irreversible: bool,
        use_llm: bool,
        provider: Any,
        native: Any,
    ) -> ToolResult:
        call_id = call_id or ""
        sub_id = record.id
        emit = self._sub_spec.emit_event
        asyncio_cancel = record.asyncio_cancel

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
            notes_workspace=self._sub_spec.parent_workspace or None,
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

        text = render_result(result)
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
