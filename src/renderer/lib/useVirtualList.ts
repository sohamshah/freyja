import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Fixed-row windowing for long lists.
 *
 * The app ships six runtime dependencies and had no virtualization anywhere —
 * long lists were handled with `content-visibility` on conversation rows and
 * hard `slice(0, N)` caps everywhere else. The cross-session artifact browser
 * is the first surface where neither works: it has to scroll thousands of rows
 * and every row is interactive, so a cap would hide real data and
 * `content-visibility` still pays the DOM cost.
 *
 * Deliberately fixed-height. Measured/variable rows need a resize observer per
 * row and a running offset table, which is a lot of machinery to buy something
 * a uniform row height gives for free — and uniform rows are what the browser's
 * design calls for anyway.
 */

export interface WindowRange {
  /** First index to render (already includes overscan). */
  start: number
  /** One past the last index to render. */
  end: number
  /** Total scrollable height, so the scrollbar reflects the full list. */
  totalHeight: number
  /** Pixel offset to translate the rendered slab by. */
  offsetY: number
}

/**
 * Which slice of a uniform-height list is on screen.
 *
 * Pure so the arithmetic can be driven directly from a test — see
 * `test-virtual-list.mjs`.
 */
export function windowRange(opts: {
  count: number
  rowHeight: number
  scrollTop: number
  viewportHeight: number
  /** Extra rows rendered above and below, so a fast scroll doesn't flash. */
  overscan?: number
}): WindowRange {
  const { count, rowHeight } = opts
  const overscan = opts.overscan ?? 8
  const totalHeight = Math.max(0, count * rowHeight)

  if (count <= 0 || rowHeight <= 0) {
    return { start: 0, end: 0, totalHeight: 0, offsetY: 0 }
  }

  // A viewport height of 0 means the container hasn't been measured yet.
  // Render a first screenful rather than nothing, so the list isn't blank for
  // a frame on mount.
  const viewportHeight = opts.viewportHeight > 0 ? opts.viewportHeight : rowHeight * 12
  const scrollTop = Math.max(0, Math.min(opts.scrollTop, Math.max(0, totalHeight - viewportHeight)))

  const firstVisible = Math.floor(scrollTop / rowHeight)
  const lastVisible = Math.ceil((scrollTop + viewportHeight) / rowHeight)

  const start = Math.max(0, firstVisible - overscan)
  const end = Math.min(count, lastVisible + overscan)

  return { start, end, totalHeight, offsetY: start * rowHeight }
}

export interface VirtualList extends WindowRange {
  /**
   * Attach to the scrolling container. A CALLBACK ref, not an object ref: the
   * container is frequently mounted later than the hook (the browser renders
   * nothing until it is open; the source pane renders nothing until the file
   * has loaded). An effect with an empty dependency list runs once, before the
   * node exists, and would never measure it — leaving the viewport height at
   * zero and the window pinned to a fallback screenful no matter how tall the
   * container actually is.
   */
  ref: (node: HTMLDivElement | null) => void
  onScroll: () => void
  /** Scroll so `index` is fully visible, moving as little as possible. */
  scrollToIndex: (index: number) => void
}

export function useVirtualList(opts: {
  count: number
  rowHeight: number
  overscan?: number
  /** Breathing room kept between a revealed row and the viewport edge. */
  pad?: number
}): VirtualList {
  const { count, rowHeight } = opts
  const pad = opts.pad ?? 8
  const nodeRef = useRef<HTMLDivElement | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewportHeight, setViewportHeight] = useState(0)
  const frame = useRef<number | null>(null)

  // Measure on attach, and keep measuring. A plain window resize listener
  // misses the panel drag handles and the activity panel collapsing, both of
  // which resize this container without resizing the window.
  const ref = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    nodeRef.current = node
    if (!node) return
    setViewportHeight(node.clientHeight)
    // Whatever scroll position the container came back with — it is reused
    // across mounts when the browser is closed and reopened.
    setScrollTop(node.scrollTop)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setViewportHeight(node.clientHeight))
    ro.observe(node)
    observerRef.current = ro
  }, [])

  useEffect(() => {
    return () => {
      observerRef.current?.disconnect()
      observerRef.current = null
    }
  }, [])

  // Coalesce scroll events to one state update per frame — the handler fires
  // far more often than React can usefully re-render.
  const onScroll = useCallback(() => {
    if (frame.current != null) return
    frame.current = requestAnimationFrame(() => {
      frame.current = null
      const el = nodeRef.current
      if (el) setScrollTop(el.scrollTop)
    })
  }, [])

  useEffect(() => {
    return () => {
      if (frame.current != null) cancelAnimationFrame(frame.current)
    }
  }, [])

  const scrollToIndex = useCallback(
    (index: number) => {
      const el = nodeRef.current
      if (!el || index < 0) return
      const rowTop = index * rowHeight
      const rowBottom = rowTop + rowHeight
      const viewTop = el.scrollTop
      const viewBottom = viewTop + el.clientHeight
      if (rowTop - pad < viewTop) {
        el.scrollTop = Math.max(0, rowTop - pad)
      } else if (rowBottom + pad > viewBottom) {
        el.scrollTop = rowBottom + pad - el.clientHeight
      }
    },
    [rowHeight, pad],
  )

  return {
    ref,
    onScroll,
    scrollToIndex,
    ...windowRange({
      count,
      rowHeight,
      scrollTop,
      viewportHeight,
      overscan: opts.overscan,
    }),
  }
}
