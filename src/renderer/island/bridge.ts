import type { JevPaletteDecision } from '../../shared/events'
import type { IslandAction, IslandControl, IslandDigest, IslandGeometry, IslandPaletteIndex } from '../../shared/island'

export interface IslandApi {
  onState(listener: (digest: IslandDigest) => void): () => void
  onGeometry(listener: (geometry: IslandGeometry) => void): () => void
  onToggle(listener: () => void): () => void
  onPalette(listener: (index: IslandPaletteIndex) => void): () => void
  action(action: IslandAction): Promise<{ ok: boolean }>
  control(control: IslandControl): Promise<{ ok: boolean }>
  jevClassify(query: string): Promise<JevPaletteDecision>
}

declare global {
  interface Window {
    island?: IslandApi
    /** Browser/probe hooks: push a digest, a geometry, a palette index or a toggle into the page. */
    __islandPush?: (digest: IslandDigest) => void
    __islandGeometry?: (geometry: IslandGeometry) => void
    __islandPalette?: (index: IslandPaletteIndex) => void
    __islandToggle?: () => void
    /** A stand-in for Jev in the probe: answers the decision for a query. */
    __islandJev?: (query: string) => JevPaletteDecision | Promise<JevPaletteDecision>
    /** Actions the page sent while running without Electron. */
    __islandActions?: IslandAction[]
    __islandControls?: IslandControl[]
  }
}

/**
 * The preload's bridge in Electron; in a plain browser (screenshots, the
 * probe) a stand-in that takes digests from `window.__islandPush` and
 * records actions on `window.__islandActions`.
 */
export function islandApi(): IslandApi {
  if (window.island) return window.island
  const stateListeners = new Set<(d: IslandDigest) => void>()
  const geometryListeners = new Set<(g: IslandGeometry) => void>()
  const paletteListeners = new Set<(p: IslandPaletteIndex) => void>()
  const toggleListeners = new Set<() => void>()
  window.__islandPush = (d) => stateListeners.forEach((l) => l(d))
  window.__islandGeometry = (g) => geometryListeners.forEach((l) => l(g))
  window.__islandPalette = (p) => paletteListeners.forEach((l) => l(p))
  window.__islandToggle = () => toggleListeners.forEach((l) => l())
  window.__islandActions = []
  window.__islandControls = []
  return {
    onState(l) {
      stateListeners.add(l)
      return () => stateListeners.delete(l)
    },
    onGeometry(l) {
      geometryListeners.add(l)
      return () => geometryListeners.delete(l)
    },
    onPalette(l) {
      paletteListeners.add(l)
      return () => paletteListeners.delete(l)
    },
    onToggle(l) {
      toggleListeners.add(l)
      return () => toggleListeners.delete(l)
    },
    async action(a) {
      window.__islandActions!.push(a)
      return { ok: true }
    },
    async control(c) {
      window.__islandControls!.push(c)
      return { ok: true }
    },
    async jevClassify(query) {
      if (window.__islandJev) return window.__islandJev(query)
      return { ok: false, query, error: 'no jev in the browser', domain: 'palette_command', subTarget: 'none', confidence: 0, readiness: 0, latencyMs: 0 }
    },
  }
}
