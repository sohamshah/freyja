import React, { useMemo, useState } from 'react'
import type {
  AgentView,
  KanbanCardView,
  TelemetryEventView,
} from '../shared/types'
import {
  DetailDrawer,
  DrawerAction,
  DrawerAssignment,
  DrawerDependencies,
  DrawerSection,
  DrawerTimeline,
} from '../shared/DetailDrawer'
import { useHarness } from '../../state/store'
import { AddTaskForm } from './AddTaskForm'
import { ShinyFabricBackdrop } from './ShinyFabricBackdrop'

interface Props {
  sessionId: string
  objective: string
  cards: KanbanCardView[]
  agents: AgentView[]
  telemetryEvents: TelemetryEventView[]
  contextPct: number
  cost: number
  /** Bridge `auto_dispatch_enabled` mirror, tracked per session in the
   *  slice. Drives the header's autopilot toggle so the operator can
   *  flip the dispatcher on/off without knowing the `/autopilot` slash
   *  command exists. */
  autoDispatchEnabled: boolean
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onOpenDispatcherBrief: () => void
}

const STALE_THRESHOLD_MS = 12 * 60 * 1000

export function KanbanBridgeView({
  sessionId,
  objective,
  cards,
  agents,
  telemetryEvents,
  contextPct,
  cost,
  autoDispatchEnabled,
  onAttach,
  onOpenDispatcherBrief,
}: Props) {
  const setKanbanAutopilot = useHarness((s) => s.setKanbanAutopilot)
  const [openCardId, setOpenCardId] = useState<string | null>(null)
  // Operator-intake form open state — toggled by the "+ Add task"
  // affordance at the top of the board column. Lifted here (instead of
  // inside BoardColumn) so the form lifecycle survives buckets
  // rebuilding between renders.
  const [addOpen, setAddOpen] = useState(false)

  const buckets = useMemo(() => bucketCards(cards), [cards])
  const stations = useMemo(() => agentStations(agents, cards), [agents, cards])
  const stalest = useMemo(() => stations.find((s) => s.stale), [stations])
  const nextCard = useMemo(() => buckets.ready[0], [buckets])
  const dispatchFeed = useMemo(() => dispatchEvents(telemetryEvents), [telemetryEvents])

  const openCard = openCardId ? cards.find((c) => c.id === openCardId) ?? null : null
  const drawerOpen = openCard != null
  const idleStations = useMemo(() => stations.filter((s) => !s.cardId), [stations])

  // Two columns plus an optional drawer column on the right. Crew was
  // consolidated into the Board (each in-flight card now embeds its
  // assigned-agent station info), so there's no separate crew rail.
  const gridCols = drawerOpen
    ? 'grid-cols-[minmax(0,1fr)_320px_480px]'
    : 'grid-cols-[minmax(0,1fr)_320px]'

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <Header
        objective={objective}
        buckets={buckets}
        stalest={stalest}
        agentsCount={agents.length}
        contextPct={contextPct}
        cost={cost}
        autoDispatchEnabled={autoDispatchEnabled}
        onToggleAutopilot={() => {
          void setKanbanAutopilot(sessionId, !autoDispatchEnabled)
        }}
        onOpenDispatcherBrief={onOpenDispatcherBrief}
      />

      <div className={`grid min-h-0 flex-1 overflow-hidden ${gridCols}`}>
        <BoardColumn
          sessionId={sessionId}
          cards={cards}
          buckets={buckets}
          stations={stations}
          idleStations={idleStations}
          nextCardId={nextCard?.id}
          addOpen={addOpen}
          onSetAddOpen={setAddOpen}
          onOpenCard={setOpenCardId}
          onOpenAgent={(id) => onAttach(id, 'split')}
        />
        <FeedColumn
          events={dispatchFeed}
          nextCard={nextCard}
          stalest={stalest}
          onOpenDispatcherBrief={onOpenDispatcherBrief}
        />
        <DetailDrawer
          open={drawerOpen}
          onClose={() => setOpenCardId(null)}
          title={openCard?.title ?? ''}
          statusLabel={openCard ? cardStatusLabel(openCard) : undefined}
          backdrop={<ShinyFabricBackdrop active={drawerOpen} intensity={0.85} />}
          footer={
            <>
              <DrawerAction onClick={() => openCard && onAttach(openCard.id, 'split')}>Open split</DrawerAction>
              <DrawerAction onClick={() => openCard && onAttach(openCard.id, 'replace')}>Open here</DrawerAction>
              <DrawerAction variant="warn">Reclaim</DrawerAction>
              <DrawerAction variant="ok">Mark done</DrawerAction>
            </>
          }
        >
          {openCard ? <CardDrawerBody card={openCard} allCards={cards} /> : null}
        </DetailDrawer>
      </div>
    </div>
  )
}

// ============ HEADER ============

function Header({
  objective,
  buckets,
  stalest,
  agentsCount,
  contextPct,
  cost,
  autoDispatchEnabled,
  onToggleAutopilot,
  onOpenDispatcherBrief,
}: {
  objective: string
  buckets: Buckets
  stalest: Station | undefined
  agentsCount: number
  contextPct: number
  cost: number
  autoDispatchEnabled: boolean
  onToggleAutopilot: () => void
  onOpenDispatcherBrief: () => void
}) {
  const total =
    buckets.done.length +
    buckets.ready.length +
    buckets.flight.length +
    buckets.review.length +
    buckets.blocked.length
  return (
    <header className="flex items-end justify-between gap-8 border-b border-white/[0.06] px-10 py-7">
      <div className="min-w-0 flex-1">
        <div className="mb-3 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          mission
        </div>
        <h1 className="m-0 max-w-[880px] truncate font-serif text-[24px] font-light leading-[1.4] tracking-[-0.005em] text-fg-1">
          {objective || <span className="italic text-fg-3">no objective set</span>}
        </h1>
        <div className="mt-4 flex flex-wrap gap-5 font-mono text-[11.5px] tracking-[0.06em] text-fg-2">
          <span>
            <span className="tabular-nums text-fg-0">{buckets.done.length}</span>{' '}
            <span className="text-fg-3">of</span>{' '}
            <span className="tabular-nums text-fg-0">{total}</span> done
          </span>
          <span>
            <span className="text-accent tabular-nums">{buckets.flight.length}</span> in flight
          </span>
          <span>
            <span className="text-fg-0 tabular-nums">{buckets.ready.length}</span> ready
          </span>
          {buckets.review.length > 0 ? (
            <span>
              <span className="text-fg-1 tabular-nums">{buckets.review.length}</span> in review
            </span>
          ) : null}
          {buckets.blocked.length > 0 ? (
            <span className="text-warn">
              <span className="tabular-nums">{buckets.blocked.length}</span> blocked
            </span>
          ) : null}
          {stalest ? (
            <span className="text-danger">
              <span className="tabular-nums">1</span> stale
            </span>
          ) : null}
          <span className="text-fg-3">·</span>
          <span>
            <span className="text-fg-0 tabular-nums">{agentsCount}</span> agents
          </span>
          <span>
            <span className="text-fg-0 tabular-nums">{contextPct}%</span> ctx
          </span>
          <span>
            <span className="text-fg-0 tabular-nums">${cost.toFixed(2)}</span> spend
          </span>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <AutopilotToggle
          enabled={autoDispatchEnabled}
          onClick={onToggleAutopilot}
        />
        <button
          type="button"
          onClick={onOpenDispatcherBrief}
          className="rounded-md border border-accent/[0.22] bg-accent/[0.06] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.12]"
        >
          Rules
        </button>
      </div>
    </header>
  )
}

/** Pill-shaped on/off toggle for kanban auto-dispatch. Acts as the
 *  discoverable UI surface for the underlying `kanban_autopilot`
 *  command — most users won't know `/autopilot on` exists, so we
 *  surface the state + action right next to the dispatch rules
 *  button. Operator-added cards still dispatch immediately on create
 *  (force=True path in the bridge); this toggle controls whether
 *  *background* dispatch picks up agent-created cards + downstream
 *  follow-ups. */
function AutopilotToggle({
  enabled,
  onClick,
}: {
  enabled: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      role="switch"
      aria-checked={enabled}
      title={
        enabled
          ? 'Autopilot is ON — agent-created and follow-up cards auto-dispatch as agents free up.'
          : 'Autopilot is OFF — agent-created and follow-up cards sit in READY until you flip this on (operator-added cards still dispatch immediately).'
      }
      className={`group inline-flex items-center gap-2 rounded-full border px-3 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] transition ${
        enabled
          ? 'border-accent/[0.40] bg-accent/[0.12] text-accent shadow-[0_0_0_1px_rgba(168,212,252,0.10)]'
          : 'border-white/[0.10] bg-bg-1 text-fg-3 hover:border-white/[0.20] hover:text-fg-1'
      }`}
    >
      <span
        className={`relative inline-flex h-3 w-6 items-center rounded-full transition ${
          enabled ? 'bg-accent/[0.55]' : 'bg-white/[0.08]'
        }`}
      >
        <span
          className={`block h-2 w-2 rounded-full bg-fg-0 shadow-sm transition-transform ${
            enabled ? 'translate-x-3.5' : 'translate-x-0.5'
          }`}
        />
      </span>
      <span>autopilot</span>
      <span
        className={`tabular-nums normal-case tracking-normal text-[10px] ${
          enabled ? 'text-accent' : 'text-fg-4'
        }`}
      >
        {enabled ? 'on' : 'off'}
      </span>
    </button>
  )
}

// ============ AGENT BADGE + COLOR HELPERS ============

interface Station {
  agent: AgentView
  card?: KanbanCardView
  cardId?: string
  stale: boolean
  ageMs: number
  lastEventMs?: number
}

/** Visual palette indexed by agent type. Falls back to a muted grey for
 *  unknown types so badges still render distinctly without dominating
 *  the chrome. Saturations are deliberately soft — Freyja's chrome is
 *  near-monochrome so an agent badge only needs to register as
 *  "different category" at a glance. */
const AGENT_TYPE_PALETTE: Record<string, { bg: string; fg: string; ring: string }> = {
  explore:     { bg: 'rgba(168, 212, 252, 0.10)', fg: 'rgb(168, 212, 252)', ring: 'rgba(168, 212, 252, 0.35)' },
  'explore-fast': { bg: 'rgba(126, 175, 234, 0.10)', fg: 'rgb(126, 175, 234)', ring: 'rgba(126, 175, 234, 0.32)' },
  code:        { bg: 'rgba(126, 201, 165, 0.10)', fg: 'rgb(126, 201, 165)', ring: 'rgba(126, 201, 165, 0.35)' },
  verify:      { bg: 'rgba(180, 130, 130, 0.10)', fg: 'rgb(208, 158, 158)', ring: 'rgba(208, 158, 158, 0.35)' },
  plan:        { bg: 'rgba(232, 196, 132, 0.10)', fg: 'rgb(232, 196, 132)', ring: 'rgba(232, 196, 132, 0.35)' },
  memory:      { bg: 'rgba(208, 158, 220, 0.10)', fg: 'rgb(208, 158, 220)', ring: 'rgba(208, 158, 220, 0.35)' },
  browser:     { bg: 'rgba(208, 160, 64, 0.12)',  fg: 'rgb(228, 180, 96)',  ring: 'rgba(228, 180, 96, 0.35)' },
  docs:        { bg: 'rgba(200, 180, 230, 0.10)', fg: 'rgb(200, 180, 230)', ring: 'rgba(200, 180, 230, 0.35)' },
  perf:        { bg: 'rgba(245, 165, 110, 0.10)', fg: 'rgb(245, 165, 110)', ring: 'rgba(245, 165, 110, 0.35)' },
  general:     { bg: 'rgba(168, 168, 168, 0.10)', fg: 'rgb(184, 184, 184)', ring: 'rgba(184, 184, 184, 0.30)' },
}

function agentPalette(type?: string) {
  const t = (type || 'general').toLowerCase().trim()
  if (AGENT_TYPE_PALETTE[t]) return AGENT_TYPE_PALETTE[t]
  for (const key of Object.keys(AGENT_TYPE_PALETTE)) {
    if (t.startsWith(key) || key.startsWith(t)) return AGENT_TYPE_PALETTE[key]
  }
  return AGENT_TYPE_PALETTE.general
}

function agentInitials(type?: string, fallback?: string): string {
  const base = (type || fallback || 'AG').trim()
  if (!base) return 'AG'
  const parts = base.split(/[-_\s.]+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return base.slice(0, 2).toUpperCase()
}

/** Compact visual representation of an agent. Square avatar with the
 *  agent type's initials, a status dot in the corner, and an optional
 *  trailing meta line ("running · 12m" / "stale" / "idle"). Clicking
 *  the badge opens the agent's session pane via `onOpenAgent`,
 *  bypassing the card drawer — gives a direct "what is this worker
 *  doing right now" route distinct from "what is this card about". */
function AgentBadge({
  station,
  variant = 'card',
  onOpenAgent,
}: {
  station: Station
  variant?: 'card' | 'inline' | 'idle'
  onOpenAgent?: (sessionId: string) => void
}) {
  const { agent, stale, card, ageMs } = station
  const palette = agentPalette(agent.agentType)
  const running = agent.status === 'running'
  const initials = agentInitials(agent.agentType, agent.session.title)
  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    onOpenAgent?.(agent.session.id)
  }
  const dotClass = stale
    ? 'bg-danger'
    : running
    ? 'bg-accent shadow-[0_0_5px_rgba(168,212,252,0.7)] animate-pulse-soft'
    : 'bg-fg-3'
  const tooltipBase = `${agent.agentType || 'agent'} · ${agent.status}`
  const tooltip = card
    ? `${tooltipBase}\non "${card.title}" for ${minutesAgo(ageMs)}${stale ? ' · stale' : ''}`
    : tooltipBase

  if (variant === 'idle') {
    return (
      <button
        type="button"
        onClick={handleClick}
        title={tooltip}
        className="group flex items-center gap-2 rounded-md border border-white/[0.04] bg-white/[0.012] px-2 py-1 transition hover:border-white/[0.12] hover:bg-white/[0.03]"
        style={{ borderColor: palette.ring }}
      >
        <span className="relative inline-flex">
          <span
            className="inline-flex h-6 w-6 items-center justify-center rounded-md font-mono text-[10px] font-medium tabular-nums"
            style={{ background: palette.bg, color: palette.fg }}
          >
            {initials}
          </span>
          <span className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ${dotClass}`} />
        </span>
        <span className="font-mono text-[10.5px] text-fg-2 group-hover:text-fg-0">
          {agent.agentType || agent.session.title?.slice(0, 10) || 'agent'}
        </span>
      </button>
    )
  }

  if (variant === 'inline') {
    return (
      <button
        type="button"
        onClick={handleClick}
        title={tooltip}
        className="relative inline-flex h-5 w-5 items-center justify-center rounded font-mono text-[9px] font-medium tabular-nums transition hover:scale-[1.06]"
        style={{ background: palette.bg, color: palette.fg }}
      >
        {initials}
        <span className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ${dotClass}`} />
      </button>
    )
  }

  // 'card' — primary in-flight rendering: bigger avatar + a short
  // status meta line beside it.
  return (
    <button
      type="button"
      onClick={handleClick}
      title={tooltip}
      className="group flex items-center gap-2.5 rounded-md border border-transparent px-1 py-1 -ml-1 transition hover:border-white/[0.06] hover:bg-white/[0.025]"
    >
      <span className="relative inline-flex">
        <span
          className="inline-flex h-8 w-8 items-center justify-center rounded-md font-mono text-[11px] font-medium tabular-nums ring-1"
          style={{ background: palette.bg, color: palette.fg, '--tw-ring-color': palette.ring } as React.CSSProperties}
        >
          {initials}
        </span>
        <span className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ring-1 ring-bg-0 ${dotClass}`} />
      </span>
      <span className="flex min-w-0 flex-col text-left leading-none">
        <span className="font-mono text-[11px] tracking-[-0.005em] text-fg-0">
          {agent.agentType || agent.session.title || agent.session.id.slice(0, 8)}
        </span>
        <span
          className={`mt-0.5 font-mono text-[9.5px] uppercase tracking-[0.14em] ${
            stale ? 'text-danger' : running ? 'text-accent' : 'text-fg-3'
          }`}
        >
          {stale ? `stale · ${minutesAgo(ageMs)}` : running ? `live · ${minutesAgo(ageMs)}` : agent.status}
        </span>
      </span>
    </button>
  )
}

/** Horizontal cluster of agent badges. Used when a card has more than
 *  one assigned worker (rare today; future-proofing). */
function AgentBadgeCluster({
  stations,
  onOpenAgent,
}: {
  stations: Station[]
  onOpenAgent?: (sessionId: string) => void
}) {
  if (stations.length === 0) return null
  if (stations.length === 1) {
    return <AgentBadge station={stations[0]} onOpenAgent={onOpenAgent} />
  }
  return (
    <div className="flex flex-col gap-1.5">
      {stations.map((s) => (
        <AgentBadge key={s.agent.session.id} station={s} onOpenAgent={onOpenAgent} />
      ))}
    </div>
  )
}

// ============ BOARD COLUMN ============

function BoardColumn({
  sessionId,
  cards,
  buckets,
  stations,
  idleStations,
  nextCardId,
  addOpen,
  onSetAddOpen,
  onOpenCard,
  onOpenAgent,
}: {
  sessionId: string
  cards: KanbanCardView[]
  buckets: Buckets
  stations: Station[]
  idleStations: Station[]
  nextCardId: string | undefined
  addOpen: boolean
  onSetAddOpen: (open: boolean) => void
  onOpenCard: (id: string) => void
  onOpenAgent: (sessionId: string) => void
}) {
  // Map cardId → assigned stations so the in-flight cards can render
  // their crew inline. Built once per render — small N, no need to
  // memoize the outer hook in callers.
  const stationsByCard = useMemo(() => {
    const m = new Map<string, Station[]>()
    for (const s of stations) {
      if (!s.cardId) continue
      const arr = m.get(s.cardId) ?? []
      arr.push(s)
      m.set(s.cardId, arr)
    }
    return m
  }, [stations])

  const total =
    buckets.ready.length +
    buckets.review.length +
    buckets.blocked.length +
    buckets.flight.length +
    buckets.done.length

  return (
    <aside className="overflow-y-auto px-6 pb-32">
      <div className="sticky top-0 z-[2] flex items-baseline justify-between bg-gradient-to-b from-bg-0 via-bg-0/70 to-transparent px-1 pt-4 pb-2.5 text-fg-3 text-[10.5px] uppercase tracking-[0.14em]">
        <span>Board</span>
        <div className="flex items-baseline gap-3">
          <span className="font-mono tracking-normal normal-case tabular-nums text-fg-3">
            {total} cards
          </span>
          <button
            type="button"
            onClick={() => onSetAddOpen(!addOpen)}
            className={`rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] transition ${
              addOpen
                ? 'border-accent/[0.40] bg-accent/[0.12] text-accent'
                : 'border-white/[0.06] bg-transparent text-fg-3 hover:border-accent/[0.30] hover:bg-accent/[0.05] hover:text-accent'
            }`}
            title="Add a task as the operator (⌘N)"
          >
            {addOpen ? '✕ close' : '+ add task'}
          </button>
        </div>
      </div>

      <AddTaskForm
        sessionId={sessionId}
        open={addOpen}
        cards={cards}
        onClose={() => onSetAddOpen(false)}
      />

      {/* In flight — the old "crew" column collapsed into the board.
          Each card now embeds the assigned-agent station info so the
          operator gets one focused view instead of having to mentally
          stitch crew rows to board cards. */}
      <FlightSection
        cards={buckets.flight}
        stationsByCard={stationsByCard}
        onOpenCard={onOpenCard}
        onOpenAgent={onOpenAgent}
      />

      {/* Available-agents strip — idle workers that can pick up the
          next card. Compact, scannable; click to jump straight into
          their session pane (gives the unique-view-per-click
          behavior the operator was asking for). */}
      {idleStations.length > 0 ? (
        <div className="mt-1 mb-5 animate-fade-in">
          <div className="mb-2 flex items-baseline justify-between text-fg-3 text-[10px] uppercase tracking-[0.14em]">
            <span className="inline-flex items-center gap-1.5 text-fg-2">
              <span className="h-1 w-1 rounded-full bg-fg-3" />
              available
            </span>
            <span className="font-mono tracking-normal normal-case tabular-nums">
              {idleStations.length} idle
            </span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {idleStations.map((s) => (
              <AgentBadge
                key={s.agent.session.id}
                station={s}
                variant="idle"
                onOpenAgent={onOpenAgent}
              />
            ))}
          </div>
        </div>
      ) : null}

      <QSection
        label="ready"
        tone="accent"
        count={`${buckets.ready.length}`}
        cards={buckets.ready}
        nextCardId={nextCardId}
        onOpenCard={onOpenCard}
      />
      {buckets.review.length > 0 ? (
        <QSection
          label="review"
          tone="info"
          count={`${buckets.review.length}`}
          cards={buckets.review}
          onOpenCard={onOpenCard}
        />
      ) : null}
      {buckets.blocked.length > 0 ? (
        <QSection label="blocked" tone="warn" count={`${buckets.blocked.length}`} cards={buckets.blocked} onOpenCard={onOpenCard} />
      ) : null}
      <QSection
        label="done"
        tone="ok"
        count={`${buckets.done.length}`}
        cards={buckets.done.slice(-6)}
        compact
        onOpenCard={onOpenCard}
      />
    </aside>
  )
}

/** In-flight section — the visual heart of the board. Each card is a
 *  bigger, richer block than the ready/done variants because this is
 *  where the operator's attention should land: what's actually
 *  happening right now, by whom, for how long. */
function FlightSection({
  cards,
  stationsByCard,
  onOpenCard,
  onOpenAgent,
}: {
  cards: KanbanCardView[]
  stationsByCard: Map<string, Station[]>
  onOpenCard: (id: string) => void
  onOpenAgent: (sessionId: string) => void
}) {
  if (cards.length === 0) {
    return (
      <div className="pb-5 border-b border-white/[0.06] pt-3 animate-fade-in">
        <div className="flex items-baseline justify-between mb-2 text-fg-3 text-[10px] uppercase tracking-[0.14em]">
          <span className="inline-flex items-center gap-1.5 text-fg-1">
            <span className="h-1 w-1 rounded-full bg-fg-3" />
            in flight
          </span>
          <span className="font-mono tracking-normal normal-case tabular-nums">0</span>
        </div>
        <div className="px-2 py-3 text-fg-3 italic text-[11.5px]">
          nothing in flight — pull a card to dispatch
        </div>
      </div>
    )
  }
  return (
    <div className="pb-5 border-b border-white/[0.06] pt-3">
      <div className="flex items-baseline justify-between mb-2.5 text-fg-3 text-[10px] uppercase tracking-[0.14em]">
        <span className="inline-flex items-center gap-1.5 text-fg-1">
          <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_5px_rgba(168,212,252,0.6)] animate-pulse-soft" />
          in flight
        </span>
        <span className="font-mono tracking-normal normal-case tabular-nums">
          {cards.length}
        </span>
      </div>
      <div className="flex flex-col gap-2.5">
        {cards.map((card) => {
          const cardStations = stationsByCard.get(card.id) ?? []
          return (
            <FlightCard
              key={card.id}
              card={card}
              stations={cardStations}
              onOpen={() => onOpenCard(card.id)}
              onOpenAgent={onOpenAgent}
            />
          )
        })}
      </div>
    </div>
  )
}

/** Rich in-flight card. Subtle accent border, prominent title, and
 *  the assigned agent station(s) on the right rail. Stale state
 *  paints the whole card warm-red so it surfaces as "needs attention"
 *  without needing the operator to read the meta line. */
function FlightCard({
  card,
  stations,
  onOpen,
  onOpenAgent,
}: {
  card: KanbanCardView
  stations: Station[]
  onOpen: () => void
  onOpenAgent: (sessionId: string) => void
}) {
  const stale = stations.some((s) => s.stale)
  const isOperatorCard = card.createdBy === 'operator'
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      className={`relative grid grid-cols-[1fr_auto] gap-4 rounded-[10px] border px-3.5 py-3 cursor-pointer transition animate-fade-in ${
        stale
          ? 'border-danger/[0.22] bg-danger/[0.035] hover:border-danger/[0.38] hover:bg-danger/[0.06]'
          : 'border-accent/[0.16] bg-accent/[0.025] hover:border-accent/[0.30] hover:bg-accent/[0.05]'
      }`}
    >
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="text-fg-0 text-[13px] leading-[1.45]">{card.title}</div>
        {card.summary ? (
          <div className="line-clamp-2 text-fg-2 text-[11px] leading-[1.55]">
            {card.summary}
          </div>
        ) : null}
        <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] tracking-[0.06em] text-fg-3">
          {isOperatorCard ? <OperatorChip /> : null}
          {card.priority != null ? <span>p{card.priority}</span> : null}
          {card.parents && card.parents.length > 0 ? (
            <span>↑ {card.parents.length} dep{card.parents.length === 1 ? '' : 's'}</span>
          ) : null}
          {card.children && card.children.length > 0 ? (
            <span>↓ {card.children.length} blocks</span>
          ) : null}
          {stale ? (
            <span className="rounded px-1.5 py-px text-danger bg-danger/[0.10] border border-danger/[0.30] text-[9.5px] uppercase tracking-[0.18em]">
              stale
            </span>
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 items-start">
        {stations.length > 0 ? (
          <AgentBadgeCluster stations={stations} onOpenAgent={onOpenAgent} />
        ) : (
          <span className="italic text-fg-3 text-[10.5px]">unassigned</span>
        )}
      </div>
    </div>
  )
}

/** Provenance chip for operator-created cards. Same shape as the other
 *  meta chips (priority, dep count) so it visually fits into the row,
 *  but accent-toned to read "this came from you" at a glance. */
function OperatorChip() {
  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-accent/[0.30] bg-accent/[0.08] px-1.5 py-px text-[9.5px] uppercase tracking-[0.18em] text-accent"
      title="Added by you · operator-provided task"
    >
      <span aria-hidden>↗</span>
      <span>you</span>
    </span>
  )
}

function QSection({
  label,
  tone,
  count,
  cards,
  compact = false,
  nextCardId,
  onOpenCard,
}: {
  label: string
  tone: 'accent' | 'warn' | 'ok' | 'info'
  count: string
  cards: KanbanCardView[]
  compact?: boolean
  nextCardId?: string
  onOpenCard: (id: string) => void
}) {
  const dot =
    tone === 'accent'
      ? 'bg-accent shadow-[0_0_5px_rgba(168,212,252,0.6)]'
      : tone === 'warn'
      ? 'bg-warn'
      : tone === 'info'
      ? 'bg-fg-1'
      : 'bg-ok'
  return (
    <div className="pb-5 border-b border-white/[0.06] last:border-b-0 pt-3">
      <div className="flex items-baseline justify-between mb-2 text-fg-3 text-[10px] uppercase tracking-[0.14em]">
        <span className="inline-flex items-center gap-1.5 text-fg-1">
          <span className={`h-1 w-1 rounded-full ${dot}`} />
          {label}
        </span>
        <span className="font-mono tracking-normal normal-case tabular-nums">{count}</span>
      </div>
      <div className="flex flex-col gap-1.5">
        {cards.length === 0 ? (
          <div className="text-fg-3 italic text-[11.5px] px-2">none</div>
        ) : (
          cards.map((c) => (
            <BoardCard key={c.id} card={c} tone={tone} compact={compact} isNext={c.id === nextCardId} onClick={() => onOpenCard(c.id)} />
          ))
        )}
      </div>
    </div>
  )
}

function BoardCard({
  card,
  tone,
  compact,
  isNext,
  onClick,
}: {
  card: KanbanCardView
  tone: 'accent' | 'warn' | 'ok' | 'info'
  compact: boolean
  isNext: boolean
  onClick: () => void
}) {
  const isOperatorCard = card.createdBy === 'operator'
  if (compact) {
    return (
      <div
        onClick={onClick}
        role="button"
        tabIndex={0}
        className="px-2 py-1 cursor-pointer rounded text-fg-2 text-[11.5px] leading-[1.45] line-through decoration-fg-4 hover:bg-white/[0.02] hover:text-fg-1 flex items-baseline gap-1.5"
      >
        {isOperatorCard ? <span className="not-italic text-accent text-[9px] leading-none">↗</span> : null}
        <span>{card.title}</span>
      </div>
    )
  }
  return (
    <div
      onClick={onClick}
      role="button"
      tabIndex={0}
      className={`px-2.5 py-2 rounded-md cursor-pointer flex flex-col gap-1 transition border animate-fade-in ${
        isNext
          ? 'border-accent/[0.22] bg-accent/[0.04]'
          : tone === 'warn'
          ? 'border-warn/[0.18] bg-warn/[0.04]'
          : 'border-white/[0.06] bg-white/[0.022] hover:bg-white/[0.04]'
      }`}
    >
      <div className="text-fg-0 text-[12px] leading-[1.45]">{card.title}</div>
      <div className="text-fg-3 text-[10px] tracking-[0.06em] flex gap-2 items-center">
        {isOperatorCard ? <OperatorChip /> : null}
        {isNext ? (
          <span className="rounded px-1.5 py-px bg-accent/[0.08] border border-accent/[0.22] text-accent text-[9.5px] uppercase tracking-[0.18em]">
            next
          </span>
        ) : null}
        {card.assignee ? <span className="text-fg-2">{card.assignee}</span> : null}
        {card.priority != null ? <span>p{card.priority}</span> : null}
      </div>
    </div>
  )
}

// ============ FEED COLUMN ============

function FeedColumn({
  events,
  nextCard,
  stalest,
  onOpenDispatcherBrief,
}: {
  events: TelemetryEventView[]
  nextCard: KanbanCardView | undefined
  stalest: Station | undefined
  onOpenDispatcherBrief: () => void
}) {
  return (
    <aside className="border-l border-white/[0.06] bg-black/[0.18] overflow-y-auto px-5 pb-32">
      <ColEyebrow label="Dispatch" count={`${events.length} today`} />
      <div className="flex flex-col">
        {events.length === 0 ? (
          <div className="text-fg-3 italic text-[11.5px] py-2">no dispatches yet</div>
        ) : (
          events.slice(-30).reverse().map((e, i) => <FeedRow key={`${e.at}-${i}`} ev={e} />)
        )}
      </div>

      {nextCard ? (
        <div className="mt-4 rounded-[10px] border border-accent/[0.18] bg-accent/[0.04] px-3.5 py-3 text-fg-1 text-[12px] leading-[1.55]">
          <div className="text-accent text-[10px] uppercase tracking-[0.14em] mb-1.5 inline-flex items-center gap-1.5">
            <span className="h-1 w-1 rounded-full bg-accent animate-pulse-soft" />
            Autopilot
          </div>
          Will dispatch <span className="text-fg-0">{nextCard.title}</span> when an agent is free.
          {stalest ? (
            <div className="mt-1.5 font-mono text-[11.5px] italic text-fg-2">
              {stalest.agent.agentType || 'agent'} is currently stale; will fall back to the next eligible agent.
            </div>
          ) : null}
          <button
            type="button"
            onClick={onOpenDispatcherBrief}
            className="block mt-2 text-accent text-[10.5px] uppercase tracking-[0.18em] hover:underline"
          >
            Open Autopilot Rules →
          </button>
        </div>
      ) : null}
    </aside>
  )
}

function FeedRow({ ev }: { ev: TelemetryEventView }) {
  const kind = ev.subtype ?? ''
  const actor = ((ev.details?.actor ?? ev.details?.agent) as string | undefined) ?? undefined
  const isAuto = kind.startsWith('kanban_dispatch') || kind.includes('autopilot')
  const isDone = kind.endsWith('_done') || kind.endsWith('_completed')
  const isWarn = kind.includes('stale') || kind.includes('failed') || kind.includes('block')
  return (
    <div className="grid grid-cols-[42px_1fr] gap-2.5 py-1 text-fg-1 text-[11.5px] leading-[1.55] border-t border-transparent first:border-t-0 [&:not(:first-child)]:border-t-white/[0.03]">
      <span className="tabular-nums text-fg-3">{tsStr(ev.at)}</span>
      <span>
        {isAuto ? (
          <span className="text-fg-2 italic font-sans font-light text-[12px]">autopilot</span>
        ) : actor ? (
          <span className="text-fg-0 font-medium">{actor}</span>
        ) : (
          <span className="text-fg-2">system</span>
        )}{' '}
        {isDone ? <span className="text-ok">✓</span> : null}{' '}
        <span className={isWarn ? 'text-warn' : 'text-fg-1'}>{(ev.message ?? kind.replace(/_/g, ' '))}</span>
      </span>
    </div>
  )
}

// ============ DRAWER BODY ============

function CardDrawerBody({ card, allCards }: { card: KanbanCardView; allCards: KanbanCardView[] }) {
  const events = (card.events ?? []).slice(-12)
  const blocks = (card.children ?? [])
    .map((id) => allCards.find((c) => c.id === id))
    .filter((c): c is KanbanCardView => !!c)
  const deps = (card.parents ?? [])
    .map((id) => allCards.find((c) => c.id === id))
    .filter((c): c is KanbanCardView => !!c)
  const isOperatorCard = card.createdBy === 'operator'
  return (
    <>
      {isOperatorCard ? (
        <div className="mb-2 inline-flex items-center gap-1.5 rounded-md border border-accent/[0.30] bg-accent/[0.08] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-accent">
          <span aria-hidden>↗</span>
          <span>added by you</span>
          <span className="text-accent/60">· operator-provided</span>
        </div>
      ) : null}
      <DrawerAssignment
        agent={card.assignee ?? 'unassigned'}
        age={card.startedAt ? `${Math.round((Date.now() - card.startedAt) / 60000)}m` : undefined}
        current={card.summary}
      />
      {card.body ? (
        <DrawerSection label="brief">
          <p className="m-0 whitespace-pre-wrap font-mono text-[12.5px] leading-[1.7] text-fg-1">{card.body}</p>
        </DrawerSection>
      ) : null}
      {(deps.length > 0 || blocks.length > 0) ? (
        <DrawerSection label="dependencies">
          <DrawerDependencies
            dependsOn={deps.map((d) => ({
              text: d.title,
              status: (d.status === 'done' ? 'done' : 'queued') as 'done' | 'queued' | 'blocked',
              meta: d.assignee,
            }))}
            blocks={blocks.map((b) => ({
              text: b.title,
              status: 'queued' as const,
            }))}
          />
        </DrawerSection>
      ) : null}
      {events.length > 0 ? (
        <DrawerSection label={`activity · ${events.length} events`}>
          <DrawerTimeline
            events={events.map((e) => ({
              ts: tsStr(e.timestamp),
              who: e.actor ?? 'system',
              body: e.message ?? e.kind ?? '',
              kind: e.actor ? 'agent' : (e.kind ?? '').includes('autopilot') ? 'auto' : 'system',
            }))}
          />
        </DrawerSection>
      ) : null}
    </>
  )
}

// ============ HELPERS ============

interface Buckets {
  ready: KanbanCardView[]
  flight: KanbanCardView[]
  review: KanbanCardView[]
  blocked: KanbanCardView[]
  done: KanbanCardView[]
}

function bucketCards(cards: KanbanCardView[]): Buckets {
  const buckets: Buckets = { ready: [], flight: [], review: [], blocked: [], done: [] }
  for (const c of cards) {
    const s = (c.status ?? '').toLowerCase()
    if (s === 'done' || s === 'complete' || s === 'completed' || s === 'sealed') buckets.done.push(c)
    else if (s === 'blocked') buckets.blocked.push(c)
    else if (s === 'review') buckets.review.push(c)
    // `done_unverified` is the legacy verifier-lane status; the new
    // default-on judge-review pipeline writes `review` instead but
    // we group it with review here so old boards still bucket sanely.
    else if (s === 'done_unverified') buckets.review.push(c)
    else if (s === 'running' || s === 'in_progress' || s === 'in-progress')
      buckets.flight.push(c)
    else buckets.ready.push(c)
  }
  return buckets
}

function agentStations(agents: AgentView[], cards: KanbanCardView[]): Station[] {
  return agents.map((agent) => {
    // Find the card this agent is on, if any. KanbanCardView lists agents[].
    const card = cards.find((c) => c.agents?.some((a) => a.session.id === agent.session.id))
    const ageMs = card?.startedAt ? Date.now() - card.startedAt : 0
    // Stale heuristic: agent has a card and elapsedMs since last tool > threshold,
    // or the card age exceeds the threshold without recent activity.
    const lastEventMs = card?.events && card.events.length > 0
      ? Math.max(...card.events.map((e) => e.timestamp ?? 0))
      : undefined
    const stale = !!(card && (lastEventMs ? Date.now() - lastEventMs > STALE_THRESHOLD_MS : ageMs > STALE_THRESHOLD_MS))
    return { agent, card, cardId: card?.id, stale, ageMs, lastEventMs }
  })
}

function dispatchEvents(events: TelemetryEventView[]): TelemetryEventView[] {
  return events.filter((e) => {
    const k = e.subtype ?? ''
    return (
      k.startsWith('kanban_') ||
      k.includes('autopilot') ||
      k === 'agent_claim' ||
      k === 'agent_complete' ||
      k === 'agent_reclaim'
    )
  })
}

function cardStatusLabel(card: KanbanCardView): string {
  const s = (card.status ?? '').toLowerCase()
  if (s === 'done' || s === 'sealed') return 'done'
  if (s === 'blocked') return 'blocked'
  if (s === 'review' || s === 'done_unverified') {
    const iter = card.reviewIteration
    const cap = 5
    return iter ? `in review · iter ${iter}/${cap}` : 'in review'
  }
  if (s === 'running' || s === 'in_progress') {
    const age = card.startedAt ? `${Math.round((Date.now() - card.startedAt) / 60000)}m` : ''
    return `in flight · ${age}`.trim()
  }
  return 'ready'
}

function minutesAgo(ms: number): string {
  return `${Math.max(1, Math.round(ms / 60000))}m`
}

function tsStr(ts: number | undefined): string {
  if (!ts) return ''
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function ColEyebrow({ label, count }: { label: string; count?: string }) {
  return (
    <div className="sticky top-0 z-[2] flex items-baseline justify-between px-1 pt-4 pb-2.5 bg-gradient-to-b from-bg-0 via-bg-0/70 to-transparent text-fg-3 text-[10.5px] uppercase tracking-[0.14em]">
      <span>{label}</span>
      {count ? <span className="text-fg-3 font-mono tabular-nums normal-case tracking-normal">{count}</span> : null}
    </div>
  )
}
