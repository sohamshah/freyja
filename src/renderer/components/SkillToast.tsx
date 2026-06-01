import { useState } from 'react'
import { useHarness } from '../state/store'

/**
 * Non-modal toast surfaced at the bottom-right of the screen when a
 * drafter candidate is awaiting operator review. Renders the head of
 * ``skillCandidateQueue``; tail entries get a "+N more" indicator.
 *
 * Unlike PermissionPrompt this is deliberately non-blocking — the
 * operator can keep working and decide on the candidate when they
 * notice it. Esc collapses the expanded body but keeps the candidate
 * in the queue (no destructive default).
 *
 * Three actions:
 *   - Promote → writes ~/.freyja/skills/<name>/SKILL.md, removes from queue
 *   - Discard → moves to .rejected/ negative library, removes from queue
 *   - View   → expands the toast to show the full body + guard report
 */
export function SkillToast() {
  const queue = useHarness((s) => s.skillCandidateQueue)
  const resolve = useHarness((s) => s.resolveSkillCandidate)
  const [expanded, setExpanded] = useState(false)
  const current = queue[0]

  if (!current) return null

  const promote = () => {
    setExpanded(false)
    resolve(current.candidateId, 'promote')
  }
  const discard = () => {
    setExpanded(false)
    resolve(current.candidateId, 'discard')
  }
  const cautionToneClasses =
    current.guardVerdict === 'caution'
      ? 'border-warn/40'
      : 'border-accent/30'

  return (
    <div className="fixed bottom-4 right-4 z-40 w-[440px] max-w-[calc(100vw-2rem)]">
      <div
        className={`overflow-hidden rounded-xl glass-strong shadow-2xl ring-1 ${cautionToneClasses}`}
      >
        {/* Header */}
        <div className="flex items-center gap-2 px-4 py-3 hairline-b">
          <span className="font-mono text-[14px] text-accent">💡</span>
          <span className="label text-accent">learned a skill</span>
          {current.guardVerdict === 'caution' && (
            <span className="label text-warn">⚠ caution</span>
          )}
          {queue.length > 1 && (
            <span className="label ml-auto text-fg-3">
              +{queue.length - 1} more
            </span>
          )}
        </div>

        {/* Summary */}
        <div className="px-4 py-3">
          <div className="font-mono text-[12px] text-fg-0">{current.name}</div>
          <div className="mt-1 text-[11.5px] leading-[1.5] text-fg-1">
            {current.description}
          </div>
          {current.triggers.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {current.triggers.slice(0, 4).map((t) => (
                <span
                  key={t}
                  className="rounded-sm bg-white/[0.04] px-1.5 py-0.5 font-mono text-[10px] text-fg-2 ring-hairline"
                >
                  {t}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Expanded detail */}
        {expanded && (
          <div className="hairline-t px-4 py-3">
            {current.guardSummary && (
              <div className="mb-3">
                <div className="mb-1 label">guard report</div>
                <div className="selectable rounded-md bg-black/35 p-2 font-mono text-[10.5px] leading-[1.5] text-fg-1 ring-hairline whitespace-pre-wrap">
                  {current.guardSummary}
                </div>
              </div>
            )}
            <div className="label mb-1">body preview</div>
            <div className="selectable max-h-[280px] overflow-y-auto rounded-md bg-black/30 p-2.5 font-mono text-[11px] leading-[1.55] text-fg-1 ring-hairline whitespace-pre-wrap">
              {current.bodyPreview}
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center justify-between gap-2 hairline-t px-4 py-2.5">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-fg-3 hover:text-fg-1"
          >
            {expanded ? 'collapse' : 'view detail'}
          </button>
          <div className="flex items-center gap-2">
            <button
              onClick={discard}
              className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              discard
            </button>
            <button
              onClick={promote}
              className="rounded-md bg-accent/15 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/40 hover:bg-accent/25"
            >
              promote
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
