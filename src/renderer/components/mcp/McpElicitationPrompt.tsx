import { useEffect, useMemo, useState } from 'react'
import type { McpElicitationProperty, McpElicitationSchema } from '@shared/events'
import { useHarness, type McpElicitationRecord } from '../../state/store'
import { coerceElicitationValue } from '../../lib/mcp'
import { BTN, BTN_ACCENT, INPUT, formatCountdown, openExternalUrl } from './mcpUi'
import { useNowTicker } from './McpOAuthNotice'

type FieldValue = string | boolean

export interface ElicitationField {
  key: string
  type: string
  title: string
  description?: string
  required: boolean
  enumValues?: Array<string | number>
  enumLabels?: string[]
  format?: string
  minimum?: number
  maximum?: number
  defaultValue?: unknown
}

function primaryType(t: string | string[] | undefined): string {
  if (Array.isArray(t)) return t.find((x) => x !== 'null') ?? 'string'
  return t ?? 'string'
}

/** Flatten a JSON-schema object into renderable fields. Unsupported
 *  property types (object/array) degrade to a free-text field whose value
 *  is parsed as JSON when it looks like JSON. */
export function fieldsFromSchema(schema: McpElicitationSchema | undefined): ElicitationField[] {
  if (!schema || typeof schema !== 'object') return []
  const props = schema.properties ?? {}
  const required = new Set(Array.isArray(schema.required) ? schema.required : [])
  return Object.entries(props).map(([key, raw]) => {
    const p: McpElicitationProperty = raw && typeof raw === 'object' ? raw : {}
    const type = primaryType(p.type)
    return {
      key,
      type,
      title: typeof p.title === 'string' && p.title ? p.title : key,
      description: typeof p.description === 'string' ? p.description : undefined,
      required: required.has(key),
      enumValues: Array.isArray(p.enum) ? p.enum : undefined,
      enumLabels: Array.isArray(p.enumNames) ? p.enumNames : undefined,
      format: typeof p.format === 'string' ? p.format : undefined,
      minimum: typeof p.minimum === 'number' ? p.minimum : undefined,
      maximum: typeof p.maximum === 'number' ? p.maximum : undefined,
      defaultValue: p.default,
    }
  })
}

function initialValues(fields: ElicitationField[]): Record<string, FieldValue> {
  const out: Record<string, FieldValue> = {}
  for (const f of fields) {
    if (f.type === 'boolean') out[f.key] = f.defaultValue === true
    else if (f.defaultValue != null) out[f.key] = String(f.defaultValue)
    else if (f.enumValues && f.enumValues.length && f.required) out[f.key] = String(f.enumValues[0])
    else out[f.key] = ''
  }
  return out
}

/** Typed `content` for mcp_elicitation_response; `errors` lists required
 *  fields that are still empty or unparsable. */
export function buildElicitationContent(
  fields: ElicitationField[],
  values: Record<string, FieldValue>,
): { content: Record<string, unknown>; errors: string[] } {
  const content: Record<string, unknown> = {}
  const errors: string[] = []
  for (const f of fields) {
    const raw = values[f.key]
    let v: unknown
    if (f.type === 'object' || f.type === 'array') {
      const s = typeof raw === 'string' ? raw.trim() : ''
      if (s) {
        try {
          v = JSON.parse(s)
        } catch {
          v = s
        }
      }
    } else if (f.enumValues && f.enumValues.length && typeof raw === 'string' && raw !== '') {
      // Preserve the enum member's own type (number vs string).
      v = f.enumValues.find((e) => String(e) === raw) ?? raw
    } else {
      v = coerceElicitationValue(raw, f.type)
    }
    if (v === undefined || v === '') {
      if (f.required) errors.push(f.key)
      continue
    }
    if ((f.type === 'number' || f.type === 'integer') && typeof v === 'number') {
      if (f.minimum != null && v < f.minimum) errors.push(f.key)
      if (f.maximum != null && v > f.maximum) errors.push(f.key)
    }
    content[f.key] = v
  }
  return { content, errors }
}

function inputTypeFor(f: ElicitationField): string {
  if (f.type === 'number' || f.type === 'integer') return 'number'
  switch (f.format) {
    case 'email':
      return 'email'
    case 'uri':
    case 'url':
      return 'url'
    case 'date':
      return 'date'
    case 'date-time':
      return 'datetime-local'
    case 'password':
      return 'password'
    default:
      return 'text'
  }
}

/**
 * Presentational card — modeled on PermissionPrompt. `now` is injected so
 * the countdown is testable; `onAnswer` receives the typed response.
 */
export function McpElicitationCard({
  request,
  queueLength,
  now,
  onAnswer,
}: {
  request: McpElicitationRecord
  queueLength: number
  now: number
  onAnswer: (action: 'accept' | 'decline' | 'cancel', content?: Record<string, unknown>) => void
}) {
  const fields = useMemo(() => fieldsFromSchema(request.requestedSchema), [request.requestId])
  const [values, setValues] = useState<Record<string, FieldValue>>(() => initialValues(fields))
  const [touched, setTouched] = useState(false)
  useEffect(() => {
    setValues(initialValues(fields))
    setTouched(false)
  }, [request.requestId])

  const deadline = request.receivedAt + request.timeoutS * 1000
  const remainingS = Math.max(0, Math.ceil((deadline - now) / 1000))
  const { content, errors } = buildElicitationContent(fields, values)
  const canAccept = request.mode === 'url' || errors.length === 0

  const accept = () => {
    setTouched(true)
    if (!canAccept) return
    onAnswer('accept', request.mode === 'form' ? content : undefined)
  }

  const schemaTitle = request.requestedSchema?.title
  const schemaDescription = request.requestedSchema?.description
  const urgent = remainingS <= 30

  return (
    <div className="relative w-[560px] overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong" data-testid="mcp-elicitation">
      <div className="flex items-center gap-3 px-5 py-4 hairline-b">
        <span className="font-mono text-[15px] text-accent">◆</span>
        <span className="label text-accent">mcp · {request.server}</span>
        <span className="label text-fg-2">{request.mode === 'url' ? 'action required' : 'input requested'}</span>
        <span
          className={`label ml-auto font-mono ${urgent ? 'text-danger' : 'text-fg-2'}`}
          title="auto-cancels when the timer lapses"
        >
          {formatCountdown(remainingS)}
        </span>
      </div>
      <form
        className="px-5 py-5"
        onSubmit={(e) => {
          e.preventDefault()
          accept()
        }}
      >
        <div className="mb-4">
          <div className="mb-1 label">message</div>
          <div className="selectable whitespace-pre-wrap rounded-md bg-black/45 p-3 text-[12px] leading-[1.5] text-fg-0 ring-hairline">
            {request.message}
          </div>
        </div>

        {request.mode === 'url' && (
          <div className="mb-4">
            <div className="mb-1 label">link</div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className={BTN_ACCENT}
                disabled={!request.url}
                onClick={() => request.url && openExternalUrl(request.url)}
              >
                open link
              </button>
              <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-fg-2" title={request.url}>
                {request.url ?? '(no url provided)'}
              </span>
            </div>
            <div className="mt-2 text-[10.5px] leading-[1.45] text-fg-2">
              Complete the step in your browser, then confirm below so the server can continue.
            </div>
          </div>
        )}

        {request.mode === 'form' && (
          <div className="mb-4">
            <div className="mb-1 label">
              {schemaTitle ? schemaTitle : 'fields'}
              {fields.some((f) => f.required) && <span className="ml-2 text-fg-3">* required</span>}
            </div>
            {schemaDescription && (
              <div className="mb-2 text-[11px] leading-[1.5] text-fg-2">{schemaDescription}</div>
            )}
            {fields.length === 0 ? (
              <div className="text-[11px] italic text-fg-3">No fields — accepting sends an empty response.</div>
            ) : (
              <div className="space-y-2.5">
                {fields.map((f) => {
                  const invalid = touched && errors.includes(f.key)
                  const id = `elicit-${request.requestId}-${f.key}`
                  const value = values[f.key]
                  const set = (v: FieldValue) => setValues((prev) => ({ ...prev, [f.key]: v }))
                  return (
                    <div key={f.key} className="rounded-md bg-white/[0.025] px-2.5 py-2 ring-hairline">
                      <label htmlFor={id} className="flex items-baseline gap-2">
                        <span className="font-mono text-[11px] text-fg-0">
                          {f.title}
                          {f.required && <span className="text-warn"> *</span>}
                        </span>
                        <span className="font-mono text-[10px] text-fg-3">{f.type}</span>
                        {invalid && <span className="ml-auto font-mono text-[10px] text-danger">required</span>}
                      </label>
                      {f.description && (
                        <div className="mt-0.5 text-[10.5px] leading-[1.45] text-fg-2">{f.description}</div>
                      )}
                      <div className="mt-1.5">
                        {f.type === 'boolean' ? (
                          <label className="flex cursor-pointer items-center gap-2">
                            <input
                              id={id}
                              type="checkbox"
                              checked={value === true}
                              onChange={(e) => set(e.target.checked)}
                              className="h-3 w-3 accent-accent"
                            />
                            <span className="font-mono text-[10.5px] text-fg-1">{value === true ? 'yes' : 'no'}</span>
                          </label>
                        ) : f.enumValues && f.enumValues.length > 0 ? (
                          <select
                            id={id}
                            value={typeof value === 'string' ? value : ''}
                            onChange={(e) => set(e.target.value)}
                            className={INPUT}
                          >
                            {!f.required && <option value="">—</option>}
                            {f.enumValues.map((opt, i) => (
                              <option key={String(opt)} value={String(opt)}>
                                {f.enumLabels?.[i] ?? String(opt)}
                              </option>
                            ))}
                          </select>
                        ) : f.type === 'object' || f.type === 'array' ? (
                          <textarea
                            id={id}
                            value={typeof value === 'string' ? value : ''}
                            onChange={(e) => set(e.target.value)}
                            rows={3}
                            placeholder="JSON"
                            className={`${INPUT} resize-y`}
                          />
                        ) : (
                          <input
                            id={id}
                            type={inputTypeFor(f)}
                            value={typeof value === 'string' ? value : ''}
                            onChange={(e) => set(e.target.value)}
                            min={f.minimum}
                            max={f.maximum}
                            step={f.type === 'integer' ? 1 : f.type === 'number' ? 'any' : undefined}
                            className={`${INPUT} ${invalid ? 'ring-1 ring-danger/50' : ''}`}
                          />
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {queueLength > 1 && (
          <div className="mb-4 text-[10.5px] text-fg-3">{queueLength - 1} more pending…</div>
        )}

        <div className="mt-4 flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => onAnswer('cancel')}
            className="font-mono text-[10px] uppercase tracking-[0.08em] text-fg-3 hover:text-fg-1"
          >
            cancel <span className="text-fg-4">(esc)</span>
          </button>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => onAnswer('decline')} className={`${BTN} px-3 py-1.5 text-[11px]`}>
              decline
            </button>
            <button
              type="submit"
              disabled={touched && !canAccept}
              className={`${BTN_ACCENT} px-3 py-1.5 text-[11px]`}
            >
              {request.mode === 'url' ? "i've completed this" : 'accept'}{' '}
              <span className="text-fg-3">(↵)</span>
            </button>
          </div>
        </div>
      </form>
    </div>
  )
}

/**
 * Store-connected modal. Shows the head of `mcp.elicitations`; the rest
 * queue behind it. Enter accepts (form mode only when valid), Esc cancels.
 * When the countdown lapses the request is auto-answered `cancel`.
 */
export function McpElicitationPrompt() {
  const queue = useHarness((s) => s.mcp.elicitations)
  const answer = useHarness((s) => s.answerMcpElicitation)
  const current = queue[0]
  const now = useNowTicker(!!current)

  // Auto-cancel on timeout (covers every queued item, not just the head,
  // so a stale one behind a long-lived head doesn't linger).
  useEffect(() => {
    for (const e of queue) {
      if (e.receivedAt + e.timeoutS * 1000 <= now) void answer(e.requestId, 'cancel')
    }
  }, [now, queue, answer])

  useEffect(() => {
    if (!current) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        void answer(current.requestId, 'cancel')
      }
      // Enter is handled by the card's accept button for form mode so
      // required-field validation runs; in url mode there's nothing to
      // validate, accept directly.
      if (e.key === 'Enter' && current.mode === 'url') {
        const target = e.target as HTMLElement | null
        if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
        e.preventDefault()
        void answer(current.requestId, 'accept')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [current?.requestId, current?.mode, answer])

  if (!current) return null
  return (
    <div className="fixed inset-0 z-[49] flex items-start justify-center pt-[14vh]">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-[2px]" />
      <McpElicitationCard
        request={current}
        queueLength={queue.length}
        now={now}
        onAnswer={(action, content) => void answer(current.requestId, action, content)}
      />
    </div>
  )
}
