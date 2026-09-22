import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

interface BranchSessionDialogProps {
  /** Default name pre-filled in the input. */
  defaultName: string
  /** 'before-message': everything before message #branchAtHumanIndex.
   *  'whole': the session as it stands right now. */
  mode?: 'before-message' | 'whole'
  /** 1-indexed message number for the human-readable hint. */
  branchAtHumanIndex?: number
  onCancel: () => void
  onConfirm: (name: string) => void
}

/**
 * Tiny modal that asks for a name before kicking off a session fork.
 * The bridge clones the engine transcript, every sidecar (goal, inbox,
 * sub-agent re-wake records, task + kanban journals) and the session's
 * project folder (artifacts, ledger, working memory) under new ids; the
 * renderer seeds and persists the UI slice so the fork opens with its
 * tool timeline and sub-agent cards intact. Workspace files on disk are
 * shared, not copied. Portaled to <body> so it can be mounted from
 * anywhere (conversation, sidebar row) without clipping.
 */
export function BranchSessionDialog({
  defaultName,
  mode = 'before-message',
  branchAtHumanIndex,
  onCancel,
  onConfirm,
}: BranchSessionDialogProps) {
  const [name, setName] = useState(defaultName)
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onCancel()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  const submit = () => {
    const trimmed = name.trim()
    if (!trimmed) return
    onConfirm(trimmed)
  }

  const scope =
    mode === 'whole' || branchAtHumanIndex == null
      ? 'Copy this session as it stands right now'
      : `Copy everything before message #${branchAtHumanIndex}`

  return createPortal(
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-6">
      <div
        className="absolute inset-0 bg-black/55 backdrop-blur-[3px]"
        onClick={onCancel}
      />
      <div className="relative w-[min(440px,94vw)] rounded-2xl modal-opaque p-5 ring-hairline-strong shadow-2xl">
        <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.2em] text-fg-2">
          {mode === 'whole' ? 'fork session' : 'branch session'}
        </div>
        <div className="mb-3 font-prose text-[12.5px] leading-[1.45] text-fg-1">
          {scope} into a new session. Context, sub-agent runs, goal state,
          task board and the session&apos;s project folder (artifacts, ledger,
          working memory) are cloned; workspace files on disk stay shared.
        </div>
        <input
          ref={inputRef}
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submit()
            }
          }}
          placeholder={mode === 'whole' ? 'fork name' : 'branch name'}
          className="w-full rounded-md bg-white/[0.04] px-3 py-2 font-prose text-[12.5px] text-fg-0 ring-hairline placeholder:text-fg-3 focus:outline-none focus:ring-1 focus:ring-accent/40"
        />
        <div className="mt-4 flex items-center justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
          >
            cancel
          </button>
          <button
            onClick={submit}
            disabled={!name.trim()}
            className="rounded-md bg-accent/15 px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30 hover:bg-accent/25 disabled:opacity-60"
          >
            {mode === 'whole' ? 'create fork' : 'create branch'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
