import { useEffect, useRef } from 'react'

/** Dense flowing-dash backdrop — a tight grid of short white tick
 *  marks whose orientation traces a slowly-evolving procedural curl
 *  field. Default orientation is vertical; smooth low-frequency noise
 *  bends the dashes ±~75° so the surface reads as fabric or
 *  iron-filings under a moving magnet. No arrowheads, no dots, no
 *  mouse reaction — purely procedural so it stays calm under any
 *  cursor activity in the surrounding form.
 *
 *  Designed to live inside a card, not as a fullscreen backdrop.
 *  Sits absolutely-positioned with `pointer-events: none` behind the
 *  form's foreground content; the parent decides the visible
 *  rectangle. */
export function VectorFieldBackdrop({
  active = true,
  /** Grid spacing in CSS pixels. Tight default — the reference look is
   *  field-dense, not sparse-pointer. */
  spacing = 14,
  /** Base stroke alpha (0..1) for dashes at rest. Local peaks add a
   *  bit on top via the procedural field. */
  alpha = 0.18,
  /** Tint of the dashes. Default white for the high-contrast iron-
   *  filings look; callers can pass an accent rgb if they need to
   *  match a surrounding palette. */
  color = '255, 255, 255',
}: {
  active?: boolean
  spacing?: number
  alpha?: number
  color?: string
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rafRef = useRef<number | null>(null)
  const startTimeRef = useRef<number>(0)
  const reducedMotionRef = useRef<boolean>(false)
  const dimsRef = useRef<{ w: number; h: number; dpr: number }>({ w: 1, h: 1, dpr: 1 })

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d', { alpha: true })
    if (!ctx) return

    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const w = Math.max(1, Math.floor(rect.width * dpr))
      const h = Math.max(1, Math.floor(rect.height * dpr))
      if (canvas.width !== w) canvas.width = w
      if (canvas.height !== h) canvas.height = h
      dimsRef.current = { w, h, dpr }
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(canvas)

    const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    reducedMotionRef.current = motionQuery.matches
    const motionListener = () => {
      reducedMotionRef.current = motionQuery.matches
    }
    motionQuery.addEventListener('change', motionListener)

    startTimeRef.current = performance.now()

    /** Vertical-biased curl field. Base orientation is π/2 (straight
     *  up); a two-octave smoothed noise term bends the angle ±~75°
     *  over slow spatial + temporal frequencies. Result: dashes mostly
     *  point up, but smoothly tilt through swirl regions that drift
     *  across the canvas like air over a wing. Cheap enough to
     *  evaluate at every cell every frame. */
    const fieldAngle = (x: number, y: number, t: number): number => {
      const sx = x * 0.0085
      const sy = y * 0.0070
      const tt = t * 0.22
      const n1 =
        Math.sin(sx + tt) *
        Math.cos(sy - tt * 0.65)
      const n2 =
        Math.sin(sx * 2.1 + tt * 1.3) *
        Math.cos(sy * 1.7 + tt * 0.85) *
        0.45
      // 1.32 ≈ 75° tilt at the noise's extremes — keeps dashes from
      // ever fully flipping upside-down, which preserves the "growing
      // grass" / "iron filings" reading.
      return Math.PI / 2 + (n1 + n2) * 1.32
    }

    const draw = () => {
      const { w, h, dpr } = dimsRef.current
      ctx.clearRect(0, 0, w, h)
      const t = (performance.now() - startTimeRef.current) / 1000
      const ttick = reducedMotionRef.current ? 0 : t
      const step = spacing * dpr
      // Dashes nearly fill the cell, leaving a faint gutter that lets
      // the grid stay legible at high density.
      const dashHalf = step * 0.40
      ctx.lineCap = 'round'
      ctx.lineWidth = 0.9 * dpr

      for (let y = step * 0.5; y < h; y += step) {
        for (let x = step * 0.5; x < w; x += step) {
          const angle = fieldAngle(x, y, ttick)
          const cos = Math.cos(angle)
          const sin = Math.sin(angle)
          const x1 = x - cos * dashHalf
          const y1 = y - sin * dashHalf
          const x2 = x + cos * dashHalf
          const y2 = y + sin * dashHalf
          // Per-cell intensity from the same noise that drives angle —
          // swirl centers shimmer a touch brighter than the calm
          // regions, giving the field a subtle highlight texture.
          const wobble =
            (Math.sin(x * 0.009 + ttick * 0.3) *
              Math.cos(y * 0.008 - ttick * 0.25) +
              1) *
            0.5
          const a = alpha + wobble * alpha * 0.45
          ctx.strokeStyle = `rgba(${color}, ${a.toFixed(3)})`
          ctx.beginPath()
          ctx.moveTo(x1, y1)
          ctx.lineTo(x2, y2)
          ctx.stroke()
        }
      }
    }

    const loop = () => {
      if (active) draw()
      rafRef.current = requestAnimationFrame(loop)
    }
    draw()
    rafRef.current = requestAnimationFrame(loop)

    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
      ro.disconnect()
      motionQuery.removeEventListener('change', motionListener)
    }
  }, [active, spacing, alpha, color])

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 h-full w-full"
      aria-hidden
    />
  )
}
