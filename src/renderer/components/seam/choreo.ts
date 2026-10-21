import { SEAM_Y } from '../../lib/seam'

/**
 * The fold, as motion.
 *
 * Gather (lines → seam): the lines dissolve and each mark flies to its own
 * place on the line: a step to its ripple, a call in a group to its strand.
 * Then the seam draws itself left to right, and each mark melts into its
 * stretch of thread as the line reaches it. The summary inks in behind.
 *
 * Unravel (seam → lines): the marks lift off the seam and drop back into
 * their lines while the lines unfold.
 *
 * Both work on the DOM directly, on an overlay inside the stretch, and
 * leave nothing behind when they finish.
 */

export interface Mark {
  u: number
  s: number | null
  x: number
  y: number
  w: number
  h: number
  el: HTMLElement
}

const easeOut = (q: number) => 1 - Math.pow(1 - q, 3)
const easeInOut = (q: number) => (q < 0.5 ? 4 * q * q * q : 1 - Math.pow(-2 * q + 2, 3) / 2)
const r2 = (n: number) => Math.round(n * 100) / 100

/** Every mark in the stretch's lines, with where it sits now. A thought's
 *  diamond belongs to its item; a step's ring or a fan names its unit. */
export function findMarks(root: HTMLElement): Array<{ el: HTMLElement; u: number; s: number | null }> {
  const out: Array<{ el: HTMLElement; u: number; s: number | null }> = []
  root.querySelectorAll<HTMLElement>('.wk-stretch-list > .wk-item').forEach((item) => {
    const u = Number(item.dataset.ui)
    const own = item.querySelectorAll<HTMLElement>('[data-u]')
    if (own.length) {
      own.forEach((el) => out.push({ el, u: Number(el.dataset.u), s: el.dataset.s != null ? Number(el.dataset.s) : null }))
      return
    }
    const diamond = item.querySelector<HTMLElement>('.thought-mark')
    if (diamond && Number.isFinite(u)) out.push({ el: diamond, u, s: null })
  })
  return out
}

export function captureMarks(root: HTMLElement): Mark[] {
  const rr = root.getBoundingClientRect()
  return findMarks(root)
    .map(({ el, u, s }) => {
      const r = el.getBoundingClientRect()
      if (!r.width || r.bottom < rr.top - 2) return null
      return { u, s, x: r.left - rr.left, y: r.top - rr.top, w: r.width, h: r.height, el }
    })
    .filter((m): m is Mark => m != null)
}

interface Geom {
  svg: SVGSVGElement
  ox: number
  oy: number
  width: number
  hits: Record<number, { cx: number; sy: number[] }>
}

function seamGeom(root: HTMLElement): Geom | null {
  const svg = root.querySelector<SVGSVGElement>('.wk-seam-row .wk-seam-svg')
  if (!svg) return null
  const rr = root.getBoundingClientRect()
  const vr = svg.getBoundingClientRect()
  const hits: Geom['hits'] = {}
  svg.querySelectorAll<SVGRectElement>('.wk-seam-hit').forEach((h) => {
    hits[Number(h.dataset.hit)] = { cx: Number(h.dataset.cx), sy: (h.dataset.sy ?? String(SEAM_Y)).split(',').map(Number) }
  })
  return { svg, ox: vr.left - rr.left, oy: vr.top - rr.top, width: Number(svg.dataset.w) || vr.width, hits }
}

function ghost(layer: HTMLElement, source: HTMLElement, w: number, h: number): HTMLElement {
  const g = document.createElement('div')
  g.className = 'wk-ghost'
  g.style.width = `${w}px`
  g.style.height = `${h}px`
  const c = source.cloneNode(true) as HTMLElement
  c.removeAttribute('data-u')
  g.append(c)
  layer.append(g)
  return g
}

/** Lines → seam. Returns a cancel function; `done` runs either way. */
export function gather(root: HTMLElement, layer: HTMLElement, marks: Mark[], done: () => void): () => void {
  const geom = seamGeom(root)
  if (!geom) {
    done()
    return () => {}
  }
  const { svg, ox, oy, width: W, hits } = geom
  root.classList.add('wk-gathering')
  const pieces = Array.from(svg.querySelectorAll<SVGGElement>('[data-piece]')).map((g) => ({
    x0: Number(g.dataset.x0),
    x1: Number(g.dataset.x1),
    paths: Array.from(g.querySelectorAll<SVGPathElement>('path')).map((el) => {
      const r = (el.dataset.r ?? '0,1').split(',').map(Number)
      return { el, a: r[0], b: r[1] }
    }),
  }))
  for (const p of pieces) for (const q of p.paths) {
    q.el.style.strokeDasharray = '1 2'
    q.el.style.strokeDashoffset = '1'
  }
  const FLY = 520
  const penStart = 620
  const D = Math.min(1250, Math.max(620, W * 4.6))
  const easePen = (p: number) => (p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2)
  const invPen = (y: number) => (y < 0.5 ? Math.sqrt(y / 2) : 1 - Math.sqrt((1 - y) / 2))
  const tAt = (x: number) => penStart + D * invPen(Math.max(0, Math.min(1, x / W)))
  const ghosts = marks
    .map((m, i) => {
      const u = hits[m.u]
      if (!u) return null
      const n = u.sy.length
      const j = m.s != null ? Math.min(m.s, n - 1) : null
      // Marks bound for a spindle land on a slant, one per strand.
      const dx = j != null && n > 1 ? (j - (n - 1) / 2) * Math.min(3.4, 22 / n) : 0
      const ty = oy + (j != null ? u.sy[j] : SEAM_Y)
      const launch = Math.min(i * 16, 110)
      const el = ghost(layer, m.el, m.w, m.h)
      el.style.transform = `translate(${r2(m.x)}px, ${r2(m.y)}px)`
      return {
        el,
        sx: m.x,
        sy: m.y,
        tx: ox + u.cx + dx - m.w / 2,
        ty: ty - m.h / 2,
        rest: j != null && n > 1 ? 0.55 : 0.75,
        launch,
        absorb: Math.max(launch + FLY, tAt(u.cx + dx)),
      }
    })
    .filter((g): g is NonNullable<typeof g> => g != null)
  const anims: Animation[] = []
  root.querySelectorAll<HTMLElement>('.wk-seam-row .wk-seam-txt, .wk-seam-row .wk-seam-diff').forEach((el) => {
    if (typeof el.animate !== 'function') return
    anims.push(
      el.animate([{ clipPath: 'inset(-3px 100% -3px 0)', opacity: 0.25 }, { clipPath: 'inset(-3px 0 -3px 0)', opacity: 1 }], {
        duration: 620,
        delay: penStart + D * 0.72,
        easing: 'cubic-bezier(.25,.8,.25,1)',
        fill: 'backwards',
      }),
    )
  })
  const END = penStart + D + 560
  const t0 = performance.now()
  let raf = 0
  let over = false
  const frame = (now: number) => {
    const t = now - t0
    const p = Math.max(0, Math.min(1, (t - penStart) / D))
    const X = W * easePen(p)
    for (const pc of pieces) {
      const span = pc.x1 - pc.x0
      const fr = span <= 0 ? (X >= pc.x0 ? 1 : 0) : Math.max(0, Math.min(1, (X - pc.x0) / span))
      for (const q of pc.paths) {
        // Strokes inside a piece can run one after another: a break is
        // drawn end, gap, end.
        const lf = Math.max(0, Math.min(1, (fr - q.a) / Math.max(0.001, q.b - q.a)))
        q.el.style.strokeDashoffset = String(r2(1 - lf))
      }
    }
    for (const g of ghosts) {
      let x = g.sx
      let y = g.sy
      let s = 1
      let op = 1
      if (t >= g.launch) {
        // Rise first, then settle sideways: an arc that clears the text below.
        const q = Math.min(1, (t - g.launch) / FLY)
        x = g.sx + (g.tx - g.sx) * easeInOut(q)
        y = g.sy + (g.ty - g.sy) * easeOut(Math.min(1, q * 1.35))
        s = 1 - (1 - g.rest) * q
        if (t > g.absorb) {
          const k = Math.min(1, (t - g.absorb) / 240)
          s = g.rest * (k < 0.3 ? 1 + k : 1.3 * (1 - (k - 0.3) / 0.7))
          op = 1 - k * k
        }
      }
      g.el.style.transform = `translate(${r2(x)}px, ${r2(y)}px) scale(${r2(Math.max(0, s))})`
      g.el.style.opacity = String(op)
    }
    if (t < END) raf = requestAnimationFrame(frame)
    else finish()
  }
  const finish = () => {
    if (over) return
    over = true
    cancelAnimationFrame(raf)
    layer.replaceChildren()
    for (const p of pieces) for (const q of p.paths) {
      q.el.style.removeProperty('stroke-dasharray')
      q.el.style.removeProperty('stroke-dashoffset')
    }
    anims.forEach((a) => a.cancel())
    root.classList.remove('wk-gathering')
    done()
  }
  raf = requestAnimationFrame(frame)
  return finish
}

/** Seam → lines. The marks lift off the seam and drop into their lines,
 *  following each line as it unfolds. */
export function unravel(root: HTMLElement, layer: HTMLElement, done: () => void): () => void {
  const geom = seamGeom(root)
  if (!geom) {
    done()
    return () => {}
  }
  const { ox, oy, hits } = geom
  const flights = findMarks(root)
    .map(({ el, u, s }, i) => {
      const unit = hits[u]
      if (!unit) return null
      const r = el.getBoundingClientRect()
      const j = s != null ? Math.min(s, unit.sy.length - 1) : null
      const g = ghost(layer, el, r.width, r.height)
      const sx = ox + unit.cx - r.width / 2
      const sy = oy + (j != null ? unit.sy[j] : SEAM_Y) - r.height / 2
      g.style.transform = `translate(${r2(sx)}px, ${r2(sy)}px) scale(.6)`
      g.style.opacity = '0'
      el.style.visibility = 'hidden'
      return { el, g, sx, sy, launch: 40 + i * 28, dur: 560, landed: false }
    })
    .filter((f): f is NonNullable<typeof f> => f != null)
  const END = Math.max(0, ...flights.map((f) => f.launch + f.dur)) + 60
  const t0 = performance.now()
  let raf = 0
  let over = false
  const frame = (now: number) => {
    const t = now - t0
    const rr = root.getBoundingClientRect()
    for (const f of flights) {
      if (f.landed) continue
      const r = f.el.getBoundingClientRect()
      const q = Math.max(0, Math.min(1, (t - f.launch) / f.dur))
      const x = f.sx + (r.left - rr.left - f.sx) * easeOut(q)
      const y = f.sy + (r.top - rr.top - f.sy) * easeInOut(q)
      f.g.style.transform = `translate(${r2(x)}px, ${r2(y)}px) scale(${r2(0.6 + 0.4 * q)})`
      f.g.style.opacity = t < f.launch ? '0' : String(Math.min(1, (t - f.launch) / 90))
      if (q >= 1) {
        f.landed = true
        f.el.style.removeProperty('visibility')
        f.g.remove()
      }
    }
    if (t < END) raf = requestAnimationFrame(frame)
    else finish()
  }
  const finish = () => {
    if (over) return
    over = true
    cancelAnimationFrame(raf)
    flights.forEach((f) => f.el.style.removeProperty('visibility'))
    layer.replaceChildren()
    done()
  }
  raf = requestAnimationFrame(frame)
  return finish
}
