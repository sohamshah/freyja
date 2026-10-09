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
    Decision,
    Thresholds,
    decide,
    describe,
)
from bridge.tools.jev_operator.handoff import LLMHelper
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
)
from bridge.tools.jev_operator.notes import app_notes, site_notes
from bridge.tools.jev_operator import dom_surface
from bridge.tools.jev_operator.dom_surface import DOMSurface, DOMUnavailable, TabGone
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
MAX_VERIFY_ROUNDS = 2
# The same action this many times in a row with no meaningful change forces a
# replan; the same thing again after that ends the run blocked.
MAX_REPEATS = 3
HANDOFF_STATUSES = ("blocked", "budget_exhausted", "error", "needs_confirmation")
HANDOFF_ROWS = 60
HANDOFF_SCREEN_CHARS = 300

APP_DIRS = [
    "/Applications",
    "/System/Applications",
    "/System/Applications/Utilities",
    str(Path.home() / "Applications"),
]


_URL = re.compile(r"https?://[^\s\"'<>()\[\]{}]+", re.I)


def urls_from_goal(goal: str) -> list[str]:
    out: list[str] = []
    for m in _URL.finditer(goal):
        u = m.group(0).rstrip(".,;:!?")
        if u not in out:
            out.append(u)
    return out[:5]


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
    return el.role in GATED_ROLES and bool(_IRREVERSIBLE.search(el.label))


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
    llm_max_calls: int = 8
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

    def footer(self) -> str:
        med = int(statistics.median(self.jev_ms)) if self.jev_ms else 0
        return (
            f"[jev_computer_use] status={self.status} surface={self.surface} steps={self.steps} "
            f"jev_calls={self.jev_calls} "
            f"jev_median_ms={med} llm_calls={self.llm_calls} llm_ms={self.llm_ms} "
            f"elapsed={self.elapsed_s:.1f}s log={self.log_path}"
        )

    def next_hint(self) -> str:
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
        self._fell_back = False
        self.history: list[dict[str, Any]] = []
        self.jev_ms: list[int] = []
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 10000:04d}"
        self.log_path = RUN_DIR / f"{self.run_id}.jsonl"
        self._subgoal: str | None = None
        self._subgoal_steps = 0
        self._replans_since_progress = 0
        # (window, role, label, text) of the last step when it appended text to a
        # text area; any other step clears it.
        self._last_append: tuple[str, str, str, str] | None = None
        self._consecutive_waits = 0
        self._settle_next = False  # after a scroll, wait for the animation to end before acting
        self._clicked_toggle: tuple[str, str, str, str | None] | None = None
        self._seen_windows: set[str] = set()
        self._start_windows: list[str] | None = None
        self._window_checks: set[str] = set()
        self._shot_geometry: tuple[tuple[float, ...], int, int] | None = None
        self.read_ms: list[int] = []
        self._last_obs: Observation | None = None
        self._last_frame: tuple[bytes, str] | None = None
        self._finish_logged = False
        self._notes = ""
        self._app_notes = ""
        self._site_notes: dict[str, str] = {}  # host -> notes, looked up once per host
        self._repeat_key: str | None = None
        self._repeat_count = 0
        self._repeat_replanned = False
        self.item: int | None = None  # for-each index; stamped on every log row

    def share_with(self, prior: "Operator", item: int | None) -> None:
        """Continue a for-each run: reuse the prior operator's surface (with its
        fallback state), actuator, run id and log. Everything else stays fresh."""
        self.actuator = prior.actuator
        self.surface = prior.surface
        self._surface_injected = True
        self._fell_back = prior._fell_back
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
        if not ok and "did not answer" in detail:
            # A busy browser can miss the first 3 s; the accessibility fallback for
            # a browser is far slower than waiting once more.
            ok, again = await asyncio.to_thread(
                dom_surface.probe, target.bundle, None, dom_surface.PROBE_RETRY_TIMEOUT_S
            )
            detail = f"{detail}; retry: {again}"
        self._log({"event": "surface_probe", "ok": ok, "detail": detail, "bundle": target.bundle})
        if ok:
            self.surface = self._make_dom(target.bundle)
            self.surface.may_confirm = self.cfg.allow_irreversible
            await self.surface.pin_active()

    def _swap_if_dom_failing(self) -> None:
        """Two DOM failures in a row (timeout or error, not a stale element): use AX
        for the rest of the run, never back."""
        s = self.surface
        if s.name == "dom" and getattr(s, "fail_streak", 0) >= 2:
            self._log({"event": "surface_fallback", "surface": "dom", "to": "ax", "reason": "dom failed twice in a row"})
            self.surface = self._make_ax()
            self._fell_back = True

    def _surface_label(self) -> str:
        return "dom\u2192ax" if self._fell_back else self.surface.name

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
                f.write(
                    json.dumps(
                        {"run_id": self.run_id, "t": time.time(), "surface": self.surface.name,
                         **({"item": self.item} if self.item is not None else {}), **row},
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
        self.actuator.window_frames = obs.window_frames
        self._seen_windows.update(w for w in obs.windows if w != "(untitled window)")
        if self._start_windows is None:
            self._start_windows = list(obs.windows)
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
        self._app_notes = await self._load_notes(target)
        self._notes = self._app_notes
        # The DOM surface drives the page through JavaScript; the browser can stay
        # behind whatever the person is working in.
        if not cfg.dry_run and self.surface.name != "dom":
            await self.actuator.focus(target.bundle)
        await self._say(f"target: {target.name} ({target.bundle}, pid {target.pid})")
        browser = dom_surface.supported(target.bundle)
        urls = urls_from_goal(cfg.goal) if browser else []
        if urls and not cfg.dry_run:
            await self._open_goal_url(urls[0])

        before: Observation | None = None
        pending: dict[str, Any] | None = None
        verify_rounds = 0
        stuck_streak = 0
        step = 0

        while step < cfg.max_steps:
            if self.spec.cancel_event.is_set():
                return finish("cancelled", "Cancelled by emergency stop.")

            elapsed = time.perf_counter() - self._t_start
            if elapsed > cfg.max_runtime_s:
                return finish(
                    "budget_exhausted",
                    f"Stopped after {elapsed:.0f}s (limit {cfg.max_runtime_s:.0f}s) "
                    f"and {len(self.history)} steps without reaching done.",
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
                entry = {**pending, "changed": changed, "diff": diff}
                self.history.append(entry)
                if pending.get("kind") not in ("wait", "scroll_down", "scroll_up"):
                    stuck_streak = 0 if meaningful else stuck_streak + 1
                    self._track_repeat(pending, meaningful)
                if meaningful:
                    self._replans_since_progress = 0
                await self._say(f"  → {'changed' if changed else 'no change'}: {diff}")
                self._log({"event": "outcome", "step": step, **entry, "meaningful": meaningful})
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

            step += 1
            try:
                d = await decide(
                    self.provider,
                    obs,
                    goal=cfg.goal,
                    subgoal=self._subgoal,
                    history=self.history,
                    # The planner's own quoted text ("\\nBread 1", with its line break)
                    # is a typing option too; the goal's literals alone lost the break.
                    literals=list(dict.fromkeys([*literals, *quoted_literals(self._subgoal or "")])),
                    launch_candidates=launch_cands,
                    thresholds=cfg.thresholds,
                    model=cfg.jev_model,
                    notes=self._notes,
                    urls=urls or None,
                    key_options=getattr(self.surface, "key_options", None),
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
                out = await self._verify(obs)
                if out is not None:
                    verify_rounds += 1
                    summary = str(out.get("summary") or "")
                    if out.get("satisfied") is True:
                        return finish(
                            "done", summary or "Goal reported satisfied.", screen=obs.screen_text
                        )
                    if verify_rounds >= MAX_VERIFY_ROUNDS:
                        return finish(
                            "blocked",
                            f"Jev reported done {verify_rounds} times but the end-state check "
                            f"disagreed each time: {summary} " + self._template_summary(obs),
                            screen=obs.screen_text,
                        )
                    self._set_subgoal(out.get("subgoal"))
                    await self._say(f"  verifier disagrees: {summary}; sub-goal: {self._subgoal}")
                    continue
                unverified = (
                    ""
                    if self.llm is None or not cfg.verify_with_llm
                    else "Jev reported done; the end state was not checked "
                    "(LLM check unavailable). "
                )
                return finish(
                    "done", unverified + self._template_summary(obs), screen=obs.screen_text
                )

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
                        obs, target, reason="the screen did not settle after repeated waits"
                    )
                    if out is not None:
                        return out
                    continue
                await asyncio.sleep(0.6)
                before, pending = obs, {"step": step, "kind": "wait", "action": "wait"}
                continue
            self._consecutive_waits = 0

            action_desc = describe(d)
            kind = d.operation
            prev_append, self._last_append = self._last_append, None
            try:
                if d.target is not None and d.target.offscreen and d.operation in ("click", "double_click", "type"):
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
            before = obs
            pending = {
                "step": step,
                "kind": kind,
                "action": action_desc,
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
        return finish(
            "budget_exhausted",
            f"Stopped after {cfg.max_steps} steps without reaching done. "
            + self._template_summary(final)
            if final
            else f"Stopped after {cfg.max_steps} steps without reaching done.",
            screen=final.screen_text if final else "",
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
            if hint in ("search", "form"):
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

    def _log_finish(self, status: str, summary: str, footer: str = "") -> None:
        if self._finish_logged:
            return
        self._finish_logged = True
        self._log(
            {
                "event": "finish",
                "status": status,
                "reason": summary,
                "summary": summary,
                "footer": footer,
                "steps": len(self.history),
            }
        )

    def _finish(
        self,
        status: str,
        summary: str,
        *,
        pending: str | None = None,
        screen: str = "",
        code: str = "",
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
        )
        self._log_finish(status, summary, res.footer())
        return res

    def _page_line(self) -> str:
        url = getattr(self.surface, "url", "") if self.surface.name == "dom" else ""
        if not url:
            return ""
        title = getattr(self.surface, "title", "")
        return f"{title} — {url}" if title else url

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

    def _set_subgoal(self, s: Any) -> None:
        if isinstance(s, str) and s.strip():
            self._subgoal = s.strip()[:400]
            self._subgoal_steps = 0

    def _log_llm(self, since: int) -> None:
        if self.llm is not None:
            for entry in self.llm.stats.log[since:]:
                self._log({"event": "llm", **entry})

    async def _field_text(self, d: Decision, obs: Observation) -> str | None:
        if self.llm is None or d.target is None:
            return None
        await self._say("  → LLM: composing field text")
        since = len(self.llm.stats.log)
        text = await self.llm.field_text(
            goal=self.cfg.goal,
            field_label=d.target.label,
            field_role=d.target.role,
            field_value=d.target.value,
            screen_text=obs.screen_text,
            history=self.history,
        )
        self._log_llm(since)
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
            screen_text=obs.screen_text,
            screenshot=shot[0].data if shot else None,
            screenshot_size=(shot[0].width, shot[0].height) if shot else None,
            media_type=shot[0].mime_type if shot else "image/jpeg",
            app_notes=self._notes,
            surface=self.surface.name,
        )
        self._log_llm(since)
        self._log({"event": "replan", "reason": reason, "out": out, "screenshot": shot is not None})
        return out

    async def _replan(self, obs: Observation, target: Target, *, reason: str) -> RunResult | None:
        """Returns a RunResult when the run should end, None to continue."""
        self._replans_since_progress += 1
        if self.llm is None or self.llm.budget_left <= 0 or self._replans_since_progress > 2:
            # Jev can stall right after the goal was met (e.g. by a planner click);
            # check the end state before reporting the run as stuck.
            if self.history and self.llm is not None and self.llm.budget_left > 0:
                check = await self._verify(obs)
                if check and check.get("satisfied") is True:
                    summary = str(check.get("summary") or "Goal satisfied.")
                    return self._finish("done", summary, screen=obs.screen_text)
            return self._finish_blocked(obs, reason)
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
            return self._finish_blocked(obs, reason)
        status = out.get("status")
        if status == "done":
            # The planner sees the same screen and can be just as wrong; hold its
            # "done" to the end-state check Jev's own "done" goes through.
            check = await self._verify(obs)
            if check is None:
                return self._finish(
                    "done",
                    "The planner reported done; the end state was not checked (LLM check "
                    "unavailable). " + self._template_summary(obs),
                    screen=obs.screen_text,
                )
            if check.get("satisfied") is True:
                note = str(check.get("summary") or out.get("note") or "Goal satisfied.")
                return self._finish("done", note, screen=obs.screen_text)
            self._set_subgoal(check.get("subgoal"))
            await self._say(
                f"  planner said done, end-state check disagrees: {check.get('summary')}"
            )
            return None
        if status == "give_up":
            return self._finish("blocked", str(out.get("note") or reason), screen=obs.screen_text)
        self._set_subgoal(out.get("subgoal"))
        await self._say(f"  sub-goal: {self._subgoal}")
        da = out.get("direct_action")
        # Pointer and key actions need the screenshot they were read from; an
        # address to open does not.
        if isinstance(da, dict) and not self.cfg.dry_run and (shot is not None or da.get("kind") == "open_url"):
            try:
                await self._direct(da, obs)
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

    def _allowed_hosts(self) -> set[str]:
        """Sites a planner-built address may point at: the goal's, and the ones
        this run has been on."""
        hosts = {(urlparse(u).hostname or "").lower() for u in urls_from_goal(self.cfg.goal)}
        hosts.update(self._site_notes)
        cur = (urlparse(getattr(self.surface, "url", "") or "").hostname or "").lower()
        hosts.add(cur)
        hosts.discard("")
        return {h[4:] if h.startswith("www.") else h for h in hosts}

    async def _direct(self, da: dict[str, Any], obs: Observation) -> None:
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
        if (
            kind == "click"
            and isinstance(da.get("x"), (int, float))
            and isinstance(da.get("y"), (int, float))
            and self._shot_geometry is not None
        ):
            (wx, wy, ww, wh), fw, fh = self._shot_geometry
            x = wx + float(da["x"]) * ww / fw
            y = wy + float(da["y"]) * wh / fh
            hit = obs.element_at(x, y)
            if hit is not None and is_irreversible(hit) and not self.cfg.allow_irreversible:
                self.history.append(
                    {
                        "step": len(self.history) + 1,
                        "kind": "click",
                        "action": f"planner click at ({int(x)}, {int(y)}) refused",
                        "ok": False,
                        "changed": False,
                        "diff": f"refused: {hit.label!r} looks irreversible",
                    }
                )
                return
            rec = await self.surface.execute("click_point", 
                int(x),
                int(y),
                description=f"planner click{' on ' + hit.label if hit else ''}",
                window=(wx, wy, ww, wh),
            )
            self.history.append(
                {
                    "step": len(self.history) + 1,
                    "kind": "click",
                    "action": f"planner click at ({int(x)}, {int(y)})"
                    + (f" [{hit.label}]" if hit else ""),
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
        elif kind == "type" and isinstance(da.get("text"), str):
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
            rec = await self.surface.execute("type_raw", da["text"])
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

    async def _verify(self, obs: Observation) -> dict[str, Any] | None:
        if self.llm is None or not self.cfg.verify_with_llm or self.llm.budget_left <= 0:
            return None
        await self._say("  → LLM: verifying end state")
        since = len(self.llm.stats.log)
        out = await self.llm.verify(
            goal=self.cfg.goal,
            history=self.history,
            screen_text=obs.screen_text,
            elements_table=obs.door_table(),
            window=obs.focused_window,
            windows_at_start=self._start_windows or [],
            windows_now=obs.windows,
            app_notes=self._notes,
        )
        self._log_llm(since)
        self._log({"event": "verify", "out": out})
        return out

    def _template_summary(self, obs: Observation) -> str:
        acts = [h["action"] for h in self.history if h.get("kind") not in ("wait",)]
        head = (
            f"Performed {len(acts)} actions in {obs.app_name}: " + "; ".join(acts[-6:])
            if acts
            else f"No actions were taken in {obs.app_name}"
        )
        text = obs.screen_text.replace("\n", " | ")[:300]
        return head + (f". Screen now shows: {text}" if text else ".")

    def _finish_blocked(self, obs: Observation, reason: str) -> RunResult:
        tried = f" Last sub-goal: {self._subgoal}." if self._subgoal else ""
        return self._finish(
            "blocked",
            f"Stopped: {reason}.{tried} " + self._template_summary(obs),
            screen=obs.screen_text,
        )
