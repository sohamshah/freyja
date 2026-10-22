import React, { useEffect, useRef, useState } from 'react'
import type { JevPaletteDecision } from '../../../shared/events'
import type { PaletteSlots, PaletteWidgetType } from '../../lib/paletteIntent'

/**
 * Shared chrome for the ⌘K cards.
 *
 * Every card receives the raw query, Jev's decision, the slots parsed from
 * the query, and a `handoff` it can call to push another card on top of
 * itself (agenda row → reschedule → create card, PR row → deep review →
 * delegation card). The palette owns the stack; Esc pops it.
 */
export interface Handoff {
  type: Exclude<PaletteWidgetType, null>
  label: string
  slots?: Partial<PaletteSlots>
  context?: Record<string, unknown>
}

export interface WidgetProps {
  type: Exclude<PaletteWidgetType, null>
  query: string
  decision: JevPaletteDecision
  slots: PaletteSlots
  /** Extra data handed over by the previous card, if this one was pushed. */
  context?: Record<string, unknown>
  crumbs: string[]
  onClose: () => void
  onToast: (msg: string) => void
  handoff: (next: Handoff) => void
}

export function Frame({
  kicker,
  title,
  badge,
  crumbs,
  children,
  footer,
}: {
  kicker: string
  title: React.ReactNode
  badge?: React.ReactNode
  crumbs?: string[]
  children: React.ReactNode
  footer?: React.ReactNode
}) {
  return (
    <div className="animate-fade-in space-y-3 p-3.5 font-mono">
      <div className="hairline-b flex items-center justify-between pb-2">
        <div className="flex min-w-0 items-center gap-2 text-[12px] text-fg-0">
          <span className="shrink-0 text-[9.5px] uppercase tracking-[0.16em] text-fg-3">{kicker}</span>
          {crumbs && crumbs.length > 0 && (
            <span className="flex shrink-0 items-center gap-1 text-[10px] text-fg-3">
              {crumbs.map((c, i) => (
                <React.Fragment key={i}>
                  <span>›</span>
                  <span>{c}</span>
                </React.Fragment>
              ))}
            </span>
          )}
          <span className="text-fg-3">·</span>
          <span className="truncate">{title}</span>
        </div>
        {badge && <span className="kbd shrink-0 text-[9.5px]">{badge}</span>}
      </div>
      {children}
      {footer && <div className="hairline-t flex items-center justify-end gap-2 pt-2">{footer}</div>}
    </div>
  )
}

export function Well({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded-lg bg-black/[0.18] p-3 ring-hairline ${className}`}>{children}</div>
}

export function Label({ children }: { children: React.ReactNode }) {
  return <div className="text-[9.5px] uppercase tracking-[0.16em] text-fg-3">{children}</div>
}

export function Btn({
  primary,
  danger,
  disabled,
  onClick,
  children,
  title,
}: {
  primary?: boolean
  danger?: boolean
  disabled?: boolean
  onClick: () => void
  children: React.ReactNode
  title?: string
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      title={title}
      data-primary={primary ? '' : undefined}
      className={
        primary
          ? 'rounded-md bg-accent/20 px-3 py-1 text-[11px] text-fg-0 ring-1 ring-accent/40 transition-colors hover:bg-accent/30 disabled:cursor-not-allowed disabled:opacity-40'
          : danger
            ? 'rounded-md px-2.5 py-1 text-[11px] text-danger ring-1 ring-danger/40 transition-colors hover:bg-danger/10 disabled:opacity-40'
            : 'rounded-md px-2.5 py-1 text-[11px] text-fg-2 ring-hairline transition-colors hover:bg-white/[0.05] hover:text-fg-0 disabled:opacity-40'
      }
    >
      {children}
    </button>
  )
}

/** A slot value, or a placeholder the next words will fill. */
export function Slot({ label, value, hint }: { label: string; value?: React.ReactNode; hint: string }) {
  const filled = value !== undefined && value !== null && value !== ''
  return (
    <div className="flex items-baseline gap-2 text-[11.5px]">
      <span className="w-16 shrink-0 text-[10px] uppercase tracking-wider text-fg-3">{label}</span>
      {filled ? (
        <span className="text-fg-0">{value}</span>
      ) : (
        <span className="text-fg-3">
          <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full ring-1 ring-fg-3/60 align-middle" />
          {hint}
        </span>
      )}
    </div>
  )
}

export function Progress({ filled, total }: { filled: number; total: number }) {
  return (
    <div className="flex items-center gap-1.5">
      {Array.from({ length: total }).map((_, i) => (
        <span key={i} className={`h-1 w-4 rounded-full ${i < filled ? 'bg-accent' : 'bg-fg-4'}`} />
      ))}
      <span className="ml-1 text-[9.5px] text-fg-3">
        {filled}/{total}
      </span>
    </div>
  )
}

/** Selectable chip (free slots, filters, suggested values). */
export function Chip({
  on,
  onClick,
  children,
  tone = 'neutral',
}: {
  on?: boolean
  onClick?: () => void
  children: React.ReactNode
  tone?: 'neutral' | 'ok' | 'warn' | 'danger'
}) {
  const toneCls =
    tone === 'ok' ? 'text-ok ring-ok/40' : tone === 'warn' ? 'text-warn ring-warn/40' : tone === 'danger' ? 'text-danger ring-danger/40' : 'text-fg-2 ring-hairline'
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      className={`rounded px-2 py-0.5 font-mono text-[10.5px] transition-colors ${
        on ? 'bg-accent/20 text-fg-0 ring-1 ring-accent/50' : `${toneCls} ring-1 hover:bg-white/[0.05] hover:text-fg-0 disabled:cursor-default disabled:hover:bg-transparent`
      }`}
    >
      {children}
    </button>
  )
}

/** List row: title + meta line on the left, a right column, optional hover actions. */
export function Row({
  title,
  meta,
  right,
  onClick,
  active,
  actions,
  flag,
}: {
  title: React.ReactNode
  meta?: React.ReactNode
  right?: React.ReactNode
  onClick?: () => void
  active?: boolean
  actions?: React.ReactNode
  flag?: React.ReactNode
}) {
  return (
    <div
      className={`group flex w-full items-center justify-between gap-3 rounded-md px-2.5 py-1.5 text-left transition-colors ${
        active ? 'bg-accent/15 ring-1 ring-accent/40' : 'ring-hairline hover:bg-white/[0.04]'
      }`}
    >
      <button type="button" onClick={onClick} disabled={!onClick} className="min-w-0 flex-1 text-left disabled:cursor-default">
        <span className="flex items-center gap-1.5 text-[11.5px] text-fg-0">
          <span className="truncate">{title}</span>
          {flag}
        </span>
        {meta && <span className="block truncate text-[10px] text-fg-3">{meta}</span>}
      </button>
      {right && <span className="shrink-0 text-right text-[10px] text-fg-3">{right}</span>}
      {actions && <span className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">{actions}</span>}
    </div>
  )
}

/** Tiny inline action inside a Row. */
export function RowAction({ onClick, children, title }: { onClick: () => void; children: React.ReactNode; title?: string }) {
  return (
    <button
      type="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation()
        onClick()
      }}
      className="rounded px-1.5 py-0.5 text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.06] hover:text-fg-0"
    >
      {children}
    </button>
  )
}

export function Note({ tone = 'neutral', children }: { tone?: 'neutral' | 'ok' | 'warn' | 'danger'; children: React.ReactNode }) {
  const cls = tone === 'ok' ? 'text-ok' : tone === 'warn' ? 'text-warn' : tone === 'danger' ? 'text-danger' : 'text-fg-3'
  return <div className={`text-[10.5px] ${cls}`}>{children}</div>
}

export function Pending({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 py-2 text-[10.5px] text-fg-3">
      <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
      {label}
    </div>
  )
}

/** Small async hook: re-runs `fn` when `key` changes, keeps the last data while reloading. */
export function useAsync<T>(fn: () => Promise<T>, key: string, enabled = true) {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean; key?: string }>({ loading: enabled })
  const seq = useRef(0)
  useEffect(() => {
    if (!enabled) return
    const mine = ++seq.current
    setState((s) => ({ ...s, loading: true }))
    fn().then(
      (data) => mine === seq.current && setState({ data, loading: false, key }),
      (err) => mine === seq.current && setState((s) => ({ ...s, error: String(err?.message ?? err), loading: false, key })),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled])
  return state
}

export const fmtTime = (d: Date) =>
  d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }).replace(':00', '')

export const fmtDay = (d: Date) => d.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })

export const relTime = (ms: number) => {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}
