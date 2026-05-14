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
}

/** Build the full iframe srcdoc for a widget. The result is meant to
 *  be passed verbatim to `<iframe srcdoc={…}>`. */
export function buildWidgetHtml(opts: WidgetBuildOptions): string {
  const safeTitle = escapeHtml(opts.title || 'widget')
  const body = opts.kind === 'svg' ? wrapSvgFragment(opts.code) : opts.code
  return [
    '<!doctype html>',
    // color-scheme declared on <html> tells Chromium this document is
    // dark, so `background: transparent` actually resolves to
    // transparent instead of falling back to the user-agent light-mode
    // page background (the cause of the giant white rectangle inside
    // the iframe). Matches the rest of Freyja's dark-only chrome.
    '<html lang="en" style="color-scheme: dark; background: transparent;">',
    '<head>',
    '<meta charset="utf-8" />',
    '<meta name="color-scheme" content="dark" />',
    `<title>${safeTitle}</title>`,
    '<meta name="viewport" content="width=device-width, initial-scale=1" />',
    `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.44.0/dist/tabler-icons.min.css">`,
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
  /* Freyja palette tokens. These are the canonical dark surface; we
     don't ship a light variant yet — Freyja itself is dark-only. */
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

  /* Short aliases used inside SVG. */
  --p: var(--color-text-primary);
  --s: var(--color-text-secondary);
  --t: var(--color-text-tertiary);
  --bg2: var(--color-background-secondary);
  --b: var(--color-border-secondary);

  --font-sans: 'Geist Mono', ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Monaco, monospace;
  --font-mono: 'Geist Mono', ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Monaco, monospace;
  --font-serif: 'Fraunces', Georgia, serif;
  --border-radius-sm: 4px;
  --border-radius-md: 8px;
  --border-radius-lg: 12px;
  --border-radius-xl: 16px;
}

* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: transparent !important;
  color: var(--color-text-primary);
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 1.5;
  /* No nested scroll. The parent iframe height adjusts to content. */
  overflow: visible;
  /* color-scheme also declared here as a belt-and-suspenders defense
     in case the agent's fragment overrides the <html style="..."> we
     set in the wrapper. Without dark scheme, transparent surfaces
     render against a white user-agent default. */
  color-scheme: dark;
}

#freyja-widget-root {
  display: block;
  width: 100%;
  max-width: 680px;
  margin: 0 auto;
  padding: 0;
  background: transparent;
}

/* Generic text */
h1, h2, h3, h4 { margin: 0 0 8px; font-weight: 500; color: var(--color-text-primary); }
h1 { font-size: 18px; }
h2 { font-size: 15px; }
h3 { font-size: 13px; }
p { margin: 0 0 8px; color: var(--color-text-secondary); }
small { color: var(--color-text-tertiary); font-size: 11px; }

/* ============== HTML form elements ============== */
input[type="text"], input[type="date"], input[type="number"], input[type="email"],
input[type="search"], textarea, select {
  width: 100%;
  padding: 8px 10px;
  background: var(--color-background-secondary);
  color: var(--color-text-primary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-md);
  font-family: var(--font-sans);
  font-size: 13px;
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
  font-size: 13px;
  font-weight: 500;
  padding: 8px 14px;
  background: var(--color-background-tertiary);
  color: var(--color-text-primary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-md);
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease;
}
button:hover {
  background: var(--bg-4);
  border-color: var(--color-border-primary);
}

/* ============== Card / metric layouts ============== */
.card {
  background: var(--color-background-secondary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-lg);
  padding: 16px 20px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
}
.metric {
  background: var(--color-background-secondary);
  border: 1px solid var(--color-border-tertiary);
  border-radius: var(--border-radius-md);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.metric-label {
  font-size: 11px;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.metric-value {
  font-size: 22px;
  font-weight: 500;
  color: var(--color-text-primary);
  font-variant-numeric: tabular-nums;
}
.delta {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  margin-top: 2px;
}
.delta-up { color: var(--color-text-success); }
.delta-down { color: var(--color-text-danger); }
.delta-flat { color: var(--color-text-tertiary); }
.delta i { font-size: 14px; }

.compare-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.compare-card {
  background: var(--color-background-secondary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-lg);
  padding: 14px 16px;
}
.compare-card.recommended {
  border: 2px solid var(--color-border-info);
}

/* ============== SVG classes ============== */
text.t  { font: 14px var(--font-sans); fill: var(--color-text-primary); }
text.ts { font: 12px var(--font-sans); fill: var(--color-text-secondary); }
text.th { font: 500 14px var(--font-sans); fill: var(--color-text-primary); }
rect.box, .box rect {
  fill: var(--color-background-secondary);
  stroke: var(--color-border-secondary);
  stroke-width: 0.5;
}
.node { cursor: pointer; }
.node:hover rect.box, .node:hover > rect { fill: var(--bg-3); }
.arr, line.arr, path.arr {
  stroke: var(--color-border-primary);
  stroke-width: 1.5;
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
  background: var(--color-background-secondary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-lg);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  overflow: hidden;
}
.elicit-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-border-tertiary);
  background: var(--color-background-tertiary);
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.04em;
  color: var(--color-text-secondary);
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
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-primary);
}
.elicit-pills, .elicit-cards, .elicit-tiles {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.elicit-pill {
  padding: 6px 12px;
  background: var(--color-background-tertiary);
  border: 1px solid var(--color-border-secondary);
  border-radius: 999px;
  font-size: 12px;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: background 120ms, border-color 120ms, color 120ms;
}
.elicit-pill:hover { color: var(--color-text-primary); border-color: var(--color-border-primary); }
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
  background: var(--color-background-tertiary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-md);
  font-size: 12px;
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
  background: var(--color-background-tertiary);
  border: 1px solid var(--color-border-secondary);
  border-radius: var(--border-radius-md);
  font-size: 12px;
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
  border-top: 1px solid var(--color-border-tertiary);
  background: var(--color-background-tertiary);
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
// to whichever SVG element carries the class.
function buildColorRamps(): string {
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
      var h = (root ? root.scrollHeight : document.documentElement.scrollHeight) || 0;
      post('ui/resize', { height: Math.max(40, Math.ceil(h) + 8) });
    });
  }
  var ro = new ResizeObserver(reportHeight);
  ro.observe(document.documentElement);
  ro.observe(document.body);
  var mo = new MutationObserver(reportHeight);
  mo.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });

  // Initial pass — wire any elicit forms that streamed in pre-load,
  // attach range readouts, and signal ready.
  function boot(){
    document.querySelectorAll('.elicit').forEach(attachElicit);
    attachRangeReadouts();
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
