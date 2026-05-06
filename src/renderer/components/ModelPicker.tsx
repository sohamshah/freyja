import { useMemo } from 'react'
import { useHarness } from '../state/store'
import type { ModelChoice } from '../state/store'
import { formatTokens } from '../lib/format'

const FALLBACK_MODELS: ModelChoice[] = [
  // Anthropic
  { id: 'claude-opus-4-7', family: 'anthropic', label: 'Claude Opus 4.7', tier: 'max', contextWindow: 1_000_000, thinking: false, envVar: 'ANTHROPIC_API_KEY', description: 'Latest Opus. Best for hard coding and agentic tasks. Adaptive thinking, 128k output.' },
  { id: 'claude-opus-4-6', family: 'anthropic', label: 'Claude Opus 4.6', tier: 'max', contextWindow: 1_000_000, thinking: true, envVar: 'ANTHROPIC_API_KEY', description: 'Previous-gen Opus. Deep reasoning with extended thinking.' },
  { id: 'claude-sonnet-4-6', family: 'anthropic', label: 'Claude Sonnet 4.6', tier: 'balanced', contextWindow: 1_000_000, thinking: true, envVar: 'ANTHROPIC_API_KEY', description: 'Balanced default. Strong quality, sane latency.' },
  { id: 'claude-haiku-4-5', family: 'anthropic', label: 'Claude Haiku 4.5', tier: 'fast', contextWindow: 200_000, thinking: true, envVar: 'ANTHROPIC_API_KEY', description: 'Fastest Claude. Good for quick edits and fanout.' },
  { id: 'claude-opus-4-5', family: 'anthropic', label: 'Claude Opus 4.5', tier: 'max', contextWindow: 200_000, thinking: true, envVar: 'ANTHROPIC_API_KEY', description: 'Previous-gen Opus.' },
  { id: 'claude-sonnet-4-5', family: 'anthropic', label: 'Claude Sonnet 4.5', tier: 'balanced', contextWindow: 1_000_000, thinking: true, envVar: 'ANTHROPIC_API_KEY', description: 'Previous-gen Sonnet.' },
  // OpenAI
  { id: 'gpt-5.5', family: 'openai', label: 'GPT-5.5', tier: 'max', contextWindow: 1_050_000, thinking: true, envVar: 'OPENAI_API_KEY', description: "OpenAI's newest frontier model. Best for complex coding, reasoning, and computer use." },
  { id: 'gpt-5.4', family: 'openai', label: 'GPT-5.4', tier: 'max', contextWindow: 1_050_000, thinking: true, envVar: 'OPENAI_API_KEY', description: 'Previous OpenAI flagship. Strong reasoning, vision, tool use.' },
  { id: 'gpt-5.4-mini', family: 'openai', label: 'GPT-5.4 Mini', tier: 'balanced', contextWindow: 400_000, thinking: true, envVar: 'OPENAI_API_KEY', description: 'Balanced OpenAI tier. Cheap per-turn, still reasons.' },
  { id: 'gpt-5.4-nano', family: 'openai', label: 'GPT-5.4 Nano', tier: 'fast', contextWindow: 400_000, thinking: true, envVar: 'OPENAI_API_KEY', description: 'Cheapest OpenAI tier. Good for fanout and high-volume subagents.' },
  { id: 'gpt-5.3-codex', family: 'openai', label: 'GPT-5.3 Codex', tier: 'balanced', contextWindow: 400_000, thinking: true, envVar: 'OPENAI_API_KEY', description: 'Agentic coding specialist. Powers GPT-5.4 coding capabilities.' },
  // Cerebras
  { id: 'zai-glm-4.7', family: 'cerebras', label: 'GLM 4.7 (Cerebras)', tier: 'fast', contextWindow: 131_072, thinking: false, envVar: 'CEREBRAS_API_KEY', description: '~1000 tps on Cerebras. Great for subagents and fanout.' },
  // Fireworks
  { id: 'deepseek-v4-pro', family: 'fireworks', label: 'DeepSeek V4 Pro', tier: 'max', contextWindow: 1_048_576, thinking: true, reasoningMode: 'effort', reasoningLevels: ['none', 'low', 'medium', 'high', 'max'], reasoningDefault: 'high', reasoningHistory: ['interleaved'], envVar: 'FIREWORKS_API_KEY', description: "DeepSeek's frontier MoE reasoning model via Fireworks. 1M ctx, function calling." },
  { id: 'glm-5.1', family: 'fireworks', label: 'GLM 5.1', tier: 'max', contextWindow: 202_752, thinking: true, reasoningMode: 'effort', reasoningLevels: ['none', 'low', 'medium', 'high'], reasoningDefault: 'high', envVar: 'FIREWORKS_API_KEY', description: "Z.ai's newer GLM 5.1 via Fireworks. Agentic engineering, tool use, 202.8k ctx." },
  { id: 'kimi-k2.6', family: 'fireworks', label: 'Kimi K2.6', tier: 'max', contextWindow: 262_144, thinking: true, reasoningMode: 'effort', reasoningLevels: ['none', 'low', 'medium', 'high'], reasoningDefault: 'high', reasoningHistory: ['preserved'], envVar: 'FIREWORKS_API_KEY', description: "Moonshot's newer multimodal agentic model via Fireworks. Vision + 262k ctx." },
  { id: 'minimax-m2.7', family: 'fireworks', label: 'MiniMax M2.7', tier: 'balanced', contextWindow: 196_608, thinking: true, reasoningMode: 'required', reasoningLevels: ['low', 'medium', 'high'], reasoningDefault: 'medium', reasoningHistory: ['interleaved'], envVar: 'FIREWORKS_API_KEY', description: 'MiniMax M2.7 via Fireworks. Agent harnesses, teams, skills, and dynamic tool search.' },
  { id: 'deepseek-v3.2', family: 'fireworks', label: 'DeepSeek v3.2', tier: 'balanced', contextWindow: 163_840, thinking: true, reasoningMode: 'binary', reasoningLevels: ['none', 'high'], reasoningDefault: 'high', envVar: 'FIREWORKS_API_KEY', description: 'DeepSeek v3.2 via Fireworks. Efficient reasoning and agent performance.' },
  { id: 'qwen3.6-plus', family: 'fireworks', label: 'Qwen3.6 Plus', tier: 'balanced', contextWindow: 1_000_000, thinking: true, reasoningMode: 'effort', reasoningLevels: ['none', 'low', 'medium', 'high'], reasoningDefault: 'medium', reasoningHistory: ['preserved'], envVar: 'FIREWORKS_API_KEY', description: "Alibaba's Qwen3.6 Plus via Fireworks. Vision, function calling, preserved reasoning, 1M ctx." },
  { id: 'kimi-k2.5', family: 'fireworks', label: 'Kimi K2.5', tier: 'balanced', contextWindow: 262_144, thinking: false, reasoningMode: 'none', envVar: 'FIREWORKS_API_KEY', description: "Moonshot's Kimi K2.5 via Fireworks. Vision + 262k ctx." },
  { id: 'glm5', family: 'fireworks', label: 'GLM 5 (Fireworks)', tier: 'balanced', contextWindow: 202_752, thinking: false, reasoningMode: 'none', envVar: 'FIREWORKS_API_KEY', description: "Zhipu's GLM 5 via Fireworks." },
  { id: 'minimax-m2.5', family: 'fireworks', label: 'MiniMax M2.5', tier: 'fast', contextWindow: 196_608, thinking: true, reasoningMode: 'required', reasoningLevels: ['low', 'medium', 'high'], reasoningDefault: 'medium', reasoningHistory: ['interleaved'], envVar: 'FIREWORKS_API_KEY', description: 'MiniMax M2.5 via Fireworks. Fast and cheap.' },
]

const TIER_LABEL: Record<string, string> = {
  max: 'MAX',
  balanced: 'BALANCED',
  fast: 'FAST',
  other: 'EXT',
}

const TIER_COLOR: Record<string, string> = {
  max: 'text-accent',
  balanced: 'text-fg-0',
  fast: 'text-ok',
  other: 'text-fg-2',
}

function reasoningLabel(model: ModelChoice): string {
  const mode = model.reasoningMode ?? (model.thinking ? 'effort' : 'none')
  if (mode === 'adaptive') return 'adaptive reasoning'
  if (mode === 'budget') return `thinking ${model.reasoningDefault ?? 'on'}`
  if (mode === 'required') return `reasoning required/${model.reasoningDefault ?? 'medium'}`
  if (mode === 'binary') return `reasoning ${model.reasoningDefault ?? 'on'}`
  if (mode === 'effort') return `reasoning ${model.reasoningDefault ?? 'high'}`
  return 'no reasoning'
}

function reasoningTitle(model: ModelChoice): string {
  const levels = model.reasoningLevels?.length
    ? ` Levels: ${model.reasoningLevels.join(', ')}.`
    : ''
  const history = model.reasoningHistory?.length
    ? ` History: ${model.reasoningHistory.join(', ')}.`
    : ''
  return `${reasoningLabel(model)}.${levels}${history}`
}

interface ModelPickerProps {
  /** Called when user selects a model. Fires after the store is updated. */
  onSelect?: (id: string) => void
  /** If true, renders inline (no modal chrome). */
  inline?: boolean
  /** If true, each row has a compact description. */
  dense?: boolean
}

/**
 * Model picker — used both inline on the hero welcome and as a modal
 * popped from the title bar model pill. Renders every model advertised by
 * the bridge's `ready` capabilities (falling back to a hard-coded list when
 * the bridge is offline).
 */
export function ModelPicker({ onSelect, inline = false, dense = false }: ModelPickerProps) {
  const available = useHarness((s) => s.availableModels)
  const activeModel = useHarness((s) => s.model)
  const setModel = useHarness((s) => s.setModel)
  const toggle = useHarness((s) => s.toggleModelPicker)

  const models = useMemo(
    () => (available.length > 0 ? available : FALLBACK_MODELS),
    [available],
  )

  const onPick = (id: string) => {
    setModel(id)
    onSelect?.(id)
    if (!inline) toggle(false)
  }

  const content = (
    <div className={inline ? '' : 'p-2'}>
      <div className="px-3 pb-2 pt-1 label flex items-center justify-between">
        <span>choose model</span>
        {!inline && (
          <button
            onClick={() => toggle(false)}
            className="text-[10px] text-fg-2 hover:text-fg-0"
          >
            esc
          </button>
        )}
      </div>
      <div
        className={`space-y-1 ${
          inline ? '' : 'max-h-[min(60vh,520px)] overflow-y-auto pr-1'
        }`}
      >
        {models.map((m) => {
          const isActive = m.id === activeModel
          const tierColor = TIER_COLOR[m.tier] ?? 'text-fg-2'
          const unavailable = m.available === false
          return (
            <button
              key={m.id}
              onClick={() => {
                if (unavailable) {
                  useHarness
                    .getState()
                    .showToast(`${m.envVar || 'API key'} is not set`, 'warn')
                  return
                }
                onPick(m.id)
              }}
              className={`group flex w-full items-start gap-3 rounded-md px-3 py-2.5 text-left transition-colors ${
                isActive
                  ? 'bg-accent/15 text-fg-0 ring-1 ring-accent/30'
                  : unavailable
                    ? 'text-fg-3 hover:bg-white/[0.02]'
                    : 'hover:bg-white/[0.04] text-fg-1'
              }`}
            >
              <div className="mt-[3px] flex h-4 w-4 shrink-0 items-center justify-center">
                {isActive ? (
                  <span className="text-accent">▸</span>
                ) : unavailable ? (
                  <span className="text-danger">✕</span>
                ) : (
                  <span className="text-fg-3">·</span>
                )}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span
                    className={`font-mono text-[12px] ${unavailable ? 'text-fg-3' : 'text-fg-0'}`}
                  >
                    {m.label}
                  </span>
                  <span
                    className={`font-mono text-[9.5px] uppercase tracking-[0.08em] ${
                      unavailable ? 'text-fg-3' : tierColor
                    }`}
                  >
                    {TIER_LABEL[m.tier] ?? m.tier}
                  </span>
                  <span className="font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
                    {m.family}
                  </span>
                  {m.thinking && (
                    <span
                      className="font-mono text-[9px] uppercase tracking-[0.08em] text-accent/70"
                      title={reasoningTitle(m)}
                    >
                      ◐
                    </span>
                  )}
                </div>
                <div className="mt-[2px] flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10px] text-fg-2">
                  <span className="font-mono">{m.id}</span>
                  <span>·</span>
                  <span className="font-mono">{formatTokens(m.contextWindow)} ctx</span>
                  {m.reasoningMode && m.reasoningMode !== 'none' && (
                    <>
                      <span>·</span>
                      <span className="font-mono text-accent/75">
                        {reasoningLabel(m)}
                      </span>
                    </>
                  )}
                  {unavailable && (
                    <>
                      <span>·</span>
                      <span className="font-mono text-danger/80">{m.envVar} unset</span>
                    </>
                  )}
                </div>
                {!dense && (
                  <div className="mt-1.5 text-[11px] leading-[1.5] text-fg-2">{m.description}</div>
                )}
              </div>
            </button>
          )
        })}
      </div>
      {!inline && (
        <div className="hairline-t mt-2 px-3 pb-1 pt-2 text-[10px] text-fg-3">
          <span className="font-mono">⏎</span> select · <span className="font-mono">esc</span> close
        </div>
      )}
    </div>
  )

  if (inline) {
    return (
      <div className="glass-raised rounded-xl p-1">{content}</div>
    )
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center pt-[14vh]">
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-[2px]"
        onClick={() => toggle(false)}
      />
      <div className="relative w-[560px] overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong">
        {content}
      </div>
    </div>
  )
}
