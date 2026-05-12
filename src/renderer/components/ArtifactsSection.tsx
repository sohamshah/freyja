import { useMemo, useState } from 'react'
import { useHarness } from '../state/store'
import type { ArtifactRecord } from '@shared/events'
import { relativeTime } from '../lib/format'
import { ArtifactWorkspace } from './ArtifactWorkspace'
import { StickyHeader } from './StickyHeader'

/**
 * File type to icon/color mapping for the artifact list.
 */
const TYPE_META: Record<string, { icon: string; color: string }> = {
  html: { icon: '◇', color: 'text-accent' },
  md:   { icon: '◆', color: 'text-fg-1' },
  json: { icon: '{}', color: 'text-warn' },
  py:   { icon: 'λ', color: 'text-ok' },
  ts:   { icon: 'τ', color: 'text-accent' },
  tsx:  { icon: 'τ', color: 'text-accent' },
  js:   { icon: 'ƒ', color: 'text-warn' },
  css:  { icon: '#', color: 'text-ok' },
  txt:  { icon: '≡', color: 'text-fg-2' },
  svg:  { icon: '◎', color: 'text-accent' },
  sh:   { icon: '$', color: 'text-ok' },
  yaml: { icon: '⊞', color: 'text-warn' },
  yml:  { icon: '⊞', color: 'text-warn' },
  toml: { icon: '⊞', color: 'text-warn' },
}

function getTypeMeta(ext: string) {
  return TYPE_META[ext] ?? { icon: '·', color: 'text-fg-3' }
}

/**
 * Artifacts section for the ActivityPanel sidebar.
 * Groups artifacts by creator (parent vs each subagent).
 */
export function ArtifactsSection() {
  const artifacts = useHarness((s) => s.artifacts)
  const [expanded, setExpanded] = useState(true)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)

  // Group by creator
  const groups = useMemo(() => {
    const map = new Map<string, { label: string; items: ArtifactRecord[] }>()
    for (const a of artifacts) {
      if (!map.has(a.creator)) {
        map.set(a.creator, { label: a.creatorLabel, items: [] })
      }
      map.get(a.creator)!.items.push(a)
    }
    return Array.from(map.entries()).map(([id, g]) => ({
      creatorId: id,
      creatorLabel: g.label,
      items: g.items,
    }))
  }, [artifacts])

  const openFile = (path: string) => {
    const api = (window as any).harness
    if (api?.openExternal) {
      // For HTML files, open in browser; for others, use default app
      if (path.endsWith('.html') || path.endsWith('.htm')) {
        api.openExternal(`file://${path}`)
      } else {
        api.openExternal(`file://${path}`)
      }
    }
  }

  return (
    <div className="hairline-b">
      {workspaceOpen && <ArtifactWorkspace onClose={() => setWorkspaceOpen(false)} />}
      <StickyHeader>
        <div className="flex w-full items-baseline justify-between gap-2 px-4 py-2">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex items-baseline gap-2 text-left"
          >
            <div className="label">artifacts</div>
            <span className="font-mono text-[10px] text-fg-3">{artifacts.length}</span>
            <span className="text-[9px] text-fg-3">{expanded ? '▾' : '▸'}</span>
          </button>
          {artifacts.length > 0 && (
            <button
              onClick={() => setWorkspaceOpen(true)}
              className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              workspace ↗
            </button>
          )}
        </div>
      </StickyHeader>

      {!expanded ? null : artifacts.length === 0 ? (
        <div className="px-4 pb-3 pt-1 text-[11px] italic text-fg-3">No file changes yet</div>
      ) : (
        <div className="space-y-3 px-4 pb-3 pt-1">
          {groups.map((group) => (
            <div key={group.creatorId}>
              {/* Creator header */}
              <div className="mb-1.5 flex items-center gap-1.5">
                <span className={`block h-1 w-1 rounded-full ${
                  group.creatorId === 'parent' ? 'bg-accent' : 'bg-ok'
                }`} />
                <span className="font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
                  {group.creatorLabel}
                </span>
                <span className="font-mono text-[9px] text-fg-3">
                  ({group.items.length})
                </span>
              </div>

              {/* File list */}
              <div className="space-y-1">
                {group.items.map((artifact) => (
                  <ArtifactRow
                    key={artifact.id}
                    artifact={artifact}
                    onOpen={() => openFile(artifact.path)}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function ArtifactRow({
  artifact,
  onOpen,
}: {
  artifact: ArtifactRecord
  onOpen: () => void
}) {
  const meta = getTypeMeta(artifact.fileType)
  const isSubagent = artifact.operation === 'subagent_artifact'

  return (
    <button
      onClick={onOpen}
      title={`Open ${artifact.path}`}
      className="group flex w-full items-center gap-2 rounded-md bg-white/[0.02] px-2 py-1.5 text-left ring-hairline transition-colors hover:bg-white/[0.06] hover:ring-accent/30"
    >
      {/* Type icon */}
      <span className={`shrink-0 font-mono text-[10px] font-bold ${meta.color}`}>
        {meta.icon}
      </span>

      {/* Filename + path hint */}
      <div className="min-w-0 flex-1">
        <div className="truncate font-mono text-[10.5px] text-fg-0">
          {artifact.filename}
        </div>
        <div className="truncate font-mono text-[8.5px] text-fg-3">
          {shortenPath(artifact.path)}
        </div>
      </div>

      {/* Badges */}
      <div className="flex shrink-0 items-center gap-1.5">
        {(artifact.additions != null || artifact.deletions != null) && (
          <span className="rounded-full bg-white/[0.04] px-1.5 py-[1px] font-mono text-[7.5px] uppercase text-fg-2 ring-hairline">
            <span className="text-ok">+{artifact.additions ?? 0}</span>{' '}
            <span className="text-danger">-{artifact.deletions ?? 0}</span>
          </span>
        )}
        {isSubagent && (
          <span className="rounded-full bg-ok/15 px-1.5 py-[1px] font-mono text-[7.5px] font-bold uppercase text-ok ring-1 ring-ok/20">
            agent
          </span>
        )}
        <span className="rounded-full bg-white/[0.04] px-1.5 py-[1px] font-mono text-[7.5px] uppercase text-fg-3">
          .{artifact.fileType || '?'}
        </span>
      </div>

      {/* Open icon on hover */}
      <span className="shrink-0 text-[10px] text-fg-3 opacity-0 group-hover:opacity-100">
        ↗
      </span>
    </button>
  )
}

/**
 * Shorten a full path to just the last 2-3 segments for display.
 */
function shortenPath(path: string): string {
  const parts = path.split('/')
  if (parts.length <= 3) return path
  return '…/' + parts.slice(-3).join('/')
}
