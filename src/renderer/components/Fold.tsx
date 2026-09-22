import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { CSSProperties, ReactNode, TransitionEvent } from 'react'

/**
 * Fold — the animated disclosure every collapsible section shares.
 *
 * Why it exists: `{open && children}` snaps. The list appears in one
 * paint and whatever is laid out around it (the sidebar's bottom rail,
 * the sessions pane above it) jumps in the same frame. Fold animates
 * the outer grid track (`grid-template-rows: 0fr → 1fr`) so height is
 * never animated directly, and everything that depends on that height
 * — the rail growing, the section names above riding upward — follows
 * the fold at one continuous rate.
 *
 * Choreography lives in globals.css under "Fold"; this component only
 * owns the state machine:
 *
 *   · `open` flips true  → children mount THIS commit, and `data-open`
 *     flips on the next frame so the 0fr→1fr transition has content to
 *     measure. Rows then stagger in via CSS keyed on
 *     `[data-open="true"][data-settled="false"]`.
 *   · `open` flips false → `data-open` drops immediately (sheet folds,
 *     rows fade) but the last-rendered children stay mounted until the
 *     grid transition ends, then unmount. Callers whose children list
 *     is derived from `open` (the sessions tree) still get a close
 *     animation because the snapshot is what's rendered on the way out.
 *   · `data-settled` marks the resting open state: row animations stop
 *     matching (so a re-sorted or newly inserted row never replays the
 *     entrance) and the inner overflow is released so focus rings and
 *     hover shadows aren't clipped.
 */
export function Fold({
  open,
  children,
  className = '',
  innerClassName = '',
  stagger = true,
  style,
}: {
  open: boolean
  children?: ReactNode
  /** Extra classes on the outer grid (e.g. `fold-spine`). */
  className?: string
  /** Extra classes on the inner clip box — padding lives here. */
  innerClassName?: string
  /** Apply the direct-child stagger (`fold-rows`) to the inner box.
   *  Turn off when the rows live deeper and carry `fold-rows` themselves. */
  stagger?: boolean
  style?: CSSProperties
}) {
  const [armed, setArmed] = useState(open)
  const [settled, setSettled] = useState(open)
  const [lingering, setLingering] = useState(false)
  const first = useRef(true)
  const lastChildren = useRef<ReactNode>(children)
  if (open) lastChildren.current = children

  useLayoutEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    if (open) {
      setLingering(false)
      setSettled(false)
      const id = requestAnimationFrame(() => setArmed(true))
      return () => cancelAnimationFrame(id)
    }
    setArmed(false)
    setSettled(false)
    setLingering(true)
    // Fallback: if the transition never fires (display:none ancestor,
    // reduced-motion at 0ms) the snapshot must not stay mounted forever.
    const t = window.setTimeout(() => setLingering(false), 700)
    return () => window.clearTimeout(t)
  }, [open])

  // Settle a beat AFTER the sheet stops so the row choreography (which
  // runs longer than the fold when there are many rows) isn't cut off
  // by its selector un-matching.
  const settleTimer = useRef<number | null>(null)
  useEffect(() => () => {
    if (settleTimer.current != null) window.clearTimeout(settleTimer.current)
  }, [])

  const onTransitionEnd = (e: TransitionEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget || e.propertyName !== 'grid-template-rows') return
    if (open) {
      if (settleTimer.current != null) window.clearTimeout(settleTimer.current)
      settleTimer.current = window.setTimeout(() => setSettled(true), 240)
    } else {
      setLingering(false)
    }
  }

  const mounted = open || lingering
  return (
    <div
      className={`fold ${className}`}
      data-open={open && armed ? 'true' : 'false'}
      data-settled={open && settled ? 'true' : 'false'}
      onTransitionEnd={onTransitionEnd}
      style={style}
    >
      <div className={`fold-in ${stagger ? 'fold-rows' : ''} ${innerClassName}`}>
        {mounted ? (open ? children : lastChildren.current) : null}
      </div>
    </div>
  )
}

/**
 * FoldCaret — the disclosure triangle. Turns 90° with an expo ease and,
 * from the second toggle on, blinks accent once as it turns: the same
 * lamp the running-session indicator uses, here meaning "the press was
 * taken". The first render never blinks, so sections that boot open
 * don't all flash at once.
 */
export function FoldCaret({
  open,
  size = 8,
  className = '',
}: {
  open: boolean
  size?: number
  className?: string
}) {
  const [touched, setTouched] = useState(false)
  const first = useRef(true)
  useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    setTouched(true)
  }, [open])
  return (
    <svg
      viewBox="0 0 10 10"
      width={size}
      height={size}
      aria-hidden="true"
      className={`fold-caret ${className}`}
      data-open={open ? 'true' : 'false'}
      data-lamp={touched ? 'true' : 'false'}
    >
      <path d="M3 2 L7 5 L3 8 Z" fill="currentColor" />
      <path className="fold-lamp" d="M3 2 L7 5 L3 8 Z" />
    </svg>
  )
}
