import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import { getEffectiveTheme, oppositeTheme } from '../lib/theme'
import { useSchedulerStore } from '../state/scheduler-store'
import { SLASH_COMMANDS } from '../lib/slash'
import { scrollToReveal, stepSelection } from '../lib/listNavigation'
import { JevWidget } from './JevGenerativeWidgets'
import { chooseWidget, parseSlots, type PaletteWidgetType } from '../lib/paletteIntent'
import type { JevPaletteDecision } from '../../shared/events'
interface PaletteItem {
  id: string
  title: string
  subtitle?: string
  group: 'Command' | 'Skill' | 'Subagent' | 'Session'
  action: () => void
}

/** Jev answers domain and sub-target independently, so a sub-target is only
 *  worth showing when it belongs to the domain that won. */
const SUB_OF: Record<string, RegExp> = {
  calendar: /^calendar_/,
  quick_control: /_toggle$|^timer_set$/,
  model_picker: /^model_/,
  token_usage: /^usage_/,
  artifact_explorer: /artifact/,
  diagnostics: /^log_errors$|^key_status$/,
}
function subTargetOf(d: JevPaletteDecision): boolean {
  return !!SUB_OF[d.domain]?.test(d.subTarget)
}

/** Breathing room kept between the selected row and the scroll viewport edge. */
const SCROLL_PAD = 8
/** Rows a PageUp/PageDown jumps by. */
const PAGE_STEP = 8

export function CommandPalette() {
  const close = useHarness((s) => s.toggleCommandPalette)
  const skills = useHarness((s) => s.skills)
  const subagents = useHarness((s) => s.subagents)
  const sessions = useHarness((s) => s.sessions)
  const openSubagent = useHarness((s) => s.openSubagent)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const toggleArtifactsBrowser = useHarness((s) => s.toggleArtifactsBrowser)
  const openScheduler = useSchedulerStore((s) => s.openDashboard)
  const schedulerJobCount = useSchedulerStore((s) => s.jobs.length)
  const openSessionPane = useHarness((s) => s.openSessionPane)
  const setDraft = useHarness((s) => s.setInputDraft)
  const burst = useHarness((s) => s.requestDemoBurst)

  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  // ── Jev generative mode ──────────────────────────────────────────
  // Two clocks. Slots (who / when / which) are parsed synchronously on
  // every keystroke, so a widget fills in as the sentence grows. Jev only
  // decides the domain, debounced, and a stale answer never overrides a
  // newer query. A widget stays mounted while the typing continues inside
  // its domain; it only yields to the list when Jev is sure the text is a
  // plain command, or when a palette row matches the text outright.
  const [decision, setDecision] = useState<JevPaletteDecision | null>(null)
  const [inferring, setInferring] = useState(false)
  const jevSeq = useRef(0)
  const widgetRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // Keyboard scrolling slides rows underneath a stationary cursor, which fires
  // mouseenter and yanks the selection back. Ignore hover until the pointer
  // actually moves again.
  const suppressHover = useRef(false)

  // Widget actions usually close the palette, so their confirmation goes
  // through the app-level toast rather than one that unmounts with us.
  const showToast = useCallback((msg: string) => {
    useHarness.getState().showToast(msg, 'ok')
  }, [])

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const trimmedQuery = query.trim()
  const slots = useMemo(() => parseSlots(trimmedQuery), [trimmedQuery])
  const items: PaletteItem[] = useMemo(() => {
    const out: PaletteItem[] = []
    out.push(
      {
        id: 'morning-room',
        title: 'Morning Room',
        subtitle: "Today's briefing — projects, decisions, and a staged plan (⌘⇧B)",
        group: 'Command',
        action: () => {
          useHarness.getState().toggleMorningRoom(true)
          close(false)
        },
      },
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
          toggleMissionDashboard(true, 'overview')
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
      {
        id: 'artifacts:browse',
        title: 'Artifacts',
        subtitle: 'Every file every session ever produced — search, read, edit, comment (⌘⇧A)',
        group: 'Command',
        action: () => {
          toggleArtifactsBrowser(true)
          close(false)
        },
      },
      {
        id: 'scheduler:open',
        title: 'Scheduled Jobs',
        subtitle: schedulerJobCount > 0
          ? `${schedulerJobCount} schedule${schedulerJobCount === 1 ? '' : 's'} · past runs · daemon status (⌘⇧S)`
          : 'Create a schedule, browse past runs, daemon status (⌘⇧S)',
        group: 'Command',
        action: () => {
          openScheduler()
          close(false)
        },
      },
      {
        id: 'appshot:capture',
        title: 'Appshot',
        subtitle: 'Capture the window behind Freyja, picture and text, into the composer',
        group: 'Command',
        action: () => {
          close(false)
          void useHarness.getState().captureAppshot('palette')
        },
      },
      {
        id: 'appearance:toggle',
        title: `Appearance: switch to ${oppositeTheme(getEffectiveTheme())} mode`,
        subtitle: 'Flip between the dark and light shells (⌘⇧L)',
        group: 'Command',
        action: () => {
          close(false)
          void useHarness.getState().setAppearanceTheme(oppositeTheme(getEffectiveTheme()))
        },
      },
      {
        id: 'appearance:system',
        title: 'Appearance: follow system',
        subtitle: 'Let macOS decide, including its sunset schedule',
        group: 'Command',
        action: () => {
          close(false)
          void useHarness.getState().setAppearanceTheme('system')
        },
      },
    )
    {
      const st = useHarness.getState()
      const current = st.sessions.find((sess) => sess.id === st.activeSessionId)
      const runtime = st.runtime ?? current?.runtime ?? 'native'
      if (current && runtime === 'native') {
        out.push({
          id: 'session:fork',
          title: 'Fork session',
          subtitle: 'Copy this session as it stands — context, sub-agents, artifacts, task board',
          group: 'Command',
          action: () => {
            void useHarness.getState().branchSessionFrom(null, `${current.title} (fork)`)
            close(false)
          },
        })
      }
    }
    for (const c of SLASH_COMMANDS.filter((command) => !command.hidden)) {
      out.push({
        id: `cmd:${c.name}`,
        title: c.name,
        subtitle: c.description,
        group: 'Command',
        action: () => {
          if (c.name === '/burst') {
            burst()
          } else if (c.name === '/schedule'
                     || c.name === '/schedules'
                     || c.name === '/jobs'
                     || c.name === '/cron') {
            // Skip the slash-prefill round-trip — these all just open
            // the modal directly.
            openScheduler()
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
          openSessionPane(s.id, 'replace').catch(() => {})
          close(false)
        },
      })
    }
    return out
  }, [skills, subagents, sessions, setDraft, close, openSubagent, burst, toggleMissionDashboard, toggleArtifactsBrowser, openSessionPane, openScheduler, schedulerJobCount])

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

  // Group items for display while keeping each row's index into `filtered`, so
  // the render pass never has to do an O(n) indexOf per row.
  const groups = useMemo(() => {
    type Group = { group: string; rows: { item: PaletteItem; index: number }[] }
    const out: Group[] = []
    const byGroup = new Map<string, Group>()
    filtered.forEach((item, index) => {
      let bucket = byGroup.get(item.group)
      if (!bucket) {
        bucket = { group: item.group, rows: [] }
        byGroup.set(item.group, bucket)
        out.push(bucket)
      }
      bucket.rows.push({ item, index })
    })
    return out
  }, [filtered])

  // A row whose title starts with the text is what the person is reaching
  // for ("swarm", "morning", "/goal"): no Jev call, no widget.
  const rowMatch = useMemo(() => {
    const q = trimmedQuery.toLowerCase()
    if (!q || q.length > 28) return false
    return items.some((i) => i.title.toLowerCase().startsWith(q))
  }, [items, trimmedQuery])

  useEffect(() => {
    const q = trimmedQuery
    if (q.length < 3 || q.startsWith('/') || rowMatch) {
      jevSeq.current += 1
      setDecision(null)
      setInferring(false)
      return
    }
    const api = window.harness as { jevClassify?: (q: string) => Promise<JevPaletteDecision> } | undefined
    if (!api?.jevClassify) return
    const seq = ++jevSeq.current
    setInferring(true)
    const timer = window.setTimeout(() => {
      api
        .jevClassify!(q)
        .then((d: JevPaletteDecision) => {
          if (seq !== jevSeq.current) return
          setDecision(d.ok ? d : null)
          setInferring(false)
        })
        .catch(() => {
          if (seq !== jevSeq.current) return
          setInferring(false)
        })
    }, 160)
    return () => window.clearTimeout(timer)
  }, [trimmedQuery, rowMatch])

  // Hysteresis: while Jev is still deciding on a longer version of the same
  // sentence, keep the widget it already chose, so the card fills in rather
  // than flickering back to the list between keystrokes.
  const lastWidget = useRef<{ type: PaletteWidgetType; query: string }>({ type: null, query: '' })
  const widgetType: PaletteWidgetType = useMemo(() => {
    if (trimmedQuery.length < 3 || rowMatch) return null
    if (decision) {
      const chosen = chooseWidget(decision.domain, decision.subTarget, decision.confidence, decision.readiness, trimmedQuery, slots)
      const stale = decision.query !== trimmedQuery
      if (chosen) return chosen
      // Jev said "plain command" for an older prefix of what is being typed:
      // the newer text may still belong to the widget already showing.
      if (stale && lastWidget.current.type && trimmedQuery.startsWith(lastWidget.current.query)) return lastWidget.current.type
      if (!stale && decision.confidence >= 0.6) return null
      return lastWidget.current.type && trimmedQuery.startsWith(lastWidget.current.query) ? lastWidget.current.type : null
    }
    if (lastWidget.current.type && trimmedQuery.startsWith(lastWidget.current.query)) return lastWidget.current.type
    return null
  }, [decision, trimmedQuery, slots, rowMatch])

  useEffect(() => {
    if (widgetType) lastWidget.current = { type: widgetType, query: decision?.query ?? trimmedQuery }
    else if (trimmedQuery.length < 3) lastWidget.current = { type: null, query: '' }
  }, [widgetType, decision, trimmedQuery])

  // A new query starts over at the top. A list that merely grew or shrank
  // underneath us (live sessions, subagents) keeps the selection, clamped.
  useEffect(() => {
    setSelected(0)
    if (listRef.current) listRef.current.scrollTop = 0
  }, [query])

  useEffect(() => {
    setSelected((i) => Math.min(i, Math.max(0, filtered.length - 1)))
  }, [filtered.length])

  // Keep the highlighted row inside the scroll viewport. Layout effect so the
  // scroll lands in the same frame as the highlight — no visible lag.
  useLayoutEffect(() => {
    const container = listRef.current
    if (!container) return
    const row = container.querySelector<HTMLElement>(`[data-palette-row="${selected}"]`)
    if (!row) return
    // Arrowing into the first row of a group should reveal that group's label
    // too, otherwise the header stays clipped above the fold. Both the row and
    // the label measure against the scroll container, which is `relative` and
    // therefore their shared offsetParent.
    const label =
      row.dataset.paletteGroupFirst === '1'
        ? (row.parentElement?.firstElementChild as HTMLElement | null)
        : null
    const next = scrollToReveal({
      scrollTop: container.scrollTop,
      viewportHeight: container.clientHeight,
      rowTop: row.offsetTop,
      rowHeight: row.offsetHeight,
      anchorTop: label ? Math.min(label.offsetTop, row.offsetTop) : undefined,
      pad: SCROLL_PAD,
      contentHeight: container.scrollHeight,
    })
    if (next != null) container.scrollTop = next
  }, [selected, filtered])

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    const next = stepSelection(e, {
      selected,
      count: filtered.length,
      pageStep: PAGE_STEP,
    })
    if (next != null) {
      e.preventDefault()
      suppressHover.current = true
      setSelected(next)
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      if (widgetType) {
        const primary = widgetRef.current?.querySelector<HTMLButtonElement>('[data-primary]:not(:disabled)')
        primary?.click()
        return
      }
      if (filtered[selected]) filtered[selected].action()
      else if (trimmedQuery.length >= 3) askFreyja()
    } else if (e.key === 'Escape') {
      if (widgetType) {
        // App.tsx closes the palette on a bubbled Esc; inside a widget the
        // first Esc only steps back to the list.
        e.preventDefault()
        e.stopPropagation()
        setQuery('')
        return
      }
      close(false)
    }
  }

  const askFreyja = () => {
    setDraft(trimmedQuery)
    close(false)
  }

  const onHoverRow = useCallback((index: number) => {
    if (suppressHover.current) return
    setSelected(index)
  }, [])

  const onPointerMove = useCallback(() => {
    suppressHover.current = false
  }, [])

  return (
    // z-[60]: above the full-screen views (z-50) and the Artifacts browser
    // (z-[55]). ⌘K is the app-wide switcher — it has to be reachable from
    // inside whatever is open, and visible when it is.
    <div className="fixed inset-0 z-[60] flex items-start justify-center pt-[14vh]">
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-[1px]"
        onClick={() => close(false)}
      />
      <div className="relative w-[620px] overflow-hidden rounded-2xl modal-opaque shadow-2xl ring-hairline-strong transition-all duration-150">
        <div className="flex items-center gap-3 px-4 py-3 hairline-b">
          <svg width="14" height="14" viewBox="0 0 14 14">
            <circle cx="6" cy="6" r="4" stroke="var(--accent)" strokeWidth="1.2" fill="none" />
            <line x1="9" y1="9" x2="12" y2="12" stroke="var(--accent)" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Search commands, skills, subagents, sessions…"
            className="flex-1 bg-transparent text-[12.5px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
          />
          {(inferring || decision) && !rowMatch && (
            <span className={`kbd text-[9.5px] ${inferring ? 'text-accent' : 'text-fg-3'}`}>
              {inferring ? 'jev…' : `${decision!.domain}${subTargetOf(decision!) ? `/${decision!.subTarget}` : ''} · ${decision!.latencyMs}ms`}
            </span>
          )}
          <kbd className="kbd">esc</kbd>
        </div>

        {widgetType ? (
          <div ref={widgetRef} key={widgetType}>
            <JevWidget
              type={widgetType}
              query={trimmedQuery}
              decision={decision ?? { ok: true, query: trimmedQuery, domain: '', subTarget: 'none', confidence: 0, readiness: 0, latencyMs: 0 }}
              slots={slots}
              onClose={() => close(false)}
              onToast={showToast}
            />
          </div>
        ) : (
          <div
            ref={listRef}
            onMouseMove={onPointerMove}
            className="relative max-h-[360px] overflow-y-auto p-1"
          >
            {filtered.length === 0 && trimmedQuery.length < 3 && (
              <div className="py-8 text-center text-[12px] italic text-fg-3">No results</div>
            )}
            {filtered.length === 0 && trimmedQuery.length >= 3 && (
              <button
                type="button"
                onClick={askFreyja}
                className="flex w-full items-start gap-3 rounded-md bg-accent/15 px-3 py-2 text-left text-[12.5px] text-fg-0"
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate">Ask Freyja: “{trimmedQuery}”</span>
                  <span className="block truncate text-[11px] text-fg-2">
                    {inferring ? 'reading the request…' : 'puts it in the composer'}
                  </span>
                </span>
                <kbd className="kbd">↵</kbd>
              </button>
            )}
            {groups.map(({ group, rows }) => (
              <div key={group}>
                <div className="px-3 pb-1 pt-3 text-[9.5px] uppercase tracking-[0.16em] text-fg-3">
                  {group}
                </div>
                {rows.map(({ item, index }, positionInGroup) => (
                  <PaletteRow
                    key={item.id}
                    item={item}
                    index={index}
                    firstInGroup={positionInGroup === 0}
                    active={index === selected}
                    onHover={onHoverRow}
                  />
                ))}
              </div>
            ))}
          </div>
        )}

        <div className="hairline-t flex items-center justify-between bg-black/35 px-4 py-2 text-[10px] text-fg-2">
          <div className="flex items-center gap-2">
            {widgetType ? (
              <>
                <kbd className="kbd">↵</kbd>
                <span>primary action</span>
                <kbd className="kbd ml-2">esc</kbd>
                <span>back to list</span>
              </>
            ) : (
              <>
                <kbd className="kbd">↑</kbd>
                <kbd className="kbd">↓</kbd>
                <span>navigate</span>
                <kbd className="kbd ml-2">⇞</kbd>
                <kbd className="kbd">⇟</kbd>
                <span>page</span>
                <kbd className="kbd ml-2">↵</kbd>
                <span>select</span>
              </>
            )}
          </div>
          <span>
            {widgetType ? (
              <span className="text-fg-3">{widgetType.replace('_', ' ')}</span>
            ) : (
              <>
                {filtered.length > 0 && (
                  <span className="mr-2 text-fg-3">
                    {selected + 1}/{filtered.length}
                  </span>
                )}
                {filtered.length} results
              </>
            )}
          </span>
        </div>
      </div>
    </div>
  )
}

/**
 * One palette row. Memoized so arrowing through a 1000+ result list only
 * re-renders the two rows whose highlight actually changed.
 */
const PaletteRow = memo(function PaletteRow({
  item,
  index,
  active,
  firstInGroup,
  onHover,
}: {
  item: PaletteItem
  index: number
  active: boolean
  firstInGroup: boolean
  onHover: (index: number) => void
}) {
  return (
    <button
      data-palette-row={index}
      data-palette-group-first={firstInGroup ? '1' : undefined}
      onClick={() => item.action()}
      onMouseEnter={() => onHover(index)}
      className={`flex w-full items-start gap-3 rounded-md px-3 py-2 text-left text-[12.5px] ${
        active ? 'bg-accent/15 text-fg-0' : 'text-fg-1 hover:bg-white/[0.03]'
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
})
