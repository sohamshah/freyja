"""Shared harness for the computer-use environment tests (tests/test_cua_env*.py
and tests/test_cua_envs.py): one headless Chromium and one env server per test
module, a fresh browser context per episode, the privileged oracle paced for
local pages, and each env's timelines and server waits shortened through its
episode config. Not a test module itself; the test modules import `harness`."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "evals") not in sys.path:
    sys.path.insert(0, str(ROOT / "evals"))

# The oracle first: importing it collects every env's SOLVERS.
from computer_use import envs, oracle  # noqa: E402
from computer_use.browser import Browser, find_chrome  # noqa: E402
from computer_use.run import make_server  # noqa: E402
from computer_use.server import Episode  # noqa: E402
from computer_use.tasks import select  # noqa: E402

__all__ = [
    "Episode", "FAST", "Harness", "envs", "harness_fixture", "oracle", "quirks_off", "select",
]

# The same scenarios with the clock run faster: every generated number, every
# page and every check is unchanged; only the waits the server and the pages
# impose are a tenth of their real length. Keyed by env name and by task id.
FAST: dict[str, dict[str, Any]] = {
    "airline": {"continue_delay_s": 0.6},
    "console": {
        "timings": {"first_ms": 300, "swap_ms": 300, "agree_s": [0.3, 0.5], "more_s": 0.3}
    },
    "desk": {"panel_delay_s": [0.15, 0.3]},
    "ops": {"save_delay_s": [0.2, 0.35], "plain_save_s": 0.05, "message_delay_s": 0.3},
    # The first tick (1.5 s) comes after the solver's first read of the backlog,
    # so its start value is the real one; the crossing follows a few ticks later.
    "ops/monitor_backlog": {"timeline": {"cross_s": [3, 5], "tick_s": 1.5}},
    "ops/monitor_quiet": {"timeline": {"quiet_s": 4, "report_after_s": 3, "tick_s": 1.5}},
    "widgets": {"signup_delay_s": 0.3, "lookup_delay_ms": 300},
}
PACE = 0.2  # the oracle's settle time after a click or typed text, scaled for local pages


def fast_config(task: Any, **overrides: Any) -> dict[str, Any]:
    return {**task.config, **FAST.get(task.env, {}), **FAST.get(task.id, {}), **overrides}


def quirks_off(env: str) -> dict[str, bool]:
    return {k: False for k in envs.backend(env).QUIRKS}


class Harness:
    """One browser and one server for a module; `run` drives a coroutine on their loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, browser: Browser, server: Any) -> None:
        self.loop, self.browser, self.server = loop, browser, server

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def episode(self, env: str, config: dict[str, Any] | None = None) -> Any:
        return self.server.new_episode(env, config or {})

    async def page(self, ep: Any, path: str) -> Any:
        """A page bound to `ep` in a fresh browser context, with `path` of the env's site open."""
        page = await self.browser.new_page(context=await self.browser.new_context())
        await page.navigate(self.server.start_url(ep, f"/{ep.env}/{path}"))
        return page

    async def open(self, env: str, config: dict[str, Any] | None, path: str) -> tuple[Any, Any]:
        ep = self.episode(env, config)
        return ep, await self.page(ep, path)

    async def close(self, page: Any) -> None:
        ctx = page.context
        await page.close()
        if ctx:
            await self.browser.dispose_context(ctx)

    def solvable(self, task_id: str, quirks: str = "on") -> Any:
        """The checks before and after the solver, with the task's own quirks ("on")
        or every quirk of its env off ("off": each widget's accessible twin)."""
        off = quirks_off(task_id.split("/")[0]) if quirks == "off" else None
        return self.run(self.solve(task_id, quirks=off))

    async def solve(
        self, task_id: str, *, quirks: dict[str, bool] | None = None, **cfg: Any
    ) -> Any:
        """The task's check before anything is done and after its privileged solver
        ran, on the fast config; `quirks` replaces the task's own switches."""
        t = select([task_id])[0]
        config = fast_config(t, **cfg)
        if quirks is not None:
            config["quirks"] = quirks
        backend = envs.backend(t.env)
        ep = self.episode(t.env, config)
        before = t.check(backend.view(ep), list(ep.events), {"status": "", "text": ""})
        page = await self.page(ep, t.start)
        try:
            run = await oracle.solve(task_id, page)
            return before, t.check(backend.view(ep), list(ep.events), run)
        finally:
            await self.close(page)


@pytest.fixture(scope="module", name="harness")
def harness_fixture():
    """Imported by name into a test module, where it registers the `harness` fixture."""
    if find_chrome() is None:
        pytest.skip("no Chromium for the environments")
    loop = asyncio.new_event_loop()
    browser = loop.run_until_complete(Browser.launch(width=1280, height=900))
    server = make_server()
    pace, oracle.PACE = oracle.PACE, PACE
    try:
        yield Harness(loop, browser, server)
    finally:
        oracle.PACE = pace
        loop.run_until_complete(browser.close())
        server.stop()
        loop.close()
