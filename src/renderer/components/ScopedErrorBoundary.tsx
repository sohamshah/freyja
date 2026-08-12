import React from 'react'

type Props = React.PropsWithChildren<{
  /** Short label for what this region is, e.g. "session pane". */
  label: string
  /** When this value changes, the boundary resets and re-renders its
   *  children. Pass the session/card id so navigating away from a broken
   *  session clears the error instead of sticking. */
  resetKey?: string | number
}>

type State = { error: Error | null; componentStack: string | null }

/**
 * Containment boundary that isolates a crash to ONE region instead of
 * letting it bubble to the app-root AppErrorBoundary (which replaces the
 * entire window — so one bad component would wipe the whole session,
 * activity and all). Renders a compact inline fallback and keeps the rest
 * of the app alive. The fallback surfaces the component stack inline so the
 * exact culprit is visible without opening DevTools.
 */
export class ScopedErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null, componentStack: null }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(`[renderer] error in ${this.props.label}`, error, info)
    this.setState({ componentStack: info.componentStack ?? null })
  }

  componentDidUpdate(prev: Props) {
    // Reset when the caller points this region at a different subject.
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null, componentStack: null })
    }
  }

  render() {
    if (!this.state.error) return this.props.children
    const { error, componentStack } = this.state
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center overflow-auto p-6">
        <div className="glass-panel w-full max-w-[560px] rounded-[14px] p-5 ring-hairline">
          <div className="label mb-2 text-danger">{this.props.label} hit an error</div>
          <pre className="mb-3 max-h-[120px] overflow-auto whitespace-pre-wrap rounded-lg bg-black/35 p-3 font-mono text-[11px] text-fg-1">
            {error.message}
          </pre>
          {componentStack && (
            <details className="mb-3">
              <summary className="cursor-pointer label text-fg-3 hover:text-fg-1">
                component stack
              </summary>
              <pre className="mt-2 max-h-[200px] overflow-auto whitespace-pre-wrap rounded-lg bg-black/35 p-3 font-mono text-[10px] text-fg-2">
                {(error.stack ? error.stack + '\n\n' : '') + componentStack.trim()}
              </pre>
            </details>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded-md bg-white/[0.05] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-1 ring-white/10 hover:bg-white/[0.10]"
              onClick={() => this.setState({ error: null, componentStack: null })}
            >
              retry
            </button>
            <button
              type="button"
              className="rounded-md bg-white/[0.05] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-1 ring-white/10 hover:bg-white/[0.10]"
              onClick={() => {
                const payload = [
                  `region: ${this.props.label}`,
                  `error: ${error.message}`,
                  '',
                  error.stack ?? '',
                  '',
                  '── component stack ──',
                  componentStack ?? '(none)',
                ].join('\n')
                navigator.clipboard?.writeText(payload).catch(() => {})
              }}
            >
              copy error
            </button>
          </div>
        </div>
      </div>
    )
  }
}
