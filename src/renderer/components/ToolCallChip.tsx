import { useEffect, useState } from 'react'
import { useHarness } from '../state/store'
import { formatDuration } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { useFrameObjectUrl } from '../lib/frameMedia'
import { FileChangeBadge, FileChangeCard } from './FileChangeCard'
import type { ToolCallRecord } from '@shared/events'

export function ToolCallChip({ id, record }: { id: string; record?: ToolCallRecord }) {
  const storeCall = useHarness((s) => s.toolCalls[id])
  const call = record ?? storeCall
  const focusSerial = useHarness((s) =>
    s.focusedToolCallId === id ? s.focusedToolCallSerial : 0,
  )
  // `userOpen` is null until the operator clicks the chevron; the
  // effective open state falls back to "expanded while the call is
  // still streaming" so the operator can watch the JSON build instead
  // of staring at a spinner. Once they click, their choice wins — we
  // never collapse out from under them.
  const [userOpen, setUserOpen] = useState<boolean | null>(null)
  const [imgExpanded, setImgExpanded] = useState(false)
  const frameUrl = useFrameObjectUrl(call?.frame)

  useEffect(() => {
    if (focusSerial > 0) setUserOpen(true)
  }, [focusSerial])

  if (!call) return null

  const argsText =
    call.arguments !== undefined
      ? JSON.stringify(call.arguments, null, 2)
      : call.partialJson ?? ''

  // Header summary — prefer the parsed-args summary once available,
  // else regex-scrape the most useful key out of the partial JSON
  // mid-stream so the chip header isn't a blank "tool_name spinner"
  // while the agent is still emitting its arguments.
  const summary =
    summarizeArgs(call.name, call.arguments) ||
    summarizePartialJson(call.name, call.partialJson)

  const isStreaming = call.status === 'running'
  const hasAnyArgs = call.arguments !== undefined || !!call.partialJson
  const open = userOpen ?? (isStreaming && hasAnyArgs)
  // Heartbeats are liveness pings — they carry no narrative value and
  // shouldn't compete with real actions for visual weight. Render them
  // as a one-line muted note so a string of them reads as background
  // pulse rather than twelve interchangeable rows. No expand affordance
  // because the args payload (`task_id` + `comment: Heartbeat`) is
  // boring by construction.
  const isHeartbeat =
    call.name === 'kanban' &&
    typeof call.arguments === 'object' &&
    call.arguments !== null &&
    (call.arguments as Record<string, unknown>).action === 'heartbeat'

  if (isHeartbeat) {
    return (
      <div
        data-tool-call-id={id}
        className="flex items-center gap-2 px-3 py-[3px] text-fg-3/70"
      >
        <span className="flex w-[18px] justify-center">
          <span className="block h-1 w-1 rounded-full bg-fg-3/55" />
        </span>
        <span className="font-mono text-[10.5px] italic">{summary || 'heartbeat'}</span>
      </div>
    )
  }

  const statusIndicator =
    call.status === 'running' ? (
      <Spinner name="braille" className="text-accent" />
    ) : call.status === 'error' ? (
      <span className="block h-1.5 w-1.5 rounded-full bg-danger" />
    ) : (
      <span className="block h-1.5 w-1.5 rounded-full bg-ok" />
    )

  const hasFrame = Boolean(call.frame)
  const resultImages = call.resultImages ?? []
  const hasResultImages = resultImages.length > 0

  return (
    <div
      data-tool-call-id={id}
      className={`rounded-lg glass-raised overflow-hidden transition-shadow ${
        focusSerial > 0 ? 'ring-1 ring-accent/50 shadow-glow-accent' : ''
      }`}
    >
      <button
        onClick={() => setUserOpen((prev) => !(prev ?? open))}
        className="flex w-full items-center gap-2.5 px-3 py-2 text-left hover:bg-white/[0.025]"
      >
        <span className="flex w-[18px] justify-center">{statusIndicator}</span>
        <span className="font-mono text-[11.5px] text-accent">{call.name}</span>
        {summary && (
          <span className="truncate font-mono text-[11px] text-fg-1">{summary}</span>
        )}
        <span className="ml-auto flex items-center gap-2 text-[10px] text-fg-2">
          {hasFrame && (
            <span className="font-mono text-[9.5px] uppercase tracking-[0.08em] text-warn">
              ◱ frame
            </span>
          )}
          <FileChangeBadge changeSet={call.fileChangeSet} />
          {call.durationMs !== undefined && call.status !== 'running' && (
            <span className="font-mono">{formatDuration(call.durationMs)}</span>
          )}
          <svg
            width="10"
            height="10"
            viewBox="0 0 10 10"
            style={{ transform: open ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 120ms' }}
          >
            <path d="M2 4 L5 7 L8 4" stroke="currentColor" strokeWidth="1" fill="none" />
          </svg>
        </span>
      </button>
      {/* Inline screenshot thumbnail, always visible even when the
          chip is collapsed. Clicking expands it to ~560px. */}
      {call.frame && frameUrl && (
        <div
          className="hairline-t cursor-zoom-in bg-black"
          onClick={(e) => {
            e.stopPropagation()
            setImgExpanded((v) => !v)
          }}
          title="Click to toggle full size"
        >
          <img
            src={frameUrl}
            alt="captured screen"
            className={`block w-full object-contain ${
              imgExpanded ? 'max-h-[540px]' : 'max-h-[200px]'
            }`}
            loading="lazy"
            decoding="async"
            draggable={false}
          />
          <div className="flex items-center justify-between px-3 py-1 font-mono text-[9.5px] text-fg-3">
            <span>
              {call.frame.width}×{call.frame.height}
            </span>
            <span>
              {Math.round(
                (call.frame.byteSize ??
                  Math.floor(((call.frame.pngBase64?.length ?? 0) * 3) / 4)) /
                  1024,
              )}
              KB{' '}
              {imgExpanded ? '· click to shrink' : '· click to expand'}
            </span>
          </div>
        </div>
      )}
      {hasResultImages && (
        <ToolResultImages
          images={resultImages}
          toolCallId={id}
          className="hairline-t bg-black/55 p-3"
        />
      )}
      {open && (
        <div className="hairline-t selectable space-y-2 bg-black/35 p-3 font-mono text-[11px] text-fg-1">
          {argsText && (
            <div>
              <div className="mb-1 flex items-center gap-1.5 text-[9.5px] uppercase tracking-[0.12em] text-fg-2">
                <span>arguments</span>
                {isStreaming && call.arguments === undefined && (
                  <span className="font-mono text-[9.5px] text-accent/80">· streaming</span>
                )}
              </div>
              <pre className="max-h-[180px] overflow-auto whitespace-pre-wrap rounded-md bg-black/45 p-2 text-[11px] text-fg-0">
                {argsText}
              </pre>
            </div>
          )}
          {call.fileChangeSet && <FileChangeCard changeSet={call.fileChangeSet} />}
          {call.result && (
            <div>
              <div className="mb-1 text-[9.5px] uppercase tracking-[0.12em] text-fg-2">
                result{call.isError ? ' (error)' : ''}
              </div>
              <pre
                className={`max-h-[260px] overflow-auto whitespace-pre-wrap rounded-md bg-black/45 p-2 text-[11px] ${
                  call.isError ? 'text-danger' : 'text-fg-0'
                }`}
              >
                {call.result}
              </pre>
            </div>
          )}
          {call.status === 'running' && !call.result && (
            <div className="flex items-center gap-2 text-fg-2">
              <Spinner name="pulse" className="text-accent" />
              <span>executing…</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function ToolResultImages({
  images,
  toolCallId,
  className = 'border-t border-white/[0.04] bg-black/45 p-3',
}: {
  images: NonNullable<ToolCallRecord['resultImages']>
  toolCallId: string
  className?: string
}) {
  if (images.length === 0) return null

  return (
    <div className={className}>
      <div className={`grid gap-2 ${images.length > 1 ? 'sm:grid-cols-2' : ''}`}>
        {images.map((image, idx) => (
          <ToolResultImage
            key={image.frameId ?? `${toolCallId}-image-${idx}`}
            image={image}
            label={image.label || `generated image ${idx + 1}`}
          />
        ))}
      </div>
    </div>
  )
}

function summarizeArgs(name: string, args?: Record<string, unknown>): string {
  if (!args) return ''
  // Kanban gets its own summary path so each chip reads as the action it
  // is rather than a generic "kanban" pill. Otherwise twenty board moves
  // in a row look identical.
  if (name === 'kanban') return summarizeKanban(args)
  if (typeof args.path === 'string') return args.path
  if (typeof args.pattern === 'string') return args.pattern as string
  if (typeof args.query === 'string') return args.query as string
  if (typeof args.prompt === 'string') return (args.prompt as string).slice(0, 120)
  if (typeof args.command === 'string') return (args.command as string).slice(0, 80)
  const keys = Object.keys(args)
  return keys.length > 0 ? keys.join(', ') : ''
}

/** Pull the first useful key out of a partial JSON arg blob mid-stream
 *  so the chip header shows something more than a spinner before the
 *  full JSON parses. Order matches `summarizeArgs` so the live summary
 *  doesn't visually re-shuffle when the full args land. Returns the
 *  empty string when nothing meaningful is available yet. Exported so
 *  ParallelToolGroup can share the same regex extraction. */
export function summarizePartialJson(name: string, partial?: string): string {
  if (!partial) return ''
  const grab = (key: string, max?: number): string | undefined => {
    const re = new RegExp(`"${key}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*?)(?:"|$)`)
    const m = partial.match(re)
    if (!m) return undefined
    const raw = m[1].replace(/\\n/g, ' ').replace(/\\"/g, '"')
    return max != null ? raw.slice(0, max) : raw
  }
  if (name === 'kanban') {
    const action = grab('action')
    const taskId = grab('task_id')
    return action ? (taskId ? `${action} ${taskId}` : action) : ''
  }
  const path = grab('path')
  if (path) return path
  const pattern = grab('pattern')
  if (pattern) return pattern
  const query = grab('query')
  if (query) return query
  const prompt = grab('prompt', 120)
  if (prompt) return prompt
  const command = grab('command', 80)
  if (command) return command
  // Last resort — surface whatever key appears first so an opaque tool
  // call at least shows "title", "id", "to", etc. while the rest
  // streams.
  const firstKey = partial.match(/"([a-zA-Z_][\w]*)"\s*:/)
  return firstKey ? firstKey[1] : ''
}

function summarizeKanban(args: Record<string, unknown>): string {
  const action = typeof args.action === 'string' ? args.action : ''
  const taskId = typeof args.task_id === 'string' ? args.task_id : ''
  const status = typeof args.status === 'string' ? args.status : ''
  const title = typeof args.title === 'string' ? args.title.slice(0, 48) : ''
  const parentId = typeof args.parent_id === 'string' ? args.parent_id : ''
  const childId = typeof args.child_id === 'string' ? args.child_id : ''
  switch (action) {
    case 'create': {
      const verifies = args.requires_verification === true ? ' · verifies' : ''
      return title ? `create "${title}"${verifies}` : `create${verifies}`
    }
    case 'update':
      return taskId && status ? `update ${taskId} → ${status}` : `update ${taskId || ''}`.trim()
    case 'complete':
      return `complete ${taskId}`.trim()
    case 'claim':
      return `claim ${taskId}`.trim()
    case 'block':
      return `block ${taskId}`.trim()
    case 'heartbeat':
      return `heartbeat ${taskId}`.trim()
    case 'comment':
      return `comment on ${taskId}`.trim()
    case 'show':
      return `show ${taskId}`.trim()
    case 'show_history':
      return `history ${taskId}`.trim()
    case 'link':
      return parentId && childId ? `link ${parentId} → ${childId}` : 'link'
    case 'unblock':
      return `unblock ${taskId}`.trim()
    case 'digest':
      return 'digest'
    case 'list':
      return 'list'
    default:
      return action
  }
}

function ToolResultImage({
  image,
  label,
}: {
  image: NonNullable<ToolCallRecord['resultImages']>[number]
  label: string
}) {
  const url = useFrameObjectUrl(image)
  if (!url) {
    return (
      <figure className="rounded-lg bg-black/70 p-4 ring-1 ring-danger/25">
        <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-danger">
          image unavailable
        </div>
        <div className="mt-1 text-[11px] leading-[1.5] text-fg-2">
          The tool returned image metadata, but this renderer no longer has the image bytes.
        </div>
      </figure>
    )
  }

  const dimensions =
    image.width > 0 && image.height > 0 ? `${image.width}×${image.height}` : image.mimeType
  const size =
    image.byteSize && image.byteSize > 0
      ? `${Math.round(image.byteSize / 1024)}KB`
      : ''

  return (
    <figure className="overflow-hidden rounded-lg bg-black ring-1 ring-white/10">
      <img
        src={url}
        alt={label}
        className="block max-h-[520px] w-full object-contain"
        loading="lazy"
        decoding="async"
        draggable={false}
      />
      <figcaption className="flex items-center justify-between gap-3 px-3 py-1.5 font-mono text-[9.5px] uppercase tracking-[0.08em] text-fg-2">
        <span>{label}</span>
        <span className="text-fg-3">
          {dimensions}{size ? ` · ${size}` : ''}
        </span>
      </figcaption>
    </figure>
  )
}
