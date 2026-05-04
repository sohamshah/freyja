import { useEffect, useRef } from 'react'

/**
 * An ambient volumetric topography mesh — a slowly rotating 3D wireframe
 * terrain driven by fractal brownian motion, with soft depth-of-field and
 * gentle chromatic aberration. Renders to a 2D canvas (no WebGL) so it can
 * live inside the hero welcome card without pulling in Three.js.
 *
 * Tuned to be meditative: much slower time/rotation than a typical demo,
 * denser grid, theme-matched cool-blue palette, retina-sharp via DPR, and
 * automatically pauses when the tab/window is hidden.
 */

type Proj = { x: number; y: number; z: number; scale: number }

interface Props {
  height?: number
}

export function TopographyMesh({ height = 260 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d', { alpha: true })
    if (!ctx) return

    // Retina sharpness — cap at 2 so we don't destroy perf on 3x displays.
    const dpr = Math.min(window.devicePixelRatio || 1, 2)

    let cssW = 0
    let cssH = height
    let centerX = 0
    let centerY = 0

    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      cssW = Math.max(1, rect.width)
      cssH = Math.max(1, rect.height)
      canvas.width = Math.floor(cssW * dpr)
      canvas.height = Math.floor(cssH * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      centerX = cssW / 2
      centerY = cssH / 2
    }
    resize()

    const ro = new ResizeObserver(resize)
    ro.observe(canvas)

    // ── Engine config ────────────────────────────────────────────
    // Reduced from 18→12 for perf (55% fewer draw calls).
    const GRID_SIZE = 12
    const SPACING = 26
    const BASE_Y = 130
    const ELEVATION_AMP = 130
    const FOV = 820
    const CAMERA_Z = 1800 // further camera = smaller projected mesh
    const FOCAL_PLANE = 1800

    let time = 0
    const angleX = 0.76 // tilt — locked
    let angleY = 0.5

    // ── Fractal brownian motion (organic rolling terrain) ────────
    const fbm = (x: number, z: number, t: number) => {
      let v = 0
      let amp = 0.5
      let freq = 0.017
      for (let i = 0; i < 4; i++) {
        v += amp * (Math.sin(x * freq + t) * Math.cos(z * freq - t * 0.7))
        amp *= 0.55
        freq *= 2.1
      }
      return v
    }

    // ── 3D → 2D projection ───────────────────────────────────────
    const project = (x: number, y: number, z: number): Proj => {
      const cosY = Math.cos(angleY)
      const sinY = Math.sin(angleY)
      const rx1 = x * cosY - z * sinY
      const rz1 = z * cosY + x * sinY

      const cosX = Math.cos(angleX)
      const sinX = Math.sin(angleX)
      const ry2 = y * cosX - rz1 * sinX
      const rz2 = rz1 * cosX + y * sinX

      let zPos = rz2 + CAMERA_Z
      if (zPos < 1) zPos = 1
      const scale = FOV / zPos
      return {
        x: centerX + rx1 * scale,
        y: centerY + ry2 * scale - 20,
        z: zPos,
        scale,
      }
    }

    // ── Draw primitives with DOF (no chromatic aberration — 3x cheaper) ──
    const drawLine = (p1: Proj, p2: Proj, isVertical: boolean) => {
      const avgZ = (p1.z + p2.z) / 2
      const dof = Math.abs(avgZ - FOCAL_PLANE)
      let opacity = Math.max(0.018, 0.68 - dof * 0.00085)
      const thickness = Math.max(0.5, 0.9 + dof * 0.0042)
      if (isVertical) opacity *= 0.3

      ctx.beginPath()
      ctx.moveTo(p1.x, p1.y)
      ctx.lineTo(p2.x, p2.y)
      ctx.strokeStyle = `rgba(200, 220, 245, ${(opacity * 0.75).toFixed(3)})`
      ctx.lineWidth = thickness * 0.6
      ctx.stroke()
    }

    const drawNode = (p: Proj, isBase: boolean) => {
      const dof = Math.abs(p.z - FOCAL_PLANE)
      let opacity = Math.max(0.04, 0.82 - dof * 0.00085)
      const radius = Math.max(0.4, (isBase ? 0.8 : 2.0) + dof * 0.011)
      if (isBase) opacity *= 0.28

      ctx.fillStyle = `rgba(232, 242, 255, ${opacity.toFixed(3)})`
      ctx.beginPath()
      ctx.arc(p.x, p.y, radius * 0.78, 0, Math.PI * 2)
      ctx.fill()
    }

    // ── Main render loop (throttled to ~15fps) ─────────────────
    let raf = 0
    let running = true
    let lastRender = 0
    const FRAME_INTERVAL = 66 // ~15fps — meditative animation doesn't need 60

    type RenderItem =
      | { type: 'line'; p1: Proj; p2: Proj; isVert: boolean; z: number }
      | { type: 'node'; p: Proj; isBase: boolean; z: number }

    const render = (now: number = 0) => {
      if (!running) return
      raf = requestAnimationFrame(render)

      // Throttle: skip frames if not enough time has passed
      if (now - lastRender < FRAME_INTERVAL) return
      lastRender = now

      // Clear to transparent so the card's glass surface shows through.
      ctx.clearRect(0, 0, cssW, cssH)

      // Build surface + base grids
      const surfaceGrid: Proj[][] = []
      const baseGrid: Proj[][] = []
      const offset = ((GRID_SIZE - 1) * SPACING) / 2

      for (let x = 0; x < GRID_SIZE; x++) {
        surfaceGrid[x] = []
        baseGrid[x] = []
        for (let z = 0; z < GRID_SIZE; z++) {
          const worldX = x * SPACING - offset
          const worldZ = z * SPACING - offset
          const elevation = fbm(worldX, worldZ, time)
          const surfaceY = -elevation * ELEVATION_AMP - 20
          surfaceGrid[x][z] = project(worldX, surfaceY, worldZ)
          baseGrid[x][z] = project(worldX, BASE_Y, worldZ)
        }
      }

      // Painter's algorithm — sort back to front so nearer ridges pop.
      const queue: RenderItem[] = []
      for (let x = 0; x < GRID_SIZE; x++) {
        for (let z = 0; z < GRID_SIZE; z++) {
          const sP = surfaceGrid[x][z]
          const bP = baseGrid[x][z]

          queue.push({ type: 'line', p1: sP, p2: bP, isVert: true, z: (sP.z + bP.z) / 2 })
          queue.push({ type: 'node', p: sP, isBase: false, z: sP.z })
          queue.push({ type: 'node', p: bP, isBase: true, z: bP.z })

          if (x < GRID_SIZE - 1) {
            const sN = surfaceGrid[x + 1][z]
            const bN = baseGrid[x + 1][z]
            queue.push({ type: 'line', p1: sP, p2: sN, isVert: false, z: (sP.z + sN.z) / 2 })
            queue.push({ type: 'line', p1: bP, p2: bN, isVert: false, z: (bP.z + bN.z) / 2 })
          }
          if (z < GRID_SIZE - 1) {
            const sN = surfaceGrid[x][z + 1]
            const bN = baseGrid[x][z + 1]
            queue.push({ type: 'line', p1: sP, p2: sN, isVert: false, z: (sP.z + sN.z) / 2 })
            queue.push({ type: 'line', p1: bP, p2: bN, isVert: false, z: (bP.z + bN.z) / 2 })
          }
        }
      }

      queue.sort((a, b) => b.z - a.z)

      for (const item of queue) {
        if (item.type === 'line') drawLine(item.p1, item.p2, item.isVert)
        else drawNode(item.p, item.isBase)
      }

      // Meditative pace — much slower than the reference.
      time += 0.0045
      angleY += 0.00085
    }

    raf = requestAnimationFrame(render)

    // Pause when the window/tab is hidden to save cycles.
    const onVisibility = () => {
      if (document.hidden) {
        running = false
        cancelAnimationFrame(raf)
      } else if (!running) {
        running = true
        raf = requestAnimationFrame(render)
      }
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      running = false
      cancelAnimationFrame(raf)
      ro.disconnect()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [height])

  return (
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: `${height}px`, display: 'block' }}
    />
  )
}
