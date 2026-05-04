import { useMemo } from 'react'

export interface FrameRef {
  frameId?: string
  mimeType: string
  width: number
  height: number
  takenAt: number
  reason?: string
  byteSize?: number
  /** Legacy/persisted payload. Hot renderer state should not rely on it. */
  pngBase64?: string
}

interface FrameEntry {
  frameId: string
  pngBase64: string
  mimeType: string
  width: number
  height: number
  takenAt: number
  reason?: string
  byteSize: number
  objectUrl?: string
  refs: number
}

let frameCounter = 0
const frames = new Map<string, FrameEntry>()

function approxBase64Bytes(dataBase64: string): number {
  const padding = dataBase64.endsWith('==') ? 2 : dataBase64.endsWith('=') ? 1 : 0
  return Math.max(0, Math.floor((dataBase64.length * 3) / 4) - padding)
}

function nextFrameId(prefix = 'frame', takenAt = Date.now()): string {
  frameCounter += 1
  return `${prefix}_${takenAt.toString(36)}_${frameCounter.toString(36)}`
}

function base64ToBlob(dataBase64: string, mimeType: string): Blob {
  const binary = atob(dataBase64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i)
  }
  return new Blob([bytes], { type: mimeType })
}

export function registerFrame(
  frame: FrameRef & { pngBase64: string },
  prefix?: string,
): FrameRef {
  const mimeType = frame.mimeType || 'image/png'
  const frameId = frame.frameId || nextFrameId(prefix, frame.takenAt)
  const byteSize = frame.byteSize ?? approxBase64Bytes(frame.pngBase64)
  const existing = frames.get(frameId)
  if (existing?.objectUrl) URL.revokeObjectURL(existing.objectUrl)
  frames.set(frameId, {
    frameId,
    pngBase64: frame.pngBase64,
    mimeType,
    width: frame.width,
    height: frame.height,
    takenAt: frame.takenAt,
    reason: frame.reason,
    byteSize,
    refs: existing?.refs ?? 0,
  })
  return {
    frameId,
    mimeType,
    width: frame.width,
    height: frame.height,
    takenAt: frame.takenAt,
    reason: frame.reason,
    byteSize,
  }
}

export function normalizeFrame(frame?: FrameRef): FrameRef | undefined {
  if (!frame) return undefined
  if (frame.pngBase64) return registerFrame(frame as FrameRef & { pngBase64: string })
  return frame
}

export function retainFrame(frame?: FrameRef | null): void {
  if (!frame?.frameId) return
  const entry = frames.get(frame.frameId)
  if (entry) entry.refs += 1
}

export function releaseFrame(frame?: FrameRef | null): void {
  if (!frame?.frameId) return
  const entry = frames.get(frame.frameId)
  if (!entry) return
  entry.refs -= 1
  if (entry.refs > 0) return
  if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl)
  frames.delete(frame.frameId)
}

export function getFrameObjectUrl(frame?: FrameRef | null): string | undefined {
  if (!frame) return undefined
  if (frame.frameId) {
    const entry = frames.get(frame.frameId)
    if (entry) {
      if (!entry.objectUrl) {
        entry.objectUrl = URL.createObjectURL(
          base64ToBlob(entry.pngBase64, entry.mimeType),
        )
      }
      return entry.objectUrl
    }
  }
  if (frame.pngBase64) {
    return `data:${frame.mimeType};base64,${frame.pngBase64}`
  }
  return undefined
}

export function useFrameObjectUrl(frame?: FrameRef | null): string | undefined {
  return useMemo(
    () => getFrameObjectUrl(frame),
    [frame?.frameId, frame?.pngBase64, frame?.takenAt],
  )
}

export function getPersistableFrame(frame?: FrameRef | null): FrameRef | undefined {
  if (!frame) return undefined
  const entry = frame.frameId ? frames.get(frame.frameId) : undefined
  if (entry) {
    return {
      pngBase64: entry.pngBase64,
      mimeType: entry.mimeType,
      width: entry.width,
      height: entry.height,
      takenAt: entry.takenAt,
      reason: entry.reason,
      byteSize: entry.byteSize,
    }
  }
  return frame.pngBase64 ? frame : undefined
}

export function retainedFrameCount(): number {
  return frames.size
}
