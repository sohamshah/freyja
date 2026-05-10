import { useEffect, useMemo, useState } from 'react'
import type { SystemEventRecord } from '../../state/store'

const TICK_INTERVAL_FALLBACK_MS = 30_000
const MAX_PARALLEL_FALLBACK = 3

interface AutopilotState {
  enabled: boolean
  enabledAt: number | null
  lastTickAt: number | null
  tickIntervalMs: number
  maxParallel: number
}

function deriveAutopilotState(events: SystemEventRecord[]): AutopilotState {
  let enabled = false
  let enabledAt: number | null = null
  let lastTickAt: number | null = null
  let tickIntervalMs = TICK_INTERVAL_FALLBACK_MS
  for (const event of events) {
    if (event.subtype === 'kanban_autopilot_enabled') {
      enabled = true
      enabledAt = event.at
    } else if (event.subtype === 'kanban_autopilot_disabled') {
      enabled = false
      enabledAt = null
    } else if (event.subtype === 'kanban_tick') {
      lastTickAt = event.at
      const interval = (event.details as Record<string, unknown> | undefined)?.intervalSeconds
      if (typeof interval === 'number' && interval > 0) {
        tickIntervalMs = interval * 1000
      }
    }
  }
  return {
    enabled,
    enabledAt,
    lastTickAt,
    tickIntervalMs,
    maxParallel: MAX_PARALLEL_FALLBACK,
  }
}

function formatCountdown(nextTickAt: number, now: number): string {
  const remaining = Math.max(0, nextTickAt - now)
  const seconds = Math.ceil(remaining / 1000)
  if (seconds <= 0) return 'now'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`
}

function formatElapsed(since: number, now: number): string {
  const ms = Math.max(0, now - since)
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`
}

export function KanbanAutopilotStrip({
  sessionId,
  systemEvents,
  runningAgents,
}: {
  sessionId: string
  systemEvents: SystemEventRecord[]
  runningAgents: number
}) {
  // Walk all system events for autopilot state. Cheap on a per-render
  // basis since the array is already memoised upstream and the loop is
  // a few constant-time comparisons.
  const state = useMemo(() => deriveAutopilotState(systemEvents), [systemEvents])
  // Tick the countdown every second by triggering a re-render. We don't
  // store the time in state because we want the countdown text to be
  // derived from `Date.now()`, not lag behind a stale state snapshot.
  const [, setNowTick] = useState(0)
  useEffect(() => {
    if (!state.enabled) return
    const handle = window.setInterval(() => setNowTick((n) => n + 1), 1000)
    return () => window.clearInterval(handle)
  }, [state.enabled])

  const now = Date.now()
  const slotsUsed = Math.min(runningAgents, state.maxParallel)

  const toggle = () => {
    const api = (window as any).harness
    if (!api?.sendCommand) return
    api.sendCommand({
      type: 'kanban_autopilot',
      sessionId,
      enabled: !state.enabled,
    })
  }

  if (!state.enabled) {
    return (
      <div className="flex items-center justify-between rounded-md bg-white/[0.025] px-3 py-2 ring-hairline">
        <div className="flex items-center gap-3">
          <span className="inline-block h-2 w-2 rounded-full bg-fg-3/55" aria-hidden />
          <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-fg-2">
            autopilot off
          </span>
          <span className="text-[11px] text-fg-3">manual mode — parent dispatches by hand</span>
        </div>
        <button
          type="button"
          onClick={toggle}
          className="rounded-md bg-white/[0.05] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline transition hover:bg-white/[0.08]"
        >
          enable
        </button>
      </div>
    )
  }

  // Estimate next tick. Without a recent kanban_tick to anchor, fall back
  // to "soon" — the bridge ticks at a fixed cadence, but the renderer
  // hasn't seen one yet.
  const nextTickAt =
    state.lastTickAt !== null
      ? state.lastTickAt + state.tickIntervalMs
      : null
  const countdown = nextTickAt !== null ? formatCountdown(nextTickAt, now) : '~30s'
  const since = state.enabledAt !== null ? formatElapsed(state.enabledAt, now) : null

  return (
    <div className="flex items-center justify-between rounded-md bg-accent/[0.06] px-3 py-2 ring-1 ring-accent/15">
      <div className="flex items-center gap-3">
        <span className="relative inline-flex h-2 w-2" aria-hidden>
          <span className="absolute inset-0 animate-ping rounded-full bg-accent/60" />
          <span className="relative inline-block h-2 w-2 rounded-full bg-accent" />
        </span>
        <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-accent">
          autopilot on
        </span>
        <span className="font-mono text-[11px] text-fg-1">
          {slotsUsed}/{state.maxParallel} slots
        </span>
        <span className="font-mono text-[11px] text-fg-2">
          next tick {countdown}
        </span>
        {since && (
          <span className="font-mono text-[10px] text-fg-3">running {since}</span>
        )}
      </div>
      <button
        type="button"
        onClick={toggle}
        className="rounded-md bg-white/[0.05] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline transition hover:bg-white/[0.08]"
      >
        pause
      </button>
    </div>
  )
}
