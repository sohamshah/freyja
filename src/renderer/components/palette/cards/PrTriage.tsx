import React, { useEffect, useMemo, useState } from 'react'
import { useHarness } from '../../../state/store'
import type { PalettePr } from '../../../../shared/events'
import { fetchPrs, openExternal } from '../../../lib/paletteData'
import { Btn, Chip, Frame, Note, Pending, Row, RowAction, relTime, useAsync, type WidgetProps } from '../chrome'

const REPO_ALIASES: Record<string, string> = {
  'agent-harness': 'Ema-Unlimited/agent-harness',
  harness: 'Ema-Unlimited/agent-harness',
  'ema-next': 'Ema-Unlimited/ema-next',
  emu: 'Ema-Unlimited/ema-next',
}

const SCOPES: Array<{ id: 'review-requested' | 'mine' | 'involves'; label: string }> = [
  { id: 'review-requested', label: 'waiting on me' },
  { id: 'mine', label: 'mine' },
  { id: 'involves', label: 'involving me' },
]

function checksTone(c: string) {
  return c === 'failure' ? 'text-danger' : c === 'pending' ? 'text-warn' : c === 'success' ? 'text-ok' : 'text-fg-3'
}

export function PrTriage({ slots, crumbs, onClose, onToast, handoff }: WidgetProps) {
  const sendMessage = useHarness((s) => s.sendMessage)
  const [scopeOverride, setScopeOverride] = useState<typeof SCOPES[number]['id'] | null>(null)
  useEffect(() => setScopeOverride(null), [slots.prScope])
  const scope = scopeOverride ?? slots.prScope ?? 'review-requested'
  const repo = slots.repo ? (REPO_ALIASES[slots.repo.toLowerCase()] ?? (slots.repo.includes('/') ? slots.repo : undefined)) : undefined

  const { data, loading, error } = useAsync(() => fetchPrs(scope, repo), `${scope}|${repo ?? ''}`)
  const prs = useMemo(() => {
    const list = data?.prs ?? []
    const wanted = slots.prNumber ? list.filter((p) => String(p.number) === slots.prNumber) : list
    return wanted.length ? wanted : list
  }, [data, slots.prNumber])
  const [confirm, setConfirm] = useState<PalettePr | null>(null)

  const review = (pr: PalettePr) =>
    handoff({
      type: 'agent_task',
      label: `Review #${pr.number}`,
      slots: {
        profile: 'review',
        prNumber: String(pr.number),
        repo: pr.repo,
        focus: pr.promptFiles.length ? `the prompt and skill text changes (${pr.promptFiles.slice(0, 3).join(', ')}) read line by line, then correctness` : undefined,
      },
      context: { pr },
    })

  const promptCount = prs.filter((p) => p.promptFiles.length).length

  return (
    <Frame
      kicker="Pull requests"
      crumbs={crumbs}
      title={SCOPES.find((s) => s.id === scope)?.label ?? scope}
      badge={loading && !data ? 'gh…' : error ? 'gh failed' : `${prs.length} open${repo ? ` · ${repo.split('/')[1]}` : ''}${data ? ` · ${data.latencyMs}ms` : ''}`}
      footer={
        <>
          <Btn onClick={() => openExternal(scope === 'mine' ? 'https://github.com/pulls' : 'https://github.com/pulls/review-requested')}>Open on GitHub</Btn>
          <Btn
            primary
            disabled={!prs[0]}
            onClick={() => {
              if (prs[0]) openExternal(prs[0].url)
              onClose()
            }}
          >
            {prs[0] ? `Open #${prs[0].number}` : 'Open'}
          </Btn>
        </>
      }
    >
      <div className="flex flex-wrap items-center gap-1">
        {SCOPES.map((s) => (
          <Chip key={s.id} on={s.id === scope} onClick={() => setScopeOverride(s.id)}>
            {s.label}
          </Chip>
        ))}
        {repo && <span className="ml-auto text-[10px] text-fg-3">{repo}</span>}
      </div>
      {error && <Note tone="danger">{error}</Note>}
      {loading && !data && <Pending label="Asking gh…" />}
      {data && promptCount > 0 && (
        <Note tone="warn">
          {promptCount} PR{promptCount === 1 ? ' touches' : 's touch'} prompt or skill text — those get the line-by-line read.
        </Note>
      )}
      {data && (
        <div className="max-h-[280px] space-y-1 overflow-y-auto">
          {prs.map((pr) => (
            <React.Fragment key={`${pr.repo}#${pr.number}`}>
              <Row
                title={
                  <>
                    <span className="text-fg-3">#{pr.number}</span> {pr.title}
                  </>
                }
                flag={
                  <>
                    {pr.isDraft && <span className="kbd text-[8.5px]">draft</span>}
                    {pr.promptFiles.length > 0 && <span className="kbd text-[8.5px] text-warn">prompt</span>}
                    {pr.reviewDecision === 'APPROVED' && <span className="kbd text-[8.5px] text-ok">approved</span>}
                    {pr.reviewDecision === 'CHANGES_REQUESTED' && <span className="kbd text-[8.5px] text-danger">changes</span>}
                  </>
                }
                meta={
                  <>
                    {pr.repo.split('/')[1]} · {pr.author}
                    {pr.additions != null && (
                      <>
                        {' · '}
                        <span className="text-ok">+{pr.additions}</span> <span className="text-danger">−{pr.deletions}</span>
                        {pr.changedFiles != null && ` · ${pr.changedFiles} files`}
                      </>
                    )}
                    {' · '}
                    <span className={checksTone(pr.checks)}>{pr.checks === 'none' ? 'no checks' : `ci ${pr.checks}`}</span>
                    {' · '}
                    {relTime(new Date(pr.updatedAt).getTime())}
                  </>
                }
                onClick={() => openExternal(pr.url)}
                actions={
                  <>
                    <RowAction onClick={() => review(pr)} title="Spawn a review sub-agent (prompt files flagged)">
                      review
                    </RowAction>
                    {!pr.isDraft && scope !== 'mine' && (
                      <RowAction onClick={() => setConfirm(pr)} title="Quick approval pass, then approve if ready">
                        approve
                      </RowAction>
                    )}
                  </>
                }
              />
              {confirm === pr && (
                <div className="flex items-center justify-between gap-2 rounded-md bg-warn/10 px-2.5 py-1.5 text-[10.5px] text-fg-1 ring-1 ring-warn/30">
                  <span>
                    Approve #{pr.number} as you, after a quick sanity pass{pr.promptFiles.length ? ' (prompt files get the deep read first)' : ''}?
                  </span>
                  <span className="flex shrink-0 gap-1">
                    <RowAction onClick={() => setConfirm(null)}>cancel</RowAction>
                    <RowAction
                      onClick={() => {
                        void sendMessage(
                          `Do a quick approval pass on ${pr.url} (${pr.repo} #${pr.number}, "${pr.title}" by ${pr.author}). Check merge state, CI, diff size${
                            pr.promptFiles.length ? `, and read the prompt/skill files line by line: ${pr.promptFiles.join(', ')}` : ''
                          }. If it is ready, post an APPROVE review with gh; otherwise tell me what blocks it.`,
                        )
                        onToast(`Approval pass on #${pr.number}`)
                        setConfirm(null)
                        onClose()
                      }}
                    >
                      approve
                    </RowAction>
                  </span>
                </div>
              )}
            </React.Fragment>
          ))}
          {prs.length === 0 && !loading && <div className="py-3 text-center text-[11px] text-fg-3">Nothing open {SCOPES.find((s) => s.id === scope)?.label}.</div>}
        </div>
      )}
    </Frame>
  )
}
