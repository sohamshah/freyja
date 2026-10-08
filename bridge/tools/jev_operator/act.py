"""Execute one chosen action through `freyja_native`, with the same UI
contract as the atomic computer tools: an `action_planned` event and a
short highlight before anything lands, a `screenshot_frame` afterwards,
and cooperative cancellation through the shared cancel event.
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import math
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from bridge.tools.computer_tools import (
    ACTION_HIGHLIGHT_MS,
    ComputerToolSpec,
    EmittedFrame,
    _await_highlight_or_cancel,
    _emit_frame,
    _ensure_dims,
    _fire,
    _native_to_api,
)
from bridge.tools.jev_operator.observe import Element

IRREVERSIBLE_HIGHLIGHT_MS = 1500
# Menu-bar items and open menus are not inside app windows; for them only the
# frontmost-app check applies (the menu bar belongs to the frontmost app).
MENU_ROLES = {"AXMenuBarItem", "AXMenuItem"}
# Controls clicked with the AXPress action rather than a synthetic mouse event.
# The press goes to the element itself, so it neither depends on what is drawn
# over the control nor on the control accepting synthesized clicks; a pointer
# click remains the fallback when the element cannot be pressed.
PRESS_ROLES = {
    "AXButton",
    "AXCheckBox",
    "AXRadioButton",
    "AXMenuItem",
    "AXMenuBarItem",
    "AXPopUpButton",
    "AXMenuButton",
    "AXDisclosureTriangle",
    "AXLink",
}
# Window layers that can intercept a click: normal windows (0) through floating
# panels and system alerts (8). The Dock's window (layer 20) spans the whole
# display although it is mostly transparent, so it and the menu bar (24/25) and
# lock screen (2000+) above it are left out; the frontmost check covers the lock.
OCCLUDER_MAX_LAYER = 20
# Out-of-process panels an app presents as its own (sandboxed open/save panels).
HOSTED_PANEL_PREFIXES = ("com.apple.appkit.xpc.",)
_BUNDLE_ID = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+){2,}")
# Keyboard or pointer input this recent that the operator did not send means a
# person is using the machine; the guard then stops instead of taking focus back.
HUMAN_INPUT_WINDOW_S = 3.0
ACTIVATION_SETTLE_S = 0.3


class Cancelled(RuntimeError):
    pass


class Refused(RuntimeError):
    """Input would not reach the target app, so nothing was sent. `fatal` means
    the screen is not under the operator's control (focus taken by another app,
    target window covered); otherwise only the chosen point was bad and the loop
    can try a different action."""

    def __init__(self, message: str, *, fatal: bool) -> None:
        super().__init__(message)
        self.fatal = fatal


@functools.cache
def _cg_idle() -> Callable[[int, int], float]:
    lib = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    fn = lib.CGEventSourceSecondsSinceLastEventType
    fn.restype = ctypes.c_double
    fn.argtypes = [ctypes.c_int32, ctypes.c_uint32]
    return fn


def _same_frame(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return all(abs(p - q) <= 2 for p, q in zip(a, b))


def seconds_since_input() -> float | None:
    """Seconds since the last keyboard, mouse or trackpad event of any kind."""
    try:
        return float(_cg_idle()(1, 0xFFFFFFFF))  # HID system state, any input event
    except (OSError, AttributeError):
        return None


def frontmost_pid() -> int | None:
    """Pid of the active app according to Launch Services, queried fresh on each
    call. The `is_frontmost` flag on native windows comes from
    NSWorkspace.frontmostApplication, which is not refreshed in a process that
    does not run the main run loop, such as the bridge."""
    try:
        asn = subprocess.run(
            ["/usr/bin/lsappinfo", "front"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(
            ["/usr/bin/lsappinfo", "info", "-only", "pid", asn],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"pid"\s*=\s*(\d+)', out)
    return int(m.group(1)) if m else None


@dataclass
class ActionRecord:
    kind: str
    description: str
    duration_ms: int
    ok: bool
    error: str | None = None
    # A web page's alert()/prompt() text raised by this action, and the question
    # of a confirm() the run answered Cancel (it may not confirm on its own).
    dialog: str | None = None
    confirm_declined: str | None = None


class Actuator:
    def __init__(self, spec: ComputerToolSpec, native: Any, *, settle_ms: int = 350) -> None:
        self.spec = spec
        self.native = native
        self.settle_ms = settle_ms
        self.window_id: int | None = None
        self.target_pid: int | None = None
        self.target_bundle = ""
        self.target_name = ""
        self.frontmost: Callable[[], int | None] = frontmost_pid
        self.idle_seconds: Callable[[], float | None] = seconds_since_input
        # Frames of the target app's AX windows, from the last observation.
        self.window_frames: list[tuple[float, float, float, float]] = []
        self._last_input: float | None = None

    def set_target(self, pid: int, bundle: str, name: str) -> None:
        self.target_pid, self.target_bundle, self.target_name = pid, bundle, name

    def _check_cancel(self) -> None:
        if self.spec.cancel_event.is_set():
            raise Cancelled("cancelled by emergency stop")

    async def _planned(
        self,
        action: str,
        description: str,
        *,
        x: int | None = None,
        y: int | None = None,
        highlight_ms: int = ACTION_HIGHLIGHT_MS,
    ) -> None:
        self._check_cancel()
        await _ensure_dims(self.spec, self.native)
        evt: dict[str, Any] = {
            "type": "action_planned",
            "sessionId": self.spec.session_id,
            "action": action,
            "description": description,
        }
        if x is not None and y is not None:
            ax, ay = _native_to_api(self.spec, x, y)
            evt["x"], evt["y"] = ax, ay
        await _fire(self.spec.emit_event, evt)
        if not await _await_highlight_or_cancel(self.spec, highlight_ms):
            raise Cancelled("cancelled during highlight")

    def _window_at(self, x: float, y: float) -> Any | None:
        for w in self.native.list_windows(include_helpers=True):
            if w.layer < 0 or w.layer >= OCCLUDER_MAX_LAYER:
                continue
            b = w.bounds
            if b.x <= x < b.x + b.w and b.y <= y < b.y + b.h:
                return w
        return None

    def _owned(self, w: Any) -> bool:
        return w.pid == self.target_pid or str(w.bundle).startswith(HOSTED_PANEL_PREFIXES)

    async def _guard(
        self,
        action: str,
        t0: float,
        x: int | None = None,
        y: int | None = None,
        role: str | None = None,
        window: tuple[float, float, float, float] | None = None,
    ) -> None:
        """Runs after the highlight, immediately before input is sent, so focus
        stolen during the highlight cannot redirect a click or keystroke."""
        try:
            await self._ensure_input_reaches_target(x, y, role, window)
        except Refused as exc:
            await self._executed(action, t0, ok=False, error=str(exc))
            raise

    async def _ensure_input_reaches_target(
        self,
        x: int | None,
        y: int | None,
        role: str | None,
        window: tuple[float, float, float, float] | None = None,
    ) -> None:
        """`window` is the frame of the window the input is meant for."""
        if self.target_pid is None:
            return
        pid: int | None = None
        hit: Any | None = None
        for attempt in range(2):
            if attempt:
                if await asyncio.to_thread(self._user_active):
                    raise Refused(
                        f"{self.target_name} lost focus while someone is using the computer "
                        "(keyboard or mouse input in the last few seconds), so the operator "
                        "stopped instead of taking focus back",
                        fatal=True,
                    )
                await self.focus(self.target_bundle)
            pid = await asyncio.to_thread(self.frontmost)
            if pid != self.target_pid:
                continue
            if x is None or y is None:
                front = await asyncio.to_thread(self._front_window)
                if front is not None and self._other_window(front, window):
                    raise Refused(
                        f"another {self.target_name} window {front.title!r} is in front of the "
                        "one this input is meant for",
                        fatal=False,
                    )
                return
            if role in MENU_ROLES:
                return
            hit = await asyncio.to_thread(self._window_at, x, y)
            if hit is None:
                raise Refused(
                    f"({x}, {y}) is not inside any window of {self.target_name}", fatal=False
                )
            if self._owned(hit):
                if self._other_window(hit, window):
                    raise Refused(
                        f"({x}, {y}) is covered by another {self.target_name} window "
                        f"{hit.title!r}; bring the intended window to the front first "
                        "(for example from the Window menu)",
                        fatal=False,
                    )
                return
        if pid != self.target_pid:
            raise Refused(
                f"{self.target_name} is not the frontmost app (pid {pid} has focus), "
                "so input would go to another app",
                fatal=True,
            )
        raise Refused(
            f"({x}, {y}) is covered by a {hit.bundle} window {hit.title!r}, "
            f"not {self.target_name}",
            fatal=True,
        )

    def _front_window(self) -> Any | None:
        for w in self.native.list_windows(include_helpers=False):
            if w.pid == self.target_pid and getattr(w, "layer", 0) == 0:
                return w
        return None

    def _other_window(self, win: Any, intended: tuple[float, float, float, float] | None) -> bool:
        """True when `win` is a different window from the last observation. Sheets,
        popovers and panels have frames of their own and do not count."""
        if intended is None:
            return False
        b = win.bounds
        frame = (b.x, b.y, b.w, b.h)
        return not _same_frame(frame, intended) and any(
            _same_frame(frame, f) for f in self.window_frames
        )

    def _user_active(self) -> bool:
        idle = self.idle_seconds()
        if idle is None:
            return False
        since_ours = math.inf if self._last_input is None else time.monotonic() - self._last_input
        return idle + 0.5 < min(since_ours, HUMAN_INPUT_WINDOW_S)

    async def _executed(
        self, action: str, t0: float, *, ok: bool, error: str | None = None
    ) -> ActionRecord:
        self._last_input = time.monotonic()
        await _fire(
            self.spec.emit_event,
            {
                "type": "action_executed",
                "sessionId": self.spec.session_id,
                "action": action,
                "success": ok,
                "durationMs": int((time.perf_counter() - t0) * 1000),
                **({"error": error} if error else {}),
            },
        )
        return ActionRecord(
            kind=action,
            description="",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            ok=ok,
            error=error,
        )

    async def settle(self, ms: int | None = None) -> None:
        await asyncio.sleep((self.settle_ms if ms is None else ms) / 1000.0)

    async def frame(self, reason: str) -> EmittedFrame | None:
        return await _emit_frame(self.spec, self.native, window_id=self.window_id, reason=reason)

    async def click(
        self, el: Element, *, double: bool = False, irreversible: bool = False
    ) -> ActionRecord:
        x, y = el.center
        desc = f"{'Double-click' if double else 'Click'} {el.role} {el.label!r}"
        await self._planned(
            "click",
            desc,
            x=x,
            y=y,
            highlight_ms=IRREVERSIBLE_HIGHLIGHT_MS if irreversible else ACTION_HIGHLIGHT_MS,
        )
        t0 = time.perf_counter()
        press = getattr(self.native, "ax_press", None)
        if press and not double and el.role in PRESS_ROLES and self.target_pid is not None:
            await self._guard("click", t0)
            try:
                pressed = await asyncio.to_thread(
                    press, self.target_pid, x, y, el.role, el.bounds
                )
            except Exception:  # noqa: BLE001
                pressed = False
            if pressed:
                await self.settle()
                await self.frame("post_action")
                rec = await self._executed("click", t0, ok=True)
                rec.description = "AXPress"
                return rec
            if el.role == "AXMenuItem":
                # Usually its menu closed; the guard does not hit-test menu points, and a
                # pointer click there would land on whatever is underneath.
                return await self._executed(
                    "click", t0, ok=False, error="the menu item could not be pressed (menu closed?)"
                )
        await self._guard("click", t0, x, y, el.role, el.window_frame)
        try:
            await asyncio.to_thread(
                self.native.click, x, y, button="left", double=double, modifiers=[]
            )
        except Exception as exc:  # noqa: BLE001
            return await self._executed("click", t0, ok=False, error=str(exc))
        await self.settle()
        await self.frame("post_action")
        return await self._executed("click", t0, ok=True)

    async def type_into(self, el: Element, text: str, *, replace: bool) -> ActionRecord:
        x, y = el.center
        await self._planned("type_text", f"Type {text!r} into {el.label!r}", x=x, y=y)
        t0 = time.perf_counter()
        await self._guard("type_text", t0, x, y, el.role, el.window_frame)
        try:
            if not el.focused:
                await asyncio.to_thread(
                    self.native.click, x, y, button="left", double=False, modifiers=[]
                )
                await asyncio.sleep(0.15)
                if el.role == "AXTextArea":
                    # The focusing click puts the caret under the element's center,
                    # which is mid-document once text fills the view; append instead.
                    await asyncio.to_thread(self.native.press_key, "down", modifiers=["cmd"])
                    await asyncio.sleep(0.05)
            if replace and el.value:
                # When the text ends in an unfinished word, the first Cmd+A only makes
                # macOS commit its pending autocorrection; the second one selects.
                # (Other shortcuts are not affected; press() doubles Cmd+A too.)
                for _ in range(2):
                    await asyncio.to_thread(self.native.press_key, "a", modifiers=["cmd"])
                    await asyncio.sleep(0.1)
            await asyncio.to_thread(self.native.type_text, text)
        except Exception as exc:  # noqa: BLE001
            return await self._executed("type_text", t0, ok=False, error=str(exc))
        await self.settle()
        await self.frame("post_action")
        return await self._executed("type_text", t0, ok=True)

    async def press(
        self, combo: str, *, window: tuple[float, float, float, float] | None = None
    ) -> ActionRecord:
        parts = combo.split("+")
        key = parts[-1]
        modifiers = [m for m in parts[:-1]]
        await self._planned("press_key", f"Press {combo}")
        t0 = time.perf_counter()
        await self._guard("press_key", t0, window=window)
        select_all = key.lower() == "a" and [m.lower() for m in modifiers] in (["cmd"], ["command"])
        try:
            for _ in range(2 if select_all else 1):
                await asyncio.to_thread(self.native.press_key, key, modifiers=modifiers)
                if select_all:
                    await asyncio.sleep(0.1)
        except Exception as exc:  # noqa: BLE001
            return await self._executed("press_key", t0, ok=False, error=str(exc))
        await self.settle()
        await self.frame("post_action")
        return await self._executed("press_key", t0, ok=True)

    async def scroll(self, at: tuple[int, int] | None, *, down: bool) -> ActionRecord:
        x, y = at if at else (None, None)
        await self._planned("scroll", f"Scroll {'down' if down else 'up'}", x=x, y=y)
        t0 = time.perf_counter()
        await self._guard("scroll", t0, x, y)
        try:
            await asyncio.to_thread(self.native.scroll, 0, 6 if down else -6, x=x, y=y)
        except Exception as exc:  # noqa: BLE001
            return await self._executed("scroll", t0, ok=False, error=str(exc))
        await self.settle()
        await self.frame("post_action")
        return await self._executed("scroll", t0, ok=True)

    async def click_point(
        self,
        x: int,
        y: int,
        *,
        description: str,
        window: tuple[float, float, float, float] | None = None,
    ) -> ActionRecord:
        await self._planned("click", description, x=x, y=y)
        t0 = time.perf_counter()
        await self._guard("click", t0, x, y, window=window)
        try:
            await asyncio.to_thread(
                self.native.click, x, y, button="left", double=False, modifiers=[]
            )
        except Exception as exc:  # noqa: BLE001
            return await self._executed("click", t0, ok=False, error=str(exc))
        await self.settle()
        await self.frame("post_action")
        return await self._executed("click", t0, ok=True)

    async def type_raw(self, text: str) -> ActionRecord:
        await self._planned("type_text", f"Type {text!r}")
        t0 = time.perf_counter()
        await self._guard("type_text", t0)
        try:
            await asyncio.to_thread(self.native.type_text, text)
        except Exception as exc:  # noqa: BLE001
            return await self._executed("type_text", t0, ok=False, error=str(exc))
        await self.settle()
        await self.frame("post_action")
        return await self._executed("type_text", t0, ok=True)

    async def launch(self, app_name: str) -> ActionRecord:
        await self._planned("launch_app", f"Open {app_name}")
        t0 = time.perf_counter()
        flag = "-b" if _BUNDLE_ID.fullmatch(app_name) else "-a"
        try:
            proc = await asyncio.to_thread(
                subprocess.run, ["open", flag, app_name], capture_output=True, text=True, timeout=15
            )
            if proc.returncode != 0:
                return await self._executed(
                    "launch_app",
                    t0,
                    ok=False,
                    error=proc.stderr.strip() or f"open {flag} exited {proc.returncode}",
                )
        except Exception as exc:  # noqa: BLE001
            return await self._executed("launch_app", t0, ok=False, error=str(exc))
        await self.settle(1200)
        await self.frame("post_action")
        return await self._executed("launch_app", t0, ok=True)

    async def open_url(self, url: str) -> ActionRecord:
        """Open `url` in a new tab of the target browser, without the address bar.

        Arc and Chrome get the tab through AppleScript (an `open` from another
        app can land in a separate "Little Arc" window); other apps hand the
        URL to `open -b`."""
        from bridge.tools.jev_operator import dom_surface  # noqa: PLC0415

        await self._planned("open_url", f"Open {url}")
        t0 = time.perf_counter()
        bundle = getattr(self, "target_bundle", "") or ""
        try:
            if dom_surface.supported(bundle):
                await asyncio.to_thread(dom_surface.new_tab, bundle, url)
            else:
                cmd = ["open", "-b", bundle, url] if bundle else ["open", url]
                proc = await asyncio.to_thread(
                    subprocess.run, cmd, capture_output=True, text=True, timeout=15
                )
                if proc.returncode != 0:
                    return await self._executed(
                        "open_url", t0, ok=False, error=proc.stderr.strip() or "open failed"
                    )
        except Exception as exc:  # noqa: BLE001
            return await self._executed("open_url", t0, ok=False, error=str(exc))
        await self.settle(2500)
        await self.frame("post_action")
        return await self._executed("open_url", t0, ok=True)

    async def focus(self, bundle: str) -> None:
        try:
            await asyncio.to_thread(self.native.focus_app, bundle)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(ACTIVATION_SETTLE_S)
