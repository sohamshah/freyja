import { useState } from 'react'
import { useHarness } from '../state/store'
import { formatDuration, formatTokens } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { useFrameObjectUrl } from '../lib/frameMedia'

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
  const statusColor = isFailed
    ? 'text-danger'
    : isDone
      ? 'text-ok'
      : isRunning
        ? 'text-accent'
        : 'text-fg-2'
  const statusLabel = isFailed
    ? 'failed'
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
    </div>
  )
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
