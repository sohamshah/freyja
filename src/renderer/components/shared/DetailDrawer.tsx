import React, { useEffect } from 'react'

interface DetailDrawerProps {
  open: boolean
  onClose: () => void
  title: string
  statusLabel?: string
  width?: number
  children: React.ReactNode
  footer?: React.ReactNode
  /** Optional decorative layer rendered absolutely behind every
   *  other child (header / body / footer all sit on top). Designed
   *  for animated WebGL or canvas backdrops — the drawer adds
   *  `position:absolute inset-0 pointer-events:none` framing so the
   *  backdrop only needs to paint pixels, not worry about layout. */
  backdrop?: React.ReactNode
}

/**
 * Layout-column detail panel. When `open` is true, this renders as a
 * regular block element sized to `width` (default 480px) that the
 * parent can place inside its CSS grid as an additional column —
 * compressing other columns rather than overlaying them. Closes on
 * Escape.
 *
 * Style match: Fraunces serif for the title, generous breathing
 * room, hairline borders, and the same six-section composition the
 * mocks established (status header · assignment · brief · deps ·
 * timeline · files · footer actions).
 */
export function DetailDrawer({
  open,
  onClose,
  title,
  statusLabel,
  width = 480,
  children,
  footer,
  backdrop,
}: DetailDrawerProps) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <aside
      style={{ width }}
      className="relative flex h-full flex-col overflow-hidden border-l border-white/[0.06] bg-bg-0/[0.96] shadow-[-12px_0_32px_-12px_rgba(0,0,0,0.55)] backdrop-blur-[24px] animate-fade-in"
    >
      {/* Decorative backdrop layer — sits behind every header/body/
        * footer pixel via absolute positioning. Explicit z-0 keeps it
        * below the chrome regardless of any backdrop-filter stacking
        * weirdness on the parent <aside>. Pointer-events:none so the
        * canvas never steals clicks from the close button. */}
      {backdrop ? (
        <div className="pointer-events-none absolute inset-0 z-0 overflow-hidden">
          {backdrop}
        </div>
      ) : null}

      <header className="relative z-10 flex items-center justify-between border-b border-white/[0.06] px-6 py-4">
        {statusLabel ? (
          <span className="inline-flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent">
            <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent shadow-[0_0_6px_rgba(168,212,252,0.6)]" />
            {statusLabel}
          </span>
        ) : (
          <span />
        )}
        <button
          type="button"
          onClick={onClose}
          className="rounded px-2 py-1 font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3 transition hover:bg-white/[0.04] hover:text-fg-0"
        >
          close ✕
        </button>
      </header>

      <div className="relative z-10 flex min-h-0 flex-1 flex-col gap-7 overflow-y-auto px-7 py-6">
        <h2 className="m-0 font-serif text-[26px] font-light leading-[1.3] tracking-[-0.005em] text-fg-0">
          {title}
        </h2>
        {children}
      </div>

      {footer ? (
        <footer className="relative z-10 flex flex-wrap gap-2 border-t border-white/[0.06] bg-black/30 px-5 py-3">
          {footer}
        </footer>
      ) : null}
    </aside>
  )
}

export function DrawerSection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <h3 className="m-0 font-mono text-[10.5px] font-normal uppercase tracking-[0.14em] text-fg-3">
        {label}
      </h3>
      {children}
    </section>
  )
}

export function DrawerAssignment({
  agent,
  age,
  current,
}: {
  agent: string
  age?: string
  current?: string
}) {
  return (
    <div className="flex flex-wrap items-center gap-3.5 rounded-[10px] border border-white/[0.08] bg-white/[0.02] px-4 py-2.5 text-[12px] text-fg-1">
      <span className="inline-flex items-center gap-2 font-mono font-medium text-fg-0">
        <span className="h-1.5 w-1.5 rounded-full bg-fg-1" />
        {agent}
      </span>
      {age ? <span className="font-mono tabular-nums text-fg-2">{age}</span> : null}
      {current ? (
        <span className="ml-auto font-mono text-[11.5px] italic text-fg-2">{current}</span>
      ) : null}
    </div>
  )
}

interface TimelineEvent {
  ts: string
  who: string
  body: React.ReactNode
  kind?: 'agent' | 'auto' | 'system'
}

export function DrawerTimeline({ events }: { events: TimelineEvent[] }) {
  return (
    <div className="flex flex-col">
      {events.map((ev, i) => (
        <div
          key={i}
          className="grid grid-cols-[46px_72px_1fr] gap-3 border-t border-transparent py-1.5 text-[11.5px] leading-[1.55] text-fg-1 first:border-t-0 [&:not(:first-child)]:border-t-white/[0.03]"
        >
          <span className="font-mono tabular-nums text-fg-3">{ev.ts}</span>
          <span
            className={
              ev.kind === 'auto'
                ? 'font-mono text-[11.5px] italic text-fg-2'
                : ev.kind === 'system'
                ? 'font-mono text-fg-3'
                : 'font-mono text-fg-1'
            }
          >
            {ev.who}
          </span>
          <span>{ev.body}</span>
        </div>
      ))}
    </div>
  )
}

interface DependencyItem {
  text: string
  status: 'done' | 'queued' | 'blocked'
  meta?: string
}

export function DrawerDependencies({
  dependsOn,
  blocks,
}: {
  dependsOn?: DependencyItem[]
  blocks?: DependencyItem[]
}) {
  return (
    <div className="flex flex-col gap-4">
      {dependsOn && dependsOn.length > 0 ? (
        <div>
          <span className="mb-2 block font-mono text-[10px] uppercase tracking-[0.18em] text-fg-3">
            depends on
          </span>
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
            {dependsOn.map((d, i) => (
              <DepRow key={i} {...d} />
            ))}
          </ul>
        </div>
      ) : null}
      {blocks && blocks.length > 0 ? (
        <div>
          <span className="mb-2 block font-mono text-[10px] uppercase tracking-[0.18em] text-fg-3">
            blocks · {blocks.length} {blocks.length === 1 ? 'card' : 'cards'} waiting
          </span>
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
            {blocks.map((d, i) => (
              <DepRow key={i} {...d} />
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}

function DepRow({ text, status, meta }: DependencyItem) {
  const glyph = status === 'done' ? '✓' : status === 'blocked' ? '⊘' : '○'
  const glyphColor =
    status === 'done' ? 'text-ok' : status === 'blocked' ? 'text-warn' : 'text-fg-3'
  const textColor = status === 'done' ? 'text-fg-2' : 'text-fg-1'
  return (
    <li className="cursor-pointer rounded px-2 py-1 text-[12.5px] leading-[1.5] transition hover:bg-white/[0.025]">
      <span className={`mr-1.5 ${glyphColor}`}>{glyph}</span>
      <span className={`font-mono ${textColor}`}>{text}</span>
      {meta ? <span className="ml-1.5 font-mono text-fg-3">· {meta}</span> : null}
    </li>
  )
}

export function DrawerAction({
  children,
  variant = 'default',
  onClick,
}: {
  children: React.ReactNode
  variant?: 'default' | 'ok' | 'warn'
  onClick?: () => void
}) {
  const cls =
    variant === 'ok'
      ? 'text-ok border-ok/25 bg-ok/[0.06] hover:bg-ok/[0.12]'
      : variant === 'warn'
      ? 'text-warn border-warn/25 bg-warn/[0.05] hover:bg-warn/[0.10]'
      : 'text-fg-1 border-white/[0.06] bg-white/[0.03] hover:bg-white/[0.06] hover:text-fg-0'
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md border px-3 py-1.5 font-mono text-[11px] tracking-[0.04em] transition ${cls}`}
    >
      {children}
    </button>
  )
}
