// Rendered HTML → clipboard, for copy-out.
//
// `renderMarkdown` (./markdown) turns model prose into the small HTML
// vocabulary the conversation pane paints; this walks it back out two ways.
// ⌘C copies what the reader sees: clean HTML (bold stays bold, without the
// pane's dark-theme styles) plus plain text with no markdown markers. The
// "copy as markdown" paths get markdown source instead of the DOM's
// flattened text (where tables become tab soup, code fences vanish, and
// headings lose their `#`).
//
// Whole-message copies take the cheap, lossless path — `messageToMarkdown`
// reads the raw part text straight off the store. Partial copies can't: the
// operator selects *pixels*, and half a table cell has no natural offset back
// into the source string. So `selectionToMarkdown` walks the live DOM instead.
//
// Three decorations sit on top of renderMarkdown's output and are handled
// here: KaTeX (the TeX is read back out of the MathML `annotation` rather
// than reconstructed from the glyph spans), `<mark class="search-hit">`
// highlights, and kanban card-mention chips. The last two are plain unwraps.

import type { Message } from '@shared/events'
import { renderMarkdown } from './markdown'

const ELEMENT_NODE = 1
const TEXT_NODE = 3

/** Chrome the serializer must never pick up (hover buttons and the like).
 *  Applied as a plain attribute so it survives `cloneContents`. */
const SKIP_ATTR = 'data-copy-skip'

/** Tags that force a line break around their content. Used to decide
 *  whether a whitespace-only text node is a real inter-word space or just
 *  source formatting between two blocks. */
const BLOCK_TAGS = new Set([
  'P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL', 'LI', 'PRE',
  'BLOCKQUOTE', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD', 'HR', 'DIV',
  'SECTION', 'ARTICLE', 'HEADER', 'FOOTER',
])

/** A formatted copy: HTML for rich paste targets, plain text for the rest. */
export interface RichText {
  html: string
  text: string
}

interface Ctx {
  /** Plain text instead of markdown: no emphasis markers, `#`, backticks
   *  or fences. Lists keep their numbers and bullets, as on screen. */
  plain: boolean
  listDepth: number
  /** Fenced code blocks are stashed here and replaced with a sentinel so
   *  the final whitespace-normalization pass can't collapse blank lines
   *  *inside* code. Restored verbatim at the very end. */
  codeBlocks: string[]
}

const NUL = String.fromCharCode(0)
const CODE_SENTINEL = (n: number) => `${NUL}MDCODE${n}${NUL}`
const CODE_SENTINEL_RE = new RegExp(`${NUL}MDCODE(\\d+)${NUL}`, 'g')

// ── Public API ─────────────────────────────────────────────────────────────

/** Raw markdown for a whole message: its text parts, verbatim and
 *  unrendered. Thinking blocks, tool chips and system cards are chrome
 *  around the answer, not the answer, so they're left out. */
export function messageToMarkdown(message: Message): string {
  const chunks: string[] = []
  for (const part of message.parts) {
    if (part.type === 'text' && part.text && part.text.trim()) {
      chunks.push(part.text.trim())
    }
  }
  return chunks.join('\n\n').trim()
}

/** Markdown for the current DOM selection. Returns '' for an empty
 *  selection so callers can fall through to the browser's native copy. */
export function selectionToMarkdown(selection: Selection | null): string {
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return ''
  const chunks: string[] = []
  for (let i = 0; i < selection.rangeCount; i += 1) {
    const md = rangeToMarkdown(selection.getRangeAt(i))
    if (md) chunks.push(md)
  }
  return chunks.join('\n\n').trim()
}

/** Markdown for one Range. Exported for tests. */
export function rangeToMarkdown(range: Range): string {
  if (range.collapsed) return ''
  return fragmentToText(contextualFragment(range), false)
}

/** What the reader sees in the current DOM selection, as HTML plus plain
 *  text. Returns null for an empty selection so callers can fall through
 *  to the browser's native copy. */
export function selectionToRichText(selection: Selection | null): RichText | null {
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null
  const html: string[] = []
  const text: string[] = []
  for (let i = 0; i < selection.rangeCount; i += 1) {
    const rich = rangeToRichText(selection.getRangeAt(i))
    if (!rich) continue
    html.push(rich.html)
    text.push(rich.text)
  }
  if (text.length === 0) return null
  return { html: wrapHtml(html.join('')), text: text.join('\n\n').trim() }
}

/** HTML and plain text for one Range, without the clipboard wrapper.
 *  Exported for tests. */
export function rangeToRichText(range: Range): RichText | null {
  if (range.collapsed) return null
  const frag = contextualFragment(range)
  const text = fragmentToText(frag, true)
  if (!text) return null
  return { html: serializeHtmlChildren(frag).trim(), text }
}

/** A whole message's markdown, rendered the way the pane paints it. Used
 *  when the menu copies a message with nothing selected. */
export function markdownToRichText(markdown: string): RichText | null {
  if (!markdown.trim()) return null
  const host = document.createElement('div')
  host.className = 'md'
  host.innerHTML = renderMarkdown(markdown)
  const range = document.createRange()
  range.selectNodeContents(host)
  const rich = rangeToRichText(range)
  return rich ? { html: wrapHtml(rich.html), text: rich.text } : null
}

/** Text the pane shows as typed (the user's bubble), kept as typed. */
export function textToRichText(text: string): RichText | null {
  const body = text.trim()
  if (!body) return null
  return { html: wrapHtml(escapeHtml(body).replace(/\n/g, '<br>')), text: body }
}

/** True when the selection has at least one character inside `root`. Used
 *  to decide whether the message menu should offer "copy selection". */
export function selectionIntersects(selection: Selection | null, root: Node | null): boolean {
  if (!selection || !root || selection.isCollapsed || selection.rangeCount === 0) return false
  for (let i = 0; i < selection.rangeCount; i += 1) {
    if (selection.getRangeAt(i).intersectsNode(root)) return true
  }
  return false
}

/** Write to the clipboard, resolving false when the platform refuses
 *  (clipboard access is blocked in some dev/webview contexts). */
export async function writeClipboard(text: string): Promise<boolean> {
  if (!text) return false
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

/** Write HTML and plain text together, so a rich target (Docs, Slack,
 *  Notion, mail) keeps the formatting and a plain one gets clean text.
 *  Falls back to plain text when the platform refuses the HTML. */
export async function writeRichClipboard(rich: RichText | null): Promise<boolean> {
  if (!rich?.text) return false
  try {
    await navigator.clipboard.write([
      new ClipboardItem({
        'text/html': new Blob([rich.html], { type: 'text/html' }),
        'text/plain': new Blob([rich.text], { type: 'text/plain' }),
      }),
    ])
    return true
  } catch {
    return writeClipboard(rich.text)
  }
}

// ── Range → contextual fragment ────────────────────────────────────────────

/** `Range.cloneContents()` keeps every element *between* the common
 *  ancestor and the two boundaries, but drops everything above it — so a
 *  selection inside one table cell comes back as a bare text node, and one
 *  inside a code block loses its fence. Re-wrap the clone in shallow copies
 *  of each ancestor up to the message root to put that context back. */
function contextualFragment(range: Range): DocumentFragment {
  const doc = range.startContainer.ownerDocument ?? document
  const frag = doc.createDocumentFragment()
  let current: Node = range.cloneContents()

  const anchor = range.commonAncestorContainer
  let el: Element | null =
    anchor.nodeType === ELEMENT_NODE ? (anchor as Element) : anchor.parentElement
  // `.md` is the rendered-markdown body; `.selectable` also covers the
  // plain-text user bubble. Selections spanning whole messages match
  // neither and need no re-wrap — cloneContents already has their blocks.
  const root = el ? el.closest('.md, .selectable') : null

  while (el && root && el !== root && root.contains(el)) {
    const wrapper = el.cloneNode(false) as Element
    wrapper.appendChild(current)
    current = wrapper
    el = el.parentElement
  }
  frag.appendChild(current)
  return frag
}

// ── Serializer ─────────────────────────────────────────────────────────────

function fragmentToText(frag: DocumentFragment, plain: boolean): string {
  const ctx: Ctx = { plain, listDepth: 0, codeBlocks: [] }
  return restoreCode(normalize(serializeChildren(frag, ctx)), ctx)
}

function serializeChildren(node: Node, ctx: Ctx): string {
  let out = ''
  const kids = node.childNodes
  for (let i = 0; i < kids.length; i += 1) out += serialize(kids[i], ctx)
  return out
}

/** Wrap a block so it's separated from its neighbours by a blank line.
 *  `normalize` collapses the resulting runs down to exactly one. */
function block(inner: string): string {
  const body = inner.trim()
  return body ? `\n\n${body}\n\n` : ''
}

function isBlockNode(node: Node | null): boolean {
  if (!node || node.nodeType !== ELEMENT_NODE) return false
  const el = node as Element
  return BLOCK_TAGS.has(el.tagName.toUpperCase()) || el.classList.contains('katex-display')
}

/** A text node's contribution, with runs of whitespace collapsed. */
function textValue(node: Node): string {
  const raw = node.nodeValue ?? ''
  if (!raw) return ''
  if (!raw.trim()) {
    // Whitespace between two inline siblings is a real word gap;
    // between (or outside) blocks it's just HTML source formatting.
    const inline = !isBlockNode(node.previousSibling) &&
      !isBlockNode(node.nextSibling) &&
      !!node.previousSibling &&
      !!node.nextSibling
    return inline ? ' ' : ''
  }
  return raw.replace(/\s+/g, ' ')
}

function serialize(node: Node, ctx: Ctx): string {
  if (node.nodeType === TEXT_NODE) return textValue(node)
  if (node.nodeType !== ELEMENT_NODE) return ''

  const el = node as Element
  if (el.hasAttribute(SKIP_ATTR)) return ''

  // KaTeX first — its glyph spans would otherwise serialize as scrambled
  // letters, and `.katex-mathml` would duplicate the whole expression.
  if (el.classList.contains('katex-display')) {
    const tex = texOf(el)
    return tex ? block(`$$\n${tex}\n$$`) : block(katexFallback(el))
  }
  if (el.classList.contains('katex')) {
    const tex = texOf(el)
    return tex ? `$${tex}$` : katexFallback(el)
  }
  if (el.classList.contains('katex-mathml')) return ''
  if (el.classList.contains('math-fallback')) {
    const tex = (el.textContent ?? '').trim()
    return tex ? `$${tex}$` : ''
  }

  const tag = el.tagName.toUpperCase()
  switch (tag) {
    case 'BR':
      return '\n'
    case 'HR':
      return block('---')
    case 'P':
      return block(serializeChildren(el, ctx))
    case 'H1':
    case 'H2':
    case 'H3':
    case 'H4':
    case 'H5':
    case 'H6': {
      const heading = serializeChildren(el, ctx).trim()
      return block(ctx.plain ? heading : `${'#'.repeat(Number(tag[1]))} ${heading}`)
    }
    case 'STRONG':
    case 'B':
      return wrapInline(serializeChildren(el, ctx), ctx.plain ? '' : '**')
    case 'EM':
    case 'I':
      return wrapInline(serializeChildren(el, ctx), ctx.plain ? '' : '*')
    case 'DEL':
    case 'S':
    case 'STRIKE':
      return wrapInline(serializeChildren(el, ctx), ctx.plain ? '' : '~~')
    case 'CODE': {
      // A <code> inside <pre> never reaches here — the PRE branch consumes
      // it — so this is always an inline span.
      const text = el.textContent ?? ''
      if (!text || ctx.plain) return text
      const ticks = '`'.repeat(longestBacktickRun(text) + 1)
      const pad = text.startsWith('`') || text.endsWith('`') ? ' ' : ''
      return `${ticks}${pad}${text}${pad}${ticks}`
    }
    case 'PRE': {
      const codeEl = el.querySelector('code')
      const text = (codeEl ?? el).textContent ?? ''
      if (ctx.plain) {
        ctx.codeBlocks.push(text.replace(/\n+$/, ''))
        return block(CODE_SENTINEL(ctx.codeBlocks.length - 1))
      }
      const lang = codeEl?.getAttribute('class')?.match(/lang-([\w+#.-]+)/)?.[1] ?? ''
      const fence = '`'.repeat(Math.max(3, longestBacktickRun(text) + 1))
      const fenced = `${fence}${lang}\n${text.replace(/\n+$/, '')}\n${fence}`
      ctx.codeBlocks.push(fenced)
      return block(CODE_SENTINEL(ctx.codeBlocks.length - 1))
    }
    case 'A': {
      const label = serializeChildren(el, ctx).trim()
      const href = el.getAttribute('href') ?? ''
      if (!href) return label
      if (!label || label === href || ctx.plain) return label || href
      return `[${label}](${href})`
    }
    case 'UL':
    case 'OL':
      return block(serializeList(el, ctx))
    case 'LI':
      // Only reachable if a list item was re-wrapped without its parent.
      return block(`${ctx.plain ? '•' : '-'} ${serializeChildren(el, ctx).trim()}`)
    case 'BLOCKQUOTE': {
      const body = serializeChildren(el, ctx).trim()
      if (!body) return ''
      return block(body.split('\n').map((l) => (l ? `> ${l}` : '>')).join('\n'))
    }
    case 'TABLE':
      return block(serializeTable(el, ctx))
    case 'SVG':
    case 'SCRIPT':
    case 'STYLE':
    case 'MATH':
      return ''
    case 'DIV':
    case 'SECTION':
    case 'ARTICLE':
      // Structural wrappers around non-markdown content (tool chips,
      // system cards). Keep their text but don't run it together.
      return block(serializeChildren(el, ctx))
    default:
      // Unknown inline decoration — <mark class="search-hit">, the kanban
      // card-mention chip, <span>. Unwrap.
      return serializeChildren(el, ctx)
  }
}

/** Keep surrounding whitespace outside the emphasis markers — `** bold **`
 *  is not bold in any markdown flavour. */
function wrapInline(inner: string, marker: string): string {
  if (!inner.trim()) return inner
  const lead = /^\s*/.exec(inner)?.[0] ?? ''
  const tail = /\s*$/.exec(inner)?.[0] ?? ''
  return `${lead}${marker}${inner.trim()}${marker}${tail}`
}

function serializeList(listEl: Element, ctx: Ctx): string {
  const ordered = listEl.tagName.toUpperCase() === 'OL'
  const start = Number(listEl.getAttribute('start') ?? '1')
  let n = Number.isFinite(start) && start > 0 ? start : 1
  const indent = '  '.repeat(ctx.listDepth)
  const rows: string[] = []

  for (const child of Array.from(listEl.children)) {
    if (child.tagName.toUpperCase() !== 'LI') continue
    const marker = ordered ? `${n++}. ` : ctx.plain ? '• ' : '- '
    const body = serializeChildren(child, { ...ctx, listDepth: ctx.listDepth + 1 })
      .trim()
      .split('\n')
      // Continuation prose hangs under the marker; a nested list already
      // carries its own indent from the recursive call, so leave it be.
      .map((line, i) => (i === 0 || /^\s/.test(line) ? line : `${' '.repeat(marker.length)}${line}`))
      .join('\n')
    rows.push(`${indent}${marker}${body}`)
  }
  return rows.join('\n')
}

function serializeTable(tableEl: Element, ctx: Ctx): string {
  const aligns: Array<string | null> = []
  const rows: string[][] = []
  let header: string[] | null = null

  for (const tr of Array.from(tableEl.querySelectorAll('tr'))) {
    const cells = Array.from(tr.children).filter((c) => {
      const t = c.tagName.toUpperCase()
      return t === 'TD' || t === 'TH'
    })
    if (cells.length === 0) continue
    const texts = cells.map((c) => {
      const cell = serializeChildren(c, ctx).replace(/\s+/g, ' ').trim()
      return ctx.plain ? cell : cell.replace(/\|/g, '\\|')
    })
    const isHeader = cells.some((c) => c.tagName.toUpperCase() === 'TH')
    if (isHeader && !header) {
      header = texts
      cells.forEach((c, i) => {
        const a = (c as HTMLElement).style?.textAlign || ''
        aligns[i] = a === 'left' || a === 'center' || a === 'right' ? a : null
      })
      continue
    }
    rows.push(texts)
  }

  if (!header) {
    // A partial selection can land on body cells only. One lone cell is
    // just text; anything more gets its first row promoted so the result
    // is still a valid GFM table.
    if (rows.length === 0) return ''
    if (rows.length === 1 && rows[0].length === 1) return rows[0][0]
    header = rows.shift() as string[]
  }

  // Plain text: one tab between cells, which spreadsheets split into columns.
  if (ctx.plain) return [header, ...rows].map((cells) => cells.join('\t')).join('\n')

  const width = Math.max(header.length, ...rows.map((r) => r.length), 1)
  const pad = (cells: string[]) => {
    const out = cells.slice()
    while (out.length < width) out.push('')
    return out
  }
  const sep = Array.from({ length: width }, (_, i) => {
    const a = aligns[i]
    if (a === 'center') return ':---:'
    if (a === 'right') return '---:'
    if (a === 'left') return ':---'
    return '---'
  })
  const line = (cells: string[]) => `| ${pad(cells).join(' | ')} |`
  return [line(header), `| ${sep.join(' | ')} |`, ...rows.map(line)].join('\n')
}

// ── HTML serializer ────────────────────────────────────────────────────────

/** Tags copied as themselves. Everything else unwraps to its children,
 *  and no attribute survives except the few below, so the pane's classes
 *  and dark-theme styles never reach the paste target. */
const HTML_TAGS = new Set([
  'P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'STRONG', 'B', 'EM', 'I', 'DEL',
  'S', 'STRIKE', 'UL', 'OL', 'LI', 'BLOCKQUOTE', 'TABLE', 'THEAD', 'TBODY',
  'TR', 'TH', 'TD', 'DIV',
])

function serializeHtmlChildren(node: Node): string {
  let out = ''
  const kids = node.childNodes
  for (let i = 0; i < kids.length; i += 1) out += serializeHtml(kids[i])
  return out
}

function serializeHtml(node: Node): string {
  if (node.nodeType === TEXT_NODE) return escapeHtml(textValue(node))
  if (node.nodeType !== ELEMENT_NODE) return ''

  const el = node as Element
  if (el.hasAttribute(SKIP_ATTR)) return ''

  // Rich targets can't take KaTeX's positioned glyphs, so math goes out
  // as its TeX source, the same as the markdown copy.
  if (el.classList.contains('katex-display')) {
    const tex = texOf(el) || katexFallback(el)
    return tex ? `<p>${escapeHtml(`$$${tex}$$`)}</p>` : ''
  }
  if (el.classList.contains('katex')) {
    const tex = texOf(el)
    return escapeHtml(tex ? `$${tex}$` : katexFallback(el))
  }
  if (el.classList.contains('katex-mathml')) return ''
  if (el.classList.contains('math-fallback')) {
    const tex = (el.textContent ?? '').trim()
    return tex ? escapeHtml(`$${tex}$`) : ''
  }

  const tag = el.tagName.toUpperCase()
  switch (tag) {
    case 'BR':
      return '<br>'
    case 'HR':
      return '<hr>'
    case 'CODE': {
      const text = el.textContent ?? ''
      return text ? `<code>${escapeHtml(text)}</code>` : ''
    }
    case 'PRE': {
      const text = ((el.querySelector('code') ?? el).textContent ?? '').replace(/\n+$/, '')
      return `<pre><code>${escapeHtml(text)}</code></pre>`
    }
    case 'A': {
      const label = serializeHtmlChildren(el)
      const href = el.getAttribute('href') ?? ''
      if (!href) return label
      return `<a href="${escapeHtml(href)}">${label || escapeHtml(href)}</a>`
    }
    case 'SVG':
    case 'SCRIPT':
    case 'STYLE':
    case 'MATH':
      return ''
  }

  // renderMarkdown pads block contents with newlines; inside an <li> or a
  // cell they would paste as stray spaces.
  const inner = BLOCK_TAGS.has(tag) ? serializeHtmlChildren(el).trim() : serializeHtmlChildren(el)
  if (!HTML_TAGS.has(tag)) return inner
  const name = tag.toLowerCase()
  let attrs = ''
  if (tag === 'OL') {
    const start = Number(el.getAttribute('start') ?? '1')
    if (Number.isFinite(start) && start > 1) attrs = ` start="${start}"`
  } else if (tag === 'TH' || tag === 'TD') {
    const align = (el as HTMLElement).style?.textAlign || ''
    if (align === 'left' || align === 'center' || align === 'right') {
      attrs = ` style="text-align:${align}"`
    }
  }
  return `<${name}${attrs}>${inner}</${name}>`
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** Clipboard HTML carries no charset of its own; without this some macOS
 *  apps read UTF-8 punctuation (em dashes, curly quotes) as Latin-1. */
function wrapHtml(html: string): string {
  return `<meta charset="utf-8">${html}`
}

// ── Helpers ────────────────────────────────────────────────────────────────

/** KaTeX renders with `output: 'htmlAndMathml'`, so the original TeX is
 *  sitting in the MathML annotation — far more reliable than trying to
 *  reassemble it from the positioned glyph spans. */
function texOf(el: Element): string {
  const ann = el.querySelector('annotation[encoding="application/x-tex"]')
  return (ann?.textContent ?? '').trim()
}

/** Selection clipped the annotation away — fall back to the visual glyphs,
 *  which at least reads as the equation rather than as nothing. */
function katexFallback(el: Element): string {
  const html = el.querySelector('.katex-html')
  return (html?.textContent ?? '').trim()
}

function longestBacktickRun(s: string): number {
  let best = 0
  const re = /`+/g
  let m: RegExpExecArray | null
  while ((m = re.exec(s)) !== null) best = Math.max(best, m[0].length)
  return best
}

function normalize(md: string): string {
  return md
    .split(String.fromCharCode(0xa0))
    .join(' ')
    .split('\n')
    .map((line) => line.replace(/[ \t]+$/, ''))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function restoreCode(md: string, ctx: Ctx): string {
  if (ctx.codeBlocks.length === 0) return md
  return md.replace(CODE_SENTINEL_RE, (_m, i) => ctx.codeBlocks[Number(i)] ?? '')
}
