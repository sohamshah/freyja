import { useEffect, useMemo, useState } from 'react'
import { useHarness } from '../state/store'
import { formatDuration } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { useFrameObjectUrl } from '../lib/frameMedia'
import { FileChangeBadge, FileChangeCard } from './FileChangeCard'
import { ToolResultImages } from './ToolCallChip'
import type { ToolCallRecord } from '@shared/events'

/**
 * Renders a group of parallel tool calls as side-by-side lanes with
 * a split/merge junction indicator. Single tool calls still get this
 * treatment but without the junction lines.
 *
 * Visual:
 *   ┬─ web_search "query 1"    ── 2.3s ✓
 *   ├─ web_search "query 2"    ── 1.9s ✓
 *   └─ web_fetch arxiv.org/... ── 4.1s ✓
 */
export function ParallelToolGroup({ ids }: { ids: string[] }) {
  const toolCalls = useHarness((s) => s.toolCalls)
  const records = useMemo(
    () => ids.map((id) => toolCalls[id]).filter(Boolean) as ToolCallRecord[],
    [ids, toolCalls],
  )

  if (records.length === 0) return null

  const isParallel = records.length > 1
  const allDone = records.every((r) => r.status !== 'running')
  const anyError = records.some((r) => r.status === 'error')
  const maxDuration = Math.max(1, ...records.map((r) => r.durationMs ?? 0))
  const totalDuration = Math.max(...records.map((r) => r.durationMs ?? 0))

  return (
    <div className={`my-1 ${isParallel ? 'rounded-xl glass-raised overflow-hidden' : ''}`}>
      {/* Parallel header — only for multi-call groups */}
      {isParallel && (
        <div className="flex items-center gap-2 px-3 py-1.5 border-b border-white/[0.04]">
          <span className="font-mono text-[9px] uppercase tracking-[0.1em] text-fg-3">
            parallel
          </span>
          <span className="font-mono text-[9px] text-fg-3">
            {records.length} calls
          </span>
          {allDone && (
            <>
              <span className="text-fg-3">·</span>
              <span className="font-mono text-[9px] text-fg-3">
                {formatDuration(totalDuration)} wall
              </span>
            </>
          )}
          <span className="ml-auto">
            {!allDone ? (
              <Spinner name="braille" className="text-accent" />
            ) : anyError ? (
              <span className="block h-1.5 w-1.5 rounded-full bg-danger" />
            ) : (
              <span className="block h-1.5 w-1.5 rounded-full bg-ok" />
            )}
          </span>
        </div>
      )}

      {/* Tool call lanes */}
      <div className={isParallel ? '' : ''}>
        {records.map((record, i) => (
          <ToolLane
            key={record.id}
            record={record}
            isParallel={isParallel}
            isLast={i === records.length - 1}
            maxDuration={maxDuration}
          />
        ))}
      </div>
    </div>
  )
}

function ToolLane({
  record,
  isParallel,
  isLast,
  maxDuration,
}: {
  record: ToolCallRecord
  isParallel: boolean
  isLast: boolean
  maxDuration: number
}) {
  const [open, setOpen] = useState(false)
  const [imgExpanded, setImgExpanded] = useState(false)
  const focusSerial = useHarness((s) =>
    s.focusedToolCallId === record.id ? s.focusedToolCallSerial : 0,
  )
  const frameUrl = useFrameObjectUrl(record.frame)
  const resultImages = record.resultImages ?? []
  const hasResultImages = resultImages.length > 0

  const isRunning = record.status === 'running'
  const isError = record.status === 'error'
  const durationPct = record.durationMs ? Math.min(100, (record.durationMs / maxDuration) * 100) : 0

  const summary = summarizeArgs(record.name, record.arguments)
  const toolCategory = getToolCategory(record.name)

  const statusDot = isRunning ? (
    <Spinner name="braille" className="text-accent" />
  ) : isError ? (
    <span className="block h-1.5 w-1.5 rounded-full bg-danger" />
  ) : (
    <span className="block h-1.5 w-1.5 rounded-full bg-ok" />
  )

  // Junction line character for parallel groups
  const junction = isParallel
    ? isLast ? '└' : '├'
    : null

  const wrapper = isParallel
    ? `border-b border-white/[0.03] last:border-b-0`
    : 'rounded-lg glass-raised overflow-hidden'

  useEffect(() => {
    if (focusSerial > 0) setOpen(true)
  }, [focusSerial])

  return (
    <div
      data-tool-call-id={record.id}
      className={`${wrapper} transition-shadow ${
        focusSerial > 0 ? 'ring-1 ring-accent/50 shadow-glow-accent' : ''
      }`}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-white/[0.025] transition-colors"
      >
        {/* Junction indicator for parallel */}
        {junction && (
          <span className="shrink-0 font-mono text-[10px] text-fg-3 w-[12px]">
            {junction}
          </span>
        )}

        {/* Category color bar */}
        <span
          className="shrink-0 h-[14px] w-[3px] rounded-full"
          style={{ backgroundColor: toolCategory.color }}
        />

        {/* Status */}
        <span className="flex w-[16px] justify-center shrink-0">{statusDot}</span>

        {/* Tool name */}
        <span className="shrink-0 font-mono text-[11px] text-accent">
          {record.name}
        </span>

        {/* Args summary */}
        {summary && (
          <span className="min-w-0 truncate font-mono text-[10.5px] text-fg-2">
            {summary}
          </span>
        )}

        {/* Duration + chevron */}
        <span className="ml-auto flex shrink-0 items-center gap-2 text-[10px] text-fg-3">
          {record.frame && (
            <span className="font-mono text-[9px] uppercase tracking-[0.08em] text-warn">
              ◱
            </span>
          )}
          {hasResultImages && (
            <span className="font-mono text-[9px] uppercase tracking-[0.08em] text-accent">
              image
            </span>
          )}
          <FileChangeBadge changeSet={record.fileChangeSet} />
          {record.durationMs != null && !isRunning && (
            <span className="font-mono">{formatDuration(record.durationMs)}</span>
          )}
          <svg
            width="9" height="9" viewBox="0 0 10 10"
            className="transition-transform"
            style={{ transform: open ? 'rotate(180deg)' : 'rotate(0deg)' }}
          >
            <path d="M2 4 L5 7 L8 4" stroke="currentColor" strokeWidth="1" fill="none" />
          </svg>
        </span>
      </button>

      {/* Duration bar — only in parallel mode for visual comparison */}
      {isParallel && record.durationMs != null && !isRunning && (
        <div className="px-3 pb-1.5">
          <div className="h-[2px] overflow-hidden rounded-full bg-white/[0.04]">
            <div
              className="h-full rounded-full transition-all duration-300"
              style={{
                width: `${durationPct}%`,
                backgroundColor: isError ? 'var(--danger, #e05050)' : toolCategory.color,
                opacity: 0.7,
              }}
            />
          </div>
        </div>
      )}

      {/* Running progress bar */}
      {isRunning && (
        <div className="px-3 pb-1.5">
          <div className="h-[2px] overflow-hidden rounded-full bg-white/[0.04]">
            <div
              className="h-full rounded-full bg-accent/60 animate-pulse"
              style={{ width: '60%' }}
            />
          </div>
        </div>
      )}

      {/* Screenshot frame */}
      {record.frame && frameUrl && (
        <div
          className="border-t border-white/[0.04] cursor-zoom-in bg-black"
          onClick={(e) => { e.stopPropagation(); setImgExpanded((v) => !v) }}
        >
          <img
            src={frameUrl}
            alt="captured"
            className={`block w-full object-contain ${imgExpanded ? 'max-h-[540px]' : 'max-h-[180px]'}`}
            loading="lazy"
            decoding="async"
            draggable={false}
          />
        </div>
      )}

      {hasResultImages && (
        <ToolResultImages
          images={resultImages}
          toolCallId={record.id}
        />
      )}

      {/* Expanded details */}
      {open && (
        <div className="border-t border-white/[0.04] selectable space-y-2 bg-black/25 p-3 font-mono text-[11px] text-fg-1">
          {record.arguments && (
            <div>
              <div className="mb-1 text-[9px] uppercase tracking-[0.1em] text-fg-3">arguments</div>
              <pre className="max-h-[160px] overflow-auto whitespace-pre-wrap rounded-md bg-black/40 p-2 text-[10.5px] text-fg-0">
                {JSON.stringify(record.arguments, null, 2)}
              </pre>
            </div>
          )}
          {record.result && (
            <div>
              <div className="mb-1 text-[9px] uppercase tracking-[0.1em] text-fg-3">
                result{record.isError ? ' (error)' : ''}
              </div>
              <pre className={`max-h-[200px] overflow-auto whitespace-pre-wrap rounded-md bg-black/40 p-2 text-[10.5px] ${
                record.isError ? 'text-danger' : 'text-fg-0'
              }`}>
                {record.result}
              </pre>
            </div>
          )}
          {record.fileChangeSet && <FileChangeCard changeSet={record.fileChangeSet} />}
          {isRunning && !record.result && (
            <div className="flex items-center gap-2 text-fg-3">
              <Spinner name="pulse" className="text-accent" />
              <span>executing…</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Helpers ──────────────────────────────────────────────────────

const TOOL_CATEGORIES: Record<string, { color: string; label: string }> = {
  // Read
  read_file:       { color: '#6ba3d6', label: 'read' },
  read:            { color: '#6ba3d6', label: 'read' },
  glob:            { color: '#6ba3d6', label: 'read' },
  grep:            { color: '#6ba3d6', label: 'read' },
  list_directory:  { color: '#6ba3d6', label: 'read' },
  // Write
  write_file:      { color: '#5bbb5b', label: 'write' },
  write:           { color: '#5bbb5b', label: 'write' },
  edit_file:       { color: '#5bbb5b', label: 'write' },
  edit:            { color: '#5bbb5b', label: 'write' },
  edit_json:       { color: '#5bbb5b', label: 'write' },
  // Web
  web_search:      { color: '#e0a040', label: 'web' },
  web_fetch:       { color: '#e0a040', label: 'web' },
  web_research:    { color: '#e0a040', label: 'web' },
  // Shell
  bash:            { color: '#b080d0', label: 'shell' },
  // Agents
  sub_agent:       { color: '#60c0c0', label: 'agent' },
  subagents:       { color: '#60c0c0', label: 'agent' },
  // Bus
  publish_finding: { color: '#d0a040', label: 'bus' },
  read_findings:   { color: '#d0a040', label: 'bus' },
  // Media
  generate_image:  { color: '#b6f2ff', label: 'media' },
}

function getToolCategory(name: string) {
  return TOOL_CATEGORIES[name] ?? { color: '#888', label: 'other' }
}

function summarizeArgs(name: string, args?: Record<string, unknown>): string {
  if (!args) return ''
  const str = (k: string): string => {
    const v = args[k]
    return typeof v === 'string' ? v : ''
  }
  const limit = 70

  switch (name) {
    case 'web_search': case 'web.search': case 'search_web': {
      const q = str('query') || str('q') || str('objective')
      return q ? `"${q.slice(0, limit)}"` : ''
    }
    case 'web_fetch': case 'fetch_url': case 'http_get': {
      const u = str('url')
      return u ? u.slice(0, limit) : ''
    }
    case 'read_file': case 'read': return str('path') || str('file_path') || ''
    case 'write_file': case 'write': return str('path') || str('file_path') || ''
    case 'edit_file': case 'edit': return str('path') || str('file_path') || ''
    case 'bash': case 'shell': {
      const cmd = (str('command') || str('cmd')).split('\n')[0]
      return cmd ? `$ ${cmd.slice(0, limit)}` : ''
    }
    case 'glob': return str('pattern') || ''
    case 'grep': return str('pattern') ? `"${str('pattern').slice(0, 50)}"` : ''
    case 'publish_finding': return str('content').slice(0, 50)
    case 'sub_agent': return str('label') || ''
    case 'generate_image': return str('prompt').slice(0, limit)
  }
  if (typeof args.path === 'string') return args.path as string
  return ''
}
