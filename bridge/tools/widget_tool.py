"""
Generative widget tools — `show_widget` + `widget_spec`.

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

  widget_spec(modules=None)
      Returns the widget design-system spec. Agents call this once
      before their first show_widget so they know which CSS classes,
      icons, form patterns, layout rules and constraints apply.
      `modules` filters to a subset (mockup | flow | table | diagram |
      interactive | chart | art | elicitation | equation). Omit to get
      the consolidated spec.

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
modules with `widget_spec(modules=[…])` if you forget the rules.

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

The widget sits flush left in the message, like a paragraph, and is
never wider than 760px; SVG keeps a 680px design width. There is no
background: the transcript is the ground, in the dark shell and on the
light paper shell alike. Do not paint one, and do not wrap the whole
widget in a `.card`. Height grows to the content; you never compute it.

## Type

Everything is set in Geist Mono (the chat's own face) at 12.5px, and
`h1`–`h3` switch to the Fraunces serif automatically. Do not override
the fonts; do not use hex colours; do not go below 11px.

Monospace has one consequence you must plan for: **every character is
0.6 × the font size wide**. At 13px that is 7.8px per character, so a
24-character label is 187px. See "Layout that survives real text".

## Hard constraints

You MUST obey these — violations cause silent visual breakage:

- **No** `<!DOCTYPE>` / `<html>` / `<head>` / `<body>` — fragments only
- **No** `<script>` tags inside elicitation forms; the shell auto-wires
  selections and submit
- **No** `localStorage`, `sessionStorage`, or any persistent storage
- **No** `position: fixed` — the runtime turns it into `relative`
- **No** comments in markup or JS (`<!-- … -->` / `/* … */`) — they
  waste tokens and may break streaming
- **No** font-size under 11px
- **No** font-weight other than 400 or 500
- **No** gradients, shadows, blur, glow — except: one `<linearGradient>`
  per illustrative diagram, one drop-shadow inside elicitation forms
- **No** tabs, carousels, or `display: none` during streaming
- **No** nested scrollers
- **No** fixed pixel widths or heights on anything that holds text
- **No** `white-space: nowrap` — the runtime undoes it
- Background of the outer container MUST be transparent

## Layout that survives real text

The runtime lets grid and flex children shrink, breaks long words,
caps media and tables at the frame, keeps SVG at its aspect ratio and
squeezes an SVG label that overruns its box. It cannot rescue a layout
whose numbers were wrong to begin with, so:

1. **Reach for the runtime classes first** (catalogue below) and write
   a `<style>` block only for what they do not cover. Keep it short.
   Never restyle `h1`–`h3`, the body font, or colours.
2. **Flowcharts and process diagrams are HTML** (`.flow`), not SVG,
   unless the drawing needs real geometry (a scatter, a map, overlapping
   shapes). HTML nodes size to their words and wrap; SVG text cannot.
3. **In SVG, size every box from its text**: width ≥ characters × 0.6 ×
   font-size + 24. Keep labels under 20 characters and move anything
   longer into prose under the diagram. Put edge labels in the gap
   between boxes, never over a box. A label the runtime had to squeeze
   reads worse than a short one.
4. **Tables are plain `<table>` markup** with a `<thead>`: headers stay
   on one line and cells wrap. Put `class="num"` on numeric columns.
5. **Grids are the runtime's** (`.metric-grid`, `.card-grid`, `.grid-2`,
   `.grid-3`, `.compare-grid`): they collapse to one column when narrow.
   Hand-written `grid-template-columns: 300px 300px` does not.
6. **One idea per widget.** A widget longer than one screen should be
   two widgets or a widget plus prose. Under ~2,000 characters is right.
7. **Prefer prose when a widget adds nothing.** Two rows of two cells is
   a sentence; a single number is a sentence. Widgets earn their place
   with structure the reader scans: comparisons, flows, ledgers, forms.

## Theme

The runtime ships its own CSS variables that auto-adapt to the
operator's color scheme. Always reference variables via `var(--…)`
instead of hardcoding colors:

  Backgrounds:  --color-background-primary, -secondary, -tertiary
                --color-background-info, -danger, -success, -warning
  Washes:       --wash (a breath of ink on the ground), --wash-2
  Edges:        --hair (0.5px hairline), --hair-soft
  Text:         --color-text-primary, -secondary, -tertiary
                --color-text-info, -danger, -success, -warning, -inverse
  Borders:      --color-border-primary, -secondary, -tertiary
                --color-border-info, -danger, -success, -warning
  Accent:       --color-accent (steel blue on dark, deep blue on paper)
  Fonts:        --font-sans (Geist Mono), --font-serif (Fraunces), --font-mono
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
the metric card makes a delta arrow green/red just by recoloring the
wrapping `<span>`. Filled variants ship in the same font as
`ti ti-home-filled`. Icon-only buttons need `aria-label`; decorative
icons take `aria-hidden="true"`.

## HTML class catalogue

Type
  .kicker        small uppercase line above a title ("Release · week 42")
  h1 h2 h3       Fraunces serif, 24 / 19 / 16px; h4 is a 13px mono label
  p small code   body, aside, inline code (accent)
  .muted .num    tertiary text; tabular right-aligned numbers
  .tone-info .tone-success .tone-warning .tone-danger   text colour,
                 and the variant for .callout, .tag, .flow-node, .bars .bar

Surfaces
  .card          a wash with a hairline, 10px radius; for one record or
                 for peers inside .card-grid — never around the whole widget
  .card-grid     auto-fit cards, 220px minimum each
  .grid-2 .grid-3   auto-fit columns (240px / 170px minimum)
  .callout       left rule + wash; add a tone for info/success/warning/danger
  .tag           small pill; add a tone

Data
  .metric-grid > .metric > .metric-label .metric-value .metric-note
                 editorial figures with a hairline above each; .delta
                 .delta-up / .delta-down / .delta-flat for the change line
  table thead th td   ledger table: uppercase headers, hairlines, wrapping
                 cells; th.num / td.num right-align numbers
  .bars          label · bar · value rows:
                 <span class="lab">…</span><div class="bar"><i style="width:62%"></i></div><span class="val">62%</span>
  .kv            <dl class="kv"><dt>key</dt><dd>value</dd>…</dl>
  .steps         <ol class="steps"><li><b>Title</b> detail…</li></ol>, numbered

Flow
  .flow > .flow-row > .flow-node     nodes wrap and size to their text;
                 the runtime draws the → between nodes in a row
  .flow-node > b + span              title line and detail line
  .flow-edge                         a ↓ transition between rows, with its label
  .flow-node[data-prompt]            clickable, sends a follow-up

Compare
  .compare-grid > .compare-card(.recommended)

Actions
  button, button.primary, [data-prompt="…"]

## SVG pre-built classes (use on SVG elements)

Text (monospace — 0.6 × size per character):
  .t   13px, color text-primary
  .ts  11.5px, color text-secondary
  .th  500 13px, color text-primary

Shape:
  .box     neutral fill (wash), stroke border-secondary
  .node    clickable group — hover bg shift; pair with onclick or
           data-prompt to send a follow-up
  .arr     1.25px arrow line, color border-primary, requires
           marker-end="url(#arr)" — the shell injects the def
  .leader  dashed 0.5px hairline, color border-tertiary

Color ramps (apply to `<g>`, `<rect>`, `<circle>`, `<ellipse>` —
never `<path>`):
  .c-purple .c-teal .c-coral .c-pink .c-gray
  .c-blue .c-green .c-amber .c-red

Each ramp exposes 7 stops as nested classes: `c-blue-50`, `-100`,
`-200`, `-400`, `-600`, `-800`, `-900`. Use `-400` for fills and
`-600` for accents. The ramps flip with the shell, so a `-50` wash
stays a wash on paper and a `-900` stays the strongest ink.

## Form elements (HTML)

`<input>`, `<select>`, `<textarea>`, `<button>`, `<input type="range">`
are pre-styled. Just emit them — no inline `style=`. Selection state
on form widgets is always blue regardless of any accent.

## Globals you can call from JS

  sendPrompt(text: string) — posts `text` as the user's next message
  openLink(url: string)    — opens the link confirmation dialog

Wire these from `onclick`, not `addEventListener` in a `<script>`, so
they survive partial streaming. Both also work via attribute:

    <button data-prompt="Show me Q4 numbers">Q4</button>
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
## Module: mockup (figures, ledgers, flows, cards)

Editorial by default: the widget flows like a paragraph, with a kicker
and a serif title, and structure comes from the classes below rather
than from boxes. Wrap peers in cards; never the whole widget.

### Figures

    <p class="kicker">Release readiness · week 42</p>
    <h2>Where the launch stands</h2>
    <div class="metric-grid">
      <div class="metric">
        <div class="metric-label">Revenue (MRR)</div>
        <div class="metric-value">$42,118</div>
        <div class="delta delta-up"><i class="ti ti-trending-up"></i><span>+12% MoM</span></div>
      </div>
      <div class="metric">
        <div class="metric-label">Rollout</div>
        <div class="metric-value">12%</div>
        <div class="metric-note">canary cohort</div>
      </div>
    </div>

### Ledger table

    <table>
      <thead><tr><th>Gate</th><th>Status</th><th class="num">Days late</th></tr></thead>
      <tbody>
        <tr><td>Performance budget</td><td class="tone-success">passed · p95 under 180 ms</td><td class="num">0</td></tr>
        <tr><td>Data migration</td><td class="tone-warning">blocked on schema review</td><td class="num">3</td></tr>
      </tbody>
    </table>

Cells wrap; headers do not. Keep a table to five columns or fewer.

### Share of something

    <div class="bars">
      <span class="lab">LLM fallback</span><div class="bar tone-warning"><i style="width:87%"></i></div><span class="val">165 s · 87%</span>
      <span class="lab">everything else</span><div class="bar"><i style="width:13%"></i></div><span class="val">24 s</span>
    </div>

The width on the inner `<i>` is the only inline style you need.

### Facts, steps, callouts, tags

    <dl class="kv"><dt>Owner</dt><dd>Priya N.</dd><dt>Target</dt><dd>Thu 30 Oct</dd></dl>

    <ol class="steps">
      <li><b>Hold the canary at 12%</b> until the dry run passes twice.</li>
      <li><b>Widen to 50%</b> on Monday if error rates stay flat.</li>
    </ol>

    <div class="callout tone-warning">Turning the LLM off also skips its completion check.</div>

    <span class="tag tone-success">on track</span> <span class="tag">infra</span>

### Cards and compare

    <div class="card-grid">
      <div class="card"><h4>Separate payload from instructions</h4><p>Give exact text an ID…</p></div>
      <div class="card"><h4>Keep the default loop LLM-free</h4><p>Return a specific blocker…</p></div>
    </div>

    <div class="compare-grid">
      <div class="compare-card"><h4>Ship Thursday</h4><p>…</p></div>
      <div class="compare-card recommended"><h4>Ship Monday</h4><p>…</p></div>
    </div>

`.recommended` is the only place a 1px accent ring is allowed;
everything else is a 0.5px hairline.


## Module: flow (HTML flowcharts)

Use `.flow` for any "how does it work" diagram: a pipeline, a decision
loop, a request path. Nodes size to their words and wrap; the runtime
draws the arrows. One `.flow-row` is one left-to-right stage, and a
`.flow-edge` between rows carries the transition and its label.

    <div class="flow">
      <div class="flow-row">
        <div class="flow-node"><b>Your goal + current page</b><span>Arc DOM; native apps use AX</span></div>
        <div class="flow-node"><b>Read and label controls</b><span>[38] button · [43] text field</span></div>
        <div class="flow-node tone-info"><b>Jev selects an action</b><span>click / type / key / done / help</span></div>
      </div>
      <div class="flow-edge">passes the confidence, target and permission checks</div>
      <div class="flow-row">
        <div class="flow-node"><b>Execute through code</b><span>Click, fill or press a key</span></div>
        <div class="flow-node"><b>Read again; compare changes</b></div>
      </div>
      <div class="flow-edge">fails a check</div>
      <div class="flow-row">
        <div class="flow-node tone-warning"><b>Optional LLM assistance</b><span>Replan, compose text, verify</span></div>
        <div class="flow-node"><b>Or stop immediately</b></div>
      </div>
    </div>

Rules: three or four nodes per row at most; a node is a title line plus
one short detail line; branches are separate rows with a `.flow-edge`
each; a node with `data-prompt="…"` opens a follow-up when clicked.


## Module: diagram (SVG)

SVG is for geometry: scatter plots, maps, nested shapes, anything a
flow cannot express. Reference diagrams use viewBox width 680
(load-bearing — fixed unit ratio with the frame width). Every `<text>`
needs a class (`.t`, `.ts`, or `.th`). Arrows require
`marker-end="url(#arr)"`; the shell already injects the `<marker
id="arr">` def — don't redeclare it.

Text is monospace and cannot wrap, so size boxes from their labels:
width ≥ characters × 7.8 + 24 for `.t`/`.th` (13px), characters × 6.9
+ 24 for `.ts` (11.5px). Keep labels under 20 characters; longer
copy goes into prose below the drawing. Edge labels sit in the gap
between boxes. The runtime shrinks and squeezes a label that still
overruns its box, but plan so it never has to.

    <svg viewBox="0 0 680 160" width="100%">
      <g class="node" data-prompt="Tell me about Step A">
        <rect class="box" x="20" y="50" width="150" height="60" rx="6"/>
        <text class="th" x="95" y="85" text-anchor="middle">Step A</text>
      </g>
      <line class="arr" x1="170" y1="80" x2="250" y2="80" marker-end="url(#arr)"/>
      <g class="node">
        <rect class="box" x="250" y="50" width="150" height="60" rx="6"/>
        <text class="th" x="325" y="85" text-anchor="middle">Step B</text>
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

  "What is X?"               → editorial: kicker, title, prose, maybe one card
  "How does X work?"         → .flow
  "What's the architecture?" → .flow, or SVG when the geometry matters
  "Show me the numbers"      → .metric-grid
  "Where did the time go?"   → .bars
  "What happened, per item?" → table
  "What are the facts?"      → .kv
  "What are the steps?"      → .steps
  "I need to gather inputs"  → elicitation form
  "Compare A vs B"           → .compare-grid
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

    widget_spec(modules=["mockup", "flow"])

Then emit:

    show_widget(
      title="launch_status",
      widget_code="<p class='kicker'>Release · week 42</p><h2>Where the launch stands</h2><div class='metric-grid'>…</div>",
      loading_messages=["Crunching numbers…", "Stacking deltas…"],
    )

Keep the widget tight (under ~2,000 chars when you can) and let it
flow: no outer card, no fixed widths, no nowrap, flows in HTML. If the
data needs more than one widget, post several `show_widget` calls in
sequence — each is independent.
"""


_MODULE_SECTIONS: dict[str, str] = {
    "mockup": _SPEC_PATTERNS,
    "flow": _SPEC_PATTERNS,
    "table": _SPEC_PATTERNS,
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
                                "flow",
                                "table",
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
                "<body>. The shell wraps it. The widget floats on the transcript: "
                "no background, no outer card. Hard constraints: no localStorage, no "
                "position:fixed, no <script> in elicit forms, no font-size <11px, "
                "no font-weight other than 400/500, no gradients/shadows/blur except "
                "documented exceptions, no fixed pixel widths on anything holding "
                "text, no white-space:nowrap. Flowcharts are HTML (`.flow`), not "
                "SVG; SVG text is monospace (0.6em per character) and cannot wrap, "
                "so size every box from its label."
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
