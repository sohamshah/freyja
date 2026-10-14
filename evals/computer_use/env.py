"""A step-level interface to the environments, for policies other than jev's own
loop (a screenshot agent, a scripted baseline, a policy being trained).

    env = await WebEnv.create(task)
    obs = await env.reset(seed=0)
    while not done:
        obs, reward, done, info = await env.step({"op": "click", "target": 12})

An observation is what jev reads: the page's controls as numbered rows, the
page text, the dialog over the page if any, and (on request) a screenshot.
Actions name a row: click, type (with text), select (an option's text), key
(return, escape, tab, down, up), scroll (down/up), open_url. The reward is the
change in the share of the task's marks that hold, read from the env server's
ground truth after every step; `done` comes when every required mark holds, a
forbidden thing happened, or the step budget runs out.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .browser import Browser, Page
from .tasks import Task

TARGET = SimpleNamespace(name="Chrome", bundle="com.google.Chrome", pid=0)


@dataclass
class Observation:
    url: str
    title: str
    rows: list[str]
    text: str
    dialog: dict[str, str] | None
    screenshot: bytes | None = None


@dataclass
class WebEnv:
    task: Task
    server: Any
    browser: Browser
    max_steps: int = 60
    screenshots: bool = False
    page: Page | None = None
    episode: Any = None
    steps: int = 0
    score: float = 0.0
    _surface: Any = None
    _ctx: str | None = None
    _rows: list[Any] = field(default_factory=list)

    @classmethod
    async def create(
        cls, task: Task, *, server: Any = None, browser: Browser | None = None, **kw: Any
    ) -> "WebEnv":
        from .run import make_server  # noqa: PLC0415

        return cls(
            task=task,
            server=server or make_server(),
            browser=browser or await Browser.launch(),
            **kw,
        )

    async def reset(self, seed: int = 7) -> Observation:
        from .jev_policy import _surface_class  # noqa: PLC0415

        if self._ctx:
            await self.browser.dispose_context(self._ctx)
        cfg = dict(self.task.config)
        cfg["seed"] = seed
        self.episode = self.server.new_episode(self.task.env, cfg)
        self._ctx = await self.browser.new_context()
        self.page = await self.browser.new_page(context=self._ctx)
        if self.task.env == "airline":
            url = self.server.start_url(self.episode, f"/airline/{self.task.start}")
        else:
            url = f"{self.server.base}/{self.task.env}/{self.task.start}?run={self.episode.id}"
        await self.page.navigate(url)
        await asyncio.sleep(1.0)
        self._surface = _surface_class()(self.page, asyncio.get_running_loop())
        self.steps, self.score = 0, self._marks()[0]
        return await self._observe()

    async def step(self, action: dict[str, Any]) -> tuple[Observation, float, bool, dict[str, Any]]:
        self.steps += 1
        rec = await self._act(action)
        obs = await self._observe()
        frac, chk = self._marks()
        reward, self.score = frac - self.score, frac
        done = bool(chk.reward) or bool(chk.forbidden) or self.steps >= self.max_steps
        info = {
            "ok": rec.ok,
            "error": rec.error,
            "marks": chk.marks,
            "forbidden": chk.forbidden,
            "success": chk.reward,
        }
        return obs, reward, done, info

    async def close(self) -> None:
        if self._ctx:
            await self.browser.dispose_context(self._ctx)

    # ─── internals ───────────────────────────────────────────────────

    async def _observe(self) -> Observation:
        o = await self._surface.observe(TARGET)
        self._rows = list(o.elements)
        shot = await self.page.screenshot() if self.screenshots else None
        return Observation(
            url=self._surface.url,
            title=self._surface.title,
            rows=o.table().split("\n") if o.elements else [],
            text=o.screen_text,
            dialog=o.dialog,
            screenshot=shot,
        )

    async def _act(self, a: dict[str, Any]) -> Any:
        op = a.get("op")
        el = next((e for e in self._rows if e.index == a.get("target")), None)
        if op == "click" and el is not None:
            return await self._surface.execute("click", el)
        if op == "type" and el is not None:
            return await self._surface.execute(
                "type_into", el, str(a.get("text") or ""), replace=True
            )
        if op == "select" and el is not None:
            return await self._surface.execute(
                "type_into", el, str(a.get("text") or ""), replace=True
            )
        if op == "key":
            return await self._surface.execute("press", str(a.get("key") or "escape"))
        if op == "scroll":
            return await self._surface.execute("scroll", down=a.get("dir", "down") != "up")
        if op == "open_url":
            return await self._surface.execute("open_url", str(a.get("url") or ""))
        return SimpleNamespace(ok=False, error=f"bad action {a!r}")

    def _marks(self) -> tuple[float, Any]:
        from .envs.airline import backend as airline  # noqa: PLC0415

        if self.task.env == "airline":
            state = airline.session_view(self.episode)
            state["_total"] = airline.total(self.episode)
            events = list(self.episode.events)
        else:
            state, events = {}, self.server.events_for_run(self.episode.id)
        chk = self.task.check(state, events, {"status": "", "text": ""})
        marks = chk.marks or {}
        return (sum(1 for v in marks.values() if v) / len(marks) if marks else chk.reward), chk
