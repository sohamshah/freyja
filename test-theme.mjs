// Appearance: the preference → effective-theme resolution, the quick
// toggle, the document attribute the tokens key off, and the browser-mode
// path (no Electron preload) that keeps the preference in localStorage.
// Run from the repo root:
//   npx tsx test-theme.mjs

import assert from 'node:assert/strict'

// Needs a DOM, so install jsdom first (it is not a project dependency):
//   npm i --no-save jsdom
let JSDOM
try {
  ({ JSDOM } = await import('jsdom'))
} catch {
  console.error('jsdom not found — run:  npm i --no-save jsdom')
  process.exit(2)
}
// A real origin, or jsdom refuses localStorage.
const dom = new JSDOM('<!doctype html><html><body></body></html>', { pretendToBeVisual: true, url: 'http://localhost/' })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.localStorage = dom.window.localStorage
// A controllable prefers-color-scheme.
let systemLight = false
const listeners = new Set()
dom.window.matchMedia = (q) => ({
  matches: q.includes('light') ? systemLight : !systemLight,
  media: q,
  addEventListener: (_t, fn) => listeners.add(fn),
  removeEventListener: (_t, fn) => listeners.delete(fn),
})
const flipSystem = (light) => {
  systemLight = light
  for (const fn of listeners) fn()
}

const theme = await import('./src/renderer/lib/theme.ts')

// ── pure resolution ────────────────────────────────────────────────
assert.equal(theme.resolveEffectiveTheme('dark', true), 'dark')
assert.equal(theme.resolveEffectiveTheme('light', false), 'light')
assert.equal(theme.resolveEffectiveTheme('system', true), 'light')
assert.equal(theme.resolveEffectiveTheme('system', false), 'dark')
assert.equal(theme.oppositeTheme('dark'), 'light')
assert.equal(theme.oppositeTheme('light'), 'dark')
assert.ok(theme.isAppearanceTheme('system') && theme.isAppearanceTheme('light'))
assert.ok(!theme.isAppearanceTheme('auto') && !theme.isAppearanceTheme(null))

// ── browser mode: boot follows the OS, the preference pins it ──────
theme.initTheme()
assert.equal(document.documentElement.dataset.theme, 'dark', 'boots dark when the OS is dark')
assert.equal(document.documentElement.style.colorScheme, 'dark')
assert.equal(theme.getEffectiveTheme(), 'dark')

flipSystem(true)
assert.equal(document.documentElement.dataset.theme, 'light', 'follows the OS under `system`')
assert.equal(theme.getEffectiveTheme(), 'light')

theme.applyBrowserPreference('dark')
assert.equal(document.documentElement.dataset.theme, 'dark', 'an explicit preference wins over the OS')
assert.equal(localStorage.getItem('freyja.appearance.theme'), 'dark', 'the browser keeps the preference locally')
flipSystem(false)
flipSystem(true)
assert.equal(document.documentElement.dataset.theme, 'dark', 'OS changes do not move a pinned theme')

theme.applyBrowserPreference('system')
assert.equal(document.documentElement.dataset.theme, 'light', 'back to `system` picks the OS up again')

// ── the store subscription painters use ────────────────────────────
const seen = []
const unsub = theme.useThemeStore.subscribe((s) => seen.push(s.effective))
theme.applyEffectiveTheme('dark')
theme.applyEffectiveTheme('dark')
theme.applyEffectiveTheme('light')
unsub()
assert.deepEqual(seen, ['dark', 'light'], 'subscribers hear each change once')

// ── tokens read back off the root for raster painters ──────────────
document.documentElement.style.setProperty('--accent-rgb', '43 110 194')
assert.deepEqual(theme.cssTokenRgb('--accent-rgb', [0, 0, 0]), [43, 110, 194])
assert.deepEqual(theme.cssTokenRgb('--missing', [1, 2, 3]), [1, 2, 3])

console.log('ok test-theme')
