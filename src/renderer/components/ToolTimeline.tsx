import { useMemo } from 'react'
import { useHarness } from '../state/store'
import { formatDuration } from '../lib/format'
import type { ToolCallRecord } from '@shared/events'
import { StickyHeader } from './StickyHeader'

/**
 * Gantt-style micro timeline for the activity panel.
 *
 * Horizontal bars showing start/end time of each tool call relative
 * to the session's first tool call. Parallel calls stack vertically
 * at the same horizontal position. Color by tool category.
 */

const CATEGORY_COLORS: Record<string, string> = {
  read_file: '#6ba3d6', read: '#6ba3d6', glob: '#6ba3d6', grep: '#6ba3d6', list_directory: '#6ba3d6',
  write_file: '#5bbb5b', write: '#5bbb5b', edit_file: '#5bbb5b', edit: '#5bbb5b', edit_json: '#5bbb5b',
  web_search: '#e0a040', web_fetch: '#e0a040', web_research: '#e0a040',
  bash: '#b080d0', shell: '#b080d0',
  sub_agent: '#60c0c0', subagents: '#60c0c0',
  publish_finding: '#d0a040', read_findings: '#d0a040',
}

function getColor(name: string): string {
  return CATEGORY_COLORS[name] ?? '#888'
}

export function ToolTimeline() {
  const toolCallOrder = useHarness((s) => s.toolCallOrder)
  const toolCalls = useHarness((s) => s.toolCalls)

  const records = useMemo(
    () => toolCallOrder.map((id) => toolCalls[id]).filter(Boolean) as ToolCallRecord[],
    [toolCallOrder, toolCalls],
  )

  // Segment the timeline: when there's a long idle gap between tool
  // calls (session continued hours later), collapse it into a fixed
  // visual break so the activity segments stay readable instead of
  // being compressed into a thin strip at one edge of the chart.
  //
  // Each segment has its own proportional share of the visual width,
  // allocated by its real duration, but gaps between segments get a
  // fixed small gutter.
  const IDLE_GAP_THRESHOLD_MS = 5 * 60 * 1000 // 5 minutes
  const segments = useMemo(() => {
    if (records.length === 0) {
      return {
        segs: [] as Array<{ startTime: number; endTime: number; gapBefore: number }>,
        totalRealSpan: 1,
      }
    }
    const sorted = [...records].sort((a, b) => a.startedAt - b.startedAt)
    const segs: Array<{ startTime: number; endTime: number; gapBefore: number }> = []
    let curStart = sorted[0].startedAt
    let curEnd = curStart + (sorted[0].durationMs ?? 0)
    for (let i = 1; i < sorted.length; i++) {
      const r = sorted[i]
      const recEnd = r.startedAt + (r.durationMs ?? 0)
      const gap = r.startedAt - curEnd
      if (gap > IDLE_GAP_THRESHOLD_MS) {
        segs.push({ startTime: curStart, endTime: curEnd, gapBefore: 0 })
        curStart = r.startedAt
        curEnd = recEnd
      } else {
        curEnd = Math.max(curEnd, recEnd)
      }
    }
    segs.push({ startTime: curStart, endTime: curEnd, gapBefore: 0 })
    // Fill in the gap-before field
    for (let i = 1; i < segs.length; i++) {
      segs[i].gapBefore = segs[i].startTime - segs[i - 1].endTime
    }
    const totalRealSpan = segs.reduce(
      (sum, s) => sum + Math.max(1, s.endTime - s.startTime),
      0,
    )
    return { segs, totalRealSpan }
  }, [records])

  // Visual layout: segments share the non-gutter portion of the width
  // proportionally to their real duration. Each idle gap consumes a
  // fixed visual percentage so it reads as a distinct break.
  const GAP_VISUAL_PCT = 4 // each idle gap takes 4% of width
  const layout = useMemo(() => {
    const { segs, totalRealSpan } = segments
    if (segs.length === 0) {
      return {
        segments: [] as Array<{ startTime: number; endTime: number; pctStart: number; pctWidth: number; gapBefore: number }>,
      }
    }
    const totalGapPct = GAP_VISUAL_PCT * (segs.length - 1)
    const segSpacePct = 100 - totalGapPct
    let cursor = 0
    const laid: Array<{ startTime: number; endTime: number; pctStart: number; pctWidth: number; gapBefore: number }> = []
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i]
      if (i > 0) cursor += GAP_VISUAL_PCT
      const realDur = Math.max(1, s.endTime - s.startTime)
      const pctWidth = (realDur / totalRealSpan) * segSpacePct
      laid.push({
        startTime: s.startTime,
        endTime: s.endTime,
        pctStart: cursor,
        pctWidth,
        gapBefore: s.gapBefore,
      })
      cursor += pctWidth
    }
    return { segments: laid }
  }, [segments])

  // Map a real timestamp to a visual percentage using the segment layout.
  function timeToPct(t: number): number | null {
    for (const s of layout.segments) {
      if (t >= s.startTime && t <= s.endTime) {
        const localSpan = Math.max(1, s.endTime - s.startTime)
        const localPct = (t - s.startTime) / localSpan
        return s.pctStart + localPct * s.pctWidth
      }
    }
    return null
  }

  const totalSpan = useMemo(() => {
    if (records.length === 0) return 1
    const starts = records.map((r) => r.startedAt)
    const ends = records.map((r) => r.startedAt + (r.durationMs ?? 0))
    return Math.max(1, Math.max(...ends) - Math.min(...starts))
  }, [records])

  const hasCollapsedGaps = layout.segments.length > 1

  // Assign rows — parallel calls (same groupId) stack vertically
  const rows = useMemo(() => {
    // Greedy row-packing: for each tool call, place it on the lowest
    // row where its time range doesn't overlap with any previously
    // placed bar on that row. Sequential calls stack on row 0; parallel
    // calls (overlapping time ranges) spill onto rows 1, 2, ...
    //
    // Falls back to `groupId` grouping when durations are 0 or missing —
    // same groupId stacks vertically, different groupIds share row 0.
    const result: Array<{ record: ToolCallRecord; row: number }> = []
    const rowEndTimes: number[] = [] // last end time on each row

    // Sort by start time so greedy assignment is deterministic.
    const sorted = [...records].sort((a, b) => a.startedAt - b.startedAt)

    for (const r of sorted) {
      const start = r.startedAt
      // Running calls with no duration get a small projected window so
      // the packer doesn't think they're instantaneous.
      const dur = r.durationMs ?? (r.status === 'running' ? 1000 : 0)
      const end = start + dur

      // Find the lowest row whose last bar ended before this one starts.
      let assigned = -1
      for (let row = 0; row < rowEndTimes.length; row++) {
        if (rowEndTimes[row] <= start) {
          assigned = row
          break
        }
      }
      if (assigned === -1) {
        assigned = rowEndTimes.length
        rowEndTimes.push(end)
      } else {
        rowEndTimes[assigned] = end
      }
      result.push({ record: r, row: assigned })
    }
    return result
  }, [records])

  const totalRows = rows.length > 0 ? Math.max(...rows.map((r) => r.row)) + 1 : 0
  const ROW_HEIGHT = 16
  // Internal padding above the bars: gives first-row tooltips room to
  // pop upward without escaping the chart's visual frame. Matched by
  // an 8px buffer baked into the overall chart height so the bars sit
  // in a comfortable middle band.
  const TOP_PAD = 24
  const chartHeight = Math.max(40, totalRows * ROW_HEIGHT + 8)

  if (records.length === 0) {
    return (
      <div className="hairline-b">
        <StickyHeader>
          <div className="flex w-full items-baseline justify-between gap-2 px-4 py-2">
            <div className="label">tool calls</div>
            <div className="font-mono text-[10px] text-fg-3">0</div>
          </div>
        </StickyHeader>
        <div className="px-4 pb-3 pt-1 text-[11px] italic text-fg-3">No tool calls yet</div>
      </div>
    )
  }

  return (
    <div className="hairline-b">
      <StickyHeader>
        <div className="flex w-full items-baseline justify-between gap-2 px-4 py-2">
          <div className="label">tool calls</div>
          <div className="font-mono text-[10px] text-fg-3">
            {records.length} · {formatDuration(totalSpan)}
          </div>
        </div>
      </StickyHeader>

      <div className="px-4 pb-3 pt-1">
      {/* Gantt chart. We keep the rounded frame + ring on the outer,
          but drop overflow:hidden so first-row tooltips can extend
          upward without being clipped. A 24px top buffer + 8px bottom
          buffer is baked into the bar y-coords so tooltips land inside
          this breathing room rather than escaping the box. */}
      <div
        className="relative w-full rounded-lg bg-black/25 ring-hairline"
        style={{ height: `${chartHeight + 32}px` }}
      >
        {/* Segment backgrounds — subtle highlight per activity segment */}
        {layout.segments.map((seg, i) => (
          <div
            key={`seg-${i}`}
            className="absolute top-0 h-full bg-white/[0.015]"
            style={{
              left: `${seg.pctStart}%`,
              width: `${seg.pctWidth}%`,
            }}
          />
        ))}

        {/* Collapsed gap markers — vertical dashed line + duration label */}
        {layout.segments.map((seg, i) => {
          if (i === 0 || seg.gapBefore <= 0) return null
          const gapLeft = seg.pctStart - GAP_VISUAL_PCT
          return (
            <div
              key={`gap-${i}`}
              className="absolute top-0 flex h-full flex-col items-center justify-center"
              style={{
                left: `${gapLeft}%`,
                width: `${GAP_VISUAL_PCT}%`,
              }}
              title={`Idle gap: ${formatDuration(seg.gapBefore)}`}
            >
              {/* Dashed vertical hatch */}
              <div
                className="h-full w-full"
                style={{
                  background:
                    'repeating-linear-gradient(45deg, transparent, transparent 3px, rgba(255,255,255,0.06) 3px, rgba(255,255,255,0.06) 4px)',
                }}
              />
              {/* Small gap label */}
              <div className="pointer-events-none absolute inset-x-0 top-1/2 -translate-y-1/2 text-center">
                <span className="rounded bg-[#1a1a1e]/80 px-1 py-[1px] font-mono text-[7.5px] font-bold text-fg-3 ring-1 ring-white/[0.08]">
                  ~{shortDuration(seg.gapBefore)}
                </span>
              </div>
            </div>
          )
        })}

        {/* Time grid lines — inside each segment for reference */}
        {layout.segments.map((seg, i) => (
          <div
            key={`grid-${i}`}
            className="absolute top-0 h-full w-px bg-white/[0.04]"
            style={{ left: `${seg.pctStart + seg.pctWidth / 2}%` }}
          />
        ))}

        {/* Tool bars */}
        {rows.map(({ record, row }) => {
          const startPct = timeToPct(record.startedAt)
          const endPct = timeToPct(record.startedAt + (record.durationMs ?? 0))
          if (startPct == null) return null
          const widthPct = endPct != null
            ? Math.max(1.5, endPct - startPct)
            : record.status === 'running' ? 4 : 1.5
          const color = getColor(record.name)
          const isRunning = record.status === 'running'
          const isError = record.status === 'error'

          return (
            <div
              key={record.id}
              className="absolute group"
              style={{
                left: `${startPct}%`,
                width: `${Math.max(1.5, widthPct)}%`,
                top: `${row * ROW_HEIGHT + 4 + TOP_PAD}px`,
                height: `${ROW_HEIGHT - 4}px`,
              }}
            >
              {/* Bar */}
              <div
                className={`h-full rounded-sm transition-all duration-150 group-hover:scale-y-110 group-hover:brightness-125 ${
                  isRunning ? 'animate-pulse' : ''
                }`}
                style={{
                  backgroundColor: isError ? '#e05050' : color,
                  opacity: isRunning ? 0.6 : 0.85,
                  boxShadow: 'inset 0 0.5px 0 0 rgba(255,255,255,0.18)',
                }}
              />

              {/* Tooltip on hover — escapes the chart via overflow-visible
                   on the chart container. z-30 keeps it above sibling bars
                   and the activity panel drag handle (z-10). */}
              <div className="pointer-events-none absolute bottom-full left-0 z-30 mb-1.5 hidden rounded-md bg-[#0e0e10]/95 px-2 py-1 text-[9.5px] text-fg-0 shadow-[0_8px_20px_-8px_rgba(0,0,0,0.7)] ring-1 ring-white/[0.08] backdrop-blur-md group-hover:block whitespace-nowrap">
                <span className="font-bold" style={{ color }}>{record.name}</span>
                {record.durationMs != null && (
                  <span className="ml-1.5 text-fg-3">{formatDuration(record.durationMs)}</span>
                )}
                {summarizeShort(record) && (
                  <div className="mt-0.5 max-w-[220px] truncate text-fg-2">
                    {summarizeShort(record)}
                  </div>
                )}
                {/* Pointer caret connecting the tooltip to the bar */}
                <span
                  className="absolute left-3 top-full h-0 w-0"
                  style={{
                    borderLeft: '4px solid transparent',
                    borderRight: '4px solid transparent',
                    borderTop: '4px solid rgba(14,14,16,0.95)',
                  }}
                />
              </div>
            </div>
          )
        })}
      </div>

      {hasCollapsedGaps && (
        <div className="mt-1 font-mono text-[8.5px] text-fg-3">
          {layout.segments.length} activity segment{layout.segments.length !== 1 ? 's' : ''} —
          idle gaps &gt; 5min collapsed
        </div>
      )}

      {/* Legend */}
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
        {uniqueCategories(records).map(({ name, color }) => (
          <div key={name} className="flex items-center gap-1">
            <span
              className="block h-[6px] w-[6px] rounded-sm"
              style={{ backgroundColor: color }}
            />
            <span className="font-mono text-[8px] text-fg-3">{name}</span>
          </div>
        ))}
      </div>
      </div>
    </div>
  )
}

function shortDuration(ms: number): string {
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m`
  if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)}h`
  return `${Math.round(ms / 86_400_000)}d`
}

function summarizeShort(r: ToolCallRecord): string {
  const args = r.arguments as Record<string, unknown> | undefined
  if (!args) return ''
  const s = (k: string) => typeof args[k] === 'string' ? args[k] as string : ''
  return s('path') || s('file_path') || s('query') || s('url')?.slice(0, 50) || s('command')?.split('\n')[0]?.slice(0, 50) || ''
}

function uniqueCategories(records: ToolCallRecord[]) {
  const seen = new Map<string, string>()
  for (const r of records) {
    const color = getColor(r.name)
    const cat = Object.entries(CATEGORY_COLORS).find(([, c]) => c === color)?.[0] ?? r.name
    // Use the category color as key to deduplicate
    if (!seen.has(color)) {
      const label = color === '#6ba3d6' ? 'read' : color === '#5bbb5b' ? 'write' : color === '#e0a040' ? 'web' : color === '#b080d0' ? 'shell' : color === '#60c0c0' ? 'agent' : color === '#d0a040' ? 'bus' : r.name
      seen.set(color, label)
    }
  }
  return Array.from(seen.entries()).map(([color, name]) => ({ name, color }))
}
