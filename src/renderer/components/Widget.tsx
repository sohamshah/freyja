import React, { useEffect, useMemo, useRef, useState } from 'react'
import { buildWidgetHtml } from '../widgetRuntime'
import { useHarness } from '../state/store'

/** Mount a generative-UI widget in a sandboxed iframe — chromeless by
 *  default so the widget content floats directly in the message
 *  stream, matching Claude Desktop's "Imagine" rendering style.
 *
 *  The iframe is sandboxed with `allow-scripts` (no `allow-same-origin`,
 *  so it gets an opaque origin and can't touch the parent's storage
 *  or DOM) + `allow-popups` (so `openLink` can surface a confirm).
 *  All cross-frame messaging is via `postMessage`. We identity-check
 *  `event.source === iframe.contentWindow` on every inbound message —
 *  origin checks are useless under opaque origin.
 *
 *  The iframe's height is owned by the iframe itself: it reports its
 *  content height via `ui/resize` and we mirror it on the
 *  `<iframe height>` attribute. Avoids nested scrollers, which is a
 *  documented runtime constraint.
 *
 *  `chromeless` is the default. Set it false only if you need the
 *  debug header strip (kind / live / title) — e.g. inside a tool
 *  inspector. The normal in-chat rendering should always be
 *  chromeless. */
export function Widget({
  toolCallId,
  title,
  code,
  kind,
  loadingMessages,
  isStreaming,
  chromeless = true,
}: {
  toolCallId: string
  title: string
  code: string
  kind: 'html' | 'svg'
  loadingMessages: string[]
  isStreaming: boolean
  chromeless?: boolean
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const [iframeHeight, setIframeHeight] = useState<number>(80)
  const [ready, setReady] = useState(false)
  const [linkConfirm, setLinkConfirm] = useState<string | null>(null)
  const sendMessage = useHarness((s) => s.sendMessage)

  // Build the srcdoc only once per (code, kind, title) tuple. Mounting
  // a fresh srcdoc resets the iframe, so we want it stable across
  // renders unrelated to the widget content.
  const srcdoc = useMemo(() => {
    if (isStreaming) return null
    return buildWidgetHtml({ title, code, kind, loadingMessages })
  }, [isStreaming, title, code, kind, loadingMessages])

  // Cycle loading messages while we wait for `ui/ready`. Defaults to
  // a generic shimmer when the agent didn't pass any.
  const [loadingIdx, setLoadingIdx] = useState(0)
  useEffect(() => {
    if (ready || !loadingMessages.length) return
    const id = window.setInterval(
      () => setLoadingIdx((i) => (i + 1) % loadingMessages.length),
      1200,
    )
    return () => window.clearInterval(id)
  }, [ready, loadingMessages.length])

  // postMessage handler.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const iframe = iframeRef.current
      if (!iframe || event.source !== iframe.contentWindow) return
      const data = event.data
      if (!data || typeof data !== 'object') return
      switch ((data as { type?: string }).type) {
        case 'ui/ready':
          setReady(true)
          break
        case 'ui/resize': {
          const h = Number((data as { height?: unknown }).height)
          if (Number.isFinite(h) && h > 0) {
            setIframeHeight(Math.min(2000, Math.max(40, Math.round(h))))
          }
          break
        }
        case 'ui/message': {
          const text = String((data as { text?: unknown }).text ?? '').trim()
          if (text) void sendMessage(text)
          break
        }
        case 'ui/open-link': {
          const url = String((data as { url?: unknown }).url ?? '').trim()
          if (url) setLinkConfirm(url)
          break
        }
        default:
          break
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [sendMessage])

  const containerClass = chromeless
    ? 'my-2 animate-fade-in'
    : 'my-3 overflow-hidden rounded-md border border-white/[0.06] bg-white/[0.015] animate-fade-in'

  return (
    <div className={containerClass}>
      {!chromeless && (
        <header className="flex items-center justify-between gap-3 border-b border-white/[0.04] bg-black/[0.18] px-3 py-1.5">
          <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-3">
            <span aria-hidden>◰</span>
            <span>widget</span>
            <span className="text-fg-4">·</span>
            <span className="normal-case tracking-normal text-fg-1">{title}</span>
          </div>
          <span className="font-mono text-[10px] tabular-nums text-fg-4">
            {kind}
            {ready && !isStreaming ? ' · live' : ''}
          </span>
        </header>
      )}

      {srcdoc ? (
        <iframe
          ref={iframeRef}
          title={title}
          srcDoc={srcdoc}
          /* allow-scripts: JS inside the widget can run.
             allow-popups: openLink() can spawn a confirm dialog.
             Deliberately NO allow-same-origin — the iframe gets an
             opaque origin which blocks access to the parent's
             cookies, storage, and DOM. */
          sandbox="allow-scripts allow-popups"
          style={{
            display: 'block',
            width: '100%',
            height: ready ? `${iframeHeight}px` : '64px',
            border: 'none',
            background: 'transparent',
            colorScheme: 'dark',
            transition: 'height 200ms cubic-bezier(0.16, 1, 0.3, 1)',
          }}
        />
      ) : (
        <ChromelessLoader label={loadingMessages[loadingIdx] || 'Composing widget…'} />
      )}

      {linkConfirm && (
        <LinkConfirm
          url={linkConfirm}
          onCancel={() => setLinkConfirm(null)}
          onOpen={() => {
            try {
              window.open(linkConfirm, '_blank', 'noopener,noreferrer')
            } catch {
              // pop-up blocked or shell denied — silently swallow
            }
            setLinkConfirm(null)
          }}
        />
      )}
    </div>
  )
}

/** Skinny loading shimmer matched to chromeless widget alignment.
 *  Centered label + a thin animated progress strip below it. */
function ChromelessLoader({ label }: { label: string }) {
  return (
    <div className="flex h-[64px] items-center justify-center px-4 opacity-80">
      <div className="flex w-full max-w-[360px] flex-col gap-1.5">
        <div className="text-center font-mono text-[10.5px] tracking-[0.08em] text-fg-3">
          {label}
        </div>
        <div className="relative h-[2px] w-full overflow-hidden rounded-full bg-white/[0.04]">
          <div
            className="absolute inset-y-0 left-0 w-1/3 rounded-full animate-shimmer"
            style={{
              backgroundImage:
                'linear-gradient(90deg, transparent 0%, rgba(168,212,252,0.55) 50%, transparent 100%)',
              backgroundSize: '200% 100%',
            }}
          />
        </div>
      </div>
    </div>
  )
}

/** Inline confirm strip when widget code calls `openLink(url)` or
 *  the user clicks an `<a href>` inside the widget. Agent-generated
 *  content can include arbitrary URLs — the operator needs a beat
 *  to consent before we hand them off to the OS browser.
 *
 *  TODO(v2): route through Electron's shell.openExternal via preload
 *  IPC for proper OS-level handoff. window.open is the fallback. */
function LinkConfirm({
  url,
  onOpen,
  onCancel,
}: {
  url: string
  onOpen: () => void
  onCancel: () => void
}) {
  return (
    <div className="mt-2 flex items-center justify-between gap-3 rounded-md border border-white/[0.06] bg-black/[0.18] px-3 py-2 font-mono text-[11px]">
      <div className="min-w-0 flex-1 truncate text-fg-2">
        <span className="text-fg-3">open link →</span>{' '}
        <span className="text-fg-1">{url}</span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded border border-white/[0.06] px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-fg-3 transition hover:bg-white/[0.04] hover:text-fg-1"
        >
          cancel
        </button>
        <button
          type="button"
          onClick={onOpen}
          className="rounded border border-accent/[0.30] bg-accent/[0.10] px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-accent transition hover:bg-accent/[0.18]"
        >
          open
        </button>
      </div>
    </div>
  )
}
