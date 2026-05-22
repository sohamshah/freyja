import { useMemo, useState } from 'react'

/**
 * Detect + render JSON output an agent emitted as plain text.
 *
 * Agents like the judge calibrator return a single JSON object as
 * their entire assistant turn. Rendering that as markdown produces a
 * wall of inline string we can't read. This module recognises the
 * shape and swaps in a structured card.
 *
 * Recognition has two layers:
 *   1. Lenient parse — strip fences / preamble / postamble and try to
 *      pull the first balanced `{...}` or `[...]` out of the text.
 *   2. Schema dispatch — if the parsed value matches a known shape
 *      (calibrator response, judge verdict), render a specialised
 *      card. Otherwise fall back to a pretty-printed JSON view.
 *
 * Callers gate this on streaming-complete to avoid trying to parse
 * partial JSON every keystroke.
 */

export function tryParseCompleteJson(raw: string): unknown | undefined {
  if (!raw) return undefined
  let s = raw.trim()
  if (!s) return undefined
  // Strip ``` fences (with or without `json` language tag). Same
  // tolerance as the bridge-side parser in goal_loop.py — the
  // calibrator prompt forbids fences but agents sometimes ignore
  // that, and a fenced JSON shouldn't fall through to markdown if we
  // can rescue it.
  if (s.startsWith('```')) {
    s = s
      .replace(/^```(?:json|JSON)?\s*\n?/, '')
      .replace(/\n?```\s*$/, '')
      .trim()
  }
  // Locate the first opener and walk a depth tracker to its matching
  // close. Skips characters inside strings (with backslash escape
  // handling) so braces inside quoted values don't confuse the depth
  // count. Anything before the opener / after the matching close is
  // ignored — preamble and postamble are tolerated.
  const firstObj = s.indexOf('{')
  const firstArr = s.indexOf('[')
  let start: number
  if (firstObj < 0 && firstArr < 0) return undefined
  if (firstObj < 0) start = firstArr
  else if (firstArr < 0) start = firstObj
  else start = Math.min(firstObj, firstArr)
  const opener = s[start]
  const closer = opener === '{' ? '}' : ']'
  let depth = 0
  let inString = false
  let escape = false
  let end = -1
  for (let i = start; i < s.length; i++) {
    const c = s[i]
    if (escape) {
      escape = false
      continue
    }
    if (c === '\\') {
      escape = true
      continue
    }
    if (c === '"') {
      inString = !inString
      continue
    }
    if (inString) continue
    if (c === opener) depth += 1
    else if (c === closer) {
      depth -= 1
      if (depth === 0) {
        end = i
        break
      }
    }
  }
  if (end < 0) return undefined
  const candidate = s.slice(start, end + 1)
  // Require the candidate to be most of the input. If it's only a
  // small fraction of a much longer body, the text is probably prose
  // that happens to contain a JSON snippet — let markdown handle it.
  if (candidate.length < Math.min(s.length * 0.6, s.length - 80)) {
    return undefined
  }
  try {
    return JSON.parse(candidate)
  } catch {
    return undefined
  }
}

interface CalibratorJson {
  judgeProfile: 'skip' | 'quick' | 'standard' | 'deep'
  rigorScore: 1 | 2 | 3 | 4
  voice: string
  criteria: Array<{
    id: string
    text: string
    priority: 'must' | 'should' | 'may'
  }>
  neverDo: string[]
  whenToStop?: string
  judgeTools?: string[]
  rationaleOverall?: string
  rationaleByField?: Record<string, string>
  confidence?: number
}

function isCalibratorJson(data: unknown): data is CalibratorJson {
  if (!data || typeof data !== 'object') return false
  const d = data as Record<string, unknown>
  return (
    typeof d.judgeProfile === 'string' &&
    Array.isArray(d.criteria) &&
    typeof d.voice === 'string'
  )
}

export function StructuredJsonView({ data }: { data: unknown }) {
  const [showRaw, setShowRaw] = useState(false)
  const pretty = useMemo(() => JSON.stringify(data, null, 2), [data])

  if (showRaw) {
    return (
      <div className="rounded-md border border-white/[0.06] bg-white/[0.02]">
        <div className="flex items-center justify-between border-b border-white/[0.04] px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
          <span>raw json</span>
          <button
            type="button"
            onClick={() => setShowRaw(false)}
            className="rounded px-1.5 py-0.5 text-fg-3 hover:bg-white/[0.06] hover:text-fg-1"
          >
            structured
          </button>
        </div>
        <pre className="m-0 max-h-[480px] select-text overflow-auto px-3 py-2 font-mono text-[11.5px] leading-[1.55] text-fg-1 whitespace-pre">
          {pretty}
        </pre>
      </div>
    )
  }

  if (isCalibratorJson(data)) {
    return <CalibratorCard data={data} onShowRaw={() => setShowRaw(true)} />
  }
  return <GenericJsonView data={data} onShowRaw={() => setShowRaw(true)} />
}

function CalibratorCard({
  data,
  onShowRaw,
}: {
  data: CalibratorJson
  onShowRaw: () => void
}) {
  const [rationaleOpen, setRationaleOpen] = useState(false)
  const profile = data.judgeProfile
  const profileCls =
    profile === 'deep'
      ? 'text-accent border-accent/[0.32] bg-accent/[0.08]'
      : profile === 'quick'
      ? 'text-fg-1 border-fg-4/[0.32] bg-white/[0.03]'
      : profile === 'skip'
      ? 'text-fg-3 border-fg-4/[0.32] bg-white/[0.02]'
      : 'text-fg-1 border-white/[0.12] bg-white/[0.04]'
  const rigor = data.rigorScore
  const rigorCls =
    rigor >= 4
      ? 'text-warn'
      : rigor >= 3
      ? 'text-accent'
      : 'text-fg-2'
  const conf = typeof data.confidence === 'number' ? data.confidence : null
  const confCls =
    conf == null
      ? 'text-fg-3'
      : conf >= 0.85
      ? 'text-ok'
      : conf >= 0.5
      ? 'text-accent'
      : 'text-warn'
  const criteriaByPriority = useMemo(() => {
    const groups: Record<'must' | 'should' | 'may', CalibratorJson['criteria']> = {
      must: [],
      should: [],
      may: [],
    }
    for (const c of data.criteria) {
      const k = (c.priority as 'must' | 'should' | 'may') ?? 'should'
      if (groups[k]) groups[k].push(c)
      else groups.should.push(c)
    }
    return groups
  }, [data.criteria])

  return (
    <div className="overflow-hidden rounded-lg border border-accent/[0.18] bg-accent/[0.025]">
      <header className="flex flex-wrap items-center gap-2.5 border-b border-white/[0.05] px-3.5 py-2">
        <span className="font-mono text-[9.5px] uppercase tracking-[0.18em] text-fg-4">
          calibrator
        </span>
        <span
          className={`inline-flex items-center rounded-[6px] border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] ${profileCls}`}
        >
          {profile}
        </span>
        <span className="font-mono text-[11px] tabular-nums text-fg-2">
          rigor <span className={rigorCls}>{rigor}/4</span>
        </span>
        {conf != null ? (
          <span className="font-mono text-[11px] tabular-nums text-fg-2">
            conf <span className={confCls}>{conf.toFixed(2)}</span>
          </span>
        ) : null}
        <button
          type="button"
          onClick={onShowRaw}
          className="ml-auto rounded px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3 transition hover:bg-white/[0.06] hover:text-fg-1"
        >
          raw
        </button>
      </header>

      {data.voice ? (
        <Section label="voice">
          <p className="m-0 whitespace-pre-wrap font-prose text-[12.5px] leading-[1.6] text-fg-1">
            {data.voice}
          </p>
        </Section>
      ) : null}

      {data.criteria.length > 0 ? (
        <Section label={`criteria · ${data.criteria.length}`}>
          <div className="flex flex-col gap-1.5">
            {(['must', 'should', 'may'] as const).map((p) =>
              criteriaByPriority[p].length === 0 ? null : (
                <CriteriaGroup
                  key={p}
                  priority={p}
                  items={criteriaByPriority[p]}
                />
              ),
            )}
          </div>
        </Section>
      ) : null}

      {data.neverDo && data.neverDo.length > 0 ? (
        <Section label={`never do · ${data.neverDo.length}`}>
          <ul className="m-0 flex list-none flex-col gap-1">
            {data.neverDo.map((item, i) => (
              <li
                key={i}
                className="select-text rounded-md border border-warn/[0.16] bg-warn/[0.04] px-2 py-1 font-prose text-[12px] leading-[1.55] text-fg-1"
              >
                {item}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {data.whenToStop && data.whenToStop.trim() ? (
        <Section label="when to stop">
          <p className="m-0 whitespace-pre-wrap font-prose text-[12px] leading-[1.55] text-fg-1">
            {data.whenToStop}
          </p>
        </Section>
      ) : null}

      {data.judgeTools && data.judgeTools.length > 0 ? (
        <Section label="judge tools">
          <div className="flex flex-wrap gap-1.5">
            {data.judgeTools.map((t, i) => (
              <span
                key={i}
                className="inline-flex items-center rounded border border-white/[0.10] bg-white/[0.04] px-1.5 py-0.5 font-mono text-[10.5px] text-fg-1"
              >
                {t}
              </span>
            ))}
          </div>
        </Section>
      ) : null}

      {data.rationaleOverall || data.rationaleByField ? (
        <Section label="rationale">
          {data.rationaleOverall ? (
            <p className="m-0 mb-2 whitespace-pre-wrap font-prose text-[12px] leading-[1.55] text-fg-2">
              {data.rationaleOverall}
            </p>
          ) : null}
          {data.rationaleByField &&
          Object.keys(data.rationaleByField).length > 0 ? (
            <>
              <button
                type="button"
                onClick={() => setRationaleOpen((v) => !v)}
                className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3 hover:text-fg-1"
              >
                {rationaleOpen ? '▾' : '▸'} per-field rationale
              </button>
              {rationaleOpen ? (
                <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5">
                  {Object.entries(data.rationaleByField).map(([k, v]) => (
                    <RationaleRow key={k} field={k} text={v} />
                  ))}
                </dl>
              ) : null}
            </>
          ) : null}
        </Section>
      ) : null}
    </div>
  )
}

function CriteriaGroup({
  priority,
  items,
}: {
  priority: 'must' | 'should' | 'may'
  items: CalibratorJson['criteria']
}) {
  const chipCls =
    priority === 'must'
      ? 'text-warn border-warn/[0.30] bg-warn/[0.08]'
      : priority === 'should'
      ? 'text-accent border-accent/[0.28] bg-accent/[0.06]'
      : 'text-fg-2 border-white/[0.12] bg-white/[0.04]'
  return (
    <div className="flex flex-col gap-1">
      <span
        className={`self-start rounded border px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.14em] ${chipCls}`}
      >
        {priority}
      </span>
      <ul className="m-0 flex list-none flex-col gap-1">
        {items.map((c) => (
          <li
            key={c.id}
            className="select-text rounded-md border border-white/[0.06] bg-white/[0.02] px-2 py-1 font-prose text-[12px] leading-[1.55] text-fg-1"
          >
            <span className="mr-2 font-mono text-[10px] text-fg-4">{c.id}</span>
            {c.text}
          </li>
        ))}
      </ul>
    </div>
  )
}

function RationaleRow({ field, text }: { field: string; text: string }) {
  return (
    <>
      <dt className="font-mono text-[10.5px] uppercase tracking-[0.10em] text-fg-3">
        {field}
      </dt>
      <dd className="m-0 whitespace-pre-wrap font-prose text-[11.5px] leading-[1.55] text-fg-2">
        {text || <span className="text-fg-4">—</span>}
      </dd>
    </>
  )
}

function Section({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <section className="border-t border-white/[0.04] px-3.5 py-2.5 first:border-t-0">
      <div className="mb-1.5 font-mono text-[9.5px] uppercase tracking-[0.18em] text-fg-4">
        {label}
      </div>
      {children}
    </section>
  )
}

function GenericJsonView({
  data,
  onShowRaw,
}: {
  data: unknown
  onShowRaw: () => void
}) {
  const pretty = useMemo(() => JSON.stringify(data, null, 2), [data])
  return (
    <div className="overflow-hidden rounded-md border border-white/[0.06] bg-white/[0.02]">
      <header className="flex items-center justify-between border-b border-white/[0.04] px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
        <span>json output</span>
        <button
          type="button"
          onClick={onShowRaw}
          className="rounded px-1.5 py-0.5 text-fg-3 hover:bg-white/[0.06] hover:text-fg-1"
        >
          raw
        </button>
      </header>
      <pre className="m-0 max-h-[480px] select-text overflow-auto px-3 py-2 font-mono text-[11.5px] leading-[1.55] text-fg-1 whitespace-pre">
        {pretty}
      </pre>
    </div>
  )
}
