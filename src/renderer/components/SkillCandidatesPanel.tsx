import { useEffect, useMemo, useState } from 'react'
import { activeSessionScope, useHarness } from '../state/store'
import { SkillDetailModal } from './SkillDetailModal'

type Tab = 'pending' | 'learned' | 'rejected'

/**
 * Skill-candidate queue + negative-library viewer.
 *
 * The live queue (``skillCandidateQueue``) drives the "pending" tab in
 * sync with bridge events. The "rejected" tab hydrates from
 * ``~/.freyja/skills/.rejected/`` via the ``skill:listRejected`` IPC so
 * the operator can audit guard-blocked drafts and operator discards.
 *
 * Each pending row supports promote / edit / discard inline; edits flow
 * through ``resolveSkillCandidate(..., edits={name, description, body})``
 * which the bridge applies before the Skills Guard rescan in
 * ``confirmation.promote``.
 */
export function SkillCandidatesPanel() {
  const rawQueue = useHarness((s) => s.skillCandidateQueue)
  const rawCachedPending = useHarness((s) => s.skillCandidatesCache)
  const rawRejected = useHarness((s) => s.skillRejectedCache)
  const rawPromoted = useHarness((s) => s.skillPromotedCache)
  const refreshPending = useHarness((s) => s.refreshSkillCandidates)
  const refreshRejected = useHarness((s) => s.refreshRejectedSkills)
  const refreshPromoted = useHarness((s) => s.refreshPromotedSkills)
  const resolve = useHarness((s) => s.resolveSkillCandidate)
  const openSkillFile = useHarness((s) => s.openSkillFile)
  // Activity panel is strictly session-scoped: show candidates / runs
  // produced by THIS session and its sub-agents only. The IPC layer
  // ships every session's records (the candidates / rejected / events
  // files are global on disk); we trim to scope here at the render
  // boundary so a long-running install doesn't drown the panel in
  // unrelated history.
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const sessions = useHarness((s) => s.sessions)
  const scope = useMemo(
    () => activeSessionScope(activeSessionId, sessions),
    [activeSessionId, sessions],
  )
  const inScope = (sid?: string | null): boolean => {
    // No active session → show nothing (panel is for the current
    // session). A record with no sourceSessionId likely predates the
    // field; treat it as out-of-scope rather than leaking it in.
    if (!scope) return false
    if (!sid) return false
    return scope.has(sid)
  }
  const queue = useMemo(
    () => rawQueue.filter((q) => inScope(q.sessionId)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rawQueue, scope],
  )
  const cachedPending = useMemo(
    () =>
      rawCachedPending == null
        ? null
        : rawCachedPending.filter((c) => inScope(c.sourceSessionId)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rawCachedPending, scope],
  )
  const rejected = useMemo(
    () =>
      rawRejected == null
        ? null
        : rawRejected.filter((r) => inScope(r.sourceSessionId)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rawRejected, scope],
  )
  const promoted = useMemo(
    () =>
      rawPromoted == null
        ? null
        : rawPromoted.filter((p) => inScope(p.sourceSessionId)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rawPromoted, scope],
  )
  const [tab, setTab] = useState<Tab>('pending')
  const [openId, setOpenId] = useState<string | null>(null)
  const [editId, setEditId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editBody, setEditBody] = useState('')
  // Active candidate for the centered detail modal. Distinct from
  // openId (which controls inline expand-in-row for a quick peek) —
  // operator can have one row inline-expanded AND open the full
  // modal for a different one. Esc / click-outside / × dismiss.
  const [detailCandidateId, setDetailCandidateId] = useState<string | null>(null)

  // First-mount load — also refresh whenever the user flips tabs so
  // we don't show stale data after a candidate was promoted elsewhere.
  useEffect(() => {
    if (tab === 'pending') void refreshPending()
    if (tab === 'rejected') void refreshRejected(100)
    if (tab === 'learned') void refreshPromoted(100)
  }, [tab, refreshPending, refreshRejected, refreshPromoted])

  // Merge the live queue with the on-disk cache so a candidate that
  // came in via skill_candidate event and one already on disk are
  // shown together without duplicates. Live entries win on conflict.
  const liveById = new Map(queue.map((q) => [q.candidateId, q]))
  const pending = [
    ...queue,
    ...(cachedPending ?? []).filter((c) => !liveById.has(c.candidateId)),
  ]

  const startEdit = (cand: {
    candidateId: string
    name: string
    description: string
    body?: string
    bodyPreview?: string
  }) => {
    setEditId(cand.candidateId)
    setEditName(cand.name)
    setEditDesc(cand.description)
    setEditBody(cand.body ?? cand.bodyPreview ?? '')
  }

  const submitEdit = (candidateId: string, action: 'promote' | 'discard') => {
    const edits =
      action === 'promote'
        ? {
            name: editName.trim() || undefined,
            description: editDesc.trim() || undefined,
            body: editBody || undefined,
          }
        : undefined
    // Look up the candidate to see if the promote is intentionally
    // overwriting an existing skill. The +X/-Y badge means yes —
    // pass overwrite=true so the backend doesn't refuse name_collision.
    // After an edit the name might have changed; if the new name no
    // longer collides we still pass true (the backend just ignores it).
    const cand = pending.find((c) => c.candidateId === candidateId)
    const overwrite =
      action === 'promote' && Boolean(cand?.existingSkill?.exists)
    void resolve(candidateId, action, edits, overwrite)
    setEditId(null)
  }

  // Tabs row sits at the top of the section (no duplicate label —
  // the outer SkillCandidatesPanelContainer header already names the
  // section). Body is the only scroll container; it has a real
  // max-height so the inner list can scroll even when the outer
  // ActivityPanel layout doesn't give us flex-1 height.
  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 px-3 py-2 hairline-b">
        <div className="flex items-center gap-1">
          <TabButton active={tab === 'pending'} onClick={() => setTab('pending')}>
            pending · {pending.length}
          </TabButton>
          <TabButton active={tab === 'learned'} onClick={() => setTab('learned')}>
            learned · {promoted?.length ?? 0}
          </TabButton>
          <TabButton active={tab === 'rejected'} onClick={() => setTab('rejected')}>
            rejected · {rejected?.length ?? 0}
          </TabButton>
        </div>
        <button
          type="button"
          onClick={() =>
            tab === 'pending'
              ? refreshPending()
              : tab === 'learned'
                ? refreshPromoted(100)
                : refreshRejected(100)
          }
          className="ml-auto font-mono text-[10px] uppercase tracking-[0.08em] text-fg-3 hover:text-fg-1"
        >
          refresh
        </button>
      </div>

      <div className="max-h-[420px] overflow-y-auto px-3 py-2">
        {tab === 'pending' && pending.length === 0 && (
          <div className="rounded-md bg-white/[0.025] px-3 py-2 text-[11px] text-fg-3 ring-hairline">
            No pending skill candidates. The drafter writes here when a
            session produces a save-worthy generalization.
          </div>
        )}
        {tab === 'pending' &&
          pending.map((cand) => {
            const isOpen = openId === cand.candidateId
            const isEditing = editId === cand.candidateId
            const isCaution = cand.guardVerdict === 'caution'
            return (
              <div
                key={cand.candidateId}
                className={`mb-2 rounded-md ring-1 ${
                  isCaution ? 'ring-warn/30' : 'ring-white/10'
                } bg-white/[0.025] px-3 py-2`}
              >
                {/* Title block — full width. Click toggles the body
                    preview. Badges + name flex-wrap so a long name +
                    overwrite stat don't push the layout. */}
                <button
                  type="button"
                  onClick={() => setOpenId(isOpen ? null : cand.candidateId)}
                  className="block w-full text-left"
                >
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-mono text-[12px] text-fg-0 break-all">
                      {cand.name}
                    </span>
                    {isCaution && (
                      <span className="rounded bg-warn/15 px-1 py-[1px] font-mono text-[8.5px] uppercase tracking-[0.08em] text-warn ring-1 ring-warn/30">
                        review
                      </span>
                    )}
                    {cand.existingSkill?.exists && (
                      <span
                        className={`rounded px-1 py-[1px] font-mono text-[8.5px] uppercase tracking-[0.08em] ring-1 ${
                          cand.existingSkill.isDestructive
                            ? 'bg-danger/15 text-danger ring-danger/30'
                            : 'bg-accent/15 text-accent ring-accent/30'
                        }`}
                        title={`Overwrites existing skill: +${cand.existingSkill.linesAdded ?? 0} / -${cand.existingSkill.linesRemoved ?? 0} lines${cand.existingSkill.linesExisting ? ` of ${cand.existingSkill.linesExisting}` : ''}`}
                      >
                        ↻ +{cand.existingSkill.linesAdded ?? 0} / -{cand.existingSkill.linesRemoved ?? 0}
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 text-[11px] leading-[1.4] text-fg-2 line-clamp-2">
                    {cand.description}
                  </div>
                </button>
                {/* Action row — own line below the title so PROMOTE
                    can't get clipped when the operator drags the
                    activity panel narrower. flex-wrap so even an
                    extra-narrow panel (e.g. 220px) wraps the buttons
                    onto multiple rows instead of overflowing. */}
                <div className="mt-2 flex flex-wrap items-center justify-end gap-1">
                  <button
                    type="button"
                    onClick={() => setDetailCandidateId(cand.candidateId)}
                    className="rounded-md bg-white/[0.04] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:text-fg-0"
                    title="Open the full skill body in a centered modal — no truncation"
                  >
                    view
                  </button>
                  <button
                    type="button"
                    onClick={() => (isEditing ? setEditId(null) : startEdit(cand))}
                    className="rounded-md bg-white/[0.04] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:text-fg-0"
                  >
                    {isEditing ? 'cancel' : 'edit'}
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      isEditing
                        ? submitEdit(cand.candidateId, 'discard')
                        : resolve(cand.candidateId, 'discard')
                    }
                    className="rounded-md bg-white/[0.04] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-danger/15 hover:text-danger"
                  >
                    discard
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      isEditing
                        ? submitEdit(cand.candidateId, 'promote')
                        : resolve(
                            cand.candidateId,
                            'promote',
                            undefined,
                            Boolean(cand.existingSkill?.exists),
                          )
                    }
                    className="rounded-md bg-accent/15 px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30 hover:bg-accent/25"
                  >
                    {isEditing
                      ? 'promote with edits'
                      : cand.existingSkill?.exists
                        ? 'overwrite'
                        : 'promote'}
                  </button>
                </div>
                {isCaution && cand.guardSummary && (
                  <div className="mt-2 rounded border-l-2 border-warn/40 bg-warn/[0.06] px-2 py-1 text-[10.5px] leading-[1.45] text-warn">
                    {cand.guardSummary}
                  </div>
                )}
                {isEditing && (
                  <div className="mt-2 space-y-2">
                    <div>
                      <div className="label mb-1 text-[9px] text-fg-3">name</div>
                      <input
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
                        rows={2}
                        className="w-full rounded-md bg-black/40 px-2 py-1 text-[12px] leading-[1.45] text-fg-0 ring-1 ring-white/[0.08] focus:outline-none focus:ring-accent/40"
                      />
                    </div>
                    <div>
                      <div className="label mb-1 text-[9px] text-fg-3">body</div>
                      <textarea
                        value={editBody}
                        onChange={(e) => setEditBody(e.target.value)}
                        spellCheck={false}
                        rows={10}
                        className="w-full rounded-md bg-black/40 px-2 py-1 font-mono text-[11px] leading-[1.55] text-fg-0 ring-1 ring-white/[0.08] focus:outline-none focus:ring-accent/40"
                      />
                    </div>
                    <div className="text-[10.5px] text-fg-3">
                      Body edits are re-scanned by the Skills Guard before write.
                    </div>
                  </div>
                )}
                {isOpen && !isEditing && (
                  <div className="mt-2 max-h-[280px] overflow-y-auto rounded-md bg-black/30 p-2 font-mono text-[11px] leading-[1.55] text-fg-1 ring-hairline whitespace-pre-wrap">
                    {(cand as any).body || cand.bodyPreview}
                  </div>
                )}
              </div>
            )
          })}

        {tab === 'learned' && (!promoted || promoted.length === 0) && (
          <div className="rounded-md bg-white/[0.025] px-3 py-2 text-[11px] text-fg-3 ring-hairline">
            No promoted skills yet. Once a candidate is promoted it
            moves here and is written to ~/.freyja/skills/&lt;name&gt;/SKILL.md.
          </div>
        )}
        {tab === 'learned' &&
          (promoted ?? []).map((rec) => {
            const isOpen = openId === rec.candidateId
            return (
              <div
                key={`${rec.candidateId || rec.name}-${rec.promotedAt}`}
                className="mb-2 rounded-md ring-1 ring-accent/15 bg-accent/[0.04] px-3 py-2"
              >
                <button
                  type="button"
                  onClick={() => setOpenId(isOpen ? null : rec.candidateId || rec.name)}
                  className="w-full text-left"
                >
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-mono text-[12px] text-fg-0 break-all">
                      {rec.name}
                    </span>
                    <span className="rounded bg-accent/15 px-1 py-[1px] font-mono text-[8.5px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30">
                      learned
                    </span>
                    <span className="ml-auto font-mono text-[10px] text-fg-3">
                      {rec.promotedAt
                        ? new Date(rec.promotedAt).toLocaleString()
                        : ''}
                    </span>
                  </div>
                  {rec.description && (
                    <div className="mt-0.5 text-[11px] leading-[1.4] text-fg-2 line-clamp-2">
                      {rec.description}
                    </div>
                  )}
                </button>
                <div className="mt-2 flex flex-wrap items-center justify-end gap-1">
                  <button
                    type="button"
                    onClick={() => void openSkillFile(rec.name)}
                    className="rounded-md bg-white/[0.04] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:text-fg-0"
                    title={rec.skillPath || 'Open SKILL.md in your editor'}
                  >
                    open file
                  </button>
                </div>
                {isOpen && (rec.body || rec.bodyPreview) && (
                  <div className="mt-2 max-h-[280px] overflow-y-auto rounded-md bg-black/30 p-2 font-mono text-[11px] leading-[1.55] text-fg-1 ring-hairline whitespace-pre-wrap">
                    {rec.body || rec.bodyPreview}
                  </div>
                )}
              </div>
            )
          })}

        {tab === 'rejected' && (!rejected || rejected.length === 0) && (
          <div className="rounded-md bg-white/[0.025] px-3 py-2 text-[11px] text-fg-3 ring-hairline">
            No rejected candidates yet. Skills Guard or operator discards land here.
          </div>
        )}
        {tab === 'rejected' &&
          (rejected ?? []).map((rec) => {
            const isOpen = openId === rec.candidateId
            const isDanger = rec.guardVerdict === 'dangerous'
            return (
              <div
                key={rec.candidateId}
                className={`mb-2 rounded-md ring-1 ${
                  isDanger ? 'ring-danger/30' : 'ring-white/10'
                } bg-white/[0.025] px-3 py-2`}
              >
                <button
                  type="button"
                  onClick={() => setOpenId(isOpen ? null : rec.candidateId)}
                  className="w-full text-left"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[12px] text-fg-0">
                      {rec.name || '(unnamed)'}
                    </span>
                    {isDanger && (
                      <span className="rounded bg-danger/15 px-1 py-[1px] font-mono text-[8.5px] uppercase tracking-[0.08em] text-danger ring-1 ring-danger/30">
                        dangerous
                      </span>
                    )}
                    <span className="ml-auto font-mono text-[10px] text-fg-3">
                      {rec.actor || '—'}
                    </span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-[10.5px] text-fg-2">
                    <span className="uppercase">{rec.reason || 'unknown'}</span>
                    {rec.rejectedAt ? (
                      <span className="text-fg-3">
                        {new Date(rec.rejectedAt).toLocaleString()}
                      </span>
                    ) : null}
                  </div>
                  {rec.description && (
                    <div className="mt-1 text-[11px] leading-[1.4] text-fg-2 line-clamp-2">
                      {rec.description}
                    </div>
                  )}
                </button>
                {rec.guardSummary && (
                  <div className="mt-2 rounded border-l-2 border-warn/40 bg-warn/[0.04] px-2 py-1 text-[10.5px] leading-[1.45] text-warn">
                    {rec.guardSummary}
                  </div>
                )}
                {isOpen && (
                  <div className="mt-2 max-h-[280px] overflow-y-auto rounded-md bg-black/30 p-2 font-mono text-[11px] leading-[1.55] text-fg-1 ring-hairline whitespace-pre-wrap">
                    {rec.body || rec.bodyPreview}
                  </div>
                )}
              </div>
            )
          })}
      </div>
      {detailCandidateId && (() => {
        const cand =
          pending.find((c) => c.candidateId === detailCandidateId) ??
          (rejected ?? []).find((c) => c.candidateId === detailCandidateId)
        if (!cand) return null
        return (
          <SkillDetailModal
            candidateId={cand.candidateId}
            name={cand.name}
            description={cand.description}
            triggers={cand.triggers ?? []}
            tags={cand.tags ?? []}
            guardSummary={cand.guardSummary}
            guardVerdict={
              cand.guardVerdict === 'caution' ? 'caution' : 'safe'
            }
            existingSkill={
              (cand as { existingSkill?: import('@shared/events').ExistingSkillStats })
                .existingSkill
            }
            onClose={() => setDetailCandidateId(null)}
          />
        )
      })()}
    </div>
  )
}

function TabButton({
  active,
  children,
  onClick,
}: {
  active: boolean
  children: React.ReactNode
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] ring-1 ${
        active
          ? 'bg-accent/15 text-accent ring-accent/30'
          : 'bg-white/[0.04] text-fg-2 ring-white/10 hover:text-fg-0'
      }`}
    >
      {children}
    </button>
  )
}
