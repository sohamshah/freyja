import { useEffect, useMemo, useRef, useState } from 'react'
import { DatelineTS } from './memory/primitives'

/**
 * RecallPanel — "The Morgue" (Grounded Memory Surface 2).
 *
 * A controlled recall drawer over the verbatim, pre-compaction transcript
 * archive (raw_messages.jsonl). Compaction condenses/evicts old turns from the
 * live context, but every message is preserved verbatim; this drawer is the UI
 * face of the `recall` tool — grep the archive and read back what the summary
 * dropped.
 *
 * The surface reads as old newsroom clippings filed down a left time-spine:
 *   ⌕ search field (accent caret) → debounced query
 *   reverse-chron clipping rows, each with a left role-mark (assistant ◆ /
 *   user ◂ / system ⊡), the snippet (matched query lit in accent), and a
 *   right-aligned DatelineTS. Day boundaries get a heavier rule + uppercase
 *   fg-3 dateline. Empty query falls back to the recent timeline — never blank.
 *
 * Motion budget: rows fade in once, staggered ~30ms, gated on
 * prefers-reduced-motion. Nothing pulses here — the morgue is still.
 */

const DEBOUNCE_MS = 250
const ROW_STAGGER_MS = 30

type RecallRow = {
  role: string
  turn_id: string | null
  ts: number
  text: string
  snippet: string
}

type RecallResponse = {
  ok: boolean
  rows?: RecallRow[]
  error?: string
}

/** Left-margin role mark — assistant ◆ (fg-2) / user ◂ (accent) / system ⊡
 *  (fg-3). The user's mark is the only accent-tinted glyph so a scan of the
 *  spine reads who-said-what at a glance. */
function roleMark(role: string): { glyph: string; color: string; label: string } {
  switch ((role || '').toLowerCase()) {
    case 'user':
      return { glyph: '◂', color: 'text-accent', label: 'user' }
    case 'system':
      return { glyph: '⊡', color: 'text-fg-3', label: 'system' }
    case 'assistant':
    default:
      return { glyph: '◆', color: 'text-fg-2', label: 'assistant' }
  }
}

/** Split a snippet around case-insensitive matches of `query`, lighting the
 *  matched runs in accent over a faint selection tint. Returns the raw text
 *  unhighlighted when there's no query. */
function highlight(text: string, query: string): React.ReactNode {
  const q = query.trim()
  if (!q) return text
  const out: React.ReactNode[] = []
  const lo = text.toLowerCase()
  const needle = q.toLowerCase()
  let i = 0
  let key = 0
  while (i < text.length) {
    const idx = lo.indexOf(needle, i)
    if (idx < 0) {
      out.push(text.slice(i))
      break
    }
    if (idx > i) out.push(text.slice(i, idx))
    out.push(
      <span key={key++} className="rounded-[2px] bg-accent/[0.12] px-[1px] text-accent">
        {text.slice(idx, idx + needle.length)}
      </span>,
    )
    i = idx + needle.length
  }
  return out
}

/** YYYY-MM-DD-ish day key + an uppercase dateline ("today" / "yesterday" /
 *  a short date) used to bucket the spine into days. */
function dayKey(ts: number): string {
  const d = new Date(ts)
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}
function dayLabel(ts: number): string {
  const d = new Date(ts)
  const now = new Date()
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  return d
    .toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    .toLowerCase()
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

export interface RecallPanelProps {
  open: boolean
  onClose: () => void
  sessionId: string | null
  initialQuery?: string
}

export function RecallPanel({ open, onClose, sessionId, initialQuery = '' }: RecallPanelProps) {
  const [query, setQuery] = useState(initialQuery)
  const [debounced, setDebounced] = useState(initialQuery)
  const [rows, setRows] = useState<RecallRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  const reduceMotion = useMemo(prefersReducedMotion, [])

  // Seed the field from initialQuery each time the drawer opens, and focus it.
  useEffect(() => {
    if (!open) return
    setQuery(initialQuery)
    setDebounced(initialQuery)
    const t = window.setTimeout(() => inputRef.current?.focus(), 0)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialQuery])

  // Close on Escape while open.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // Debounce the query (~250ms). The trimmed debounced value is what we fetch.
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(query), DEBOUNCE_MS)
    return () => window.clearTimeout(t)
  }, [query])

  // Fetch on debounced query (NOT on activityTick — the morgue is a read of the
  // durable archive, not a live feed). Empty query → recent timeline fallback.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    const q = debounced.trim()
    const run = async () => {
      const api = (window as any).harness
      if (!api?.getRecall || !sessionId) {
        setRows([])
        setLoading(false)
        return
      }
      setLoading(true)
      try {
        const res: RecallResponse = await api.getRecall(sessionId, q || undefined)
        if (cancelled) return
        if (!res?.ok) {
          setError(res?.error ?? 'archive unavailable')
          setRows([])
          return
        }
        setError(null)
        setRows((res.rows ?? []) as RecallRow[])
      } catch (err) {
        if (!cancelled) {
          setError(String(err))
          setRows([])
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [open, sessionId, debounced])

  // Reverse-chron (newest first), then bucket into day groups so we can drop a
  // heavier rule + dateline at each boundary.
  const groups = useMemo(() => {
    const sorted = [...rows].sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))
    const out: { key: string; label: string; rows: RecallRow[] }[] = []
    for (const r of sorted) {
      const k = dayKey(r.ts ?? 0)
      const last = out[out.length - 1]
      if (last && last.key === k) last.rows.push(r)
      else out.push({ key: k, label: dayLabel(r.ts ?? 0), rows: [r] })
    }
    return out
  }, [rows])

  const q = debounced.trim()
  const hasQuery = q.length > 0
  const isEmpty = !loading && !error && groups.length === 0

  if (!open) return null

  return (
    <div className="fixed inset-0 z-30 flex justify-end" role="dialog" aria-label="Recall archive">
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-[1px]"
        onClick={onClose}
        aria-hidden="true"
      />
      <div className="relative flex h-full w-[560px] flex-col glass-panel glass-strong ring-hairline-strong">
        {/* Header — title slug + the search field with its accent caret. */}
        <div className="shrink-0 px-4 pb-3 pt-3 hairline-b">
          <div className="flex items-baseline justify-between gap-2">
            <div className="flex items-baseline gap-2">
              <span className="font-serif text-[14px] font-light italic leading-none text-fg-0">
                the morgue
              </span>
              <span className="label">recall</span>
            </div>
            <button
              onClick={onClose}
              className="rounded bg-white/[0.05] px-2 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
            >
              close ✕
            </button>
          </div>

          {/* Search field — ⌕ glyph + accent caret-blink when focused/empty. */}
          <label className="mt-3 flex items-center gap-2 rounded-md bg-white/[0.03] px-2.5 py-1.5 ring-hairline focus-within:ring-hairline-strong">
            <span className="shrink-0 select-none font-mono text-[12px] text-accent" aria-hidden="true">
              ⌕
            </span>
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="search the record…"
              spellCheck={false}
              autoComplete="off"
              className="min-w-0 flex-1 bg-transparent font-mono text-[11px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
            />
            <span
              aria-hidden="true"
              className={`shrink-0 font-mono text-[11px] text-accent ${
                query.length === 0 ? 'animate-caret-blink' : 'opacity-0'
              }`}
            >
              ▋
            </span>
          </label>

          <div className="mt-2 label">
            {hasQuery ? `clippings «${q}»` : 'recent dispatches'}
          </div>
        </div>

        {/* Spine — faint 1px left time-spine the rows hang off of. */}
        <div className="relative min-h-0 flex-1 overflow-y-auto">
          <div
            className="pointer-events-none absolute bottom-0 left-[26px] top-0 w-px bg-white/[0.06]"
            aria-hidden="true"
          />

          {error ? (
            <div className="px-4 py-4 font-mono text-[11px] italic text-fg-3">
              archive unavailable{error && error !== 'archive unavailable' ? ` — ${error}` : ''}
            </div>
          ) : loading ? (
            <GhostRows />
          ) : isEmpty ? (
            <div className="px-4 py-4 font-mono text-[11px] italic text-fg-3">
              {hasQuery ? `No clipping matches «${q}».` : 'No archived dispatches yet.'}
            </div>
          ) : (
            <div className="pb-6">
              {groups.map((group, gi) => {
                // Continuous stagger index across groups so the cascade reads
                // as one filing motion down the spine.
                const before = groups
                  .slice(0, gi)
                  .reduce((n, g) => n + g.rows.length, 0)
                return (
                  <div key={group.key}>
                    <DayBoundary label={group.label} ts={group.rows[0]?.ts ?? 0} />
                    {group.rows.map((row, ri) => (
                      <ClippingRow
                        key={`${row.turn_id ?? 'r'}-${row.ts}-${ri}`}
                        row={row}
                        query={q}
                        index={before + ri}
                        reduceMotion={reduceMotion}
                      />
                    ))}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** Heavier day-boundary rule + an uppercase fg-3 dateline + a right-aligned
 *  DatelineTS for the day's first (newest) clipping. */
function DayBoundary({ label, ts }: { label: string; ts: number }) {
  return (
    <div className="flex items-baseline justify-between gap-2 px-4 pb-1.5 pt-3 hairline-b">
      <span className="pl-[14px] font-mono text-[9px] uppercase tracking-[0.14em] text-fg-3">
        {label}
      </span>
      {ts > 0 && <DatelineTS ts={ts} />}
    </div>
  )
}

/** One filed clipping: role-mark in the spine gutter, the snippet (matched
 *  query lit), and a right-aligned DatelineTS. Fades in once. */
function ClippingRow({
  row,
  query,
  index,
  reduceMotion,
}: {
  row: RecallRow
  query: string
  index: number
  reduceMotion: boolean
}) {
  const mark = roleMark(row.role)
  const body = (row.snippet ?? row.text ?? '').trim()
  return (
    <div
      className={`group flex items-start gap-2 px-4 py-2 hairline-b ${
        reduceMotion ? '' : 'animate-fade-in'
      }`}
      style={reduceMotion ? undefined : { animationDelay: `${Math.min(index, 12) * ROW_STAGGER_MS}ms` }}
    >
      {/* Role mark sits centered on the spine (left-[26px] + ~14px gutter). */}
      <span
        title={mark.label}
        aria-hidden="true"
        className={`relative z-[1] mt-[1px] inline-flex w-[18px] shrink-0 select-none justify-center font-mono text-[11px] leading-[1.5] ${mark.color}`}
      >
        {mark.glyph}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-mono text-[8.5px] uppercase tracking-[0.08em] text-fg-3">
            {mark.label}
          </span>
          <DatelineTS ts={row.ts ?? 0} />
        </div>
        <div className="mt-0.5 whitespace-pre-wrap break-words font-mono text-[11px] leading-[1.55] text-fg-1">
          {highlight(body, query)}
        </div>
      </div>
    </div>
  )
}

/** Loading state — 3 ghost clipping rows; the first carries a faint shimmer. */
function GhostRows() {
  return (
    <div aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex items-start gap-2 px-4 py-2 hairline-b">
          <span className="mt-[1px] inline-flex w-[18px] shrink-0 justify-center font-mono text-[11px] text-fg-4">
            ◆
          </span>
          <div className="min-w-0 flex-1 space-y-1.5 py-0.5">
            <div
              className={`h-[7px] w-1/3 rounded bg-white/[0.05] ${i === 0 ? 'animate-shimmer' : ''}`}
            />
            <div className="h-[9px] w-11/12 rounded bg-white/[0.04]" />
            <div className="h-[9px] w-3/5 rounded bg-white/[0.03]" />
          </div>
        </div>
      ))}
    </div>
  )
}

export default RecallPanel
