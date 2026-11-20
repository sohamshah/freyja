import { useEffect, useRef, useState } from 'react'

import type { SessionSearchResult } from '@shared/events'

/** Wait for typing to pause before asking main to search the files. */
const DEBOUNCE_MS = 200

type SearchApi = { sessionSearch?: (query: string) => Promise<SessionSearchResult> }

function searchApi(): SearchApi | undefined {
  return (window as unknown as { harness?: SearchApi }).harness
}

/**
 * Ids of sessions on disk that match `query` (src/main/sessionSearch.ts).
 *
 * The sidebar can only search transcripts it has loaded; this covers the
 * rest. `query` is the normalized token string (lowercased, single spaces).
 * Returns null until main has answered for exactly this query, so ids found
 * for an older query are never shown against a newer one. The last query
 * wins: an answer that arrives after the query moved on is dropped. When the
 * call fails, or the method is missing (the perf mock harness), it stays
 * null and the sidebar shows its in-memory matches alone.
 */
export function useDiskSessionSearch(query: string): ReadonlySet<string> | null {
  const [result, setResult] = useState<{ query: string; ids: ReadonlySet<string> } | null>(null)
  const seq = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    if (!query) {
      setResult(null)
      return
    }
    const api = searchApi()
    if (typeof api?.sessionSearch !== 'function') return
    const timer = window.setTimeout(() => {
      Promise.resolve()
        .then(() => api.sessionSearch!(query))
        .then(
        (res) => {
          if (mine !== seq.current) return
          if (!res?.ok || !Array.isArray(res.ids)) return
          setResult({ query, ids: new Set(res.ids) })
        },
        () => {
          /* the in-memory matches stand */
        },
      )
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [query])

  return result && result.query === query ? result.ids : null
}

/** Bring the disk index up to date before the first query lands, so the
 *  first search after a launch does not wait on changed files. */
export function warmDiskSessionSearch(): void {
  const api = searchApi()
  if (typeof api?.sessionSearch !== 'function') return
  Promise.resolve()
    .then(() => api.sessionSearch!(''))
    .catch(() => {})
}
