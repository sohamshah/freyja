import { createContext, memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import { renderMarkdown } from '../lib/markdown'
import { HeroWelcome } from './HeroWelcome'
import { ToolCallChip } from './ToolCallChip'
import { ParallelToolGroup } from './ParallelToolGroup'
import { SubagentCard } from './SubagentCard'
import { SubagentSwarmGrid } from './SubagentSwarmGrid'
import { ChildSessionBreadcrumb } from './ChildSessionBreadcrumb'
import { ConversationSearch } from './ConversationSearch'
import { Spinner } from '../lib/spinner'
import { highlightHtml, highlightRuns } from '../lib/searchHighlight'
import type { Message, MessagePart } from '@shared/events'

/** Current search query shared across all conversation parts. Empty
 *  string means "no active search" — no highlights are rendered. */
const SearchQueryContext = createContext<string>('')

export function Conversation() {
  const messages = useHarness((s) => s.messages)
  const thinking = useHarness((s) => s.thinking)
  const isStreaming = useHarness((s) => s.isStreaming)
  const focusedToolCallId = useHarness((s) => s.focusedToolCallId)
  const focusedToolCallSerial = useHarness((s) => s.focusedToolCallSerial)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const userScrolledUpRef = useRef(false)

  // ── In-session search ──────────────────────────────────────────
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [activeMatchIdx, setActiveMatchIdx] = useState(0)
  const [totalMatches, setTotalMatches] = useState(0)

  const closeSearch = useCallback(() => {
    setSearchOpen(false)
    setSearchQuery('')
    setActiveMatchIdx(0)
    setTotalMatches(0)
  }, [])

  // Global ⌘F / Ctrl+F — open the search bar, or re-focus + select if
  // it's already open. Esc inside the search input itself closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey
      if (mod && (e.key === 'f' || e.key === 'F')) {
        e.preventDefault()
        setSearchOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Pass 1 — after every content change, count hits, clamp the active
  // index, and keep the highlight classes in sync. This runs on every
  // streaming message update but does NOT scroll (so new content
  // arriving while the user is hunting never yanks them around).
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!searchQuery) {
      setTotalMatches(0)
      setActiveMatchIdx(0)
      return
    }
    if (!el) {
      setTotalMatches(0)
      return
    }
    const hits = el.querySelectorAll<HTMLElement>('.search-hit')
    setTotalMatches(hits.length)
    const clampedActive =
      hits.length === 0 ? 0 : Math.min(activeMatchIdx, hits.length - 1)
    if (clampedActive !== activeMatchIdx) setActiveMatchIdx(clampedActive)
    hits.forEach((h, i) => {
      h.classList.toggle('search-hit--active', i === clampedActive)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery, messages, thinking])

  // Pass 2 — when the user explicitly navigates (activeMatchIdx changes),
  // re-toggle classes and scroll the active hit into view.
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const hits = el.querySelectorAll<HTMLElement>('.search-hit')
    if (hits.length === 0) return
    hits.forEach((h, i) => {
      h.classList.toggle('search-hit--active', i === activeMatchIdx)
    })
    const target = hits[activeMatchIdx]
    if (target) {
      target.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  }, [activeMatchIdx])

  const nextMatch = useCallback(() => {
    setActiveMatchIdx((prev) =>
      totalMatches > 0 ? (prev + 1) % totalMatches : 0,
    )
  }, [totalMatches])

  const prevMatch = useCallback(() => {
    setActiveMatchIdx((prev) =>
      totalMatches > 0 ? (prev - 1 + totalMatches) % totalMatches : 0,
    )
  }, [totalMatches])

  // Track when the user manually scrolls away from the bottom so we
  // don't yank them back down mid-read. The `programmaticScroll` flag
  // prevents auto-scroll events from resetting the lock.
  const programmaticScrollRef = useRef(false)

  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const onScroll = () => {
      // Ignore scroll events caused by our own auto-scroll — they
      // would reset userScrolledUpRef and re-enable sticky scroll
      // right after the user tried to escape it.
      if (programmaticScrollRef.current) return

      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      if (distFromBottom > 40) {
        // User scrolled away from bottom — lock out auto-scroll.
        userScrolledUpRef.current = true
      } else if (distFromBottom < 10) {
        // User scrolled back to the very bottom — re-enable.
        userScrolledUpRef.current = false
      }
    }
    el.addEventListener('scroll', onScroll)
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  // Track the last message count so we can detect when the user sends
  // a new message (which should snap scroll back to the bottom).
  const prevMsgCountRef = useRef(messages.length)

  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    if (searchOpen) return

    // Did the user just send a new message? If the message count grew
    // and the latest message is from the user, reset the scroll lock
    // so the conversation snaps to show their message + the response.
    const lastMsg = messages[messages.length - 1]
    if (
      messages.length > prevMsgCountRef.current &&
      lastMsg?.role === 'user'
    ) {
      userScrolledUpRef.current = false
    }
    prevMsgCountRef.current = messages.length

    // Respect the user's scroll position — if they scrolled up to
    // read something, don't yank them back down even while streaming.
    if (userScrolledUpRef.current) return

    // Mark as programmatic so the scroll handler ignores this event.
    programmaticScrollRef.current = true
    el.scrollTop = el.scrollHeight
    requestAnimationFrame(() => {
      programmaticScrollRef.current = false
    })
  }, [messages, thinking, isStreaming, searchOpen])

  useEffect(() => {
    if (!focusedToolCallId) return
    const scroller = scrollerRef.current
    if (!scroller) return
    requestAnimationFrame(() => {
      const target = Array.from(
        scroller.querySelectorAll<HTMLElement>('[data-tool-call-id]'),
      ).find((el) => el.dataset.toolCallId === focusedToolCallId)
      if (!target) return
      userScrolledUpRef.current = true
      target.scrollIntoView({ block: 'center', behavior: 'smooth' })
    })
  }, [focusedToolCallId, focusedToolCallSerial])

  if (messages.length === 0 && !thinking) {
    return (
      <div className="relative flex min-h-0 flex-1 flex-col">
        {searchOpen && (
          <ConversationSearch
            query={searchQuery}
            onQueryChange={setSearchQuery}
            onClose={closeSearch}
            onNext={nextMatch}
            onPrev={prevMatch}
            total={totalMatches}
            activeIdx={totalMatches > 0 ? activeMatchIdx : -1}
          />
        )}
        <div ref={scrollerRef} className="flex min-h-0 flex-1 flex-col overflow-y-auto overflow-x-hidden">
          <ChildSessionBreadcrumb />
          <HeroWelcome />
        </div>
      </div>
    )
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {searchOpen && (
        <ConversationSearch
          query={searchQuery}
          onQueryChange={setSearchQuery}
          onClose={closeSearch}
          onNext={nextMatch}
          onPrev={prevMatch}
          total={totalMatches}
          activeIdx={totalMatches > 0 ? activeMatchIdx : -1}
        />
      )}
      <SearchQueryContext.Provider value={searchQuery}>
        <div ref={scrollerRef} className="flex min-h-0 flex-1 flex-col overflow-y-auto overflow-x-hidden">
          <ChildSessionBreadcrumb />
          <div className="mx-auto w-full max-w-[1200px] px-8 py-6">
            {messages.map((m) => (
              <MessageView key={m.id} message={m} />
            ))}
            {/* Thinking renders inline within message parts now */}
          </div>
        </div>
      </SearchQueryContext.Provider>
    </div>
  )
}

const MessageView = memo(function MessageView({ message }: { message: Message }) {
  const searchQuery = useContext(SearchQueryContext)
  // Is this message the one currently receiving streamed deltas? We use
  // it to decide whether to animate the "active tail" of the message
  // (e.g. a still-rendering thinking block). Once the model emits new
  // parts after a thinking block, that thinking block is finished and
  // should stop animating even if the overall turn is still running.
  const currentStreamingMessageId = useHarness((s) => s.currentStreamingMessageId)
  const isStreaming = useHarness((s) => s.isStreaming)
  const isStreamingMessage = isStreaming && currentStreamingMessageId === message.id

  if (message.role === 'user') {
    const textContent = message.parts
      .filter((p) => p.type === 'text')
      .map((p) => p.text)
      .join('')
    return (
      <div className="render-cached animate-fade-in mb-6 flex flex-col items-end gap-2">
        {message.attachments && message.attachments.length > 0 && (
          <div className="flex max-w-[76%] flex-wrap justify-end gap-1.5">
            {message.attachments.map((att) => (
              <img
                key={att.id}
                src={att.previewUrl}
                alt="attached"
                className="max-h-[180px] rounded-lg ring-1 ring-accent/20"
                loading="lazy"
                decoding="async"
              />
            ))}
          </div>
        )}
        {textContent && (
          <div className="font-prose max-w-[76%] rounded-lg bg-accent/10 px-3.5 py-2 text-[12.5px] leading-[1.55] text-fg-0 ring-1 ring-accent/20">
            <div className="selectable whitespace-pre-wrap">
              <HighlightedText text={textContent} query={searchQuery} />
            </div>
          </div>
        )}
      </div>
    )
  }

  // Group consecutive `subagent` parts so the renderer can lay them out
  // as a swarm grid when the assistant spawned multiple in parallel. A
  // standalone subagent still renders as the classic stacked card.
  const groups = useMemo(() => groupParts(message.parts), [message.parts])

  return (
    <div className={`${isStreamingMessage ? '' : 'render-cached '}animate-fade-in mb-6`}>
      <div className="mb-1.5 flex items-center gap-2 label">
        <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent" />
        assistant
      </div>
      <div className="space-y-2.5 pl-0">
        {groups.map((group, idx) => (
          <PartGroupView
            key={idx}
            group={group}
            isActiveTail={isStreamingMessage && idx === groups.length - 1}
          />
        ))}
      </div>
    </div>
  )
})

type PartGroup =
  | { kind: 'subagents'; ids: string[] }
  | { kind: 'parallel_tools'; ids: string[] }
  | { kind: 'single'; part: MessagePart }

function groupParts(parts: MessagePart[]): PartGroup[] {
  const out: PartGroup[] = []
  const toolCalls = useHarness.getState().toolCalls
  for (const part of parts) {
    if (part.type === 'subagent' && part.subagentId) {
      const last = out[out.length - 1]
      if (last && last.kind === 'subagents') {
        last.ids.push(part.subagentId)
        continue
      }
      out.push({ kind: 'subagents', ids: [part.subagentId] })
      continue
    }
    // Skip spawn tool_call parts — they sit between subagent parts
    if (part.type === 'tool_call' && part.toolCallId) {
      const tc = toolCalls[part.toolCallId]
      if (tc && SUBAGENT_SPAWN_TOOLS.has(tc.name)) {
        continue
      }
      // Group consecutive tool calls with the same groupId
      if (tc?.groupId) {
        const last = out[out.length - 1]
        if (last && last.kind === 'parallel_tools') {
          const prevTc = toolCalls[last.ids[0]]
          if (prevTc?.groupId === tc.groupId) {
            last.ids.push(part.toolCallId)
            continue
          }
        }
        out.push({ kind: 'parallel_tools', ids: [part.toolCallId] })
        continue
      }
    }
    out.push({ kind: 'single', part })
  }
  return out
}

function PartGroupView({ group, isActiveTail }: { group: PartGroup; isActiveTail: boolean }) {
  if (group.kind === 'subagents') {
    if (group.ids.length === 1) return <SubagentCard id={group.ids[0]} />
    return <SubagentSwarmGrid ids={group.ids} />
  }
  if (group.kind === 'parallel_tools') {
    return <ParallelToolGroup ids={group.ids} />
  }
  return <Part part={group.part} isActiveTail={isActiveTail} />
}

// Tool calls that spawn subagents are already fully represented by the
// swarm grid / subagent card that follows them. Rendering the raw tool
// chip too would duplicate the label/task/mode args.
const SUBAGENT_SPAWN_TOOLS = new Set([
  'sub_agent',
  'subagent',
  'spawn_subagent',
  'subagent_spawn',
])

function Part({ part, isActiveTail }: { part: MessagePart; isActiveTail: boolean }) {
  const spawnToolName = useHarness((s) => {
    if (part.type !== 'tool_call' || !part.toolCallId) return undefined
    return s.toolCalls[part.toolCallId]?.name
  })
  const searchQuery = useContext(SearchQueryContext)
  const sourceText = part.type === 'text' ? part.text ?? '' : ''
  const visibleText = useCharacterReveal(
    sourceText,
    part.type === 'text' && isActiveTail && !searchQuery,
  )
  const renderedTextHtml = useMemo(
    () => (part.type === 'text' ? renderMarkdown(visibleText) : ''),
    [part.type, visibleText],
  )

  if (part.type === 'text') {
    const html = searchQuery ? highlightHtml(renderedTextHtml, searchQuery) : renderedTextHtml
    return (
      <div
        className="md selectable"
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{ __html: html }}
      />
    )
  }
  if (part.type === 'tool_call' && part.toolCallId) {
    if (spawnToolName && SUBAGENT_SPAWN_TOOLS.has(spawnToolName)) {
      return null
    }
    return <ToolCallChip id={part.toolCallId} />
  }
  if (part.type === 'subagent' && part.subagentId) {
    return <SubagentCard id={part.subagentId} />
  }
  if (part.type === 'thinking' && part.text) {
    return <ThinkingBlock text={part.text} isActive={isActiveTail} />
  }
  if (part.type === 'system') {
    return (
      <div className="flex items-center gap-2 rounded-md bg-white/[0.025] px-2.5 py-1.5 text-[11px] text-fg-2 ring-hairline">
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" className="shrink-0">
          <circle cx="5" cy="5" r="4" fill="none" stroke="#ffcc66" strokeWidth="1" />
          <path d="M5 3 V5.5" stroke="#ffcc66" strokeWidth="1" strokeLinecap="round" />
          <circle cx="5" cy="7" r="0.5" fill="#ffcc66" />
        </svg>
        <span className="font-mono text-[10.5px] uppercase text-warn/80">{part.systemSubtype}</span>
        <span className="text-fg-1">{part.text}</span>
      </div>
    )
  }
  return null
}

function ThinkingBlock({ text, isActive }: { text: string; isActive: boolean }) {
  // `isActive` means this is the literal tail of the currently-streaming
  // message — i.e. the model is still emitting deltas for THIS thinking
  // block. Once text / tool calls appear after it, it's done and the
  // rain animation + caret stop, even if the overall turn is still going.
  const [collapsed, setCollapsed] = useState(false)
  const visibleText = useCharacterReveal(text, isActive && !collapsed)

  return (
    <div className="rounded-xl glass-raised p-3">
      <button
        onClick={() => setCollapsed((v) => !v)}
        className="mb-1.5 flex w-full items-center gap-2 text-left label"
      >
        {isActive && !collapsed && (
          <Spinner name="rain" className="text-accent" />
        )}
        <span className="font-mono text-[10px] uppercase tracking-wider text-fg-2">
          {collapsed ? '+ thinking' : 'thinking'}
        </span>
      </button>
      {!collapsed && (
        <div className="selectable font-mono text-[11px] leading-[1.65] text-fg-2 whitespace-pre-wrap max-h-[300px] overflow-y-auto">
          {visibleText}
          {isActive && <span className="caret" />}
        </div>
      )}
    </div>
  )
}

const REVEAL_CHARS_PER_SECOND = 72
const MAX_REVEAL_CHARS_PER_FRAME = 6

function useCharacterReveal(text: string, active: boolean): string {
  const [visibleText, setVisibleText] = useState(() => (active ? '' : text))
  const activeRef = useRef(active)
  const targetRef = useRef(text)
  const visibleRef = useRef(active ? '' : text)
  const rafRef = useRef<number | null>(null)
  const lastFrameRef = useRef(0)
  const carryRef = useRef(0)

  const cancelFrame = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
  }, [])

  const setVisible = useCallback((next: string) => {
    visibleRef.current = next
    setVisibleText(next)
  }, [])

  const step = useCallback(
    (now: number) => {
      rafRef.current = null
      if (!activeRef.current) return

      const target = targetRef.current
      const current = visibleRef.current
      if (current === target) return

      if (!target.startsWith(current)) {
        setVisible(target)
        return
      }

      const elapsed = lastFrameRef.current > 0 ? now - lastFrameRef.current : 16.7
      lastFrameRef.current = now
      carryRef.current += (elapsed / 1000) * REVEAL_CHARS_PER_SECOND

      const remaining = target.length - current.length
      const catchUp = remaining > 240 ? MAX_REVEAL_CHARS_PER_FRAME : remaining > 96 ? 3 : 1
      const count = Math.min(
        remaining,
        Math.max(1, Math.min(MAX_REVEAL_CHARS_PER_FRAME, Math.floor(carryRef.current), catchUp)),
      )
      carryRef.current = Math.max(0, carryRef.current - count)

      let nextIndex = current.length
      for (let i = 0; i < count && nextIndex < target.length; i++) {
        nextIndex = nextCharacterBoundary(target, nextIndex)
      }
      setVisible(target.slice(0, nextIndex))

      if (nextIndex < target.length) {
        rafRef.current = requestAnimationFrame(step)
      }
    },
    [setVisible],
  )

  const schedule = useCallback(() => {
    if (rafRef.current != null || !activeRef.current) return
    rafRef.current = requestAnimationFrame(step)
  }, [step])

  useEffect(() => {
    activeRef.current = active
    targetRef.current = text

    if (!active) {
      cancelFrame()
      lastFrameRef.current = 0
      carryRef.current = 0
      if (visibleRef.current !== text) setVisible(text)
      return
    }

    if (!text.startsWith(visibleRef.current)) {
      setVisible('')
    }
    if (visibleRef.current !== text) schedule()
  }, [active, cancelFrame, schedule, setVisible, text])

  useEffect(() => cancelFrame, [cancelFrame])

  return visibleText
}

function nextCharacterBoundary(text: string, index: number): number {
  if (index >= text.length) return text.length
  const codePoint = text.codePointAt(index)
  if (codePoint === undefined) return index + 1
  return index + (codePoint > 0xffff ? 2 : 1)
}

/**
 * Renders `text` with any search-query matches wrapped in `<mark>` spans
 * so they participate in the shared `.search-hit` CSS highlight / active
 * state flow. Falls back to a plain text node when the query is empty.
 */
function HighlightedText({ text, query }: { text: string; query: string }) {
  const runs = useMemo(() => highlightRuns(text, query), [text, query])
  if (runs.length === 1 && !runs[0].isHit) return <>{text}</>
  return (
    <>
      {runs.map((r, i) =>
        r.isHit ? (
          <mark key={i} className="search-hit">
            {r.text}
          </mark>
        ) : (
          <span key={i}>{r.text}</span>
        ),
      )}
    </>
  )
}
