import { useEffect, useRef, useState } from 'react'
import type { ArtifactRecord, ArtifactReadResult } from '@shared/events'
import { extractArtifactMeta, type ArtifactMeta } from './artifactMeta'

/**
 * Lazy-load artifact metadata for a list of records.
 *
 * - Fires `artifact:read` IPC per artifact, capped at 6 concurrent
 *   requests so we don't hammer the main process.
 * - Caches results in a stable Map keyed by path so re-renders don't
 *   refetch.
 * - For binary files (images, etc.) skips the read entirely and just
 *   uses the filename as the title.
 *
 * Returns `byPath` — a record of path → meta. Keys absent until the
 * fetch resolves.
 */
export function useArtifactMeta(artifacts: ArtifactRecord[]): {
  byPath: Record<string, ArtifactMeta>
  loading: boolean
} {
  const [byPath, setByPath] = useState<Record<string, ArtifactMeta>>({})
  const [loading, setLoading] = useState(false)
  // Persist the cache across re-renders so we don't refetch when the
  // artifacts array changes shape (e.g. new entries appended).
  const cacheRef = useRef<Map<string, ArtifactMeta>>(new Map())
  // Track in-flight paths to dedupe concurrent fetches.
  const inFlightRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    const api = (window as any).harness
    if (!api?.artifactRead) return

    const TEXTUAL = new Set([
      'md', 'markdown', 'txt', 'log', 'json', 'yaml', 'yml', 'toml',
      'csv', 'tsv', 'html', 'htm', 'svg', 'xml',
      'ts', 'tsx', 'js', 'jsx', 'py', 'rs', 'go', 'java', 'c', 'h',
      'cpp', 'css', 'scss', 'sh', 'bash', 'zsh', 'sql',
    ])

    const pending = artifacts.filter(
      (a) =>
        TEXTUAL.has(a.fileType) &&
        !cacheRef.current.has(a.path) &&
        !inFlightRef.current.has(a.path),
    )
    if (pending.length === 0) return

    setLoading(true)

    // Concurrency: process a window of N at a time.
    const CONCURRENCY = 6
    let cancelled = false

    const processOne = async (artifact: ArtifactRecord): Promise<void> => {
      inFlightRef.current.add(artifact.path)
      try {
        const result: ArtifactReadResult = await api.artifactRead(artifact.path)
        if (cancelled) return
        const content = result.ok && result.content ? result.content : ''
        const meta = extractArtifactMeta(content, artifact.fileType, artifact.filename)
        cacheRef.current.set(artifact.path, meta)
      } catch {
        // Cache an empty meta so we don't keep retrying.
        cacheRef.current.set(artifact.path, {
          title: artifact.filename,
          excerpt: '',
        })
      } finally {
        inFlightRef.current.delete(artifact.path)
      }
    }

    const drain = async () => {
      const queue = [...pending]
      const workers: Promise<void>[] = []
      for (let i = 0; i < CONCURRENCY; i++) {
        workers.push((async () => {
          while (queue.length > 0 && !cancelled) {
            const next = queue.shift()
            if (next) await processOne(next)
          }
        })())
      }
      await Promise.all(workers)
      if (!cancelled) {
        // Sync cache → state once all done. We use a single state update
        // so React batches the re-render instead of triggering one per
        // artifact.
        setByPath(Object.fromEntries(cacheRef.current))
        setLoading(false)
      }
    }
    drain()

    // Also flush periodically so the user sees results stream in
    // rather than waiting for the full batch.
    const interval = window.setInterval(() => {
      if (!cancelled) {
        setByPath(Object.fromEntries(cacheRef.current))
      }
    }, 250)

    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifacts.length, artifacts.map((a) => a.path).join('|')])

  return { byPath, loading }
}
