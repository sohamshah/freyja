import { createContext, memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useHarness, type SystemEventRecord } from '../state/store'
import { renderMarkdown } from '../lib/markdown'
import { HeroWelcome } from './HeroWelcome'
import { ToolCallChip } from './ToolCallChip'
import { Widget } from './Widget'
import { ParallelToolGroup } from './ParallelToolGroup'
import { SubagentCard } from './SubagentCard'
import { SubagentSwarmGrid } from './SubagentSwarmGrid'
import { ChildSessionBreadcrumb } from './ChildSessionBreadcrumb'
import { ConversationSearch } from './ConversationSearch'
import { Spinner } from '../lib/spinner'
import { highlightHtml, highlightRuns } from '../lib/searchHighlight'
import { MessageContextMenu, type MessageMenuAction } from './MessageContextMenu'
import { BranchSessionDialog } from './BranchSessionDialog'
import { CalibrationCard } from './shared/CalibrationCard'
import {
  StructuredJsonView,
  tryParseCompleteJson,
} from './shared/StructuredJson'
import type { CalibrationStatus, JudgeRules } from './shared/types'
import type { Message, MessagePart } from '@shared/events'

/** Current search query shared across all conversation parts. Empty
 *  string means "no active search" — no highlights are rendered. */
const SearchQueryContext = createContext<string>('')

/** Snapshot map of `card_NNN` -> { status, title, assignee } so the
 *  Part renderer can decorate card mentions in parent prose without
 *  re-deriving from system events on every text part. */
interface KanbanCardSnapshot {
  status: string
  title: string
  assignee: string
}
const KanbanCardLookupContext = createContext<Map<string, KanbanCardSnapshot>>(
  new Map(),
)

/** Lookup table from system-event id -> SystemEventRecord so inline
 *  parts (e.g. `goal_judge` verdict cards) can hydrate their full
 *  payload at render time without duplicating verdict data into the
 *  message-part shape. Populated by ConversationStream. */
const SystemEventLookupContext = createContext<Map<string, SystemEventRecord>>(
  new Map(),
)

interface MessageActionsValue {
  openMenu: (e: React.MouseEvent, message: Message) => void
  editingId: string | null
  beginEdit: (messageId: string) => void
  saveEdit: (messageId: string, text: string) => void
  cancelEdit: () => void
}
const MessageActionsContext = createContext<MessageActionsValue | null>(null)

export function Conversation() {
  const messages = useHarness((s) => s.messages)
  const systemEvents = useHarness((s) => s.systemEvents)
  const systemPrompt = useHarness((s) => s.systemPrompt)
  const coordinationStrategy = useHarness((s) => s.coordinationStrategy)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const thinking = useHarness((s) => s.thinking)
  const isStreaming = useHarness((s) => s.isStreaming)
  const focusedToolCallId = useHarness((s) => s.focusedToolCallId)
  const focusedToolCallSerial = useHarness((s) => s.focusedToolCallSerial)
  const editUserMessage = useHarness((s) => s.editUserMessage)
  const rerunUserMessage = useHarness((s) => s.rerunUserMessage)
  const deleteMessagesFrom = useHarness((s) => s.deleteMessagesFrom)
  const toggleEntryPin = useHarness((s) => s.toggleEntryPin)
  const branchSessionFrom = useHarness((s) => s.branchSessionFrom)
  const scrollerRef = useRef<HTMLDivElement>(null)
  // Scroll lock + "new messages" tracking. The user can scroll up to
  // read while the agent streams; the auto-scroll effect stops forcing
  // the viewport to the bottom and we surface a floating jump button.
  // Lock flips immediately on `wheel` / `touchmove` (synchronous) so a
  // streaming tick that arrives in the same frame can't yank the
  // viewport down before the user's intent has registered — that was
  // the bug the old `programmaticScrollRef` + rAF dance was trying to
  // solve, but the rAF window itself was swallowing fast user input.
  const scrolledUpRef = useRef(false)
  const [scrolledUp, setScrolledUp] = useState(false)
  const [unreadCount, setUnreadCount] = useState(0)
  // Message count at the moment the user scrolled up. While they stay
  // scrolled up, unread = messages.length - this snapshot.
  const unreadPinRef = useRef(0)

  // ── Message context menu, edit mode, branch dialog state ──────
  const [menuState, setMenuState] = useState<
    | { messageId: string; role: Message['role']; x: number; y: number }
    | null
  >(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [branchFor, setBranchFor] = useState<string | null>(null)

  const openMenu = useCallback(
    (e: React.MouseEvent, message: Message) => {
      // Ignore right-clicks on links / inputs / textareas inside the
      // message — preserve the native browser behaviour for those.
      const target = e.target as HTMLElement
      if (target.closest('a[href], input, textarea, [contenteditable="true"]')) return
      e.preventDefault()
      setMenuState({
        messageId: message.id,
        role: message.role,
        x: e.clientX,
        y: e.clientY,
      })
    },
    [],
  )
  const closeMenu = useCallback(() => setMenuState(null), [])

  const handlePick = useCallback(
    (action: MessageMenuAction) => {
      if (!menuState) return
      const id = menuState.messageId
      if (action === 'edit') {
        setEditingId(id)
      } else if (action === 'rerun') {
        void rerunUserMessage(id)
      } else if (action === 'delete') {
        void deleteMessagesFrom(id)
      } else if (action === 'branch') {
        setBranchFor(id)
      } else if (action === 'pin') {
        void toggleEntryPin(id, true)
      } else if (action === 'unpin') {
        void toggleEntryPin(id, false)
      }
    },
    [menuState, rerunUserMessage, deleteMessagesFrom, toggleEntryPin],
  )

  const beginEdit = useCallback((id: string) => setEditingId(id), [])
  const cancelEdit = useCallback(() => setEditingId(null), [])
  const saveEdit = useCallback(
    (id: string, text: string) => {
      setEditingId(null)
      void editUserMessage(id, text)
    },
    [editUserMessage],
  )

  const messageActionsValue = useMemo<MessageActionsValue>(
    () => ({ openMenu, editingId, beginEdit, saveEdit, cancelEdit }),
    [openMenu, editingId, beginEdit, saveEdit, cancelEdit],
  )

  // Goal-mode calibration ribbon: surfaces the judge calibrator in the
  // chat pane so the operator sees the lifecycle even with the mission
  // dashboard closed. Derived from goal_calibration_* events in the
  // session's systemEvents stream.
  const calibrationView = useMemo(
    () => deriveCalibrationView(systemEvents),
    [systemEvents],
  )
  const showCalibrationRibbon =
    coordinationStrategy === 'goal' && calibrationView.calibration !== null

  // Branch dialog details
  const branchTarget = branchFor
    ? messages.find((m) => m.id === branchFor)
    : null
  const branchHumanIndex = branchTarget
    ? messages.findIndex((m) => m.id === branchFor) + 1
    : 0

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

  const lockScrollUp = useCallback(() => {
    if (scrolledUpRef.current) return
    scrolledUpRef.current = true
    unreadPinRef.current = messages.length
    setScrolledUp(true)
    setUnreadCount(0)
  }, [messages.length])

  const unlockScroll = useCallback(() => {
    if (!scrolledUpRef.current) {
      // Even if the lock is already off, clear any lingering unread
      // counter so the button label resets cleanly.
      setUnreadCount(0)
      return
    }
    scrolledUpRef.current = false
    setScrolledUp(false)
    setUnreadCount(0)
  }, [])

  const scrollToBottom = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    unlockScroll()
    el.scrollTop = el.scrollHeight
  }, [unlockScroll])

  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const onScroll = () => {
      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      if (distFromBottom < 10) {
        // Back at the bottom — drop the lock and clear unread no
        // matter how it got there (user scrolled down, jump button,
        // auto-scroll while unlocked).
        if (scrolledUpRef.current) {
          scrolledUpRef.current = false
          setScrolledUp(false)
        }
        setUnreadCount(0)
      } else if (distFromBottom > 40 && !scrolledUpRef.current) {
        // Backup path for scrollbar-drag / keyboard scrolling that
        // doesn't fire wheel. Wheel/touchmove are the primary lock
        // triggers — by the time scroll fires, the lock is usually
        // already on.
        scrolledUpRef.current = true
        unreadPinRef.current = messages.length
        setScrolledUp(true)
        setUnreadCount(0)
      }
    }
    const onWheel = (e: WheelEvent) => {
      // Any upward wheel intent → lock immediately. Downward wheels
      // are ambiguous (could be the user catching up to the bottom),
      // so let the scroll handler decide via position.
      if (e.deltaY < 0) lockScrollUp()
    }
    const onTouchMove = () => {
      // Trackpad / touch drags are almost always reads. Lock
      // proactively; the scroll handler will unlock at the bottom.
      lockScrollUp()
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    el.addEventListener('wheel', onWheel, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('touchmove', onTouchMove)
    }
  }, [lockScrollUp, messages.length])

  // Track the last message count so we can detect when the user sends
  // a new message (which should snap scroll back to the bottom).
  const prevMsgCountRef = useRef(messages.length)

  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    if (searchOpen) return

    const lastMsg = messages[messages.length - 1]
    const grew = messages.length > prevMsgCountRef.current
    const userSent = grew && lastMsg?.role === 'user'
    prevMsgCountRef.current = messages.length

    // Operator's own message always snaps to bottom — they expect to
    // see their own send + whatever response is about to stream.
    if (userSent) {
      scrolledUpRef.current = false
      setScrolledUp(false)
      setUnreadCount(0)
      el.scrollTop = el.scrollHeight
      return
    }

    if (scrolledUpRef.current) {
      // Scrolled-up case: don't move the viewport, but tally how many
      // new messages have arrived since the lock landed so the jump
      // button can read "↓ N new".
      if (grew) {
        setUnreadCount(messages.length - unreadPinRef.current)
      }
      return
    }

    // Sticky-bottom case: keep the tail glued to the bottom as
    // streaming chunks arrive. No programmatic-scroll guard — the
    // scroll handler's threshold-based logic already distinguishes
    // "at bottom" (dist < 10) from "user pulled up" (dist > 40).
    el.scrollTop = el.scrollHeight
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
      lockScrollUp()
      target.scrollIntoView({ block: 'center', behavior: 'smooth' })
    })
  }, [focusedToolCallId, focusedToolCallSerial, lockScrollUp])

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
        <MessageActionsContext.Provider value={messageActionsValue}>
          <div ref={scrollerRef} className="flex min-h-0 flex-1 flex-col overflow-y-auto overflow-x-hidden">
            <ChildSessionBreadcrumb />
            <div className="mx-auto w-full max-w-[1200px] px-8 py-6">
              {systemPrompt && systemPrompt.trim() && (
                <SystemPromptHeader prompt={systemPrompt} />
              )}
              {showCalibrationRibbon && (
                <CalibrationCard
                  calibration={calibrationView.calibration}
                  judgeRules={calibrationView.judgeRules}
                  proposal={calibrationView.proposal}
                  variant="chat"
                  onOpenJudgeBrief={() => toggleMissionDashboard(true, 'overview')}
                />
              )}
              <ConversationStream messages={messages} systemEvents={systemEvents} />
              {/* Thinking renders inline within message parts now */}
            </div>
          </div>
        </MessageActionsContext.Provider>
      </SearchQueryContext.Provider>
      {scrolledUp && (
        <ScrollToBottomButton
          unread={unreadCount}
          onClick={scrollToBottom}
        />
      )}
      {menuState && (
        <MessageContextMenu
          x={menuState.x}
          y={menuState.y}
          isUserMessage={menuState.role === 'user'}
          busy={isStreaming}
          isPinned={Boolean(
            messages.find((m) => m.id === menuState.messageId)?.pinned,
          )}
          onPick={handlePick}
          onClose={closeMenu}
        />
      )}
      {branchFor && branchTarget && (
        <BranchSessionDialog
          defaultName={`branch @ msg ${branchHumanIndex}`}
          branchAtHumanIndex={branchHumanIndex}
          onCancel={() => setBranchFor(null)}
          onConfirm={(name) => {
            setBranchFor(null)
            void branchSessionFrom(branchFor, name)
          }}
        />
      )}
    </div>
  )
}

/** Floating pill anchored to the bottom-center of the conversation
 *  pane. Visible only while the user is scrolled up; label switches to
 *  a "N new" counter once messages have arrived behind their back. */
function ScrollToBottomButton({
  unread,
  onClick,
}: {
  unread: number
  onClick: () => void
}) {
  const hasUnread = unread > 0
  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-3 z-20 flex justify-center">
      <button
        type="button"
        onClick={onClick}
        className={`pointer-events-auto inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.14em] shadow-lg backdrop-blur-md transition ${
          hasUnread
            ? 'bg-accent/15 text-accent ring-1 ring-accent/40 hover:bg-accent/25'
            : 'bg-white/[0.06] text-fg-1 ring-hairline hover:bg-white/[0.10] hover:text-fg-0'
        }`}
        title={hasUnread ? 'Jump to new messages' : 'Jump to bottom'}
      >
        <span aria-hidden>↓</span>
        {hasUnread ? `${unread} new` : 'bottom'}
      </button>
    </div>
  )
}

function UserMessageEditor({
  initialText,
  onCancel,
  onSave,
}: {
  initialText: string
  onCancel: () => void
  onSave: (text: string) => void
}) {
  const [text, setText] = useState(initialText)
  const ref = useRef<HTMLTextAreaElement | null>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
    // Auto-grow.
    const grow = () => {
      el.style.height = 'auto'
      el.style.height = `${Math.min(360, el.scrollHeight)}px`
    }
    grow()
    el.addEventListener('input', grow)
    return () => el.removeEventListener('input', grow)
  }, [])
  const submit = () => {
    const trimmed = text.trim()
    if (!trimmed) {
      onCancel()
      return
    }
    onSave(trimmed)
  }
  return (
    <div className="render-cached animate-fade-in mb-6 flex flex-col items-end gap-2">
      <div className="font-prose w-full max-w-[76%] rounded-lg bg-accent/10 ring-1 ring-accent/30">
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.preventDefault()
              onCancel()
            } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault()
              submit()
            }
          }}
          rows={2}
          className="block w-full resize-none bg-transparent px-3.5 py-2 text-[12.5px] leading-[1.55] text-fg-0 placeholder:text-fg-3 focus:outline-none"
          placeholder="Edit message…"
        />
        <div className="flex items-center justify-between gap-2 hairline-t px-3 py-2 font-mono text-[10px] uppercase tracking-[0.08em]">
          <span className="text-fg-3">edit + rerun · ⌘↵ save · esc cancel</span>
          <div className="flex items-center gap-2">
            <button
              onClick={onCancel}
              className="rounded bg-white/[0.05] px-2 py-[2px] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              cancel
            </button>
            <button
              onClick={submit}
              disabled={!text.trim()}
              className="rounded bg-accent/15 px-2 py-[2px] text-accent ring-1 ring-accent/30 hover:bg-accent/25 disabled:opacity-60"
            >
              save + rerun
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// Narrator-voice system events. These are board-level moves the
// dispatcher makes on its own — not part of any model turn — and they
// render as italic stage directions interleaved with the conversation
// by timestamp. Keep this set tight: only events the user genuinely
// benefits from seeing in narrative flow. Per-tick noise stays in the
// dashboard's dispatcher pulse panel.
const NARRATOR_SUBTYPES = new Set([
  'kanban_dispatched',
  'kanban_reclaimed',
  'kanban_autopilot_enabled',
  'kanban_autopilot_disabled',
  'kanban_replay',
  // Default-on judge-review pipeline (Move R).
  'kanban_review_started',
  'kanban_judge_verdict',
  'kanban_rework_started',
  'kanban_blocked',
  'kanban_judge_failed',
])

function ConversationStream({
  messages,
  systemEvents,
}: {
  messages: Message[]
  systemEvents: SystemEventRecord[]
}) {
  const inboxEvents = useHarness((s) => s.inboxEvents)
  const openSessionPane = useHarness((s) => s.openSessionPane)
  const sessions = useHarness((s) => s.sessions)
  const narratorEvents = useMemo(
    () => systemEvents.filter((e) => NARRATOR_SUBTYPES.has(e.subtype)),
    [systemEvents],
  )
  // Walk kanban-state events to build a snapshot map keyed by card id.
  // Used by Part text rendering to decorate `card_NNN` mentions in
  // prose with hover tooltips + click-to-dashboard.
  const kanbanLookup = useMemo(() => {
    const map = new Map<string, KanbanCardSnapshot>()
    for (const event of systemEvents) {
      if (!event.subtype.startsWith('kanban_')) continue
      const details = event.details as Record<string, unknown> | undefined
      const task = details?.task as Record<string, unknown> | undefined
      if (!task) continue
      const id = typeof task.id === 'string' ? task.id : ''
      if (!id) continue
      map.set(id, {
        status: typeof task.status === 'string' ? task.status : 'ready',
        title: typeof task.title === 'string' ? task.title : '',
        assignee: typeof task.assignee === 'string' ? task.assignee : '',
      })
    }
    return map
  }, [systemEvents])
  // Inbox events: only show "enqueued" actions (each message at the
  // moment it landed in this session's inbox). Delivered duplicates
  // would clutter the stream. Cap to recent — the slice already trims
  // at 100, but a long-running session might still have many.
  //
  // `kind === 'spawn'` events are synthetic — they exist only so the
  // activity comm graph can show parent → child intent. The actual
  // task is already rendered as the first user message in this
  // transcript, so we'd just be double-printing it as a chip. Skip
  // them in the inline rail.
  const inboxArrivals = useMemo(
    () =>
      inboxEvents.filter(
        (e) => e.action === 'enqueued' && (e as { kind?: string }).kind !== 'spawn',
      ),
    [inboxEvents],
  )

  // Build a chronologically-ordered stream. Tie-breaker on equal
  // timestamps: messages come first so a narrator event written
  // immediately after a parent turn lands beneath the message it
  // followed, not before it.
  const stream = useMemo(() => {
    type Entry =
      | { kind: 'message'; at: number; message: Message }
      | { kind: 'narrator'; at: number; event: SystemEventRecord }
      | { kind: 'inbox'; at: number; event: typeof inboxArrivals[number] }
    const entries: Entry[] = []
    for (const m of messages) entries.push({ kind: 'message', at: m.createdAt, message: m })
    for (const e of narratorEvents) entries.push({ kind: 'narrator', at: e.at, event: e })
    for (const e of inboxArrivals) entries.push({ kind: 'inbox', at: e.timestamp, event: e })
    entries.sort((a, b) => {
      if (a.at !== b.at) return a.at - b.at
      if (a.kind === b.kind) return 0
      // messages first, then inbox chips, then narrator lines
      const order = { message: 0, inbox: 1, narrator: 2 } as const
      return order[a.kind] - order[b.kind]
    })
    return entries
  }, [messages, narratorEvents, inboxArrivals])

  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  // Delegated click handler for `.kanban-card-mention` spans inserted
  // into prose by the markdown post-pass. Opens the dashboard on the
  // swarm tab so the operator can scroll to the card; per-card focus
  // selection is a follow-up.
  const onClick = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      const target = event.target as HTMLElement | null
      if (!target) return
      const mention = target.closest('.kanban-card-mention')
      if (!mention) return
      event.preventDefault()
      event.stopPropagation()
      toggleMissionDashboard(true, 'overview')
    },
    [toggleMissionDashboard],
  )

  // System event index keyed by id so inline system parts can hydrate
  // their richer payloads (verdict details, etc.) without round-tripping
  // through the global systemEvents array on every render.
  const systemEventLookup = useMemo(() => {
    const map = new Map<string, SystemEventRecord>()
    for (const event of systemEvents) map.set(event.id, event)
    return map
  }, [systemEvents])

  return (
    <KanbanCardLookupContext.Provider value={kanbanLookup}>
      <SystemEventLookupContext.Provider value={systemEventLookup}>
        <div onClick={onClick}>
          {stream.map((entry, idx) => {
            if (entry.kind === 'message') {
              return <MessageView key={entry.message.id} message={entry.message} />
            }
            if (entry.kind === 'inbox') {
              const ev = entry.event
              const senderSession = sessions.find((s) => s.id === ev.fromSession)
              return (
                <InboxChip
                  key={`inbox-${ev.id}-${idx}`}
                  fromLabel={ev.fromLabel}
                  fromRole={ev.fromRole}
                  content={ev.content}
                  force={ev.force}
                  replyTo={ev.replyTo}
                  senderSessionId={ev.fromSession}
                  onOpenSender={
                    senderSession
                      ? () => openSessionPane(ev.fromSession, 'split')
                      : undefined
                  }
                />
              )
            }
            const event = entry.event
            return <NarratorLine key={`${event.id}-${idx}`} event={event} />
          })}
        </div>
      </SystemEventLookupContext.Provider>
    </KanbanCardLookupContext.Provider>
  )
}

function NarratorLine({ event }: { event: SystemEventRecord }) {
  const text = formatNarratorEvent(event)
  if (!text) return null
  // No glyph chrome, no border, no badge — italic prose at low contrast
  // that sits at the same column as the parent's text. Reads as a
  // stage direction in the screenplay sense, not a system notification.
  return (
    <div className="my-3 select-text font-prose text-[12.5px] italic leading-[1.55] text-fg-3">
      {text}
    </div>
  )
}

/** Inline chip rendering for an inbox message that arrived at this
 *  session. Shows sender attribution (operator / agent + label), the
 *  message content (truncated), a force flag, and a click target to
 *  open the sender's session pane. */
function InboxChip({
  fromLabel,
  fromRole,
  content,
  force,
  replyTo,
  senderSessionId,
  onOpenSender,
}: {
  fromLabel: string
  fromRole: 'operator' | 'agent'
  content: string
  force: boolean
  replyTo: string | null
  senderSessionId: string
  onOpenSender?: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const preview = content.length > 160 ? content.slice(0, 157) + '…' : content
  const roleTone =
    fromRole === 'operator'
      ? 'text-accent border-accent/[0.30] bg-accent/[0.05]'
      : 'text-fg-1 border-white/[0.10] bg-white/[0.02]'
  return (
    <div
      className={`my-2 select-text rounded-md border px-3 py-1.5 font-mono text-[11.5px] leading-[1.55] ${roleTone} ${
        force ? 'ring-1 ring-warn/[0.30]' : ''
      }`}
    >
      <div className="flex items-center gap-2 text-[10.5px] uppercase tracking-[0.14em]">
        <span className="text-fg-3">↳ message</span>
        {senderSessionId && onOpenSender ? (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()
              onOpenSender()
            }}
            className={`rounded px-1.5 py-0.5 normal-case tracking-normal transition hover:bg-white/[0.06] ${
              fromRole === 'operator' ? 'text-accent' : 'text-fg-2 hover:text-fg-0'
            }`}
            title={`Open ${fromLabel}'s session pane`}
          >
            {fromLabel}
          </button>
        ) : (
          <span className={fromRole === 'operator' ? 'text-accent' : 'text-fg-2'}>
            {fromLabel}
          </span>
        )}
        {force && (
          <span className="rounded border border-warn/[0.40] bg-warn/[0.06] px-1.5 py-0.5 text-[9.5px] tracking-[0.14em] text-warn">
            force
          </span>
        )}
        {replyTo && (
          <span className="text-fg-4">· reply to {replyTo.slice(0, 6)}</span>
        )}
      </div>
      <div
        onClick={() => setExpanded((v) => !v)}
        className="mt-1 cursor-pointer whitespace-pre-wrap"
      >
        {expanded ? content : preview}
        {!expanded && content.length > preview.length && (
          <span className="ml-1 text-fg-4">[click to expand]</span>
        )}
      </div>
    </div>
  )
}

function formatNarratorEvent(event: SystemEventRecord): string {
  const details = (event.details ?? {}) as Record<string, unknown>
  switch (event.subtype) {
    case 'kanban_dispatched': {
      const agent = typeof details.agentType === 'string' ? details.agentType : 'worker'
      const card = typeof details.cardId === 'string' ? details.cardId : ''
      if (!card) return `dispatching ${agent}`
      return `dispatching ${agent} on ${card}`
    }
    case 'kanban_reclaimed': {
      const card = typeof details.cardId === 'string' ? details.cardId : ''
      const ageSeconds = typeof details.ageSeconds === 'number' ? details.ageSeconds : null
      if (!card) return 'reclaiming a stuck card'
      if (ageSeconds === null) return `reclaiming ${card}`
      const minutes = Math.floor(ageSeconds / 60)
      const window = minutes > 0 ? `${minutes}m` : `${Math.floor(ageSeconds)}s`
      return `reclaiming ${card} — heartbeat stale ${window}`
    }
    case 'kanban_autopilot_enabled':
      return 'autopilot on'
    case 'kanban_autopilot_disabled':
      return 'autopilot off'
    case 'kanban_replay': {
      const count = typeof details.eventCount === 'number' ? details.eventCount : 0
      const restarts = typeof details.restartCount === 'number' ? details.restartCount : 0
      if (count === 0) return ''
      const prefix = restarts > 1 ? `mission resumed (restart ${restarts})` : 'mission resumed'
      return `${prefix} — ${count} prior events replayed`
    }
    case 'kanban_review_started': {
      const card = typeof details.cardId === 'string' ? details.cardId : ''
      const iter = typeof details.reviewIteration === 'number' ? details.reviewIteration : 0
      const sticky = details.sticky === true
      if (!card) return 'judge reviewing'
      return `${sticky ? 're-reviewing' : 'judging'} ${card}${iter ? ` · iter ${iter}` : ''}`
    }
    case 'kanban_judge_verdict': {
      const card = typeof details.cardId === 'string' ? details.cardId : ''
      const done = details.done === true
      if (!card) return done ? 'judge passed a card' : 'judge rejected a card'
      return done ? `judge passed ${card}` : `judge rejected ${card}`
    }
    case 'kanban_rework_started': {
      const card = typeof details.cardId === 'string' ? details.cardId : ''
      const iter = typeof details.reviewIteration === 'number' ? details.reviewIteration : 0
      if (!card) return 'reworking a card'
      return `reworking ${card}${iter ? ` · iter ${iter}/5` : ''}`
    }
    case 'kanban_blocked': {
      const card = typeof details.cardId === 'string' ? details.cardId : ''
      return card ? `blocked ${card} (5 rework attempts exhausted)` : 'card blocked at iteration cap'
    }
    case 'kanban_judge_failed': {
      const card = typeof details.cardId === 'string' ? details.cardId : ''
      return card ? `judge crashed on ${card}` : 'judge crashed'
    }
    default:
      return ''
  }
}

/** Wrap `card_NNN` mentions in rendered prose with a low-key chip that
 *  carries the card's current status as a tooltip and is wired (via
 *  delegated click in ConversationStream) to open the dashboard.
 *
 *  Naive regex pass: relies on the matched id being unlikely to appear
 *  inside HTML attributes generated by `renderMarkdown`. Code/pre
 *  blocks can technically contain `card_NNN` literally; if they do
 *  they'll also be chipped. Acceptable cosmetic edge for V1. */
function decorateCardMentions(
  html: string,
  lookup: Map<string, KanbanCardSnapshot>,
): string {
  if (lookup.size === 0) return html
  return html.replace(/\bcard_(\d{3,})\b/g, (match) => {
    const snapshot = lookup.get(match)
    if (!snapshot) return match
    const title = `${snapshot.status}${snapshot.title ? ` · ${snapshot.title}` : ''}${snapshot.assignee ? ` · ${snapshot.assignee}` : ''}`
    return `<span class="kanban-card-mention" data-card-id="${escapeAttr(match)}" title="${escapeAttr(title)}">${match}</span>`
  })
}

function escapeAttr(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
}

const MessageView = memo(function MessageView({ message }: { message: Message }) {
  const searchQuery = useContext(SearchQueryContext)
  const actions = useContext(MessageActionsContext)
  // Is this message the one currently receiving streamed deltas? We use
  // it to decide whether to animate the "active tail" of the message
  // (e.g. a still-rendering thinking block). Once the model emits new
  // parts after a thinking block, that thinking block is finished and
  // should stop animating even if the overall turn is still running.
  const currentStreamingMessageId = useHarness((s) => s.currentStreamingMessageId)
  const isStreaming = useHarness((s) => s.isStreaming)
  const isStreamingMessage = isStreaming && currentStreamingMessageId === message.id
  const isEditing = actions?.editingId === message.id

  const onContextMenu = useCallback(
    (e: React.MouseEvent) => actions?.openMenu(e, message),
    [actions, message],
  )

  if (message.role === 'user') {
    const textContent = message.parts
      .filter((p) => p.type === 'text')
      .map((p) => p.text)
      .join('')

    if (isEditing && actions) {
      return (
        <UserMessageEditor
          initialText={textContent}
          onCancel={actions.cancelEdit}
          onSave={(text) => actions.saveEdit(message.id, text)}
        />
      )
    }

    return (
      <div
        onContextMenu={onContextMenu}
        className="render-cached animate-fade-in mb-6 flex flex-col items-end gap-2"
      >
        {message.attachments && message.attachments.length > 0 && (
          <div className="flex max-w-[76%] flex-wrap justify-end gap-1.5">
            {message.attachments.map((att) =>
              att.type === 'video' ? (
                <video
                  key={att.id}
                  src={att.previewUrl}
                  controls
                  preload="metadata"
                  className="max-h-[260px] max-w-full rounded-lg ring-1 ring-accent/20"
                />
              ) : (
                <img
                  key={att.id}
                  src={att.previewUrl}
                  alt="attached"
                  className="max-h-[180px] rounded-lg ring-1 ring-accent/20"
                  loading="lazy"
                  decoding="async"
                />
              ),
            )}
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
    <div
      onContextMenu={onContextMenu}
      className={`${isStreamingMessage ? '' : 'render-cached '}animate-fade-in mb-6`}
    >
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
  | { kind: 'widget'; toolCallId: string }
  | { kind: 'single'; part: MessagePart }

function groupParts(parts: MessagePart[]): PartGroup[] {
  const out: PartGroup[] = []
  const state = useHarness.getState()
  const toolCalls = state.toolCalls
  const widgets = state.widgets
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
      // Generative-UI widgets — the tool chip + parallel-group chrome
      // are dead weight; the widget IS the artifact. Pull these out of
      // parallel grouping so each renders chromeless floating directly
      // in the message stream. Errored calls (no widget, status=error)
      // fall through to the normal chip so the operator sees what
      // failed.
      if (tc?.name === 'show_widget') {
        const hasWidget = !!widgets[part.toolCallId]
        const stillRunning = tc.status === 'running'
        if (hasWidget || stillRunning) {
          out.push({ kind: 'widget', toolCallId: part.toolCallId })
          continue
        }
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
  if (group.kind === 'widget') {
    return <FloatingWidget toolCallId={group.toolCallId} />
  }
  return <Part part={group.part} isActiveTail={isActiveTail} />
}

/** Chromeless widget wrapper used inline in the message stream.
 *  Subscribes by id so an unrelated widget render doesn't re-render
 *  every group. Maps the live tool-call state into the Widget
 *  component's streaming flag — while the agent is still emitting the
 *  widget_code argument, the iframe stays unmounted and we show a
 *  cycling loader so partial markup never reflows mid-stream. */
function FloatingWidget({ toolCallId }: { toolCallId: string }) {
  const widget = useHarness((s) => s.widgets[toolCallId])
  const toolStatus = useHarness((s) => s.toolCalls[toolCallId]?.status)
  const isStreaming = toolStatus === 'running' || !widget
  return (
    <Widget
      toolCallId={toolCallId}
      title={widget?.title ?? 'widget'}
      code={widget?.code ?? ''}
      kind={widget?.kind ?? 'html'}
      loadingMessages={widget?.loadingMessages ?? []}
      isStreaming={isStreaming}
    />
  )
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
  const kanbanLookup = useContext(KanbanCardLookupContext)
  const sourceText = part.type === 'text' ? part.text ?? '' : ''
  const visibleText = useCharacterReveal(
    sourceText,
    part.type === 'text' && isActiveTail && !searchQuery,
  )
  const renderedTextHtml = useMemo(
    () => (part.type === 'text' ? renderMarkdown(visibleText) : ''),
    [part.type, visibleText],
  )
  // Detect when an assistant text part is actually a JSON payload
  // (e.g. judge calibrator output) and short-circuit to a structured
  // card. Only attempt this once the part is no longer the streaming
  // tail — partial JSON parses produce flicker, and a search query
  // means the operator wants to scan raw text, so we leave markdown
  // in place there too.
  const parsedJson = useMemo(() => {
    if (part.type !== 'text') return undefined
    if (isActiveTail) return undefined
    if (searchQuery) return undefined
    const parsed = tryParseCompleteJson(sourceText)
    if (!parsed || typeof parsed !== 'object') return undefined
    return parsed
  }, [part.type, isActiveTail, searchQuery, sourceText])

  if (part.type === 'text') {
    if (parsedJson !== undefined) {
      return <StructuredJsonView data={parsedJson} />
    }
    const decorated = decorateCardMentions(renderedTextHtml, kanbanLookup)
    const html = searchQuery ? highlightHtml(decorated, searchQuery) : decorated
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
    // `show_widget` tool calls never reach Part — `groupParts` lifts
    // them into the `widget` group kind which renders FloatingWidget
    // directly. So the chip path here is the right fallback for any
    // non-widget tool call.
    return <ToolCallChip id={part.toolCallId} />
  }
  if (part.type === 'subagent' && part.subagentId) {
    return <SubagentCard id={part.subagentId} />
  }
  if (part.type === 'thinking' && part.text) {
    return <ThinkingBlock text={part.text} isActive={isActiveTail} />
  }
  if (part.type === 'system') {
    // Goal verdicts get a richer inline card — the operator wants to
    // see the judge's reasoning + criteria delta without switching to
    // the studio view. All other system subtypes fall through to the
    // compact warn-glyph chip used by compaction / context_pruning /
    // tool_truncation etc.
    if (part.systemSubtype === 'goal_judge' && part.eventId) {
      return <InlineGoalVerdict eventId={part.eventId} />
    }
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

// ============ INLINE GOAL VERDICT ============
//
// Compact card that drops into the conversation after each judge turn.
// Shows verdict badge + confidence + profile, plus the judge's reason
// clamped to 3 lines (click to expand). Includes a criteria summary
// row (counts by status) and a "view in studio" link. Reads the rich
// verdict payload from systemEvents via SystemEventLookupContext —
// the part itself only carries an eventId pointer.

function InlineGoalVerdict({ eventId }: { eventId: string }) {
  const lookup = useContext(SystemEventLookupContext)
  const event = lookup.get(eventId)
  const [expanded, setExpanded] = useState(false)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const openSessionPane = useHarness((s) => s.openSessionPane)

  if (!event) {
    // Event hasn't arrived in the global slice yet (e.g. mid-flight) —
    // fall through to a placeholder so layout doesn't jump.
    return (
      <div className="flex items-center gap-2 rounded-md border border-white/[0.06] bg-white/[0.018] px-2.5 py-1.5 font-mono text-[11px] text-fg-3">
        judge thinking…
      </div>
    )
  }
  const verdict = (event.details?.verdict ?? {}) as {
    done?: boolean
    confidence?: number
    reason?: string
    criteria?: Array<{ status?: string; priority?: string }>
    openQuestions?: string[]
    judgeSessionId?: string | null
    fallbackFrom?: string | null
  }
  const rules = (event.details?.judgeRules ?? null) as {
    judgeProfile?: 'skip' | 'quick' | 'standard' | 'deep'
  } | null
  const profile = rules?.judgeProfile ?? 'standard'
  const done = !!verdict.done
  const conf = typeof verdict.confidence === 'number' ? verdict.confidence : 0
  const reason = verdict.reason || (done ? 'Goal satisfied.' : 'Continuing.')
  const criteria = Array.isArray(verdict.criteria) ? verdict.criteria : []
  const counts = {
    met: criteria.filter((c) => c.status === 'met').length,
    partial: criteria.filter((c) => c.status === 'partial').length,
    missing: criteria.filter((c) => c.status === 'missing').length,
  }
  const openQ = Array.isArray(verdict.openQuestions) ? verdict.openQuestions : []

  const accent = done
    ? 'border-ok/[0.28] bg-ok/[0.05]'
    : 'border-accent/[0.18] bg-accent/[0.025]'
  const profileChip =
    profile === 'deep'
      ? 'border-accent/[0.32] bg-accent/[0.08] text-accent'
      : profile === 'quick'
      ? 'border-fg-4/[0.32] bg-white/[0.03] text-fg-1'
      : 'border-white/[0.12] bg-white/[0.04] text-fg-1'

  return (
    <div className={`overflow-hidden rounded-lg border ${accent}`}>
      {/* Header row */}
      <div className="flex flex-wrap items-center gap-2.5 border-b border-white/[0.04] px-3 py-2">
        <span className="font-mono text-[9.5px] uppercase tracking-[0.18em] text-fg-4">
          judge
        </span>
        <span
          className={`inline-flex items-center rounded-[6px] border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] ${profileChip}`}
        >
          {profile}
        </span>
        <span
          className={`inline-flex items-center gap-1.5 rounded-[10px] border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] ${
            done
              ? 'border-ok/[0.32] bg-ok/[0.08] text-ok'
              : 'border-accent/[0.22] bg-accent/[0.06] text-accent'
          }`}
        >
          <span className={`h-1 w-1 rounded-full ${done ? 'bg-ok' : 'bg-accent'}`} />
          {done ? 'done' : 'continue'}
        </span>
        <span className="font-mono text-[11px] tabular-nums text-fg-2">
          conf{' '}
          <span
            className={
              conf >= 0.85 ? 'text-ok' : conf >= 0.5 ? 'text-accent' : 'text-warn'
            }
          >
            {conf.toFixed(2)}
          </span>
        </span>
        {criteria.length > 0 ? (
          <span className="font-mono text-[11px] text-fg-3">
            ·{' '}
            {counts.met > 0 ? (
              <span className="text-ok">{counts.met} met</span>
            ) : null}
            {counts.met > 0 && (counts.partial > 0 || counts.missing > 0) ? ' · ' : ''}
            {counts.partial > 0 ? (
              <span className="text-accent">{counts.partial} partial</span>
            ) : null}
            {counts.partial > 0 && counts.missing > 0 ? ' · ' : ''}
            {counts.missing > 0 ? (
              <span className="text-warn">{counts.missing} missing</span>
            ) : null}
          </span>
        ) : null}
        {openQ.length > 0 ? (
          <span className="font-mono text-[11px] text-warn">
            · {openQ.length} open
          </span>
        ) : null}
        <button
          type="button"
          onClick={() => toggleMissionDashboard(true)}
          className="ml-auto rounded px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3 transition hover:bg-white/[0.06] hover:text-fg-1"
          title="Open goal studio for the full timeline"
        >
          studio ↗
        </button>
        {verdict.judgeSessionId ? (
          <button
            type="button"
            onClick={() => openSessionPane(verdict.judgeSessionId!)}
            className="rounded px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3 transition hover:bg-white/[0.06] hover:text-accent"
            title="Open the judge subagent's own session"
          >
            judge session ↗
          </button>
        ) : null}
      </div>

      {/* Reason — 3 lines clamped by default, click to expand */}
      {verdict.fallbackFrom ? (
        <div className="select-text border-b border-warn/[0.12] bg-warn/[0.05] px-3 py-1.5 font-mono text-[10.5px] text-warn">
          judge-fallback · {verdict.fallbackFrom}
        </div>
      ) : null}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="block w-full cursor-text select-text px-3 py-2 text-left"
      >
        <p
          className={`m-0 whitespace-pre-wrap font-mono text-[12px] leading-[1.6] text-fg-1 ${
            expanded ? '' : 'line-clamp-3'
          }`}
        >
          {reason}
        </p>
        {!expanded && reason.length > 200 ? (
          <span className="mt-1 inline-block font-mono text-[10px] uppercase tracking-[0.14em] text-fg-4">
            click to expand
          </span>
        ) : null}
      </button>

      {/* Open questions (expanded only) */}
      {expanded && openQ.length > 0 ? (
        <ul className="m-0 flex list-none flex-col gap-1 border-t border-white/[0.04] px-3 py-2">
          {openQ.map((q, i) => (
            <li
              key={i}
              className="select-text rounded-md border border-warn/[0.16] bg-warn/[0.04] px-2 py-1 font-mono text-[11.5px] leading-[1.55] text-fg-1"
            >
              {q}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
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

/** Collapsible header that surfaces the active session's system prompt
 *  at the top of the conversation. Useful for inspecting judge /
 *  calibrator child sessions where the system prompt IS the contract.
 *  Rendered for any session with a stored systemPrompt — main sessions
 *  rarely have one set, so this is functionally subagent-only today. */
function SystemPromptHeader({ prompt }: { prompt: string }) {
  const [open, setOpen] = useState(false)
  const lineCount = useMemo(() => prompt.split(/\n/).length, [prompt])
  const charCount = prompt.length
  return (
    <section className="mb-4 overflow-hidden rounded-md border border-white/[0.06] bg-white/[0.02]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-3.5 py-2 text-left transition hover:bg-white/[0.04]"
      >
        <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-3">
          system prompt
        </span>
        <span className="font-mono text-[11px] tabular-nums text-fg-4">
          {lineCount.toLocaleString()} lines · {charCount.toLocaleString()} chars
        </span>
        <span className="ml-auto font-mono text-[10px] uppercase tracking-[0.14em] text-fg-4">
          {open ? '▾' : '▸'}
        </span>
      </button>
      {open && (
        <pre className="m-0 max-h-[480px] select-text overflow-y-auto border-t border-white/[0.05] px-3.5 py-3 font-mono text-[11.5px] leading-[1.55] text-fg-1 whitespace-pre-wrap">
          {prompt}
        </pre>
      )}
    </section>
  )
}

interface CalibrationView {
  calibration: CalibrationStatus | null
  judgeRules: JudgeRules | null
  proposal: JudgeRules | null
}

/** Walk the session's systemEvents stream once and return:
 *  - the latest calibration lifecycle state
 *  - the most recent JudgeRules snapshot (carries calibrator meta)
 *  - any pending proposal
 *
 * Mirrors collectGoalState in MissionDashboard but standalone to avoid
 * cross-importing dashboard internals into the chat surface. */
function deriveCalibrationView(
  events: ReadonlyArray<SystemEventRecord>,
): CalibrationView {
  let calibration: CalibrationStatus | null = null
  let judgeRules: JudgeRules | null = null
  let proposal: JudgeRules | null = null

  // Walk newest-first. Pick the first goal_calibration_* event we see;
  // pick the first event that carries judgeRules / judgeRulesProposal.
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i]
    if (!ev.subtype.startsWith('goal_')) continue
    const details = (ev.details ?? {}) as Record<string, unknown>
    if (judgeRules === null && details.judgeRules) {
      judgeRules = details.judgeRules as JudgeRules
    }
    if (proposal === null && details.judgeRulesProposal) {
      proposal = details.judgeRulesProposal as JudgeRules
    }
    if (
      calibration === null &&
      (ev.subtype === 'goal_calibration_started' ||
        ev.subtype === 'goal_calibration_complete' ||
        ev.subtype === 'goal_calibration_failed')
    ) {
      const sessionId = (details.calibratorSessionId as string | null | undefined) ?? null
      const reason = typeof details.reason === 'string' ? details.reason : undefined
      const model = typeof details.model === 'string' ? details.model : undefined
      if (ev.subtype === 'goal_calibration_started') {
        calibration = {
          status: 'running',
          sessionId,
          model,
          reason,
          at: ev.at,
          willApplyAutomatically: details.willApplyAutomatically === true,
        }
      } else if (ev.subtype === 'goal_calibration_failed') {
        calibration = {
          status: 'failed',
          sessionId,
          model,
          reason,
          at: ev.at,
          errorMessage: typeof details.error === 'string' ? details.error : undefined,
        }
      } else {
        const applied = details.applied === true
        calibration = {
          status: applied ? 'applied' : 'proposed',
          sessionId,
          model,
          reason,
          at: ev.at,
        }
      }
    }
    if (calibration && judgeRules && proposal) break
  }
  return { calibration, judgeRules, proposal }
}
