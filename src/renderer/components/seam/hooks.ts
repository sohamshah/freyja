import { useEffect, useState } from 'react'
import type { RefObject } from 'react'

/* One clock for every live timer in the conversation. A long session can
   have many running steps at once; each owning its own interval is the
   cost docs/PERFORMANCE-DEEP-DIVE.md flags for spinners. */
const tickers = new Set<(now: number) => void>()
let tickTimer: number | null = null

function startTicking() {
  if (tickTimer != null) return
  tickTimer = window.setInterval(() => {
    const now = Date.now()
    tickers.forEach((fn) => fn(now))
  }, 1000)
}

/** Date.now(), refreshed once a second while `active`. */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    tickers.add(setNow)
    startTicking()
    return () => {
      tickers.delete(setNow)
      if (tickers.size === 0 && tickTimer != null) {
        window.clearInterval(tickTimer)
        tickTimer = null
      }
    }
  }, [active])
  return now
}

/* One ResizeObserver for every seam's width. */
const widthSetters = new WeakMap<Element, (w: number) => void>()
let widthObserver: ResizeObserver | null = null

function observer(): ResizeObserver | null {
  if (typeof ResizeObserver === 'undefined') return null
  if (!widthObserver) {
    widthObserver = new ResizeObserver((entries) => {
      for (const e of entries) widthSetters.get(e.target)?.(Math.round(e.contentRect.width))
    })
  }
  return widthObserver
}

/** The width of an element, kept current. */
export function useWidth(ref: RefObject<HTMLElement>): number {
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    const ro = observer()
    if (!el || !ro) return
    widthSetters.set(el, setWidth)
    ro.observe(el)
    setWidth(Math.round(el.getBoundingClientRect().width))
    return () => {
      ro.unobserve(el)
      widthSetters.delete(el)
    }
  }, [ref])
  return width
}

export const prefersReducedMotion = (): boolean =>
  typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
