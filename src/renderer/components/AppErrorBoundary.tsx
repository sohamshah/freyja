import React from 'react'

type ErrorState = {
  error: Error | null
}

export class AppErrorBoundary extends React.Component<React.PropsWithChildren, ErrorState> {
  state: ErrorState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorState {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[renderer] uncaught render error', error, info)
  }

  render() {
    if (!this.state.error) return this.props.children

    return (
      <div className="flex h-screen w-screen items-center justify-center bg-[#171b20] p-8 text-fg-0">
        <div className="glass-panel max-w-[620px] rounded-[18px] p-6 ring-hairline">
          <div className="label mb-3 text-danger">renderer recovered</div>
          <h1 className="mb-3 font-mono text-[22px] text-fg-0">Freyja hit a UI error</h1>
          <pre className="mb-5 max-h-[220px] overflow-auto whitespace-pre-wrap rounded-lg bg-black/35 p-3 font-mono text-[11px] text-fg-1">
            {this.state.error.message}
          </pre>
          <button
            type="button"
            className="rounded-md bg-accent/12 px-3 py-2 font-mono text-[11px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/25 hover:bg-accent/18"
            onClick={() => window.location.reload()}
          >
            reload app
          </button>
        </div>
      </div>
    )
  }
}
