import { useEffect, useState } from 'react'

/**
 * Modal showing the unified diff between a pending candidate and the
 * existing on-disk SKILL.md it would overwrite.
 *
 * Fetched lazily via the ``skill:candidateDiff`` IPC so we don't ship a
 * potentially 100KB body delta on every ``skill_candidate`` event. The
 * modal stays open until the operator dismisses; PROMOTE/DISCARD live
 * on the SkillToast underneath.
 *
 * Coloring is the standard diff palette: `+` lines green, `-` lines
 * red, `@@` hunk headers muted, context lines neutral. No syntax
 * highlighting — operators are scanning structural changes, not
 * reading code.
 */
export function SkillDiffModal({
  candidateId,
  name,
  onClose,
}: {
  candidateId: string
  name: string
  onClose: () => void
}) {
  const [diffText, setDiffText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [exists, setExists] = useState<boolean | null>(null)
  const [skillPath, setSkillPath] = useState<string>('')

  useEffect(() => {
    const api = (window as any).harness
    if (!api?.skillCandidateDiff) {
      setError('skill diff IPC unavailable in this build')
      return
    }
    let cancelled = false
    api
      .skillCandidateDiff(candidateId)
      .then(
        (res: {
          ok: boolean
          exists?: boolean
          unifiedDiff?: string
          skillPath?: string
          error?: string
        }) => {
          if (cancelled) return
          if (!res?.ok) {
            setError(res?.error ?? 'unknown error')
            return
          }
          setExists(res.exists ?? false)
          setSkillPath(res.skillPath ?? '')
          setDiffText(res.unifiedDiff ?? '')
        },
      )
      .catch((err: unknown) => {
        if (cancelled) return
        setError(String(err))
      })
    return () => {
      cancelled = true
    }
  }, [candidateId])

  // Dismiss on Esc — matches the rest of the modal palette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flex max-h-[88vh] w-[920px] max-w-[calc(100vw-3rem)] flex-col overflow-hidden rounded-xl glass-strong shadow-2xl ring-hairline-strong"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 px-4 py-3 hairline-b">
          <span className="label text-accent">diff</span>
          <span className="font-mono text-[12px] text-fg-0">{name}</span>
          {skillPath && (
            <span className="truncate font-mono text-[10px] text-fg-3">
              {skillPath.replace(/^\/Users\/[^/]+/, '~')}
            </span>
          )}
          <button
            onClick={onClose}
            className="ml-auto rounded bg-white/[0.04] px-2 py-[2px] font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            title="Close (Esc)"
          >
            close
          </button>
        </div>
        <div className="flex-1 overflow-y-auto bg-black/30">
          {error ? (
            <div className="px-4 py-3 font-mono text-[11px] text-danger">
              error: {error}
            </div>
          ) : diffText === null ? (
            <div className="px-4 py-3 font-mono text-[11px] text-fg-3">
              loading diff…
            </div>
          ) : exists === false ? (
            <div className="px-4 py-3 font-mono text-[11px] text-fg-2">
              No existing skill at that name — this candidate is net-new.
              Nothing to diff.
            </div>
          ) : diffText.trim().length === 0 ? (
            <div className="px-4 py-3 font-mono text-[11px] text-fg-2">
              Identical — the candidate body matches the existing skill
              verbatim. No-op promote.
            </div>
          ) : (
            <pre className="m-0 whitespace-pre p-4 font-mono text-[11.5px] leading-[1.5]">
              {diffText.split('\n').map((line, i) => (
                <div key={i} className={diffLineTone(line)}>
                  {line || ' '}
                </div>
              ))}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}

function diffLineTone(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) {
    return 'text-fg-2'
  }
  if (line.startsWith('@@')) {
    return 'text-fg-3 bg-white/[0.03]'
  }
  if (line.startsWith('+')) {
    return 'text-ok bg-ok/[0.06]'
  }
  if (line.startsWith('-')) {
    return 'text-danger bg-danger/[0.06]'
  }
  return 'text-fg-1'
}
