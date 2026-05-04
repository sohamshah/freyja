import { useHarness } from '../state/store'

/**
 * Always-visible panic button for computer-use sessions.
 *
 * Renders a fixed-position card in the top-right corner of the
 * window whenever at least one computer session is running. Click
 * fires `emergency_stop` which cancels every active computer
 * sub-agent and drops the mouse/keyboard control immediately.
 *
 * The component is intentionally "loud" — red border, pulsing ring,
 * large click target — because when you need it, you need it
 * *fast*. Vy's equivalent is similarly impossible to miss.
 *
 * Keyboard alternatives: triple-Esc within 1s, ⌘⇧Esc (global,
 * works even when the app isn't focused).
 */
export function EmergencyPanic() {
  const active = useHarness((s) => s.computerActive)
  const sessions = useHarness((s) => s.computerSessions)
  const stop = useHarness((s) => s.emergencyStopComputer)

  if (!active) return null

  const running = Object.values(sessions).filter((s) => s.status === 'running')
  const count = running.length
  if (count === 0) return null

  return (
    <div
      className="fixed right-4 top-[52px] z-[60] select-none"
      role="alertdialog"
      aria-label="Computer control active"
    >
      <button
        onClick={() => stop('panic-button')}
        className="group flex items-center gap-2.5 rounded-xl bg-danger/15 px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.08em] text-danger shadow-[0_0_32px_rgba(255,90,90,0.35)] ring-1 ring-danger/45 backdrop-blur-xl hover:bg-danger/25"
        title="Stop all computer-use sessions (⌘⇧Esc / triple-Esc also work)"
      >
        <span className="relative flex h-2.5 w-2.5">
          <span className="absolute inset-0 animate-ping rounded-full bg-danger/60" />
          <span className="relative inline-block h-2.5 w-2.5 rounded-full bg-danger" />
        </span>
        <span className="flex flex-col items-start leading-[1.1]">
          <span className="text-[10px]">stop</span>
          <span className="text-[8.5px] text-danger/80">
            {count} session{count === 1 ? '' : 's'}
          </span>
        </span>
        <span className="ml-1 text-[9px] text-danger/70 group-hover:text-danger">
          ⌘⇧⎋
        </span>
      </button>
    </div>
  )
}
