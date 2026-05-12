import { useMemo, useState } from 'react'
import { useHarness } from '../state/store'
import type { FileChangeSet } from '@shared/events'
import { relativeTime } from '../lib/format'
import { ArtifactWorkspace } from './ArtifactWorkspace'
import { FileChangeCard } from './FileChangeCard'
import { StickyHeader } from './StickyHeader'

export function ChangesSection() {
  const changeSets = useHarness((s) => s.fileChanges)
  const focusToolCall = useHarness((s) => s.focusToolCall)
  const [expanded, setExpanded] = useState(true)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)

  const openExternal = (path: string) => {
    const api = (window as any).harness
    if (api?.openExternal) api.openExternal(`file://${path}`)
  }

  const ordered = useMemo(
    () => [...changeSets].sort((a, b) => b.createdAt - a.createdAt),
    [changeSets],
  )

  const totals = useMemo(
    () =>
      changeSets.reduce(
        (acc, set) => ({
          files: acc.files + set.totals.files,
          additions: acc.additions + set.totals.additions,
          deletions: acc.deletions + set.totals.deletions,
        }),
        { files: 0, additions: 0, deletions: 0 },
      ),
    [changeSets],
  )

  return (
    <div className="hairline-b">
      {workspaceOpen && (
        <ArtifactWorkspace
          initialView="changes"
          onClose={() => setWorkspaceOpen(false)}
        />
      )}
      <StickyHeader>
        <div className="flex w-full items-baseline justify-between gap-2 px-4 py-2">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex items-baseline gap-2 text-left"
          >
            <div className="label">changes</div>
            <span className="font-mono text-[10px] text-fg-3">{changeSets.length}</span>
            {changeSets.length > 0 && (
              <span className="font-mono text-[9px] text-fg-3">
                {totals.files} files · <span className="text-ok">+{totals.additions}</span>{' '}
                <span className="text-danger">-{totals.deletions}</span>
              </span>
            )}
            <span className="text-[9px] text-fg-3">{expanded ? '▾' : '▸'}</span>
          </button>
          {changeSets.length > 0 && (
            <button
              onClick={() => setWorkspaceOpen(true)}
              className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              diff view ↗
            </button>
          )}
        </div>
      </StickyHeader>

      {!expanded ? null : ordered.length === 0 ? (
        <div className="px-4 pb-3 pt-1 text-[11px] italic text-fg-3">No file changes yet</div>
      ) : (
        <div className="space-y-2 px-4 pb-3 pt-1">
          {ordered.slice(0, 8).map((changeSet) => (
            <ChangeSetRow
              key={changeSet.id}
              changeSet={changeSet}
              onJump={() => focusToolCall(changeSet.toolCallId)}
              onOpenFile={openExternal}
            />
          ))}
          {ordered.length > 8 && (
            <button
              onClick={() => setWorkspaceOpen(true)}
              className="w-full rounded-md bg-white/[0.025] px-2 py-1.5 text-left font-mono text-[9.5px] text-fg-3 ring-hairline hover:bg-white/[0.05] hover:text-fg-1"
            >
              {ordered.length - 8} more change set{ordered.length - 8 === 1 ? '' : 's'} in diff view
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function ChangeSetRow({
  changeSet,
  onJump,
  onOpenFile,
}: {
  changeSet: FileChangeSet
  onJump: () => void
  onOpenFile: (path: string) => void
}) {
  const [open, setOpen] = useState(false)
  const firstFile = changeSet.files[0]
  const extra = Math.max(0, changeSet.files.length - 1)

  return (
    <div className="overflow-hidden rounded-md bg-white/[0.025] ring-hairline">
      <div className="flex items-start gap-2 px-2 py-1.5">
        <button
          onClick={() => setOpen((v) => !v)}
          className="min-w-0 flex-1 text-left"
        >
          <div className="flex items-center gap-1.5">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${
              changeSet.source === 'bash' ? 'bg-warn' : 'bg-ok'
            }`} />
            <span className="truncate font-mono text-[10px] text-fg-0">
              {firstFile?.filename ?? changeSet.toolName}
            </span>
            {extra > 0 && (
              <span className="font-mono text-[9px] text-fg-3">+{extra}</span>
            )}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 font-mono text-[8.5px] text-fg-3">
            <span>{changeSet.toolName}</span>
            <span>·</span>
            <span>{relativeTime(changeSet.createdAt)}</span>
            <span>·</span>
            <span className="text-ok">+{changeSet.totals.additions}</span>
            <span className="text-danger">-{changeSet.totals.deletions}</span>
          </div>
        </button>
        <button
          onClick={onJump}
          className="shrink-0 rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[8.5px] uppercase tracking-[0.08em] text-fg-3 ring-hairline hover:bg-accent/10 hover:text-accent hover:ring-accent/30"
        >
          jump
        </button>
      </div>
      {open && (
        <div className="border-t border-white/[0.04] p-1.5">
          <FileChangeCard
            changeSet={changeSet}
            onJumpToTool={() => onJump()}
            onOpenFile={onOpenFile}
            title="diff"
          />
        </div>
      )}
    </div>
  )
}
