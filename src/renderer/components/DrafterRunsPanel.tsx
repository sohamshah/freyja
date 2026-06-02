import { useMemo, useState } from 'react'
import { useHarness } from '../state/store'

/**
 * Recent drafter runs — what's being read as input, what got created.
 *
 * Lives below DrafterActivityStrip in the Activity panel. The strip
 * shows the LATEST decision in one line; this panel shows the rolling
 * list with per-run detail (trigger, guidance, status, candidate name,
 * conversation size, loaded skills) so the operator can scrub recent
 * activity without diving into `.events.jsonl`.
 *
 * Click a row to expand its full detail in-line: the in-session loaded
 * skill list, the operator guidance verbatim, and the drafter's
 * rationale text.
 */
export function DrafterRunsPanel() {
  const runs = useHarness((s) => s.drafterRuns ?? [])
  const switchSession = useHarness((s) => s.switchSession)
  const [expanded, setExpanded] = useState<string | null>(null)

  if (runs.length === 0) {
    return null
  }

  return (
    <div className="hairline-b">
      <div className="flex items-center justify-between px-4 py-2 label">
        <span>drafter runs</span>
        <span className="font-mono text-[10px] text-fg-3">{runs.length}</span>
      </div>
      <div className="max-h-[320px] overflow-y-auto">
        {runs.map((r) => {
          const isExpanded = expanded === r.runId
          const status = runStatus(r)
          return (
            <div
              key={r.runId}
              className="hairline-b last:border-b-0 px-3 py-2 text-[11px] hover:bg-white/[0.025]"
            >
              <button
                onClick={() => setExpanded(isExpanded ? null : r.runId)}
                className="flex w-full items-center gap-2 text-left"
              >
                <span className={`shrink-0 rounded px-1.5 py-[1px] font-mono text-[9.5px] uppercase tracking-wider ${triggerTone(r.trigger)}`}>
                  {r.trigger === 'learn_this' ? 'manual' : r.trigger}
                </span>
                <span className={`shrink-0 rounded px-1.5 py-[1px] font-mono text-[9.5px] uppercase tracking-wider ${statusTone(status)}`}>
                  {status}
                </span>
                <span className="truncate text-fg-1">
                  {r.candidateName ? (
                    <>
                      <span className="text-fg-2">→ </span>
                      <span className="text-ok">{r.candidateName}</span>
                    </>
                  ) : r.guidance ? (
                    <span className="text-fg-2">"{truncate(r.guidance, 80)}"</span>
                  ) : (
                    <span className="text-fg-3">no guidance</span>
                  )}
                </span>
                <span className="ml-auto shrink-0 font-mono text-[10px] text-fg-3">
                  {timeAgo(r.startedAt)}
                </span>
              </button>
              {r.subagentSessionId && (
                <button
                  onClick={() => switchSession(r.subagentSessionId!)}
                  className="mt-1 inline-flex items-center gap-1 rounded bg-accent/10 px-1.5 py-[2px] font-mono text-[9.5px] uppercase tracking-wider text-accent ring-1 ring-accent/30 hover:bg-accent/20"
                  title="Open the drafter sub-agent's session — scroll its transcript and keep talking"
                >
                  ↳ open session
                </button>
              )}
              {isExpanded && (
                <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 rounded bg-white/[0.03] px-3 py-2 font-mono text-[10px] text-fg-1 ring-hairline">
                  <DetailRow label="runId" value={r.runId} mono />
                  <DetailRow label="model" value={r.model || '—'} mono />
                  <DetailRow label="started" value={absoluteTime(r.startedAt)} />
                  <DetailRow
                    label="duration"
                    value={r.finishedAt ? `${Math.max(0, r.finishedAt - r.startedAt) / 1000}s` : 'running'}
                  />
                  <DetailRow
                    label="conversation"
                    value={`${formatChars(r.conversationCharCount)} read`}
                  />
                  <DetailRow
                    label="skill library"
                    value={`${r.loadedSkills.length} loaded · ${r.allSkillsCount} total`}
                  />
                  {r.guidance && (
                    <div className="col-span-2 mt-1 border-t border-white/5 pt-1">
                      <div className="text-fg-3">guidance</div>
                      <div className="mt-0.5 whitespace-pre-wrap text-fg-0">{r.guidance}</div>
                    </div>
                  )}
                  {r.loadedSkills.length > 0 && (
                    <div className="col-span-2 mt-1 border-t border-white/5 pt-1">
                      <div className="text-fg-3">loaded skills in session</div>
                      <div className="mt-0.5 flex flex-wrap gap-1">
                        {r.loadedSkills.map((s) => (
                          <span
                            key={s}
                            className="rounded bg-accent/10 px-1.5 py-[1px] text-[9.5px] text-accent ring-1 ring-accent/20"
                          >
                            {s}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                  {r.rationale && (
                    <div className="col-span-2 mt-1 border-t border-white/5 pt-1">
                      <div className="text-fg-3">rationale</div>
                      <div className="mt-0.5 whitespace-pre-wrap text-fg-0">{r.rationale}</div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

type RunStatus = 'running' | 'saved' | 'skipped' | 'discarded' | 'errored'

function runStatus(r: {
  finishedAt?: number
  decision?: string
}): RunStatus {
  if (!r.finishedAt) return 'running'
  switch (r.decision) {
    case 'save':
      return 'saved'
    case 'skip':
      return 'skipped'
    case 'discard':
      return 'discarded'
    case 'error':
      return 'errored'
    default:
      return 'skipped'
  }
}

function statusTone(s: RunStatus): string {
  switch (s) {
    case 'running':
      return 'bg-accent/15 text-accent'
    case 'saved':
      return 'bg-ok/15 text-ok'
    case 'skipped':
      return 'bg-white/[0.06] text-fg-2'
    case 'discarded':
      return 'bg-warn/15 text-warn'
    case 'errored':
      return 'bg-danger/15 text-danger'
  }
}

function triggerTone(t: string): string {
  if (t === 'learn_this') return 'bg-accent/15 text-accent'
  return 'bg-white/[0.04] text-fg-2'
}

function truncate(s: string, n: number) {
  if (s.length <= n) return s
  return s.slice(0, n) + '…'
}

function formatChars(n: number) {
  if (n === 0) return '0 chars'
  if (n < 1000) return `${n} chars`
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k chars`
  return `${(n / 1_000_000).toFixed(2)}M chars`
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

function absoluteTime(ts: number): string {
  const d = new Date(ts)
  return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`
}

function DetailRow({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="shrink-0 text-fg-3">{label}</span>
      <span className={`truncate text-fg-0 ${mono ? 'font-mono' : ''}`}>
        {value}
      </span>
    </div>
  )
}
