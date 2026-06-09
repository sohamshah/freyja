// Tiny dependency-free markdown → HTML renderer. Supports the subset that
// Freyja responses actually use:
//   - ATX headings (#, ##, ###, ####)
//   - Paragraphs + hard line breaks
//   - Fenced code blocks (``` or ~~~)
//   - Inline code, bold, italic, strikethrough, links
//   - Bullet / numbered lists (no nesting yet)
//   - Blockquotes
//   - Horizontal rules (---, ***, ___)
//   - GitHub-flavored tables with alignment
//   - LaTeX math via KaTeX: inline $…$ / \(…\) and display $$…$$ / \[…\]
//
// All HTML is escaped before inline processing.

import katex from 'katex'

const ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ESCAPE_MAP[c] ?? c)
}

// ── Math (KaTeX) ───────────────────────────────────────────────────────────
// Math is extracted from the raw source into sentinel placeholders BEFORE any
// markdown/escaping runs, so `_`, `^`, `\`, `*`, `<`, `&` inside TeX are never
// mangled by the inline-styling or HTML-escaping passes. The rendered KaTeX
// HTML is spliced back in as the very last step.
//
// Streaming-safe by construction: every delimiter regex REQUIRES its closing
// token, so a half-streamed `$$\pi` simply doesn't match and stays as literal
// text until the closing `$$` arrives — no flicker, no thrown errors mid-token.
//
// Sentinels use NUL bytes (never present in model output) so they can't
// collide with real content and survive line-splitting + paragraph joins.
const MATH_INLINE_SENTINEL = (n: number) => `\u0000MATHI${n}\u0000`
const MATH_DISPLAY_SENTINEL = (n: number) => `\u0000MATHD${n}\u0000`

function renderTex(tex: string, displayMode: boolean): string {
  try {
    return katex.renderToString(tex.trim(), {
      displayMode,
      throwOnError: false,
      output: 'htmlAndMathml',
      strict: false,
      trust: false,
    })
  } catch {
    // throwOnError:false already handles parse errors; this only fires on
    // truly unexpected failures. Fall back to the raw source as plain text.
    const wrap = displayMode ? 'div' : 'span'
    return `<${wrap} class="math-fallback">${esc(tex)}</${wrap}>`
  }
}

interface MathStore {
  inline: string[]
  display: string[]
}

/** Pull every math span out of the raw source, replacing each with a
 *  sentinel. Display delimiters are matched first so a `$$` block isn't
 *  shredded by the inline `$` rule. Returns the rewritten source plus the
 *  rendered-HTML store keyed by sentinel index. */
function protectMath(src: string): { text: string; store: MathStore } {
  const store: MathStore = { inline: [], display: [] }
  let out = src

  // Display: $$…$$ and \[…\] (may span multiple lines).
  out = out.replace(/\$\$([\s\S]+?)\$\$/g, (_m, body) => {
    store.display.push(renderTex(body, true))
    return MATH_DISPLAY_SENTINEL(store.display.length - 1)
  })
  out = out.replace(/\\\[([\s\S]+?)\\\]/g, (_m, body) => {
    store.display.push(renderTex(body, true))
    return MATH_DISPLAY_SENTINEL(store.display.length - 1)
  })

  // Inline: \(…\) — unambiguous, no currency confusion.
  out = out.replace(/\\\(([\s\S]+?)\\\)/g, (_m, body) => {
    store.inline.push(renderTex(body, false))
    return MATH_INLINE_SENTINEL(store.inline.length - 1)
  })

  // Inline: $…$ — guarded against currency ("$5 and $10"). The opener may
  // not be preceded by `$`/`\`, no whitespace may hug either delimiter, and
  // a closing `$` may not be immediately followed by a digit. Backslash
  // escapes inside the body are allowed (\$, \%, etc.).
  out = out.replace(
    /(?<![\\$])\$(?!\s)((?:\\.|[^$\\\n])+?)(?<!\s)\$(?!\d)/g,
    (_m, body) => {
      store.inline.push(renderTex(body, false))
      return MATH_INLINE_SENTINEL(store.inline.length - 1)
    },
  )

  return { text: out, store }
}

/** Splice rendered KaTeX back into the final HTML. Display sentinels that
 *  ended up wrapped in their own `<p>` are unwrapped so the block-level
 *  `.katex-display` isn't nested inside a paragraph. */
function restoreMath(html: string, store: MathStore): string {
  let out = html
  out = out.replace(
    /<p>\u0000MATHD(\d+)\u0000<\/p>/g,
    (_m, idx) => store.display[+idx] ?? '',
  )
  out = out.replace(
    /\u0000MATHD(\d+)\u0000/g,
    (_m, idx) => store.display[+idx] ?? '',
  )
  out = out.replace(
    /\u0000MATHI(\d+)\u0000/g,
    (_m, idx) => store.inline[+idx] ?? '',
  )
  return out
}

/**
 * Inline token pass. Order matters: code first (so ** inside code isn't
 * treated as bold), then bold/italic/strike/links. We use placeholders for
 * code spans to avoid downstream re-escaping them.
 */
function inline(src: string): string {
  // Step 1: extract code spans into placeholders so later rules can't touch
  // them. Escape the body of each span.
  const codeSpans: string[] = []
  let out = src.replace(/`+([^`]+)`+/g, (_m, body) => {
    codeSpans.push(`<code>${esc(body)}</code>`)
    return `\u0000CODE${codeSpans.length - 1}\u0000`
  })

  // Step 2: escape the remaining text
  out = esc(out)

  // Step 3: inline styling
  out = out.replace(/\*\*([^*\n]+)\*\*/g, (_m, t) => `<strong>${t}</strong>`)
  out = out.replace(/__([^_\n]+)__/g, (_m, t) => `<strong>${t}</strong>`)
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, (_m, pre, t) => `${pre}<em>${t}</em>`)
  out = out.replace(/(^|[^_])_([^_\n]+)_/g, (_m, pre, t) => `${pre}<em>${t}</em>`)
  out = out.replace(/~~([^~\n]+)~~/g, (_m, t) => `<del>${t}</del>`)
  // Markdown-style links [label](url)
  out = out.replace(
    /\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;([^&]+)&quot;)?\)/g,
    (_m, label, url, title) => {
      const t = title ? ` title="${title}"` : ''
      return `<a href="${url}" target="_blank" rel="noreferrer"${t}>${label}</a>`
    },
  )
  // Auto-link bare URLs that aren't already inside an <a> tag.
  // Matches http(s)://... up to common URL boundary characters.
  out = out.replace(
    /(?<!href="|">)(https?:\/\/[^\s<>"'`,;)\]]+)/g,
    (url) => `<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`,
  )

  // Step 4: reinsert code spans
  out = out.replace(/\u0000CODE(\d+)\u0000/g, (_m, idx) => codeSpans[+idx] ?? '')

  return out
}

type Align = 'left' | 'center' | 'right' | null

function parseTableSeparator(line: string): Align[] | null {
  // Matches separator rows like `| --- | :---: | ---: |`.
  // Relaxed: accept 1+ dashes (models often produce `|--|` or `|:-:|`).
  const stripped = line.trim().replace(/^\||\|$/g, '')
  const cells = stripped.split('|')
  if (cells.length === 0) return null
  const aligns: Align[] = []
  for (const raw of cells) {
    const cell = raw.trim()
    if (!/^:?-+:?$/.test(cell)) return null
    const left = cell.startsWith(':')
    const right = cell.endsWith(':')
    aligns.push(left && right ? 'center' : right ? 'right' : left ? 'left' : null)
  }
  return aligns
}

function splitRow(line: string): string[] {
  // Strip leading/trailing pipes, then split on unescaped pipes.
  const stripped = line.trim().replace(/^\||\|$/g, '')
  const cells: string[] = []
  let buf = ''
  let i = 0
  while (i < stripped.length) {
    const ch = stripped[i]
    if (ch === '\\' && stripped[i + 1] === '|') {
      buf += '|'
      i += 2
      continue
    }
    if (ch === '|') {
      cells.push(buf.trim())
      buf = ''
      i += 1
      continue
    }
    buf += ch
    i += 1
  }
  cells.push(buf.trim())
  return cells
}

export function renderMarkdown(src: string): string {
  if (!src) return ''
  const { text: protectedSrc, store: mathStore } = protectMath(src)
  const lines = protectedSrc.replace(/\r\n/g, '\n').split('\n')
  const out: string[] = []
  let i = 0
  let inList = false
  let listType: 'ul' | 'ol' | null = null

  const closeList = () => {
    if (inList && listType) {
      out.push(`</${listType}>`)
      inList = false
      listType = null
    }
  }

  while (i < lines.length) {
    const line = lines[i]

    // Fenced code block — supports ``` and ~~~, optional language, optional
    // leading indent (up to 3 spaces).
    const fence = line.match(/^\s{0,3}(```+|~~~+)(\w+)?\s*$/)
    if (fence) {
      closeList()
      const marker = fence[1]
      const lang = fence[2] || ''
      const block: string[] = []
      i += 1
      const closeRe = new RegExp(`^\\s{0,3}${marker[0]}{${marker.length},}\\s*$`)
      while (i < lines.length && !closeRe.test(lines[i])) {
        block.push(lines[i])
        i += 1
      }
      i += 1 // consume closing fence
      const langClass = lang ? ` class="lang-${esc(lang)}"` : ''
      out.push(`<pre><code${langClass}>${esc(block.join('\n'))}</code></pre>`)
      continue
    }

    // Horizontal rule
    if (/^\s{0,3}([-*_])\s*(\1\s*){2,}$/.test(line)) {
      closeList()
      out.push('<hr />')
      i += 1
      continue
    }

    // ATX heading (# through ####)
    const h = line.match(/^(#{1,4})\s+(.*?)\s*#*\s*$/)
    if (h) {
      closeList()
      const level = h[1].length
      out.push(`<h${level}>${inline(h[2])}</h${level}>`)
      i += 1
      continue
    }

    // Table — requires header row followed by a separator row.
    if (
      line.includes('|') &&
      i + 1 < lines.length &&
      lines[i + 1].includes('|')
    ) {
      const aligns = parseTableSeparator(lines[i + 1])
      if (aligns) {
        closeList()
        const header = splitRow(line)
        // Pad header to align with separator length.
        while (header.length < aligns.length) header.push('')
        const headerHtml = aligns
          .map((a, idx) => {
            const style = a ? ` style="text-align:${a}"` : ''
            return `<th${style}>${inline(header[idx] ?? '')}</th>`
          })
          .join('')
        const rows: string[] = []
        i += 2 // consume header + separator
        while (
          i < lines.length &&
          lines[i].includes('|') &&
          lines[i].trim() !== ''
        ) {
          const cells = splitRow(lines[i])
          while (cells.length < aligns.length) cells.push('')
          const rowHtml = aligns
            .map((a, idx) => {
              const style = a ? ` style="text-align:${a}"` : ''
              return `<td${style}>${inline(cells[idx] ?? '')}</td>`
            })
            .join('')
          rows.push(`<tr>${rowHtml}</tr>`)
          i += 1
        }
        out.push(
          `<table><thead><tr>${headerHtml}</tr></thead><tbody>${rows.join('')}</tbody></table>`,
        )
        continue
      }
    }

    // Bullet list
    const ul = line.match(/^\s*[-*+]\s+(.*)$/)
    if (ul) {
      if (!inList || listType !== 'ul') {
        closeList()
        out.push('<ul>')
        inList = true
        listType = 'ul'
      }
      out.push(`<li>${inline(ul[1])}</li>`)
      i += 1
      continue
    }

    // Numbered list
    const ol = line.match(/^\s*\d+\.\s+(.*)$/)
    if (ol) {
      if (!inList || listType !== 'ol') {
        closeList()
        out.push('<ol>')
        inList = true
        listType = 'ol'
      }
      out.push(`<li>${inline(ol[1])}</li>`)
      i += 1
      continue
    }

    // Blockquote — collapse consecutive quote lines into one <blockquote>
    if (/^>\s?/.test(line)) {
      closeList()
      const buf: string[] = []
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ''))
        i += 1
      }
      out.push(`<blockquote>${inline(buf.join(' '))}</blockquote>`)
      continue
    }

    // Paragraph / blank
    if (line.trim() === '') {
      closeList()
      out.push('')
      i += 1
      continue
    }

    // Gather paragraph until blank or another block starts
    closeList()
    const para: string[] = [line]
    i += 1
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !/^(#{1,4}\s|```|~~~|>\s?|\s*[-*+]\s|\s*\d+\.\s)/.test(lines[i]) &&
      !(lines[i].includes('|') && i + 1 < lines.length && parseTableSeparator(lines[i + 1]))
    ) {
      para.push(lines[i])
      i += 1
    }
    out.push(`<p>${inline(para.join(' '))}</p>`)
  }

  closeList()
  return restoreMath(out.filter(Boolean).join('\n'), mathStore)
}
