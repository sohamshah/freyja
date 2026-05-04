import { useEffect, useState } from 'react'
import { useHarness } from '../state/store'
import { formatDuration } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { useFrameObjectUrl } from '../lib/frameMedia'
import { FileChangeBadge, FileChangeCard } from './FileChangeCard'

export function ToolCallChip({ id }: { id: string }) {
  const call = useHarness((s) => s.toolCalls[id])
  const focusSerial = useHarness((s) =>
    s.focusedToolCallId === id ? s.focusedToolCallSerial : 0,
  )
  const [open, setOpen] = useState(false)
  const [imgExpanded, setImgExpanded] = useState(false)
  const frameUrl = useFrameObjectUrl(call?.frame)

  useEffect(() => {
    if (focusSerial > 0) setOpen(true)
  }, [focusSerial])

  if (!call) return null

  const argsText =
    call.arguments !== undefined
      ? JSON.stringify(call.arguments, null, 2)
      : call.partialJson ?? ''

  const summary = summarizeArgs(call.name, call.arguments)

  const statusIndicator =
    call.status === 'running' ? (
      <Spinner name="braille" className="text-accent" />
    ) : call.status === 'error' ? (
      <span className="block h-1.5 w-1.5 rounded-full bg-danger" />
    ) : (
      <span className="block h-1.5 w-1.5 rounded-full bg-ok" />
    )

  const hasFrame = Boolean(call.frame)

  return (
    <div
      data-tool-call-id={id}
      className={`rounded-lg glass-raised overflow-hidden transition-shadow ${
        focusSerial > 0 ? 'ring-1 ring-accent/50 shadow-glow-accent' : ''
      }`}
    >
      <button
        onClick={() => setOpen((o) => !o)}
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
      {open && (
        <div className="hairline-t selectable space-y-2 bg-black/35 p-3 font-mono text-[11px] text-fg-1">
          {argsText && (
            <div>
              <div className="mb-1 text-[9.5px] uppercase tracking-[0.12em] text-fg-2">
                arguments
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

function summarizeArgs(name: string, args?: Record<string, unknown>): string {
  if (!args) return ''
  if (typeof args.path === 'string') return args.path
  if (typeof args.pattern === 'string') return args.pattern as string
  if (typeof args.query === 'string') return args.query as string
  if (typeof args.command === 'string') return (args.command as string).slice(0, 80)
  const keys = Object.keys(args)
  return keys.length > 0 ? keys.join(', ') : ''
}
