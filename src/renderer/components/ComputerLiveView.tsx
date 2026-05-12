import { useEffect, useMemo, useRef, useState } from 'react'
import { useHarness, type ComputerSessionState } from '../state/store'
import { formatDuration } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { useFrameObjectUrl } from '../lib/frameMedia'
import { StickyHeader } from './StickyHeader'

/**
 * Live screenshot viewer for an active computer-use session.
 *
 * Shows the most recent `screenshot_frame` stretched to fit, with an
 * amber "about to click here" ring rendered on top whenever the
 * current session has a `plannedAction` with coordinates. A brief
 * narration line at the top says what's about to happen.
 *
 * This component is *not* routed — it picks the computer session
 * linked to the currently-active main session (as a child) OR the
 * most recently updated running computer session. That means it
 * follows the user's attention automatically without a state toggle.
 */
export function ComputerLiveView() {
  const computerSessions = useHarness((s) => s.computerSessions)
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const sessions = useHarness((s) => s.sessions)
  const emergencyStop = useHarness((s) => s.emergencyStopComputer)

  // Pick which computer session to display:
  //  1. If the currently active main session IS a computer session, show it.
  //  2. Else if a running computer session is a child of the active session, show it.
  //  3. Else show the most recently running one.
  const displayed: ComputerSessionState | null = useMemo(() => {
    const direct = computerSessions[activeSessionId]
    if (direct) return direct
    const childIds = sessions
      .filter((s) => s.parentSessionId === activeSessionId)
      .map((s) => s.id)
    for (const id of childIds) {
      const cs = computerSessions[id]
      if (cs && cs.status === 'running') return cs
    }
    const running = Object.values(computerSessions).filter(
      (s) => s.status === 'running',
    )
    if (running.length > 0) {
      return running.sort(
        (a, b) => (b.latestFrame?.takenAt ?? 0) - (a.latestFrame?.takenAt ?? 0),
      )[0]
    }
    return null
  }, [computerSessions, activeSessionId, sessions])

  if (!displayed) return null

  return (
    <div className="hairline-b">
      <StickyHeader>
        <div className="flex w-full items-center justify-between gap-2 px-4 py-2">
          <div className="label flex items-center gap-1.5">
            {displayed.status === 'running' ? (
              <Spinner name="scan" className="text-accent" />
            ) : (
              <span
                className={`block h-1.5 w-1.5 rounded-full ${
                  displayed.status === 'done'
                    ? 'bg-ok'
                    : displayed.status === 'failed'
                      ? 'bg-danger'
                      : 'bg-fg-2'
                }`}
              />
            )}
            computer
            <span className="font-mono text-[10px] text-fg-3">
              {displayed.status}
            </span>
            <span className="font-mono text-[9.5px] text-fg-3">
              · {displayed.frameCount} frame
              {displayed.frameCount === 1 ? '' : 's'}
            </span>
          </div>
          {displayed.status === 'running' && (
            <button
              onClick={() => emergencyStop('live-view-stop')}
              className="rounded-md bg-danger/10 px-2 py-[2px] font-mono text-[10px] uppercase tracking-[0.08em] text-danger ring-1 ring-danger/30 hover:bg-danger/20"
              title="Emergency stop (also: triple-Esc, ⌘⇧Esc)"
            >
              ■ stop
            </button>
          )}
        </div>
      </StickyHeader>
      <div className="px-4 pb-3 pt-1">
        <ScreenshotCanvas session={displayed} />
        <Narration session={displayed} />
        <MiniHistory session={displayed} />
      </div>
    </div>
  )
}

function ScreenshotCanvas({ session }: { session: ComputerSessionState }) {
  const [box, setBox] = useState<{ w: number; h: number } | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)

  // Observe the wrapper so the highlight ring can be positioned in
  // *display* pixels not original screenshot pixels.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      const rect = el.getBoundingClientRect()
      setBox({ w: rect.width, h: rect.height })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const frame = session.latestFrame
  const planned = session.plannedAction
  const src = useFrameObjectUrl(frame)

  // Compute the ring position in the wrapper's pixel space.
  const ring = useMemo(() => {
    if (!planned || planned.x === undefined || planned.y === undefined) return null
    if (!frame || !box) return null
    const scaleX = box.w / frame.width
    const scaleY = box.h / frame.height
    const cx = planned.x * scaleX
    const cy = planned.y * scaleY
    return { cx, cy }
  }, [planned, frame, box])

  if (!src) {
    return (
      <div className="relative flex aspect-[16/10] w-full items-center justify-center rounded-md bg-black/40 ring-hairline">
        <div className="text-center font-mono text-[10.5px] text-fg-2">
          <div>waiting for first screenshot…</div>
          <div className="mt-1 text-[9.5px] text-fg-3">
            the sub-agent will capture one on its first tool call
          </div>
        </div>
      </div>
    )
  }

  return (
    <div
      ref={wrapRef}
      className="relative overflow-hidden rounded-md bg-black ring-hairline"
      style={{ aspectRatio: frame ? `${frame.width}/${frame.height}` : '16/10' }}
    >
      <img
        src={src}
        alt="live screen"
        className="block h-full w-full object-contain"
        decoding="async"
        draggable={false}
      />
      {ring && (
        <div
          className="pointer-events-none absolute"
          style={{
            left: `${ring.cx}px`,
            top: `${ring.cy}px`,
            transform: 'translate(-50%, -50%)',
          }}
          aria-hidden
        >
          <HighlightRing />
        </div>
      )}
    </div>
  )
}

/** Amber 28px highlight ring with a soft pulse. Shown for ~200ms
 *  before every mutating action lands. */
function HighlightRing() {
  return (
    <div className="relative">
      <div
        className="h-7 w-7 rounded-full border-2"
        style={{
          borderColor: '#f5b640',
          boxShadow:
            '0 0 0 2px rgba(245,182,64,0.18), 0 0 18px rgba(245,182,64,0.65)',
          animation: 'comp-ring 200ms ease-out',
        }}
      />
      <div
        className="absolute inset-0 flex items-center justify-center"
        style={{ pointerEvents: 'none' }}
      >
        <div
          className="h-1 w-1 rounded-full"
          style={{ backgroundColor: '#f5b640' }}
        />
      </div>
      <style>{`
        @keyframes comp-ring {
          0%   { transform: scale(0.6); opacity: 0.4 }
          60%  { transform: scale(1.15); opacity: 1 }
          100% { transform: scale(1.0); opacity: 0.9 }
        }
      `}</style>
    </div>
  )
}

function Narration({ session }: { session: ComputerSessionState }) {
  const planned = session.plannedAction
  const lastAction = session.history[session.history.length - 1]
  const line = planned?.description ?? (lastAction ? describe(lastAction.action) : session.goal)
  return (
    <div className="mt-2 flex items-center gap-2 font-mono text-[10.5px] text-fg-1">
      <span
        className={`inline-block h-1 w-1 shrink-0 rounded-full ${
          planned ? 'bg-accent' : 'bg-fg-3'
        }`}
      />
      <span className="truncate">{line || '(idle)'}</span>
    </div>
  )
}

function MiniHistory({ session }: { session: ComputerSessionState }) {
  if (session.history.length === 0) return null
  const recent = session.history.slice(-6).reverse()
  return (
    <div className="mt-2 space-y-0.5">
      {recent.map((h, i) => (
        <div
          key={`${h.at}-${i}`}
          className="flex items-center gap-2 font-mono text-[10px] text-fg-2"
        >
          <span
            className={`block h-1 w-1 shrink-0 rounded-full ${
              h.success ? 'bg-ok' : 'bg-danger'
            }`}
          />
          <span className="text-fg-1">{h.action}</span>
          <span className="ml-auto text-fg-3">
            {formatDuration(h.durationMs)}
          </span>
        </div>
      ))}
    </div>
  )
}

function describe(action: string): string {
  switch (action) {
    case 'click':
      return 'clicked'
    case 'type_text':
      return 'typed text'
    case 'press_key':
      return 'pressed key'
    case 'scroll':
      return 'scrolled'
    case 'focus_window':
      return 'focused window'
    default:
      return action
  }
}
