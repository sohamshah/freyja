import React, { useEffect, useMemo, useState } from 'react'
import type { CalibrationStatus, JudgeRules } from './types'
import { useHarness } from '../../state/store'

/**
 * Visual surface for the judge calibrator lifecycle.
 *
 * Goals: ambient and natural — not a modal, not a popup. A horizontal
 * card that fits between sibling content (conversation messages, goal
 * dashboard sections) without commanding attention. While running it
 * pulses subtly. When complete it collapses to a single line on its
 * own — no auto-dismiss timer; the operator can re-expand to see the
 * rationale or click through to the calibrator's own session.
 *
 * Variants:
 *   - `chat`   :: rendered inline in the conversation pane (left-aligned,
 *                 narrower)
 *   - `studio` :: rendered in GoalStudioView (full-width within the
 *                 dashboard column)
 *
 * Both variants share the same sub-states: running, applied, proposed,
 * failed, idle. The compact one-line summary is identical; only the
 * expanded body changes per variant.
 */
export function CalibrationCard({
  calibration,
  judgeRules,
  proposal,
  variant = 'studio',
  onOpenJudgeBrief,
}: {
  calibration: CalibrationStatus | null | undefined
  /** Currently active rules — used to pull calibrator metadata when
   *  status === 'applied' (the meta lives on judgeRules, not on the
   *  calibration object). */
  judgeRules: JudgeRules | null | undefined
  /** Pending proposal (when status === 'proposed'). */
  proposal: JudgeRules | null | undefined
  variant?: 'chat' | 'studio'
  /** When provided, the "open judge rules" button uses this; otherwise
   *  the button is hidden (chat variant in modes where the button
   *  doesn't make sense). */
  onOpenJudgeBrief?: () => void
}) {
  const recalibrate = useHarness((s) => s.recalibrateJudge)
  const acceptProposal = useHarness((s) => s.acceptCalibratorProposal)
  const dismissProposal = useHarness((s) => s.dismissCalibratorProposal)
  const openSessionPane = useHarness((s) => s.openSessionPane)
  const [expanded, setExpanded] = useState(true)

  // When the calibrator finishes, collapse the running card to a single
  // line after a short beat so the user can keep reading the conversation
  // without a wall of result staring at them. They can re-expand from
  // the chevron.
  const status = calibration?.status ?? 'idle'
  useEffect(() => {
    if (status === 'applied' || status === 'failed') {
      const t = setTimeout(() => setExpanded(false), 8000)
      return () => clearTimeout(t)
    }
    return undefined
  }, [status])

  if (!calibration || status === 'idle') return null

  const meta = judgeRules?.calibratorMeta ?? null
  const effectiveRules =
    status === 'proposed' ? proposal ?? null : judgeRules ?? null
  const effectiveMeta = effectiveRules?.calibratorMeta ?? meta
  const isChat = variant === 'chat'

  return (
    <div
      className={`overflow-hidden rounded-md border bg-white/[0.02] ${
        status === 'failed'
          ? 'border-warn/[0.20]'
          : status === 'running'
          ? 'border-accent/[0.22]'
          : 'border-white/[0.08]'
      } ${isChat ? 'mx-auto my-3 max-w-[820px]' : 'mb-4'}`}
    >
      <header
        className={`flex items-center gap-3 px-4 py-2 ${
          status === 'running' ? 'bg-accent/[0.04]' : ''
        }`}
      >
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex flex-1 items-center gap-3 text-left"
          aria-label={expanded ? 'collapse' : 'expand'}
        >
          <CalibrationIndicator status={status} />
          <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-3">
            judge calibration
          </span>
          <Headline calibration={calibration} rules={effectiveRules} />
          <span className="ml-auto font-mono text-[10px] uppercase tracking-[0.14em] text-fg-4">
            {expanded ? '▾' : '▸'}
          </span>
        </button>
      </header>

      {expanded && (
        <div className="border-t border-white/[0.05] px-4 py-3">
          {status === 'running' && (
            <RunningBody calibration={calibration} />
          )}
          {status === 'applied' && (
            <AppliedBody
              meta={effectiveMeta}
              rules={effectiveRules}
              onOpenJudgeBrief={onOpenJudgeBrief}
              onRecalibrate={recalibrate}
              onOpenCalibratorSession={
                effectiveMeta?.sessionId
                  ? () => openSessionPane(effectiveMeta.sessionId!)
                  : undefined
              }
            />
          )}
          {status === 'proposed' && (
            <ProposedBody
              meta={effectiveMeta}
              rules={effectiveRules}
              currentRules={judgeRules}
              onAccept={acceptProposal}
              onDismiss={dismissProposal}
              onOpenJudgeBrief={onOpenJudgeBrief}
            />
          )}
          {status === 'failed' && (
            <FailedBody calibration={calibration} onRetry={recalibrate} />
          )}
        </div>
      )}
    </div>
  )
}

function CalibrationIndicator({ status }: { status: CalibrationStatus['status'] }) {
  // Dot semantics:
  //   running   :: pulsing accent (active work)
  //   applied   :: solid ok (done; rules in effect)
  //   proposed  :: hollow accent (waiting on operator)
  //   failed    :: solid warn
  if (status === 'running') {
    return (
      <span className="relative inline-flex h-2 w-2 items-center justify-center">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-50" />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
      </span>
    )
  }
  if (status === 'applied') return <span className="inline-block h-1.5 w-1.5 rounded-full bg-ok" />
  if (status === 'proposed')
    return <span className="inline-block h-2 w-2 rounded-full border border-accent" />
  return <span className="inline-block h-1.5 w-1.5 rounded-full bg-warn" />
}

function Headline({
  calibration,
  rules,
}: {
  calibration: CalibrationStatus
  rules: JudgeRules | null | undefined
}) {
  if (calibration.status === 'running') {
    return (
      <span className="font-mono text-[12.5px] text-fg-1">
        reading the goal and inferring the optimal judge…
      </span>
    )
  }
  if (calibration.status === 'failed') {
    return (
      <span className="font-mono text-[12.5px] text-warn">
        calibration failed{calibration.errorMessage ? ` · ${calibration.errorMessage}` : ''}
      </span>
    )
  }
  // applied or proposed: summarize the picked configuration
  if (rules) {
    const tag = calibration.status === 'applied' ? 'applied' : 'awaiting review'
    return (
      <span className="font-mono text-[12.5px] text-fg-1">
        <span className="text-fg-0">{rules.judgeProfile}</span>
        <span className="text-fg-3"> · rigor </span>
        <span className="text-fg-0">{rules.rigorScore}</span>
        <span className="text-fg-3"> · </span>
        <span className="text-fg-0">{rules.criteria.length}</span>
        <span className="text-fg-3"> criteria · {tag}</span>
      </span>
    )
  }
  return (
    <span className="font-mono text-[12.5px] italic text-fg-3">
      {calibration.status}
    </span>
  )
}

function RunningBody({ calibration }: { calibration: CalibrationStatus }) {
  return (
    <div className="flex items-baseline gap-3 font-mono text-[11.5px] text-fg-2">
      <DotsLoader />
      <span className="leading-[1.55]">
        the calibrator is reading your goal and proposing a profile, rigor, voice, and a set
        of must / should / may criteria. usually finishes in 5–15s.
      </span>
      <span className="ml-auto whitespace-nowrap text-fg-3">
        {calibration.model ?? 'frontier'}
      </span>
    </div>
  )
}

function AppliedBody({
  meta,
  rules,
  onOpenJudgeBrief,
  onRecalibrate,
  onOpenCalibratorSession,
}: {
  meta: JudgeRules['calibratorMeta'] | null | undefined
  rules: JudgeRules | null | undefined
  onOpenJudgeBrief?: () => void
  onRecalibrate: () => void
  onOpenCalibratorSession?: () => void
}) {
  return (
    <div className="flex flex-col gap-3">
      {meta?.rationaleOverall ? (
        <p className="m-0 select-text font-mono text-[12px] leading-[1.6] text-fg-1">
          {meta.rationaleOverall}
        </p>
      ) : null}
      <div className="flex items-center gap-2 pt-1">
        {onOpenJudgeBrief && (
          <button
            type="button"
            onClick={onOpenJudgeBrief}
            className="rounded border border-accent/[0.22] bg-accent/[0.06] px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.12]"
          >
            open judge rules
          </button>
        )}
        <button
          type="button"
          onClick={onRecalibrate}
          className="rounded border border-white/[0.08] bg-white/[0.02] px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-2 transition hover:border-white/[0.16] hover:bg-white/[0.06] hover:text-fg-0"
        >
          recalibrate
        </button>
        {onOpenCalibratorSession && (
          <button
            type="button"
            onClick={onOpenCalibratorSession}
            className="ml-auto rounded px-2 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3 transition hover:bg-white/[0.06] hover:text-accent"
          >
            calibrator session ↗
          </button>
        )}
        {!onOpenCalibratorSession && (
          <span className="ml-auto font-mono text-[10px] uppercase tracking-[0.14em] text-fg-4">
            {meta?.model ?? ''}
            {meta?.confidence ? ` · ${(meta.confidence * 100).toFixed(0)}%` : ''}
          </span>
        )}
      </div>
    </div>
  )
}

function ProposedBody({
  meta,
  rules,
  currentRules,
  onAccept,
  onDismiss,
  onOpenJudgeBrief,
}: {
  meta: JudgeRules['calibratorMeta'] | null | undefined
  rules: JudgeRules | null | undefined  // the proposal
  currentRules: JudgeRules | null | undefined  // operator's current rules
  onAccept: () => void
  onDismiss: () => void
  onOpenJudgeBrief?: () => void
}) {
  const diffSummary = useMemo(
    () => summarizeProposalDiff(currentRules ?? null, rules ?? null),
    [currentRules, rules],
  )
  return (
    <div className="flex flex-col gap-3">
      <p className="m-0 font-mono text-[12px] italic leading-[1.55] text-fg-3">
        you already authored judge rules — the calibrator's proposal is below for review.
        accepting will replace your current configuration.
      </p>
      {meta?.rationaleOverall ? (
        <p className="m-0 select-text font-mono text-[12px] leading-[1.6] text-fg-1">
          {meta.rationaleOverall}
        </p>
      ) : null}
      {diffSummary.length > 0 ? (
        <ul className="m-0 flex list-none flex-col gap-1 p-0 font-mono text-[11.5px] leading-[1.5] text-fg-2">
          {diffSummary.map((line, i) => (
            <li key={i} className="flex items-baseline gap-2">
              <span className="text-fg-4">·</span>
              <span>{line}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={onAccept}
          className="rounded border border-accent/[0.32] bg-accent/[0.12] px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.18]"
        >
          accept proposal
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className="rounded border border-white/[0.08] bg-white/[0.02] px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-2 transition hover:border-white/[0.16] hover:bg-white/[0.06] hover:text-fg-0"
        >
          dismiss
        </button>
        {onOpenJudgeBrief && (
          <button
            type="button"
            onClick={onOpenJudgeBrief}
            className="ml-auto rounded px-2 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3 transition hover:bg-white/[0.06] hover:text-fg-0"
          >
            open editor for full diff
          </button>
        )}
      </div>
    </div>
  )
}

function FailedBody({
  calibration,
  onRetry,
}: {
  calibration: CalibrationStatus
  onRetry: () => void
}) {
  return (
    <div className="flex items-center gap-3 font-mono text-[12px] text-fg-2">
      <span className="leading-[1.5]">
        the calibrator couldn't run for this goal
        {calibration.errorMessage ? ` (${calibration.errorMessage})` : ''}. judge will use
        defaults until you tune it manually or retry.
      </span>
      <button
        type="button"
        onClick={onRetry}
        className="ml-auto whitespace-nowrap rounded border border-white/[0.08] bg-white/[0.02] px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-2 transition hover:border-white/[0.16] hover:bg-white/[0.06] hover:text-fg-0"
      >
        retry
      </button>
    </div>
  )
}

function DotsLoader() {
  return (
    <span className="inline-flex items-center gap-0.5">
      <span className="inline-block h-1 w-1 animate-pulse-soft rounded-full bg-accent" />
      <span
        className="inline-block h-1 w-1 animate-pulse-soft rounded-full bg-accent"
        style={{ animationDelay: '120ms' }}
      />
      <span
        className="inline-block h-1 w-1 animate-pulse-soft rounded-full bg-accent"
        style={{ animationDelay: '240ms' }}
      />
    </span>
  )
}

/** A short list of the most useful diffs between the operator's current
 *  rules and the calibrator's proposal. Capped to ~5 lines so the card
 *  doesn't grow into a wall. */
function summarizeProposalDiff(
  current: JudgeRules | null,
  proposal: JudgeRules | null,
): string[] {
  if (!proposal) return []
  if (!current) return [`proposes ${proposal.judgeProfile} profile · rigor ${proposal.rigorScore}`]
  const out: string[] = []
  if (current.judgeProfile !== proposal.judgeProfile) {
    out.push(`profile · ${current.judgeProfile} → ${proposal.judgeProfile}`)
  }
  if (current.rigorScore !== proposal.rigorScore) {
    out.push(`rigor · ${current.rigorScore} → ${proposal.rigorScore}`)
  }
  const cCount = current.criteria?.length ?? 0
  const pCount = proposal.criteria?.length ?? 0
  if (cCount !== pCount) {
    out.push(`criteria · ${cCount} → ${pCount}`)
  }
  const cVoice = (current.voice ?? '').trim().length
  const pVoice = (proposal.voice ?? '').trim().length
  if (cVoice === 0 && pVoice > 0) {
    out.push(`voice · empty → ${pVoice}-char paragraph`)
  } else if (cVoice > 0 && pVoice === 0) {
    out.push(`voice · would clear current ${cVoice}-char paragraph`)
  }
  const nd = (current.neverDo?.length ?? 0) - (proposal.neverDo?.length ?? 0)
  if (nd !== 0) {
    out.push(`never-do · ${current.neverDo?.length ?? 0} → ${proposal.neverDo?.length ?? 0} entries`)
  }
  if (out.length === 0) {
    out.push('proposal matches current configuration')
  }
  return out.slice(0, 5)
}
