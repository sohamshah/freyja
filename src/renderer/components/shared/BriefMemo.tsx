import React from 'react'

interface BriefMemoProps {
  open: boolean
  onClose: () => void
  to: string
  toRole: string
  re: string
  date?: string
  title: string
  prelude?: React.ReactNode
  children: React.ReactNode
  preview: React.ReactNode
  signoffName?: string
}

/**
 * Memo-style editable surface used by JudgeBrief (goal mode) and
 * DispatcherBrief (kanban mode). The left side carries the brief itself
 * as a series of editable sections; the right side carries a live preview
 * of the verdict/decision the orchestrator would make under the brief.
 *
 * Modal overlay with backdrop dismissal.
 */
export function BriefMemo({
  open,
  onClose,
  to,
  toRole,
  re,
  date,
  title,
  prelude,
  children,
  preview,
  signoffName,
}: BriefMemoProps) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-[70] flex flex-col bg-black/65 backdrop-blur-[8px]">
      <div className="flex items-center justify-between border-b border-white/[0.06] bg-bg-0/95 px-7 py-3 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3 backdrop-blur-[10px]">
        <div className="flex items-center gap-4">
          <span>rule book</span>
          <span className="text-fg-2">v2 · 2 edits this session</span>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded px-3 py-1 font-mono text-[10.5px] tracking-[0.18em] text-fg-2 transition hover:bg-white/[0.06] hover:text-fg-0"
        >
          close ✕
        </button>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_380px] min-h-0 flex-1 overflow-hidden">
        <div className="overflow-y-auto px-14 py-12">
          <div className="max-w-[680px] mx-auto">
            <header className="grid grid-cols-[60px_1fr] gap-y-1.5 gap-x-4 mb-9 pb-4 border-b border-white/[0.06] text-[11.5px] text-fg-1 tracking-[0.04em]">
              <span className="pt-0.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">to</span>
              <span className="font-mono text-fg-1">
                <span className="font-mono text-[12px] italic text-accent">{to}</span>
                <span className="ml-1.5 inline-block rounded border border-accent/[0.18] bg-accent/[0.06] px-2 py-px text-[10.5px] tracking-[0.04em] text-accent">
                  {toRole}
                </span>
              </span>
              <span className="pt-0.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">re</span>
              <span className="font-mono text-fg-0">{re}</span>
              <span className="pt-0.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">from</span>
              <span className="font-mono text-fg-1">the operator (you)</span>
              {date ? (
                <>
                  <span className="pt-0.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">date</span>
                  <span className="font-mono text-fg-1">{date}</span>
                </>
              ) : null}
            </header>

            <div className="mb-3 font-mono text-[10.5px] uppercase tracking-[0.22em] text-fg-3">rules</div>
            <h1 className="mb-4 font-serif text-[30px] font-light leading-[1.35] tracking-[-0.005em] text-fg-0">
              {title}
            </h1>
            {prelude ? (
              <p className="mb-9 max-w-[600px] font-mono text-[13.5px] leading-[1.7] text-fg-1">
                {prelude}
              </p>
            ) : null}

            <div className="flex flex-col gap-9">{children}</div>

            <div className="mt-12 flex items-end justify-between border-t-2 border-white/[0.06] pt-4 font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3">
              <span>brief ends here</span>
              {signoffName ? (
                <span className="font-mono text-[12px] italic normal-case tracking-normal text-fg-2">
                  — {signoffName}
                </span>
              ) : null}
            </div>
          </div>
        </div>

        <aside className="border-l border-white/[0.06] bg-black/25 overflow-y-auto">
          {preview}
        </aside>
      </div>
    </div>
  )
}

export function BriefSection({
  marker,
  title,
  control,
  children,
}: {
  marker: string
  title: string
  control?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-3.5">
      <div className="flex items-baseline justify-between border-b border-white/[0.06] pb-2">
        <span className="font-mono text-[13px] font-medium uppercase tracking-[0.08em] text-fg-0">
          <span className="mr-2.5 font-mono text-[11px] tracking-normal text-fg-3 normal-case">{marker}</span>
          {title}
        </span>
        {control}
      </div>
      <div className="font-mono text-[13px] leading-[1.7] text-fg-1">{children}</div>
    </section>
  )
}

export function BriefPreview({
  label,
  verdict,
  reason,
  counterfactuals,
  footer,
}: {
  label: string
  verdict: string
  reason: React.ReactNode
  counterfactuals?: { body: React.ReactNode }[]
  footer?: React.ReactNode
}) {
  return (
    <>
      <div className="flex items-center justify-between border-b border-white/[0.06] px-5 py-4">
        <span className="inline-flex items-center gap-2 font-mono text-[12px] italic text-accent">
          <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_6px_rgba(168,212,252,0.6)]" />
          live preview
        </span>
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">updates as you edit</span>
      </div>
      <div className="px-5 py-4 border-b border-white/[0.06]">
        <div className="text-fg-3 text-[10px] uppercase tracking-[0.14em] mb-2">{label}</div>
        <div className="rounded-[10px] border border-accent/[0.20] bg-gradient-to-b from-accent/[0.05] to-accent/[0.01] px-4 py-3.5">
          <span className="inline-block px-2.5 py-0.5 rounded mb-2 bg-accent/[0.08] border border-accent/[0.22] text-accent font-mono text-[10.5px] uppercase tracking-[0.18em]">
            {verdict}
          </span>
          <p className="m-0 font-mono text-[12.5px] leading-[1.7] italic text-fg-1">
            {reason}
          </p>
        </div>
      </div>
      {counterfactuals && counterfactuals.length > 0 ? (
        <div className="px-5 py-4">
          <div className="text-fg-3 text-[10px] uppercase tracking-[0.14em] mb-2">
            counterfactual · how would this change if…
          </div>
          <div className="flex flex-col gap-2">
            {counterfactuals.map((cf, i) => (
              <div
                key={i}
                className="grid grid-cols-[14px_1fr] gap-2 rounded-md border border-white/[0.06] bg-white/[0.02] px-3 py-2.5 text-fg-1 text-[12px] leading-[1.6]"
              >
                <span className="text-fg-3">↪</span>
                <span>{cf.body}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {footer}
    </>
  )
}
