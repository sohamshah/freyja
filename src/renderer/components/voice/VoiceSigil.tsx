import { useEffect, useRef } from 'react'
import type { VoiceEngineState } from '../../voice/engine'
import { cssTokenRgb, useEffectiveTheme } from '../../lib/theme'
import { paintSigil, targetsFor, type SigilPalette, type SigilParams } from './sigilPainter'

/**
 * VoiceSigil — the Galdr presence mark (docs/freyja-voice-galdr.html §03).
 *
 * The Freyja mark itself, animated by the real pipeline state: the
 * contours of TopographicMark breathe, bloom, swell with the live mic,
 * counter-rotate while it thinks and ripple outward while it speaks. The
 * state vocabulary and what each state does are in sigilPainter.ts.
 *
 * State targets are smoothed (attack/release) so transitions glide rather
 * than snap. The rAF loop only runs while the canvas is actually on
 * screen (IntersectionObserver + document visibility); reduced-motion
 * renders one static, representative frame. Canvas 2D throughout, so it
 * reads identically on the dark material and on paper.
 */

// Palette — read from the theme tokens (styles/globals.css) when the sigil
// mounts, and again when the shell flips between dark and light. The
// fallbacks are the dark values.
function sigilPalette(): SigilPalette {
  return {
    dim: cssTokenRgb('--fg-2', [110, 110, 110]),
    accent: cssTokenRgb('--accent-rgb', [168, 212, 252]),
    accentHi: cssTokenRgb('--accent-hi-rgb', [196, 224, 252]),
    warn: cssTokenRgb('--warn-rgb', [184, 160, 120]),
    danger: cssTokenRgb('--danger-rgb', [180, 130, 130]),
  }
}

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
  const levelRef = useRef(0)
  levelRef.current = Math.max(0, Math.min(1, level))

  // The palette is read once per mount, so a theme flip re-runs the effect.
  const theme = useEffectiveTheme()
  useEffect(() => {
    stateRef.current = state
  }, [state])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const dpr = Math.max(1, Math.min(2.5, window.devicePixelRatio || 1))
    canvas.width = Math.round(size * dpr)
    canvas.height = Math.round(size * dpr)
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    const reducedMotion = !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    const palette = sigilPalette()

    // Smoothed current parameters (lerped toward the per-state target), the
    // integrated landform rotation (so a change of drift never snaps the
    // angle) and the mic level with a fast attack and slow release.
    const cur: SigilParams = { ...targetsFor(state) }
    let phase = 0
    let clock = 0
    let smoothLvl = 0

    const render = (dt: number, staticFrame: boolean): void => {
      const tgt = targetsFor(staticFrame ? 'listening' : stateRef.current)
      const k = staticFrame ? 1 : Math.min(1, dt * 4.5)
      for (const key of Object.keys(cur) as (keyof SigilParams)[]) {
        cur[key] += (tgt[key] - cur[key]) * k
      }
      const rawLvl = staticFrame ? 0.4 : levelRef.current
      smoothLvl += (rawLvl - smoothLvl) * (rawLvl > smoothLvl ? 0.5 : 0.15)
      phase += staticFrame ? 0.7 : cur.drift * dt
      clock += staticFrame ? 0.7 : dt
      // Mic swell only reads in listening — other states ignore the feed.
      const listening = staticFrame || stateRef.current === 'listening'
      paintSigil(ctx, { size, params: cur, level: listening ? smoothLvl : 0, phase, time: clock, palette })
    }

    if (reducedMotion) {
      render(0, true)
      return
    }

    let raf = 0
    let running = false
    let inView = true
    let last = performance.now()
    const loop = (now: number) => {
      if (!running) return
      const dt = Math.min(0.05, (now - last) / 1000)
      last = now
      render(dt, false)
      raf = requestAnimationFrame(loop)
    }
    const sync = () => {
      const shouldRun = inView && document.visibilityState !== 'hidden'
      if (shouldRun && !running) {
        running = true
        last = performance.now()
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
    const onVis = () => sync()
    document.addEventListener('visibilitychange', onVis)
    sync()

    return () => {
      running = false
      cancelAnimationFrame(raf)
      observer.disconnect()
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [size, theme])

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
