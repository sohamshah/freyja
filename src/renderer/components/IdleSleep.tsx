import { useEffect, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import sleepVideoUrl from '../assets/dogs_sleep.mp4'

/* IdleSleep — ambient screensaver that fades over the app after a stretch of
 * complete quiet.
 *
 * "Quiet" here means:
 *   - no main turn is streaming (isStreaming === false)
 *   - no subagent in `running` or `pending` state
 *   - no computer-use session in `running` status
 *   - no window-level user input (keydown / pointermove / pointerdown /
 *     wheel / touchstart) for the past IDLE_MS
 *
 * Any of those flipping back on (or the next pointer flick) instantly wakes
 * the overlay. The video element stays mounted but paused, so wake/sleep is
 * just a play/pause + opacity transition — no remount cost.
 *
 * Mount this once at the top level. It manages its own listeners + timer.
 */

const IDLE_MS = 30_000 // 30s of full quiet before we sleep
const POLL_MS = 750 // how often the sleep/wake check runs
const WAKE_GRACE_MS = 1000 // any activity within this window counts as "recent"

// EXACT corner-pixel color of the baked sleep video (measured at multiple
// corners → rgb(0, 4, 4) after h264 yuv420p encoding). Use this as the
// overlay backdrop so there's no visible seam where the video sits.
const VIDEO_BG = 'rgb(0, 4, 4)'

export function IdleSleep() {
  // ---- busy signals — any one of these blocks sleep AND counts as activity
  const isStreaming = useHarness((s) => s.isStreaming)
  const subagentBusy = useHarness((s) => {
    for (const id of s.subagentOrder) {
      const rec = s.subagents[id]
      if (rec && (rec.state === 'running' || rec.state === 'pending')) return true
    }
    return false
  })
  const computerBusy = useHarness((s) => {
    for (const id in s.computerSessions) {
      if (s.computerSessions[id]?.status === 'running') return true
    }
    return false
  })
  const busy = isStreaming || subagentBusy || computerBusy

  const lastActivityRef = useRef<number>(Date.now())
  const [sleeping, setSleeping] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)

  // Treat any "busy=true" observation as activity so the timer never advances
  // while the engine is working. Includes the moment busy transitions back to
  // false — we don't want to sleep the instant a turn finishes.
  useEffect(() => {
    if (busy) lastActivityRef.current = Date.now()
  }, [busy])

  // Window-level user-input listeners. We bump activity on a passive ref —
  // no React state churn until the poll decides we crossed a threshold.
  useEffect(() => {
    const bump = () => {
      lastActivityRef.current = Date.now()
    }
    const opts = { passive: true } as AddEventListenerOptions
    window.addEventListener('keydown', bump, opts)
    window.addEventListener('pointerdown', bump, opts)
    window.addEventListener('pointermove', bump, opts)
    window.addEventListener('wheel', bump, opts)
    window.addEventListener('touchstart', bump, opts)
    return () => {
      window.removeEventListener('keydown', bump)
      window.removeEventListener('pointerdown', bump)
      window.removeEventListener('pointermove', bump)
      window.removeEventListener('wheel', bump)
      window.removeEventListener('touchstart', bump)
    }
  }, [])

  // Poll — single periodic check. Cheap, ~one comparison every POLL_MS.
  useEffect(() => {
    const id = window.setInterval(() => {
      const elapsed = Date.now() - lastActivityRef.current
      setSleeping((prev) => {
        if (prev) {
          // wake if anything moved recently
          if (elapsed < WAKE_GRACE_MS) return false
          return prev
        }
        // sleep only if we're not busy AND truly past the threshold
        if (!busy && elapsed >= IDLE_MS) return true
        return prev
      })
    }, POLL_MS)
    return () => window.clearInterval(id)
  }, [busy])

  // Play/pause the video to match visibility. preload="none" keeps the
  // bytes off disk until we actually need them.
  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    if (sleeping) {
      v.play().catch(() => {})
    } else {
      v.pause()
    }
  }, [sleeping])

  // Soft radial mask — keeps the center fully opaque and dissolves the outer
  // edge into transparency so any sub-pixel color mismatch between the video
  // and the matched backdrop (encoding noise, monitor gamma, anti-aliasing)
  // fades to nothing instead of drawing a hard rectangle.
  const portalMask =
    'radial-gradient(circle at center, #000 70%, rgba(0,0,0,0.55) 88%, transparent 100%)'

  return (
    <div
      aria-hidden={!sleeping}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 9999,
        backgroundColor: VIDEO_BG,
        opacity: sleeping ? 1 : 0,
        transition: 'opacity 720ms ease',
        // While invisible, let every pointer/keyboard event reach the app.
        pointerEvents: sleeping ? 'auto' : 'none',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexDirection: 'column',
        gap: 36,
        // Hide the I-beam from any focused <textarea> behind the overlay.
        cursor: sleeping ? 'default' : 'auto',
      }}
    >
      {/* slow halo pulse — sits behind the video, masked to a soft circle */}
      <div
        aria-hidden
        style={{
          position: 'absolute',
          left: '50%',
          top: '50%',
          width: 'min(78vw, 78vh)',
          height: 'min(78vw, 78vh)',
          transform: 'translate(-50%, calc(-50% - 18px))',
          background:
            'radial-gradient(circle, rgba(150, 180, 215, 0.10) 0%, rgba(150, 180, 215, 0.04) 35%, transparent 65%)',
          pointerEvents: 'none',
          animation: sleeping ? 'idle-halo-breathe 8s ease-in-out infinite' : 'none',
          opacity: sleeping ? 1 : 0,
          transition: 'opacity 1100ms ease 200ms',
        }}
      />

      <video
        ref={videoRef}
        src={sleepVideoUrl}
        muted
        loop
        playsInline
        preload="none"
        // Square portal centered in the viewport. Source is 720² so no
        // letterboxing. The radial mask still anti-aliases the edge, but
        // the heavy lifting is the blend mode below.
        style={{
          width: 'min(58vw, 58vh)',
          height: 'min(58vw, 58vh)',
          objectFit: 'contain',
          display: 'block',
          maskImage: portalMask,
          WebkitMaskImage: portalMask,
          // `lighten` = max(backdrop, video) per channel. Any video pixel
          // darker than the backdrop is replaced by the backdrop — so the
          // video's "paper" (rendered as pure or near-pure black) becomes
          // identical to the overlay bg, killing the visible rectangle
          // regardless of how the h264 decoder lands the dark pixels.
          // Bright pixels (the dogs) pass through unchanged.
          mixBlendMode: 'lighten',
          // drop-shadow respects the mask alpha, so the glow follows the
          // SOFT circle shape rather than the underlying rectangle.
          filter: 'drop-shadow(0 0 50px rgba(150, 180, 215, 0.20))',
        }}
      />

      <div
        style={{
          fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace',
          fontSize: 10.5,
          letterSpacing: '0.38em',
          textTransform: 'uppercase',
          color: 'rgba(200, 215, 230, 0.34)',
          pointerEvents: 'none',
          // fade the hint in slightly after the video so it doesn't pop
          opacity: sleeping ? 1 : 0,
          transition: 'opacity 900ms ease 320ms',
        }}
      >
        idle · move to wake
      </div>

      {/* keyframes scoped to this overlay; safe to inject — single instance */}
      <style>{`
        @keyframes idle-halo-breathe {
          0%, 100% { opacity: 0.55; transform: translate(-50%, calc(-50% - 18px)) scale(1); }
          50%      { opacity: 1.0;  transform: translate(-50%, calc(-50% - 18px)) scale(1.04); }
        }
      `}</style>
    </div>
  )
}
