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

interface Props {
  objective: string
  cards: KanbanCardView[]
  agents: AgentView[]
  telemetryEvents: TelemetryEventView[]
  contextPct: number
  cost: number
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onOpenDispatcherBrief: () => void
}

const STALE_THRESHOLD_MS = 12 * 60 * 1000

export function KanbanBridgeView({
  objective,
  cards,
  agents,
  telemetryEvents,
  contextPct,
  cost,
  onAttach,
  onOpenDispatcherBrief,
}: Props) {
  const [openCardId, setOpenCardId] = useState<string | null>(null)

  const buckets = useMemo(() => bucketCards(cards), [cards])
  const stations = useMemo(() => agentStations(agents, cards), [agents, cards])
  const stalest = useMemo(() => stations.find((s) => s.stale), [stations])
  const nextCard = useMemo(() => buckets.ready[0], [buckets])
  const dispatchFeed = useMemo(() => dispatchEvents(telemetryEvents), [telemetryEvents])

  const openCard = openCardId ? cards.find((c) => c.id === openCardId) ?? null : null
  const drawerOpen = openCard != null

  // Three columns plus an optional drawer column on the right. Compresses
  // the existing columns when the drawer is open.
  const gridCols = drawerOpen
    ? 'grid-cols-[minmax(0,1fr)_320px_320px_480px]'
    : 'grid-cols-[minmax(0,1fr)_320px_320px]'

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <Header
        objective={objective}
        buckets={buckets}
        stalest={stalest}
        agentsCount={agents.length}
        contextPct={contextPct}
        cost={cost}
        onOpenDispatcherBrief={onOpenDispatcherBrief}
      />

      <div className={`grid min-h-0 flex-1 overflow-hidden ${gridCols}`}>
        <CrewColumn
          stations={stations}
          nextCard={nextCard}
          stalest={stalest}
          onOpenStation={(s) => setOpenCardId(s.cardId ?? null)}
        />
        <BoardColumn buckets={buckets} nextCardId={nextCard?.id} onOpenCard={setOpenCardId} />
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
  onOpenDispatcherBrief,
}: {
  objective: string
  buckets: Buckets
  stalest: Station | undefined
  agentsCount: number
  contextPct: number
  cost: number
  onOpenDispatcherBrief: () => void
}) {
  const total = buckets.done.length + buckets.ready.length + buckets.flight.length + buckets.blocked.length
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
      <div className="flex gap-2 shrink-0">
        <button
          type="button"
          onClick={onOpenDispatcherBrief}
          className="rounded-md border border-accent/[0.22] bg-accent/[0.06] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.12]"
        >
          Autopilot Rules
        </button>
      </div>
    </header>
  )
}

// ============ CREW COLUMN ============

function CrewColumn({
  stations,
  nextCard,
  stalest,
  onOpenStation,
}: {
  stations: Station[]
  nextCard: KanbanCardView | undefined
  stalest: Station | undefined
  onOpenStation: (s: Station) => void
}) {
  return (
    <aside className="overflow-y-auto px-5 pb-32">
      <ColEyebrow label="Crew" count={`${stations.length}`} />
      <div className="flex flex-col gap-3">
        {stations.length === 0 ? (
          <div className="text-fg-3 italic text-[12px] px-1 py-4">no agents online</div>
        ) : (
          stations.map((s) => <StationCard key={s.agent.session.id} station={s} onClick={() => onOpenStation(s)} />)
        )}
      </div>
      {nextCard ? (
        <div className="mt-4 rounded-[10px] border border-dashed border-accent/[0.22] bg-accent/[0.03] px-3.5 py-3 text-fg-1 text-[12px] leading-[1.55]">
          <div className="text-accent text-[9.5px] uppercase tracking-[0.14em] mb-1.5 inline-flex items-center gap-1.5">
            <span className="h-1 w-1 rounded-full bg-accent animate-pulse-soft" />
            Next Dispatch
          </div>
          {stalest ? (
            <>
              Reclaim <span className="text-fg-0">{stalest.agent.agentType || 'stalest agent'}</span>, then dispatch{' '}
              <span className="text-fg-0">{nextCard.title}</span>.
            </>
          ) : (
            <>
              Dispatch <span className="text-fg-0">{nextCard.title}</span> when an agent comes free.
            </>
          )}
        </div>
      ) : null}
    </aside>
  )
}

interface Station {
  agent: AgentView
  card?: KanbanCardView
  cardId?: string
  stale: boolean
  ageMs: number
  lastEventMs?: number
}

function StationCard({ station, onClick }: { station: Station; onClick: () => void }) {
  const { agent, card, stale } = station
  const idle = !card
  return (
    <div
      onClick={onClick}
      role="button"
      tabIndex={0}
      className={`grid grid-cols-[auto_1fr] gap-3.5 px-3.5 py-3 rounded-[12px] border transition cursor-pointer ${
        stale
          ? 'border-danger/[0.18] bg-danger/[0.025] hover:border-danger/[0.32]'
          : idle
          ? 'border-white/[0.06] bg-white/[0.008] hover:border-white/[0.12]'
          : 'border-white/[0.06] bg-white/[0.018] hover:border-white/[0.14]'
      }`}
    >
      <div className="flex flex-col gap-1 min-w-[96px]">
        <span className="inline-flex items-center gap-1.5 font-mono text-[16px] text-fg-0 tracking-[-0.005em]">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              agent.status === 'running'
                ? 'bg-accent shadow-[0_0_6px_rgba(168,212,252,0.6)] animate-pulse-soft'
                : stale
                ? 'bg-danger'
                : 'bg-fg-3'
            }`}
          />
          {agent.agentType || agent.session.title || agent.session.id.slice(0, 8)}
        </span>
        <span className="text-fg-3 text-[9.5px] uppercase tracking-[0.18em]">{agent.status}</span>
        {!idle ? (
          <span className="text-fg-3 text-[11px] mt-1.5 tabular-nums">
            on this for <span className={stale ? 'text-danger' : 'text-fg-0'}>{minutesAgo(station.ageMs)}</span>
          </span>
        ) : null}
      </div>
      <div className="flex flex-col gap-1 min-w-0">
        <div className="text-fg-0 text-[13px] leading-[1.45]">
          {idle ? (
            <span className="italic text-fg-2">idle — waiting for dispatch</span>
          ) : (
            card?.title
          )}
        </div>
        {!idle ? (
          <div className="text-fg-2 text-[11px] flex flex-wrap gap-2.5">
            {stale ? (
              <span className="rounded px-1.5 py-px text-danger bg-danger/[0.08] border border-danger/[0.22] text-[9.5px] uppercase tracking-[0.18em]">
                stale
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 text-accent">
                <span className="h-1 w-1 rounded-full bg-accent animate-pulse-soft" />
                live
              </span>
            )}
            <span className="text-fg-3">·</span>
            <span>{agent.tools.length} tools</span>
          </div>
        ) : null}
      </div>
    </div>
  )
}

// ============ BOARD COLUMN ============

function BoardColumn({
  buckets,
  nextCardId,
  onOpenCard,
}: {
  buckets: Buckets
  nextCardId: string | undefined
  onOpenCard: (id: string) => void
}) {
  return (
    <aside className="border-l border-white/[0.06] bg-black/[0.10] overflow-y-auto px-4 pb-32">
      <ColEyebrow label="Board"
        count={`${buckets.ready.length + buckets.blocked.length + buckets.flight.length + buckets.done.length} cards`}
      />
      <QSection
        label="ready"
        tone="accent"
        count={`${buckets.ready.length}`}
        cards={buckets.ready}
        nextCardId={nextCardId}
        onOpenCard={onOpenCard}
      />
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
  tone: 'accent' | 'warn' | 'ok'
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
  tone: 'accent' | 'warn' | 'ok'
  compact: boolean
  isNext: boolean
  onClick: () => void
}) {
  if (compact) {
    return (
      <div
        onClick={onClick}
        role="button"
        tabIndex={0}
        className="px-2 py-1 cursor-pointer rounded text-fg-2 text-[11.5px] leading-[1.45] line-through decoration-fg-4 hover:bg-white/[0.02] hover:text-fg-1"
      >
        {card.title}
      </div>
    )
  }
  return (
    <div
      onClick={onClick}
      role="button"
      tabIndex={0}
      className={`px-2.5 py-2 rounded-md cursor-pointer flex flex-col gap-1 transition border ${
        isNext
          ? 'border-accent/[0.22] bg-accent/[0.04]'
          : tone === 'warn'
          ? 'border-warn/[0.18] bg-warn/[0.04]'
          : 'border-white/[0.06] bg-white/[0.022] hover:bg-white/[0.04]'
      }`}
    >
      <div className="text-fg-0 text-[12px] leading-[1.45]">{card.title}</div>
      <div className="text-fg-3 text-[10px] tracking-[0.06em] flex gap-2 items-center">
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
  return (
    <>
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
  blocked: KanbanCardView[]
  done: KanbanCardView[]
}

function bucketCards(cards: KanbanCardView[]): Buckets {
  const buckets: Buckets = { ready: [], flight: [], blocked: [], done: [] }
  for (const c of cards) {
    const s = (c.status ?? '').toLowerCase()
    if (s === 'done' || s === 'complete' || s === 'completed' || s === 'sealed') buckets.done.push(c)
    else if (s === 'blocked') buckets.blocked.push(c)
    else if (s === 'running' || s === 'in_progress' || s === 'in-progress' || s === 'done_unverified')
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
