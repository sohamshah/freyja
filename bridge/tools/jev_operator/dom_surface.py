"""DOM surface: read and drive a web page through the browser's own JavaScript.

The page runs `dom_snapshot.js` (injected once per page load). `snapshot()` lists
the actionable elements with stable ids; `act()` clicks, fills or scrolls one of
them, guarded so an element that changed since the snapshot is refused ("stale").
The transport is `osascript` talking to the browser, so no accessibility tree and
no pointer are involved, and the browser does not need to be frontmost. Arc and
Chrome are supported; Chrome needs "Allow JavaScript from Apple Events" turned on.

A run is pinned to one tab by id: the tab that was active when it started, or
the tab it opened with `open_url`. Switching tabs while it runs does not move it,
and a click that opens a new tab moves the run to that tab.
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
PROBE_RETRY_TIMEOUT_S = 8.0
OPEN_TIMEOUT_S = 10.0
# After an action, wait until the page stops changing (same mutation count and
# URL on two reads ~0.25 s apart, document complete), at most this long.
SETTLE_S = {"click": 3.0, "key": 3.0, "type": 1.5, "scroll": 1.0, "open_url": 12.0}
MAX_OPTIONS = 25

# bundle id -> application name. Both expose `execute <tab> javascript <text>`
# and tab ids through AppleScript.
SUPPORTED_BROWSERS: dict[str, str] = {
    "company.thebrowser.Browser": "Arc",
    "com.google.Chrome": "Google Chrome",
}

# argv: <js file> <tab id or "">. An empty tab id means the front window's active
# tab. Ids are matched through one bulk `id of every tab` per window (a window
# can hold a thousand tabs; per-tab Apple Events would take seconds).
_RUN_IN_TAB = """on run argv
    set js to read (POSIX file (item 1 of argv)) as «class utf8»
    set tid to item 2 of argv
    tell application "{app}"
        if tid is "" then return execute active tab of front window javascript js
        repeat with wi from 1 to (count of windows)
            set ids to id of every tab of window wi
            repeat with i from 1 to (count of ids)
                if ((item i of ids) as text) is tid then return execute tab i of window wi javascript js
            end repeat
        end repeat
    end tell
    error "freyja: tab " & tid & " is gone" number 1404
end run
"""
_ACTIVE_TAB = 'tell application "{app}" to return (id of active tab of front window) as text'
# Title and address of one tab, by id, from the browser's own tab properties.
_TAB_INFO = """on run argv
    set tid to item 1 of argv
    tell application "{app}"
        repeat with wi from 1 to (count of windows)
            set ids to id of every tab of window wi
            repeat with i from 1 to (count of ids)
                if ((item i of ids) as text) is tid then return (title of tab i of window wi) & linefeed & (URL of tab i of window wi)
            end repeat
        end repeat
    end tell
    return ""
end run
"""
# Every tab id of every window, one bulk read per window.
_TAB_IDS = """tell application "{app}"
    set out to {{}}
    repeat with wi from 1 to (count of windows)
        set out to out & (id of every tab of window wi)
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
return out as text
"""
_ACTIVE_URL = 'tell application "{app}" to return (URL of active tab of front window) as text'
# Arc's `make new tab` returns a broken reference, so read the new tab's id from
# the window's active tab, which the new tab becomes.
_NEW_TAB = """on run argv
    tell application "{app}"
        tell front window to make new tab with properties {{URL:(item 1 of argv)}}
        delay 0.2
        return (id of active tab of front window) as text
    end tell
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
# The keys a page understands; browser shortcuts (cmd+l, cmd+t) act on the
# browser window, which this surface does not see.
DOM_KEYS = ("return", "escape", "tab")
_SECURE = re.compile(r"password|passcode|\bpin\b|secure", re.I)
_WS = re.compile(r"\s+")
_VERSION = re.compile(r"var VERSION = (\d+);")


class DOMUnavailable(Exception):
    """The page could not be reached (timeout, osascript error, JavaScript disabled)."""


class TabGone(DOMUnavailable):
    """The tab the run is pinned to was closed."""


def supported(bundle_id: str) -> bool:
    return bundle_id in SUPPORTED_BROWSERS


# Tabs a run has opened or worked in, per browser. A run whose goal gives no
# address starts only in one of these: the tab in front when it starts can be
# anything the person has open (a test run once acted in a live video call that
# had come to the front).
_RUN_TABS: dict[str, set[str]] = {}


def remember_tab(bundle_id: str, tab_id: str) -> None:
    if tab_id:
        _RUN_TABS.setdefault(bundle_id, set()).add(tab_id)


def run_tab(bundle_id: str, tab_id: str) -> bool:
    """Has a run opened or worked in this tab?"""
    return bool(tab_id) and tab_id in _RUN_TABS.get(bundle_id, set())


def _osascript(script: str, args: list[str], timeout_s: float, app: str) -> str:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script, *args], capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as exc:
        raise DOMUnavailable(f"{app} did not answer within {timeout_s:.0f}s") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "osascript failed").strip()
        if "(1404)" in err or "is gone" in err:
            raise TabGone("the run's browser tab was closed")
        raise DOMUnavailable(err[:300])
    return proc.stdout.rstrip("\n")


def _app(bundle_id: str) -> str:
    app = SUPPORTED_BROWSERS.get(bundle_id)
    if app is None:
        raise DOMUnavailable(f"{bundle_id} is not a supported browser")
    return app


def osascript_run_js(
    bundle_id: str, js: str, timeout_s: float = RUN_TIMEOUT_S, tab_id: str = ""
) -> str:
    """Run `js` in the pinned tab (or the front window's active tab) and return its
    text result. The JS goes through a temp file the AppleScript reads as UTF-8,
    so quotes, newlines and unicode never touch AppleScript string syntax."""
    app = _app(bundle_id)
    fd, path = tempfile.mkstemp(prefix="freyja-jev-", suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js)
        return _osascript(_RUN_IN_TAB.format(app=app), [path, tab_id or ""], timeout_s, app)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def active_tab_id(bundle_id: str, timeout_s: float = PROBE_TIMEOUT_S) -> str:
    app = _app(bundle_id)
    return _osascript(_ACTIVE_TAB.format(app=app), [], timeout_s, app).strip()


def active_tab_url(bundle_id: str, timeout_s: float = PROBE_TIMEOUT_S) -> str:
    app = _app(bundle_id)
    return _osascript(_ACTIVE_URL.format(app=app), [], timeout_s, app).strip()


def tab_ids(bundle_id: str, timeout_s: float = PROBE_RETRY_TIMEOUT_S) -> set[str]:
    """Ids of every tab in every window."""
    app = _app(bundle_id)
    out = _osascript(_TAB_IDS.format(app=app), [], timeout_s, app)
    return {x.strip() for x in out.splitlines() if x.strip()}


def tab_info(bundle_id: str, tab_id: str, timeout_s: float = PROBE_TIMEOUT_S) -> tuple[str, str]:
    """(title, url) of the tab with this id; ("", "") when it is gone."""
    app = _app(bundle_id)
    out = _osascript(_TAB_INFO.format(app=app), [tab_id], timeout_s, app)
    title, _, url = out.partition("\n")
    return title.strip(), url.strip()


def new_tab(bundle_id: str, url: str, timeout_s: float = OPEN_TIMEOUT_S) -> str:
    """Open `url` in a new tab of the front window; return the new tab's id."""
    app = _app(bundle_id)
    return _osascript(_NEW_TAB.format(app=app), [url], timeout_s, app).strip()


def probe(
    bundle_id: str, run_js: Callable[..., str] | None = None, timeout_s: float = PROBE_TIMEOUT_S
) -> tuple[bool, str]:
    """Can we run page JavaScript in this browser's front tab? Read-only."""
    if not supported(bundle_id):
        return False, "unsupported browser"
    try:
        if run_js is None:
            out = osascript_run_js(bundle_id, "document.title", timeout_s)
        else:
            out = run_js(bundle_id, "document.title")
    except DOMUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"title={_clip(str(out), 60)!r}"


def _decode(raw: Any) -> Any:
    """Arc JSON-encodes a script's return value, so a script that returns a JSON
    string arrives as a quoted string: decode until it is not a string."""
    data = json.loads(raw)
    if isinstance(data, str):
        data = json.loads(data)
    return data


@dataclass
class DOMElement(Element):
    dom_id: int = 0
    guard: str = ""
    raw_label: str = ""
    context: str = ""
    dom_kind: str = ""
    expanded: bool | None = None
    below: str | None = None  # "above" / "below": outside the viewport, scrolled into view on use
    option_value: str | None = None  # for an option row: the value to select
    enter_hint: str = ""  # what Enter does in this field: search | form:<submit label> | form | unknown

    def row(self, *args: Any, **kwargs: Any) -> str:
        text = super().row(*args, **kwargs)
        if self.expanded is not None:
            text += " (expanded)" if self.expanded else " (collapsed)"
        if self.below:
            text += f" ({self.below} the visible area; scrolled into view when used)"
        return text

    def identity(self) -> tuple[str, str, str]:
        return (self.role, self.raw_label, self.context)


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


class DOMSurface:
    """Surface over page JavaScript. `run_js(bundle_id, js) -> str` is injectable."""

    name = "dom"
    key_options = DOM_KEYS

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
        self.tab_id = ""  # pinned tab; "" = the front window's active tab
        self.own_tab = False  # the run opened the pinned tab itself, so it may navigate it
        # Tabs that existed when the run started, plus the ones it opened: a tab
        # in front after a click is followed only if it is not one of these.
        self.known_tabs: set[str] | None = None
        self.may_confirm = False  # answer a page's confirm() with OK (allow_irreversible)
        # A confirm() the page raised after an action (async) and the run declined.
        self.declined_confirm: str | None = None
        self._injected_js = run_js is not None
        self._run_js = run_js or (
            lambda b, js: osascript_run_js(b, js, timeout_s, self.tab_id)
        )
        self._js_source = js_source
        self._log = log or (lambda row: None)
        self.timeout_s = timeout_s
        self.fail_streak = 0
        self.ax_fail_streak = 0  # the loop's AX-unreadable check never fires for DOM
        self._target: Any = None
        self._viewport_h = 0
        self.url = ""
        self.title = ""

    # ─── transport ───────────────────────────────────────────────────

    def _source(self) -> str:
        if self._js_source is None:
            try:
                self._js_source = JS_FILE.read_text(encoding="utf-8")
            except OSError as exc:
                raise DOMUnavailable(f"dom_snapshot.js unreadable: {exc}") from exc
        return self._js_source

    def _wrap(self, call: str) -> str:
        # Inject when the page has no install or an older version (a tab that
        # stayed open across an update keeps the old script otherwise).
        src = self._source()
        m = _VERSION.search(src)
        need = (
            f"!window.__freyjaJev||window.__freyjaJev.version!=={m.group(1)}"
            if m
            else "typeof window.__freyjaJev==='undefined'"
        )
        return f"(function(){{if({need}){{{src}\n}}return window.__freyjaJev.{call};}})()"

    async def _js(self, call: str, *, count: bool = True, reset: bool = True) -> Any:
        """Run one `window.__freyjaJev.<call>` and decode its JSON. `count=False`
        keeps best-effort calls (settling) out of the failure streak that decides
        the fallback to AX."""
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._run_js, self.bundle, self._wrap(call)),
                timeout=self.timeout_s + 1,
            )
        except asyncio.TimeoutError as exc:
            self.fail_streak += count
            raise DOMUnavailable(f"page JavaScript timed out after {self.timeout_s:.0f}s") from exc
        except DOMUnavailable:
            self.fail_streak += count
            raise
        except Exception as exc:  # noqa: BLE001
            self.fail_streak += count
            raise DOMUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            data = _decode(raw)
        except (TypeError, ValueError) as exc:
            self.fail_streak += count
            raise DOMUnavailable(f"page returned non-JSON: {_clip(str(raw), 80)!r}") from exc
        if count and reset:
            self.fail_streak = 0
        return data

    # ─── tab ─────────────────────────────────────────────────────────

    async def pin_active(self) -> None:
        """Pin the run to the tab that is active now, so later tab switches by the
        person do not move it."""
        if self._injected_js:
            return
        try:
            self.tab_id = await asyncio.to_thread(active_tab_id, self.bundle)
        except DOMUnavailable as exc:
            self._log({"event": "dom_pin_failed", "error": str(exc)})
            return
        self._log({"event": "dom_tab", "tab": self.tab_id, "how": "active at start"})
        try:
            self.known_tabs = await asyncio.to_thread(tab_ids, self.bundle)
        except DOMUnavailable as exc:
            self._log({"event": "dom_tabs_unread", "error": str(exc)})

    async def current_url(self) -> str:
        """The pinned tab's address ("" when the page does not answer)."""
        try:
            q = await self._js("quiet()", count=False)
        except DOMUnavailable:
            return ""
        return str(q.get("u") or "") if isinstance(q, dict) else ""

    async def describe_tab(self) -> str:
        """The pinned tab as "'title' (url)", for a message to the caller. Read
        from the browser's tab properties: running the page script there would
        install it in a tab that is not the run's."""
        if not self.tab_id:
            return "a tab that could not be identified"
        try:
            title, url = await asyncio.to_thread(tab_info, self.bundle, self.tab_id)
        except DOMUnavailable:
            return "a tab that did not answer"
        return f"{title!r} ({url})" if title else url or "an unnamed tab"

    async def open_url(self, url: str) -> ActionRecord:
        """Open `url`: in the tab this run opened earlier (items, a second address),
        else in a new tab. The person's own tabs are never navigated away."""
        t0 = time.perf_counter()
        if self._injected_js:
            self.url = url
            return ActionRecord(kind="open_url", description=f"Open {url}", duration_ms=0, ok=True)
        if self.own_tab and self.tab_id:
            try:
                await self._js(f"go({json.dumps(url)})", count=False)
            except TabGone:
                self.own_tab = False
            except DOMUnavailable:
                pass  # navigating away can interrupt the answer; the settle below decides
            else:
                await self.settle("open_url", min_s=0.6)
                return ActionRecord(
                    kind="open_url", description=f"Open {url} in the run's tab", duration_ms=_ms(t0), ok=True
                )
            if self.own_tab:
                await self.settle("open_url", min_s=0.6)
                return ActionRecord(
                    kind="open_url", description=f"Open {url} in the run's tab", duration_ms=_ms(t0), ok=True
                )
        try:
            tid = await asyncio.to_thread(new_tab, self.bundle, url)
        except DOMUnavailable as exc:
            return ActionRecord(
                kind="open_url", description=f"Open {url}", duration_ms=_ms(t0), ok=False, error=str(exc)
            )
        if tid:
            self.tab_id = tid
            self.own_tab = True
            remember_tab(self.bundle, tid)
            if self.known_tabs is not None:
                self.known_tabs.add(tid)
            self._log({"event": "dom_tab", "tab": tid, "how": f"opened {url}"})
        await self.settle("open_url", min_s=0.6)
        return ActionRecord(
            kind="open_url", description=f"Open {url} in a new tab", duration_ms=_ms(t0), ok=True
        )

    async def _follow_new_tab(self) -> None:
        """A click opened a new tab (target=_blank or window.open): continue there."""
        if self._injected_js:
            return
        await asyncio.sleep(0.5)
        try:
            tid = await asyncio.to_thread(active_tab_id, self.bundle)
        except DOMUnavailable:
            return
        if tid and tid != self.tab_id:
            # The person may have brought one of their own tabs to the front
            # meanwhile; follow only a tab that did not exist before.
            if self.known_tabs is None or tid in self.known_tabs:
                self._log({"event": "dom_tab_not_followed", "tab": tid,
                           "why": "tabs unknown" if self.known_tabs is None else "an existing tab is in front"})
                return
            self._log({"event": "dom_tab", "tab": tid, "how": "the click opened a new tab"})
            self.tab_id = tid
            self.own_tab = True
            remember_tab(self.bundle, tid)
            self.known_tabs.add(tid)
            await self.settle("open_url", min_s=0.3)

    async def settle(self, kind: str, *, min_s: float = 0.0) -> None:
        """Wait until the page stops changing, at most SETTLE_S[kind] seconds.

        Quiet means the same mutation count and URL across consecutive reads
        ~0.25 s apart with the document complete. A freshly opened page must stay
        quiet for three reads: single-page consoles pause between loading stages,
        and one quiet pair let the first decision see half a page."""
        if min_s:
            await asyncio.sleep(min_s)
        need = 3 if kind == "open_url" else 1
        deadline = time.perf_counter() + SETTLE_S.get(kind, 2.0)
        last: tuple[Any, Any] | None = None
        quiet = 0
        while time.perf_counter() < deadline:
            try:
                q = await self._js("quiet()", count=False)
            except DOMUnavailable:
                if kind == "open_url":  # still loading: the page may not answer yet
                    await asyncio.sleep(0.4)
                    continue
                return
            if not isinstance(q, dict):
                return
            key = (q.get("m"), q.get("u"))
            quiet = quiet + 1 if q.get("rs") == "complete" and key == last else 0
            if quiet >= need:
                return
            last = key
            await asyncio.sleep(0.25)

    # ─── observe ─────────────────────────────────────────────────────

    async def observe(self, target: Any) -> Observation:
        self._target = target
        t0 = time.perf_counter()
        snap = await self._js("snapshot()", reset=False)
        read_ms = int((time.perf_counter() - t0) * 1000)
        try:
            obs = self._build(snap, target, read_ms)
        except Exception as exc:  # noqa: BLE001  # a page we cannot parse is a surface failure
            self.fail_streak += 1
            raise DOMUnavailable(f"unreadable snapshot: {type(exc).__name__}: {exc}") from exc
        self.fail_streak = 0
        return obs

    def _build(self, snap: dict[str, Any], target: Any, read_ms: int) -> Observation:
        if not isinstance(snap, dict):
            raise TypeError(f"snapshot is {type(snap).__name__}, not an object")
        url = str(snap.get("url") or "")
        title = str(snap.get("title") or "")
        self.url, self.title = url, title
        window = f"{title} ({url})" if url else title or "(untitled page)"
        elements: list[DOMElement] = []

        def add(**kw: Any) -> None:
            elements.append(DOMElement(index=len(elements) + 1, bounds=(0, 0, 0, 0), enabled=True, window=window, **kw))

        for a in snap.get("actions") or []:
            kind = a.get("kind") or "click"
            role = KIND_ROLES.get(kind, "AXButton")
            if kind == "toggle" and a.get("role") in ("radio", "menuitemradio"):
                role = "AXRadioButton"  # one choice of a group, not an on/off box
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
            add(
                role=role,
                subrole="AXSecureTextField" if secure else None,
                label=_clip(label, MAX_LABEL_CHARS),
                value=value_s,
                focused=bool(a.get("focused")),
                kind="type" if kind in ("fill", "select") else "click",
                offscreen=False,
                below=a.get("offscreen") or None,
                dom_id=int(a.get("id", 0)),
                guard=str(a.get("guard") or ""),
                raw_label=raw,
                context=ctx,
                dom_kind=kind,
                expanded=a.get("expanded"),
                enter_hint=str(a.get("enter") or ""),
            )
            if kind == "select":
                # Each option is its own click target, so choosing one is a click
                # on a listed row, not text Jev has to produce.
                for o in (a.get("options") or [])[:MAX_OPTIONS]:
                    text = str(o.get("t") or o.get("v") or "").strip()
                    if not text:
                        continue
                    add(
                        role="AXMenuItem",
                        subrole=None,
                        label=_clip(f"{text} (option of {raw or 'list'})", MAX_LABEL_CHARS),
                        value="selected" if o.get("sel") else None,
                        focused=False,
                        kind="click",
                        offscreen=False,
                        below=a.get("offscreen") or None,
                        dom_id=int(a.get("id", 0)),
                        guard=str(a.get("guard") or ""),
                        raw_label=text,
                        context=raw,
                        dom_kind="option",
                        option_value=str(o.get("v") if o.get("v") is not None else text),
                    )
        truncated = len(elements) > MAX_ROWS
        if truncated:
            near = [e for e in elements if not e.below]
            far = [e for e in elements if e.below]
            elements = (near + far)[:MAX_ROWS]
            for i, e in enumerate(elements, 1):
                e.index = i
        text = str(snap.get("text") or "")
        shown = []
        for d in snap.get("dialogs") or []:
            if not isinstance(d, dict):
                continue
            msg = str(d.get("message") or "").strip()
            shown.append(f"[page {d.get('kind')}] {msg}")
            if d.get("kind") == "confirm" and not d.get("answer"):
                self.declined_confirm = msg or "(no text)"
        if shown:
            text = "\n".join(shown) + "\n" + text
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
        if action == "open_url":
            return await self.open_url(str(args[0]))
        if action == "click":
            el = args[0]
            if kwargs.get("double"):
                return await self._fail("click", "double-click is not supported on the DOM surface")
            if getattr(el, "dom_kind", "") == "option":
                return await self._act_on(
                    el, "select", el.option_value, f"Choose {el.raw_label!r} in {el.context!r}", "click",
                    settle="click",
                )
            return await self._act_on(el, "click", None, f"Click {el.role} {el.label!r}", "click", settle="click")
        if action == "type_into":
            el, text = args[0], args[1]
            return await self._type_into(el, text, replace=bool(kwargs.get("replace", True)))
        if action == "press":
            key = KEY_MAP.get(str(args[0]).lower())
            if key is None:
                return await self._fail("press_key", f"{args[0]} is not a page key (the DOM surface sends Return, Escape, Tab)")
            return await self._act_on(None, "key", key, f"Press {args[0]}", "press_key", settle="key")
        if action == "scroll":
            down = bool(kwargs.get("down", True))
            return await self._scroll(down)
        # launch, click_point, type_raw: not page operations.
        return await getattr(self.actuator, action)(*args, **kwargs)

    async def _fail(self, kind: str, error: str, t0: float | None = None) -> ActionRecord:
        ms = _ms(t0) if t0 else 0
        return ActionRecord(kind=kind, description="", duration_ms=ms, ok=False, error=error)

    async def _type_into(self, el: DOMElement, text: str, *, replace: bool) -> ActionRecord:
        if el.subrole == "AXSecureTextField":
            return await self._fail("type_text", "refusing to type into a secure field")
        desc = f"Type {text!r} into {el.label!r}"
        if el.dom_kind == "select":
            return await self._act_on(el, "select", text, desc, "type_text", settle="type")
        arg = {"text": text, "mode": "replace" if replace else "append"}
        return await self._act_on(el, "fill", arg, desc, "type_text", expect=(text, replace), settle="type")

    async def _scroll(self, down: bool) -> ActionRecord:
        t0 = time.perf_counter()
        vp = self._viewport_h or 600
        dy = int(vp * 0.8) * (1 if down else -1)
        try:
            res = await self._js(f"act(0,'scroll',{json.dumps(dy)},'')")
        except DOMUnavailable as exc:
            return await self._fail("scroll", f"dom transport: {exc}", t0)
        ok = bool(isinstance(res, dict) and res.get("ok"))
        if ok:
            await self.settle("scroll")
        return ActionRecord(
            kind="scroll",
            description=f"Scroll {'down' if down else 'up'}"
            + (f" ({res.get('readback')})" if isinstance(res, dict) and res.get("readback") else ""),
            duration_ms=_ms(t0),
            ok=ok,
            error=None if ok else str((res or {}).get("reason") or "scroll failed"),
        )

    async def _call_act(self, dom_id: int, op: str, arg: Any, guard: str) -> dict[str, Any]:
        args = [dom_id, op, arg, guard] + ([True] if self.may_confirm else [])
        call = "act(" + ",".join(json.dumps(a) for a in args) + ")"
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
        settle: str = "",
    ) -> ActionRecord:
        t0 = time.perf_counter()

        def done(ok: bool, error: str | None = None) -> ActionRecord:
            return ActionRecord(kind=kind, description=desc, duration_ms=_ms(t0), ok=ok, error=error)

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
        if res.get("newTab"):
            await self._follow_new_tab()
        elif settle:
            await self.settle(settle)
        rec = done(True)
        _note_dialogs(rec, res.get("dialogs"))
        return rec

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


def _note_dialogs(rec: ActionRecord, dialogs: Any) -> None:
    """Fold the page's dialogs raised by this action into its record."""
    for d in dialogs or []:
        if not isinstance(d, dict):
            continue
        msg = str(d.get("message") or "").strip()
        if d.get("kind") == "confirm" and not d.get("answer"):
            rec.confirm_declined = msg or "(no text)"
        else:
            rec.dialog = (rec.dialog + " | " if rec.dialog else "") + f"{d.get('kind')}: {msg}"
    if rec.dialog:
        rec.description = f"{rec.description} (page {rec.dialog})"


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
