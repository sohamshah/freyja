import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import { useSchedulerStore } from '../state/scheduler-store'
import { scrollToReveal, stepSelection } from '../lib/listNavigation'
import { buildPaletteItems, type PaletteItem } from '../lib/paletteItems'
import { JevWidget } from './JevGenerativeWidgets'
import type { Handoff } from './palette/chrome'
import { DOMAIN_LABEL, chooseWidget, parseSlots, widgetForDomain, type PaletteSlots, type PaletteWidgetType } from '../lib/paletteIntent'
import { openExternal, readClipboard } from '../lib/paletteData'
import type { JevPaletteDecision, PaletteClipboard } from '../../shared/events'
interface StackEntry extends Handoff {
  queryBefore: string
}

/** Slots a card was handed keep filling the gaps the typed text leaves. */
function mergeSlots(parsed: PaletteSlots, over?: Partial<PaletteSlots>): PaletteSlots {
  if (!over) return parsed
  const out = { ...parsed } as unknown as Record<string, unknown>
  for (const [k, v] of Object.entries(over)) {
    const cur = out[k]
    if (Array.isArray(v)) {
      if (!Array.isArray(cur) || cur.length === 0) out[k] = v
    } else if (cur === undefined || cur === null || cur === '') {
      out[k] = v
    }
  }
  return out as unknown as PaletteSlots
}

interface Candidate {
  domain: string
  p: number
  widget: Exclude<PaletteWidgetType, null>
}

/** Keeps items around for a beat after they drop out so they can fade. */
function useLeaving<T extends { domain: string; p: number }>(items: T[], ms = 220): Array<T & { leaving: boolean }> {
  const [shown, setShown] = useState<Array<T & { leaving: boolean }>>([])
  useEffect(() => {
    setShown((prev) => {
      const live = new Set(items.map((i) => i.domain))
      const kept = prev.filter((x) => live.has(x.domain) || !x.leaving).map((x) => (live.has(x.domain) ? { ...items.find((i) => i.domain === x.domain)!, leaving: false } : { ...x, leaving: true }))
      for (const i of items) if (!kept.some((k) => k.domain === i.domain)) kept.push({ ...i, leaving: false })
      return kept
    })
    const t = window.setTimeout(() => setShown((prev) => prev.filter((x) => !x.leaving)), ms)
    return () => window.clearTimeout(t)
  }, [items.map((i) => `${i.domain}:${i.p.toFixed(2)}`).join('|')])
  return shown
}

function clipboardItems(clip: PaletteClipboard, push: (h: Handoff) => void, say: (text: string) => void, draft: (text: string) => void, toast: (m: string) => void): PaletteItem[] {
  const t = clip.text.trim()
  const preview = t.replace(/\s+/g, ' ').slice(0, 90) + (t.length > 90 ? '…' : '')
  const fence = (lang = '') => `\n\`\`\`${lang}\n${t.slice(0, 6000)}\n\`\`\``
  const out: PaletteItem[] = []
  const add = (id: string, title: string, action: () => void) => out.push({ id: `clip:${id}`, title, subtitle: preview, group: 'Clipboard', action })
  switch (clip.kind) {
    case 'url':
      add('summarize', 'Summarize this page', () => say(`Summarize ${t} and pull out anything that matters to me.`))
      add('open', 'Open link', () => openExternal(t))
      break
    case 'stack':
      add('explain', 'Explain this error', () => say(`Explain this error and the most likely fix:${fence()}`))
      break
    case 'json':
      add('pretty', 'Pretty-print JSON back to the clipboard', () => {
        try {
          void navigator.clipboard.writeText(JSON.stringify(JSON.parse(t), null, 2))
          toast('Formatted JSON copied')
        } catch {
          toast('Clipboard JSON did not parse')
        }
      })
      add('explain', 'Explain this JSON', () => say(`What is this JSON and is anything off about it?${fence('json')}`))
      break
    case 'path':
      add('preview', 'Preview this file', () => push({ type: 'artifact_preview', label: 'Clipboard', context: { path: t.replace(/^~/, '') } }))
      break
    case 'diff':
      add('review', 'Review this diff', () => say(`Review this diff for bugs, regressions and missing tests:${fence('diff')}`))
      break
    case 'code':
      add('explain', 'Explain this code', () => say(`Explain what this code does and flag anything risky:${fence()}`))
      add('review', 'Review this code', () => say(`Review this code critically:${fence()}`))
      break
    case 'text':
      if (t.length >= 40) {
        add('ask', 'Ask Freyja about the clipboard', () => draft(t))
        add('summarize', 'Summarize the clipboard', () => say(`Summarize this:${fence()}`))
      }
      break
  }
  return out
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
  const schedulerJobCount = useSchedulerStore((s) => s.jobs.length)
  const setDraft = useHarness((s) => s.setInputDraft)

  const [query, setQuery] = useState(() => useHarness.getState().commandPaletteQuery ?? '')
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
  // Cards pushed on top of the typed one (agenda row → reschedule). The
  // top card renders until Esc pops it; typing refines its slots.
  const [stack, setStack] = useState<StackEntry[]>([])
  // A secondary candidate the person promoted by clicking its strip.
  const [pinnedDomain, setPinnedDomain] = useState<string | null>(null)
  const [clip, setClip] = useState<PaletteClipboard | null>(null)
  const jevSeq = useRef(0)
  const widgetRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // Keyboard scrolling slides rows underneath a stationary cursor, which fires
  // mouseenter and yanks the selection back. Ignore hover until the pointer
  // actually moves again.
  const suppressHover = useRef(false)
  // The island hands a sentence over ("open in Freyja" on a card it cannot
  // show): the palette takes it as if it had been typed here, on open and
  // again while it is already open.
  const handed = useHarness((s) => s.commandPaletteQuery)
  useEffect(() => {
    if (handed == null) return
    setQuery(handed)
    setStack([])
    setPinnedDomain(null)
    useHarness.setState({ commandPaletteQuery: null })
    inputRef.current?.focus()
  }, [handed])

  // Widget actions usually close the palette, so their confirmation goes
  // through the app-level toast rather than one that unmounts with us.
  const showToast = useCallback((msg: string) => {
    useHarness.getState().showToast(msg, 'ok')
  }, [])

  useEffect(() => {
    inputRef.current?.focus()
    // Read once per open: the empty palette proposes actions for what is
    // already on the clipboard, no typing needed.
    void readClipboard().then((c) => c && c.kind !== 'empty' && setClip(c))
  }, [])

  const handoff = useCallback(
    (next: Handoff) => {
      setStack((st) => [...st, { ...next, queryBefore: query }])
      setPinnedDomain(null)
      setQuery('')
      inputRef.current?.focus()
    },
    [query],
  )
  const say = useCallback(
    (text: string) => {
      void useHarness.getState().sendMessage(text)
      close(false)
    },
    [close],
  )

  const trimmedQuery = query.trim()
  const slots = useMemo(() => parseSlots(trimmedQuery), [trimmedQuery])
  const items: PaletteItem[] = useMemo(() => {
    const out: PaletteItem[] = []
    if (clip) out.push(...clipboardItems(clip, handoff, say, (t) => { setDraft(t); close(false) }, showToast))
    out.push(...buildPaletteItems(() => close(false)))
    return out
    // The rows read the store at build time; these are the slices whose change should rebuild them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skills, subagents, sessions, schedulerJobCount, setDraft, close, clip, handoff, say, showToast])

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
    if (stack.length > 0) return
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
  }, [trimmedQuery, rowMatch, stack.length])

  useEffect(() => {
    if (trimmedQuery.length < 3) setPinnedDomain(null)
  }, [trimmedQuery])

  // Hysteresis: while Jev is still deciding on a longer version of the same
  // sentence, keep the widget it already chose, so the card fills in rather
  // than flickering back to the list between keystrokes.
  const lastWidget = useRef<{ type: PaletteWidgetType; query: string }>({ type: null, query: '' })
  const top = stack[stack.length - 1]
  const widgetType: PaletteWidgetType = useMemo(() => {
    if (top) return top.type
    if (pinnedDomain && trimmedQuery.length >= 3) return widgetForDomain(pinnedDomain, trimmedQuery, slots)
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
  }, [decision, trimmedQuery, slots, rowMatch, top, pinnedDomain])

  const effSlots = useMemo(() => mergeSlots(slots, top?.slots), [slots, top])

  // Secondary candidates from Jev's domain distribution. They show while
  // the leader is not clearly ahead and prune away as certainty grows;
  // clicking one promotes it.
  const candidates: Candidate[] = useMemo(() => {
    if (top || !decision?.probabilities || trimmedQuery.length < 3 || rowMatch) return []
    const probs = decision.probabilities
    // The card on screen may come from a local tiebreak rather than Jev's
    // top domain; whichever domain it represents is the leader.
    const shownDomain = widgetType ? (Object.keys(probs).find((d) => widgetForDomain(d, trimmedQuery, slots) === widgetType) ?? decision.domain) : null
    const leader = pinnedDomain ?? shownDomain
    const leadP = leader ? (probs[leader] ?? decision.confidence) : 0
    return Object.entries(probs)
      .filter(([d]) => d !== 'palette_command' && d !== leader)
      .map(([domain, p]) => ({ domain, p, widget: widgetForDomain(domain, trimmedQuery, slots) }))
      .filter((c): c is Candidate => !!c.widget && c.p >= 0.12 && leadP - c.p < 0.55)
      .sort((a, b) => b.p - a.p)
      .slice(0, 3)
  }, [decision, trimmedQuery, slots, rowMatch, top, pinnedDomain, widgetType])
  const strips = useLeaving(candidates)

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
      if (top) {
        e.preventDefault()
        e.stopPropagation()
        setStack((st) => st.slice(0, -1))
        setQuery(top.queryBefore)
        return
      }
      if (widgetType) {
        // App.tsx closes the palette on a bubbled Esc; inside a widget the
        // first Esc only steps back to the list.
        e.preventDefault()
        e.stopPropagation()
        setPinnedDomain(null)
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
          {(inferring || decision) && !rowMatch && !top && (
            <span className={`kbd text-[9.5px] ${inferring ? 'text-accent' : 'text-fg-3'}`}>
              {inferring
                ? 'jev…'
                : `${pinnedDomain ?? decision!.domain}${!pinnedDomain && subTargetOf(decision!) ? `/${decision!.subTarget}` : ''} · ${Math.round(
                    ((pinnedDomain ? decision!.probabilities?.[pinnedDomain] : decision!.probabilities?.[decision!.domain]) ?? decision!.confidence) * 100,
                  )}% · ${decision!.latencyMs}ms`}
            </span>
          )}
          <kbd className="kbd">esc</kbd>
        </div>

        {widgetType ? (
          <div ref={widgetRef} key={`${widgetType}:${stack.length}`}>
            <JevWidget
              type={widgetType}
              query={trimmedQuery}
              decision={decision ?? { ok: true, query: trimmedQuery, domain: '', subTarget: 'none', confidence: 0, readiness: 0, latencyMs: 0 }}
              slots={effSlots}
              context={top?.context}
              crumbs={stack.map((e) => e.label)}
              onClose={() => close(false)}
              onToast={showToast}
              handoff={handoff}
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

        {strips.length > 0 && (
          <div className={`flex flex-wrap items-center gap-1.5 px-3.5 pb-2.5 font-mono text-[10px] text-fg-3 ${widgetType ? '' : 'hairline-t pt-2.5'}`}>
            <span className="text-[9px] uppercase tracking-[0.16em]">{widgetType ? 'also' : 'jev sees'}</span>
            {strips.map((c) => (
              <button
                key={c.domain}
                type="button"
                onClick={() => {
                  setPinnedDomain(c.domain)
                  inputRef.current?.focus()
                }}
                className={`flex items-center gap-1.5 rounded-md px-2 py-0.5 ring-hairline transition-all duration-200 hover:bg-white/[0.05] hover:text-fg-0 ${
                  c.leaving ? 'scale-95 opacity-0' : 'opacity-100'
                }`}
              >
                <span className="text-fg-2">{DOMAIN_LABEL[c.domain] ?? c.domain}</span>
                <span className="h-1 w-10 overflow-hidden rounded-full bg-fg-4/60">
                  <span className="block h-full bg-accent/70 transition-all duration-300" style={{ width: `${Math.round(c.p * 100)}%` }} />
                </span>
                <span className="tabular-nums">{Math.round(c.p * 100)}%</span>
              </button>
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
                <span>{top ? `back to ${stack.length > 1 ? stack[stack.length - 2].label.toLowerCase() : 'previous card'}` : 'back to list'}</span>
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
