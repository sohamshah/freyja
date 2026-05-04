"""
Browser tools — interact with live browser tabs via Chrome DevTools Protocol.

Two tools are provided:

  browser_execute_js   — runs a JS expression (Runtime.evaluate), returning
                         text, JSON, or a saved PNG for canvas.toDataURL().
                         Mirrors typing in the DevTools Console.

  browser_screenshot   — captures the rendered compositor frame of a tab
                         (Page.captureScreenshot). This is the right way to
                         capture canvas / WebGL / CSS animations — it reads
                         what the GPU has already composited, not raw pixel
                         buffers that are cleared between animation frames.

Prerequisite — launch the browser with a remote debugging port (quit first):

    open -a Arc --args --remote-debugging-port=9222
    open -a "Google Chrome" --args --remote-debugging-port=9222
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

import httpx

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

# ── defaults ────────────────────────────────────────────────────────────────
CDP_HOST = "localhost"
CDP_DEFAULT_PORT = 9222
CDP_CONNECT_TIMEOUT = 5.0   # seconds — how long to wait for the HTTP list
CDP_EVAL_TIMEOUT = 30.0     # seconds — how long to wait for JS to finish

# Results larger than this are written to a temp file so we don't flood the
# context window.  Base64-encoded canvas images can be several MB.
LARGE_RESULT_THRESHOLD = 50_000  # bytes


# ── helpers ──────────────────────────────────────────────────────────────────

def _setup_instructions(port: int) -> str:
    return (
        f"CDP is not reachable at localhost:{port}.\n"
        "Restart your browser with the remote-debugging flag, e.g.:\n\n"
        f"  Arc:    open -a Arc --args --remote-debugging-port={port}\n"
        f"  Chrome: open -a 'Google Chrome' --args --remote-debugging-port={port}\n"
        f"  Brave:  open -a Brave --args --remote-debugging-port={port}\n\n"
        "After restarting, re-open the page you want to inspect and retry."
    )


async def _list_tabs(port: int) -> list[dict[str, Any]]:
    """Return the list of debuggable targets from the running browser."""
    async with httpx.AsyncClient(timeout=CDP_CONNECT_TIMEOUT) as client:
        resp = await client.get(f"http://{CDP_HOST}:{port}/json/list")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


def _pick_tab(
    tabs: list[dict[str, Any]],
    url_filter: str | None,
) -> dict[str, Any]:
    """Return the best matching page tab."""
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        raise RuntimeError(
            "No page tabs found. Make sure the browser has at least one page open."
        )

    if url_filter:
        matches = [t for t in pages if url_filter in t.get("url", "")]
        if not matches:
            available = "\n".join(f"  • {t['url']}" for t in pages)
            raise RuntimeError(
                f"No tab URL contains '{url_filter}'.\n"
                f"Open tabs:\n{available}"
            )
        return matches[0]

    # Without a filter, return the first page (browsers tend to list the
    # active/most-recently-accessed tab first in /json/list).
    return pages[0]


def _unwrap_result(cdp_result: dict[str, Any]) -> str:
    """
    Convert a CDP ``Runtime.evaluate`` result object into a plain string.

    cdp_result is the ``result`` sub-object inside the response, e.g.:
        {"type": "string",  "value": "hello"}
        {"type": "number",  "value": 42}
        {"type": "boolean", "value": True}
        {"type": "undefined"}
        {"type": "object",  "subtype": "null"}
        {"type": "object",  "value": {...}}   (returnByValue=True)
    """
    typ = cdp_result.get("type")

    if typ == "undefined":
        return "undefined"

    if typ == "object" and cdp_result.get("subtype") == "null":
        return "null"

    if "value" in cdp_result:
        val = cdp_result["value"]
        if isinstance(val, str):
            return val
        return json.dumps(val, indent=2, ensure_ascii=False)

    # Fallback — serialize whatever CDP gave us
    return json.dumps(cdp_result, indent=2, ensure_ascii=False)


def _handle_large_result(result_str: str, call_id: str) -> str:
    """
    If the result is very large (e.g. a canvas toDataURL base64 blob), write
    it to a temp file and return the path instead of flooding the context.
    """
    if len(result_str) <= LARGE_RESULT_THRESHOLD:
        return result_str

    # Detect data-URL — save as actual file with the right extension
    if result_str.startswith("data:image/png;base64,"):
        import base64
        b64 = result_str[len("data:image/png;base64,"):]
        suffix = ".png"
        raw = base64.b64decode(b64)
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, prefix="freyja_cdp_"
        ) as f:
            f.write(raw)
            path = f.name
        return f"[Large image result saved to {path}]"

    if result_str.startswith("data:image/"):
        import base64
        _, rest = result_str.split(";base64,", 1)
        raw = base64.b64decode(rest)
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".png", prefix="freyja_cdp_"
        ) as f:
            f.write(raw)
            path = f.name
        return f"[Large image result saved to {path}]"

    # Generic large text — truncate with a note
    size_kb = len(result_str) // 1024
    truncated = result_str[:LARGE_RESULT_THRESHOLD]
    return (
        f"{truncated}\n\n"
        f"… [truncated — full result was {size_kb} KB. "
        "Re-run with a more specific expression to get a smaller value.]"
    )


# ── tool ─────────────────────────────────────────────────────────────────────

class BrowserExecuteJsTool:
    """
    Execute JavaScript in the active browser tab via Chrome DevTools Protocol.

    Requires the browser to be running with ``--remote-debugging-port``.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_execute_js",
            summary=(
                "Execute JavaScript in a live browser tab via CDP. "
                "Extracts DOM text, canvas images, SVG, computed styles, "
                "network state — anything the DevTools Console can see."
            ),
            tier=ToolTier.HOT,
            description="""Execute JavaScript in the currently open browser tab via the Chrome DevTools Protocol (CDP).

This is the equivalent of typing an expression into the browser's DevTools Console — lossless, instant, and exact. Use it to extract any design asset or DOM content from a live page:

| Asset type | Example code |
|---|---|
| ASCII / text art | `document.querySelector('pre').innerText` |
| Canvas frame | `document.querySelector('canvas').toDataURL('image/png')` |
| SVG illustration | `document.querySelector('svg').outerHTML` |
| CSS-generated content | `getComputedStyle(el, '::before').content` |
| Full page text | `document.body.innerText` |
| Specific element | `$0.innerText` (inspected element in DevTools) |

**Prerequisite — enable remote debugging in your browser (quit first):**

```
# Arc
open -a Arc --args --remote-debugging-port=9222

# Chrome
open -a "Google Chrome" --args --remote-debugging-port=9222
```

**Tips:**
- `awaitPromise` is true by default, so you can return Promises
- Large canvas `toDataURL()` results are automatically saved to a temp file
- Use `tab_url_filter` to target a specific tab when multiple are open
- The JS runs in the page's context — full DOM + window access""",
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "JavaScript expression to evaluate in the tab. "
                            "Must be a single expression (not a statement block) "
                            "or wrap statements in an IIFE: (() => { ... })(). "
                            "The return value must be JSON-serialisable or a string."
                        ),
                    },
                    "tab_url_filter": {
                        "type": "string",
                        "description": (
                            "Optional substring to match against open tab URLs. "
                            "If omitted, the first page tab is used (usually the active one)."
                        ),
                    },
                    "cdp_port": {
                        "type": "integer",
                        "description": (
                            f"CDP debug port (default: {CDP_DEFAULT_PORT}). "
                            "Change only if the browser was started with a non-standard port."
                        ),
                    },
                    "await_promise": {
                        "type": "boolean",
                        "description": (
                            "Await Promises returned by the expression before returning "
                            "the result (default: true)."
                        ),
                    },
                },
                "required": ["code"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        code: str = arguments["code"]
        tab_url_filter: str | None = arguments.get("tab_url_filter")
        port: int = int(arguments.get("cdp_port", CDP_DEFAULT_PORT))
        await_promise: bool = arguments.get("await_promise", True)

        try:
            result = await asyncio.wait_for(
                self._run(code, tab_url_filter, port, await_promise),
                timeout=CDP_EVAL_TIMEOUT + CDP_CONNECT_TIMEOUT + 2,
            )
            return ToolResult(call_id=call_id, content=result, is_error=False)

        except asyncio.TimeoutError:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Timed out after {CDP_EVAL_TIMEOUT:.0f}s. "
                    "The JS expression may be awaiting a long-running Promise. "
                    "Try a synchronous expression or increase the timeout."
                ),
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call_id=call_id, content=str(exc), is_error=True)

    # ── internal ─────────────────────────────────────────────────────────────

    async def _run(
        self,
        code: str,
        tab_url_filter: str | None,
        port: int,
        await_promise: bool,
    ) -> str:
        # 1. List tabs
        try:
            tabs = await _list_tabs(port)
        except httpx.ConnectError:
            raise RuntimeError(_setup_instructions(port))
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Unexpected HTTP error talking to CDP: {exc}\n\n"
                + _setup_instructions(port)
            )

        # 2. Pick tab
        tab = _pick_tab(tabs, tab_url_filter)
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError(
                f"Tab '{tab.get('url')}' has no webSocketDebuggerUrl. "
                "It may already be attached to another debugger (DevTools open?)."
            )

        # 3. Connect + evaluate
        import websockets  # installed at runtime via `uv add websockets`

        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:  # 32 MB — canvas toDataURL can be large
            msg = {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": code,
                    "returnByValue": True,
                    "awaitPromise": await_promise,
                    "userGesture": True,
                },
            }
            await ws.send(json.dumps(msg))
            raw = await asyncio.wait_for(ws.recv(), timeout=CDP_EVAL_TIMEOUT)

        response = json.loads(raw)

        # 4. Handle CDP-level errors
        if "error" in response:
            err = response["error"]
            raise RuntimeError(
                f"CDP error {err.get('code')}: {err.get('message')}"
            )

        payload = response.get("result", {})

        # JS threw an exception
        if payload.get("exceptionDetails"):
            exc_details = payload["exceptionDetails"]
            exc_text = exc_details.get("text", "")
            exc_val = exc_details.get("exception", {}).get("description", "")
            raise RuntimeError(
                f"JavaScript exception: {exc_text} — {exc_val}".strip(" — ")
            )

        result_obj = payload.get("result", {})
        result_str = _unwrap_result(result_obj)
        return _handle_large_result(result_str, call_id="")


# ── BrowserScreenshotTool ────────────────────────────────────────────────────

class BrowserScreenshotTool:
    """
    Capture the rendered frame of a browser tab via CDP Page.captureScreenshot.

    Unlike reading canvas.toDataURL() from JS (which reads raw pixel buffers
    that may be cleared between animation frames), Page.captureScreenshot reads
    the GPU compositor output — what the user actually sees — so it correctly
    captures canvas animations, WebGL, and CSS transitions at any moment.

    Saves to a temp PNG file and returns the path.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_screenshot",
            summary=(
                "Capture the rendered frame of a browser tab as a PNG via CDP. "
                "Works on canvas/WebGL animations that canvas.toDataURL() can't read. "
                "Returns a file path to the saved image."
            ),
            tier=ToolTier.HOT,
            description="""Capture a screenshot of a live browser tab using the Chrome DevTools Protocol.

**Why this instead of the OS screenshot tool?**
- Captures only the browser page — no OS chrome, dock, or other windows
- Works correctly on canvas/WebGL animations: reads the GPU compositor output (what the user sees), not raw pixel buffers that are cleared between animation frames
- Can target any open tab by URL, even if it's not frontmost
- Can capture the full scrollable page, not just the visible viewport
- Higher fidelity than a compressed OS screenshot for extracting design assets

**Typical use cases:**
- Grab an animated ASCII art / matrix effect from a canvas
- Capture a D3/WebGL data visualisation mid-animation
- Screenshot a specific tab without switching to it
- Capture the full scrollable page for long-form analysis

**Prerequisite — launch browser with remote debugging (quit first):**
```
open -a Arc --args --remote-debugging-port=9222
```""",
            parameters={
                "type": "object",
                "properties": {
                    "tab_url_filter": {
                        "type": "string",
                        "description": (
                            "Substring to match against open tab URLs. "
                            "If omitted, uses the first page tab (usually the active one)."
                        ),
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": (
                            "Capture the full scrollable page, not just the visible viewport. "
                            "Default: false."
                        ),
                    },
                    "scale": {
                        "type": "number",
                        "description": (
                            "Device scale factor for the capture (e.g. 2.0 for retina). "
                            "Default: 1.0 (device pixels)."
                        ),
                    },
                    "clip": {
                        "type": "object",
                        "description": (
                            "Optional viewport clip region in CSS pixels: "
                            "{x, y, width, height}. "
                            "Useful for cropping to just the element of interest."
                        ),
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                        },
                    },
                    "cdp_port": {
                        "type": "integer",
                        "description": f"CDP debug port (default: {CDP_DEFAULT_PORT}).",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        tab_url_filter: str | None = arguments.get("tab_url_filter")
        full_page: bool = arguments.get("full_page", False)
        scale: float = float(arguments.get("scale", 1.0))
        clip: dict | None = arguments.get("clip")
        port: int = int(arguments.get("cdp_port", CDP_DEFAULT_PORT))

        try:
            path = await asyncio.wait_for(
                self._run(tab_url_filter, full_page, scale, clip, port),
                timeout=30.0,
            )
            return ToolResult(call_id=call_id, content=path, is_error=False)
        except asyncio.TimeoutError:
            return ToolResult(
                call_id=call_id,
                content="Timed out waiting for browser screenshot.",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(call_id=call_id, content=str(exc), is_error=True)

    async def _run(
        self,
        tab_url_filter: str | None,
        full_page: bool,
        scale: float,
        clip: dict | None,
        port: int,
    ) -> str:
        # 1. List tabs
        try:
            tabs = await _list_tabs(port)
        except httpx.ConnectError:
            raise RuntimeError(_setup_instructions(port))

        # 2. Pick tab
        tab = _pick_tab(tabs, tab_url_filter)
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError(
                f"Tab '{tab.get('url')}' has no webSocketDebuggerUrl. "
                "It may already be attached to another debugger."
            )

        # 3. Build CDP params
        params: dict[str, Any] = {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": full_page,
        }
        if scale != 1.0 or clip:
            # Page.captureScreenshot uses a Viewport object for clip + scale
            vp: dict[str, Any] = {"scale": scale}
            if clip:
                vp["x"] = clip.get("x", 0)
                vp["y"] = clip.get("y", 0)
                vp["width"] = clip["width"]
                vp["height"] = clip["height"]
            else:
                # Need explicit width/height even without a clip when scale != 1
                # Query from the browser first
                vp["x"] = 0
                vp["y"] = 0
                vp["width"] = 1280
                vp["height"] = 800
            params["clip"] = vp

        # 4. Connect, capture, decode
        import base64
        import websockets

        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": params}))
            raw = await asyncio.wait_for(ws.recv(), timeout=20.0)

        resp = json.loads(raw)
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"CDP error {err.get('code')}: {err.get('message')}")

        img_b64 = resp["result"]["data"]
        img_bytes = base64.b64decode(img_b64)

        # 5. Save to temp file
        label = (tab_url_filter or tab.get("url", "page")).replace("/", "_").replace(":", "")[:30]
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".png", prefix=f"freyja_browser_{label}_"
        ) as f:
            f.write(img_bytes)
            path = f.name

        size_kb = len(img_bytes) // 1024
        url = tab.get("url", "?")
        return f"Saved {size_kb} KB PNG → {path}\nTab: {url}"
