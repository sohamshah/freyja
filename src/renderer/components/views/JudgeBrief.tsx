import React, { useEffect, useMemo, useState } from 'react'
import { BriefMemo, BriefPreview, BriefSection } from '../shared/BriefMemo'
import type {
  BriefCriterion,
  CriterionPriority,
  JudgeRules,
  GoalStateView,
  JudgeProfile,
} from '../shared/types'
import { useHarness } from '../../state/store'

interface Props {
  open: boolean
  onClose: () => void
  goalState: GoalStateView | null
}

const PRIORITIES: CriterionPriority[] = ['must', 'should', 'may']
const JUDGE_PROFILES: JudgeProfile[] = ['quick', 'standard', 'deep']

// Tool surface the deep judge can use to verify agent claims. Read-only by
// design — see GOAL_JUDGE_SYSTEM_PROMPT for the bash-read-only contract.
const DEEP_TOOLS: Array<{ id: string; label: string; hint: string }> = [
  { id: 'read_file', label: 'read_file', hint: 'open files the agent claims' },
  { id: 'list_directory', label: 'list_directory', hint: 'inspect file trees' },
  { id: 'grep', label: 'grep', hint: 'search code for evidence' },
  { id: 'glob', label: 'glob', hint: 'match file patterns' },
  { id: 'bash', label: 'bash', hint: 'compound read-only commands (cat / awk / find / wc)' },
  { id: 'fetch_url', label: 'fetch_url', hint: 'verify cited URLs' },
]
const DEFAULT_DEEP_TOOL_IDS = DEEP_TOOLS.map((t) => t.id)

const JUDGE_PROFILE_META: Record<
  JudgeProfile,
  { name: string; cost: string; tagline: string }
> = {
  quick: {
    name: 'Quick',
    cost: 'cheap · fast',
    tagline: 'Haiku 4.5, no thinking. Use when every turn needs a sanity gate.',
  },
  standard: {
    name: 'Standard',
    cost: 'same as agent',
    tagline: 'Mirrors the agent model, no thinking. Default rigor.',
  },
  deep: {
    name: 'Deep',
    cost: 'frontier · slow',
    tagline:
      'Spawns the judge-deep subagent with extended thinking and read-only verification tools. Catches what Standard rubber-stamps.',
  },
}

export function JudgeBrief({ open, onClose, goalState }: Props) {
  const updateJudgeRules = useHarness((s) => s.updateJudgeRules)
  const persistedBrief: JudgeRules = useMemo(
    () => goalState?.judgeRules ?? defaultBrief(),
    [goalState?.judgeRules],
  )

  // Local editor state — committed on blur / button click, then pushed via
  // the store's IPC action. The persisted copy still arrives back through
  // the goal_brief_updated event, but we mirror it locally for fluent edits.
  const [voice, setVoice] = useState(persistedBrief.voice)
  const [rigor, setRigor] = useState(persistedBrief.rigorScore)
  const [judgeProfile, setJudgeProfile] = useState<JudgeProfile>(
    persistedBrief.judgeProfile ?? 'standard',
  )
  const [criteria, setCriteria] = useState<BriefCriterion[]>(persistedBrief.criteria)
  const [neverDo, setNeverDo] = useState<string[]>(persistedBrief.neverDo)
  const [whenToStop, setWhenToStop] = useState(persistedBrief.whenToStop)
  // judgeTools: empty array = use profile default. Otherwise an explicit
  // allowlist. We keep "default" and "custom" as distinct states so the
  // operator can toggle a single tool off without committing to managing
  // the whole list manually.
  const [judgeTools, setJudgeTools] = useState<string[]>(persistedBrief.judgeTools ?? [])
  const [judgeMaxIter, setJudgeMaxIter] = useState<number>(
    persistedBrief.judgeMaxIterations ?? 3,
  )

  // Tracks whether the most recent state was set by an *open* transition.
  // The sync effect below treats the first sync after open as authoritative
  // (clobbers any leftover state) but ignores remote updates while the
  // operator has pending edits — so we don't blow away in-progress work.
  const justOpenedRef = React.useRef(false)

  const dirty =
    voice !== persistedBrief.voice ||
    rigor !== persistedBrief.rigorScore ||
    judgeProfile !== (persistedBrief.judgeProfile ?? 'standard') ||
    JSON.stringify(criteria) !== JSON.stringify(persistedBrief.criteria) ||
    JSON.stringify(neverDo) !== JSON.stringify(persistedBrief.neverDo) ||
    whenToStop !== persistedBrief.whenToStop ||
    JSON.stringify(judgeTools) !== JSON.stringify(persistedBrief.judgeTools ?? []) ||
    judgeMaxIter !== (persistedBrief.judgeMaxIterations ?? 3)

  // Reset the just-opened flag whenever the modal opens.
  useEffect(() => {
    if (open) justOpenedRef.current = true
  }, [open])

  // Sync local editor state from the persisted brief when:
  //   · the modal opens (always — load the latest)
  //   · OR the persisted brief changes server-side while the modal is open
  //     AND the operator has no pending edits (avoid clobbering their work).
  useEffect(() => {
    if (!open) return
    if (dirty && !justOpenedRef.current) return
    justOpenedRef.current = false
    setVoice(persistedBrief.voice)
    setRigor(persistedBrief.rigorScore)
    setJudgeProfile(persistedBrief.judgeProfile ?? 'standard')
    setCriteria(persistedBrief.criteria)
    setNeverDo(persistedBrief.neverDo)
    setWhenToStop(persistedBrief.whenToStop)
    setJudgeTools(persistedBrief.judgeTools ?? [])
    setJudgeMaxIter(persistedBrief.judgeMaxIterations ?? 3)
    // We intentionally exclude `dirty` from the deps — its identity changes
    // every render and would loop. justOpenedRef covers the initial-open
    // path; remote updates while editing fall through the dirty check.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, persistedBrief])

  if (!goalState) return null

  const save = () => {
    updateJudgeRules({
      voice: voice.trim(),
      rigorScore: clamp(rigor, 1, 10),
      judgeProfile,
      criteria: criteria
        .filter((c) => c.text.trim().length > 0)
        .map((c) => ({ id: c.id, text: c.text.trim(), priority: c.priority })),
      neverDo: neverDo.map((s) => s.trim()).filter((s) => s.length > 0),
      whenToStop: whenToStop.trim(),
      judgeTools,
      judgeMaxIterations: clamp(judgeMaxIter, 1, 10),
    })
  }

  const toolsCustom = judgeTools.length > 0
  const activeTools = toolsCustom ? judgeTools : DEFAULT_DEEP_TOOL_IDS
  const toggleTool = (id: string) => {
    // Switch from default to custom on first toggle so the user sees their
    // intent persist. Removing the last tool resets back to "default" since
    // an empty allowlist would brick the judge.
    const base = toolsCustom ? judgeTools : DEFAULT_DEEP_TOOL_IDS
    const next = base.includes(id) ? base.filter((t) => t !== id) : [...base, id]
    setJudgeTools(next.length > 0 ? next : [])
  }

  return (
    <BriefMemo
      open={open}
      onClose={onClose}
      to="judge"
      toRole={goalState.lastVerdict?.done ? 'last verdict: done' : 'acting as judge'}
      re={`${goalState.goal} · turn ${goalState.turnsUsed} of ${goalState.maxTurns}`}
      date={dateStr()}
      title="Judge Rules"
      prelude={
        <>
          The judge reads each turn and decides whether the work satisfies this goal. Tune the
          rules below to shape its rigor and voice; updates take effect on the next judge call.
          The judge is skeptical by default and will not mark <code>done</code> until every{' '}
          <code>must</code> criterion is explicitly met.
        </>
      }
      signoffName="operator"
      preview={
        <PreviewPane
          goalState={goalState}
          dirty={dirty}
          onSave={save}
        />
      }
    >
      <BriefSection marker="§ 0" title="Judge profile">
        <p className="m-0 mb-3 font-mono text-[12.5px] leading-[1.6] text-fg-2">
          Profile picks the model + thinking budget for every judge call. Cheaper profiles are
          faster but rubber-stamp more often; deeper profiles catch more but cost more.
        </p>
        <div className="grid grid-cols-3 gap-2">
          {JUDGE_PROFILES.map((p) => {
            const meta = JUDGE_PROFILE_META[p]
            const active = judgeProfile === p
            return (
              <button
                key={p}
                type="button"
                onClick={() => setJudgeProfile(p)}
                className={`flex flex-col gap-1 rounded-md border px-3 py-2.5 text-left transition ${
                  active
                    ? 'border-accent/[0.4] bg-accent/[0.10]'
                    : 'border-white/[0.06] bg-white/[0.018] hover:border-white/[0.16] hover:bg-white/[0.04]'
                }`}
              >
                <div className="flex items-baseline justify-between font-mono text-[12px]">
                  <span className={active ? 'text-accent' : 'text-fg-0'}>{meta.name}</span>
                  <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
                    {meta.cost}
                  </span>
                </div>
                <div className="font-mono text-[11.5px] leading-[1.55] text-fg-2">
                  {meta.tagline}
                </div>
              </button>
            )
          })}
        </div>
      </BriefSection>

      <BriefSection marker="§ 1" title="Rigor">
        <div className="flex items-center gap-4">
          <input
            type="range"
            min={1}
            max={10}
            value={rigor}
            onChange={(e) => setRigor(parseInt(e.target.value, 10))}
            className="flex-1 accent-[#a8d4fc]"
          />
          <div className="min-w-[110px] font-mono text-[12px] tabular-nums text-fg-1">
            <span className="text-fg-0">{rigor}</span>/10 · {rigorLabel(rigor)}
          </div>
        </div>
        <p className="m-0 mt-2 font-mono text-[11.5px] italic leading-[1.55] text-fg-3">
          Higher rigor = more demanding evidence required, fewer "good enough" verdicts.
        </p>
      </BriefSection>

      <BriefSection marker="§ 2" title="Voice">
        <textarea
          value={voice}
          onChange={(e) => setVoice(e.target.value)}
          rows={5}
          placeholder="Write a few sentences describing how the judge should think. Specific guidance beats vague adjectives — e.g. 'When a claim is quantitative, demand a citation. When the work proposes a solution, name a failure mode the agent didn't consider.'"
          className="w-full resize-y rounded-md border border-white/[0.06] bg-white/[0.02] px-3 py-2 font-mono text-[13px] leading-[1.6] text-fg-0 outline-none placeholder:text-fg-4 focus:border-accent/[0.32] focus:bg-white/[0.04]"
        />
      </BriefSection>

      <BriefSection
        marker="§ 3"
        title="Required criteria"
        control={
          <button
            type="button"
            onClick={() =>
              setCriteria([
                ...criteria,
                {
                  id: `crit_${Math.random().toString(16).slice(2, 10)}`,
                  text: '',
                  priority: 'must',
                },
              ])
            }
            className="rounded border border-white/[0.06] bg-white/[0.04] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-2 transition hover:bg-white/[0.08] hover:text-fg-0"
          >
            + add
          </button>
        }
      >
        {criteria.length === 0 ? (
          <p className="m-0 font-mono text-[12.5px] italic text-fg-3">
            No criteria yet. The judge will use only its own judgment. Add must/should/may items
            for the things the operator wants explicitly tracked.
          </p>
        ) : (
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
            {criteria.map((c, idx) => (
              <li
                key={c.id}
                className="grid grid-cols-[72px_1fr_auto] items-center gap-2 rounded-md border border-white/[0.06] bg-white/[0.018] px-2 py-1.5"
              >
                <select
                  value={c.priority}
                  onChange={(e) => {
                    const next = [...criteria]
                    next[idx] = { ...c, priority: e.target.value as CriterionPriority }
                    setCriteria(next)
                  }}
                  className="rounded border border-white/[0.06] bg-white/[0.04] px-1.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-1 outline-none focus:border-accent/[0.32]"
                >
                  {PRIORITIES.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
                <input
                  value={c.text}
                  onChange={(e) => {
                    const next = [...criteria]
                    next[idx] = { ...c, text: e.target.value }
                    setCriteria(next)
                  }}
                  placeholder="What must the work demonstrate?"
                  className="bg-transparent px-1 py-0.5 font-mono text-[13px] text-fg-0 outline-none placeholder:text-fg-4"
                />
                <button
                  type="button"
                  onClick={() => setCriteria(criteria.filter((_, i) => i !== idx))}
                  className="rounded px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3 transition hover:bg-white/[0.06] hover:text-warn"
                >
                  remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </BriefSection>

      <BriefSection
        marker="§ 4"
        title="Never do (hard constraints)"
        control={
          <button
            type="button"
            onClick={() => setNeverDo([...neverDo, ''])}
            className="rounded border border-white/[0.06] bg-white/[0.04] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-2 transition hover:bg-white/[0.08] hover:text-fg-0"
          >
            + add
          </button>
        }
      >
        {neverDo.length === 0 ? (
          <p className="m-0 font-mono text-[12.5px] italic text-fg-3">
            No hard constraints. Add things the judge must refuse to approve (e.g. fabricated
            citations, unsourced claims, premature ship-it).
          </p>
        ) : (
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
            {neverDo.map((text, idx) => (
              <li
                key={idx}
                className="grid grid-cols-[14px_1fr_auto] items-center gap-2 rounded-md border border-white/[0.06] bg-white/[0.018] px-2 py-1.5"
              >
                <span className="text-danger">·</span>
                <input
                  value={text}
                  onChange={(e) => {
                    const next = [...neverDo]
                    next[idx] = e.target.value
                    setNeverDo(next)
                  }}
                  placeholder="Hard constraint the judge must honor"
                  className="bg-transparent px-1 py-0.5 font-mono text-[13px] text-fg-0 outline-none placeholder:text-fg-4"
                />
                <button
                  type="button"
                  onClick={() => setNeverDo(neverDo.filter((_, i) => i !== idx))}
                  className="rounded px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3 transition hover:bg-white/[0.06] hover:text-warn"
                >
                  remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </BriefSection>

      <BriefSection marker="§ 5" title="When to stop">
        <textarea
          value={whenToStop}
          onChange={(e) => setWhenToStop(e.target.value)}
          rows={3}
          placeholder="Optional extra termination logic. The default (all must met, no open questions, confidence ≥ 0.85) always applies. Add anything else — 'stop if the agent proposes a second cutover plan,' 'stop on the third regression,' etc."
          className="w-full resize-y rounded-md border border-white/[0.06] bg-white/[0.02] px-3 py-2 font-mono text-[13px] leading-[1.6] text-fg-0 outline-none placeholder:text-fg-4 focus:border-accent/[0.32] focus:bg-white/[0.04]"
        />
      </BriefSection>

      {judgeProfile === 'deep' && (
        <BriefSection marker="§ 6" title="Deep judge controls">
          <p className="m-0 mb-3 font-mono text-[12.5px] leading-[1.6] text-fg-2">
            The deep judge runs as a subagent with read-only tools and extended thinking.
            Toggle individual tools to constrain its surface; raise iterations when verdicts
            need more verification work per turn.
          </p>

          <div className="mb-3">
            <div className="mb-1.5 flex items-center justify-between font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
              <span>Tool allowlist</span>
              <span className="text-fg-4">
                {toolsCustom ? 'custom' : 'profile default'}
                {toolsCustom && (
                  <button
                    type="button"
                    onClick={() => setJudgeTools([])}
                    className="ml-2 rounded px-1.5 py-0.5 font-mono text-[10px] text-fg-3 hover:bg-white/[0.06] hover:text-fg-1"
                  >
                    reset
                  </button>
                )}
              </span>
            </div>
            <div className="flex flex-col gap-1.5">
              {DEEP_TOOLS.map((tool) => {
                const active = activeTools.includes(tool.id)
                return (
                  <label
                    key={tool.id}
                    className={`flex cursor-pointer items-center gap-2 rounded-md border px-2.5 py-1.5 transition ${
                      active
                        ? 'border-white/[0.10] bg-white/[0.035]'
                        : 'border-white/[0.04] bg-white/[0.012] opacity-60 hover:opacity-100'
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={active}
                      onChange={() => toggleTool(tool.id)}
                      className="h-3.5 w-3.5 accent-[#a8d4fc]"
                    />
                    <span className="font-mono text-[12px] text-fg-0">{tool.label}</span>
                    <span className="font-mono text-[11px] italic text-fg-3">— {tool.hint}</span>
                  </label>
                )
              })}
            </div>
          </div>

          <div>
            <div className="mb-1.5 flex items-center justify-between font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
              <span>Max iterations per verdict</span>
              <span className="font-mono text-[11px] tabular-nums text-fg-1">
                <span className="text-fg-0">{judgeMaxIter}</span>/10
              </span>
            </div>
            <input
              type="range"
              min={1}
              max={10}
              value={judgeMaxIter}
              onChange={(e) => setJudgeMaxIter(parseInt(e.target.value, 10))}
              className="w-full accent-[#a8d4fc]"
            />
            <p className="m-0 mt-1.5 font-mono text-[11.5px] italic leading-[1.55] text-fg-3">
              Each iteration = one think+act step. The judge usually finishes in 1–3 with
              read-only tools; raise this if you want it to verify multiple claims in depth.
            </p>
          </div>
        </BriefSection>
      )}
    </BriefMemo>
  )
}

function PreviewPane({
  goalState,
  dirty,
  onSave,
}: {
  goalState: GoalStateView
  dirty: boolean
  onSave: () => void
}) {
  return (
    <BriefPreview
      label="given this brief and the latest turn, the judge will:"
      verdict={goalState.lastVerdict?.done ? 'done (last)' : 'continue (last)'}
      reason={
        goalState.lastVerdict?.reason ?? (
          <span className="italic text-fg-3">
            no verdict yet — the brief will apply on the first judge call.
          </span>
        )
      }
      counterfactuals={[
        {
          body: (
            <>
              Adding a <span className="text-fg-0">must</span>-criterion forces the judge to
              explicitly check it every turn. Operator criteria are backfilled as{' '}
              <span className="text-fg-0">missing</span> when the judge omits them.
            </>
          ),
        },
        {
          body: (
            <>
              Raising rigor above <span className="text-fg-0">8</span> typically adds 2–4 turns
              to a goal; the judge demands more evidence and resists premature{' '}
              <span className="text-fg-0">done</span>.
            </>
          ),
        },
        {
          body: (
            <>
              The loop will never mark <span className="text-fg-0">done</span> while any open
              question remains — even if you don't add a single must-criterion, naming gaps in
              your voice instruction will keep the judge honest.
            </>
          ),
        },
      ]}
      footer={
        <div className="flex items-center justify-between gap-3 border-t border-white/[0.06] px-5 py-3">
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
            {dirty ? 'unsaved changes' : 'saved'}
          </span>
          <button
            type="button"
            onClick={onSave}
            disabled={!dirty}
            className={`rounded-md border px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.14em] transition ${
              dirty
                ? 'border-accent/[0.32] bg-accent/[0.12] text-accent hover:bg-accent/[0.18]'
                : 'cursor-not-allowed border-white/[0.06] bg-white/[0.02] text-fg-3'
            }`}
          >
            Save brief
          </button>
        </div>
      }
    />
  )
}

function defaultBrief(): JudgeRules {
  return {
    voice: '',
    rigorScore: 6,
    judgeProfile: 'standard',
    criteria: [],
    neverDo: [],
    whenToStop: '',
  }
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(n, hi))
}

function rigorLabel(score: number): string {
  if (score <= 2) return 'lenient'
  if (score <= 4) return 'easy'
  if (score <= 6) return 'moderate'
  if (score <= 8) return 'demanding'
  return 'unforgiving'
}

function dateStr(): string {
  const d = new Date()
  const months = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
  return `${months[d.getMonth()]} ${d.getDate()} · ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
