import { useEffect, useState } from 'react'
import { SkillDiffModal } from './SkillDiffModal'

/**
 * Centered modal for fully reviewing a pending skill candidate.
 *
 * The bottom-right SkillToast can only afford ~600 chars of preview;
 * an "edit" path needs the full body verbatim, and operators reviewing
 * a learned skill before promote want to scroll the whole markdown.
 * This modal pulls the FULL rendered SKILL.md (frontmatter + body) via
 * the existing skill:candidateDiff IPC — that helper already produces
 * the same bytes confirmation.promote would write, so what the operator
 * reviews here is exactly what lands on disk.
 *
 * From here:
 *   · "view diff" opens SkillDiffModal stacked above this one when the
 *     candidate would overwrite an existing skill.
 *   · "open candidate file" hits skill:open under the .candidates/
 *     directory so the operator can edit in their default editor (yaml
 *     view, full body, no preview cap) — useful when even the modal's
 *     scroll feels too cramped.
 *
 * Dismissal: Esc, click-outside, or the close button. No actions are
 * taken on dismiss — the toast underneath keeps the candidate in the
 * queue.
 */
export function SkillDetailModal({
  candidateId,
  name,
  description,
  triggers,
  tags,
  guardSummary,
  guardVerdict,
  existingSkill,
  onClose,
}: {
  candidateId: string
  name: string
  description: string
  triggers: string[]
  tags: string[]
  guardSummary?: string
  guardVerdict?: 'safe' | 'caution'
  existingSkill?: import('@shared/events').ExistingSkillStats
  onClose: () => void
}) {
  const [body, setBody] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showDiff, setShowDiff] = useState(false)

  useEffect(() => {
    const api = (window as any).harness
    if (!api?.skillCandidateDiff) {
      setError('skill detail IPC unavailable in this build')
      return
    }
    let cancelled = false
    api
      .skillCandidateDiff(candidateId)
      .then(
        (res: {
          ok: boolean
          candidateBody?: string
          error?: string
        }) => {
          if (cancelled) return
          if (!res?.ok) {
            setError(res?.error ?? 'unknown error')
            return
          }
          setBody(res.candidateBody ?? '')
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

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const isCaution = guardVerdict === 'caution'
  const overwrites = existingSkill?.exists === true
  const isDestructive = overwrites && existingSkill?.isDestructive === true
  const linesAdded = existingSkill?.linesAdded ?? 0
  const linesRemoved = existingSkill?.linesRemoved ?? 0

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flex max-h-[92vh] w-[960px] max-w-[calc(100vw-3rem)] flex-col overflow-hidden rounded-xl glass-strong shadow-2xl ring-hairline-strong"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-3 hairline-b bg-black/70 px-5 py-3 backdrop-blur-md">
          <span className="font-mono text-[14px] text-accent">💡</span>
          <span className="label text-accent">candidate detail</span>
          {isCaution && (
            <span className="label text-warn">⚠ review</span>
          )}
          {overwrites && (
            <span
              className={`rounded px-1.5 py-[1px] font-mono text-[9.5px] uppercase tracking-wider ring-1 ${
                isDestructive
                  ? 'bg-danger/15 text-danger ring-danger/30'
                  : 'bg-accent/15 text-accent ring-accent/30'
              }`}
            >
              ↻ +{linesAdded} / -{linesRemoved}
            </span>
          )}
          <button
            onClick={onClose}
            className="ml-auto rounded-md bg-white/[0.06] px-2 py-[2px] font-mono text-[12px] leading-none text-fg-2 ring-hairline hover:bg-white/[0.12] hover:text-fg-0"
            title="Close (Esc)"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {/* Top metadata block — name, description, triggers, tags, guard report */}
        <div className="hairline-b bg-black/30 px-5 py-4">
          <div className="font-mono text-[14px] text-fg-0">{name}</div>
          <div className="mt-1.5 text-[12px] leading-[1.55] text-fg-1">
            {description}
          </div>
          {(triggers.length > 0 || tags.length > 0) && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {triggers.map((t) => (
                <span
                  key={`t-${t}`}
                  className="rounded-sm bg-accent/[0.08] px-1.5 py-0.5 font-mono text-[10px] text-accent ring-1 ring-accent/20"
                  title="trigger phrase"
                >
                  {t}
                </span>
              ))}
              {tags.map((t) => (
                <span
                  key={`tag-${t}`}
                  className="rounded-sm bg-white/[0.05] px-1.5 py-0.5 font-mono text-[10px] text-fg-2 ring-hairline"
                  title="tag"
                >
                  #{t}
                </span>
              ))}
            </div>
          )}
          {guardSummary && (
            <div className="mt-3">
              <div className="mb-1 label">guard report</div>
              <div
                className={`selectable rounded-md p-2.5 font-mono text-[10.5px] leading-[1.55] ring-hairline whitespace-pre-wrap ${
                  isCaution
                    ? 'border-l-2 border-warn/40 bg-warn/[0.06] text-warn'
                    : 'bg-black/35 text-fg-1'
                }`}
              >
                {guardSummary}
              </div>
            </div>
          )}
          {overwrites && (
            <div
              className={`mt-3 flex items-center gap-3 rounded-md border-l-2 px-3 py-2 text-[11px] ${
                isDestructive
                  ? 'border-danger/60 bg-danger/[0.07] text-danger'
                  : 'border-accent/40 bg-accent/[0.06] text-fg-1'
              }`}
            >
              <span className="font-mono">↻ overwrites existing skill</span>
              <span className="font-mono text-[10.5px]">
                <span className="text-ok">+{linesAdded}</span>
                <span className="text-fg-3"> / </span>
                <span className="text-danger">-{linesRemoved}</span>
                {existingSkill?.linesExisting !== undefined && (
                  <span className="text-fg-3"> of {existingSkill.linesExisting} lines</span>
                )}
              </span>
              <button
                onClick={() => setShowDiff(true)}
                className="ml-auto rounded-md bg-white/[0.05] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.10] hover:text-fg-0"
              >
                view diff
              </button>
            </div>
          )}
        </div>

        {/* Full body — no truncation. This is the rendered SKILL.md that
            confirmation.promote will write on approval. */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <div className="flex items-center justify-between px-5 py-2 label text-fg-3">
            <span>full skill body</span>
            <span className="font-mono text-[9.5px] text-fg-3">
              {body ? `${body.split('\n').length} lines · ${body.length.toLocaleString()} chars` : ''}
            </span>
          </div>
          <div className="flex-1 overflow-auto bg-black/40 px-5 pb-5">
            {error ? (
              <div className="py-3 font-mono text-[11px] text-danger">
                error: {error}
              </div>
            ) : body === null ? (
              <div className="py-3 font-mono text-[11px] text-fg-3">loading…</div>
            ) : (
              <pre className="selectable m-0 whitespace-pre-wrap font-mono text-[11.5px] leading-[1.55] text-fg-1">
                {body}
              </pre>
            )}
          </div>
        </div>
      </div>
      {showDiff && (
        <SkillDiffModal
          candidateId={candidateId}
          name={name}
          onClose={() => setShowDiff(false)}
        />
      )}
    </div>
  )
}
