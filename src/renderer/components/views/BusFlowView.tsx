import React, { useMemo, useRef, useState, useEffect } from 'react'
import type { AgentView, BusEventView } from '../shared/types'

/**
 * Bus-mode dashboard view.
 *
 * Two stacked panes:
 *   - Top (~38%): swim-row timeline. Each subagent gets a horizontal
 *     lane on the time axis. Findings appear as colored chips anchored
 *     at publish time on the publisher's lane. read_findings calls
 *     appear as outline chips on the reader's lane with thin arcs back
 *     to each source finding's chip (the bridge now carries
 *     messageIndices on read events for exact targeting).
 *   - Bottom (~62%): chronological bus tape — one card per published
 *     finding plus collapsed rows for read events. Click a chip in the
 *     timeline → tape scrolls to and highlights that card. Click an
 *     agent label → both panes filter to that agent's contributions.
 *
 * Designed to scale to ~15 agents and ~50 findings before density gets
 * uncomfortable; beyond that the operator can scroll the lane stack
 * vertically and the timeline keeps the time axis stable at the top.
 */

interface Props {
  objective: string
  agents: AgentView[]
  findings: BusEventView[]
  contextPct: number
  cost: number
  onAttach: (sessionId: string, mode?: 'replace' | 'split') => void
}

interface AgentLane {
  id: string
  label: string
  agentType: string
  status: AgentView['status']
  startedAt: number
  endedAt: number  // === now if still running
  ended: boolean   // distinguishes "still running" from "completed"
  agent: AgentView
}

interface ReadEvent {
  id: string         // synthetic — sender + timestamp + sortIdx
  senderId: string
  timestamp: number
  sourceIndices: number[]
}

const TOPIC_COLORS: Record<string, { fill: string; ring: string; text: string }> = {
  findings: { fill: 'fill-accent/[0.55]', ring: 'ring-accent/[0.40]', text: 'text-accent' },
  errors:   { fill: 'fill-warn/[0.65]',   ring: 'ring-warn/[0.45]',   text: 'text-warn' },
  progress: { fill: 'fill-fg-3/[0.55]',   ring: 'ring-white/[0.18]',  text: 'text-fg-2' },
}

export function BusFlowView({
  objective,
  agents,
  findings,
  contextPct,
  cost,
  onAttach,
}: Props) {
  const [selectedFindingIndex, setSelectedFindingIndex] = useState<number | null>(null)
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const tapeScrollerRef = useRef<HTMLDivElement>(null)

  // Build the agent lane stack. Sort by spawn time so the visual flow
  // reads top-to-bottom as a fan-out cascade. Parent session (no
  // parentId) lives implicitly above; we don't show it as a lane since
  // the parent doesn't publish findings — only synthesizes them.
  const lanes: AgentLane[] = useMemo(() => {
    const now = Date.now()
    const out: AgentLane[] = agents.map((a) => {
      const startedAt =
        a.sub?.startedAt ??
        a.session.createdAt ??
        now
      const elapsedMs = a.sub?.elapsedMs ?? 0
      const ended = a.status === 'done' || a.status === 'failed' || a.status === 'cancelled'
      const endedAt = ended ? startedAt + elapsedMs : now
      return {
        id: a.session.id,
        label: a.session.title || a.session.id,
        agentType: a.agentType || 'general',
        status: a.status,
        startedAt,
        endedAt,
        ended,
        agent: a,
      }
    })
    out.sort((a, b) => a.startedAt - b.startedAt)
    return out
  }, [agents])

  // Lookup: senderId → lane index. Findings + reads off-lane (no
  // matching agent — could be parent or a now-archived agent) get
  // anchored to a synthetic "off-stage" track at index -1.
  const laneIndexById = useMemo(() => {
    const m = new Map<string, number>()
    lanes.forEach((l, i) => m.set(l.id, i))
    return m
  }, [lanes])

  // Split bus messages into published findings and read events.
  const publishedFindings = useMemo(
    () => findings.filter((f) => f.topic !== 'read').sort((a, b) => a.timestamp - b.timestamp),
    [findings],
  )

  const readEvents: ReadEvent[] = useMemo(() => {
    const reads: ReadEvent[] = []
    findings.forEach((f, i) => {
      if (f.topic !== 'read') return
      const indices = (f as any).messageIndices as number[] | undefined
      if (!indices || indices.length === 0) return
      reads.push({
        id: `read-${f.senderId}-${f.timestamp}-${i}`,
        senderId: f.senderId,
        timestamp: f.timestamp,
        sourceIndices: indices,
      })
    })
    return reads
  }, [findings])


  // Findings indexed by their bus index for fast arc-target lookup.
  const findingByIndex = useMemo(() => {
    const m = new Map<number, BusEventView>()
    for (const f of publishedFindings) m.set(f.index, f)
    return m
  }, [publishedFindings])

  // Auto-scroll tape when a finding is selected from the timeline.
  useEffect(() => {
    if (selectedFindingIndex === null) return
    const node = tapeScrollerRef.current?.querySelector(
      `[data-finding-index="${selectedFindingIndex}"]`,
    )
    if (node) (node as HTMLElement).scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [selectedFindingIndex])

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <HeaderStrip
        objective={objective}
        agents={agents}
        findings={publishedFindings}
        reads={readEvents}
        contextPct={contextPct}
        cost={cost}
      />

      <div className="grid min-h-0 flex-1 grid-rows-[minmax(220px,38%)_minmax(0,1fr)] overflow-hidden">
        <Timeline
          lanes={lanes}
          laneIndexById={laneIndexById}
          publishedFindings={publishedFindings}
          readEvents={readEvents}
          findingByIndex={findingByIndex}
          selectedFindingIndex={selectedFindingIndex}
          selectedAgentId={selectedAgentId}
          onSelectFinding={setSelectedFindingIndex}
          onSelectAgent={(id) => {
            setSelectedAgentId((current) => (current === id ? null : id))
            setSelectedFindingIndex(null)
          }}
          onOpenAgent={onAttach}
        />

        <BusTape
          scrollerRef={tapeScrollerRef}
          publishedFindings={publishedFindings}
          readEvents={readEvents}
          findingByIndex={findingByIndex}
          lanes={lanes}
          selectedFindingIndex={selectedFindingIndex}
          selectedAgentId={selectedAgentId}
          onSelectFinding={setSelectedFindingIndex}
          onSelectAgent={(id) => {
            setSelectedAgentId((current) => (current === id ? null : id))
            setSelectedFindingIndex(null)
          }}
        />
      </div>
    </div>
  )
}

// ===================== HEADER =====================

function HeaderStrip({
  objective,
  agents,
  findings,
  reads,
  contextPct,
  cost,
}: {
  objective: string
  agents: AgentView[]
  findings: BusEventView[]
  reads: ReadEvent[]
  contextPct: number
  cost: number
}) {
  const running = agents.filter((a) => a.status === 'running').length
  return (
    <header className="border-b border-white/[0.06]">
      <div className="flex items-start gap-5 px-10 pb-3.5 pt-5">
        <span className="mt-[7px] shrink-0 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-4">
          mission
        </span>
        <h1 className="m-0 line-clamp-2 flex-1 select-text font-serif text-[17px] font-light leading-[1.4] tracking-[-0.005em] text-fg-0">
          {objective || <span className="italic text-fg-3">no objective set</span>}
        </h1>
      </div>
      <div className="flex items-center gap-0 border-t border-white/[0.04] bg-black/[0.16] px-10 py-2.5">
        <div className="flex items-center gap-3 pr-5 font-mono text-[11.5px] tabular-nums text-fg-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-fg-4">bus</span>
          <span><span className="text-fg-0">{findings.length}</span> findings</span>
          <span className="text-fg-4">·</span>
          <span><span className="text-fg-0">{reads.length}</span> reads</span>
        </div>
        <div className="h-3.5 w-px shrink-0 bg-white/[0.08]" />
        <div className="flex items-center gap-3 px-5 font-mono text-[11.5px] tabular-nums text-fg-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-fg-4">agents</span>
          <span><span className="text-fg-0">{running}</span> running</span>
          <span className="text-fg-4">/</span>
          <span><span className="text-fg-0">{agents.length}</span> total</span>
        </div>
        <div className="ml-auto flex items-center gap-4 font-mono text-[11.5px] text-fg-2">
          <MetaItem label="ctx" value={`${contextPct}%`} />
          <MetaItem label="spend" value={`$${cost.toFixed(2)}`} />
        </div>
      </div>
    </header>
  )
}

function MetaItem({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-fg-4">{label}</span>
      <span className="tabular-nums text-fg-0">{value}</span>
    </span>
  )
}

// ===================== TIMELINE =====================

const LANE_HEIGHT = 30
const LANE_LABEL_WIDTH = 180
const TIMELINE_PAD_X = 16
const CHIP_R = 4.5
const READ_R = 3.5

function Timeline({
  lanes,
  laneIndexById,
  publishedFindings,
  readEvents,
  findingByIndex,
  selectedFindingIndex,
  selectedAgentId,
  onSelectFinding,
  onSelectAgent,
  onOpenAgent,
}: {
  lanes: AgentLane[]
  laneIndexById: Map<string, number>
  publishedFindings: BusEventView[]
  readEvents: ReadEvent[]
  findingByIndex: Map<number, BusEventView>
  selectedFindingIndex: number | null
  selectedAgentId: string | null
  onSelectFinding: (idx: number | null) => void
  onSelectAgent: (id: string) => void
  onOpenAgent: (id: string, mode?: 'replace' | 'split') => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(800)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(Math.max(400, entry.contentRect.width))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const laneY = (i: number) => 18 + i * LANE_HEIGHT + LANE_HEIGHT / 2

  // Even spacing: chips are placed by ORDINAL position in the merged
  // chronological event stream, not by timestamp. This keeps old +
  // new events equally readable when a session is reopened after a
  // long gap — otherwise an early burst gets compressed into the
  // first 5% of the timeline width.
  //
  // Time information isn't lost; it moves to per-chip labels + the
  // tooltip + gap markers between distant events.
  type OrderedEvent =
    | { kind: 'finding'; id: string; t: number; ref: BusEventView }
    | { kind: 'read'; id: string; t: number; ref: ReadEvent }
  const ordered = useMemo<OrderedEvent[]>(() => {
    const arr: OrderedEvent[] = []
    for (const f of publishedFindings) {
      arr.push({ kind: 'finding', id: `f-${f.index}`, t: f.timestamp, ref: f })
    }
    for (const r of readEvents) {
      arr.push({ kind: 'read', id: r.id, t: r.timestamp, ref: r })
    }
    arr.sort((a, b) => a.t - b.t)
    return arr
  }, [publishedFindings, readEvents])

  const ordinalById = useMemo(() => {
    const m = new Map<string, number>()
    ordered.forEach((e, i) => m.set(e.id, i))
    return m
  }, [ordered])

  const innerWidth = Math.max(100, width - LANE_LABEL_WIDTH - TIMELINE_PAD_X * 2)
  // Even step between ordinals. With one event we center; with N we
  // spread across innerWidth with a half-step margin on each side
  // so nothing hugs the edge.
  const ordinalCount = Math.max(1, ordered.length)
  const ordinalStep = ordinalCount > 1 ? innerWidth / (ordinalCount - 1) : 0
  const xForOrdinal = (n: number): number => {
    if (ordered.length <= 1) return TIMELINE_PAD_X + innerWidth / 2
    return TIMELINE_PAD_X + n * ordinalStep
  }
  const xForEventId = (id: string): number | null => {
    const ord = ordinalById.get(id)
    return ord === undefined ? null : xForOrdinal(ord)
  }

  // Gap markers: when consecutive events are far apart in wall-clock
  // time, place a vertical dashed line BETWEEN their ordinals with
  // a relative-duration label. Lets the operator see "there was a
  // 6h pause between burst A and burst B" without having to read
  // every timestamp.
  const gapMarkers = useMemo(() => {
    if (ordered.length < 2) return []
    const out: Array<{ key: string; xMid: number; label: string }> = []
    // Threshold scales with the session's overall duration. For a
    // 10-minute session, mark gaps > 30s; for a multi-day session,
    // only call out gaps > 1h. Logarithmic.
    const totalSpan = ordered[ordered.length - 1].t - ordered[0].t
    const threshold = Math.max(30_000, totalSpan * 0.05)
    for (let i = 1; i < ordered.length; i++) {
      const dt = ordered[i].t - ordered[i - 1].t
      if (dt < threshold) continue
      const xMid =
        (xForOrdinal(i - 1) + xForOrdinal(i)) / 2
      out.push({
        key: `gap-${i}`,
        xMid,
        label: formatDurationCompact(dt),
      })
    }
    return out
  }, [ordered, ordinalStep])

  const totalHeight = Math.max(120, 18 + lanes.length * LANE_HEIGHT + 18)

  // Build read-arc render data, using ordinal positions for both
  // endpoints. Arcs no longer represent "time passing horizontally"
  // — they show source → reader connectivity at whatever ordinal
  // positions those events occupy.
  const arcs = useMemo(() => {
    const out: Array<{
      key: string
      x1: number
      y1: number
      x2: number
      y2: number
      sourceIndex: number
      readerId: string
      sourceSenderId: string
    }> = []
    for (const r of readEvents) {
      const readerLaneIdx = laneIndexById.get(r.senderId)
      if (readerLaneIdx === undefined) continue
      const xR = xForEventId(r.id)
      if (xR === null) continue
      const yR = laneY(readerLaneIdx)
      for (const idx of r.sourceIndices) {
        const f = findingByIndex.get(idx)
        if (!f) continue
        const srcLaneIdx = laneIndexById.get(f.senderId)
        if (srcLaneIdx === undefined) continue
        const x1 = xForEventId(`f-${idx}`)
        if (x1 === null) continue
        const y1 = laneY(srcLaneIdx)
        out.push({
          key: `${r.id}-${idx}`,
          x1,
          y1,
          x2: xR,
          y2: yR,
          sourceIndex: idx,
          readerId: r.senderId,
          sourceSenderId: f.senderId,
        })
      }
    }
    return out
  }, [readEvents, publishedFindings, laneIndexById, findingByIndex, ordinalById, ordinalStep])

  if (lanes.length === 0) {
    return (
      <div className="flex items-center justify-center border-b border-white/[0.06] px-10 py-12 text-center font-mono text-[12px] text-fg-3">
        no subagents yet — spawn agents with the <code className="text-fg-1">sub_agent</code> tool to see the bus flow.
      </div>
    )
  }

  // Relative-time label policy: show the absolute clock time above
  // every chip; if there are too many chips for that to be legible
  // we'd switch to every Nth, but the limit isn't a hard wall (the
  // labels just stack visually). At ordinalStep < 14 we drop them.
  const showInlineTimeLabels = ordinalStep >= 14

  return (
    <section
      ref={containerRef}
      className="relative min-h-0 overflow-y-auto border-b border-white/[0.06] bg-black/[0.06]"
    >
      <svg
        width={width}
        height={totalHeight}
        className="block"
        onClick={() => {
          // Click-through to background clears selection
          onSelectFinding(null)
        }}
      >
        {/* Lanes */}
        {lanes.map((lane, i) => {
          const dim =
            (selectedAgentId !== null && selectedAgentId !== lane.id) ||
            (selectedFindingIndex !== null && !findingTouchesAgent(
              selectedFindingIndex,
              lane.id,
              findingByIndex,
              readEvents,
            ))
          const y = laneY(i)
          return (
            <g key={lane.id} opacity={dim ? 0.32 : 1}>
              {/* Lane label (clickable) */}
              <foreignObject x={0} y={y - 12} width={LANE_LABEL_WIDTH - 6} height={24}>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    onSelectAgent(lane.id)
                  }}
                  onDoubleClick={(e) => {
                    e.stopPropagation()
                    onOpenAgent(lane.id)
                  }}
                  title={`${lane.label} (${lane.agentType}) — double-click to open session`}
                  className={`flex h-full w-full items-center gap-1.5 truncate rounded px-2 text-left font-mono text-[11px] transition ${
                    selectedAgentId === lane.id
                      ? 'bg-accent/[0.10] text-accent'
                      : 'text-fg-1 hover:bg-white/[0.04] hover:text-fg-0'
                  }`}
                >
                  <StatusDot status={lane.status} />
                  <span className="truncate">{lane.label}</span>
                  <span className="ml-auto shrink-0 font-mono text-[9.5px] uppercase tracking-[0.12em] text-fg-4">
                    {lane.agentType}
                  </span>
                </button>
              </foreignObject>

              {/* Lane background line — faint, full-row. Replaces
                  the old time-anchored lifespan line. */}
              <line
                x1={LANE_LABEL_WIDTH + TIMELINE_PAD_X}
                y1={y}
                x2={LANE_LABEL_WIDTH + innerWidth + TIMELINE_PAD_X}
                y2={y}
                stroke={lane.ended ? 'rgba(255,255,255,0.05)' : 'rgba(168,212,252,0.10)'}
                strokeWidth={1}
                strokeDasharray={lane.ended ? '2,3' : undefined}
              />
              {/* Running indicator chip at the far right of the lane */}
              {!lane.ended && (
                <circle
                  cx={LANE_LABEL_WIDTH + innerWidth + TIMELINE_PAD_X}
                  cy={y}
                  r={3}
                  fill="rgba(168,212,252,0.85)"
                  className="animate-pulse-soft"
                />
              )}
            </g>
          )
        })}

        {/* Gap markers — vertical dashed lines between ordinals
            where wall-clock time jumped significantly. */}
        <g>
          {gapMarkers.map((g) => {
            const x = LANE_LABEL_WIDTH + g.xMid
            return (
              <g key={g.key}>
                <line
                  x1={x}
                  y1={6}
                  x2={x}
                  y2={totalHeight - 6}
                  stroke="rgba(255,255,255,0.10)"
                  strokeWidth={1}
                  strokeDasharray="1,4"
                />
                <text
                  x={x}
                  y={12}
                  textAnchor="middle"
                  fontSize={8.5}
                  fill="rgba(255,255,255,0.40)"
                  fontFamily="ui-monospace, monospace"
                  className="uppercase tracking-wider"
                >
                  {`↤ ${g.label} ↦`}
                </text>
              </g>
            )
          })}
        </g>

        {/* Read arcs (drawn beneath chips) */}
        <g>
          {arcs.map((arc) => {
            const dim =
              (selectedFindingIndex !== null && selectedFindingIndex !== arc.sourceIndex) ||
              (selectedAgentId !== null &&
                selectedAgentId !== arc.readerId &&
                selectedAgentId !== arc.sourceSenderId)
            const x1 = LANE_LABEL_WIDTH + arc.x1
            const x2 = LANE_LABEL_WIDTH + arc.x2
            const dx = x2 - x1
            const dy = arc.y2 - arc.y1
            // Cubic bezier with vertical-ish control offsets so the curve
            // bends out and back instead of crossing the lanes flat.
            const cy1 = arc.y1 + dy * 0.05
            const cy2 = arc.y2 - dy * 0.05
            const cx1 = x1 + dx * 0.35
            const cx2 = x2 - dx * 0.35
            const d = `M ${x1} ${arc.y1} C ${cx1} ${cy1}, ${cx2} ${cy2}, ${x2} ${arc.y2}`
            return (
              <path
                key={arc.key}
                d={d}
                fill="none"
                stroke="rgba(168,212,252,0.55)"
                strokeWidth={selectedFindingIndex === arc.sourceIndex ? 1.4 : 0.75}
                opacity={dim ? 0.12 : 0.65}
                strokeLinecap="round"
              />
            )
          })}
        </g>

        {/* Finding chips (drawn over arcs) */}
        {publishedFindings.map((f) => {
          const laneIdx = laneIndexById.get(f.senderId)
          if (laneIdx === undefined) return null
          const xRel = xForEventId(`f-${f.index}`)
          if (xRel === null) return null
          const cx = LANE_LABEL_WIDTH + xRel
          const cy = laneY(laneIdx)
          const colors = TOPIC_COLORS[f.topic] ?? TOPIC_COLORS.findings
          const selected = selectedFindingIndex === f.index
          const dim =
            (selectedFindingIndex !== null && !selected) ||
            (selectedAgentId !== null && selectedAgentId !== f.senderId)
          return (
            <g key={`f-${f.index}`} opacity={dim ? 0.30 : 1}>
              <circle
                cx={cx}
                cy={cy}
                r={selected ? CHIP_R + 2 : CHIP_R}
                className={`${colors.fill} cursor-pointer`}
                stroke={selected ? 'rgba(168,212,252,0.95)' : 'rgba(0,0,0,0.4)'}
                strokeWidth={selected ? 1.5 : 1}
                onClick={(e) => {
                  e.stopPropagation()
                  onSelectFinding(selected ? null : f.index)
                }}
              >
                <title>
                  {`F${f.index} · ${f.topic} · ${f.senderLabel || f.senderId} · ${fmtClock(f.timestamp)}\n\n${f.content}`}
                </title>
              </circle>
              {/* F# label above chip */}
              <text
                x={cx}
                y={cy - CHIP_R - 4}
                textAnchor="middle"
                fontSize={8}
                fill="rgba(255,255,255,0.45)"
                fontFamily="ui-monospace, monospace"
                pointerEvents="none"
              >
                F{f.index}
              </text>
              {/* Inline clock-time label below the chip when there's
                  room — supplements the on-hover tooltip. */}
              {showInlineTimeLabels && (
                <text
                  x={cx}
                  y={cy + CHIP_R + 10}
                  textAnchor="middle"
                  fontSize={8}
                  fill="rgba(255,255,255,0.32)"
                  fontFamily="ui-monospace, monospace"
                  pointerEvents="none"
                >
                  {fmtClock(f.timestamp)}
                </text>
              )}
            </g>
          )
        })}

        {/* Read marks — small open circles on the reader's lane */}
        {readEvents.map((r) => {
          const laneIdx = laneIndexById.get(r.senderId)
          if (laneIdx === undefined) return null
          const xRel = xForEventId(r.id)
          if (xRel === null) return null
          const cx = LANE_LABEL_WIDTH + xRel
          const cy = laneY(laneIdx)
          const dim =
            (selectedFindingIndex !== null && !r.sourceIndices.includes(selectedFindingIndex)) ||
            (selectedAgentId !== null && selectedAgentId !== r.senderId)
          return (
            <circle
              key={r.id}
              cx={cx}
              cy={cy}
              r={READ_R}
              fill="none"
              stroke="rgba(168,212,252,0.65)"
              strokeWidth={1.1}
              opacity={dim ? 0.30 : 0.85}
            >
              <title>
                {`Read ${r.sourceIndices.length} finding(s): ${r.sourceIndices.map((i) => `F${i}`).join(', ')} · ${fmtClock(r.timestamp)}`}
              </title>
            </circle>
          )
        })}
      </svg>
    </section>
  )
}

/** "HH:MM" — short clock time used above each chip. */
function fmtClock(ts: number): string {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** Compact human-readable duration: "30s", "5m", "2h", "1d6h". Used in
 *  gap markers between distant events. */
function formatDurationCompact(ms: number): string {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s gap`
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m gap`
  const h = Math.floor(m / 60)
  const remM = m % 60
  if (h < 24) return remM ? `${h}h${remM}m gap` : `${h}h gap`
  const d = Math.floor(h / 24)
  const remH = h % 24
  return remH ? `${d}d${remH}h gap` : `${d}d gap`
}

function StatusDot({ status }: { status: AgentView['status'] }) {
  if (status === 'running') {
    return (
      <span className="relative inline-flex h-2 w-2 shrink-0 items-center justify-center">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-50" />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
      </span>
    )
  }
  const cls =
    status === 'done'
      ? 'bg-ok'
      : status === 'failed'
      ? 'bg-warn'
      : status === 'cancelled'
      ? 'bg-fg-4'
      : 'bg-white/40'
  return <span className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${cls}`} />
}

function findingTouchesAgent(
  findingIndex: number,
  agentId: string,
  findingByIndex: Map<number, BusEventView>,
  readEvents: ReadEvent[],
): boolean {
  const f = findingByIndex.get(findingIndex)
  if (!f) return false
  if (f.senderId === agentId) return true
  for (const r of readEvents) {
    if (r.senderId === agentId && r.sourceIndices.includes(findingIndex)) return true
  }
  return false
}

// ===================== BUS TAPE =====================

function BusTape({
  scrollerRef,
  publishedFindings,
  readEvents,
  findingByIndex,
  lanes,
  selectedFindingIndex,
  selectedAgentId,
  onSelectFinding,
  onSelectAgent,
}: {
  scrollerRef: React.RefObject<HTMLDivElement>
  publishedFindings: BusEventView[]
  readEvents: ReadEvent[]
  findingByIndex: Map<number, BusEventView>
  lanes: AgentLane[]
  selectedFindingIndex: number | null
  selectedAgentId: string | null
  onSelectFinding: (idx: number | null) => void
  onSelectAgent: (id: string) => void
}) {
  // Compute "read by" reverse index: finding-index → list of reader sessionIds.
  const readersByFinding = useMemo(() => {
    const m = new Map<number, string[]>()
    for (const r of readEvents) {
      for (const idx of r.sourceIndices) {
        const arr = m.get(idx) ?? []
        if (!arr.includes(r.senderId)) arr.push(r.senderId)
        m.set(idx, arr)
      }
    }
    return m
  }, [readEvents])

  const labelById = useMemo(() => {
    const m = new Map<string, { label: string; agentType: string }>()
    for (const l of lanes) m.set(l.id, { label: l.label, agentType: l.agentType })
    return m
  }, [lanes])

  // Merge findings + reads into a single chronological stream so the
  // tape reads as a real conversation. Reads get rendered as a more
  // compact row; findings as full cards.
  type TapeEntry =
    | { kind: 'finding'; at: number; finding: BusEventView }
    | { kind: 'read'; at: number; read: ReadEvent }
  const stream: TapeEntry[] = useMemo(() => {
    const out: TapeEntry[] = []
    for (const f of publishedFindings) out.push({ kind: 'finding', at: f.timestamp, finding: f })
    for (const r of readEvents) out.push({ kind: 'read', at: r.timestamp, read: r })
    out.sort((a, b) => a.at - b.at)
    return out
  }, [publishedFindings, readEvents])

  if (stream.length === 0) {
    return (
      <section className="flex items-center justify-center px-10 py-12 text-center font-mono text-[12px] text-fg-3">
        no bus traffic yet — subagents publish via{' '}
        <code className="ml-1 mr-1 text-fg-1">publish_finding</code> and consume via{' '}
        <code className="ml-1 text-fg-1">read_findings</code>.
      </section>
    )
  }

  return (
    <section
      ref={scrollerRef}
      className="min-h-0 overflow-y-auto px-10 py-5"
    >
      <div className="mx-auto flex max-w-[920px] flex-col gap-2">
        {stream.map((entry, i) => {
          if (entry.kind === 'finding') {
            const f = entry.finding
            const senderMeta = labelById.get(f.senderId)
            const readers = readersByFinding.get(f.index) ?? []
            const colors = TOPIC_COLORS[f.topic] ?? TOPIC_COLORS.findings
            const selected = selectedFindingIndex === f.index
            const dim =
              selectedAgentId !== null &&
              selectedAgentId !== f.senderId &&
              !readers.includes(selectedAgentId)
            return (
              <article
                key={`f-${f.index}-${i}`}
                data-finding-index={f.index}
                onClick={() => onSelectFinding(selected ? null : f.index)}
                className={`cursor-pointer overflow-hidden rounded-md border transition ${
                  selected
                    ? 'border-accent/[0.40] bg-accent/[0.06]'
                    : 'border-white/[0.06] bg-white/[0.018] hover:border-white/[0.12] hover:bg-white/[0.035]'
                } ${dim ? 'opacity-40' : ''}`}
              >
                <header className="flex items-center gap-3 border-b border-white/[0.04] px-4 py-2">
                  <span className={`font-mono text-[10px] uppercase tracking-[0.16em] ${colors.text}`}>
                    F{f.index} · {f.topic}
                  </span>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      onSelectAgent(f.senderId)
                    }}
                    className="font-mono text-[11.5px] text-fg-1 transition hover:text-accent"
                  >
                    {senderMeta?.label || f.senderLabel || f.senderId}
                  </button>
                  {senderMeta && (
                    <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-fg-4">
                      {senderMeta.agentType}
                    </span>
                  )}
                  <span className="ml-auto font-mono text-[10px] tabular-nums text-fg-4">
                    {fmtTime(f.timestamp)}
                  </span>
                </header>
                <p className="m-0 select-text whitespace-pre-wrap px-4 py-3 font-mono text-[12.5px] leading-[1.6] text-fg-1">
                  {f.content}
                </p>
                {readers.length > 0 && (
                  <footer className="flex flex-wrap items-center gap-1.5 border-t border-white/[0.04] px-4 py-2 font-mono text-[10.5px] text-fg-3">
                    <span className="uppercase tracking-[0.14em] text-fg-4">read by</span>
                    {readers.map((rId) => {
                      const meta = labelById.get(rId)
                      return (
                        <button
                          key={rId}
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            onSelectAgent(rId)
                          }}
                          className={`rounded border px-1.5 py-0.5 transition ${
                            selectedAgentId === rId
                              ? 'border-accent/[0.32] bg-accent/[0.10] text-accent'
                              : 'border-white/[0.08] bg-white/[0.02] text-fg-2 hover:border-white/[0.18] hover:text-fg-0'
                          }`}
                        >
                          {meta?.label || rId}
                        </button>
                      )
                    })}
                  </footer>
                )}
              </article>
            )
          }
          // read row
          const r = entry.read
          const reader = labelById.get(r.senderId)
          const dim =
            selectedAgentId !== null && selectedAgentId !== r.senderId &&
            !r.sourceIndices.some((idx) => {
              const f = findingByIndex.get(idx)
              return f?.senderId === selectedAgentId
            })
          const matchesSelectedFinding =
            selectedFindingIndex !== null && r.sourceIndices.includes(selectedFindingIndex)
          return (
            <div
              key={`r-${i}`}
              className={`flex items-baseline gap-2 rounded px-3 py-1 font-mono text-[11.5px] text-fg-2 ${
                matchesSelectedFinding ? 'bg-accent/[0.06]' : ''
              } ${dim ? 'opacity-40' : ''}`}
            >
              <span className="text-fg-4">↳</span>
              <button
                type="button"
                onClick={() => onSelectAgent(r.senderId)}
                className="text-fg-1 transition hover:text-accent"
              >
                {reader?.label || r.senderId}
              </button>
              <span className="text-fg-3">read</span>
              {r.sourceIndices.map((idx) => (
                <button
                  key={idx}
                  type="button"
                  onClick={() => onSelectFinding(idx)}
                  className={`rounded px-1 transition ${
                    selectedFindingIndex === idx
                      ? 'bg-accent/[0.12] text-accent'
                      : 'text-fg-2 hover:bg-white/[0.06] hover:text-accent'
                  }`}
                >
                  F{idx}
                </button>
              ))}
              <span className="ml-auto font-mono text-[10px] tabular-nums text-fg-4">
                {fmtTime(r.timestamp)}
              </span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function fmtTime(at: number): string {
  const d = new Date(at)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}

