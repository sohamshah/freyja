import React from 'react'
import { BriefMemo, BriefPreview, BriefSection } from '../shared/BriefMemo'
import type { AgentView, KanbanCardView } from '../shared/types'

interface Props {
  open: boolean
  onClose: () => void
  cards: KanbanCardView[]
  agents: AgentView[]
  objective: string
}

export function DispatcherBrief({ open, onClose, cards, agents, objective }: Props) {
  const ready = cards.filter((c) => {
    const s = (c.status ?? '').toLowerCase()
    return s !== 'done' && s !== 'blocked' && s !== 'running' && s !== 'in_progress'
  }).length
  const done = cards.filter((c) => {
    const s = (c.status ?? '').toLowerCase()
    return s === 'done' || s === 'sealed' || s === 'completed'
  }).length

  const nextCard = cards.find((c) => {
    const s = (c.status ?? '').toLowerCase()
    return s !== 'done' && s !== 'blocked' && s !== 'running' && s !== 'in_progress'
  })

  return (
    <BriefMemo
      open={open}
      onClose={onClose}
      to="the dispatcher"
      toRole="autopilot"
      re={`${objective || 'mission'} · ${cards.length} cards · ${done} done · ${ready} ready`}
      date={dateStr()}
      title="Autopilot Rules"
      prelude={
        <>
          When an agent comes free, autopilot decides what they should pick up next. Tune the
          rules below to control who can claim what, when to escalate, and when to defer to the
          operator.
        </>
      }
      signoffName="soham"
      preview={
        <BriefPreview
          label="given this brief, the next dispatch will be:"
          verdict={nextCard ? 'dispatch' : 'idle'}
          reason={
            nextCard ? (
              <>
                <span className="text-fg-0">{nextCard.title}</span> — first card eligible under the
                rules. Will be assigned to the first agent that comes free.
              </>
            ) : (
              'no ready cards · waiting for a card to enter the queue.'
            )
          }
          counterfactuals={[
            {
              body: (
                <>
                  If you reduce <span className="text-fg-0">stale after</span> to{' '}
                  <code className="text-fg-0">8m</code>, current stale claims would be reclaimed sooner.
                </>
              ),
            },
            {
              body: (
                <>
                  If you set <span className="text-fg-0">max in flight</span> to{' '}
                  <code className="text-fg-0">2</code>, idle agents could pick up a second card alongside
                  their current one.
                </>
              ),
            },
            {
              body: (
                <>
                  If you set <span className="text-fg-0">escalation</span> to{' '}
                  <span className="text-fg-0">"before every dispatch"</span>, autopilot will pause and ask
                  you before each move.
                </>
              ),
            },
          ]}
        />
      }
    >
      <BriefSection marker="§ 1" title="Who can claim what">
        <p className="m-0 mb-3 text-[14px] leading-[1.7] font-mono">
          By default every agent can claim every card. Add type-restrictions or preferences below.
        </p>
        <div className="grid grid-cols-[120px_1fr] gap-3 px-2 py-1.5 text-[12px] text-fg-2">
          <span className="text-fg-3 text-[10.5px] uppercase tracking-[0.18em]">card type</span>
          <span className="text-fg-3 text-[10.5px] uppercase tracking-[0.18em]">rule</span>
          <span className="text-fg-1">research</span>
          <span className="text-fg-0">any agent · prefer agents with the <code>research</code> role tag</span>
          <span className="text-fg-1">build / migrate</span>
          <span className="text-fg-0">any agent · prefer agents with the <code>code</code> role tag</span>
          <span className="text-fg-1">verify</span>
          <span className="text-fg-0">any agent · prefer agents with the <code>verify</code> role tag</span>
          <span className="text-fg-1">cutover / risky</span>
          <span className="text-warn">never auto-dispatch · wait for the operator to assign</span>
        </div>
        <div className="mt-3 text-fg-3 text-[11px] italic">
          "Cutover / risky" cards always wait for the operator. The dispatcher never touches them.
        </div>
      </BriefSection>

      <BriefSection marker="§ 2" title="Policies">
        <div className="flex flex-col gap-1.5">
          <Policy label="stale after">A claim is stale if the agent makes no tool call for <Val>12 min</Val>.</Policy>
          <Policy label="retry policy">If a card fails, retry up to <Val>2×</Val> with the same agent before reassigning.</Policy>
          <Policy label="circuit breaker">Pause autopilot if more than <Val>3</Val> failures occur in <Val>10 min</Val>.</Policy>
          <Policy label="max in flight">No more than <Val>1 card</Val> per agent at a time.</Policy>
          <Policy label="spend cap">Pause &amp; ask the operator if mission spend exceeds <Val>$5.00</Val>.</Policy>
        </div>
      </BriefSection>

      <BriefSection marker="§ 3" title="When to ask the operator">
        <ul className="list-none p-0 m-0 flex flex-col gap-1.5 text-[13px]">
          <Radio>never · just keep going</Radio>
          <Radio checked>on stalls, regressions, and risky cards — current setting</Radio>
          <Radio>before every dispatch · most conservative</Radio>
        </ul>
      </BriefSection>

      <BriefSection marker="§ 4" title="What you should never do">
        <ul className="list-none p-0 m-0 flex flex-col gap-2 text-[13.5px] leading-[1.65]">
          <Never>Auto-dispatch a "cutover / risky" card. These always wait for the operator.</Never>
          <Never>Reclaim a card before it's gone stale. Even slow agents need a chance to think.</Never>
          <Never>Dispatch a card whose dependencies are unmet.</Never>
          <Never>Send the same card to the same agent twice in a row after a failure.</Never>
        </ul>
      </BriefSection>
    </BriefMemo>
  )
}

function Policy({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[auto_1fr_auto] gap-3.5 items-center rounded-md border border-white/[0.06] bg-white/[0.018] px-3 py-2.5">
      <span className="text-fg-3 text-[10.5px] uppercase tracking-[0.18em] min-w-[120px]">{label}</span>
      <span className="text-fg-0 text-[13px]">{children}</span>
      <button type="button" className="rounded border border-white/[0.06] bg-white/[0.03] px-2 py-0.5 text-fg-2 text-[10px] uppercase tracking-[0.18em] hover:text-fg-0 hover:bg-white/[0.06] transition">
        edit
      </button>
    </div>
  )
}

function Val({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-block rounded px-1.5 py-0.5 bg-accent/[0.06] border border-accent/[0.18] text-accent text-[12px] font-mono tabular-nums">
      {children}
    </span>
  )
}

function Radio({ checked, children }: { checked?: boolean; children: React.ReactNode }) {
  return (
    <li
      className={`grid grid-cols-[18px_1fr] gap-3 px-2 py-1.5 rounded cursor-pointer transition ${
        checked ? '' : 'hover:bg-white/[0.018]'
      }`}
    >
      <span
        className={`h-3.5 w-3.5 rounded-full border-[1.5px] flex items-center justify-center ${
          checked ? 'border-accent' : 'border-fg-3'
        }`}
      >
        {checked ? (
          <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_6px_rgba(168,212,252,0.6)]" />
        ) : null}
      </span>
      <span className={`italic font-sans font-light text-[14px] ${checked ? 'text-fg-0' : 'text-fg-1'}`}>
        {children}
      </span>
    </li>
  )
}

function Never({ children }: { children: React.ReactNode }) {
  return (
    <li className="grid grid-cols-[14px_1fr] gap-2.5 text-fg-1">
      <span className="text-danger pt-px">·</span>
      <span>{children}</span>
    </li>
  )
}

function dateStr(): string {
  const d = new Date()
  const months = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
  return `${months[d.getMonth()]} ${d.getDate()} · ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
