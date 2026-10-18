import { useMemo, useState } from 'react'
import type { AgentView, TaskCardView } from '../shared/types'
import { renderMarkdown } from '../../lib/markdown'
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

export function TasksListRailView({ title, goal, latestAsk, tasks, agents, contextPct, cost, onAttach }: Props) {
  const [openTaskId, setOpenTaskId] = useState<string | null>(null)
  const [showAllDone, setShowAllDone] = useState(false)

  const buckets = useMemo(() => {
    const map: Record<Bucket, TaskCardView[]> = { now: [], next: [], blocked: [], done: [] }
    for (const t of tasks) map[bucketFor(t.status)].push(t)
    // Most recently finished first: that's what you check after a run.
    map.done.sort((a, b) => (b.completedAt ?? b.updatedAt ?? 0) - (a.completedAt ?? a.updatedAt ?? 0))
    return map
  }, [tasks])

  const tasksById = useMemo(() => new Map(tasks.map((t) => [t.id, t])), [tasks])
  const openTask = openTaskId ? tasksById.get(openTaskId) ?? null : null
  const openAgent = openTask ? primaryAgent(openTask) : null

  const gridCols = openTask
    ? 'grid-cols-[minmax(0,1fr)_280px_500px]'
    : 'grid-cols-[minmax(0,1fr)_280px]'

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
        <main className="min-w-0 overflow-y-auto px-10 py-7">
          <div className="flex max-w-[880px] flex-col gap-7">
            {BUCKETS.map((b) => {
              const list = buckets[b]
              if (list.length === 0) return null
              const hidden = b === 'done' && !showAllDone ? Math.max(0, list.length - DONE_VISIBLE) : 0
              return (
                <section key={b} className="flex flex-col gap-0.5">
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

        <aside className="flex min-w-0 flex-col gap-8 overflow-y-auto border-l border-white/[0.06] bg-black/[0.10] px-5 py-7">
          <AgentsRail agents={agents} onOpen={(id) => onAttach(id, 'split')} />
          <ActivityRail tasks={tasks} onOpenTask={setOpenTaskId} />
        </aside>

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
          {openTask ? <TaskDrawerBody task={openTask} tasksById={tasksById} onOpenTask={setOpenTaskId} /> : null}
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
          <span className="tabular-nums text-fg-0">{agentsCount}</span> agents
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
  const when =
    bucket === 'done'
      ? ago(task.completedAt ?? task.updatedAt)
      : bucket === 'now'
      ? task.startedAt ? `started ${ago(task.startedAt)}` : ''
      : ''
  const cancelled = task.status.toLowerCase() === 'cancelled'
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
      className={`grid cursor-pointer grid-cols-[18px_minmax(0,1fr)_auto] items-baseline gap-3 rounded-md px-3 py-2 transition ${
        selected
          ? 'bg-accent/[0.08] ring-1 ring-inset ring-accent/40'
          : bucket === 'now'
          ? 'bg-accent/[0.04] hover:bg-accent/[0.07]'
          : 'hover:bg-white/[0.03]'
      }`}
    >
      <span className={`text-center text-[12px] ${cancelled ? 'text-fg-3' : GLYPH_CLASS[bucket]}`}>
        {cancelled ? '×' : GLYPH[bucket]}
      </span>
      <span
        className={`line-clamp-2 break-words font-mono text-[12.5px] leading-[1.5] ${
          bucket === 'done' ? 'text-fg-2' : 'text-fg-0'
        }`}
      >
        {task.title}
      </span>
      <span className="flex items-baseline gap-2 whitespace-nowrap font-mono text-[10.5px] text-fg-3">
        {who ? <span className="max-w-[140px] truncate text-fg-2">{who}</span> : null}
        {when ? <span className="tabular-nums">{when}</span> : null}
      </span>
    </div>
  )
}

// ── Right rail ─────────────────────────────────────────────────────────────

const AGENT_ORDER: Record<AgentView['status'], number> = {
  running: 0,
  pending: 1,
  failed: 2,
  stopped: 3,
  cancelled: 4,
  done: 5,
}

function AgentsRail({ agents, onOpen }: { agents: AgentView[]; onOpen: (id: string) => void }) {
  const sorted = useMemo(
    () => [...agents].sort((a, b) => AGENT_ORDER[a.status] - AGENT_ORDER[b.status]),
    [agents],
  )
  const running = agents.filter((a) => a.status === 'running').length
  return (
    <section className="flex flex-col gap-2.5">
      <div className="flex items-baseline justify-between font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
        <span>agents</span>
        <span className="tabular-nums normal-case tracking-normal">
          {running > 0 ? <span className="text-accent">{running} running · </span> : null}
          {agents.length}
        </span>
      </div>
      {sorted.length === 0 ? (
        <div className="font-mono text-[11.5px] text-fg-3">No sub-agents in this session.</div>
      ) : (
        <div className="flex flex-col">
          {sorted.map((a) => (
            <button
              key={a.session.id}
              type="button"
              disabled={!a.attachable}
              onClick={() => onOpen(a.session.id)}
              title={a.attachable ? `Open ${agentLabel(a)}` : agentLabel(a)}
              className="grid grid-cols-[10px_minmax(0,1fr)] items-baseline gap-x-2.5 rounded px-1.5 py-1.5 text-left transition enabled:hover:bg-white/[0.04] disabled:cursor-default"
            >
              <span
                className={`h-1.5 w-1.5 -translate-y-px rounded-full ${
                  a.status === 'running'
                    ? 'animate-pulse-soft bg-accent shadow-[0_0_6px_rgba(168,212,252,0.6)]'
                    : a.status === 'done'
                    ? 'bg-ok'
                    : a.status === 'failed'
                    ? 'bg-danger'
                    : 'bg-fg-3'
                }`}
              />
              <span className="min-w-0">
                <span className="line-clamp-2 break-words text-[12px] leading-[1.4] text-fg-1">{agentLabel(a)}</span>
                <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-fg-3">
                  {[a.agentType, a.status].filter(Boolean).join(' · ')}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

/** The latest task events across the board, newest first. */
function ActivityRail({ tasks, onOpenTask }: { tasks: TaskCardView[]; onOpenTask: (id: string) => void }) {
  const recent = useMemo(() => {
    const rows: Array<{ at: number; who: string; what: string; task: TaskCardView }> = []
    for (const task of tasks) {
      for (const e of task.events ?? []) {
        if (!e.timestamp) continue
        rows.push({ at: e.timestamp, who: e.actor || 'system', what: e.message || e.kind || '', task })
      }
    }
    return rows.sort((a, b) => b.at - a.at).slice(0, 12)
  }, [tasks])
  return (
    <section className="flex flex-col gap-2.5">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">task activity</div>
      {recent.length === 0 ? (
        <div className="font-mono text-[11.5px] text-fg-3">Nothing yet.</div>
      ) : (
        <div className="flex flex-col">
          {recent.map((r, i) => (
            <button
              key={`${r.task.id}-${r.at}-${i}`}
              type="button"
              onClick={() => onOpenTask(r.task.id)}
              className="flex flex-col gap-0.5 rounded px-1.5 py-1.5 text-left transition hover:bg-white/[0.04]"
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
        </div>
      )}
    </section>
  )
}

// ── Drawer ─────────────────────────────────────────────────────────────────

function TaskDrawerBody({
  task,
  tasksById,
  onOpenTask,
}: {
  task: TaskCardView
  tasksById: Map<string, TaskCardView>
  onOpenTask: (id: string) => void
}) {
  const agent = primaryAgent(task)
  const events = (task.events ?? []).slice(-20)
  const outcome = task.summary?.trim()
  const details = task.result?.trim()
  const dependsOn = task.blockedBy ?? []
  const blocks = task.blocks ?? []
  return (
    <>
      <dl className="m-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-5 gap-y-1.5 font-mono text-[11.5px]">
        <dt className="text-fg-3">agent</dt>
        <dd className="m-0 truncate text-fg-1">
          {agent ? `${agentLabel(agent)} · ${agent.status}` : task.assignee || 'unassigned'}
        </dd>
        {task.startedAt ? (
          <>
            <dt className="text-fg-3">started</dt>
            <dd className="m-0 text-fg-1">{dayClock(task.startedAt)}</dd>
          </>
        ) : null}
        {task.completedAt ? (
          <>
            <dt className="text-fg-3">finished</dt>
            <dd className="m-0 text-fg-1">
              {dayClock(task.completedAt)}
              {task.startedAt ? (
                <span className="text-fg-3"> · took {span(task.completedAt - task.startedAt)}</span>
              ) : null}
            </dd>
          </>
        ) : null}
      </dl>

      {outcome ? (
        <DrawerSection label="outcome">
          <Markdown text={outcome} />
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
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {task.artifacts.map((a, i) => {
              const cut = a.lastIndexOf('/')
              return (
                <li key={i} className="break-all font-mono text-[11.5px] leading-[1.5]" title={a}>
                  <span className="text-fg-0">{cut >= 0 ? a.slice(cut + 1) : a}</span>
                  {cut >= 0 ? <span className="block text-[10.5px] text-fg-3">{a.slice(0, cut + 1)}</span> : null}
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
