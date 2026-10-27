import { contextBridge, ipcRenderer } from 'electron'
import { IPC } from '../shared/events.js'
import type { IslandAction, IslandControl, IslandDigest, IslandGeometry } from '../shared/island.js'

// The island's bridge is tiny on purpose: it sees a digest, not the event
// stream, and it never calls `getMode` (that call is the main window's
// "renderer ready" switch and would flush the early events to the wrong
// window).
function on<T>(channel: string, listener: (payload: T) => void): () => void {
  const handler = (_e: unknown, payload: T) => {
    try {
      listener(payload)
    } catch (err) {
      console.error('[island-preload] listener error', err)
    }
  }
  ipcRenderer.on(channel, handler)
  return () => {
    ipcRenderer.removeListener(channel, handler)
  }
}

const api = {
  onState(listener: (digest: IslandDigest) => void): () => void {
    return on<IslandDigest>(IPC.islandState, listener)
  },
  onGeometry(listener: (geometry: IslandGeometry) => void): () => void {
    return on<IslandGeometry>(IPC.islandGeometry, listener)
  },
  onToggle(listener: () => void): () => void {
    return on<void>(IPC.islandToggle, () => listener())
  },
  async action(action: IslandAction): Promise<{ ok: boolean }> {
    return ipcRenderer.invoke(IPC.islandAction, action)
  },
  async control(control: IslandControl): Promise<{ ok: boolean }> {
    return ipcRenderer.invoke(IPC.islandControl, control)
  },
} as const

export type IslandApi = typeof api

contextBridge.exposeInMainWorld('island', api)
