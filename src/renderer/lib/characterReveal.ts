import { useCallback, useEffect, useRef, useState } from 'react'

const REVEAL_CHARS_PER_SECOND = 72
const MAX_REVEAL_CHARS_PER_FRAME = 6

/**
 * Typewriter reveal for streamed text. While `active`, the returned string
 * grows toward `text` at a steady rate, so bursty deltas read as an even
 * stream. When `active` is false it returns `text` as is.
 *
 * The steady rate is 72 characters a second. A stream that runs faster
 * falls further and further behind it; pass `keepUpSeconds` to speed the
 * reveal up so it never trails the stream by more than about that long.
 */
export function useCharacterReveal(text: string, active: boolean, keepUpSeconds?: number): string {
  const [visibleText, setVisibleText] = useState(() => (active ? '' : text))
  const keepUpRef = useRef(keepUpSeconds)
  keepUpRef.current = keepUpSeconds
  const activeRef = useRef(active)
  const targetRef = useRef(text)
  const visibleRef = useRef(active ? '' : text)
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
      if (current === target) return

      if (!target.startsWith(current)) {
        setVisible(target)
        return
      }

      const elapsed = lastFrameRef.current > 0 ? now - lastFrameRef.current : 16.7
      lastFrameRef.current = now
      const remaining = target.length - current.length
      const keepUp = keepUpRef.current
      const rate = keepUp ? Math.max(REVEAL_CHARS_PER_SECOND, remaining / keepUp) : REVEAL_CHARS_PER_SECOND
      carryRef.current += (elapsed / 1000) * rate

      const catchUp = keepUp
        ? remaining
        : remaining > 240 ? MAX_REVEAL_CHARS_PER_FRAME : remaining > 96 ? 3 : 1
      const perFrame = keepUp ? remaining : MAX_REVEAL_CHARS_PER_FRAME
      const count = Math.min(
        remaining,
        Math.max(1, Math.min(perFrame, Math.floor(carryRef.current), catchUp)),
      )
      carryRef.current = Math.max(0, carryRef.current - count)

      let nextIndex = current.length
      for (let i = 0; i < count && nextIndex < target.length; i++) {
        nextIndex = nextCharacterBoundary(target, nextIndex)
      }
      setVisible(target.slice(0, nextIndex))

      if (nextIndex < target.length) {
        rafRef.current = requestAnimationFrame(step)
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
