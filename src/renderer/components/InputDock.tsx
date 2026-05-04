import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import { matchSlash, type SlashCommand } from '../lib/slash'

/** Auto-grow a textarea to fit its content, up to a max height in px. */
function resizeTextarea(el: HTMLTextAreaElement, maxPx: number) {
  el.style.height = 'auto'
  const next = Math.min(maxPx, el.scrollHeight)
  el.style.height = `${next}px`
  el.style.overflowY = el.scrollHeight > maxPx ? 'auto' : 'hidden'
}

/** Find a `@word` token at the current caret position, if any. */
function detectAtToken(
  text: string,
  caret: number,
): { start: number; end: number; query: string } | null {
  let i = caret - 1
  while (i >= 0) {
    const ch = text[i]
    if (ch === '@') {
      // Must be at start of string or after whitespace
      if (i > 0 && !/\s/.test(text[i - 1])) return null
      const end = i + 1 + (text.slice(i + 1).match(/^[\w./\-]*/)?.[0].length ?? 0)
      if (end < caret) return null
      return { start: i, end, query: text.slice(i + 1, end) }
    }
    if (/\s/.test(ch)) return null
    i -= 1
  }
  return null
}

export function InputDock() {
  const draft = useHarness((s) => s.inputDraft)
  const setDraft = useHarness((s) => s.setInputDraft)
  const send = useHarness((s) => s.sendMessage)
  const isStreaming = useHarness((s) => s.isStreaming)
  const cancel = useHarness((s) => s.cancelTurn)
  const runSlashCommand = useHarness((s) => s.runSlashCommand)
  const showToast = useHarness((s) => s.showToast)
  const attachImage = useHarness((s) => s.attachImage)
  const removeAttachment = useHarness((s) => s.removeAttachment)
  const pendingAttachments = useHarness((s) => s.pendingAttachments)
  const fileMatches = useHarness((s) => s.fileMatches)
  const requestFileMatches = useHarness((s) => s.requestFileMatches)

  const [focused, setFocused] = useState(false)
  const [selectedSuggestion, setSelectedSuggestion] = useState(0)
  const [caret, setCaret] = useState(0)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const MAX_PX = 260

  const slashSuggestions: SlashCommand[] = useMemo(() => {
    if (!draft.startsWith('/')) return []
    return matchSlash(draft.split(/\s+/)[0])
  }, [draft])

  // Detect the active @ token at the current caret position.
  const atToken = useMemo(() => detectAtToken(draft, caret), [draft, caret])

  // Fire a list_files request (debounced via the focus effect below).
  useEffect(() => {
    if (!atToken) return
    const handle = window.setTimeout(() => {
      requestFileMatches(atToken.query)
    }, 80)
    return () => window.clearTimeout(handle)
  }, [atToken?.query, requestFileMatches])

  useEffect(() => {
    if (draft.length === 0) setSelectedSuggestion(0)
  }, [draft])

  // Grow textarea as the user types or as the draft is set externally (e.g.
  // from a jump-off prompt button).
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    resizeTextarea(el, MAX_PX)
  }, [draft])

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const submit = async () => {
    const content = draft.trim()
    if (!content && pendingAttachments.length === 0) return
    if (content.startsWith('/')) {
      const space = content.indexOf(' ')
      const name = space === -1 ? content : content.slice(0, space)
      const args = space === -1 ? '' : content.slice(space + 1)
      const handled = runSlashCommand(name, args)
      if (handled) {
        setDraft('')
        return
      }
      showToast(`unknown command: ${name}`, 'warn')
      setDraft('')
      return
    }
    await send(content)
  }

  const insertFilePath = (path: string) => {
    if (!atToken) return
    const before = draft.slice(0, atToken.start)
    const after = draft.slice(atToken.end)
    // Wrap the path in backticks so it reads naturally for the model
    // alongside the surrounding prose.
    const inserted = `\`${path}\``
    const nextDraft = `${before}${inserted} ${after}`
    setDraft(nextDraft)
    setSelectedSuggestion(0)
    const nextCaret = before.length + inserted.length + 1
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (el) {
        el.focus()
        el.selectionStart = el.selectionEnd = nextCaret
        setCaret(nextCaret)
      }
    })
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (slashSuggestions.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedSuggestion((i) => (i + 1) % slashSuggestions.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedSuggestion((i) => (i - 1 + slashSuggestions.length) % slashSuggestions.length)
        return
      }
      if (e.key === 'Tab') {
        // Tab only autocompletes — keeps the cursor in the textarea so
        // the user can type args after the command name.
        e.preventDefault()
        const picked = slashSuggestions[selectedSuggestion]
        if (picked) {
          setDraft(picked.name + ' ')
          setSelectedSuggestion(0)
        }
        return
      }
      if (e.key === 'Enter' && !e.shiftKey) {
        // Enter *executes* the highlighted suggestion directly. Previous
        // behavior re-inserted the command name and returned without
        // running it, leaving the user stuck on a chatbox that wouldn't
        // submit zero-arg commands like /usage.
        e.preventDefault()
        const picked = slashSuggestions[selectedSuggestion]
        if (picked) {
          const trimmed = draft.trim()
          // If the user already typed `/cmd some args`, preserve those args;
          // otherwise execute with no args.
          const matchesPicked =
            trimmed === picked.name || trimmed.startsWith(picked.name + ' ')
          const args =
            matchesPicked && trimmed.length > picked.name.length
              ? trimmed.slice(picked.name.length + 1)
              : ''
          const handled = runSlashCommand(picked.name, args)
          if (handled) {
            setDraft('')
            setSelectedSuggestion(0)
          } else {
            showToast(`unknown command: ${picked.name}`, 'warn')
            setDraft('')
          }
          return
        }
      }
    }
    if (atToken && fileMatches.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedSuggestion((i) => (i + 1) % fileMatches.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedSuggestion((i) => (i - 1 + fileMatches.length) % fileMatches.length)
        return
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault()
        const picked = fileMatches[selectedSuggestion]
        if (picked) {
          insertFilePath(picked.path)
          return
        }
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        // Collapse the popup by deleting the lone @ char if it's still
        // floating; otherwise just move caret past the token.
        setSelectedSuggestion(0)
        setCaret(-1)
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const onSelect = (e: React.SyntheticEvent<HTMLTextAreaElement>) => {
    setCaret(e.currentTarget.selectionStart ?? 0)
  }

  const onPaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items
    if (!items) return
    const images: File[] = []
    for (let i = 0; i < items.length; i++) {
      const it = items[i]
      if (it.kind === 'file' && it.type.startsWith('image/')) {
        const file = it.getAsFile()
        if (file) images.push(file)
      }
    }
    if (images.length > 0) {
      e.preventDefault()
      for (const img of images) {
        await attachImage(img)
      }
      showToast(`${images.length} image${images.length > 1 ? 's' : ''} attached`, 'ok')
    }
    // If it's plain text, let the browser insert normally. The textarea
    // onChange will fire and we'll auto-resize.
  }

  const onDrop = async (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    const files = Array.from(e.dataTransfer?.files ?? [])
    const imgs = files.filter((f) => f.type.startsWith('image/'))
    if (imgs.length === 0) return
    for (const img of imgs) await attachImage(img)
    showToast(`${imgs.length} image${imgs.length > 1 ? 's' : ''} attached`, 'ok')
  }

  const onDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    if (Array.from(e.dataTransfer?.items ?? []).some((i) => i.kind === 'file')) {
      e.preventDefault()
    }
  }

  return (
    <div className="relative shrink-0">
      {slashSuggestions.length > 0 && (
        <div className="absolute bottom-full left-1/2 z-20 mb-3 w-[560px] -translate-x-1/2 rounded-xl glass-strong p-1.5 shadow-2xl ring-hairline-strong">
          <div className="px-2 py-1.5 label">slash commands</div>
          <div className="max-h-[280px] overflow-y-auto">
            {slashSuggestions.map((c, i) => (
              <button
                key={c.name}
                onClick={() => {
                  setDraft(c.name + ' ')
                  inputRef.current?.focus()
                }}
                onMouseEnter={() => setSelectedSuggestion(i)}
                className={`flex w-full items-center gap-3 rounded-md px-2.5 py-2 text-left text-[12px] ${
                  i === selectedSuggestion ? 'bg-accent/15 text-fg-0' : 'text-fg-1 hover:bg-white/[0.04]'
                }`}
              >
                <span className="text-accent">{c.name}</span>
                <span className="flex-1 text-fg-2">{c.description}</span>
                {c.keys && <span className="text-[10px] text-fg-2">{c.keys}</span>}
              </button>
            ))}
          </div>
        </div>
      )}
      {atToken && fileMatches.length > 0 && slashSuggestions.length === 0 && (
        <div className="absolute bottom-full left-1/2 z-20 mb-3 w-[560px] -translate-x-1/2 rounded-xl glass-strong p-1.5 shadow-2xl ring-hairline-strong">
          <div className="flex items-center justify-between px-2 py-1.5 label">
            <span>files{atToken.query ? ` · @${atToken.query}` : ''}</span>
            <span className="text-[9.5px] text-fg-3">{fileMatches.length}</span>
          </div>
          <div className="max-h-[280px] overflow-y-auto">
            {fileMatches.map((m, i) => {
              const lastSlash = m.path.lastIndexOf('/')
              const dir = lastSlash > 0 ? m.path.slice(0, lastSlash + 1) : ''
              return (
                <button
                  key={m.path}
                  onClick={() => insertFilePath(m.path)}
                  onMouseEnter={() => setSelectedSuggestion(i)}
                  className={`flex w-full items-center gap-2 rounded-md px-2.5 py-[7px] text-left text-[12px] ${
                    i === selectedSuggestion
                      ? 'bg-accent/15 text-fg-0'
                      : 'text-fg-1 hover:bg-white/[0.04]'
                  }`}
                >
                  <span className="text-[11.5px] text-accent">{m.name}</span>
                  <span className="truncate text-[10px] text-fg-2">{dir}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      <div className="cradle font-prose px-4 py-[6px]" onDrop={onDrop} onDragOver={onDragOver}>
        <div className="mx-auto w-full max-w-[820px]">
          {/* Attachment tray */}
          {pendingAttachments.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {pendingAttachments.map((a) => (
                <div
                  key={a.id}
                  className="group relative flex items-center gap-2 rounded-lg glass-raised px-2 py-1.5"
                >
                  <img
                    src={a.previewUrl}
                    alt="attachment"
                    className="h-10 w-10 rounded object-cover ring-hairline"
                  />
                  <div className="flex flex-col">
                    <span className="text-[10.5px] text-fg-0">image</span>
                    <span className="text-[9.5px] text-fg-2">{a.mimeType}</span>
                  </div>
                  <button
                    onClick={() => removeAttachment(a.id)}
                    className="ml-1 rounded bg-white/[0.06] px-1.5 py-[2px] text-[10px] text-fg-1 ring-hairline hover:bg-danger/20 hover:text-danger"
                    title="Remove"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}

          <div
            className={`flex items-start gap-3 rounded-lg px-3 py-2.5 transition-all ${
              focused
                ? 'glass-raised ring-1 ring-accent/30 shadow-glow-accent'
                : 'glass-raised'
            }`}
          >
            <span
              className="select-none text-[13px] text-accent"
              style={{ lineHeight: '19.375px' }}
            >
              ❯
            </span>
            <textarea
              ref={inputRef}
              value={draft}
              rows={1}
              onChange={(e) => {
                setDraft(e.target.value)
                setCaret(e.target.selectionStart ?? e.target.value.length)
              }}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
              onKeyDown={onKeyDown}
              onKeyUp={onSelect}
              onClick={onSelect}
              onSelect={onSelect}
              onPaste={onPaste}
              placeholder={
                pendingAttachments.length > 0
                  ? 'Add a caption or send as-is'
                  : 'Type @ to mention files, / for commands, paste images to attach'
              }
              className="min-h-[22px] flex-1 resize-none bg-transparent text-[12.5px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
              style={{ lineHeight: 1.55, maxHeight: `${MAX_PX}px` }}
            />
          </div>
          <div className="font-mono mt-2 flex items-center justify-between px-1 text-[10.5px] text-fg-3">
            <div className="flex items-center gap-3">
              <span>
                <kbd className="kbd">⌘</kbd>
                <kbd className="kbd ml-1">K</kbd> palette
              </span>
              <span>
                <kbd className="kbd">⌘</kbd>
                <kbd className="kbd ml-1">N</kbd> new
              </span>
              <span>
                <kbd className="kbd">⌘</kbd>
                <kbd className="kbd ml-1">O</kbd> subagents
              </span>
              <span>
                <kbd className="kbd">⌘</kbd>
                <kbd className="kbd ml-1">V</kbd> paste image
              </span>
            </div>
            <div className="flex items-center gap-2">
              {isStreaming ? (
                <button
                  onClick={() => cancel()}
                  title="Force-cancel this turn and every sub-agent running under it. Also bound to ⎋."
                  className="rounded-md bg-danger/15 px-2 py-[2px] text-[10.5px] text-danger ring-1 ring-danger/30 hover:bg-danger/25"
                >
                  ■ force cancel (esc)
                </button>
              ) : (
                <span>~/work/services/freyja (main*)</span>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
