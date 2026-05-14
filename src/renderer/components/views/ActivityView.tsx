import React, { useMemo, useState } from 'react'
import { formatTokens } from '../../lib/format'
import type { AgentView, BusEventView, TelemetryEventView } from '../shared/types'
import type { SessionSnapshot } from '../../../shared/events'

/** Shape of an inbox_event record stored in slice.inboxEvents.
 *  Aggregated across the swarm by MissionDashboard. */
export interface InboxEventRecord {
  id: string
  action: 'enqueued' | 'delivered' | 'dropped'
  fromSession: string
  fromLabel: string
  fromRole: 'operator' | 'agent'
  content: string
  force: boolean
  replyTo: string | null
  timestamp: number
  sessionId: string  // the recipient session
}

interface Props {
  findings: BusEventView[]
  readEvents: BusEventView[]
  telemetryEvents: TelemetryEventView[]
  agents: AgentView[]
  sessions: SessionSnapshot[]
  inboxEvents: InboxEventRecord[]
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
  sessions,
  inboxEvents,
  onCopyFinding,
}: Props) {
  // Comm-activity visual is shown only when there's actual inbox
  // traffic. Operator-initiated and agent-to-agent both count.
  const hasComms = useMemo(
    () => inboxEvents.some((e) => e.action === 'enqueued'),
    [inboxEvents],
  )
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

      <main
        className={`min-h-0 flex-1 overflow-hidden pb-20 pt-6 ${
          hasComms ? 'grid grid-cols-[minmax(0,1fr)_360px] gap-0' : ''
        }`}
      >
        <div className={`min-h-0 overflow-y-auto px-10 ${hasComms ? '' : ''}`}>
          <div className={`mx-auto ${hasComms ? 'max-w-[680px]' : 'max-w-[820px]'}`}>
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
        </div>
        {hasComms && (
          <aside className="min-h-0 overflow-y-auto border-l border-white/[0.06] bg-black/[0.10] px-5 py-2">
            <CommGraph
              inboxEvents={inboxEvents}
              sessions={sessions}
              agents={agents}
            />
          </aside>
        )}
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
      {expanded ? (
        <div className="ml-[170px] mr-3 mb-2 mt-1">
          {event.category === 'summary' &&
          !isBusFinding(event.raw) &&
          isCompactionTelemetry(event.raw) ? (
            <CompactionDetailPanel telemetry={event.raw} />
          ) : event.body ? (
            <div className="whitespace-pre-wrap rounded-md border border-white/[0.06] bg-white/[0.015] px-3 py-2.5 font-mono text-[12.5px] leading-[1.7] text-fg-1">
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
        </div>
      ) : null}
    </li>
  )
}

function isCompactionTelemetry(raw: BusEventView | TelemetryEventView): raw is TelemetryEventView {
  if ('topic' in raw) return false
  const sub = (raw as TelemetryEventView).subtype
  return (
    sub === 'compaction_complete' ||
    sub === 'compaction_start' ||
    sub === 'compaction_skipped' ||
    sub === 'context_pruning' ||
    sub === 'media_pruning'
  )
}

/**
 * Structured detail panel for compaction system events. Replaces the
 * old single-line key-value summarization with a layout that surfaces
 * the actual content: scope/mechanism/trigger chips, tokens
 * before→after with delta and percent saved, agent's free-text reason,
 * and the full summary text in a scrollable container the user can
 * copy.
 *
 * Falls back to the message itself for events that don't carry a
 * summary (e.g. context_pruning, media_pruning, compaction_skipped).
 */
function CompactionDetailPanel({
  telemetry,
}: {
  telemetry: TelemetryEventView
}) {
  const details = (telemetry.details ?? {}) as Record<string, unknown>
  const scope = (details.scope as string | undefined) || null
  const mechanism = (details.mechanism as string | undefined)
    || (details.strategy as string | undefined)
    || telemetry.subtype
  const trigger = (details.trigger as string | undefined) || null
  const reason = (details.reason as string | undefined) || null
  const resumed = Boolean(details.resumed_from_previous)
  const tokensBefore = Number(
    details.tokens_before ?? details.context_tokens_before ?? 0,
  )
  const tokensAfter = Number(
    details.tokens_after ?? details.context_tokens_after ?? 0,
  )
  const summaryText =
    (details.summary_text as string | undefined)
    ?? (details.summary_preview as string | undefined)
    ?? (details.summary_excerpt as string | undefined)
    ?? ''
  const entriesRemoved = Number(details.entries_removed ?? 0)

  const delta = tokensBefore - tokensAfter
  const percentSaved =
    tokensBefore > 0 ? Math.round((delta / tokensBefore) * 100) : 0

  return (
    <div className="rounded-md border border-white/[0.06] bg-white/[0.015] px-3 py-2.5 font-mono text-[12px] text-fg-1">
      {/* Metadata chips */}
      <div className="flex flex-wrap items-center gap-2 text-[10.5px]">
        {scope ? <Chip label="scope" value={scope} /> : null}
        <Chip label="mechanism" value={mechanism} />
        {trigger ? (
          <Chip
            label="trigger"
            value={trigger}
            tone={trigger === 'agent_summarize_context' ? 'accent' : 'neutral'}
          />
        ) : null}
        {resumed ? <Chip label="iterative" value="yes" tone="accent" /> : null}
        {entriesRemoved > 0 ? (
          <Chip label="entries" value={`${entriesRemoved}`} />
        ) : null}
      </div>

      {/* Tokens before → after */}
      {(tokensBefore > 0 || tokensAfter > 0) && (
        <div className="mt-2.5 flex flex-wrap items-baseline gap-3 text-[12px]">
          <span className="text-fg-3">tokens</span>
          <span className="text-fg-0">{formatTokens(tokensBefore)}</span>
          <span className="text-fg-3">→</span>
          <span className="text-fg-0">{formatTokens(tokensAfter)}</span>
          {delta > 0 ? (
            <span className="text-ok">
              −{formatTokens(delta)} ({percentSaved}%)
            </span>
          ) : null}
        </div>
      )}

      {/* Reason (agent rationale) */}
      {reason ? (
        <div className="mt-2.5 rounded bg-white/[0.025] px-2.5 py-1.5">
          <div className="text-[10px] uppercase tracking-[0.14em] text-fg-3">
            reason
          </div>
          <div className="mt-1 whitespace-pre-wrap text-[12px] leading-[1.55] text-fg-1">
            {reason}
          </div>
        </div>
      ) : null}

      {/* Full summary text — the actual content the agent will see in
          place of the truncated transcript. Scrollable so big
          summaries don't blow out the activity feed. */}
      {summaryText ? (
        <div className="mt-2.5">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-[0.14em] text-fg-3">
              summary
            </span>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                void navigator.clipboard?.writeText(summaryText)
              }}
              className="rounded border border-white/[0.06] bg-white/[0.03] px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-fg-2 transition hover:bg-white/[0.06] hover:text-fg-0"
            >
              copy
            </button>
          </div>
          <pre className="max-h-[420px] overflow-y-auto whitespace-pre-wrap rounded bg-black/30 px-3 py-2 text-[12px] leading-[1.55] text-fg-1 ring-1 ring-white/[0.04]">
            {summaryText}
          </pre>
        </div>
      ) : null}
    </div>
  )
}

function Chip({
  label,
  value,
  tone = 'neutral',
}: {
  label: string
  value: string
  tone?: 'neutral' | 'accent'
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-[1px] text-[10px] ${
        tone === 'accent'
          ? 'bg-accent/[0.15] text-accent ring-1 ring-accent/30'
          : 'bg-white/[0.04] text-fg-1 ring-1 ring-white/[0.06]'
      }`}
    >
      <span className="text-fg-3">{label}</span>
      <span>{value}</span>
    </span>
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

// ============ comm graph ============

/** Swim-row visualization of agent-to-agent + operator-to-agent talk.
 *  Each unique participant gets a horizontal lane; each enqueued
 *  inbox event is a chip on the recipient's lane at the event
 *  timestamp; a thin curve connects sender → recipient at that
 *  moment. Operator-originated messages tint the curve + chip with
 *  the accent color so cross-mission (external-feeling) traffic is
 *  visually distinct from internal swarm chatter.
 */
function CommGraph({
  inboxEvents,
  sessions,
  agents,
}: {
  inboxEvents: InboxEventRecord[]
  sessions: SessionSnapshot[]
  agents: AgentView[]
}) {
  // Filter to enqueued only — drops and delivered duplicates would
  // clutter the graph. Time-sort ascending.
  const enqueued = useMemo(
    () =>
      inboxEvents
        .filter((e) => e.action === 'enqueued')
        .sort((a, b) => a.timestamp - b.timestamp),
    [inboxEvents],
  )

  // Pre-build participant set (every unique sender + recipient) and a
  // lookup for human-readable labels. Operator gets a virtual id.
  const participants = useMemo(() => {
    const set = new Set<string>()
    for (const e of enqueued) {
      set.add(e.fromSession)
      set.add(e.sessionId)
    }
    // Order: operator first, then by first-mention timestamp
    const firstSeen = new Map<string, number>()
    for (const e of enqueued) {
      if (!firstSeen.has(e.fromSession)) firstSeen.set(e.fromSession, e.timestamp)
      if (!firstSeen.has(e.sessionId)) firstSeen.set(e.sessionId, e.timestamp)
    }
    const arr = Array.from(set)
    arr.sort((a, b) => {
      if (a === 'operator') return -1
      if (b === 'operator') return 1
      return (firstSeen.get(a) ?? 0) - (firstSeen.get(b) ?? 0)
    })
    return arr
  }, [enqueued])

  const labelFor = (id: string): string => {
    if (id === 'operator') return 'operator'
    const sess = sessions.find((s) => s.id === id)
    if (sess) return sess.title
    return id.slice(0, 8)
  }

  const counts = useMemo(() => {
    let internal = 0
    let external = 0
    let force = 0
    for (const e of enqueued) {
      if (e.fromRole === 'operator') external++
      else internal++
      if (e.force) force++
    }
    return { internal, external, force, total: enqueued.length }
  }, [enqueued])

  // Time domain — pad 3% each side so endpoints don't hug the edges.
  const { tMin, tMax } = useMemo(() => {
    if (enqueued.length === 0) {
      const now = Date.now()
      return { tMin: now - 1000, tMax: now }
    }
    const times = enqueued.map((e) => e.timestamp)
    const min = Math.min(...times)
    const max = Math.max(...times)
    const span = Math.max(1000, max - min)
    return { tMin: min - span * 0.03, tMax: max + span * 0.03 }
  }, [enqueued])

  // Layout
  const LANE_HEIGHT = 26
  const LABEL_WIDTH = 110
  const TOP_PADDING = 22
  const SIDE_PADDING = 8
  const containerRef = React.useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(320)
  React.useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(Math.max(280, entry.contentRect.width))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const innerWidth = Math.max(60, width - LABEL_WIDTH - SIDE_PADDING * 2)
  const tSpan = Math.max(1, tMax - tMin)
  const tToX = (t: number) => SIDE_PADDING + ((t - tMin) / tSpan) * innerWidth
  const laneY = (i: number) => TOP_PADDING + i * LANE_HEIGHT + LANE_HEIGHT / 2

  const totalHeight = Math.max(120, TOP_PADDING + participants.length * LANE_HEIGHT + 12)

  return (
    <div className="flex flex-col gap-3" ref={containerRef}>
      <header className="flex items-baseline gap-3 pt-3">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-3">
          agent talk
        </span>
        <span className="font-mono text-[11px] tabular-nums text-fg-2">
          <span className="text-fg-0">{counts.total}</span> msgs
        </span>
        {counts.external > 0 && (
          <span className="font-mono text-[10.5px] text-accent">
            · <span className="tabular-nums">{counts.external}</span> from operator
          </span>
        )}
        {counts.force > 0 && (
          <span className="font-mono text-[10.5px] text-warn">
            · <span className="tabular-nums">{counts.force}</span> force
          </span>
        )}
      </header>

      {enqueued.length === 0 ? (
        <div className="py-10 text-center font-mono text-[11.5px] italic text-fg-3">
          no talk activity yet
        </div>
      ) : (
        <svg
          width={width}
          height={totalHeight}
          className="block"
          aria-label="agent talk graph"
        >
          {/* Lane labels + horizontal rules */}
          {participants.map((pid, i) => {
            const y = laneY(i)
            const isOperator = pid === 'operator'
            return (
              <g key={`lane-${pid}`}>
                <foreignObject
                  x={0}
                  y={y - 11}
                  width={LABEL_WIDTH - 4}
                  height={22}
                >
                  <div
                    className={`flex h-full items-center truncate font-mono text-[10.5px] ${
                      isOperator ? 'text-accent' : 'text-fg-2'
                    }`}
                    title={labelFor(pid)}
                  >
                    <span className="mr-1.5 inline-block h-1 w-1 rounded-full"
                          style={{
                            background: isOperator ? 'rgb(168, 212, 252)' : 'rgba(255,255,255,0.45)',
                          }}
                    />
                    <span className="truncate">{labelFor(pid)}</span>
                  </div>
                </foreignObject>
                <line
                  x1={LABEL_WIDTH}
                  y1={y}
                  x2={LABEL_WIDTH + innerWidth + SIDE_PADDING}
                  y2={y}
                  stroke="rgba(255,255,255,0.06)"
                  strokeWidth={1}
                  strokeDasharray="2,3"
                />
              </g>
            )
          })}

          {/* Curves: sender → recipient at message timestamp. Drawn
              first so the chips sit on top. */}
          {enqueued.map((e) => {
            const senderIdx = participants.indexOf(e.fromSession)
            const recipIdx = participants.indexOf(e.sessionId)
            if (senderIdx < 0 || recipIdx < 0) return null
            const x = LABEL_WIDTH + tToX(e.timestamp)
            const y1 = laneY(senderIdx)
            const y2 = laneY(recipIdx)
            const isOperator = e.fromRole === 'operator'
            const color = isOperator
              ? 'rgba(168,212,252,0.55)'
              : 'rgba(255,255,255,0.35)'
            // Cubic bezier curve that bows slightly so concurrent
            // messages don't overlap as one flat line.
            const midX = x + (Math.abs(y2 - y1) > LANE_HEIGHT ? 6 : 0)
            const cp1y = y1 + (y2 - y1) * 0.25
            const cp2y = y1 + (y2 - y1) * 0.75
            const d = `M ${x} ${y1} C ${midX} ${cp1y}, ${midX} ${cp2y}, ${x} ${y2}`
            return (
              <path
                key={`arc-${e.id}`}
                d={d}
                fill="none"
                stroke={color}
                strokeWidth={e.force ? 1.4 : 0.8}
                opacity={0.85}
                strokeLinecap="round"
              />
            )
          })}

          {/* Recipient chips — small filled circles on the recipient
              lane, brighter than the sender end (which is just a
              tail of the curve). Operator messages tinted accent. */}
          {enqueued.map((e) => {
            const recipIdx = participants.indexOf(e.sessionId)
            const senderIdx = participants.indexOf(e.fromSession)
            if (recipIdx < 0) return null
            const cx = LABEL_WIDTH + tToX(e.timestamp)
            const cy = laneY(recipIdx)
            const isOperator = e.fromRole === 'operator'
            const r = e.force ? 4 : 3
            const senderY = senderIdx >= 0 ? laneY(senderIdx) : cy
            const preview =
              e.content.length > 80 ? e.content.slice(0, 77) + '…' : e.content
            return (
              <g key={`chip-${e.id}`}>
                {/* Sender end as a small hollow dot */}
                {senderIdx >= 0 && (
                  <circle
                    cx={cx}
                    cy={senderY}
                    r={2}
                    fill="none"
                    stroke={isOperator ? 'rgba(168,212,252,0.65)' : 'rgba(255,255,255,0.45)'}
                    strokeWidth={1}
                  />
                )}
                {/* Recipient chip — the head of the arrow */}
                <circle
                  cx={cx}
                  cy={cy}
                  r={r}
                  fill={isOperator ? 'rgba(168,212,252,0.85)' : 'rgba(255,255,255,0.65)'}
                  stroke={e.force ? 'rgb(245, 182, 64)' : 'rgba(0,0,0,0.35)'}
                  strokeWidth={e.force ? 1.4 : 0.8}
                >
                  <title>
                    {`${e.fromLabel} → ${labelFor(e.sessionId)}${
                      e.force ? ' (force)' : ''
                    }\n${tsStr(e.timestamp)} · ${preview}`}
                  </title>
                </circle>
              </g>
            )
          })}
        </svg>
      )}

      {/* Compact legend */}
      {enqueued.length > 0 && (
        <div className="flex flex-wrap gap-3 px-1 pb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-4">
          <span className="inline-flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-full bg-accent" />
            operator
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-full bg-white/50" />
            agent
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full border border-warn" />
            force
          </span>
        </div>
      )}

      {/* Recent rows — chronological mini-log right under the graph */}
      {enqueued.length > 0 && (
        <div className="border-t border-white/[0.06] pt-2">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.16em] text-fg-4">
            recent
          </div>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {enqueued.slice(-6).reverse().map((e) => (
              <li
                key={`recent-${e.id}`}
                className="grid grid-cols-[auto_1fr_auto] items-baseline gap-1.5 font-mono text-[10.5px] leading-[1.45]"
              >
                <span className="text-fg-4 tabular-nums">{tsStr(e.timestamp)}</span>
                <span className="truncate text-fg-1" title={e.content}>
                  <span
                    className={
                      e.fromRole === 'operator' ? 'text-accent' : 'text-fg-2'
                    }
                  >
                    {e.fromLabel}
                  </span>
                  <span className="text-fg-4"> → </span>
                  <span className="text-fg-2">{labelFor(e.sessionId)}</span>
                  {e.force && (
                    <span className="ml-1 rounded border border-warn/[0.30] bg-warn/[0.05] px-1 text-[8.5px] text-warn">
                      force
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
