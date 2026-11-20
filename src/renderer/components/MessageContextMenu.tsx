import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

export type MessageMenuAction =
  | 'copy'
  | 'copy-markdown'
  | 'edit'
  | 'rerun'
  | 'delete'
  | 'branch'
  | 'pin'
  | 'unpin'

interface MessageContextMenuProps {
  /** Anchor in viewport coordinates (e.g. clientX/clientY from the
   *  contextmenu event). */
  x: number
  y: number
  /** Whether edit/rerun should be enabled. False for assistant messages
   *  per the agreed UX: only user messages are editable/rerunnable. */
  isUserMessage: boolean
  /** A turn is running. The bridge refuses edit, rerun and delete then;
   *  pin and branch work mid-turn. */
  busy?: boolean
  /** False for a Claude Code / Codex session: its transcript lives in the
   *  external CLI, so there is nothing here to branch. */
  canBranch?: boolean
  /** True when this message is currently pinned (compaction_excluded). */
  isPinned?: boolean
  /** True when text inside this message was selected when the menu opened,
   *  which scopes both copy rows to the selection. */
  hasSelection?: boolean
  onPick: (action: MessageMenuAction) => void
  onClose: () => void
}

/**
 * Right-click context menu rendered in a portal so it can spill out of
 * any clipped scroll containers. Closes on outside click / Escape /
 * the operator scrolling. Positions itself flush to the cursor and flips to fit inside
 * the viewport edges.
 */
export function MessageContextMenu({
  x,
  y,
  isUserMessage,
  busy,
  canBranch = true,
  isPinned,
  hasSelection,
  onPick,
  onClose,
}: MessageContextMenuProps) {
  const menuRef = useRef<HTMLDivElement | null>(null)
  const [pos, setPos] = useState({ left: x, top: y })

  useLayoutEffect(() => {
    const el = menuRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const margin = 8
    let left = x
    let top = y
    if (left + rect.width + margin > window.innerWidth) {
      left = Math.max(margin, window.innerWidth - rect.width - margin)
    }
    if (top + rect.height + margin > window.innerHeight) {
      top = Math.max(margin, window.innerHeight - rect.height - margin)
    }
    setPos({ left, top })
  }, [x, y])

  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent) => {
      const el = menuRef.current
      if (el && e.target instanceof Node && el.contains(e.target)) return
      onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    // Close when the operator scrolls, not on every scroll event: while a
    // reply streams, stick-to-bottom sets scrollTop on each delta, and that
    // closed the menu under the cursor. A scrollbar drag starts with a
    // mousedown, which closes it above.
    const onUserScroll = (e: Event) => {
      const el = menuRef.current
      if (el && e.target instanceof Node && el.contains(e.target)) return
      onClose()
    }
    window.addEventListener('mousedown', onDocMouseDown, true)
    window.addEventListener('keydown', onKey, true)
    window.addEventListener('wheel', onUserScroll, { capture: true, passive: true })
    window.addEventListener('touchmove', onUserScroll, { capture: true, passive: true })
    return () => {
      window.removeEventListener('mousedown', onDocMouseDown, true)
      window.removeEventListener('keydown', onKey, true)
      window.removeEventListener('wheel', onUserScroll, true)
      window.removeEventListener('touchmove', onUserScroll, true)
    }
  }, [onClose])

  const rows: Array<{
    action: MessageMenuAction
    label: string
    hint?: string
    disabled?: boolean
    /** Draw a hairline above this row. */
    separator?: boolean
  }> = [
    // Copy sits at the top and is never blocked by a streaming turn —
    // reading out what's already on screen is always safe. `copy` matches
    // ⌘C (formatted text); `copy as markdown` gives the source.
    {
      action: 'copy',
      label: 'copy',
      hint: hasSelection ? 'selection' : 'message',
    },
    {
      action: 'copy-markdown',
      label: 'copy as markdown',
      hint: hasSelection ? 'selection' : 'message',
    },
    {
      separator: true,
      action: 'edit',
      label: 'edit',
      hint: 'rewrite + rerun',
      disabled: !isUserMessage || busy,
    },
    {
      action: 'rerun',
      label: 'rerun',
      hint: 'replay verbatim',
      disabled: !isUserMessage || busy,
    },
    {
      separator: true,
      action: isPinned ? 'unpin' : 'pin',
      label: isPinned ? 'unpin' : 'pin',
      hint: isPinned ? 'allow compaction again' : 'keep through compactions',
    },
    {
      action: 'delete',
      label: 'delete',
      hint: 'rewind to here',
      disabled: busy,
    },
    {
      action: 'branch',
      label: 'branch',
      hint: 'fork into new session',
    },
  ]
  const items = canBranch ? rows : rows.filter((row) => row.action !== 'branch')

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      onContextMenu={(e) => e.preventDefault()}
      className="fixed z-[60] min-w-[176px] rounded-md menu-opaque p-1 ring-hairline-strong shadow-2xl"
      style={{ left: pos.left, top: pos.top }}
    >
      {items.map((item) => (
        <button
          key={item.action}
          role="menuitem"
          disabled={item.disabled}
          onClick={(e) => {
            e.stopPropagation()
            if (item.disabled) return
            onPick(item.action)
            onClose()
          }}
          className={`flex w-full items-center justify-between gap-3 rounded px-2.5 py-1.5 text-left font-mono text-[11.5px] ${
            item.disabled
              ? 'cursor-default text-fg-3/60'
              : 'text-fg-1 hover:bg-white/[0.06] hover:text-fg-0'
          } ${item.separator ? 'mt-0.5 hairline-t pt-2' : ''}`}
        >
          <span>{item.label}</span>
          {item.hint && (
            <span className="text-[9.5px] uppercase tracking-[0.08em] text-fg-3">
              {item.hint}
            </span>
          )}
        </button>
      ))}
    </div>,
    document.body,
  )
}
