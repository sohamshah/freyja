/**
 * ⌘K palette intent classification through TypeSafe's System One (Jev).
 *
 * The renderer sends the raw query; this returns a typed decision (which
 * palette domain, which granular sub-target, how actionable the text is).
 * Everything else — slot parsing, widget choice, data — lives in the
 * renderer, where the store has the real models, sessions, and subagents.
 * Jev decides *what* the person means; the renderer decides *with what*.
 */
import os from 'node:os'
import path from 'node:path'
import { app } from 'electron'
import type { JevPaletteDecision } from '../shared/events.js'
import { readDotEnv } from './bridge.js'

const API_URL = 'https://api.typesafe.ai/v1/systemone'
const MODEL = 'jev-latest'
const TIMEOUT_MS = 4000

export const PALETTE_DOMAINS = {
  calendar: 'Meetings: scheduling, booking, viewing an agenda, checking availability',
  memory: 'Recalling past notes, transcripts, conversations, recommendations, or facts someone said',
  agent_task: 'Delegating work to a sub-agent: review a PR, explore a codebase, run tests, research a topic',
  model_picker: 'Listing, comparing, or switching the LLM model (Sonnet, Opus, GPT, Luna, Gemini, thinking level)',
  token_usage: 'LLM token consumption, API cost, model spend, cache hit rate, how much Freyja used or cost over a period',
  artifact_explorer: 'Finding, opening, or listing generated files, reports, artifacts, deliverables',
  briefing: 'Morning briefing, today plan, what is on for today, staged tasks, decisions',
  swarm: 'Active sub-agents, worker lanes, what agents are running, swarm status',
  tool_inspect: 'Details, parameters, schema, or risk of an agent tool (bash, read_file, sub_agent, jev_computer_use)',
  diagnostics: 'Diagnostic logs, bridge health, gateway status, errors, debug state, which API keys are set',
  voice_session: 'Starting or ending a voice conversation, talking instead of typing',
  quick_control: 'Appearance, dark or light mode, theme, mute the microphone, do not disturb, timer',
  finance: 'Personal money: credit card spending, expenses, receipts, a purchase category like travel or food (not LLM or API cost)',
  skill_lookup: 'Asking how to do a kind of task Freyja has a skill or playbook for, finding or loading a skill, "how do I", "is there a skill for"',
  pr_triage: 'Pull requests: PRs waiting on me, PRs to review, my open PRs, CI status, approving or triaging GitHub PRs',
  slack_read: 'Reading Slack: what someone said, messages in a channel, catching up on a thread or channel',
  context_budget: 'Context window usage, how full the context is, compaction, token pressure of the current conversation',
  palette_command: 'A plain command palette search: a command name, a skill name, a session name, a slash command, a keyword',
} as const

export const PALETTE_SUBTARGETS = {
  calendar_view: 'VIEW or LIST existing meetings, agenda, or availability (what meetings do I have, am I free)',
  calendar_create: 'CREATE, schedule, book, or set up a new meeting or event',
  appearance_toggle: 'Theme, dark mode, light mode, appearance',
  mic_toggle: 'Mute or unmute the microphone',
  focus_toggle: 'Do not disturb, focus mode, silence notifications',
  timer_set: 'A timer, countdown, or pomodoro',
  model_switch: 'Switch to a specific named model',
  model_list: 'List or compare the available models',
  usage_period: 'Usage or spend over a time window (today, last 7 days, this month)',
  recent_artifacts: 'Recent or latest artifacts',
  artifact_search: 'Search artifacts by name, type, or topic',
  log_errors: 'Only errors or warnings',
  key_status: 'Which API keys or providers are configured',
  pr_review_requested: 'PRs waiting on my review, review requests',
  pr_mine: 'My own open PRs, PRs I authored',
  slack_channel: 'Messages in a named channel',
  slack_person: 'What a specific person said or posted',
  compact_now: 'Compact or summarize the context now',
  none: 'No specific sub-target; a general query in its domain',
} as const

export type PaletteDomain = keyof typeof PALETTE_DOMAINS
export type PaletteSubTarget = keyof typeof PALETTE_SUBTARGETS

function harnessRoot(): string {
  return app.isPackaged ? (process.resourcesPath ?? path.resolve(__dirname, '..')) : path.resolve(__dirname, '..')
}

/** Same three files the bridge layers, in the same order, so the two
 *  processes always agree on which key is live. */
function apiKey(): string | null {
  const fromEnv = process.env.TYPESAFE_AI_API_KEY || process.env.TYPESAFE_API_KEY
  if (fromEnv) return fromEnv
  const sourceRoot = path.resolve(__dirname, '..')
  const merged = {
    ...readDotEnv(path.join(sourceRoot, '.env')),
    ...readDotEnv(path.join(harnessRoot(), '.env')),
    ...readDotEnv(path.join(os.homedir(), '.freyja', '.env')),
  }
  return merged.TYPESAFE_AI_API_KEY || merged.TYPESAFE_API_KEY || null
}

let cachedKey: string | null | undefined

export function jevConfigured(): boolean {
  if (cachedKey === undefined) cachedKey = apiKey()
  return !!cachedKey
}

export async function classifyPaletteQuery(query: string): Promise<JevPaletteDecision> {
  const trimmed = query.trim()
  if (cachedKey === undefined) cachedKey = apiKey()
  if (!cachedKey) {
    return { ok: false, query: trimmed, error: 'TYPESAFE_AI_API_KEY is not set', domain: 'palette_command', subTarget: 'none', confidence: 0, readiness: 0, latencyMs: 0 }
  }

  const body = {
    model: MODEL,
    state: trimmed,
    questions: {
      domain: {
        type: 'choice',
        instructions:
          'The person is typing into the ⌘K command palette of Freyja, a desktop AI agent. Which domain is this query about?',
        criteria: PALETTE_DOMAINS,
      },
      sub_target: {
        type: 'choice',
        instructions: 'Within that domain, which specific action or view is the query asking for?',
        criteria: PALETTE_SUBTARGETS,
      },
      readiness: {
        type: 'score',
        instructions: 'How complete is the query as an instruction to act on?',
        criteria: [
          'A fragment or a few keywords, still being typed',
          'A clear intent that is still missing a detail needed to act (who, when, which)',
          'A complete, actionable instruction',
        ],
      },
    },
  }

  const t0 = performance.now()
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  try {
    const res = await fetch(API_URL, {
      method: 'POST',
      headers: { Authorization: `Bearer ${cachedKey}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    })
    const latencyMs = Math.round(performance.now() - t0)
    if (!res.ok) {
      return { ok: false, query: trimmed, error: `jev ${res.status}`, domain: 'palette_command', subTarget: 'none', confidence: 0, readiness: 0, latencyMs }
    }
    const data = (await res.json()) as {
      answers?: {
        domain?: { choice?: string; confidence?: number; probabilities?: Record<string, number> }
        sub_target?: { choice?: string; confidence?: number }
        readiness?: { score?: number }
      }
    }
    const d = data.answers?.domain
    const s = data.answers?.sub_target
    return {
      ok: true,
      query: trimmed,
      domain: (d?.choice as PaletteDomain) || 'palette_command',
      subTarget: (s?.choice as PaletteSubTarget) || 'none',
      confidence: d?.confidence ?? 0,
      subTargetConfidence: s?.confidence ?? 0,
      readiness: data.answers?.readiness?.score ?? 0,
      probabilities: d?.probabilities,
      latencyMs,
    }
  } catch (err) {
    const latencyMs = Math.round(performance.now() - t0)
    const msg = err instanceof Error ? (err.name === 'AbortError' ? 'jev timeout' : err.message) : String(err)
    return { ok: false, query: trimmed, error: msg, domain: 'palette_command', subTarget: 'none', confidence: 0, readiness: 0, latencyMs }
  } finally {
    clearTimeout(timer)
  }
}
