import { useCallback, useEffect, useRef, useState } from 'react'

/** The slowest the reveal ever runs, for a stream that trickles. */
const REVEAL_CHARS_PER_SECOND = 72
/** The reveal never trails the stream by much more than this. */
const KEEP_UP_SECONDS = 0.6
/** A frame longer than this (a stall, a hidden window) counts as this long,
 *  so the next frame does not dump a burst of text. */
const MAX_FRAME_MS = 50

/**
 * Typewriter reveal for streamed text. While `active`, the returned string
 * grows toward `text` frame by frame, so bursty deltas read as an even
 * stream. When `active` is false it returns `text` as is.
 *
 * The pace follows the stream: the further the shown text trails, the
 * faster it moves (about `KEEP_UP_SECONDS` to close any gap), with a floor
 * of 72 characters a second. Text already there when the hook mounts is
 * shown at once; only what arrives after is revealed.
 */
export function useCharacterReveal(text: string, active: boolean): string {
  const [visibleText, setVisibleText] = useState(text)
  const activeRef = useRef(active)
  const targetRef = useRef(text)
  const visibleRef = useRef(text)
  const rafRef = useRef<number | null>(null)
  const lastFrameRef = useRef(0)
  const carryRef = useRef(0)

  const cancelFrame = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
  }, [])

  const setVisible = useCallback((next: string) => {
    visibleRef.current = next
    setVisibleText(next)
  }, [])

  const step = useCallback(
    (now: number) => {
      rafRef.current = null
      if (!activeRef.current) return

      const target = targetRef.current
      const current = visibleRef.current
      if (current === target || !target.startsWith(current)) {
        if (current !== target) setVisible(target)
        lastFrameRef.current = 0
        return
      }

      const elapsed = lastFrameRef.current > 0 ? Math.min(MAX_FRAME_MS, now - lastFrameRef.current) : 16.7
      lastFrameRef.current = now
      const remaining = target.length - current.length
      const rate = Math.max(REVEAL_CHARS_PER_SECOND, remaining / KEEP_UP_SECONDS)
      carryRef.current += (elapsed / 1000) * rate
      const count = Math.min(remaining, Math.max(1, Math.floor(carryRef.current)))
      carryRef.current = Math.max(0, carryRef.current - count)

      let nextIndex = current.length
      for (let i = 0; i < count && nextIndex < target.length; i++) {
        nextIndex = nextCharacterBoundary(target, nextIndex)
      }
      setVisible(target.slice(0, nextIndex))

      if (nextIndex < target.length) {
        rafRef.current = requestAnimationFrame(step)
      } else {
        // Caught up: the wait for the next delta is not reveal time.
        lastFrameRef.current = 0
      }
    },
    [setVisible],
  )

  const schedule = useCallback(() => {
    if (rafRef.current != null || !activeRef.current) return
    rafRef.current = requestAnimationFrame(step)
  }, [step])

  useEffect(() => {
    activeRef.current = active
    targetRef.current = text

    if (!active) {
      cancelFrame()
      lastFrameRef.current = 0
      carryRef.current = 0
      if (visibleRef.current !== text) setVisible(text)
      return
    }

    if (!text.startsWith(visibleRef.current)) {
      setVisible('')
    }
    if (visibleRef.current !== text) schedule()
  }, [active, cancelFrame, schedule, setVisible, text])

  useEffect(() => cancelFrame, [cancelFrame])

  return visibleText
}

function nextCharacterBoundary(text: string, index: number): number {
  if (index >= text.length) return text.length
  const codePoint = text.codePointAt(index)
  if (codePoint === undefined) return index + 1
  return index + (codePoint > 0xffff ? 2 : 1)
}
