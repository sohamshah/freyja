import { forwardRef, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useVirtualList } from '../lib/useVirtualList'
import type { ArtifactIndexRow, ArtifactReadResult } from '@shared/events'

/**
 * Line-addressable source view for one artifact: read, annotate, edit.
 *
 * Annotating is the point. Selecting a range of lines and writing a comment
 * sends that comment to whichever session most recently touched the file, so a
 * review turns straight back into work for the agent that produced the thing
 * — no hunting for which of a thousand sessions that was.
 *
 * Editing writes through the `artifact:write` IPC, which had been wired end to
 * end since the session library shipped and never had a caller. The save
 * carries the hash of exactly what was displayed: if an agent wrote to the
 * file while the editor was open, the save is refused rather than winning
 * silently.
 */

const LINE_HEIGHT = 18

/** Above this, rendering every line at once stops being free. */
const VIRTUALIZE_ABOVE = 400

/** A quote longer than this bloats the note without adding context. */
const MAX_QUOTE_CHARS = 2000

export function ArtifactSource({
  row,
  editing,
  onEditing,
  onSaved,
  onNoteCreated,
  onDirtyChange,
  escapeSlot,
}: {
  row: ArtifactIndexRow
  editing: boolean
  onEditing: (v: boolean) => void
  onSaved: () => void
  onNoteCreated: () => void
  /** Raised while the editor holds unsaved text, so the browser can refuse to
   *  move the selection out from under it. */
  onDirtyChange?: (dirty: boolean) => void
  /** The browser calls whatever we park here before it acts on Escape. Return
   *  true to consume the key. */
  escapeSlot?: React.MutableRefObject<(() => boolean) | null>
}) {
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'error'; message: string }
    | { kind: 'ready'; content: string; sha256: string | null; binary: boolean }
  >({ kind: 'loading' })

  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const [anchor, setAnchor] = useState<number | null>(null)
  const [head, setHead] = useState<number | null>(null)
  const [composing, setComposing] = useState(false)
  const [comment, setComment] = useState('')
  const [sending, setSending] = useState(false)
  const [sendResult, setSendResult] = useState<string | null>(null)

  const commentRef = useRef<HTMLTextAreaElement>(null)

  // ── load ───────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false
    const api = (window as any).harness
    if (!api?.artifactRead) {
      setState({ kind: 'error', message: 'Artifact IPC unavailable' })
      return
    }
    setState({ kind: 'loading' })
    setAnchor(null)
    setHead(null)
    setComposing(false)
    setSendResult(null)
    setSaveError(null)
    api.artifactRead(row.path).then((result: ArtifactReadResult) => {
      if (cancelled) return
      if (!result.ok) {
        setState({ kind: 'error', message: result.error ?? 'Unknown error' })
        return
      }
      if (result.binary) {
        setState({ kind: 'ready', content: '', sha256: result.sha256 ?? null, binary: true })
        return
      }
      const content = result.content ?? ''
      setState({ kind: 'ready', content, sha256: result.sha256 ?? null, binary: false })
      setDraft(content)
    })
    return () => {
      cancelled = true
    }
  }, [row.path])

  const content = state.kind === 'ready' ? state.content : ''
  const lines = useMemo(() => content.split('\n'), [content])
  const dirty = editing && state.kind === 'ready' && draft !== state.content

  // Report upward so the list can lock while there is unsaved text, and always
  // clear on unmount — a pane that unmounts dirty would otherwise leave the
  // browser permanently locked.
  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])
  useEffect(() => {
    return () => onDirtyChange?.(false)
  }, [onDirtyChange])

  // ── save ───────────────────────────────────────────────────────
  const save = useCallback(async () => {
    if (state.kind !== 'ready' || saving) return
    const api = (window as any).harness
    if (!api?.artifactWrite) {
      setSaveError('Artifact write IPC unavailable')
      return
    }
    setSaving(true)
    setSaveError(null)
    try {
      const res = await api.artifactWrite(row.path, draft, state.sha256)
      if (!res?.ok) {
        setSaveError(res?.error ?? 'Save failed')
        return
      }
      setState({ kind: 'ready', content: draft, sha256: res.sha256 ?? null, binary: false })
      onEditing(false)
      onSaved()
    } catch (err) {
      setSaveError(String(err))
    } finally {
      setSaving(false)
    }
  }, [state, draft, row.path, saving, onEditing, onSaved])

  useEffect(() => {
    if (!editing) return
    const onKey = (e: KeyboardEvent) => {
      const isMac = navigator.platform.toLowerCase().includes('mac')
      const mod = isMac ? e.metaKey : e.ctrlKey
      if (mod && (e.key === 's' || e.key === 'S')) {
        e.preventDefault()
        void save()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [editing, save])

  // ── selection ──────────────────────────────────────────────────
  const range = useMemo(() => {
    if (anchor == null || head == null) return null
    return { start: Math.min(anchor, head), end: Math.max(anchor, head) }
  }, [anchor, head])

  const selectLine = useCallback(
    (index: number, extend: boolean) => {
      if (extend && anchor != null) {
        setHead(index)
      } else {
        setAnchor(index)
        setHead(index)
      }
      setSendResult(null)
    },
    [anchor],
  )

  const quote = useMemo(() => {
    if (!range) return ''
    const text = lines.slice(range.start, range.end + 1).join('\n')
    return text.length > MAX_QUOTE_CHARS
      ? text.slice(0, MAX_QUOTE_CHARS) + '\n…[truncated]'
      : text
  }, [range, lines])

  const submitComment = useCallback(async () => {
    const api = (window as any).harness
    if (!api?.artifactNoteCreate || !comment.trim() || sending) return
    setSending(true)
    setSendResult(null)
    try {
      const res = await api.artifactNoteCreate({
        artifactPath: row.path,
        body: comment.trim(),
        anchor: range
          ? { startLine: range.start + 1, endLine: range.end + 1, quote }
          : null,
      })
      if (!res?.ok) {
        setSendResult(res?.error ?? 'Could not save the comment')
        return
      }
      setComment('')
      setComposing(false)
      setAnchor(null)
      setHead(null)
      onNoteCreated()
      const target = res.note?.targetSessionTitle || res.note?.targetSessionId
      setSendResult(
        res.note?.status === 'delivered'
          ? `sent to ${target}`
          : res.note?.status === 'failed'
          ? `saved, but delivery failed: ${res.note?.error ?? res.error ?? 'unknown'}`
          : 'saved',
      )
    } catch (err) {
      setSendResult(String(err))
    } finally {
      setSending(false)
    }
  }, [comment, range, quote, row.path, sending, onNoteCreated])

  useEffect(() => {
    if (composing) commentRef.current?.focus()
  }, [composing])

  // Escape closes the composer first, and clears a selection second, before
  // the browser gets to close itself. Without this the browser's capture-phase
  // handler swallows the key and a half-written comment goes with it.
  useEffect(() => {
    if (!escapeSlot) return
    escapeSlot.current = () => {
      if (composing) {
        setComposing(false)
        return true
      }
      if (anchor != null) {
        setAnchor(null)
        setHead(null)
        return true
      }
      return false
    }
    return () => {
      escapeSlot.current = null
    }
  }, [escapeSlot, composing, anchor])

  // ── virtualization ─────────────────────────────────────────────
  const virtualize = lines.length > VIRTUALIZE_ABOVE && !editing
  const list = useVirtualList({
    count: virtualize ? lines.length : 0,
    rowHeight: LINE_HEIGHT,
    overscan: 20,
  })

  if (state.kind === 'loading') {
    return <div className="p-6 font-mono text-[11px] italic text-fg-3">loading source…</div>
  }
  if (state.kind === 'error') {
    return (
      <div className="p-6">
        <div className="max-w-[520px] rounded-xl bg-danger/[0.06] p-4 ring-1 ring-danger/30">
          <div className="mb-1.5 font-mono text-[10px] uppercase tracking-[0.1em] text-danger">
            couldn't read this file
          </div>
          <div className="font-mono text-[11.5px] text-fg-1">{state.message}</div>
        </div>
      </div>
    )
  }
  if (state.binary) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center">
        <div className="max-w-[420px] font-prose text-[12px] leading-relaxed text-fg-3">
          This is a binary file, so there is no source to annotate or edit. Use the{' '}
          <span className="font-mono text-fg-2">preview</span> tab to view it, or leave a
          whole-file comment from the button below.
          <div className="mt-3">
            <button
              onClick={() => setComposing(true)}
              className="rounded bg-white/[0.05] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.1]"
            >
              comment on this file
            </button>
          </div>
        </div>
        {composing && (
          <CommentComposer
            ref={commentRef}
            value={comment}
            onChange={setComment}
            onCancel={() => setComposing(false)}
            onSubmit={submitComment}
            sending={sending}
            targetLabel={row.sessionTitle || row.sessionId}
            rangeLabel="the whole file"
          />
        )}
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex shrink-0 items-center gap-2 border-b border-white/[0.06] px-4 py-1.5">
        <span className="font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
          {lines.length} lines
        </span>
        {range && !editing && (
          <span className="font-mono text-[9px] text-accent">
            {range.start === range.end
              ? `line ${range.start + 1}`
              : `lines ${range.start + 1}–${range.end + 1}`}
          </span>
        )}
        {range && !editing && !composing && (
          <button
            onClick={() => setComposing(true)}
            className="rounded bg-accent/15 px-2 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30 hover:bg-accent/25"
          >
            comment
          </button>
        )}
        {sendResult && (
          <span className="truncate font-mono text-[9px] text-ok">{sendResult}</span>
        )}

        <div className="ml-auto flex items-center gap-1.5">
          {editing && dirty && (
            <span className="font-mono text-[9px] text-warn">unsaved</span>
          )}
          {saveError && (
            <span className="max-w-[340px] truncate font-mono text-[9px] text-danger" title={saveError}>
              {saveError}
            </span>
          )}
          {editing ? (
            <>
              <button
                onClick={() => {
                  setDraft(state.content)
                  onEditing(false)
                  setSaveError(null)
                }}
                className="rounded bg-white/[0.04] px-2 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
              >
                discard
              </button>
              <button
                onClick={() => void save()}
                disabled={saving || !dirty}
                className="rounded bg-accent/15 px-2 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30 hover:bg-accent/25 disabled:opacity-40"
              >
                {saving ? 'saving…' : 'save ⌘S'}
              </button>
            </>
          ) : (
            <button
              onClick={() => onEditing(true)}
              className="rounded bg-white/[0.04] px-2 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              edit ⌘E
            </button>
          )}
        </div>
      </div>

      {/* Body */}
      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
          className="min-h-0 flex-1 resize-none bg-transparent px-4 py-2 font-mono text-[11.5px] leading-[18px] text-fg-0 focus:outline-none"
        />
      ) : virtualize ? (
        <div
          ref={list.ref}
          onScroll={list.onScroll}
          className="min-h-0 flex-1 overflow-auto py-2"
        >
          <div style={{ height: list.totalHeight, position: 'relative' }}>
            <div style={{ transform: `translateY(${list.offsetY}px)` }}>
              {lines.slice(list.start, list.end).map((line, i) => (
                <SourceLine
                  key={list.start + i}
                  index={list.start + i}
                  text={line}
                  selected={
                    range != null && list.start + i >= range.start && list.start + i <= range.end
                  }
                  onSelect={selectLine}
                />
              ))}
            </div>
          </div>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto py-2">
          {lines.map((line, i) => (
            <SourceLine
              key={i}
              index={i}
              text={line}
              selected={range != null && i >= range.start && i <= range.end}
              onSelect={selectLine}
            />
          ))}
        </div>
      )}

      {composing && (
        <CommentComposer
          ref={commentRef}
          value={comment}
          onChange={setComment}
          onCancel={() => setComposing(false)}
          onSubmit={submitComment}
          sending={sending}
          targetLabel={row.sessionTitle || row.sessionId}
          rangeLabel={
            range
              ? range.start === range.end
                ? `line ${range.start + 1}`
                : `lines ${range.start + 1}–${range.end + 1}`
              : 'the whole file'
          }
        />
      )}
    </div>
  )
}

function SourceLine({
  index,
  text,
  selected,
  onSelect,
}: {
  index: number
  text: string
  selected: boolean
  onSelect: (index: number, extend: boolean) => void
}) {
  return (
    <div
      onMouseDown={(e) => onSelect(index, e.shiftKey)}
      style={{ height: LINE_HEIGHT }}
      className={`flex cursor-text items-center ${
        selected ? 'bg-accent/[0.14]' : 'hover:bg-white/[0.025]'
      }`}
    >
      <span className="w-[52px] shrink-0 select-none pr-3 text-right font-mono text-[10px] leading-[18px] text-fg-3">
        {index + 1}
      </span>
      <pre className="flex-1 whitespace-pre pr-6 font-mono text-[11.5px] leading-[18px] text-fg-1">
        {text || ' '}
      </pre>
    </div>
  )
}

/** forwardRef so the composer can take focus the moment it opens. */
const CommentComposer = forwardRef<
  HTMLTextAreaElement,
  {
    value: string
    onChange: (v: string) => void
    onCancel: () => void
    onSubmit: () => void
    sending: boolean
    targetLabel: string
    rangeLabel: string
  }
>(function CommentComposer(
  { value, onChange, onCancel, onSubmit, sending, targetLabel, rangeLabel },
  ref,
) {
  return (
    <div className="shrink-0 border-t border-white/[0.08] bg-[#0a0a0e]/80 px-4 py-2.5">
      <div className="mb-1.5 flex items-baseline gap-2 font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
        <span>comment on {rangeLabel}</span>
        <span className="normal-case tracking-normal text-fg-3">
          → goes to <span className="text-fg-1">{targetLabel}</span>
        </span>
        <button onClick={onCancel} className="ml-auto hover:text-fg-0">
          cancel
        </button>
      </div>
      <textarea
        ref={ref}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            onSubmit()
          }
          if (e.key === 'Escape') {
            e.preventDefault()
            e.stopPropagation()
            onCancel()
          }
        }}
        rows={3}
        placeholder="What should change here?"
        className="w-full resize-none rounded-md bg-white/[0.04] px-2.5 py-1.5 font-prose text-[12px] leading-relaxed text-fg-0 ring-hairline placeholder:text-fg-3 focus:outline-none focus:ring-1 focus:ring-accent/40"
      />
      <div className="mt-1.5 flex items-center justify-end gap-2">
        <span className="mr-auto font-mono text-[9px] text-fg-3">⌘↵ to send</span>
        <button
          onClick={onSubmit}
          disabled={sending || !value.trim()}
          className="rounded bg-accent/15 px-2.5 py-[3px] font-mono text-[9.5px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30 hover:bg-accent/25 disabled:opacity-40"
        >
          {sending ? 'sending…' : 'send to session'}
        </button>
      </div>
    </div>
  )
})
