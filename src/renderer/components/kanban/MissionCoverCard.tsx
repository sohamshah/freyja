import { useMemo, useState } from 'react'

interface MissionCoverCardProps {
  cards: Array<{
    id: string
    title: string
    body?: string
    status: string
    createdAt?: number
    updatedAt?: number
    summary?: string
    children?: string[]
  }>
  restartCount?: number
}

interface CardLike {
  id: string
  title: string
  body?: string
  status: string
  createdAt?: number
  updatedAt?: number
  summary?: string
  children?: string[]
}

function findMissionRoot(cards: CardLike[]): CardLike | null {
  // The mission root is the card the bridge stamped with
  // metadata.role = "mission_root". The renderer doesn't currently
  // surface metadata on the card view, so as a pragmatic detection
  // heuristic we treat the lowest-id (`card_001`) card with no parents
  // as the mission root. The first card in any kanban session under
  // Move B is the auto-created mission root, so this matches by
  // construction.
  if (cards.length === 0) return null
  const sortedById = [...cards].sort((a, b) => a.id.localeCompare(b.id))
  return sortedById[0] ?? null
}

function formatElapsed(since: number, now: number): string {
  const ms = Math.max(0, now - since)
  const minutes = Math.floor(ms / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  const remainder = minutes % 60
  if (hours < 24) {
    return remainder === 0 ? `${hours}h ago` : `${hours}h ${remainder}m ago`
  }
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function classifyChildren(root: CardLike, cards: CardLike[]) {
  const children = (root.children ?? []).map((id) =>
    cards.find((c) => c.id === id),
  ).filter(Boolean) as CardLike[]
  let running = 0
  let done = 0
  let blocked = 0
  for (const card of children) {
    if (card.status === 'running' || card.status === 'done_unverified') running += 1
    else if (card.status === 'done') done += 1
    else if (
      card.status === 'blocked' ||
      card.status === 'failed' ||
      card.status === 'crashed' ||
      card.status === 'timed_out'
    )
      blocked += 1
  }
  return { total: children.length, running, done, blocked }
}

export function MissionCoverCard({ cards, restartCount = 0 }: MissionCoverCardProps) {
  const root = useMemo(() => findMissionRoot(cards), [cards])
  const [expanded, setExpanded] = useState(false)

  if (!root) return null

  const now = Date.now()
  const stats = classifyChildren(root, cards)
  const ago = root.createdAt ? formatElapsed(root.createdAt, now) : null
  const previewLength = 220
  const body = root.body ?? ''
  const needsExpand = body.length > previewLength
  const visibleBody = expanded || !needsExpand ? body : `${body.slice(0, previewLength)}…`

  return (
    <section className="mb-3 rounded-xl border border-amber-100/15 bg-[linear-gradient(180deg,rgba(252,243,221,0.10),rgba(34,28,18,0.55))] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_18px_36px_rgba(0,0,0,0.34)] ring-hairline">
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-amber-200/70">
            mission
          </span>
          <h3 className="truncate text-[15px] font-semibold text-fg-0">{root.title}</h3>
        </div>
        <span className="font-mono text-[10px] uppercase tracking-[0.10em] text-fg-3">
          {root.id}
          {ago ? <span className="ml-2 text-fg-3/80">started {ago}</span> : null}
          {restartCount > 0 ? (
            <span className="ml-2 text-amber-200/65">
              · persisted across {restartCount} restart{restartCount === 1 ? '' : 's'}
            </span>
          ) : null}
        </span>
      </div>
      {body && (
        <div className="mt-2 text-[12px] leading-[1.55] text-fg-1">
          <p className="whitespace-pre-wrap">{visibleBody}</p>
          {needsExpand && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 hover:text-fg-0"
            >
              {expanded ? 'collapse' : 'expand'}
            </button>
          )}
        </div>
      )}
      {root.summary && (
        <div className="mt-3 rounded-md bg-black/25 p-2.5 text-[11px] leading-[1.55] text-fg-1 ring-hairline">
          <div className="label mb-1 text-fg-3">parent summary</div>
          {root.summary}
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-2 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2">
        <span className="rounded-full bg-white/[0.04] px-2 py-0.5 ring-hairline">
          {stats.total} {stats.total === 1 ? 'card' : 'cards'}
        </span>
        {stats.running > 0 && (
          <span className="rounded-full bg-accent/[0.10] px-2 py-0.5 text-accent ring-1 ring-accent/20">
            {stats.running} running
          </span>
        )}
        {stats.done > 0 && (
          <span className="rounded-full bg-ok/[0.10] px-2 py-0.5 text-ok ring-1 ring-ok/20">
            {stats.done} done
          </span>
        )}
        {stats.blocked > 0 && (
          <span className="rounded-full bg-warn/[0.10] px-2 py-0.5 text-warn ring-1 ring-warn/20">
            {stats.blocked} blocked
          </span>
        )}
      </div>
    </section>
  )
}
