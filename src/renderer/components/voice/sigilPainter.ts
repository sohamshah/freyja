import type { VoiceEngineState } from '../../voice/engine'

/**
 * The Galdr sigil painter: the Freyja mark (the concentric landform of
 * TopographicMark in TitleBar.tsx) brought to life by the voice pipeline.
 * At rest it IS the mark; every state bends the same contours rather than
 * drawing something else, so the sigil is never decorative:
 *
 *   idle        the mark asleep: dim, a slow breath, the mic is OFF
 *   minting /
 *   connecting  the contours bloom outward from a knot at the centre
 *   listening   the landform swells and ripples with the LIVE mic level
 *   thinking    rings counter-rotate and a pulse of light travels inward
 *   acting      light runs along the outer contour, tools firing
 *   speaking    crests travel outward through the rings like sound
 *   error       the mark flushes red and jitters, then settles
 *   closing     the contours dim and draw back to the centre
 *
 * Pure canvas-2D so it reads the same on the dark material and on paper
 * (an additive point cloud washed out to a speckled blob on a light
 * ground). The component owns the clock, the smoothing and the palette;
 * this file only paints a frame.
 */

export type Rgb = [number, number, number]

export interface SigilPalette {
  dim: Rgb
  accent: Rgb
  accentHi: Rgb
  warn: Rgb
  danger: Rgb
}

/** Continuous per-state parameters; the component lerps toward them. */
export interface SigilParams {
  /** Overall stroke alpha. */
  bright: number
  /** 0 = the dim resting ink, 1 = the accent. */
  accent: number
  /** 0..1 red flush. */
  danger: number
  /** Landform amplitude; 1 is the mark itself. */
  amp: number
  /** Landform rotation speed, radians per second of noise angle. */
  drift: number
  /** 1 = odd rings rotate the other way (thinking). */
  alt: number
  /** Slow radius breathing amplitude. */
  breathe: number
  /** Pulls the inner rings toward the core (thinking). */
  contract: number
  /** Speaking crests travelling outward, 0..1. */
  ripple: number
  /** Thinking pulse travelling inward, 0..1. */
  scan: number
  /** Acting light running along the contours, 0..1. */
  sweep: number
  /** 1 = the formed mark, 0 = collapsed to a knot at the centre. */
  form: number
}

export const REST: SigilParams = {
  bright: 0.58,
  accent: 0.35,
  danger: 0,
  amp: 1,
  drift: 0.05,
  alt: 0,
  breathe: 0.012,
  contract: 0,
  ripple: 0,
  scan: 0,
  sweep: 0,
  form: 1,
}

export function targetsFor(state: VoiceEngineState): SigilParams {
  switch (state) {
    case 'idle':
      return { ...REST }
    case 'minting':
      return { ...REST, bright: 0.75, accent: 1, form: 0.3, drift: 0.5, amp: 1.2 }
    case 'connecting':
      return { ...REST, bright: 0.85, accent: 1, form: 0.7, drift: 0.6, amp: 1.2 }
    case 'listening':
      return { ...REST, bright: 0.9, accent: 1, drift: 0.35, amp: 1.1, breathe: 0.02 }
    case 'thinking':
      return { ...REST, bright: 0.85, accent: 1, drift: 0.9, alt: 1, amp: 1.3, contract: 0.5, scan: 1 }
    case 'acting':
      return { ...REST, bright: 0.95, accent: 1, drift: 0.5, amp: 1.25, sweep: 1 }
    case 'speaking':
      return { ...REST, bright: 0.9, accent: 1, drift: 0.2, ripple: 1 }
    case 'error':
      return { ...REST, bright: 0.85, accent: 1, danger: 1, drift: 1.3, amp: 1.7, form: 0.92 }
    case 'closing':
      return { ...REST, bright: 0.3, accent: 0, form: 0.5, drift: 0.05 }
    default:
      return { ...REST }
  }
}

/** The mark's landform: the same sum of sines TopographicMark draws, so a
 *  resting sigil and the title-bar logo are one shape. */
export function landform(angle: number, ring: number): number {
  return (
    Math.sin(angle * 3 + ring * 0.35) * 0.09 +
    Math.cos(angle * 2 + ring * 0.6 + 1.2) * 0.07 +
    Math.sin(angle * 5 - ring * 0.2 + 0.8) * 0.04 +
    Math.cos(angle + ring * 0.9 + 2.1) * 0.05
  )
}

export interface PaintInput {
  size: number
  params: SigilParams
  /** Smoothed mic level 0..1; the component passes 0 outside listening. */
  level: number
  /** Integrated landform rotation (∫ drift dt). */
  phase: number
  /** Seconds since mount, for the travelling pulses. */
  time: number
  palette: SigilPalette
}

const mix = (a: Rgb, b: Rgb, k: number): Rgb => [
  a[0] + (b[0] - a[0]) * k,
  a[1] + (b[1] - a[1]) * k,
  a[2] + (b[2] - a[2]) * k,
]
const rgba = (c: Rgb, a: number): string =>
  `rgba(${c[0].toFixed(0)},${c[1].toFixed(0)},${c[2].toFixed(0)},${Math.max(0, Math.min(1, a)).toFixed(3)})`
const frac = (x: number): number => ((x % 1) + 1) % 1

export function ringCount(size: number): number {
  return size <= 30 ? 7 : 9
}

export function paintSigil(
  ctx: CanvasRenderingContext2D,
  { size, params: p, level, phase, time, palette }: PaintInput,
): void {
  const rings = ringCount(size)
  const steps = size <= 30 ? 36 : 72
  const cx = size / 2
  const cy = size / 2
  const R = size * 0.44
  const lw = size <= 30 ? 0.9 : Math.max(1.2, size * 0.014)
  ctx.clearRect(0, 0, size, size)
  ctx.lineJoin = 'round'
  ctx.lineCap = 'round'

  const ink = mix(mix(palette.dim, palette.accent, p.accent), palette.danger, p.danger)
  const glint = mix(mix(palette.dim, palette.accentHi, p.accent), palette.danger, p.danger)
  const amp = p.amp + level * 0.6
  const formR = 0.12 + 0.88 * p.form

  const ringRadius = (i: number): number => {
    const t = (i + 1) / rings
    let r = R * t * formR
    r *= 1 - p.contract * (0.08 + 0.3 * (1 - t))
    r *= 1 + p.breathe * Math.sin(time * 0.8 + i * 0.5)
    r *= 1 + level * 0.22 * (0.4 + 0.6 * t)
    r *= 1 + p.ripple * 0.05 * Math.sin(frac(time * 0.8 - t) * Math.PI * 2)
    return r
  }
  const ringDir = (i: number): number => (i % 2 === 0 ? 1 : 1 - 2 * p.alt)

  const trace = (i: number, a0: number, a1: number, n: number): void => {
    const r = ringRadius(i)
    const rot = phase * ringDir(i)
    ctx.beginPath()
    for (let s = 0; s <= n; s++) {
      const ang = a0 + ((a1 - a0) * s) / n
      const rr = r * (1 + landform(ang + rot, i) * amp)
      const x = cx + Math.cos(ang) * rr
      const y = cy + Math.sin(ang) * rr
      if (s === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    }
  }

  for (let i = 0; i < rings; i++) {
    const t = (i + 1) / rings
    // The mark's own falloff: the innermost ring solid, the outermost at 55%.
    let a = p.bright * (0.55 + ((rings - 1 - i) / (rings - 1)) * 0.45)
    a *= 0.35 + 0.65 * p.form
    a += p.ripple * 0.3 * Math.max(0, Math.cos(frac(time * 0.8 - t) * Math.PI * 2))
    const d = Math.abs(frac(time * 0.6) - (1 - t))
    a += p.scan * 0.45 * Math.exp(-(d * d) / 0.02)
    trace(i, 0, Math.PI * 2, steps)
    ctx.closePath()
    ctx.strokeStyle = rgba(ink, a)
    ctx.lineWidth = lw
    ctx.stroke()
  }

  // Acting: light runs along the outer contour; a warm glint runs the other
  // way a few rings in. Both follow the landform, not a circle.
  if (p.sweep > 0.01) {
    const a0 = time * 2.4
    trace(rings - 1, a0, a0 + 0.9, 24)
    ctx.strokeStyle = rgba(glint, 0.9 * p.sweep)
    ctx.lineWidth = lw * 1.6
    ctx.stroke()
    const a1 = -time * 1.7
    trace(Math.max(0, rings - 4), a1, a1 + 0.6, 18)
    ctx.strokeStyle = rgba(palette.warn, 0.55 * p.sweep)
    ctx.lineWidth = lw * 1.2
    ctx.stroke()
  }

  // A small core only while the mic is hot or it speaks: mic truth at a glance.
  const core = p.accent * Math.min(1, level * 0.9 + p.ripple * (0.35 + 0.25 * Math.sin(time * 6)))
  if (core > 0.02) {
    ctx.beginPath()
    ctx.arc(cx, cy, size * (0.025 + 0.04 * core), 0, Math.PI * 2)
    ctx.fillStyle = rgba(glint, 0.5 + 0.4 * core)
    ctx.fill()
  }
}
