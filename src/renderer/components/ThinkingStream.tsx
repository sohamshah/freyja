import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { renderMarkdown } from '../lib/markdown'
import { highlightHtml } from '../lib/searchHighlight'
import { useCharacterReveal } from '../lib/characterReveal'
import { formatDuration } from '../lib/format'
import { Fold } from './Fold'

/** While live, only this much of the newest text is rendered. The window
 *  shows six lines, so the rest would be clipped anyway, and markdown cost
 *  stays flat however long the thought runs. */
const LIVE_TAIL_CHARS = 1400

/** A line that is only bold text: the step titles Claude and GPT write
 *  into their thinking summaries ("**Checking the store reducer**"). */
const STEP_TITLE = /^[ \t]*\*\*([^*\n]{2,120})\*\*[ \t]*$/gm

/**
 * A thinking part, drawn as an aside rather than a card: italic serif set
 * off by a hairline rule, so the thought reads as a different voice from
 * the answer.
 *
 * While the model writes it (`live`), it is open. A six-line window shows
 * the newest lines as they arrive, the oldest fading out at the top, and
 * the header names the step the model is on. When the next part starts (a
 * tool call, the answer, more thinking), it folds to one line. A click
 * opens or folds it; that choice holds until the block goes live again.
 */
export function ThinkingStream({
  text,
  live,
  startedAt,
  endedAt,
  searchQuery = '',
  compact = false,
}: {
  text: string
  live: boolean
  startedAt?: number
  endedAt?: number
  searchQuery?: string
  /** Smaller type for split panes. */
  compact?: boolean
}) {
  // null follows the default: open while live, folded after.
  const [choice, setChoice] = useState<boolean | null>(null)

  const searchHtml = useMemo(
    () => (searchQuery.trim() ? highlightHtml(renderMarkdown(withStepTitles(text)), searchQuery) : null),
    [text, searchQuery],
  )
  const searchHit = searchHtml?.includes('class="search-hit') ?? false

  // Going quiet folds the block, and a search hit inside it opens it, even
  // over an earlier click. Both are state derived from props, so they are
  // applied during render rather than one frame late in an effect.
  const [prev, setPrev] = useState({ live, searchHit })
  if (prev.live !== live || prev.searchHit !== searchHit) {
    if ((prev.live && !live) || (!prev.searchHit && searchHit)) setChoice(null)
    setPrev({ live, searchHit })
  }
  const open = choice ?? (live || searchHit)

  // Thinking often streams faster than the answer's typewriter pace; keep
  // the reveal close behind so the block never folds on half a thought.
  const shown = useCharacterReveal(text, live && open, 0.6)
  const steps = useMemo(() => stepTitles(live ? shown : text), [live, shown, text])
  const elapsed = useElapsed(live, startedAt, endedAt)

  // Live: the step the model is on. Folded: the steps it went through.
  // Open and done: nothing, since the text below carries the same titles.
  const summary = live
    ? steps[steps.length - 1] ?? ''
    : open
      ? ''
      : steps.length > 0
        ? steps.join('  →  ')
        : firstSentence(text)

  return (
    <div
      className={`thought${compact ? ' thought--compact' : ''}`}
      data-live={live ? 'true' : 'false'}
      data-open={open ? 'true' : 'false'}
    >
      <button
        type="button"
        className="thought-head"
        aria-expanded={open}
        onClick={() => setChoice(!open)}
        title={open ? 'Fold the thinking' : 'Show the thinking'}
      >
        <span className="thought-mark" aria-hidden />
        <span className="thought-verb">{live ? 'thinking' : 'thought'}</span>
        {elapsed != null && (
          <span className="thought-time">{live ? formatSeconds(elapsed) : `for ${formatSeconds(elapsed)}`}</span>
        )}
        {summary && (
          <span key={live ? summary : undefined} className={`thought-topic${live ? ' animate-fade-in' : ''}`}>
            {summary}
          </span>
        )}
        <svg className="thought-caret" viewBox="0 0 10 10" width="8" height="8" aria-hidden>
          <path d="M3 1.5 L7 5 L3 8.5" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <Fold open={open} stagger={false}>
        {live ? (
          <LiveWindow text={shown} />
        ) : (
          <div
            className="thought-body md selectable"
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: searchHtml ?? renderMarkdown(withStepTitles(text)) }}
          />
        )}
      </Fold>
    </div>
  )
}

/** The newest lines of a live thought, bottom-anchored: new text pushes old
 *  text up and out of a fixed window. The top fades only once the text
 *  overflows, so a short thought is never half-faded. */
function LiveWindow({ text }: { text: string }) {
  const tail = tailOf(text, LIVE_TAIL_CHARS)
  const html = useMemo(() => renderMarkdown(withStepTitles(tail)), [tail])
  const windowRef = useRef<HTMLDivElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const [overflowing, setOverflowing] = useState(false)
  useLayoutEffect(() => {
    const win = windowRef.current
    const body = bodyRef.current
    if (!win || !body) return
    const next = body.offsetHeight > win.clientHeight + 1
    if (next !== overflowing) setOverflowing(next)
  }, [html, overflowing])
  return (
    <div ref={windowRef} className="thought-window" data-overflowing={overflowing ? 'true' : 'false'}>
      <div
        ref={bodyRef}
        className="thought-body md"
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  )
}

/** Milliseconds of thinking: ticking while live, first-to-last delta after.
 *  Null when the block carries no timing (transcripts rebuilt from disk) or
 *  it all arrived in one burst. */
function useElapsed(live: boolean, startedAt?: number, endedAt?: number): number | null {
  const [mountedAt] = useState(() => Date.now())
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!live) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [live])
  if (live) return Math.max(0, now - (startedAt ?? mountedAt))
  if (startedAt == null || endedAt == null) return null
  const ms = endedAt - startedAt
  return ms >= 1000 ? ms : null
}

function formatSeconds(ms: number): string {
  return ms < 60_000 ? `${Math.floor(ms / 1000)}s` : formatDuration(ms)
}

/** Step titles become small headings, so the renderer can set them apart
 *  from the prose without touching bold text inside a sentence. */
function withStepTitles(text: string): string {
  return text.replace(STEP_TITLE, (_, title: string) => `\n#### ${title.trim()}\n`)
}

function stepTitles(text: string): string[] {
  const out: string[] = []
  for (const m of text.matchAll(STEP_TITLE)) out.push(m[1].trim())
  return out
}

function firstSentence(text: string): string {
  const flat = text.replace(/[*_`#>]+/g, '').replace(/\s+/g, ' ').trim()
  const end = flat.search(/[.!?](\s|$)/)
  return end > 0 && end < 160 ? flat.slice(0, end + 1) : flat.slice(0, 160)
}

/** The last `max` characters of `text`, starting at a line break (or at
 *  least a word) so the window never opens on half a word. */
function tailOf(text: string, max: number): string {
  if (text.length <= max) return text
  const cut = text.length - max
  const line = text.indexOf('\n', cut)
  if (line >= 0 && text.length - line > max / 3) return text.slice(line + 1)
  const space = text.indexOf(' ', cut)
  return space >= 0 ? text.slice(space + 1) : text.slice(cut)
}
