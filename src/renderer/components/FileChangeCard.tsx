import { useMemo, useState } from 'react'
import type { FileChangeRecord, FileChangeSet } from '@shared/events'

const MAX_RENDERED_DIFF_LINES = 520

type FileChangeCardProps = {
  changeSet: FileChangeSet
  onJumpToTool?: (toolCallId: string) => void
  onOpenFile?: (path: string) => void
  title?: string
}

export function FileChangeBadge({ changeSet }: { changeSet?: FileChangeSet }) {
  if (!changeSet || changeSet.files.length === 0) return null
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-ok/10 px-1.5 py-[1px] font-mono text-[9px] uppercase tracking-[0.06em] text-ok ring-1 ring-ok/20">
      <span>{changeSet.totals.files} file{changeSet.totals.files === 1 ? '' : 's'}</span>
      <span className="text-ok/70">+{changeSet.totals.additions}</span>
      <span className="text-danger/80">-{changeSet.totals.deletions}</span>
    </span>
  )
}

export function FileChangeCard({
  changeSet,
  onJumpToTool,
  onOpenFile,
  title = 'files changed',
}: FileChangeCardProps) {
  const [openFiles, setOpenFiles] = useState<Set<string>>(() => {
    const first = changeSet.files[0]?.path
    return first ? new Set([first]) : new Set()
  })

  const toggleFile = (path: string) => {
    setOpenFiles((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  return (
    <div className="space-y-2 rounded-md bg-black/25 p-2 ring-1 ring-white/[0.06]">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-fg-2">
          {title}
        </span>
        <FileChangeBadge changeSet={changeSet} />
        {changeSet.source === 'bash' && (
          <span className="rounded-full bg-white/[0.04] px-1.5 py-[1px] font-mono text-[8.5px] uppercase text-fg-3 ring-hairline">
            shell
          </span>
        )}
        {changeSet.truncated && (
          <span className="rounded-full bg-warn/10 px-1.5 py-[1px] font-mono text-[8.5px] uppercase text-warn ring-1 ring-warn/20">
            partial
          </span>
        )}
        {onJumpToTool && (
          <button
            onClick={() => onJumpToTool(changeSet.toolCallId)}
            className="ml-auto rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-accent/10 hover:text-accent hover:ring-accent/30"
          >
            jump
          </button>
        )}
      </div>

      <div className="space-y-1.5">
        {changeSet.files.map((file) => (
          <FileChangeRow
            key={file.path}
            file={file}
            open={openFiles.has(file.path)}
            onToggle={() => toggleFile(file.path)}
            onOpenFile={onOpenFile}
          />
        ))}
      </div>
    </div>
  )
}

function FileChangeRow({
  file,
  open,
  onToggle,
  onOpenFile,
}: {
  file: FileChangeRecord
  open: boolean
  onToggle: () => void
  onOpenFile?: (path: string) => void
}) {
  const diffLines = useMemo(() => {
    const lines = (file.diff ?? '').split('\n')
    return lines.length > MAX_RENDERED_DIFF_LINES
      ? lines.slice(0, MAX_RENDERED_DIFF_LINES)
      : lines
  }, [file.diff])

  const isDiffViewTruncated =
    Boolean(file.diff) && (file.diff?.split('\n').length ?? 0) > MAX_RENDERED_DIFF_LINES

  return (
    <div className="overflow-hidden rounded-md bg-white/[0.025] ring-hairline">
      <div className="flex items-center gap-2 px-2 py-1.5 hover:bg-white/[0.03]">
        <button
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <OperationBadge operation={file.operation} />
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-[10.5px] text-fg-0">
              {file.filename}
            </div>
            <div className="truncate font-mono text-[8.5px] text-fg-3">
              {file.path}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5 font-mono text-[9.5px]">
            <span className="text-ok">+{file.additions}</span>
            <span className="text-danger">-{file.deletions}</span>
            {file.binary && <span className="text-fg-3">binary</span>}
            {file.tooLarge && <span className="text-fg-3">large</span>}
          </div>
          <svg
            width="9"
            height="9"
            viewBox="0 0 10 10"
            className="shrink-0 text-fg-3 transition-transform"
            style={{ transform: open ? 'rotate(180deg)' : 'rotate(0deg)' }}
          >
            <path d="M2 4 L5 7 L8 4" stroke="currentColor" strokeWidth="1" fill="none" />
          </svg>
        </button>
        {onOpenFile && file.operation !== 'delete' && (
          <button
            onClick={() => onOpenFile(file.path)}
            className="shrink-0 rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[8.5px] uppercase tracking-[0.08em] text-fg-3 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
          >
            open
          </button>
        )}
      </div>

      {open && (
        <div className="border-t border-white/[0.04] bg-black/35">
          {file.diff ? (
            <>
              <pre className="max-h-[360px] overflow-auto py-1 font-mono text-[10.5px] leading-[1.45]">
                {diffLines.map((line, idx) => (
                  <DiffLine key={idx} line={line} />
                ))}
              </pre>
              {(file.diffTruncated || isDiffViewTruncated) && (
                <div className="border-t border-white/[0.04] px-2 py-1 font-mono text-[9px] text-fg-3">
                  diff truncated
                </div>
              )}
            </>
          ) : (
            <div className="px-2 py-2 font-mono text-[10px] text-fg-3">
              diff unavailable
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function OperationBadge({ operation }: { operation: FileChangeRecord['operation'] }) {
  const tone =
    operation === 'create'
      ? 'bg-ok/10 text-ok ring-ok/20'
      : operation === 'delete'
        ? 'bg-danger/10 text-danger ring-danger/20'
        : operation === 'rename'
          ? 'bg-warn/10 text-warn ring-warn/20'
          : 'bg-accent/10 text-accent ring-accent/20'
  return (
    <span className={`shrink-0 rounded px-1.5 py-[1px] font-mono text-[8.5px] uppercase ring-1 ${tone}`}>
      {operation}
    </span>
  )
}

function DiffLine({ line }: { line: string }) {
  const color =
    line.startsWith('+++') || line.startsWith('---')
      ? 'text-fg-2'
      : line.startsWith('@@')
        ? 'bg-accent/10 text-accent'
        : line.startsWith('+')
          ? 'bg-ok/10 text-ok'
          : line.startsWith('-')
            ? 'bg-danger/10 text-danger'
            : 'text-fg-1'
  return (
    <span className={`block min-w-max px-2 ${color}`}>
      {line || ' '}
    </span>
  )
}
