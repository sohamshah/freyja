import { useEffect, useMemo, useRef, useState } from 'react'
import type { AgentView, TaskCardView } from '../shared/types'
import { renderMarkdown } from '../../lib/markdown'
import { useEscapeClose } from '../../lib/useEscapeClose'
import { DetailDrawer, DrawerAction, DrawerSection, DrawerTimeline } from '../shared/DetailDrawer'

interface Props {
  /** The session's title: short and stable, unlike the latest message. */
  title: string
  goal?: string
  latestAsk?: { text: string; at?: number } | null
  tasks: TaskCardView[]
  agents: AgentView[]
  contextPct: number
  cost: number
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  /** A task to open on arrival (the activity panel's task rows link here). */
  focusTaskId?: string | null
  onFocusTaskHandled?: () => void
}

type Bucket = 'now' | 'next' | 'blocked' | 'done'

function bucketFor(status: string): Bucket {
  const s = status.toLowerCase()
  if (s === 'done' || s === 'complete' || s === 'completed' || s === 'cancelled') return 'done'
  if (s === 'blocked' || s === 'paused' || s === 'waiting') return 'blocked'
  if (s === 'active' || s === 'running' || s === 'in_progress' || s === 'in-progress') return 'now'
  return 'next'
}

const BUCKETS: Bucket[] = ['now', 'next', 'blocked', 'done']
const BUCKET_LABEL: Record<Bucket, string> = { now: 'now', next: 'up next', blocked: 'blocked', done: 'done' }
const GLYPH: Record<Bucket, string> = { now: '◐', next: '○', blocked: '⊘', done: '✓' }
const GLYPH_CLASS: Record<Bucket, string> = {
  now: 'text-accent',
  next: 'text-fg-3',
  blocked: 'text-warn',
  done: 'text-ok',
}
/** Done tasks past this many collapse behind a "show earlier" row. */
const DONE_VISIBLE = 5

export function TasksListRailView({
  title,
  goal,
  latestAsk,
  tasks,
  agents,
  contextPct,
  cost,
  onAttach,
  focusTaskId,
  onFocusTaskHandled,
}: Props) {
  const [openTaskId, setOpenTaskId] = useState<string | null>(null)
  const [showAllDone, setShowAllDone] = useState(false)

  const buckets = useMemo(() => {
    const map: Record<Bucket, TaskCardView[]> = { now: [], next: [], blocked: [], done: [] }
    for (const t of tasks) map[bucketFor(t.status)].push(t)
    // Most recently finished first: that's what you check after a run.
    map.done.sort((a, b) => (b.completedAt ?? b.updatedAt ?? 0) - (a.completedAt ?? a.updatedAt ?? 0))
    return map
  }, [tasks])

  // A task picked elsewhere (the activity panel) opens here: its card in
  // the drawer, its row unfolded from "show earlier" and scrolled to.
  useEffect(() => {
    if (!focusTaskId) return
    if (tasks.some((t) => t.id === focusTaskId)) {
      setOpenTaskId(focusTaskId)
      if (buckets.done.slice(DONE_VISIBLE).some((t) => t.id === focusTaskId)) setShowAllDone(true)
      requestAnimationFrame(() => {
        document
          .querySelector(`[data-task-row="${CSS.escape(focusTaskId)}"]`)
          ?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      })
    }
    onFocusTaskHandled?.()
  }, [focusTaskId, tasks, buckets, onFocusTaskHandled])

  const tasksById = useMemo(() => new Map(tasks.map((t) => [t.id, t])), [tasks])
  const openTask = openTaskId ? tasksById.get(openTaskId) ?? null : null
  const openAgent = openTask ? primaryAgent(openTask) : null

  const gridCols = openTask ? 'grid-cols-[minmax(0,1fr)_500px]' : 'grid-cols-[minmax(0,1fr)]'

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <Header
        title={title}
        goal={goal}
        latestAsk={latestAsk}
        done={buckets.done.length}
        total={tasks.length}
        now={buckets.now.length}
        blocked={buckets.blocked.length}
        agentsCount={agents.length}
        contextPct={contextPct}
        cost={cost}
      />

      <div className={`grid min-h-0 flex-1 overflow-hidden ${gridCols}`}>
        {/* The list owns the column; the agents dock floats over its
            bottom-left corner, so the padding below the last sheet keeps
            the final rows reachable above it. */}
        <div className="relative min-h-0 min-w-0">
          <main className="h-full min-w-0 overflow-y-auto px-10 pb-28 pt-7">
            <div className="flex max-w-[880px] flex-col gap-7">
              {BUCKETS.map((b) => {
                const list = buckets[b]
                if (list.length === 0) return null
                const hidden = b === 'done' && !showAllDone ? Math.max(0, list.length - DONE_VISIBLE) : 0
                return (
                  <section key={b} className="task-section flex flex-col gap-0.5">
                    <div className="mb-2 flex items-baseline gap-3">
                      <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-2">
                        {BUCKET_LABEL[b]}
                      </span>
                      <span className="font-mono text-[10.5px] tabular-nums text-fg-3">{list.length}</span>
                    </div>
                    {list.slice(0, list.length - hidden).map((t) => (
                      <TaskRow
                        key={t.id}
                        task={t}
                        bucket={b}
                        selected={openTaskId === t.id}
                        onClick={() => setOpenTaskId(openTaskId === t.id ? null : t.id)}
                      />
                    ))}
                    {b === 'done' && list.length > DONE_VISIBLE ? (
                      <button
                        type="button"
                        onClick={() => setShowAllDone((v) => !v)}
                        className="mt-1 self-start rounded px-3 py-1.5 font-mono text-[11px] text-fg-3 transition hover:bg-white/[0.03] hover:text-fg-1"
                      >
                        {showAllDone ? 'show fewer' : `show ${hidden} earlier`}
                      </button>
                    ) : null}
                  </section>
                )
              })}
              {tasks.length === 0 ? (
                <div className="py-12 font-mono text-[12px] tracking-[0.04em] text-fg-3">
                  No tasks yet. The agent adds them here as it plans the work.
                </div>
              ) : null}
            </div>
          </main>
          <AgentsDock
            agents={agents}
            tasks={tasks}
            onOpenAgent={(id) => onAttach(id, 'split')}
            onOpenTask={setOpenTaskId}
          />
        </div>

        <DetailDrawer
          open={openTask != null}
          onClose={() => setOpenTaskId(null)}
          title={openTask?.title ?? ''}
          statusLabel={openTask ? statusLabel(openTask) : undefined}
          statusTone={openTask ? STATUS_TONE[bucketFor(openTask.status)] : undefined}
          statusPulse={openTask ? bucketFor(openTask.status) === 'now' : false}
          width={500}
          footer={
            openAgent?.attachable ? (
              <>
                <DrawerAction variant="accent" onClick={() => onAttach(openAgent.session.id, 'split')}>
                  Open agent in split
                </DrawerAction>
                <DrawerAction onClick={() => onAttach(openAgent.session.id, 'replace')}>Open agent here</DrawerAction>
              </>
            ) : undefined
          }
        >
          {openTask ? <TaskTicket task={openTask} tasksById={tasksById} onOpenTask={setOpenTaskId} /> : null}
        </DetailDrawer>
      </div>
    </div>
  )
}

const STATUS_TONE: Record<Bucket, 'accent' | 'ok' | 'warn' | 'muted'> = {
  now: 'accent',
  next: 'muted',
  blocked: 'warn',
  done: 'ok',
}

function statusLabel(task: TaskCardView): string {
  const b = bucketFor(task.status)
  if (task.status.toLowerCase() === 'cancelled') return 'cancelled'
  if (b === 'now') return 'in flight'
  if (b === 'next') return 'queued'
  return b
}

/** The sub-agent working on a task, preferring one that can be opened. */
function primaryAgent(task: TaskCardView): AgentView | null {
  const agents = task.agents ?? []
  return agents.find((a) => a.status === 'running' && a.attachable) ?? agents.find((a) => a.attachable) ?? agents[0] ?? null
}

function agentLabel(a: AgentView): string {
  return a.sub?.label || a.session.title || a.agentType || a.session.id.slice(0, 8)
}

// ── Header ─────────────────────────────────────────────────────────────────

function Header({
  title,
  goal,
  latestAsk,
  done,
  total,
  now,
  blocked,
  agentsCount,
  contextPct,
  cost,
}: {
  title: string
  goal?: string
  latestAsk?: { text: string; at?: number } | null
  done: number
  total: number
  now: number
  blocked: number
  agentsCount: number
  contextPct: number
  cost: number
}) {
  const [askOpen, setAskOpen] = useState(false)
  return (
    <header className="border-b border-white/[0.06] px-10 pb-5 pt-6">
      <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">mission</div>
      <h1
        className="m-0 truncate font-serif text-[22px] font-light leading-[1.35] tracking-[-0.005em] text-fg-0"
        title={title}
      >
        {title || <span className="italic text-fg-3">Untitled session</span>}
      </h1>
      {goal ? (
        <p className="m-0 mt-1.5 line-clamp-2 max-w-[880px] text-[12.5px] leading-[1.55] text-fg-1" title={goal}>
          <span className="mr-2 font-mono text-[10.5px] uppercase tracking-[0.12em] text-fg-3">goal</span>
          {goal}
        </p>
      ) : null}
      {latestAsk ? (
        <button
          type="button"
          onClick={() => setAskOpen((v) => !v)}
          title={askOpen ? 'Collapse' : 'Show the whole message'}
          className="mt-2 flex w-full max-w-[880px] items-baseline gap-2 rounded text-left transition hover:text-fg-0"
        >
          <span className="shrink-0 font-mono text-[10.5px] uppercase tracking-[0.12em] text-fg-3">
            latest ask{latestAsk.at ? ` · ${clock(latestAsk.at)}` : ''}
          </span>
          <span
            className={`min-w-0 text-[12.5px] leading-[1.55] text-fg-2 ${
              askOpen ? 'max-h-[28vh] overflow-y-auto whitespace-pre-wrap break-words' : 'truncate'
            }`}
          >
            {latestAsk.text}
          </span>
        </button>
      ) : null}
      <div className="mt-4 flex max-w-[880px] flex-wrap items-center gap-x-5 gap-y-2 font-mono text-[11.5px] tracking-[0.04em] text-fg-2">
        <span className="inline-flex items-center gap-2.5">
          <span className="h-[3px] w-28 overflow-hidden rounded-full bg-white/[0.07]">
            <span
              className="block h-full rounded-full bg-ok/80"
              style={{ width: `${total ? Math.round((done / total) * 100) : 0}%` }}
            />
          </span>
          <span>
            <span className="tabular-nums text-fg-0">{done}</span> of{' '}
            <span className="tabular-nums text-fg-0">{total}</span> done
          </span>
        </span>
        {now > 0 ? (
          <span>
            <span className="tabular-nums text-accent">{now}</span> in flight
          </span>
        ) : null}
        {blocked > 0 ? (
          <span>
            <span className="tabular-nums text-warn">{blocked}</span> blocked
          </span>
        ) : null}
        <span>
          <span className="tabular-nums text-fg-0">{agentsCount}</span> {agentsCount === 1 ? 'agent' : 'agents'}
        </span>
        <span>
          <span className="tabular-nums text-fg-0">{contextPct}%</span> context
        </span>
        <span>
          <span className="tabular-nums text-fg-0">${cost.toFixed(2)}</span> spend
        </span>
      </div>
    </header>
  )
}

// ── Task list ──────────────────────────────────────────────────────────────

function TaskRow({
  task,
  bucket,
  selected,
  onClick,
}: {
  task: TaskCardView
  bucket: Bucket
  selected: boolean
  onClick: () => void
}) {
  const agent = primaryAgent(task)
  const who = agent ? agent.agentType || agentLabel(agent) : task.assignee
  const cancelled = task.status.toLowerCase() === 'cancelled'
  const took = task.startedAt && task.completedAt ? span(task.completedAt - task.startedAt) : null
  const artifactCount = task.artifacts?.length ?? 0
  // The second line is the ticket's substance at a glance: who worked it,
  // how long it took and what it left behind.
  const meta = [
    who,
    bucket === 'done' && took ? `took ${took}` : null,
    artifactCount > 0 ? `${artifactCount} ${artifactCount === 1 ? 'artifact' : 'artifacts'}` : null,
  ].filter(Boolean) as string[]
  const when =
    bucket === 'done'
      ? ago(task.completedAt ?? task.updatedAt)
      : bucket === 'now'
      ? task.startedAt ? `started ${ago(task.startedAt)}` : 'in flight'
      : bucket === 'blocked'
      ? 'waiting'
      : task.priority != null
      ? `p${task.priority}`
      : ''
  return (
    <div
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onClick()
        }
      }}
      role="button"
      tabIndex={0}
      data-task-row={task.id}
      className={`grid cursor-pointer grid-cols-[18px_minmax(0,1fr)_auto] items-start gap-x-3 rounded-[10px] px-3 py-2.5 transition ${
        selected
          ? 'task-row-selected bg-accent/[0.08] ring-1 ring-inset ring-accent/40'
          : bucket === 'now'
          ? 'bg-accent/[0.04] hover:bg-accent/[0.07]'
          : 'hover:bg-white/[0.03]'
      }`}
    >
      <span
        className={`text-center text-[12px] leading-[1.5] ${cancelled ? 'text-fg-3' : GLYPH_CLASS[bucket]} ${
          bucket === 'now' && !cancelled ? 'animate-pulse-soft' : ''
        }`}
      >
        {cancelled ? '×' : GLYPH[bucket]}
      </span>
      <span className="min-w-0">
        <span
          className={`line-clamp-2 break-words font-mono text-[12.5px] leading-[1.5] ${
            bucket === 'done' ? 'text-fg-1' : 'text-fg-0'
          }`}
        >
          {task.title}
        </span>
        {meta.length > 0 ? (
          <span className="mt-0.5 block truncate font-mono text-[10.5px] tracking-[0.02em] text-fg-3">
            {meta.join(' · ')}
          </span>
        ) : null}
      </span>
      {when ? (
        <span className="whitespace-nowrap pt-[2px] font-mono text-[10.5px] tabular-nums text-fg-3">{when}</span>
      ) : null}
    </div>
  )
}

// ── Agents dock ────────────────────────────────────────────────────────────

const AGENT_ORDER: Record<AgentView['status'], number> = {
  running: 0,
  pending: 1,
  failed: 2,
  stopped: 3,
  cancelled: 4,
  done: 5,
}

function recentActivity(tasks: TaskCardView[], limit: number) {
  const rows: Array<{ at: number; who: string; what: string; task: TaskCardView }> = []
  for (const task of tasks) {
    for (const e of task.events ?? []) {
      if (!e.timestamp) continue
      rows.push({ at: e.timestamp, who: e.actor || 'system', what: e.message || e.kind || '', task })
    }
  }
  return rows.sort((a, b) => b.at - a.at).slice(0, limit)
}

function StatusDot({ status, stacked = false }: { status: AgentView['status']; stacked?: boolean }) {
  const tone =
    status === 'running'
      ? 'animate-pulse-soft bg-accent shadow-[0_0_6px_rgb(var(--accent-rgb)/0.6)]'
      : status === 'done'
      ? 'bg-ok'
      : status === 'failed'
      ? 'bg-danger'
      : 'bg-fg-3'
  return (
    <span
      className={`inline-block shrink-0 rounded-full ${tone} ${
        stacked ? 'h-[9px] w-[9px] ring-[1.5px] ring-bg-1' : 'h-1.5 w-1.5'
      }`}
    />
  )
}

/**
 * The session's sub-agents, folded into a key in the list's bottom-left
 * corner. They matter when one is running or failed, not often enough to
 * own a column: the key shows the roster as stacked dots and a count,
 * and opens upward into the full list plus the latest task events.
 */
function AgentsDock({
  agents,
  tasks,
  onOpenAgent,
  onOpenTask,
}: {
  agents: AgentView[]
  tasks: TaskCardView[]
  onOpenAgent: (id: string) => void
  onOpenTask: (id: string) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  // Registered unconditionally so Esc closes the panel before it can
  // reach the dashboard (see useEscapeClose).
  useEscapeClose(open, () => setOpen(false))
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const sorted = useMemo(
    () => [...agents].sort((a, b) => AGENT_ORDER[a.status] - AGENT_ORDER[b.status]),
    [agents],
  )
  const running = agents.filter((a) => a.status === 'running').length
  const failed = agents.filter((a) => a.status === 'failed').length
  const recent = useMemo(() => recentActivity(tasks, 8), [tasks])

  if (agents.length === 0 && recent.length === 0) return null

  return (
    <div ref={ref} className="absolute bottom-6 left-10 z-20 flex flex-col items-start gap-2">
      {open ? (
        <div className="glass-strong flex max-h-[min(60vh,560px)] w-[380px] flex-col overflow-hidden rounded-[12px] animate-fade-in">
          <div className="flex items-baseline justify-between px-4 pb-2 pt-3.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
            <span>agents</span>
            <span className="tabular-nums normal-case tracking-normal">
              {running > 0 ? <span className="text-accent">{running} running · </span> : null}
              {failed > 0 ? <span className="text-danger">{failed} failed · </span> : null}
              {agents.length}
            </span>
          </div>
          <div className="min-h-0 overflow-y-auto px-2 pb-2.5">
            {sorted.length === 0 ? (
              <div className="px-2 pb-2 font-mono text-[11.5px] text-fg-3">No sub-agents in this session.</div>
            ) : (
              sorted.map((a) => (
                <button
                  key={a.session.id}
                  type="button"
                  disabled={!a.attachable}
                  onClick={() => {
                    onOpenAgent(a.session.id)
                    setOpen(false)
                  }}
                  title={a.attachable ? `Open ${agentLabel(a)} in a split pane` : agentLabel(a)}
                  className="group grid w-full grid-cols-[10px_minmax(0,1fr)_auto] items-center gap-x-2.5 rounded-md px-2 py-1.5 text-left transition enabled:hover:bg-white/[0.04] disabled:cursor-default"
                >
                  <StatusDot status={a.status} />
                  <span className="min-w-0">
                    <span className="block truncate text-[12px] leading-[1.4] text-fg-1">{agentLabel(a)}</span>
                    <span className="block truncate font-mono text-[10px] uppercase tracking-[0.1em] text-fg-3">
                      {[a.agentType, a.status].filter(Boolean).join(' · ')}
                    </span>
                  </span>
                  {a.attachable ? (
                    <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-fg-3 opacity-0 transition group-hover:opacity-100">
                      open ↗
                    </span>
                  ) : (
                    <span />
                  )}
                </button>
              ))
            )}
            {recent.length > 0 ? (
              <>
                <div className="px-2 pb-1.5 pt-3 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
                  latest task events
                </div>
                {recent.map((r, i) => (
                  <button
                    key={`${r.task.id}-${r.at}-${i}`}
                    type="button"
                    onClick={() => {
                      onOpenTask(r.task.id)
                      setOpen(false)
                    }}
                    className="flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left transition hover:bg-white/[0.04]"
                  >
                    <span className="flex items-baseline gap-2 font-mono text-[10.5px] text-fg-3">
                      <span className="tabular-nums">{clock(r.at)}</span>
                      <span className="truncate text-fg-2">{r.who}</span>
                    </span>
                    <span className="line-clamp-2 break-words text-[11.5px] leading-[1.45] text-fg-1">
                      {r.what ? `${r.what} · ` : ''}
                      <span className="text-fg-2">{r.task.title}</span>
                    </span>
                  </button>
                ))}
              </>
            ) : null}
          </div>
        </div>
      ) : null}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? 'Hide the agents' : 'The agents working this mission'}
        className={`task-dock glass-chip inline-flex h-[30px] items-center gap-2.5 rounded-full pl-2.5 pr-3 font-mono text-[10.5px] uppercase tracking-[0.12em] text-fg-1 transition hover:text-fg-0 ${
          open ? 'task-dock-open' : ''
        }`}
      >
        {sorted.length > 0 ? (
          <span className="flex items-center -space-x-[3px]">
            {sorted.slice(0, 5).map((a) => (
              <StatusDot key={a.session.id} status={a.status} stacked />
            ))}
          </span>
        ) : null}
        <span>
          <span className="tabular-nums text-fg-0">{agents.length}</span> {agents.length === 1 ? 'agent' : 'agents'}
          {running > 0 ? <span className="text-accent"> · {running} running</span> : null}
          {failed > 0 ? <span className="text-danger"> · {failed} failed</span> : null}
        </span>
        <span className="text-fg-3">{open ? '▾' : '▴'}</span>
      </button>
    </div>
  )
}

// ── Ticket ─────────────────────────────────────────────────────────────────

const CALLOUT: Record<Bucket, string> = {
  now: 'border-accent/60 bg-accent/[0.06]',
  next: 'border-fg-3/50 bg-white/[0.03]',
  blocked: 'border-warn/60 bg-warn/[0.06]',
  done: 'border-ok/60 bg-ok/[0.06]',
}

function TaskTicket({
  task,
  tasksById,
  onOpenTask,
}: {
  task: TaskCardView
  tasksById: Map<string, TaskCardView>
  onOpenTask: (id: string) => void
}) {
  const agent = primaryAgent(task)
  const bucket = bucketFor(task.status)
  const events = (task.events ?? []).slice(-20)
  const outcome = task.summary?.trim()
  const details = task.result?.trim()
  const dependsOn = task.blockedBy ?? []
  const blocks = task.blocks ?? []
  const took =
    task.startedAt && task.completedAt
      ? span(task.completedAt - task.startedAt)
      : task.startedAt && bucket === 'now'
      ? `${span(Date.now() - task.startedAt)} so far`
      : null
  const facts: Array<[string, string]> = [
    ['agent', agent ? `${agentLabel(agent)} · ${agent.status}` : task.assignee || 'unassigned'],
    ['started', task.startedAt ? dayClock(task.startedAt) : task.createdAt ? `queued ${dayClock(task.createdAt)}` : '—'],
    ['finished', task.completedAt ? dayClock(task.completedAt) : bucket === 'now' ? 'in flight' : '—'],
    ['took', took ?? '—'],
  ]
  return (
    <>
      {/* shrink-0: an overflow-hidden flex item would otherwise give up its
          whole height to the sections below it in the scroller. */}
      <dl className="ticket-facts m-0 grid shrink-0 grid-cols-2 overflow-hidden rounded-[10px] bg-white/[0.03] ring-1 ring-white/[0.06] [&>div:nth-child(n+3)]:border-t [&>div:nth-child(n+3)]:border-white/[0.06] [&>div:nth-child(odd)]:border-r [&>div:nth-child(odd)]:border-white/[0.06]">
        {facts.map(([k, v]) => (
          <div key={k} className="min-w-0 px-3.5 py-2.5" title={v}>
            <dt className="font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">{k}</dt>
            <dd className="m-0 mt-0.5 truncate font-mono text-[12px] text-fg-0">{v}</dd>
          </div>
        ))}
      </dl>

      {outcome ? (
        <DrawerSection label="outcome">
          <div className={`rounded-r-md border-l-2 px-4 py-3 ${CALLOUT[bucket]}`}>
            <Markdown text={outcome} />
          </div>
        </DrawerSection>
      ) : null}
      {details && details !== outcome ? (
        <DrawerSection label="details">
          <Markdown text={details} />
        </DrawerSection>
      ) : null}
      {task.body?.trim() ? (
        <DrawerSection label="brief">
          <Markdown text={task.body} />
        </DrawerSection>
      ) : null}
      {dependsOn.length > 0 || blocks.length > 0 ? (
        <DrawerSection label="dependencies">
          <DependencyList
            label={dependsOn.every((id) => bucketFor(tasksById.get(id)?.status ?? '') === 'done') ? 'depended on' : 'waits on'}
            ids={dependsOn}
            tasksById={tasksById}
            onOpenTask={onOpenTask}
          />
          <DependencyList label="blocks" ids={blocks} tasksById={tasksById} onOpenTask={onOpenTask} />
        </DrawerSection>
      ) : null}
      {events.length > 0 ? (
        <DrawerSection label={`activity · ${events.length}`}>
          <DrawerTimeline
            events={events.map((e) => ({
              ts: e.timestamp ? clock(e.timestamp) : '',
              who: e.actor ?? 'system',
              body: e.message ?? e.kind ?? '',
              kind: e.actor ? 'agent' : 'system',
            }))}
          />
        </DrawerSection>
      ) : null}
      {task.artifacts && task.artifacts.length > 0 ? (
        <DrawerSection label={`artifacts · ${task.artifacts.length}`}>
          <ul className="m-0 flex list-none flex-col p-0">
            {task.artifacts.map((a, i) => {
              const cut = a.lastIndexOf('/')
              return (
                <li
                  key={i}
                  className="grid grid-cols-[14px_minmax(0,1fr)] items-baseline gap-x-2 border-t border-white/[0.04] py-2 font-mono text-[11.5px] leading-[1.45] first:border-t-0"
                  title={a}
                >
                  <span className="text-[9px] text-fg-3">▪</span>
                  <span className="min-w-0">
                    <span className="block truncate text-fg-0">{cut >= 0 ? a.slice(cut + 1) : a}</span>
                    {cut >= 0 ? <span className="block truncate text-[10.5px] text-fg-3">{a.slice(0, cut + 1)}</span> : null}
                  </span>
                </li>
              )
            })}
          </ul>
        </DrawerSection>
      ) : null}
    </>
  )
}

function DependencyList({
  label,
  ids,
  tasksById,
  onOpenTask,
}: {
  label: string
  ids: string[]
  tasksById: Map<string, TaskCardView>
  onOpenTask: (id: string) => void
}) {
  if (ids.length === 0) return null
  return (
    <div className="flex flex-col gap-0.5">
      <span className="mb-1 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">{label}</span>
      {ids.map((id) => {
        const t = tasksById.get(id)
        const b = t ? bucketFor(t.status) : 'next'
        return (
          <button
            key={id}
            type="button"
            disabled={!t}
            onClick={() => onOpenTask(id)}
            className="grid grid-cols-[16px_minmax(0,1fr)] items-baseline gap-2 rounded px-2 py-1 text-left transition enabled:hover:bg-white/[0.04]"
          >
            <span className={`text-center text-[11px] ${GLYPH_CLASS[b]}`}>{GLYPH[b]}</span>
            <span className={`break-words font-mono text-[12px] ${b === 'done' ? 'text-fg-2' : 'text-fg-1'}`}>
              {t?.title ?? id}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/** Agent-written markdown, rendered and kept inside the drawer: long words
 *  and paths wrap, tables and code scroll in their own box, and a long
 *  block folds behind "show all". */
function Markdown({ text }: { text: string }) {
  const html = useMemo(() => renderMarkdown(text), [text])
  const long = text.length > 1200
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div
        className={`md task-md selectable min-w-0 text-[12.5px] ${
          long && !expanded ? 'max-h-[340px] overflow-hidden [mask-image:linear-gradient(to_bottom,black_70%,transparent)]' : ''
        }`}
        dangerouslySetInnerHTML={{ __html: html }}
      />
      {long ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="self-start rounded px-2 py-1 font-mono text-[11px] text-fg-3 transition hover:bg-white/[0.04] hover:text-fg-1"
        >
          {expanded ? 'show less' : 'show all'}
        </button>
      ) : null}
    </div>
  )
}

// ── Time ───────────────────────────────────────────────────────────────────

function clock(ts: number): string {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function dayClock(ts: number): string {
  const d = new Date(ts)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  return sameDay
    ? clock(ts)
    : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${clock(ts)}`
}

function span(ms: number): string {
  const minutes = Math.max(0, Math.round(ms / 60000))
  if (minutes < 1) return 'under a minute'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`
}

function ago(ts: number | undefined): string {
  if (!ts) return ''
  const minutes = Math.round((Date.now() - ts) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}
