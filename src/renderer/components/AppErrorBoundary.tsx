import React from 'react'

type ErrorState = {
  error: Error | null
  componentStack: string | null
}

export class AppErrorBoundary extends React.Component<React.PropsWithChildren, ErrorState> {
  state: ErrorState = { error: null, componentStack: null }

  static getDerivedStateFromError(error: Error): Partial<ErrorState> {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[renderer] uncaught render error', error, info)
    // Capture the component stack so the recovery card surfaces it
    // inline. Saves the operator from having to open DevTools just to
    // see which component looped on a React #185 / unmount thrash.
    this.setState({ componentStack: info.componentStack ?? null })
  }

  render() {
    if (!this.state.error) return this.props.children

    const { error, componentStack } = this.state
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-[#171b20] p-8 text-fg-0">
        <div className="glass-panel max-h-[88vh] w-[860px] max-w-[calc(100vw-3rem)] overflow-hidden rounded-[18px] p-6 ring-hairline">
          <div className="label mb-3 text-danger">renderer recovered</div>
          <h1 className="mb-3 font-mono text-[22px] text-fg-0">Freyja hit a UI error</h1>
          <div className="mb-3 label text-fg-3">error</div>
          <pre className="mb-4 max-h-[160px] overflow-auto whitespace-pre-wrap rounded-lg bg-black/35 p-3 font-mono text-[11px] text-fg-1">
            {error.message}
          </pre>
          {error.stack && (
            <details className="mb-4">
              <summary className="cursor-pointer label text-fg-3 hover:text-fg-1">
                stack trace
              </summary>
              <pre className="mt-2 max-h-[220px] overflow-auto whitespace-pre-wrap rounded-lg bg-black/35 p-3 font-mono text-[10.5px] text-fg-2">
                {error.stack}
              </pre>
            </details>
          )}
          {componentStack && (
            <details className="mb-4" open>
              <summary className="cursor-pointer label text-fg-3 hover:text-fg-1">
                component stack
              </summary>
              <pre className="mt-2 max-h-[260px] overflow-auto whitespace-pre-wrap rounded-lg bg-black/35 p-3 font-mono text-[10.5px] text-fg-2">
                {componentStack.trim()}
              </pre>
            </details>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded-md bg-accent/12 px-3 py-2 font-mono text-[11px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/25 hover:bg-accent/18"
              onClick={() => window.location.reload()}
            >
              reload app
            </button>
            <button
              type="button"
              className="rounded-md bg-white/[0.05] px-3 py-2 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-1 ring-white/10 hover:bg-white/[0.10]"
              onClick={() => {
                const payload = [
                  `error: ${error.message}`,
                  '',
                  error.stack ?? '',
                  '',
                  '── component stack ──',
                  componentStack ?? '(none)',
                ].join('\n')
                navigator.clipboard?.writeText(payload).catch(() => {})
              }}
              title="Copy full error + stacks to clipboard for sharing"
            >
              copy error
            </button>
          </div>
        </div>
      </div>
    )
  }
}
