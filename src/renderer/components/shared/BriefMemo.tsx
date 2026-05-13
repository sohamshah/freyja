import React, { useEffect } from 'react'

interface BriefMemoProps {
  open: boolean
  onClose: () => void
  /** Who the brief addresses — e.g. "judge", "dispatcher". Rendered as
   *  the small uppercase eyebrow above the title. */
  to: string
  /** Live status chip beside the title — e.g. "last verdict: done",
   *  "autopilot · ready". */
  toRole: string
  /** Subject line — short prose summary of what the brief is for. */
  re: string
  /** Optional last-edited / last-seen timestamp shown in the corner. */
  date?: string
  /** Big serif title at the top of the left pane. */
  title: string
  /** Optional intro paragraph rendered just under the title. */
  prelude?: React.ReactNode
  /** The form sections — one BriefSection per editable concern. */
  children: React.ReactNode
  /** Right-rail content. Typically a BriefPreview. */
  preview: React.ReactNode
  /** Legacy prop, no longer rendered. Kept so DispatcherBrief / JudgeBrief
   *  call sites don't have to change in lockstep. */
  signoffName?: string
}

/**
 * Editable brief surface used by JudgeBrief (goal mode) and
 * DispatcherBrief (kanban mode). Two-column layout: left side is the
 * form (sections + fields), right side is the live preview the
 * orchestrator would produce given the current rules.
 *
 * Header is a single horizontal strip — no memo affectations, just a
 * scope eyebrow + status chip + close. Title + intro live with the
 * form content below.
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
}: BriefMemoProps) {
  // ESC closes — wired here so every brief surface gets it for free
  // and the close button can advertise the keystroke.
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-[70] flex flex-col bg-black/65 backdrop-blur-[8px]">
      {/* Strip header — scope + status, balanced by the close button.
          pl-[88px] clears the OS-level traffic-light buttons (rendered
          on top of the window content under hiddenInset titleBarStyle).
          no-drag on the close button so the OS doesn't intercept clicks. */}
      <div className="flex items-center gap-4 border-b border-white/[0.06] bg-bg-0/95 py-3 pl-[88px] pr-4 backdrop-blur-[10px]">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3">
          {to}
        </span>
        <span className="inline-flex items-center gap-1.5 rounded border border-white/[0.08] bg-white/[0.025] px-2 py-0.5 font-mono text-[10.5px] text-fg-2">
          <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_5px_rgba(168,212,252,0.55)]" />
          {toRole}
        </span>
        <span className="hidden min-w-0 truncate font-mono text-[11.5px] text-fg-3 lg:inline">
          {re}
        </span>
        {date ? (
          <span className="ml-auto font-mono text-[10.5px] tabular-nums text-fg-4">
            {date}
          </span>
        ) : (
          <span className="ml-auto" />
        )}
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="no-drag relative z-[1] flex h-7 items-center justify-center rounded border border-white/[0.08] bg-white/[0.025] px-3 font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-2 transition hover:border-white/[0.18] hover:bg-white/[0.08] hover:text-fg-0"
        >
          close · esc
        </button>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_380px] overflow-hidden">
        <div className="overflow-y-auto px-14 py-10">
          <div className="mx-auto max-w-[680px]">
            <h1 className="m-0 mb-3 font-serif text-[28px] font-light leading-[1.3] tracking-[-0.005em] text-fg-0">
              {title}
            </h1>
            {prelude ? (
              <p className="m-0 mb-9 max-w-[620px] font-mono text-[13px] leading-[1.7] text-fg-2">
                {prelude}
              </p>
            ) : (
              <div className="mb-7" />
            )}

            <div className="flex flex-col gap-9">{children}</div>
          </div>
        </div>

        <aside className="overflow-y-auto border-l border-white/[0.06] bg-black/25">
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
      <div className="border-b border-white/[0.06] px-5 py-4">
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">{label}</div>
        <div className="rounded-[10px] border border-accent/[0.20] bg-gradient-to-b from-accent/[0.05] to-accent/[0.01] px-4 py-3.5">
          <span className="mb-2 inline-block rounded border border-accent/[0.22] bg-accent/[0.08] px-2.5 py-0.5 font-mono text-[10.5px] uppercase tracking-[0.18em] text-accent">
            {verdict}
          </span>
          <p className="m-0 font-mono text-[12.5px] italic leading-[1.7] text-fg-1">
            {reason}
          </p>
        </div>
      </div>
      {counterfactuals && counterfactuals.length > 0 ? (
        <div className="px-5 py-4">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
            counterfactual · how would this change if…
          </div>
          <div className="flex flex-col gap-2">
            {counterfactuals.map((cf, i) => (
              <div
                key={i}
                className="grid grid-cols-[14px_1fr] gap-2 rounded-md border border-white/[0.06] bg-white/[0.02] px-3 py-2.5 text-[12px] leading-[1.6] text-fg-1"
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
