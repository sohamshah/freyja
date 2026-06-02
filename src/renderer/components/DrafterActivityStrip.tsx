import { useHarness } from '../state/store'

/**
 * Compact one-line strip that surfaces drafter + cadence telemetry to
 * the operator. Without this, "loop is operating but conservative" is
 * indistinguishable from "loop is broken." The strip is the
 * explicit smoke test required by the design brief.
 *
 * Data sources (both filled by the store reducer):
 *   - ``drafterActivity.lastDecision`` / ``lastRationale`` — from the
 *     ``skill_drafter_pass`` event (H10 fix).
 *   - ``drafterActivity.turnsSinceLastReview`` / ``turnsUntilTrip`` —
 *     from the ``cadence_state`` event the bridge emits per turn.
 *
 * Renders an inert placeholder until at least one of those fields is
 * populated so a brand-new install doesn't show fake numbers.
 */
export function DrafterActivityStrip() {
  const activity = useHarness((s) => s.drafterActivity)
  const runs = useHarness((s) => s.drafterRuns ?? [])

  const decision = activity.lastDecision
  const rationale = activity.lastRationale
  const lastRanAt = activity.lastRanAt
  const turnsUntilTrip = activity.turnsUntilTrip
  const turnsSinceLastReview = activity.turnsSinceLastReview

  // In-flight run takes precedence over "last decision". When a run
  // is mid-LLM-call we want the strip to clearly say "running" so the
  // operator knows work is happening, not that they're stuck on a
  // stale finished verdict.
  const inFlight = runs.find((r) => r.finishedAt === undefined)

  const hasAny =
    decision !== undefined ||
    turnsUntilTrip !== undefined ||
    turnsSinceLastReview !== undefined ||
    inFlight !== undefined

  if (!hasAny) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 hairline-b">
        <span className="label text-fg-3">drafter</span>
        <span className="font-mono text-[10.5px] text-fg-3">idle · awaiting first review pass</span>
      </div>
    )
  }
  if (inFlight) {
    return (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 hairline-b text-[10.5px]">
        <span className="label text-fg-3">drafter</span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
          <span className="font-mono text-accent">running</span>
          <span className="text-fg-3">
            ({inFlight.trigger === 'learn_this' ? 'manual /learn-this' : 'cadence trip'})
          </span>
        </span>
        {inFlight.guidance ? (
          <span className="ml-auto truncate text-fg-2" title={inFlight.guidance}>
            "{inFlight.guidance.length > 60 ? inFlight.guidance.slice(0, 60) + '…' : inFlight.guidance}"
          </span>
        ) : null}
      </div>
    )
  }

  const decisionTone =
    decision === 'save'
      ? 'text-ok'
      : decision === 'error'
        ? 'text-danger'
        : decision === 'discard'
          ? 'text-warn'
          : 'text-fg-2'
  const lastCandidateName = activity.lastCandidateName
  const decisionLabel =
    decision === 'save' && lastCandidateName
      ? `save (${lastCandidateName})`
      : decision

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 hairline-b text-[10.5px]">
      <span className="label text-fg-3">drafter</span>
      {decision ? (
        <span>
          last decision:{' '}
          <span className={`font-mono ${decisionTone}`}>{decisionLabel}</span>
        </span>
      ) : (
        <span className="text-fg-3">no review yet</span>
      )}
      {lastRanAt ? (
        <span className="text-fg-3">
          · {timeAgo(lastRanAt)}
        </span>
      ) : null}
      {turnsUntilTrip !== undefined ? (
        <span className="text-fg-2">
          · next cadence in <span className="font-mono">{Math.max(0, turnsUntilTrip)}</span> turn
          {turnsUntilTrip === 1 ? '' : 's'}
        </span>
      ) : null}
      {turnsSinceLastReview !== undefined ? (
        <span className="text-fg-3">
          · {turnsSinceLastReview} since last
        </span>
      ) : null}
      {rationale ? (
        <span className="ml-auto truncate text-fg-2" title={rationale}>
          {rationale.length > 80 ? rationale.slice(0, 80) + '…' : rationale}
        </span>
      ) : null}
    </div>
  )
}

function timeAgo(ts: number): string {
  const diff = Date.now() - ts
  if (diff < 0) return 'now'
  const secs = Math.floor(diff / 1000)
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}
