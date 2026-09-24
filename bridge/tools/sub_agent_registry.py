"""
Thread-safe central registry for sub-agent lifecycle state.

Tracks every sub-agent a session spawns, providing:
- Registration and state tracking
- Efficient blocking via per-agent done_event (no polling) — used by bridge
  infrastructure (kanban judge lane), never by the model: agent-spawned
  children run in the background and report back with an inbox memo
  (see SubAgentTool._deliver_memo).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class SubAgentState(Enum):
    """Lifecycle state of a sub-agent."""
    RUNNING = auto()
    DONE = auto()
    FAILED = auto()
    CANCELLED = auto()


@dataclass
class SubAgentRecord:
    """Complete record for a single sub-agent execution."""

    id: str                          # "sub_1", "sub_2"
    label: str
    task: str
    mode: str                        # "background" for agent spawns; "foreground" only for bridge-internal blocking spawns
    state: SubAgentState = SubAgentState.RUNNING
    result: Any | None = None        # ToolResult when done
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: int = 0
    iterations: int = 0
    tools_called: int = 0
    agent_type_name: str = "general"
    artifact_path: str | None = None
    created_files: list[str] = field(default_factory=list)
    # Optional direct-cancel hook used by the computer_use tool: the
    # child session registers its asyncio.Event here, and the
    # emergency-stop command handler can wake it instantly (zero poll
    # latency) instead of relying on the threading.Event + 100ms poll
    # bridge. Typed as Any to avoid a hard asyncio import.
    asyncio_cancel: Any = None
    loop: Any = None  # the event loop owning asyncio_cancel, for thread-safe set
    # Per-session inbox for inter-agent + operator-to-agent talk.
    # Attached at spawn time inside SubAgentTool._run_child; drained by
    # the runner's pre-iteration hook. Typed as Any to keep the
    # registry module dependency-free.
    inbox: Any = None
    # Parent session id stashed here so TalkRouter can resolve the
    # "parent" alias for sibling-to-sibling discovery without walking
    # registries. Set by the spawn path.
    parent_session_id: str = ""
    # Final transcript snapshot. Populated by `_run_child` right before
    # the run returns; consumed by callers that need to chain a follow-up
    # LLM call against the same conversational context (e.g. the deep
    # judge's structured-output synthesis pass). Typed as Any so this
    # module doesn't pull in engine.types. Empty list = subagent was
    # cancelled / errored before populating.
    final_messages: Any = field(default_factory=list)
    final_system_prompt: str = ""
    final_model_id: str = ""
    # Send the parent a memo when this child reaches a terminal state.
    # Set for children the parent's MODEL spawned (sub_agent,
    # computer_use, archived re-wakes); bridge-internal spawns (judges,
    # drafters, kanban workers) report through their own channels.
    notify_parent: bool = False
    # Who stopped it, when it was stopped: "parent" (its own subagents
    # kill — no memo, the parent knows), "operator" (session stop — memo
    # queued without waking), or "" (not cancelled / external).
    cancel_origin: str = ""
    # The asyncio task running this child in the background. Held here so
    # it can't be garbage-collected mid-run (asyncio keeps only weak refs).
    bg_task: Any = None

    @property
    def elapsed(self) -> float:
        if self.end_time is not None:
            return self.end_time - self.start_time
        return time.time() - self.start_time

    @property
    def is_running(self) -> bool:
        return self.state == SubAgentState.RUNNING


class SubAgentRegistry:
    """
    Thread-safe central tracker for all sub-agent lifecycle state.

    Each record has a `done_event: threading.Event` for efficient blocking
    (no polling). `wait()` calls `record.done_event.wait()` — there is
    NO timeout on wait. Sub-agents reach a terminal state exactly once
    (done / failed / cancelled) and the event fires at that point; the
    only way to "abandon" a running sub-agent is to explicitly `kill`
    it.
    """

    def __init__(self) -> None:
        self._records: dict[str, SubAgentRecord] = {}
        self._lock = threading.Lock()

    def register(
        self,
        id: str,
        label: str,
        task: str,
        mode: str,
    ) -> SubAgentRecord:
        """Register a new sub-agent. Returns the created record."""
        record = SubAgentRecord(
            id=id,
            label=label,
            task=task,
            mode=mode,
        )
        with self._lock:
            self._records[id] = record
        return record

    def get(self, id: str) -> SubAgentRecord | None:
        """Get a record by ID."""
        with self._lock:
            return self._records.get(id)

    def list_all(self) -> list[SubAgentRecord]:
        """Return a snapshot of all records."""
        with self._lock:
            return list(self._records.values())

    def mark_done(
        self,
        id: str,
        result: Any,
        state: SubAgentState,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        iterations: int | None = None,
        tools_called: int | None = None,
    ) -> None:
        """
        Mark a sub-agent as complete. Sets done_event so waiters unblock.

        Stats fields (input_tokens, output_tokens, etc.) default to None,
        meaning "preserve the existing value on the record". This avoids
        clobbering live stats set by the on_result callback during execution.
        Pass explicit values to override.
        """
        with self._lock:
            record = self._records.get(id)
            if record is None:
                return
            record.state = state
            record.result = result
            record.end_time = time.time()
            if input_tokens is not None:
                record.input_tokens = input_tokens
            if output_tokens is not None:
                record.output_tokens = output_tokens
            if iterations is not None:
                record.iterations = iterations
            if tools_called is not None:
                record.tools_called = tools_called
            record.done_event.set()

    def kill(self, id: str) -> bool:
        """
        Signal a sub-agent to cancel. Returns True if the agent was running.
        The sub-agent's _poll_cancel detects the event and aborts.
        """
        with self._lock:
            record = self._records.get(id)
            if record is None:
                return False
            if not record.is_running:
                return False
            record.cancel_event.set()
            return True

    def wait(self, id: str) -> SubAgentRecord | None:
        """
        Block until the specified agent reaches a terminal state.
        Returns the record, or None if the agent doesn't exist.

        Blocks indefinitely — sub-agents do not time out. Bridge
        infrastructure only (run it via asyncio.to_thread); the model
        never blocks on a child.
        """
        with self._lock:
            record = self._records.get(id)
        if record is None:
            return None
        record.done_event.wait()
        return record

    def adopt(self, record: SubAgentRecord) -> None:
        """Track an existing record (a child still running when its
        session's runner was rebuilt — see _BridgeSession.reset)."""
        with self._lock:
            self._records[record.id] = record

    def running(self) -> list[SubAgentRecord]:
        """Snapshot of the sub-agents still running."""
        with self._lock:
            return [r for r in self._records.values() if r.is_running]
