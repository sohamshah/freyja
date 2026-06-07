import { useEffect, useRef } from 'react'

/**
 * ScrambleText — drop-in replacement for a text node that plays a
 * descrambler reveal animation on mount and on value change.
 *
 * Ported from the morning-room briefing mock (mockups/morning-room/
 * morning-room-i-briefing.html). Each character cycles through a pool
 * of random glyphs (by character class) at ~55ms intervals until its
 * staggered lock time fires; then it snaps to the final glyph with a
 * brief brightness + text-shadow pulse. Hover any character to retrigger
 * a shorter rescramble of just that character.
 *
 * Visual styles live in globals.css under .scr-ch / @keyframes scr-lock-pulse.
 */

const POOL_UPPER  = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
const POOL_LOWER  = 'abcdefghjkmnpqrstuvwxyz'
const POOL_DIGITS = '0123456789'
const POOL_SYMS   = '@#$%&*+=/<>?'

function pick(s: string): string {
  return s[Math.floor(Math.random() * s.length)]
}

function pickGlyph(finalCh: string): string {
  if (/[A-Z]/.test(finalCh)) return pick(POOL_UPPER)
  if (/[a-z]/.test(finalCh)) return pick(POOL_LOWER)
  if (/[0-9]/.test(finalCh)) return pick(POOL_DIGITS + POOL_SYMS)
  // Periods, commas, colons, $, %, etc — keep as-is to avoid chaotic punctuation.
  return finalCh
}

interface ScrambleTextProps {
  value: string
  className?: string
  /**
   * Timing multiplier. 1.0 = the briefing-hero default (fast reveal,
   * ~500ms for a 7-char string). Higher = slower. Use ~2.4 for dashboard
   * surfaces where the user's attention is split across many simultaneous
   * scrambles and needs more time to register the reveal.
   */
  pace?: number
}

export function ScrambleText({ value, className, pace = 1 }: ScrambleTextProps) {
  const ref = useRef<HTMLSpanElement | null>(null)

  useEffect(() => {
    const container = ref.current
    if (!container) return

    // Reduced-motion users get the plain final text instantly.
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      container.textContent = value
      return
    }

    container.textContent = ''
    container.setAttribute('aria-label', value)

    const spans: HTMLSpanElement[] = []
    for (const ch of value) {
      if (ch === ' ' || ch === '\n' || ch === '\t') {
        container.appendChild(document.createTextNode(ch))
      } else {
        const span = document.createElement('span')
        span.className = 'scr-ch'
        span.dataset.final = ch
        span.dataset.locked = '0'
        span.setAttribute('aria-hidden', 'true')
        span.textContent = pickGlyph(ch)
        spans.push(span)
        container.appendChild(span)
      }
    }

    // Stagger lock times left → right with a small random jitter so the
    // reveal doesn't feel like a marching wave. All timings scale with
    // the `pace` multiplier.
    const INITIAL_DELAY = 200 * pace
    const PER_CHAR      = 25  * pace
    const RAND_JITTER   = 100 * pace
    spans.forEach((span, i) => {
      span.dataset.lockAt = String(INITIAL_DELAY + i * PER_CHAR + Math.random() * RAND_JITTER)
    })

    // Hover-rescramble — shorter than the initial reveal.
    const RESCRAMBLE_MS = 320
    function rescramble(span: HTMLSpanElement) {
      if (span.dataset.locked !== '1') return
      if (span.dataset.rescrambling === '1') return
      span.dataset.rescrambling = '1'
      const finalCh = span.dataset.final ?? ''
      span.classList.remove('locked')
      void span.offsetWidth  // forces reflow so the lock-pulse can re-trigger

      const r0 = performance.now()
      let rlast = r0
      function rtick(now: number) {
        const elapsed = now - r0
        if (elapsed >= RESCRAMBLE_MS) {
          span.textContent = finalCh
          span.style.removeProperty('--jx')
          span.style.removeProperty('--jy')
          span.dataset.rescrambling = '0'
          span.classList.add('locked')
          return
        }
        if (now - rlast > 50) {
          rlast = now
          span.textContent = pickGlyph(finalCh)
        }
        span.style.setProperty('--jx', (Math.random() * 1.5 - 0.75).toFixed(2) + 'px')
        span.style.setProperty('--jy', (Math.random() * 1.5 - 0.75).toFixed(2) + 'px')
        requestAnimationFrame(rtick)
      }
      requestAnimationFrame(rtick)
    }
    const onEnter = (e: Event) => rescramble(e.currentTarget as HTMLSpanElement)
    for (const span of spans) span.addEventListener('mouseenter', onEnter)

    const startT = performance.now()
    let lastSwap = startT
    let cancelled = false
    let rafId = 0
    // Swap rate scales sublinearly so individual glyphs stay readable
    // at slow paces (otherwise they cycle so fast the eye sees a blur).
    const SWAP_MS = 55 * Math.max(1, Math.sqrt(pace))

    function tick(now: number) {
      if (cancelled) return
      const elapsed = now - startT
      const doSwap = (now - lastSwap) > SWAP_MS
      if (doSwap) lastSwap = now

      let stillScrambling = false
      for (const span of spans) {
        if (span.dataset.locked === '1') continue
        const lockAt = parseFloat(span.dataset.lockAt ?? '0')
        const finalCh = span.dataset.final ?? ''
        if (elapsed >= lockAt) {
          span.textContent = finalCh
          span.style.removeProperty('--jx')
          span.style.removeProperty('--jy')
          span.dataset.locked = '1'
          span.classList.add('locked')
          continue
        }
        stillScrambling = true
        if (doSwap) {
          span.textContent = pickGlyph(finalCh)
        }
        span.style.setProperty('--jx', (Math.random() * 1.5 - 0.75).toFixed(2) + 'px')
        span.style.setProperty('--jy', (Math.random() * 1.5 - 0.75).toFixed(2) + 'px')
      }

      if (stillScrambling) {
        rafId = requestAnimationFrame(tick)
      }
    }
    rafId = requestAnimationFrame(tick)

    return () => {
      cancelled = true
      if (rafId) cancelAnimationFrame(rafId)
      for (const span of spans) span.removeEventListener('mouseenter', onEnter)
    }
  }, [value])

  return (
    <span ref={ref} className={className}>
      {value}
    </span>
  )
}
