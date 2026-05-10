"""
Thread-safe central registry for sub-agent lifecycle state.

Tracks all sub-agents (foreground and background), providing:
- Registration and state tracking
- Efficient blocking via per-agent done_event (no polling)
- Background result delivery queue
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
    mode: str                        # "foreground" | "background"
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
    it. See the `subagents` tool docstring for the rationale.
    """

    def __init__(self) -> None:
        self._records: dict[str, SubAgentRecord] = {}
        self._lock = threading.Lock()
        self._undelivered_bg: list[str] = []  # IDs of completed bg agents not yet delivered

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
        Appends to _undelivered_bg if it was a background agent.

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
            if record.mode == "background":
                self._undelivered_bg.append(id)

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

        Blocks indefinitely — sub-agents do not time out. Use the
        async `SubAgentsTool.execute` wrapper for a cancellable wait
        that cooperates with the asyncio event loop.
        """
        with self._lock:
            record = self._records.get(id)
        if record is None:
            return None
        record.done_event.wait()
        return record

    def wait_all(self) -> list[SubAgentRecord]:
        """
        Block until every currently-running background agent reaches
        a terminal state. Returns the snapshot list of agents that
        were running when wait_all started.

        Blocks indefinitely. See the async wrapper in `SubAgentsTool`
        for cancellation semantics.
        """
        with self._lock:
            bg_records = [
                r for r in self._records.values()
                if r.mode == "background" and r.is_running
            ]
        for record in bg_records:
            record.done_event.wait()
        return bg_records

    def mark_delivered(self, id: str) -> None:
        """Remove an agent from the undelivered queue.

        Call this after explicitly delivering a result via wait/wait_all
        so that pop_completed_background() doesn't auto-inject it again.
        """
        with self._lock:
            try:
                self._undelivered_bg.remove(id)
            except ValueError:
                pass

    def pop_completed_background(self) -> list[SubAgentRecord]:
        """Return and clear undelivered completed background agent records."""
        with self._lock:
            if not self._undelivered_bg:
                return []
            records = [
                self._records[id]
                for id in self._undelivered_bg
                if id in self._records
            ]
            self._undelivered_bg.clear()
            return records

    def has_running_background(self) -> bool:
        """Check if any background agents are still running."""
        with self._lock:
            return any(
                r.mode == "background" and r.is_running
                for r in self._records.values()
            )
