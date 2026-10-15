import type { AppshotMeta } from './events'

/**
 * The `<appshot>` text block that carries a captured window's text to the
 * model. The Python side (`bridge/appshot.py`) writes the same format, and
 * this file reads it back, so a reloaded session can hide the block from the
 * operator's own message.
 *
 *   <appshot app="Safari" bundle-identifier="com.apple.Safari" window-title="..." captured-at="..." text-status="ok">
 *   the window text, with & < > escaped
 *   </appshot>
 *
 * Escaping `<` in the body is what makes the block safe: text on a page
 * cannot close the tag early, so the first `</appshot>` always ends it.
 */

type BlockMeta = Pick<
  AppshotMeta,
  'app' | 'bundleId' | 'windowTitle' | 'capturedAt' | 'textStatus'
>

export interface ParsedAppshotBlock {
  app: string
  bundleId: string
  windowTitle: string
  capturedAt: string
  textStatus: string
  /** The body with the escapes undone. */
  text: string
  /** Where the whole block sits in the source string, end exclusive. */
  start: number
  end: number
}

/** Escape text for the body of a block: `&`, `<` and `>`. */
export function escapeXml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** Escape text for an attribute value: the body escapes plus `"`. */
export function escapeXmlAttr(s: string): string {
  return escapeXml(s).replace(/"/g, '&quot;')
}

/** Undo `escapeXml` and `escapeXmlAttr`. `&amp;` goes last so `&amp;lt;`
 *  comes back as the literal text `&lt;` and not as `<`. */
export function unescapeXml(s: string): string {
  return s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
}

export function formatAppshotBlock(meta: BlockMeta, text: string): string {
  const attrs = [
    ['app', meta.app],
    ['bundle-identifier', meta.bundleId],
    ['window-title', meta.windowTitle],
    ['captured-at', meta.capturedAt],
    ['text-status', meta.textStatus],
  ]
    .map(([k, v]) => `${k}="${escapeXmlAttr(v)}"`)
    .join(' ')
  return `<appshot ${attrs}>\n${escapeXml(text)}\n</appshot>`
}

const BLOCK_RE = /<appshot((?:\s+[a-z-]+="[^"]*")*)\s*>\n?([\s\S]*?)\n?<\/appshot>/g
const ATTR_RE = /([a-z-]+)="([^"]*)"/g

export function parseAppshotBlocks(source: string): ParsedAppshotBlock[] {
  const out: ParsedAppshotBlock[] = []
  for (const m of source.matchAll(BLOCK_RE)) {
    const attrs: Record<string, string> = {}
    for (const a of m[1].matchAll(ATTR_RE)) attrs[a[1]] = unescapeXml(a[2])
    out.push({
      app: attrs['app'] ?? '',
      bundleId: attrs['bundle-identifier'] ?? '',
      windowTitle: attrs['window-title'] ?? '',
      capturedAt: attrs['captured-at'] ?? '',
      textStatus: attrs['text-status'] ?? '',
      text: unescapeXml(m[2]),
      start: m.index ?? 0,
      end: (m.index ?? 0) + m[0].length,
    })
  }
  return out
}

/** One line for where a block was: `[Appshot: Safari, "Page title"]`. */
export function appshotPlaceholder(app: string, windowTitle: string): string {
  if (!app) return '[Appshot]'
  return windowTitle ? `[Appshot: ${app}, "${windowTitle}"]` : `[Appshot: ${app}]`
}

/** Replace every block with its one-line placeholder, for showing a user
 *  turn that the bridge stored with the block in it. The blank lines the
 *  bridge put around a block go with it. */
export function stripAppshotBlocks(text: string): string {
  if (!text.includes('<appshot')) return text
  return text
    .replace(BLOCK_RE, (_block, attrs: string) => {
      const a: Record<string, string> = {}
      for (const m of attrs.matchAll(ATTR_RE)) a[m[1]] = unescapeXml(m[2])
      return appshotPlaceholder(a['app'] ?? '', a['window-title'] ?? '')
    })
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}
