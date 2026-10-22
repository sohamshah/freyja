import React, { useMemo } from 'react'
import { useHarness } from '../../../state/store'
import type { ArtifactIndexRow } from '../../../../shared/events'
import { revealInFinder } from '../../../lib/paletteData'
import { Btn, Frame, Note, Pending, Slot, Well, relTime, useAsync, type WidgetProps } from '../chrome'

type ReadResult = { ok: boolean; content: string | null; binary: string | null; mimeType: string; size?: number; error?: string }

async function readArtifact(path: string): Promise<{ read: ReadResult; url?: string }> {
  const api = (window as any).harness
  if (!api?.artifactRead) return { read: { ok: false, content: null, binary: null, mimeType: '', error: 'not available outside the app' } }
  const read: ReadResult = await api.artifactRead(path)
  let url: string | undefined
  if (api.artifactServeUrl && (read.mimeType.startsWith('image/') || read.mimeType === 'text/html' || read.mimeType === 'application/pdf' || read.mimeType.startsWith('video/'))) {
    const served = await api.artifactServeUrl(path).catch(() => null)
    if (served?.ok) url = served.url
  }
  return { read, url }
}

export function ArtifactPreview({ context, slots, crumbs, onClose, onToast }: WidgetProps) {
  const row = context?.row as ArtifactIndexRow | undefined
  const path = (context?.path as string | undefined) ?? row?.path
  const { data, loading, error } = useAsync(() => readArtifact(path!), path ?? '', !!path)
  const read = data?.read
  const isHtml = read?.mimeType === 'text/html'
  const isImage = read?.mimeType.startsWith('image/')
  const isText = !!read?.content && !isHtml
  const textExcerpt = useMemo(() => {
    if (!read?.content) return null
    const lines = read.content.split('\n')
    return { text: lines.slice(0, 60).join('\n'), more: Math.max(0, lines.length - 60) }
  }, [read?.content])
  const filename = row?.filename ?? path?.split('/').pop() ?? 'artifact'

  return (
    <Frame
      kicker="Artifact"
      crumbs={crumbs}
      title={filename}
      badge={read?.mimeType || row?.fileType || slots.fileTypes[0] || 'file'}
      footer={
        <>
          <Btn
            onClick={() => {
              if (path) {
                void navigator.clipboard.writeText(path)
                onToast('Path copied')
              }
            }}
          >
            Copy path
          </Btn>
          <Btn
            onClick={() => {
              if (path) revealInFinder(path)
            }}
          >
            Reveal in Finder
          </Btn>
          <Btn
            primary
            disabled={!path}
            onClick={() => {
              useHarness.getState().toggleArtifactsBrowser(true, path ?? null)
              onClose()
            }}
          >
            Open in browser (⌘⇧A)
          </Btn>
        </>
      }
    >
      {!path && <Note>No artifact selected. Pick one from the Artifacts card.</Note>}
      {row && (
        <Well className="space-y-1">
          <Slot label="From" value={row.sessionTitle ?? row.sessionId} hint="" />
          <Slot label="Size" value={`${(row.bytes / 1024).toFixed(0)} KB · ${row.lines ? `${row.lines} lines · ` : ''}${relTime(new Date(row.modifiedAt ?? row.createdAt).getTime())}`} hint="" />
          {row.excerpt && !read?.content && <Slot label="About" value={row.excerpt.slice(0, 160)} hint="" />}
        </Well>
      )}
      {loading && !data && <Pending label="Reading…" />}
      {error && <Note tone="danger">{error}</Note>}
      {read && !read.ok && <Note tone="warn">{read.error ?? 'Could not read the file'}</Note>}
      {isHtml && data?.url && (
        <div className="relative h-[220px] overflow-hidden rounded-lg ring-hairline">
          <iframe title={filename} src={data.url} sandbox="allow-same-origin" className="pointer-events-none h-[880px] w-[2480px] origin-top-left scale-[0.25] bg-white" />
        </div>
      )}
      {isImage && data?.url && (
        <div className="flex max-h-[240px] items-center justify-center overflow-hidden rounded-lg bg-black/20 ring-hairline">
          <img src={data.url} alt={filename} className="max-h-[240px] object-contain" />
        </div>
      )}
      {isText && textExcerpt && (
        <pre className="max-h-[240px] overflow-auto rounded-lg bg-black/[0.18] p-2.5 text-[10.5px] leading-relaxed text-fg-2 ring-hairline">
          {textExcerpt.text}
          {textExcerpt.more > 0 && `\n… ${textExcerpt.more} more lines`}
        </pre>
      )}
    </Frame>
  )
}
