import React, { useEffect, useMemo, useRef, useState } from 'react'
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
  autoDispatchEnabled: boolean
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onOpenDispatcherBrief: () => void
}

const STALE_THRESHOLD_MS = 12 * 60 * 1000
const KANBAN_MAX_REVIEW_ITERATIONS = 5

// ─────────────────────────────────────────────────────────────────────────────
// Top-level view
// ─────────────────────────────────────────────────────────────────────────────

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
  const [addOpen, setAddOpen] = useState(false)
  const [addColumn, setAddColumn] = useState<ColumnKey>('ready')

  const buckets = useMemo(() => bucketCards(cards), [cards])
  const stations = useMemo(() => agentStations(agents, cards), [agents, cards])
  const stalest = useMemo(() => stations.find((s) => s.stale), [stations])
  const dispatchFeed = useMemo(
    () => dispatchEvents(telemetryEvents),
    [telemetryEvents],
  )
  // Verdict events indexed by cardId so each TicketCard's
  // JudgeIterationMeter can mount its own per-card history without
  // re-walking the global event stream per card.
  const verdictsByCard = useMemo(() => {
    const m = new Map<string, JudgeHistoryEntry[]>()
    for (const ev of telemetryEvents) {
      if (ev.subtype !== 'kanban_judge_verdict') continue
      const details = (ev.details ?? {}) as Record<string, unknown>
      const cardId = typeof details.cardId === 'string' ? details.cardId : ''
      if (!cardId) continue
      const verdict = (details.verdict ?? {}) as Record<string, unknown>
      const entry: JudgeHistoryEntry = {
        iteration: Number(details.reviewIteration ?? 0),
        done: verdict.done === true,
        confidence:
          typeof verdict.confidence === 'number' ? verdict.confidence : 0,
        reason: typeof verdict.reason === 'string' ? verdict.reason : '',
        judgeSessionId:
          typeof details.judgeSessionId === 'string'
            ? details.judgeSessionId
            : undefined,
        at: ev.at,
      }
      const arr = m.get(cardId) ?? []
      arr.push(entry)
      m.set(cardId, arr)
    }
    // Sort each card's entries by iteration. Dedupe (keep newest by
    // `at`) in case a verdict double-fires.
    for (const [cardId, arr] of m.entries()) {
      const byIter = new Map<number, JudgeHistoryEntry>()
      for (const entry of arr) {
        const existing = byIter.get(entry.iteration)
        if (!existing || existing.at < entry.at) byIter.set(entry.iteration, entry)
      }
      m.set(
        cardId,
        Array.from(byIter.values()).sort((a, b) => a.iteration - b.iteration),
      )
    }
    return m
  }, [telemetryEvents])

  const openCard = openCardId ? cards.find((c) => c.id === openCardId) ?? null : null
  const drawerOpen = openCard != null
  const cardJudgeHistory = openCard ? verdictsByCard.get(openCard.id) ?? [] : []

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-bg-0">
      <SessionHeader
        sessionId={sessionId}
        objective={objective}
        cardsTotal={cards.length}
        buckets={buckets}
        agents={agents}
        contextPct={contextPct}
        cost={cost}
        autoDispatchEnabled={autoDispatchEnabled}
        onToggleAutopilot={() => {
          void setKanbanAutopilot(sessionId, !autoDispatchEnabled)
        }}
        onOpenDispatcherBrief={onOpenDispatcherBrief}
      />

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_320px] overflow-hidden">
        <TaskBoard
          sessionId={sessionId}
          cards={cards}
          buckets={buckets}
          stations={stations}
          verdictsByCard={verdictsByCard}
          addOpen={addOpen}
          addColumn={addColumn}
          onOpenAddForm={(col) => {
            setAddColumn(col)
            setAddOpen(true)
          }}
          onCloseAddForm={() => setAddOpen(false)}
          onOpenCard={setOpenCardId}
          onOpenAgent={(id) => onAttach(id, 'split')}
        />
        <RightRail
          dispatchEvents={dispatchFeed}
          stations={stations}
          stalest={stalest}
          onOpenAgent={(id) => onAttach(id, 'split')}
          onOpenDispatcherBrief={onOpenDispatcherBrief}
        />
      </div>

      <BottomStrip
        cards={cards}
        buckets={buckets}
        telemetryEvents={telemetryEvents}
        contextPct={contextPct}
      />

      <DetailDrawer
        open={drawerOpen}
        onClose={() => setOpenCardId(null)}
        title={openCard?.title ?? ''}
        statusLabel={openCard ? cardStatusLabel(openCard) : undefined}
        backdrop={<ShinyFabricBackdrop active={drawerOpen} intensity={0.85} />}
        footer={
          openCard ? (
            <>
              {openCard.workerSessionId ? (
                <>
                  <DrawerAction
                    onClick={() =>
                      onAttach(openCard.workerSessionId!, 'split')
                    }
                  >
                    Worker · split
                  </DrawerAction>
                  <DrawerAction
                    onClick={() =>
                      onAttach(openCard.workerSessionId!, 'replace')
                    }
                  >
                    Worker · here
                  </DrawerAction>
                </>
              ) : null}
              {openCard.judgeSessionId ? (
                <DrawerAction
                  onClick={() => onAttach(openCard.judgeSessionId!, 'split')}
                >
                  Judge · split
                </DrawerAction>
              ) : null}
              {!openCard.workerSessionId && !openCard.judgeSessionId ? (
                <DrawerAction
                  onClick={() => onAttach(openCard.id, 'split')}
                >
                  Open
                </DrawerAction>
              ) : null}
            </>
          ) : null
        }
      >
        {openCard ? (
          <CardDrawerBody
            card={openCard}
            allCards={cards}
            judgeHistory={cardJudgeHistory}
            onOpenAgent={(id) => onAttach(id, 'split')}
          />
        ) : null}
      </DetailDrawer>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Header
// ─────────────────────────────────────────────────────────────────────────────

function SessionHeader({
  sessionId,
  objective,
  cardsTotal,
  buckets,
  agents,
  contextPct,
  cost,
  autoDispatchEnabled,
  onToggleAutopilot,
  onOpenDispatcherBrief,
}: {
  sessionId: string
  objective: string
  cardsTotal: number
  buckets: Buckets
  agents: AgentView[]
  contextPct: number
  cost: number
  autoDispatchEnabled: boolean
  onToggleAutopilot: () => void
  onOpenDispatcherBrief: () => void
}) {
  const onlineCount = agents.filter((a) => a.status === 'running').length
  return (
    <header className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-8 border-b border-white/[0.06] bg-bg-0 px-8 pb-5 pt-6">
      <div className="min-w-0">
        <div className="mb-2 flex items-center gap-3 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-3">
          <span>session</span>
          <span className="rounded border border-white/[0.06] bg-white/[0.02] px-1.5 py-px text-fg-2 normal-case tracking-normal">
            {sessionId.slice(0, 12)}
          </span>
          <span className="inline-flex items-center gap-1.5 text-ok">
            <span className="block h-1.5 w-1.5 rounded-full bg-ok animate-pulse-soft" />
            live
          </span>
        </div>
        <h1 className="m-0 max-w-[760px] truncate font-serif text-[22px] font-light leading-[1.3] tracking-[-0.01em] text-fg-0">
          {objective || (
            <span className="italic text-fg-3">no objective set</span>
          )}
        </h1>
      </div>
      <div className="flex items-end gap-2">
        <StatTile label="agents" value={`${onlineCount}/${agents.length}`} />
        <StatTile label="tasks" value={`${cardsTotal}`} />
        <StatTile label="ready" value={`${buckets.ready.length}`} accent="accent" />
        <StatTile
          label="in flight"
          value={`${buckets.flight.length}`}
          accent="info"
        />
        <StatTile label="review" value={`${buckets.review.length}`} accent="info" />
        <StatTile
          label="blocked"
          value={`${buckets.blocked.length}`}
          accent={buckets.blocked.length > 0 ? 'warn' : undefined}
        />
        <StatTile label="done" value={`${buckets.done.length}`} accent="ok" />
        <StatTile
          label="ctx"
          value={`${Math.round(contextPct)}%`}
          accent={contextPct > 80 ? 'warn' : undefined}
        />
        <StatTile label="cost" value={`$${cost.toFixed(2)}`} />
        <div className="ml-3 flex items-center gap-2">
          <AutopilotToggle
            enabled={autoDispatchEnabled}
            onClick={onToggleAutopilot}
          />
          <button
            type="button"
            onClick={onOpenDispatcherBrief}
            title="Open autopilot rules / dispatcher brief"
            className="rounded-md border border-white/[0.08] bg-white/[0.02] px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-2 transition hover:border-white/[0.18] hover:bg-white/[0.05] hover:text-fg-0"
          >
            brief ↗
          </button>
        </div>
      </div>
    </header>
  )
}

function StatTile({
  label,
  value,
  accent,
}: {
  label: string
  value: string
  accent?: 'accent' | 'info' | 'ok' | 'warn'
}) {
  const valueCls =
    accent === 'accent'
      ? 'text-accent'
      : accent === 'ok'
        ? 'text-ok'
        : accent === 'warn'
          ? 'text-warn'
          : accent === 'info'
            ? 'text-fg-0'
            : 'text-fg-0'
  return (
    <div className="flex min-w-[58px] flex-col items-start rounded-md border border-white/[0.05] bg-white/[0.015] px-2.5 py-1.5">
      <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-fg-3">
        {label}
      </span>
      <span
        className={`mt-0.5 font-mono text-[15px] tabular-nums leading-none ${valueCls}`}
      >
        {value}
      </span>
    </div>
  )
}

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
          ? 'Autopilot is ON — agent-created and follow-up cards auto-dispatch.'
          : 'Autopilot is OFF — agent-created cards sit in READY.'
      }
      className={`group inline-flex items-center gap-2 rounded-md border px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] transition ${
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

// ─────────────────────────────────────────────────────────────────────────────
// Task board — 5 columns side-by-side
// ─────────────────────────────────────────────────────────────────────────────

type ColumnKey = 'ready' | 'flight' | 'review' | 'blocked' | 'done'

const COLUMNS: Array<{
  key: ColumnKey
  label: string
  tone: 'accent' | 'info' | 'warn' | 'ok' | 'neutral'
}> = [
  { key: 'ready', label: 'ready', tone: 'accent' },
  { key: 'flight', label: 'in progress', tone: 'info' },
  { key: 'review', label: 'review', tone: 'info' },
  { key: 'blocked', label: 'blocked', tone: 'warn' },
  { key: 'done', label: 'done', tone: 'ok' },
]

function TaskBoard({
  sessionId,
  cards,
  buckets,
  stations,
  verdictsByCard,
  addOpen,
  addColumn,
  onOpenAddForm,
  onCloseAddForm,
  onOpenCard,
  onOpenAgent,
}: {
  sessionId: string
  cards: KanbanCardView[]
  buckets: Buckets
  stations: Station[]
  verdictsByCard: Map<string, JudgeHistoryEntry[]>
  addOpen: boolean
  addColumn: ColumnKey
  onOpenAddForm: (col: ColumnKey) => void
  onCloseAddForm: () => void
  onOpenCard: (id: string) => void
  onOpenAgent: (sessionId: string) => void
}) {
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

  return (
    <section className="min-w-0 overflow-hidden">
      {/* Add-task form, slides down from the top of the board when open */}
      {addOpen ? (
        <div className="border-b border-white/[0.06] bg-bg-1 px-6 py-3 animate-fade-in">
          <AddTaskForm
            sessionId={sessionId}
            open={addOpen}
            cards={cards}
            onClose={onCloseAddForm}
          />
        </div>
      ) : null}
      <div className="grid h-full min-w-0 grid-cols-5 gap-0 overflow-hidden">
        {COLUMNS.map((col) => (
          <BoardColumnSection
            key={col.key}
            columnKey={col.key}
            label={col.label}
            tone={col.tone}
            cards={buckets[col.key]}
            stationsByCard={stationsByCard}
            verdictsByCard={verdictsByCard}
            isAddingHere={addOpen && addColumn === col.key}
            onOpenAddForm={() => onOpenAddForm(col.key)}
            onOpenCard={onOpenCard}
            onOpenAgent={onOpenAgent}
          />
        ))}
      </div>
    </section>
  )
}

function BoardColumnSection({
  columnKey,
  label,
  tone,
  cards,
  stationsByCard,
  verdictsByCard,
  isAddingHere,
  onOpenAddForm,
  onOpenCard,
  onOpenAgent,
}: {
  columnKey: ColumnKey
  label: string
  tone: 'accent' | 'info' | 'warn' | 'ok' | 'neutral'
  cards: KanbanCardView[]
  stationsByCard: Map<string, Station[]>
  verdictsByCard: Map<string, JudgeHistoryEntry[]>
  isAddingHere: boolean
  onOpenAddForm: () => void
  onOpenCard: (id: string) => void
  onOpenAgent: (sessionId: string) => void
}) {
  const dot =
    tone === 'accent'
      ? 'bg-accent shadow-[0_0_5px_rgba(168,212,252,0.6)]'
      : tone === 'warn'
        ? 'bg-warn'
        : tone === 'ok'
          ? 'bg-ok'
          : tone === 'info'
            ? 'bg-fg-1'
            : 'bg-fg-3'
  return (
    <div
      className={`flex min-h-0 flex-col overflow-hidden border-r border-white/[0.05] last:border-r-0 ${
        isAddingHere ? 'bg-accent/[0.025]' : ''
      }`}
    >
      <header className="sticky top-0 z-[2] flex items-center justify-between gap-2 border-b border-white/[0.05] bg-bg-0/95 px-3 py-2 backdrop-blur">
        <div className="inline-flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-2">
          <span className={`h-1 w-1 rounded-full ${dot}`} />
          {label}
        </div>
        <div className="inline-flex items-center gap-2">
          <span className="font-mono text-[10.5px] tabular-nums text-fg-3">
            {cards.length}
          </span>
          {/* Ready is the only column where adding a task makes sense.
              The others are owned by agents / the judge / the dispatcher. */}
          {columnKey === 'ready' ? (
            <button
              type="button"
              onClick={onOpenAddForm}
              title="Add task to ready"
              className="rounded border border-white/[0.06] bg-white/[0.02] px-1.5 py-px font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3 transition hover:border-accent/[0.30] hover:bg-accent/[0.05] hover:text-accent"
            >
              + new
            </button>
          ) : null}
        </div>
      </header>
      <div className="flex-1 overflow-y-auto px-2 py-2">
        {cards.length === 0 ? (
          <EmptyColumnSlot label={label} />
        ) : (
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {cards.map((c) => (
              <TicketCard
                key={c.id}
                card={c}
                column={columnKey}
                stations={stationsByCard.get(c.id) ?? []}
                verdicts={verdictsByCard.get(c.id) ?? []}
                onOpen={() => onOpenCard(c.id)}
                onOpenAgent={onOpenAgent}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function EmptyColumnSlot({ label }: { label: string }) {
  return (
    <div className="mt-2 rounded-md border border-dashed border-white/[0.05] bg-white/[0.01] px-3 py-6 text-center font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
      no {label}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// TicketCard
// ─────────────────────────────────────────────────────────────────────────────

function TicketCard({
  card,
  column,
  stations,
  verdicts,
  onOpen,
  onOpenAgent,
}: {
  card: KanbanCardView
  column: ColumnKey
  stations: Station[]
  verdicts: JudgeHistoryEntry[]
  onOpen: () => void
  onOpenAgent: (sessionId: string) => void
}) {
  // Mount animation — every card "drops in" once. Tracks first render
  // so re-rendering on data updates doesn't replay the animation.
  const mounted = useRef(false)
  const [justMounted, setJustMounted] = useState(true)
  useEffect(() => {
    if (mounted.current) return
    mounted.current = true
    const t = setTimeout(() => setJustMounted(false), 420)
    return () => clearTimeout(t)
  }, [])

  // Completion glow — fires once when status transitions to done. We
  // detect "just landed in done" by comparing the latest update to
  // mount time; cards rehydrating into done from disk don't glow.
  const [completionFlash, setCompletionFlash] = useState(false)
  const prevStatus = useRef(card.status)
  useEffect(() => {
    if (
      prevStatus.current &&
      prevStatus.current !== card.status &&
      card.status === 'done'
    ) {
      setCompletionFlash(true)
      const t = setTimeout(() => setCompletionFlash(false), 1500)
      return () => clearTimeout(t)
    }
    prevStatus.current = card.status
    return undefined
  }, [card.status])

  const isOperatorCard = card.createdBy === 'operator'
  const inFlight = column === 'flight'
  const inReview = column === 'review'
  const isBlocked = column === 'blocked'
  const isDone = column === 'done'

  const progress = computeProgress(card, column)
  const eta = computeEta(card, column)

  const judgeSessionId = card.judgeSessionId
  const workerSessionId = card.workerSessionId

  const showProgressBar = inFlight && progress != null
  const showJudgeMeter =
    inReview || isBlocked || (verdicts.length > 0 && !isDone)

  const animClasses = [
    justMounted ? 'animate-kanban-drop-in' : '',
    completionFlash ? 'animate-kanban-complete-glow' : '',
    inFlight ? 'animate-kanban-active-pulse' : '',
  ]
    .filter(Boolean)
    .join(' ')

  const baseEdge = isBlocked
    ? 'border-warn/[0.28] bg-warn/[0.04]'
    : isDone
      ? 'border-white/[0.05] bg-white/[0.015]'
      : 'border-white/[0.07] bg-white/[0.022] hover:border-white/[0.14] hover:bg-white/[0.04]'

  return (
    <li>
      <article
        role="button"
        tabIndex={0}
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onOpen()
          }
        }}
        className={`group relative flex cursor-pointer flex-col gap-2 rounded-lg border px-3 py-2.5 transition ${baseEdge} ${animClasses}`}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <CardIdBadge id={card.id} />
            {isOperatorCard ? <OperatorChip /> : null}
            {card.priority != null && card.priority !== 2 ? (
              <PriorityChip priority={card.priority} />
            ) : null}
          </div>
          <StatusPip column={column} workerTerminalState={card.workerTerminalState} />
        </div>

        <h3 className="m-0 line-clamp-2 font-mono text-[12.5px] font-normal leading-[1.4] tracking-[-0.005em] text-fg-0">
          {card.title}
        </h3>

        {card.assignee || stations.length > 0 ? (
          <div className="-mb-0.5 flex items-center gap-2">
            {stations.length > 0 ? (
              <AgentBadge
                station={stations[0]}
                variant="inline"
                onOpenAgent={onOpenAgent}
              />
            ) : (
              <AgentTypeBadge type={card.assignee} />
            )}
            <span className="truncate font-mono text-[10.5px] text-fg-2">
              {card.assignee || stations[0]?.agent.agentType || 'unassigned'}
            </span>
          </div>
        ) : null}

        {showProgressBar ? (
          <ProgressBar progress={progress ?? 0} eta={eta} />
        ) : null}

        {showJudgeMeter ? (
          <JudgeIterationMeter
            card={card}
            verdicts={verdicts}
            inReview={inReview}
            isBlocked={isBlocked}
            judgeSessionId={judgeSessionId}
            onOpenJudge={
              judgeSessionId ? () => onOpenAgent(judgeSessionId) : undefined
            }
          />
        ) : null}

        <footer className="flex items-center justify-between gap-2 pt-0.5 font-mono text-[10px] text-fg-3">
          <CardMeta card={card} column={column} />
          <div className="flex items-center gap-1.5">
            {workerSessionId && !inFlight ? (
              <QuickLink
                label="worker"
                onClick={(e) => {
                  e.stopPropagation()
                  onOpenAgent(workerSessionId)
                }}
              />
            ) : null}
            {judgeSessionId && !inReview ? (
              <QuickLink
                label="judge"
                onClick={(e) => {
                  e.stopPropagation()
                  onOpenAgent(judgeSessionId)
                }}
              />
            ) : null}
            {(card.artifacts?.length ?? 0) > 0 ? (
              <span title={`${card.artifacts!.length} artifact(s)`}>
                ◇ {card.artifacts!.length}
              </span>
            ) : null}
          </div>
        </footer>
      </article>
    </li>
  )
}

function CardIdBadge({ id }: { id: string }) {
  // Strip the `card_` prefix for visual brevity. The id is mono-spaced
  // and tabular so a long board still grids nicely.
  const short = id.startsWith('card_') ? id.slice(5) : id
  return (
    <span className="rounded border border-white/[0.06] bg-white/[0.02] px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.12em] tabular-nums text-fg-3">
      {short}
    </span>
  )
}

function PriorityChip({ priority }: { priority: number }) {
  // Convention: priority 1 = high, 2 = normal (default, hidden), 3 = low.
  const isHigh = priority <= 1
  const isLow = priority >= 3
  if (!isHigh && !isLow) return null
  return (
    <span
      className={`rounded px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.14em] ${
        isHigh
          ? 'border border-warn/[0.30] bg-warn/[0.08] text-warn'
          : 'border border-white/[0.06] bg-white/[0.02] text-fg-3'
      }`}
      title={isHigh ? 'High priority' : 'Low priority'}
    >
      {isHigh ? 'p1' : 'p3'}
    </span>
  )
}

function OperatorChip() {
  return (
    <span
      className="rounded border border-accent/[0.30] bg-accent/[0.08] px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.16em] text-accent"
      title="Created by you (operator-added)"
    >
      you
    </span>
  )
}

function StatusPip({
  column,
  workerTerminalState,
}: {
  column: ColumnKey
  workerTerminalState?: string
}) {
  const meta = STATUS_PIP_META[column]
  // For review/blocked, surface what the worker exited as — clean
  // delivery vs partial crash — since that meaningfully changes how
  // an operator should read the card.
  const subtitle =
    (column === 'review' || column === 'blocked') && workerTerminalState
      ? workerTerminalState
      : null
  return (
    <span
      title={subtitle ? `${meta.label} · worker exited ${subtitle}` : meta.label}
      className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.14em] ${meta.cls}`}
    >
      <span className={`h-1 w-1 rounded-full ${meta.dotCls}`} />
      {subtitle ?? meta.label}
    </span>
  )
}

const STATUS_PIP_META: Record<
  ColumnKey,
  { label: string; cls: string; dotCls: string }
> = {
  ready: {
    label: 'ready',
    cls: 'text-accent bg-accent/[0.08] border border-accent/[0.22]',
    dotCls: 'bg-accent',
  },
  flight: {
    label: 'running',
    cls: 'text-fg-1 bg-white/[0.04] border border-white/[0.10]',
    dotCls: 'bg-accent animate-pulse-soft',
  },
  review: {
    label: 'in review',
    cls: 'text-fg-1 bg-white/[0.04] border border-white/[0.10]',
    dotCls: 'bg-fg-1 animate-pulse-soft',
  },
  blocked: {
    label: 'blocked',
    cls: 'text-warn bg-warn/[0.08] border border-warn/[0.30]',
    dotCls: 'bg-warn',
  },
  done: {
    label: 'done',
    cls: 'text-ok bg-ok/[0.06] border border-ok/[0.22]',
    dotCls: 'bg-ok',
  },
}

function AgentTypeBadge({ type }: { type?: string }) {
  const palette = agentPalette(type)
  const initials = agentInitials(type, undefined)
  return (
    <span
      className="inline-flex h-5 w-5 items-center justify-center rounded font-mono text-[9px] font-medium tabular-nums"
      style={{ background: palette.bg, color: palette.fg }}
    >
      {initials}
    </span>
  )
}

function ProgressBar({ progress, eta }: { progress: number; eta?: string }) {
  const pct = Math.max(0, Math.min(100, progress))
  return (
    <div className="flex flex-col gap-1">
      <div className="relative h-1.5 overflow-hidden rounded-full bg-white/[0.04]">
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-accent/70"
          style={{ width: `${pct}%`, transition: 'width 600ms cubic-bezier(0.16, 1, 0.3, 1)' }}
        />
        {/* Flowing highlight overlay so the active card visibly
            indicates "things are happening" even when the percentage
            isn't visibly changing. */}
        <div
          className="absolute inset-y-0 left-0 animate-kanban-progress-flow rounded-full opacity-50"
          style={{
            width: `${pct}%`,
            background:
              'linear-gradient(90deg, transparent 0%, rgba(168,212,252,0.45) 50%, transparent 100%)',
            backgroundSize: '200% 100%',
          }}
        />
      </div>
      <div className="flex items-center justify-between font-mono text-[10px] tabular-nums text-fg-3">
        <span>{pct.toFixed(0)}%</span>
        {eta ? <span className="text-fg-2">eta {eta}</span> : null}
      </div>
    </div>
  )
}

function CardMeta({ card, column }: { card: KanbanCardView; column: ColumnKey }) {
  if (column === 'done') {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="text-ok">✓</span>
        <span>{relTs(card.completedAt ?? card.updatedAt)}</span>
      </span>
    )
  }
  if (column === 'flight') {
    return (
      <span className="text-fg-3">
        started {relTs(card.startedAt ?? card.updatedAt)}
      </span>
    )
  }
  if (column === 'review' || column === 'blocked') {
    const iter = card.reviewIteration ?? 0
    return (
      <span>
        rework {iter}/{KANBAN_MAX_REVIEW_ITERATIONS} · {relTs(card.updatedAt)}
      </span>
    )
  }
  return <span>{relTs(card.updatedAt ?? card.createdAt)}</span>
}

function QuickLink({
  label,
  onClick,
}: {
  label: string
  onClick: (e: React.MouseEvent) => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded border border-white/[0.06] bg-white/[0.02] px-1.5 py-px text-[9.5px] uppercase tracking-[0.10em] text-fg-3 transition hover:border-accent/[0.30] hover:bg-accent/[0.05] hover:text-accent"
    >
      {label} ↗
    </button>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// JudgeIterationMeter — the novel piece
// ─────────────────────────────────────────────────────────────────────────────

interface JudgeHistoryEntry {
  iteration: number
  done: boolean
  confidence: number
  reason: string
  judgeSessionId?: string
  at: number
}

function JudgeIterationMeter({
  card,
  verdicts,
  inReview,
  isBlocked,
  judgeSessionId,
  onOpenJudge,
}: {
  card: KanbanCardView
  verdicts: JudgeHistoryEntry[]
  inReview: boolean
  isBlocked: boolean
  judgeSessionId?: string
  onOpenJudge?: () => void
}) {
  const max = KANBAN_MAX_REVIEW_ITERATIONS
  const iterations = verdicts.length
  const cur = card.reviewIteration ?? iterations
  // Build the segment array. Iteration N (1-indexed) corresponds to
  // slot N-1. Segments after `cur` are unused. The current slot is
  // "in-flight" if the card is in review and we don't yet have a
  // verdict for this iteration.
  const segments: Array<SegmentState> = []
  const verdictByIter = new Map<number, JudgeHistoryEntry>()
  for (const v of verdicts) verdictByIter.set(v.iteration, v)
  for (let i = 1; i <= max; i++) {
    const v = verdictByIter.get(i)
    if (v) {
      segments.push({
        state: v.done ? 'pass' : 'fail',
        confidence: v.confidence,
        reason: v.reason,
      })
    } else if (inReview && i === cur) {
      segments.push({ state: 'in_flight', confidence: 0, reason: '' })
    } else if (i <= cur) {
      // Card was at this iteration but verdict is missing (race or
      // event drop). Mark as inflight-like so the operator notices.
      segments.push({ state: 'in_flight', confidence: 0, reason: '' })
    } else {
      segments.push({ state: 'empty', confidence: 0, reason: '' })
    }
  }

  const lastVerdict = verdicts.length > 0 ? verdicts[verdicts.length - 1] : null
  const lastConfidence = lastVerdict?.confidence ?? null

  // Animation: when a new verdict lands (verdicts.length changes), the
  // newest segment flashes. Tracked by length so it fires once per
  // new verdict rather than on every render.
  const prevCount = useRef(verdicts.length)
  const [flashIter, setFlashIter] = useState<number | null>(null)
  useEffect(() => {
    if (verdicts.length > prevCount.current) {
      const newest = verdicts[verdicts.length - 1]
      if (newest) {
        setFlashIter(newest.iteration)
        const t = setTimeout(() => setFlashIter(null), 1000)
        prevCount.current = verdicts.length
        return () => clearTimeout(t)
      }
    }
    prevCount.current = verdicts.length
    return undefined
  }, [verdicts])

  return (
    <div
      className={`-mx-0.5 flex flex-col gap-1 rounded px-1 py-1 transition ${
        onOpenJudge
          ? 'cursor-pointer hover:bg-white/[0.025]'
          : ''
      }`}
      onClick={(e) => {
        if (onOpenJudge) {
          e.stopPropagation()
          onOpenJudge()
        }
      }}
      title={
        onOpenJudge
          ? 'Click to open the judge session'
          : 'Judge iteration meter'
      }
    >
      <div className="flex items-center gap-1.5">
        <span className="font-mono text-[9px] uppercase tracking-[0.16em] text-fg-3">
          judge
        </span>
        <div className="flex flex-1 gap-[3px]">
          {segments.map((seg, idx) => (
            <JudgeSegment
              key={idx}
              state={seg.state}
              confidence={seg.confidence}
              reason={seg.reason}
              flash={flashIter === idx + 1}
              iteration={idx + 1}
            />
          ))}
        </div>
        <span className="font-mono text-[9.5px] tabular-nums text-fg-2">
          {iterations}/{max}
        </span>
      </div>
      {/* Confidence sparkline — one dot per verdict, plotted on a tiny
          0..1 vertical axis. Only renders when we have ≥2 data points
          (a single point isn't a trend). The novel piece: confidence
          trajectory tells you whether the worker is converging or
          oscillating. */}
      {verdicts.length >= 2 ? (
        <ConfidenceTrace verdicts={verdicts} />
      ) : lastConfidence != null ? (
        <div className="flex items-center gap-2">
          <ConfidenceBar value={lastConfidence} />
          <span className="font-mono text-[9.5px] tabular-nums text-fg-3">
            conf {lastConfidence.toFixed(2)}
          </span>
          {isBlocked ? (
            <span className="ml-auto font-mono text-[9.5px] uppercase tracking-[0.14em] text-warn">
              cap reached
            </span>
          ) : null}
        </div>
      ) : isBlocked ? (
        <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-warn">
          cap reached
        </div>
      ) : null}
    </div>
  )
}

interface SegmentState {
  state: 'pass' | 'fail' | 'in_flight' | 'empty'
  confidence: number
  reason: string
}

function JudgeSegment({
  state,
  confidence,
  reason,
  flash,
  iteration,
}: SegmentState & { flash: boolean; iteration: number }) {
  const base =
    'h-[6px] flex-1 rounded-[2px] transition relative overflow-hidden'
  const stateCls =
    state === 'pass'
      ? 'bg-ok/80'
      : state === 'fail'
        ? 'bg-warn/80'
        : state === 'in_flight'
          ? 'bg-accent/50'
          : 'bg-white/[0.06]'
  const flashCls =
    flash && state === 'pass'
      ? 'animate-kanban-verdict-pass'
      : flash && state === 'fail'
        ? 'animate-kanban-verdict-fail'
        : ''
  const tooltip =
    state === 'pass'
      ? `iter ${iteration}: passed (conf ${confidence.toFixed(2)})${reason ? ` — ${truncate(reason, 200)}` : ''}`
      : state === 'fail'
        ? `iter ${iteration}: rejected (conf ${confidence.toFixed(2)})${reason ? ` — ${truncate(reason, 200)}` : ''}`
        : state === 'in_flight'
          ? `iter ${iteration}: judging now…`
          : `iter ${iteration}: not yet`
  return (
    <div className={`${base} ${stateCls} ${flashCls}`} title={tooltip}>
      {state === 'in_flight' ? (
        <div className="absolute inset-0 bg-accent/40 animate-kanban-judge-thinking" />
      ) : null}
    </div>
  )
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, value * 100))
  const tone =
    value >= 0.85 ? 'bg-ok/70' : value >= 0.5 ? 'bg-accent/70' : 'bg-warn/70'
  return (
    <div className="relative h-1 flex-1 overflow-hidden rounded-full bg-white/[0.04]">
      <div
        className={`absolute inset-y-0 left-0 rounded-full ${tone}`}
        style={{ width: `${pct}%`, transition: 'width 500ms ease-out' }}
      />
    </div>
  )
}

function ConfidenceTrace({ verdicts }: { verdicts: JudgeHistoryEntry[] }) {
  // SVG sparkline of confidence over iterations. X = iteration,
  // Y = confidence (inverted, since SVG y grows downward). Points
  // colored by verdict (pass = ok, fail = warn). Adds a subtle area
  // fill under the line for visual weight.
  const w = 100
  const h = 16
  const n = verdicts.length
  if (n < 2) return null
  const xs = verdicts.map((_, i) => (i / (n - 1)) * w)
  const ys = verdicts.map((v) => h - v.confidence * h)
  const path = xs.map((x, i) => `${i === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${ys[i].toFixed(2)}`).join(' ')
  const area =
    `M ${xs[0].toFixed(2)} ${h} ` +
    xs.map((x, i) => `L ${x.toFixed(2)} ${ys[i].toFixed(2)}`).join(' ') +
    ` L ${xs[n - 1].toFixed(2)} ${h} Z`
  const last = verdicts[n - 1]
  return (
    <div className="flex items-center gap-2">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        className="h-3 flex-1"
        aria-hidden
      >
        <defs>
          <linearGradient id="conf-area" x1="0" y1="0" x2="0" y2="1">
            <stop
              offset="0%"
              stopColor="rgba(168,212,252,0.20)"
            />
            <stop offset="100%" stopColor="rgba(168,212,252,0.0)" />
          </linearGradient>
        </defs>
        <path d={area} fill="url(#conf-area)" />
        <path
          d={path}
          fill="none"
          stroke="rgba(168,212,252,0.55)"
          strokeWidth="0.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
        {verdicts.map((v, i) => (
          <circle
            key={i}
            cx={xs[i]}
            cy={ys[i]}
            r="1.4"
            fill={v.done ? 'rgb(168,176,168)' : 'rgb(184,160,120)'}
            stroke="rgba(10,10,10,0.9)"
            strokeWidth="0.4"
            vectorEffect="non-scaling-stroke"
          >
            <title>
              iter {v.iteration}: {v.done ? 'passed' : 'rejected'} · conf{' '}
              {v.confidence.toFixed(2)}
            </title>
          </circle>
        ))}
      </svg>
      <span
        className={`font-mono text-[9.5px] tabular-nums ${
          last.done ? 'text-ok' : 'text-warn'
        }`}
      >
        {last.confidence.toFixed(2)}
      </span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Right rail
// ─────────────────────────────────────────────────────────────────────────────

function RightRail({
  dispatchEvents,
  stations,
  stalest,
  onOpenAgent,
  onOpenDispatcherBrief,
}: {
  dispatchEvents: TelemetryEventView[]
  stations: Station[]
  stalest: Station | undefined
  onOpenAgent: (sessionId: string) => void
  onOpenDispatcherBrief: () => void
}) {
  return (
    <aside className="flex min-h-0 flex-col overflow-hidden border-l border-white/[0.06] bg-bg-1/40">
      <AgentRosterSection
        stations={stations}
        onOpenAgent={onOpenAgent}
      />
      <ActivityFeedSection
        events={dispatchEvents}
        stalest={stalest}
        onOpenDispatcherBrief={onOpenDispatcherBrief}
        onOpenAgent={onOpenAgent}
      />
    </aside>
  )
}

function AgentRosterSection({
  stations,
  onOpenAgent,
}: {
  stations: Station[]
  onOpenAgent: (sessionId: string) => void
}) {
  const online = stations.filter((s) => s.agent.status === 'running').length
  return (
    <section className="flex max-h-[42%] min-h-0 flex-col border-b border-white/[0.06]">
      <header className="flex items-center justify-between px-4 py-2.5 font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3">
        <span className="inline-flex items-center gap-2 text-fg-2">
          <span className="h-1 w-1 rounded-full bg-ok" />
          agents
        </span>
        <span className="tabular-nums normal-case tracking-normal text-fg-3">
          {online}/{stations.length} online
        </span>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {stations.length === 0 ? (
          <div className="rounded-md border border-dashed border-white/[0.05] bg-white/[0.01] px-3 py-4 text-center font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
            no agents yet
          </div>
        ) : (
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {stations.map((s) => (
              <li key={s.agent.session.id}>
                <RosterRow station={s} onOpen={onOpenAgent} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}

function RosterRow({
  station,
  onOpen,
}: {
  station: Station
  onOpen: (sessionId: string) => void
}) {
  const { agent, card, stale, ageMs } = station
  const running = agent.status === 'running'
  const palette = agentPalette(agent.agentType)
  const initials = agentInitials(agent.agentType, agent.session.title)
  return (
    <button
      type="button"
      onClick={() => onOpen(agent.session.id)}
      className="group flex w-full items-center gap-2.5 rounded-md border border-transparent px-2 py-1.5 text-left transition hover:border-white/[0.08] hover:bg-white/[0.025]"
    >
      <span className="relative inline-flex">
        <span
          className="inline-flex h-7 w-7 items-center justify-center rounded-md font-mono text-[10.5px] font-medium tabular-nums ring-1"
          style={{
            background: palette.bg,
            color: palette.fg,
            ['--tw-ring-color']: palette.ring,
          } as React.CSSProperties}
        >
          {initials}
        </span>
        <span
          className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ring-1 ring-bg-1 ${
            stale ? 'bg-danger' : running ? 'bg-accent animate-pulse-soft' : 'bg-fg-3'
          }`}
        />
      </span>
      <div className="flex min-w-0 flex-1 flex-col leading-none">
        <span className="truncate font-mono text-[11px] text-fg-0">
          {agent.agentType || agent.session.title || 'agent'}
        </span>
        <span
          className={`mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] ${
            stale ? 'text-danger' : running ? 'text-accent' : 'text-fg-3'
          }`}
        >
          {stale
            ? `stale · ${minutesAgo(ageMs)}`
            : running
              ? card
                ? `on ${card.id} · ${minutesAgo(ageMs)}`
                : `live · ${minutesAgo(ageMs)}`
              : agent.status}
        </span>
      </div>
    </button>
  )
}

function ActivityFeedSection({
  events,
  stalest,
  onOpenDispatcherBrief,
  onOpenAgent,
}: {
  events: TelemetryEventView[]
  stalest: Station | undefined
  onOpenDispatcherBrief: () => void
  onOpenAgent: (sessionId: string) => void
}) {
  const recent = events.slice(-30).reverse()
  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <header className="flex items-center justify-between px-4 py-2.5 font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3">
        <span className="inline-flex items-center gap-2 text-fg-2">
          <span className="h-1 w-1 rounded-full bg-fg-2 animate-pulse-soft" />
          activity
        </span>
        <button
          type="button"
          onClick={onOpenDispatcherBrief}
          className="font-mono text-[10px] uppercase tracking-[0.16em] text-fg-3 transition hover:text-accent"
        >
          brief →
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {recent.length === 0 ? (
          <div className="rounded-md border border-dashed border-white/[0.05] bg-white/[0.01] px-3 py-4 text-center font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
            no activity yet
          </div>
        ) : (
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
            {recent.map((ev, i) => (
              <li key={`${ev.at}-${i}`}>
                <ActivityRow ev={ev} onOpenAgent={onOpenAgent} />
              </li>
            ))}
          </ul>
        )}
        {stalest ? (
          <div className="mt-3 rounded-md border border-warn/[0.18] bg-warn/[0.04] px-2.5 py-2 font-mono text-[10.5px] leading-[1.55] text-warn">
            {stalest.agent.agentType || 'agent'} is stale — autopilot will
            fall back to the next eligible worker.
          </div>
        ) : null}
      </div>
    </section>
  )
}

function ActivityRow({
  ev,
  onOpenAgent,
}: {
  ev: TelemetryEventView
  onOpenAgent: (sessionId: string) => void
}) {
  const kind = ev.subtype ?? ''
  const details = (ev.details ?? {}) as Record<string, unknown>
  const actor =
    typeof details.actor === 'string'
      ? (details.actor as string)
      : typeof details.agentType === 'string'
        ? (details.agentType as string)
        : undefined
  const sessionId =
    typeof details.judgeSessionId === 'string'
      ? (details.judgeSessionId as string)
      : typeof details.workerSessionId === 'string'
        ? (details.workerSessionId as string)
        : undefined
  const isWarn =
    kind === 'kanban_blocked' ||
    kind === 'kanban_judge_failed' ||
    kind.includes('stale') ||
    kind.includes('failed')
  const isOk = kind === 'kanban_judge_verdict' && details.done === true
  return (
    <div
      className="group grid grid-cols-[40px_1fr] gap-2 rounded px-1.5 py-1 font-mono text-[10.5px] leading-[1.55] transition hover:bg-white/[0.025]"
      onClick={() => {
        if (sessionId) onOpenAgent(sessionId)
      }}
      role={sessionId ? 'button' : undefined}
      style={{ cursor: sessionId ? 'pointer' : 'default' }}
    >
      <span className="tabular-nums text-fg-3">{tsStr(ev.at)}</span>
      <span className="min-w-0">
        {actor ? (
          <span className="text-fg-0">{actor}</span>
        ) : (
          <span className="text-fg-2 italic">autopilot</span>
        )}{' '}
        {isOk ? <span className="text-ok">✓</span> : null}{' '}
        <span className={isWarn ? 'text-warn' : 'text-fg-1'}>
          {ev.message ?? kind.replace(/_/g, ' ')}
        </span>
      </span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Bottom stats strip
// ─────────────────────────────────────────────────────────────────────────────

function BottomStrip({
  cards,
  buckets,
  telemetryEvents,
  contextPct,
}: {
  cards: KanbanCardView[]
  buckets: Buckets
  telemetryEvents: TelemetryEventView[]
  contextPct: number
}) {
  // Tasks/hr: count `kanban_judge_verdict` events with done=true in
  // the last 60 minutes; falls back to completion timestamps on cards.
  const now = Date.now()
  const hour = 60 * 60 * 1000
  const completedLastHour = useMemo(() => {
    const fromEvents = telemetryEvents.filter((e) => {
      if (e.subtype !== 'kanban_judge_verdict') return false
      if ((e.details as Record<string, unknown>)?.done !== true) return false
      return now - e.at < hour
    }).length
    if (fromEvents > 0) return fromEvents
    return cards.filter(
      (c) => c.completedAt && now - c.completedAt < hour,
    ).length
  }, [telemetryEvents, cards, now])

  // Cycle time avg: completedAt - createdAt across done cards.
  const cycleAvgMs = useMemo(() => {
    const done = cards.filter(
      (c) => c.completedAt && c.createdAt && c.completedAt > c.createdAt,
    )
    if (done.length === 0) return null
    const total = done.reduce(
      (acc, c) => acc + ((c.completedAt ?? 0) - (c.createdAt ?? 0)),
      0,
    )
    return total / done.length
  }, [cards])

  // WIP distribution — already in buckets.
  const wipTotal =
    buckets.ready.length +
    buckets.flight.length +
    buckets.review.length +
    buckets.blocked.length
  return (
    <footer className="grid grid-cols-[1fr_1fr_1fr_1fr] items-center gap-4 border-t border-white/[0.06] bg-bg-0 px-8 py-3 font-mono text-[10.5px] text-fg-2">
      <StripStat
        label="throughput"
        value={`${completedLastHour}`}
        unit="/hr"
      />
      <StripStat
        label="cycle time"
        value={cycleAvgMs != null ? humanDuration(cycleAvgMs) : '—'}
        unit="avg"
      />
      <WipBar
        ready={buckets.ready.length}
        flight={buckets.flight.length}
        review={buckets.review.length}
        blocked={buckets.blocked.length}
        total={wipTotal}
      />
      <StripStat
        label="context"
        value={`${Math.round(contextPct)}%`}
        accent={contextPct > 80 ? 'warn' : undefined}
      />
    </footer>
  )
}

function StripStat({
  label,
  value,
  unit,
  accent,
}: {
  label: string
  value: string
  unit?: string
  accent?: 'warn'
}) {
  const valCls = accent === 'warn' ? 'text-warn' : 'text-fg-0'
  return (
    <div className="flex items-baseline gap-2">
      <span className="font-mono text-[9.5px] uppercase tracking-[0.16em] text-fg-3">
        {label}
      </span>
      <span className={`tabular-nums text-[13px] ${valCls}`}>{value}</span>
      {unit ? (
        <span className="tabular-nums text-[10px] text-fg-3">{unit}</span>
      ) : null}
    </div>
  )
}

function WipBar({
  ready,
  flight,
  review,
  blocked,
  total,
}: {
  ready: number
  flight: number
  review: number
  blocked: number
  total: number
}) {
  const denom = Math.max(total, 1)
  const r = (ready / denom) * 100
  const f = (flight / denom) * 100
  const rev = (review / denom) * 100
  const b = (blocked / denom) * 100
  return (
    <div className="flex items-center gap-2">
      <span className="font-mono text-[9.5px] uppercase tracking-[0.16em] text-fg-3">
        wip
      </span>
      <div className="relative flex h-2 flex-1 overflow-hidden rounded-full bg-white/[0.04]">
        <span style={{ width: `${r}%` }} className="bg-accent/55" />
        <span style={{ width: `${f}%` }} className="bg-fg-1/65" />
        <span style={{ width: `${rev}%` }} className="bg-fg-2/55" />
        <span style={{ width: `${b}%` }} className="bg-warn/65" />
      </div>
      <span className="font-mono text-[10px] tabular-nums text-fg-2">{total}</span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Drawer body
// ─────────────────────────────────────────────────────────────────────────────

function CardDrawerBody({
  card,
  allCards,
  judgeHistory,
  onOpenAgent,
}: {
  card: KanbanCardView
  allCards: KanbanCardView[]
  judgeHistory: JudgeHistoryEntry[]
  onOpenAgent: (sessionId: string) => void
}) {
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
        </div>
      ) : null}
      <DrawerAssignment
        agent={card.assignee ?? 'unassigned'}
        age={
          card.startedAt
            ? `${Math.round((Date.now() - card.startedAt) / 60000)}m`
            : undefined
        }
        current={card.summary}
      />
      {judgeHistory.length > 0 || card.judgeSessionId ? (
        <DrawerSection
          label={`judge · ${judgeHistory.length}/${KANBAN_MAX_REVIEW_ITERATIONS} iterations`}
        >
          <JudgeIterationMeter
            card={card}
            verdicts={judgeHistory}
            inReview={(card.status ?? '').toLowerCase() === 'review'}
            isBlocked={(card.status ?? '').toLowerCase() === 'blocked'}
            judgeSessionId={card.judgeSessionId}
            onOpenJudge={
              card.judgeSessionId
                ? () => onOpenAgent(card.judgeSessionId!)
                : undefined
            }
          />
          {judgeHistory.length > 0 ? (
            <ul className="mt-2 flex list-none flex-col gap-1 p-0">
              {judgeHistory.map((v) => (
                <li
                  key={v.iteration}
                  className={`rounded-md border px-2.5 py-1.5 font-mono text-[11px] leading-[1.5] ${
                    v.done
                      ? 'border-ok/[0.24] bg-ok/[0.04] text-fg-1'
                      : 'border-warn/[0.20] bg-warn/[0.04] text-fg-1'
                  }`}
                >
                  <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.14em]">
                    <span>
                      iter {v.iteration} ·{' '}
                      <span className={v.done ? 'text-ok' : 'text-warn'}>
                        {v.done ? 'passed' : 'rejected'}
                      </span>
                    </span>
                    <span className="tabular-nums text-fg-3">
                      conf {v.confidence.toFixed(2)}
                    </span>
                  </div>
                  {v.reason ? (
                    <div className="mt-1 whitespace-pre-wrap text-fg-2">
                      {truncate(v.reason, 320)}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </DrawerSection>
      ) : null}
      {card.body ? (
        <DrawerSection label="brief">
          <p className="m-0 whitespace-pre-wrap font-mono text-[12.5px] leading-[1.7] text-fg-1">
            {card.body}
          </p>
        </DrawerSection>
      ) : null}
      {(card.artifacts?.length ?? 0) > 0 ? (
        <DrawerSection label={`artifacts · ${card.artifacts!.length}`}>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {card.artifacts!.map((p) => (
              <li
                key={p}
                className="truncate rounded border border-white/[0.05] bg-white/[0.015] px-2 py-1 font-mono text-[11px] text-fg-1"
              >
                {p}
              </li>
            ))}
          </ul>
        </DrawerSection>
      ) : null}
      {(deps.length > 0 || blocks.length > 0) ? (
        <DrawerSection label="dependencies">
          <DrawerDependencies
            dependsOn={deps.map((d) => ({
              text: d.title,
              status: (d.status === 'done' ? 'done' : 'queued') as
                | 'done'
                | 'queued'
                | 'blocked',
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
              kind: e.actor
                ? 'agent'
                : (e.kind ?? '').includes('autopilot')
                  ? 'auto'
                  : 'system',
            }))}
          />
        </DrawerSection>
      ) : null}
    </>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Agent badge / palette helpers (preserved from previous version)
// ─────────────────────────────────────────────────────────────────────────────

interface Station {
  agent: AgentView
  card?: KanbanCardView
  cardId?: string
  stale: boolean
  ageMs: number
  lastEventMs?: number
}

const AGENT_TYPE_PALETTE: Record<
  string,
  { bg: string; fg: string; ring: string }
> = {
  explore: {
    bg: 'rgba(168, 212, 252, 0.10)',
    fg: 'rgb(168, 212, 252)',
    ring: 'rgba(168, 212, 252, 0.35)',
  },
  'explore-fast': {
    bg: 'rgba(126, 175, 234, 0.10)',
    fg: 'rgb(126, 175, 234)',
    ring: 'rgba(126, 175, 234, 0.32)',
  },
  code: {
    bg: 'rgba(126, 201, 165, 0.10)',
    fg: 'rgb(126, 201, 165)',
    ring: 'rgba(126, 201, 165, 0.35)',
  },
  verify: {
    bg: 'rgba(180, 130, 130, 0.10)',
    fg: 'rgb(208, 158, 158)',
    ring: 'rgba(208, 158, 158, 0.35)',
  },
  plan: {
    bg: 'rgba(232, 196, 132, 0.10)',
    fg: 'rgb(232, 196, 132)',
    ring: 'rgba(232, 196, 132, 0.35)',
  },
  memory: {
    bg: 'rgba(208, 158, 220, 0.10)',
    fg: 'rgb(208, 158, 220)',
    ring: 'rgba(208, 158, 220, 0.35)',
  },
  browser: {
    bg: 'rgba(208, 160, 64, 0.12)',
    fg: 'rgb(228, 180, 96)',
    ring: 'rgba(228, 180, 96, 0.35)',
  },
  'judge-deep': {
    bg: 'rgba(208, 158, 158, 0.10)',
    fg: 'rgb(212, 168, 168)',
    ring: 'rgba(212, 168, 168, 0.35)',
  },
  general: {
    bg: 'rgba(168, 168, 168, 0.10)',
    fg: 'rgb(184, 184, 184)',
    ring: 'rgba(184, 184, 184, 0.30)',
  },
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
  const t = (type || fallback || '').trim()
  if (!t) return '??'
  const words = t.split(/[\s_\-]+/).filter(Boolean)
  if (words.length >= 2) {
    return (words[0][0] + words[1][0]).toUpperCase()
  }
  return t.slice(0, 2).toUpperCase()
}

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
        <span
          className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ${dotClass}`}
        />
      </button>
    )
  }
  // 'card' / 'idle' fall through to the larger version; this view
  // mostly uses 'inline'.
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
          style={{
            background: palette.bg,
            color: palette.fg,
            ['--tw-ring-color']: palette.ring,
          } as React.CSSProperties}
        >
          {initials}
        </span>
        <span
          className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ring-1 ring-bg-0 ${dotClass}`}
        />
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
          {stale
            ? `stale · ${minutesAgo(ageMs)}`
            : running
              ? `live · ${minutesAgo(ageMs)}`
              : agent.status}
        </span>
      </span>
    </button>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Bucket + helper math
// ─────────────────────────────────────────────────────────────────────────────

interface Buckets {
  ready: KanbanCardView[]
  flight: KanbanCardView[]
  review: KanbanCardView[]
  blocked: KanbanCardView[]
  done: KanbanCardView[]
}

function bucketCards(cards: KanbanCardView[]): Buckets {
  const buckets: Buckets = {
    ready: [],
    flight: [],
    review: [],
    blocked: [],
    done: [],
  }
  for (const c of cards) {
    const s = (c.status ?? '').toLowerCase()
    if (s === 'done' || s === 'complete' || s === 'completed' || s === 'sealed')
      buckets.done.push(c)
    else if (s === 'blocked') buckets.blocked.push(c)
    else if (s === 'review') buckets.review.push(c)
    else if (s === 'done_unverified') buckets.review.push(c)
    else if (s === 'running' || s === 'in_progress' || s === 'in-progress')
      buckets.flight.push(c)
    else buckets.ready.push(c)
  }
  return buckets
}

function agentStations(
  agents: AgentView[],
  cards: KanbanCardView[],
): Station[] {
  return agents.map((agent) => {
    const card = cards.find((c) =>
      c.agents?.some((a) => a.session.id === agent.session.id),
    )
    const ageMs = card?.startedAt ? Date.now() - card.startedAt : 0
    const lastEventMs =
      card?.events && card.events.length > 0
        ? Math.max(...card.events.map((e) => e.timestamp ?? 0))
        : undefined
    const stale = !!(
      card &&
      (lastEventMs
        ? Date.now() - lastEventMs > STALE_THRESHOLD_MS
        : ageMs > STALE_THRESHOLD_MS)
    )
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
    return iter ? `in review · iter ${iter}/5` : 'in review'
  }
  if (s === 'running' || s === 'in_progress') {
    const age = card.startedAt
      ? `${Math.round((Date.now() - card.startedAt) / 60000)}m`
      : ''
    return `in flight · ${age}`.trim()
  }
  return 'ready'
}

function computeProgress(card: KanbanCardView, column: ColumnKey): number | null {
  if (column !== 'flight') return null
  if (typeof card.progress === 'number' && card.progress > 0)
    return card.progress * 100
  // Heuristic: estimate progress from age since started, capped at
  // 95% until the worker actually finishes. Use 6 minutes as the
  // "typical card" half-life so simple cards quickly look ~85% and
  // long-runners hold near 90% rather than racing to 100.
  if (!card.startedAt) return 5
  const ageMs = Date.now() - card.startedAt
  const minutes = ageMs / 60000
  if (minutes <= 0) return 5
  const pct = 100 - 100 / (1 + minutes / 6)
  return Math.min(95, Math.max(5, pct))
}

function computeEta(card: KanbanCardView, column: ColumnKey): string | undefined {
  if (column !== 'flight') return undefined
  if (!card.startedAt) return undefined
  const ageMs = Date.now() - card.startedAt
  const expectedMs = 8 * 60 * 1000 // 8 min default
  const remaining = expectedMs - ageMs
  if (remaining <= 0) return 'past est'
  return humanDuration(remaining)
}

function minutesAgo(ms: number): string {
  const sec = Math.floor(ms / 1000)
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  return `${m}m`
}

function relTs(ts?: number): string {
  if (!ts) return ''
  const delta = Date.now() - ts
  if (delta < 60_000) return 'now'
  const m = Math.floor(delta / 60_000)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  return `${d}d ago`
}

function tsStr(ts: number | undefined): string {
  if (!ts) return ''
  const d = new Date(ts)
  return `${d.getHours().toString().padStart(2, '0')}:${d
    .getMinutes()
    .toString()
    .padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`
}

function humanDuration(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000))
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m}m ${sec % 60}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text
  return text.slice(0, max - 1) + '…'
}
