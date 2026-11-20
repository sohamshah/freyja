import { useEffect, useRef } from 'react'

/**
 * Close-on-Escape for layered views: one Esc closes only the layer opened
 * last.
 *
 * Every open layer joins one stack when it becomes `active`. A single
 * capture-phase `window` listener closes the top of the stack and
 * swallows the key (`stopImmediatePropagation`), so App's bubble-phase
 * Escape handler (which closes the dashboard, the scheduler and Settings)
 * and the layers below do not also act on it.
 *
 * Order of opening decides, not order of mounting: a picker opened inside
 * a form closes before the form, and a receipt opened inside a dashboard
 * tab closes before the tab's other layers. Before, each hook added its
 * own capture listener at mount, so whichever mounted first won, and one
 * Esc in a dependency picker threw the whole add-task form away.
 */

interface Layer {
  close: () => void
}

const layers: Layer[] = []
let listening = false

function onEscape(e: KeyboardEvent): void {
  if (e.key !== 'Escape') return
  const top = layers[layers.length - 1]
  if (!top) return
  e.stopImmediatePropagation()
  e.preventDefault()
  top.close()
}

export function useEscapeClose(active: boolean, onClose: () => void): void {
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (!listening) {
      window.addEventListener('keydown', onEscape, { capture: true })
      listening = true
    }
  }, [])

  useEffect(() => {
    if (!active) return
    const layer: Layer = { close: () => onCloseRef.current() }
    layers.push(layer)
    return () => {
      const i = layers.indexOf(layer)
      if (i >= 0) layers.splice(i, 1)
    }
  }, [active])
}
