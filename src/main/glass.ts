import { createRequire } from 'node:module'
import path from 'node:path'

/**
 * The island's backdrop: native/island-glass/glass.m, built by
 * scripts/build-glass.sh into dist-native/island-glass.node. It puts a pane
 * of system glass behind a transparent window in exactly the island's shape:
 * Liquid Glass (NSGlassEffectView) on macOS 26, a masked blur
 * (NSVisualEffectView) before that. Electron's own vibrancy frosts the whole
 * window rectangle, hence the shim. Optional: without it the island paints a
 * solid pane.
 */
export type GlassKind = 'liquid' | 'blur'

export interface Glass {
  /**
   * Puts the glass behind the window, made for the shell's appearance; says
   * which kind it got, or null when the handle is not a view. `preferBlur`
   * asks for the legacy blur even where Liquid Glass exists.
   */
  attach(handle: Buffer, dark: boolean, preferBlur?: boolean): GlassKind | null
  detach(handle: Buffer): boolean
  /** The glass rectangle in CSS px from the window's top-left, with the top and bottom corner radii. */
  setShape(handle: Buffer, x: number, y: number, width: number, height: number, radiusTop: number, radiusBottom: number): boolean
  /**
   * Liquid: `material` is the NSGlassEffectViewStyle (0 regular, 1 clear) and
   * the tint (0–255 and alpha) washes the glass. Blur: `material` is an
   * NSVisualEffectMaterial raw value, -1 for the appearance's default.
   */
  setStyle(handle: Buffer, dark: boolean, material: number, tintR?: number, tintG?: number, tintB?: number, tintA?: number): boolean
  describe(handle: Buffer): string
}

/** NSVisualEffectMaterial values for the legacy blur: under-window background on dark, popover on light. */
export const BLUR_MATERIAL = { dark: 21, light: 6 } as const
/** Liquid Glass: the regular style, washed toward the shell's ground so the words sit on it. */
export const LIQUID_STYLE = { dark: 0, light: 0 } as const
export const LIQUID_TINT: Record<'dark' | 'light', [number, number, number, number]> = {
  dark: [0, 0, 0, 0],
  light: [0, 0, 0, 0],
}

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
    console.warn(`[island] no backdrop glass (${file}): ${err instanceof Error ? err.message : String(err)}`)
    cached = null
  }
  return cached
}
