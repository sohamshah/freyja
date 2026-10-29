import type { ReactNode } from 'react'
import type { IslandAction } from '../../shared/island'
import { DOMAIN_LABEL, instructionFor, requiredSlots, slotFilled, type PaletteSlots, type PaletteWidgetType } from '../lib/paletteIntent'

/**
 * The intent card: what ⌘K's generative palette shows for a sentence, in
 * the island's shape. Jev names the domain, the slots are parsed from the
 * words as they are typed, and the card fills in one field at a time. The
 * cards whose action is a prompt (recall, delegate, spend) or a switch
 * (voice, appearance, a view) act from here; the rest hand the sentence to
 * ⌘K in Freyja, where the full card has the app's data.
 */
export type IntentType = Exclude<PaletteWidgetType, null>

export interface IntentCandidate {
  domain: string
  p: number
}

export interface IntentProps {
  type: IntentType
  domain: string
  query: string
  slots: PaletteSlots
  confidence: number
  inferring: boolean
  also: IntentCandidate[]
  onPin: (domain: string) => void
  target: { id: string; title: string } | null
  selected: boolean
  act: (action: IslandAction) => void
  /** The card did its thing: the sheet folds. */
  onDone: () => void
}

interface SlotRow {
  label: string
  value?: ReactNode
  hint: string
}

export interface Plan {
  title: string
  rows: SlotRow[]
  /** The card's own action, when it has one; null means ↵ opens the card in Freyja. */
  primary: { label: string; run: () => void; disabled?: boolean } | null
  note: string
  chips?: Array<{ label: string; run: () => void }>
}

const THEME_NOTE = 'the island stays black either way'

/** What the card would do, for the field's hint as much as for the card itself. */
export function intentPlan(p: IntentProps): Plan {
  const { type, query, slots, target, act, onDone } = p
  const send = (text: string) => {
    if (!target) return
    act({ type: 'send', sessionId: target.id, text })
    onDone()
  }
  const to = target ? `sends to ${target.title}` : 'no session to send to'
  const inFreyja = 'opens the full card in Freyja, with the sentence typed'
  const prompt = (label: string) => {
    const text = instructionFor(type, query, slots)
    return text ? { label, run: () => send(text), disabled: !target } : null
  }
  switch (type) {
    case 'calendar_create':
      return {
        title: slots.topic ? `Event: ${slots.topic}` : 'New event',
        rows: [
          { label: 'with', value: slots.attendees.length ? slots.attendees.join(', ') : undefined, hint: 'who? “with nari”' },
          { label: 'when', value: slots.day ? `${slots.day.weekday} ${slots.day.iso}` : undefined, hint: 'a day? “tomorrow”, “next tue”' },
          { label: 'time', value: slots.time ? `${slots.time.label}${slots.durationMin ? ` · ${slots.durationMin} min` : ''}` : undefined, hint: 'a time? “at 1pm”' },
        ],
        primary: null,
        note: `${inFreyja}; the card books it through Calendar`,
      }
    case 'calendar_view':
      return {
        title: slots.range === 'week' ? 'This week' : slots.range === 'next_week' ? 'Next week' : slots.day ? slots.day.label : 'Agenda',
        rows: [{ label: 'day', value: slots.day?.label ?? slots.range, hint: 'a day or a range? “tomorrow”, “this week”' }],
        primary: null,
        note: inFreyja,
      }
    case 'memory':
      return {
        title: slots.person ? `What ${slots.person} said` : 'Recall',
        rows: [
          { label: 'who', value: slots.person, hint: 'a person? “what did nari say”' },
          { label: 'about', value: slots.topic ?? slots.keyword, hint: 'a topic? “about lisbon”' },
          { label: 'when', value: slots.periodLabel ?? slots.day?.label, hint: 'a timeframe? “last week”' },
        ],
        primary: prompt('recall'),
        note: `${to}; it quotes the lines and names the source`,
      }
    case 'agent_task': {
      const req = requiredSlots(type)
      const ready = req.every((k) => slotFilled(slots, k))
      const targetLine = slots.prNumber ? `PR #${slots.prNumber}${slots.repo ? ` on ${slots.repo}` : ''}` : slots.repo
      const primary = prompt(`spawn ${slots.profile ?? 'sub-agent'}`)
      return {
        title: targetLine ?? 'Delegate',
        rows: [
          { label: 'profile', value: slots.profile, hint: 'what kind? “review”, “explore”, “test”, “implement”' },
          { label: 'target', value: targetLine, hint: 'which PR or repo? “pr 1321 on agent-harness”' },
          { label: 'focus', value: slots.focus, hint: 'a lens? “for security”' },
        ],
        primary: primary ? { ...primary, disabled: primary.disabled || !ready } : null,
        note: target ? `runs in the background beside ${target.title}; a memo lands there when it finishes` : to,
      }
    }
    case 'finance':
      return {
        title: slots.category ? `Spend: ${slots.category}` : 'Spend',
        rows: [
          { label: 'category', value: slots.category, hint: 'a category? “on travel”' },
          { label: 'period', value: slots.periodLabel, hint: 'a window? “last month”' },
        ],
        primary: prompt('pull spend'),
        note: `${to}; it uses whatever finance tool is connected`,
      }
    case 'briefing':
      return {
        title: new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }),
        rows: [],
        primary: {
          label: 'open Morning Room',
          run: () => {
            act({ type: 'view', view: 'morning-room' })
            onDone()
          },
        },
        note: 'the briefing, the staged plan and the decisions',
      }
    case 'swarm':
      return {
        title: slots.swarmStatus ? `${slots.swarmStatus} workers` : 'Workers',
        rows: [
          { label: 'status', value: slots.swarmStatus, hint: '“running”, “done”, “failed”' },
          { label: 'profile', value: slots.profile, hint: '“review”, “explore”…' },
        ],
        primary: {
          label: 'open swarm',
          run: () => {
            act({ type: 'view', view: 'dashboard:overview' })
            onDone()
          },
        },
        note: 'clear the field to see the live ones here',
      }
    case 'voice':
      return {
        title: 'Talk instead of typing',
        rows: [],
        primary: {
          label: 'start voice',
          run: () => {
            act({ type: 'voice', on: true })
            onDone()
          },
        },
        note: 'a voice turn in the current session; the strip shows the sigil',
      }
    case 'mic':
      return {
        title: slots.mute === undefined ? 'Voice input' : slots.mute ? 'Mute' : 'Unmute',
        rows: [],
        primary: {
          label: slots.mute ? 'stop listening' : 'start listening',
          run: () => {
            act({ type: 'voice', on: !slots.mute })
            onDone()
          },
        },
        note: slots.mute ? 'ends the voice turn' : 'opens a voice turn in the current session',
      }
    case 'appearance': {
      const apply = (theme: 'dark' | 'light' | 'system') => {
        act({ type: 'appearance', theme })
        onDone()
      }
      return {
        title: slots.theme ? `Switch to ${slots.theme}` : 'Appearance',
        rows: [{ label: 'theme', value: slots.theme, hint: '“dark”, “light”, “system”' }],
        primary: slots.theme ? { label: `apply ${slots.theme}`, run: () => apply(slots.theme!) } : null,
        note: THEME_NOTE,
        chips: slots.theme
          ? undefined
          : [
              { label: 'dark', run: () => apply('dark') },
              { label: 'light', run: () => apply('light') },
              { label: 'follow system', run: () => apply('system') },
            ],
      }
    }
    case 'quick_controls':
      return {
        title: 'Quick actions',
        rows: [],
        primary: null,
        chips: [
          { label: 'appearance: system', run: () => act({ type: 'appearance', theme: 'system' }) },
          { label: 'voice', run: () => act({ type: 'voice', on: true }) },
          { label: 'scheduled jobs', run: () => act({ type: 'view', view: 'scheduler' }) },
          { label: 'artifacts', run: () => act({ type: 'view', view: 'artifacts' }) },
        ],
        note: 'or open ⌘K in Freyja for the rest',
      }
    case 'model_picker':
      return {
        title: slots.modelQuery ? `Model: ${slots.modelQuery}` : 'Model',
        rows: [
          { label: 'model', value: slots.modelQuery, hint: '“opus”, “gpt-5.5”, “gemini”' },
          { label: 'thinking', value: slots.reasoning, hint: '“high thinking”, “minimal”' },
        ],
        primary: null,
        note: `${inFreyja}; the picker switches the session's model`,
      }
    case 'token_usage':
      return {
        title: slots.periodLabel ? `Usage · ${slots.periodLabel}` : 'Usage',
        rows: [{ label: 'period', value: slots.periodLabel, hint: '“today”, “last 7 days”, “this month”' }],
        primary: null,
        note: inFreyja,
      }
    case 'artifact_explorer':
    case 'artifact_preview':
      return {
        title: 'Artifacts',
        rows: [
          { label: 'types', value: slots.fileTypes.length ? slots.fileTypes.join(', ') : undefined, hint: '“pdf”, “markdown”, “images”' },
          { label: 'keyword', value: slots.keyword ?? slots.topic, hint: 'a name or a topic' },
        ],
        primary: null,
        note: `${inFreyja}; the card searches every file every session made`,
      }
    case 'tool_inspect':
      return {
        title: slots.toolName ? `Tool: ${slots.toolName}` : 'Tool',
        rows: [{ label: 'tool', value: slots.toolName, hint: '“bash”, “read_file”, “sub_agent”' }],
        primary: null,
        note: inFreyja,
      }
    case 'diagnostics':
      return {
        title: 'Diagnostics',
        rows: [
          { label: 'level', value: slots.logLevel, hint: '“errors”, “warnings”' },
          { label: 'subsystem', value: slots.subsystem, hint: '“bridge”, “gateway”, “keys”' },
        ],
        primary: null,
        note: inFreyja,
      }
    case 'skill_lookup':
      return {
        title: 'Skills',
        rows: [{ label: 'task', value: slots.skillQuery, hint: '“how do I … ”' }],
        primary: null,
        note: `${inFreyja}; the card ranks the skills and loads one`,
      }
    case 'pr_triage':
      return {
        title: 'Pull requests',
        rows: [
          { label: 'scope', value: slots.prScope === 'mine' ? 'mine' : slots.prScope === 'involves' ? 'involving me' : 'waiting on me', hint: '' },
          { label: 'repo', value: slots.repo, hint: '“on agent-harness”' },
          { label: 'pr', value: slots.prNumber ? `#${slots.prNumber}` : undefined, hint: '“pr 1321”' },
        ],
        primary: null,
        note: `${inFreyja}; the card reads GitHub through gh`,
      }
    case 'slack_read':
      return {
        title: slots.channel ? `#${slots.channel}` : slots.person ? `What ${slots.person} posted` : 'Slack',
        rows: [
          { label: 'channel', value: slots.channel ? `#${slots.channel}` : undefined, hint: '“in #eng”' },
          { label: 'person', value: slots.person, hint: '“what did nari post”' },
          { label: 'when', value: slots.periodLabel ?? slots.day?.label, hint: '“since yesterday”' },
        ],
        primary: null,
        note: inFreyja,
      }
    case 'context_budget':
      return {
        title: 'Context',
        rows: [],
        primary: null,
        note: `${inFreyja}; it shows the window's fill and can compact`,
      }
  }
}

export function Intent(p: IntentProps) {
  const { type, domain, query, slots, confidence, inferring, also, onPin, selected, act, onDone } = p
  const made = intentPlan(p)
  const req = requiredSlots(type)
  const filled = req.filter((k) => slotFilled(slots, k)).length
  const openInFreyja = () => {
    act({ type: 'palette-open', query })
    onDone()
  }
  return (
    <div className="island-intent" data-selected={selected ? 'true' : 'false'} data-nav="card">
      <div className="island-intent-head">
        <span className="island-kicker">{DOMAIN_LABEL[domain] ?? domain}</span>
        <span className="island-intent-title">{made.title}</span>
        <span className="island-intent-badge">
          {inferring ? (
            'jev…'
          ) : req.length > 0 ? (
            <span className="island-progress" title={`${filled} of ${req.length} details`}>
              {req.map((k) => (
                <i key={k} data-on={slotFilled(slots, k) ? 'true' : 'false'} />
              ))}
            </span>
          ) : (
            `${Math.round(confidence * 100)}%`
          )}
        </span>
      </div>
      {made.rows.length > 0 && (
        <div className="island-slots">
          {made.rows.map((row) => {
            const has = row.value !== undefined && row.value !== null && row.value !== ''
            return (
              <div className="island-slot" key={row.label}>
                <span className="island-slot-label">{row.label}</span>
                {has ? <span className="island-slot-value">{row.value}</span> : <span className="island-slot-hint">{row.hint}</span>}
              </div>
            )
          })}
        </div>
      )}
      {made.chips && (
        <div className="island-chips">
          {made.chips.map((chip) => (
            <button
              key={chip.label}
              type="button"
              className="island-key island-key-sm"
              onClick={() => {
                chip.run()
                onDone()
              }}
            >
              {chip.label}
            </button>
          ))}
        </div>
      )}
      <div className="island-intent-note">{made.note}</div>
      <div className="island-intent-keys">
        {also.length > 0 && (
          <span className="island-also">
            <span className="island-also-word">also</span>
            {also.map((c) => (
              <button key={c.domain} type="button" className="island-also-chip" onClick={() => onPin(c.domain)} title={`read it as ${DOMAIN_LABEL[c.domain] ?? c.domain}`}>
                {DOMAIN_LABEL[c.domain] ?? c.domain}
                <i style={{ width: `${Math.round(c.p * 100)}%` }} />
              </button>
            ))}
          </span>
        )}
        <span className="island-intent-spacer" />
        {made.primary ? (
          <>
            <button type="button" className="island-key island-key-sm" onClick={openInFreyja}>
              in Freyja
            </button>
            <button type="button" className="island-key island-key-sm island-key-primary" data-primary disabled={made.primary.disabled} onClick={made.primary.run}>
              {made.primary.label}
            </button>
          </>
        ) : (
          <button type="button" className="island-key island-key-sm island-key-primary" data-primary onClick={openInFreyja}>
            open in Freyja
          </button>
        )}
      </div>
    </div>
  )
}
