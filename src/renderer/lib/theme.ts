import { create } from 'zustand'
import type { AppearanceTheme } from '@shared/events'

/**
 * Appearance — dark (the original shell) and light (the new one), with
 * `system` following macOS.
 *
 * Who owns what:
 *   · The PREFERENCE (`system` | `dark` | `light`) lives in
 *     `~/.freyja/settings.json` under `appearance.theme` and is read
 *     through the harness store like every other desktop setting. In a
 *     plain browser (design-review demo mode, no preload) it lives in
 *     localStorage instead.
 *   · The EFFECTIVE theme (`dark` | `light`) is what the page paints.
 *     Inside Electron the main process mirrors the preference into
 *     `nativeTheme.themeSource`, which drives both the macOS vibrancy
 *     material behind the window and `prefers-color-scheme` in here —
 *     so the media query is the single source of truth and the two
 *     layers can never disagree. In a browser the preference is applied
 *     directly on top of the media query.
 *
 * The effective theme is written to `<html data-theme>` (+ the CSS
 * `color-scheme`), which every token in globals.css keys off, and is
 * mirrored into a tiny store so canvas / WebGL painters that can't read
 * CSS variables at draw time can subscribe to it.
 */
export type EffectiveTheme = 'dark' | 'light'

const STORAGE_KEY = 'freyja.appearance.theme'
const MEDIA_LIGHT = '(prefers-color-scheme: light)'

export const APPEARANCE_THEMES: readonly AppearanceTheme[] = ['system', 'dark', 'light']

export function isAppearanceTheme(value: unknown): value is AppearanceTheme {
  return value === 'system' || value === 'dark' || value === 'light'
}

/** What the page should paint for a preference given what the OS wants. */
export function resolveEffectiveTheme(
  preference: AppearanceTheme,
  systemPrefersLight: boolean,
): EffectiveTheme {
  if (preference === 'light') return 'light'
  if (preference === 'dark') return 'dark'
  return systemPrefersLight ? 'light' : 'dark'
}

/** The explicit theme a quick toggle should switch to from `current`. */
export function oppositeTheme(current: EffectiveTheme): EffectiveTheme {
  return current === 'light' ? 'dark' : 'light'
}

interface ThemeState {
  effective: EffectiveTheme
}

export const useThemeStore = create<ThemeState>(() => ({ effective: 'dark' }))

/** Subscribe a component to the effective theme (dark | light). */
export function useEffectiveTheme(): EffectiveTheme {
  return useThemeStore((s) => s.effective)
}

export function getEffectiveTheme(): EffectiveTheme {
  return useThemeStore.getState().effective
}

function hasElectronHarness(): boolean {
  return typeof window !== 'undefined' && !!(window as any).harness?.settingsUpdate
}

function systemPrefersLight(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia(MEDIA_LIGHT).matches
  } catch {
    return false
  }
}

function readStoredPreference(): AppearanceTheme {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return isAppearanceTheme(raw) ? raw : 'system'
  } catch {
    return 'system'
  }
}

/** Browser-only preference (Electron keeps it in settings.json). */
let browserPreference: AppearanceTheme = 'system'

/** Paint `theme` on the document and notify subscribers. Idempotent. */
export function applyEffectiveTheme(theme: EffectiveTheme): void {
  if (typeof document !== 'undefined') {
    const root = document.documentElement
    if (root.dataset.theme !== theme) root.dataset.theme = theme
    // Native widgets (scrollbars, <select>, form controls, the
    // <input type=number> spinners) follow color-scheme, not our tokens.
    if (root.style.colorScheme !== theme) root.style.colorScheme = theme
  }
  if (useThemeStore.getState().effective !== theme) {
    useThemeStore.setState({ effective: theme })
  }
}

function recompute(): void {
  const pref = hasElectronHarness() ? 'system' : browserPreference
  applyEffectiveTheme(resolveEffectiveTheme(pref, systemPrefersLight()))
}

let initialized = false

/**
 * Called synchronously before the first render so the first paint is
 * already in the right theme (no dark flash on a light launch). Listens
 * to the OS / Electron media query from then on.
 */
export function initTheme(): void {
  if (initialized) return
  initialized = true
  if (!hasElectronHarness()) browserPreference = readStoredPreference()
  recompute()
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    try {
      const mq = window.matchMedia(MEDIA_LIGHT)
      const onChange = () => recompute()
      if (typeof mq.addEventListener === 'function') mq.addEventListener('change', onChange)
      else if (typeof (mq as any).addListener === 'function') (mq as any).addListener(onChange)
    } catch {
      /* no media query support — stay on the boot theme */
    }
  }
}

/**
 * Browser-mode counterpart of the Electron settings write: remember the
 * preference locally and repaint. In Electron this is a no-op because
 * the main process answers the settings write by flipping
 * `nativeTheme.themeSource`, and the media query listener above repaints.
 */
export function applyBrowserPreference(preference: AppearanceTheme): void {
  if (hasElectronHarness()) return
  browserPreference = preference
  try {
    localStorage.setItem(STORAGE_KEY, preference)
  } catch {
    /* private mode etc. — the choice just doesn't survive a reload */
  }
  recompute()
}

/**
 * Read a design token off the root element, for painters that need a
 * concrete colour string (canvas 2D, WebGL uniforms). CSS variables in
 * SVG attributes and inline styles resolve on their own; only raster
 * code needs this. Returns `fallback` when the token is missing (tests,
 * detached documents).
 */
export function cssToken(name: string, fallback = ''): string {
  if (typeof document === 'undefined') return fallback
  const root = document.documentElement
  try {
    const v = getComputedStyle(root).getPropertyValue(name).trim()
    if (v) return v
  } catch {
    /* fall through to the inline style */
  }
  // Inline overrides (and jsdom, whose computed style skips custom
  // properties) still resolve through the element's own declaration.
  try {
    const inline = root.style.getPropertyValue(name).trim()
    return inline || fallback
  } catch {
    return fallback
  }
}

/** `--x: 168 212 252` → [168, 212, 252]. */
export function cssTokenRgb(name: string, fallback: [number, number, number]): [number, number, number] {
  const raw = cssToken(name)
  if (!raw) return fallback
  const parts = raw.split(/[\s,]+/).map(Number)
  if (parts.length < 3 || parts.some((n) => !Number.isFinite(n))) return fallback
  return [parts[0], parts[1], parts[2]]
}
