import { useMemo } from 'react'
import type { SystemEventRecord } from '../../state/store'

const DISPATCH_EVENT_SUBTYPES = new Set([
  'kanban_dispatched',
  'kanban_reclaimed',
  'kanban_stale',
  'kanban_autopilot_enabled',
  'kanban_autopilot_disabled',
  'kanban_orphan',
])

const GLYPH: Record<string, string> = {
  kanban_dispatched: '↗',
  kanban_reclaimed: '⟲',
  kanban_stale: 'ⓘ',
  kanban_autopilot_enabled: '▶',
  kanban_autopilot_disabled: '■',
  kanban_orphan: '·',
}

const TONE: Record<string, string> = {
  kanban_dispatched: 'text-accent',
  kanban_reclaimed: 'text-warn',
  kanban_stale: 'text-warn',
  kanban_autopilot_enabled: 'text-ok',
  kanban_autopilot_disabled: 'text-fg-2',
  kanban_orphan: 'text-fg-3',
}

function formatTime(at: number): string {
  const d = new Date(at)
  return `${d.getHours().toString().padStart(2, '0')}:${d
    .getMinutes()
    .toString()
    .padStart(2, '0')}`
}

function formatEntry(event: SystemEventRecord): string {
  const details = (event.details ?? {}) as Record<string, unknown>
  switch (event.subtype) {
    case 'kanban_dispatched': {
      const card = details.cardId ? String(details.cardId) : '?'
      const agent = details.agentType ? String(details.agentType) : 'worker'
      const lane = details.lane ? String(details.lane) : ''
      return `dispatched ${agent} on ${card}${lane ? ` (${lane} lane)` : ''}`
    }
    case 'kanban_reclaimed': {
      const card = details.cardId ? String(details.cardId) : '?'
      const age = typeof details.ageSeconds === 'number' ? Math.floor(details.ageSeconds) : null
      const suffix = age !== null ? ` — heartbeat stale ${age}s` : ''
      return `reclaimed ${card}${suffix}`
    }
    case 'kanban_stale': {
      const card = details.cardId ? String(details.cardId) : '?'
      const age = typeof details.ageSeconds === 'number' ? Math.floor(details.ageSeconds) : null
      const suffix = age !== null ? ` — ${age}s without activity` : ''
      return `stale ${card}${suffix}`
    }
    case 'kanban_autopilot_enabled':
      return 'autopilot enabled'
    case 'kanban_autopilot_disabled':
      return 'autopilot disabled'
    case 'kanban_orphan': {
      const card = details.cardId ? String(details.cardId) : '?'
      return `orphan ${card}`
    }
    default:
      return event.message
  }
}

export function KanbanDispatchTicker({
  systemEvents,
  limit = 8,
}: {
  systemEvents: SystemEventRecord[]
  limit?: number
}) {
  const entries = useMemo(() => {
    const matching: SystemEventRecord[] = []
    // Walk newest-first by collecting then reversing. Cheaper than
    // sorting the whole telemetry list.
    for (let i = systemEvents.length - 1; i >= 0 && matching.length < limit; i--) {
      const event = systemEvents[i]
      if (DISPATCH_EVENT_SUBTYPES.has(event.subtype)) {
        matching.push(event)
      }
    }
    return matching
  }, [systemEvents, limit])

  return (
    <div className="flex min-h-0 flex-1 flex-col rounded-lg bg-black/20 p-3 ring-hairline">
      <div className="label mb-2 flex items-center justify-between">
        <span>dispatcher pulse</span>
        <span className="font-mono text-[9px] text-fg-3">{entries.length} recent</span>
      </div>
      {entries.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-[11px] text-fg-3">
          No dispatcher activity yet.
        </div>
      ) : (
        <ul className="flex flex-col gap-1.5 overflow-y-auto pr-1">
          {entries.map((event) => (
            <li
              key={event.id}
              className="flex items-start gap-2 font-mono text-[11px] leading-snug text-fg-1"
            >
              <span className="shrink-0 text-[10px] text-fg-3">
                {formatTime(event.at)}
              </span>
              <span className={`shrink-0 ${TONE[event.subtype] ?? 'text-fg-2'}`}>
                {GLYPH[event.subtype] ?? '·'}
              </span>
              <span className="min-w-0 truncate text-fg-1">{formatEntry(event)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
