import React, { useMemo, useState } from 'react'
import type { AgentView, TaskCardView, TelemetryEventView } from '../shared/types'
import {
  DetailDrawer,
  DrawerAction,
  DrawerAssignment,
  DrawerSection,
  DrawerTimeline,
} from '../shared/DetailDrawer'

interface Props {
  objective: string
  tasks: TaskCardView[]
  agents: AgentView[]
  events: TelemetryEventView[]
  contextPct: number
  cost: number
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}

type Bucket = 'now' | 'next' | 'blocked' | 'done'

function bucketFor(status: string): Bucket {
  const s = status.toLowerCase()
  if (s === 'done' || s === 'complete' || s === 'completed') return 'done'
  if (s === 'blocked' || s === 'paused' || s === 'waiting') return 'blocked'
  if (s === 'active' || s === 'running' || s === 'in_progress' || s === 'in-progress') return 'now'
  return 'next'
}

const GLYPHS: Record<Bucket, string> = {
  now: '◐',
  next: '○',
  blocked: '⊘',
  done: '✓',
}

export function TasksListRailView({
  objective,
  tasks,
  agents,
  events,
  contextPct,
  cost,
  onAttach,
}: Props) {
  const [openTaskId, setOpenTaskId] = useState<string | null>(null)
  const drawerOpen = openTaskId != null

  const buckets = useMemo(() => {
    const map: Record<Bucket, TaskCardView[]> = { now: [], next: [], blocked: [], done: [] }
    for (const t of tasks) map[bucketFor(t.status)].push(t)
    return map
  }, [tasks])

  const counts = {
    total: tasks.length,
    done: buckets.done.length,
    now: buckets.now.length,
    blocked: buckets.blocked.length,
  }

  const openTask = openTaskId ? tasks.find((t) => t.id === openTaskId) ?? null : null

  // Grid template — when the drawer is open, the drawer column is added on
  // the right and the list column flexes to make room (DevTools style).
  const gridCols = drawerOpen
    ? 'grid-cols-[minmax(0,1fr)_320px_480px]'
    : 'grid-cols-[minmax(0,1fr)_320px]'

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <Header
        objective={objective}
        counts={counts}
        contextPct={contextPct}
        cost={cost}
        agentsCount={agents.length}
      />

      <div className={`grid min-h-0 flex-1 overflow-hidden ${gridCols}`}>
        <main className="overflow-y-auto px-10 py-9">
          <div className="mx-auto flex max-w-[760px] flex-col gap-7">
            {(['now', 'next', 'blocked', 'done'] as Bucket[]).map((b) =>
              buckets[b].length > 0 ? (
                <TaskGroup
                  key={b}
                  bucket={b}
                  tasks={buckets[b]}
                  onOpen={setOpenTaskId}
                  selectedId={openTaskId}
                />
              ) : null,
            )}
            {tasks.length === 0 ? (
              <div className="py-12 text-center font-mono text-[12px] tracking-[0.06em] text-fg-3">
                no tasks yet — add one from the cradle below.
              </div>
            ) : null}
          </div>
        </main>

        <aside className="flex flex-col gap-9 overflow-y-auto border-l border-white/[0.06] bg-black/[0.10] px-6 py-8">
          <CrewRail agents={agents} />
          <PulseRail events={events} />
        </aside>

        <DetailDrawer
          open={drawerOpen}
          onClose={() => setOpenTaskId(null)}
          title={openTask?.title ?? ''}
          statusLabel={openTask ? statusLabel(openTask) : undefined}
          footer={
            <>
              <DrawerAction onClick={() => openTask && onAttach(openTask.id, 'split')}>Open split</DrawerAction>
              <DrawerAction onClick={() => openTask && onAttach(openTask.id, 'replace')}>Open here</DrawerAction>
              <DrawerAction variant="warn">Mark blocked</DrawerAction>
              <DrawerAction variant="ok">Mark done</DrawerAction>
            </>
          }
        >
          {openTask ? <TaskDrawerBody task={openTask} /> : null}
        </DetailDrawer>
      </div>
    </div>
  )
}

function statusLabel(task: TaskCardView): string {
  const b = bucketFor(task.status)
  if (b === 'now') return `in flight · ${task.assignee ?? 'unassigned'}`
  if (b === 'blocked') return 'blocked'
  if (b === 'done') return 'done'
  return 'queued'
}

function Header({
  objective,
  counts,
  contextPct,
  cost,
  agentsCount,
}: {
  objective: string
  counts: { total: number; done: number; now: number; blocked: number }
  contextPct: number
  cost: number
  agentsCount: number
}) {
  return (
    <header className="border-b border-white/[0.06] px-10 py-7">
      <div className="mb-3 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
        mission
      </div>
      <h1 className="m-0 max-w-[880px] font-serif text-[24px] font-light leading-[1.4] tracking-[-0.005em] text-fg-1">
        {objective || <span className="italic text-fg-3">no objective set</span>}
      </h1>
      <div className="mt-4 flex flex-wrap gap-5 font-mono text-[11.5px] tracking-[0.06em] text-fg-2">
        <Stat>
          <span className="tabular-nums text-fg-0">{counts.done}</span>{' '}
          <span className="text-fg-3">of</span>{' '}
          <span className="tabular-nums text-fg-0">{counts.total}</span> done
        </Stat>
        <Stat>
          <span className="tabular-nums text-accent">{counts.now}</span> in flight
        </Stat>
        {counts.blocked > 0 ? (
          <Stat>
            <span className="tabular-nums text-warn">{counts.blocked}</span> blocked
          </Stat>
        ) : null}
        <span className="text-fg-3">·</span>
        <Stat>
          <span className="tabular-nums text-fg-0">{agentsCount}</span> agents
        </Stat>
        <Stat>
          <span className="tabular-nums text-fg-0">{contextPct}%</span> context
        </Stat>
        <Stat>
          <span className="tabular-nums text-fg-0">${cost.toFixed(2)}</span> spend
        </Stat>
      </div>
    </header>
  )
}

function Stat({ children }: { children: React.ReactNode }) {
  return <span className="inline-flex items-baseline gap-1.5">{children}</span>
}

function TaskGroup({
  bucket,
  tasks,
  onOpen,
  selectedId,
}: {
  bucket: Bucket
  tasks: TaskCardView[]
  onOpen: (id: string) => void
  selectedId: string | null
}) {
  return (
    <section className="flex flex-col gap-1.5">
      <div className="mb-2 flex items-baseline gap-3 pb-1.5">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-2">
          {labelFor(bucket)}
        </span>
        <span className="font-mono text-[10.5px] tabular-nums text-fg-3">{tasks.length}</span>
      </div>
      {tasks.map((t) => (
        <TaskRow
          key={t.id}
          task={t}
          bucket={bucket}
          onClick={() => onOpen(t.id)}
          selected={selectedId === t.id}
        />
      ))}
    </section>
  )
}

function labelFor(b: Bucket): string {
  if (b === 'now') return 'now'
  if (b === 'next') return 'up next'
  if (b === 'blocked') return 'blocked'
  return 'done'
}

function TaskRow({
  task,
  bucket,
  onClick,
  selected,
}: {
  task: TaskCardView
  bucket: Bucket
  onClick: () => void
  selected: boolean
}) {
  const glyph = GLYPHS[bucket]
  const glyphClass =
    bucket === 'now'
      ? 'text-accent'
      : bucket === 'blocked'
      ? 'text-warn'
      : bucket === 'done'
      ? 'text-ok'
      : 'text-fg-2'
  const titleClass =
    bucket === 'done'
      ? 'text-fg-2 line-through decoration-fg-4'
      : 'text-fg-0'
  const rowBg =
    bucket === 'now'
      ? 'bg-accent/[0.045] hover:bg-accent/[0.08]'
      : 'hover:bg-white/[0.025]'
  return (
    <div
      onClick={onClick}
      role="button"
      tabIndex={0}
      className={`grid cursor-pointer grid-cols-[18px_1fr_auto] items-center gap-3.5 rounded-md px-3 py-2.5 transition ${rowBg} ${
        selected ? 'ring-1 ring-accent/[0.45] ring-offset-2 ring-offset-bg-0' : ''
      }`}
    >
      <span className={`text-center text-[13px] ${glyphClass}`}>{glyph}</span>
      <span className={`font-mono text-[13px] leading-[1.45] ${titleClass}`}>{task.title}</span>
      <span className="font-mono text-[11px] tabular-nums text-fg-3">
        {task.assignee ?? ''} {task.completedAt && bucket === 'done' ? `· ${ageStr(task)}` : ''}
      </span>
    </div>
  )
}

function ageStr(t: TaskCardView): string {
  const ts = t.completedAt ?? t.updatedAt ?? t.startedAt
  if (!ts) return ''
  const minutes = Math.max(1, Math.round((Date.now() - ts) / 60000))
  return `${minutes}m`
}

function CrewRail({ agents }: { agents: AgentView[] }) {
  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
        <span>crew</span>
        <span className="tabular-nums text-fg-3">{agents.length}</span>
      </div>
      <div className="flex flex-col gap-2.5">
        {agents.length === 0 ? (
          <div className="font-mono text-[11.5px] text-fg-3">no active agents</div>
        ) : (
          agents.slice(0, 8).map((a) => (
            <div key={a.session.id} className="grid grid-cols-[10px_1fr] items-center gap-2.5 text-[12px]">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  a.status === 'running'
                    ? 'animate-pulse-soft bg-accent shadow-[0_0_6px_rgba(168,212,252,0.6)]'
                    : a.status === 'done'
                    ? 'bg-ok'
                    : a.status === 'failed' || a.status === 'cancelled'
                    ? 'bg-danger'
                    : 'bg-fg-3'
                }`}
              />
              <span className="font-mono">
                <span className="text-fg-0">
                  {a.agentType || a.session.title || a.session.id.slice(0, 8)}
                </span>
                <span className="ml-2 text-[10.5px] uppercase tracking-[0.12em] text-fg-3">
                  {a.status}
                </span>
              </span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}

function PulseRail({ events }: { events: TelemetryEventView[] }) {
  const recent = useMemo(() => events.slice(-10).reverse(), [events])
  return (
    <section className="flex flex-col gap-3">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">pulse</div>
      <div className="flex flex-col">
        {recent.length === 0 ? (
          <div className="font-mono text-[11.5px] text-fg-3">no events yet</div>
        ) : (
          recent.map((ev, i) => (
            <div
              key={`${ev.sessionId}-${ev.at}-${i}`}
              className="grid grid-cols-[44px_60px_1fr] gap-2.5 border-t border-transparent py-1.5 font-mono text-[11px] first:border-t-0 [&:not(:first-child)]:border-t-white/[0.03]"
            >
              <span className="tabular-nums text-fg-3">{tsStr(ev.at)}</span>
              <span className="truncate text-fg-2">{ev.sessionTitle ?? ev.sessionId.slice(0, 6)}</span>
              <span className="truncate text-fg-1">{(ev.subtype ?? '').replace(/_/g, ' ')}</span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}

function tsStr(ts: number | undefined): string {
  if (!ts) return ''
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function TaskDrawerBody({ task }: { task: TaskCardView }) {
  const events = (task.events ?? []).slice(-12)
  return (
    <>
      <DrawerAssignment
        agent={task.assignee ?? 'unassigned'}
        age={task.startedAt ? `${Math.round((Date.now() - task.startedAt) / 60000)}m` : undefined}
        current={task.summary}
      />
      {task.body ? (
        <DrawerSection label="brief">
          <p className="m-0 whitespace-pre-wrap font-mono text-[12.5px] leading-[1.7] text-fg-1">
            {task.body}
          </p>
        </DrawerSection>
      ) : null}
      {events.length > 0 ? (
        <DrawerSection label={`activity · ${events.length} events`}>
          <DrawerTimeline
            events={events.map((e) => ({
              ts: tsStr(e.timestamp),
              who: e.actor ?? 'system',
              body: e.message ?? e.kind ?? '',
              kind: e.actor ? 'agent' : 'system',
            }))}
          />
        </DrawerSection>
      ) : null}
      {task.artifacts && task.artifacts.length > 0 ? (
        <DrawerSection label="artifacts">
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {task.artifacts.map((a, i) => (
              <li key={i} className="font-mono text-[12px] text-fg-1">
                {a}
              </li>
            ))}
          </ul>
        </DrawerSection>
      ) : null}
    </>
  )
}
