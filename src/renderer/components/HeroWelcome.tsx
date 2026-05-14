import { useEffect, useRef } from 'react'
import { useHarness } from '../state/store'
import { AnimatedTopographicMark } from './AnimatedTopographicMark'


export function HeroWelcome() {
  const mode = useHarness((s) => s.mode)
  const requestDemoBurst = useHarness((s) => s.requestDemoBurst)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const sessions = useHarness((s) => s.sessions)
  const model = useHarness((s) => s.model)

  const activeSession = sessions.find((session) => session.id === activeSessionId)
  const workspace = compactPath(activeSession?.workspace || '')
  const sessionModel = activeSession?.model || model
  const messageCount = activeSession?.messageCount ?? 0

  const triggerBurst = () => {
    const api = (window as any).harness
    if (api) requestDemoBurst()
    else (window as any).__harnessDemo?.burst()
  }

  // ── Expansion animation. The welcome icon emerges as the visual
  //    inverse of the gravity well: ep ramps from 0 → 1 over 1.8s,
  //    inner rings popping out to natural radius first, propagating
  //    outward ring-by-ring. We start the ramp when the splash has
  //    unmounted (body.splash-active class removed); until then the
  //    icon sits at its collapsed point, invisible behind the splash.
  const expandProgressRef = useRef(0)
  useEffect(() => {
    let raf = 0
    const DURATION_MS = 1800
    const runExpansion = () => {
      const start = performance.now()
      const tick = (now: number) => {
        const t = Math.min(1, (now - start) / DURATION_MS)
        // easeOutCubic — brisk initial onset, soft landing
        expandProgressRef.current = 1 - Math.pow(1 - t, 3)
        if (t < 1) raf = requestAnimationFrame(tick)
      }
      raf = requestAnimationFrame(tick)
    }
    // If the splash has already finished (e.g. hot reload, or this
    // component remounts later), start immediately.
    if (!document.body.classList.contains('splash-active')) {
      runExpansion()
      return () => cancelAnimationFrame(raf)
    }
    // Otherwise watch for the splash to unmount, then kick off.
    const observer = new MutationObserver(() => {
      if (!document.body.classList.contains('splash-active')) {
        observer.disconnect()
        runExpansion()
      }
    })
    observer.observe(document.body, { attributes: true, attributeFilter: ['class'] })
    return () => {
      observer.disconnect()
      cancelAnimationFrame(raf)
    }
  }, [])

  return (
    <>
      {/* ── Identity block — pinned to the VIEWPORT centre via fixed
          positioning, not centred in HeroWelcome's flex layout. That
          way the bottom strip's height doesn't push the icon upward.
          The splash icon hero is also at viewport centre, so the two
          icons overlap exactly when the splash dissolves — no
          migration, no upward drift. */}
      <div
        className="pointer-events-none fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2"
        style={{ zIndex: 0 }}
      >
        <div data-splash-target="hero-icon">
          <AnimatedTopographicMark
            size={190}
            className="text-accent"
            intensity={1}
            expandProgressRef={expandProgressRef}
          />
        </div>
      </div>

      {/* ── Bottom strip — workspace + quick-start cards and the tip
          line, pinned just above the input dock. Sits in HeroWelcome's
          flex layout normally; the spacer above pushes it to the
          bottom of the scroller column. */}
      <div className="flex h-full w-full flex-col px-8">
        <div className="flex-1" />
        <div className="mx-auto flex w-full max-w-[940px] flex-col gap-5 pb-4">
          <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
            <Card title="Workspace">
              <div className="truncate font-mono text-[12px] text-fg-0">{workspace || 'detecting workspace...'}</div>
              <div className="mt-1 text-[11px] text-fg-2">
                {sessionModel} · {messageCount > 0 ? `${messageCount} messages` : 'empty session'}
              </div>
            </Card>
            <Card title="Quick start">
              <div className="text-[12px] text-fg-1">Start from the input below, or open the current session map.</div>
              <button
                onClick={() => toggleMissionDashboard(true, 'overview')}
                className="mt-3 rounded-md bg-accent/10 px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/25 hover:bg-accent/18"
              >
                open mission dashboard
              </button>
            </Card>
          </div>

          <div className="flex w-full flex-col items-center gap-2 text-[11px] text-fg-2">
            <div className="flex items-center gap-2">
              <span className="inline-block h-1 w-1 rounded-full bg-accent" />
              <span>
                Tip: <kbd className="kbd">⌘</kbd> <kbd className="kbd">⇧</kbd> <kbd className="kbd">M</kbd> opens the mission dashboard
              </span>
            </div>
            {mode === 'demo' && (
              <button
                onClick={triggerBurst}
                className="mt-2 rounded-md bg-accent/10 px-3 py-1 text-[11px] text-accent ring-1 ring-accent/30 hover:bg-accent/20"
              >
                ▸ trigger demo burst (⌘B)
              </button>
            )}
          </div>
        </div>
      </div>
    </>
  )
}

function compactPath(path: string): string {
  if (!path) return ''
  return path.replace(/^\/Users\/[^/]+/, '~')
}

function Card({ title, children, wide }: { title: string; children: React.ReactNode; wide?: boolean }) {
  return (
    <div
      className={`rounded-xl glass-raised p-4 ${wide ? 'sm:col-span-2' : ''}`}
    >
      <div className="mb-2 flex items-center gap-2 label">
        <span className="inline-block h-1 w-1 rounded-full bg-accent" />
        {title}
      </div>
      {children}
    </div>
  )
}
