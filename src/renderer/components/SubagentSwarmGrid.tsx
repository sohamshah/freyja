import { useMemo, useState } from 'react'
import { useHarness } from '../state/store'
import type { ToolCallRecord } from '@shared/events'
import { formatDuration, formatTokens } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { SubagentCard, AgentTypeTag } from './SubagentCard'
import { SwarmMonitor } from './SwarmMonitor'
import { BusPublishBadge } from './BusActivityFeed'

/**
 * Swarm grid — renders a row of "monitors" side-by-side when the assistant
 * spawns multiple concurrent subagents. Inspired by the trading-floor look:
 * each tile is a compact live feed of that subagent's activity (label, task,
 * recent tool calls, token tally) and clicking it attaches to that session.
 *
 * When a swarm has only a single subagent, or when the user prefers the
 * stacked view, we delegate back to the original `SubagentCard` layout.
 */
export function SubagentSwarmGrid({ ids }: { ids: string[] }) {
  const subagents = useHarness((s) => s.subagents)
  const records = useMemo(
    () => ids.map((id) => subagents[id]).filter(Boolean),
    [ids, subagents],
  )

  const runningCount = records.filter(
    (r) => r.state === 'running' || r.state === 'pending',
  ).length
  const doneCount = records.filter((r) => r.state === 'done').length
  const total = records.length

  // Grid is the default when multiple subagents are alive. The toggle lets
  // the user pop back to the classic stacked layout at any time.
  const [mode, setMode] = useState<'grid' | 'stack'>('grid')
  const [monitorOpen, setMonitorOpen] = useState(false)

  // 1→1col, 2→2col, 3+→3col max (4 is too cramped inline)
  const cols = Math.min(3, Math.max(2, total))

  if (total === 0) return null
  if (total === 1) return <SubagentCard id={ids[0]} />

  return (
    <div className="my-2">
      {monitorOpen && (
        <SwarmMonitor ids={ids} onClose={() => setMonitorOpen(false)} />
      )}
      {/* Header — mirrors Slate's "Running 4 subagents… (0/4 completed)" */}
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          {runningCount > 0 ? (
            <Spinner name="scan" className="text-accent" />
          ) : (
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-ok" />
          )}
          <span className="label text-fg-2">
            {runningCount > 0
              ? `Running ${total} subagent${total === 1 ? '' : 's'}…`
              : `${total} subagent${total === 1 ? '' : 's'}`}
          </span>
          <span className="font-mono text-[10px] text-fg-3">
            ({doneCount}/{total} completed)
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setMonitorOpen(true)}
            className="rounded px-1.5 py-[2px] font-mono text-[9.5px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30 transition-colors hover:bg-accent/15"
          >
            monitor
          </button>
          <span className="font-mono text-[9.5px] uppercase tracking-[0.08em] text-fg-3">
            view
          </span>
          <button
            onClick={() => setMode('grid')}
            className={`rounded px-1.5 py-[2px] font-mono text-[9.5px] uppercase tracking-[0.08em] ring-hairline transition-colors ${
              mode === 'grid'
                ? 'bg-accent/15 text-accent ring-accent/30'
                : 'text-fg-2 hover:bg-white/[0.05] hover:text-fg-0'
            }`}
          >
            grid
          </button>
          <button
            onClick={() => setMode('stack')}
            className={`rounded px-1.5 py-[2px] font-mono text-[9.5px] uppercase tracking-[0.08em] ring-hairline transition-colors ${
              mode === 'stack'
                ? 'bg-accent/15 text-accent ring-accent/30'
                : 'text-fg-2 hover:bg-white/[0.05] hover:text-fg-0'
            }`}
          >
            stack
          </button>
        </div>
      </div>

      {mode === 'grid' ? (
        <div
          className="grid gap-2"
          style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
        >
          {ids.map((id) => (
            <SubagentTile key={id} id={id} />
          ))}
        </div>
      ) : (
        <div className="space-y-2.5">
          {ids.map((id) => (
            <SubagentCard key={id} id={id} />
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * A single "monitor" tile. Pulls live state from the subagent record and
 * the child session slice stored in `sessionArchive[id]`. The recent tool
 * calls feed gives the tile its trading-floor character.
 */
function SubagentTile({ id }: { id: string }) {
  const sub = useHarness((s) => s.subagents[id])
  const childSlice = useHarness((s) => s.sessionArchive[id])
  const childSnapshot = useHarness((s) =>
    s.sessions.find((session) => session.id === id),
  )
  const openSessionPane = useHarness((s) => s.openSessionPane)

  const recentCalls = useMemo<ToolCallRecord[]>(() => {
    if (!childSlice) return []
    const order = childSlice.toolCallOrder ?? []
    // Show the last ~6 tool calls so the tile reads like a short feed.
    return order
      .slice(-6)
      .map((tcId) => childSlice.toolCalls[tcId])
      .filter(Boolean)
  }, [childSlice])

  if (!sub) return null

  const isRunning = sub.state === 'running' || sub.state === 'pending'
  const isDone = sub.state === 'done'
  const isFailed = sub.state === 'failed' || sub.state === 'cancelled'

  const canAttach = Boolean(childSnapshot)
  const tokens = sub.tokensIn + sub.tokensOut

  return (
    <div
      onClick={(event) =>
        canAttach &&
        openSessionPane(id, event.metaKey || event.ctrlKey ? 'split' : 'replace')
      }
      title={canAttach ? 'Click to open; ⌘-click opens a split pane' : undefined}
      className={`group relative flex min-h-[170px] flex-col overflow-hidden rounded-lg glass-raised p-3.5 transition-all ${
        canAttach ? 'cursor-pointer hover:ring-1 hover:ring-accent/40' : ''
      }`}
    >
      {/* Header row — circle marker, "Subagent" label, state */}
      <div className="mb-1.5 flex items-center gap-1.5 min-w-0">
        {isRunning ? (
          <Spinner name="scan" className="shrink-0 text-accent" />
        ) : (
          <span
            className={`block h-1.5 w-1.5 shrink-0 rounded-full ${
              isFailed ? 'bg-danger' : isDone ? 'bg-ok' : 'bg-fg-2'
            }`}
          />
        )}
        <span className="label shrink-0 text-fg-2">Subagent:</span>
        {sub.agentType && sub.agentType !== 'general' && (
          <AgentTypeTag type={sub.agentType} />
        )}
        <span
          className={`ml-auto shrink-0 font-mono text-[9px] uppercase tracking-[0.08em] ${
            isFailed
              ? 'text-danger'
              : isDone
                ? 'text-ok'
                : isRunning
                  ? 'text-accent'
                  : 'text-fg-3'
          }`}
        >
          {sub.state}
        </span>
      </div>

      {/* Label (prompt/role) — one-line truncated */}
      <div className="mb-1 truncate text-[11px] text-fg-0">{sub.label}</div>

      {/* Task — two-line clamp, the "I'll conduct extensive research on..." */}
      <div className="mb-2 line-clamp-2 text-[10.5px] leading-[1.4] text-fg-1">
        {sub.task}
      </div>

      {/* Live feed of recent tool calls */}
      <div className="flex-1 space-y-[2px] overflow-hidden text-[10px] leading-[1.45] text-fg-2">
        {recentCalls.length === 0 ? (
          <div className="italic text-fg-3">
            {isRunning ? 'Starting…' : 'No activity recorded'}
          </div>
        ) : (
          recentCalls.map((tc, i) => {
            const isLast = i === recentCalls.length - 1
            return (
              <div key={tc.id} className="flex items-start gap-1">
                <span className="shrink-0 font-mono text-fg-3">
                  {isLast ? '└' : '├'}
                </span>
                <span className="truncate text-fg-1">
                  {summarizeToolCall(tc)}
                </span>
              </div>
            )
          })
        )}
      </div>

      {/* Footer stats — tokens + elapsed, Slate-style */}
      <div className="hairline-t mt-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 pt-1.5 text-[9.5px] text-fg-3">
        <span className="font-mono">
          {tokens > 0 ? formatTokens(tokens) : '—'}
        </span>
        <span>·</span>
        <span className="font-mono">{formatDuration(sub.elapsedMs)}</span>
        <span>·</span>
        <span className="font-mono">{sub.toolsCalled} tools</span>
        <BusPublishBadge agentId={id} />
        {canAttach && (
          <span className="ml-auto font-mono text-accent/80">attach →</span>
        )}
      </div>
    </div>
  )
}

/**
 * Turn a raw ToolCallRecord into a one-line "trading-ticker" string. The
 * common tools get hand-tuned summaries; anything else falls
 * back to a generic `name(first_arg)` preview.
 */
function summarizeToolCall(tc: ToolCallRecord): string {
  const name = tc.name
  const args = (tc.arguments ?? {}) as Record<string, unknown>
  const str = (k: string): string => {
    const v = args[k]
    return typeof v === 'string' ? v : ''
  }

  switch (name) {
    case 'web_search':
    case 'web.search':
    case 'search_web': {
      const q = str('query') || str('q')
      return q ? `Searching web "${q}"` : 'Searching web'
    }
    case 'web_fetch':
    case 'fetch_url':
    case 'http_get': {
      const u = str('url')
      return u ? `Fetching ${shorten(u, 38)}` : 'Fetching URL'
    }
    case 'todo_write':
    case 'update_todo':
    case 'write_todo':
      return 'Updating todo list'
    case 'read_file':
    case 'read': {
      const p = str('path') || str('file_path') || str('file')
      return p ? `Reading ${shorten(p, 38)}` : 'Reading file'
    }
    case 'write_file':
    case 'write': {
      const p = str('path') || str('file_path') || str('file')
      return p ? `Writing ${shorten(p, 38)}` : 'Writing file'
    }
    case 'edit_file':
    case 'edit': {
      const p = str('path') || str('file_path') || str('file')
      return p ? `Editing ${shorten(p, 38)}` : 'Editing file'
    }
    case 'bash':
    case 'run_bash':
    case 'shell': {
      const cmd = str('command') || str('cmd')
      const first = cmd.split('\n')[0]
      return first ? `$ ${shorten(first, 38)}` : 'Running bash'
    }
    case 'glob': {
      const p = str('pattern') || str('glob')
      return p ? `Glob ${shorten(p, 34)}` : 'Glob'
    }
    case 'grep': {
      const p = str('pattern') || str('query')
      return p ? `Grep "${shorten(p, 32)}"` : 'Grep'
    }
    case 'spawn_subagent':
    case 'subagent_spawn':
      return `Spawning ${str('label') || 'subagent'}`
    case 'publish_finding':
      return `◆ Published: ${shorten(str('content'), 34)}`
    case 'read_findings':
      return `◇ Reading bus findings`
  }

  // Generic fallback — tool name + first string-valued arg.
  const firstStr = Object.values(args).find(
    (v): v is string => typeof v === 'string' && v.length > 0,
  )
  if (firstStr) return `${name} ${shorten(firstStr, 30)}`
  return name
}

function shorten(s: string, max: number): string {
  if (s.length <= max) return s
  return s.slice(0, max - 1) + '…'
}
