"""Accessibility tree → the observation Jev reads.

The tree comes from `freyja_native.read_ax_tree(pid)` as nested dicts with
role/subrole/title/label/value/description/help/enabled/focused/bounds/children.
`label` is AXDescription (the accessible name of icon buttons and most SwiftUI
controls) and `description` is AXRoleDescription ("button", "text"), which is
never a name. This module flattens the tree into an indexed table of actionable
elements, a block of screen text in reading order, and a fingerprint used to
detect whether an action changed anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

CLICK_ROLES = {
    "AXButton",
    "AXMenuButton",
    "AXPopUpButton",
    "AXCheckBox",
    "AXRadioButton",
    "AXLink",
    "AXRow",
    "AXCell",
    "AXMenuItem",
    "AXMenuBarItem",
    "AXDisclosureTriangle",
    "AXTab",
    "AXToolbarButton",
}
TYPE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
TEXT_ROLES = {"AXStaticText", "AXHeading"}
SKIP_SUBTREES = {"AXMenuBar"}
SECURE_SUBROLES = {"AXSecureTextField"}
DIALOG_ROLES = {"AXSheet"}
DIALOG_SUBROLES = {"AXDialog", "AXSystemDialog", "AXSystemFloatingWindow"}
# Window buttons carry no name, but their role description ("close button") is specific.
NAMED_BY_SUBROLE = {"AXCloseButton", "AXMinimizeButton", "AXZoomButton", "AXFullScreenButton"}
# Elements outside these containers' frames are scrolled out of view: AX still reports
# their bounds, and a click at their center lands on whatever is drawn there instead.
CLIP_RESET_ROLES = {"AXWindow", "AXSheet", "AXPopover"}
CLIP_ROLES = {"AXScrollArea"}

MAX_ROWS = 200
MAX_TEXT_CHARS = 3000
MAX_LABEL_CHARS = 80
MAX_VALUE_CHARS = 120
DIFF_VALUE_CHARS = 60
DOOR_VALUE_CHARS = 4000  # the LLM doors read whole documents; Jev only needs a glimpse
# Every window has these with the same labels, so rows name the window they belong to.
WINDOW_CHROME = ("AXCloseButton", "AXMinimizeButton", "AXZoomButton", "AXFullScreenButton")
TOGGLE_ROLES = ("AXCheckBox", "AXSwitch", "AXToggle")
# Controls whose AXValue is their state ("0"/"1"), never their name.
STATE_ROLES = (*TOGGLE_ROLES, "AXRadioButton", "AXDisclosureTriangle")
DISCLOSURE_STATES = {"0": "collapsed", "1": "expanded"}
TOGGLE_STATES = {"0": "off", "1": "on", "2": "mixed"}
RADIO_STATES = {"0": "not selected", "1": "selected"}


@dataclass
class Element:
    index: int
    role: str
    subrole: str | None
    label: str
    value: str | None
    bounds: tuple[float, float, float, float]
    enabled: bool
    focused: bool
    window: str
    kind: str  # "click" | "type"
    offscreen: bool = False
    has_submenu: bool = False  # menu items: clicking opens a submenu instead of running a command
    scroll_area: tuple[float, float, float, float] | None = None  # visible rect of its scroll view
    window_frame: tuple[float, float, float, float] | None = None  # frame of its AX window

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.bounds
        return int(x + w / 2), int(y + h / 2)

    def row(self, value_chars: int = MAX_VALUE_CHARS, *, keep_lines: bool = False) -> str:
        parts = [f"[{self.index}] {self.role} {self.label!r}"]
        states = []
        toggle = self.toggle_state()
        if toggle:
            states.append(toggle)
        elif self.value:
            parts.append(f"value={_clip(self.value, value_chars, keep_lines=keep_lines)!r}")
        if not self.enabled:
            states.append("disabled")
        if self.focused:
            states.append("focused")
        if self.subrole == "AXSecureTextField":
            states.append("secure")
        if self.offscreen:
            states.append("scrolled out of view")
        if self.has_submenu:
            states.append("opens submenu")
        if self.subrole in WINDOW_CHROME and self.window:
            states.append(f"of window {self.window!r}")
        if states:
            parts.append("(" + ", ".join(states) + ")")
        return " ".join(parts)

    def toggle_state(self) -> str | None:
        if self.role == "AXDisclosureTriangle":
            return DISCLOSURE_STATES.get(self.value or "")
        if self.role == "AXRadioButton":
            return RADIO_STATES.get(self.value or "")
        if self.role in TOGGLE_ROLES or self.subrole in TOGGLE_ROLES:
            return TOGGLE_STATES.get(self.value or "")
        return None

    def fingerprint(self) -> str:
        return f"{self.role}|{self.label}|{self.value or ''}|{int(self.bounds[0])},{int(self.bounds[1])}"


@dataclass
class Observation:
    # Which observation backend produced this; a later stage adds more surfaces.
    SURFACE: ClassVar[str] = "ax"
    app_name: str
    bundle: str
    pid: int
    windows: list[str]
    focused_window: str
    elements: list[Element]
    screen_text: str
    dialog: dict[str, str] | None
    menu_open: bool
    node_count: int
    read_ms: int
    truncated: bool = False
    raw_tree: dict[str, Any] | None = field(default=None, repr=False)
    window_frames: list[tuple[float, float, float, float]] = field(default_factory=list)
    focused_frame: tuple[float, float, float, float] | None = None

    def scroll_point(self, hint: str) -> tuple[int, int] | None:
        """Where to scroll: inside the scroll view holding rows that are scrolled out of
        view, preferring rows `hint` names. None when nothing is out of view."""
        hidden = [e for e in self.elements if e.offscreen and e.scroll_area]
        if not hidden:
            return None
        named = [
            e for e in hidden
            if len(e.label) >= 3 and re.search(rf"(?<!\w){re.escape(e.label)}(?!\w)", hint, re.I)
        ]
        areas = [e.scroll_area for e in named or hidden]
        x, y, w, h = max(set(areas), key=areas.count)
        return int(x + w / 2), int(y + h / 2)

    @property
    def click_targets(self) -> list[Element]:
        return [e for e in self.elements if e.kind == "click" and e.enabled and not e.offscreen]

    @property
    def type_targets(self) -> list[Element]:
        return [
            e
            for e in self.elements
            if e.kind == "type"
            and e.enabled
            and not e.offscreen
            and e.subrole not in SECURE_SUBROLES
        ]

    def element_at(self, x: float, y: float) -> Element | None:
        """Smallest listed element whose bounds contain the point."""
        hits = [
            e
            for e in self.elements
            if e.bounds[0] <= x <= e.bounds[0] + e.bounds[2]
            and e.bounds[1] <= y <= e.bounds[1] + e.bounds[3]
        ]
        return min(hits, key=lambda e: e.bounds[2] * e.bounds[3]) if hits else None

    def table(self) -> str:
        return "\n".join(e.row() for e in self.elements)

    def door_table(self) -> str:
        """The table for the LLM doors: whole values with their line breaks, so a
        planner can count lines or see paragraphs; Jev only needs a glimpse."""
        return "\n".join(e.row(DOOR_VALUE_CHARS, keep_lines=True) for e in self.elements)

    def by_index(self, index: int) -> Element | None:
        return next((e for e in self.elements if e.index == index), None)

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        h.update(self.focused_window.encode())
        h.update(b"\0")
        h.update(self.screen_text.encode())
        for e in self.elements:
            h.update(b"\0")
            h.update(e.fingerprint().encode())
        return h.hexdigest()[:16]

    def to_state(self, history: list[dict[str, Any]], subgoal: str | None) -> dict[str, Any]:
        state: dict[str, Any] = {
            "app": {
                "name": self.app_name,
                "windows": self.windows,
                "focused_window": self.focused_window,
            },
            "screen_text": self.screen_text,
            "elements": self.table()
            if self.elements
            else "(no actionable elements exposed by the accessibility tree)",
            "recent_actions": history[-8:],
        }
        if self.dialog:
            state["dialog"] = self.dialog
        if self.menu_open:
            state["menu_open"] = True
        if self.truncated:
            state["elements_truncated"] = True
        if subgoal:
            state["current_subgoal"] = subgoal
        return state


def _clip(s: str, n: int, *, keep_lines: bool = False) -> str:
    if not keep_lines:
        s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# Invisible direction marks some apps (Calculator) wrap around numbers; they break
# literal matching and leak into summaries.
_BIDI_MARKS = dict.fromkeys(
    map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
)


def _first_text(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        v = node.get(key)
        if isinstance(v, str) and (v := v.translate(_BIDI_MARKS).strip()):
            return v
    return ""


def _text_of(node: dict[str, Any]) -> str:
    """The name of a control or window."""
    return _first_text(node, ("title", "label", "value", "help"))


def _display_text(node: dict[str, Any]) -> str:
    """What a static text or heading shows; its AXValue, not its accessible name."""
    return _first_text(node, ("value", "title", "label"))


def _descendant_text(node: dict[str, Any], limit: int = 3, depth: int = 3) -> str:
    out: list[str] = []

    def walk(n: dict[str, Any], d: int) -> None:
        if len(out) >= limit or d > depth:
            return
        for ch in n.get("children") or []:
            if ch.get("role") in TEXT_ROLES:
                t = _display_text(ch)
                if t:
                    out.append(t)
            elif ch.get("role") in ("AXTextField", "AXImage"):
                t = _first_text(ch, ("title", "label"))
                if t:
                    out.append(t)
            walk(ch, d + 1)

    walk(node, 0)
    return " · ".join(dict.fromkeys(out))


def _intersect(
    a: tuple[float, float, float, float] | None, b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    if a is None:
        return b
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def _center_outside(
    b: tuple[float, float, float, float], clip: tuple[float, float, float, float] | None
) -> bool:
    if clip is None:
        return False
    cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
    return not (clip[0] <= cx <= clip[0] + clip[2] and clip[1] <= cy <= clip[1] + clip[3])


def _bounds(node: dict[str, Any]) -> tuple[float, float, float, float] | None:
    b = node.get("bounds")
    if isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b):
        if b[2] <= 0 or b[3] <= 0:
            return None
        return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    return None


def _menu_showing(menu: dict[str, Any]) -> bool:
    """Closed menus still list their items, but with zero-size bounds."""
    items = [ch for ch in menu.get("children") or [] if ch.get("role") == "AXMenuItem"]
    return menu.get("role") == "AXMenu" and any(_bounds(ch) is not None for ch in items)


def build_observation(
    tree: dict[str, Any],
    *,
    app_name: str,
    bundle: str,
    pid: int,
    read_ms: int = 0,
    max_rows: int = MAX_ROWS,
) -> Observation:
    elements: list[Element] = []
    texts: list[tuple[float, float, str]] = []
    windows: list[str] = []
    window_frames: list[tuple[float, float, float, float]] = []
    focused_window = ""
    focused_frame: tuple[float, float, float, float] | None = None
    dialog: dict[str, str] | None = None
    menu_open = False
    node_count = 0

    Rect = tuple[float, float, float, float]

    def walk(
        node: dict[str, Any],
        window: str,
        depth: int,
        in_row: bool,
        clip: Rect | None,
        frame: Rect | None,
        row: str = "",
    ) -> None:
        nonlocal focused_window, focused_frame, dialog, menu_open, node_count
        node_count += 1
        role = node.get("role") or ""
        subrole = node.get("subrole")
        if role in SKIP_SUBTREES:
            for ch in node.get("children") or []:
                if ch.get("role") == "AXMenuBarItem":
                    _add(ch, window, "click", in_row, None)
                    for menu in ch.get("children") or []:
                        if _menu_showing(menu):
                            menu_open = True
                            _walk_menu(menu, window)
            return
        own = _bounds(node)
        if own is not None:
            if role in CLIP_RESET_ROLES:
                clip = own
            elif role in CLIP_ROLES:
                clip = _intersect(clip, own)
        if role == "AXWindow":
            window = _text_of(node) or "(untitled window)"
            windows.append(window)
            frame = own
            if own is not None:
                window_frames.append(own)
            if node.get("focused") or (not focused_window and subrole != "AXDialog"):
                focused_window = window
                focused_frame = own
            if subrole in DIALOG_SUBROLES:
                dialog = {"title": window, "text": _descendant_text(node, limit=6, depth=6)}
        if role in DIALOG_ROLES:
            dialog = {
                "title": _text_of(node) or "sheet",
                "text": _descendant_text(node, limit=6, depth=6),
            }
        if role == "AXMenu":
            if _menu_showing(node):
                menu_open = True
                _walk_menu(node, window)
            return
        if role in TEXT_ROLES:
            t = _display_text(node)
            if t and own and not in_row and not _center_outside(own, clip):
                texts.append((own[1], own[0], t))
        elif role in CLICK_ROLES or role in TYPE_ROLES or subrole in SECURE_SUBROLES:
            added = _add(
                node,
                window,
                "type" if (role in TYPE_ROLES or subrole in SECURE_SUBROLES) else "click",
                in_row,
                clip,
                frame,
                row,
            )
            if added and role in ("AXRow", "AXCell"):
                in_row = True
                row = elements[-1].label
        for ch in node.get("children") or []:
            walk(ch, window, depth + 1, in_row, clip, frame, row)

    def _walk_menu(menu: dict[str, Any], window: str) -> None:
        for item in menu.get("children") or []:
            if item.get("role") != "AXMenuItem":
                continue
            children = item.get("children") or []
            submenus = [s for s in children if s.get("role") == "AXMenu" and s.get("children")]
            if _add(item, window, "click", False, None):
                elements[-1].has_submenu = bool(submenus)
            for sub in submenus:
                if _menu_showing(sub):
                    _walk_menu(sub, window)

    def _add(
        node: dict[str, Any],
        window: str,
        kind: str,
        in_row: bool,
        clip: Rect | None,
        frame: Rect | None = None,
        row: str = "",
    ) -> bool:
        role = node.get("role") or ""
        b = _bounds(node)
        if b is None:
            return False
        if role in TYPE_ROLES:
            label = _first_text(node, ("title", "label", "help", "placeholder"))
        elif role in STATE_ROLES:
            label = _first_text(node, ("title", "label", "help"))
        else:
            label = _text_of(node)
        if role in ("AXRow", "AXCell") or not label:
            label = _descendant_text(node) or label
        if not label and role in STATE_ROLES:
            label = row  # e.g. the switch in a settings row named by the row's text
        if not label and node.get("subrole") in NAMED_BY_SUBROLE:
            label = _first_text(node, ("description",))
        if role in TYPE_ROLES and not label:
            label = _first_text(node, ("description",)) or "text field"
        if not label:
            return False
        if in_row and role in ("AXCell", "AXStaticText"):
            return False
        value = node.get("value")
        value_s = None if value is None else str(value).translate(_BIDI_MARKS)
        if role in TYPE_ROLES and value_s == label:
            value_s = None
        if node.get("subrole") in SECURE_SUBROLES:
            value_s = "••••" if value_s else None
        elements.append(
            Element(
                index=len(elements) + 1,
                role=role,
                subrole=node.get("subrole"),
                label=_clip(label, MAX_LABEL_CHARS),
                value=value_s,
                bounds=b,
                enabled=bool(node.get("enabled", True)),
                focused=bool(node.get("focused", False)),
                window=window,
                kind=kind,
                offscreen=_center_outside(b, clip),
                scroll_area=clip,
                window_frame=frame,
            )
        )
        return True

    walk(tree, "", 0, False, None, None)

    truncated = False
    if len(elements) > max_rows:
        truncated = True
        visible = [e for e in elements if not e.offscreen]
        focused = [e for e in visible if e.window == focused_window]
        others = [e for e in visible if e.window != focused_window]
        hidden = [e for e in elements if e.offscreen]
        elements = (focused + others + hidden)[:max_rows]
        for i, e in enumerate(elements, 1):
            e.index = i

    texts.sort(key=lambda t: (round(t[0] / 8), t[1]))
    seen: set[str] = set()
    lines: list[str] = []
    for _, _, t in texts:
        t = _clip(t, 200)
        if t in seen:
            continue
        seen.add(t)
        lines.append(t)
    screen_text = "\n".join(lines)
    if len(screen_text) > MAX_TEXT_CHARS:
        screen_text = screen_text[: MAX_TEXT_CHARS - 1] + "…"

    return Observation(
        app_name=app_name,
        bundle=bundle,
        pid=pid,
        windows=windows,
        focused_window=focused_window or (windows[0] if windows else ""),
        elements=elements,
        screen_text=screen_text,
        dialog=dialog,
        menu_open=menu_open,
        node_count=node_count,
        read_ms=read_ms,
        truncated=truncated,
        raw_tree=tree,
        window_frames=window_frames,
        focused_frame=focused_frame or (window_frames[0] if window_frames else None),
    )


def diff_observations(before: Observation, after: Observation) -> tuple[bool, str]:
    """Return (changed, summary). The summary is short, literal, and goes into
    the history Jev reads, so it names what moved rather than judging it."""
    notes: list[str] = []
    if before.focused_window != after.focused_window:
        notes.append(f"window {before.focused_window!r} -> {after.focused_window!r}")
    if bool(before.dialog) != bool(after.dialog):
        notes.append("dialog appeared" if after.dialog else "dialog dismissed")
    if before.menu_open != after.menu_open:
        notes.append("menu opened" if after.menu_open else "menu closed")

    b_els = {(e.window, e.role, e.label): e for e in before.elements}
    a_els = {(e.window, e.role, e.label): e for e in after.elements}
    value_changes = []
    for k, b in b_els.items():
        a = a_els.get(k)
        if a is None or a.value == b.value:
            continue
        if a.toggle_state() and b.toggle_state():
            value_changes.append(f"{k[2]!r} now {a.toggle_state()}")
        else:
            was, now = (_clip(e.value or "", DIFF_VALUE_CHARS) for e in (b, a))
            value_changes.append(f"{k[2]!r}: {was!r} -> {now!r}")
    notes.extend(value_changes[:4])
    added = len(set(a_els) - set(b_els))
    removed = len(set(b_els) - set(a_els))
    if added or removed:
        notes.append(f"elements +{added}/-{removed}")

    if before.screen_text != after.screen_text:
        b_lines = set(before.screen_text.split("\n"))
        new_lines = [ln for ln in after.screen_text.split("\n") if ln and ln not in b_lines]
        if new_lines:
            notes.append("new text: " + " | ".join(_clip(ln, 60) for ln in new_lines[:3]))
        elif not value_changes:
            notes.append("screen text changed")

    changed = before.fingerprint() != after.fingerprint()
    if changed and not notes:
        notes.append("layout changed")
    return changed, "; ".join(notes) if notes else "no visible change"


# Stuck detection counts only changes that show the action did something. A
# fingerprint moves on autocomplete churn and a +0/-1 element jitter, which
# kept an operator pressing escape forever (2026-10-07).
MEANINGFUL_ELEMENT_DELTA = 3


def meaningful_change(before: Observation, after: Observation) -> bool:
    """True when the screen moved in a way that shows progress: another window,
    a dialog or menu opened or closed, screen-text lines came or went, or more
    than a few elements appeared or disappeared. Text-field values are ignored."""
    if before.focused_window != after.focused_window:
        return True
    if bool(before.dialog) != bool(after.dialog) or before.menu_open != after.menu_open:
        return True
    typed = {
        ln
        for o in (before, after)
        for e in o.elements
        if e.kind == "type" and e.value
        for ln in e.value.split("\n")
    }
    b_lines = {ln for ln in before.screen_text.split("\n") if ln and ln not in typed}
    a_lines = {ln for ln in after.screen_text.split("\n") if ln and ln not in typed}
    if b_lines != a_lines:
        return True
    b_keys = {(e.window, e.role, e.label) for e in before.elements}
    a_keys = {(e.window, e.role, e.label) for e in after.elements}
    return len(a_keys - b_keys) + len(b_keys - a_keys) > MEANINGFUL_ELEMENT_DELTA


# Double quotes pair with their own kind; a single quote only opens or closes a
# literal when it is not inside a word, so apostrophes ("Don't") never split one.
_QUOTED = re.compile(
    r"\"([^\"]{1,200})\""
    r"|“([^”]{1,200})”"
    r"|(?<!\w)'(.{1,200}?)'(?!\w)"
    r"|(?<!\w)‘(.{1,200}?)’(?!\w)"
)
_NUMBERISH = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?![\w.])")


def literals_from_goal(goal: str) -> list[str]:
    """Strings the goal itself supplies as typing candidates: quoted spans
    first, then bare numbers. Jev chooses among these so simple goals need
    no LLM call to fill a field."""
    out: list[str] = []
    for m in _QUOTED.finditer(goal):
        s = next(g for g in m.groups() if g is not None).strip()
        if s and s not in out:
            out.append(s)
    for m in _NUMBERISH.finditer(goal):
        s = m.group(0)
        if s not in out:
            out.append(s)
    return out[:12]


def dumps_state(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False)
