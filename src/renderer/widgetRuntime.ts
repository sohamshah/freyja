/**
 * Widget runtime — builds the iframe HTML for a generative-UI widget.
 *
 * Mirrors Claude Desktop's "Imagine" runtime contract: pre-loaded CSS
 * variables, pre-built SVG/CSS classes, Tabler icon webfont,
 * `sendPrompt()` / `openLink()` JS globals, and auto-wired `.elicit-*`
 * form chrome. The bridge's `show_widget` tool emits a `widget_render`
 * event with the agent's fragment; the Widget component feeds that
 * fragment into `buildWidgetHtml()` and uses the result as the
 * iframe `srcdoc`.
 *
 * Wire protocol (postMessage between parent ↔ iframe) borrows from
 * MCP Apps SEP-1865:
 *   parent → iframe: { type: 'ui/initialize', hostContext: {…} }
 *   parent → iframe: { type: 'ui/theme', theme: 'dark' | 'light' }   when the shell flips
 *   iframe → parent: { type: 'ui/ready' }                  on load
 *   iframe → parent: { type: 'ui/resize', height: number } on content reflow
 *   iframe → parent: { type: 'ui/message', text: string }  from sendPrompt / elicit submit
 *   iframe → parent: { type: 'ui/open-link', url: string } from anchor / openLink()
 *
 * Identity-check `event.source === iframe.contentWindow` on every
 * inbound message — the iframe has opaque origin (sandbox without
 * `allow-same-origin`), so origin-checking is useless.
 */

export interface WidgetBuildOptions {
  /** Snake_case identifier used as the iframe aria-label + html title. */
  title: string
  /** Agent-emitted HTML or SVG fragment. */
  code: string
  /** Sniffed by the bridge — 'svg' if it opens with <svg, else 'html'. */
  kind: 'html' | 'svg'
  /** Optional 1-4 loading strings shown by the parent while the
   *  iframe streams. Pass-through only; not consumed inside iframe. */
  loadingMessages?: string[]
  /** Shell theme at build time. The parent posts `ui/theme` when it
   *  flips later, so a mounted widget re-skins without a reload. */
  theme?: 'dark' | 'light'
}

/** Build the full iframe srcdoc for a widget. The result is meant to
 *  be passed verbatim to `<iframe srcdoc={…}>`. */
export function buildWidgetHtml(opts: WidgetBuildOptions): string {
  const safeTitle = escapeHtml(opts.title || 'widget')
  const body = opts.kind === 'svg' ? wrapSvgFragment(opts.code) : opts.code
  // Only pull in KaTeX (≈300KB across css+js) when the fragment actually
  // contains math — detected by a display/inline delimiter or an explicit
  // `.eqn` equation card. Keeps plain dashboards/diagrams lean.
  const needsMath = /\$\$|\\\(|\\\[|class=["']?[^"']*\beqn\b/.test(opts.code)
  const mathHead = needsMath
    ? [
        `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">`,
        `<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>`,
        `<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js" onload="window.__freyjaRenderMath&&window.__freyjaRenderMath()"></script>`,
      ].join('\n')
    : ''
  const theme = opts.theme === 'light' ? 'light' : 'dark'
  return [
    '<!doctype html>',
    // color-scheme declared on <html> tells Chromium which user-agent
    // defaults apply, so `background: transparent` actually resolves to
    // transparent instead of falling back to the page background of the
    // other scheme (the cause of a giant white rectangle inside the
    // iframe under the dark shell). data-theme picks the token set.
    `<html lang="en" data-theme="${theme}" style="color-scheme: ${theme}; background: transparent;">`,
    '<head>',
    '<meta charset="utf-8" />',
    `<meta name="color-scheme" content="${theme}" />`,
    `<title>${safeTitle}</title>`,
    '<meta name="viewport" content="width=device-width, initial-scale=1" />',
    `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.44.0/dist/tabler-icons.min.css">`,
    // The shell's two faces. The sandboxed frame has an opaque origin, so
    // it cannot read the app's bundled fonts; the CDN copies have the same
    // metrics, and the stacks fall back to ui-monospace / Georgia offline.
    `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-mono@5.2.5/index.css">`,
    `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-mono@5.2.5/500.css">`,
    `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource-variable/fraunces@5.2.5/index.css">`,
    mathHead,
    `<style>${RUNTIME_CSS}</style>`,
    '</head>',
    '<body>',
    `<div id="freyja-widget-root" role="region" aria-label="${safeTitle}">`,
    body,
    '</div>',
    `<script>${RUNTIME_JS}</script>`,
    '</body>',
    '</html>',
  ].join('\n')
}

// -------------------------------------------------------------------------
// SVG wrapper — auto-inject the arrow marker once per fragment so
// agents can use marker-end="url(#arr)" without rewriting <defs>.
// -------------------------------------------------------------------------

function wrapSvgFragment(svg: string): string {
  if (!svg.trimStart().startsWith('<svg')) return svg
  if (/url\(#arr\)/.test(svg) === false) return svg
  // Inject <defs> right after the opening <svg ...>. If the SVG
  // already has a <defs>, regex-merge our marker into it.
  if (/<defs[\s>]/.test(svg)) {
    return svg.replace(/<defs([^>]*)>/, `<defs$1>${ARROW_MARKER_DEF}`)
  }
  return svg.replace(/<svg([^>]*)>/, `<svg$1><defs>${ARROW_MARKER_DEF}</defs>`)
}

const ARROW_MARKER_DEF = `<marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor"/></marker>`

// -------------------------------------------------------------------------
// CSS — design tokens, base styles, SVG classes, color ramps, forms,
// elicitation chrome, metric cards.
// Mirrors the Imagine contract but uses Freyja's mono-first palette.
// -------------------------------------------------------------------------

const RUNTIME_CSS = String.raw`
:root {
  /* Freyja palette tokens — the dark surface. The light set below
     applies when the parent shell is light (html[data-theme="light"]). */
  --bg-0: #0a0a0a;
  --bg-1: #121212;
  --bg-2: #181818;
  --bg-3: #1e1e1e;
  --bg-4: #262626;
  --fg-0: #e8e8e8;
  --fg-1: #a8a8a8;
  --fg-2: #6e6e6e;
  --fg-3: #4a4a4a;
  --fg-4: #303030;
  --accent: #a8d4fc;
  --accent-hi: #c4e0fc;
  --accent-lo: #7aafea;
  --ok: #a8b0a8;
  --warn: #b8a078;
  --danger: #b48282;

  /* MCP-Apps-style semantic variables. Agents code against these. */
  --color-background-primary: var(--bg-1);
  --color-background-secondary: var(--bg-2);
  --color-background-tertiary: var(--bg-3);
  --color-background-info: rgba(168, 212, 252, 0.08);
  --color-background-danger: rgba(180, 130, 130, 0.10);
  --color-background-success: rgba(168, 176, 168, 0.10);
  --color-background-warning: rgba(184, 160, 120, 0.10);
  --color-text-primary: var(--fg-0);
  --color-text-secondary: var(--fg-1);
  --color-text-tertiary: var(--fg-2);
  --color-text-info: var(--accent);
  --color-text-danger: var(--danger);
  --color-text-success: var(--ok);
  --color-text-warning: var(--warn);
  --color-text-inverse: var(--bg-0);
  --color-border-primary: rgba(255, 255, 255, 0.16);
  --color-border-secondary: rgba(255, 255, 255, 0.10);
  --color-border-tertiary: rgba(255, 255, 255, 0.06);
  --color-border-info: rgba(168, 212, 252, 0.32);
  --color-border-danger: rgba(180, 130, 130, 0.32);
  --color-border-success: rgba(168, 176, 168, 0.32);
  --color-border-warning: rgba(184, 160, 120, 0.32);
  --color-accent: var(--accent);

  /* Surfaces. A widget floats on the transcript, so the only fills are
     washes: a breath of ink on paper, a breath of light on the dark
     material. Edges are hairlines. */
  --wash: rgba(255, 255, 255, 0.035);
  --wash-2: rgba(255, 255, 255, 0.07);
  --hair: 0.5px solid var(--color-border-secondary);
  --hair-soft: 0.5px solid var(--color-border-tertiary);

  /* Short aliases used inside SVG. */
  --p: var(--color-text-primary);
  --s: var(--color-text-secondary);
  --t: var(--color-text-tertiary);
  --bg2: var(--color-background-secondary);
  --b: var(--color-border-secondary);

  /* Geist Mono carries every label and figure, Fraunces the titles —
     the same two faces as the shell around the widget. Both load from
     the CDN; the fallbacks keep the metrics close when offline. */
  --font-sans: 'Geist Mono', ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Monaco, monospace;
  --font-mono: 'Geist Mono', ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Monaco, monospace;
  --font-serif: 'Fraunces Variable', 'Fraunces', Georgia, serif;
  --border-radius-sm: 4px;
  --border-radius-md: 8px;
  --border-radius-lg: 12px;
  --border-radius-xl: 16px;
}
html[data-theme="light"] {
  /* Warm paper, matching the shell's light tokens. */
  --bg-0: #f8f5ef;
  --bg-1: #f3efe8;
  --bg-2: #ece7df;
  --bg-3: #e4ded5;
  --bg-4: #dad3c9;
  --fg-0: #24201b;
  --fg-1: #4c463e;
  --fg-2: #605950;
  --fg-3: #766f65;
  --fg-4: #a69f95;
  --accent: #2864b4;
  --accent-hi: #1a4e96;
  --accent-lo: #5e8ecc;
  --ok: #427654;
  --warn: #92661e;
  --danger: #ac423e;
  --color-background-info: rgba(40, 100, 180, 0.08);
  --color-background-danger: rgba(172, 66, 62, 0.08);
  --color-background-success: rgba(66, 118, 84, 0.10);
  --color-background-warning: rgba(146, 102, 30, 0.10);
  --color-text-inverse: #f8f5ef;
  --color-border-primary: rgba(34, 30, 24, 0.18);
  --color-border-secondary: rgba(34, 30, 24, 0.12);
  --color-border-tertiary: rgba(34, 30, 24, 0.07);
  --color-border-info: rgba(40, 100, 180, 0.35);
  --color-border-danger: rgba(172, 66, 62, 0.35);
  --color-border-success: rgba(66, 118, 84, 0.35);
  --color-border-warning: rgba(146, 102, 30, 0.35);
  --wash: rgba(34, 30, 24, 0.035);
  --wash-2: rgba(34, 30, 24, 0.07);
}

* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: transparent !important;
  color: var(--color-text-primary);
  font-family: var(--font-sans);
  font-size: 12.5px;
  line-height: 1.55;
  /* No nested scroll. The parent iframe height adjusts to content. */
  overflow: visible;
  /* color-scheme also declared here as a belt-and-suspenders defense
     in case the agent's fragment overrides the <html style="..."> we
     set in the wrapper. Without the right scheme, transparent surfaces
     render against the other scheme's user-agent default. */
  color-scheme: dark;
  -webkit-font-smoothing: antialiased;
}
html[data-theme="light"], html[data-theme="light"] body {
  color-scheme: light;
}

/* The root sits flush left with the prose around it and never wider
   than a comfortable measure; SVG keeps its 680 design width. */
#freyja-widget-root {
  display: block;
  width: 100%;
  max-width: 760px;
  margin: 0;
  padding: 2px 0;
  background: transparent;
  /* break-word, not anywhere: a word only breaks when it cannot fit a
     line on its own, so table columns still size to whole words. */
  overflow-wrap: break-word;
}

/* ============== Resilience ==============
   Agent markup is written blind. These rules keep it inside the frame:
   grid and flex children may shrink, long words break, media and tables
   never exceed the root, SVG keeps its aspect ratio, nothing refuses to
   wrap, and preformatted text folds instead of scrolling. */
#freyja-widget-root *, #freyja-widget-root *::before, #freyja-widget-root *::after { min-width: 0; }
#freyja-widget-root img, #freyja-widget-root canvas, #freyja-widget-root video,
#freyja-widget-root table, #freyja-widget-root pre, #freyja-widget-root iframe { max-width: 100%; }
#freyja-widget-root svg[viewBox] { max-width: 680px; height: auto; display: block; }
#freyja-widget-root [style*="nowrap"] { white-space: normal !important; }
#freyja-widget-root [style*="position: fixed"], #freyja-widget-root [style*="position:fixed"] { position: relative !important; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
#freyja-widget-root [style*="nowrap"].tag, #freyja-widget-root th { white-space: nowrap !important; }

/* ============== Type ============== */
h1, h2, h3 {
  margin: 0 0 8px;
  font-family: var(--font-serif);
  font-weight: 400;
  letter-spacing: -0.005em;
  line-height: 1.25;
  color: var(--color-text-primary);
}
h1 { font-size: 24px; }
h2 { font-size: 19px; }
h3 { font-size: 16px; }
h4 { margin: 0 0 6px; font-size: 13px; font-weight: 500; color: var(--color-text-primary); }
h2:not(:first-child), h3:not(:first-child) { margin-top: 20px; }
p { margin: 0 0 10px; color: var(--color-text-secondary); font-size: 12.5px; line-height: 1.6; }
p:last-child { margin-bottom: 0; }
small { color: var(--color-text-tertiary); font-size: 11px; }
strong, b { font-weight: 500; color: var(--color-text-primary); }
code { font-family: var(--font-mono); font-size: 0.95em; color: var(--color-text-info); }
ul, ol { margin: 0 0 10px; padding-left: 20px; color: var(--color-text-secondary); }
li { margin: 2px 0; }
hr { border: 0; border-top: var(--hair); margin: 14px 0; }
.kicker { font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--color-text-tertiary); margin: 0 0 6px; }
.muted { color: var(--color-text-tertiary); }
.num { font-variant-numeric: tabular-nums; text-align: right; }

/* ============== Surfaces ============== */
.card {
  background: var(--wash);
  border: var(--hair);
  border-radius: 10px;
  padding: 14px 16px;
}
.card > :last-child { margin-bottom: 0; }
.card-grid, .grid-2, .grid-3 { display: grid; gap: 12px; margin: 12px 0; }
.card-grid { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.grid-2 { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
.grid-3 { grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }

.callout, .note {
  border-left: 2px solid var(--color-border-primary);
  padding: 8px 14px;
  margin: 12px 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--color-text-secondary);
  border-radius: 0 6px 6px 0;
}
.callout.tone-info, .note.tone-info { border-color: var(--color-border-info); background: var(--color-background-info); }
.callout.tone-success, .note.tone-success { border-color: var(--color-border-success); background: var(--color-background-success); }
.callout.tone-warning, .note.tone-warning { border-color: var(--color-border-warning); background: var(--color-background-warning); }
.callout.tone-danger, .note.tone-danger { border-color: var(--color-border-danger); background: var(--color-background-danger); }

.tag {
  display: inline-block;
  padding: 1px 8px;
  border: var(--hair);
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.tag.tone-info { color: var(--color-text-info); border-color: var(--color-border-info); background: var(--color-background-info); }
.tag.tone-success { color: var(--color-text-success); border-color: var(--color-border-success); background: var(--color-background-success); }
.tag.tone-warning { color: var(--color-text-warning); border-color: var(--color-border-warning); background: var(--color-background-warning); }
.tag.tone-danger { color: var(--color-text-danger); border-color: var(--color-border-danger); background: var(--color-background-danger); }

/* Key → value pairs. */
.kv { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 5px 18px; margin: 10px 0; font-size: 12.5px; }
.kv dt { color: var(--color-text-tertiary); }
.kv dd { margin: 0; color: var(--color-text-primary); }

/* Numbered steps. */
.steps { list-style: none; margin: 10px 0; padding: 0; display: flex; flex-direction: column; gap: 8px; counter-reset: step; }
.steps > li { position: relative; padding-left: 32px; margin: 0; color: var(--color-text-secondary); }
.steps > li::before {
  counter-increment: step;
  content: counter(step);
  position: absolute; left: 0; top: 0;
  width: 20px; height: 20px;
  display: inline-flex; align-items: center; justify-content: center;
  border: var(--hair); border-radius: 50%;
  font-size: 10.5px; color: var(--color-text-tertiary);
}
.steps > li > b, .steps > li > strong { display: block; }

/* Horizontal bars: label · bar · value. Width goes on the inner <i>. */
.bars { display: grid; grid-template-columns: minmax(70px, max-content) minmax(0, 1fr) max-content; gap: 7px 12px; align-items: center; margin: 10px 0; font-size: 12px; }
.bars .bar { height: 8px; border-radius: 2px; background: var(--wash-2); overflow: hidden; }
.bars .bar > i { display: block; height: 100%; background: var(--color-accent); border-radius: inherit; }
.bars .bar.tone-success > i { background: var(--color-text-success); }
.bars .bar.tone-warning > i { background: var(--color-text-warning); }
.bars .bar.tone-danger > i { background: var(--color-text-danger); }
.bars .val { font-variant-numeric: tabular-nums; color: var(--color-text-primary); }
.bars .lab { color: var(--color-text-secondary); }

/* ============== Tables ============== */
table { width: 100%; border-collapse: collapse; margin: 10px 0 14px; font-size: 12.5px; line-height: 1.45; }
th {
  text-align: left; font-weight: 500; white-space: nowrap;
  font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--color-text-tertiary);
  padding: 4px 10px 8px;
  border-bottom: 1px solid var(--color-border-primary);
}
td { padding: 8px 10px; border-bottom: var(--hair-soft); vertical-align: top; color: var(--color-text-primary); }
th:first-child, td:first-child { padding-left: 0; }
th:last-child, td:last-child { padding-right: 0; }
tbody tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.tone-success { color: var(--color-text-success); }
.tone-warning { color: var(--color-text-warning); }
.tone-danger { color: var(--color-text-danger); }
.tone-info { color: var(--color-text-info); }

/* ============== Metrics ============== */
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px 22px;
  margin: 12px 0;
}
.metric {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding-top: 9px;
  border-top: var(--hair);
}
.metric-label {
  font-size: 10.5px;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.1em;
}
.metric-value {
  font-size: 24px;
  font-weight: 400;
  letter-spacing: -0.01em;
  line-height: 1.15;
  color: var(--color-text-primary);
  font-variant-numeric: tabular-nums;
}
.metric-note { font-size: 11.5px; color: var(--color-text-secondary); }
.delta {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  margin-top: 2px;
}
.delta-up { color: var(--color-text-success); }
.delta-down { color: var(--color-text-danger); }
.delta-flat { color: var(--color-text-tertiary); }
.delta i { font-size: 13px; }

.compare-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  margin: 12px 0;
}
.compare-card {
  background: var(--wash);
  border: var(--hair);
  border-radius: 10px;
  padding: 14px 16px;
}
.compare-card.recommended {
  border-color: var(--color-border-info);
  box-shadow: inset 0 0 0 1px var(--color-border-info);
  background: var(--color-background-info);
}

/* ============== Flow (HTML flowchart) ==============
   Nodes size to their text and wrap; the arrows are drawn by the
   runtime. A row is one left-to-right stage, a .flow-edge between rows
   is the transition and its label. */
.flow { display: flex; flex-direction: column; gap: 6px; margin: 12px 0; }
.flow-row { display: flex; flex-wrap: wrap; align-items: stretch; gap: 10px 30px; }
.flow-node {
  position: relative;
  flex: 1 1 150px;
  max-width: 100%;
  padding: 9px 12px;
  border-radius: 8px;
  background: var(--wash);
  border: var(--hair);
  font-size: 12px;
  line-height: 1.4;
  color: var(--color-text-secondary);
}
.flow-node > b, .flow-node > strong { display: block; font-weight: 500; color: var(--color-text-primary); margin-bottom: 2px; }
.flow-node > span, .flow-node > small { display: block; font-size: 11.5px; color: var(--color-text-secondary); }
.flow-node + .flow-node::before {
  content: '→';
  position: absolute;
  left: -22px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--color-text-tertiary);
  font-size: 13px;
  pointer-events: none;
}
.flow-node.tone-info { background: var(--color-background-info); border-color: var(--color-border-info); }
.flow-node.tone-success { background: var(--color-background-success); border-color: var(--color-border-success); }
.flow-node.tone-warning { background: var(--color-background-warning); border-color: var(--color-border-warning); }
.flow-node.tone-danger { background: var(--color-background-danger); border-color: var(--color-border-danger); }
.flow-node[data-prompt] { cursor: pointer; }
.flow-node[data-prompt]:hover { border-color: var(--color-border-primary); }
.flow-edge {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 0 0 12px;
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--color-text-tertiary);
}
.flow-edge::before { content: '↓'; font-size: 13px; }

/* ============== HTML form elements ============== */
input[type="text"], input[type="date"], input[type="number"], input[type="email"],
input[type="search"], textarea, select {
  width: 100%;
  padding: 8px 10px;
  background: var(--wash);
  color: var(--color-text-primary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-md);
  font-family: var(--font-sans);
  font-size: 12.5px;
  outline: none;
  transition: border-color 120ms ease;
}
input:focus, textarea:focus, select:focus {
  border-color: var(--color-border-info);
}
textarea { min-height: 60px; resize: vertical; }
input[type="range"] {
  width: 100%;
  appearance: none;
  background: transparent;
  margin: 8px 0;
}
input[type="range"]::-webkit-slider-runnable-track {
  height: 4px;
  background: var(--color-border-secondary);
  border-radius: 2px;
}
input[type="range"]::-webkit-slider-thumb {
  appearance: none;
  width: 14px;
  height: 14px;
  background: var(--color-accent);
  border-radius: 50%;
  margin-top: -5px;
  cursor: pointer;
}

button {
  font-family: var(--font-sans);
  font-size: 12.5px;
  font-weight: 500;
  padding: 7px 14px;
  background: var(--wash);
  color: var(--color-text-primary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-md);
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease;
}
button:hover {
  background: var(--wash-2);
  border-color: var(--color-border-primary);
}
.btn-accent, button.primary {
  background: var(--color-accent);
  color: var(--color-text-inverse);
  border-color: var(--color-accent);
}
.btn-accent:hover, button.primary:hover { background: var(--accent-hi); border-color: var(--accent-hi); }

/* ============== Equation card (.eqn) ============== */
/* A framed display equation with optional annotated term breakdown —
 * the showcase counterpart to inline prose math. KaTeX is auto-rendered
 * by the runtime; author writes $$…$$ / $…$ directly in the markup. */
.eqn {
  background: var(--wash);
  border: var(--hair);
  border-radius: var(--border-radius-lg);
  overflow: hidden;
}
.eqn-title {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-bottom: var(--hair-soft);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--color-text-tertiary);
}
.eqn-title i { font-size: 14px; color: var(--color-text-info); }
.eqn-main {
  padding: 18px 16px;
  text-align: center;
  overflow-x: auto;
  overflow-y: hidden;
}
.eqn-main .katex-display { margin: 0; }
.eqn-main .katex { font-size: 1.18em; color: var(--color-text-primary); }
.eqn-note {
  padding: 0 16px 14px;
  margin: 0;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-tertiary);
}
/* Annotated term breakdown — each row pairs a rendered symbol with a
 * plain-English gloss, mirroring the "read the pieces" explainer. */
.eqn-terms {
  display: flex;
  flex-direction: column;
  border-top: var(--hair-soft);
}
.eqn-term {
  display: grid;
  grid-template-columns: minmax(72px, max-content) 1fr;
  gap: 14px;
  align-items: baseline;
  padding: 10px 16px;
  border-top: var(--hair-soft);
}
.eqn-term:first-child { border-top: 0; }
.eqn-term-sym {
  font-size: 1.02em;
  color: var(--color-text-primary);
  text-align: right;
  white-space: nowrap;
}
.eqn-term-sym .katex { font-size: 1em; }
.eqn-term-def {
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  text-align: left;
}
.eqn-term-def code {
  font-family: var(--font-mono);
  font-size: 0.92em;
  color: var(--color-text-info);
}
/* ============== SVG classes ============== */
/* Monospace, so one character is 0.6 × the font size: a 13px label
 * runs 7.8px per character. The runtime also squeezes any <text>
 * that would overrun the <rect> it sits in. */
text.t  { font: 13px var(--font-sans); fill: var(--color-text-primary); }
text.ts { font: 11.5px var(--font-sans); fill: var(--color-text-secondary); }
text.th { font: 500 13px var(--font-sans); fill: var(--color-text-primary); }
rect.box, .box rect {
  fill: var(--wash);
  stroke: var(--color-border-secondary);
  stroke-width: 0.75;
}
.node { cursor: pointer; }
.node:hover rect.box, .node:hover > rect { fill: var(--wash-2); }
.arr, line.arr, path.arr {
  stroke: var(--color-border-primary);
  stroke-width: 1.25;
  fill: none;
  color: var(--color-border-primary);
}
.leader, line.leader {
  stroke: var(--color-border-tertiary);
  stroke-width: 0.5;
  stroke-dasharray: 3 2;
  fill: none;
}

/* ============== Color ramps ============== */
/* Apply to <g>, <rect>, <circle>, <ellipse> — never <path>. Each
 * ramp has 7 stops. Default class (no stop) renders the -400 fill. */
${buildColorRamps()}

/* ============== Elicitation form ============== */
.elicit {
  background: var(--wash);
  border: var(--hair);
  border-radius: var(--border-radius-lg);
  overflow: hidden;
}
.elicit-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: var(--hair-soft);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--color-text-tertiary);
}
.elicit-header i { font-size: 14px; color: var(--color-text-info); }
.elicit-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px;
}
.elicit-group { display: flex; flex-direction: column; gap: 6px; }
.elicit-question {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--color-text-primary);
}
.elicit-pills, .elicit-cards, .elicit-tiles {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.elicit-pill {
  padding: 5px 12px;
  background: transparent;
  border: 1px solid var(--color-border-secondary);
  border-radius: 999px;
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: background 120ms, border-color 120ms, color 120ms;
}
.elicit-pill:hover { color: var(--color-text-primary); border-color: var(--color-border-primary); background: var(--wash); }
.elicit-pill.is-selected {
  background: var(--color-background-info);
  border-color: var(--color-accent);
  color: var(--color-accent);
}
.elicit-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px 12px;
  background: var(--wash);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-md);
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-primary);
  cursor: pointer;
  text-align: left;
  min-width: 100px;
  transition: background 120ms, border-color 120ms;
}
.elicit-card:hover { border-color: var(--color-border-primary); }
.elicit-card i { font-size: 18px; color: var(--color-text-secondary); }
.elicit-card span { font-weight: 500; }
.elicit-card small { color: var(--color-text-tertiary); font-size: 11px; }
.elicit-card.is-selected {
  background: var(--color-background-info);
  border-color: var(--color-accent);
}
.elicit-card.is-selected i,
.elicit-card.is-selected span { color: var(--color-accent); }
.elicit-card.is-selected small { color: var(--color-text-info); }
.elicit-tile {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 10px;
  background: var(--wash);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-md);
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-primary);
  cursor: pointer;
  min-width: 90px;
}
.elicit-tile svg { width: 32px; height: 32px; color: var(--color-text-tertiary); }
.elicit-tile.is-selected {
  background: var(--color-background-info);
  border-color: var(--color-accent);
  color: var(--color-accent);
}
.elicit-tile.is-selected svg { color: var(--color-accent); }
.elicit-other[hidden] { display: none; }
.elicit-other {
  margin-top: 4px;
}
.elicit-footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-top: var(--hair-soft);
}
.elicit-skip {
  background: transparent;
  border: 1px solid var(--color-border-secondary);
  color: var(--color-text-tertiary);
}
.elicit-submit {
  background: var(--color-accent);
  color: var(--color-text-inverse);
  border: 1px solid var(--color-accent);
  font-weight: 500;
}
.elicit-submit:hover { background: var(--accent-hi); border-color: var(--accent-hi); }

/* Range slider value indicator. The shell injects this span. */
.elicit-range-readout {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--color-text-tertiary);
  margin-left: 8px;
}

/* Anchor / link interception cosmetic */
a { color: var(--color-text-info); text-decoration: none; border-bottom: 1px solid var(--color-border-info); }
a:hover { color: var(--accent-hi); }

/* Tabler-icon base — inherits color + font-size from parent */
.ti { font-size: inherit; line-height: 1; vertical-align: middle; }
`

// Color ramps — 9 hues × 7 stops. Each stop applies fill or stroke
// to whichever SVG element carries the class. The dark ramps run from
// a near-black tint (50) to a pastel (900); on the light shell the
// same classes map to the ramp read the other way round, so a `-50`
// wash stays a wash and a `-900` highlight stays the strongest ink.
function buildColorRamps(): string {
  const lightRamps: Record<string, string[]> = {
    purple: ['#f1ecfa', '#e2d8f5', '#c5b2ec', '#9a7ad8', '#6f4abc', '#4e2f90', '#331c63'],
    teal: ['#e6f5f4', '#c8ebe8', '#93d7d1', '#4cc1b8', '#27908f', '#1a6667', '#0f4445'],
    coral: ['#fbeeea', '#f6d8cf', '#efb3a2', '#e07f66', '#c2503a', '#8f3526', '#5e2920'],
    pink: ['#f9ecf2', '#f3d3e0', '#e8a8c4', '#d97aa3', '#b4507e', '#853859', '#5f2942'],
    gray: ['#f3f4f6', '#e6e8eb', '#cfd3d8', '#aab0b8', '#7c838d', '#555b64', '#2f343b'],
    blue: ['#eaf2fb', '#d2e3f6', '#a8c8ec', '#6a9fdc', '#2d6a9d', '#1f4f7a', '#163a5a'],
    green: ['#e8f4ee', '#cde8d9', '#9dd2b6', '#5fb48c', '#3a8e6e', '#28654d', '#1a4634'],
    amber: ['#faf3e4', '#f3e3bf', '#e9c886', '#d4a84a', '#a07a26', '#75581b', '#4f3c12'],
    red: ['#fbecea', '#f6d1cd', '#eea39e', '#df6f67', '#c2423d', '#912d29', '#64201d'],
  }
  const ramps: Record<string, string[]> = {
    purple: ['#1a1325', '#23173a', '#2e1d4f', '#4a2e7d', '#6f4abc', '#a07ee8', '#c2adf0'],
    teal: ['#0e1a1a', '#10262a', '#0f3a3e', '#185b62', '#27908f', '#4cc1b8', '#82d8d0'],
    coral: ['#1e1110', '#2c1815', '#3e1f1a', '#5e2920', '#a04432', '#d36f56', '#e8a08c'],
    pink: ['#1c1118', '#2a161f', '#3d1c2a', '#5f2942', '#a44a72', '#d97aa3', '#e8a4c4'],
    gray: ['#0f0f0f', '#181818', '#242424', '#3a3a3a', '#5a5a5a', '#8a8a8a', '#bcbcbc'],
    blue: ['#0e1620', '#101e2c', '#0f2940', '#1a3d5c', '#2d6a9d', '#7aafea', '#a8d4fc'],
    green: ['#0e1812', '#11241b', '#103425', '#19533c', '#3a8e6e', '#7ec9a5', '#b3e0c8'],
    amber: ['#1c1810', '#28210f', '#3a2e10', '#5e4818', '#a07a26', '#d4ad5b', '#e8c789'],
    red: ['#1d100f', '#2a1715', '#3a1d1c', '#5b2624', '#a23a37', '#d76664', '#eaa19d'],
  }
  const stops = [50, 100, 200, 400, 600, 800, 900]
  const lines: string[] = []
  for (const [name, palette] of Object.entries(ramps)) {
    const mid = palette[3]
    lines.push(`.c-${name} { fill: ${mid}; stroke: ${mid}; color: ${mid}; }`)
    palette.forEach((hex, i) => {
      const stop = stops[i]
      lines.push(`.c-${name}-${stop} { fill: ${hex}; stroke: ${hex}; color: ${hex}; }`)
    })
  }
  const L = 'html[data-theme="light"]'
  for (const [name, palette] of Object.entries(lightRamps)) {
    const mid = palette[3]
    lines.push(`${L} .c-${name} { fill: ${mid}; stroke: ${mid}; color: ${mid}; }`)
    palette.forEach((hex, i) => {
      const stop = stops[i]
      lines.push(`${L} .c-${name}-${stop} { fill: ${hex}; stroke: ${hex}; color: ${hex}; }`)
    })
  }
  return lines.join('\n')
}

// -------------------------------------------------------------------------
// JS — postMessage protocol + elicitation auto-wiring + globals.
// -------------------------------------------------------------------------

const RUNTIME_JS = String.raw`
(function(){
  'use strict';

  function post(type, payload){
    try { parent.postMessage(Object.assign({ type: type }, payload || {}), '*'); }
    catch(e){}
  }

  // The shell tells us when it flips between dark and light; swap the
  // token set and the user-agent colour scheme in place.
  window.addEventListener('message', function(ev){
    var d = ev && ev.data;
    if (!d || d.type !== 'ui/theme') return;
    var t = d.theme === 'light' ? 'light' : 'dark';
    var root = document.documentElement;
    root.setAttribute('data-theme', t);
    root.style.colorScheme = t;
    if (document.body) document.body.style.colorScheme = t;
  });

  // Globals exposed to agent code.
  window.sendPrompt = function(text){
    if (typeof text !== 'string' || !text.trim()) return;
    post('ui/message', { text: text.trim() });
  };
  window.openLink = function(url){
    if (typeof url !== 'string' || !url.trim()) return;
    post('ui/open-link', { url: url.trim() });
  };

  // Intercept anchor clicks — never let an iframe nav-away.
  document.addEventListener('click', function(ev){
    var a = ev.target && ev.target.closest && ev.target.closest('a[href]');
    if (a) {
      ev.preventDefault();
      var url = a.getAttribute('href') || '';
      if (url) window.openLink(url);
      return;
    }
    var prompt = ev.target && ev.target.closest && ev.target.closest('[data-prompt]');
    if (prompt) {
      ev.preventDefault();
      var t = prompt.getAttribute('data-prompt') || '';
      if (t) window.sendPrompt(t);
    }
  }, true);

  // Range sliders get a live numeric readout next to them.
  function attachRangeReadouts(){
    document.querySelectorAll('input[type="range"]').forEach(function(r){
      if (r.dataset.freyjaReadout === '1') return;
      r.dataset.freyjaReadout = '1';
      var span = document.createElement('span');
      span.className = 'elicit-range-readout';
      span.textContent = r.value;
      r.insertAdjacentElement('afterend', span);
      r.addEventListener('input', function(){ span.textContent = r.value; });
    });
  }

  // Elicitation chrome auto-wiring. Selection state, multi-select,
  // "Other" reveal, submit collection.
  function attachElicit(form){
    if (form.dataset.freyjaWired === '1') return;
    form.dataset.freyjaWired = '1';
    var groups = form.querySelectorAll('.elicit-group');
    groups.forEach(function(group){
      var multi = group.getAttribute('data-multi') === 'true';
      var picks = group.querySelectorAll('.elicit-pill, .elicit-card, .elicit-tile');
      var otherInput = group.querySelector('.elicit-other');
      picks.forEach(function(btn){
        btn.addEventListener('click', function(ev){
          ev.preventDefault();
          if (multi) {
            btn.classList.toggle('is-selected');
          } else {
            picks.forEach(function(b){ if (b !== btn) b.classList.remove('is-selected'); });
            btn.classList.toggle('is-selected');
          }
          if (otherInput) {
            var otherSelected = !!group.querySelector('.is-selected[data-other]');
            otherInput.hidden = !otherSelected;
            if (otherSelected) setTimeout(function(){ otherInput.focus(); }, 0);
          }
        });
      });
    });

    var submitBtn = form.querySelector('.elicit-submit');
    var skipBtn = form.querySelector('.elicit-skip');
    if (submitBtn) submitBtn.addEventListener('click', function(ev){
      ev.preventDefault();
      var msg = collectElicit(form);
      if (msg) window.sendPrompt(msg);
    });
    if (skipBtn) skipBtn.addEventListener('click', function(ev){
      ev.preventDefault();
      window.sendPrompt('(Skipped the form — proceed with defaults or ask me in plain text)');
    });
  }

  function collectElicit(form){
    var subject = (form.getAttribute('data-subject') || 'Form').trim();
    var parts = [];
    var groups = form.querySelectorAll('.elicit-group');
    groups.forEach(function(group){
      var name = group.getAttribute('data-name') || '';
      if (!name) return;
      var label = name.replace(/_/g, ' ').replace(/(^|\s)\S/g, function(c){ return c.toUpperCase(); });
      var value = readGroupValue(group);
      if (value === null || value === undefined || value === '') return;
      parts.push(label + ': ' + formatValue(value));
    });
    if (!parts.length) return subject;
    return subject + ' — ' + parts.join(' · ');
  }

  function readGroupValue(group){
    var picks = group.querySelectorAll('.elicit-pill, .elicit-card, .elicit-tile');
    if (picks.length) {
      var selected = group.querySelectorAll('.is-selected');
      if (!selected.length) return '';
      var values = [];
      selected.forEach(function(el){
        if (el.hasAttribute('data-other')) {
          var other = group.querySelector('.elicit-other');
          if (other && other.value) values.push(other.value.trim());
        } else {
          values.push((el.getAttribute('data-value') || el.textContent || '').trim());
        }
      });
      return values.filter(Boolean).join(', ');
    }
    var range = group.querySelector('input[type="range"]');
    if (range) return range.value;
    var date = group.querySelector('input[type="date"]');
    if (date) return date.value;
    var textarea = group.querySelector('textarea');
    if (textarea) return (textarea.value || '').replace(/\s*\n\s*/g, ' / ').trim();
    var text = group.querySelector('input[type="text"], input[type="email"], input[type="number"], input[type="search"], select');
    if (text) return (text.value || '').trim();
    return '';
  }

  function formatValue(v){
    var s = String(v);
    if (s.length > 200) {
      return s.slice(0, 80).trim() + '… [see --- Full content ---]';
    }
    if (s.length > 80) return '"' + s + '"';
    return s;
  }

  // Resize observer: report content height up so the parent can grow
  // the iframe. Throttle via rAF so a burst of mutations doesn't
  // flood the host with messages.
  var resizePending = false;
  function reportHeight(){
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(function(){
      resizePending = false;
      var root = document.getElementById('freyja-widget-root');
      var h = Math.max(
        root ? root.scrollHeight : 0,
        root ? root.getBoundingClientRect().bottom : 0,
        document.body ? document.body.scrollHeight : 0,
        document.documentElement.scrollHeight || 0
      );
      post('ui/resize', { height: Math.max(40, Math.ceil(h) + 8) });
    });
  }
  var ro = new ResizeObserver(reportHeight);
  ro.observe(document.documentElement);
  ro.observe(document.body);
  var rootEl = document.getElementById('freyja-widget-root');
  if (rootEl) ro.observe(rootEl);
  // Late layout: webfonts, icons, images and CDN scripts all land after
  // boot, so measure again when they do and once more on a timer.
  window.addEventListener('load', reportHeight);
  setTimeout(reportHeight, 400);
  setTimeout(reportHeight, 1500);
  var mo = new MutationObserver(reportHeight);
  mo.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });

  // KaTeX auto-render. The katex + auto-render scripts are injected into
  // <head> (with defer) only when the fragment contains math; once the
  // contrib script loads it calls window.__freyjaRenderMath. We also call
  // it from boot in case the scripts resolved before boot ran.
  window.__freyjaRenderMath = function(){
    if (typeof window.renderMathInElement !== 'function') return;
    var root = document.getElementById('freyja-widget-root');
    if (!root) return;
    try {
      window.renderMathInElement(root, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '\\[', right: '\\]', display: true },
          { left: '$', right: '$', display: false },
          { left: '\\(', right: '\\)', display: false }
        ],
        throwOnError: false,
        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
      });
    } catch (e) {}
    reportHeight();
  };

  // SVG labels cannot wrap. For every <text> that starts inside a <rect>
  // drawn before it (the box pattern every diagram uses), measure the
  // run against the room left in that box and, when it overruns, shrink
  // the type a little and squeeze the glyphs the rest of the way. Runs
  // at boot and again once the webfonts land, since they change widths.
  function fitSvgText(){
    document.querySelectorAll('svg').forEach(function(svg){
      var rects = Array.prototype.slice.call(svg.querySelectorAll('rect'));
      if (!rects.length) return;
      svg.querySelectorAll('text').forEach(function(t){
        var x = parseFloat(t.getAttribute('x')), y = parseFloat(t.getAttribute('y'));
        if (!isFinite(x) || !isFinite(y)) return;
        var box = null;
        for (var i = rects.length - 1; i >= 0; i--) {
          var r = rects[i];
          if (!(r.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
          var rx = parseFloat(r.getAttribute('x')) || 0, ry = parseFloat(r.getAttribute('y')) || 0;
          var rw = parseFloat(r.getAttribute('width')) || 0, rh = parseFloat(r.getAttribute('height')) || 0;
          if (x >= rx && x <= rx + rw && y >= ry && y <= ry + rh) { box = { x: rx, w: rw }; break; }
        }
        if (!box) return;
        var anchorMode = t.getAttribute('text-anchor') || getComputedStyle(t).textAnchor || 'start';
        var pad = 6;
        var avail = anchorMode === 'middle'
          ? 2 * Math.min(x - box.x, box.x + box.w - x) - pad
          : anchorMode === 'end' ? x - box.x - pad : box.x + box.w - x - pad;
        if (!(avail > 0)) return;
        if (t.dataset.freyjaFit === '1') { t.style.fontSize = ''; t.removeAttribute('textLength'); t.removeAttribute('lengthAdjust'); }
        var len;
        try { len = t.getComputedTextLength(); } catch (e) { return; }
        if (!(len > avail)) return;
        t.dataset.freyjaFit = '1';
        var fs = parseFloat(getComputedStyle(t).fontSize) || 13;
        var scale = avail / len;
        if (scale < 0.85) {
          t.style.fontSize = Math.max(10, fs * Math.max(scale, 0.78)).toFixed(2) + 'px';
          try { len = t.getComputedTextLength(); } catch (e) { return; }
        }
        if (len > avail) {
          t.setAttribute('textLength', avail.toFixed(1));
          t.setAttribute('lengthAdjust', 'spacingAndGlyphs');
        }
      });
    });
  }
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function(){ fitSvgText(); reportHeight(); });
  }

  // Initial pass — wire any elicit forms that streamed in pre-load,
  // attach range readouts, render math, and signal ready.
  function boot(){
    document.querySelectorAll('.elicit').forEach(attachElicit);
    attachRangeReadouts();
    fitSvgText();
    window.__freyjaRenderMath();
    reportHeight();
    post('ui/ready', {});
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  // Re-attach after every DOM mutation in case agents stream in
  // additional elicit blocks post-load.
  new MutationObserver(function(){
    document.querySelectorAll('.elicit').forEach(attachElicit);
    attachRangeReadouts();
  }).observe(document.body, { childList: true, subtree: true });
})();
`

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
