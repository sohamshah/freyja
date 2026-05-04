"""
Atomic computer-use tools for the desktop bridge.

Each tool wraps a single primitive from the `freyja_native` Rust
extension (screen capture, click, key, scroll, window enumeration, AX
tree read). The loop itself — "screenshot → decide → act → screenshot"
— is driven by the LLM as a sequence of tool calls.

All tools:
  * are async (using `asyncio.to_thread` for the blocking native call)
  * emit structured events to the UI via an injected `emit_event`
    callback: `action_planned` 200ms before every mutating action,
    `screenshot_frame` after any capture (and after every mutation),
    and `action_executed` on completion
  * honor a shared `asyncio.Event`-ish cancel signal so the emergency
    stop can abort a tool mid-flight
  * return ToolResult with human-readable text that the LLM can reason
    about on the next turn

The `action_planned → 200ms delay → execute` pattern is what makes the
cursor-highlight UX work: the user gets a heads-up before the click
lands, which is also their window to triple-Esc.

None of these tools spawn sub-agents directly — the `computer_use` tool
in `computer_use_tool.py` is a separate thing that wraps an entire
computer-control task in a sub-agent. These atomic tools are what that
sub-agent calls.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from bridge.tools.base import (
    ImageBlock,
    PermissionLevel,
    TextBlock,
    ToolDefinition,
    ToolResult,
    ToolTier,
)

# When set, every emitted frame is also dumped to disk so you can open
# it with Preview and verify the native layer is actually capturing
# what you think it is. Disk-dump is off by default — if you're
# debugging a "screenshots look wrong" situation, set this env var to
# a directory and restart the bridge.
_FRAME_DUMP_DIR = os.environ.get("FREYJA_FRAME_DUMP_DIR") or str(
    Path.home() / ".freyja" / "last-frames"
)
_FRAME_DUMP_ENABLED = (
    os.environ.get("FREYJA_FRAME_DUMP", "1").lower()
    not in ("0", "false", "no")
)

logger = logging.getLogger(__name__)

# How long between action_planned and the actual mutation. This is the
# user's abort window — long enough to notice, short enough to feel
# responsive. Vy uses ~250ms; we pick 200 as a compromise.
ACTION_HIGHLIGHT_MS = 200

# Post-action settle time before we capture the "what happened next"
# screenshot. Mouse clicks usually land instantly, but keyboard
# shortcuts that trigger UI (Spotlight, Mission Control, Cmd+Tab
# switcher) can take 200-800ms to fully paint, and some animations
# (Mission Control, Launchpad, fullscreen transitions) take over a
# second. 1000ms default is conservative — the agent can pass a
# smaller `settle_ms` to any mutating tool when it knows the target
# UI updates instantly, or a larger one for slow transitions.
DEFAULT_POST_ACTION_SETTLE_MS = 1000

# Long-edge pixel cap for screenshots streamed to the UI and to the
# model. 1280px matches Anthropic's recommended WXGA for computer-use
# (https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
# and stays under their 1.15 megapixel API ceiling on landscape
# displays (1280×827 = 1.06 MP). Critically: anything above 1.15 MP
# gets auto-downsampled by Anthropic's vision API, which introduces
# a coordinate-space mismatch between what the model sees and what
# our click tool expects. 1280 is the sweet spot.
#
# At JPEG q75, a 1280×827 preview is ~150-300KB — still comfortable
# for Electron IPC at 2fps.
PREVIEW_MAX_DIM = 1280
PREVIEW_FORMAT = "jpeg"
PREVIEW_QUALITY = 75


ComputerEventCb = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class ComputerToolSpec:
    """Shared configuration injected into every computer tool.

    The Freyja bridge builds one of these per session and passes it to
    the tool constructors. It carries (a) the event-emit callback so
    tools can stream live state to the UI, (b) the session id so
    `action_planned` events are tagged correctly, (c) a shared
    `cancel_event` the global emergency stop uses to abort in-flight
    actions, (d) whether the settings layer has granted computer
    control at all, and (e) the default display the tools should
    target when the caller doesn't pass `display_id` explicitly.
    """

    session_id: str
    emit_event: ComputerEventCb
    cancel_event: asyncio.Event
    enabled: bool = True
    # When True, every mutating tool requires a PermissionPrompt before
    # it runs. The bridge drops this to False inside a sub-agent that
    # the user has already approved for the session.
    require_approval: bool = False
    # Default display id for screenshot/list_windows when not
    # explicitly specified. None = main display. Used by computer_use
    # to pin a sub-agent to a specific monitor.
    default_display_id: int | None = None
    # Name of the tool that owns this spec — for logging only.
    owner: str = "computer"

    # ─── Coordinate-space state ────────────────────────────────────
    #
    # We follow the Anthropic computer-use reference implementation
    # pattern: capture at native resolution, resize to fit Anthropic's
    # 1568/1.15MP image limits, and expose the resized space as the
    # ONLY coordinate space the model ever sees.
    #
    # - Claude receives screenshots sized at `api_dims`
    # - All tool inputs (click, move_mouse, scroll, inspect_region)
    #   accept coordinates in `api_dims` space
    # - All tool outputs that include coordinates (find_element,
    #   cursor_position, list_windows bounds) are translated DOWN
    #   from native to `api_dims` before being returned
    # - We scale api → native at the one place we call CGEventPost /
    #   CGDisplayCreateImage
    #
    # `native_dims` is the actual display resolution (e.g. 3456×2234
    # for a 16" MacBook Pro internal). `api_dims` is the downscaled
    # representation (e.g. 1280×827). Populated lazily on first
    # access via `_ensure_dims(spec, native_module)`. None means
    # "not resolved yet" — caller should call _ensure_dims.
    native_dims: tuple[int, int] | None = None
    api_dims: tuple[int, int] | None = None


async def _fire(cb: ComputerEventCb, event: dict[str, Any]) -> None:
    try:
        result = cb(event)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001
        logger.exception("computer event callback failed")


def _import_native() -> Any:
    """Import the native module lazily so the bridge can still start
    when computer control is disabled (e.g. the Rust wheel isn't built
    yet). Returns the module or raises ImportError."""
    import freyja_native as native  # noqa: PLC0415

    return native


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass
class EmittedFrame:
    """Payload returned from `_emit_frame` when a capture succeeds.

    `data` is the raw image bytes (possibly with the cursor marker
    composited in). `mime_type` tells the caller whether it's JPEG or
    PNG so they can build an ImageBlock with the right media_type.
    """

    width: int
    height: int
    byte_len: int
    data: bytes
    mime_type: str


async def _emit_frame(
    spec: ComputerToolSpec,
    native: Any,
    *,
    display_id: int | None = None,
    window_id: int | None = None,
    reason: str = "",
) -> EmittedFrame | None:
    """Capture a downscaled JPEG preview and emit a `screenshot_frame` event.

    Uses `PREVIEW_MAX_DIM` (1280px long edge) + JPEG quality 75 so the
    payload stays ~100KB per frame. Returns an `EmittedFrame` (with the
    final rendered bytes) on success, None if the capture failed.
    Swallows errors so the caller doesn't have to. Falls back to
    `spec.default_display_id` when `display_id` is None.
    """
    # Short-circuit if Screen Recording is denied AND we have no
    # capture proxy fallback. The wrapper's screenshot() will
    # transparently use the proxy when the env var is set, so we
    # don't need to skip in that case.
    try:
        if not native.Permissions.screen_recording() and not os.environ.get(
            "FREYJA_CAPTURE_URL"
        ):
            logger.warning(
                "_emit_frame: skipping capture, Screen Recording not "
                "granted and no capture proxy configured"
            )
            return None
    except Exception:  # noqa: BLE001
        pass
    effective_display = display_id if display_id is not None else spec.default_display_id

    # Resolve the native display dimensions for the display we're
    # capturing. For window_id captures we skip this — window bounds
    # are a different coordinate space than display bounds, and the
    # scale factor we cache is keyed to a display, not a window. The
    # click tool still works after a window capture because it relies
    # on whatever full-display frame was most recently emitted.
    native_w, native_h = 0, 0
    if window_id is None:
        try:
            displays = await asyncio.to_thread(native.list_displays)
            target = None
            if effective_display is not None:
                target = next((d for d in displays if d.id == effective_display), None)
            if target is None and displays:
                target = next((d for d in displays if d.is_primary), displays[0])
            if target is not None:
                native_w, native_h = target.width, target.height
        except Exception:  # noqa: BLE001
            pass

    try:
        frame = await asyncio.to_thread(
            native.screenshot,
            display_id=effective_display,
            window_id=window_id,
            max_dim=PREVIEW_MAX_DIM,
            format=PREVIEW_FORMAT,
            quality=PREVIEW_QUALITY,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("screenshot failed: %s", exc)
        return None

    # Cache the coordinate-space pair on the spec. `native_dims` is
    # the actual display resolution (for input injection); `api_dims`
    # is whatever the captured frame ended up being (the size Claude
    # sees). Keeping api_dims = frame.width × frame.height — rather
    # than recomputing via _compute_api_dims — guarantees the model's
    # coordinate space matches the pixel grid of the image we just
    # sent it, even if the native encoder rounded dimensions slightly.
    if native_w > 0 and frame.width > 0 and window_id is None:
        spec.native_dims = (int(native_w), int(native_h))
        spec.api_dims = (int(frame.width), int(frame.height))

    # Composite the cursor onto the frame. macOS screen capture APIs
    # omit the cursor overlay by default, so without this the model
    # has no way to see where the pointer currently is. We do it in
    # Python via Pillow so it works whether the capture came from the
    # native Rust path or the Electron HTTP proxy.
    png_bytes: bytes = frame.png
    if native_w > 0 and window_id is None:
        try:
            cursor_pos = await asyncio.to_thread(native.cursor_position)
            png_bytes = await asyncio.to_thread(
                _draw_cursor_on_frame,
                frame.png,
                cursor_pos[0],
                cursor_pos[1],
                native_w,
                native_h,
                PREVIEW_FORMAT,
                PREVIEW_QUALITY,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("cursor overlay failed: %s", exc)
            png_bytes = frame.png

    # Dump to disk for debugging — so you can open Preview on the
    # resulting file and verify the native layer captured what the
    # agent thinks it did. We keep only the last ~20 frames per
    # session to avoid filling the disk during long runs.
    if _FRAME_DUMP_ENABLED:
        try:
            dump_dir = Path(_FRAME_DUMP_DIR)
            dump_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            ext = "jpg" if frame.format == "jpeg" else "png"
            filename = f"{spec.session_id}_{ts}_{reason or 'frame'}.{ext}"
            (dump_dir / filename).write_bytes(png_bytes)
            # Also write a stable "latest" symlink for the session so
            # you can always `open ~/.freyja/last-frames/latest.jpg`.
            latest = dump_dir / f"latest.{ext}"
            try:
                latest.unlink()
            except FileNotFoundError:
                pass
            latest.write_bytes(png_bytes)
            # Trim old frames for this session
            pattern = f"{spec.session_id}_*.{ext}"
            existing = sorted(dump_dir.glob(pattern))
            for old in existing[:-20]:
                try:
                    old.unlink()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("frame dump failed: %s", exc)
    await _fire(
        spec.emit_event,
        {
            "type": "screenshot_frame",
            "sessionId": spec.session_id,
            # We still call the field pngBase64 for protocol compat, but
            # the payload can be JPEG depending on PREVIEW_FORMAT. The
            # mimeType field tells the renderer which data URL prefix to
            # use.
            "pngBase64": _b64(png_bytes),
            "mimeType": frame.mime_type,
            "width": frame.width,
            "height": frame.height,
            "takenAt": int(time.time() * 1000),
            "reason": reason,
        },
    )
    return EmittedFrame(
        width=frame.width,
        height=frame.height,
        byte_len=len(png_bytes),
        data=png_bytes,
        mime_type=frame.mime_type,
    )


async def _await_highlight_or_cancel(
    spec: ComputerToolSpec, ms: int = ACTION_HIGHLIGHT_MS
) -> bool:
    """Sleep for `ms` milliseconds OR until cancel is triggered.

    Returns True if we slept normally, False if cancelled. Tools use
    this both as the pre-action highlight window and as a cooperative
    cancel checkpoint.
    """
    if spec.cancel_event.is_set():
        return False
    try:
        await asyncio.wait_for(spec.cancel_event.wait(), timeout=ms / 1000.0)
        return False  # cancelled
    except asyncio.TimeoutError:
        return True  # slept normally


def _cancelled_result(call_id: str, tool: str) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        content=f"{tool}: cancelled by emergency stop",
        is_error=True,
    )


def _result_with_frame(
    call_id: str,
    summary: str,
    frame: EmittedFrame | None,
    *,
    is_error: bool = False,
) -> ToolResult:
    """Build a ToolResult whose content includes the summary text AND
    the freshly captured post-action frame as an ImageBlock.

    If the capture failed we fall back to a text-only result so the
    tool is still usable. Every mutating tool uses this helper so the
    model ALWAYS sees a fresh screenshot alongside the action summary
    — matching Anthropic's reference computer-use pattern where every
    atomic action returns a new `base64_image` in its result.
    """
    if frame is None:
        return ToolResult(
            call_id=call_id, content=summary, is_error=is_error
        )
    return ToolResult(
        call_id=call_id,
        content=[
            TextBlock(text=summary),
            ImageBlock.from_base64(
                _b64(frame.data), media_type=frame.mime_type
            ),
        ],
        is_error=is_error,
    )


def _draw_cursor_on_frame(
    png_or_jpeg_bytes: bytes,
    cursor_native_x: int,
    cursor_native_y: int,
    native_w: int,
    native_h: int,
    out_format: str,
    quality: int,
) -> bytes:
    """Composite a visible cursor marker onto a captured frame.

    macOS `CGDisplayCreateImage` and Electron's `desktopCapturer` both
    omit the cursor from their captured output — the cursor is a
    Window Server overlay drawn on top of the composited frame, and
    captures grab the frame itself. Without compositing it back in,
    the agent has no idea where the mouse is in the image it's looking
    at, which breaks any task that depends on "click relative to where
    the cursor is" or "verify my last move_mouse actually landed".

    We draw a stylized arrow cursor (white fill, black outline, same
    ~18px tall silhouette as macOS's default pointer) at the scaled
    cursor position. Slightly oversized and high-contrast so it's
    legible even after JPEG compression at q75.
    """
    if native_w <= 0 or native_h <= 0:
        return png_or_jpeg_bytes
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return png_or_jpeg_bytes

    try:
        img = Image.open(BytesIO(png_or_jpeg_bytes))
        img.load()
    except Exception:  # noqa: BLE001
        return png_or_jpeg_bytes

    if img.mode not in ("RGB", "RGBA"):
        try:
            img = img.convert("RGB")
        except Exception:  # noqa: BLE001
            return png_or_jpeg_bytes

    preview_w, preview_h = img.size
    scale = preview_w / native_w
    cx = cursor_native_x * scale
    cy = cursor_native_y * scale

    # Clamp to visible bounds; if the cursor is off-screen (on another
    # display, negative coords, etc.) just don't draw anything.
    if cx < 0 or cy < 0 or cx > preview_w or cy > preview_h:
        # Re-encode to the requested format and return unchanged — we
        # still want the frame, just without a cursor marker.
        return _reencode_image(img, out_format, quality)

    draw = ImageDraw.Draw(img, "RGBA")
    # Classic pointer arrow silhouette, anchored at the tip (cx, cy).
    # Coordinates are relative to the tip; we translate by (cx, cy).
    # This shape is ~14px wide, ~20px tall — visible on a 1280
    # preview, not so big it obscures UI.
    arrow = [
        (0, 0),
        (0, 16),
        (4, 12),
        (7, 18),
        (9, 17),
        (6, 11),
        (11, 11),
    ]

    # Draw black outline slightly larger first, then white fill on top
    # for a crisp high-contrast pointer that survives JPEG compression.
    outline = [(cx + x, cy + y) for x, y in arrow]
    draw.polygon(outline, fill=(0, 0, 0, 255))
    # Shrink the fill by ~1 pixel toward the centroid so the black
    # outline remains visible around the edges.
    centroid_x = sum(x for x, _ in arrow) / len(arrow)
    centroid_y = sum(y for _, y in arrow) / len(arrow)
    shrunk = []
    for x, y in arrow:
        dx = centroid_x - x
        dy = centroid_y - y
        length = max(1.0, (dx * dx + dy * dy) ** 0.5)
        shrink_px = 1.2
        sx = x + (dx / length) * shrink_px
        sy = y + (dy / length) * shrink_px
        shrunk.append((cx + sx, cy + sy))
    draw.polygon(shrunk, fill=(255, 255, 255, 255))

    return _reencode_image(img, out_format, quality)


def _reencode_image(img: Any, out_format: str, quality: int) -> bytes:
    """Encode a PIL image back to PNG or JPEG bytes."""
    from io import BytesIO

    buf = BytesIO()
    fmt = (out_format or "jpeg").lower()
    if fmt == "png":
        img.save(buf, format="PNG", optimize=True)
    else:
        # JPEG requires RGB
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=max(1, min(100, int(quality))))
    return buf.getvalue()


def _compute_api_dims(native_w: int, native_h: int) -> tuple[int, int]:
    """Compute the API-space dimensions for a native display.

    Matches Anthropic's computer-use scaling recommendation: the screenshot
    we ship to the model must fit under their image size limits (1568 px
    longest edge AND 1.15 MP total) to avoid auto-downsampling that would
    silently shift the coordinate space under the model's feet. We pin the
    long edge to `PREVIEW_MAX_DIM` (1280) which satisfies both constraints
    comfortably on every practical aspect ratio (1280×827 on 16:10.35 is
    1.06 MP, well under the 1.15 MP ceiling).

    If the native display is already smaller than PREVIEW_MAX_DIM on its
    long edge, we return native unchanged — no upscaling.
    """
    if native_w <= 0 or native_h <= 0:
        return native_w, native_h
    long_edge = max(native_w, native_h)
    scale = min(1.0, PREVIEW_MAX_DIM / long_edge)
    api_w = max(1, int(round(native_w * scale)))
    api_h = max(1, int(round(native_h * scale)))
    return api_w, api_h


async def _ensure_dims(spec: ComputerToolSpec, native: Any) -> None:
    """Populate `spec.native_dims` and `spec.api_dims` if missing.

    Called at the top of every coordinate-handling tool so translation
    never sees a None dimension pair. Uses the session's pinned display
    (or the primary display) as the reference. Does nothing if dims
    are already set — the most recent `_emit_frame` call is the source
    of truth, so we only fall back to `list_displays` when no frame has
    been emitted yet.
    """
    if spec.native_dims is not None and spec.api_dims is not None:
        return
    try:
        displays = await asyncio.to_thread(native.list_displays)
    except Exception:  # noqa: BLE001
        return
    if not displays:
        return
    target = None
    if spec.default_display_id is not None:
        target = next(
            (d for d in displays if d.id == spec.default_display_id), None
        )
    if target is None:
        target = next((d for d in displays if d.is_primary), displays[0])
    if target is not None and target.width > 0 and target.height > 0:
        spec.native_dims = (int(target.width), int(target.height))
        spec.api_dims = _compute_api_dims(
            int(target.width), int(target.height)
        )


def _api_to_native(spec: ComputerToolSpec, x: float, y: float) -> tuple[int, int]:
    """Deterministically scale an API-space coordinate UP to native pixels.

    This is the ONE place where API coords get translated before we hand
    them to CGEventPost (via `native.click` / `native.move_mouse` / etc).
    No heuristic, no "does this look like native" guessing — we trust the
    single coordinate-space contract (see `ComputerToolSpec` docstring).
    If `native_dims` hasn't been resolved yet we return the coordinate
    unchanged rather than raising, so callers must `_ensure_dims` first
    for the scale to actually apply.
    """
    if spec.native_dims is None or spec.api_dims is None:
        return int(round(x)), int(round(y))
    nw, nh = spec.native_dims
    aw, ah = spec.api_dims
    if aw <= 0 or ah <= 0:
        return int(round(x)), int(round(y))
    return int(round(x * nw / aw)), int(round(y * nh / ah))


def _native_to_api(spec: ComputerToolSpec, x: float, y: float) -> tuple[int, int]:
    """Deterministically scale a native coordinate DOWN to API space.

    Used for `find_element`, `cursor_position`, and `list_windows` bounds
    — anywhere we read a coordinate out of a native API and need to hand
    it back to the model. The model only ever sees API-space numbers, so
    AXFrame bounds (in native points) must be translated before we
    return them in tool result text.
    """
    if spec.native_dims is None or spec.api_dims is None:
        return int(round(x)), int(round(y))
    nw, nh = spec.native_dims
    aw, ah = spec.api_dims
    if nw <= 0 or nh <= 0:
        return int(round(x)), int(round(y))
    return int(round(x * aw / nw)), int(round(y * ah / nh))


def _translate_ax_tree_bounds(
    spec: ComputerToolSpec, tree: dict[str, Any]
) -> None:
    """Walk a parsed AX tree in place and rewrite `bounds` arrays from
    native points to API space, so they match the coordinate space the
    model sees in screenshots.

    The native `read_ax_tree` implementation returns bounds as a
    four-element list `[x, y, w, h]` in display points (possibly with
    negative coordinates for elements on off-origin displays). Without
    translation the raw JSON disagreed with `list_windows` / `find_element`
    (which now return API space), producing the exact kind of
    coordinate-space confusion that wrecked the Arc navigation session
    (agent read bounds `(27, 77, 1651x908)` from the tree and concluded
    the Arc window "spanned two displays", when the list_windows report
    showed it at API bounds `(20, 57, 1223x672)` on the primary display).

    Mutates the dict in place.
    """
    if spec.native_dims is None or spec.api_dims is None:
        return
    nw, nh = spec.native_dims
    aw, ah = spec.api_dims
    if nw <= 0 or nh <= 0 or aw <= 0 or ah <= 0:
        return
    sx = aw / nw
    sy = ah / nh

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            b = node.get("bounds")
            if (
                isinstance(b, list)
                and len(b) == 4
                and all(isinstance(v, (int, float)) for v in b)
            ):
                node["bounds"] = [
                    int(round(b[0] * sx)),
                    int(round(b[1] * sy)),
                    int(round(b[2] * sx)),
                    int(round(b[3] * sy)),
                ]
            children = node.get("children")
            if isinstance(children, list):
                for c in children:
                    _walk(c)
        elif isinstance(node, list):
            for c in node:
                _walk(c)

    _walk(tree)


def _effective_settle_ms(arguments: dict[str, Any]) -> int:
    """Resolve the post-action settle duration for this tool call.

    The agent can override per-call via a `settle_ms` parameter when it
    knows the action triggers a slow UI animation (Mission Control,
    Launchpad, a fullscreen transition) or conversely when it's
    clicking on something instantaneous and wants the loop to feel
    snappier. Clamped to [0, 10000] ms.
    """
    raw = arguments.get("settle_ms")
    if raw is None:
        return DEFAULT_POST_ACTION_SETTLE_MS
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POST_ACTION_SETTLE_MS
    return max(0, min(ms, 10_000))


def _disabled_result(call_id: str, tool: str) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        content=(
            f"{tool}: computer control is disabled. Enable it in "
            "Settings → Computer Control, then grant Screen Recording "
            "and Accessibility permissions in System Settings."
        ),
        is_error=True,
    )


# Module-level latches — on the first mutating tool call we trigger
# the macOS permission prompts for the current process. Each prompt
# only shows once per process; subsequent calls are no-ops.
_AX_PROMPT_FIRED = False
_SR_PROMPT_FIRED = False
# Cached result of the input self-test (True = CGEventPost actually
# delivers events, False = silently dropped, None = not tested yet).
_AX_SELF_TEST: bool | None = None


def _require_screen_recording(call_id: str, tool: str) -> ToolResult | None:
    """Return an error ToolResult if we have no way to capture the screen.

    Checks in order:
      1. `CGPreflightScreenCaptureAccess` on the current Python process.
         If that's true we're good — the native path works.
      2. `FREYJA_CAPTURE_URL` env var, set by the Electron main
         process when it launches the Python bridge. If present, the
         Python wrapper automatically routes screen captures through
         that localhost HTTP proxy, which runs inside Electron main
         (the process that DOES have Screen Recording via TCC).
      3. Otherwise, there's no viable capture path. Return a loud
         error with instructions to grant Electron (or the .app)
         Screen Recording in System Settings.

    The fallback via the capture proxy is the workaround for macOS's
    "Screen Recording permission does not inherit via subprocess
    responsibility" quirk — Apple intentionally prevents children from
    inheriting SR, so we have to do the capture from the parent
    (Electron main) and stream the bytes back to Python.
    """
    global _SR_PROMPT_FIRED
    import os as _os

    try:
        import freyja_native as native

        if not _SR_PROMPT_FIRED:
            _SR_PROMPT_FIRED = True
            try:
                native.Permissions.prompt_screen_recording()
            except Exception:  # noqa: BLE001
                pass
        if native.Permissions.screen_recording():
            return None
    except Exception:  # noqa: BLE001
        return None

    # Fallback: if the Electron main process exposed a capture proxy,
    # the Python wrapper will automatically route through it and
    # captures WILL include real window content because Electron
    # main has SR. We don't verify connectivity here — a 1-time HEAD
    # check adds latency and the wrapper's own error handling will
    # surface any failure.
    if _os.environ.get("FREYJA_CAPTURE_URL"):
        return None

    return ToolResult(
        call_id=call_id,
        content=(
            f"{tool}: Screen Recording permission is NOT granted and "
            f"no capture proxy is available. Any screenshot taken right "
            f"now will be PRIVACY-FILTERED — macOS will return an image "
            f"showing only the desktop wallpaper and menu bar, with "
            f"every other app's window redacted/invisible. DO NOT "
            f"attempt to act on a filtered capture; you will "
            f"hallucinate that the screen is empty when it's full "
            f"of apps.\n\n"
            f"FIX:\n"
            f"  1. Open System Settings → Privacy & Security → Screen Recording\n"
            f"  2. Drag the Electron binary (or `Freyja.app`) into the list\n"
            f"  3. Toggle it ON\n"
            f"  4. Quit + relaunch the app\n\n"
            f"Note: macOS does NOT inherit Screen Recording via "
            f"subprocess responsibility the way it does Accessibility. "
            f"If you're running in dev mode the app uses a localhost "
            f"capture proxy routed through Electron main (which has "
            f"the permission), but that proxy didn't start for some "
            f"reason — check the main process logs."
        ),
        is_error=True,
    )


def _require_accessibility(call_id: str, tool: str) -> ToolResult | None:
    """Return an error ToolResult if input injection is not working.

    The permission flag (``AXIsProcessTrustedWithOptions``) can return
    True even when CGEventPost silently drops events — so on the first
    call we run an actual self-test that moves the cursor by a few
    pixels and checks whether it landed. The result is cached for the
    rest of the session.

    Fallback: if ``FREYJA_INPUT_URL`` is set the ``freyja_native``
    wrapper routes input through the Electron main process proxy.
    """
    global _AX_PROMPT_FIRED, _AX_SELF_TEST
    import os as _os

    try:
        import freyja_native as native
    except Exception:  # noqa: BLE001
        return None  # can't check — let the action attempt fail naturally

    # Prompt macOS to show the "grant Accessibility" dialog once.
    if not _AX_PROMPT_FIRED:
        _AX_PROMPT_FIRED = True
        try:
            native.Permissions.prompt_accessibility()
        except Exception:  # noqa: BLE001
            pass

    # Fast path: already verified this session.
    if _AX_SELF_TEST is True:
        return None

    # Run the actual self-test: move cursor a few pixels and check it
    # landed. This is the only reliable way to know whether CGEventPost
    # is being silently dropped.
    if _AX_SELF_TEST is None:
        try:
            result = native.Permissions.input_self_test()
            _AX_SELF_TEST = result.get("ok", False)
        except Exception:  # noqa: BLE001
            _AX_SELF_TEST = False

    if _AX_SELF_TEST:
        return None

    # Direct injection failed. Check for input proxy fallback.
    if _os.environ.get("FREYJA_INPUT_URL"):
        return None

    return ToolResult(
        call_id=call_id,
        content=(
            f"{tool}: Accessibility permission is NOT working. macOS is "
            f"silently dropping CGEvent injection — your clicks and "
            f"keystrokes will NOT land. Do NOT claim any action succeeded "
            f"until this is fixed.\n\n"
            f"FIX:\n"
            f"  1. Open System Settings → Privacy & Security → Accessibility\n"
            f"  2. Click '+' and add Freyja.app (from /Applications)\n"
            f"  3. Toggle it ON\n"
            f"  4. Quit + relaunch the app (TCC takes effect on next start)\n\n"
            f"Note: macOS TCC responsibility inheritance requires the "
            f"app to be code-signed. If you're running in dev mode, "
            f"run `scripts/bundle-python.sh` to create a properly-signed "
            f"Python bundle, then restart the app."
        ),
        is_error=True,
    )


# ─── Tools ──────────────────────────────────────────────────────────────


class ScreenshotTool:
    """Capture the screen and return the PNG as a tool result."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="screenshot",
            summary="Capture the screen",
            tier=ToolTier.HOT,
            description="""Capture a screenshot of the user's screen.

Pass `window_id` to capture a single window (see `list_windows`), or
`display_id` to pick a specific monitor. Omit both for the main display.

The returned PNG is the ground truth for what's on screen right now.
Use it to plan your next action — especially when the AX tree (via
`read_ax_tree`) is empty or stale. Coordinates are in scaled display
points (what the cursor uses), not physical pixels.

Always screenshot before mutating the screen (click/type) so you can
decide where to act. Screenshots are cheap — take as many as you need.
""",
            parameters={
                "type": "object",
                "properties": {
                    "window_id": {
                        "type": "integer",
                        "description": "Optional CGWindowID from list_windows()",
                    },
                    "display_id": {
                        "type": "integer",
                        "description": "Optional display id from list_displays() (default: main)",
                    },
                },
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "screenshot")
        sr_err = _require_screen_recording(call_id, "screenshot")
        if sr_err is not None:
            return sr_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id,
                content=f"Error: freyja_native not available ({exc})",
                is_error=True,
            )
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "screenshot")

        display_id = arguments.get("display_id")
        window_id = arguments.get("window_id")

        frame = await _emit_frame(
            self._spec,
            native,
            display_id=int(display_id) if display_id is not None else None,
            window_id=int(window_id) if window_id is not None else None,
            reason="screenshot_tool",
        )
        if frame is None:
            return ToolResult(
                call_id=call_id,
                content=(
                    "screenshot: capture failed. Likely missing Screen "
                    "Recording permission — open System Settings → "
                    "Privacy & Security → Screen Recording and enable "
                    "it for this app."
                ),
                is_error=True,
            )
        summary = (
            f"Captured screenshot {frame.width}x{frame.height} "
            f"({frame.byte_len // 1024}KB). This is the ONE coordinate "
            f"space you work in: every x,y you pass to click, "
            f"move_mouse, scroll is interpreted as a pixel in THIS "
            f"{frame.width}x{frame.height} image, and every coordinate "
            f"find_element / cursor_position / list_windows returns "
            f"is also in this space. Count pixels on the image you "
            f"just received and pass those numbers directly — the "
            f"tools handle any translation to the underlying display "
            f"for you."
        )
        # Return the captured frame as an ImageBlock in the tool
        # result so Claude actually sees the pixels. Prior versions
        # returned text-only, which forced the model to navigate the
        # AX tree blind and hallucinate about what was on screen.
        return ToolResult(
            call_id=call_id,
            content=[
                TextBlock(text=summary),
                ImageBlock.from_base64(
                    _b64(frame.data), media_type=frame.mime_type
                ),
            ],
            is_error=False,
        )


class ListDisplaysTool:
    """Enumerate attached displays so the agent can target a specific monitor."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_displays",
            summary="List attached displays / monitors",
            tier=ToolTier.HOT,
            description="""List every connected display with id, resolution, scale factor, and primary flag.

Use this when the user mentions a specific monitor (e.g. "the left screen"
or "display 2"). Pass the returned `id` as `display_id` in subsequent
`screenshot` calls to capture that specific display.

Global click coordinates are continuous across all displays — the primary
display's origin is (0,0) and other displays live at offsets. You can
click anywhere in the global coordinate space regardless of which display
you captured.
""",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "list_displays")
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )
        displays = await asyncio.to_thread(native.list_displays)
        if not displays:
            return ToolResult(
                call_id=call_id,
                content="No displays found (is the screen locked?)",
                is_error=False,
            )
        default_id = self._spec.default_display_id
        lines = [f"Found {len(displays)} display(s):"]
        for d in displays:
            flags = []
            if d.is_primary:
                flags.append("primary")
            if default_id is not None and d.id == default_id:
                flags.append("★ selected")
            flag_text = f" ({', '.join(flags)})" if flags else ""
            lines.append(
                f"  id={d.id}  {d.width}x{d.height}  "
                f"scale={d.scale:.1f}x{flag_text}"
            )
        if default_id is not None:
            lines.append(
                f"\nYour session is pinned to display {default_id}. "
                "Pass `display_id={id}` in `screenshot` to capture a "
                "different one, but keep your clicks targeted at "
                f"display {default_id} unless told otherwise."
            )
        return ToolResult(
            call_id=call_id, content="\n".join(lines), is_error=False
        )


class ListWindowsTool:
    """Enumerate on-screen application windows."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_windows",
            summary="List on-screen application windows",
            tier=ToolTier.HOT,
            description="""List every normal window currently on screen.

Returns an ordered list (frontmost first) with id, pid, bundle id,
title, and bounds. Use this to find the window you want to act on,
then pass its `id` to `focus_window` or `screenshot`, or its `pid` to
`read_ax_tree`.

Menubars, indicators, and system overlays are filtered out — you only
see real application windows.
""",
            parameters={
                "type": "object",
                "properties": {
                    "include_helpers": {
                        "type": "boolean",
                        "description": "Include menubars/indicators (default false)",
                    }
                },
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "list_windows")
        sr_err = _require_screen_recording(call_id, "list_windows")
        if sr_err is not None:
            return sr_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )
        await _ensure_dims(self._spec, native)
        include_helpers = bool(arguments.get("include_helpers", False))
        windows = await asyncio.to_thread(
            native.list_windows, include_helpers=include_helpers
        )
        if not windows:
            return ToolResult(
                call_id=call_id,
                content="No visible windows. The desktop may be empty or permissions missing.",
                is_error=False,
            )
        lines = [f"Found {len(windows)} window(s):"]
        for w in windows[:40]:
            flag = " ★frontmost" if w.is_frontmost else ""
            title = w.title[:50] if w.title else "(untitled)"
            # Translate CG window bounds (native points) to API space
            # so the coordinates match the model's screenshot frame.
            bx, by = _native_to_api(self._spec, w.bounds.x, w.bounds.y)
            bx2, by2 = _native_to_api(
                self._spec, w.bounds.x + w.bounds.w, w.bounds.y + w.bounds.h
            )
            bw = max(0, bx2 - bx)
            bh = max(0, by2 - by)
            lines.append(
                f"  #{w.id:>6}  pid={w.pid:>5}  {w.bundle:<40}  "
                f"{title:<52}  bounds=({bx},{by},{bw}x{bh}){flag}"
            )
        if len(windows) > 40:
            lines.append(f"  … {len(windows) - 40} more omitted")
        return ToolResult(
            call_id=call_id, content="\n".join(lines), is_error=False
        )


class FocusWindowTool:
    """Bring a window to the front by its id or an app by bundle id."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="focus_window",
            summary="Focus a window or app",
            tier=ToolTier.HOT,
            description="""Focus an application (bringing it to the front).

Pass `window_id` from `list_windows` to focus a specific window, or
`bundle_id` (e.g. `com.apple.finder`) to activate an application by
bundle identifier. macOS doesn't let us raise a specific CGWindow
directly, so either path activates the owning app and lets it restore
its key window.

This is a cross-app handoff and may count as a permission-gated
action depending on the policy.
""",
            parameters={
                "type": "object",
                "properties": {
                    "window_id": {
                        "type": "integer",
                        "description": "CGWindowID from list_windows",
                    },
                    "bundle_id": {
                        "type": "string",
                        "description": "App bundle id, e.g. com.apple.finder",
                    },
                    "settle_ms": {
                        "type": "integer",
                        "description": "Post-action wait (default 1000ms) before capturing the next screenshot. Bump to 1500-3000 if the target app has a slow launch or window-restore animation.",
                    },
                },
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "focus_window")
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "focus_window")
        ax_err = _require_accessibility(call_id, "focus_window")
        if ax_err is not None:
            return ax_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        window_id = arguments.get("window_id")
        bundle_id = arguments.get("bundle_id")
        if not window_id and not bundle_id:
            return ToolResult(
                call_id=call_id,
                content="Error: either window_id or bundle_id is required",
                is_error=True,
            )
        settle_ms = _effective_settle_ms(arguments)

        await _fire(
            self._spec.emit_event,
            {
                "type": "action_planned",
                "sessionId": self._spec.session_id,
                "action": "focus_window",
                "description": f"Focus {bundle_id or f'window {window_id}'}",
            },
        )
        if not await _await_highlight_or_cancel(self._spec):
            return _cancelled_result(call_id, "focus_window")

        try:
            if window_id:
                await asyncio.to_thread(native.focus_window, int(window_id))
            else:
                await asyncio.to_thread(native.focus_app, str(bundle_id))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id,
                content=f"focus_window failed: {exc}",
                is_error=True,
            )

        # Give the OS a beat to switch, then capture the new state.
        await asyncio.sleep(settle_ms / 1000.0)
        frame = await _emit_frame(
            self._spec, native, reason="focus_window"
        )
        await _fire(
            self._spec.emit_event,
            {
                "type": "action_executed",
                "sessionId": self._spec.session_id,
                "action": "focus_window",
                "success": True,
                "durationMs": ACTION_HIGHLIGHT_MS + settle_ms,
            },
        )
        return _result_with_frame(
            call_id,
            f"Focused {bundle_id or f'window #{window_id}'}.",
            frame,
        )


class ClickTool:
    """Click at screen coordinates, with a 200ms pre-action highlight."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="click",
            summary="Click at screen coordinates",
            tier=ToolTier.HOT,
            description="""Click at an absolute screen coordinate.

`x`, `y` are in display points (the same coordinate space the cursor
uses) — derived either from pixel counting on a screenshot or from an
`AXFrame` via `find_element`. Pass `double=true` for a double-click;
pass `modifiers` for Cmd/Ctrl/Alt/Shift combinations.

An amber highlight ring is drawn at (x,y) for ~200ms BEFORE the click
so the user can see what's about to happen and abort with triple-Esc.

After clicking, we auto-capture a fresh screenshot and stream it as a
`screenshot_frame` event so the next tool call (usually another
screenshot for grounding) sees the post-click state.
""",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Screen X in display points"},
                    "y": {"type": "integer", "description": "Screen Y in display points"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button (default left)",
                    },
                    "double": {
                        "type": "boolean",
                        "description": "Double-click (default false)",
                    },
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Modifier keys to hold: cmd, ctrl, alt, shift",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-line description shown in the UI (optional)",
                    },
                    "settle_ms": {
                        "type": "integer",
                        "description": "Post-action wait before capturing the next screenshot. Default 1000ms. Use a larger value (2000-5000) for actions that trigger slow animations (Mission Control, Launchpad, fullscreen transitions); smaller (200-500) for clicks on instantaneous UI.",
                    },
                },
                "required": ["x", "y"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "click")
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "click")
        ax_err = _require_accessibility(call_id, "click")
        if ax_err is not None:
            return ax_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        await _ensure_dims(self._spec, native)
        api_x = int(arguments.get("x", 0))
        api_y = int(arguments.get("y", 0))
        x, y = _api_to_native(self._spec, api_x, api_y)
        button = str(arguments.get("button") or "left")
        double = bool(arguments.get("double", False))
        modifiers = list(arguments.get("modifiers") or [])
        description = str(
            arguments.get("description")
            or f"{'Double-' if double else ''}{button} click at ({api_x}, {api_y})"
        )
        settle_ms = _effective_settle_ms(arguments)

        # action_planned is emitted in API space — the renderer overlays
        # the highlight ring by dividing x/latestFrame.width, and
        # latestFrame is the API-space preview.
        await _fire(
            self._spec.emit_event,
            {
                "type": "action_planned",
                "sessionId": self._spec.session_id,
                "action": "click",
                "x": api_x,
                "y": api_y,
                "description": description,
                "modifiers": modifiers,
                "double": double,
            },
        )
        if not await _await_highlight_or_cancel(self._spec):
            return _cancelled_result(call_id, "click")

        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(
                native.click,
                x,
                y,
                button=button,
                double=double,
                modifiers=modifiers,
            )
        except Exception as exc:  # noqa: BLE001
            await _fire(
                self._spec.emit_event,
                {
                    "type": "action_executed",
                    "sessionId": self._spec.session_id,
                    "action": "click",
                    "success": False,
                    "durationMs": int((time.perf_counter() - t0) * 1000),
                    "error": str(exc),
                },
            )
            return ToolResult(
                call_id=call_id, content=f"click failed: {exc}", is_error=True
            )

        await asyncio.sleep(settle_ms / 1000.0)
        frame = await _emit_frame(self._spec, native, reason="post_click")
        await _fire(
            self._spec.emit_event,
            {
                "type": "action_executed",
                "sessionId": self._spec.session_id,
                "action": "click",
                "success": True,
                "durationMs": int((time.perf_counter() - t0) * 1000),
            },
        )
        summary = (
            f"Clicked at ({api_x}, {api_y}) with {button}"
            + (" double" if double else "")
            + (f" + {'+'.join(modifiers)}" if modifiers else "")
            + "."
        )
        return _result_with_frame(call_id, summary, frame)


class MoveMouseTool:
    """Move the cursor without clicking."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="move_mouse",
            summary="Move the mouse cursor",
            tier=ToolTier.HOT,
            description="""Move the cursor to absolute screen coordinates without clicking.

Useful for hovering over tooltips, setting up a future click, or just
drawing attention without triggering anything. No highlight ring is
drawn since no click is involved.
""",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "move_mouse")
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "move_mouse")
        ax_err = _require_accessibility(call_id, "move_mouse")
        if ax_err is not None:
            return ax_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        await _ensure_dims(self._spec, native)
        api_x = int(arguments.get("x", 0))
        api_y = int(arguments.get("y", 0))
        x, y = _api_to_native(self._spec, api_x, api_y)
        try:
            await asyncio.to_thread(native.move_mouse, x, y)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"move_mouse failed: {exc}", is_error=True
            )
        # Verify the cursor actually moved — CGEventPost silently drops
        # events when Accessibility permission is missing, so the call
        # above can "succeed" without anything happening.
        try:
            actual = await asyncio.to_thread(native.cursor_position)
            dx = abs(actual[0] - x)
            dy = abs(actual[1] - y)
            if dx > 10 or dy > 10:
                return ToolResult(
                    call_id=call_id,
                    content=(
                        f"move_mouse FAILED: cursor is at ({actual[0]}, {actual[1]}) "
                        f"instead of ({x}, {y}). Input injection is being silently "
                        f"dropped by macOS — Accessibility permission is likely missing."
                    ),
                    is_error=True,
                )
        except Exception:  # noqa: BLE001
            pass  # verification failed — report nominal success
        return ToolResult(
            call_id=call_id,
            content=f"Moved cursor to ({api_x}, {api_y}).",
            is_error=False,
        )


class TypeTextTool:
    """Type a string of text."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="type_text",
            summary="Type text at the current focus",
            tier=ToolTier.HOT,
            description="""Type text into whatever has keyboard focus.

Whatever field the user most recently clicked (or that the agent
`click`ed) receives the input. Honors the active keyboard layout.

Typing is a high-impact mutation — a brief action_planned event is
emitted before the keys start flowing so the user has a chance to
cancel. After typing, a screenshot is captured so the next turn can
see what landed in the field.
""",
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to type. Newlines become Enter.",
                    },
                    "settle_ms": {
                        "type": "integer",
                        "description": "Post-action wait (default 1000ms) before capturing the next screenshot.",
                    },
                },
                "required": ["text"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "type_text")
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "type_text")
        ax_err = _require_accessibility(call_id, "type_text")
        if ax_err is not None:
            return ax_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        text = str(arguments.get("text", ""))
        if not text:
            return ToolResult(
                call_id=call_id, content="Error: text is required", is_error=True
            )
        settle_ms = _effective_settle_ms(arguments)

        preview = text[:60].replace("\n", "⏎")
        await _fire(
            self._spec.emit_event,
            {
                "type": "action_planned",
                "sessionId": self._spec.session_id,
                "action": "type_text",
                "description": f"Type: {preview}{'…' if len(text) > 60 else ''}",
                "length": len(text),
            },
        )
        if not await _await_highlight_or_cancel(self._spec):
            return _cancelled_result(call_id, "type_text")

        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(native.type_text, text)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"type_text failed: {exc}", is_error=True
            )
        await asyncio.sleep(settle_ms / 1000.0)
        frame = await _emit_frame(self._spec, native, reason="post_type")
        await _fire(
            self._spec.emit_event,
            {
                "type": "action_executed",
                "sessionId": self._spec.session_id,
                "action": "type_text",
                "success": True,
                "durationMs": int((time.perf_counter() - t0) * 1000),
            },
        )
        return _result_with_frame(
            call_id, f"Typed {len(text)} characters.", frame
        )


class PressKeyTool:
    """Press a named key (with optional modifiers)."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="press_key",
            summary="Press a named key (with modifiers)",
            tier=ToolTier.HOT,
            description="""Press a named key, optionally with modifier combinations.

Key names: return, enter, tab, space, escape, backspace, delete,
home, end, pageup, pagedown, up, down, left, right, f1-f12, or a
single character like `a`, `.`, `1`.

Modifiers are any subset of [cmd, ctrl, alt, shift] and are held down
for the duration of the key press.

Use this for hotkeys: `press_key("t", modifiers=["cmd"])` opens a new
tab in most apps, `press_key("escape")` dismisses dialogs.
""",
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key name or single char",
                    },
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Modifier keys: cmd, ctrl, alt, shift",
                    },
                    "settle_ms": {
                        "type": "integer",
                        "description": "Post-action wait (default 1000ms). Bump for slow-to-paint UIs like Spotlight (which takes ~400-800ms to render fully) or Mission Control (~1500ms).",
                    },
                },
                "required": ["key"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "press_key")
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "press_key")
        ax_err = _require_accessibility(call_id, "press_key")
        if ax_err is not None:
            return ax_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        key = str(arguments.get("key", ""))
        modifiers = list(arguments.get("modifiers") or [])
        if not key:
            return ToolResult(
                call_id=call_id, content="Error: key is required", is_error=True
            )
        settle_ms = _effective_settle_ms(arguments)

        combo = "+".join([*modifiers, key]) if modifiers else key
        await _fire(
            self._spec.emit_event,
            {
                "type": "action_planned",
                "sessionId": self._spec.session_id,
                "action": "press_key",
                "description": f"Press {combo}",
                "key": key,
                "modifiers": modifiers,
            },
        )
        if not await _await_highlight_or_cancel(self._spec):
            return _cancelled_result(call_id, "press_key")

        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(native.press_key, key, modifiers=modifiers)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"press_key failed: {exc}", is_error=True
            )
        await asyncio.sleep(settle_ms / 1000.0)
        frame = await _emit_frame(self._spec, native, reason="post_press_key")
        await _fire(
            self._spec.emit_event,
            {
                "type": "action_executed",
                "sessionId": self._spec.session_id,
                "action": "press_key",
                "success": True,
                "durationMs": int((time.perf_counter() - t0) * 1000),
            },
        )
        return _result_with_frame(call_id, f"Pressed {combo}.", frame)


class KeyDownTool:
    """Press a key down without releasing — for hold-key workflows."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="key_down",
            summary="Press and hold a key without releasing",
            tier=ToolTier.HOT,
            description="""Press a key DOWN without releasing it.

Pair with `key_up` to implement hold-key workflows that `press_key`
(which always releases) can't express. The canonical example is the
macOS ⌘Tab app switcher:

    key_down("cmd")          # ⌘ is now held
    press_key("tab")         # app switcher appears, advances once
    press_key("tab")         # advances again (⌘ still held)
    wait(ms=200)             # let the overlay render
    screenshot()             # verify the highlighted app
    key_up("cmd")            # release ⌘ → activates highlighted app

Other uses: shift-click range selection, ⌥-drag duplication,
⌃-hold for right-click. Every `key_down` MUST be matched by a
`key_up` on the same key, or the modifier stays latched and
pollutes every subsequent keystroke until the user quits the
app receiving the stuck key.
""",
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key name (cmd, ctrl, alt, shift, tab, return, ...)",
                    },
                },
                "required": ["key"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "key_down")
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "key_down")
        ax_err = _require_accessibility(call_id, "key_down")
        if ax_err is not None:
            return ax_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        key = str(arguments.get("key", ""))
        if not key:
            return ToolResult(
                call_id=call_id, content="Error: key is required", is_error=True
            )

        await _fire(
            self._spec.emit_event,
            {
                "type": "action_planned",
                "sessionId": self._spec.session_id,
                "action": "key_down",
                "description": f"Hold {key}",
                "key": key,
            },
        )
        # Do NOT await the full highlight delay here — hold-key
        # actions need to land immediately so follow-up presses see
        # the modifier. We still honor the cancel event.
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "key_down")
        try:
            await asyncio.to_thread(native.key_down, key)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"key_down failed: {exc}", is_error=True
            )
        await _fire(
            self._spec.emit_event,
            {
                "type": "action_executed",
                "sessionId": self._spec.session_id,
                "action": "key_down",
                "success": True,
                "durationMs": 0,
            },
        )
        return ToolResult(
            call_id=call_id,
            content=(
                f"Holding {key}. REMEMBER to call key_up('{key}') "
                "when done or it will stay latched forever."
            ),
            is_error=False,
        )


class KeyUpTool:
    """Release a previously-held key."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="key_up",
            summary="Release a previously-held key",
            tier=ToolTier.HOT,
            description="""Release a key that was previously held via `key_down`.

Always call this to complete a key_down sequence. See `key_down`
for the canonical ⌘Tab app switcher example.
""",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "settle_ms": {
                        "type": "integer",
                        "description": "Post-release wait (default 1000ms) before the confirmation screenshot.",
                    },
                },
                "required": ["key"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "key_up")
        # Note: we do NOT short-circuit on cancel_event here.
        # Releasing a held key is a CLEANUP action that must always
        # run — if an emergency stop fires while a modifier is held,
        # the last thing we want is to leave the modifier latched.
        ax_err = _require_accessibility(call_id, "key_up")
        if ax_err is not None:
            return ax_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        key = str(arguments.get("key", ""))
        if not key:
            return ToolResult(
                call_id=call_id, content="Error: key is required", is_error=True
            )
        settle_ms = _effective_settle_ms(arguments)
        try:
            await asyncio.to_thread(native.key_up, key)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"key_up failed: {exc}", is_error=True
            )
        # Optional post-release frame so the model sees the effect
        # (e.g. the app switcher closing and the chosen app activating).
        await asyncio.sleep(settle_ms / 1000.0)
        frame = await _emit_frame(self._spec, native, reason="post_key_up")
        return _result_with_frame(call_id, f"Released {key}.", frame)


class ScrollTool:
    """Scroll at a location."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="scroll",
            summary="Scroll at a screen location",
            tier=ToolTier.HOT,
            description="""Scroll by (dx, dy) clicks. Positive dy = down, positive dx = right.

If (x, y) is provided, the cursor moves there first so the scroll
targets the right view (useful when multiple scroll containers are on
screen). A typical scroll-a-page-down is `dy=8`.
""",
            parameters={
                "type": "object",
                "properties": {
                    "dx": {"type": "integer"},
                    "dy": {"type": "integer"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "settle_ms": {
                        "type": "integer",
                        "description": "Post-action wait (default 1000ms) before the screenshot. Reduce for momentum-free scrolls; increase for lists with lazy-load.",
                    },
                },
                "required": ["dy"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "scroll")
        if self._spec.cancel_event.is_set():
            return _cancelled_result(call_id, "scroll")
        ax_err = _require_accessibility(call_id, "scroll")
        if ax_err is not None:
            return ax_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        await _ensure_dims(self._spec, native)
        dx = int(arguments.get("dx", 0))
        dy = int(arguments.get("dy", 0))
        raw_x = arguments.get("x")
        raw_y = arguments.get("y")
        api_x: int | None
        api_y: int | None
        x: int | None
        y: int | None
        if raw_x is not None and raw_y is not None:
            api_x = int(raw_x)
            api_y = int(raw_y)
            x, y = _api_to_native(self._spec, api_x, api_y)
        else:
            api_x = api_y = None
            x = y = None
        settle_ms = _effective_settle_ms(arguments)

        try:
            await asyncio.to_thread(native.scroll, dx, dy, x=x, y=y)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"scroll failed: {exc}", is_error=True
            )
        await asyncio.sleep(settle_ms / 1000.0)
        frame = await _emit_frame(self._spec, native, reason="post_scroll")
        summary = (
            f"Scrolled dx={dx} dy={dy}"
            + (f" at ({api_x},{api_y})" if api_x is not None else "")
            + "."
        )
        return _result_with_frame(call_id, summary, frame)


class WaitTool:
    """Yield control for a bounded wall-clock duration."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="wait",
            summary="Sleep briefly (e.g. while a page loads)",
            tier=ToolTier.HOT,
            description="""Sleep for a bounded duration (up to 10 seconds).

Useful while a page finishes loading, a menu animates, or an app
settles after a click. Cancellable by the emergency stop — you never
block the cancel path for longer than a few hundred ms.
""",
            parameters={
                "type": "object",
                "properties": {
                    "ms": {
                        "type": "integer",
                        "description": "Milliseconds to sleep (max 10000)",
                    }
                },
                "required": ["ms"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "wait")
        ms = max(0, min(10_000, int(arguments.get("ms", 500))))
        slept_ok = True
        remaining = ms / 1000.0
        while remaining > 0:
            step = min(remaining, 0.2)
            await asyncio.sleep(step)
            remaining -= step
            if self._spec.cancel_event.is_set():
                slept_ok = False
                break
        if not slept_ok:
            return _cancelled_result(call_id, "wait")
        return ToolResult(
            call_id=call_id, content=f"Waited {ms}ms.", is_error=False
        )


class InspectRegionTool:
    """Zoom-style detail capture — crops a specific rectangle at
    full native resolution without the preview downscale."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="inspect_region",
            summary="Emergency zoom (use sparingly)",
            tier=ToolTier.HOT,
            description="""Emergency zoom into a screen region at full native resolution.

DO NOT USE THIS TOOL UNLESS YOU HAVE TO. The normal `screenshot`
tool already returns a 1280-wide preview that is sharp enough to
read menu items, button labels, dialog text, and most UI chrome.
You should be able to complete 95% of tasks using only `screenshot`
+ the accessibility tree tools (`read_ax_tree`, `find_element`).

Only call `inspect_region` when ALL of the following are true:
  1. You already took a `screenshot` and actually looked at it
  2. There is a specific small piece of text or UI element you
     cannot read in the preview (you tried and it's genuinely blurry)
  3. You NEED to read that specific element to make the next decision
  4. `read_ax_tree` on the target app didn't give you the info
     (which should be your first fallback — it's faster and exact)

If you're calling inspect_region "just to be sure" or "to get a
better view", you are wasting tokens and slowing the loop. The
overall screenshot is sufficient for planning clicks and reading
most UI text.

Each inspect_region call costs extra tokens (lossless PNG, usually
50-300KB) and adds latency. Budget yourself: at most ONE
inspect_region per observation cycle, and only when you've
already identified exactly what tiny element you need to see.

Parameters: (x1, y1) top-left and (x2, y2) bottom-right. Keep the
rectangle tight — a 200×100 crop is more useful than a 1000×800
one and costs less.
""",
            parameters={
                "type": "object",
                "properties": {
                    "x1": {
                        "type": "integer",
                        "description": "Top-left x in preview or native coords",
                    },
                    "y1": {
                        "type": "integer",
                        "description": "Top-left y",
                    },
                    "x2": {
                        "type": "integer",
                        "description": "Bottom-right x",
                    },
                    "y2": {
                        "type": "integer",
                        "description": "Bottom-right y",
                    },
                    "display_id": {
                        "type": "integer",
                        "description": "Optional display id (default: primary or session-pinned display)",
                    },
                },
                "required": ["x1", "y1", "x2", "y2"],
            },
        )

    async def execute(
        self, call_id: str, arguments: dict[str, Any]
    ) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "inspect_region")
        sr_err = _require_screen_recording(call_id, "inspect_region")
        if sr_err is not None:
            return sr_err
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        await _ensure_dims(self._spec, native)
        api_x1 = int(arguments.get("x1", 0))
        api_y1 = int(arguments.get("y1", 0))
        api_x2 = int(arguments.get("x2", 0))
        api_y2 = int(arguments.get("y2", 0))
        x1, y1 = _api_to_native(self._spec, api_x1, api_y1)
        x2, y2 = _api_to_native(self._spec, api_x2, api_y2)
        if x2 <= x1 or y2 <= y1:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: invalid rectangle (x1={x1}, y1={y1}, "
                    f"x2={x2}, y2={y2}) — x2 must be > x1 and y2 > y1"
                ),
                is_error=True,
            )

        display_id = arguments.get("display_id")
        effective_display = (
            int(display_id)
            if display_id is not None
            else self._spec.default_display_id
        )

        # Capture at FULL native resolution (max_dim=None means no
        # downscale). Then crop in Python. This is simpler than
        # adding a crop primitive to the Rust layer and the
        # performance is fine — PNG encoding of a cropped region
        # takes a few ms.
        try:
            frame = await asyncio.to_thread(
                native.screenshot,
                display_id=effective_display,
                max_dim=None,
                format="png",
                quality=100,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id,
                content=f"inspect_region capture failed: {exc}",
                is_error=True,
            )

        # Crop with Pillow
        try:
            from io import BytesIO
            from PIL import Image, ImageDraw

            img = Image.open(BytesIO(frame.png))
            # Clamp the crop rect to the actual image bounds.
            full_w, full_h = img.size
            cx1 = max(0, min(x1, full_w))
            cy1 = max(0, min(y1, full_h))
            cx2 = max(cx1 + 1, min(x2, full_w))
            cy2 = max(cy1 + 1, min(y2, full_h))
            crop = img.crop((cx1, cy1, cx2, cy2))

            # Composite the cursor onto the crop if it falls inside
            # the cropped region. At native resolution there's no
            # scaling to do.
            try:
                cursor_pos = native.cursor_position()
                cur_x = cursor_pos[0] - cx1
                cur_y = cursor_pos[1] - cy1
                crop_w, crop_h = crop.size
                if 0 <= cur_x <= crop_w and 0 <= cur_y <= crop_h:
                    if crop.mode not in ("RGB", "RGBA"):
                        crop = crop.convert("RGB")
                    draw = ImageDraw.Draw(crop, "RGBA")
                    arrow = [
                        (0, 0), (0, 16), (4, 12), (7, 18),
                        (9, 17), (6, 11), (11, 11),
                    ]
                    outline = [(cur_x + ax, cur_y + ay) for ax, ay in arrow]
                    draw.polygon(outline, fill=(0, 0, 0, 255))
                    cx_c = sum(ax for ax, _ in arrow) / len(arrow)
                    cy_c = sum(ay for _, ay in arrow) / len(arrow)
                    shrunk = []
                    for ax, ay in arrow:
                        dx = cx_c - ax
                        dy = cy_c - ay
                        length = max(1.0, (dx * dx + dy * dy) ** 0.5)
                        sx = ax + (dx / length) * 1.2
                        sy = ay + (dy / length) * 1.2
                        shrunk.append((cur_x + sx, cur_y + sy))
                    draw.polygon(shrunk, fill=(255, 255, 255, 255))
            except Exception:  # noqa: BLE001
                pass

            out = BytesIO()
            crop.save(out, format="PNG", optimize=True)
            png_bytes = out.getvalue()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id,
                content=f"inspect_region crop failed: {exc}",
                is_error=True,
            )

        # Emit the cropped frame to the UI via the standard
        # screenshot_frame event so it shows up in the live view
        # and in the inline conversation frame.
        await _fire(
            self._spec.emit_event,
            {
                "type": "screenshot_frame",
                "sessionId": self._spec.session_id,
                "pngBase64": _b64(png_bytes),
                "mimeType": "image/png",
                "width": cx2 - cx1,
                "height": cy2 - cy1,
                "takenAt": int(time.time() * 1000),
                "reason": "inspect_region",
            },
        )

        return ToolResult(
            call_id=call_id,
            content=(
                f"Inspected region ({cx1},{cy1})→({cx2},{cy2}): "
                f"{cx2 - cx1}×{cy2 - cy1} PNG, "
                f"{len(png_bytes) // 1024}KB. Full-resolution detail "
                "— text and icons should be legible."
            ),
            is_error=False,
        )


class ReadAxTreeTool:
    """Dump the accessibility tree for a running app."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_ax_tree",
            summary="Read the accessibility tree for an app",
            tier=ToolTier.HOT,
            description="""Return a JSON tree of every UI element in an app's accessibility hierarchy.

Each node has role (AXButton, AXWindow, AXTextField, ...), title,
description, identifier, bounds, and children. This is dramatically
faster and more reliable than screenshot+vision for apps that are
AX-friendly (most native macOS apps, Cocoa, SwiftUI). Electron,
web views, and custom-drawn UIs may return empty trees — in which
case fall back to `screenshot` + pixel counting.

Prefer this over `screenshot` whenever the target exposes an AX
tree: it's 10-50x faster and gives exact element bounds, so you can
click the *center* of a button without guessing.

Pass `pid` from `list_windows` or `get_frontmost_window`.
""",
            parameters={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer"},
                    "max_depth": {
                        "type": "integer",
                        "description": "Max recursion depth (default 8)",
                    },
                },
                "required": ["pid"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "read_ax_tree")
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        await _ensure_dims(self._spec, native)
        pid = int(arguments.get("pid", 0))
        max_depth = int(arguments.get("max_depth", 8))
        if pid <= 0:
            return ToolResult(
                call_id=call_id, content="Error: pid is required", is_error=True
            )
        try:
            tree_json = await asyncio.to_thread(
                native.read_ax_tree, pid, max_depth=max_depth
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"read_ax_tree failed: {exc}", is_error=True
            )
        if not tree_json or tree_json == "{}":
            return ToolResult(
                call_id=call_id,
                content=(
                    f"AX tree for pid={pid} was empty. This usually means "
                    "the app isn't AX-friendly (Electron, custom-drawn UIs). "
                    "Fall back to `screenshot` and pixel-counting."
                ),
                is_error=False,
            )

        # Parse the tree and translate every `bounds` entry from native
        # display points to API space so the numbers agree with
        # list_windows, find_element, cursor_position, and the
        # screenshot the model is looking at. If the JSON parse fails
        # (shouldn't happen — the native side produces it), fall back
        # to the raw string with a coordinate-space caveat appended.
        translated_json: str
        try:
            import json as _json

            tree_obj = _json.loads(tree_json)
            _translate_ax_tree_bounds(self._spec, tree_obj)
            translated_json = _json.dumps(tree_obj, separators=(",", ":"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("ax tree bound translation failed: %s", exc)
            translated_json = tree_json

        # Truncate huge trees — the model will ask for specific subtrees
        # if it needs more. 40KB of JSON is ~10k tokens.
        if len(translated_json) > 40_000:
            translated_json = translated_json[:40_000] + "\n\n… [truncated]"
        return ToolResult(
            call_id=call_id,
            content=(
                f"AX tree for pid={pid} (bounds in API coordinate space, "
                f"matching your last screenshot):\n{translated_json}"
            ),
            is_error=False,
        )


class FindElementTool:
    """Locate an AX element by role/label and return its bounds."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="find_element",
            summary="Find an AX element by role/label",
            tier=ToolTier.HOT,
            description="""Search the accessibility tree for an element matching role/label/title.

Any subset of (role, label, title) can be provided — all supplied
fields must match. Matching is a case-insensitive substring on the
label field and an exact match on role.

Returns (x, y, w, h) bounds in display points, ready to pass to
`click` via its center. This is the high-precision alternative to
vision grounding when the target app exposes AX.

Example: `find_element(pid=123, role="AXButton", label="Submit")`
""",
            parameters={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer"},
                    "role": {
                        "type": "string",
                        "description": "AX role (AXButton, AXTextField, AXWindow, ...)",
                    },
                    "label": {
                        "type": "string",
                        "description": "Partial/case-insensitive match against AXDescription/AXTitle/AXValue",
                    },
                    "title": {
                        "type": "string",
                        "description": "AXTitle substring match",
                    },
                },
                "required": ["pid"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "find_element")
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )

        await _ensure_dims(self._spec, native)
        pid = int(arguments.get("pid", 0))
        role = arguments.get("role")
        label = arguments.get("label")
        title = arguments.get("title")
        if pid <= 0:
            return ToolResult(
                call_id=call_id, content="Error: pid is required", is_error=True
            )
        try:
            bounds = await asyncio.to_thread(
                native.find_ax_element,
                pid,
                role=role,
                label=label,
                title=title,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"find_element failed: {exc}", is_error=True
            )
        if bounds is None:
            query = ", ".join(
                f"{k}={v!r}" for k, v in (("role", role), ("label", label), ("title", title)) if v
            )
            return ToolResult(
                call_id=call_id,
                content=f"No element found for pid={pid} {query}. Try relaxing the query or screenshot + vision grounding.",
                is_error=False,
            )
        # AX bounds come back in native display points. Translate to
        # API space so they're immediately usable as click/move_mouse
        # arguments without the model doing any scaling math.
        native_cx, native_cy = bounds.center
        api_x, api_y = _native_to_api(self._spec, bounds.x, bounds.y)
        api_x2, api_y2 = _native_to_api(
            self._spec, bounds.x + bounds.w, bounds.y + bounds.h
        )
        api_w = max(1, api_x2 - api_x)
        api_h = max(1, api_y2 - api_y)
        api_cx, api_cy = _native_to_api(self._spec, native_cx, native_cy)
        return ToolResult(
            call_id=call_id,
            content=(
                f"Found element: bounds=({api_x}, {api_y}, "
                f"{api_w}x{api_h}), center=({api_cx}, {api_cy}). "
                f"Coordinates are in the SAME space as your last "
                f"screenshot — pass center directly to `click` to "
                f"activate it."
            ),
            is_error=False,
        )


class CursorPositionTool:
    """Report where the cursor currently is."""

    def __init__(self, spec: ComputerToolSpec) -> None:
        self._spec = spec

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="cursor_position",
            summary="Get current mouse cursor position",
            tier=ToolTier.HOT,
            description="""Return the current mouse position as (x, y) in display points.

Zero-permission — works even without Screen Recording or Accessibility.
Useful as a quick sanity check that the native layer is alive.
""",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._spec.enabled:
            return _disabled_result(call_id, "cursor_position")
        try:
            native = _import_native()
        except ImportError as exc:
            return ToolResult(
                call_id=call_id, content=f"Error: {exc}", is_error=True
            )
        await _ensure_dims(self._spec, native)
        try:
            native_x, native_y = await asyncio.to_thread(native.cursor_position)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id, content=f"cursor_position failed: {exc}", is_error=True
            )
        api_x, api_y = _native_to_api(self._spec, native_x, native_y)
        return ToolResult(
            call_id=call_id,
            content=f"Cursor at ({api_x}, {api_y}).",
            is_error=False,
        )


# ─── Helper used by the registry builder ────────────────────────────────


def build_computer_tools(spec: ComputerToolSpec) -> list[Any]:
    """Return all the atomic computer tools, ready to be registered.

    The registry builder passes a ComputerToolSpec into this function
    when `computer_control.enabled` is True in settings. If native
    bindings aren't installed, the tools still construct (they just
    error out at call time with a clear message).

    `InspectRegionTool` is intentionally NOT registered here right now.
    In practice the model was calling it "just to be sure" on almost
    every observation cycle, which doubled screenshot traffic and
    produced large PNG payloads that blew out the context window
    without providing any new grounding the 1280-wide preview didn't
    already have. The class itself stays defined so we can flip it
    back on by adding it to this list once we've tuned the usage.
    """
    return [
        ScreenshotTool(spec),
        ListDisplaysTool(spec),
        ListWindowsTool(spec),
        FocusWindowTool(spec),
        ClickTool(spec),
        MoveMouseTool(spec),
        TypeTextTool(spec),
        PressKeyTool(spec),
        KeyDownTool(spec),
        KeyUpTool(spec),
        ScrollTool(spec),
        WaitTool(spec),
        ReadAxTreeTool(spec),
        FindElementTool(spec),
        CursorPositionTool(spec),
    ]
