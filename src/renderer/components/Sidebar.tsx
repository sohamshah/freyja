import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import type {
  MemoryRecord,
  SessionSnapshot,
  Skill,
  SubagentRecord,
  SubagentState,
} from '@shared/events'
import { formatDuration, formatTokens, relativeTime } from '../lib/format'
import { Spinner } from '../lib/spinner'

type Section = 'sessions' | 'skills' | 'subagents' | 'memory'

const CONFIDENCE_ORDER: Record<Skill['confidence'], number> = {
  verified: 0,
  experimental: 1,
  unvalidated: 2,
  deprecated: 3,
}

const CONFIDENCE_COLOR: Record<Skill['confidence'], string> = {
  verified: 'bg-ok',
  experimental: 'bg-warn',
  unvalidated: 'bg-fg-2',
  deprecated: 'bg-danger',
}

const SKILL_STATUS_ORDER: Record<NonNullable<Skill['status']>, number> = {
  loaded: 0,
  suggested: 1,
  available: 2,
  pruned: 3,
}

const SKILL_SCOPE_ORDER: Record<string, number> = {
  project: 0,
  user: 1,
  compat: 2,
}

export function Sidebar() {
  const sessions = useHarness((s) => s.sessions)
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const skills = useHarness((s) => s.skills)
  const memories = useHarness((s) => s.memories)
  const subagents = useHarness((s) => s.subagents)
  const subagentOrder = useHarness((s) => s.subagentOrder)
  const computerSessions = useHarness((s) => s.computerSessions)
  const openSubagent = useHarness((s) => s.openSubagent)
  const newSession = useHarness((s) => s.newSession)
  const switchSession = useHarness((s) => s.switchSession)
  const messageCount = useHarness((s) => s.messages.length)
  const toggleSidebar = useHarness((s) => s.toggleSidebar)

  const [open, setOpen] = useState<Record<Section, boolean>>({
    sessions: true,
    skills: false,
    subagents: true,
    memory: false,
  })

  const sortedSkills = useMemo(() => {
    return Object.values(skills).sort((a, b) => {
      const sa = SKILL_STATUS_ORDER[a.status ?? 'available']
      const sb = SKILL_STATUS_ORDER[b.status ?? 'available']
      if (sa !== sb) return sa - sb
      const c = CONFIDENCE_ORDER[a.confidence] - CONFIDENCE_ORDER[b.confidence]
      if (c !== 0) return c
      const scope =
        (SKILL_SCOPE_ORDER[a.scope ?? 'project'] ?? 9) -
        (SKILL_SCOPE_ORDER[b.scope ?? 'project'] ?? 9)
      if (scope !== 0) return scope
      const activity =
        ((b.loadCount ?? 0) + b.retrievalCount) -
        ((a.loadCount ?? 0) + a.retrievalCount)
      if (activity !== 0) return activity
      return a.name.localeCompare(b.name)
    })
  }, [skills])

  const sortedMemories = useMemo(() => {
    return Object.values(memories).sort((a, b) => {
      const at = a.updatedAt ?? a.createdAt ?? 0
      const bt = b.updatedAt ?? b.createdAt ?? 0
      return bt - at
    })
  }, [memories])

  // Children of the active session — treat each as a first-class sub-session.
  const childSessions: SessionSnapshot[] = useMemo(() => {
    return sessions.filter((s) => s.parentSessionId === activeSessionId)
  }, [sessions, activeSessionId])

  // Flatten the session list into a depth-first tree walk so subagent
  // sessions render indented under the session that spawned them. Orphan
  // children (whose parent is no longer in the list) are promoted to roots
  // so they're still reachable.
  //
  // Ordering is based on START TIME (createdAt) — never updatedAt.
  // We deliberately avoid update-bubbling because it causes constant
  // re-shuffling when many subagents are running concurrently, which
  // makes the sidebar disorienting and breaks the natural numbering of
  // sequentially-spawned subagents (e.g. tasks 1, 2, 3, 4 should always
  // render in that order, not whichever happened to update last).
  //
  //   - Top-level sessions: createdAt DESCENDING (newest at top)
  //   - Subagent sessions:  createdAt ASCENDING  (spawn order preserved)
  const sessionTree: Array<SessionSnapshot & { depth: number }> = useMemo(() => {
    const ids = new Set(sessions.map((s) => s.id))
    const byParent = new Map<string | null, SessionSnapshot[]>()
    for (const s of sessions) {
      const pid =
        s.parentSessionId && ids.has(s.parentSessionId) ? s.parentSessionId : null
      if (!byParent.has(pid)) byParent.set(pid, [])
      byParent.get(pid)!.push(s)
    }

    const out: Array<SessionSnapshot & { depth: number }> = []
    const walk = (parentId: string | null, depth: number) => {
      const kids = byParent.get(parentId) ?? []
      const sorted = [...kids].sort((a, b) =>
        depth === 0
          // Top-level: newest first.
          ? b.createdAt - a.createdAt
          // Subagent: spawn order (oldest first).
          : a.createdAt - b.createdAt,
      )
      for (const s of sorted) {
        out.push({ ...s, depth })
        walk(s.id, depth + 1)
      }
    }
    walk(null, 0)
    return out
  }, [sessions])

  const orderedSubagents: SubagentRecord[] = useMemo(() => {
    return subagentOrder.map((id) => subagents[id]).filter(Boolean) as SubagentRecord[]
  }, [subagentOrder, subagents])

  return (
    <aside className="glass flex w-[256px] shrink-0 flex-col rounded-[18px]">
      <div className="flex items-center justify-between px-3 py-3 hairline-b">
        <div className="label">workspace</div>
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => newSession()}
            title={messageCount > 0 ? 'Start a fresh session (⌘N)' : 'New session (⌘N)'}
            className="rounded bg-accent/15 px-2 py-[2px] font-mono text-[10px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30 hover:bg-accent/25"
          >
            + new
          </button>
          <button
            onClick={() => toggleSidebar(true)}
            title="Collapse sidebar (⌘[)"
            className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[11px] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
          >
            ‹
          </button>
        </div>
      </div>

      {/* Sessions — scrollable, takes remaining space */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <Section
          title="sessions"
          count={sessions.length}
          open={open.sessions}
          onToggle={() => setOpen((p) => ({ ...p, sessions: !p.sessions }))}
        >
          {sessionTree.map((s) => (
            <SessionRow
              key={s.id}
              session={s}
              depth={s.depth}
              isActive={s.id === activeSessionId}
              onSwitch={() => switchSession(s.id)}
            />
          ))}
        </Section>
      </div>

      {/* Swarm / Skills / Memory — pinned to bottom, own scroll */}
      <div className="shrink-0 overflow-y-auto hairline-t" style={{ maxHeight: '40%' }}>
        <Section
          title="swarm"
          count={childSessions.length}
          open={open.subagents}
          onToggle={() => setOpen((p) => ({ ...p, subagents: !p.subagents }))}
        >
          {childSessions.length === 0 && (
            <div className="px-2 py-2 text-[11px] italic text-fg-2">
              No sub-agents for this session
            </div>
          )}
          {childSessions.map((child) => {
            const subRecord = subagents[child.id]
            const running = !child.completed
            const failed = child.completed && child.success === false
            const computerSession = computerSessions[child.id]
            const isComputer = Boolean(computerSession)
            return (
              <button
                key={child.id}
                onClick={() => switchSession(child.id)}
                className={`group relative flex w-full items-start gap-2 rounded-md px-2 py-[7px] text-left text-[12px] text-fg-1 hover:bg-white/[0.04] ${
                  running ? 'swarm-row-running' : ''
                }`}
              >
                {/* Animated left accent bar for running agents */}
                {running && (
                  <span
                    className="absolute inset-y-1 left-0 w-[2px] rounded-full"
                    style={{
                      background: isComputer
                        ? 'var(--color-warn, #f5b640)'
                        : 'var(--color-accent, #6e8eff)',
                      animation: 'swarm-pulse 2s ease-in-out infinite',
                    }}
                  />
                )}
                <span className="mt-[3px] flex h-3 w-3 items-center justify-center">
                  {running ? (
                    <Spinner
                      name="scan"
                      className={isComputer ? 'text-warn' : 'text-accent'}
                    />
                  ) : (
                    <span
                      className={`block h-1.5 w-1.5 rounded-full ${
                        failed ? 'bg-danger' : 'bg-ok'
                      }`}
                    />
                  )}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-fg-0">{child.title}</span>
                    {isComputer && (
                      <span className="shrink-0 rounded bg-warn/15 px-1 py-[1px] font-mono text-[8.5px] uppercase tracking-[0.08em] text-warn ring-1 ring-warn/30">
                        computer
                      </span>
                    )}
                  </div>
                  <div className="mt-[2px] flex items-center gap-1.5 text-[10px] text-fg-2">
                    <span className="font-mono">
                      {child.model.replace('claude-', '')}
                    </span>
                    <span>·</span>
                    <span className="font-mono">
                      {subRecord
                        ? formatDuration(subRecord.elapsedMs)
                        : relativeTime(child.createdAt)}
                    </span>
                    <span>·</span>
                    <span className="font-mono">
                      {isComputer
                        ? `${computerSession.history.length} act`
                        : formatTokens(
                            (child.totalInputTokens ?? 0) +
                              (child.totalOutputTokens ?? 0),
                          )}
                    </span>
                  </div>
                </div>
                <span className="ml-1 self-center font-mono text-[9px] text-fg-3">
                  attach →
                </span>
              </button>
            )
          })}
        </Section>

        <Section
          title="skills"
          count={sortedSkills.length}
          open={open.skills}
          onToggle={() => setOpen((p) => ({ ...p, skills: !p.skills }))}
        >
          {sortedSkills.map((skill) => (
            <SkillRow key={skill.id} skill={skill} />
          ))}
          {sortedSkills.length === 0 && (
            <div className="px-2 py-2 text-[11px] italic leading-[1.5] text-fg-2">
              No skills available yet
            </div>
          )}
        </Section>

        <Section
          title="memory"
          count={sortedMemories.length}
          open={open.memory}
          onToggle={() => setOpen((p) => ({ ...p, memory: !p.memory }))}
        >
          {sortedMemories.slice(0, 8).map((memory) => (
            <MemoryRow key={memory.id} memory={memory} />
          ))}
          {sortedMemories.length > 8 && (
            <div className="px-2 py-1 text-[10.5px] text-fg-3">
              +{sortedMemories.length - 8} more in memory
            </div>
          )}
          {sortedMemories.length === 0 && (
            <div className="px-2 py-2 text-[11px] italic text-fg-2">
              No memory loaded yet
            </div>
          )}
        </Section>
      </div>

      <div className="shrink-0 hairline-t flex items-center justify-between px-3 py-2 text-[10px] text-fg-2">
        <button
          onClick={() => useHarness.getState().toggleSettings(true)}
          className="flex items-center gap-1.5 rounded px-1.5 py-[2px] text-fg-1 hover:bg-white/[0.05] hover:text-fg-0"
          title="Settings (⌘,)"
        >
          <span className="font-mono text-[11px]">⚙</span>
          <span>settings</span>
        </button>
        <span>
          <kbd className="kbd">⌘</kbd>
          <kbd className="kbd ml-1">,</kbd>
        </span>
      </div>
    </aside>
  )
}

function Section({
  title,
  count,
  open,
  onToggle,
  children,
}: {
  title: string
  count: number
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <div className="px-2 py-2">
      <button
        onClick={onToggle}
        className="mb-1 flex w-full items-center justify-between px-1 label hover:text-fg-1"
      >
        <span className="flex items-center gap-1.5">
          <Caret open={open} />
          {title}
        </span>
        <span className="font-mono text-fg-3">{count}</span>
      </button>
      {open && <div className="space-y-0.5">{children}</div>}
    </div>
  )
}

function SkillRow({ skill }: { skill: Skill }) {
  const totalSignals = skill.successSignals + skill.failureSignals
  const successRate = totalSignals > 0
    ? Math.round((skill.successSignals / totalSignals) * 100)
    : null
  const status = skill.status ?? 'available'

  return (
    <div
      className={`group flex w-full items-start gap-2 rounded-md px-2 py-[6px] text-left hover:bg-white/[0.04] ${
        status === 'loaded' ? 'bg-accent/[0.06]' : ''
      }`}
      title={`${skill.description}\n\n${skill.scope ?? 'project'} · ${skill.skillType} · ${skill.confidence}\n${skill.retrievalCount} retrievals · ${skill.loadCount ?? 0} loads · ${skill.successSignals} success · ${skill.failureSignals} fail`}
    >
      <span
        className={`mt-[7px] inline-block h-1.5 w-1.5 rounded-full ${CONFIDENCE_COLOR[skill.confidence]}`}
      />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-1.5">
          <div className="truncate text-[12.5px] text-fg-0">{skill.name}</div>
          {status !== 'available' && (
            <span
              className={`shrink-0 rounded px-1 py-[1px] font-mono text-[8.5px] uppercase tracking-[0.08em] ring-1 ${
                status === 'loaded'
                  ? 'bg-accent/15 text-accent ring-accent/25'
                  : status === 'pruned'
                    ? 'bg-fg-2/10 text-fg-2 ring-white/10'
                    : 'bg-white/[0.04] text-fg-2 ring-white/10'
              }`}
            >
              {status}
            </span>
          )}
        </div>
        <div className="mt-[2px] flex min-w-0 items-center gap-1.5 text-[10.5px] text-fg-2">
          <span className="uppercase">{skill.skillType}</span>
          <span>·</span>
          <span>{skill.scope ?? 'project'}</span>
          <span>·</span>
          <span>{skill.loadCount ? `${skill.loadCount} load` : `${skill.retrievalCount} seen`}</span>
          {successRate !== null && (
            <>
              <span>·</span>
              <span className={skill.successSignals >= skill.failureSignals ? 'text-ok' : 'text-warn'}>
                {successRate}%
              </span>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function MemoryRow({ memory }: { memory: MemoryRecord }) {
  const text = memory.summary || memory.text
  return (
    <div
      className="group flex w-full items-start gap-2 rounded-md px-2 py-[6px] text-left hover:bg-white/[0.04]"
      title={`${memory.text}\n\n${memory.scope} · ${memory.kind}${memory.source ? ` · ${memory.source}` : ''}`}
    >
      <span className="mt-[7px] inline-block h-1.5 w-1.5 rounded-full bg-accent" />
      <div className="min-w-0 flex-1">
        <div className="line-clamp-2 text-[11.5px] leading-[1.35] text-fg-0">
          {text}
        </div>
        <div className="mt-[2px] flex items-center gap-1.5 text-[10.5px] text-fg-2">
          <span className="uppercase">{memory.scope}</span>
          <span>·</span>
          <span>{memory.kind}</span>
          {memory.updatedAt && (
            <>
              <span>·</span>
              <span>{relativeTime(memory.updatedAt)}</span>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function Caret({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 10 10"
      width="8"
      height="8"
      style={{ transform: open ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 120ms' }}
    >
      <path d="M3 2 L7 5 L3 8 Z" fill="currentColor" />
    </svg>
  )
}

const SUBAGENT_STATE_COLOR: Record<SubagentState, string> = {
  pending: 'bg-fg-3',
  running: 'bg-accent',
  done: 'bg-ok',
  failed: 'bg-danger',
  cancelled: 'bg-fg-2',
}

function StateDot({ state }: { state: SubagentState }) {
  if (state === 'running') {
    return (
      <span className="mt-[4px] inline-flex h-3 w-3 items-center justify-center text-accent">
        <Spinner name="orbit" />
      </span>
    )
  }
  return (
    <span className="relative mt-[6px]">
      <span
        className={`inline-block h-1.5 w-1.5 rounded-full ${SUBAGENT_STATE_COLOR[state]}`}
      />
    </span>
  )
}

// ─── Session row with context menu ──────────────────────────────────

function SessionRow({
  session: s,
  depth,
  isActive,
  onSwitch,
}: {
  session: SessionSnapshot & { depth: number }
  depth: number
  isActive: boolean
  onSwitch: () => void
}) {
  const renameSession = useHarness((st) => st.renameSession)
  const deleteSession = useHarness((st) => st.deleteSession)
  const downloadSession = useHarness((st) => st.downloadSession)
  const [menuOpen, setMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState(s.title)
  const inputRef = useRef<HTMLInputElement>(null)
  const rowRef = useRef<HTMLDivElement>(null)

  // Close context menu on click outside
  useEffect(() => {
    if (!menuOpen) return
    const handler = (e: MouseEvent) => {
      if (rowRef.current && !rowRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [menuOpen])

  const label = s.messageCount > 0 ? `${s.messageCount} msg` : 'empty'
  const isChild = depth > 0
  const leftPad = 8 + depth * 14

  const startRename = useCallback(() => {
    setRenameValue(s.title)
    setRenaming(true)
    setMenuOpen(false)
    setTimeout(() => inputRef.current?.select(), 50)
  }, [s.title])

  const commitRename = useCallback(() => {
    const trimmed = renameValue.trim()
    if (trimmed && trimmed !== s.title) {
      renameSession(s.id, trimmed)
    }
    setRenaming(false)
  }, [renameValue, s.id, s.title, renameSession])

  return (
    <div
      ref={rowRef}
      className="group relative"
      onContextMenu={(e) => {
        e.preventDefault()
        setMenuOpen((v) => !v)
      }}
    >
      <button
        onClick={() => {
          if (menuOpen) {
            setMenuOpen(false)
            return
          }
          if (!isActive) onSwitch()
        }}
        style={{ paddingLeft: `${leftPad}px` }}
        className={`flex w-full items-start gap-2 rounded-md py-[7px] pr-2 text-left text-[12px] hover:bg-white/[0.04] ${
          isActive ? 'bg-white/[0.06] text-fg-0 ring-hairline' : 'text-fg-1'
        } ${
          isChild
            ? 'before:absolute before:left-[11px] before:top-0 before:h-full before:w-px before:bg-white/[0.08] before:content-[""]'
            : ''
        }`}
      >
        <span
          className={`mt-[5px] inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
            isActive ? 'bg-accent' : isChild ? 'bg-fg-3/70' : 'bg-fg-3'
          }`}
        />
        <div className="min-w-0 flex-1">
          {renaming ? (
            <input
              ref={inputRef}
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitRename()
                if (e.key === 'Escape') setRenaming(false)
              }}
              className="w-full rounded bg-black/40 px-1 py-0.5 text-[12px] text-fg-0 ring-1 ring-accent/40 focus:outline-none"
              autoFocus
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <div className="truncate">{s.title}</div>
          )}
          <div className="mt-[2px] flex items-center gap-1.5 text-[10px] text-fg-2">
            <span className="font-mono">
              {s.model.replace('claude-', '')}
            </span>
            <span>·</span>
            <span>{label}</span>
            <span>·</span>
            <span>{relativeTime(s.updatedAt)}</span>
          </div>
        </div>
        {/* Action buttons on hover */}
        <span className="ml-auto mt-1 flex shrink-0 items-center gap-0.5 opacity-0 group-hover:opacity-100">
          <span
            onClick={(e) => {
              e.stopPropagation()
              downloadSession(s.id)
            }}
            title="Download session (JSON)"
            className="rounded px-1 py-0.5 text-[11px] text-fg-3 hover:bg-white/[0.08] hover:text-fg-0"
          >
            ↓
          </span>
          <span
            onClick={(e) => {
              e.stopPropagation()
              setMenuOpen((v) => !v)
            }}
            className="rounded px-1 py-0.5 text-[11px] text-fg-3 hover:bg-white/[0.08] hover:text-fg-0"
          >
            ⋯
          </span>
        </span>
      </button>

      {/* Context menu */}
      {menuOpen && (
        <div className="absolute right-2 top-full z-30 mt-0.5 w-[140px] rounded-lg glass-strong py-1 shadow-xl ring-hairline-strong">
          <CtxBtn label="Rename" onClick={startRename} />
          <CtxBtn
            label="Download"
            onClick={() => {
              setMenuOpen(false)
              downloadSession(s.id)
            }}
          />
          {!isActive && (
            <CtxBtn
              label="Delete"
              danger
              onClick={() => {
                setMenuOpen(false)
                deleteSession(s.id)
              }}
            />
          )}
        </div>
      )}
    </div>
  )
}

function CtxBtn({
  label,
  onClick,
  danger,
}: {
  label: string
  onClick: () => void
  danger?: boolean
}) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation()
        onClick()
      }}
      className={`flex w-full items-center px-3 py-1.5 text-left text-[11px] hover:bg-white/[0.06] ${
        danger ? 'text-danger hover:bg-danger/10' : 'text-fg-1'
      }`}
    >
      {label}
    </button>
  )
}
