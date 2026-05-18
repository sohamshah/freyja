import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
  /** `'spawn'` marks synthetic events emitted at sub-agent spawn time
   *  (the task was delivered as the runner's initial user message, not
   *  via an inbox push). Used to style spawn arcs distinctly so the
   *  operator can tell "this was a spawn-time request" vs a later
   *  talk() call. */
  kind?: 'spawn'
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
  // Scroll-to-event request — set when a chip elsewhere wants to jump
  // here. Cleared by the scroll effect after firing.
  const [scrollToEventId, setScrollToEventId] = useState<string | null>(null)
  const eventRowRefs = useRef<Map<string, HTMLElement>>(new Map())

  // Resizable left/right divider — drag handle between event timeline
  // and comm pane. Default 480px (wider than the original 360 so the
  // chat-log cards have room); range 320-720. Persisted to
  // localStorage so the operator's preference sticks across sessions.
  const [commWidth, setCommWidth] = useState<number>(() => {
    try {
      const stored = localStorage.getItem('freyja.activity.commWidth')
      const n = stored ? parseInt(stored, 10) : 480
      return Number.isFinite(n) ? Math.max(320, Math.min(720, n)) : 480
    } catch {
      return 480
    }
  })
  useEffect(() => {
    try { localStorage.setItem('freyja.activity.commWidth', String(commWidth)) } catch {}
  }, [commWidth])

  const visible = useMemo(
    () => events.filter((e) => filters.has(e.category)),
    [events, filters],
  )

  // Relationship index. Built once per event set so chip expansion +
  // scroll-to-jump can resolve cross-references without rescanning:
  //   readersByFindingIndex: which read events consumed a given finding
  //   findingsByReadId:      which findings a given read event pulled
  //   findingByIndex:        finding lookup by bus index
  //   eventIdByFindingIndex: ActivityView event id for a finding (so
  //                          chips can jump to the right row)
  //   eventIdByReadId:       same for reads
  const relations = useMemo(() => {
    const readersByFindingIndex = new Map<number, Array<{
      readerLabel: string
      readerSessionId: string
      readEventTimestamp: number
      readEventId: string
    }>>()
    const findingsByReadId = new Map<string, Array<{
      index: number
      sender: string
      timestamp: number
      eventId: string
      preview: string
    }>>()
    const findingByIndex = new Map<number, BusEventView>()
    const eventIdByFindingIndex = new Map<number, string>()
    const eventIdByReadId = new Map<string, string>()

    for (const f of findings) {
      findingByIndex.set(f.index, f)
      const id = `f:${f.sessionId}:${f.index}:${f.timestamp}`
      eventIdByFindingIndex.set(f.index, id)
    }
    for (const r of readEvents) {
      const readEventId = `r:${r.sessionId}:${r.index}:${r.timestamp}`
      eventIdByReadId.set(readEventId, readEventId)
      const indices = (r as any).messageIndices as number[] | undefined
      if (!indices || indices.length === 0) continue
      const fLinks: Array<{
        index: number
        sender: string
        timestamp: number
        eventId: string
        preview: string
      }> = []
      for (const idx of indices) {
        const src = findingByIndex.get(idx)
        const arr = readersByFindingIndex.get(idx) ?? []
        arr.push({
          readerLabel: r.senderLabel || r.sessionId.slice(0, 8),
          readerSessionId: r.sessionId,
          readEventTimestamp: r.timestamp,
          readEventId,
        })
        readersByFindingIndex.set(idx, arr)
        if (src) {
          fLinks.push({
            index: idx,
            sender: src.senderLabel || src.sessionId.slice(0, 8),
            timestamp: src.timestamp,
            eventId: eventIdByFindingIndex.get(idx) ?? '',
            preview: oneLine(src.content),
          })
        }
      }
      findingsByReadId.set(readEventId, fLinks)
    }
    return {
      readersByFindingIndex,
      findingsByReadId,
      findingByIndex,
      eventIdByFindingIndex,
      eventIdByReadId,
    }
  }, [findings, readEvents])

  // Scroll-to-event effect. Triggered when a relationship chip is
  // clicked anywhere — left-pane chip in an expanded EventRow, or a
  // right-pane chat-log card. Scrolls the row into view, expands it,
  // and pulses the row briefly so the operator can find it.
  useEffect(() => {
    if (!scrollToEventId) return
    const node = eventRowRefs.current.get(scrollToEventId)
    if (node) {
      node.scrollIntoView({ behavior: 'smooth', block: 'center' })
      setExpanded(scrollToEventId)
    }
    // Clear after the scroll fires so the same id can be re-clicked later.
    const t = setTimeout(() => setScrollToEventId(null), 600)
    return () => clearTimeout(t)
  }, [scrollToEventId])

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

      <main className="flex min-h-0 flex-1 overflow-hidden pt-2">
        <div className="min-h-0 flex-1 overflow-y-auto px-10 pb-20 pt-4">
          <div className={`mx-auto ${hasComms ? 'max-w-[760px]' : 'max-w-[820px]'}`}>
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
                    isPulsing={scrollToEventId === ev.id}
                    onToggle={() => setExpanded((cur) => (cur === ev.id ? null : ev.id))}
                    onJumpTo={(targetId) => setScrollToEventId(targetId)}
                    readers={
                      ev.category === 'finding' && isBusFinding(ev.raw)
                        ? relations.readersByFindingIndex.get((ev.raw as BusEventView).index)
                        : undefined
                    }
                    readFindings={
                      ev.category === 'read'
                        ? relations.findingsByReadId.get(ev.id)
                        : undefined
                    }
                    rowRef={(el) => {
                      if (el) eventRowRefs.current.set(ev.id, el)
                      else eventRowRefs.current.delete(ev.id)
                    }}
                  />
                ))}
              </ol>
            )}
          </div>
        </div>
        {hasComms && (
          <>
            <ResizeHandle
              width={commWidth}
              onResize={(next) => setCommWidth(Math.max(320, Math.min(720, next)))}
            />
            <aside
              className="min-h-0 overflow-y-auto border-l border-white/[0.06] bg-black/[0.10]"
              style={{ width: commWidth, flexShrink: 0 }}
            >
              <CommPane
                inboxEvents={inboxEvents}
                sessions={sessions}
                agents={agents}
                onCopyFinding={onCopyFinding}
              />
            </aside>
          </>
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

interface ReaderInfo {
  readerLabel: string
  readerSessionId: string
  readEventTimestamp: number
  readEventId: string
}
interface ReadFindingInfo {
  index: number
  sender: string
  timestamp: number
  eventId: string
  preview: string
}

function EventRow({
  event,
  expanded,
  isPulsing,
  onToggle,
  onJumpTo,
  readers,
  readFindings,
  rowRef,
}: {
  event: UnifiedEvent
  expanded: boolean
  isPulsing: boolean
  onToggle: () => void
  onJumpTo: (eventId: string) => void
  readers?: ReaderInfo[]
  readFindings?: ReadFindingInfo[]
  rowRef: (el: HTMLElement | null) => void
}) {
  const glyph = GLYPHS[event.category]
  const glyphClass = GLYPH_CLASSES[event.category]
  const hasRelations =
    (readers && readers.length > 0) ||
    (readFindings && readFindings.length > 0)
  const hasBodyDifferentFromTitle =
    event.body !== undefined && event.body.trim() !== event.title.trim()
  const isCompactionDetail =
    event.category === 'summary' &&
    !isBusFinding(event.raw) &&
    isCompactionTelemetry(event.raw)
  const hasExpansion =
    hasRelations || hasBodyDifferentFromTitle || isCompactionDetail

  return (
    <li ref={rowRef as any}>
      <div
        className={`overflow-hidden rounded-md border transition ${
          expanded
            ? 'border-white/[0.10] bg-white/[0.028]'
            : isPulsing
            ? 'border-accent/[0.30] bg-accent/[0.06]'
            : 'border-transparent hover:bg-white/[0.025]'
        }`}
      >
        <button
          type="button"
          onClick={hasExpansion ? onToggle : undefined}
          className={`group grid w-full grid-cols-[56px_92px_18px_1fr_auto] items-baseline gap-3 px-3 py-2.5 text-left ${
            hasExpansion ? 'cursor-pointer' : 'cursor-default'
          }`}
        >
          <span className="font-mono text-[11px] tabular-nums text-fg-3">{tsStr(event.at)}</span>
          <span className="truncate font-mono text-[11px] text-fg-2">{event.author}</span>
          <span className={`text-center text-[13px] ${glyphClass}`}>{glyph}</span>
          <span className="min-w-0 font-mono text-[13px] leading-[1.5] text-fg-0">
            <span className="mr-2 text-[10px] uppercase tracking-[0.14em] text-fg-3">
              {LABELS[event.category]}
            </span>
            {event.title}
            {!expanded && hasRelations ? (
              <span className="ml-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-4">
                {readers && readers.length > 0
                  ? `· read by ${readers.length}`
                  : `· ${readFindings?.length ?? 0} read`}
              </span>
            ) : null}
          </span>
          <span
            className={`font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3 transition ${
              hasExpansion
                ? expanded
                  ? 'opacity-100'
                  : 'opacity-0 group-hover:opacity-100'
                : 'opacity-0'
            }`}
          >
            {expanded ? '▾' : '▸'}
          </span>
        </button>
        {expanded && hasExpansion ? (
          <ExpandedBody
            event={event}
            isCompaction={isCompactionDetail}
            hasBodyDifferentFromTitle={hasBodyDifferentFromTitle}
            readers={readers}
            readFindings={readFindings}
            onJumpTo={onJumpTo}
          />
        ) : null}
      </div>
    </li>
  )
}

/** Body shown inline inside the same row container when expanded.
 *  No inner card / border / background — content flows directly
 *  inside the row box so the operator perceives one container, not
 *  two. Replaces the prior floating panel that visually duplicated
 *  the row above. */
function ExpandedBody({
  event,
  isCompaction,
  hasBodyDifferentFromTitle,
  readers,
  readFindings,
  onJumpTo,
}: {
  event: UnifiedEvent
  isCompaction: boolean
  hasBodyDifferentFromTitle: boolean
  readers?: ReaderInfo[]
  readFindings?: ReadFindingInfo[]
  onJumpTo: (eventId: string) => void
}) {
  if (isCompaction) {
    return (
      <div className="pl-[170px] pr-3 pb-3">
        <CompactionDetailPanel telemetry={event.raw as TelemetryEventView} />
      </div>
    )
  }

  const fullBody = event.body ?? ''

  return (
    <div className="flex flex-col gap-2 pl-[170px] pr-3 pb-3">
      {hasBodyDifferentFromTitle && (
        <div className="select-text whitespace-pre-wrap font-mono text-[12.5px] leading-[1.7] text-fg-1">
          {fullBody}
        </div>
      )}

      {readers && readers.length > 0 && (
        <RelationStrip
          label={`read by ${readers.length}`}
          icon="↻"
        >
          {readers.map((r, i) => (
            <button
              key={`${r.readEventId}-${i}`}
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                onJumpTo(r.readEventId)
              }}
              className="inline-flex items-center gap-1.5 rounded border border-white/[0.10] bg-white/[0.03] px-2 py-0.5 font-mono text-[11px] text-fg-1 transition hover:border-accent/[0.32] hover:bg-accent/[0.06] hover:text-accent"
              title={`Jump to ${r.readerLabel}'s read at ${tsStr(r.readEventTimestamp)}`}
            >
              <span className="text-fg-3 tabular-nums">
                {tsStr(r.readEventTimestamp)}
              </span>
              <span className="truncate max-w-[200px]">{r.readerLabel}</span>
            </button>
          ))}
        </RelationStrip>
      )}

      {readFindings && readFindings.length > 0 && (
        <RelationStrip
          label={`pulled ${readFindings.length} finding${
            readFindings.length === 1 ? '' : 's'
          }`}
          icon="↦"
        >
          {readFindings.map((f) => (
            <button
              key={`f-${f.index}`}
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                onJumpTo(f.eventId)
              }}
              className="inline-flex max-w-[420px] items-baseline gap-2 rounded border border-white/[0.10] bg-white/[0.03] px-2 py-0.5 font-mono text-[11px] text-fg-1 transition hover:border-accent/[0.32] hover:bg-accent/[0.06] hover:text-accent"
              title={`Jump to F${f.index} from ${f.sender} at ${tsStr(
                f.timestamp,
              )}`}
            >
              <span className="font-mono text-[10px] tabular-nums text-accent">
                F{f.index}
              </span>
              <span className="text-fg-3 tabular-nums">{tsStr(f.timestamp)}</span>
              <span className="truncate text-fg-2">{f.sender}</span>
              <span className="truncate text-fg-3 italic">
                {f.preview.slice(0, 60)}
                {f.preview.length > 60 ? '…' : ''}
              </span>
            </button>
          ))}
        </RelationStrip>
      )}
    </div>
  )
}

function RelationStrip({
  label,
  icon,
  children,
}: {
  label: string
  icon: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-2 px-1">
      <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fg-4">
        <span className="mr-1.5">{icon}</span>
        {label}
      </span>
      {children}
    </div>
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
    <div className="font-mono text-[12px] text-fg-1">
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

/** Compact human-readable duration label used by the comm graph's
 *  gap markers. Picks the largest unit so a "6h 14m pause" reads as
 *  "6h", a 90s pause reads as "1m", etc. — the gap line itself is
 *  decoration; the label just needs to convey order of magnitude. */
function formatGapDuration(ms: number): string {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.round(m / 60)
  if (h < 24) return `${h}h`
  const d = Math.round(h / 24)
  return `${d}d`
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
/** Color tokens for the comm visualization. Operator and agent
 *  occupy distinctly different hues (cool steel-blue vs warm amber)
 *  so they're separable at a glance — not just by lightness. Force
 *  gets a high-contrast hot-orange ring that reads on top of either
 *  base color. */
const COMM_COLORS = {
  operator: {
    chip: 'rgb(120, 180, 255)',           // bright steel blue, full sat
    chipDim: 'rgba(120, 180, 255, 0.55)',
    arc: 'rgba(120, 180, 255, 0.55)',
    text: 'rgb(168, 212, 252)',           // matches Tailwind accent
  },
  agent: {
    chip: 'rgb(232, 196, 132)',           // warm amber/tan, full sat
    chipDim: 'rgba(232, 196, 132, 0.45)',
    arc: 'rgba(232, 196, 132, 0.40)',
    text: 'rgb(232, 196, 132)',
  },
  force: {
    ring: 'rgb(245, 130, 80)',            // hot orange — pops on either base
  },
}

/** Wraps the SVG comm graph + the conversation log into one scrollable
 *  pane. ActivityView renders this in the resizable right column when
 *  there's any inbox traffic. */
function CommPane({
  inboxEvents,
  sessions,
  agents,
  onCopyFinding,
}: {
  inboxEvents: InboxEventRecord[]
  sessions: SessionSnapshot[]
  agents: AgentView[]
  onCopyFinding: (event: BusEventView) => void
}) {
  void onCopyFinding
  const enqueued = useMemo(
    () =>
      inboxEvents
        .filter((e) => e.action === 'enqueued')
        .sort((a, b) => a.timestamp - b.timestamp),
    [inboxEvents],
  )
  // Anything outside this set is rendered as a "cross-session" lane
  // (dashed lane line, ↗ prefix on the label, hollow chip) so the
  // operator can tell at a glance whether a message came from inside
  // the mission's own tree or from a disconnected session. We seed
  // with 'operator' (the human) + every agent's session id + each
  // agent's parentSessionId (catches the mission root, which isn't
  // in `agents` itself).
  const inTreeIds = useMemo(() => {
    const s = new Set<string>(['operator'])
    for (const a of agents) {
      if (a.session?.id) s.add(a.session.id)
      if (a.session?.parentSessionId) s.add(a.session.parentSessionId)
    }
    return s
  }, [agents])
  // The "operator" pseudo-id is the human typing into a session pane
  // (via the operator_talk IPC). The parent / root agent is just
  // another session — operator is NOT the parent agent. Rendered as
  // "you" so the distinction is obvious to the operator reading the
  // graph.
  const labelFor = useCallback(
    (id: string): string => {
      if (id === 'operator') return 'you'
      const sess = sessions.find((s) => s.id === id)
      if (sess) return sess.title
      return id.slice(0, 8)
    },
    [sessions],
  )
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

  // Selected message — clicking a chip in the graph or a card in the
  // log highlights the same message in both panes. Powers cross-pane
  // navigation without a fancy state machine.
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const logRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!selectedId) return
    const node = logRef.current?.querySelector(
      `[data-msg-id="${selectedId}"]`,
    ) as HTMLElement | null
    if (node) node.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [selectedId])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="border-b border-white/[0.06] px-5 pb-3 pt-4">
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-3">
            agent talk
          </span>
          <span className="font-mono text-[11.5px] tabular-nums text-fg-2">
            <span className="text-fg-0">{counts.total}</span> msgs
          </span>
          {counts.external > 0 && (
            <span
              className="font-mono text-[10.5px] tabular-nums"
              style={{ color: COMM_COLORS.operator.text }}
            >
              · {counts.external} from you
            </span>
          )}
          {counts.force > 0 && (
            <span
              className="font-mono text-[10.5px] tabular-nums"
              style={{ color: COMM_COLORS.force.ring }}
            >
              · {counts.force} force
            </span>
          )}
        </div>
        <Legend />
      </header>

      <div className="border-b border-white/[0.06] bg-black/[0.10] px-3 py-3">
        <CommGraph
          enqueued={enqueued}
          labelFor={labelFor}
          inTreeIds={inTreeIds}
          selectedId={selectedId}
          onSelect={(id) => setSelectedId((cur) => (cur === id ? null : id))}
        />
      </div>

      <div
        ref={logRef}
        className="min-h-0 flex-1 overflow-y-auto px-3 py-3"
      >
        {enqueued.length === 0 ? (
          <div className="py-10 text-center font-mono text-[11.5px] italic text-fg-3">
            no talk activity yet
          </div>
        ) : (
          <ConversationLog
            messages={enqueued}
            labelFor={labelFor}
            inTreeIds={inTreeIds}
            selectedId={selectedId}
            onSelect={(id) => setSelectedId((cur) => (cur === id ? null : id))}
          />
        )}
      </div>
    </div>
  )
}

/** SVG swim-row diagram of inbox traffic. Pulled out of CommPane so
 *  the layout logic stays focused. `inTreeIds` flags participants
 *  that belong to the current mission's session tree; anyone else is
 *  marked as a cross-session lane (dashed lane line, ↗ prefix,
 *  warm-tinted chip ring). */
function CommGraph({
  enqueued,
  labelFor,
  inTreeIds,
  selectedId,
  onSelect,
}: {
  enqueued: InboxEventRecord[]
  labelFor: (id: string) => string
  inTreeIds: Set<string>
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const participants = useMemo(() => {
    const set = new Set<string>()
    for (const e of enqueued) {
      set.add(e.fromSession)
      set.add(e.sessionId)
    }
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

  // Order events by timestamp once; everything downstream keys off the
  // ordinal position, not the actual wall-clock time. Wall-clock-scaled
  // X coordinates were causing the operator's pain: two messages 50ms
  // apart looked stacked on top of each other while a third message 4h
  // later sat alone at the far right with two-thirds of the canvas
  // empty in between. Even spacing keeps every message readable
  // regardless of how the conversation paced itself in real time.
  const ordered = useMemo(
    () => [...enqueued].sort((a, b) => a.timestamp - b.timestamp),
    [enqueued],
  )
  const ordinalById = useMemo(() => {
    const m = new Map<string, number>()
    ordered.forEach((e, i) => m.set(e.id, i))
    return m
  }, [ordered])

  const LANE_HEIGHT = 28
  const LABEL_WIDTH = 132
  const TOP_PADDING = 10
  const SIDE_PADDING = 8
  const containerRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(420)
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(Math.max(280, entry.contentRect.width))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const innerWidth = Math.max(60, width - LABEL_WIDTH - SIDE_PADDING * 2)
  // Even spacing between ordinals. One event → centered; many events →
  // distributed across the inner width with half-step end caps so
  // chips never hug the canvas edges.
  const ordinalCount = Math.max(1, ordered.length)
  const ordinalStep = ordinalCount > 1 ? innerWidth / (ordinalCount - 1) : 0
  const xForOrdinal = (n: number): number => {
    if (ordered.length <= 1) return SIDE_PADDING + innerWidth / 2
    return SIDE_PADDING + n * ordinalStep
  }
  const xForId = (id: string): number => {
    const ord = ordinalById.get(id)
    return ord === undefined ? SIDE_PADDING : xForOrdinal(ord)
  }

  // Gap markers — when consecutive messages are far apart in wall-clock
  // time, drop a vertical dashed line BETWEEN their ordinals with a
  // relative-duration label so the operator still gets "there was a
  // pause" signal without us reverting to time-scaled spacing. The
  // threshold scales with total session duration: short conversations
  // surface 30s-plus pauses, multi-day ones only flag hour-plus gaps.
  const gapMarkers = useMemo(() => {
    if (ordered.length < 2) return []
    const totalSpan = ordered[ordered.length - 1].timestamp - ordered[0].timestamp
    const threshold = Math.max(30_000, totalSpan * 0.05)
    const out: Array<{ key: string; xMid: number; label: string }> = []
    for (let i = 1; i < ordered.length; i++) {
      const dt = ordered[i].timestamp - ordered[i - 1].timestamp
      if (dt < threshold) continue
      const xMid = (xForOrdinal(i - 1) + xForOrdinal(i)) / 2
      out.push({
        key: `gap-${ordered[i].id}`,
        xMid,
        label: formatGapDuration(dt),
      })
    }
    return out
    // ordinalStep is a derived value — including it pulls the recompute
    // back onto layout changes from the resize observer.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ordered, ordinalStep])

  const laneY = (i: number) => TOP_PADDING + i * LANE_HEIGHT + LANE_HEIGHT / 2
  const totalHeight = Math.max(
    80,
    TOP_PADDING + participants.length * LANE_HEIGHT + 12,
  )

  return (
    <div ref={containerRef}>
      {enqueued.length === 0 ? (
        <div className="py-6 text-center font-mono text-[11px] italic text-fg-3">
          waiting for talk activity
        </div>
      ) : (
        <svg
          width={width}
          height={totalHeight}
          className="block"
          aria-label="agent talk graph"
        >
          {/* Lanes — labels left, faint dashed rule across. Cross-
             * session participants (anything outside this mission's
             * tree) get a ↗ prefix, italic muted label, and a warmer
             * dashed lane line so external traffic reads as visibly
             * "outside the swarm." */}
          {participants.map((pid, i) => {
            const y = laneY(i)
            const isOperator = pid === 'operator'
            const isCross = !isOperator && !inTreeIds.has(pid)
            return (
              <g key={`lane-${pid}`}>
                <foreignObject
                  x={0}
                  y={y - 11}
                  width={LABEL_WIDTH - 4}
                  height={22}
                >
                  <div
                    className={`flex h-full items-center gap-1.5 truncate font-mono text-[10.5px] ${
                      isCross ? 'italic' : ''
                    }`}
                    style={{
                      color: isOperator
                        ? COMM_COLORS.operator.text
                        : isCross
                        ? 'rgba(245, 165, 110, 0.85)'
                        : 'rgba(232, 196, 132, 0.95)',
                    }}
                    title={
                      isCross
                        ? `${labelFor(pid)} — cross-session (outside this mission's tree)`
                        : labelFor(pid)
                    }
                  >
                    {isCross ? (
                      <span aria-hidden className="shrink-0 text-[10px] leading-none">
                        ↗
                      </span>
                    ) : (
                      <span
                        className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
                        style={{
                          background: isOperator
                            ? COMM_COLORS.operator.chip
                            : COMM_COLORS.agent.chip,
                        }}
                      />
                    )}
                    <span className="truncate">{labelFor(pid)}</span>
                  </div>
                </foreignObject>
                <line
                  x1={LABEL_WIDTH}
                  y1={y}
                  x2={LABEL_WIDTH + innerWidth + SIDE_PADDING}
                  y2={y}
                  stroke={
                    isCross
                      ? 'rgba(245, 165, 110, 0.18)'
                      : 'rgba(255,255,255,0.05)'
                  }
                  strokeWidth={1}
                  strokeDasharray={isCross ? '1,5' : '2,3'}
                />
              </g>
            )
          })}

          {/* Gap markers — vertical dashed dividers between ordinals
            * where wall-clock time jumped significantly. Lets the
            * operator see "there was a 12m pause here" without us
            * reverting to the time-scaled spacing that made the chart
            * unreadable when activity was bursty. */}
          {gapMarkers.length > 0 && (
            <g>
              {gapMarkers.map((g) => {
                const x = LABEL_WIDTH + g.xMid
                return (
                  <g key={g.key}>
                    <line
                      x1={x}
                      y1={2}
                      x2={x}
                      y2={totalHeight - 14}
                      stroke="rgba(255,255,255,0.10)"
                      strokeWidth={1}
                      strokeDasharray="1,4"
                    />
                    <text
                      x={x}
                      y={totalHeight - 3}
                      textAnchor="middle"
                      fontSize={8}
                      fill="rgba(255,255,255,0.30)"
                      fontFamily="ui-monospace, monospace"
                      pointerEvents="none"
                    >
                      ↔ {g.label}
                    </text>
                  </g>
                )
              })}
            </g>
          )}

          {/* Sender → recipient curves + animated motes (drawn below
            * chips). The static path conveys topology; the moving dot
            * along it conveys direction + message type:
            *   regular  — filled mote, solid arc
            *   force    — filled mote + pulsing halo, faster animation
            *   spawn    — hollow ring mote on a dashed arc, marking
            *              the parent → child spawn-time task assignment
            *              (synthetic — see _emit_spawn_inbox_event). */}
          {enqueued.map((e) => {
            const senderIdx = participants.indexOf(e.fromSession)
            const recipIdx = participants.indexOf(e.sessionId)
            if (senderIdx < 0 || recipIdx < 0) return null
            const x = LABEL_WIDTH + xForId(e.id)
            const y1 = laneY(senderIdx)
            const y2 = laneY(recipIdx)
            const isOperator = e.fromRole === 'operator'
            const isSpawn = e.kind === 'spawn'
            // Cross-session arcs touch at least one participant
            // outside the mission tree. Recolor the arc + mote to the
            // warm cross-session tint and force a dashed stroke so the
            // operator can spot inbound/outbound external traffic at a
            // glance even when no chip is selected.
            const senderCross = e.fromSession !== 'operator' && !inTreeIds.has(e.fromSession)
            const recipCross = e.sessionId !== 'operator' && !inTreeIds.has(e.sessionId)
            const isCross = senderCross || recipCross
            const baseStroke = isCross
              ? 'rgba(245, 165, 110, 0.55)'
              : isOperator
              ? COMM_COLORS.operator.arc
              : COMM_COLORS.agent.arc
            const moteFill = isCross
              ? 'rgb(245, 165, 110)'
              : isOperator
              ? COMM_COLORS.operator.chip
              : COMM_COLORS.agent.chip
            const isSelected = selectedId === e.id
            const cp1y = y1 + (y2 - y1) * 0.25
            const cp2y = y1 + (y2 - y1) * 0.75
            const midX = x + (Math.abs(y2 - y1) > LANE_HEIGHT ? 6 : 0)
            const d = `M ${x} ${y1} C ${midX} ${cp1y}, ${midX} ${cp2y}, ${x} ${y2}`
            const pathId = `comm-arc-${e.id}`
            const dur = e.force ? '1.4s' : isSpawn ? '3.2s' : '2.6s'
            // Self-loop sentinel — sender == recipient produces a
            // degenerate path; skip the mote to avoid an invisible
            // stationary dot.
            const isLoop = senderIdx === recipIdx
            return (
              <g key={`arc-${e.id}`}>
                <path
                  id={pathId}
                  d={d}
                  fill="none"
                  stroke={baseStroke}
                  strokeWidth={isSelected ? 2 : e.force ? 1.6 : 1}
                  strokeDasharray={isSpawn ? '4 3' : isCross ? '2,2' : undefined}
                  opacity={isSelected ? 1 : isSpawn ? 0.6 : 0.85}
                  strokeLinecap="round"
                />
                {!isLoop && (
                  <circle
                    r={e.force ? 2.6 : isSpawn ? 2.8 : 2.1}
                    fill={isSpawn || isCross ? 'none' : moteFill}
                    stroke={isSpawn || isCross ? moteFill : 'none'}
                    strokeWidth={isSpawn ? 1.3 : isCross ? 1.2 : 0}
                    opacity={0.95}
                  >
                    <animateMotion
                      dur={dur}
                      repeatCount="indefinite"
                    >
                      <mpath xlinkHref={`#${pathId}`} />
                    </animateMotion>
                  </circle>
                )}
                {!isLoop && e.force && (
                  <circle
                    r={4}
                    fill="none"
                    stroke={COMM_COLORS.force.ring}
                    strokeWidth={1}
                    opacity={0.5}
                  >
                    <animateMotion
                      dur={dur}
                      repeatCount="indefinite"
                    >
                      <mpath xlinkHref={`#${pathId}`} />
                    </animateMotion>
                    <animate
                      attributeName="r"
                      values="3;5.5;3"
                      dur={dur}
                      repeatCount="indefinite"
                    />
                    <animate
                      attributeName="opacity"
                      values="0.25;0.7;0.25"
                      dur={dur}
                      repeatCount="indefinite"
                    />
                  </circle>
                )}
              </g>
            )
          })}

          {/* Recipient chips + sender ring */}
          {enqueued.map((e) => {
            const senderIdx = participants.indexOf(e.fromSession)
            const recipIdx = participants.indexOf(e.sessionId)
            if (recipIdx < 0) return null
            const cx = LABEL_WIDTH + xForId(e.id)
            const cy = laneY(recipIdx)
            const senderY = senderIdx >= 0 ? laneY(senderIdx) : cy
            const isOperator = e.fromRole === 'operator'
            const isSpawn = e.kind === 'spawn'
            const fill = isOperator
              ? COMM_COLORS.operator.chip
              : COMM_COLORS.agent.chip
            const isSelected = selectedId === e.id
            const r = isSelected ? 5 : e.force ? 4.5 : 3.5
            const preview =
              e.content.length > 80 ? e.content.slice(0, 77) + '…' : e.content
            return (
              <g
                key={`chip-${e.id}`}
                style={{ cursor: 'pointer' }}
                onClick={(ev) => {
                  ev.stopPropagation()
                  onSelect(e.id)
                }}
              >
                {senderIdx >= 0 && (
                  <circle
                    cx={cx}
                    cy={senderY}
                    r={2}
                    fill="none"
                    stroke={fill}
                    strokeWidth={1}
                    opacity={0.85}
                  />
                )}
                {/* force halo — drawn outside the chip */}
                {e.force && (
                  <circle
                    cx={cx}
                    cy={cy}
                    r={r + 3}
                    fill="none"
                    stroke={COMM_COLORS.force.ring}
                    strokeWidth={1.6}
                  />
                )}
                <circle
                  cx={cx}
                  cy={cy}
                  r={r}
                  fill={isSpawn ? 'none' : fill}
                  stroke={
                    isSelected
                      ? 'rgba(255,255,255,0.95)'
                      : isSpawn
                      ? fill
                      : 'rgba(0,0,0,0.35)'
                  }
                  strokeWidth={isSelected ? 1.8 : isSpawn ? 1.4 : 1}
                >
                  <title>
                    {`${labelFor(e.fromSession)} → ${labelFor(e.sessionId)}${
                      e.force ? ' (force)' : ''
                    }${isSpawn ? ' (spawn task)' : ''}\n${tsStr(e.timestamp)} · ${preview}`}
                  </title>
                </circle>
              </g>
            )
          })}
        </svg>
      )}
    </div>
  )
}

/** Legend strip — actual chip samples next to labels so the operator
 *  doesn't have to guess what "you color" or "force ring" actually
 *  look like. "you" tags messages typed by the human via the talk
 *  pane; "agent" tags any agent-to-agent message (including ones
 *  from the parent / root agent). */
function Legend() {
  return (
    <div
      className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3"
      title="‘you’ = human typing in the talk pane. ‘agent’ = any agent calling talk(), including the parent agent."
    >
      <span className="inline-flex items-center gap-1.5">
        <svg width={12} height={12} viewBox="0 0 12 12" aria-hidden>
          <circle cx={6} cy={6} r={4} fill={COMM_COLORS.operator.chip} />
        </svg>
        you
      </span>
      <span className="inline-flex items-center gap-1.5">
        <svg width={12} height={12} viewBox="0 0 12 12" aria-hidden>
          <circle cx={6} cy={6} r={4} fill={COMM_COLORS.agent.chip} />
        </svg>
        agent
      </span>
      <span className="inline-flex items-center gap-1.5">
        <svg width={14} height={14} viewBox="0 0 14 14" aria-hidden>
          <circle
            cx={7}
            cy={7}
            r={6}
            fill="none"
            stroke={COMM_COLORS.force.ring}
            strokeWidth={1.6}
          />
          <circle cx={7} cy={7} r={3} fill="rgba(232,196,132,0.85)" />
        </svg>
        force
      </span>
      <span
        className="inline-flex items-center gap-1.5"
        title="Synthetic event marking when a sub-agent was spawned with an initial task. The task itself is delivered as the first user message, not via the inbox."
      >
        <svg width={14} height={14} viewBox="0 0 14 14" aria-hidden>
          <line
            x1={2}
            y1={7}
            x2={12}
            y2={7}
            stroke={COMM_COLORS.agent.chip}
            strokeWidth={1.2}
            strokeDasharray="2 1.5"
            opacity={0.7}
          />
          <circle
            cx={7}
            cy={7}
            r={2.8}
            fill="none"
            stroke={COMM_COLORS.agent.chip}
            strokeWidth={1.2}
          />
        </svg>
        spawn task
      </span>
      <span
        className="inline-flex items-center gap-1.5"
        title="Message involving a session outside this mission's tree (grandchildren, sessions from a sibling mission, or orphan workers). Hollow chip + warm dashed arc."
      >
        <svg width={14} height={14} viewBox="0 0 14 14" aria-hidden>
          <line
            x1={2}
            y1={7}
            x2={12}
            y2={7}
            stroke="rgb(245, 165, 110)"
            strokeWidth={1.2}
            strokeDasharray="2,2"
            opacity={0.7}
          />
          <circle
            cx={7}
            cy={7}
            r={2.8}
            fill="none"
            stroke="rgb(245, 165, 110)"
            strokeWidth={1.2}
          />
        </svg>
        ↗ cross-session
      </span>
      <span className="inline-flex items-center gap-1.5 text-fg-4 normal-case tracking-normal">
        <span aria-hidden>→</span>
        moving dot = direction of message
      </span>
    </div>
  )
}

/** Full conversation log — each message a card with proper text wrap,
 *  sender → recipient chips, force indicator. Replaces the previous
 *  one-line truncated "Recent" rows. Cards are clickable to highlight
 *  in the graph above. */
function ConversationLog({
  messages,
  labelFor,
  inTreeIds,
  selectedId,
  onSelect,
}: {
  messages: InboxEventRecord[]
  labelFor: (id: string) => string
  inTreeIds: Set<string>
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  // Newest at the top of the scroller — operator looks at "what
  // just happened" more often than "what kicked off the session".
  const reversed = useMemo(() => [...messages].reverse(), [messages])
  return (
    <ul className="m-0 flex list-none flex-col gap-2 p-0">
      {reversed.map((e) => {
        const isOperator = e.fromRole === 'operator'
        const selected = selectedId === e.id
        const senderCross =
          e.fromSession !== 'operator' && !inTreeIds.has(e.fromSession)
        const recipCross =
          e.sessionId !== 'operator' && !inTreeIds.has(e.sessionId)
        const isCross = senderCross || recipCross
        return (
          <li
            key={e.id}
            data-msg-id={e.id}
            onClick={() => onSelect(e.id)}
            className={`cursor-pointer overflow-hidden rounded-md border transition ${
              selected
                ? 'border-white/[0.18] bg-white/[0.04]'
                : isCross
                ? 'border-white/[0.06] bg-white/[0.012] hover:border-white/[0.12] hover:bg-white/[0.025]'
                : 'border-white/[0.06] bg-white/[0.015] hover:border-white/[0.12] hover:bg-white/[0.03]'
            }`}
          >
            <div className="flex items-baseline gap-2 border-b border-white/[0.05] px-3 py-1.5 font-mono text-[10.5px]">
              <span className="tabular-nums text-fg-3">{tsStr(e.timestamp)}</span>
              <span
                className={`font-mono ${senderCross ? 'italic' : ''}`}
                style={{
                  color: senderCross
                    ? 'rgba(245, 165, 110, 0.95)'
                    : isOperator
                    ? COMM_COLORS.operator.text
                    : COMM_COLORS.agent.text,
                }}
              >
                {senderCross ? '↗ ' : ''}{labelFor(e.fromSession)}
              </span>
              <span className="text-fg-4">→</span>
              <span
                className={`truncate ${recipCross ? 'italic' : ''}`}
                style={recipCross ? { color: 'rgba(245, 165, 110, 0.85)' } : undefined}
              >
                {recipCross ? '↗ ' : ''}{labelFor(e.sessionId)}
              </span>
              {e.kind === 'spawn' && (
                <span
                  className="ml-auto rounded border px-1.5 py-[1px] text-[9px] uppercase tracking-[0.14em]"
                  style={{
                    color: COMM_COLORS.agent.text,
                    borderColor: 'rgba(232, 196, 132, 0.45)',
                    background: 'rgba(232, 196, 132, 0.06)',
                  }}
                  title="Spawn-time task assignment — delivered as the runner's initial user message rather than via an inbox push. Shown here for directional context."
                >
                  spawn task
                </span>
              )}
              {e.force && (
                <span
                  className={`${e.kind === 'spawn' ? '' : 'ml-auto'} rounded border px-1.5 py-[1px] text-[9px] uppercase tracking-[0.14em]`}
                  style={{
                    color: COMM_COLORS.force.ring,
                    borderColor: COMM_COLORS.force.ring,
                    background: 'rgba(245, 130, 80, 0.08)',
                  }}
                >
                  force
                </span>
              )}
              {e.replyTo && (
                <span className={`${e.kind === 'spawn' || e.force ? '' : 'ml-auto'} font-mono text-[10px] text-fg-4`}>
                  ↪ reply
                </span>
              )}
            </div>
            <p className="m-0 select-text whitespace-pre-wrap break-words px-3 py-2 font-mono text-[12px] leading-[1.6] text-fg-1">
              {e.content}
            </p>
          </li>
        )
      })}
    </ul>
  )
}

/** Drag handle between the event timeline (left) and comm pane
 *  (right). 6px-wide hit area, 1px visible rule. Captures pointer
 *  events on mousedown and resizes commWidth as the mouse moves. */
function ResizeHandle({
  width,
  onResize,
}: {
  width: number
  onResize: (next: number) => void
}) {
  const draggingRef = useRef(false)
  const startXRef = useRef(0)
  const startWidthRef = useRef(0)

  const onMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault()
    draggingRef.current = true
    startXRef.current = e.clientX
    startWidthRef.current = width
    const move = (mv: MouseEvent) => {
      if (!draggingRef.current) return
      // Mouse moves right → shrink commWidth (right pane gets narrower
      // because the divider is between left and right). Inverted.
      const delta = mv.clientX - startXRef.current
      onResize(startWidthRef.current - delta)
    }
    const up = () => {
      draggingRef.current = false
      document.removeEventListener('mousemove', move)
      document.removeEventListener('mouseup', up)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    document.addEventListener('mousemove', move)
    document.addEventListener('mouseup', up)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }

  return (
    <div
      onMouseDown={onMouseDown}
      className="group relative w-1.5 shrink-0 cursor-col-resize bg-transparent"
      role="separator"
      aria-orientation="vertical"
      title="Drag to resize"
    >
      <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-white/[0.06] transition group-hover:bg-accent/[0.40]" />
    </div>
  )
}
