import React, { useEffect, useMemo, useState } from 'react'
import { useHarness } from '../state/store'
import { useVoiceStore } from '../state/voice-store'
import { useSchedulerStore } from '../state/scheduler-store'
import { FALLBACK_MODELS } from './ModelPicker'
import type { JevPaletteDecision, ArtifactIndexRow } from '../../shared/events'
import {
  type PaletteSlots,
  type PaletteWidgetType,
  instructionFor,
  requiredSlots,
  slotFilled,
  TOOL_NAMES,
} from '../lib/paletteIntent'
import { Frame, Well, Label, Btn, Slot, Progress, type WidgetProps } from './palette/chrome'
import { CalendarCreate, CalendarView } from './palette/cards/CalendarCards'
import { SkillLookup } from './palette/cards/SkillLookup'
import { PrTriage } from './palette/cards/PrTriage'
import { SlackRead } from './palette/cards/SlackRead'
import { ArtifactPreview } from './palette/cards/ArtifactPreview'
import { ContextBudget } from './palette/cards/ContextBudget'

export type { WidgetProps }

/**
 * Generative widgets for the ⌘K palette.
 *
 * Every widget receives the same three things: the raw query, Jev's
 * decision, and the slots parsed from the query. A widget stays mounted
 * while the person keeps typing inside its domain, so it fills in one
 * field at a time instead of re-appearing. Empty slots render as quiet
 * placeholders; the primary action turns on once the required ones are in.
 */

export function JevWidget(p: WidgetProps) {
  switch (p.type) {
    case 'calendar_create':
      return <CalendarCreate {...p} />
    case 'calendar_view':
      return <CalendarView {...p} />
    case 'memory':
      return <MemoryRecall {...p} />
    case 'agent_task':
      return <AgentTask {...p} />
    case 'model_picker':
      return <ModelPicker {...p} />
    case 'token_usage':
      return <TokenUsage {...p} />
    case 'artifact_explorer':
      return <ArtifactExplorer {...p} />
    case 'briefing':
      return <Briefing {...p} />
    case 'swarm':
      return <Swarm {...p} />
    case 'tool_inspect':
      return <ToolInspect {...p} />
    case 'diagnostics':
      return <Diagnostics {...p} />
    case 'voice':
      return <Voice {...p} />
    case 'appearance':
      return <Appearance {...p} />
    case 'mic':
      return <Mic {...p} />
    case 'quick_controls':
      return <QuickControls {...p} />
    case 'finance':
      return <Finance {...p} />
    case 'skill_lookup':
      return <SkillLookup {...p} />
    case 'pr_triage':
      return <PrTriage {...p} />
    case 'slack_read':
      return <SlackRead {...p} />
    case 'artifact_preview':
      return <ArtifactPreview {...p} />
    case 'context_budget':
      return <ContextBudget {...p} />
  }
}

function useRequired(type: PaletteWidgetType, slots: PaletteSlots) {
  const req = requiredSlots(type)
  const filled = req.filter((k) => slotFilled(slots, k)).length
  return { req, filled, total: req.length, ready: filled === req.length }
}

function timeRange(t: PaletteSlots['time'], dur: number) {
  if (!t) return undefined
  const end = new Date(2000, 0, 1, t.hour, t.minute + dur)
  const h12 = end.getHours() % 12 === 0 ? 12 : end.getHours() % 12
  const endLabel = `${h12}:${String(end.getMinutes()).padStart(2, '0')} ${end.getHours() >= 12 ? 'PM' : 'AM'}`
  return `${t.label} – ${endLabel}`
}

// ─── memory ───────────────────────────────────────────────────────────────

function MemoryRecall({ query, slots, onClose, onToast, crumbs }: WidgetProps) {
  const sendMessage = useHarness((s) => s.sendMessage)
  const sessions = useHarness((s) => s.sessions)
  const needle = (slots.keyword ?? slots.topic ?? slots.person ?? '').toLowerCase()
  const related = useMemo(() => {
    if (!needle) return []
    return sessions.filter((s) => s.title.toLowerCase().includes(needle)).slice(0, 4)
  }, [sessions, needle])
  const instruction = instructionFor('memory', query, slots)!

  return (
    <Frame
      crumbs={crumbs}
      kicker="Memory"
      title={slots.person ? `What ${slots.person} said` : 'Recall'}
      badge={related.length ? `${related.length} related sessions` : 'memory + transcripts'}
      footer={
        <>
          <Btn
            onClick={() => {
              useHarness.getState().setInputDraft(instruction)
              onClose()
            }}
          >
            Edit as prompt
          </Btn>
          <Btn
            primary
            onClick={() => {
              void sendMessage(instruction)
              onToast('Recalling…')
              onClose()
            }}
          >
            Recall
          </Btn>
        </>
      }
    >
      <Well className="space-y-1.5">
        <Slot label="Who" value={slots.person} hint="a person? “what did nari say”" />
        <Slot label="About" value={slots.topic ?? slots.keyword} hint="a topic? “about lisbon”" />
        <Slot label="When" value={slots.periodLabel ?? slots.day?.label} hint="a timeframe? “last week”" />
      </Well>
      {related.length > 0 && (
        <div className="space-y-1">
          <Label>Sessions mentioning “{needle}”</Label>
          {related.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => {
                useHarness.getState().openSessionPane(s.id)
                onClose()
              }}
              className="flex w-full items-center justify-between rounded px-2 py-1 text-left text-[11px] text-fg-1 ring-hairline hover:bg-white/[0.04] hover:text-fg-0"
            >
              <span className="truncate">{s.title}</span>
              <span className="ml-2 shrink-0 text-[9.5px] text-fg-3">{new Date(s.updatedAt).toLocaleDateString()}</span>
            </button>
          ))}
        </div>
      )}
    </Frame>
  )
}

// ─── agent task ───────────────────────────────────────────────────────────

const PROFILE_MODEL: Record<string, string> = {
  review: 'gpt-5.5 (independent model)',
  explore: 'claude-sonnet-5-5',
  test: 'parent model',
  code: 'parent model · high thinking',
  verify: 'gpt-5.5 (independent model)',
}

function AgentTask({ type, query, slots, onClose, onToast, crumbs }: WidgetProps) {
  const sendMessage = useHarness((s) => s.sendMessage)
  const { filled, total, ready } = useRequired(type, slots)
  const profile = slots.profile
  const target = slots.prNumber ? `PR #${slots.prNumber}${slots.repo ? ` on ${slots.repo}` : ''}` : slots.repo
  const instruction = instructionFor('agent_task', query, slots)!

  return (
    <Frame
      crumbs={crumbs}
      kicker="Delegate"
      title={target ?? 'Sub-agent task'}
      badge={<Progress filled={filled} total={total} />}
      footer={
        <>
          <Btn
            onClick={() => {
              useHarness.getState().setInputDraft(instruction)
              onClose()
            }}
          >
            Edit as prompt
          </Btn>
          <Btn
            primary
            disabled={!ready}
            onClick={() => {
              void sendMessage(instruction)
              onToast(`Spawning ${profile} sub-agent`)
              onClose()
            }}
          >
            Spawn {profile ?? 'sub-agent'}
          </Btn>
        </>
      }
    >
      <Well className="space-y-1.5">
        <Slot label="Profile" value={profile} hint="what kind? “review”, “explore”, “test”, “implement”" />
        <Slot label="Model" value={profile ? PROFILE_MODEL[profile] : undefined} hint="" />
        <Slot label="Target" value={target} hint="which PR or repo? “pr 1321 on agent-harness”" />
        <Slot label="Focus" value={slots.focus} hint="a lens? “for security”, “for performance”" />
      </Well>
      <div className="text-[10.5px] text-fg-3">Runs in the background; a memo lands in this conversation when it finishes.</div>
    </Frame>
  )
}

// ─── models ───────────────────────────────────────────────────────────────

const LEVEL_SHORT: Record<string, string> = { minimal: 'min', medium: 'med', xhigh: 'x-high' }

function ModelPicker({ slots, onClose, onToast, crumbs }: WidgetProps) {
  const reported = useHarness((s) => s.availableModels)
  const models = reported.length > 0 ? reported : FALLBACK_MODELS
  const current = useHarness((s) => s.model)
  const reasoningLevel = useHarness((s) => s.reasoningLevel)
  const setModel = useHarness((s) => s.setModel)
  const q = slots.modelQuery?.toLowerCase().replace(/[\s-]/g, '')
  const matches = (m: { id: string; label: string; family: string }) =>
    !!q && `${m.id} ${m.label} ${m.family}`.toLowerCase().replace(/[\s-]/g, '').includes(q)
  // Provider keys arrive after the first paint; rows without one dim and
  // drop out of the slot-driven pick, so the card settles on something usable.
  const [keys, setKeys] = useState<Record<string, boolean> | null>(null)
  useEffect(() => {
    let alive = true
    void window.harness?.llmKeysProbe?.().then((k: any) => alive && setKeys(k?.keys ?? k?.present ?? null)).catch(() => {})
    return () => {
      alive = false
    }
  }, [])
  const hasKey = (m: { envVar?: string }) => !keys || !m.envVar || keys[m.envVar] !== false
  const ranked = useMemo(
    () =>
      [...models].sort(
        (a, b) =>
          Number(matches(b) && hasKey(b)) - Number(matches(a) && hasKey(a)) ||
          Number(matches(b)) - Number(matches(a)) ||
          Number(hasKey(b)) - Number(hasKey(a)) ||
          (a.id === current ? -1 : b.id === current ? 1 : 0),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [models, q, current, keys],
  )
  const best = q ? (ranked.find((m) => matches(m) && hasKey(m)) ?? ranked.find(matches)) : undefined

  const [pickedOverride, setPickedOverride] = useState<string | null>(null)
  const [levelOverride, setLevelOverride] = useState<string | null>(null)
  useEffect(() => setPickedOverride(null), [best?.id])
  useEffect(() => setLevelOverride(null), [slots.reasoning])

  const pickedId = pickedOverride ?? best?.id ?? current
  const picked = models.find((m) => m.id === pickedId)
  const levels = picked?.reasoningLevels ?? []
  const level = (() => {
    if (levelOverride && levels.includes(levelOverride)) return levelOverride
    if (slots.reasoning && levels.includes(slots.reasoning)) return slots.reasoning
    if (pickedId === current && levels.includes(reasoningLevel)) return reasoningLevel
    return picked?.reasoningDefault ?? levels[0]
  })()
  const modelChanged = pickedId !== current
  const levelChanged = !!level && level !== reasoningLevel
  const nowLabel = models.find((m) => m.id === current)?.label ?? current

  const apply = () => {
    void setModel(pickedId, level)
    onToast(
      modelChanged
        ? `Model → ${picked?.label ?? pickedId}${level ? ` · ${level} thinking` : ''}`
        : `Thinking → ${level}`,
    )
    onClose()
  }

  return (
    <Frame
      crumbs={crumbs}
      kicker="Model"
      title={modelChanged ? `Switch to ${picked?.label ?? pickedId}` : levelChanged ? `Set thinking to ${level}` : 'Available models'}
      badge={`now: ${nowLabel}${reasoningLevel ? ` · ${reasoningLevel}` : ''}`}
      footer={
        modelChanged || levelChanged ? (
          <Btn primary onClick={apply}>
            {modelChanged ? `Switch to ${picked?.label ?? pickedId}` : 'Apply'}
            {level ? ` · ${level}` : ''}
          </Btn>
        ) : (
          <Btn onClick={onClose}>Close</Btn>
        )
      }
    >
      <div className="max-h-[220px] space-y-1 overflow-y-auto">
        {ranked.slice(0, 8).map((m) => {
          const isCurrent = m.id === current
          const isPicked = m.id === pickedId
          const keyed = hasKey(m)
          return (
            <button
              key={m.id}
              type="button"
              onClick={() => setPickedOverride(m.id)}
              className={`flex w-full items-center justify-between rounded-md px-2.5 py-1.5 text-left transition-colors ${
                isPicked ? 'bg-accent/15 ring-1 ring-accent/40' : 'ring-hairline hover:bg-white/[0.04]'
              }`}
            >
              <span className="min-w-0">
                <span className={`flex items-center gap-1.5 text-[11.5px] ${keyed ? 'text-fg-0' : 'text-fg-3'}`}>
                  {m.label}
                  {isCurrent && <span className="kbd text-[8.5px]">current</span>}
                  {!keyed && <span className="kbd text-[8.5px] text-warn">no key</span>}
                </span>
                <span className="block truncate text-[10px] text-fg-3">{m.description}</span>
              </span>
              <span className="ml-3 shrink-0 text-right text-[10px] text-fg-3">
                <span className="block">{m.tier}</span>
                <span className="block">{Math.round(m.contextWindow / 1000)}k ctx</span>
              </span>
            </button>
          )
        })}
      </div>
      {levels.length > 1 && (
        <div className="mt-2 flex items-center gap-2">
          <Label>Thinking</Label>
          <div className="flex flex-wrap gap-1">
            {levels.map((l) => {
              const on = l === level
              return (
                <button
                  key={l}
                  type="button"
                  onClick={() => setLevelOverride(l)}
                  className={`rounded px-2 py-0.5 font-mono text-[10.5px] transition-colors ${
                    on ? 'bg-accent/20 text-fg-0 ring-1 ring-accent/50' : 'text-fg-2 ring-hairline hover:bg-white/[0.05] hover:text-fg-0'
                  }`}
                >
                  {LEVEL_SHORT[l] ?? l}
                </button>
              )
            })}
          </div>
          {picked?.reasoningMode && picked.reasoningMode !== 'none' && (
            <span className="ml-auto text-[10px] text-fg-3">{picked.reasoningMode}</span>
          )}
        </div>
      )}
    </Frame>
  )
}

// ─── token usage ──────────────────────────────────────────────────────────

function fmtTokens(n: number) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}k` : String(n)
}

function TokenUsage({ slots, onClose, crumbs }: WidgetProps) {
  const sessions = useHarness((s) => s.sessions)
  const live = useHarness((s) => s.usage)
  const days = slots.periodDays ?? 7
  const label = slots.periodLabel ?? 'Last 7 days'
  const modelQ = slots.modelQuery?.toLowerCase()
  const stats = useMemo(() => {
    const since = Date.now() - days * 86_400_000
    const rows = sessions.filter((s) => s.updatedAt >= since && (!modelQ || s.model.toLowerCase().includes(modelQ)))
    const byModel = new Map<string, { cost: number; tokens: number; n: number }>()
    let cost = 0
    let input = 0
    let output = 0
    let cached = 0
    for (const s of rows) {
      cost += s.totalCost ?? 0
      input += s.totalInputTokens
      output += s.totalOutputTokens
      cached += s.cacheReadTokens
      const b = byModel.get(s.model) ?? { cost: 0, tokens: 0, n: 0 }
      b.cost += s.totalCost ?? 0
      b.tokens += s.totalInputTokens + s.totalOutputTokens
      b.n += 1
      byModel.set(s.model, b)
    }
    const models = [...byModel.entries()].sort((a, b) => b[1].cost - a[1].cost).slice(0, 5)
    return { n: rows.length, cost, input, output, cached, models }
  }, [sessions, days, modelQ])
  const cacheRate = stats.input + stats.cached > 0 ? Math.round((stats.cached / (stats.input + stats.cached)) * 100) : 0

  return (
    <Frame
      crumbs={crumbs}
      kicker="Usage"
      title={`${label}${modelQ ? ` · ${modelQ}` : ''}`}
      badge={`${stats.n} sessions`}
      footer={
        <>
          <Btn onClick={onClose}>Close</Btn>
          <Btn
            primary
            onClick={() => {
              useHarness.getState().toggleMissionDashboard(true, 'telemetry')
              onClose()
            }}
          >
            Open telemetry
          </Btn>
        </>
      }
    >
      <div className="flex items-baseline justify-between">
        <div>
          <Label>Spend</Label>
          <div className="text-[24px] text-fg-0">
            ${stats.cost.toFixed(2)}
            <span className="ml-2 text-[12px] text-fg-2">
              {fmtTokens(stats.input + stats.output)} tok · {fmtTokens(stats.cached)} cached
            </span>
          </div>
        </div>
        <div className="text-right text-[10.5px] text-fg-2">
          <div>cache read {cacheRate}%</div>
          <div className="text-fg-3">this session ${live.totalCost.toFixed(2)}</div>
        </div>
      </div>
      <div className="space-y-1">
        {stats.models.map(([model, b]) => (
          <div key={model} className="flex items-center justify-between rounded px-2 py-1 text-[11px] ring-hairline">
            <span className="truncate text-fg-1">{model}</span>
            <span className="ml-2 shrink-0 text-fg-3">
              {b.n} · {fmtTokens(b.tokens)} · <span className="text-fg-0">${b.cost.toFixed(2)}</span>
            </span>
          </div>
        ))}
        {stats.models.length === 0 && <div className="py-3 text-center text-[11px] text-fg-3">No sessions in this window.</div>}
      </div>
    </Frame>
  )
}

// ─── artifacts ────────────────────────────────────────────────────────────

function ArtifactExplorer({ slots, onClose, onToast, handoff, crumbs }: WidgetProps) {
  const [rows, setRows] = useState<ArtifactIndexRow[]>([])
  const [total, setTotal] = useState(0)
  const text = slots.keyword ?? slots.topic ?? ''
  const types = slots.fileTypes
  useEffect(() => {
    let alive = true
    if (!window.harness?.artifactIndexQuery) return
    void window.harness
      .artifactIndexQuery({ text: text || undefined, types: types.length ? types : undefined, onlyExisting: true, sort: 'newest', limit: 6 })
      .then((r: { ok: boolean; rows: ArtifactIndexRow[]; total: number }) => {
        if (!alive || !r?.ok) return
        setRows(r.rows)
        setTotal(r.total)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [text, types.join(',')])

  return (
    <Frame
      crumbs={crumbs}
      kicker="Artifacts"
      title={text ? `“${text}”` : types.length ? `${types.join('/')} files` : 'Recent'}
      badge={`${total} match${total === 1 ? '' : 'es'}`}
      footer={
        <Btn
          primary
          onClick={() => {
            useHarness.getState().toggleArtifactsBrowser(true, rows[0]?.path ?? null)
            onClose()
          }}
        >
          Open browser (⌘⇧A)
        </Btn>
      }
    >
      <Well className="space-y-1.5">
        <Slot label="Type" value={types.length ? types.join(', ') : undefined} hint="a kind? “html reports”, “png”" />
        <Slot label="About" value={text || undefined} hint="a topic? “about jev”" />
      </Well>
      <div className="space-y-1">
        {rows.map((r) => (
          <button
            key={r.id}
            type="button"
            onClick={() => handoff({ type: 'artifact_preview', label: 'Preview', context: { path: r.path, row: r } })}
            className="flex w-full items-center justify-between rounded px-2 py-1 text-left ring-hairline hover:bg-white/[0.04]"
          >
            <span className="min-w-0">
              <span className="block truncate text-[11.5px] text-fg-0">{r.filename}</span>
              <span className="block truncate text-[10px] text-fg-3">{r.sessionTitle ?? r.sessionId}</span>
            </span>
            <span className="ml-2 flex shrink-0 items-center gap-2 text-[9.5px] text-fg-3">
              <span>{(r.bytes / 1024).toFixed(0)} KB</span>
              <span
                className="kbd text-[8.5px]"
                onClick={(e) => {
                  e.stopPropagation()
                  useHarness.getState().toggleArtifactsBrowser(true, r.path)
                  onClose()
                }}
              >
                open
              </span>
              <span
                className="kbd text-[8.5px]"
                onClick={(e) => {
                  e.stopPropagation()
                  void navigator.clipboard.writeText(r.path)
                  onToast('Path copied')
                }}
              >
                copy path
              </span>
            </span>
          </button>
        ))}
        {rows.length === 0 && <div className="py-3 text-center text-[11px] text-fg-3">Nothing indexed for that yet.</div>}
      </div>
    </Frame>
  )
}

// ─── briefing ─────────────────────────────────────────────────────────────

function Briefing({ onClose, crumbs }: WidgetProps) {
  const sessions = useHarness((s) => s.sessions)
  const subagents = useHarness((s) => s.subagents)
  const jobs = useSchedulerStore((s) => s.jobs)
  const today = new Date()
  const since = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()
  const todays = sessions.filter((s) => s.updatedAt >= since)
  const running = Object.values(subagents).filter((a) => a.state === 'running')
  const upcoming = jobs
    .filter((j) => j.enabled && j.next_fire_at)
    .sort((a, b) => (a.next_fire_at ?? 0) - (b.next_fire_at ?? 0))
    .slice(0, 3)

  return (
    <Frame
      crumbs={crumbs}
      kicker="Briefing"
      title={today.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
      badge="Morning Room"
      footer={
        <Btn
          primary
          onClick={() => {
            useHarness.getState().toggleMorningRoom(true)
            onClose()
          }}
        >
          Open Morning Room (⌘⇧B)
        </Btn>
      }
    >
      <div className="grid grid-cols-3 gap-2">
        {[
          ['Sessions today', todays.length],
          ['Agents running', running.length],
          ['Scheduled', upcoming.length],
        ].map(([k, v]) => (
          <Well key={String(k)} className="!p-2.5">
            <div className="text-[18px] text-fg-0">{v}</div>
            <Label>{k}</Label>
          </Well>
        ))}
      </div>
      {todays.length > 0 && (
        <div className="space-y-1">
          <Label>Worked on today</Label>
          {todays.slice(0, 4).map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => {
                useHarness.getState().openSessionPane(s.id)
                onClose()
              }}
              className="flex w-full items-center justify-between rounded px-2 py-1 text-left text-[11px] text-fg-1 ring-hairline hover:bg-white/[0.04] hover:text-fg-0"
            >
              <span className="truncate">{s.title}</span>
              <span className="ml-2 shrink-0 text-[9.5px] text-fg-3">{s.model}</span>
            </button>
          ))}
        </div>
      )}
      {upcoming.length > 0 && (
        <div className="space-y-1">
          <Label>Next scheduled</Label>
          {upcoming.map((j) => (
            <div key={j.id} className="flex items-center justify-between rounded px-2 py-1 text-[11px] ring-hairline">
              <span className="truncate text-fg-1">{j.name}</span>
              <span className="ml-2 shrink-0 text-[9.5px] text-fg-3">{j.next_fire_at ? new Date(j.next_fire_at * (j.next_fire_at < 1e12 ? 1000 : 1)).toLocaleString() : ''}</span>
            </div>
          ))}
        </div>
      )}
    </Frame>
  )
}

// ─── swarm ────────────────────────────────────────────────────────────────

function Swarm({ slots, onClose, crumbs }: WidgetProps) {
  const subagents = useHarness((s) => s.subagents)
  const order = useHarness((s) => s.subagentOrder)
  const list = order
    .map((id) => subagents[id])
    .filter(Boolean)
    .filter((a) => (slots.swarmStatus ? a.state === slots.swarmStatus : true))
    .filter((a) => (slots.profile ? a.agentType === slots.profile : true))
    .slice(-8)
    .reverse()
  const running = Object.values(subagents).filter((a) => a.state === 'running').length

  return (
    <Frame
      crumbs={crumbs}
      kicker="Swarm"
      title={slots.swarmStatus ? `${slots.swarmStatus} workers` : 'Workers'}
      badge={`${running} running`}
      footer={
        <Btn
          primary
          onClick={() => {
            useHarness.getState().toggleMissionDashboard(true, 'swarm')
            onClose()
          }}
        >
          Open swarm (⌘O)
        </Btn>
      }
    >
      <div className="max-h-[260px] space-y-1 overflow-y-auto">
        {list.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => {
              useHarness.getState().openSubagent(a.id)
              onClose()
            }}
            className="flex w-full items-center justify-between rounded px-2 py-1.5 text-left ring-hairline hover:bg-white/[0.04]"
          >
            <span className="min-w-0">
              <span className="flex items-center gap-1.5 text-[11.5px] text-fg-0">
                <span className={`h-1.5 w-1.5 rounded-full ${a.state === 'running' ? 'bg-accent' : a.state === 'failed' ? 'bg-danger' : 'bg-fg-3'}`} />
                <span className="truncate">{a.label}</span>
              </span>
              <span className="block truncate text-[10px] text-fg-3">{a.task}</span>
            </span>
            <span className="ml-2 shrink-0 text-right text-[9.5px] text-fg-3">
              <span className="block">{a.agentType ?? 'general'}</span>
              <span className="block">{a.toolsCalled} tools</span>
            </span>
          </button>
        ))}
        {list.length === 0 && <div className="py-3 text-center text-[11px] text-fg-3">No workers match.</div>}
      </div>
    </Frame>
  )
}

// ─── tool inspect ─────────────────────────────────────────────────────────

const TOOL_DOCS: Record<string, { category: string; risk: string; summary: string; params: Array<[string, string, boolean, string]> }> = {
  bash: {
    category: 'shell',
    risk: 'varies by command',
    summary: 'Run a shell command. Classified LOW / MEDIUM / HIGH / DANGEROUS before it runs; non-trivial ones prompt for approval.',
    params: [
      ['command', 'string', true, 'the command'],
      ['summary', 'string', true, 'one line shown in the approval prompt'],
      ['timeout', 'number', false, 'seconds, default 120'],
      ['working_dir', 'string', false, 'cwd for the command'],
    ],
  },
  read_file: { category: 'filesystem', risk: 'read-only', summary: 'Read a text file with line numbers; optional line range.', params: [['path', 'string', true, 'file path'], ['start_line', 'int', false, '1-indexed'], ['end_line', 'int', false, 'inclusive']] },
  write_file: { category: 'filesystem', risk: 'overwrites', summary: 'Create or replace a whole file.', params: [['path', 'string', true, 'file path'], ['content', 'string', true, 'full contents'], ['base', 'enum', false, 'workspace | project']] },
  edit_file: { category: 'filesystem', risk: 'modifies', summary: 'Targeted edit: line range, anchors, insert, or exact string replace.', params: [['path', 'string', true, 'file path'], ['start_line / end_line', 'int', false, 'line mode'], ['old_string / new_string', 'string', false, 'replace mode']] },
  grep: { category: 'filesystem', risk: 'read-only', summary: 'Regex search across files with context lines.', params: [['pattern', 'regex', true, ''], ['path', 'string', false, 'dir or file'], ['file_pattern', 'glob', false, 'filter']] },
  glob: { category: 'filesystem', risk: 'read-only', summary: 'Find files by glob pattern.', params: [['pattern', 'glob', true, ''], ['path', 'string', false, 'base dir']] },
  web_search: { category: 'web', risk: 'read-only', summary: 'AI-optimized web search returning URLs with long excerpts.', params: [['objective', 'string', true, 'what you are looking for'], ['queries', 'string[]', false, 'keyword queries']] },
  web_fetch: { category: 'web', risk: 'read-only', summary: 'Fetch a URL as clean markdown.', params: [['url', 'string', true, ''], ['objective', 'string', false, 'focus the extraction']] },
  twitter_search: { category: 'web', risk: 'read-only', summary: 'Search X through Grok; can analyze images and video in posts.', params: [['query', 'string', true, 'natural-language question or tweet URL'], ['include_images', 'bool', false, ''], ['include_videos', 'bool', false, '']] },
  sub_agent: { category: 'swarm', risk: 'spawns work', summary: 'Spawn a background worker with a specialized profile; a memo arrives when it finishes.', params: [['label', 'string', true, '< 40 chars'], ['task', 'string', true, 'self-contained prompt'], ['agent_type', 'enum', false, 'general · code · review · explore · test · verify …']] },
  jev_computer_use: { category: 'computer', risk: 'drives the UI', summary: 'Drive a macOS app or Arc/Chrome page with Jev deciding each step in ~200 ms; irreversible controls stop for confirmation.', params: [['goal', 'string', true, 'app-scoped goal'], ['app', 'string', false, 'bundle id or name'], ['max_steps', 'int', false, 'default 40'], ['allow_irreversible', 'bool', false, 'default false']] },
  computer_use: { category: 'computer', risk: 'drives the UI', summary: 'Screenshot-based sub-agent for canvas or custom-drawn UIs.', params: [['goal', 'string', true, ''], ['target_app', 'string', false, ''], ['max_steps', 'int', false, 'default 60']] },
  show_widget: { category: 'ui', risk: 'none', summary: 'Mount a generative UI fragment (dashboard, form, diagram) in the conversation.', params: [['title', 'string', true, 'snake_case'], ['widget_code', 'html', true, 'fragment, no <html>']] },
  tasks: { category: 'planning', risk: 'none', summary: 'Session task ledger the operator sees live.', params: [['action', 'enum', true, 'list · create · claim · complete · block …'], ['task_id', 'string', false, '']] },
  memory: { category: 'knowledge', risk: 'durable', summary: 'Cross-session durable notes about the user, project, feedback, references.', params: [['action', 'enum', true, ''], ['content', 'string', false, '']] },
  schedule: { category: 'automation', risk: 'durable', summary: 'Create and manage scheduled agent turns (cron).', params: [['action', 'enum', true, '']] },
  talk: { category: 'swarm', risk: 'messages agents', summary: 'Message another agent session; wakes idle recipients.', params: [['to', 'string', true, 'parent · main · label · id'], ['content', 'string', true, '']] },
}

function ToolInspect({ slots, onClose, onToast, crumbs }: WidgetProps) {
  const name = slots.toolName
  const doc = name ? TOOL_DOCS[name] : undefined
  const suggestions = name ? [] : TOOL_NAMES.slice(0, 12)
  return (
    <Frame
      crumbs={crumbs}
      kicker="Tool"
      title={name ?? 'Which tool?'}
      badge={doc ? `${doc.category} · ${doc.risk}` : undefined}
      footer={
        name ? (
          <Btn
            primary
            onClick={() => {
              void navigator.clipboard.writeText(name)
              onToast(`Copied “${name}”`)
              onClose()
            }}
          >
            Copy name
          </Btn>
        ) : (
          <Btn onClick={onClose}>Close</Btn>
        )
      }
    >
      {!name && (
        <Well>
          <Slot label="Tool" value={undefined} hint="name one: “tool details for bash”" />
          <div className="mt-2 flex flex-wrap gap-1">
            {suggestions.map((t) => (
              <span key={t} className="kbd text-[9.5px]">
                {t}
              </span>
            ))}
          </div>
        </Well>
      )}
      {name && !doc && <Well className="text-[11px] text-fg-2">Known tool, no local doc card yet. Ask Freyja: “describe the {name} tool”.</Well>}
      {doc && (
        <>
          <Well className="text-[11.5px] leading-relaxed text-fg-1">{doc.summary}</Well>
          <div className="overflow-hidden rounded-lg ring-hairline">
            <table className="w-full text-[11px]">
              <thead>
                <tr className="bg-black/[0.18] text-left text-[9.5px] uppercase tracking-wider text-fg-3">
                  <th className="px-2.5 py-1.5 font-normal">param</th>
                  <th className="px-2.5 py-1.5 font-normal">type</th>
                  <th className="px-2.5 py-1.5 font-normal">req</th>
                  <th className="px-2.5 py-1.5 font-normal">note</th>
                </tr>
              </thead>
              <tbody>
                {doc.params.map(([p, t, r, n]) => (
                  <tr key={p} className="hairline-t">
                    <td className="px-2.5 py-1.5 text-fg-0">{p}</td>
                    <td className="px-2.5 py-1.5 text-fg-3">{t}</td>
                    <td className="px-2.5 py-1.5">{r ? <span className="text-accent">yes</span> : <span className="text-fg-3">no</span>}</td>
                    <td className="px-2.5 py-1.5 text-fg-2">{n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Frame>
  )
}

// ─── diagnostics ──────────────────────────────────────────────────────────

function Diagnostics({ slots, onClose, onToast, crumbs }: WidgetProps) {
  const mode = useHarness((s) => s.mode)
  const subagents = useHarness((s) => s.subagents)
  const usage = useHarness((s) => s.usage)
  const [gateway, setGateway] = useState<{ pid: number | null; slackConfigured: boolean; plistInstalled: boolean } | null>(null)
  const [keys, setKeys] = useState<Record<string, boolean> | null>(null)
  useEffect(() => {
    let alive = true
    void window.harness.gatewayStatus?.().then((g: any) => alive && setGateway(g)).catch(() => {})
    void window.harness.llmKeysProbe?.().then((k: any) => alive && setKeys(k?.keys ?? k?.present ?? null)).catch(() => {})
    return () => {
      alive = false
    }
  }, [])
  const failed = Object.values(subagents).filter((a) => a.state === 'failed')
  const rows: Array<[string, string, 'ok' | 'warn' | 'bad']> = [
    ['bridge', mode === 'live' ? 'live' : mode === 'demo' ? 'demo mode' : 'error', mode === 'live' ? 'ok' : mode === 'demo' ? 'warn' : 'bad'],
    ['gateway', gateway ? (gateway.pid ? `running · pid ${gateway.pid}` : gateway.plistInstalled ? 'installed, not running' : 'not installed') : '…', gateway?.pid ? 'ok' : 'warn'],
    ['slack', gateway ? (gateway.slackConfigured ? 'tokens present' : 'not configured') : '…', gateway?.slackConfigured ? 'ok' : 'warn'],
    ['context', `${Math.round((usage.currentContextTokens / Math.max(1, usage.contextWindow)) * 100)}% of ${fmtTokens(usage.contextWindow)}`, usage.currentContextTokens / Math.max(1, usage.contextWindow) > 0.7 ? 'warn' : 'ok'],
    ['workers', failed.length ? `${failed.length} failed` : 'none failed', failed.length ? 'bad' : 'ok'],
  ]
  const filtered = slots.logLevel ? rows.filter((r) => r[2] !== 'ok') : slots.subsystem ? rows.filter((r) => r[0].includes(slots.subsystem!)) : rows
  const keyRows = keys ? Object.entries(keys) : []

  return (
    <Frame
      crumbs={crumbs}
      kicker="Diagnostics"
      title={slots.logLevel ? 'Problems only' : slots.subsystem ? slots.subsystem : 'System health'}
      badge={filtered.some((r) => r[2] === 'bad') ? 'attention' : filtered.some((r) => r[2] === 'warn') ? 'warnings' : 'healthy'}
      footer={
        <>
          <Btn
            onClick={() => {
              void navigator.clipboard.writeText(filtered.map((r) => `${r[0]}: ${r[1]}`).join('\n'))
              onToast('Copied')
            }}
          >
            Copy
          </Btn>
          <Btn
            primary
            onClick={() => {
              useHarness.getState().toggleMissionDashboard(true, 'activity')
              onClose()
            }}
          >
            Open activity
          </Btn>
        </>
      }
    >
      <div className="space-y-1">
        {filtered.map(([k, v, tone]) => (
          <div key={k} className="flex items-center justify-between rounded px-2 py-1 text-[11px] ring-hairline">
            <span className="flex items-center gap-1.5 text-fg-1">
              <span className={`h-1.5 w-1.5 rounded-full ${tone === 'ok' ? 'bg-ok' : tone === 'warn' ? 'bg-warn' : 'bg-danger'}`} />
              {k}
            </span>
            <span className="text-fg-2">{v}</span>
          </div>
        ))}
        {filtered.length === 0 && <div className="py-3 text-center text-[11px] text-fg-3">Nothing to report.</div>}
      </div>
      {keyRows.length > 0 && (slots.subsystem === undefined || /key|provider/.test(slots.subsystem)) && (
        <div className="space-y-1">
          <Label>Provider keys</Label>
          <div className="flex flex-wrap gap-1">
            {keyRows.map(([k, present]) => (
              <span key={k} className={`kbd text-[9px] ${present ? '' : 'opacity-50 line-through'}`}>
                {k.replace(/_API_KEY$/, '').toLowerCase()}
              </span>
            ))}
          </div>
        </div>
      )}
      {failed.length > 0 && (
        <div className="space-y-1">
          <Label>Failed workers</Label>
          {failed.slice(0, 3).map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => {
                useHarness.getState().openSubagent(a.id)
                onClose()
              }}
              className="flex w-full items-center justify-between rounded px-2 py-1 text-left text-[11px] text-fg-1 ring-hairline hover:bg-white/[0.04]"
            >
              <span className="truncate">{a.label}</span>
              <span className="ml-2 shrink-0 truncate text-[9.5px] text-danger">{(a.result ?? '').slice(0, 48)}</span>
            </button>
          ))}
        </div>
      )}
    </Frame>
  )
}

// ─── voice ────────────────────────────────────────────────────────────────

function Voice({ onClose, onToast, crumbs }: WidgetProps) {
  const toggleVoice = useVoiceStore((s) => s.toggleVoice)
  return (
    <Frame
      crumbs={crumbs}
      kicker="Voice"
      title="Talk instead of typing"
      badge="⌥Space"
      footer={
        <Btn
          primary
          onClick={() => {
            toggleVoice()
            onToast('Voice on')
            onClose()
          }}
        >
          Start voice
        </Btn>
      }
    >
      <Well className="flex items-center justify-between">
        <div className="flex items-end gap-1">
          {[4, 10, 16, 22, 14, 18, 8, 5].map((h, i) => (
            <span key={i} style={{ height: h }} className="w-1 rounded-full bg-accent/70" />
          ))}
        </div>
        <div className="text-right text-[10.5px] text-fg-2">
          <div>Push-to-talk with ⌥Space anywhere.</div>
          <div className="text-fg-3">Replies stream back as speech and text.</div>
        </div>
      </Well>
    </Frame>
  )
}

// ─── quick controls ───────────────────────────────────────────────────────

function Appearance({ slots, onClose, onToast, crumbs }: WidgetProps) {
  const setAppearanceTheme = useHarness((s) => s.setAppearanceTheme)
  const want = slots.theme
  const options: Array<['dark' | 'light' | 'system', string, string]> = [
    ['dark', 'Dark', 'near-black shell, sky accent'],
    ['light', 'Light', 'paper shell, steel-blue accent'],
    ['system', 'Follow system', 'macOS decides, sunset included'],
  ]
  const apply = (t: 'dark' | 'light' | 'system') => {
    void setAppearanceTheme(t)
    onToast(`Appearance → ${t}`)
    onClose()
  }
  return (
    <Frame
      crumbs={crumbs}
      kicker="Appearance"
      title={want ? `Switch to ${want}` : 'Theme'}
      footer={want ? <Btn primary onClick={() => apply(want)}>Apply {want}</Btn> : <Btn onClick={onClose}>Close</Btn>}
    >
      <div className="space-y-1">
        {options.map(([id, label, note]) => (
          <button
            key={id}
            type="button"
            onClick={() => apply(id)}
            className={`flex w-full items-center justify-between rounded-md px-2.5 py-1.5 text-left ${
              want === id ? 'bg-accent/15 ring-1 ring-accent/40' : 'ring-hairline hover:bg-white/[0.04]'
            }`}
          >
            <span>
              <span className="block text-[11.5px] text-fg-0">{label}</span>
              <span className="block text-[10px] text-fg-3">{note}</span>
            </span>
            {want === id && <span className="text-[10px] text-accent">selected</span>}
          </button>
        ))}
      </div>
    </Frame>
  )
}

function Mic({ slots, onClose, onToast, crumbs }: WidgetProps) {
  const toggleVoice = useVoiceStore((s) => s.toggleVoice)
  const want = slots.mute
  return (
    <Frame
      crumbs={crumbs}
      kicker="Microphone"
      title={want === undefined ? 'Voice input' : want ? 'Mute' : 'Unmute'}
      badge="⌥Space"
      footer={
        <Btn
          primary
          onClick={() => {
            toggleVoice()
            onToast(want ? 'Voice off' : 'Voice on')
            onClose()
          }}
        >
          {want ? 'Stop listening' : 'Start listening'}
        </Btn>
      }
    >
      <Well className="text-[11px] text-fg-2">Freyja only listens during a voice turn. {want ? 'Stopping ends the current turn.' : 'Starting opens a voice turn in this session.'}</Well>
    </Frame>
  )
}

function QuickControls({ onClose, onToast, crumbs }: WidgetProps) {
  const setAppearanceTheme = useHarness((s) => s.setAppearanceTheme)
  const toggleVoice = useVoiceStore((s) => s.toggleVoice)
  const items: Array<[string, string, () => void]> = [
    ['Appearance', 'dark · light · system', () => void setAppearanceTheme('system')],
    ['Voice', 'start a voice turn', () => toggleVoice()],
    ['Scheduled jobs', 'cron, daemon status', () => useSchedulerStore.getState().openDashboard()],
    ['Artifacts', 'every file every session made', () => useHarness.getState().toggleArtifactsBrowser(true)],
  ]
  return (
    <Frame kicker="Controls" title="Quick actions" footer={<Btn onClick={onClose}>Close</Btn>}>
      <div className="grid grid-cols-2 gap-2">
        {items.map(([k, v, fn]) => (
          <button
            key={k}
            type="button"
            onClick={() => {
              fn()
              onToast(k)
              onClose()
            }}
            className="rounded-lg bg-black/[0.18] p-2.5 text-left ring-hairline hover:bg-white/[0.04]"
          >
            <span className="block text-[11.5px] text-fg-0">{k}</span>
            <span className="block text-[10px] text-fg-3">{v}</span>
          </button>
        ))}
      </div>
    </Frame>
  )
}

// ─── finance (no local data source; hands off to the agent) ───────────────

function Finance({ query, slots, onClose, onToast, crumbs }: WidgetProps) {
  const sendMessage = useHarness((s) => s.sendMessage)
  const instruction = instructionFor('finance', query, slots)!
  return (
    <Frame
      crumbs={crumbs}
      kicker="Spend"
      title={slots.category ?? 'Expenses'}
      badge={slots.periodLabel ?? 'period?'}
      footer={
        <>
          <Btn
            onClick={() => {
              useHarness.getState().setInputDraft(instruction)
              onClose()
            }}
          >
            Edit as prompt
          </Btn>
          <Btn
            primary
            onClick={() => {
              void sendMessage(instruction)
              onToast('Pulling spend…')
              onClose()
            }}
          >
            Pull spend
          </Btn>
        </>
      }
    >
      <Well className="space-y-1.5">
        <Slot label="Category" value={slots.category} hint="a category? “on travel”" />
        <Slot label="Period" value={slots.periodLabel} hint="a window? “last month”" />
      </Well>
      <div className="text-[10.5px] text-fg-3">No ledger lives in Freyja; this asks the agent to use whatever finance tool is connected.</div>
    </Frame>
  )
}
