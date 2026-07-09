"""Live computer verbs — rung 2 of the actuator ladder: direct GUI control.

``computer.do`` hands long jobs to a background mission; these verbs are
the opposite — "click the compose button" happens *now*, inside the live
exchange. The realtime voice model *sees* images (probed live 2026-07-09:
gpt-realtime-2.1 and -mini both read text off a PNG and completed a full
see→inject-image→click loop), so the loop is the SAME visual one Freyja's
computer-use agent runs:

    computer.see   → a screenshot (api_dims PNG + coordinate grid) comes back
    computer.click → click by pixel {x, y} in that image's coordinate space
    (the click's own result screenshot) → look again, act again

Reuse, not reinvention: every action funnels through the SAME atomic
tool classes agent sessions use (``bridge.tools.computer_tools``), so
coordinate translation (API↔native), the 200 ms pre-action highlight
delay, permission preflights, and the capture/input proxy fallbacks
behave identically to agent clicks. The image we hand the model is the
api_dims PNG — the exact coordinate space ``ClickTool`` accepts — so a
pixel the model reads off the grid feeds straight through with NO
rescaling. The one divergence from agent sessions is the
``ComputerToolSpec`` construction: voice has no computer-session pane in
the renderer, and contract §11 forbids session-scoped voice events, so
the spec's ``emit_event`` is a documented no-op — the emission *code
path* still runs (timing parity), the events just have no audience. The
cancel event is a fresh, never-set ``asyncio.Event``: voice's emergency
stop is the panic word, which ends the exchange before another verb can
run.

Ref cache: kept only to back the ``ref`` fallback on ``computer.click`` /
``computer.scroll`` (the model's primary mode is pixel {x, y}). ``Verb.run(args)``
carries no session identity, so instead of a per-session cache there is
ONE process-level "last seen" snapshot — honest, not lazy: voice is a
single-operator surface with one active exchange at a time; a superseding
session's first ``see`` simply replaces the snapshot. Refs are numbered
by a process-lifetime counter (``e1..e5`` then ``e6..e12``), never
restarting per snapshot: that is what makes a ref minted by an older
``see`` *detectably* stale (absent from the current map) instead of
silently re-pointing at whatever now occupies its slot.

Gating: every verb re-checks the same enablement signal agent sessions
are built from (``state.computer_enabled``, threaded in by service.py as
``enabled_fn`` so a settings flip applies without rebuilding the
registry). Tool-layer refusals that need operator setup (control
disabled, Screen Recording / Accessibility missing, native module
absent) surface as ok=False with ``data.setup="computer"`` and the tool
layer's own actionable message.
"""

from __future__ import annotations

import asyncio
import base64
import io as _io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from bridge.voice.adapters import mac, screen
from bridge.voice.adapters.mac import as_quoted
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

# ── module state (process-level; see docstring for why not per-session) ────

_TOOLS: Optional[dict[str, Any]] = None
# The shared ComputerToolSpec, kept so cancel_inflight() can trip its
# cancel_event to interrupt a verb mid-action on panic.
_SPEC: Optional[Any] = None
_snapshot: Optional["_Snapshot"] = None
_generation: int = 0
_ref_seq: int = 0  # never resets — stale refs must stay detectable

_FRAMES_DIR = Path.home() / ".freyja" / "voice" / "frames"
_FRAMES_KEEP = 10
_MAX_ELEMENTS = 80
_LABEL_MAX = 60

# Coordinate grid overlaid on every screenshot before it goes to the
# model. Thin lines every _GRID_STEP px with small labels at each line
# aim the model's pixel clicks — measurably better than a bare image.
_GRID_STEP = 100

# Pinned to match computer.do's refusal so the model/HUD see one voice.
_DISABLED_SUMMARY = "computer control is disabled — enable it in settings"
_DISABLED_MESSAGE = (
    "computer control is disabled. Enable it in Settings → Computer "
    "Control, then grant Screen Recording and Accessibility permissions "
    "in System Settings."
)

# Substrings the tool layer uses in its setup-shaped refusals
# (_disabled_result, _require_screen_recording, _require_accessibility,
# the native ImportError path) plus osascript's Accessibility denial.
_SETUP_MARKERS = (
    "computer control is disabled",
    "screen recording permission",
    "accessibility permission",
    "freyja_native not available",
    "assistive access",
)

# AX roles worth resolving a ref for, with plain-English names. The ref
# cache backs computer.click/scroll's `ref` fallback (the model's primary
# mode is pixel {x, y}), so only these interactive roles are tracked.
_INTERACTIVE_ROLES = {
    "AXButton": "button",
    "AXLink": "link",
    "AXTextField": "text field",
    "AXTextArea": "text area",
    "AXSearchField": "search field",
    "AXComboBox": "combo box",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXPopUpButton": "popup",
    "AXMenuButton": "menu button",
    "AXMenuItem": "menu item",
    "AXMenuBarItem": "menu",
    "AXTab": "tab",
    "AXDisclosureTriangle": "disclosure",
}

_MODIFIER_ALIASES = {"command": "cmd", "option": "alt", "opt": "alt", "control": "ctrl"}
_CENTER_RE = re.compile(r"center=\((-?\d+),\s*(-?\d+)\)")
_DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


@dataclass
class _Snapshot:
    """What the last computer.see resolved for the `ref` fallback.
    Coordinates live ONLY here — refs map to (API-space) centers that
    ClickTool/ScrollTool accept directly."""

    generation: int
    app: str
    pid: int
    window: str
    elements: dict[str, tuple[int, int, str]]  # ref → (x, y, label)


# ── tool plumbing ───────────────────────────────────────────────────────────


def _ensure_tools() -> dict[str, Any]:
    """One spec + one tool set per process, built exactly the way the
    bridge builds them for agent sessions (`_build_harness_computer_tools`)
    minus the two session-bound fields; the spec is shared so the
    native/API coordinate-space cache persists across verb calls, same
    as it does across an agent session. Test seam: monkeypatched whole."""
    global _TOOLS, _SPEC
    if _TOOLS is None:
        from bridge.tools.computer_tools import (
            ClickTool,
            ComputerToolSpec,
            FindElementTool,
            PressKeyTool,
            ReadAxTreeTool,
            ScreenshotTool,
            ScrollTool,
            TypeTextTool,
        )

        spec = ComputerToolSpec(
            session_id="voice",
            # No-op by design: there is no renderer pane for voice
            # computer frames, and voice events must not be
            # session-scoped (contract §11). See module docstring.
            emit_event=lambda _evt: None,
            cancel_event=asyncio.Event(),
            enabled=True,  # the voice-side gate runs before any tool call
            require_approval=False,
            owner="voice",
        )
        _SPEC = spec
        _TOOLS = {
            tool.definition.name: tool
            for tool in (
                ScreenshotTool(spec),
                ClickTool(spec),
                TypeTextTool(spec),
                PressKeyTool(spec),
                ScrollTool(spec),
                ReadAxTreeTool(spec),
                FindElementTool(spec),
            )
        }
    return _TOOLS


def cancel_inflight() -> None:
    """Trip the shared cancel event so a computer verb in flight — a long
    type_text, a click waiting on its highlight — aborts at its next
    checkpoint. The service calls this on panic ("stop") so the brake
    reaches mid-action, not just between verbs. Best-effort + idempotent;
    the event is cleared again before the next verb runs."""
    spec = _SPEC
    if spec is not None:
        try:
            spec.cancel_event.set()
        except Exception:  # noqa: BLE001 — a dead brake must never raise
            pass


async def _run_tool(name: str, args: dict[str, Any]) -> Any:
    tools = _ensure_tools()
    # Clear any stale cancel from a prior panic so this fresh verb starts
    # unblocked; cancel_inflight() re-sets it if a new "stop" lands.
    if _SPEC is not None:
        try:
            _SPEC.cancel_event.clear()
        except Exception:  # noqa: BLE001
            pass
    return await tools[name].execute(f"voice-{name}", dict(args))


def _result_text(result: Any) -> str:
    """ToolResult.content is a str or a list of blocks; join the text."""
    content = getattr(result, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _result_image(result: Any) -> Optional[tuple[bytes, str]]:
    """First image block of a ToolResult as (bytes, mime), if any."""
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return None
    for block in content:
        data = getattr(block, "data", None)
        if isinstance(data, str) and data:
            try:
                raw = base64.b64decode(data)
            except Exception:  # noqa: BLE001 — malformed block, keep looking
                continue
            return raw, str(getattr(block, "media_type", "image/png") or "image/png")
    return None


def _needs_setup(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _SETUP_MARKERS)


def _tool_failure(verb_label: str, text: str) -> VerbResult:
    """A tool-layer error as a VerbResult: terse first line for the
    receipt, the tool's full actionable message in error, and
    data.setup='computer' when it is a permissions/enablement problem."""
    stripped = (text or "").strip()
    first = stripped.splitlines()[0][:120] if stripped else f"{verb_label} failed"
    data = {"setup": "computer"} if _needs_setup(stripped) else {}
    return VerbResult(ok=False, summary=first, data=data, error=stripped[:600] or first)


def _gate(enabled_fn: Callable[[], bool]) -> Optional[VerbResult]:
    try:
        enabled = bool(enabled_fn())
    except Exception:  # noqa: BLE001 — a broken gate reads as disabled
        enabled = False
    if enabled:
        return None
    return VerbResult(
        ok=False,
        summary=_DISABLED_SUMMARY,
        data={"setup": "computer"},
        error=_DISABLED_MESSAGE,
    )


# ── the grid overlay (aim aid for the model's pixel clicks) ─────────────────


def _grid_overlay(png_bytes: bytes, w: int, h: int) -> bytes:
    """Draw a light labeled coordinate grid onto the api_dims screenshot,
    return PNG bytes. Thin lines every ~_GRID_STEP px with small coord
    labels at each gridline. This is drawn on the SAME pixels the model
    clicks in (api_dims), so a coordinate the model reads off a label is
    exactly what ClickTool receives — no rescaling.

    Best-effort by contract: any PIL failure returns the original bytes
    (an un-gridded screenshot is still a usable screenshot). Runs
    synchronously; callers wrap it in asyncio.to_thread."""
    try:
        from PIL import Image, ImageDraw  # lazy — keep import free of PIL at boot

        with Image.open(_io.BytesIO(png_bytes)) as src:
            img = src.convert("RGB")
        width, height = img.width, img.height
        draw = ImageDraw.Draw(img, "RGBA")
        line = (255, 60, 60, 90)  # faint red, low alpha — visible, not obscuring
        label_fill = (255, 60, 60, 200)
        label_bg = (0, 0, 0, 130)
        for x in range(_GRID_STEP, width, _GRID_STEP):
            draw.line([(x, 0), (x, height)], fill=line, width=1)
        for y in range(_GRID_STEP, height, _GRID_STEP):
            draw.line([(0, y), (width, y)], fill=line, width=1)
        # Coordinate labels at every grid intersection (skip the origin
        # column/row to keep the top-left readable).
        for x in range(_GRID_STEP, width, _GRID_STEP):
            for y in range(_GRID_STEP, height, _GRID_STEP):
                text = f"{x},{y}"
                # A small dark plate behind the text so it reads on any
                # background; ~5.5px/char at the default bitmap font.
                pad = 1
                tw = int(len(text) * 5.5) + 2 * pad
                th = 9 + 2 * pad
                draw.rectangle([x + 2, y + 2, x + 2 + tw, y + 2 + th], fill=label_bg)
                draw.text((x + 2 + pad, y + 2 + pad), text, fill=label_fill)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — a bare screenshot beats no screenshot
        return png_bytes


def _save_frame(data: bytes, ext: str = "png") -> Optional[str]:
    """Write a see/act screenshot under ~/.freyja/voice/frames (a receipt
    the operator can open later) and prune to the newest _FRAMES_KEEP.
    Names embed epoch-ns, so lexicographic order is capture order.
    Best-effort — an unwritable frames dir must not fail the verb."""
    try:
        _FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        path = _FRAMES_DIR / f"see-{time.time_ns()}.{ext}"
        path.write_bytes(data)
    except OSError:
        return None
    try:
        frames = sorted(p for p in _FRAMES_DIR.iterdir() if p.name.startswith("see-"))
        for old in frames[: max(0, len(frames) - _FRAMES_KEEP)]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass
    return str(path)


@dataclass
class _Frame:
    """A captured, grid-overlaid screenshot ready to hand the model."""

    b64: str
    w: int
    h: int
    path: Optional[str]


async def _capture_grid() -> tuple[Optional[_Frame], Optional[VerbResult]]:
    """Screenshot through the SAME tool the agent uses, overlay the
    coordinate grid, and return a _Frame. On a setup-shaped capture
    failure (Screen Recording denied, native missing) return a
    (None, VerbResult) pair so the caller can surface it; on any other
    capture failure return (None, None) so the caller decides whether the
    action still succeeded without a fresh screenshot.

    api_dims (the coordinate space ClickTool accepts) is populated on the
    spec by the screenshot call; we read the frame's own PNG dimensions
    as the authority (they ARE api_dims by construction — see
    computer_tools._emit_frame) and fall back to the spec / a default."""
    shot = await _run_tool("screenshot", {})
    if getattr(shot, "is_error", False):
        text = _result_text(shot)
        if _needs_setup(text):
            return None, _tool_failure("see", text)
        return None, None
    image = _result_image(shot)
    if image is None:
        return None, None
    raw, mime = image
    # Read the true pixel dimensions of the frame — this is api_dims by
    # construction. PIL failure falls back to the spec's api_dims.
    w = h = 0
    try:
        from PIL import Image  # lazy

        with Image.open(_io.BytesIO(raw)) as im:
            w, h = im.width, im.height
    except Exception:  # noqa: BLE001
        dims = getattr(_SPEC, "api_dims", None) if _SPEC is not None else None
        if isinstance(dims, tuple) and len(dims) == 2:
            w, h = int(dims[0]), int(dims[1])
    if w <= 0 or h <= 0:
        return None, None
    gridded = await asyncio.to_thread(_grid_overlay, raw, w, h)
    path = _save_frame(gridded, "png")
    b64 = base64.b64encode(gridded).decode("ascii")
    return _Frame(b64=b64, w=w, h=h, path=path), None


def _attach(result: VerbResult, frame: Optional[_Frame]) -> VerbResult:
    """Copy a captured frame onto a VerbResult's image_* fields and record
    its saved path in data.screenshotPath. A missing frame leaves the
    result untouched (the action still happened; the model just doesn't
    get a fresh look)."""
    if frame is None:
        return result
    result.image_b64 = frame.b64
    result.image_w = frame.w
    result.image_h = frame.h
    if frame.path:
        result.data = {**(result.data or {}), "screenshotPath": frame.path}
    return result


# ── seeing ──────────────────────────────────────────────────────────────────


async def _resolve_app(app_name: str) -> tuple[str, Optional[int], str, str]:
    """(app, pid, focused-window title, error) via System Events —
    the same osascript seam every other adapter runs through."""
    selector = (
        f"first application process whose name is {as_quoted(app_name)}"
        if app_name
        else "first application process whose frontmost is true"
    )
    script = (
        'tell application "System Events"\n'
        f"set p to {selector}\n"
        'set winName to ""\n'
        "try\n"
        "set winName to name of front window of p\n"
        "end try\n"
        "(name of p) & linefeed & (unix id of p) & linefeed & winName\n"
        "end tell"
    )
    ok, out = await mac.run_osascript(script)
    if not ok:
        return app_name, None, "", out or "System Events unavailable"
    parts = out.split("\n")
    if len(parts) < 2:
        return app_name, None, "", f"unexpected System Events reply: {out[:80]}"
    try:
        pid = int(parts[1].strip())
    except ValueError:
        return app_name, None, "", f"bad pid from System Events: {parts[1][:40]}"
    window = parts[2].strip() if len(parts) > 2 else ""
    return parts[0].strip(), pid, window, ""


def _parse_ax_json(text: str) -> Any:
    """The JSON body of a read_ax_tree result (its first line is prose).
    Truncated 40 KB+ trees fail the parse → the ref cache stays empty
    rather than half-populated (the model can still click by pixel)."""
    idx = text.find("\n")
    if idx < 0:
        return None
    payload = text[idx + 1 :].strip()
    if not payload.startswith(("{", "[")):
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None


def _window_subtree(tree: Any, title: str) -> Any:
    """The AXWindow node matching the focused window's title, else the
    first window found, else None (caller walks the whole tree)."""
    first_window = None
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        if node.get("role") == "AXWindow":
            if title and str(node.get("title") or "") == title:
                return node
            if first_window is None:
                first_window = node
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(reversed(children))
    return first_window


def _label_of(node: dict[str, Any]) -> str:
    for key in ("title", "description", "identifier"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            label = " ".join(value.split())
            return label if len(label) <= _LABEL_MAX else label[: _LABEL_MAX - 1] + "…"
    return ""


def _condense_elements(tree: Any, window_title: str) -> list[tuple[str, str, int, int]]:
    """(role, label, center-x, center-y) for every interactive element of
    the target window, in document order, capped at _MAX_ELEMENTS.
    Bounds are already API-space — ReadAxTreeTool translated them — so
    the centers are directly what ClickTool expects. Feeds the ref cache
    that backs computer.click/scroll's `ref` fallback."""
    root = _window_subtree(tree, window_title) or tree
    found: list[tuple[str, str, int, int]] = []
    stack = [root]
    while stack and len(found) < _MAX_ELEMENTS:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        role = node.get("role")
        plain = _INTERACTIVE_ROLES.get(role) if isinstance(role, str) else None
        if plain is not None:
            bounds = node.get("bounds")
            if (
                isinstance(bounds, list)
                and len(bounds) == 4
                and all(isinstance(v, (int, float)) for v in bounds)
                and bounds[2] > 0
                and bounds[3] > 0
            ):
                cx = int(round(bounds[0] + bounds[2] / 2))
                cy = int(round(bounds[1] + bounds[3] / 2))
                found.append((plain, _label_of(node), cx, cy))
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(reversed(children))
    return found


async def _refresh_snapshot(app_arg: str) -> tuple[str, Optional[int], str, Optional[VerbResult]]:
    """Resolve the target app and (best-effort) rebuild the ref cache from
    its AX tree, so a later computer.click {ref:"e3"} still works. Returns
    (app, pid, window, setup_failure). The ref cache is a convenience
    fallback — an AX-opaque app just yields an empty map and the model
    clicks by pixel — so only a setup-shaped AX failure short-circuits."""
    app, pid, window, err = await _resolve_app(app_arg)
    if pid is None:
        target = app_arg or "the front app"
        return app, None, window, VerbResult(
            ok=False, summary=f"couldn't see {target}: {err[:80]}", error=err
        )

    elements: list[tuple[str, str, int, int]] = []
    ax_res = await _run_tool("read_ax_tree", {"pid": pid})
    ax_text = _result_text(ax_res)
    if getattr(ax_res, "is_error", False):
        if _needs_setup(ax_text):
            return app, pid, window, _tool_failure("see", ax_text)
        # Non-setup AX failure = AX-opaque app; pixel clicking still works.
    else:
        tree = _parse_ax_json(ax_text)
        if tree is not None:
            elements = _condense_elements(tree, window)

    global _snapshot, _generation, _ref_seq
    _generation += 1
    refs: dict[str, tuple[int, int, str]] = {}
    for _role, label, cx, cy in elements:
        _ref_seq += 1
        refs[f"e{_ref_seq}"] = (cx, cy, label)
    _snapshot = _Snapshot(
        generation=_generation, app=app, pid=pid, window=window, elements=refs
    )
    return app, pid, window, None


async def _see(args: dict[str, Any]) -> VerbResult:
    """The eyes: a grid-overlaid api_dims screenshot the model reads and
    clicks against by pixel. Also (best-effort) refreshes the ref cache so
    a `ref`-based click still works, but no AX listing or vision caption
    reaches the model — it sees the pixels now (contract §12.2)."""
    app_arg = str(args.get("app") or "").strip()
    app, pid, window, setup_fail = await _refresh_snapshot(app_arg)
    if setup_fail is not None and pid is None:
        # App resolution failed — nothing to look at.
        return setup_fail

    frame, cap_fail = await _capture_grid()
    if frame is None:
        # No frame. If the AX step reported a setup problem, surface it;
        # otherwise the capture itself is the setup problem.
        if setup_fail is not None:
            return setup_fail
        if cap_fail is not None:
            return cap_fail
        return VerbResult(
            ok=False,
            summary="couldn't capture the screen",
            error="screenshot returned no frame",
        )

    result = VerbResult(
        ok=True,
        summary=f"saw {app}: {frame.w}x{frame.h}",
        data={"app": app, "window": window, "screen": f"{frame.w}x{frame.h}"},
    )
    return _attach(result, frame)


# ── acting ──────────────────────────────────────────────────────────────────


def _lookup_ref(ref: str) -> tuple[int, int, str] | VerbResult:
    snap = _snapshot
    if snap is None or ref not in snap.elements:
        return VerbResult(
            ok=False,
            summary=f"{ref} is stale or unknown — run computer.see first",
            error="stale_ref",
        )
    return snap.elements[ref]


async def _locate_element(element: str) -> tuple[int, int] | VerbResult:
    """Live FindElementTool lookup against the frontmost app (always
    resolved fresh — an app switch since the last see must not send the
    query to a stale pid)."""
    _app, pid, _window, err = await _resolve_app("")
    if pid is None:
        return VerbResult(ok=False, summary=f"couldn't find the front app: {err[:80]}", error=err)
    res = await _run_tool("find_element", {"pid": pid, "label": element})
    text = _result_text(res)
    if getattr(res, "is_error", False):
        return _tool_failure("click", text)
    match = _CENTER_RE.search(text)
    if match is None:
        return VerbResult(
            ok=False,
            summary=f"no element matching {element!r} — run computer.see",
            error=text[:200],
        )
    return int(match.group(1)), int(match.group(2))


async def _vision_locate(target: str) -> tuple[int, int] | VerbResult:
    """Click point for a natural-language target via vision — a fallback
    when the model would rather describe than read a pixel. Screenshot
    through the same tool the frames come from, ask the vision model for
    normalized coordinates, scale into api-space (the space click accepts)
    by the spec's api_dims."""
    shot = await _run_tool("screenshot", {})
    if getattr(shot, "is_error", False):
        return _tool_failure("click", _result_text(shot))
    image = _result_image(shot)
    if image is None:
        return VerbResult(ok=False, summary="couldn't capture the screen to look", error="no_frame")
    dims = getattr(_SPEC, "api_dims", None) if _SPEC is not None else None
    if not (isinstance(dims, tuple) and len(dims) == 2):
        # Screenshot populates api_dims; if it's still missing, read the
        # image's own dimensions so grounding still works.
        try:
            from PIL import Image  # lazy

            with Image.open(_io.BytesIO(image[0])) as im:
                dims = (im.width, im.height)
        except Exception:  # noqa: BLE001
            return VerbResult(ok=False, summary="couldn't size the screen", error="no_dims")
    try:
        located = await screen.locate_in_image(image[0], target)
    except Exception as exc:  # noqa: BLE001 — vision transport failure → words
        err = str(exc).splitlines()[0][:120] if str(exc) else exc.__class__.__name__
        return VerbResult(ok=False, summary=f"couldn't look for {target!r}: {err}", error=err)
    if located is None:
        return VerbResult(
            ok=False,
            summary=f"couldn't spot {target!r} on screen",
            error="not_found — describe it differently, or say where it is",
        )
    fx, fy = located
    return round(fx * dims[0]), round(fy * dims[1])


async def _click(args: dict[str, Any]) -> VerbResult:
    """Click by pixel {x, y} (primary — a coordinate read off the grid in
    the last screenshot's api_dims space), or by ref / element / target
    fallbacks. Then screenshot the result and attach it (see→act→see in
    one hop): the model always gets a fresh look at what the click did."""
    ref = str(args.get("ref") or "").strip()
    element = str(args.get("element") or "").strip()
    target = str(args.get("target") or "").strip()
    x = args.get("x")
    y = args.get("y")
    label = ""
    # Pixel {x, y} is the primary mode — it feeds ClickTool directly in
    # the SAME coordinate space as the screenshot the model just read.
    if isinstance(x, (int, float)) and not isinstance(x, bool) and isinstance(y, (int, float)) and not isinstance(y, bool):
        cx, cy = int(x), int(y)
    elif ref:
        resolved = _lookup_ref(ref)
        if isinstance(resolved, VerbResult):
            return resolved
        cx, cy, label = resolved
    elif element:
        located = await _locate_element(element)
        if isinstance(located, VerbResult):
            return located
        cx, cy = located
        label = element
    elif target:
        # Vision-grounded: click what you can SEE by describing it.
        vloc = await _vision_locate(target)
        if isinstance(vloc, VerbResult):
            return vloc
        cx, cy = vloc
        label = target
    else:
        return VerbResult(
            ok=False,
            summary="click needs x and y, a ref, an element label, or a target to look for",
            error="missing_target",
        )
    res = await _run_tool(
        "click",
        {"x": cx, "y": cy, "description": f"voice: click {label or f'({cx}, {cy})'}"},
    )
    if getattr(res, "is_error", False):
        return _tool_failure("click", _result_text(res))
    result = VerbResult(
        ok=True, summary=f"clicked {label}" if label else f"clicked ({cx}, {cy})"
    )
    frame, _ = await _capture_grid()
    return _attach(result, frame)


def _short(text: str, limit: int = 40) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


async def _type(args: dict[str, Any]) -> VerbResult:
    text = args.get("text")
    if not isinstance(text, str) or not text:
        return VerbResult(ok=False, summary="type needs text", error="missing_text")
    res = await _run_tool("type_text", {"text": text})
    if getattr(res, "is_error", False):
        return _tool_failure("type", _result_text(res))
    # The receipt summary never carries long text verbatim (it is spoken
    # and journaled); the full text stays on the receipt's args.
    result = VerbResult(ok=True, summary=f'typed "{_short(text)}"')
    frame, _ = await _capture_grid()
    return _attach(result, frame)


async def _press(args: dict[str, Any]) -> VerbResult:
    key = str(args.get("key") or "").strip()
    raw_mods = args.get("modifiers")
    modifiers = (
        [str(m).strip().lower() for m in raw_mods if str(m).strip()]
        if isinstance(raw_mods, list)
        else []
    )
    if not key:
        return VerbResult(ok=False, summary="press needs a key", error="missing_key")
    # "cmd+t"-style combos: everything before the last + is a modifier.
    if "+" in key and len(key) > 1:
        parts = [p.strip() for p in key.split("+") if p.strip()]
        if parts:
            key = parts[-1]
            modifiers = [*modifiers, *(p.lower() for p in parts[:-1])]
    modifiers = list(dict.fromkeys(_MODIFIER_ALIASES.get(m, m) for m in modifiers))
    res = await _run_tool("press_key", {"key": key, "modifiers": modifiers})
    if getattr(res, "is_error", False):
        return _tool_failure("press", _result_text(res))
    combo = "+".join([*modifiers, key]) if modifiers else key
    result = VerbResult(ok=True, summary=f"pressed {combo}")
    frame, _ = await _capture_grid()
    return _attach(result, frame)


async def _scroll(args: dict[str, Any]) -> VerbResult:
    direction = str(args.get("direction") or "").strip().lower()
    vec = _DIRECTIONS.get(direction)
    if vec is None:
        return VerbResult(
            ok=False,
            summary="scroll direction must be up, down, left, or right",
            error="bad_direction",
        )
    try:
        amount = int(args.get("amount") or 8)
    except (TypeError, ValueError):
        amount = 8
    amount = max(1, min(50, amount))
    tool_args: dict[str, Any] = {"dx": vec[0] * amount, "dy": vec[1] * amount}
    ref = str(args.get("ref") or "").strip()
    x = args.get("x")
    y = args.get("y")
    if ref:
        resolved = _lookup_ref(ref)
        if isinstance(resolved, VerbResult):
            return resolved
        tool_args["x"], tool_args["y"] = resolved[0], resolved[1]
    elif isinstance(x, (int, float)) and not isinstance(x, bool) and isinstance(y, (int, float)) and not isinstance(y, bool):
        tool_args["x"], tool_args["y"] = int(x), int(y)
    res = await _run_tool("scroll", tool_args)
    if getattr(res, "is_error", False):
        return _tool_failure("scroll", _result_text(res))
    result = VerbResult(ok=True, summary=f"scrolled {direction}")
    frame, _ = await _capture_grid()
    return _attach(result, frame)


async def _menu(args: dict[str, Any]) -> VerbResult:
    raw = args.get("menu_path")
    path = (
        [str(seg).strip() for seg in raw if str(seg).strip()] if isinstance(raw, list) else []
    )
    if len(path) < 2:
        return VerbResult(
            ok=False,
            summary='menu needs a path of at least menu and item, like ["File", "New Tab"]',
            error="bad_menu_path",
        )
    app = str(args.get("app") or "").strip()
    # Nested menu reference, innermost last: for ["Format","Font","Bold"] →
    # menu item "Bold" of menu "Font" of menu item "Font" of menu "Format"
    # of menu bar 1. Every segment goes through as_quoted.
    target = f"menu {as_quoted(path[0])} of menu bar 1"
    for seg in path[1:-1]:
        target = f"menu {as_quoted(seg)} of menu item {as_quoted(seg)} of {target}"
    inner_tell = (
        f"tell process {as_quoted(app)}"
        if app
        else "tell (first application process whose frontmost is true)"
    )
    script = "\n".join(
        [
            'tell application "System Events"',
            inner_tell,
            "set frontmost to true",
            f"click menu item {as_quoted(path[-1])} of {target}",
            "end tell",
            "end tell",
        ]
    )
    ok, out = await mac.run_osascript(script)
    if not ok:
        if _needs_setup(out):
            return _tool_failure("menu", out)
        return VerbResult(ok=False, summary=f"menu failed: {out[:80]}", error=out)
    result = VerbResult(ok=True, summary="menu: " + " → ".join(path))
    frame, _ = await _capture_grid()
    return _attach(result, frame)


async def _open_url(args: dict[str, Any]) -> VerbResult:
    url = str(args.get("url") or "").strip()
    if not url:
        return VerbResult(ok=False, summary="open_url needs a url", error="missing_url")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return VerbResult(
            ok=False,
            summary="only http(s) URLs can be opened",
            error=f"refused scheme: {parsed.scheme or '(none)'}",
        )
    ok, out = await mac.run_exec(["open", url])
    if not ok:
        return VerbResult(ok=False, summary=f"couldn't open the url: {out[:80]}", error=out)
    return VerbResult(ok=True, summary=f"opened {parsed.netloc}")


# ── registration ────────────────────────────────────────────────────────────


def register(registry: VerbRegistry, *, enabled_fn: Callable[[], bool]) -> None:
    """Registered by service.py (NOT register_all) because the gate needs
    bridge state: ``enabled_fn`` is the live ``state.computer_enabled``
    read, checked per call so a settings flip applies immediately."""

    def gated(run: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        async def wrapped(args: dict[str, Any]) -> VerbResult:
            blocked = _gate(enabled_fn)
            if blocked is not None:
                return blocked
            return await run(args)

        return wrapped

    registry.register(
        Verb(
            name="computer.see",
            description=(
                "Look at the front (or named) app: returns a screenshot with a "
                "coordinate grid. Read it, then click by the x,y you see"
            ),
            params={
                "app": {"type": "string", "description": "app name; omit for frontmost"},
            },
            required=[],
            tier="auto",
            run=gated(_see),
        )
    )
    registry.register(
        Verb(
            name="computer.click",
            description=(
                "Click at pixel x,y read off the last screenshot's grid (primary). "
                "Also accepts a ref from computer.see, a visible label, or a "
                "target described in plain words (vision-grounded). Returns a fresh "
                "screenshot of the result"
            ),
            params={
                "x": {"type": "integer", "description": "pixel x in the last screenshot"},
                "y": {"type": "integer", "description": "pixel y in the last screenshot"},
                "ref": {"type": "string", "description": "element ref from computer.see"},
                "target": {
                    "type": "string",
                    "description": (
                        "what to click, described plainly ('the blue Send button') "
                        "— located by vision if you'd rather not read a pixel"
                    ),
                },
                "element": {"type": "string", "description": "exact visible label to find live"},
            },
            required=[],
            tier="auto",
            run=gated(_click),
        )
    )
    registry.register(
        Verb(
            name="computer.type",
            description="Type text into the focused field; returns a fresh screenshot",
            params={"text": {"type": "string"}},
            required=["text"],
            tier="auto",
            run=gated(_type),
        )
    )
    registry.register(
        Verb(
            name="computer.press",
            description='Press a key or combo ("enter", "tab", "cmd+t"); returns a screenshot',
            params={
                "key": {"type": "string", "description": 'key name or combo like "cmd+t"'},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "cmd, ctrl, alt, shift",
                },
            },
            required=["key"],
            tier="auto",
            run=gated(_press),
        )
    )
    registry.register(
        Verb(
            name="computer.scroll",
            description="Scroll the front window (optionally at x,y or a ref); returns a screenshot",
            params={
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "integer", "description": "scroll clicks, default 8"},
                "ref": {"type": "string", "description": "element ref from computer.see"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
            required=["direction"],
            tier="auto",
            run=gated(_scroll),
        )
    )
    registry.register(
        Verb(
            name="computer.menu",
            description=(
                'Click a menu-bar command by path, e.g. ["File", "New Tab"] — '
                "no coordinates needed; returns a screenshot"
            ),
            params={
                "menu_path": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "menu title first, then submenus, item last",
                },
                "app": {"type": "string", "description": "app name; omit for frontmost"},
            },
            required=["menu_path"],
            tier="auto",
            run=gated(_menu),
        )
    )
    registry.register(
        Verb(
            name="computer.open_url",
            description="Open an http(s) URL in the default browser",
            params={"url": {"type": "string"}},
            required=["url"],
            tier="auto",
            run=gated(_open_url),
        )
    )
