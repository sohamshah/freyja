"""The surface seam: how the operator reads a screen and acts on it.

`AXSurface` wraps the accessibility-tree read and the `Actuator` exactly as the
loop used them before. Later stages can supply another `Surface` instead.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Protocol, runtime_checkable

from bridge.tools.jev_operator.act import Actuator
from bridge.tools.jev_operator.observe import Observation, build_observation


@runtime_checkable
class Surface(Protocol):
    name: str

    async def observe(self, target: Any) -> Observation: ...

    async def execute(self, action: str, *args: Any, **kwargs: Any) -> Any: ...


class AXSurface:
    """Accessibility-tree surface: thread-wrapped AX read plus the Actuator."""

    name = "ax"

    def __init__(
        self,
        native: Any,
        actuator: Actuator,
        *,
        ax_depth: int,
        read_timeout_s: float,
        log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.native = native
        self.actuator = actuator
        self.ax_depth = ax_depth
        self.read_timeout_s = read_timeout_s
        self._log = log or (lambda row: None)
        self.ax_fail_streak = 0

    async def observe(self, target: Any) -> Observation:
        t0 = time.perf_counter()
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.native.read_ax_tree, target.pid, max_depth=self.ax_depth),
                timeout=self.read_timeout_s,
            )
            tree = json.loads(raw) if raw else {}
            self.ax_fail_streak = 0
            if isinstance(tree, dict) and tree.get("truncated"):
                self._log({"event": "ax_truncated", "pid": target.pid})
        except asyncio.TimeoutError:
            # The worker thread cannot be cancelled; it is abandoned. Arc's
            # tree read took 100-140 s per step on 2026-10-06 and once 2.4 h.
            self.ax_fail_streak += 1
            tree = {
                "role": "AXApplication",
                "children": [],
                "error": f"accessibility read timed out after {self.read_timeout_s:.0f}s",
            }
        except Exception as exc:  # noqa: BLE001
            self.ax_fail_streak += 1
            tree = {"role": "AXApplication", "children": [], "error": str(exc)}
        read_ms = int((time.perf_counter() - t0) * 1000)
        return build_observation(
            tree,
            app_name=target.name,
            bundle=target.bundle,
            pid=target.pid,
            read_ms=read_ms,
        )

    async def execute(self, action: str, *args: Any, **kwargs: Any) -> Any:
        """Run an Actuator action by name (click, type_into, press, scroll, ...)."""
        return await getattr(self.actuator, action)(*args, **kwargs)
