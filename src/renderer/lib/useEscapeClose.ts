import { useEffect, useRef } from 'react'

/**
 * Close-on-Escape that WINS against ancestor Escape handlers.
 *
 * MissionDashboard (and App) register capture-phase `window` keydown
 * listeners that close the whole dashboard on Escape. A modal that
 * registers its own Escape listener lazily *when it opens* is added to
 * `window` LATER than those ancestor listeners, so in capture order it
 * fires after them — the dashboard is already gone before the modal's
 * handler runs. That's why hitting Esc in the card detail / a brief used
 * to blow away the entire dashboard.
 *
 * This hook instead registers ONCE at mount. React runs child effects
 * before parent effects, so a modal that lives *below* MissionDashboard
 * in the tree registers its listener first and therefore fires first in
 * the capture phase. When `active`, it swallows the event with
 * `stopImmediatePropagation()` so no ancestor handler runs; when
 * inactive it's a no-op and the ancestor handlers behave normally.
 *
 * IMPORTANT: call this unconditionally at the top of a component that is
 * ALWAYS MOUNTED while its open/closed state toggles (e.g. a modal shell
 * rendered as `<Modal open={...}/>`, or an always-mounted ancestor that
 * owns the open flag). Do NOT gate the call — or the render — behind
 * `active`, or the listener registers too late to win the ordering.
 */
export function useEscapeClose(active: boolean, onClose: () => void): void {
  const activeRef = useRef(active)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    activeRef.current = active
  }, [active])
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && activeRef.current) {
        // Beat the dashboard's same-target capture listener + swallow the
        // event so it doesn't also close the dashboard / bubble to App.
        e.stopImmediatePropagation()
        e.preventDefault()
        onCloseRef.current()
      }
    }
    window.addEventListener('keydown', onKey, { capture: true })
    return () => window.removeEventListener('keydown', onKey, { capture: true })
  }, [])
}
