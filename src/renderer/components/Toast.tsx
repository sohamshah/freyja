import { useHarness } from '../state/store'

export function Toast() {
  const toast = useHarness((s) => s.toast)
  const clear = useHarness((s) => s.clearToast)
  if (!toast) return null

  const toneClass =
    toast.tone === 'ok'
      ? 'text-ok border-ok/30 bg-ok/10'
      : toast.tone === 'warn'
        ? 'text-warn border-warn/30 bg-warn/10'
        : toast.tone === 'danger'
          ? 'text-danger border-danger/30 bg-danger/10'
          : 'text-accent border-accent/30 bg-accent/10'

  return (
    <div className="pointer-events-none fixed inset-x-0 top-[54px] z-30 flex justify-center">
      <button
        onClick={() => clear()}
        className={`pointer-events-auto animate-fade-in rounded-lg border px-4 py-2 font-mono text-[11px] uppercase tracking-[0.1em] backdrop-blur-md shadow-lg ${toneClass}`}
      >
        {toast.message}
      </button>
    </div>
  )
}
