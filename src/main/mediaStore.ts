import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

/**
 * Read side of the bridge's content-addressed image store
 * (engine/media_store.py). Transcripts, raw message logs and the event
 * mirror persist an image as `{type: 'image', media_type, sha256}` instead
 * of inline base64; the bytes live at
 * `$FREYJA_HOME/media/images/<sha[:2]>/<sha>.<ext>`.
 */

const EXT_FOR_MEDIA_TYPE: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
}

function mediaRoot(): string {
  return path.join(process.env.FREYJA_HOME || path.join(os.homedir(), '.freyja'), 'media')
}

/** Base64 for a stored image, or '' if its blob is missing. */
export function readStoredImageBase64(sha256: string, mediaType: string): string {
  const ext = EXT_FOR_MEDIA_TYPE[mediaType] ?? 'bin'
  try {
    return fs
      .readFileSync(path.join(mediaRoot(), 'images', sha256.slice(0, 2), `${sha256}.${ext}`))
      .toString('base64')
  } catch {
    return ''
  }
}

/** Base64 of a serialized image block, resolving a stored reference. */
export function imageBlockBase64(block: { data?: unknown; sha256?: unknown; media_type?: unknown }): string {
  if (typeof block.data === 'string' && block.data) return block.data
  if (typeof block.sha256 === 'string' && block.sha256) {
    return readStoredImageBase64(block.sha256, String(block.media_type || 'image/png'))
  }
  return ''
}

/** Put stored images back inline, in place, so an exported file stands on
 *  its own. Blocks whose blob is missing keep their reference. */
export function inlineStoredImages(value: unknown): void {
  if (Array.isArray(value)) {
    for (const item of value) inlineStoredImages(item)
    return
  }
  if (!value || typeof value !== 'object') return
  const obj = value as Record<string, unknown>
  if (obj.type === 'image' && typeof obj.sha256 === 'string' && !obj.data) {
    const data = imageBlockBase64(obj)
    if (data) {
      obj.data = data
      delete obj.sha256
    }
    return
  }
  for (const child of Object.values(obj)) inlineStoredImages(child)
}
