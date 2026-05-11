import { useMemo, useState } from 'react'
import { useHarness, type SystemEventRecord } from '../state/store'
import { formatDuration, formatTokens } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { useFrameObjectUrl } from '../lib/frameMedia'

/** Snapshot of a kanban card's current state, derived from the running
 *  fold over `kanban_*` system events. Used by the subagent card to
 *  decide whether to show "awaiting review" / "verified" / "rejected"
 *  states instead of the bare worker `done`. */
interface KanbanCardSnapshot {
  status: string
  requiresVerification: boolean
  /** When the latest event was a verifier-authored transition, the
   *  actor string. Used to render the verifier byline. */
  verifierActor: string | null
  /** True when the latest verifier transition flipped the card from
   *  `done_unverified` back to `running`. */
  rejected: boolean
  /** Verifier's most recent rejection feedback, if any. */
  rejectionFeedback: string | null
}

function deriveKanbanCardSnapshot(
  events: SystemEventRecord[],
  taskId: string,
): KanbanCardSnapshot | null {
  let task: Record<string, unknown> | null = null
  let verifierActor: string | null = null
  let rejected = false
  let rejectionFeedback: string | null = null
  for (const event of events) {
    if (!event.subtype.startsWith('kanban_')) continue
    const details = event.details as Record<string, unknown> | undefined
    const eventTask = details?.task as Record<string, unknown> | undefined
    if (!eventTask) continue
    if (String(eventTask.id ?? '') !== taskId) continue
    task = eventTask
    // The verifier writes `update` with a verifier-ish actor. We track
    // the most recent one so the byline + rejection callout reflect
    // the latest verifier touch, not an older one.
    const eventsList = Array.isArray(eventTask.events) ? eventTask.events : []
    for (const inner of eventsList.slice().reverse()) {
      if (!inner || typeof inner !== 'object') continue
      const actor = String((inner as Record<string, unknown>).actor ?? '').toLowerCase()
      if (!actor.includes('verify')) continue
      const innerDetails = (inner as Record<string, unknown>).details as
        | Record<string, unknown>
        | undefined
      const innerStatus = String(innerDetails?.status ?? '').toLowerCase()
      verifierActor = String((inner as Record<string, unknown>).actor)
      rejected = innerStatus === 'running'
      break
    }
    if (rejected) {
      // Find the comment the verifier left when bouncing the card.
      const comments = Array.isArray(eventTask.comments) ? eventTask.comments : []
      for (const c of comments.slice().reverse()) {
        if (!c || typeof c !== 'object') continue
        const author = String((c as Record<string, unknown>).author ?? '').toLowerCase()
        if (!author.includes('verify')) continue
        rejectionFeedback = String((c as Record<string, unknown>).body ?? '') || null
        break
      }
    }
  }
  if (!task) return null
  return {
    status: String(task.status ?? 'ready'),
    requiresVerification: Boolean(task.requiresVerification),
    verifierActor,
    rejected,
    rejectionFeedback,
  }
}

function useKanbanCardSnapshot(taskId: string | undefined): KanbanCardSnapshot | null {
  const systemEvents = useHarness((s) => s.systemEvents)
  return useMemo(() => {
    if (!taskId) return null
    return deriveKanbanCardSnapshot(systemEvents, taskId)
  }, [systemEvents, taskId])
}

/**
 * Inline subagent card shown in the parent conversation. Slate-inspired:
 * the entire card is a clickable "attach" affordance that switches the
 * active session to the child. Also shows live progress blocks for
 * running subagents and a rolled-up tool count / token tally.
 */
export function SubagentCard({ id }: { id: string }) {
  const sub = useHarness((s) => s.subagents[id])
  const childSnapshot = useHarness((s) =>
    s.sessions.find((session) => session.id === id),
  )
  const openSessionPane = useHarness((s) => s.openSessionPane)
  const computerSession = useHarness((s) => s.computerSessions[id])
  const showInline = useHarness(
    (s) => s.settings.computer.showScreenshotsInline,
  )
  const latestFrameUrl = useFrameObjectUrl(computerSession?.latestFrame)
  // Screenshot thumbnail collapses by default once a computer session
  // completes — viewer can re-expand to review the final state.
  const [expanded, setExpanded] = useState(true)
  if (!sub) return null

  // Prefer the (more authoritative) session snapshot state when present.
  const isRunning = childSnapshot
    ? !childSnapshot.completed
    : sub.state === 'running'
  const isDone = childSnapshot?.completed ?? sub.state === 'done'
  const isFailed = childSnapshot?.success === false && childSnapshot.completed
  // When the subagent is bound to a kanban card with verification in
  // play, the worker's run finishing isn't the same as the card being
  // sealed. Read the latest card snapshot off system events and let
  // it override the worker's bare `done` so the subagent card reflects
  // the verifier lifecycle in-place — no new card, no shouty pill.
  const isVerifierAgent = sub.agentType === 'verify'
  const cardSnapshot = useKanbanCardSnapshot(
    !isVerifierAgent ? sub.kanbanTaskId : undefined,
  )
  const awaitingReview =
    isDone && cardSnapshot?.status === 'done_unverified'
  const verified =
    isDone && cardSnapshot?.status === 'done' && cardSnapshot.verifierActor !== null
  const rejected = Boolean(cardSnapshot?.rejected)
  const statusColor = isFailed
    ? 'text-danger'
    : rejected
      ? 'text-danger'
      : awaitingReview
        ? 'text-warn'
        : isDone
          ? 'text-ok'
          : isRunning
            ? 'text-accent'
            : 'text-fg-2'
  const statusLabel = isFailed
    ? 'failed'
    : rejected
      ? 'rejected'
      : awaitingReview
        ? 'awaiting review'
        : verified
          ? 'verified'
          : isDone
            ? 'done'
            : isRunning
              ? 'running'
              : sub.state

  const handleAttach = (mode: 'replace' | 'split' = 'replace') => {
    if (childSnapshot) openSessionPane(id, mode)
  }

  return (
    <div
      onClick={(event) => handleAttach(event.metaKey || event.ctrlKey ? 'split' : 'replace')}
      className={`group rounded-xl glass-raised p-3 transition-all ${
        childSnapshot
          ? 'cursor-pointer hover:ring-1 hover:ring-accent/40 hover:shadow-glow-accent'
          : ''
      }`}
      title={childSnapshot ? 'Click to attach (⌘O to open swarm)' : undefined}
    >
      <div className="mb-2 flex items-start gap-2.5">
        <div className="mt-[2px] flex h-4 w-5 items-center justify-center">
          {isRunning ? (
            <Spinner name="scan" className="text-accent" />
          ) : (
            <span
              className={`block h-1.5 w-1.5 rounded-full ${statusColor.replace('text-', 'bg-')}`}
            />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="label text-fg-2">sub-agent</span>
            {sub.agentType && sub.agentType !== 'general' && (
              <AgentTypeTag type={sub.agentType} />
            )}
            <span className="font-mono text-[10px] text-fg-2">{sub.mode}</span>
            <span className={`font-mono text-[10px] uppercase ${statusColor}`}>
              {statusLabel}
            </span>
            {childSnapshot && isRunning && (
              <span className="label ml-auto text-accent/70">open ↵</span>
            )}
          </div>
          <div className="mt-[2px] truncate text-[12px] text-fg-0">
            {sub.label}
          </div>
          <div className="mt-[3px] line-clamp-2 text-[11px] leading-[1.5] text-fg-1">
            {sub.task}
          </div>
        </div>
      </div>
      <div className="hairline-t flex items-center gap-4 pt-2 text-[10px] text-fg-2">
        <Stat label="elapsed" value={formatDuration(sub.elapsedMs)} />
        <Stat label="in" value={formatTokens(sub.tokensIn)} />
        <Stat label="out" value={formatTokens(sub.tokensOut)} />
        <Stat label="tools" value={String(sub.toolsCalled)} />
        {childSnapshot && (
          <span className="ml-auto flex items-center gap-1 text-[10px] text-accent/80">
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                handleAttach('split')
              }}
              className="rounded px-1.5 py-[2px] font-mono text-[9px] uppercase text-fg-2 ring-hairline hover:bg-white/[0.06] hover:text-fg-0"
            >
              split
            </button>
            <span className="font-mono">open ▸</span>
          </span>
        )}
      </div>
      {computerSession && showInline && (
        <div className="hairline-t mt-2 pt-2">
          <div className="mb-1 flex items-center justify-between">
            <div className="label flex items-center gap-1.5">
              <span className="font-mono text-[9px] uppercase tracking-[0.08em] text-warn">
                computer
              </span>
              {computerSession.status === 'running' && (
                <Spinner name="scan" className="text-warn" />
              )}
              <span className="font-mono text-[10px] text-fg-2">
                {computerSession.history.length} action
                {computerSession.history.length === 1 ? '' : 's'}
              </span>
              {/* Diagnostic: frame counter so we can see if screenshots
                  are actually streaming into the store. */}
              <span className="font-mono text-[10px] text-fg-3">
                · {computerSession.frameCount} frame
                {computerSession.frameCount === 1 ? '' : 's'}
              </span>
              {computerSession.latestFrame && (
                <span className="font-mono text-[9px] text-fg-3">
                  · {formatAge(computerSession.latestFrame.takenAt)} ago
                </span>
              )}
            </div>
            <button
              onClick={(e) => {
                e.stopPropagation()
                setExpanded((v) => !v)
              }}
              className="rounded bg-white/[0.04] px-1.5 py-[1px] font-mono text-[9px] uppercase text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-1"
            >
              {expanded ? 'collapse' : 'expand'}
            </button>
          </div>
          {expanded && computerSession.latestFrame && latestFrameUrl && (
            <div className="relative overflow-hidden rounded-md bg-black ring-hairline">
              <img
                src={latestFrameUrl}
                alt="live screen"
                className="block max-h-[240px] w-full object-contain"
                loading="lazy"
                decoding="async"
                draggable={false}
              />
              {computerSession.plannedAction?.x !== undefined &&
                computerSession.plannedAction?.y !== undefined && (
                  <div
                    className="pointer-events-none absolute"
                    style={{
                      left: `${
                        (computerSession.plannedAction.x /
                          computerSession.latestFrame.width) *
                        100
                      }%`,
                      top: `${
                        (computerSession.plannedAction.y /
                          computerSession.latestFrame.height) *
                        100
                      }%`,
                      transform: 'translate(-50%, -50%)',
                    }}
                  >
                    <div
                      className="h-5 w-5 rounded-full border-2"
                      style={{
                        borderColor: '#f5b640',
                        boxShadow:
                          '0 0 0 2px rgba(245,182,64,0.18), 0 0 14px rgba(245,182,64,0.6)',
                      }}
                    />
                  </div>
                )}
            </div>
          )}
          {computerSession.plannedAction && (
            <div className="mt-1.5 font-mono text-[10px] text-fg-2">
              → {computerSession.plannedAction.description ?? computerSession.plannedAction.action}
            </div>
          )}
        </div>
      )}
      {sub.result && isDone && (
        <div className="hairline-t mt-2 pt-2">
          <div className="mb-1 label">result</div>
          <div className="selectable line-clamp-3 text-[11px] leading-[1.55] text-fg-1">
            {sub.result}
          </div>
        </div>
      )}
      {/* Verifier byline. Sits where a co-author credit would sit on a
          print piece — small, italic, second-author voice. Only renders
          once verification has actually happened on this card. */}
      {(verified || rejected) && cardSnapshot?.verifierActor && (
        <div className="mt-2 font-mono text-[10px] italic text-fg-3">
          verifier · {cardSnapshot.verifierActor}
        </div>
      )}
      {/* Rejection feedback callout. Quoted in the same column as the
          worker's body so reading it feels like turning the page and
          seeing the editor's notes. */}
      {rejected && cardSnapshot?.rejectionFeedback && (
        <div className="mt-2 rounded-md border-l-2 border-danger/55 bg-danger/[0.06] px-3 py-2 text-[11px] leading-[1.55] text-fg-1">
          <div className="mb-1 font-mono text-[9.5px] uppercase tracking-[0.08em] text-danger/85">
            rejected — verifier note
          </div>
          <div className="whitespace-pre-wrap">{cardSnapshot.rejectionFeedback}</div>
        </div>
      )}
      {/* Phase chain — verifier and any subsequent worker re-spawns on
          the same kanban card render beneath this worker as continuation
          blocks. Lets the page grow downward as the card's lifecycle
          plays out, rather than spawning new grid cards. */}
      {!isVerifierAgent && sub.kanbanTaskId && (
        <SubagentPhaseChain
          workerId={id}
          workerStartedAt={sub.startedAt}
          taskId={sub.kanbanTaskId}
        />
      )}
    </div>
  )
}

/** Renders verifier + re-worker phases nested under the original worker
 *  for cards under verification. Each phase is a compact in-place
 *  continuation, not a separate card — visually the worker's card
 *  grows downward as the verification lifecycle plays out. */
function SubagentPhaseChain({
  workerId,
  workerStartedAt,
  taskId,
}: {
  workerId: string
  workerStartedAt: number
  taskId: string
}) {
  const phases = useHarness((s) => {
    const list: typeof s.subagents[string][] = []
    for (const id in s.subagents) {
      if (id === workerId) continue
      const rec = s.subagents[id]
      if (!rec || rec.kanbanTaskId !== taskId) continue
      if (rec.startedAt < workerStartedAt) continue
      list.push(rec)
    }
    return list.sort((a, b) => a.startedAt - b.startedAt)
  })
  if (phases.length === 0) return null
  let workerRound = 1
  return (
    <div className="mt-3 space-y-3">
      {phases.map((phase) => {
        const isVerifier = phase.agentType === 'verify'
        const heading = isVerifier
          ? `verifier · ${phase.label}`
          : `worker round ${++workerRound} · ${phase.label}`
        return (
          <PhaseBlock key={phase.id} subagentId={phase.id} heading={heading} isVerifier={isVerifier} />
        )
      })}
    </div>
  )
}

function PhaseBlock({
  subagentId,
  heading,
  isVerifier,
}: {
  subagentId: string
  heading: string
  isVerifier: boolean
}) {
  const sub = useHarness((s) => s.subagents[subagentId])
  const childSlice = useHarness((s) => s.sessionArchive[subagentId])
  const openSessionPane = useHarness((s) => s.openSessionPane)
  const childSnapshot = useHarness((s) =>
    s.sessions.find((session) => session.id === subagentId),
  )
  if (!sub) return null

  const recentTools = (childSlice?.toolCallOrder ?? [])
    .slice(-5)
    .map((tcId) => childSlice?.toolCalls[tcId])
    .filter(Boolean)

  const isRunning = sub.state === 'running' || sub.state === 'pending'
  const isDone = sub.state === 'done'
  const isFailed = sub.state === 'failed' || sub.state === 'cancelled'
  const dotClass = isFailed
    ? 'bg-danger'
    : isRunning
      ? 'bg-accent'
      : isVerifier && isDone
        ? 'bg-ok'
        : 'bg-fg-2'

  const handleAttach = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (childSnapshot) openSessionPane(subagentId, e.metaKey || e.ctrlKey ? 'split' : 'replace')
  }

  return (
    <div className="hairline-t pt-3">
      <div className="mb-1.5 flex items-baseline gap-2">
        {isRunning ? (
          <Spinner name="scan" className="text-accent" />
        ) : (
          <span className={`mt-1 inline-block h-1.5 w-1.5 rounded-full ${dotClass}`} />
        )}
        <span className="font-mono text-[11px] italic text-fg-2">{heading}</span>
        <span className="ml-auto font-mono text-[9.5px] uppercase tracking-[0.08em] text-fg-3">
          {sub.state}
        </span>
        {childSnapshot && (
          <button
            type="button"
            onClick={handleAttach}
            className="rounded px-1.5 py-[1px] font-mono text-[9px] uppercase tracking-[0.06em] text-fg-3 ring-hairline hover:bg-white/[0.04] hover:text-fg-1"
          >
            open
          </button>
        )}
      </div>
      <div className="line-clamp-2 text-[11px] leading-[1.45] text-fg-1">{sub.task}</div>
      {recentTools.length > 0 && (
        <ul className="mt-1.5 space-y-[2px] font-mono text-[10.5px] leading-[1.45] text-fg-2">
          {recentTools.map((tc, i) => {
            if (!tc) return null
            const isLast = i === recentTools.length - 1
            return (
              <li key={tc.id} className="flex items-start gap-1.5">
                <span className="shrink-0 text-fg-3">{isLast ? '└' : '├'}</span>
                <span className="truncate text-fg-1">{summarizePhaseTool(tc)}</span>
              </li>
            )
          })}
        </ul>
      )}
      <div className="mt-1.5 flex items-center gap-3 font-mono text-[9.5px] text-fg-3">
        <span>{formatDuration(sub.elapsedMs)}</span>
        <span>·</span>
        <span>{formatTokens(sub.tokensIn + sub.tokensOut)}</span>
        <span>·</span>
        <span>{sub.toolsCalled} tools</span>
      </div>
    </div>
  )
}

function summarizePhaseTool(tc: { name: string; arguments?: Record<string, unknown> }): string {
  const name = tc.name
  const args = (tc.arguments ?? {}) as Record<string, unknown>
  if (name === 'kanban') {
    const action = String(args.action ?? '')
    const taskId = String(args.task_id ?? '')
    const status = String(args.status ?? '')
    if (action === 'update' && status) return `kanban update → ${status}`
    if (action) return `kanban ${action}${taskId ? ` ${taskId}` : ''}`
    return 'kanban'
  }
  if (name === 'bash') {
    const cmd = String(args.command ?? '').split('\n')[0]
    return cmd ? `$ ${cmd.slice(0, 48)}` : 'bash'
  }
  if (name === 'read_file' || name === 'read') {
    return `read ${String(args.path ?? args.file_path ?? '').slice(0, 48)}`
  }
  if (name === 'grep') return `grep ${String(args.pattern ?? args.query ?? '').slice(0, 36)}`
  return name
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="uppercase tracking-[0.08em] text-fg-3">{label}</span>
      <span className="font-mono text-fg-1">{value}</span>
    </span>
  )
}

const AGENT_TYPE_COLORS: Record<string, string> = {
  general: 'bg-sky-500/15 text-sky-300 ring-sky-500/30',
  explore: 'bg-blue-500/15 text-blue-400 ring-blue-500/30',
  'explore-fast': 'bg-cyan-500/15 text-cyan-400 ring-cyan-500/30',
  code: 'bg-amber-500/15 text-amber-400 ring-amber-500/30',
  verify: 'bg-emerald-500/15 text-emerald-400 ring-emerald-500/30',
  plan: 'bg-violet-500/15 text-violet-300 ring-violet-500/30',
  review: 'bg-pink-500/15 text-pink-300 ring-pink-500/30',
  test: 'bg-orange-500/15 text-orange-300 ring-orange-500/30',
  'browser-qa': 'bg-indigo-500/15 text-indigo-300 ring-indigo-500/30',
  performance: 'bg-red-500/15 text-red-300 ring-red-500/30',
  docs: 'bg-teal-500/15 text-teal-300 ring-teal-500/30',
  'memory-curator': 'bg-lime-500/15 text-lime-300 ring-lime-500/30',
  computer: 'bg-fuchsia-500/15 text-fuchsia-300 ring-fuchsia-500/30',
}

export function AgentTypeTag({ type }: { type: string }) {
  const colors = AGENT_TYPE_COLORS[type] ?? 'bg-white/[0.06] text-fg-2 ring-white/[0.08]'
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-[1px] font-mono text-[9px] uppercase tracking-[0.06em] ring-1 ${colors}`}
    >
      {type}
    </span>
  )
}

function formatAge(at: number): string {
  const diff = Math.max(0, Date.now() - at)
  if (diff < 1000) return `${diff}ms`
  if (diff < 60_000) return `${(diff / 1000).toFixed(1)}s`
  return `${Math.floor(diff / 60_000)}m`
}
