import { useEffect, useState } from 'react'
import { useHarness } from '../state/store'
import type { PermissionTier } from '@shared/events'

const LEVEL_TONE: Record<string, { label: string; color: string; icon: string }> = {
  low: { label: 'LOW RISK', color: 'text-ok', icon: '●' },
  medium: { label: 'MEDIUM RISK', color: 'text-warn', icon: '⚡' },
  high: { label: 'HIGH RISK', color: 'text-warn', icon: '⚠' },
  dangerous: { label: 'DANGEROUS', color: 'text-danger', icon: '✕' },
  info: { label: 'INFO', color: 'text-fg-2', icon: '·' },
}

/**
 * Map a request level to the session-scoped auto-approve tier it would
 * enable. Approving a HIGH request with "remember" selected escalates to
 * `high` (which covers LOW/MEDIUM/HIGH); DANGEROUS escalates to `yolo`.
 */
function tierForLevel(level: string): PermissionTier {
  switch (level) {
    case 'low':
      return 'low'
    case 'medium':
      return 'medium'
    case 'high':
      return 'high'
    case 'dangerous':
      return 'yolo'
    default:
      return 'low'
  }
}

/**
 * Modal shown when the bridge asks the user to approve a tool call. Stacks
 * multiple requests — the topmost one in the queue is displayed. `Enter`
 * approves, `Esc` denies. Dangerous actions require a click (no keyboard
 * shortcut) to approve.
 */
export function PermissionPrompt() {
  const queue = useHarness((s) => s.permissionQueue)
  const answer = useHarness((s) => s.answerPermission)
  const escalate = useHarness((s) => s.escalateSessionPolicy)
  const toggleSettings = useHarness((s) => s.toggleSettings)
  const current = queue[0]
  const [rememberForSession, setRememberForSession] = useState(false)

  // Reset the toggle whenever we show a new request.
  useEffect(() => {
    if (current) setRememberForSession(false)
  }, [current?.requestId])

  const approve = () => {
    if (!current) return
    if (rememberForSession) {
      escalate(tierForLevel(current.level))
    }
    answer(current.requestId, true)
  }

  const deny = () => {
    if (!current) return
    answer(current.requestId, false)
  }

  useEffect(() => {
    if (!current) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Enter' && current.level !== 'dangerous') {
        e.preventDefault()
        approve()
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        deny()
        return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // Intentionally excluding approve/deny to keep the handler stable across
    // renders — they read fresh state via closures anyway.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, rememberForSession])

  if (!current) return null

  const tone = LEVEL_TONE[current.level] ?? LEVEL_TONE.medium

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[14vh]">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-[2px]" />
      <div className="relative w-[560px] overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong">
        <div className="flex items-center gap-3 px-5 py-4 hairline-b">
          <span className={`font-mono text-[15px] ${tone.color}`}>{tone.icon}</span>
          <span className={`label ${tone.color}`}>{tone.label}</span>
          <span className="label ml-auto text-fg-2">permission required</span>
        </div>
        <div className="px-5 py-5">
          <div className="mb-4">
            <div className="mb-1 label">action</div>
            <div className="selectable rounded-md bg-black/45 p-3 font-mono text-[12px] leading-[1.5] text-fg-0 ring-hairline">
              {current.prompt}
            </div>
          </div>
          {current.reason && (
            <div className="mb-4">
              <div className="mb-1 label">reason</div>
              <div className="text-[12px] leading-[1.55] text-fg-1">{current.reason}</div>
            </div>
          )}
          {current.details && (
            <div className="mb-4">
              <div className="mb-1 label">details</div>
              <div className="selectable rounded-md bg-black/35 p-2.5 font-mono text-[11px] leading-[1.55] text-fg-1 ring-hairline whitespace-pre-wrap">
                {current.details}
              </div>
            </div>
          )}
          {queue.length > 1 && (
            <div className="mb-4 text-[10.5px] text-fg-3">
              {queue.length - 1} more pending…
            </div>
          )}
          <label
            className={`mt-4 flex cursor-pointer items-start gap-2 rounded-md px-2.5 py-2 transition-colors ${
              rememberForSession
                ? 'bg-accent/10 ring-1 ring-accent/25'
                : 'bg-white/[0.025] ring-hairline hover:bg-white/[0.04]'
            }`}
          >
            <input
              type="checkbox"
              checked={rememberForSession}
              onChange={(e) => setRememberForSession(e.target.checked)}
              className="mt-[3px] h-3 w-3 shrink-0 accent-accent"
            />
            <div className="min-w-0 flex-1">
              <div className="font-mono text-[11px] text-fg-0">
                remember for this session
              </div>
              <div className="mt-0.5 text-[10.5px] leading-[1.45] text-fg-2">
                Allow this + any lower-risk tool in the current session without
                prompting again. Does not touch global settings.
              </div>
            </div>
          </label>

          <div className="mt-4 flex items-center justify-between gap-2">
            <button
              onClick={() => toggleSettings(true)}
              className="font-mono text-[10px] uppercase tracking-[0.08em] text-fg-3 hover:text-fg-1"
            >
              change defaults →
            </button>
            <div className="flex items-center gap-2">
              <button
                onClick={deny}
                className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
              >
                deny <span className="text-fg-3">(esc)</span>
              </button>
              <button
                onClick={approve}
                className={`rounded-md px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] ring-1 ${
                  current.level === 'dangerous'
                    ? 'bg-danger/15 text-danger ring-danger/40 hover:bg-danger/25'
                    : 'bg-accent/15 text-accent ring-accent/40 hover:bg-accent/25'
                }`}
              >
                {current.level === 'dangerous' ? 'allow this action' : 'allow'}{' '}
                {current.level !== 'dangerous' && <span className="text-fg-3">(↵)</span>}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
