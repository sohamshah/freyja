import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useHarness, type SessionSlice } from '../state/store'
import type { ToolCallRecord } from '@shared/events'
import { formatDuration, formatTokens } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { AgentTypeTag } from './SubagentCard'
import { BusActivityFeed, BusPublishBadge } from './BusActivityFeed'

/**
 * Full-viewport "Mission Control" view for a swarm of subagents.
 *
 * Opens as an overlay on top of the conversation. Each subagent gets a
 * tall, scrollable panel showing live streaming text and full tool call
 * history — far more detail than the compact grid tiles. Clicking a
 * panel "spotlights" it (expands to ~65% width) while the others
 * collapse into a compact sidebar.
 *
 * Keyboard:
 *   Esc       — un-spotlight (or close if already un-spotlighted)
 *   ↑/↓       — navigate between agents in spotlight sidebar
 *   Enter     — spotlight focused agent
 */
export function SwarmMonitor({
  ids,
  onClose,
}: {
  ids: string[]
  onClose: () => void
}) {
  const [spotlightId, setSpotlightId] = useState<string | null>(null)
  const [sidebarFocus, setSidebarFocus] = useState(0)

  // Keyboard navigation
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (spotlightId) {
          setSpotlightId(null)
        } else {
          onClose()
        }
        e.stopPropagation()
      }
      if (spotlightId) {
        if (e.key === 'ArrowDown' || e.key === 'j') {
          setSidebarFocus((i) => Math.min(i + 1, ids.length - 1))
          e.preventDefault()
        }
        if (e.key === 'ArrowUp' || e.key === 'k') {
          setSidebarFocus((i) => Math.max(i - 1, 0))
          e.preventDefault()
        }
        if (e.key === 'Enter') {
          setSpotlightId(ids[sidebarFocus])
          e.preventDefault()
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [spotlightId, onClose, ids, sidebarFocus])

  // When spotlight changes, sync sidebar focus
  useEffect(() => {
    if (spotlightId) {
      const idx = ids.indexOf(spotlightId)
      if (idx >= 0) setSidebarFocus(idx)
    }
  }, [spotlightId, ids])

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-[#0c0c10]/95 backdrop-blur-md">
      {/* Header bar — the zoom-counter-scaled --titlebar-inset var (set at the
          App root) clears the macOS traffic light buttons at any zoom level
          (hiddenInset title bar with trafficLightPosition y:16). */}
      <div
        className="drag flex items-center justify-between border-b border-white/[0.06] pr-5 py-2.5"
        style={{ paddingLeft: 'var(--titlebar-inset, 82px)' }}
      >
        <div className="no-drag flex items-center gap-3">
          <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-accent">
            swarm monitor
          </span>
          <span className="font-mono text-[10px] text-fg-2">
            {ids.length} agents
          </span>
          <RunningCount ids={ids} />
        </div>
        <div className="no-drag flex items-center gap-3">
          {spotlightId && (
            <button
              onClick={() => setSpotlightId(null)}
              className="rounded-md px-2 py-1 font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.06] hover:text-fg-0"
            >
              grid view
            </button>
          )}
          <button
            onClick={onClose}
            className="rounded-md px-2 py-1 font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.06] hover:text-fg-0"
          >
            esc close
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex flex-1 min-h-0 flex-col overflow-hidden">
        <div className="flex-1 min-h-0 overflow-hidden">
          {spotlightId ? (
            <SpotlightLayout
              ids={ids}
              spotlightId={spotlightId}
              sidebarFocus={sidebarFocus}
              onSelect={setSpotlightId}
            />
          ) : (
            <GridLayout ids={ids} onSpotlight={setSpotlightId} />
          )}
        </div>
        {/* Message bus activity feed — always visible at bottom */}
        <BusActivityFeed />
      </div>
    </div>
  )
}

/** Running agent counter with live pulse */
function RunningCount({ ids }: { ids: string[] }) {
  const subagents = useHarness((s) => s.subagents)
  const running = ids.filter((id) => {
    const s = subagents[id]
    return s && (s.state === 'running' || s.state === 'pending')
  }).length
  if (running === 0) return (
    <span className="font-mono text-[10px] text-ok">all completed</span>
  )
  return (
    <span className="flex items-center gap-1.5">
      <Spinner name="scan" className="text-accent" />
      <span className="font-mono text-[10px] text-accent">
        {running} running
      </span>
    </span>
  )
}

// ─── Grid Layout ───────────────────────────────────────────────────────

function GridLayout({
  ids,
  onSpotlight,
}: {
  ids: string[]
  onSpotlight: (id: string) => void
}) {
  const cols = ids.length <= 4 ? 2 : ids.length <= 9 ? 3 : 4
  return (
    <div
      className="flex-1 overflow-y-auto p-3"
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        gap: '8px',
        alignContent: 'start',
      }}
    >
      {ids.map((id) => (
        <MonitorCell key={id} id={id} onClick={() => onSpotlight(id)} />
      ))}
    </div>
  )
}

// ─── Spotlight Layout ──────────────────────────────────────────────────

function SpotlightLayout({
  ids,
  spotlightId,
  sidebarFocus,
  onSelect,
}: {
  ids: string[]
  spotlightId: string
  sidebarFocus: number
  onSelect: (id: string) => void
}) {
  return (
    <div className="flex flex-1 min-h-0">
      {/* Sidebar — compact agent list */}
      <div className="flex w-[200px] shrink-0 flex-col border-r border-white/[0.06] overflow-y-auto">
        {ids.map((id, i) => (
          <SidebarItem
            key={id}
            id={id}
            isActive={id === spotlightId}
            isFocused={i === sidebarFocus}
            onClick={() => onSelect(id)}
          />
        ))}
      </div>
      {/* Main — full stream of spotlighted agent */}
      <div className="flex-1 min-w-0">
        <MonitorCell id={spotlightId} expanded />
      </div>
    </div>
  )
}

function SidebarItem({
  id,
  isActive,
  isFocused,
  onClick,
}: {
  id: string
  isActive: boolean
  isFocused: boolean
  onClick: () => void
}) {
  const sub = useHarness((s) => s.subagents[id])
  if (!sub) return null
  const isRunning = sub.state === 'running' || sub.state === 'pending'
  const isDone = sub.state === 'done'
  const isFailed = sub.state === 'failed' || sub.state === 'cancelled'
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 px-3 py-2 text-left transition-colors border-l-2 ${
        isActive
          ? 'border-accent bg-accent/10 text-fg-0'
          : isFocused
            ? 'border-fg-2 bg-white/[0.03] text-fg-0'
            : 'border-transparent text-fg-2 hover:bg-white/[0.03] hover:text-fg-0'
      }`}
    >
      {isRunning ? (
        <Spinner name="scan" className="text-accent shrink-0" />
      ) : (
        <span
          className={`block h-1.5 w-1.5 shrink-0 rounded-full ${
            isFailed ? 'bg-danger' : isDone ? 'bg-ok' : 'bg-fg-2'
          }`}
        />
      )}
      <span className="truncate text-[11px]">{sub.label}</span>
      {sub.agentType && sub.agentType !== 'general' && (
        <AgentTypeTag type={sub.agentType} />
      )}
      <BusPublishBadge agentId={id} />
    </button>
  )
}

// ─── Monitor Cell ──────────────────────────────────────────────────────

function MonitorCell({
  id,
  expanded = false,
  onClick,
}: {
  id: string
  expanded?: boolean
  onClick?: () => void
}) {
  const sub = useHarness((s) => s.subagents[id])
  const childSlice = useHarness((s) => s.sessionArchive[id])
  const scrollRef = useRef<HTMLDivElement>(null)
  const userScrolledRef = useRef(false)

  const isRunning = sub?.state === 'running' || sub?.state === 'pending'
  const isDone = sub?.state === 'done'
  const isFailed = sub?.state === 'failed' || sub?.state === 'cancelled'

  // Extract live text from child session messages
  const liveText = useMemo(() => {
    if (!childSlice) return ''
    const parts: string[] = []
    for (const msg of childSlice.messages) {
      if (msg.role === 'assistant') {
        for (const p of msg.parts) {
          if (p.type === 'text' && p.text) parts.push(p.text)
        }
      }
    }
    return parts.join('\n').trim()
  }, [childSlice?.messages])

  // Extract recent tool calls
  const recentCalls = useMemo<ToolCallRecord[]>(() => {
    if (!childSlice) return []
    const order = childSlice.toolCallOrder ?? []
    const maxCalls = expanded ? 50 : 12
    return order
      .slice(-maxCalls)
      .map((tcId) => childSlice.toolCalls[tcId])
      .filter(Boolean)
  }, [childSlice, expanded])

  // Auto-scroll to bottom for live feed
  const contentForScroll = liveText.length + recentCalls.length
  useEffect(() => {
    if (userScrolledRef.current) return
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [contentForScroll])

  // Track user scroll
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      const dist = el.scrollHeight - el.scrollTop - el.clientHeight
      if (dist > 30) userScrolledRef.current = true
      else if (dist < 10) userScrolledRef.current = false
    }
    el.addEventListener('scroll', onScroll)
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  if (!sub) return null

  const statusColor = isFailed
    ? 'text-danger'
    : isDone ? 'text-ok'
    : isRunning ? 'text-accent'
    : 'text-fg-2'

  return (
    <div
      onClick={onClick}
      className={`flex flex-col overflow-hidden rounded-lg ${
        expanded ? 'h-full' : 'min-h-[260px] max-h-[45vh]'
      } ${
        isRunning ? 'ring-1 ring-accent/20' : 'ring-hairline'
      } bg-[#111118] transition-all ${
        onClick ? 'cursor-pointer hover:ring-accent/40' : ''
      }`}
    >
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-white/[0.06] px-3 py-2">
        {isRunning ? (
          <Spinner name="scan" className="text-accent" />
        ) : (
          <span
            className={`block h-1.5 w-1.5 rounded-full ${statusColor.replace('text-', 'bg-')}`}
          />
        )}
        <span className={`truncate text-[11px] font-medium text-fg-0 ${expanded ? '' : 'max-w-[180px]'}`}>
          {sub.label}
        </span>
        {sub.agentType && sub.agentType !== 'general' && (
          <AgentTypeTag type={sub.agentType} />
        )}
        <span className={`ml-auto font-mono text-[9px] uppercase tracking-[0.08em] ${statusColor}`}>
          {sub.state}
        </span>
      </div>

      {/* Scrollable content */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        {/* Task */}
        <div className={`border-b border-white/[0.04] px-3 py-2 text-[10.5px] leading-[1.5] text-fg-2 ${expanded ? '' : 'line-clamp-2'}`}>
          {sub.task}
        </div>

        {/* Live assistant text (if available) */}
        {liveText && (
          <div className="border-b border-white/[0.04] px-3 py-2">
            <div className="mb-1 font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
              output
            </div>
            <div
              className={`whitespace-pre-wrap text-[11px] leading-[1.6] text-fg-1 ${
                expanded ? '' : 'max-h-[120px] overflow-hidden'
              }`}
            >
              {expanded ? liveText : liveText.slice(0, 600) + (liveText.length > 600 ? '…' : '')}
            </div>
          </div>
        )}

        {/* Tool call feed — full URLs, not truncated */}
        <div className="px-3 py-2">
          <div className="mb-1.5 font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
            activity ({sub.toolsCalled} tools)
          </div>
          <div className="space-y-[3px]">
            {recentCalls.length === 0 ? (
              <div className="italic text-[10px] text-fg-3">
                {isRunning ? 'Starting…' : 'No activity'}
              </div>
            ) : (
              recentCalls.map((tc, i) => {
                const isLast = i === recentCalls.length - 1
                return (
                  <div key={tc.id} className="flex items-start gap-1.5 text-[10.5px]">
                    <span className="shrink-0 font-mono text-fg-3 mt-[1px]">
                      {isLast && isRunning ? '▸' : isLast ? '└' : '├'}
                    </span>
                    <span className={`${expanded ? '' : 'truncate'} text-fg-1`}>
                      {formatToolCall(tc, expanded)}
                    </span>
                    {tc.durationMs != null && tc.durationMs > 0 && (
                      <span className="shrink-0 ml-auto font-mono text-[9px] text-fg-3">
                        {tc.durationMs < 1000
                          ? `${tc.durationMs}ms`
                          : `${(tc.durationMs / 1000).toFixed(1)}s`}
                      </span>
                    )}
                  </div>
                )
              })
            )}
          </div>
        </div>

        {/* Result (when done) */}
        {sub.result && isDone && expanded && (
          <div className="border-t border-white/[0.04] px-3 py-2">
            <div className="mb-1.5 font-mono text-[9px] uppercase tracking-[0.08em] text-ok">
              final result
            </div>
            <div className="whitespace-pre-wrap text-[11px] leading-[1.6] text-fg-1">
              {sub.result}
            </div>
          </div>
        )}
      </div>

      {/* Footer stats */}
      <div className="flex items-center gap-3 border-t border-white/[0.06] px-3 py-1.5 text-[9.5px] text-fg-3">
        <span className="font-mono">{formatTokens(sub.tokensIn + sub.tokensOut)} tok</span>
        <span>·</span>
        <span className="font-mono">{formatDuration(sub.elapsedMs)}</span>
        <span>·</span>
        <span className="font-mono">{sub.toolsCalled} tools</span>
        {onClick && (
          <span className="ml-auto font-mono text-[9px] text-accent/60">
            click to spotlight
          </span>
        )}
      </div>
    </div>
  )
}

// ─── Helpers ───────────────────────────────────────────────────────────

function formatToolCall(tc: ToolCallRecord, full: boolean): string {
  const name = tc.name
  const args = (tc.arguments ?? {}) as Record<string, unknown>
  const str = (k: string): string => {
    const v = args[k]
    return typeof v === 'string' ? v : ''
  }
  const limit = full ? 200 : 80

  switch (name) {
    case 'web_search':
    case 'web.search':
    case 'search_web': {
      const q = str('query') || str('q')
      return q ? `search "${q}"` : 'search web'
    }
    case 'web_fetch':
    case 'fetch_url':
    case 'http_get': {
      const u = str('url')
      return u ? `fetch ${u.slice(0, limit)}` : 'fetch URL'
    }
    case 'read_file':
    case 'read': {
      const p = str('path') || str('file_path')
      return p ? `read ${p.slice(0, limit)}` : 'read file'
    }
    case 'write_file':
    case 'write': {
      const p = str('path') || str('file_path')
      return p ? `write ${p.slice(0, limit)}` : 'write file'
    }
    case 'bash':
    case 'shell': {
      const cmd = (str('command') || str('cmd')).split('\n')[0]
      return cmd ? `$ ${cmd.slice(0, limit)}` : 'bash'
    }
    case 'publish_finding':
      return `◆ Published: ${str('content').slice(0, limit)}`
    case 'read_findings':
      return `◇ Reading bus findings`
  }

  // Generic: tool name + first string arg
  const firstStr = Object.values(args).find(
    (v): v is string => typeof v === 'string' && v.length > 0,
  )
  if (firstStr) return `${name} ${firstStr.slice(0, limit)}`
  return name
}
