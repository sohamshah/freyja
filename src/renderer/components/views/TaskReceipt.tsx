import { useMemo, useState } from 'react'
import type { TaskCardView } from '../shared/types'
import { renderMarkdown } from '../../lib/markdown'
import { useEscapeClose } from '../../lib/useEscapeClose'
import { DrawerTimeline } from '../shared/DetailDrawer'
import { TopographicMark } from '../TopographicMark'
import { GLYPH, GLYPH_CLASS, agentLabel, bucketFor, clock, dayClock, isCancelled, primaryAgent, span } from './taskBuckets'

interface Props {
  /** The open task, or null when no ticket is pulled. */
  task: TaskCardView | null
  /** Every task of the mission: for the ticket's place in it and the dependency titles. */
  tasks: TaskCardView[]
  onClose: () => void
  onOpenTask: (id: string) => void
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}

/**
 * The open task as a receipt lying beside the list: a torn top edge, the
 * mark and a running number, the status as a rubber stamp, the facts as
 * dotted leaders, a perforation with notches between the stub and the
 * printed body, and a barcode from the task id at the foot. The paper,
 * stamp ink and shadows live in globals.css under `.ticket-*`.
 *
 * Always mounted: the Esc handler registers once so it wins over the
 * dashboard's own (see useEscapeClose); it renders nothing while closed.
 */
export function TaskReceipt({ task, tasks, onClose, onOpenTask, onAttach }: Props) {
  useEscapeClose(task != null, onClose)
  const tasksById = useMemo(() => new Map(tasks.map((t) => [t.id, t])), [tasks])
  const order = useMemo(() => [...tasks].sort((a, b) => (a.createdAt ?? 0) - (b.createdAt ?? 0)), [tasks])
  if (!task) return null
  return (
    <div className="ticket-desk relative min-h-0 min-w-0 overflow-hidden pb-6 pl-2 pr-10 pt-7">
      {/* Keyed by task so a new ticket starts at the top and fades in. */}
      <Receipt
        key={task.id}
        task={task}
        order={order}
        tasksById={tasksById}
        onClose={onClose}
        onOpenTask={onOpenTask}
        onAttach={onAttach}
      />
    </div>
  )
}

type StampTone = 'ok' | 'accent' | 'warn' | 'danger' | 'muted'

function stampFor(task: TaskCardView): { text: string; tone: StampTone } {
  if (isCancelled(task)) return { text: 'void', tone: 'danger' }
  const b = bucketFor(task.status)
  if (b === 'done') return { text: 'done', tone: 'ok' }
  if (b === 'now') return { text: 'in flight', tone: 'accent' }
  if (b === 'blocked') return { text: 'blocked', tone: 'warn' }
  return { text: 'queued', tone: 'muted' }
}

/** "003" from `task_003`; otherwise the task's place in creation order. */
function ticketNumber(task: TaskCardView, order: TaskCardView[]): string {
  const m = /^task[_-]?0*(\d+)$/i.exec(task.id)
  if (m) return m[1].padStart(3, '0')
  const i = order.findIndex((t) => t.id === task.id)
  return String(i < 0 ? order.length : i + 1).padStart(3, '0')
}

/** Bars from the task id: the same ticket always prints the same code. */
function barcode(seed: string, bars = 58): Array<{ x: number; w: number; tall: boolean }> {
  let h = 2166136261
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i)
    h = Math.imul(h, 16777619) >>> 0
  }
  let s = h || 1
  const next = () => {
    s ^= s << 13
    s >>>= 0
    s ^= s >>> 17
    s ^= s << 5
    s >>>= 0
    return s
  }
  const out: Array<{ x: number; w: number; tall: boolean }> = []
  let x = 0
  for (let i = 0; i < bars; i++) {
    const w = 1 + (next() % 3)
    out.push({ x, w, tall: i < 2 || i >= bars - 2 })
    x += w + 1 + (next() % 2)
  }
  return out
}

function Receipt({
  task,
  order,
  tasksById,
  onClose,
  onOpenTask,
  onAttach,
}: {
  task: TaskCardView
  order: TaskCardView[]
  tasksById: Map<string, TaskCardView>
  onClose: () => void
  onOpenTask: (id: string) => void
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  const agent = primaryAgent(task)
  const bucket = bucketFor(task.status)
  const stamp = stampFor(task)
  const number = ticketNumber(task, order)
  const place = Math.max(1, order.findIndex((t) => t.id === task.id) + 1)
  const events = (task.events ?? []).slice(-20)
  const outcome = task.summary?.trim()
  const details = task.result?.trim()
  const brief = task.body?.trim()
  const dependsOn = task.blockedBy ?? []
  const blocks = task.blocks ?? []
  const artifacts = task.artifacts ?? []
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
  if (task.priority != null) facts.push(['priority', `p${task.priority}`])
  const bars = useMemo(() => barcode(task.id), [task.id])
  const last = bars[bars.length - 1]
  const codeWidth = last ? last.x + last.w : 1
  const printed =
    Boolean(outcome) ||
    Boolean(details && details !== outcome) ||
    Boolean(brief) ||
    dependsOn.length > 0 ||
    blocks.length > 0 ||
    events.length > 0 ||
    artifacts.length > 0

  return (
    <div className="ticket-lift animate-fade-in">
      <article className="ticket" aria-label={`Task ticket ${number}`}>
        <header className="ticket-stub">
          <div className="ticket-head">
            <TopographicMark size={16} />
            <span>freyja · task ticket</span>
            <button type="button" className="ticket-close" onClick={onClose} title="Close (Esc)">
              close ✕
            </button>
          </div>
          <div className="ticket-stamp-slot" aria-hidden>
            <span className={`ticket-stamp ticket-stamp-${stamp.tone}`}>{stamp.text}</span>
          </div>
          <div className="ticket-no">
            <span>Nº {number}</span>
            {task.createdAt ? <span className="ticket-no-issued"> · issued {dayClock(task.createdAt)}</span> : null}
          </div>
          <h2 className="ticket-title">{task.title}</h2>
          <dl className="ticket-facts">
            {facts.map(([k, v]) => (
              <div key={k} className="ticket-fact">
                <dt>{k}</dt>
                <dd title={v}>{v}</dd>
              </div>
            ))}
          </dl>
        </header>

        <div className="ticket-lower">
          <div className="ticket-body">
            {outcome ? (
              <Section label="outcome">
                <Markdown text={outcome} />
              </Section>
            ) : null}
            {details && details !== outcome ? (
              <Section label="details">
                <Markdown text={details} />
              </Section>
            ) : null}
            {brief ? (
              <Section label="brief">
                <Markdown text={brief} />
              </Section>
            ) : null}
            {dependsOn.length > 0 || blocks.length > 0 ? (
              <Section label="dependencies">
                <DependencyList
                  label={
                    dependsOn.every((id) => bucketFor(tasksById.get(id)?.status ?? '') === 'done') ? 'depended on' : 'waits on'
                  }
                  ids={dependsOn}
                  tasksById={tasksById}
                  onOpenTask={onOpenTask}
                />
                <DependencyList label="blocks" ids={blocks} tasksById={tasksById} onOpenTask={onOpenTask} />
              </Section>
            ) : null}
            {events.length > 0 ? (
              <Section label={`activity · ${events.length}`}>
                <DrawerTimeline
                  events={events.map((e) => ({
                    ts: e.timestamp ? clock(e.timestamp) : '',
                    who: e.actor ?? 'system',
                    body: e.message ?? e.kind ?? '',
                    kind: e.actor ? 'agent' : 'system',
                  }))}
                />
              </Section>
            ) : null}
            {artifacts.length > 0 ? (
              <Section label={`artifacts · ${artifacts.length}`}>
                <ul className="ticket-artifacts">
                  {artifacts.map((a, i) => {
                    const cut = a.lastIndexOf('/')
                    return (
                      <li key={i} title={a}>
                        <span className="ticket-artifact-mark">▪</span>
                        <span className="min-w-0">
                          <span className="block truncate text-fg-0">{cut >= 0 ? a.slice(cut + 1) : a}</span>
                          {cut >= 0 ? <span className="block truncate text-[10.5px] text-fg-3">{a.slice(0, cut + 1)}</span> : null}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              </Section>
            ) : null}
            {!printed ? (
              <div className="ticket-empty">Nothing printed yet. The outcome lands here when the work finishes.</div>
            ) : null}
            {agent?.attachable ? (
              <div className="ticket-keys">
                <button type="button" className="ticket-key ticket-key-accent" onClick={() => onAttach(agent.session.id, 'split')}>
                  open agent in split
                </button>
                <button type="button" className="ticket-key" onClick={() => onAttach(agent.session.id, 'replace')}>
                  open agent here
                </button>
              </div>
            ) : null}
          </div>

          <footer className="ticket-foot">
            <svg className="ticket-code" viewBox={`0 0 ${codeWidth} 28`} preserveAspectRatio="none" aria-hidden>
              {bars.map((b, i) => (
                <rect key={i} x={b.x} y={0} width={b.w} height={b.tall ? 28 : 24} />
              ))}
            </svg>
            <div className="ticket-foot-line">
              <span className="truncate" title={task.id}>
                {task.id}
              </span>
              <span className="shrink-0">
                {place} of {order.length}
              </span>
            </div>
          </footer>
        </div>
      </article>
    </div>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="ticket-section">
      <h3 className="ticket-label">{label}</h3>
      {children}
    </section>
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

/** Agent-written markdown, rendered and kept inside the ticket: long words
 *  and paths wrap, tables and code scroll in their own box, and a long
 *  block folds behind "show all". */
function Markdown({ text }: { text: string }) {
  const html = useMemo(() => renderMarkdown(text), [text])
  const long = text.length > 1200
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div
        className={`md task-md selectable min-w-0 ${
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
