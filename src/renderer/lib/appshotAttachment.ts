import type { AppshotMeta } from '@shared/events'

/** What the composer tray holds for one queued attachment. Mirrors the store's
 *  `pendingAttachments` item. */
export interface PendingAttachment {
  id: string
  type: 'image' | 'video' | 'appshot'
  mimeType: string
  dataBase64: string
  previewUrl: string
  filename?: string
  sizeBytes?: number
  appshot?: AppshotMeta
}

/** The payload of an `appshot_captured` event that has a capture in it. */
export interface CapturedAppshot {
  appshot: AppshotMeta
  image?: { mimeType: string; dataBase64: string; width: number; height: number } | null
}

/** Cards a composer keeps at once. A fifth capture drops the oldest, so a
 *  burst of hotkey presses cannot grow one message without bound. */
export const MAX_PENDING_APPSHOTS = 4

function base64Bytes(b64: string): number {
  const pad = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0
  return Math.max(0, Math.floor((b64.length * 3) / 4) - pad)
}

/** `Safari Appshot 2026-10-14T10-22-31.png`. Colons are not safe in file
 *  names, and the fraction of a second only makes the name long. */
export function appshotFilename(meta: AppshotMeta, mimeType: string): string {
  const app = (meta.app || 'App').replace(/[\\/:]/g, '-')
  const stamp = (meta.capturedAt || '').replace(/\.\d+/, '').replace(/Z$/, '').replace(/:/g, '-')
  const ext = mimeType === 'image/jpeg' ? 'jpg' : 'png'
  return `${app} Appshot${stamp ? ` ${stamp}` : ''}.${ext}`
}

/** A tray entry for a capture. With an image it is an ordinary image
 *  attachment that also carries the appshot, so the bridge needs no second
 *  code path for the picture. Without one it is a text-only `appshot`. */
export function appshotToAttachment(id: string, cap: CapturedAppshot): PendingAttachment {
  const { appshot, image } = cap
  if (image && image.dataBase64) {
    return {
      id,
      type: 'image',
      mimeType: image.mimeType,
      dataBase64: image.dataBase64,
      previewUrl: `data:${image.mimeType};base64,${image.dataBase64}`,
      filename: appshotFilename(appshot, image.mimeType),
      sizeBytes: base64Bytes(image.dataBase64),
      appshot,
    }
  }
  return {
    id,
    type: 'appshot',
    mimeType: '',
    dataBase64: '',
    previewUrl: '',
    filename: appshotFilename(appshot, 'image/png').replace(/\.png$/, '.txt'),
    sizeBytes: 0,
    appshot,
  }
}

/** Add a capture to the tray. A second capture of the same window replaces
 *  the first, since the newer one is what the operator sees now. The tray
 *  keeps at most `MAX_PENDING_APPSHOTS` appshots, dropping the oldest, and
 *  leaves every other attachment where it was. */
export function addAppshotAttachment(
  list: PendingAttachment[],
  next: PendingAttachment,
): PendingAttachment[] {
  const sameWindow = next.appshot?.windowId
  let out = list.filter(
    (a) => !(a.appshot && sameWindow !== undefined && a.appshot.windowId === sameWindow),
  )
  out = [...out, next]
  const shots = out.filter((a) => a.appshot)
  if (shots.length > MAX_PENDING_APPSHOTS) {
    const drop = new Set(shots.slice(0, shots.length - MAX_PENDING_APPSHOTS).map((a) => a.id))
    out = out.filter((a) => !drop.has(a.id))
  }
  return out
}

/** One line for the toast after a capture. */
export function appshotToast(cap: CapturedAppshot): string {
  const { appshot, image } = cap
  const title = appshot.windowTitle ? ` — ${appshot.windowTitle}` : ''
  const shown = title.length > 60 ? `${title.slice(0, 59)}…` : title
  const textMissing = appshot.textStatus === 'unavailable'
  const imageMissing = !image || !image.dataBase64
  const part =
    imageMissing && textMissing
      ? ' (nothing readable)'
      : imageMissing
        ? ' (text only)'
        : textMissing
          ? ' (image only)'
          : ''
  return `Appshot: ${appshot.app || 'window'}${shown}${part}`
}
