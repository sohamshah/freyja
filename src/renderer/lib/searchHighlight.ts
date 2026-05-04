/**
 * Helpers for in-conversation text search. We highlight matches by
 * wrapping them in `<mark class="search-hit">` elements — both inside
 * markdown-rendered assistant HTML and inside plain-text user messages.
 *
 * Match indices are deliberately NOT embedded here: we count matches via
 * a DOM query after render, which is robust to streaming text and avoids
 * having to thread "starting match index" through the component tree.
 */

export function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * Wrap every case-insensitive match of `query` inside an HTML string
 * with `<mark class="search-hit">…</mark>`. Skips text inside <script>,
 * <style>, and pre-existing <mark> elements so we never double-wrap.
 */
export function highlightHtml(html: string, query: string): string {
  const q = query.trim()
  if (!q) return html
  try {
    const doc = new DOMParser().parseFromString(
      `<div>${html}</div>`,
      'text/html',
    )
    const root = doc.body.firstElementChild
    if (!root) return html

    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    const textNodes: Text[] = []
    let node: Node | null
    while ((node = walker.nextNode())) {
      const parent = (node as Text).parentNode as Element | null
      if (!parent) continue
      const tag = parent.tagName
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'MARK') continue
      textNodes.push(node as Text)
    }

    const re = new RegExp(escapeRegex(q), 'gi')
    for (const textNode of textNodes) {
      const text = textNode.textContent ?? ''
      re.lastIndex = 0
      if (!re.test(text)) continue
      re.lastIndex = 0
      const frag = doc.createDocumentFragment()
      let lastIdx = 0
      let m: RegExpExecArray | null
      while ((m = re.exec(text)) !== null) {
        if (m.index > lastIdx) {
          frag.appendChild(doc.createTextNode(text.slice(lastIdx, m.index)))
        }
        const mark = doc.createElement('mark')
        mark.className = 'search-hit'
        mark.textContent = m[0]
        frag.appendChild(mark)
        lastIdx = m.index + m[0].length
      }
      if (lastIdx < text.length) {
        frag.appendChild(doc.createTextNode(text.slice(lastIdx)))
      }
      textNode.parentNode?.replaceChild(frag, textNode)
    }

    return root.innerHTML
  } catch {
    // If the HTML is malformed or DOMParser fails, fall back to the
    // unhighlighted original so the conversation still renders.
    return html
  }
}

/**
 * Split plain text into an array of { text, isHit } runs for rendering
 * as a React fragment. Preserves case in the hit text.
 */
export function highlightRuns(
  text: string,
  query: string,
): Array<{ text: string; isHit: boolean }> {
  const q = query.trim()
  if (!q) return [{ text, isHit: false }]
  const out: Array<{ text: string; isHit: boolean }> = []
  const re = new RegExp(escapeRegex(q), 'gi')
  let lastIdx = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIdx) {
      out.push({ text: text.slice(lastIdx, m.index), isHit: false })
    }
    out.push({ text: m[0], isHit: true })
    lastIdx = m.index + m[0].length
  }
  if (lastIdx < text.length) {
    out.push({ text: text.slice(lastIdx), isHit: false })
  }
  return out
}
