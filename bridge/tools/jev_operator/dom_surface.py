"""DOM surface: read and drive a web page through the browser's own JavaScript.

The page runs `dom_snapshot.js` (injected once per page load). `snapshot()` lists
the actionable elements with stable ids; `act()` clicks, fills or scrolls one of
them, guarded so an element that changed since the snapshot is refused ("stale").
The transport is `osascript` talking to the browser, so no accessibility tree and
no pointer are involved. Arc and Chrome are supported; Chrome needs "Allow
JavaScript from Apple Events" turned on.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from bridge.tools.jev_operator.act import ActionRecord
from bridge.tools.jev_operator.observe import (
    MAX_LABEL_CHARS,
    MAX_ROWS,
    MAX_TEXT_CHARS,
    Element,
    Observation,
    _clip,
)

JS_FILE = Path(__file__).with_name("dom_snapshot.js")
RUN_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 3.0

# bundle id -> (application name, AppleScript that runs `js` in the front window's active tab)
_TELL_TAB = 'tell application "{app}" to tell active tab of front window to execute javascript js'
SUPPORTED_BROWSERS: dict[str, tuple[str, str]] = {
    "company.thebrowser.Browser": ("Arc", _TELL_TAB),
    "com.google.Chrome": ("Google Chrome", _TELL_TAB),
}

_APPLESCRIPT = """on run argv
    set js to read (POSIX file (item 1 of argv)) as «class utf8»
    {body}
end run
"""

# AX role names, so the loop's gates and the decision questions treat DOM rows
# exactly like accessibility rows.
KIND_ROLES = {
    "click": "AXButton",
    "link": "AXLink",
    "fill": "AXTextField",
    "select": "AXComboBox",
    "toggle": "AXCheckBox",
}
KEY_MAP = {"return": "Enter", "enter": "Enter", "escape": "Escape", "esc": "Escape", "tab": "Tab"}
_SECURE = re.compile(r"password|passcode|\bpin\b|secure", re.I)
_WS = re.compile(r"\s+")


class DOMUnavailable(Exception):
    """The page could not be reached (timeout, osascript error, JavaScript disabled)."""


def supported(bundle_id: str) -> bool:
    return bundle_id in SUPPORTED_BROWSERS


def osascript_run_js(bundle_id: str, js: str, timeout_s: float = RUN_TIMEOUT_S) -> str:
    """Run `js` in the front window's active tab and return its text result.

    The JS goes through a temp file the AppleScript reads as UTF-8, so quotes,
    newlines and unicode never touch AppleScript string syntax."""
    entry = SUPPORTED_BROWSERS.get(bundle_id)
    if entry is None:
        raise DOMUnavailable(f"{bundle_id} is not a supported browser")
    app, template = entry
    script = _APPLESCRIPT.format(body=template.format(app=app))
    fd, path = tempfile.mkstemp(prefix="freyja-jev-", suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js)
        try:
            proc = subprocess.run(
                ["osascript", "-e", script, path],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise DOMUnavailable(f"{app} did not answer within {timeout_s:.0f}s") from exc
        if proc.returncode != 0:
            raise DOMUnavailable((proc.stderr or "osascript failed").strip()[:300])
        return proc.stdout.rstrip("\n")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def probe(
    bundle_id: str, run_js: Callable[..., str] | None = None
) -> tuple[bool, str]:
    """Can we run page JavaScript in this browser's front tab? Read-only."""
    if not supported(bundle_id):
        return False, "unsupported browser"
    try:
        if run_js is None:
            out = osascript_run_js(bundle_id, "document.title", PROBE_TIMEOUT_S)
        else:
            out = run_js(bundle_id, "document.title")
    except DOMUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"title={_clip(str(out), 60)!r}"


@dataclass
class DOMElement(Element):
    dom_id: int = 0
    guard: str = ""
    raw_label: str = ""
    context: str = ""
    dom_kind: str = ""
    expanded: bool | None = None

    def row(self, *args: Any, **kwargs: Any) -> str:
        text = super().row(*args, **kwargs)
        if self.expanded is not None:
            text += " (expanded)" if self.expanded else " (collapsed)"
        return text

    def identity(self) -> tuple[str, str, str]:
        return (self.role, self.raw_label, self.context)


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


class DOMSurface:
    """Surface over page JavaScript. `run_js(bundle_id, js) -> str` is injectable."""

    name = "dom"

    def __init__(
        self,
        *,
        bundle: str,
        actuator: Any,
        run_js: Callable[[str, str], str] | None = None,
        js_source: str | None = None,
        log: Callable[[dict[str, Any]], None] | None = None,
        timeout_s: float = RUN_TIMEOUT_S,
    ) -> None:
        self.bundle = bundle
        self.actuator = actuator
        self._run_js = run_js or (lambda b, js: osascript_run_js(b, js, timeout_s))
        self._js_source = js_source
        self._log = log or (lambda row: None)
        self.timeout_s = timeout_s
        self.fail_streak = 0
        self.ax_fail_streak = 0  # the loop's AX-unreadable check never fires for DOM
        self._target: Any = None
        self._viewport_h = 0

    # ─── transport ───────────────────────────────────────────────────

    def _source(self) -> str:
        if self._js_source is None:
            try:
                self._js_source = JS_FILE.read_text(encoding="utf-8")
            except OSError as exc:
                raise DOMUnavailable(f"dom_snapshot.js unreadable: {exc}") from exc
        return self._js_source

    def _wrap(self, call: str) -> str:
        return (
            "(function(){if(typeof window.__freyjaJev==='undefined'){"
            + self._source()
            + "\n}return window.__freyjaJev."
            + call
            + ";})()"
        )

    async def _js(self, call: str) -> Any:
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._run_js, self.bundle, self._wrap(call)),
                timeout=self.timeout_s + 1,
            )
        except asyncio.TimeoutError as exc:
            self.fail_streak += 1
            raise DOMUnavailable(f"page JavaScript timed out after {self.timeout_s:.0f}s") from exc
        except DOMUnavailable:
            self.fail_streak += 1
            raise
        except Exception as exc:  # noqa: BLE001
            self.fail_streak += 1
            raise DOMUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            data = json.loads(raw)
            # Arc JSON-encodes the script's return value, so a script that
            # returns a JSON string arrives as a quoted string: decode twice.
            if isinstance(data, str):
                data = json.loads(data)
        except (TypeError, ValueError) as exc:
            self.fail_streak += 1
            raise DOMUnavailable(f"page returned non-JSON: {_clip(str(raw), 80)!r}") from exc
        self.fail_streak = 0
        return data

    # ─── observe ─────────────────────────────────────────────────────

    async def observe(self, target: Any) -> Observation:
        self._target = target
        t0 = time.perf_counter()
        snap = await self._js("snapshot()")
        read_ms = int((time.perf_counter() - t0) * 1000)
        return self._build(snap, target, read_ms)

    def _build(self, snap: dict[str, Any], target: Any, read_ms: int) -> Observation:
        url = str(snap.get("url") or "")
        title = str(snap.get("title") or "")
        window = f"{title} ({url})" if url else title or "(untitled page)"
        elements: list[DOMElement] = []
        for a in snap.get("actions") or []:
            kind = a.get("kind") or "click"
            role = KIND_ROLES.get(kind, "AXButton")
            raw = str(a.get("label") or "").strip()
            ctx = str(a.get("context") or "").strip()
            label = f"{raw} ({ctx})" if raw and ctx else raw or ctx or kind
            secure = kind == "fill" and bool(_SECURE.search(f"{a.get('role', '')} {raw}"))
            value = a.get("value")
            value_s = None if value in (None, "") else str(value)
            if kind == "toggle":
                value_s = "1" if a.get("checked") else "0"
            if secure:
                value_s = "••••" if value_s else None
            elements.append(
                DOMElement(
                    index=len(elements) + 1,
                    role=role,
                    subrole="AXSecureTextField" if secure else None,
                    label=_clip(label, MAX_LABEL_CHARS),
                    value=value_s,
                    bounds=(0, 0, 0, 0),
                    enabled=True,
                    focused=bool(a.get("focused")),
                    window=window,
                    kind="type" if kind in ("fill", "select") else "click",
                    offscreen=bool(a.get("offscreen")),
                    dom_id=int(a.get("id", 0)),
                    guard=str(a.get("guard") or ""),
                    raw_label=raw,
                    context=ctx,
                    dom_kind=kind,
                    expanded=a.get("expanded"),
                )
            )
        truncated = len(elements) > MAX_ROWS
        if truncated:
            visible = [e for e in elements if not e.offscreen]
            hidden = [e for e in elements if e.offscreen]
            elements = (visible + hidden)[:MAX_ROWS]
            for i, e in enumerate(elements, 1):
                e.index = i
        text = str(snap.get("text") or "")
        if len(text) > MAX_TEXT_CHARS:
            text = text[: MAX_TEXT_CHARS - 1] + "…"
        obs = Observation(
            app_name=target.name,
            bundle=target.bundle,
            pid=target.pid,
            windows=[window],
            focused_window=window,
            elements=list(elements),
            screen_text=text,
            dialog=None,
            menu_open=False,
            node_count=len(elements),
            read_ms=read_ms,
            truncated=truncated,
            raw_tree=None,
        )
        self._viewport_h = int((snap.get("viewport") or {}).get("h") or 0)
        return obs

    # ─── execute ─────────────────────────────────────────────────────

    async def execute(self, action: str, *args: Any, **kwargs: Any) -> Any:
        if action == "click":
            el = args[0]
            if kwargs.get("double"):
                return await self._fail("click", "double-click is not supported on the DOM surface")
            return await self._act_on(el, "click", None, f"Click {el.role} {el.label!r}", "click")
        if action == "type_into":
            el, text = args[0], args[1]
            return await self._type_into(el, text, replace=bool(kwargs.get("replace", True)))
        if action == "press":
            key = KEY_MAP.get(str(args[0]).lower())
            if key is None:
                return await self.actuator.press(*args, **kwargs)
            return await self._act_on(None, "key", key, f"Press {args[0]}", "press_key")
        if action == "scroll":
            down = bool(kwargs.get("down", True))
            return await self._scroll(down)
        # launch, click_point, type_raw: not page operations.
        return await getattr(self.actuator, action)(*args, **kwargs)

    async def _fail(self, kind: str, error: str, t0: float | None = None) -> ActionRecord:
        ms = int((time.perf_counter() - t0) * 1000) if t0 else 0
        return ActionRecord(kind=kind, description="", duration_ms=ms, ok=False, error=error)

    async def _type_into(self, el: DOMElement, text: str, *, replace: bool) -> ActionRecord:
        if el.subrole == "AXSecureTextField":
            return await self._fail("type_text", "refusing to type into a secure field")
        desc = f"Type {text!r} into {el.label!r}"
        if el.dom_kind == "select":
            return await self._act_on(el, "select", text, desc, "type_text")
        arg = {"text": text, "mode": "replace" if replace else "append"}
        rec = await self._act_on(el, "fill", arg, desc, "type_text", expect=(text, replace))
        return rec

    async def _scroll(self, down: bool) -> ActionRecord:
        t0 = time.perf_counter()
        vp = self._viewport_h or 600
        dy = int(vp * 0.8) * (1 if down else -1)
        try:
            res = await self._js(f"act(0,'scroll',{json.dumps(dy)},'')")
        except DOMUnavailable as exc:
            return await self._fail("scroll", f"dom transport: {exc}", t0)
        ok = bool(res.get("ok"))
        return ActionRecord(
            kind="scroll",
            description=f"Scroll {'down' if down else 'up'}",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            ok=ok,
            error=None if ok else str(res.get("reason") or "scroll failed"),
        )

    async def _call_act(self, dom_id: int, op: str, arg: Any, guard: str) -> dict[str, Any]:
        call = f"act({json.dumps(dom_id)},{json.dumps(op)},{json.dumps(arg)},{json.dumps(guard)})"
        res = await self._js(call)
        return res if isinstance(res, dict) else {"ok": False, "reason": "bad act() result"}

    async def _act_on(
        self,
        el: DOMElement | None,
        op: str,
        arg: Any,
        desc: str,
        kind: str,
        *,
        expect: tuple[str, bool] | None = None,
    ) -> ActionRecord:
        t0 = time.perf_counter()

        def done(ok: bool, error: str | None = None) -> ActionRecord:
            return ActionRecord(
                kind=kind,
                description=desc,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                ok=ok,
                error=error,
            )

        dom_id, guard = (el.dom_id, el.guard) if el is not None else (0, "")
        try:
            res = await self._call_act(dom_id, op, arg, guard)
            if res.get("reason") == "stale" and el is not None:
                rebound = await self._rebind(el)
                if isinstance(rebound, str):
                    return done(False, rebound)
                res = await self._call_act(rebound.dom_id, op, arg, rebound.guard)
        except DOMUnavailable as exc:
            return done(False, f"dom transport: {exc}")
        if not res.get("ok"):
            reason = str(res.get("reason") or "action failed")
            if reason == "stale":
                reason = "stale: the element changed again right after re-binding"
            return done(False, reason)
        if expect is not None:
            want, replace = expect
            got = res.get("readback")
            w, g = _norm(want), _norm(str(got) if got is not None else "")
            good = g.endswith(w) if not replace else g == w
            if got is not None and not good:
                return done(False, f"readback mismatch: typed {want!r}, field shows {got!r}")
        return done(True)

    async def _rebind(self, el: DOMElement) -> DOMElement | str:
        """Re-observe and return the one element matching `el`'s identity, or a reason."""
        if self._target is None:
            return "stale: no target to re-observe"
        obs = await self.observe(self._target)
        matches = [e for e in obs.elements if isinstance(e, DOMElement) and e.identity() == el.identity()]
        if len(matches) == 1:
            return matches[0]
        what = "gone from the page" if not matches else f"matches {len(matches)} elements now"
        return f"stale: {el.label!r} is {what}; re-read the page and choose again"
