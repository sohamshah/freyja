import type { AgentView, TaskCardView } from '../shared/types'

/** Where a task sits in the mission: the four columns of the tasks tab. */
export type Bucket = 'now' | 'next' | 'blocked' | 'done'

export function bucketFor(status: string): Bucket {
  const s = status.toLowerCase()
  if (s === 'done' || s === 'complete' || s === 'completed' || s === 'cancelled') return 'done'
  if (s === 'blocked' || s === 'paused' || s === 'waiting') return 'blocked'
  if (s === 'active' || s === 'running' || s === 'in_progress' || s === 'in-progress') return 'now'
  return 'next'
}

export const BUCKETS: Bucket[] = ['now', 'next', 'blocked', 'done']
export const BUCKET_LABEL: Record<Bucket, string> = { now: 'now', next: 'up next', blocked: 'blocked', done: 'done' }
export const GLYPH: Record<Bucket, string> = { now: '◐', next: '○', blocked: '⊘', done: '✓' }
export const GLYPH_CLASS: Record<Bucket, string> = {
  now: 'text-accent',
  next: 'text-fg-3',
  blocked: 'text-warn',
  done: 'text-ok',
}

export function isCancelled(task: TaskCardView): boolean {
  return task.status.toLowerCase() === 'cancelled'
}

/** The sub-agent working on a task, preferring one that can be opened. */
export function primaryAgent(task: TaskCardView): AgentView | null {
  const agents = task.agents ?? []
  return agents.find((a) => a.status === 'running' && a.attachable) ?? agents.find((a) => a.attachable) ?? agents[0] ?? null
}

export function agentLabel(a: AgentView): string {
  return a.sub?.label || a.session.title || a.agentType || a.session.id.slice(0, 8)
}

// ── Time ───────────────────────────────────────────────────────────────────

export function clock(ts: number): string {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export function dayClock(ts: number): string {
  const d = new Date(ts)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  return sameDay
    ? clock(ts)
    : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${clock(ts)}`
}

export function span(ms: number): string {
  const minutes = Math.max(0, Math.round(ms / 60000))
  if (minutes < 1) return 'under a minute'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`
}

export function ago(ts: number | undefined): string {
  if (!ts) return ''
  const minutes = Math.round((Date.now() - ts) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}
