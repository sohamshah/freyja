import { useEffect, useRef, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useHarness, type SessionSlice } from '../state/store'
import { renderMarkdown } from '../lib/markdown'
import { formatDuration, formatTokens } from '../lib/format'
import { Conversation } from './Conversation'
import { ToolCallChip } from './ToolCallChip'
import { Widget } from './Widget'
import type { Message, MessagePart, SessionSnapshot, SubagentRecord } from '@shared/events'

export function SessionPanes() {
  const panes = useHarness((s) => s.sessionPanes)
  const activePaneId = useHarness((s) => s.activePaneId)
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const setActivePane = useHarness((s) => s.setActiveSessionPane)
  const closePane = useHarness((s) => s.closeSessionPane)
  const switchSession = useHarness((s) => s.switchSession)

  const visiblePanes = panes.length > 0
    ? panes
    : [{ id: 'pane-main', sessionId: activeSessionId, createdAt: Date.now() }]

  if (visiblePanes.length === 1 && visiblePanes[0].sessionId === activeSessionId) {
    return <Conversation />
  }

  const handlePaneFocus = (paneId: string, sessionId: string) => {
    setActivePane(paneId)
    if (sessionId !== activeSessionId) {
      switchSession(sessionId).catch(() => {})
    }
  }

  return (
    <div className="flex min-h-0 flex-1 gap-2 overflow-x-auto pb-1">
      {visiblePanes.map((pane) => (
        <SessionPane
          key={pane.id}
          paneId={pane.id}
          sessionId={pane.sessionId}
          active={pane.id === activePaneId}
          writable={pane.sessionId === activeSessionId}
          paneCount={visiblePanes.length}
          onFocus={() => handlePaneFocus(pane.id, pane.sessionId)}
          onClose={() => closePane(pane.id)}
        />
      ))}
    </div>
  )
}

function SessionPane({
  paneId,
  sessionId,
  active,
  writable,
  paneCount,
  onFocus,
  onClose,
}: {
  paneId: string
  sessionId: string
  active: boolean
  writable: boolean
  paneCount: number
  onFocus: () => void
  onClose: () => void
}) {
  const snapshot = useHarness((s) => s.sessions.find((session) => session.id === sessionId))
  const slice = useHarness(useShallow((s) => sliceForSessionView(s, sessionId)))

  return (
    <section
      data-pane-id={paneId}
      onMouseDown={onFocus}
      className={`glass-panel flex min-w-[420px] flex-1 flex-col overflow-hidden rounded-[18px] ring-hairline ${
        active ? 'session-pane-active' : ''
      }`}
    >
      <PaneHeader
        snapshot={snapshot}
        slice={slice}
        paneCount={paneCount}
        onClose={onClose}
      />
      <PaneTranscript
        snapshot={snapshot}
        slice={slice}
        writable={writable}
      />
      <PaneChatbox sessionId={sessionId} writable={writable} onFocus={onFocus} />
    </section>
  )
}

function PaneHeader({
  snapshot,
  slice,
  paneCount,
  onClose,
}: {
  snapshot?: SessionSnapshot
  slice?: SessionSlice
  paneCount: number
  onClose: () => void
}) {
  const model = slice?.model || snapshot?.model || 'session'
  const streaming = slice?.isStreaming
  const count = safeArray<Message>(slice?.messages).length || snapshot?.messageCount || 0
  const usage = slice?.usage
  const context = usage?.currentContextTokens || usage?.totalInputTokens || snapshot?.totalInputTokens || 0

  return (
    <div className="hairline-b flex h-[42px] shrink-0 items-center gap-2 px-3">
      <span className={`h-1.5 w-1.5 rounded-full ${streaming ? 'bg-accent' : 'bg-fg-3'}`} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12px] text-fg-0">
          {snapshot?.title ?? 'Loading session'}
        </div>
        <div className="mt-[1px] flex items-center gap-1.5 font-mono text-[9.5px] text-fg-3">
          <span>{model.replace('claude-', '')}</span>
          <span>·</span>
          <span>{count} msg</span>
          {context > 0 && (
            <>
              <span>·</span>
              <span>{formatTokens(context)}</span>
            </>
          )}
        </div>
      </div>
      <div className="no-drag flex items-center gap-1">
        {paneCount > 1 && (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              onClose()
            }}
            className="rounded-md bg-white/[0.035] px-2 py-1 font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.07] hover:text-fg-0"
            title="Close split pane"
          >
            ×
          </button>
        )}
      </div>
    </div>
  )
}

function PaneTranscript({
  snapshot,
  slice,
  writable,
}: {
  snapshot?: SessionSnapshot
  slice?: SessionSlice
  writable: boolean
}) {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const messages = safeArray<Message>(slice?.messages)

  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages.length, slice?.thinking, slice?.isStreaming])

  if (!slice) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-center text-[12px] text-fg-2">
        Session state is not loaded yet.
      </div>
    )
  }

  if (messages.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-center text-[12px] text-fg-2">
        <div>
          <div className="mb-2 label">{writable ? 'active session' : 'split view'}</div>
          <div>{snapshot?.task || 'No messages yet.'}</div>
        </div>
      </div>
    )
  }

  return (
    <div ref={scrollerRef} className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
      <div className="space-y-5 px-5 py-5">
        {messages.map((message) => (
          <PaneMessage key={message.id} message={message} slice={slice} />
        ))}
      </div>
    </div>
  )
}

function PaneMessage({
  message,
  slice,
}: {
  message: Message
  slice: SessionSlice
}) {
  if (message.role === 'user') {
    const parts = safeArray<MessagePart>(message.parts)
    const attachments = safeArray(message.attachments)
    const text = parts
      .filter((part) => part.type === 'text')
      .map((part) => part.text ?? '')
      .join('')
    return (
      <div className="flex flex-col items-end gap-2">
        {attachments.length > 0 && (
          <div className="flex max-w-[86%] flex-wrap justify-end gap-1.5">
            {attachments.map((attachment) => (
              <img
                key={attachment.id}
                src={attachment.previewUrl}
                alt="attached"
                className="max-h-[120px] rounded-md ring-1 ring-accent/20"
                loading="lazy"
                decoding="async"
                draggable={false}
              />
            ))}
          </div>
        )}
        {text && (
          <div className="selectable max-w-[86%] rounded-lg bg-accent/10 px-3 py-2 font-prose text-[12px] leading-[1.55] text-fg-0 ring-1 ring-accent/20">
            {text}
          </div>
        )}
      </div>
    )
  }

  return (
    <div>
      <div className="mb-1.5 flex items-center gap-2 label">
        <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent" />
        assistant
      </div>
      <div className="space-y-2.5">
        {safeArray<MessagePart>(message.parts).map((part, index) => (
          <PanePart key={index} part={part} slice={slice} />
        ))}
      </div>
    </div>
  )
}

function PanePart({ part, slice }: { part: MessagePart; slice: SessionSlice }) {
  if (part.type === 'text' && part.text) {
    const html = renderMarkdown(part.text)
    return (
      <div
        className="md selectable text-[12.5px]"
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{ __html: html }}
      />
    )
  }

  if (part.type === 'thinking' && part.text) {
    return (
      <div className="max-h-[220px] overflow-auto rounded-lg bg-white/[0.025] p-3 font-mono text-[11px] leading-[1.55] text-fg-2 ring-hairline">
        <div className="mb-1 label">thinking</div>
        <div className="selectable whitespace-pre-wrap">{part.text}</div>
      </div>
    )
  }

  if (part.type === 'tool_call' && part.toolCallId) {
    const tcRecord = slice.toolCalls?.[part.toolCallId]
    const widget = slice.widgets?.[part.toolCallId]
    const widgetStreaming = !!tcRecord && tcRecord.status === 'running'
    // For `show_widget` calls, hide the tool chip and render the
    // widget chromeless — the widget IS the artifact; the chip
    // duplicates info nobody cares about. While the call is still in
    // flight we mount the Widget in its loading state. Errored calls
    // (status=error with no widget) fall through to the chip so the
    // operator sees what failed.
    if (tcRecord?.name === 'show_widget' && (widget || widgetStreaming)) {
      return (
        <Widget
          toolCallId={part.toolCallId}
          title={widget?.title ?? 'widget'}
          code={widget?.code ?? ''}
          kind={widget?.kind ?? 'html'}
          loadingMessages={widget?.loadingMessages ?? []}
          isStreaming={widgetStreaming || !widget}
        />
      )
    }
    return <ToolCallChip id={part.toolCallId} record={tcRecord} />
  }

  if (part.type === 'subagent' && part.subagentId) {
    const sub = slice.subagents?.[part.subagentId]
    return <PaneSubagent sub={sub} sessionId={part.subagentId} />
  }

  if (part.type === 'system') {
    return (
      <div className="flex items-center gap-2 rounded-md bg-white/[0.025] px-2.5 py-1.5 text-[11px] text-fg-2 ring-hairline">
        <span className="font-mono text-[10px] uppercase text-warn/80">{part.systemSubtype}</span>
        <span>{part.text}</span>
      </div>
    )
  }

  return null
}

function PaneSubagent({ sub, sessionId }: { sub?: SubagentRecord; sessionId: string }) {
  const snapshot = useHarness((s) => s.sessions.find((session) => session.id === sessionId))
  const openPane = useHarness((s) => s.openSessionPane)
  const label = sub?.label ?? snapshot?.title ?? sessionId
  const state = sub?.state ?? (snapshot?.completed ? 'done' : 'running')
  const tools = sub?.toolsCalled ?? 0
  const elapsed = sub?.elapsedMs ?? ((snapshot?.updatedAt ?? Date.now()) - (snapshot?.createdAt ?? Date.now()))

  return (
    <div className="rounded-xl glass-raised p-3">
      <div className="flex items-start gap-2">
        <span className={`mt-[5px] h-1.5 w-1.5 rounded-full ${state === 'done' ? 'bg-ok' : state === 'failed' ? 'bg-danger' : 'bg-accent'}`} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[12px] text-fg-0">{label}</div>
          <div className="mt-1 line-clamp-2 text-[11px] leading-[1.45] text-fg-2">
            {sub?.task ?? snapshot?.task ?? 'Sub-agent session'}
          </div>
          <div className="mt-2 flex gap-3 font-mono text-[10px] text-fg-3">
            <span>{state}</span>
            <span>{tools} tools</span>
            <span>{formatDuration(elapsed)}</span>
          </div>
        </div>
        {snapshot && (
          <div className="flex shrink-0 gap-1">
            <button
              type="button"
              onClick={() => openPane(sessionId, 'split')}
              className="rounded-md bg-white/[0.04] px-2 py-1 font-mono text-[9.5px] uppercase text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              split
            </button>
            <button
              type="button"
              onClick={() => openPane(sessionId, 'replace')}
              className="rounded-md bg-accent/10 px-2 py-1 font-mono text-[9.5px] uppercase text-accent ring-1 ring-accent/20 hover:bg-accent/18"
            >
              open
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

function PaneChatbox({
  sessionId,
  writable,
  onFocus,
}: {
  sessionId: string
  writable: boolean
  onFocus: () => void
}) {
  const draft = useHarness((s) => (writable ? s.inputDraft : ''))
  const setDraft = useHarness((s) => s.setInputDraft)
  const sendMessage = useHarness((s) => s.sendMessage)
  const operatorTalk = useHarness((s) => s.operatorTalk)
  const isStreaming = useHarness((s) => (writable ? s.isStreaming : false))
  const cancel = useHarness((s) => s.cancelTurn)
  const [localDraft, setLocalDraft] = useState('')
  const [focused, setFocused] = useState(false)
  // Force toggle for non-writable panes — interrupts the recipient
  // mid-operation (cancels their current LLM stream / tool call so they
  // process this message immediately). Auto-resets to off after each
  // send so urgency stays a deliberate choice.
  const [forceArmed, setForceArmed] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const value = writable ? draft : localDraft

  const onChange = (next: string) => {
    if (writable) setDraft(next)
    else setLocalDraft(next)
  }

  const submit = async () => {
    const content = value.trim()
    if (!content) return
    if (writable) {
      // Active root session — normal turn pipeline.
      await sendMessage(content)
      return
    }
    // Non-active session — route as operator talk (root or sub-agent
    // — bridge's TalkRouter handles either). DOES NOT switch active
    // session; the operator stays where they are.
    setLocalDraft('')
    await operatorTalk(sessionId, content, forceArmed)
    setForceArmed(false)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div
      className={`hairline-t flex shrink-0 items-start gap-2 px-3 py-2 transition-shadow ${
        focused ? 'shadow-[inset_0_1px_0_rgba(168,212,252,0.18)]' : ''
      }`}
      onMouseDown={(event) => {
        event.stopPropagation()
        onFocus()
      }}
    >
      <span
        className="select-none pt-[3px] text-[12px] text-accent"
        style={{ lineHeight: '17px' }}
      >
        {writable ? '❯' : '↳'}
      </span>
      <textarea
        ref={inputRef}
        rows={1}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => {
          setFocused(true)
          onFocus()
        }}
        onBlur={() => setFocused(false)}
        onKeyDown={onKeyDown}
        placeholder={writable ? 'Reply…' : 'Send to this session…'}
        className="min-h-[20px] max-h-[160px] flex-1 resize-none bg-transparent font-prose text-[12px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
        style={{ lineHeight: 1.5 }}
      />
      {!writable && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            setForceArmed((v) => !v)
          }}
          className={`shrink-0 rounded-md px-2 py-1 font-mono text-[9.5px] uppercase tracking-[0.12em] ring-1 transition ${
            forceArmed
              ? 'bg-warn/15 text-warn ring-warn/30 hover:bg-warn/25'
              : 'bg-white/[0.025] text-fg-3 ring-white/[0.06] hover:bg-white/[0.06] hover:text-fg-1'
          }`}
          title={forceArmed
            ? 'Force armed — next message interrupts the recipient mid-operation'
            : 'Click to arm force — interrupts recipient mid-operation'}
        >
          force
        </button>
      )}
      {isStreaming && writable ? (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            cancel()
          }}
          className="shrink-0 rounded-md bg-danger/15 px-2 py-1 font-mono text-[9.5px] uppercase text-danger ring-1 ring-danger/30 hover:bg-danger/25"
          title="Force-cancel this turn"
        >
          stop
        </button>
      ) : null}
    </div>
  )
}

function sliceForSessionView(
  state: ReturnType<typeof useHarness.getState>,
  sessionId: string,
): SessionSlice | undefined {
  if (sessionId === state.activeSessionId) {
    return {
      messages: state.messages,
      currentStreamingMessageId: state.currentStreamingMessageId,
      currentTurnId: state.currentTurnId,
      thinking: state.thinking,
      isStreaming: state.isStreaming,
      toolCalls: state.toolCalls,
      toolCallOrder: state.toolCallOrder,
      fileChanges: state.fileChanges,
      subagents: state.subagents,
      subagentOrder: state.subagentOrder,
      usage: state.usage,
      systemEvents: state.systemEvents,
      kanbanCards: state.kanbanCards,
      busMessages: state.busMessages,
      inboxEvents: state.inboxEvents,
      artifacts: state.artifacts,
      widgets: state.widgets,
      autoDispatchEnabled: state.autoDispatchEnabled,
      model: state.model,
      reasoningLevel: state.reasoningLevel,
      coordinationStrategy: state.coordinationStrategy,
      systemPrompt: state.systemPrompt,
    }
  }
  return state.sessionArchive[sessionId]
}

function safeArray<T>(value: T[] | undefined | null): T[] {
  return Array.isArray(value) ? value : []
}
