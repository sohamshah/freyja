import { createContext, Fragment } from 'react'
import { highlightRuns } from '../lib/searchHighlight'

/** Current in-session search query, shared across every conversation part so
 *  text AND tool chips can highlight matches. Empty string = no active search.
 *  Lives in its own module so leaf components (ToolCallChip, ParallelToolGroup)
 *  can consume it without importing Conversation.tsx (which imports them). */
export const SearchQueryContext = createContext<string>('')

/**
 * Render `text` with case-insensitive matches of `query` wrapped in
 * `<mark class="search-hit">`, so the conversation's find navigation — which
 * counts/scrolls `.search-hit` nodes — treats tool names, arg summaries, and
 * tool output as first-class matches alongside prose. No query → plain text.
 */
export function HighlightText({ text, query }: { text: string; query?: string }) {
  const q = (query ?? '').trim()
  if (!q || !text) return <>{text}</>
  return (
    <>
      {highlightRuns(text, q).map((run, i) =>
        run.isHit ? (
          <mark key={i} className="search-hit">
            {run.text}
          </mark>
        ) : (
          <Fragment key={i}>{run.text}</Fragment>
        ),
      )}
    </>
  )
}
