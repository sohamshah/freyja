import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import gsap from 'gsap'
import type { KanbanCardView } from '../shared/types'
import { useHarness } from '../../state/store'
import { VectorFieldBackdrop } from './VectorFieldBackdrop'

/** Modal-shape "+ Add task" form, mounted into document.body via a
 *  portal so the dropdown popovers + the form chrome never get
 *  clipped by an ancestor `overflow-y-auto`. The animated vector
 *  field paints the entire dimmed backdrop, drifting in response to
 *  the cursor — gives the modal a sense of motion + place without
 *  any pixel of decoration touching the form chrome itself.
 *
 *  Behavior:
 *  - Title autofocus on open, Cmd+Enter submits anywhere in the form
 *  - Esc closes; click on the dim backdrop also closes
 *  - After submit: form clears + title re-focuses for one-keystroke
 *    chaining. Manually cancel/close to dismiss.
 *  - Entrance: GSAP fades the backdrop in (140ms) and scales the form
 *    card from 0.96 → 1 (220ms easeOutCubic). Exit reverses the same
 *    timeline so dismiss feels intentional rather than abrupt. */
export function AddTaskForm({
  sessionId,
  open,
  cards,
  onClose,
}: {
  sessionId: string
  open: boolean
  cards: KanbanCardView[]
  onClose: () => void
}) {
  const addOperatorKanbanCard = useHarness((s) => s.addOperatorKanbanCard)
  const titleRef = useRef<HTMLInputElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const backdropRef = useRef<HTMLDivElement>(null)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [priority, setPriority] = useState<'high' | 'med' | 'low'>('med')
  const [assignee, setAssignee] = useState('')
  const [parents, setParents] = useState<string[]>([])
  const [children, setChildren] = useState<string[]>([])
  const [submitting, setSubmitting] = useState(false)

  // GSAP entrance/exit. Runs as a layout effect so the animation
  // starts on the very first paint after the modal mounts — no
  // visible "snap to final state then animate" frame.
  useLayoutEffect(() => {
    if (!open) return
    const cardEl = cardRef.current
    const backdropEl = backdropRef.current
    if (!cardEl || !backdropEl) return
    const tl = gsap.timeline()
    tl.fromTo(
      backdropEl,
      { opacity: 0 },
      { opacity: 1, duration: 0.18, ease: 'power2.out' },
    )
    tl.fromTo(
      cardEl,
      { opacity: 0, y: 12, scale: 0.97 },
      { opacity: 1, y: 0, scale: 1, duration: 0.32, ease: 'power3.out' },
      '-=0.10',
    )
    return () => {
      tl.kill()
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const t = window.setTimeout(() => titleRef.current?.focus(), 80)
    return () => window.clearTimeout(t)
  }, [open])

  // Esc + Cmd/Ctrl+Enter handled at the window level so the popover's
  // input doesn't have to forward them.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      } else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault()
        if (title.trim()) void submit()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, title, body, priority, assignee, parents, children])

  const priorityValue = priority === 'high' ? 0 : priority === 'med' ? 2 : 4

  const selectable = useMemo(() => {
    const used = new Set<string>([...parents, ...children])
    return cards
      .filter((c) => !used.has(c.id))
      .filter((c) => (c.metadata as Record<string, unknown> | undefined)?.role !== 'mission_root')
      .sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0))
  }, [cards, parents, children])

  const reset = () => {
    setTitle('')
    setBody('')
    setPriority('med')
    setAssignee('')
    setParents([])
    setChildren([])
  }

  const submit = async () => {
    const t = title.trim()
    if (!t || submitting) return
    setSubmitting(true)
    try {
      await addOperatorKanbanCard({
        sessionId,
        title: t,
        body: body.trim() || undefined,
        priority: priorityValue,
        assignee: assignee || undefined,
        parents: parents.length ? parents : undefined,
        children: children.length ? children : undefined,
      })
      reset()
      // Quick pulse on the card to acknowledge submission. GSAP-driven
      // so it overlaps cleanly with the form's existing focus return.
      if (cardRef.current) {
        gsap.fromTo(
          cardRef.current,
          { boxShadow: '0 0 0 0 rgba(168,212,252,0.6)' },
          {
            boxShadow: '0 0 0 6px rgba(168,212,252,0)',
            duration: 0.6,
            ease: 'power2.out',
          },
        )
      }
      titleRef.current?.focus()
    } finally {
      setSubmitting(false)
    }
  }

  if (!open) return null

  const node = (
    <div className="fixed inset-0 z-50 flex items-center justify-center px-6">
      <div
        ref={backdropRef}
        onClick={onClose}
        className="absolute inset-0 bg-bg-0/75 backdrop-blur-[3px]"
      />

      <div
        ref={cardRef}
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-[560px] max-h-[88vh] overflow-hidden rounded-[14px] border border-accent/[0.22] bg-bg-1 shadow-[0_30px_60px_-20px_rgba(0,0,0,0.6)]"
      >
        {/* Vector-field backdrop confined to the card itself. Sits
          * behind everything inside the card via `pointer-events:none`
          * + relative children. The header and footer use solid bands
          * on top of the field so the chrome stays crisp while the
          * body shows the flowing dashes faintly behind the inputs. */}
        <VectorFieldBackdrop active={open} spacing={12} alpha={0.10} />
        <div className="relative flex items-center justify-between gap-2 border-b border-white/[0.06] bg-bg-1 px-5 py-3 font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-3">
          <span className="inline-flex items-center gap-2 text-accent">
            <span className="h-1 w-1 rounded-full bg-accent animate-pulse-soft" />
            new task · you
          </span>
          <button
            type="button"
            onClick={onClose}
            className="text-fg-3 transition hover:text-fg-1"
            title="Close (Esc)"
          >
            ✕
          </button>
        </div>

        <div className="relative flex flex-col gap-3 px-5 py-4 max-h-[calc(88vh-100px)] overflow-y-auto overflow-x-visible">
          <input
            ref={titleRef}
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="What's the task?"
            className="w-full rounded-md border border-white/[0.08] bg-bg-0 px-3 py-2.5 font-mono text-[14px] text-fg-0 placeholder:text-fg-4 focus:border-accent/[0.40] focus:outline-none"
          />

          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="Optional details — what does done look like? any context an agent should know?"
            rows={3}
            className="w-full resize-y rounded-md border border-white/[0.06] bg-bg-0 px-3 py-2 font-mono text-[12px] text-fg-1 leading-[1.55] placeholder:text-fg-4 focus:border-accent/[0.30] focus:outline-none"
          />

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <FormFieldLabel>priority</FormFieldLabel>
              <div className="inline-flex rounded-md border border-white/[0.06] bg-bg-0 p-0.5">
                {(['high', 'med', 'low'] as const).map((p) => (
                  <button
                    key={p}
                    type="button"
                    onClick={() => setPriority(p)}
                    className={`flex-1 rounded px-3 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] transition ${
                      priority === p
                        ? 'bg-accent/[0.18] text-accent'
                        : 'text-fg-2 hover:text-fg-0'
                    }`}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <FormFieldLabel>assignee</FormFieldLabel>
              <select
                value={assignee}
                onChange={(e) => setAssignee(e.target.value)}
                className="rounded-md border border-white/[0.06] bg-bg-0 px-2.5 py-1.5 font-mono text-[11px] text-fg-1 focus:border-accent/[0.30] focus:outline-none"
              >
                <option value="">auto · specifier picks</option>
                <option value="explore">explore</option>
                <option value="code">code</option>
                <option value="plan">plan</option>
                <option value="verify">verify</option>
                <option value="memory">memory</option>
                <option value="browser">browser</option>
                <option value="docs">docs</option>
                <option value="general">general</option>
              </select>
            </div>
          </div>

          <DependencyField
            label="wait for"
            hint="cards this task depends on"
            selected={parents}
            selectable={selectable}
            cards={cards}
            onChange={setParents}
          />
          <DependencyField
            label="then unlocks"
            hint="cards that should wait for this one"
            selected={children}
            selectable={selectable}
            cards={cards}
            onChange={setChildren}
          />
        </div>

        <div className="relative flex items-center justify-end gap-2 border-t border-white/[0.06] bg-bg-1 px-5 py-3">
          <span className="mr-auto font-mono text-[10px] text-fg-4">
            ⌘↵ to add · esc to close
          </span>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-white/[0.06] bg-transparent px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-3 transition hover:bg-white/[0.04] hover:text-fg-1"
          >
            cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!title.trim() || submitting}
            className="rounded-md border border-accent/[0.30] bg-accent/[0.12] px-4 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.16em] text-accent transition hover:bg-accent/[0.22] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? 'adding…' : 'add task'}
          </button>
        </div>
      </div>
    </div>
  )

  return createPortal(node, document.body)
}

/** Combobox-shaped field used twice in the form. Selected cards
 *  render as removable chips; the trailing "+ add" button opens a
 *  popover that's portaled into document.body with fixed positioning
 *  so it floats over the modal card without forcing the modal body
 *  to scroll past it. Popover positions itself below the trigger,
 *  flips above if it'd run off the bottom of the viewport, and
 *  closes on any scroll (the trigger's screen-space rect moves out
 *  from under it). */
function DependencyField({
  label,
  hint,
  selected,
  selectable,
  cards,
  onChange,
}: {
  label: string
  hint: string
  selected: string[]
  selectable: KanbanCardView[]
  cards: KanbanCardView[]
  onChange: (next: string[]) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [popPos, setPopPos] = useState<{
    top: number
    left: number
    width: number
    flip: boolean
  } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)

  const POP_WIDTH = 320
  const POP_MAX_H = 280

  const placePopover = () => {
    const btn = triggerRef.current
    if (!btn) return null
    const r = btn.getBoundingClientRect()
    const spaceBelow = window.innerHeight - r.bottom
    const flip = spaceBelow < POP_MAX_H + 24 && r.top > spaceBelow
    const top = flip ? r.top - POP_MAX_H - 6 : r.bottom + 6
    // Clamp horizontally so the popover never spills off the right
    // edge of the viewport — important when the form's near the
    // right side of the screen.
    const left = Math.max(
      12,
      Math.min(r.left, window.innerWidth - POP_WIDTH - 12),
    )
    return { top, left, width: POP_WIDTH, flip }
  }

  const openPopover = () => {
    const pos = placePopover()
    if (!pos) return
    setPopPos(pos)
    setQuery('')
    setOpen(true)
  }

  useEffect(() => {
    if (!open) return
    const onOutside = (e: MouseEvent) => {
      if (popRef.current?.contains(e.target as Node)) return
      if (triggerRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    // Close on any scroll in the page so the popover doesn't
    // visually detach from its trigger when the modal body or some
    // ancestor moves. Capture phase so we catch scrolls on inner
    // scrollers too.
    const onScroll = () => setOpen(false)
    const onResize = () => {
      const next = placePopover()
      if (next) setPopPos(next)
    }
    document.addEventListener('mousedown', onOutside)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => {
      document.removeEventListener('mousedown', onOutside)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
    }
  }, [open])

  const lookup = useMemo(() => {
    const m = new Map<string, KanbanCardView>()
    for (const c of cards) m.set(c.id, c)
    return m
  }, [cards])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return selectable
    return selectable.filter(
      (c) =>
        c.id.toLowerCase().includes(q) ||
        (c.title ?? '').toLowerCase().includes(q),
    )
  }, [selectable, query])

  const popover =
    open && popPos
      ? createPortal(
          <div
            ref={popRef}
            style={{
              position: 'fixed',
              top: popPos.top,
              left: popPos.left,
              width: popPos.width,
              maxHeight: POP_MAX_H,
              zIndex: 80,
            }}
            className="flex flex-col overflow-hidden rounded-md border border-white/[0.10] bg-bg-0 shadow-[0_22px_44px_-16px_rgba(0,0,0,0.75)] animate-fade-in"
          >
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search cards by id or title…"
              autoFocus
              className="w-full border-b border-white/[0.06] bg-bg-1 px-3 py-2 font-mono text-[11.5px] text-fg-0 placeholder:text-fg-4 focus:outline-none"
            />
            <div
              className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-1"
              onWheel={(e) => {
                // Stop wheel from bubbling to ancestor scrollers
                // (the modal body). overscroll-contain handles the
                // overflow case; this catches the case where the
                // popover content fits inside its max height.
                e.stopPropagation()
              }}
            >
              {filtered.length === 0 ? (
                <div className="px-3 py-3 text-center font-mono text-[10.5px] italic text-fg-4">
                  {selectable.length === 0
                    ? 'no other cards on the board'
                    : 'no matches'}
                </div>
              ) : (
                filtered.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => {
                      onChange([...selected, c.id])
                      setOpen(false)
                    }}
                    className="grid w-full grid-cols-[64px_1fr] gap-2 px-3 py-1.5 text-left font-mono text-[11px] transition hover:bg-accent/[0.06]"
                  >
                    <span className="tabular-nums text-fg-3">{c.id}</span>
                    <span className="truncate text-fg-1">{c.title}</span>
                  </button>
                ))
              )}
            </div>
          </div>,
          document.body,
        )
      : null

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between">
        <FormFieldLabel>{label}</FormFieldLabel>
        <span className="rounded bg-bg-1 px-1.5 py-0.5 font-mono text-[9.5px] text-fg-2">
          {hint}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {selected.map((id) => {
          const card = lookup.get(id)
          return (
            <span
              key={id}
              className="inline-flex items-center gap-1.5 rounded-md border border-accent/[0.22] bg-accent/[0.10] px-2 py-0.5 font-mono text-[10.5px] text-accent"
            >
              <span className="tabular-nums">{id}</span>
              <span className="max-w-[160px] truncate text-fg-1">
                {card?.title ?? '(unknown)'}
              </span>
              <button
                type="button"
                onClick={() => onChange(selected.filter((x) => x !== id))}
                className="text-accent/70 transition hover:text-accent"
                title="Remove"
              >
                ✕
              </button>
            </span>
          )
        })}
        <button
          ref={triggerRef}
          type="button"
          onClick={() => (open ? setOpen(false) : openPopover())}
          className="inline-flex items-center gap-1 rounded-md border border-dashed border-white/[0.10] bg-bg-0 px-2 py-0.5 font-mono text-[10.5px] text-fg-3 transition hover:border-accent/[0.30] hover:text-accent"
        >
          <span>+</span>
          <span>{selected.length === 0 ? 'add' : 'another'}</span>
        </button>
        {popover}
      </div>
    </div>
  )
}

/** Tiny opaque chip wrapper for field labels. The vector field sits
 *  behind the form's body; without an opaque backing the
 *  10px-tracking-wide labels read as ghost text against the drifting
 *  dashes. Setting an inline-block dark pill (`bg-bg-1`) on each label
 *  hides the field directly under the letters while leaving the rest
 *  of the row transparent — readable + still lets the backdrop
 *  texture be the dominant background sensation. */
function FormFieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="inline-flex w-fit rounded bg-bg-1 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-1">
      {children}
    </label>
  )
}
