import { useMemo } from 'react'

interface CardLike {
  id: string
  title: string
  status: string
  assignee?: string
  consecutiveFailures?: number
  updatedAt?: number
}

interface WatchEntry {
  card: CardLike
  reason: string
  tone: 'danger' | 'warn' | 'info'
  urgency: number
}

// Matches bridge constants. Keep in sync.
const STALE_SECONDS = 180

function classifyForWatchList(cards: CardLike[], now: number): WatchEntry[] {
  const entries: WatchEntry[] = []
  for (const card of cards) {
    const failures = card.consecutiveFailures ?? 0
    if (card.status === 'failed') {
      entries.push({
        card,
        reason: 'circuit broken — dispatcher locked out',
        tone: 'danger',
        urgency: 100,
      })
      continue
    }
    if (card.status === 'blocked') {
      entries.push({
        card,
        reason: 'blocked — needs user input',
        tone: 'warn',
        urgency: 80,
      })
      continue
    }
    if (card.status === 'running' && failures >= 2) {
      entries.push({
        card,
        reason: `${failures}/3 failures — one more trips the breaker`,
        tone: 'danger',
        urgency: 90,
      })
      continue
    }
    if (card.status === 'running' && card.updatedAt) {
      const age = Math.floor((now - card.updatedAt) / 1000)
      if (age >= STALE_SECONDS) {
        entries.push({
          card,
          reason: `${Math.floor(age / 60)}m without heartbeat`,
          tone: 'warn',
          urgency: 70 - Math.min(20, Math.floor(age / 60)),
        })
      }
      continue
    }
    if (card.status === 'crashed' || card.status === 'timed_out') {
      entries.push({
        card,
        reason: `${card.status} — retry-eligible (${failures}/3 failures)`,
        tone: 'warn',
        urgency: 60,
      })
      continue
    }
  }
  return entries.sort((a, b) => b.urgency - a.urgency)
}

export function KanbanWatchList({
  cards,
  onSelect,
}: {
  cards: CardLike[]
  onSelect?: (id: string) => void
}) {
  const now = Date.now()
  const entries = useMemo(() => classifyForWatchList(cards, now), [cards, now])
  return (
    <div className="flex min-h-0 flex-col rounded-lg bg-black/20 p-3 ring-hairline">
      <div className="label mb-2 flex items-center justify-between">
        <span>watch list</span>
        <span className="font-mono text-[9px] text-fg-3">{entries.length} need attention</span>
      </div>
      {entries.length === 0 ? (
        <div className="flex flex-1 items-center justify-center py-3 text-[11px] text-fg-3">
          Nothing demanding attention.
        </div>
      ) : (
        <ul className="flex flex-col gap-1.5 overflow-y-auto pr-1">
          {entries.map((entry) => (
            <li key={entry.card.id}>
              <button
                type="button"
                onClick={() => onSelect?.(entry.card.id)}
                className="flex w-full items-start gap-2 rounded bg-white/[0.02] p-2 text-left ring-hairline transition hover:bg-white/[0.05]"
              >
                <span
                  className={`mt-0.5 inline-block h-2 w-2 shrink-0 rounded-full ${
                    entry.tone === 'danger'
                      ? 'bg-danger'
                      : entry.tone === 'warn'
                        ? 'bg-warn'
                        : 'bg-accent'
                  }`}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 font-mono text-[10px] text-fg-2">
                    <span className="text-accent">{entry.card.id}</span>
                    <span className="uppercase">{entry.card.status}</span>
                  </div>
                  <div className="mt-0.5 truncate text-[11px] text-fg-0">
                    {entry.card.title}
                  </div>
                  <div
                    className={`mt-0.5 truncate font-mono text-[9.5px] ${
                      entry.tone === 'danger'
                        ? 'text-danger'
                        : entry.tone === 'warn'
                          ? 'text-warn'
                          : 'text-fg-3'
                    }`}
                  >
                    {entry.reason}
                  </div>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
