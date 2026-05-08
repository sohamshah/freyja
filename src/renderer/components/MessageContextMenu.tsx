import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

export type MessageMenuAction = 'edit' | 'rerun' | 'delete' | 'branch'

interface MessageContextMenuProps {
  /** Anchor in viewport coordinates (e.g. clientX/clientY from the
   *  contextmenu event). */
  x: number
  y: number
  /** Whether edit/rerun should be enabled. False for assistant messages
   *  per the agreed UX: only user messages are editable/rerunnable. */
  isUserMessage: boolean
  /** Whether actions are temporarily blocked (e.g. a turn is streaming). */
  busy?: boolean
  onPick: (action: MessageMenuAction) => void
  onClose: () => void
}

/**
 * Right-click context menu rendered in a portal so it can spill out of
 * any clipped scroll containers. Closes on outside click / Escape /
 * scroll. Positions itself flush to the cursor and flips to fit inside
 * the viewport edges.
 */
export function MessageContextMenu({
  x,
  y,
  isUserMessage,
  busy,
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
    const onScroll = () => onClose()
    window.addEventListener('mousedown', onDocMouseDown, true)
    window.addEventListener('keydown', onKey, true)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      window.removeEventListener('mousedown', onDocMouseDown, true)
      window.removeEventListener('keydown', onKey, true)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [onClose])

  const items: Array<{
    action: MessageMenuAction
    label: string
    hint?: string
    disabled?: boolean
  }> = [
    {
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
      action: 'delete',
      label: 'delete',
      hint: 'rewind to here',
      disabled: busy,
    },
    {
      action: 'branch',
      label: 'branch',
      hint: 'fork into new session',
      disabled: busy,
    },
  ]

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      onContextMenu={(e) => e.preventDefault()}
      className="fixed z-[60] min-w-[176px] rounded-md menu-opaque p-1 ring-hairline-strong shadow-2xl"
      style={{ left: pos.left, top: pos.top }}
    >
      {items.map((item, i) => (
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
          } ${i === 2 ? 'mt-0.5 hairline-t pt-2' : ''}`}
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
