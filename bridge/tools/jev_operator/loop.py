"""The operator loop: observe → decide (Jev) → act → verify, with the LLM
doors described in docs/jev-operator.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from bridge.decisions.provider import DecisionError, DecisionProvider
from bridge.tools.computer_tools import ComputerToolSpec
from bridge.tools.jev_operator.act import Actuator, Cancelled, Refused
from bridge.tools.jev_operator.decide import (
    ALWAYS_GATED_KEYS,
    COMMIT_KEYS,
    KEY_OPTIONS,
    _PLANNER_CLICK,
    Decision,
    Thresholds,
    decide,
    describe,
)
from bridge.tools.jev_operator.handoff import MAX_WAIT_S, REPLAN_MAX_TOKENS, LLMHelper
from bridge.tools.jev_operator.observe import (
    STATE_ROLES,
    WINDOW_CHROME,
    Element,
    Observation,
    build_observation,
    diff_observations,
    literals_from_goal,
    quoted_literals,
    meaningful_change,
    typed_values,
)
from bridge.tools.jev_operator.notes import app_notes, site_notes
from bridge.tools.jev_operator import dom_surface, vision
from bridge.tools.jev_operator.dom_surface import (
    ABOVE_MARK,
    PAGE_CUT_MARK,
    VIEW_MARK,
    DOMSurface,
    DOMUnavailable,
    TabGone,
)
from bridge.tools.jev_operator.surface import AXSurface, Surface

SELF_BUNDLE = "co.freyja.desktop"
RUN_DIR = Path(
    os.environ.get("FREYJA_JEV_OPERATOR_DIR") or Path.home() / ".freyja" / "jev-operator" / "runs"
)

IRREVERSIBLE_PATTERNS = [
    r"\bdelete\b",
    r"\bremove\b",
    r"\bsend\b",
    r"\bsubmit\b",
    r"\bpurchase\b",
    r"\bbuy\b",
    r"\bpay\b",
    r"\bcheckout\b",
    r"\bplace order\b",
    r"\bempty trash\b",
    r"\bmove to trash\b",
    r"\berase\b",
    r"\breset\b",
    r"\bshut down\b",
    r"\brestart\b",
    r"\blog out\b",
    r"\bsign out\b",
    r"\buninstall\b",
    r"\bdiscard\b",
    r"\bdon.t save\b",
    r"\breplace\b",
    r"\boverwrite\b",
    r"\bformat\b",
    r"\bunsubscribe\b",
    r"\bpost\b",
    r"\bpublish\b",
    r"\btransfer\b",
    r"\bclear (all|history|data|cache|browsing)\b",
    r"\bmove to bin\b",
    r"\bempty bin\b",
    r"\brevert\b",
    r"\bforce quit\b",
    r"\bsleep\b",
    r"\block screen\b",
    # Web apps: merges, approvals, deploys, paid or access-granting switches.
    r"\bmerge\b",
    r"\bapprove\b",
    r"\breject\b",
    r"\bdeploy\b",
    r"\bconfirm\b",
    r"\baccept (the )?(terms|invitation|invite|offer|agreement|and)\b",
    # "Agree" after a terms box accepts the terms (a Marketplace purchase, say).
    r"\bagree\b",
    r"\benable\b",
    r"\brequest access\b",
    r"\barchive\b",
    r"\brevoke\b",
    r"\bdeactivate\b",
    r"\bcancel (my |the |your )?(subscription|plan|order|membership|account|booking|reservation)\b",
    r"\bsubscribe\b",
    r"\bupgrade\b",
    r"\binvite\b",
    r"\bwithdraw\b",
    r"\bbook now\b",
    r"\breserve\b",
]
_IRREVERSIBLE = re.compile("|".join(IRREVERSIBLE_PATTERNS), re.I)
# Roles whose activation can commit something. Menu-bar items only open a menu,
# and rows, cells, tabs, and checkboxes select or toggle, so matching their labels
# only produces false stops (TextEdit's "Format" menu, a file named "Remove me.txt").
GATED_ROLES = {"AXButton", "AXMenuItem", "AXLink", "AXMenuButton", "AXToolbarButton"}
# Share of the window that must differ after an action taken from a
# screenshot for it to count as a change. Measured on the web envs: a toggle
# flipping 0.12%, a field taking focus 0.34%, a name typed 0.08%, a checkbox
# ticked 0.02%, a caret 0.003%, an unchanged page 0 (evals/jev_box/RESULTS.md).
PIXEL_CHANGE_MIN = 0.00008
# A replan may return this many actions in order; past the first, each click is
# found by Jev on a fresh capture, so each step is a step the planner did not
# see. Measured on blind runs, see evals/jev_box/RESULTS.md.
MAX_DIRECT_ACTIONS = 4
MAX_PLAN_WAIT_S = 5.0
# A second look at an expectation that did not hold, for a screen that is
# still arriving (a page load, a sheet sliding in).
EXPECT_RETRY_S = 1.5
MAX_VERIFY_ROUNDS = 2
PROBE_RETRIES = 2
# Besides the final screen, the end-state check reads the other screens the run
# read, each whole and in order, and the planner's notes: a list read page by
# page is gone from the final screen. Capped to keep the check one moderate call.
SEEN_MAX_CHARS = 40_000
# Screens kept in memory, a little over what one check is sent.
SCREENS_KEPT_CHARS = 80_000
PLANNER_NOTES_KEPT = 12
BELOW_MARK = "[below the visible area]"
# Lines the surfaces add about the text itself, never page content.
TEXT_MARKS = {BELOW_MARK, ABOVE_MARK, VIEW_MARK, PAGE_CUT_MARK}
_MORE_TEXT = re.compile(r"^\[[\d,]+ more characters of page text are not shown here\]$")
# Page scrolls in a row that brought no text the run had not read force a
# replan. With the whole page read at once, scrolling over it re-reads it: a run
# once scrolled up and down a results page for four minutes.
MAX_IDLE_SCROLLS = 4
# The same action this many times in a row with no meaningful change forces a
# replan; the same thing again after that ends the run blocked.
MAX_REPEATS = 3
# A planner wait leaves this much of the run's time for what comes after it.
WAIT_MARGIN_S = 20.0
HANDOFF_STATUSES = ("blocked", "budget_exhausted", "error", "needs_confirmation")
HANDOFF_ROWS = 60
HANDOFF_SCREEN_CHARS = 300

APP_DIRS = [
    "/Applications",
    "/System/Applications",
    "/System/Applications/Utilities",
    str(Path.home() / "Applications"),
]


_URL = re.compile(r"https?://[^\s\"'<>()\[\]{}“”‘’]+", re.I)
_INDEX_REF = re.compile(r"\[(\d{1,4})\]")
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”')
_OPEN_VERB = re.compile(r"\b(?:open|go to|visit|navigate to|load|browse to)\s*$", re.I)


def _top(probs: dict[str, float], n: int) -> dict[str, float]:
    """The n likeliest options, rounded, likeliest first."""
    return {k: round(v, 3) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:n]}


def urls_from_goal(goal: str) -> list[str]:
    """Addresses the goal sends the run to. A quoted address is text to type (a
    form's website field), not a page to open, unless the goal says to open it
    ('Business website "https://acme.example"' once opened acme.example)."""
    quoted = [m.span() for m in _QUOTED.finditer(goal)]
    out: list[str] = []
    for m in _URL.finditer(goal):
        span = next((q for q in quoted if q[0] < m.start() < q[1]), None)
        if span is not None and not _OPEN_VERB.search(goal[max(0, span[0] - 24) : span[0]]):
            continue
        u = m.group(0).rstrip(".,;:!?")
        if u not in out:
            out.append(u)
    return out[:5]


# The goal gives an address only for when the run cannot go on where it is:
# "Continue in the results tab ... If the owned tab is unavailable, open https://…".
_FALLBACK = re.compile(
    r"\b(?:if|when|in case|should)\b[^.!?;\n]{0,120}?\b(?:unavailable|not available|missing|gone|"
    r"closed|not open|cannot|can't|isn't|is not|doesn't|does not|fails?|not found|no longer)\b"
    r"|\b(?:otherwise|or else|failing that|as a fallback|fall back|if needed|if necessary)\b",
    re.I,
)


def fallback_urls(goal: str) -> set[str]:
    """Addresses the goal gives only as a fallback, in a clause that starts
    with a condition ("if it is unavailable, open …", "otherwise open …")."""
    out: set[str] = set()
    for m in _URL.finditer(goal):
        start = max(goal.rfind(c, 0, m.start()) for c in ".!?;\n")
        if _FALLBACK.search(goal[start + 1 : m.start()]):
            out.add(m.group(0).rstrip(".,;:!?"))
    return out


def same_site(a: str, b: str) -> bool:
    """The same site: the last two parts of the host name match (a link
    shortener and its booking pages, t.bookwell.example and
    alameda.bookwell.example, are one site)."""

    def site(u: str) -> str:
        return ".".join((urlparse(u).hostname or "").lower().split(".")[-2:])

    return bool(site(a)) and site(a) == site(b)


def same_page(current: str, wanted: str) -> bool:
    """Is the browser already on `wanted` (same address, ignoring scheme, www,
    a trailing slash and the fragment; extra query or path on `current` is fine)?"""

    def n(u: str) -> str:
        u = u.split("#")[0].rstrip("/").lower()
        return re.sub(r"^https?://(www\.)?", "", u)

    c, w = n(current), n(wanted)
    return bool(c) and (c == w or c.startswith(w))


def own_effect(
    pending: dict[str, Any], key: Any, before: Observation, after: Observation
) -> bool:
    """The action's own target took the change it was meant to make: a field now
    holds new text, a checkbox flipped, a list option became selected. Filling a
    form changes nothing else on screen, and three verified fields in a row were
    being counted as "stuck"."""
    if not key or not pending.get("ok", True) or pending.get("kind") not in ("type", "click", "scroll"):
        return False
    key = tuple(key)
    b = next((e for e in before.elements if (e.window, e.role, e.label) == key), None)
    a = next((e for e in after.elements if (e.window, e.role, e.label) == key), None)
    if b is None or a is None:
        return False
    if pending.get("kind") == "scroll":
        return b.offscreen and not a.offscreen  # scrolled the control it was heading for into view
    if pending.get("kind") == "type":
        return bool(a.value) and (a.value or "") != (b.value or "")
    return (
        (a.value or "") != (b.value or "")
        or a.toggle_state() != b.toggle_state()
        or a.selected != b.selected
    )


class SurfaceFailed(RuntimeError):
    """Three reads in a row failed on every surface the run could use."""


def is_irreversible(el: Element) -> bool:
    # A page control is judged by its own name. Its context (the card or row it
    # sits in) names the record, not the action: "Save PO" in a card headed "New
    # purchase order" read as a purchase. A control with no name of its own is
    # judged by what is around it, as before.
    name = getattr(el, "raw_label", "") or el.label
    return el.role in GATED_ROLES and bool(_IRREVERSIBLE.search(name))


def is_open(el: Element) -> bool:
    """A control that shows its panel, list or menu open (aria-expanded, or an
    open disclosure triangle)."""
    return getattr(el, "expanded", None) is True or el.toggle_state() == "expanded"


# A sub-goal or goal that asks for a panel to be shut lets the control that
# opened it be clicked again.
_CLOSE_WORDS = re.compile(r"\b(close|collapse|hide|fold)\b", re.I)


def installed_apps() -> list[str]:
    names: set[str] = set()
    for d in APP_DIRS:
        try:
            for entry in os.listdir(d):
                if entry.endswith(".app"):
                    names.add(entry[:-4])
        except OSError:
            continue
    return sorted(names)


def launch_candidates_from_goal(goal: str, apps: list[str]) -> list[str]:
    low = goal.lower()
    out = [
        a
        for a in apps
        if len(a) >= 3 and re.search(r"(?<![a-z0-9])" + re.escape(a.lower()) + r"(?![a-z0-9])", low)
    ]
    return out[:12]


@dataclass
class OperatorConfig:
    goal: str
    app: str | None = None
    max_steps: int = 40
    allow_irreversible: bool = False
    # "auto": the page DOM for Arc/Chrome when page JavaScript is allowed, else AX.
    surface: str = "auto"
    use_llm: bool = True
    verify_with_llm: bool = True
    thresholds: Thresholds = field(default_factory=Thresholds)
    dry_run: bool = False
    jev_model: str | None = None
    settle_ms: int = 350
    ax_depth: int = 25
    launch_if_missing: bool = True
    # One accessibility read may not take longer than this. The native read
    # has its own budget (8 s), so this only fires when the native call hangs.
    ax_read_timeout_s: float = 30.0
    # Give up after this many reads in a row that timed out or failed.
    ax_max_consecutive_failures: int = 3
    # Wall-clock ceiling for the whole run. The tool is foreground, so the
    # parent turn waits for it: an unbounded run held a session for 2.9 h.
    max_runtime_s: float = 900.0
    # Workspace whose skills may hold `jev-app-<slug>` notes; None skips the lookup.
    notes_workspace: str | None = None
    # Typed-literal candidates beyond the goal's own quoted spans (for-each items).
    extra_literals: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    status: str
    summary: str
    steps: int
    jev_calls: int
    jev_ms: list[int]
    llm_calls: int
    llm_ms: int
    llm_tokens: tuple[int, int]
    elapsed_s: float
    history: list[dict[str, Any]]
    run_id: str
    log_path: str
    pending_action: str | None = None
    final_screen_text: str = ""
    surface: str = Observation.SURFACE
    final_table: str = ""
    read_ms: list[int] = field(default_factory=list)
    last_frame_path: str | None = None
    # Why a blocked run stopped, when the tool can tell: "ax_unreadable".
    code: str = ""
    # The page a DOM run ended on ("title — url"), so the caller can continue there.
    page: str = ""
    # The values the goal asked to read, one per line, as the end-state check
    # found them on the screens the run saw ("" when it asked for none).
    report: str = ""

    def footer(self) -> str:
        med = int(statistics.median(self.jev_ms)) if self.jev_ms else 0
        # actions: what the run did on the screen (the address it opened
        # included); decisions: Jev calls, one per step, some of which choose to
        # ask for help; llm_calls: planner, end-state and field-text calls.
        return (
            f"[jev_computer_use] status={self.status} surface={self.surface} actions={self.steps} "
            f"decisions={self.jev_calls} "
            f"jev_median_ms={med} llm_calls={self.llm_calls} llm_ms={self.llm_ms} "
            f"elapsed={self.elapsed_s:.1f}s log={self.log_path}"
        )

    def next_hint(self) -> str:
        if self.status == "partial":
            return (
                "the run read everything the goal let it reach; the summary names what the pages "
                "do not show. Running again will not find it: ask the person or the site"
            )
        if self.status == "needs_confirmation":
            return (
                f"confirm with the person, then re-run with allow_irreversible=true "
                f"(pending: {self.pending_action})"
            )
        if self.status == "budget_exhausted":
            return "narrow the goal or continue from the last state"
        if self.status == "blocked" and self.code == "ax_unreadable":
            return (
                "try computer_use (screenshot-based) or do it without the UI "
                "via bash/osascript/open"
            )
        if self.surface.startswith("dom"):
            # A web page: scripting it through osascript skips every check a run
            # makes (irreversible controls, page dialogs, what changed).
            frame = re.search(r"\[embedded frame from ([^,\]]+)", self.final_screen_text or "")
            if frame and self.status != "done":
                return (
                    f"part of this page is an embedded frame from {frame.group(1)}, which a page "
                    "run can neither read nor click; do that part with computer_use "
                    "(screenshots), then continue with jev on the rest; do not script the page "
                    "through osascript"
                )
            if self.status == "blocked":
                return (
                    "read the summary for what stopped it; if a dialog or login is in the way ask "
                    "the person, otherwise re-run on the step that failed with a narrower goal, or "
                    "use computer_use for that step; do not script the page through osascript"
                )
            return "retry once with a narrower goal; if it fails again use computer_use for that step"
        if self.status == "blocked":
            return (
                "read the summary for what stopped it; if a dialog or login is in the way ask the "
                "person, otherwise try computer_use or do it without the UI via bash/osascript/open"
            )
        return "retry once with a narrower goal; if it fails again use computer_use or bash/osascript/open"

    def handoff(self) -> str:
        """What the caller needs to take over after a run that did not finish
        cleanly; empty for statuses that carry no handoff."""
        if self.status not in HANDOFF_STATUSES:
            return ""
        rows = self.final_table.split("\n") if self.final_table else []
        lines = ["[handoff]", f"surface: {self.surface}"]
        if rows:
            more = f" (first {HANDOFF_ROWS} of {len(rows)})" if len(rows) > HANDOFF_ROWS else ""
            lines.append(f"elements{more}:")
            lines.extend(rows[:HANDOFF_ROWS])
        else:
            lines.append("elements: none exposed")
        screen = " | ".join(self.final_screen_text.split("\n"))[:HANDOFF_SCREEN_CHARS]
        lines.append(f"screen text: {screen}" if screen else "screen text: (none)")
        if self.last_frame_path:
            lines.append(f"last frame: {self.last_frame_path}")
        if self.read_ms:
            lines.append(
                f"read_ms: median={int(statistics.median(self.read_ms))} max={max(self.read_ms)}"
            )
        lines.append(f"next: {self.next_hint()}")
        return "\n".join(lines)


@dataclass
class Target:
    pid: int
    bundle: str
    name: str


StepCb = Callable[[str], Awaitable[None] | None]


def goal_span(goal: str, after: Any, until: Any) -> str | None:
    """The goal's own text between two short quotes from it: what the planner
    names instead of copying. `after` is the words right before the span,
    `until` the words right after (None for the goal's end). Leading and
    trailing blank lines go; the text itself is typed as written."""
    if not isinstance(after, str) or not after.strip():
        return None
    start = goal.find(after.strip())
    if start < 0:
        return None
    start += len(after.strip())
    end = len(goal)
    if isinstance(until, str) and until.strip():
        end = goal.find(until.strip(), start)
        if end < 0:
            return None
    text = goal[start:end].strip("\n").strip(" \t")
    return text or None


class Operator:
    def __init__(
        self,
        config: OperatorConfig,
        *,
        provider: DecisionProvider,
        spec: ComputerToolSpec,
        native: Any,
        llm: LLMHelper | None = None,
        on_step: StepCb | None = None,
        apps: list[str] | None = None,
        surface: Surface | None = None,
    ) -> None:
        self.cfg = config
        self.provider = provider
        self.spec = spec
        self.native = native
        self.llm = llm if config.use_llm else None
        self.on_step = on_step
        self.apps = apps if apps is not None else installed_apps()
        self.actuator = Actuator(spec, native, settle_ms=config.settle_ms)
        self._surface_injected = surface is not None
        self.surface: Surface = surface or self._make_ax()
        # Why a browser's page did not answer its JavaScript probe (the run stops).
        self._page_unreadable = ""
        self.history: list[dict[str, Any]] = []
        self.jev_ms: list[int] = []
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 10000:04d}"
        self.log_path = RUN_DIR / f"{self.run_id}.jsonl"
        self._subgoal: str | None = None
        self._subgoal_steps = 0
        self._subgoal_at = 0  # len(self.history) when the planner set the sub-goal
        # Values the goal states without quotes ("first name Ada"), as the text
        # door read them out of it: typing options for the fields after.
        self._goal_values: list[str] = []
        self._replans_since_progress = 0
        # (window, role, label, text) of the last step when it appended text to a
        # text area; any other step clears it.
        self._last_append: tuple[str, str, str, str] | None = None
        self._consecutive_waits = 0
        self._settle_next = False  # after a scroll, wait for the animation to end before acting
        self._clicked_toggle: tuple[str, str, str, str | None] | None = None
        # (window, role, label) of a control the last action clicked while it was
        # closed: if it shows open now, that click opened it.
        self._opened_by_last: tuple[str | None, str, str] | None = None
        self._seen_windows: set[str] = set()
        self._start_windows: list[str] | None = None
        self._window_checks: set[str] = set()
        self._shot_geometry: tuple[tuple[float, ...], int, int] | None = None
        self.read_ms: list[int] = []
        # What the end-state check reports from (see SEEN_MAX_CHARS): each distinct
        # screen the run read, as (page, lines). A line kept once for the whole run
        # lost the record it was under: a pet room's "Room Only Flexible 411" was
        # the Deluxe room's line too, so the check said the pet room's plans were
        # never shown.
        self._screens: list[tuple[str, list[str]]] = []
        # Every (page, line) the run has read, uncapped (only for telling new text
        # from text already read), and how many the reads since the last action
        # added: a page scroll that adds none only re-read the page. Summed over
        # every read after the action: the re-reads that wait for a scroll to
        # settle found nothing new, and a list whose every scroll brought rows
        # was taken for one that scrolled over text it had read.
        self._known: set[tuple[str, str]] = set()
        self._last_new_lines = 0
        self._idle_scrolls = 0
        # End-state checks that disagreed, counted again from 1 after progress.
        self._verify_rounds = 0
        # (scroll operation, screen fingerprint) of a scroll that moved nothing:
        # the same scroll on the same screen is not tried again.
        self._scroll_edge: tuple[str, str] | None = None
        # The controls a sub-goal names by index, as (role, label) on the screen
        # the planner read: indices are renumbered when the page changes.
        self._subgoal_refs: dict[int, tuple[str, str]] = {}
        self._planner_notes: list[str] = []
        self._start_screen = ""  # the first screen with text, for the planner
        self._last_obs: Observation | None = None
        self._last_frame: tuple[bytes, str] | None = None
        self._target: Target | None = None
        # Window captures after the last actions taken from a screenshot, for
        # telling a change from a toggle that undid itself.
        self._pixel_frames: list[bytes] = []
        self._finish_logged = False
        self._notes = ""
        self._app_notes = ""
        self._site_notes: dict[str, str] = {}  # host -> notes, looked up once per host
        self._progress_since_verify = False  # a meaningful action since the last end-state check
        # (action, screen fingerprint after it), newest last
        self._recent: list[tuple[str, str]] = []
        self._repeat_key: str | None = None
        self._repeat_count = 0
        self._repeat_replanned = False
        self.item: int | None = None  # for-each index; stamped on every log row

    def share_with(self, prior: "Operator", item: int | None) -> None:
        """Continue a for-each run: reuse the prior operator's surface (and its
        pinned tab), actuator, run id and log. Everything else stays fresh."""
        self.actuator = prior.actuator
        self.surface = prior.surface
        self._surface_injected = True
        self.run_id, self.log_path, self.item = prior.run_id, prior.log_path, item
        if hasattr(self.surface, "_log"):
            self.surface._log = self._log

    def _make_ax(self) -> AXSurface:
        return AXSurface(
            self.native,
            self.actuator,
            ax_depth=self.cfg.ax_depth,
            read_timeout_s=self.cfg.ax_read_timeout_s,
            log=self._log,
        )

    def _make_dom(self, bundle: str) -> DOMSurface:
        return DOMSurface(bundle=bundle, actuator=self.actuator, log=self._log)

    async def _select_surface(self, target: Target) -> None:
        """Pick the surface for this run. auto/dom use the page DOM only for a
        supported browser that answers a trivial JavaScript probe."""
        if self._surface_injected or self.cfg.surface == "ax":
            return
        if not dom_surface.supported(target.bundle):
            if self.cfg.surface == "dom":
                self._log({"event": "surface_probe", "ok": False, "detail": "unsupported browser", "bundle": target.bundle})
            return
        ok, detail = await asyncio.to_thread(dom_surface.probe, target.bundle)
        for attempt in range(PROBE_RETRIES):
            if ok or "did not answer" not in detail:
                break
            # A busy browser can miss the first 3 s, and a heavy page that is still
            # loading (a flight results page) can take ~20 s: wait before giving up.
            if attempt:
                await asyncio.sleep(2.0)
            ok, again = await asyncio.to_thread(
                dom_surface.probe, target.bundle, None, dom_surface.PROBE_RETRY_TIMEOUT_S
            )
            detail = f"{detail}; retry: {again}"
        self._log({"event": "surface_probe", "ok": ok, "detail": detail, "bundle": target.bundle})
        if ok:
            self.surface = self._make_dom(target.bundle)
            self.surface.may_confirm = self.cfg.allow_irreversible
            await self.surface.pin_active()
        else:
            # No fallback to the accessibility tree: on a browser it acts on the
            # front window, whose tabs may be anything the person has open (a run
            # whose test page had not loaded switched the person's own tabs).
            self._page_unreadable = detail

    async def _foreign_tab(self) -> str | None:
        """Why a run whose goal gives no address may not work in its pinned tab,
        or None. It works only in a tab a run has opened or worked in: the tab
        in front can be anything the person has open (a test run once acted in
        a live video call that had come to the front)."""
        s = self.surface
        if getattr(s, "_injected_js", False):
            return None  # a test page, not a browser
        if dom_surface.run_tab(s.bundle, s.tab_id):
            return None
        where = await s.describe_tab()
        return (
            f"The browser's front tab is {where}, which no run has opened or worked in. A run "
            "whose goal gives no address works only in a tab an earlier run opened or used, so it "
            "never acts on whatever the person has in front. Put the page's address in the goal "
            '("Open <url> and \u2026") to work on it.'
        )

    def _swap_if_dom_failing(self) -> None:
        """Two DOM failures in a row (timeout or error, not a stale element) end
        the run: the accessibility tree would act on the browser's front window,
        which may no longer be the run's tab."""
        s = self.surface
        if s.name == "dom" and getattr(s, "fail_streak", 0) >= 2:
            self._log({"event": "surface_failed", "surface": "dom", "reason": "dom failed twice in a row"})
            raise SurfaceFailed("the page stopped answering its JavaScript twice in a row")

    def _surface_label(self) -> str:
        return self.surface.name

    @property
    def _ax_fail_streak(self) -> int:
        return getattr(self.surface, "ax_fail_streak", 0)

    async def _say(self, text: str) -> None:
        if self.on_step is None:
            return
        # One event per line: the run's pane parses the log line by line.
        r = self.on_step(text.replace("\n", " "))
        if asyncio.iscoroutine(r):
            await r

    def _log(self, row: dict[str, Any]) -> None:
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                # `t` is the time of the log row (epoch seconds) and `elapsed_s`
                # the run seconds when it was written; an action's own run
                # seconds are `at_s`. Rows spread an action into themselves, and
                # its old `t` once overwrote the row's time.
                start = getattr(self, "_t_start", None)
                stamp = {"t": time.time()}
                if start is not None:
                    stamp["elapsed_s"] = round(time.perf_counter() - start, 3)
                f.write(
                    json.dumps(
                        {"run_id": self.run_id, "surface": self.surface.name,
                         **({"item": self.item} if self.item is not None else {}), **row, **stamp},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass

    # ─── target resolution ───────────────────────────────────────────

    def _windows(self) -> list[Any]:
        try:
            return [w for w in self.native.list_windows() if w.bundle != SELF_BUNDLE]
        except Exception:  # noqa: BLE001
            return []

    def _proc_name(self, pid: int) -> str:
        try:
            return (
                subprocess.check_output(["ps", "-p", str(pid), "-o", "comm="], text=True)
                .strip()
                .split("/")[-1]
            )
        except Exception:  # noqa: BLE001
            return str(pid)

    def _match_target(self, app: str, windows: list[Any]) -> Target | None:
        low = app.lower()
        for w in windows:
            if w.bundle.lower() == low:
                return Target(w.pid, w.bundle, self._proc_name(w.pid))
        for w in windows:
            name = self._proc_name(w.pid)
            if name.lower() == low or low == w.bundle.lower().split(".")[-1]:
                return Target(w.pid, w.bundle, name)
        for w in windows:
            name = self._proc_name(w.pid)
            if low in name.lower() or low in w.bundle.lower():
                return Target(w.pid, w.bundle, name)
        return None

    async def _resolve_target(self, app: str | None) -> Target | None:
        windows = self._windows()
        if app:
            t = self._match_target(app, windows)
            if t is None and self.cfg.launch_if_missing and not self.cfg.dry_run:
                await self._say(f"launching {app}")
                rec = await self.surface.execute("launch", app)
                if rec.ok:
                    for _ in range(10):
                        await asyncio.sleep(0.4)
                        t = self._match_target(app, self._windows())
                        if t:
                            break
            return t
        # Only the app that is actually frontmost; never guess the next window in
        # z-order, which is some unrelated app whenever Freyja itself has focus.
        front_pid = await asyncio.to_thread(self.actuator.frontmost)
        front = next((w for w in windows if w.pid == front_pid), None)
        if front is None:
            return None
        return Target(front.pid, front.bundle, self._proc_name(front.pid))

    def _refusal_reason(self, exc: Refused) -> str:
        if self._screen_locked():
            return "the screen locked during the run, so input would go to the lock screen"
        return str(exc)

    def _target_window(self, target: Target, title: str) -> Any | None:
        wins = [w for w in self._windows() if w.pid == target.pid]
        return next((w for w in wins if w.title == title), None) or (wins[0] if wins else None)

    # ─── observation ────────────────────────────────────────────────

    def _closes_unnamed_window(self, el: Element) -> str | None:
        """The window a close/minimize/zoom button belongs to, when the goal names a
        different window seen during this run."""
        if el.subrole not in WINDOW_CHROME or not el.window:
            return None
        goal = self.cfg.goal.lower()
        named = [w for w in self._seen_windows if len(w) >= 3 and w.lower() in goal]
        if not named or el.window.lower() in goal:
            return None
        return el.window

    async def _await_toggle(self, target: Target, obs: Observation) -> Observation:
        """System Settings flipped an Automation switch a moment before its AXValue
        changed; on "no visible change" Jev clicked again and switched it back. Wait
        up to 2 s for the clicked control's value to move before deciding again."""
        window, role, label, old = self._clicked_toggle or ("", "", "", None)
        self._clicked_toggle = None
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            el = next((e for e in obs.elements if (e.window, e.role, e.label) == (window, role, label)), None)
            if el is None or el.value != old:
                return obs
            await asyncio.sleep(0.15)
            obs = await self._observe(target)
        return obs

    async def _observe_settled(self, target: Target, obs: Observation) -> Observation:
        """Re-read until element positions stop moving (smooth scrolling keeps
        animating after the scroll event), for at most 1.5 s."""
        deadline = time.perf_counter() + 1.5
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.15)
            nxt = await self._observe(target)
            if [e.fingerprint() for e in nxt.elements] == [e.fingerprint() for e in obs.elements]:
                return nxt
            obs = nxt
        return obs

    async def _observe(self, target: Target) -> Observation:
        obs = None
        last = ""
        for _ in range(3):
            self._swap_if_dom_failing()
            try:
                obs = await self.surface.observe(target)
                break
            except TabGone as exc:
                # The person closed the run's tab: stop rather than read the whole
                # browser through the accessibility tree.
                self._log({"event": "dom_error", "error": str(exc)})
                raise SurfaceFailed(str(exc)) from exc
            except DOMUnavailable as exc:
                last = str(exc)
                self._log({"event": "dom_error", "error": last})
        if obs is None:
            raise SurfaceFailed(last or "the page surface failed repeatedly")
        read_ms = obs.read_ms
        self.read_ms.append(read_ms)
        self._last_obs = obs
        self._last_new_lines += self._remember_text(obs.full_text, obs.focused_window)
        self.actuator.window_frames = obs.window_frames
        self._seen_windows.update(w for w in obs.windows if w != "(untitled window)")
        if self._start_windows is None:
            self._start_windows = list(obs.windows)
        if not self._start_screen and obs.screen_text.strip():
            self._start_screen = obs.screen_text
        return obs

    def _window_bounds(
        self, target: Target, title: str
    ) -> tuple[float, float, float, float] | None:
        for w in self._windows():
            if w.pid == target.pid and (w.title == title or not title):
                return (w.bounds.x, w.bounds.y, w.bounds.w, w.bounds.h)
        for w in self._windows():
            if w.pid == target.pid:
                return (w.bounds.x, w.bounds.y, w.bounds.w, w.bounds.h)
        return None

    def _screen_locked(self) -> bool:
        try:
            out = subprocess.check_output(
                ["ioreg", "-n", "Root", "-d1", "-a"], text=True, timeout=5
            )
            m = re.search(r"CGSSessionScreenIsLocked</key>\s*<(true|false)/>", out)
            return bool(m and m.group(1) == "true")
        except Exception:  # noqa: BLE001
            return False

    # ─── main loop ───────────────────────────────────────────────────

    async def run(self) -> RunResult:
        self._t_start = time.perf_counter()
        try:
            return await self._run()
        except SurfaceFailed as exc:
            # Raised from a re-read outside the main loop's own handler.
            last = self._last_obs
            return self._finish(
                "blocked",
                f"Stopped: {exc}. " + (self._template_summary(last) if last else ""),
                code="surface_failed",
            )
        except BaseException as exc:  # a cancelled or crashed run still leaves a finish row
            cancelled = isinstance(exc, asyncio.CancelledError)
            self._log_finish(
                "cancelled" if cancelled else "error",
                "The run task was cancelled before it finished."
                if cancelled
                else f"{type(exc).__name__}: {exc}",
            )
            raise

    async def _refresh_site_notes(self) -> None:
        """On a page, add the `jev-site-<host>` notes for the host it is on."""
        if self.surface.name != "dom" or not self.cfg.notes_workspace:
            return
        host = (urlparse(getattr(self.surface, "url", "") or "").hostname or "").lower()
        if not host or host in self._site_notes:
            return
        try:
            got = await asyncio.wait_for(
                asyncio.to_thread(site_notes, host, workspace=self.cfg.notes_workspace), 3.0
            )
        except Exception:  # noqa: BLE001
            got = ""
        self._site_notes[host] = got
        if got:
            self._log({"event": "notes", "site": host, "chars": len(got)})
        self._notes = "\n\n".join(x for x in [self._app_notes, *self._site_notes.values()] if x)

    async def _load_notes(self, target: Target) -> str:
        if not self.cfg.notes_workspace:
            return ""
        names = [target.bundle, target.name]
        for name in names:
            try:
                got = await asyncio.wait_for(
                    asyncio.to_thread(app_notes, name, workspace=self.cfg.notes_workspace), 3.0
                )
            except Exception:  # noqa: BLE001
                got = ""
            if got:
                return got
        return ""

    async def _run(self) -> RunResult:
        cfg = self.cfg
        finish = self._finish
        literals = list(dict.fromkeys([*cfg.extra_literals, *literals_from_goal(cfg.goal)]))
        launch_cands = launch_candidates_from_goal(cfg.goal, self.apps)

        self._log(
            {
                "event": "start",
                "goal": cfg.goal,
                "app": cfg.app,
                "config": {
                    "max_steps": cfg.max_steps,
                    "allow_irreversible": cfg.allow_irreversible,
                    "use_llm": cfg.use_llm,
                    "dry_run": cfg.dry_run,
                },
            }
        )

        target = await self._resolve_target(cfg.app)
        self._target = target
        if target is None:
            if self._screen_locked():
                return finish(
                    "blocked",
                    "The screen is locked; macOS hides every window from the accessibility API until it is unlocked.",
                )
            return finish(
                "blocked",
                f"Could not find a window for {cfg.app!r}."
                if cfg.app
                else "No app was given and the frontmost app is not one the operator can drive "
                "(Freyja itself, or an app with no windows). Pass `app`.",
            )
        self.actuator.set_target(target.pid, target.bundle, target.name)
        await self._select_surface(target)
        if self._page_unreadable:
            return finish(
                "blocked",
                f"The page in {target.name} did not answer its JavaScript ({self._page_unreadable}). "
                "On a browser the accessibility route acts on whatever window is in front, so the "
                "run stops instead. Check that the page has loaded (an error page runs no "
                'JavaScript), or pass surface="ax" to use the accessibility route anyway.',
                code="page_unreadable",
            )
        browser = dom_surface.supported(target.bundle)
        urls = urls_from_goal(cfg.goal) if browser else []
        if self.surface.name == "dom" and not urls and not cfg.dry_run:
            foreign = await self._foreign_tab()
            if foreign:
                return finish("blocked", foreign, code="foreign_tab")
        self._app_notes = await self._load_notes(target)
        self._notes = self._app_notes
        # The DOM surface drives the page through JavaScript; the browser can stay
        # behind whatever the person is working in.
        if not cfg.dry_run and self.surface.name != "dom":
            await self.actuator.focus(target.bundle)
        await self._say(f"target: {target.name} ({target.bundle}, pid {target.pid})")
        if urls and not cfg.dry_run:
            if urls[0] in fallback_urls(cfg.goal) and await self._continues_here(urls[0]):
                # Opening it anyway started a continuation over from the site's
                # entry page in a new tab, with the results tab right there.
                self._log(
                    {"event": "open_url", "url": urls[0], "skipped": "the goal gives it as a "
                     "fallback, and the run continues in the tab a run already used"}
                )
            else:
                await self._open_goal_url(urls[0])

        before: Observation | None = None
        pending: dict[str, Any] | None = None
        stuck_streak = 0
        step = 0

        while step < cfg.max_steps:
            if self.spec.cancel_event.is_set():
                return finish("cancelled", "Cancelled by emergency stop.")

            elapsed = time.perf_counter() - self._t_start
            if elapsed > cfg.max_runtime_s:
                last = self._last_obs
                summary, report, met = await self._closing(
                    last,
                    f"Stopped after {elapsed:.0f}s (limit {cfg.max_runtime_s:.0f}s) "
                    f"and {len(self.history)} steps without reaching done.",
                )
                return finish(
                    "done" if met else "budget_exhausted",
                    summary,
                    screen=last.screen_text if last else "",
                    report=report,
                )

            try:
                obs = await self._observe(target)
            except SurfaceFailed as exc:
                return finish(
                    "blocked",
                    f"The app could not be read three times in a row ({exc}). Try again, "
                    "or use `computer_use` with screenshots.",
                    code="surface_failed",
                )
            await self._refresh_site_notes()
            late = getattr(self.surface, "declined_confirm", None)
            if late and not cfg.allow_irreversible:
                # The page asked to confirm after the last action returned.
                self.surface.declined_confirm = None
                return finish(
                    "needs_confirmation",
                    f"The page asked to confirm: {late!r}. The operator answered Cancel, so nothing "
                    "was confirmed. Re-run with allow_irreversible=true to answer OK.",
                    pending=f"confirm {late!r}",
                    screen=obs.screen_text,
                )
            if self._ax_fail_streak >= cfg.ax_max_consecutive_failures:
                return finish(
                    "blocked",
                    f"{target.name} did not answer {self._ax_fail_streak} accessibility "
                    "reads in a row (timed out or failed), so the operator cannot see "
                    "its screen. The app may be busy or hung; try again later or use "
                    "`computer_use` with screenshots.",
                    code="ax_unreadable",
                )
            if self._settle_next:
                obs = await self._observe_settled(target, obs)
                self._settle_next = False
            if self._clicked_toggle:
                obs = await self._await_toggle(target, obs)
            win = self._target_window(target, obs.focused_window)
            self.actuator.window_id = win.id if win else None

            if not obs.windows and not obs.elements:
                if self._screen_locked():
                    return finish(
                        "blocked",
                        "The screen is locked; no windows are visible to the accessibility API.",
                    )

            if pending is not None and before is not None:
                changed, diff = diff_observations(before, obs)
                # `changed` (any fingerprint movement) is for the log and the history;
                # stuck detection uses only changes that show the action did something:
                # the screen moved, or the action's own target took its change.
                target_key = pending.pop("target_key", None)
                meaningful = meaningful_change(before, obs) or own_effect(
                    pending, target_key, before, obs
                )
                # The same action putting the screen back where it was two actions
                # ago is a toggle undoing itself (open, close, open a panel), not
                # progress: a run once flipped a travelers panel eight times.
                fp = obs.fingerprint()
                act_key = f"{pending.get('kind')}: {pending.get('action')}"
                if meaningful and len(self._recent) >= 2 and self._recent[-2] == (act_key, fp):
                    meaningful = False
                self._recent.append((act_key, fp))
                del self._recent[:-4]
                entry = {**pending, "changed": changed, "diff": diff}
                self.history.append(entry)
                kind = pending.get("kind")
                page_scroll = kind in ("scroll_down", "scroll_up")
                if page_scroll:
                    # A scroll that moved the view is not stuck even when nothing
                    # meaningful came into it; one that changed nothing at all is
                    # (the page was at its end, or the wheel hit something that
                    # does not scroll). 29 such scrolls once ran unchecked.
                    stuck_streak = 0 if changed else stuck_streak + 1
                    self._track_repeat(pending, changed)
                    if not changed:
                        # At the top or the bottom: the same scroll on this same
                        # screen does nothing again (it was tried three times in
                        # a row, three times in one run).
                        self._scroll_edge = (str(kind), obs.fingerprint())
                    self._idle_scrolls = 0 if self._last_new_lines else self._idle_scrolls + 1
                elif kind != "wait":
                    stuck_streak = 0 if meaningful else stuck_streak + 1
                    self._track_repeat(pending, meaningful)
                    self._idle_scrolls = 0
                if meaningful:
                    self._replans_since_progress = 0
                # Between two end-state checks, a page scroll counts as progress
                # only when it brought text the run had not read. Scrolling back
                # over a page already read moves the screen but learns nothing,
                # and it had started the count of rejected checks again: six
                # checks once followed such scrolls. The stuck rule keeps
                # counting any scroll that moved the screen: with it, a run that
                # scrolled a policy it had already read, then went back to its
                # list, ended stuck after three replans (desk/expenses).
                if meaningful and kind != "wait" and (not page_scroll or self._last_new_lines > 0):
                    self._progress_since_verify = True
                await self._say(f"  → {'changed' if changed else 'no change'}: {diff}")
                # new_lines: text the run had not read before, so a reader of the log
                # can tell steps that learned something from steps that re-read.
                self._log(
                    {"event": "outcome", "step": step, **entry, "meaningful": meaningful,
                     "new_lines": self._last_new_lines}
                )
                pending = None
                if self._subgoal:
                    self._subgoal_steps += 1
                    if self._subgoal_steps >= 6:
                        self._subgoal = None

            if self._repeat_count >= MAX_REPEATS:
                action = self._repeat_key
                self._repeat_count = 0
                stuck_streak = 0
                if self._repeat_replanned:
                    return self._finish_blocked(
                        obs, f"{action} repeated {MAX_REPEATS} more times with no effect after a replan"
                    )
                self._repeat_replanned = True
                self._subgoal = None  # a repeating action means the plan behind it is stale
                out = await self._replan(
                    obs,
                    target,
                    reason=f"this action repeated with no effect: {action} "
                    f"({MAX_REPEATS} times in a row)",
                )
                if out is not None:
                    return out
                continue

            if stuck_streak >= 3:
                stuck_streak = 0
                out = await self._replan(
                    obs, target, reason="three consecutive actions changed nothing on screen"
                )
                if out is not None:
                    return out
                continue

            if self._idle_scrolls >= MAX_IDLE_SCROLLS:
                self._idle_scrolls = 0
                out = await self._replan(
                    obs,
                    target,
                    reason=f"{MAX_IDLE_SCROLLS} scrolls in a row brought no text the run had not "
                    "already read (the page's whole text is read at every step, so scrolling "
                    "does not reveal more of it)",
                )
                if out is not None:
                    return out
                continue

            if not obs.elements and not obs.full_text.strip() and self.llm is not None:
                # No controls and no text: Jev would answer need_help at low
                # confidence and the planner's screenshot is the only reading
                # of this screen, so the planner is asked at once.
                step += 1
                out = await self._replan(obs, target, reason="the app exposes no controls and no text")
                if out is not None:
                    return out
                continue

            step += 1
            try:
                d = await decide(
                    self.provider,
                    obs,
                    goal=cfg.goal,
                    subgoal=self._bound_subgoal(obs),
                    history=self.history,
                    # The planner's own quoted text ("\\nBread 1", with its line break)
                    # is a typing option too; the goal's literals alone lost the break.
                    # So is a value it worked out and wrote without quotes ("Type
                    # 86.00 into Amount [16]"), which is in no goal.
                    literals=list(
                        dict.fromkeys(
                            [
                                *literals,
                                *self._goal_values,
                                *quoted_literals(self._subgoal or ""),
                                *typed_values(self._subgoal or ""),
                            ]
                        )
                    ),
                    launch_candidates=launch_cands,
                    thresholds=cfg.thresholds,
                    model=cfg.jev_model,
                    notes=self._notes,
                    urls=urls or None,
                    key_options=getattr(self.surface, "key_options", None),
                    subgoal_actions=[
                        str(h.get("action") or "") for h in self.history[self._subgoal_at :]
                    ],
                )
            except DecisionError as exc:
                return finish("error", f"Decision provider failed: {exc}")
            self.jev_ms.append(d.latency_ms)
            self._log(
                {
                    "event": "decision",
                    "step": step,
                    "state_fp": obs.fingerprint(),
                    "elements": len(obs.elements),
                    "node_count": obs.node_count,
                    "read_ms": obs.read_ms,
                    "answers": d.answers.compact(),
                    # The whole operation distribution: a low top choice is often a
                    # split between two operations, which the choice alone hides.
                    "op_probs": _top(d.answers.choice("operation").probabilities, 5),
                    "decision": describe(d),
                    "op_conf": round(d.op_confidence, 3),
                    "target_conf": round(d.target_confidence, 3),
                    "reasons": d.reasons,
                    "subgoal": self._subgoal,
                    "table": obs.table(),
                    "screen_text": obs.screen_text,
                }
            )
            await self._say(
                f"step {step}: {describe(d)} (op {d.op_confidence:.2f}, target {d.target_confidence:.2f}, "
                f"jev {d.latency_ms} ms, read {obs.read_ms} ms, {len(obs.elements)} rows)"
                + (f" [{'; '.join(d.reasons)}]" if d.reasons else "")
            )

            if self._subgoal and d.subgoal_done_p is not None and d.subgoal_done_p >= 0.7:
                await self._say(
                    f"  sub-goal complete (p={d.subgoal_done_p:.2f}); back to the main goal"
                )
                self._subgoal = None
                if d.operation == "done":
                    # Asked to work on a sub-goal first, Jev's "done" means the
                    # sub-goal: decide again against the goal itself rather than
                    # spend an end-state check on a half-filled form (one passenger
                    # form drew three such checks and ended blocked).
                    continue

            if cfg.dry_run:
                self.history.append(
                    {
                        "step": step,
                        "kind": d.operation,
                        "action": describe(d),
                        "changed": None,
                        "diff": "dry run",
                    }
                )
                return finish("dry_run", f"Would {describe(d)}.", screen=obs.screen_text)

            if d.operation == "done":
                out = await self._check_done(obs, target, by="Jev")
                if out is not None:
                    return out
                continue

            if d.operation in ("need_help", "blocked"):
                reason = d.operation + (": " + "; ".join(d.reasons) if d.reasons else "")
                if d.dialog_p >= 0.7 and obs.dialog:
                    reason += f"; dialog present: {obs.dialog.get('title')!r} {obs.dialog.get('text', '')[:200]!r}"
                out = await self._replan(obs, target, reason=reason)
                if out is not None:
                    return out
                continue

            if d.operation == "wait":
                self._consecutive_waits += 1
                if self._consecutive_waits > 3:
                    out = await self._replan(
                        obs,
                        target,
                        reason=f"Jev chose wait {self._consecutive_waits} times in a row, under a "
                        "second each; the page may still be loading, or the goal waits for "
                        "something that has not happened yet",
                    )
                    if out is not None:
                        return out
                    continue
                await asyncio.sleep(0.6)
                before, pending = obs, {"step": step, "kind": "wait", "action": "wait"}
                self._last_new_lines = 0
                continue
            self._consecutive_waits = 0

            action_desc = describe(d)
            kind = d.operation
            prev_append, self._last_append = self._last_append, None
            try:
                if d.operation == "click" and d.target is not None and self._reshuts(d.target):
                    # The last click opened this control's panel, and nothing was done
                    # in it: a second click shuts it again. On a token form Jev opened
                    # "Add permissions", then clicked it again under the same sub-goal,
                    # twice, and the run stopped with no permission added.
                    refusal = (
                        f"not clicking [{d.target.index}] {d.target.label!r} again: the last "
                        "click opened it, and a second click would close what it opened"
                    )
                    await self._say(f"  {refusal}")
                    self.history.append(
                        {"step": step, "kind": "refused", "action": describe(d), "ok": False,
                         "changed": False, "error": refusal}
                    )
                    self._log({"event": "outcome", "step": step, **self.history[-1]})
                    if self._subgoal and _PLANNER_CLICK.match(self._subgoal):
                        # The sub-goal's click is made; decide against the goal.
                        self._subgoal = None
                    before, pending = obs, None
                    stuck_streak += 1
                    continue
                elif d.target is not None and d.target.offscreen and d.operation in ("click", "double_click", "type"):
                    # Scrolled out of view: this step scrolls toward the control, and Jev
                    # picks it again once it is in view (gates apply then).
                    plan = obs.scroll_toward(d.target)
                    rec = await self.surface.execute(
                        "scroll", plan.point, down=(plan.way == "down"), way=plan.way, area=plan.area
                    )
                    action_desc = f"scroll {plan.way} toward [{d.target.index}] {d.target.label!r}"
                    kind = "scroll"
                    self._settle_next = True
                elif d.operation in ("click", "double_click") and d.target is not None:
                    other = self._closes_unnamed_window(d.target)
                    if other:
                        # Twice tonight Jev closed the remaining, different document once
                        # the named one was gone; ask whether the goal is already met.
                        check = None
                        if other not in self._window_checks:
                            self._window_checks.add(other)
                            check = await self._verify(obs)
                        if check and check.get("satisfied") is True:
                            summary = str(check.get("summary") or "Goal satisfied.")
                            return finish("done", summary, screen=obs.screen_text)
                        refusal = f"not touching window {other!r}: the goal names a different window"
                        await self._say(f"  {refusal}")
                        self.history.append(
                            {"step": len(self.history) + 1, "kind": "refused", "action": describe(d),
                             "ok": False, "error": refusal}
                        )
                        before, pending = obs, None
                        stuck_streak += 1
                        if stuck_streak >= 3:
                            return self._finish_blocked(obs, refusal)
                        continue
                    if is_irreversible(d.target) and not cfg.allow_irreversible:
                        desc = describe(d)
                        await self._say(
                            f"  irreversible control {d.target.label!r}; stopping for confirmation"
                        )
                        return finish(
                            "needs_confirmation",
                            f"The next action is {desc}, which looks irreversible. Re-run with allow_irreversible=true to proceed.",
                            pending=desc,
                            screen=obs.screen_text,
                        )
                    rec = await self.surface.execute("click", 
                        d.target,
                        double=(d.operation == "double_click"),
                        irreversible=is_irreversible(d.target),
                    )
                    if d.target.role in STATE_ROLES:
                        t = d.target
                        self._clicked_toggle = (t.window, t.role, t.label, t.value)
                elif d.operation == "type" and d.target is not None:
                    text = d.text_literal
                    if text is None:
                        text = await self._field_text(d, obs)
                    if text is None:
                        self.history.append(
                            {
                                "step": step,
                                "kind": "type",
                                "action": describe(d),
                                "changed": False,
                                "diff": "no text available for this field",
                            }
                        )
                        out = await self._replan(
                            obs,
                            target,
                            reason=f"no text could be determined for field {d.target.label!r}",
                        )
                        if out is not None:
                            return out
                        continue
                    append = d.target.role == "AXTextArea"
                    key = (d.target.window, d.target.role, d.target.label, text)
                    if append and prev_append == key:
                        # Appending is not idempotent: the same text typed into the same
                        # area twice in a row was "Bread 1" six times (2026-10-08).
                        out = await self._replan(
                            obs,
                            target,
                            reason=f"{text!r} was just typed into {d.target.label!r}; typing it again would add it twice",
                        )
                        if out is not None:
                            return out
                        continue
                    rec = await self.surface.execute("type_into", 
                        d.target, text, replace=not append
                    )
                    self._last_append = key if append and rec.ok else None
                    action_desc = f"type {text!r} into [{d.target.index}] {d.target.label!r}"
                elif d.operation == "key" and d.key:
                    gate = self._key_gate(d.key, obs)
                    if gate and not cfg.allow_irreversible:
                        await self._say(
                            f"  key {d.key} would commit {gate!r}; stopping for confirmation"
                        )
                        return finish(
                            "needs_confirmation",
                            f"The next action is key {d.key}, which would activate {gate!r} and looks irreversible. "
                            "Re-run with allow_irreversible=true to proceed.",
                            pending=f"key {d.key} -> {gate}",
                            screen=obs.screen_text,
                        )
                    rec = await self.surface.execute("press", d.key, window=obs.focused_frame)
                elif d.operation in ("scroll_down", "scroll_up") and self._scroll_edge == (
                    d.operation,
                    obs.fingerprint(),
                ):
                    edge = "bottom" if d.operation == "scroll_down" else "top"
                    self.history.append(
                        {
                            "step": step,
                            "kind": d.operation,
                            "action": action_desc,
                            "ok": False,
                            "changed": False,
                            "diff": f"not scrolled: the last {d.operation} on this same screen moved "
                            f"nothing (already at the {edge})",
                        }
                    )
                    self._log({"event": "outcome", "step": step, **self.history[-1]})
                    before, pending = obs, None
                    out = await self._replan(
                        obs,
                        target,
                        reason=f"Jev chose {d.operation} again, but the page is already at its "
                        f"{edge}: that scroll just moved nothing on this same screen",
                    )
                    if out is not None:
                        return out
                    continue
                elif d.operation in ("scroll_down", "scroll_up"):
                    plan = obs.scroll_plan(
                        f"{self.cfg.goal}\n{self._subgoal or ''}", down=d.operation == "scroll_down"
                    )
                    at = plan.point
                    if at is None and (wb := self._window_bounds(target, obs.focused_window)):
                        at = (int(wb[0] + wb[2] / 2), int(wb[1] + wb[3] / 2))
                    rec = await self.surface.execute(
                        "scroll", at, down=(d.operation == "scroll_down"), way=plan.way, area=plan.area
                    )
                    self._settle_next = True
                elif d.operation == "open_url" and d.url:
                    rec = await self.surface.execute("open_url", d.url)
                elif d.operation == "launch_app" and d.launch_app:
                    rec = await self.surface.execute("launch", d.launch_app)
                    new_t = self._match_target(d.launch_app, self._windows())
                    if new_t:
                        target = new_t
                        self.actuator.set_target(target.pid, target.bundle, target.name)
                        await self.actuator.focus(target.bundle)
                        await self._say(f"target is now {target.name}")
                else:
                    out = await self._replan(
                        obs, target, reason=f"operation {d.operation!r} had no executable target"
                    )
                    if out is not None:
                        return out
                    continue
            except Cancelled:
                return finish("cancelled", "Cancelled by emergency stop.")
            except Refused as exc:
                if exc.fatal:
                    return self._finish_blocked(obs, self._refusal_reason(exc))
                entry = {
                    "step": step,
                    "kind": d.operation,
                    "action": action_desc,
                    "ok": False,
                    "changed": False,
                    "diff": f"refused: {exc}",
                }
                self.history.append(entry)
                stuck_streak += 1
                await self._say(f"  → refused: {exc}")
                self._log({"event": "outcome", "step": step, **entry})
                before, pending = obs, None
                continue

            declined = getattr(rec, "confirm_declined", None)
            if declined and not cfg.allow_irreversible:
                await self._say(f"  the page asked to confirm {declined!r}; answered Cancel")
                self.history.append(
                    {"step": step, "kind": d.operation, "action": action_desc, "ok": True,
                     "changed": None, "diff": f"the page asked to confirm: {declined!r}; answered Cancel"}
                )
                return finish(
                    "needs_confirmation",
                    f"{action_desc} made the page ask to confirm: {declined!r}. The operator answered "
                    "Cancel, so nothing was confirmed. Re-run with allow_irreversible=true to answer OK.",
                    pending=f"{action_desc} -> confirm {declined!r}",
                    screen=obs.screen_text,
                )
            if kind not in ("scroll", "scroll_down", "scroll_up"):
                # A scroll leaves an open panel as it was; any other action is
                # something done after the opening click.
                self._opened_by_last = (
                    (d.target.window, d.target.role, d.target.label)
                    if kind == "click" and d.target is not None and not is_open(d.target)
                    else None
                )
            before = obs
            self._last_new_lines = 0  # what the reads after this action add is its own
            pending = {
                "step": step,
                "kind": kind,
                "action": action_desc,
                # Run seconds when it ran: a watch goal's time limit is judged by
                # how old the last reading is.
                "at_s": round(time.perf_counter() - self._t_start),
                **(
                    {"target_key": (d.target.window, d.target.role, d.target.label)}
                    if d.target is not None
                    else {}
                ),
                "ok": rec.ok,
                **({"via": rec.description} if rec.description else {}),
                **({"error": rec.error} if rec.error else {}),
            }

        final = before
        if pending is not None and before is not None:
            final = await self._observe(target)
            changed, diff = diff_observations(before, final)
            self.history.append({**pending, "changed": changed, "diff": diff})
        # A read-only goal can be met by the time the steps run out (the last
        # page of a list read, the picker still open): say so, with what was read.
        summary, report, met = await self._closing(
            final, f"Stopped after {cfg.max_steps} steps without reaching done."
        )
        return finish(
            "done" if met else "budget_exhausted",
            summary,
            screen=final.screen_text if final else "",
            report=report,
        )

    def _key_gate(self, key: str, obs: Observation) -> str | None:
        """Name the irreversible control a committing key would activate, if any."""
        if key in ALWAYS_GATED_KEYS:
            return "send/submit shortcut"
        if key == "delete":
            # Outside a text field, Delete removes the selection (a mail message,
            # a note, a photo) rather than a character.
            if any(e.focused and e.kind == "type" for e in obs.elements):
                return None
            return "the current selection (Delete with no text field focused)"
        if key not in COMMIT_KEYS:
            return None
        if not obs.elements:
            return "an unseen control (the app exposes no accessibility tree)"
        focused = next((e for e in obs.elements if e.focused and e.kind == "type"), None)
        if self.surface.name == "dom" and focused is not None:
            # What Enter does in this field, as the page snapshot reports it.
            hint = getattr(focused, "enter_hint", "") or "unknown"
            # "pick": the field's suggestion list is open, and Enter takes the
            # highlighted suggestion (an airport, a city) without submitting;
            # "search-pick": the same in a search box, where Enter searches.
            if hint in ("search", "form", "pick", "search-pick"):
                return None
            if hint.startswith("form:"):
                label = hint[5:]
                return label if _IRREVERSIBLE.search(label) else None
            return (
                f"Enter in {focused.label!r} (no form or search box: the page decides what "
                "Enter does, and it may send or submit)"
            )
        scope = obs.dialog is not None
        for e in obs.elements:
            if e.role != "AXButton" or not is_irreversible(e):
                continue
            if scope or e.focused:
                return e.label
        return None

    def _remember_text(self, text: str, page: str = "") -> int:
        """Keep the screen the run just read, unless it shows the same lines as a
        screen already kept for this page (a scroll only reorders them). Returns
        how many (page, line) pairs the run had not read before."""
        lines = [
            ln
            for ln in (x.strip() for x in (text or "").split("\n"))
            if ln and ln not in TEXT_MARKS and not _MORE_TEXT.match(ln)
        ]
        added = 0
        for ln in lines:
            if (page, ln) not in self._known:
                self._known.add((page, ln))
                added += 1
        if lines:
            content = set(lines)
            if not any(p == page and set(ls) == content for p, ls in self._screens):
                self._screens.append((page, lines))
                self._trim_screens()
        return added

    def _trim_screens(self) -> None:
        """Within SCREENS_KEPT_CHARS: drop first the oldest screens that a later
        screen of the same page fully contains, then the oldest."""
        size = lambda ls: sum(len(x) + 1 for x in ls)  # noqa: E731
        total = sum(size(ls) for _, ls in self._screens)
        i = 0
        while total > SCREENS_KEPT_CHARS and i < len(self._screens) - 1:
            page, ls = self._screens[i]
            if any(p == page and set(ls) <= set(later) for p, later in self._screens[i + 1 :]):
                total -= size(ls)
                del self._screens[i]
            else:
                i += 1
        while total > SCREENS_KEPT_CHARS and len(self._screens) > 1:
            total -= size(self._screens.pop(0)[1])

    def _earlier_text(self, obs: Observation) -> str:
        """The other screens the run read, each whole under a header naming its
        page, oldest first, without those the final screen repeats. Over the
        limit, screens a later one contains go first, then the oldest."""
        final = set(
            ln for ln in (x.strip() for x in obs.full_text.split("\n")) if ln and ln not in TEXT_MARKS
        )
        here = obs.focused_window
        shown = [(p, ls) for p, ls in self._screens if not (p == here and set(ls) <= final)]
        size = lambda ls: sum(len(x) + 1 for x in ls)  # noqa: E731
        total = sum(size(ls) for _, ls in shown)
        i = 0
        while total > SEEN_MAX_CHARS and i < len(shown):
            p, ls = shown[i]
            later = [set(x) for q, x in shown[i + 1 :] if q == p] + ([final] if p == here else [])
            if any(set(ls) <= x for x in later):
                total -= size(ls)
                del shown[i]
            else:
                i += 1
        while total > SEEN_MAX_CHARS and shown:
            total -= size(shown.pop(0)[1])
        out: list[str] = []
        for n, (p, ls) in enumerate(shown, 1):
            where = p or "the screen"
            out.append(f"[earlier screen {n}, read on {where}" + (" (the final page)]" if p == here else "]"))
            out.extend(ls)
        return "\n".join(out)

    def _track_repeat(self, pending: dict[str, Any], meaningful: bool) -> None:
        """Count the same action in a row that changed nothing meaningful."""
        if meaningful:
            self._repeat_key, self._repeat_count, self._repeat_replanned = None, 0, False
            return
        key = f"{pending.get('kind')}: {pending.get('action')}"
        self._repeat_count = self._repeat_count + 1 if key == self._repeat_key else 1
        self._repeat_key = key

    def _save_last_frame(self) -> str | None:
        if self._last_frame is None:
            return None
        data, mime = self._last_frame
        ext = "png" if "png" in mime else "jpg"
        path = RUN_DIR / f"{self.run_id}-last-frame.{ext}"
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            return None
        return str(path)

    def _log_finish(self, status: str, summary: str, res: RunResult | None = None) -> None:
        """The run's last log row: everything the caller was sent, so a reader of
        the log needs no other row to know how the run ended and what it read."""
        if self._finish_logged:
            return
        self._finish_logged = True
        row: dict[str, Any] = {
            "event": "finish",
            "version": 2,
            "status": status,
            "reason": summary,
            "summary": summary,
            "actions": len(self.history),
            "decisions": len(self.jev_ms),
        }
        if res is not None:
            row.update(
                {
                    "code": res.code,
                    "report": res.report,
                    "page": res.page,
                    "pending_action": res.pending_action,
                    "llm_calls": res.llm_calls,
                    "llm_ms": res.llm_ms,
                    "llm_tokens": list(res.llm_tokens),
                    "footer": res.footer(),
                }
            )
        self._log(row)

    def _finish(
        self,
        status: str,
        summary: str,
        *,
        pending: str | None = None,
        screen: str = "",
        code: str = "",
        report: str = "",
    ) -> RunResult:
        last = self._last_obs
        res = RunResult(
            status=status,
            summary=summary,
            steps=len(self.history),
            jev_calls=len(self.jev_ms),
            jev_ms=list(self.jev_ms),
            llm_calls=self.llm.stats.calls if self.llm else 0,
            llm_ms=self.llm.stats.total_ms if self.llm else 0,
            llm_tokens=(self.llm.stats.input_tokens, self.llm.stats.output_tokens)
            if self.llm
            else (0, 0),
            elapsed_s=time.perf_counter() - self._t_start,
            history=list(self.history),
            run_id=self.run_id,
            log_path=str(self.log_path),
            pending_action=pending,
            final_screen_text=screen or (last.screen_text if last else ""),
            surface=self._surface_label(),
            final_table=last.table() if last else "",
            read_ms=list(self.read_ms),
            last_frame_path=self._save_last_frame() if status in HANDOFF_STATUSES else None,
            code=code or ("ax_unreadable" if last is not None and not last.elements and status == "blocked" else ""),
            page=self._page_line(),
            report=report,
        )
        self._log_finish(status, summary, res)
        return res

    def _page_line(self) -> str:
        url = getattr(self.surface, "url", "") if self.surface.name == "dom" else ""
        if not url:
            return ""
        title = getattr(self.surface, "title", "")
        return f"{title} — {url}" if title else url

    async def _continues_here(self, url: str) -> bool:
        """Can the run go on in its pinned tab instead of opening `url`: a tab a
        run opened or worked in, on the same site."""
        s = self.surface
        if s.name != "dom":
            return False
        if not (getattr(s, "_injected_js", False) or dom_surface.run_tab(s.bundle, s.tab_id)):
            return False
        try:
            current = await s.current_url()
        except Exception:  # noqa: BLE001
            return False
        return same_site(current, url)

    async def _open_goal_url(self, url: str) -> None:
        """The goal names a page: open it in a new tab unless the browser already
        shows it. Done in code, before the first decision, so a run never starts
        by acting on whatever unrelated tab happens to be in front."""
        current = ""
        try:
            if self.surface.name == "dom":
                current = await self.surface.current_url()
            else:
                current = await asyncio.to_thread(dom_surface.active_tab_url, self.actuator.target_bundle)
        except Exception:  # noqa: BLE001
            pass
        if same_page(current, url):
            # The goal names this page, so the run may work in this tab, and a
            # later run without an address may continue in it.
            if self.surface.name == "dom":
                dom_surface.remember_tab(self.surface.bundle, self.surface.tab_id)
            self._log({"event": "open_url", "url": url, "skipped": "already on it"})
            return
        await self._say(f"opening {url} in a new tab")
        rec = await self.surface.execute("open_url", url)
        self._log({"event": "open_url", "url": url, "ok": rec.ok, "error": rec.error})
        self.history.append(
            {
                "step": 0,
                "kind": "open_url",
                "action": f"open_url {url}",
                "ok": rec.ok,
                "changed": rec.ok,
                "diff": "opened in a new tab" if rec.ok else f"failed: {rec.error}",
            }
        )

    # ─── doors ───────────────────────────────────────────────────────

    def _set_subgoal(self, s: Any, obs: Observation | None = None) -> None:
        if isinstance(s, str) and s.strip():
            self._subgoal = s.strip()[:400]
            self._subgoal_steps = 0
            self._subgoal_at = len(self.history)
            self._subgoal_refs = {}
            if obs is not None:
                for m in _INDEX_REF.finditer(self._subgoal):
                    el = obs.by_index(int(m.group(1)))
                    if el is not None:
                        self._subgoal_refs[el.index] = (el.role, el.label)

    def _reshuts(self, el: Element) -> bool:
        """Whether clicking `el` would close the panel the run's last action opened
        with it, when nothing asks for the panel to be closed."""
        if self._opened_by_last != (el.window, el.role, el.label) or not is_open(el):
            return False
        return not _CLOSE_WORDS.search(self._subgoal or self.cfg.goal)

    def _bound_subgoal(self, obs: Observation) -> str | None:
        """The sub-goal with its [N] indices renumbered for this screen. The
        planner's indices belong to the screen it read: after a date was picked a
        RESET CALENDAR link moved the departure day from [30] to [31], and Jev,
        told [30], doubted the right day. An index whose control is not on this
        screen once (by role and label) is dropped, and the label stays."""
        sub = self._subgoal
        if not sub or not self._subgoal_refs:
            return sub

        def fix(m: re.Match[str]) -> str:
            ref = self._subgoal_refs.get(int(m.group(2)))
            if ref is None:
                return m.group(0)
            same = [e for e in obs.elements if (e.role, e.label) == ref]
            return f"{m.group(1)}[{same[0].index}]" if len(same) == 1 else ""

        return re.sub(r"(\s?)\[(\d{1,4})\]", fix, sub)

    def _log_llm(self, since: int) -> None:
        if self.llm is not None:
            for entry in self.llm.stats.log[since:]:
                self._log({"event": "llm", **entry})

    async def _field_text(self, d: Decision, obs: Observation) -> str | None:
        if self.llm is None or d.target is None:
            return None
        await self._say("  → LLM: composing field text")
        since = len(self.llm.stats.log)
        text, values = await self.llm.field_text(
            goal=self.cfg.goal,
            field_label=d.target.label,
            field_role=d.target.role,
            field_value=d.target.value,
            screen_text=obs.screen_text,
            history=self.history,
            # The planner works values out from pages the screen no longer shows;
            # without its sub-goal and notes the door returned no text for them.
            subgoal=self._subgoal,
            planner_notes=self._planner_notes,
        )
        self._log_llm(since)
        # Goals give names, codes and addresses without quotes, so each field took
        # a door call: a third of the calls on the booking pages only copied a word
        # of the goal. The door now names them all once. Only single words the goal
        # holds are kept, so a typing option never comes from the model alone. A
        # phrase bundles things a field takes apart: "Orlando (MCO)" typed into an
        # airport field matched no suggestion.
        for v in [text or "", *values]:
            v = v.strip()
            if (
                len(v) >= 2
                and not re.search(r"\s", v)
                and v not in self._goal_values
                and re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", self.cfg.goal)
            ):
                self._goal_values.append(v)
        return text

    async def _window_shot(self, target: Target) -> tuple[Any, tuple[float, ...]] | None:
        """Capture the target window with its on-screen bounds. Only a window
        capture is sent: a display capture may not even show the target when it
        sits on another monitor, and planner coordinates are mapped through the
        window's bounds."""
        win = next(
            (w for w in self._windows() if w.id == self.actuator.window_id and w.pid == target.pid),
            None,
        )
        if win is None:
            return None
        frame = await self.actuator.frame("replan")
        if frame is None or not frame.width or not frame.height:
            return None
        self._last_frame = (frame.data, frame.mime_type)
        b = win.bounds
        return frame, (float(b.x), float(b.y), float(b.w), float(b.h))

    async def _ask_planner(self, obs: Observation, shot: Any, reason: str) -> dict[str, Any] | None:
        assert self.llm is not None
        self._shot_geometry = None
        if shot is not None:
            frame, bounds = shot
            self._shot_geometry = (bounds, frame.width, frame.height)
        await self._say(f"  → LLM: replanning ({reason})" + (" with screenshot" if shot else ""))
        since = len(self.llm.stats.log)
        out = await self.llm.replan(
            goal=self.cfg.goal,
            subgoal=self._subgoal,
            reason=reason,
            history=self.history,
            elements_table=obs.door_table(),
            screen_text=obs.full_text,
            screenshot=shot[0].data if shot else None,
            screenshot_size=(shot[0].width, shot[0].height) if shot else None,
            media_type=shot[0].mime_type if shot else "image/jpeg",
            app_notes=self._notes,
            surface=self.surface.name,
            # Its own earlier notes: without them a watch run noted "the starting
            # backlog is 385, page at 424", then "treat 422 as the baseline", then
            # "the starting value was not recorded".
            planner_notes=self._planner_notes,
            run_seconds=round(time.perf_counter() - self._t_start),
            start_screen=self._start_screen,
        )
        self._log_llm(since)
        self._log({"event": "replan", "reason": reason, "out": out, "screenshot": shot is not None})
        note = str((out or {}).get("note") or "").strip()
        if note and note not in self._planner_notes:
            self._planner_notes.append(note[:600])
            del self._planner_notes[:-PLANNER_NOTES_KEPT]
        return out

    async def _replan(self, obs: Observation, target: Target, *, reason: str) -> RunResult | None:
        """Returns a RunResult when the run should end, None to continue."""
        self._replans_since_progress += 1
        # The last LLM call is kept for the end-state check: a search run that
        # reached its results spent every call on replans and ended "blocked"
        # with the goal met on screen.
        if self.llm is None or self.llm.budget_left <= 1 or self._replans_since_progress > 2:
            # Jev can stall right after the goal was met (e.g. by a planner click);
            # check the end state before reporting the run as stuck.
            check = None
            if self.history and self.llm is not None and self.llm.budget_left > 0:
                check = await self._verify(obs)
                if check and check.get("satisfied") is True:
                    summary = str(check.get("summary") or "Goal satisfied.")
                    return self._finish(
                        "done", summary, screen=obs.screen_text, report=self._report(check)
                    )
                wait_s = (check or {}).get("wait_seconds")
                if isinstance(wait_s, (int, float)) and wait_s >= 1 and self.llm.budget_left >= 1:
                    # Not stuck: the goal waits on the world, and a call is left for
                    # the next check. A watch run ended "blocked" here with the
                    # check asking for 15 more seconds.
                    self._set_subgoal(check.get("subgoal"), obs)
                    return await self._planner_wait({"seconds": wait_s}, obs, target, by="check")
            if self.llm is not None and self.llm.budget_left <= 1 and self._replans_since_progress <= 2:
                # Still making progress: the run is out of calls, not stuck, and the
                # caller can continue from this page. "blocked" told it to take over.
                return self._finish_blocked(
                    obs,
                    f"used its {self.llm.max_calls} LLM calls ({reason})",
                    check,
                    status="budget_exhausted",
                )
            return self._finish_blocked(obs, reason, check)
        want_shot = not obs.elements or self._replans_since_progress >= 2
        shot = await self._window_shot(target) if want_shot else None
        out = await self._ask_planner(obs, shot, reason)
        if out is not None and out.get("status") == "give_up" and shot is None and self.surface.name == "ax":
            # A planner giving up on the table alone often needs only a look: the
            # chosen tile or highlighted row the app does not mark as selected.
            shot = await self._window_shot(target)
            if shot is not None:
                out = await self._ask_planner(obs, shot, reason)
        if out is None:
            if self.llm.last_overflow:
                reason += (
                    f"; the planner's reply ran past {REPLAN_MAX_TOKENS} tokens and was "
                    "thrown away (a long text copied from the goal instead of named)"
                )
                await self._say(f"  → planner reply overflowed {REPLAN_MAX_TOKENS} tokens")
            return self._finish_blocked(obs, reason)
        status = out.get("status")
        if status == "done":
            # The planner sees the same screen and can be just as wrong; hold its
            # "done" to the end-state check Jev's own "done" goes through, and
            # count its rejections with Jev's. Counted apart, five rejected
            # checks in a row came from planner claims with nothing new read.
            return await self._check_done(obs, target, by="the planner")
        if status == "give_up":
            # One end-state check first, when a call is left: what the run read
            # comes back with the reason, and a read whose last values are not on
            # the page ends partial instead of blocked.
            note = str(out.get("note") or reason)
            check = await self._verify(obs) if self.history else None
            if check and check.get("satisfied") is True:
                summary = str(check.get("summary") or "Goal satisfied.")
                return self._finish("done", summary, screen=obs.screen_text, report=self._report(check))
            if check is None:
                return self._finish("blocked", note, screen=obs.screen_text)
            return self._finish_blocked(obs, note, check)
        self._set_subgoal(out.get("subgoal"), obs)
        await self._say(f"  sub-goal: {self._subgoal}")
        plan = out.get("direct_actions")
        if not isinstance(plan, list):
            plan = [out.get("direct_action")]
        plan = [d for d in plan if isinstance(d, dict)][:MAX_DIRECT_ACTIONS]
        da = plan[0] if plan else None
        if isinstance(da, dict) and da.get("kind") == "wait" and not self.cfg.dry_run:
            return await self._planner_wait(da, obs, target)
        # Pointer and key actions need the screenshot they were read from; an
        # address to open does not.
        if isinstance(da, dict) and not self.cfg.dry_run and (shot is not None or da.get("kind") == "open_url"):
            try:
                await self._direct_plan(plan, obs, target, shot)
            except Cancelled:
                return self._finish(
                    "cancelled", "Cancelled by emergency stop.", screen=obs.screen_text
                )
            except Refused as exc:
                if exc.fatal:
                    return self._finish_blocked(obs, self._refusal_reason(exc))
                self.history.append(
                    {
                        "step": len(self.history) + 1,
                        "kind": str(da.get("kind")),
                        "action": "planner action refused",
                        "ok": False,
                        "changed": False,
                        "diff": f"refused: {exc}",
                    }
                )
        return None

    async def _direct_plan(
        self, plan: list[dict[str, Any]], obs: Observation, target: Target, shot: Any
    ) -> None:
        """The planner's actions in order. The first may use pixels of the
        screenshot it read; the rest are for screens that come after it, so a
        click there names its control and Jev finds it on a fresh capture. The
        plan stops at the first action that fails, is skipped, or changes
        nothing: the screen it expected did not come."""
        frame = shot[0].data if shot is not None else None
        for i, da in enumerate(plan):
            if i and da.get("kind") == "wait":
                # A pause inside a plan is short; a long watch is its own replan.
                try:
                    secs = min(float(da.get("seconds") or 0), MAX_PLAN_WAIT_S)
                except (TypeError, ValueError):
                    secs = 0.0
                end = time.perf_counter() + secs
                while time.perf_counter() < end:
                    if self.spec.cancel_event.is_set():
                        raise Cancelled()
                    await asyncio.sleep(0.25)
                continue
            taken = len(self.history)
            await self._direct(da, obs, first=(i == 0))
            if frame is None or len(self.history) == taken or not self.history[-1].get("ok"):
                return
            expect = da.get("expect")
            outcome = await self._pixel_outcome(
                target, frame, expect=expect.strip() if isinstance(expect, str) else None
            )
            if outcome is None:
                return
            progressed, frame = outcome
            if not progressed:
                return

    async def _check_done(self, obs: Observation, target: Target, *, by: str) -> RunResult | None:
        """The end-state check behind every "done", Jev's or the planner's.
        Returns the result that ends the run, or None to go on with the check's
        sub-goal. Rejections are counted together: only progress between two of
        them (an action that took effect, or text the run had not read) starts
        the count again, so claims with nothing new between them end the run."""
        out = await self._verify(obs)
        if out is None:
            unverified = (
                ""
                if by == "Jev" and (self.llm is None or not self.cfg.verify_with_llm)
                else f"{by[0].upper()}{by[1:]} reported done; the end state was not checked "
                "(LLM check unavailable). "
            )
            return self._finish("done", unverified + self._template_summary(obs), screen=obs.screen_text)
        summary = str(out.get("summary") or "")
        if out.get("satisfied") is True:
            return self._finish(
                "done", summary or "Goal reported satisfied.", screen=obs.screen_text, report=self._report(out)
            )
        wait_s = out.get("wait_seconds")
        if isinstance(wait_s, (int, float)) and wait_s >= 1:
            # "Not yet" on a goal that waits: the claim was early, not wrong about
            # the screen, and does not count. Two such checks ended a watch run
            # "blocked" a minute into its two.
            self._set_subgoal(out.get("subgoal"), obs)
            await self._say(f"  not yet: {summary}; sub-goal: {self._subgoal}")
            return await self._planner_wait({"seconds": wait_s}, obs, target, by="check")
        self._verify_rounds = 1 if self._progress_since_verify else self._verify_rounds + 1
        self._progress_since_verify = False
        unresolved = self._unresolved(out)
        if unresolved and not str(out.get("subgoal") or "").strip():
            # The check found every place the goal lets the run look, and the
            # rest is not on the page: say what was read and what is missing.
            return self._finish_partial(obs, out, unresolved)
        if self._verify_rounds >= MAX_VERIFY_ROUNDS:
            if unresolved:
                return self._finish_partial(obs, out, unresolved)
            return self._finish(
                "blocked",
                f"{by[0].upper()}{by[1:]} reported done {self._verify_rounds} times but the end-state "
                f"check disagreed each time, with nothing new read in between: {summary} "
                + self._template_summary(obs),
                screen=obs.screen_text,
                report=self._report(out),
            )
        self._set_subgoal(out.get("subgoal"), obs)
        await self._say(f"  {by} said done, the end-state check disagrees: {summary}; sub-goal: {self._subgoal}")
        return None

    @staticmethod
    def _unresolved(check: dict[str, Any] | None) -> list[str]:
        got = (check or {}).get("unresolved")
        if not isinstance(got, list):
            return []
        return [str(x).strip() for x in got if str(x).strip()][:20]

    def _finish_partial(self, obs: Observation, check: dict[str, Any], unresolved: list[str]) -> RunResult:
        """A read that got all the page shows within the goal's limits, with the
        values the page does not show named. Not `done` (the goal asked for them),
        and not `blocked` (running again finds nothing more)."""
        summary = str(check.get("summary") or "").strip()
        missing = "; ".join(unresolved)
        return self._finish(
            "partial",
            f"{summary} Not shown on the pages the run could read: {missing}.".strip(),
            screen=obs.screen_text,
            report=self._report(check),
            code="unresolved",
        )

    async def _planner_wait(
        self, da: dict[str, Any], obs: Observation, target: Target, *, by: str = "planner"
    ) -> RunResult | None:
        """Let time pass for a goal that waits on the world (a dashboard value, a
        job, a code), then look again. A wait the screen changed during is progress:
        watching is the work. Jev's own wait is under a second, so a watch run
        replanned every few seconds and ended "stuck" 35 s in."""
        try:
            want = float(da.get("seconds") or 0)
        except (TypeError, ValueError):
            want = 0.0
        left = self.cfg.max_runtime_s - (time.perf_counter() - self._t_start) - WAIT_MARGIN_S
        secs = max(0.0, min(want, MAX_WAIT_S, left))
        if secs < 1:
            self.history.append(
                {
                    "step": len(self.history) + 1,
                    "kind": "wait",
                    "action": f"{by} wait {want:g} s refused",
                    "ok": False,
                    "changed": False,
                    "diff": "refused: not enough run time left",
                }
            )
            return None
        await self._say(f"  → waiting {secs:.0f} s")
        end = time.perf_counter() + secs
        while (now := time.perf_counter()) < end:
            if self.spec.cancel_event.is_set():
                return self._finish(
                    "cancelled", "Cancelled by emergency stop.", screen=obs.screen_text
                )
            await asyncio.sleep(min(0.5, end - now))
        try:
            after = await self._observe(target)
        except SurfaceFailed as exc:
            return self._finish_blocked(obs, f"the app could not be read after a wait ({exc})")
        changed, diff = diff_observations(obs, after)
        moved = meaningful_change(obs, after)
        if moved:
            self._replans_since_progress = 0
            self._progress_since_verify = True
        # Jev's own short waits start again; without this its next one asked the
        # planner at once.
        self._consecutive_waits = 0
        entry = {
            "step": len(self.history) + 1,
            "kind": "wait",
            "action": f"{by} wait {secs:.0f} s",
            "ok": True,
            "changed": changed,
            "diff": diff,
            "at_s": round(time.perf_counter() - self._t_start),
        }
        self.history.append(entry)
        self._log({"event": "outcome", **entry, "meaningful": moved})
        await self._say(f"  → {'changed' if changed else 'no change'}: {diff}")
        return None

    def _allowed_hosts(self) -> set[str]:
        """Sites a planner-built address may point at: the goal's, and the ones
        this run has been on."""
        hosts = {(urlparse(u).hostname or "").lower() for u in urls_from_goal(self.cfg.goal)}
        hosts.update(self._site_notes)
        cur = (urlparse(getattr(self.surface, "url", "") or "").hostname or "").lower()
        hosts.add(cur)
        hosts.discard("")
        return {h[4:] if h.startswith("www.") else h for h in hosts}

    async def _direct(self, da: dict[str, Any], obs: Observation, *, first: bool = True) -> None:
        """One planner action. `first` means the screenshot the planner read is
        still the screen, so its own pixel coordinates are good."""
        kind = da.get("kind")
        if kind == "open_url":
            url = str(da.get("url") or "").strip()
            host = (urlparse(url).hostname or "").lower()
            host = host[4:] if host.startswith("www.") else host
            browser = dom_surface.supported(getattr(self.actuator, "target_bundle", "") or "")
            if not browser or not url.lower().startswith(("http://", "https://")) or host not in self._allowed_hosts():
                self.history.append(
                    {
                        "step": len(self.history) + 1,
                        "kind": "open_url",
                        "action": f"planner open_url {url} refused",
                        "ok": False,
                        "changed": False,
                        "diff": "refused: only addresses on a site the goal names or the run has visited",
                    }
                )
                return
            rec = await self.surface.execute("open_url", url)
            self.history.append(
                {
                    "step": len(self.history) + 1,
                    "kind": "open_url",
                    "action": f"planner open_url {url}",
                    "ok": rec.ok,
                    "changed": None,
                    "diff": "opened" if rec.ok else f"failed: {rec.error}",
                }
            )
            return
        if kind == "click" and self._shot_geometry is not None:
            (wx, wy, ww, wh), fw, fh = self._shot_geometry
            described = str(da.get("target") or "").strip()
            has_pixels = isinstance(da.get("x"), (int, float)) and isinstance(da.get("y"), (int, float))
            point: tuple[float, float] | None = None
            label = ""
            if has_pixels and first:
                # The planner's own coordinates on the screenshot it read:
                # frontier planners put them inside the control about 99% of
                # the time, which Jev's box does not match (RESULTS.md).
                point = (wx + float(da["x"]) * ww / fw, wy + float(da["y"]) * wh / fh)
                label = f"planner click at ({int(point[0])}, {int(point[1])})" + (
                    f" on {described!r}" if described else ""
                )
            elif described:
                point = await self._locate(described, (wx, wy, ww, wh))
                label = f"planner click on {described!r}"
            if point is None:
                return
            x, y = point
            hit = obs.element_at(x, y)
            # With no element under the point (the app exposes none), the
            # planner's own words for the control are what can be judged.
            risky = (hit is not None and is_irreversible(hit)) or (
                hit is None and described and bool(_IRREVERSIBLE.search(described))
            )
            if risky and not self.cfg.allow_irreversible:
                self.history.append(
                    {
                        "step": len(self.history) + 1,
                        "kind": "click",
                        "action": f"{label} refused",
                        "ok": False,
                        "changed": False,
                        "diff": f"refused: {hit.label if hit else described!r} looks irreversible",
                    }
                )
                return
            rec = await self.surface.execute(
                "click_point",
                int(x),
                int(y),
                description=label + (f" [{hit.label}]" if hit else ""),
                window=(wx, wy, ww, wh),
            )
            self.history.append(
                {
                    "step": len(self.history) + 1,
                    "kind": "click",
                    "action": f"{label} at ({int(x)}, {int(y)})" + (f" [{hit.label}]" if hit else ""),
                    "ok": rec.ok,
                    "changed": None,
                    "diff": "executed from screenshot",
                }
            )
        elif kind == "key" and isinstance(da.get("combo"), str):
            combo = da["combo"]
            if combo not in KEY_OPTIONS or (
                self._key_gate(combo, obs) and not self.cfg.allow_irreversible
            ):
                self.history.append(
                    {
                        "step": len(self.history) + 1,
                        "kind": "key",
                        "action": f"planner key {combo} refused",
                        "ok": False,
                        "changed": False,
                        "diff": "refused: not an allowed key or would commit an irreversible control",
                    }
                )
                return
            rec = await self.surface.execute("press", combo, window=obs.focused_frame)
            self.history.append(
                {
                    "step": len(self.history) + 1,
                    "kind": "key",
                    "action": f"planner key {da['combo']}",
                    "ok": rec.ok,
                    "changed": None,
                    "diff": "executed",
                }
            )
        elif kind == "type" and (isinstance(da.get("text"), str) or isinstance(da.get("from_goal"), dict)):
            text = da.get("text")
            if not isinstance(text, str):
                span = da["from_goal"]
                text = goal_span(self.cfg.goal, span.get("after"), span.get("until"))
                if not text:
                    self.history.append(
                        {
                            "step": len(self.history) + 1,
                            "kind": "type",
                            "action": "planner type refused",
                            "ok": False,
                            "changed": False,
                            "diff": f"the goal has no text after {span.get('after')!r}"
                            + (f" and before {span.get('until')!r}" if span.get("until") else ""),
                        }
                    )
                    return
                da = {**da, "text": text}
            focused = next((e for e in obs.elements if e.focused), None)
            if focused is not None and focused.subrole == "AXSecureTextField":
                self.history.append(
                    {
                        "step": len(self.history) + 1,
                        "kind": "type",
                        "action": "planner type refused",
                        "ok": False,
                        "changed": False,
                        "diff": "refused: focus is on a secure field",
                    }
                )
                return
            rec = await self.surface.execute(
                "type_raw", da["text"], replace=bool(da.get("replace")), focused=focused
            )
            self.history.append(
                {
                    "step": len(self.history) + 1,
                    "kind": "type",
                    "action": f"planner typed {da['text']!r}",
                    "ok": rec.ok,
                    "changed": None,
                    "diff": "executed",
                }
            )

    async def _locate(
        self, description: str, bounds: tuple[float, float, float, float]
    ) -> tuple[float, float] | None:
        """Screen point of the control the planner described, from a fresh
        capture of the target window, or None when Jev does not see it."""
        image = await vision.capture(self.actuator)
        if image is None and self._last_frame is not None:
            image = self._last_frame[0]
        if image is None:
            self._skip_click(description, "no capture of the window")
            return None
        try:
            loc = await vision.locate(self.provider, image, description)
        except DecisionError as exc:
            self._skip_click(description, f"the decision service failed: {exc}")
            return None
        self._log(
            {
                "event": "locate",
                "target": description,
                "visible": round(loc.visible, 3),
                "confidence": round(loc.confidence, 3),
                "box": [round(v, 4) for v in (loc.box.left, loc.box.top, loc.box.right, loc.box.bottom)],
                "stages": loc.stages,
                "input_tokens": loc.input_tokens,
                "latency_ms": loc.latency_ms,
            }
        )
        if not loc.seen:
            await self._say(f"  → Jev: does not see {description!r} (p={loc.visible:.2f})")
            self._skip_click(description, f"Jev does not see it in the window (p={loc.visible:.2f})")
            return None
        if not loc.found:
            await self._say(f"  → Jev: unsure where {description!r} is (box confidence {loc.confidence:.2f})")
            self._skip_click(description, f"Jev is unsure where it is (box confidence {loc.confidence:.2f}); left to the planner")
            return None
        wx, wy, ww, wh = bounds
        x, y = wx + loc.x * ww, wy + loc.y * wh
        await self._say(
            f"  → Jev: {description!r} at ({int(x)}, {int(y)}), p={loc.visible:.2f}, box confidence {loc.confidence:.2f}"
        )
        return x, y

    def _skip_click(self, description: str, why: str) -> None:
        self.history.append(
            {
                "step": len(self.history) + 1,
                "kind": "click",
                "action": f"planner click on {description!r} skipped",
                "ok": False,
                "changed": False,
                "diff": why,
            }
        )

    async def _pixel_outcome(
        self, target: Target, before: bytes, expect: str | None = None
    ) -> tuple[bool, bytes] | None:
        """After an action taken from a screenshot, what says it worked. With
        an expectation from the planner ("the File menu is open"), Jev answers
        whether it holds in a fresh capture, and that is the verdict: a click
        that opened the wrong menu changed the window too. Without one, a
        changed window is the only sign an app with no accessibility tree
        gives; a window that looks as it did two actions ago is a toggle
        undoing itself, not progress. Returns whether the action counts as
        progress and the new capture, or None when there is no capture."""
        await self.actuator.settle()
        shot = await self._window_shot(target)
        if shot is None:
            return None
        after = shot[0].data
        held: bool | None = None
        if expect:
            held, after = await self._expectation(target, expect, after)
            if after is None:
                return None
        try:
            change = vision.frame_change(before, after)
            undone = (
                change >= PIXEL_CHANGE_MIN
                and len(self._pixel_frames) >= 2
                and vision.frame_change(self._pixel_frames[-2], after) < PIXEL_CHANGE_MIN
            )
        except Exception:  # a capture that is not an image (a stub in tests)
            return None
        changed = change >= PIXEL_CHANGE_MIN
        self._pixel_frames.append(after)
        del self._pixel_frames[:-3]
        share = f"{change:.0%}" if change >= 0.01 else f"{change:.2%}"
        progressed = held if held is not None else (changed and not undone)
        if self.history:
            self.history[-1]["changed"] = progressed
            if changed:
                self.history[-1]["diff"] += f"; {share} of the window changed" + (
                    " (back to an earlier screen)" if undone else ""
                )
        self._log({"event": "pixel_outcome", "change": round(change, 5), "changed": changed, "undone": undone,
                   "held": held})
        await self._say(f"  → {'changed' if changed else 'no change'}: {share} of the window")
        if progressed:
            self._replans_since_progress = 0
            self._progress_since_verify = True
        return progressed, after

    async def _expectation(
        self, target: Target, expect: str, after: bytes
    ) -> tuple[bool | None, bytes | None]:
        """Whether the planner's expectation holds in the capture; one more
        look after a pause when it does not, for a screen still arriving.
        None when Jev could not answer (the frame diff decides then)."""
        try:
            chk = await vision.check(self.provider, after, expect)
            if not chk.holds:
                await asyncio.sleep(EXPECT_RETRY_S)
                shot = await self._window_shot(target)
                if shot is None:
                    return None, None
                after = shot[0].data
                chk = await vision.check(self.provider, after, expect)
        except DecisionError as exc:
            self._log({"event": "expectation", "expect": expect, "error": str(exc)[:200]})
            return None, after
        except Exception:  # a capture that is not an image (a stub in tests)
            return None, after
        self._log({"event": "expectation", "expect": expect, "p": round(chk.p, 3), "held": chk.holds,
                   "input_tokens": chk.input_tokens, "latency_ms": chk.latency_ms})
        await self._say(f"  → Jev: {expect!r} {'holds' if chk.holds else 'does not hold'} (p={chk.p:.2f})")
        if self.history:
            self.history[-1]["diff"] += f"; expected {expect!r}: {'holds' if chk.holds else 'does not hold'} (p={chk.p:.2f})"
        return chk.holds, after

    async def _verify(self, obs: Observation) -> dict[str, Any] | None:
        if self.llm is None or not self.cfg.verify_with_llm or self.llm.budget_left <= 0:
            return None
        # An app that exposes no text leaves the check nothing to judge from
        # but the window itself.
        shot = None
        if not obs.elements and not obs.full_text.strip() and not self.cfg.dry_run:
            shot = await self._window_shot(self._target) if self._target else None
        await self._say("  → LLM: verifying end state" + (" with screenshot" if shot else ""))
        since = len(self.llm.stats.log)
        out = await self.llm.verify(
            goal=self.cfg.goal,
            history=self.history,
            screen_text=obs.full_text,
            elements_table=obs.door_table(),
            window=obs.focused_window,
            windows_at_start=self._start_windows or [],
            windows_now=obs.windows,
            app_notes=self._notes,
            earlier_text=self._earlier_text(obs),
            planner_notes=self._planner_notes,
            run_seconds=round(time.perf_counter() - self._t_start),
            screenshot=shot[0].data if shot else None,
            media_type=shot[0].mime_type if shot else "image/jpeg",
        )
        self._log_llm(since)
        self._log({"event": "verify", "out": out})
        return out

    @staticmethod
    def _report(check: dict[str, Any] | None) -> str:
        """The check's report as plain lines ("" when it has none)."""
        r = (check or {}).get("report")
        if isinstance(r, list):
            r = "\n".join(str(x) for x in r if str(x).strip())
        return r.strip() if isinstance(r, str) else ""

    async def _closing(self, obs: Observation | None, head: str) -> tuple[str, str, bool]:
        """Summary, report and whether the goal was met, for a run that ends
        without a decision of its own (out of steps or time). One end-state
        check when the run did something and the LLM budget allows; else the
        template."""
        if obs is None:
            return head, "", False
        check = await self._verify(obs) if self.history else None
        if check is None:
            return f"{head} {self._template_summary(obs)}", "", False
        summary = str(check.get("summary") or "").strip()
        met = check.get("satisfied") is True
        return (summary if met else f"{head} {summary}"), self._report(check), met

    def _template_summary(self, obs: Observation) -> str:
        acts = [h["action"] for h in self.history if h.get("kind") not in ("wait",)]
        head = (
            f"Performed {len(acts)} actions in {obs.app_name}: " + "; ".join(acts[-6:])
            if acts
            else f"No actions were taken in {obs.app_name}"
        )
        text = obs.screen_text.replace("\n", " | ")[:300]
        return head + (f". Screen now shows: {text}" if text else ".")

    def _finish_blocked(
        self,
        obs: Observation,
        reason: str,
        check: dict[str, Any] | None = None,
        *,
        status: str = "blocked",
    ) -> RunResult:
        """A stuck run's result. With an end-state check of this screen (`check`),
        its account of what was done and read replaces the action template."""
        unresolved = self._unresolved(check)
        if status == "blocked" and check is not None and unresolved:
            return self._finish_partial(obs, check, unresolved)
        tried = f" Last sub-goal: {self._subgoal}." if self._subgoal else ""
        said = str((check or {}).get("summary") or "").strip()
        return self._finish(
            status,
            f"Stopped: {reason}.{tried} " + (said or self._template_summary(obs)),
            screen=obs.screen_text,
            report=self._report(check),
        )
