"""
Generative widget tools — `show_widget` + `read_me`.

Lets agents render interactive HTML/SVG fragments inline in the
conversation. Modeled after Claude Desktop's "Imagine" MCP and the
MCP Apps SEP-1865 protocol: the tool emits a `widget_render` event
carrying the widget markup; the renderer mounts it inside a sandboxed
iframe with a pre-loaded design-system runtime (CSS variables, shape +
color classes, Tabler icon webfont, `sendPrompt` / `openLink` globals,
auto-wired `.elicit-*` form chrome).

Two tools:

  show_widget(title, widget_code, loading_messages=None)
      Render a fragment. `title` is a snake_case filename used for the
      iframe label + future download. `widget_code` is raw HTML
      (anything not starting with `<svg`) or an SVG fragment. The host
      widget shell wraps it with the runtime — agents must NOT emit
      `<!doctype>` / `<html>` / `<head>` / `<body>`.
      `loading_messages` (1-4 short strings) play during streaming /
      mount.

  read_me(modules=None)
      Returns the widget design-system spec. Agents call this once
      before their first show_widget so they know which CSS classes,
      icons, form patterns, and constraints apply. `modules` filters
      to a subset (diagram | mockup | interactive | chart | art |
      elicitation). Omit to get the consolidated spec.

The bridge supplies `session_id` + `emit_event`. The renderer keys
widgets by `tool_call_id` so they appear inline at the exact spot
where the tool was invoked.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from engine.tools import ToolDefinition, ToolTier
from engine.types import ToolResult


WidgetEventCb = Callable[[dict[str, Any]], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# Design-system spec text. Compiled lazily and cached.
# ---------------------------------------------------------------------------

_SPEC_HEADER = """\
# Freyja Widget Runtime — Design System

This document is the reference for `show_widget`. Read it once at the
start of a session before emitting your first widget. Re-read selected
modules with `read_me(modules=[…])` if you forget the rules.

## What `show_widget` does

`show_widget(title, widget_code, loading_messages=None)` mounts an HTML
or SVG fragment in a sandboxed iframe inside the chat. The runtime
provides CSS variables, pre-built classes, the Tabler icon webfont,
and two JS globals — `sendPrompt(text)` (posts a follow-up to the
chat as if the user typed it) and `openLink(url)` (opens a link
confirmation dialog). Plain `<a href>` clicks are intercepted too.

`widget_code` is a **fragment** — no `<!doctype>`, no `<html>`, no
`<head>`, no `<body>`. The shell wraps your markup; if you emit the
boilerplate the runtime drops it on the floor.

`title` is a snake_case filename (e.g. `trip_form`, `quarterly_dash`).
It's used as the iframe aria-label and the future download name.

`loading_messages` is a list of 1–4 short strings (≤ 50 chars each)
shown while the widget mounts. Optional — omit for a generic shimmer.

## Container

The widget iframe is `display: block; width: 100%` and 680px wide on
desktop, ~380px on mobile. Background is transparent on the outer
container — don't paint a backdrop or your widget will look stranded
on the chat surface. Height auto-grows to content; you do not need to
report or compute it.

## Hard constraints

You MUST obey these — violations cause silent visual breakage:

- **No** `<!DOCTYPE>` / `<html>` / `<head>` / `<body>` — fragments only
- **No** `<script>` tags inside elicitation forms; the shell auto-wires
  selections and submit
- **No** `localStorage`, `sessionStorage`, or any persistent storage
- **No** `position: fixed` — the iframe collapses to 100px
- **No** comments in markup or JS (`<!-- … -->` / `/* … */`) — they
  waste tokens and may break streaming
- **No** font-size under 11px
- **No** font-weight other than 400 or 500
- **No** gradients, shadows, blur, glow — except: one `<linearGradient>`
  per illustrative diagram, one drop-shadow inside elicitation forms
- **No** tabs, carousels, or `display: none` during streaming
- **No** nested scrollers
- Background of the outer container MUST be transparent

## Theme

The runtime ships its own CSS variables that auto-adapt to the
operator's color scheme. Always reference variables via `var(--…)`
instead of hardcoding colors:

  Backgrounds:  --color-background-primary, -secondary, -tertiary
                --color-background-info, -danger, -success, -warning
  Text:         --color-text-primary, -secondary, -tertiary
                --color-text-info, -danger, -success, -warning, -inverse
  Borders:      --color-border-primary, -secondary, -tertiary
                --color-border-info, -danger, -success, -warning
  Accent:       --color-accent (steel blue — Freyja's signature)
  Fonts:        --font-sans, --font-serif, --font-mono
  Radii:        --border-radius-sm 4px, -md 8px, -lg 12px, -xl 16px

For SVG, prefer the short aliases: `var(--p)` text-primary,
`var(--s)` secondary, `var(--t)` tertiary, `var(--bg2)`
background-secondary, `var(--b)` border-secondary.

## Icons

The Tabler outline webfont (5,800+ icons) is preloaded. Use it like:

    <i class="ti ti-home" aria-hidden="true"></i>
    <i class="ti ti-trending-up"></i>
    <i class="ti ti-chevron-right"></i>

Icons inherit `color` and `font-size` from their parent — that's how
the dashboard mockup makes a delta arrow green/red just by recoloring
the wrapping `<span>`. Filled variants ship in the same font as
`ti ti-home-filled`. Icon-only buttons need `aria-label`; decorative
icons take `aria-hidden="true"`.

## SVG pre-built classes (use on SVG elements)

Text:
  .t   sans-serif 14px, color text-primary
  .ts  sans-serif 12px, color text-secondary
  .th  sans-serif 14px, weight 500, color text-primary

Shape:
  .box     neutral fill background-secondary, stroke border-secondary
  .node    clickable group — hover bg shift; pair with onclick or
           data-prompt to send a follow-up
  .arr     1.5px arrow line, color border-primary, requires
           marker-end="url(#arr)" — the shell injects the def
  .leader  dashed 0.5px hairline, color border-tertiary

Color ramps (apply to `<g>`, `<rect>`, `<circle>`, `<ellipse>` —
never `<path>`):
  .c-purple .c-teal .c-coral .c-pink .c-gray
  .c-blue .c-green .c-amber .c-red

Each ramp exposes 7 stops as nested classes: `c-blue-50`, `-100`,
`-200`, `-400`, `-600`, `-800`, `-900`. Use `-400` for fills and
`-600` for accents.

## Form elements (HTML)

`<input>`, `<select>`, `<textarea>`, `<button>`, `<input type="range">`
are pre-styled. Just emit them — no inline `style=`. Selection state
on form widgets is always blue regardless of any accent.

## Globals you can call from JS

  sendPrompt(text: string) — posts `text` as the user's next message
  openLink(url: string)    — opens the link confirmation dialog

Wire these from `onclick`, not `addEventListener` in a `<script>`, so
they survive partial streaming. Both also work via attribute:

    <button data-prompt="Show me Q4 numbers" class="btn">Q4</button>
    <a href="https://example.com">Example</a>   (intercepted by shell)
"""


_SPEC_ELICITATION = """\
## Module: elicitation (form-driven follow-ups)

When a slash command or skill needs structured arguments, render an
`elicit` form. The shell auto-wires selection, multi-select, "Other",
slider readouts, and submit. **Zero JS in your fragment** — the shell
handles every interaction.

### Skeleton

    <div class="elicit" data-subject="Trip details">
      <header class="elicit-header">
        <i class="ti ti-file-text" aria-hidden="true"></i>
        <span>Trip details</span>
      </header>

      <div class="elicit-body">
        <div class="elicit-group" data-name="destination">
          <label class="elicit-question">Where to?</label>
          <input type="text" placeholder="Tokyo" />
        </div>

        <div class="elicit-group" data-name="transport" data-multi="true">
          <label class="elicit-question">Getting around</label>
          <div class="elicit-cards">
            <button class="elicit-card" data-value="Flights">
              <i class="ti ti-plane"></i>
              <span>Flights</span>
              <small>Long hops</small>
            </button>
            <button class="elicit-card" data-value="Walking and transit">
              <i class="ti ti-walk"></i>
              <span>Walking and transit</span>
              <small>City</small>
            </button>
          </div>
        </div>

        <div class="elicit-group" data-name="party">
          <label class="elicit-question">Who's going?</label>
          <div class="elicit-pills">
            <button class="elicit-pill" data-value="Solo">Solo</button>
            <button class="elicit-pill" data-value="Couple">Couple</button>
            <button class="elicit-pill" data-value="Family">Family</button>
            <button class="elicit-pill" data-other>Other</button>
          </div>
          <input class="elicit-other" placeholder="Group of 6, mostly kids" hidden />
        </div>

        <div class="elicit-group" data-name="budget_usd">
          <label class="elicit-question">Budget (USD)</label>
          <input type="range" min="500" max="10000" step="100" value="3000" />
        </div>

        <div class="elicit-group" data-name="output_format">
          <label class="elicit-question">Output format</label>
          <div class="elicit-tiles">
            <button class="elicit-tile" data-value="Itinerary doc">
              <svg viewBox="0 0 32 32"><rect class="c-blue-400" x="6" y="4" width="20" height="24" rx="2" /></svg>
              <span>Itinerary doc</span>
            </button>
            <button class="elicit-tile" data-value="Day-by-day cards">
              <svg viewBox="0 0 32 32"><rect class="c-blue-400" x="4" y="8" width="10" height="16" rx="2"/><rect class="c-blue-400" x="18" y="8" width="10" height="16" rx="2"/></svg>
              <span>Day-by-day cards</span>
            </button>
          </div>
        </div>
      </div>

      <footer class="elicit-footer">
        <button type="button" class="elicit-skip">Skip</button>
        <button type="button" class="elicit-submit">Continue</button>
      </footer>
    </div>

### Input types

  <input type="text" />                       free text
  <input type="date" class="elicit-date" />   date picker
  <textarea class="elicit-textarea"></textarea>  long form
  <input type="range" min=… max=… value=… />  slider; value auto-shown
  .elicit-pills + .elicit-pill[data-value]    short labels (≤4 words)
  .elicit-cards + .elicit-card                Tabler icon + title + sub
  .elicit-tiles + .elicit-tile                tiny inline SVG + label
  .elicit-files                               file picker with fallback

### Selection rules

- Selected state is always blue. Accent overrides
  (`data-accent="warning|danger|success"`) only apply to the
  UNSELECTED state.
- `data-multi="true"` on the group → multi-select; otherwise single
- `data-other` on a pill/card → reveals the adjacent `.elicit-other`
  input when selected
- `.elicit-group[data-name]` is the field name in the submitted output

### Submit format

On Continue, the shell sends a message of the form

    {subject} — {Field 1}: {value 1} · {Field 2}: {value 2}

`subject` comes from `.elicit[data-subject]`. Each `data-name` becomes
a label (snake → Title Case). Multi-select values join with `, `.
Textareas with newlines flatten to ` / `. Values 81–200 chars are
quoted; values over 200 chars are appended under
`--- Full content ---`. On Skip the shell sends
`(Skipped the form — proceed with defaults or ask me in plain text)`
and you should fall back to a prose follow-up.
"""


_SPEC_PATTERNS = """\
## Module: mockup (UI components, dashboards, metric cards)

Use editorial layout (no wrapper, prose flows) for casual content.
Use a single raised card for self-contained data records. Use a
metric-card grid for KPI dashboards.

### Metric card

    <div class="metric">
      <div class="metric-label">Revenue (MRR)</div>
      <div class="metric-value">$42,118</div>
      <div class="delta delta-up">
        <i class="ti ti-trending-up"></i>
        <span>+12% MoM</span>
      </div>
    </div>

`.delta-up` is green, `.delta-down` is red. The trending-up/-down icon
inherits the color from its `.delta` parent — same icon, two colors.

Grid:

    <div class="metric-grid">
      <div class="metric">…</div>
      <div class="metric">…</div>
    </div>

### Card

    <div class="card">
      <h3>Section title</h3>
      <p>Body text…</p>
    </div>

White-bg raised cards with 1rem 1.25rem padding,
`var(--border-radius-lg)` corners.

### Compare options

Side-by-side cards with a 2px `border-info` accent on the recommended
one — the only place 2px borders are allowed (everything else is 0.5px
hairline):

    <div class="compare-grid">
      <div class="compare-card">…</div>
      <div class="compare-card recommended">…</div>
    </div>


## Module: diagram

Reference diagrams use viewBox width 680 (load-bearing — fixed unit
ratio with the iframe width). Every `<text>` needs a class (`.t`,
`.ts`, or `.th`). Use only 14px and 12px text. Arrows require
`marker-end="url(#arr)"`; the shell already injects the `<marker
id="arr">` def — don't redeclare it.

Flowchart:

    <svg viewBox="0 0 680 240" width="100%">
      <g class="node" data-prompt="Tell me about Step A">
        <rect class="box" x="20" y="80" width="120" height="60" rx="6"/>
        <text class="th" x="80" y="115" text-anchor="middle">Step A</text>
      </g>
      <line class="arr" x1="140" y1="110" x2="220" y2="110" marker-end="url(#arr)"/>
      <g class="node">
        <rect class="box" x="220" y="80" width="120" height="60" rx="6"/>
        <text class="th" x="280" y="115" text-anchor="middle">Step B</text>
      </g>
    </svg>


## Module: chart

For Chart.js: wrap `<canvas>` in a fixed-height `position: relative`
div, height ≥ (bars × 40) + 80 for horizontal bar charts. Set height
on the wrapper, never the canvas. Canvas can't read CSS variables —
use hardcoded hex. Build a custom HTML legend below the canvas; the
built-in legend is disabled by default. Every canvas needs
`role="img"` + `aria-label`.

Chart.js 4.4.1 is reachable via `https://cdn.jsdelivr.net/npm/chart.js@4.4.1`.


## Module: art

The one place custom `<style>` color blocks are allowed. Layer
overlapping opaque shapes for depth, no gradients except one
`<linearGradient>` per illustration. Still bound by no-font-under-11px
and viewBox safety. Use `prefers-color-scheme` for dark/light variants.


## When to use which

  "What is X?"               → editorial mockup with one card
  "How does X work?"         → illustrative diagram
  "What's the architecture?" → structural flowchart diagram
  "Show me the numbers"      → metric grid
  "I need to gather inputs"  → elicitation form
  "Compare A vs B"           → compare-options pattern
  "Plot this data"           → chart


## Allowed external resources (CDN allowlist)

  cdnjs.cloudflare.com
  esm.sh
  cdn.jsdelivr.net
  unpkg.com

Anything else fails silently. Known good imports: Chart.js 4.4.1,
D3 7.8.5 + topojson 3.0.2, Mermaid 11 via esm.sh, Three.js r128.
"""


_SPEC_EQUATION = """\
## Module: equation (showcase LaTeX with KaTeX)

Inline math in normal prose already renders automatically — just write
`$\\pi_\\theta$` or `$$…$$` in your chat reply and the markdown layer
typesets it. Use a **widget** only when an equation deserves to be a
standalone, framed artifact: a key result, a derivation, or a formula
whose symbols you want to break down term by term (the "read the
pieces" explainer pattern).

The runtime auto-loads KaTeX when your fragment contains `$$`, `$`,
`\\(`, `\\[`, or an `.eqn` card, and auto-renders every delimiter after
mount. You write raw TeX — no JS, no manual render call.

### Delimiters

  $$ … $$   display (centered block)
  \\[ … \\]   display
  $ … $     inline
  \\( … \\)   inline

### Equation card

    <div class="eqn">
      <div class="eqn-title">
        <i class="ti ti-math-function" aria-hidden="true"></i>
        <span>Autoregressive policy</span>
      </div>
      <div class="eqn-main">
        $$\\pi_\\theta(y \\mid x) = \\prod_{t=1}^{|y|} \\pi_\\theta(y_t \\mid x, y_{\\lt t})$$
      </div>
      <p class="eqn-note">The chain rule of probability, applied left to right.</p>
      <div class="eqn-terms">
        <div class="eqn-term">
          <div class="eqn-term-sym">$\\theta$</div>
          <div class="eqn-term-def">The model's parameters — the weights you train.</div>
        </div>
        <div class="eqn-term">
          <div class="eqn-term-sym">$y_{\\lt t}$</div>
          <div class="eqn-term-def">Everything before step <code>t</code> — the prefix so far.</div>
        </div>
        <div class="eqn-term">
          <div class="eqn-term-sym">$\\pi_\\theta(y_t \\mid x, y_{\\lt t})$</div>
          <div class="eqn-term-def">The next-token distribution: a softmax over the vocabulary.</div>
        </div>
      </div>
        </div>
      </div>
    </div>

`.eqn-title` (with a Tabler icon) and `.eqn-note` / `.eqn-terms` are all
optional — a bare `.eqn` with just an `.eqn-main` is a clean framed
formula. The term breakdown reads symbol-on-the-left, gloss-on-the-right;
keep glosses to one short sentence and wrap literal token names in
`<code>`.

### Constraints

- **Never put a literal `<` or `>` inside widget TeX.** The browser parses
  `y_{<t}` as an HTML tag and corrupts the widget before KaTeX runs. Write
  `\\lt`, `\\gt`, `\\leq`, `\\geq` instead (`y_{\\lt t}`), and use `\\mid`
  for the conditioning bar. This applies ONLY to widget markup — inline
  prose math in a normal reply handles `<`/`>` fine.
- Escape backslashes for the tool call as usual (`\\pi`, `\\theta`,
  `\\prod`) — what reaches the widget must be valid TeX.
- KaTeX (not full LaTeX): supported macros are the standard math set.
  Avoid `\\usepackage`, `\\begin{document}`, tikz, or text-mode layout.
- Malformed TeX renders in a soft error color instead of throwing — it
  won't break the rest of the widget, but check your output.
- Don't wrap `$$` inside `<pre>`/`<code>` — those tags are skipped by
  the auto-renderer (so you can still show TeX *source* if you want).
"""

_SPEC_FOOTER = """\
## Cheat sheet

Before your first widget, call:

    read_me(modules=["mockup", "elicitation"])

Then emit:

    show_widget(
      title="kpi_dash",
      widget_code="<div class='metric-grid'>…</div>",
      loading_messages=["Crunching numbers…", "Stacking deltas…"],
    )

Keep the widget tight (under ~2,000 chars when you can). If the data
flow needs more than one widget, post several `show_widget` calls in
sequence — each is independent.
"""


_MODULE_SECTIONS: dict[str, str] = {
    "mockup": _SPEC_PATTERNS,
    "interactive": _SPEC_PATTERNS,
    "diagram": _SPEC_PATTERNS,
    "chart": _SPEC_PATTERNS,
    "art": _SPEC_PATTERNS,
    "elicitation": _SPEC_ELICITATION,
    "equation": _SPEC_EQUATION,
}


def _compile_spec(modules: list[str] | None) -> str:
    if not modules:
        return "\n\n".join(
            [_SPEC_HEADER, _SPEC_ELICITATION, _SPEC_PATTERNS, _SPEC_EQUATION, _SPEC_FOOTER]
        )
    seen: set[str] = set()
    chunks = [_SPEC_HEADER]
    for m in modules:
        section = _MODULE_SECTIONS.get(m)
        if section is None or section in seen:
            continue
        seen.add(section)
        chunks.append(section)
    chunks.append(_SPEC_FOOTER)
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ReadWidgetSpecTool:
    """Returns the widget runtime design-system spec.

    Agents call this once per session before their first `show_widget`
    so they know the available classes, icons, form patterns, and
    constraints. Filterable by module to keep context lean."""

    def __init__(self) -> None:
        pass

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="widget_spec",
            summary="Read the widget design-system spec (classes, form chrome, constraints)",
            tier=ToolTier.WARM,
            description=(
                "Return the Freyja widget runtime spec — design-system CSS variables, "
                "pre-built SVG classes, Tabler icon usage, elicitation form chrome, "
                "and hard constraints. Call this once before your first `show_widget` "
                "so your fragments respect the runtime contract. `modules` filters "
                "the response (e.g. `[\"elicitation\"]` for a form-only refresher)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "modules": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "diagram",
                                "mockup",
                                "interactive",
                                "chart",
                                "art",
                                "elicitation",
                                "equation",
                            ],
                        },
                        "description": (
                            "Optional subset of modules to return. Omit to get the "
                            "consolidated spec covering all modules."
                        ),
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        raw_modules = arguments.get("modules") or []
        modules = [str(m).strip().lower() for m in raw_modules if str(m).strip()]
        spec = _compile_spec(modules or None)
        return ToolResult(call_id=call_id, content=spec, is_error=False)


class ShowWidgetTool:
    """Renders a generative UI fragment in the conversation pane.

    Emits a `widget_render` event keyed by `call_id` so the renderer
    can mount the iframe inline at the exact spot the agent invoked the
    tool. The tool's textual return is a confirmation for the model —
    the markup itself lives in the event."""

    def __init__(
        self,
        *,
        session_id: str,
        emit_event: WidgetEventCb | None = None,
    ) -> None:
        self._session_id = session_id
        self._emit_event = emit_event

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="show_widget",
            summary="Render an interactive HTML/SVG widget inline in the chat",
            tier=ToolTier.HOT,
            description=(
                "Mount a generative UI fragment in the conversation pane. The runtime "
                "provides CSS variables, pre-built SVG/CSS classes, the Tabler icon "
                "webfont, and JS globals `sendPrompt(text)` + `openLink(url)`. The "
                ".elicit-* form chrome auto-wires selection, multi-select, Other "
                "reveal, and submit — no <script> needed inside elicitation forms.\n\n"
                "Use this for dashboards (metric grids), structured input forms "
                "(elicitations), diagrams, mockups, charts, small illustrations, and "
                "showcase LaTeX equations (KaTeX, via the `equation` module — note "
                "inline math in normal prose already renders without a widget). "
                "Call `widget_spec` first so your fragment respects the runtime "
                "contract.\n\n"
                "`widget_code` is a FRAGMENT — no <!doctype>, <html>, <head>, or "
                "<body>. The shell wraps it. Hard constraints: no localStorage, no "
                "position:fixed, no <script> in elicit forms, no font-size <11px, "
                "no font-weight other than 400/500, no gradients/shadows/blur except "
                "documented exceptions, transparent outer background."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Snake_case identifier used as the iframe label and "
                            "future download name. Keep it short — `trip_form`, "
                            "`kpi_dash`, `architecture_diagram`."
                        ),
                    },
                    "widget_code": {
                        "type": "string",
                        "description": (
                            "Raw HTML or SVG fragment. SVG fragments must start "
                            "with `<svg`. HTML fragments are anything else. NO "
                            "<!doctype>, <html>, <head>, or <body> tags."
                        ),
                    },
                    "loading_messages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional 1-4 short strings (≤50 chars each) shown "
                            "while the widget mounts. Omit for a generic shimmer."
                        ),
                    },
                },
                "required": ["title", "widget_code"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        title = str(arguments.get("title") or "").strip()
        widget_code = str(arguments.get("widget_code") or "")
        loading_raw = arguments.get("loading_messages") or []
        loading_messages = [
            str(m).strip()
            for m in loading_raw
            if isinstance(m, (str, int, float)) and str(m).strip()
        ][:4]

        if not title:
            return ToolResult(
                call_id=call_id,
                content="Error: `title` is required (snake_case short identifier).",
                is_error=True,
            )
        if not widget_code:
            return ToolResult(
                call_id=call_id,
                content="Error: `widget_code` is required (HTML or SVG fragment).",
                is_error=True,
            )

        normalized = _normalize_title(title)
        kind = _classify_kind(widget_code)
        emitted_at = int(time.time() * 1000)

        if self._emit_event is not None:
            payload: dict[str, Any] = {
                "type": "widget_render",
                "sessionId": self._session_id,
                "toolCallId": call_id,
                "widget": {
                    "id": f"widget:{call_id}",
                    "title": normalized,
                    "kind": kind,
                    "code": widget_code,
                    "loadingMessages": loading_messages,
                    "createdAt": emitted_at,
                },
            }
            try:
                result_or_coro = self._emit_event(payload)
                if asyncio.iscoroutine(result_or_coro):
                    await result_or_coro
            except Exception:
                pass

        char_count = len(widget_code)
        return ToolResult(
            call_id=call_id,
            content=(
                f"Rendered widget `{normalized}` ({kind}, {char_count} chars) "
                f"in the conversation pane. The user can interact with it; any "
                f"`sendPrompt(...)` calls or elicitation form submits will arrive "
                f"as their next chat message."
            ),
            is_error=False,
        )


def _normalize_title(raw: str) -> str:
    """Snake_case-normalize the title. Strip extension, lowercase,
    collapse whitespace + dashes to underscores, drop non-[a-z0-9_]."""
    base = raw.strip().lower()
    if base.endswith(".html") or base.endswith(".svg"):
        base = base.rsplit(".", 1)[0]
    out_chars: list[str] = []
    last_underscore = False
    for ch in base:
        if ch.isalnum():
            out_chars.append(ch)
            last_underscore = False
        elif ch in {" ", "-", "_", ".", "/"}:
            if not last_underscore:
                out_chars.append("_")
                last_underscore = True
    cleaned = "".join(out_chars).strip("_")
    return cleaned or "widget"


def _classify_kind(code: str) -> str:
    """Tell HTML from SVG by sniffing the first non-whitespace token.
    Used by the renderer to pick the appropriate iframe wrapper."""
    stripped = code.lstrip()
    if stripped.startswith("<svg"):
        return "svg"
    return "html"
