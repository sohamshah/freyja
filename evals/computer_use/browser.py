"""Headless Chromium over the DevTools protocol, for running computer-use agents
against mock sites without touching anyone's browser or screen.

One websocket to the browser, flattened target sessions. Each episode gets its
own browser context (cookies and storage start empty). Page scripts can run in
an isolated world, as Arc runs AppleScript JavaScript: the script shares the
page's DOM but not its globals.

Stdlib plus `websockets`, which the bridge already depends on.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import websockets

CHROME_CANDIDATES = [
    os.environ.get("CUA_CHROME", ""),
    *sorted(
        glob.glob(
            os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
            )
        ),
        reverse=True,
    ),
    *sorted(
        glob.glob(
            os.path.expanduser(
                "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
            )
        ),
        reverse=True,
    ),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("chromium") or "",
    shutil.which("google-chrome") or "",
]
WORLD = "freyja-jev"
_DEVTOOLS = re.compile(r"DevTools listening on (ws://\S+)")


class CDPError(RuntimeError):
    """A protocol call failed."""


class PageClosed(CDPError):
    """The page's target is gone."""


def find_chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


class Browser:
    """A headless Chromium process and one protocol connection to it."""

    def __init__(self, proc: subprocess.Popen[bytes], ws: Any, profile: str) -> None:
        self._proc = proc
        self._ws = ws
        self._profile = profile
        self._next = 0
        self._waiting: dict[int, asyncio.Future[Any]] = {}
        self._sessions: dict[str, "Page"] = {}
        self._created: list[dict[str, Any]] = []  # Target.targetCreated infos (new tabs)
        self._reader = asyncio.create_task(self._read())
        self.closed = False

    @classmethod
    async def launch(
        cls, *, chrome: str | None = None, width: int = 1280, height: int = 800
    ) -> "Browser":
        exe = chrome or find_chrome()
        if not exe:
            raise CDPError(
                "no Chromium found: set CUA_CHROME or install a playwright headless shell"
            )
        profile = tempfile.mkdtemp(prefix="cua-chrome-")
        args = [
            exe,
            "--headless=new" if "headless-shell" not in exe else "--headless",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--mute-audio",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "about:blank",
        ]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        url = await asyncio.to_thread(_devtools_url, proc)
        ws = await websockets.connect(url, max_size=64 * 1024 * 1024, ping_interval=None)
        b = cls(proc, ws, profile)
        await b.send("Target.setDiscoverTargets", {"discover": True})
        b.viewport = (width, height)
        return b

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._waiting.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                CDPError(str(msg["error"].get("message") or msg["error"]))
                            )
                        else:
                            fut.set_result(msg.get("result") or {})
                    continue
                method, params = msg.get("method"), msg.get("params") or {}
                if method == "Target.targetCreated":
                    info = params.get("targetInfo") or {}
                    if info.get("type") == "page":
                        self._created.append(info)
                elif method == "Target.detachedFromTarget":
                    page = self._sessions.pop(params.get("sessionId", ""), None)
                    if page is not None:
                        page.closed = True
                sid = msg.get("sessionId")
                if sid and sid in self._sessions:
                    self._sessions[sid]._event(method, params)
        except Exception:  # noqa: BLE001  (connection closed)
            pass
        finally:
            self.closed = True
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(CDPError("browser connection closed"))
            self._waiting.clear()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session: str | None = None,
        timeout: float = 30.0,
    ) -> Any:
        if self.closed:
            raise CDPError("browser connection closed")
        self._next += 1
        mid = self._next
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[mid] = fut
        await self._ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiting.pop(mid, None)

    async def new_context(self) -> str:
        """A fresh browser context: its own cookies, storage and cache."""
        r = await self.send("Target.createBrowserContext", {"disposeOnDetach": True})
        return r["browserContextId"]

    async def new_page(self, url: str = "about:blank", *, context: str | None = None) -> "Page":
        params: dict[str, Any] = {"url": "about:blank"}
        if context:
            params["browserContextId"] = context
        r = await self.send("Target.createTarget", params)
        page = await self.attach(r["targetId"], context=context)
        if url != "about:blank":
            await page.navigate(url)
        return page

    async def attach(self, target_id: str, *, context: str | None = None) -> "Page":
        r = await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        page = Page(self, r["sessionId"], target_id, context)
        self._sessions[page.session] = page
        await page._setup()
        return page

    def opened_by(self, target_id: str, since: int = 0) -> list[dict[str, Any]]:
        """Pages another page opened (target=_blank, window.open), oldest first."""
        return [t for t in self._created[since:] if t.get("openerId") == target_id]

    def created_count(self) -> int:
        return len(self._created)

    async def dispose_context(self, context: str) -> None:
        with contextlib.suppress(Exception):
            await self.send(
                "Target.disposeBrowserContext", {"browserContextId": context}, timeout=10
            )

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.send("Browser.close", timeout=5)
        with contextlib.suppress(Exception):
            await self._ws.close()
        self._reader.cancel()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        shutil.rmtree(self._profile, ignore_errors=True)


def _devtools_url(proc: subprocess.Popen[bytes], timeout_s: float = 20.0) -> str:
    deadline = time.time() + timeout_s
    buf = b""
    assert proc.stderr is not None
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        buf += line
        m = _DEVTOOLS.search(line.decode("utf-8", "replace"))
        if m:
            # Keep draining stderr so the process never blocks on a full pipe.
            import threading

            threading.Thread(target=lambda: proc.stderr.read(), daemon=True).start()  # type: ignore[union-attr]
            return m.group(1)
    raise CDPError(f"Chromium did not start: {buf.decode('utf-8', 'replace')[-500:]}")


class Page:
    """One tab, attached through a flattened session."""

    def __init__(self, browser: Browser, session: str, target_id: str, context: str | None) -> None:
        self.browser = browser
        self.session = session
        self.target_id = target_id
        self.context = context
        self.closed = False
        self._world: int | None = None
        self._frame: str | None = None
        self._load = asyncio.Event()
        self.dialogs: list[dict[str, Any]] = []  # native dialogs auto-dismissed
        self.console: list[str] = []
        self.url = "about:blank"

    async def _setup(self) -> None:
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        tree = await self.send("Page.getFrameTree")
        self._frame = tree["frameTree"]["frame"]["id"]
        w, h = self.browser.viewport
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False},
        )

    def _event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Page.loadEventFired":
            self._load.set()
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if not frame.get("parentId"):
                self._frame = frame.get("id", self._frame)
                self.url = frame.get("url", self.url)
                self._world = None
        elif method == "Page.navigatedWithinDocument":
            if params.get("frameId") == self._frame:
                self.url = params.get("url", self.url)
        elif method == "Runtime.executionContextsCleared":
            self._world = None
        elif method == "Runtime.executionContextDestroyed":
            if params.get("executionContextId") == self._world:
                self._world = None
        elif method == "Page.javascriptDialogOpening":
            # A native dialog would block every script until answered.
            self.dialogs.append({"type": params.get("type"), "message": params.get("message", "")})
            asyncio.ensure_future(self.send("Page.handleJavaScriptDialog", {"accept": False}))
        elif method == "Runtime.consoleAPICalled":
            args = params.get("args") or []
            self.console.append(
                " ".join(str(a.get("value", a.get("description", ""))) for a in args)[:500]
            )
            del self.console[:-200]
        elif method == "Inspector.detached" or method == "Target.targetDestroyed":
            self.closed = True

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> Any:
        if self.closed:
            raise PageClosed("the page was closed")
        return await self.browser.send(method, params, session=self.session, timeout=timeout)

    async def navigate(self, url: str, *, timeout: float = 20.0) -> None:
        self._load.clear()
        r = await self.send("Page.navigate", {"url": url}, timeout=timeout)
        if r.get("errorText"):
            raise CDPError(f"navigation to {url} failed: {r['errorText']}")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._load.wait(), timeout)
        self.url = url if self.url == "about:blank" else self.url

    async def _ensure_world(self) -> int:
        if self._world is None:
            if self._frame is None:
                tree = await self.send("Page.getFrameTree")
                self._frame = tree["frameTree"]["frame"]["id"]
            r = await self.send(
                "Page.createIsolatedWorld", {"frameId": self._frame, "worldName": WORLD}
            )
            self._world = r["executionContextId"]
        return self._world

    async def evaluate(
        self,
        expression: str,
        *,
        isolated: bool = True,
        timeout: float = 10.0,
        await_promise: bool = False,
    ) -> Any:
        """Run `expression` and return its value (JSON-serialisable). In the isolated
        world by default, as the jev DOM surface runs in Arc."""
        for attempt in range(3):
            params: dict[str, Any] = {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            }
            if isolated:
                params["contextId"] = await self._ensure_world()
            try:
                r = await self.send("Runtime.evaluate", params, timeout=timeout)
            except CDPError as exc:
                if attempt < 2 and re.search(r"context|destroyed|not found", str(exc), re.I):
                    self._world = None
                    await asyncio.sleep(0.2)
                    continue
                raise
            if r.get("exceptionDetails"):
                d = r["exceptionDetails"]
                text = (d.get("exception") or {}).get("description") or d.get("text") or "exception"
                if attempt < 2 and re.search(r"context|destroyed", text, re.I):
                    self._world = None
                    continue
                raise CDPError(f"page script raised: {text[:300]}")
            return (r.get("result") or {}).get("value")
        raise CDPError("page script failed after retries")

    async def screenshot(self, *, fmt: str = "jpeg", quality: int = 70) -> bytes:
        params: dict[str, Any] = {"format": fmt}
        if fmt == "jpeg":
            params["quality"] = quality
        r = await self.send("Page.captureScreenshot", params, timeout=20)
        return base64.b64decode(r["data"])

    # Trusted input, as a person's pointer and keyboard produce it.
    async def mouse_click(self, x: float, y: float) -> None:
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            p: dict[str, Any] = {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
            if kind == "mouseMoved":
                p.pop("clickCount")
                p["button"] = "none"
            await self.send("Input.dispatchMouseEvent", p)

    async def insert_text(self, text: str) -> None:
        await self.send("Input.insertText", {"text": text})

    async def key(self, key: str) -> None:
        codes = {
            "Enter": 13,
            "Escape": 27,
            "Tab": 9,
            "ArrowDown": 40,
            "ArrowUp": 38,
            " ": 32,
            "Backspace": 8,
        }
        code = codes.get(key, 0)
        base = {"key": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        if key == "Enter":
            base["text"] = "\r"
        await self.send("Input.dispatchKeyEvent", {"type": "keyDown", **base})
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    async def title(self) -> str:
        return str(await self.evaluate("document.title", isolated=False) or "")

    async def close(self) -> None:
        if self.closed:
            return
        with contextlib.suppress(Exception):
            await self.browser.send("Target.closeTarget", {"targetId": self.target_id}, timeout=5)
        self.closed = True


def data_url(html: str) -> str:
    return "data:text/html;base64," + base64.b64encode(html.encode()).decode()


__all__ = ["Browser", "Page", "CDPError", "PageClosed", "find_chrome", "data_url", "Path"]
