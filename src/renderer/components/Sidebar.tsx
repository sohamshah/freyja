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
import { TopoBackdrop } from './TopoBackdrop'

type Section = 'sessions' | 'skills' | 'subagents' | 'memory'

/** Token-prefix match: every token in `tokens` must appear as a
 *  prefix of some whitespace-separated word in `haystack`. Both
 *  sides should already be lowercase. */
function matchesAllTokens(haystack: string, tokens: string[]): boolean {
  if (tokens.length === 0) return true
  // Tokenize haystack once per call. Cheap for sidebar-sized text.
  const words = haystack.split(/[\s\W_]+/).filter(Boolean)
  for (const token of tokens) {
    let found = false
    for (const word of words) {
      if (word.startsWith(token)) {
        found = true
        break
      }
    }
    if (!found) return false
  }
  return true
}

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
  const newSession = useHarness((s) => s.newSession)
  const openSessionPane = useHarness((s) => s.openSessionPane)
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

  // Search query for sessions (titles + content). Empty string = no filter.
  const [sessionQuery, setSessionQuery] = useState('')

  // Inspector popup for a skill or memory, opened by clicking the row.
  const [inspectItem, setInspectItem] = useState<
    | { kind: 'skill'; item: Skill }
    | { kind: 'memory'; item: MemoryRecord }
    | null
  >(null)
  // Escape closes the inspector if it's open.
  useEffect(() => {
    if (!inspectItem) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setInspectItem(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [inspectItem])

  // Ancestor chain of the active session — these auto-expand so the
  // active session is always visible in the tree without the user
  // having to click open every parent.
  const activeAncestors = useMemo(() => {
    const set = new Set<string>()
    const byId = new Map(sessions.map((s) => [s.id, s]))
    let cur: SessionSnapshot | undefined = byId.get(activeSessionId)
    while (cur) {
      set.add(cur.id)
      if (!cur.parentSessionId) break
      cur = byId.get(cur.parentSessionId)
    }
    return set
  }, [sessions, activeSessionId])

  // User-toggled expansion overrides. Adds rows that aren't in the
  // active lineage but the user explicitly clicked open.
  const [expandedSessions, setExpandedSessions] = useState<Set<string>>(new Set())
  const toggleExpanded = useCallback((id: string) => {
    setExpandedSessions((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  // Searchable text per session — title + a flat dump of every message's
  // text parts. We pull from the live slice for the active session and
  // the archive for the rest. Memoized at the slice level so this only
  // re-computes when those bags actually change.
  const activeMessages = useHarness((s) => s.messages)
  const sessionArchive = useHarness((s) => s.sessionArchive)
  const sessionSearchIndex = useMemo(() => {
    const index = new Map<string, string>()
    const flatten = (parts: Array<{ type: string; text?: string }> | undefined) => {
      if (!parts) return ''
      const buf: string[] = []
      for (const p of parts) {
        if (p.type === 'text' && p.text) buf.push(p.text)
      }
      return buf.join(' ')
    }
    for (const s of sessions) {
      const messages =
        s.id === activeSessionId ? activeMessages : sessionArchive[s.id]?.messages
      const body = messages ? messages.map((m) => flatten(m.parts)).join(' ') : ''
      index.set(s.id, `${s.title}\n${body}`.toLowerCase())
    }
    return index
  }, [sessions, activeSessionId, activeMessages, sessionArchive])

  const searchTokens = useMemo(() => {
    const trimmed = sessionQuery.trim().toLowerCase()
    if (!trimmed) return []
    return trimmed.split(/\s+/).filter(Boolean)
  }, [sessionQuery])

  // For each session, decide whether it matches the search query.
  // Prefix match: every token in the query must appear as a prefix of
  // some whitespace-separated word in the searchable text.
  const matchedSessionIds = useMemo(() => {
    if (searchTokens.length === 0) return null
    const matched = new Set<string>()
    for (const s of sessions) {
      const text = sessionSearchIndex.get(s.id) ?? ''
      if (matchesAllTokens(text, searchTokens)) matched.add(s.id)
    }
    return matched
  }, [sessions, sessionSearchIndex, searchTokens])

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
  //
  // Visibility rule: a sub-session is only rendered when its parent
  // chain is expanded. A parent is expanded when (a) it's part of the
  // active session's lineage, (b) the user manually toggled it open,
  // or (c) a search is active and we're showing matching descendants
  // in context. This keeps the rail from drowning in background sub-
  // agent rows for parents the user isn't looking at.
  const sessionTree: Array<SessionSnapshot & { depth: number; hasChildren: boolean; isExpanded: boolean }> = useMemo(() => {
    const ids = new Set(sessions.map((s) => s.id))
    const byParent = new Map<string | null, SessionSnapshot[]>()
    for (const s of sessions) {
      const pid =
        s.parentSessionId && ids.has(s.parentSessionId) ? s.parentSessionId : null
      if (!byParent.has(pid)) byParent.set(pid, [])
      byParent.get(pid)!.push(s)
    }

    // When a search is active, build a set of session ids that should
    // be expanded so a descendant match remains reachable: every
    // ancestor of every matched session.
    const searchExpand = new Set<string>()
    if (matchedSessionIds) {
      const byId = new Map(sessions.map((s) => [s.id, s]))
      for (const matchId of matchedSessionIds) {
        let cur: SessionSnapshot | undefined = byId.get(matchId)
        while (cur) {
          searchExpand.add(cur.id)
          if (!cur.parentSessionId) break
          cur = byId.get(cur.parentSessionId)
        }
      }
    }

    const out: Array<SessionSnapshot & { depth: number; hasChildren: boolean; isExpanded: boolean }> = []
    const walk = (parentId: string | null, depth: number) => {
      const kids = byParent.get(parentId) ?? []
      const sorted = [...kids].sort((a, b) =>
        depth === 0
          ? b.createdAt - a.createdAt
          : a.createdAt - b.createdAt,
      )
      for (const s of sorted) {
        const hasChildren = (byParent.get(s.id) ?? []).length > 0
        const isExpanded =
          hasChildren &&
          (activeAncestors.has(s.id)
            || expandedSessions.has(s.id)
            || searchExpand.has(s.id))
        if (matchedSessionIds && !searchExpand.has(s.id)) {
          // Filter out non-matching branches entirely when searching.
          continue
        }
        out.push({ ...s, depth, hasChildren, isExpanded })
        if (isExpanded) {
          walk(s.id, depth + 1)
        }
      }
    }
    walk(null, 0)
    return out
  }, [sessions, activeAncestors, expandedSessions, matchedSessionIds])

  const orderedSubagents: SubagentRecord[] = useMemo(() => {
    return subagentOrder.map((id) => subagents[id]).filter(Boolean) as SubagentRecord[]
  }, [subagentOrder, subagents])

  return (
    <aside className="glass glass-panel relative isolate flex w-[256px] shrink-0 flex-col overflow-hidden rounded-[18px]">
      {/* Ambient topographic backdrop — paired-peak height field with
          marching-squares contouring, same vocabulary as the logo.
          `-z-10` plus the parent's `isolate` keeps it behind every
          static sibling without us having to z-index them all. */}
      <TopoBackdrop
        seed={7}
        className="pointer-events-none absolute inset-0 -z-10"
      />
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
        <div className="px-3 pt-3 pb-2">
          <SessionSearch value={sessionQuery} onChange={setSessionQuery} />
        </div>
        <Section
          title="sessions"
          count={matchedSessionIds ? matchedSessionIds.size : sessions.length}
          open={open.sessions}
          onToggle={() => setOpen((p) => ({ ...p, sessions: !p.sessions }))}
        >
          {sessionTree.length === 0 && sessionQuery.trim().length > 0 && (
            <div className="px-2 py-2 text-[11px] italic text-fg-2">
              No sessions match "{sessionQuery.trim()}".
            </div>
          )}
          {sessionTree.map((s) => (
            <SessionRow
              key={s.id}
              session={s}
              depth={s.depth}
              isActive={s.id === activeSessionId}
              hasChildren={s.hasChildren}
              isExpanded={s.isExpanded}
              onToggleExpand={() => toggleExpanded(s.id)}
              onOpen={(mode) => openSessionPane(s.id, mode)}
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
                onClick={(event) => {
                  openSessionPane(
                    child.id,
                    event.metaKey || event.ctrlKey ? 'split' : 'replace',
                  )
                }}
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
                <span
                  onClick={(event) => {
                    event.stopPropagation()
                    openSessionPane(child.id, 'split')
                  }}
                  className="ml-1 self-center rounded px-1.5 py-[2px] font-mono text-[9px] uppercase text-fg-3 ring-hairline opacity-0 hover:bg-white/[0.06] hover:text-fg-0 group-hover:opacity-100"
                  title="Open in split pane"
                >
                  split
                </span>
                <span className="self-center font-mono text-[9px] text-fg-3">
                  open →
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
            <SkillRow
              key={skill.id}
              skill={skill}
              onSelect={() => setInspectItem({ kind: 'skill', item: skill })}
            />
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
            <MemoryRow
              key={memory.id}
              memory={memory}
              onSelect={() => setInspectItem({ kind: 'memory', item: memory })}
            />
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
      {inspectItem && (
        <InspectorPopup
          item={inspectItem}
          onClose={() => setInspectItem(null)}
        />
      )}
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

function SessionSearch({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="relative">
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="search sessions"
        spellCheck={false}
        className="w-full rounded-md bg-black/30 px-3 py-1.5 pl-7 pr-7 text-[12px] text-fg-0 placeholder:text-fg-3 ring-1 ring-white/[0.07] focus:outline-none focus:ring-accent/40"
      />
      <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 font-mono text-[11px] text-fg-3">
        /
      </span>
      {value.length > 0 && (
        <button
          type="button"
          onClick={() => onChange('')}
          title="Clear search"
          className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded px-1 text-[12px] text-fg-3 hover:bg-white/[0.05] hover:text-fg-0"
        >
          ×
        </button>
      )}
    </div>
  )
}

function SkillRow({ skill, onSelect }: { skill: Skill; onSelect?: () => void }) {
  const totalSignals = skill.successSignals + skill.failureSignals
  const successRate = totalSignals > 0
    ? Math.round((skill.successSignals / totalSignals) * 100)
    : null
  const status = skill.status ?? 'available'

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`group flex w-full items-start gap-2 rounded-md px-2 py-[6px] text-left hover:bg-white/[0.04] ${
        status === 'loaded' ? 'bg-accent/[0.06]' : ''
      }`}
      title={`Open ${skill.name}`}
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
    </button>
  )
}

function MemoryRow({ memory, onSelect }: { memory: MemoryRecord; onSelect?: () => void }) {
  const text = memory.summary || memory.text
  return (
    <button
      type="button"
      onClick={onSelect}
      className="group flex w-full items-start gap-2 rounded-md px-2 py-[6px] text-left hover:bg-white/[0.04]"
      title={`Open ${memory.kind} memory`}
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
    </button>
  )
}

function InspectorPopup({
  item,
  onClose,
}: {
  item:
    | { kind: 'skill'; item: Skill }
    | { kind: 'memory'; item: MemoryRecord }
  onClose: () => void
}) {
  // Click-outside closes the popup.
  const cardRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (cardRef.current && !cardRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  const isSkill = item.kind === 'skill'
  const title = isSkill ? item.item.name : `${item.item.kind} memory`
  const kindTag = isSkill ? item.item.skillType : item.item.kind
  const scopeTag = isSkill ? (item.item.scope ?? 'project') : item.item.scope
  const body = isSkill ? item.item.description : item.item.text
  const summary = isSkill ? null : item.item.summary
  const tags = isSkill ? item.item.tags : item.item.tags
  const triggers = isSkill ? item.item.triggers : []
  const confidence = isSkill
    ? item.item.confidence
    : item.item.confidence ?? null
  const path = isSkill ? item.item.path : item.item.path
  const source = isSkill ? null : item.item.source
  const status = isSkill ? (item.item.status ?? 'available') : null
  const createdAt = !isSkill ? item.item.createdAt : null
  const updatedAt = !isSkill ? item.item.updatedAt : null
  // Skill activity stats
  const retrievalCount = isSkill ? item.item.retrievalCount : 0
  const loadCount = isSkill ? (item.item.loadCount ?? 0) : 0
  const successSignals = isSkill ? item.item.successSignals : 0
  const failureSignals = isSkill ? item.item.failureSignals : 0
  const totalSignals = successSignals + failureSignals
  const successRate = totalSignals > 0
    ? Math.round((successSignals / totalSignals) * 100)
    : null

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/55 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
    >
      <div
        ref={cardRef}
        className="relative flex max-h-[78vh] w-[560px] max-w-[88vw] flex-col overflow-hidden rounded-2xl glass-strong ring-hairline shadow-2xl"
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-3 px-5 pt-5 pb-3 hairline-b">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 label text-fg-3">
              <span>{isSkill ? 'skill' : 'memory'}</span>
              {kindTag && (
                <>
                  <span>·</span>
                  <span className="font-mono">{kindTag}</span>
                </>
              )}
              {scopeTag && (
                <>
                  <span>·</span>
                  <span className="font-mono">{scopeTag}</span>
                </>
              )}
              {status && status !== 'available' && (
                <>
                  <span>·</span>
                  <span className="font-mono">{status}</span>
                </>
              )}
              {confidence && (
                <>
                  <span>·</span>
                  <span className="font-mono">{confidence}</span>
                </>
              )}
            </div>
            <h2 className="mt-2 truncate text-[18px] leading-snug text-fg-0">
              {title}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            title="Close (Esc)"
            className="rounded-md bg-white/[0.04] px-2 py-1 font-mono text-[11px] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
          >
            ×
          </button>
        </div>

        {/* Body — scrollable */}
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4 text-[12.5px] leading-[1.55] text-fg-1">
          {summary && (
            <section>
              <div className="label mb-1 text-[9px] text-fg-3">summary</div>
              <p className="text-fg-0">{summary}</p>
            </section>
          )}
          {body && (
            <section>
              <div className="label mb-1 text-[9px] text-fg-3">
                {isSkill ? 'description' : 'body'}
              </div>
              <p className="selectable whitespace-pre-wrap text-fg-0">{body}</p>
            </section>
          )}
          {tags && tags.length > 0 && (
            <section>
              <div className="label mb-1 text-[9px] text-fg-3">tags</div>
              <div className="flex flex-wrap gap-1.5">
                {tags.map((tag) => (
                  <span
                    key={tag}
                    className="rounded-full bg-white/[0.04] px-2 py-[2px] font-mono text-[10px] text-fg-1 ring-hairline"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            </section>
          )}
          {triggers && triggers.length > 0 && (
            <section>
              <div className="label mb-1 text-[9px] text-fg-3">triggers</div>
              <div className="flex flex-wrap gap-1.5">
                {triggers.map((t) => (
                  <span
                    key={t}
                    className="rounded-full bg-accent/[0.08] px-2 py-[2px] font-mono text-[10px] text-accent ring-1 ring-accent/20"
                  >
                    {t}
                  </span>
                ))}
              </div>
            </section>
          )}
          {isSkill && (
            <section>
              <div className="label mb-2 text-[9px] text-fg-3">activity</div>
              <div className="grid grid-cols-2 gap-2 text-[11.5px]">
                <Stat label="retrievals" value={String(retrievalCount)} />
                <Stat label="loads" value={String(loadCount)} />
                <Stat
                  label="signals"
                  value={`${successSignals} / ${failureSignals}`}
                  tone={successSignals >= failureSignals ? 'ok' : 'warn'}
                />
                <Stat
                  label="success rate"
                  value={successRate !== null ? `${successRate}%` : '—'}
                />
              </div>
            </section>
          )}
          {(path || source || createdAt || updatedAt) && (
            <section>
              <div className="label mb-1 text-[9px] text-fg-3">metadata</div>
              <dl className="grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-1 text-[11.5px]">
                {path && (
                  <>
                    <dt className="text-fg-3">path</dt>
                    <dd className="truncate font-mono text-fg-1" title={path}>
                      {path}
                    </dd>
                  </>
                )}
                {source && (
                  <>
                    <dt className="text-fg-3">source</dt>
                    <dd className="text-fg-1">{source}</dd>
                  </>
                )}
                {createdAt && (
                  <>
                    <dt className="text-fg-3">created</dt>
                    <dd className="text-fg-1">{relativeTime(createdAt)}</dd>
                  </>
                )}
                {updatedAt && (
                  <>
                    <dt className="text-fg-3">updated</dt>
                    <dd className="text-fg-1">{relativeTime(updatedAt)}</dd>
                  </>
                )}
              </dl>
            </section>
          )}
        </div>

        <div className="hairline-t px-5 py-3 text-[10px] text-fg-3">
          <span>
            Press <kbd className="kbd">Esc</kbd> to close.
          </span>
        </div>
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'ok' | 'warn'
}) {
  const toneClass =
    tone === 'ok' ? 'text-ok' : tone === 'warn' ? 'text-warn' : 'text-fg-0'
  return (
    <div className="rounded-md bg-white/[0.025] p-2 ring-hairline">
      <div className="label text-[8.5px] text-fg-3">{label}</div>
      <div className={`mt-0.5 font-mono text-[12px] ${toneClass}`}>{value}</div>
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
  hasChildren,
  isExpanded,
  onToggleExpand,
  onOpen,
}: {
  session: SessionSnapshot & { depth: number }
  depth: number
  isActive: boolean
  hasChildren: boolean
  isExpanded: boolean
  onToggleExpand: () => void
  onOpen: (mode: 'replace' | 'split') => void
}) {
  const renameSession = useHarness((st) => st.renameSession)
  const deleteSession = useHarness((st) => st.deleteSession)
  const downloadSession = useHarness((st) => st.downloadSession)
  const allSessions = useHarness((st) => st.sessions)
  const [menuOpen, setMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState(s.title)
  const inputRef = useRef<HTMLInputElement>(null)
  const rowRef = useRef<HTMLDivElement>(null)

  // Count every descendant subagent under this session so the Delete
  // entry can warn the user that the cascade reaches further than the
  // single row they right-clicked.
  const subagentCount = useMemo(() => {
    let count = 0
    const queue: string[] = [s.id]
    const seen = new Set<string>([s.id])
    while (queue.length > 0) {
      const cur = queue.shift()!
      for (const other of allSessions) {
        if (other.parentSessionId === cur && !seen.has(other.id)) {
          seen.add(other.id)
          count += 1
          queue.push(other.id)
        }
      }
    }
    return count
  }, [allSessions, s.id])

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
        onClick={(event) => {
          if (menuOpen) {
            setMenuOpen(false)
            return
          }
          if (!isActive) onOpen(event.metaKey || event.ctrlKey ? 'split' : 'replace')
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
        {hasChildren ? (
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation()
              onToggleExpand()
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                e.stopPropagation()
                onToggleExpand()
              }
            }}
            title={isExpanded ? 'Collapse sub-sessions' : 'Expand sub-sessions'}
            className="mt-[6px] flex h-3 w-3 shrink-0 items-center justify-center text-fg-3 hover:text-fg-0"
            style={{
              transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
              transition: 'transform 120ms',
            }}
          >
            <svg viewBox="0 0 10 10" width="6" height="6">
              <path d="M3 2 L7 5 L3 8 Z" fill="currentColor" />
            </svg>
          </span>
        ) : (
          <span className="mt-[6px] inline-block h-3 w-3 shrink-0" />
        )}
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
              onOpen('split')
            }}
            title="Open in split pane"
            className="rounded px-1 py-0.5 text-[11px] text-fg-3 hover:bg-white/[0.08] hover:text-fg-0"
          >
            ⇱
          </span>
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
          <CtxBtn
            label="Open here"
            onClick={() => {
              setMenuOpen(false)
              onOpen('replace')
            }}
          />
          <CtxBtn
            label="Open split"
            onClick={() => {
              setMenuOpen(false)
              onOpen('split')
            }}
          />
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
              label={
                subagentCount > 0
                  ? `Delete (+${subagentCount} subagent${subagentCount === 1 ? '' : 's'})`
                  : 'Delete'
              }
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
