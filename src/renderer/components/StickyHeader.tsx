import { useEffect, useRef, useState } from 'react'

/**
 * Sticky section header with a "chip" morph animation.
 *
 * When the user scrolls past the header's natural position inside its
 * scroll container, the full-bleed frosted bar collapses inward into
 * a centered pill — same content, just visually re-framed so the user
 * always knows what section they're in without it occupying full width.
 *
 * Detection uses an IntersectionObserver on a 1px sentinel placed
 * immediately above the header; when the sentinel scrolls out of view,
 * the header is "stuck" and we add the `data-stuck="true"` attribute
 * that CSS hooks into for the morph.
 */
export function StickyHeader({
  children,
  topOffset = 0,
}: {
  children: React.ReactNode
  /** Pixels from the top of the scroll container the header sits at.
   *  Lets stacked sections sit just below another fixed element. */
  topOffset?: number
}) {
  const sentinelRef = useRef<HTMLDivElement | null>(null)
  const [stuck, setStuck] = useState(false)

  useEffect(() => {
    const sentinel = sentinelRef.current
    if (!sentinel) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        // Sentinel intersecting = at natural position; not intersecting
        // = scrolled past = stuck. The 1px sentinel height + viewport
        // root means this fires the moment the header reaches the top
        // of its container.
        setStuck(!entry.isIntersecting)
      },
      { threshold: [0, 1] },
    )
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [])

  return (
    <>
      <div ref={sentinelRef} aria-hidden="true" className="h-px w-full" />
      <div
        data-stuck={stuck}
        className="sticky-section-header sticky z-[5]"
        style={{ top: `${topOffset}px` }}
      >
        <div className="sticky-section-inner">{children}</div>
      </div>
    </>
  )
}
