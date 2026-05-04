"""
freyja_native — native macOS bindings for computer-use tools.

Thin Python wrapper over the Rust pyo3 extension. The extension itself
(`_native`) is intentionally low-level; this module adds:

- typed dataclasses for what `list_windows()` / `get_frontmost_window()`
  / `list_displays()` return
- a single `screenshot()` helper that returns `ScreenshotFrame` with the
  PNG bytes *and* dimensions parsed from the header (the Rust side
  doesn't currently emit dimensions separately)
- a `Permissions` helper that groups the screen-recording and
  accessibility probes
- error types re-exported so callers don't need to catch generic
  `RuntimeError`

The intelligence of computer-use lives in the bridge tool layer
(`bridge/tools/computer_tools.py`). This package is purely the driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from . import _native  # type: ignore[attr-defined]


# ─── Types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Bounds:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[int, int]:
        return int(self.x + self.w / 2), int(self.y + self.h / 2)


@dataclass(frozen=True)
class DisplayInfo:
    id: int
    width: int
    height: int
    scale: float
    is_primary: bool


@dataclass(frozen=True)
class WindowInfo:
    id: int
    pid: int
    bundle: str
    title: str
    bounds: Bounds
    is_frontmost: bool
    layer: int


@dataclass(frozen=True)
class ScreenshotFrame:
    png: bytes  # may actually be JPEG — the name is historical
    width: int
    height: int
    format: str = "png"
    capture_ms: float = 0.0

    @property
    def mime_type(self) -> str:
        return "image/jpeg" if self.format == "jpeg" else "image/png"

    def __repr__(self) -> str:
        return f"ScreenshotFrame({self.width}x{self.height}, {len(self.png)} bytes, {self.format})"


# ─── Permissions ────────────────────────────────────────────────────────


class Permissions:
    """Lightweight facade over the permission probes."""

    @staticmethod
    def screen_recording() -> bool:
        return bool(_native.check_screen_recording_permission())

    @staticmethod
    def prompt_screen_recording() -> bool:
        return bool(_native.prompt_screen_recording_permission())

    @staticmethod
    def accessibility() -> bool:
        return bool(_native.check_accessibility_permission())

    @staticmethod
    def prompt_accessibility() -> bool:
        return bool(_native.prompt_accessibility_permission())

    @classmethod
    def all_granted(cls) -> bool:
        return cls.screen_recording() and cls.accessibility()

    @staticmethod
    def input_self_test() -> dict:
        """Actually try to inject input and verify it landed."""
        try:
            start = _native.cursor_position()
        except Exception as exc:
            return {"ok": False, "before": None, "after": None, "delta_px": 0, "message": f"cursor_position failed: {exc}"}
        target = (start[0] + 5, start[1] + 5)
        try:
            _native.move_mouse(target[0], target[1])
        except Exception as exc:
            return {"ok": False, "before": start, "after": None, "delta_px": 0, "message": f"move_mouse failed: {exc}"}
        try:
            end = _native.cursor_position()
        except Exception as exc:
            return {"ok": False, "before": start, "after": None, "delta_px": 0, "message": f"cursor_position(after) failed: {exc}"}
        dx = abs(end[0] - start[0])
        dy = abs(end[1] - start[1])
        delta = max(dx, dy)
        try:
            _native.move_mouse(start[0], start[1])
        except Exception:
            pass
        ok = delta >= 4
        message = f"Input injection works (cursor moved {delta}px)." if ok else (
            f"Input injection FAILED: cursor didn't move (delta={delta}px). "
            f"CGEvent injection is being silently dropped by macOS."
        )
        return {"ok": ok, "before": start, "after": end, "delta_px": delta, "message": message}


# ─── Display + screenshot ───────────────────────────────────────────────


def list_displays() -> list[DisplayInfo]:
    raw: Sequence[dict[str, Any]] = _native.list_displays()
    return [
        DisplayInfo(
            id=int(d["id"]), width=int(d["width"]), height=int(d["height"]),
            scale=float(d["scale"]), is_primary=bool(d["is_primary"]),
        )
        for d in raw
    ]


def _parse_image_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in (0xC0, 0xC2):
                h = int.from_bytes(data[i + 5 : i + 7], "big")
                w = int.from_bytes(data[i + 7 : i + 9], "big")
                return w, h
            i += 2 + seg_len
    return 0, 0


def _proxy_screenshot(
    proxy_url: str, *, display_id: int | None, max_dim: int | None,
    format: str, quality: int,
) -> ScreenshotFrame:
    import time, urllib.parse, urllib.request
    params: list[tuple[str, str]] = [("format", format), ("quality", str(int(quality)))]
    if display_id is not None:
        params.append(("display_id", str(int(display_id))))
    if max_dim is not None:
        params.append(("max_dim", str(int(max_dim))))
    url = f"{proxy_url}/capture?{urllib.parse.urlencode(params)}"
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = resp.read()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    w, h = _parse_image_dimensions(data)
    return ScreenshotFrame(png=data, width=w, height=h, format=format.lower(), capture_ms=elapsed_ms)


def screenshot(
    *, display_id: int | None = None, window_id: int | None = None,
    max_dim: int | None = None, format: str = "png", quality: int = 75,
) -> ScreenshotFrame:
    """Capture a screenshot with automatic proxy fallback for Screen Recording TCC."""
    import os, time
    have_sr = Permissions.screen_recording()
    proxy_url = os.environ.get("AGENT_HARNESS_CAPTURE_URL", "").strip()
    if not have_sr and proxy_url and window_id is None:
        try:
            return _proxy_screenshot(proxy_url, display_id=display_id, max_dim=max_dim, format=format, quality=quality)
        except Exception:
            pass
    t0 = time.perf_counter()
    if window_id is not None:
        raw = _native.capture_window(int(window_id), max_dim, format, int(quality))
    else:
        raw = _native.capture_screen(int(display_id) if display_id is not None else None, max_dim, format, int(quality))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    b = bytes(raw)
    w, h = _parse_image_dimensions(b)
    return ScreenshotFrame(png=b, width=w, height=h, format=format.lower(), capture_ms=elapsed_ms)


# ─── Windows ────────────────────────────────────────────────────────────


def _window_from_raw(raw: dict[str, Any]) -> WindowInfo:
    (x, y, w, h) = raw["bounds"]
    return WindowInfo(
        id=int(raw["id"]), pid=int(raw["pid"]), bundle=str(raw["bundle"]),
        title=str(raw["title"]), bounds=Bounds(x=float(x), y=float(y), w=float(w), h=float(h)),
        is_frontmost=bool(raw["is_frontmost"]), layer=int(raw["layer"]),
    )


def list_windows(*, include_helpers: bool = False) -> list[WindowInfo]:
    raw = _native.list_windows()
    out = [_window_from_raw(w) for w in raw]
    if not include_helpers:
        out = [w for w in out if w.layer == 0 and w.bundle != "Window Server"]
    return out


def get_frontmost_window() -> WindowInfo | None:
    raw = _native.get_frontmost_window()
    return _window_from_raw(raw) if raw is not None else None


def focus_window(window_id: int) -> None:
    _native.focus_window(window_id)


def focus_app(bundle_id: str) -> None:
    _native.focus_app(bundle_id)


# ─── Input proxy ────────────────────────────────────────────────────────


def _input_proxy_url() -> str | None:
    """Return the input proxy URL if set and Accessibility is missing."""
    import os
    return os.environ.get("FREYJA_INPUT_URL", "").strip() or None


def _proxy_input(action: str, **params: Any) -> None:
    """Route an input action through the Electron main process proxy."""
    import json, urllib.request
    url = _input_proxy_url()
    if not url:
        raise RuntimeError("no input proxy available")
    body = json.dumps({"action": action, **params}).encode()
    req = urllib.request.Request(
        f"{url}/input",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "input proxy error"))


def _have_accessibility() -> bool:
    """Check if this process has Accessibility TCC grant."""
    try:
        return bool(_native.check_accessibility_permission())
    except Exception:
        return False


# ─── Input ──────────────────────────────────────────────────────────────


def click(x: int, y: int, *, button: str = "left", double: bool = False, modifiers: Sequence[str] | None = None) -> None:
    if _have_accessibility():
        _native.click(int(x), int(y), button, double, list(modifiers or []))
    elif _input_proxy_url():
        _proxy_input("click", x=int(x), y=int(y), button=button, double=double, modifiers=list(modifiers or []))
    else:
        _native.click(int(x), int(y), button, double, list(modifiers or []))

def move_mouse(x: int, y: int) -> None:
    if _have_accessibility():
        _native.move_mouse(int(x), int(y))
    elif _input_proxy_url():
        _proxy_input("move_mouse", x=int(x), y=int(y))
    else:
        _native.move_mouse(int(x), int(y))

def type_text(text: str) -> None:
    if _have_accessibility():
        _native.type_text(str(text))
    elif _input_proxy_url():
        _proxy_input("type_text", text=str(text))
    else:
        _native.type_text(str(text))

def press_key(key: str, *, modifiers: Sequence[str] | None = None) -> None:
    if _have_accessibility():
        _native.press_key(str(key), list(modifiers or []))
    elif _input_proxy_url():
        _proxy_input("press_key", key=str(key), modifiers=list(modifiers or []))
    else:
        _native.press_key(str(key), list(modifiers or []))

def key_down(key: str) -> None:
    if _have_accessibility():
        _native.key_down(str(key))
    elif _input_proxy_url():
        _proxy_input("key_down", key=str(key))
    else:
        _native.key_down(str(key))

def key_up(key: str) -> None:
    if _have_accessibility():
        _native.key_up(str(key))
    elif _input_proxy_url():
        _proxy_input("key_up", key=str(key))
    else:
        _native.key_up(str(key))

def scroll(dx: int, dy: int, *, x: int | None = None, y: int | None = None) -> None:
    if _have_accessibility():
        _native.scroll(int(dx), int(dy), x, y)
    elif _input_proxy_url():
        _proxy_input("scroll", dx=int(dx), dy=int(dy), x=x, y=y)
    else:
        _native.scroll(int(dx), int(dy), x, y)

def cursor_position() -> tuple[int, int]:
    return _native.cursor_position()


# ─── Accessibility ──────────────────────────────────────────────────────


def read_ax_tree(pid: int, *, max_depth: int = 8) -> str:
    return _native.read_ax_tree(int(pid), int(max_depth))

def find_ax_element(pid: int, *, role: str | None = None, label: str | None = None, title: str | None = None) -> Bounds | None:
    result = _native.find_ax_element(int(pid), role, label, title)
    if result is None:
        return None
    x, y, w, h = result
    return Bounds(x=float(x), y=float(y), w=float(w), h=float(h))


__all__ = [
    "Bounds", "DisplayInfo", "WindowInfo", "ScreenshotFrame", "Permissions",
    "list_displays", "screenshot", "list_windows", "get_frontmost_window",
    "focus_window", "focus_app", "click", "move_mouse", "type_text",
    "press_key", "key_down", "key_up", "scroll", "cursor_position",
    "read_ax_tree", "find_ax_element",
]
