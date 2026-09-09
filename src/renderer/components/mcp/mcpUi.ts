import type { McpStateTone } from '../../lib/mcp'

/** Open a URL in the system browser via the preload bridge; falls back
 *  to window.open when running outside Electron (smoke tests, vite dev). */
export function openExternalUrl(url: string): void {
  if (!url) return
  const api = typeof window !== 'undefined' ? (window as any).harness : undefined
  if (api?.openExternal) {
    try {
      void api.openExternal(url)
      return
    } catch {
      // fall through
    }
  }
  if (typeof window !== 'undefined' && typeof window.open === 'function') {
    window.open(url, '_blank', 'noopener')
  }
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // ignore, fall through
  }
  return false
}

export const TONE_TEXT: Record<McpStateTone, string> = {
  ok: 'text-ok',
  accent: 'text-accent',
  warn: 'text-warn',
  danger: 'text-danger',
  muted: 'text-fg-2',
}

export const TONE_PILL: Record<McpStateTone, string> = {
  ok: 'text-ok bg-ok/10 ring-ok/30',
  accent: 'text-accent bg-accent/10 ring-accent/30',
  warn: 'text-warn bg-warn/10 ring-warn/30',
  danger: 'text-danger bg-danger/10 ring-danger/30',
  muted: 'text-fg-2 bg-white/[0.04] ring-white/10',
}

export const BTN =
  'rounded-md bg-white/[0.04] px-2 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0 disabled:opacity-40 disabled:hover:bg-white/[0.04]'
export const BTN_ACCENT =
  'rounded-md bg-accent/15 px-2.5 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/40 hover:bg-accent/25 disabled:opacity-40'
export const BTN_DANGER =
  'rounded-md bg-danger/15 px-2.5 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] text-danger ring-1 ring-danger/40 hover:bg-danger/25 disabled:opacity-40'
export const INPUT =
  'w-full rounded-md bg-black/40 px-2 py-1 font-mono text-[11px] text-fg-0 ring-hairline placeholder:text-fg-3 focus:outline-none focus:ring-accent/40'

/** Seconds → "m:ss" (or "h:mm:ss"). */
export function formatCountdown(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  return `${h > 0 ? `${h}:` : ''}${mm}:${String(sec).padStart(2, '0')}`
}
