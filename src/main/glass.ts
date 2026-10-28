import { createRequire } from 'node:module'
import path from 'node:path'

/**
 * The island's backdrop blur: native/island-glass/glass.m, built by
 * scripts/build-glass.sh into dist-native/island-glass.node. It puts a
 * masked NSVisualEffectView behind a transparent window, so the island can
 * be frosted glass in its own shape (Electron's vibrancy frosts the whole
 * window rectangle). Optional: without it the island paints CSS glass.
 */
export interface Glass {
  attach(handle: Buffer): boolean
  detach(handle: Buffer): boolean
  /** The glass rectangle in CSS px from the window's top-left, with the top and bottom corner radii. */
  setShape(handle: Buffer, x: number, y: number, width: number, height: number, radiusTop: number, radiusBottom: number): boolean
  /** `material` is an NSVisualEffectMaterial raw value; -1 picks the default for the appearance. */
  setStyle(handle: Buffer, dark: boolean, material: number): boolean
  describe(handle: Buffer): string
}

/** NSVisualEffectMaterial values the island uses: under-window background on dark, popover on light. */
export const GLASS_MATERIAL = { dark: 21, light: 6 } as const

let cached: Glass | null | undefined

export function loadGlass(root: string): Glass | null {
  if (cached !== undefined) return cached
  if (process.platform !== 'darwin') {
    cached = null
    return cached
  }
  const file = path.join(root, 'dist-native', 'island-glass.node')
  try {
    const req = createRequire(__filename)
    cached = req(file) as Glass
  } catch (err) {
    console.warn(`[island] no backdrop blur (${file}): ${err instanceof Error ? err.message : String(err)}`)
    cached = null
  }
  return cached
}
