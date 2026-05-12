import React, { useMemo, useState } from 'react'
import type { AgentView, BusEventView, TelemetryEventView } from '../shared/types'

interface Props {
  findings: BusEventView[]
  readEvents: BusEventView[]
  telemetryEvents: TelemetryEventView[]
  agents: AgentView[]
  onCopyFinding: (event: BusEventView) => void
}

type Category =
  | 'finding'
  | 'progress'
  | 'error'
  | 'summary'
  | 'media'
  | 'truncation'
  | 'read'

interface UnifiedEvent {
  id: string
  at: number
  author: string
  category: Category
  title: string
  body?: string
  raw: BusEventView | TelemetryEventView
}

/**
 * Unified chronological feed replacing the old Evidence + History tabs.
 * Bus findings (info / progress / errors), bus reads, telemetry
 * (compactions, media prunings, truncations) all merge into one stream.
 * Filter chips toggle categories. Click an event to expand its body.
 */
export function ActivityView({
  findings,
  readEvents,
  telemetryEvents,
  agents,
  onCopyFinding,
}: Props) {
  const events = useMemo(
    () => unify(findings, readEvents, telemetryEvents),
    [findings, readEvents, telemetryEvents],
  )

  const counts = useMemo(() => {
    const c: Record<Category, number> = {
      finding: 0,
      progress: 0,
      error: 0,
      summary: 0,
      media: 0,
      truncation: 0,
      read: 0,
    }
    for (const e of events) c[e.category]++
    return c
  }, [events])

  const [filters, setFilters] = useState<Set<Category>>(() => new Set(CATEGORIES))
  const [expanded, setExpanded] = useState<string | null>(null)

  const visible = useMemo(
    () => events.filter((e) => filters.has(e.category)),
    [events, filters],
  )

  const toggle = (c: Category) =>
    setFilters((prev) => {
      const next = new Set(prev)
      if (next.has(c)) next.delete(c)
      else next.add(c)
      return next
    })

  const elapsed = elapsedFrom(events)

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <header className="border-b border-white/[0.06] px-10 py-7">
        <div className="mb-3 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          activity
        </div>
        <h1 className="m-0 max-w-[880px] font-serif text-[24px] font-light leading-[1.4] tracking-[-0.005em] text-fg-1">
          Session timeline.
        </h1>
        <div className="mt-4 flex flex-wrap gap-5 font-mono text-[11.5px] tracking-[0.06em] text-fg-2">
          <span>
            <span className="tabular-nums text-fg-0">{events.length}</span> events
          </span>
          <span>
            <span className="tabular-nums text-fg-0">{elapsed}</span> elapsed
          </span>
          <span>
            <span className="tabular-nums text-fg-0">{agents.length}</span> agents
          </span>
          {counts.error > 0 ? (
            <span className="text-warn">
              <span className="tabular-nums">{counts.error}</span> errors
            </span>
          ) : null}
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-2 border-b border-white/[0.06] px-10 py-4">
        {CATEGORIES.map((c) => (
          <FilterChip
            key={c}
            label={LABELS[c]}
            count={counts[c]}
            active={filters.has(c)}
            onClick={() => toggle(c)}
          />
        ))}
        <span className="flex-1" />
        <button
          type="button"
          onClick={() => setFilters(new Set(CATEGORIES))}
          className="rounded px-2 py-1 font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3 transition hover:bg-white/[0.04] hover:text-fg-1"
        >
          show all
        </button>
        <button
          type="button"
          onClick={() => setFilters(new Set())}
          className="rounded px-2 py-1 font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3 transition hover:bg-white/[0.04] hover:text-fg-1"
        >
          hide all
        </button>
      </div>

      <main className="min-h-0 flex-1 overflow-y-auto px-10 pb-20 pt-6">
        <div className="mx-auto max-w-[820px]">
          {visible.length === 0 ? (
            <div className="py-14 text-center font-mono text-[12px] tracking-[0.06em] text-fg-3">
              {events.length === 0
                ? 'no events yet — agents will report findings, progress, and system actions here.'
                : 'no events match the active filters.'}
            </div>
          ) : (
            <ol className="m-0 flex list-none flex-col gap-1 p-0">
              {visible.map((ev) => (
                <EventRow
                  key={ev.id}
                  event={ev}
                  expanded={expanded === ev.id}
                  onToggle={() => setExpanded((cur) => (cur === ev.id ? null : ev.id))}
                  onCopy={
                    isBusFinding(ev.raw)
                      ? () => onCopyFinding(ev.raw as BusEventView)
                      : undefined
                  }
                />
              ))}
            </ol>
          )}
        </div>
      </main>
    </div>
  )
}

// ============ chips ============

const CATEGORIES: Category[] = ['finding', 'progress', 'error', 'summary', 'media', 'truncation', 'read']

const LABELS: Record<Category, string> = {
  finding: 'findings',
  progress: 'progress',
  error: 'errors',
  summary: 'summaries',
  media: 'media',
  truncation: 'truncations',
  read: 'reads',
}

function FilterChip({
  label,
  count,
  active,
  onClick,
}: {
  label: string
  count: number
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 font-mono text-[10.5px] uppercase tracking-[0.16em] transition ${
        active
          ? 'border-accent/[0.3] bg-accent/[0.07] text-accent'
          : 'border-white/[0.06] bg-white/[0.018] text-fg-3 hover:border-white/[0.12] hover:text-fg-1'
      }`}
    >
      <span>{label}</span>
      <span className={`tabular-nums normal-case ${active ? 'text-accent' : 'text-fg-3'}`}>
        {count}
      </span>
    </button>
  )
}

// ============ row ============

function EventRow({
  event,
  expanded,
  onToggle,
  onCopy,
}: {
  event: UnifiedEvent
  expanded: boolean
  onToggle: () => void
  onCopy?: () => void
}) {
  const glyph = GLYPHS[event.category]
  const glyphClass = GLYPH_CLASSES[event.category]
  return (
    <li>
      <button
        type="button"
        onClick={onToggle}
        className={`group grid w-full grid-cols-[56px_88px_18px_1fr_auto] items-baseline gap-3 rounded-md border border-transparent px-3 py-2.5 text-left transition ${
          expanded ? 'border-white/[0.08] bg-white/[0.022]' : 'hover:bg-white/[0.025]'
        }`}
      >
        <span className="font-mono text-[11px] tabular-nums text-fg-3">{tsStr(event.at)}</span>
        <span className="truncate font-mono text-[11px] text-fg-2">{event.author}</span>
        <span className={`text-center text-[13px] ${glyphClass}`}>{glyph}</span>
        <span className="min-w-0 font-mono text-[13px] leading-[1.45] text-fg-0">
          <span className="mr-2 text-[10px] uppercase tracking-[0.14em] text-fg-3">
            {LABELS[event.category]}
          </span>
          {event.title}
        </span>
        <span className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3 opacity-0 transition group-hover:opacity-100">
          {expanded ? 'collapse' : 'expand'}
        </span>
      </button>
      {expanded && event.body ? (
        <div className="ml-[170px] mr-3 mb-2 mt-1 whitespace-pre-wrap rounded-md border border-white/[0.06] bg-white/[0.015] px-3 py-2.5 font-mono text-[12.5px] leading-[1.7] text-fg-1">
          {event.body}
          {onCopy ? (
            <div className="mt-2 flex justify-end">
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onCopy()
                }}
                className="rounded border border-white/[0.06] bg-white/[0.03] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-2 transition hover:bg-white/[0.06] hover:text-fg-0"
              >
                copy
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  )
}

const GLYPHS: Record<Category, string> = {
  finding: '✓',
  progress: '◐',
  error: '✗',
  summary: '⊟',
  media: '◇',
  truncation: '⊖',
  read: '·',
}

const GLYPH_CLASSES: Record<Category, string> = {
  finding: 'text-accent',
  progress: 'text-fg-1',
  error: 'text-warn',
  summary: 'text-fg-2',
  media: 'text-fg-2',
  truncation: 'text-fg-2',
  read: 'text-fg-3',
}

// ============ data unification ============

function unify(
  findings: BusEventView[],
  reads: BusEventView[],
  telemetry: TelemetryEventView[],
): UnifiedEvent[] {
  const out: UnifiedEvent[] = []

  for (const f of findings) {
    out.push({
      id: `f:${f.sessionId}:${f.index}:${f.timestamp}`,
      at: f.timestamp,
      author: f.senderLabel || f.sessionId.slice(0, 8),
      category: mapBusTopic(f.topic),
      title: oneLine(f.content),
      body: f.content,
      raw: f,
    })
  }

  for (const r of reads) {
    out.push({
      id: `r:${r.sessionId}:${r.index}:${r.timestamp}`,
      at: r.timestamp,
      author: r.senderLabel || r.sessionId.slice(0, 8),
      category: 'read',
      title: 'read the bus',
      raw: r,
    })
  }

  for (const t of telemetry) {
    const cat = mapTelemetrySubtype(t.subtype)
    if (!cat) continue
    out.push({
      id: `t:${t.id}`,
      at: t.at,
      author: ((t.details?.actor ?? t.details?.agent) as string | undefined) || 'system',
      category: cat,
      title: t.message || t.subtype.replace(/_/g, ' '),
      body: summarizeDetails(t.details),
      raw: t,
    })
  }

  return out.sort((a, b) => b.at - a.at)
}

function mapBusTopic(topic: string): Category {
  if (topic === 'errors') return 'error'
  if (topic === 'progress') return 'progress'
  return 'finding'
}

function mapTelemetrySubtype(subtype: string): Category | null {
  if (subtype === 'compaction_complete' || subtype === 'context_pruning') return 'summary'
  if (subtype === 'media_pruning') return 'media'
  if (subtype === 'tool_truncation' || subtype === 'output_truncation') return 'truncation'
  return null
}

function summarizeDetails(d: Record<string, unknown> | undefined): string {
  if (!d) return ''
  const interesting = ['reason', 'tokens_before', 'tokens_after', 'context_tokens_before', 'context_tokens_after', 'omitted_images', 'kept_recent', 'hard_limit']
  const parts: string[] = []
  for (const key of interesting) {
    if (d[key] != null) parts.push(`${key}: ${String(d[key])}`)
  }
  return parts.join(' · ')
}

function oneLine(s: string): string {
  return s.replace(/\s+/g, ' ').slice(0, 240)
}

function isBusFinding(raw: BusEventView | TelemetryEventView): raw is BusEventView {
  return 'topic' in raw && 'senderLabel' in raw
}

function elapsedFrom(events: UnifiedEvent[]): string {
  if (events.length === 0) return '0m'
  const oldest = events.reduce((min, e) => Math.min(min, e.at), Date.now())
  const minutes = Math.max(1, Math.round((Date.now() - oldest) / 60000))
  return `${minutes}m`
}

function tsStr(ts: number): string {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
