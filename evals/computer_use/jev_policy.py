"""Run the real jev operator (bridge/tools/jev_operator) against a headless page.

Everything above the transport is production code: the snapshot script, the
decision questions, the gates, the LLM doors, the stop rules. Only the bottom
changes: page JavaScript goes over the DevTools protocol instead of AppleScript
to Arc, a click the planner places on a screenshot is a protocol mouse event,
and the screenshot is the page's own. No real window, pointer or keyboard is
touched, so episodes can run in parallel while the Mac is in use.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .browser import CDPError, Page, PageClosed

CHROME = "com.google.Chrome"


def _surface_class() -> type:
    """Built on first use, so importing this module does not import the bridge."""
    from bridge.tools.jev_operator.act import ActionRecord
    from bridge.tools.jev_operator.dom_surface import DOMSurface, DOMUnavailable, TabGone

    class HeadlessDOMSurface(DOMSurface):
        """jev's DOM surface over a protocol-driven page instead of AppleScript."""

        def __init__(self, page: Page, loop: asyncio.AbstractEventLoop, *, log: Any = None) -> None:
            self.page = page
            self._loop = loop
            super().__init__(bundle=CHROME, actuator=None, run_js=self._run, log=log)
            self.tab_id = page.target_id
            self.own_tab = True
            self._seen_targets = page.browser.created_count()

        # Called by DOMSurface._js in a worker thread: block on the page's loop.
        def _run(self, bundle: str, js: str) -> str:
            fut = asyncio.run_coroutine_threadsafe(
                self.page.evaluate(js, timeout=self.timeout_s), self._loop
            )
            try:
                val = fut.result(self.timeout_s + 2)
            except PageClosed as exc:
                raise TabGone("the run's tab was closed") from exc
            except CDPError as exc:
                raise DOMUnavailable(str(exc)[:300]) from exc
            except concurrent.futures.TimeoutError as exc:
                fut.cancel()
                raise DOMUnavailable(
                    f"page JavaScript timed out after {self.timeout_s:.0f}s"
                ) from exc
            return val if isinstance(val, str) else json.dumps(val)

        async def open_url(self, url: str) -> Any:
            t0 = time.perf_counter()
            try:
                await self.page.navigate(url, timeout=12.0)
            except CDPError as exc:
                return ActionRecord(
                    kind="open_url",
                    description=f"Open {url}",
                    duration_ms=_ms(t0),
                    ok=False,
                    error=str(exc),
                )
            await self.settle("open_url", min_s=0.6)
            return ActionRecord(
                kind="open_url",
                description=f"Open {url} in the run's tab",
                duration_ms=_ms(t0),
                ok=True,
            )

        async def _follow_new_tab(self) -> None:
            await asyncio.sleep(0.5)
            b = self.page.browser
            opened = b.opened_by(self.page.target_id, self._seen_targets)
            if not opened:
                return
            self._seen_targets = b.created_count()
            new = await b.attach(opened[-1]["targetId"], context=self.page.context)
            self._log(
                {"event": "dom_tab", "tab": new.target_id, "how": "the click opened a new tab"}
            )
            self.page = new
            self.tab_id = new.target_id
            await self.settle("open_url", min_s=0.3)

        async def describe_tab(self) -> str:
            try:
                return f"{await self.page.title()!r} ({self.page.url})"
            except Exception:  # noqa: BLE001
                return self.page.url

        async def execute(self, action: str, *args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            if action == "click_point":
                await self.page.mouse_click(float(args[0]), float(args[1]))
                await self.settle("click")
                return ActionRecord(
                    kind="click",
                    description=kwargs.get("description") or "click",
                    duration_ms=_ms(t0),
                    ok=True,
                )
            if action == "type_raw":
                await self.page.insert_text(str(args[0]))
                await self.settle("type")
                return ActionRecord(
                    kind="type_text", description=f"type {args[0]!r}", duration_ms=_ms(t0), ok=True
                )
            if action == "launch":
                return ActionRecord(
                    kind="launch",
                    description="launch",
                    duration_ms=0,
                    ok=False,
                    error="no apps in a headless episode",
                )
            return await super().execute(action, *args, **kwargs)

    return HeadlessDOMSurface


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


class _NoDesktop:
    """Stands in for freyja_native: the run's only window is the headless page,
    and any other desktop call fails loudly instead of reaching the real Mac."""

    def __init__(self, page: Page) -> None:
        w, h = page.browser.viewport
        self._win = SimpleNamespace(
            id=1,
            pid=os.getpid(),
            bundle=CHROME,
            title="",
            layer=0,
            bounds=SimpleNamespace(x=0, y=0, w=w, h=h),
        )

    def list_windows(self, include_helpers: bool = False) -> list[Any]:
        return [self._win]

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"freyja_native.{name} is not available in a headless episode")


@dataclass
class JevConfig:
    max_steps: int = 40
    max_runtime_s: float = 600.0
    use_llm: bool = True
    allow_irreversible: bool = False
    llm_model: str | None = None
    notes: str = ""  # jev-site notes to give the run (a mitigation under test)
    llm_max_calls: int | None = None  # None: the operator's own budget


async def run_jev(page: Page, goal: str, cfg: JevConfig, *, log_dir: Path) -> dict[str, Any]:
    """One jev run on `page`; returns the result fields the reward and report use."""
    from bridge.decisions.provider import TypeSafeProvider
    from bridge.tools.computer_tools import ComputerToolSpec
    from bridge.tools.jev_operator import loop as jev_loop
    from bridge.tools.jev_operator.handoff import (
        LLMHelper,
        completer_from_provider,
        default_llm_model,
    )
    from bridge.tools.jev_operator.loop import Operator, OperatorConfig

    jev_loop.RUN_DIR = log_dir  # run logs and last frames go with the episode, not ~/.freyja
    log_dir.mkdir(parents=True, exist_ok=True)
    provider = TypeSafeProvider()
    if not provider.available:
        raise RuntimeError("TYPESAFE_AI_API_KEY is not set")
    llm = None
    if cfg.use_llm:
        from bridge.freyja_bridge import build_provider

        llm = LLMHelper(
            completer_from_provider(build_provider(cfg.llm_model or default_llm_model(), "off"))
        )
        if cfg.llm_max_calls is not None:
            llm.max_calls = cfg.llm_max_calls
    spec = ComputerToolSpec(
        session_id="cua-episode",
        emit_event=lambda e: None,
        cancel_event=asyncio.Event(),
        enabled=True,
        owner="jev_computer_use",
    )
    steps: list[str] = []
    surface_cls = _surface_class()
    op = Operator(
        OperatorConfig(
            goal=goal,
            app=CHROME,
            max_steps=cfg.max_steps,
            allow_irreversible=cfg.allow_irreversible,
            surface="dom",
            use_llm=llm is not None,
            max_runtime_s=cfg.max_runtime_s,
        ),
        provider=provider,
        spec=spec,
        native=_NoDesktop(page),
        llm=llm,
        apps=[],
        on_step=lambda t: steps.append(t),
    )
    # Parallel episodes in one process would share a run id (second + pid) and
    # write one interleaved log; give each its own.
    op.run_id = f"{op.run_id}-{os.urandom(2).hex()}"
    op.log_path = log_dir / f"{op.run_id}.jsonl"
    surface = surface_cls(page, asyncio.get_running_loop(), log=op._log)
    surface.may_confirm = cfg.allow_irreversible
    op.surface = surface
    op._surface_injected = True
    if cfg.notes:
        op._notes = op._app_notes = cfg.notes  # what a jev-site skill would supply

        async def _no_notes(target: Any) -> str:
            return cfg.notes

        op._load_notes = _no_notes  # type: ignore[method-assign]

    async def _shot(target: Any) -> Any:
        data = await surface.page.screenshot()
        w, h = page.browser.viewport
        frame = SimpleNamespace(
            data=data, mime_type="image/jpeg", width=w, height=h, byte_len=len(data)
        )
        op._last_frame = (data, "image/jpeg")
        return frame, (0.0, 0.0, float(w), float(h))

    op._window_shot = _shot  # type: ignore[method-assign]
    t0 = time.time()
    res = await op.run()
    from bridge.tools.jev_computer_use_tool import render_result

    return {
        "status": res.status,
        "summary": res.summary,
        "report": getattr(res, "report", "") or "",
        "text": render_result(res),
        "steps": res.steps,
        "jev_calls": res.jev_calls,
        "llm_calls": res.llm_calls,
        "llm_ms": res.llm_ms,
        "llm_tokens": list(res.llm_tokens),
        "elapsed_s": round(time.time() - t0, 1),
        "log": str(res.log_path),
        "page_log": steps,
        "url": surface.page.url,
    }
