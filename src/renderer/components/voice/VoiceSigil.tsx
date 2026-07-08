import { useEffect, useRef } from 'react'
import type { VoiceEngineState } from '../../voice/engine'

/**
 * VoiceSigil — the Galdr presence mark (docs/freyja-voice-galdr.html §03).
 *
 * Five concentric irregular contour rings on a DPR-aware canvas, in the
 * same topographic language as the title-bar mark. Every behavior maps
 * 1:1 to the real pipeline state — the sigil is never decorative:
 *
 *   idle        rings dim (fg-2), slow ~8 s breathe — mic is OFF
 *   minting /
 *   connecting  a single accent bloom sweeps outward through the rings
 *   listening   rings ripple; amplitude is the LIVE mic RMS (`level`)
 *   thinking    rings contract ~8% and counter-rotate slowly
 *   acting      a bright accent arc sweeps the outer ring while a warm
 *               counter-arc runs the inner track — tools are firing
 *   speaking    rings emit outward-travelling, fading pulses
 *   error       one dull-red pulse, then dim
 *
 * Ring wobble reuses AnimatedTopographicMark's ordering trick: adjacent
 * rings share almost the same harmonic phase, so their per-angle noise
 * stays correlated and rings can never cross even at full mic ripple.
 *
 * The rAF loop only runs while the canvas is actually on screen
 * (IntersectionObserver + document visibility); prefers-reduced-motion
 * renders one static frame per state instead of animating.
 */

const RINGS = 5
const STEPS = 44

// Palette — pinned design tokens (tailwind.config.cjs), as r,g,b.
const ACCENT = '168,212,252' // #a8d4fc
const ACCENT_HI = '196,224,252' // #c4e0fc
const WARM = '184,160,120' // #b8a078
const DANGER = '180,130,130' // #b48282
const DIM = '110,110,110' // fg-2

export function VoiceSigil({
  size,
  state,
  level = 0,
  onClick,
  className = '',
}: {
  size: number
  state: VoiceEngineState
  level?: number
  onClick?: () => void
  className?: string
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const stateRef = useRef<VoiceEngineState>(state)
  const levelRef = useRef(level)
  const smoothRef = useRef(0)
  // When the current state was entered — the error pulse and the bloom
  // sweep are time-since-entry animations, not wall-clock ones.
  const enteredAtRef = useRef(performance.now())
  // Set by the draw effect; the state effect calls it so reduced-motion
  // mode still repaints exactly once per state change.
  const staticRedrawRef = useRef<(() => void) | null>(null)

  // Ref mirror — the rAF loop reads these without re-running the effect.
  levelRef.current = Math.max(0, Math.min(1, level))

  useEffect(() => {
    if (stateRef.current !== state) {
      stateRef.current = state
      enteredAtRef.current = performance.now()
    }
    staticRedrawRef.current?.()
  }, [state])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1))
    canvas.width = Math.round(size * dpr)
    canvas.height = Math.round(size * dpr)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    const reducedMotion = window.matchMedia?.(
      '(prefers-reduced-motion: reduce)',
    ).matches

    const cx = size / 2
    const cy = size / 2
    const R = size * 0.44
    // 1 css-px hairline at title-bar size, a touch heavier in the HUD.
    const lw = size <= 28 ? 1 : 1.3

    // Correlated harmonics: the ring index only nudges the phase
    // (±0.5 rad per ring at most), so adjacent rings deform together
    // and radial ordering survives any amplitude used below.
    const wobble = (a: number, ring: number, drift: number): number =>
      Math.sin(a * 3 + ring * 0.34 + drift) * 0.52 +
      Math.cos(a * 5 - ring * 0.2 - drift * 0.7) * 0.26 +
      Math.sin(a * 2 + ring * 0.5 + 1.2) * 0.34

    const drawFrame = (t: number, staticFrame = false): void => {
      const s = stateRef.current
      ctx.clearRect(0, 0, size, size)

      // Smoothed mic level — fast attack, slow release, so the ripple
      // tracks speech onsets but doesn't flicker at the 30 Hz feed.
      const target = levelRef.current
      smoothRef.current +=
        (target - smoothRef.current) * (target > smoothRef.current ? 0.5 : 0.15)
      const lvl = staticFrame ? 0.45 : smoothRef.current
      const te = staticFrame ? 9 : (performance.now() - enteredAtRef.current) / 1000

      for (let i = 0; i < RINGS; i++) {
        const tr = (i + 1) / RINGS
        let r = R * tr
        let amp = 0.09
        let alpha = 0.16
        let col = ACCENT
        let drift = 0

        switch (s) {
          case 'idle': {
            col = DIM
            const breathe = Math.sin((t / 8) * Math.PI * 2)
            r *= 1 + 0.02 * breathe
            alpha = 0.13 + 0.04 * Math.sin((t / 8) * Math.PI * 2 + i * 0.7)
            amp = 0.09
            break
          }
          case 'minting':
          case 'connecting': {
            // One bloom sweep per 1.4 s while the mint/handshake is in
            // flight — each ring swells slightly after the one inside it.
            const k = ((staticFrame ? 0.7 : t) % 1.4) / 1.4
            const ph = Math.max(0, Math.min(1, k * 1.8 - tr * 0.8))
            const bloom = Math.sin(Math.PI * ph)
            r *= 1 + 0.09 * bloom
            alpha = 0.1 + 0.32 * bloom
            amp = 0.08
            break
          }
          case 'listening': {
            // Outer rings ripple hardest — same weighting as the dossier
            // sketch. Amplitude is the real mic RMS, nothing synthetic.
            amp = (0.07 + 0.12 * lvl) * (0.4 + 0.6 * tr)
            alpha = 0.2 + 0.32 * lvl
            drift = t * 5
            break
          }
          case 'thinking': {
            r *= 0.92
            amp = 0.09
            alpha = 0.2
            drift = (i % 2 === 0 ? 1 : -1) * t * 0.6
            break
          }
          case 'acting': {
            amp = 0.08
            alpha = 0.16
            drift = t * 0.3
            break
          }
          case 'speaking': {
            const ph = ((staticFrame ? 0.4 : t) * 0.55 + i / RINGS) % 1
            r *= 1 + 0.06 * ph
            alpha = 0.05 + 0.3 * (1 - ph)
            amp = 0.08
            break
          }
          case 'error': {
            col = DANGER
            if (te < 1) {
              const pulse = Math.sin(Math.PI * te)
              r *= 1 + 0.07 * pulse
              alpha = 0.12 + 0.35 * pulse
            } else {
              alpha = 0.1
            }
            amp = 0.08
            break
          }
          case 'closing': {
            col = DIM
            r *= 0.96
            alpha = 0.1
            amp = 0.08
            break
          }
        }

        ctx.beginPath()
        for (let sIdx = 0; sIdx <= STEPS; sIdx++) {
          const a = (sIdx / STEPS) * Math.PI * 2
          const rr = r * (1 + wobble(a, i, drift) * amp)
          const px = cx + Math.cos(a) * rr
          const py = cy + Math.sin(a) * rr
          if (sIdx === 0) ctx.moveTo(px, py)
          else ctx.lineTo(px, py)
        }
        ctx.closePath()
        ctx.strokeStyle = `rgba(${col},${alpha.toFixed(3)})`
        ctx.lineWidth = lw
        ctx.stroke()
      }

      // Acting: a bright accent arc sweeps the outer ring while a warm
      // counter-arc runs an inner track in the opposite direction.
      if (s === 'acting') {
        const a0 = staticFrame ? 0.8 : t * 2.4
        ctx.beginPath()
        ctx.strokeStyle = `rgba(${ACCENT_HI},0.85)`
        ctx.lineWidth = lw * 1.5
        ctx.arc(cx, cy, R, a0, a0 + 0.9)
        ctx.stroke()
        ctx.beginPath()
        ctx.strokeStyle = `rgba(${WARM},0.5)`
        ctx.lineWidth = lw * 1.1
        ctx.arc(cx, cy, R * 0.62, -a0 * 0.7, -a0 * 0.7 + 0.55)
        ctx.stroke()
      }

      // Center dot — fg-3 while asleep, accent while the mic is live;
      // in listening its radius tracks the level (a second mic-truth).
      const s2 = stateRef.current
      const dotBase = Math.max(1.2, size * 0.045)
      const dotR = s2 === 'listening' ? dotBase + lvl * size * 0.045 : dotBase
      const dotCol =
        s2 === 'idle' || s2 === 'closing'
          ? `rgba(${DIM},0.7)`
          : s2 === 'error'
            ? `rgba(${DANGER},0.9)`
            : `rgba(${ACCENT_HI},0.9)`
      ctx.fillStyle = dotCol
      ctx.beginPath()
      ctx.arc(cx, cy, dotR, 0, Math.PI * 2)
      ctx.fill()
    }

    if (reducedMotion) {
      staticRedrawRef.current = () => drawFrame(0.7, true)
      drawFrame(0.7, true)
      return () => {
        staticRedrawRef.current = null
      }
    }
    staticRedrawRef.current = null

    // rAF loop, gated on actual visibility — an off-screen or hidden
    // sigil (collapsed panel, background window) burns zero frames.
    let raf = 0
    let running = false
    let inView = true
    const t0 = performance.now()
    const loop = (now: number) => {
      if (!running) return
      drawFrame((now - t0) / 1000)
      raf = requestAnimationFrame(loop)
    }
    const sync = () => {
      const shouldRun = inView && document.visibilityState !== 'hidden'
      if (shouldRun && !running) {
        running = true
        raf = requestAnimationFrame(loop)
      } else if (!shouldRun && running) {
        running = false
        cancelAnimationFrame(raf)
      }
    }
    const observer = new IntersectionObserver(([entry]) => {
      inView = entry.isIntersecting
      sync()
    })
    observer.observe(canvas)
    const onVisibility = () => sync()
    document.addEventListener('visibilitychange', onVisibility)
    sync()

    return () => {
      running = false
      cancelAnimationFrame(raf)
      observer.disconnect()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [size])

  return (
    <canvas
      ref={canvasRef}
      style={{ width: size, height: size }}
      className={`${onClick ? 'cursor-pointer ' : ''}${className}`}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      aria-label={onClick ? `voice — ${state}` : undefined}
      aria-hidden={onClick ? undefined : true}
    />
  )
}
