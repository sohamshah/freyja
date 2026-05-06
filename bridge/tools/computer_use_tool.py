"""
`computer_use` tool — spawn a sub-agent locked to the computer tools.

The user (or the parent agent) provides a plain-language goal; this
tool spins up a fresh sub-session with:

  * a specialized system prompt explaining the screenshot→act loop
    and when to prefer the AX tree over vision
  * a curated tool registry containing ONLY the atomic computer tools
    plus `sub_agent_result` (so the sub-agent can report back), no
    file/bash/web access at all
  * a shared `cancel_event` so the global emergency stop or UI panic
    button can abort the whole thing mid-loop
  * a ComputerToolSpec tagged with the child's session id so every
    screenshot_frame / action_planned event streams to the correct
    child slice in the renderer store

The tool returns once the sub-agent finishes (foreground default) or
immediately with an id for background mode. Either way, the child
session shows up as a first-class swarm entry in the sidebar and
attach/detach works the same as any other sub-agent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from bridge.tools.base import (
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolTier,
)
from bridge.tools.computer_tools import ComputerToolSpec, build_computer_tools
from bridge.tools.sub_agent_registry import (
    SubAgentRecord,
    SubAgentRegistry,
    SubAgentState,
)
from bridge.tools.sub_agent_tool import SubAgentSpec, _fire, _record_to_dict
from engine.compaction import SummaryCompaction

logger = logging.getLogger(__name__)


# Cap on how many steps a single computer-use sub-agent can take. Claude
# ~4.6 rarely needs more than 20-30 for real tasks; 60 gives us headroom
# for longer flows like multi-page form fills.
DEFAULT_MAX_ITERATIONS = 60

MAX_ACTIVE_COMPUTER_SESSIONS = 2  # don't drive the screen in parallel


COMPUTER_SYSTEM_PROMPT = """You are a focused computer-use sub-agent running inside
Freyja. Your job is to complete a specific task by driving the
user's macOS desktop via the atomic tools listed below.

THE LOOP

Your entire job is a loop of: observe → decide → act → observe. Use
this pattern:

  1. Call `list_windows` or `screenshot` to see the current state.
  2. Decide the single next action required to make progress.
  3. Call the atomic tool for that action (`click`, `type_text`,
     `press_key`, `scroll`, `focus_window`, ...).
  4. Each mutating tool automatically captures a fresh screenshot and
     streams it to the UI — use that new observation to plan the next
     step.
  5. When the goal is met, return a concise summary of what you did
     and what you saw. Do not loop forever.

DO NOT ASSUME SUCCESS WITHOUT VISUAL CONFIRMATION. After every
meaningful action, screenshot and *actually look* at the pixels
before deciding the action worked. Hallucinating that Spotlight
opened / Arc launched / a button was clicked is the single most
common failure mode. If the screenshot shows nothing changed,
something went wrong — do not declare victory.

ONE COORDINATE SPACE

There is a single coordinate space you work in: the pixel grid of
the most recent `screenshot` you received. Whatever width × height
that screenshot had, x/y arguments to `click`, `move_mouse`,
`scroll` are pixel positions in THAT image. `find_element`,
`cursor_position`, and `list_windows` bounds are also returned in
the same space. You do not need to think about "native resolution"
vs "preview" vs "retina scaling" — count pixels on the image,
pass those numbers, done.

NEVER PRESS ESCAPE

Do NOT call `press_key("escape")` or `key_down("escape")` as a
diagnostic or casually. The Freyja app window (the host
you are running inside) listens for ⌘Escape as its cancel
shortcut and an injected Escape can land on its own window if
it's the frontmost app, creating a self-cancel feedback loop
that hangs your current tool call. If you absolutely need to
dismiss a dialog with Escape, first `focus_window` the target
app so Freyja is not the frontmost window. Prefer
clicking the dialog's Cancel/Close button instead.

HOLD-KEY WORKFLOWS

Some macOS actions require holding a modifier across multiple
keystrokes. `press_key` only does a single down→up cycle, which
isn't enough for patterns like the ⌘Tab app switcher. Use
`key_down` + `key_up` for those:

    key_down("cmd")          # ⌘ is now held
    press_key("tab")         # app switcher appears, advances 1
    press_key("tab")         # advances 2 (⌘ still held)
    wait(ms=200)             # let the overlay paint
    screenshot()             # verify the right app is highlighted
    key_up("cmd")            # release → activates the highlighted app

CRITICAL: every `key_down` MUST be paired with a `key_up` on the
same key. A leaked held modifier will break every subsequent
keystroke on the user's system until they quit the offending app.
If anything goes wrong in the middle of a hold sequence, release
ALL held keys before doing anything else.

MULTI-MONITOR — READ THIS CAREFULLY

macOS keyboard shortcuts like ⌘Space (Spotlight), ⌘Tab (app switcher),
and ⌘` (window cycler) are NOT multi-monitor aware. They open on
the display where the mouse cursor is currently sitting, NOT the
display you're screenshotting. If you want Spotlight (or any other
keyboard-invoked overlay) to appear on a specific display, you MUST
move the mouse onto that display FIRST:

    move_mouse(x, y)     # somewhere inside the target display's bounds
    press_key("space", modifiers=["cmd"])
    wait(ms=400)         # let Spotlight paint
    screenshot(display_id=...)   # verify

Apps launched via Spotlight open on whichever display they last had
a window on — they do NOT necessarily open on your target display.
If the user asked for an app "on display N", you may need to
launch it first and then move its window to display N (this is
hard — ask the user for a different approach if Spotlight alone
doesn't suffice).

GROUNDING — PREFER THE ACCESSIBILITY TREE

Before resorting to screenshots + pixel counting, try the accessibility
tree. It's faster and gives exact element bounds:

  1. `list_windows` → find the target window, note its `pid`.
  2. `read_ax_tree(pid)` → scan the JSON for the element you want.
  3. `find_element(pid, role=..., label=...)` → get exact bounds.
  4. `click(center_x, center_y)` using the bounds center.

Only fall back to screenshot + vision when the AX tree is empty
(common in Electron/Chromium apps and web views). In that case:

  1. `screenshot()` — the PNG streams to the UI.
  2. Examine the image carefully in your reasoning.
  3. Compute target coordinates by counting pixels from reference
     points (screen edges, known UI landmarks).
  4. `click(x, y)` — watch for the post-click screenshot to confirm.

SAFETY

  * Every mutating action shows an amber highlight ring to the user
    for 200ms before it lands. The user can triple-Esc or hit the
    floating panic button to abort you instantly.
  * If you're about to do something destructive, STOP and explain in
    your response text what you're about to do — don't take the action.
    The user will re-prompt if they want it.
  * Never enter passwords, credit card numbers, or any sensitive data.
    If the task requires login, ask the user to do it and wait.
  * If you see a system dialog (password prompt, permission sheet,
    file save dialog, modal error), stop and summarize what you see
    before deciding what to do.

SUCCESS CRITERIA

When you believe the goal is achieved:
  * Take one final `screenshot` to confirm.
  * Return a structured summary: what you did, what the final state is,
    any data you extracted, and whether the task is 100% complete or
    needs a human to finish.

If you get stuck or confused (3+ failed actions in a row), stop and
explain what you see. Don't thrash.

AVAILABLE TOOLS

{tool_list}
"""


class ComputerUseTool:
    """Spawn a computer-use sub-agent."""

    def __init__(
        self,
        *,
        sub_spec: SubAgentSpec,
        enabled: bool = True,
    ) -> None:
        self._sub_spec = sub_spec
        self._enabled = enabled
        self._counter = 0

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="computer_use",
            summary="Drive the computer to complete a goal",
            tier=ToolTier.HOT,
            description="""Delegate a computer-control task to a focused sub-agent.

The sub-agent spawns with a fresh context and ONLY the atomic
computer tools (screenshot, click, type, key, scroll, list_windows,
list_displays, read_ax_tree, find_element, ...). It runs an
observe→decide→act loop until the goal is reached or the max step
cap is hit.

Use this when the task requires actually *doing* something on the
user's machine — filling out a form, navigating a native app, dragging
a file, automating a repetitive UI flow. Do NOT use it for read-only
information retrieval; use `web_search`/`web_fetch` for that.

The sub-agent appears as a first-class swarm session in the sidebar
with live screenshots in the activity panel. The user can attach
(⌘O) to watch it work or hit the floating panic button to abort.

Parameters:
  * `goal`: plain-language goal with explicit success criteria
  * `target_app`: optional bundle id or app name to focus first
  * `target_display`: optional display id (integer) to pin the
    sub-agent to a specific monitor in a multi-display setup.
    Get valid ids from `list_displays`. If omitted the sub-agent
    uses the primary display.
  * `max_steps`: cap on action count (default 60, max 200)
  * `mode`: "foreground" (block, default) or "background" (return
    immediately with a sub-agent id)
""",
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Plain-language goal with success criteria",
                    },
                    "target_app": {
                        "type": "string",
                        "description": "Optional: bundle id or app name to focus before starting",
                    },
                    "target_display": {
                        "type": "integer",
                        "description": "Optional: display id from list_displays to pin the sub-agent to a specific monitor",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": f"Hard cap on steps (default {DEFAULT_MAX_ITERATIONS}, max 200)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["foreground", "background"],
                        "description": "Execution mode (default foreground)",
                    },
                },
                "required": ["goal"],
            },
        )

    async def execute(
        self, call_id: str, arguments: dict[str, Any]
    ) -> ToolResult:
        if not self._enabled:
            return ToolResult(
                call_id=call_id,
                content=(
                    "computer_use: computer control is disabled. Enable "
                    "it in Settings → Computer Control and grant Screen "
                    "Recording + Accessibility permissions."
                ),
                is_error=True,
            )

        goal = (arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult(
                call_id=call_id,
                content="Error: `goal` is required",
                is_error=True,
            )
        target_app = (arguments.get("target_app") or "").strip() or None
        target_display = arguments.get("target_display")
        if target_display is not None:
            try:
                target_display = int(target_display)
            except (TypeError, ValueError):
                target_display = None
        max_steps = min(
            int(arguments.get("max_steps") or DEFAULT_MAX_ITERATIONS), 200
        )
        mode = arguments.get("mode") or "foreground"
        if mode not in ("foreground", "background"):
            mode = "foreground"

        running = sum(
            1
            for r in self._sub_spec.registry.list_all()
            if r.is_running and r.label.startswith("computer:")
        )
        if running >= MAX_ACTIVE_COMPUTER_SESSIONS:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: too many computer-use sessions active "
                    f"({running}/{MAX_ACTIVE_COMPUTER_SESSIONS}). Wait for "
                    "one to finish — we can't drive the screen in parallel."
                ),
                is_error=True,
            )

        self._counter += 1
        sub_id = f"comp_{int(time.time() * 1000):x}_{self._counter}"
        label = f"computer: {goal[:48]}{'…' if len(goal) > 48 else ''}"
        record = self._sub_spec.registry.register(
            id=sub_id, label=label, task=goal, mode=mode
        )
        record.agent_type_name = "computer"

        # Same dual-event emission as sub_agent_tool so the UI sees the
        # child as a real session AND as an inline subagent card.
        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "subagent_spawn",
                "record": _record_to_dict(record),
            },
        )
        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "session_spawned",
                "sessionId": sub_id,
                "parentSessionId": self._sub_spec.parent_session_id,
                "title": label,
                "model": self._sub_spec.parent_model,
                "task": goal,
                "mode": mode,
                "agentType": "computer",
                "workspace": self._sub_spec.parent_workspace,
                "createdAt": int(time.time() * 1000),
                "kind": "computer",
            },
        )
        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "computer_session_start",
                "sessionId": sub_id,
                "parentSessionId": self._sub_spec.parent_session_id,
                "goal": goal,
                "targetApp": target_app,
                "maxSteps": max_steps,
            },
        )

        if mode == "foreground":
            return await self._run_child(
                call_id,
                record,
                goal=goal,
                target_app=target_app,
                target_display=target_display,
                max_steps=max_steps,
            )

        asyncio.create_task(
            self._run_child(
                call_id=None,
                record=record,
                goal=goal,
                target_app=target_app,
                target_display=target_display,
                max_steps=max_steps,
            )
        )
        return ToolResult(
            call_id=call_id,
            content=(
                f"Computer sub-agent `{label}` queued (id={sub_id}). "
                "Use the `subagents` tool to monitor it."
            ),
            is_error=False,
        )

    async def _run_child(
        self,
        call_id: str | None,
        record: SubAgentRecord,
        *,
        goal: str,
        target_app: str | None,
        target_display: int | None,
        max_steps: int,
    ) -> ToolResult:
        from engine.runner import AsyncAgentRunner
        from engine.session import Session

        # Build a cancel bridge so the record's threading.Event can feed
        # into the tools' asyncio.Event. We register the asyncio.Event
        # on the record itself so the bridge's emergency-stop handler
        # can wake it directly via call_soon_threadsafe (zero poll
        # latency), and also poll the threading.Event as a fallback for
        # any path that only sets the thread event.
        asyncio_cancel = asyncio.Event()
        record.asyncio_cancel = asyncio_cancel
        record.loop = asyncio.get_running_loop()

        async def _cancel_bridge() -> None:
            while not asyncio_cancel.is_set():
                if record.cancel_event.is_set():
                    asyncio_cancel.set()
                    return
                await asyncio.sleep(0.1)

        bridge_task = asyncio.create_task(_cancel_bridge())

        # Fresh ComputerToolSpec scoped to the child session id.
        tool_spec = ComputerToolSpec(
            session_id=record.id,
            emit_event=self._sub_spec.emit_event,
            cancel_event=asyncio_cancel,
            enabled=True,
            require_approval=False,
            default_display_id=target_display,
            owner="computer_use",
        )

        child_tools = build_computer_tools(tool_spec)
        child_registry = ToolRegistry()
        for tool in child_tools:
            child_registry.register(tool)

        tool_list_text = "\n".join(
            f"- `{t.definition.name}` — {t.definition.summary}"
            for t in child_tools
        )
        system_prompt = COMPUTER_SYSTEM_PROMPT.format(tool_list=tool_list_text)
        if target_app:
            system_prompt += (
                f"\n\nThe user has nominated `{target_app}` as the target "
                "app. Focus it first via `focus_window` (bundle_id) before "
                "doing anything else.\n"
            )
        if target_display is not None:
            system_prompt += (
                f"\n\nThe user has pinned this session to display "
                f"{target_display}. Every call to `screenshot` will "
                f"default to this display. When clicking, only click "
                f"at coordinates that fall within display "
                f"{target_display}'s bounds (run `list_displays` once to "
                "see its origin + size). Do not touch other monitors.\n"
            )

        # Wrap with tracing so tool_result events land in the child slice.
        wrapped_registry = (
            self._sub_spec.wrap_registry(child_registry, record.id)
            if self._sub_spec.wrap_registry is not None
            else child_registry
        )

        provider = self._sub_spec.build_provider(self._sub_spec.parent_model)
        session = Session.create(
            system_prompt=system_prompt,
            tools=list(child_registry._tools.values()),  # noqa: SLF001
        )

        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "turn_start",
                "sessionId": record.id,
                "turnId": "turn-1",
            },
        )

        collected_text: list[str] = []
        tool_count = 0
        current_tool_id: dict[str, str] = {"id": ""}

        async def on_stream(event: Any) -> None:
            nonlocal tool_count
            if asyncio_cancel.is_set():
                return
            etype = getattr(event, "type", None)
            if etype == "text_delta":
                collected_text.append(getattr(event, "text", ""))
                await _fire(
                    self._sub_spec.emit_event,
                    {
                        "type": "text_delta",
                        "sessionId": record.id,
                        "text": getattr(event, "text", ""),
                    },
                )
            elif etype == "thinking_delta":
                await _fire(
                    self._sub_spec.emit_event,
                    {
                        "type": "thinking_delta",
                        "sessionId": record.id,
                        "thinking": getattr(event, "thinking", ""),
                    },
                )
            elif etype == "tool_use_start":
                tool_count += 1
                tid = getattr(event, "id", "")
                current_tool_id["id"] = tid
                await _fire(
                    self._sub_spec.emit_event,
                    {
                        "type": "tool_use_start",
                        "sessionId": record.id,
                        "id": tid,
                        "name": getattr(event, "name", ""),
                    },
                )
            elif etype == "tool_input_delta":
                await _fire(
                    self._sub_spec.emit_event,
                    {
                        "type": "tool_input_delta",
                        "sessionId": record.id,
                        "id": current_tool_id["id"],
                        "partialJson": getattr(event, "partial_json", ""),
                    },
                )

        async def on_system_event(event: Any) -> None:
            await _fire(
                self._sub_spec.emit_event,
                {
                    "type": "system_event",
                    "sessionId": record.id,
                    "subtype": getattr(event, "type", "unknown"),
                    "message": getattr(event, "message", ""),
                    "details": getattr(event, "details", {}) or {},
                },
            )

        runner = AsyncAgentRunner(
            provider=provider,
            compaction_strategy=SummaryCompaction(),
            tool_registry=wrapped_registry,
            on_stream=on_stream,
            on_system_event=on_system_event,
        )

        # Optionally focus the target app before handing off to the model.
        if target_app:
            try:
                import freyja_native as native  # noqa: PLC0415

                await asyncio.to_thread(native.focus_app, target_app)
            except Exception as exc:  # noqa: BLE001
                logger.warning("target_app focus failed: %s", exc)

        # Race runner.run against a cancel watchdog. The watchdog wakes
        # the moment asyncio_cancel is set (either by the cancel bridge
        # picking up record.cancel_event, or by a direct set from the
        # parent emergency-stop path via register_task below).
        run_task = asyncio.create_task(
            runner.run(session, goal, stream=True), name=f"compuse-run-{record.id}"
        )

        async def _watchdog() -> None:
            await asyncio_cancel.wait()

        watch_task = asyncio.create_task(
            _watchdog(), name=f"compuse-watch-{record.id}"
        )

        cancelled_by_watchdog = False
        run_exception: BaseException | None = None
        result = None
        try:
            done, pending = await asyncio.wait(
                {run_task, watch_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if watch_task in done and run_task not in done:
                # Watchdog fired first — cancel the runner task and
                # wait for it to actually unwind so we don't leak
                # half-finished tool calls.
                cancelled_by_watchdog = True
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            else:
                # Runner completed first (success or error). Ensure
                # the watchdog task is cleaned up.
                watch_task.cancel()
                try:
                    await watch_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                # Collect the runner's result or exception.
                try:
                    result = run_task.result()
                except asyncio.CancelledError:
                    cancelled_by_watchdog = True
                except BaseException as exc:  # noqa: BLE001
                    run_exception = exc
        except asyncio.CancelledError:
            # We were cancelled from the OUTSIDE (parent turn cancelled
            # via pending_task.cancel()). Propagate after cleanup.
            cancelled_by_watchdog = True
            run_task.cancel()
            watch_task.cancel()
            try:
                await run_task
            except BaseException:  # noqa: BLE001
                pass
            try:
                await watch_task
            except BaseException:  # noqa: BLE001
                pass
            bridge_task.cancel()
            self._sub_spec.registry.mark_done(
                record.id, "Cancelled", SubAgentState.CANCELLED
            )
            await self._emit_completion(record, cancelled=True)
            raise

        # Clean up the bridge task now that the race is over.
        bridge_task.cancel()
        try:
            await bridge_task
        except BaseException:  # noqa: BLE001
            pass

        if cancelled_by_watchdog:
            self._sub_spec.registry.mark_done(
                record.id, "Cancelled", SubAgentState.CANCELLED
            )
            await self._emit_completion(record, cancelled=True)
            return ToolResult(
                call_id=call_id or "",
                content="computer_use: cancelled by emergency stop",
                is_error=True,
            )

        if run_exception is not None:
            logger.exception(
                "computer_use sub-agent failed", exc_info=run_exception
            )
            self._sub_spec.registry.mark_done(
                record.id, f"Error: {run_exception}", SubAgentState.FAILED
            )
            await self._emit_completion(record, error=str(run_exception))
            return ToolResult(
                call_id=call_id or "",
                content=f"computer_use failed: {run_exception}",
                is_error=True,
            )

        # Success path.
        usage = runner.usage
        record.input_tokens = int(getattr(usage, "input", 0) or 0)
        record.output_tokens = int(getattr(usage, "output", 0) or 0)
        record.tools_called = tool_count
        record.iterations = int(getattr(result, "iterations", 0) or 0) if result else 0

        text = "".join(collected_text).strip() or "(no output)"
        self._sub_spec.registry.mark_done(
            record.id,
            text,
            SubAgentState.DONE,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            iterations=record.iterations,
            tools_called=record.tools_called,
        )
        await self._emit_completion(record)
        return ToolResult(
            call_id=call_id or "",
            content=text,
            is_error=False,
        )

    async def _emit_completion(
        self,
        record: SubAgentRecord,
        *,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None:
        usage_payload = {
            "type": "usage",
            "sessionId": record.id,
            "inputTokens": record.input_tokens,
            "outputTokens": record.output_tokens,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "cost": (record.input_tokens * 3 + record.output_tokens * 15)
            / 1_000_000,
        }
        await _fire(self._sub_spec.emit_event, usage_payload)
        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "turn_complete",
                "sessionId": record.id,
                "turnId": "turn-1",
                "success": not (cancelled or error),
            },
        )
        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "session_completed",
                "sessionId": record.id,
                "success": not (cancelled or error),
                "elapsedMs": int(record.elapsed * 1000),
                "inputTokens": record.input_tokens,
                "outputTokens": record.output_tokens,
                "toolsCalled": record.tools_called,
            },
        )
        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "computer_session_end",
                "sessionId": record.id,
                "outcome": "cancelled"
                if cancelled
                else ("failed" if error else "done"),
                "summary": str(record.result or "")[:400],
            },
        )
        # Also mirror the existing subagent_done event so the inline card
        # updates.
        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "subagent_done",
                "id": record.id,
                "result": str(record.result or ""),
                "elapsedMs": int(record.elapsed * 1000),
            },
        )
        await _fire(
            self._sub_spec.emit_event,
            {
                "type": "subagent_update",
                "id": record.id,
                "patch": _record_to_dict(record),
            },
        )
