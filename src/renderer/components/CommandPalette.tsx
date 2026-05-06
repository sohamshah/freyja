import { useEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import { SLASH_COMMANDS } from '../lib/slash'

interface PaletteItem {
  id: string
  title: string
  subtitle?: string
  group: 'Command' | 'Skill' | 'Subagent' | 'Session'
  action: () => void
}

export function CommandPalette() {
  const close = useHarness((s) => s.toggleCommandPalette)
  const skills = useHarness((s) => s.skills)
  const subagents = useHarness((s) => s.subagents)
  const sessions = useHarness((s) => s.sessions)
  const openSubagent = useHarness((s) => s.openSubagent)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const switchSession = useHarness((s) => s.switchSession)
  const setDraft = useHarness((s) => s.setInputDraft)
  const burst = useHarness((s) => s.requestDemoBurst)

  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const items: PaletteItem[] = useMemo(() => {
    const out: PaletteItem[] = []
    out.push(
      {
        id: 'mission:overview',
        title: 'Mission Dashboard',
        subtitle: 'Overview of session health, active agents, findings, changes, and artifacts',
        group: 'Command',
        action: () => {
          toggleMissionDashboard(true, 'overview')
          close(false)
        },
      },
      {
        id: 'mission:swarm',
        title: 'Swarm Monitor',
        subtitle: 'Agent lanes, collaboration state, and live multi-agent activity',
        group: 'Command',
        action: () => {
          toggleMissionDashboard(true, 'swarm')
          close(false)
        },
      },
      {
        id: 'mission:findings',
        title: 'Findings Board',
        subtitle: 'Message-bus findings grouped by source, topic, and reuse potential',
        group: 'Command',
        action: () => {
          toggleMissionDashboard(true, 'findings')
          close(false)
        },
      },
      {
        id: 'mission:telemetry',
        title: 'Session Telemetry',
        subtitle: 'Screenshots, media pruning, compaction, and context pressure events',
        group: 'Command',
        action: () => {
          toggleMissionDashboard(true, 'telemetry')
          close(false)
        },
      },
      {
        id: 'mission:profiles',
        title: 'Agent Profiles',
        subtitle: 'Browse built-in sub-agent profiles, tools, models, and iteration caps',
        group: 'Command',
        action: () => {
          toggleMissionDashboard(true, 'profiles')
          close(false)
        },
      },
    )
    for (const c of SLASH_COMMANDS.filter((command) => !command.hidden)) {
      out.push({
        id: `cmd:${c.name}`,
        title: c.name,
        subtitle: c.description,
        group: 'Command',
        action: () => {
          if (c.name === '/burst') {
            burst()
          } else {
            setDraft(c.name + ' ')
          }
          close(false)
        },
      })
    }
    for (const s of Object.values(skills)) {
      out.push({
        id: `skill:${s.id}`,
        title: s.name,
        subtitle: `${s.skillType} · ${s.confidence} · ${s.retrievalCount}↑ · ${s.description}`,
        group: 'Skill',
        action: () => {
          setDraft(`/skills ${s.name}`)
          close(false)
        },
      })
    }
    for (const sub of Object.values(subagents)) {
      out.push({
        id: `sub:${sub.id}`,
        title: sub.label,
        subtitle: `${sub.state} · ${sub.mode} · ${sub.task}`,
        group: 'Subagent',
        action: () => {
          openSubagent(sub.id)
          close(false)
        },
      })
    }
    for (const s of sessions) {
      out.push({
        id: `session:${s.id}`,
        title: s.title,
        subtitle: `${s.model} · ${s.workspace}`,
        group: 'Session',
        action: () => {
          switchSession(s.id).catch(() => {})
          close(false)
        },
      })
    }
    return out
  }, [skills, subagents, sessions, setDraft, close, openSubagent, burst, toggleMissionDashboard, switchSession])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    return items.filter((i) => {
      return (
        i.title.toLowerCase().includes(q) ||
        (i.subtitle ?? '').toLowerCase().includes(q) ||
        i.group.toLowerCase().includes(q)
      )
    })
  }, [items, query])

  useEffect(() => {
    setSelected(0)
  }, [filtered])

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSelected((i) => Math.min(filtered.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSelected((i) => Math.max(0, i - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      filtered[selected]?.action()
    } else if (e.key === 'Escape') {
      close(false)
    }
  }

  // Group items by section for display
  const groups = useMemo(() => {
    const g: Record<string, PaletteItem[]> = {}
    for (const item of filtered) {
      ;(g[item.group] = g[item.group] || []).push(item)
    }
    return g
  }, [filtered])

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[14vh]">
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-[1px]"
        onClick={() => close(false)}
      />
      <div className="relative w-[620px] overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong">
        <div className="flex items-center gap-3 px-4 py-3 hairline-b">
          <svg width="14" height="14" viewBox="0 0 14 14">
            <circle cx="6" cy="6" r="4" stroke="#a8d4fc" strokeWidth="1.2" fill="none" />
            <line x1="9" y1="9" x2="12" y2="12" stroke="#a8d4fc" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Search commands, skills, subagents, sessions…"
            className="flex-1 bg-transparent text-[12.5px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
          />
          <kbd className="kbd">esc</kbd>
        </div>
        <div className="max-h-[360px] overflow-y-auto p-1">
          {filtered.length === 0 && (
            <div className="py-8 text-center text-[12px] italic text-fg-3">No results</div>
          )}
          {Object.entries(groups).map(([group, items]) => (
            <div key={group}>
              <div className="px-3 pb-1 pt-3 text-[9.5px] uppercase tracking-[0.16em] text-fg-3">
                {group}
              </div>
              {items.map((item) => {
                const globalIdx = filtered.indexOf(item)
                const isActive = globalIdx === selected
                return (
                  <button
                    key={item.id}
                    onClick={() => item.action()}
                    onMouseEnter={() => setSelected(globalIdx)}
                    className={`flex w-full items-start gap-3 rounded-md px-3 py-2 text-left text-[12.5px] ${
                      isActive ? 'bg-accent/15 text-fg-0' : 'text-fg-1 hover:bg-white/[0.03]'
                    }`}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{item.title}</span>
                      {item.subtitle && (
                        <span className="block truncate text-[11px] text-fg-2">{item.subtitle}</span>
                      )}
                    </span>
                  </button>
                )
              })}
            </div>
          ))}
        </div>
        <div className="hairline-t flex items-center justify-between bg-black/35 px-4 py-2 text-[10px] text-fg-2">
          <div className="flex items-center gap-2">
            <kbd className="kbd">↑</kbd>
            <kbd className="kbd">↓</kbd>
            <span>navigate</span>
            <kbd className="kbd ml-2">↵</kbd>
            <span>select</span>
          </div>
          <span>{filtered.length} results</span>
        </div>
      </div>
    </div>
  )
}
