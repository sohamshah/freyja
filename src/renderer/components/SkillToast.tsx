import { useEffect, useState } from 'react'
import { useHarness } from '../state/store'
import { SkillDiffModal } from './SkillDiffModal'

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
 * Four actions:
 *   - Promote → writes ~/.freyja/skills/<name>/SKILL.md, removes from queue
 *   - Edit    → reveals inline name + description editor; promote with
 *               edits flows through `resolveSkillCandidate(..., edits)`
 *   - Discard → moves to .rejected/ negative library, removes from queue
 *   - View   → expands the toast to show the full body + guard report
 *
 * Caution-verdict candidates get a yellow border and a "REVIEW" badge
 * next to the name; the guard summary is shown un-collapsed above the
 * body so the operator can't miss the reason the guard flagged it.
 */
export function SkillToast() {
  const queueAll = useHarness((s) => s.skillCandidateQueue)
  const dismissed = useHarness((s) => s.dismissedSkillCandidates)
  const dismiss = useHarness((s) => s.dismissSkillToast)
  const resolve = useHarness((s) => s.resolveSkillCandidate)
  const [expanded, setExpanded] = useState(false)
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [showDiff, setShowDiff] = useState(false)
  // Destructive-promote double-tap guard — when the new body deletes
  // a large chunk of the existing skill, the first PROMOTE click only
  // arms the action; the second click executes. Prevents the ~280-line
  // ema-release-ops content loss from happening accidentally.
  const [destructiveArmed, setDestructiveArmed] = useState(false)
  // The visible queue skips candidates the operator has dismissed —
  // they still live in skillCandidateQueue (and the SkillCandidatesPanel
  // pending tab still shows them), they just don't pop up.
  const queue = queueAll.filter((c) => !dismissed.has(c.candidateId))
  const current = queue[0]

  // M15 — reset local expansion / edit state when the head of the
  // queue changes so a fresh candidate doesn't inherit the previous
  // one's "expanded" or stale edit-draft text.
  useEffect(() => {
    setExpanded(false)
    setEditing(false)
    setEditName(current?.name ?? '')
    setEditDesc(current?.description ?? '')
    setDestructiveArmed(false)
    setShowDiff(false)
  }, [current?.candidateId, current?.name, current?.description])

  if (!current) return null

  const triggers = current.triggers ?? []
  const isCaution = current.guardVerdict === 'caution'
  const existing = current.existingSkill
  const overwrites = existing?.exists === true
  const isDestructive = overwrites && existing?.isDestructive === true
  const linesAdded = existing?.linesAdded ?? 0
  const linesRemoved = existing?.linesRemoved ?? 0

  const promote = () => {
    // Destructive-promote double-tap: first click arms, second confirms.
    // Only applies when the candidate would delete ≥100 lines or ≥50%
    // of the existing skill. Operators editing name/description don't
    // trigger this; only body replacement does, since the body is what
    // the drafter substituted.
    if (isDestructive && !destructiveArmed) {
      setDestructiveArmed(true)
      // Auto-disarm after 6s so a stale "armed" state can't sit
      // around waiting for a misclick.
      setTimeout(() => setDestructiveArmed(false), 6000)
      return
    }
    setExpanded(false)
    setEditing(false)
    setDestructiveArmed(false)
    const edits =
      editing && (editName.trim() !== current.name || editDesc.trim() !== current.description)
        ? {
            name: editName.trim() || undefined,
            description: editDesc.trim() || undefined,
          }
        : undefined
    resolve(current.candidateId, 'promote', edits)
  }
  const discard = () => {
    setExpanded(false)
    setEditing(false)
    resolve(current.candidateId, 'discard')
  }
  const cautionToneClasses = isCaution ? 'border-warn/40' : 'border-accent/30'

  return (
    <div className="fixed bottom-4 right-4 z-40 w-[440px] max-w-[calc(100vw-2rem)]">
      <div
        className={`overflow-hidden rounded-xl glass-strong shadow-2xl ring-1 ${cautionToneClasses}`}
      >
        {/* Header — bumped opacity (bg-black/70) so the title bar reads
            cleanly against whatever's behind the toast (the diagnostics
            panel + topo backdrop pre-bleed through the standard
            glass-strong). Close button is a no-op-resolve dismiss:
            keeps the candidate in the queue + panel, just hides this
            popup so the operator can come back to it later. */}
        <div className="relative z-[1] flex items-center gap-2 bg-black/70 px-4 py-3 hairline-b backdrop-blur-md">
          <span className="font-mono text-[14px] text-accent">💡</span>
          <span className="label text-accent">learned a skill</span>
          {isCaution && (
            <span className="label text-warn">⚠ review</span>
          )}
          <div className="ml-auto flex items-center gap-2">
            {queue.length > 1 && (
              <span className="label text-fg-3">
                +{queue.length - 1} more
              </span>
            )}
            <button
              onClick={() => dismiss(current.candidateId)}
              className="rounded-md bg-white/[0.06] px-2 py-[2px] font-mono text-[12px] leading-none text-fg-2 ring-hairline hover:bg-white/[0.12] hover:text-fg-0"
              title="Dismiss this toast — candidate stays in the Skill Candidates panel for later"
              aria-label="Close"
            >
              ×
            </button>
          </div>
        </div>

        {/* Caution summary — shown above the body, not behind a toggle */}
        {isCaution && current.guardSummary && (
          <div className="border-l-2 border-warn/40 bg-warn/[0.05] px-4 py-2 text-[11px] leading-[1.5] text-warn">
            {current.guardSummary}
          </div>
        )}

        {/* Overwrite stats — shown above the body so the operator sees
            "↻ overwrites existing skill (+47 / -287)" before they read
            anything else. Destructive promotes get a red banner; non-
            destructive overwrites get a neutral info row. */}
        {overwrites && (
          <div
            className={`border-l-2 px-4 py-2 text-[11px] leading-[1.5] ${
              isDestructive
                ? 'border-danger/60 bg-danger/[0.07] text-danger'
                : 'border-accent/40 bg-accent/[0.06] text-fg-1'
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="font-mono">↻ overwrites existing</span>
                <span className="font-mono text-[10.5px]">
                  <span className="text-ok">+{linesAdded}</span>
                  <span className="text-fg-3"> / </span>
                  <span className="text-danger">-{linesRemoved}</span>
                  {existing?.linesExisting !== undefined && (
                    <span className="text-fg-3">
                      {' '}
                      of {existing.linesExisting} lines
                    </span>
                  )}
                </span>
              </div>
              <button
                onClick={() => setShowDiff(true)}
                className="font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 hover:text-fg-0 underline-offset-2 hover:underline"
              >
                view diff
              </button>
            </div>
            {isDestructive && (
              <div className="mt-1 text-[10.5px]">
                Promote will delete {linesRemoved} lines from the existing
                skill. Click EDIT to review, or PROMOTE twice to confirm.
              </div>
            )}
          </div>
        )}

        {/* Summary or inline editor */}
        {editing ? (
          <div className="space-y-2 px-4 py-3">
            <div>
              <div className="label mb-1 text-[9px] text-fg-3">skill name</div>
              <input
                type="text"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                spellCheck={false}
                className="w-full rounded-md bg-black/40 px-2 py-1 font-mono text-[12px] text-fg-0 ring-1 ring-white/[0.08] focus:outline-none focus:ring-accent/40"
              />
            </div>
            <div>
              <div className="label mb-1 text-[9px] text-fg-3">description</div>
              <textarea
                value={editDesc}
                onChange={(e) => setEditDesc(e.target.value)}
                spellCheck={false}
                rows={3}
                className="w-full rounded-md bg-black/40 px-2 py-1 text-[12px] leading-[1.45] text-fg-0 ring-1 ring-white/[0.08] focus:outline-none focus:ring-accent/40"
              />
            </div>
            <div className="text-[10.5px] text-fg-3">
              Body stays unchanged. Click "view detail" to inspect before promoting.
            </div>
          </div>
        ) : (
          <div className="px-4 py-3">
            <div className="font-mono text-[12px] text-fg-0">{current.name}</div>
            <div className="mt-1 text-[11.5px] leading-[1.5] text-fg-1">
              {current.description}
            </div>
            {triggers.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {triggers.slice(0, 4).map((t) => (
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
        )}

        {/* Expanded detail */}
        {expanded && (
          <div className="hairline-t px-4 py-3">
            {current.guardSummary && !isCaution && (
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
              onClick={() => {
                if (editing) {
                  // cancel — restore originals
                  setEditName(current.name)
                  setEditDesc(current.description)
                }
                setEditing((v) => !v)
              }}
              className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              {editing ? 'cancel' : 'edit'}
            </button>
            <button
              onClick={promote}
              className={`rounded-md px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] ring-1 ${
                isDestructive
                  ? destructiveArmed
                    ? 'bg-danger/30 text-danger ring-danger/60 hover:bg-danger/40'
                    : 'bg-danger/15 text-danger ring-danger/40 hover:bg-danger/25'
                  : 'bg-accent/15 text-accent ring-accent/40 hover:bg-accent/25'
              }`}
              title={
                isDestructive
                  ? destructiveArmed
                    ? `Click again to confirm — will delete ${linesRemoved} lines`
                    : `Will delete ${linesRemoved} lines · click to arm, click again to confirm`
                  : undefined
              }
            >
              {isDestructive
                ? destructiveArmed
                  ? `confirm · delete -${linesRemoved}`
                  : `replace -${linesRemoved}`
                : editing &&
                    (editName.trim() !== current.name ||
                      editDesc.trim() !== current.description)
                  ? 'promote with edits'
                  : 'promote'}
            </button>
          </div>
        </div>
      </div>
      {showDiff && (
        <SkillDiffModal
          candidateId={current.candidateId}
          name={current.name}
          onClose={() => setShowDiff(false)}
        />
      )}
    </div>
  )
}
