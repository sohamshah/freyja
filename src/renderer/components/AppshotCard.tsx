import { useState } from 'react'
import type { AppshotSummary } from '@shared/events'
import type { PendingAttachment } from '../lib/appshotAttachment'

/** One line on what a capture holds: `text 3,241 chars`, or why it holds
 *  less. Takes the summary a sent message keeps, so the composer card and
 *  the sent chip say the same thing. */
export function appshotStatusLine(
  a: Pick<AppshotSummary, 'textStatus' | 'chars' | 'truncated' | 'imageStatus'>,
  opts: { textReason?: string; imageReason?: string } = {},
): string {
  const because = (reason?: string) => (reason ? `: ${reason}` : '')
  const parts: string[] = []
  if (a.textStatus === 'unavailable') {
    parts.push(
      a.imageStatus === 'ok'
        ? `image only${because(opts.textReason)}`
        : `nothing readable${because(opts.textReason)}`,
    )
  } else {
    const n = `${a.chars.toLocaleString()} chars`
    parts.push(
      a.textStatus === 'partial' ? `text partial${because(opts.textReason)}, ${n}` : `text ${n}`,
    )
    if (a.truncated) parts.push('cut to the visible part')
  }
  if (a.imageStatus === 'unavailable' && a.textStatus !== 'unavailable') {
    parts.push(`no image${because(opts.imageReason)}`)
  }
  return parts.join(' · ')
}

/** The composer card for a queued appshot. The thumbnail, the app and the
 *  window title say what was captured, and "View text" shows the exact text
 *  the model will get, so the operator can check it before sending. */
export function AppshotCard({
  attachment,
  onRemove,
}: {
  attachment: PendingAttachment
  onRemove: () => void
}) {
  const [open, setOpen] = useState(false)
  const shot = attachment.appshot
  if (!shot) return null
  const hasText = shot.textStatus !== 'unavailable' && shot.text.length > 0
  const imageOnly = shot.textStatus === 'unavailable' && shot.imageStatus === 'ok'
  const textOnly = shot.imageStatus === 'unavailable' && shot.textStatus !== 'unavailable'
  const status = appshotStatusLine(shot, {
    textReason: shot.textReason,
    imageReason: shot.imageReason,
  })
  const warn = imageOnly || textOnly || shot.textStatus === 'partial'
  return (
    <div className="group relative flex w-full max-w-[460px] flex-col gap-1.5 rounded-lg bg-white/[0.04] px-2 py-1.5 ring-hairline">
      <div className="flex items-center gap-2">
        {attachment.previewUrl ? (
          <img
            src={attachment.previewUrl}
            alt="appshot"
            className="h-10 w-14 shrink-0 rounded object-cover object-top ring-hairline"
          />
        ) : (
          <div
            className="flex h-10 w-14 shrink-0 items-center justify-center rounded bg-accent/10 font-mono text-[9px] text-accent ring-hairline"
            aria-label="appshot without an image"
          >
            text
          </div>
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-[10.5px] text-fg-0">
            {shot.app || 'window'}
            <span className="ml-1.5 text-[9px] uppercase tracking-wide text-accent">appshot</span>
          </span>
          <span className="truncate text-[9.5px] text-fg-1" title={shot.windowTitle}>
            {shot.windowTitle || 'untitled window'}
          </span>
          <span
            className={`truncate text-[9.5px] ${warn ? 'text-warn' : 'text-fg-2'}`}
            title={status}
          >
            {status}
          </span>
        </div>
        {hasText && (
          <button
            onClick={() => setOpen((v) => !v)}
            className="shrink-0 rounded bg-white/[0.06] px-1.5 py-[2px] text-[10px] text-fg-1 ring-hairline hover:bg-white/[0.1]"
            title="Show the exact text the model will receive"
            aria-expanded={open}
          >
            {open ? 'Hide text' : 'View text'}
          </button>
        )}
        <button
          onClick={onRemove}
          className="ml-1 shrink-0 rounded bg-white/[0.06] px-1.5 py-[2px] text-[10px] text-fg-1 ring-hairline hover:bg-danger/20 hover:text-danger"
          title="Remove"
        >
          ✕
        </button>
      </div>
      {open && hasText && (
        <pre className="selectable max-h-[220px] overflow-auto whitespace-pre-wrap break-words rounded bg-black/30 p-2 font-mono text-[10px] leading-[1.45] text-fg-1">
          {shot.text}
        </pre>
      )}
    </div>
  )
}

/** The line under a sent message's thumbnail: what was captured. The text
 *  itself is in the transcript, not in the message the renderer keeps. */
export function AppshotChip({ appshot }: { appshot: AppshotSummary }) {
  const status = appshotStatusLine(appshot)
  const label = `${appshot.app || 'window'}${appshot.windowTitle ? ` — ${appshot.windowTitle}` : ''}`
  return (
    <div
      className="max-w-full truncate font-mono text-[10px] text-fg-2"
      title={`${label} · ${status}`}
    >
      ◰ {label} · {status}
    </div>
  )
}
