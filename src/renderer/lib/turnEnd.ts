import type { Message } from '@shared/events'

/** The assistant messages that end their turn, whose footer carries the
 *  time the turn finished. Input that slides into a turn between two
 *  steps closes the reply there and the turn goes on in a later message
 *  (`continues`). Transcripts saved before that mark existed are read
 *  from what follows: a mid-turn follow-up, or the continuation itself,
 *  which opens the moment the reply was closed. */
export function turnEnders(messages: Message[]): Set<string> {
  const ordered = [...messages].sort((a, b) => a.createdAt - b.createdAt)
  const out = new Set<string>()
  ordered.forEach((m, i) => {
    if (m.role !== 'assistant' || m.completedAt == null || m.continues) return
    const next = ordered[i + 1]
    if (next?.role === 'user' && next.followup?.midTurn) return
    if (next?.role === 'assistant' && Math.abs(next.createdAt - m.completedAt) <= 50) return
    out.add(m.id)
  })
  return out
}

/** "Oct 23, 2:14 AM"; the year only when it is not this one. */
export function finishedAt(at: number, now = Date.now()): string {
  const d = new Date(at)
  const year = d.getFullYear() !== new Date(now).getFullYear()
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', ...(year ? { year: 'numeric' } : {}), hour: 'numeric', minute: '2-digit' })
}
