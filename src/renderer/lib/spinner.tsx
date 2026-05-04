import { useEffect, useRef, useState } from 'react'
import spinners, { type BrailleSpinnerName, type Spinner } from 'unicode-animations'

/**
 * React hook that ticks through a unicode-animations spinner.
 * Returns the current frame string.
 */
export function useSpinnerFrame(
  name: BrailleSpinnerName = 'braille',
  active: boolean = true,
): string {
  const spinner: Spinner = spinners[name] ?? spinners.braille
  const [frame, setFrame] = useState(spinner.frames[0] ?? '')
  const indexRef = useRef(0)

  useEffect(() => {
    if (!active) {
      indexRef.current = 0
      setFrame(spinner.frames[0] ?? '')
      return
    }
    let mounted = true
    const interval = Math.max(30, spinner.interval)
    const tick = () => {
      if (!mounted) return
      indexRef.current = (indexRef.current + 1) % spinner.frames.length
      setFrame(spinner.frames[indexRef.current])
    }
    const id = window.setInterval(tick, interval)
    return () => {
      mounted = false
      window.clearInterval(id)
    }
  }, [active, name, spinner])

  return frame
}

/** Render a spinner as a span element. */
export function Spinner({
  name = 'braille',
  active = true,
  className = '',
  title,
}: {
  name?: BrailleSpinnerName
  active?: boolean
  className?: string
  title?: string
}) {
  const frame = useSpinnerFrame(name, active)
  return (
    <span
      className={`font-mono tabular-nums ${className}`}
      title={title}
      aria-hidden
      style={{ whiteSpace: 'pre' }}
    >
      {frame}
    </span>
  )
}

/** Friendly names for the menus. */
export const SPINNER_NAMES: BrailleSpinnerName[] = [
  'braille',
  'braillewave',
  'dna',
  'scan',
  'rain',
  'scanline',
  'pulse',
  'snake',
  'sparkle',
  'cascade',
  'columns',
  'orbit',
  'breathe',
  'waverows',
  'checkerboard',
  'helix',
  'fillsweep',
  'diagswipe',
]
