import { useHarness } from '../state/store'

/**
 * Open a file on disk in its default app.
 *
 * The main process rejects when the file is gone (a swept temp file, a
 * moved artifact) or no app opens it. The callers dropped that promise, so
 * the click did nothing and said nothing; a toast says so now. Each path
 * segment is encoded, so a name with `#`, `?` or `%` opens that file and
 * not a cut-off path.
 */
export async function openLocalFile(path: string): Promise<void> {
  const api = (window as any).harness
  if (!api?.openExternal || !path) return
  const url = `file://${path.split('/').map(encodeURIComponent).join('/')}`
  try {
    const res = await api.openExternal(url)
    if (res && res.ok === false) throw new Error('not opened')
  } catch {
    const name = path.split('/').pop() || path
    useHarness.getState().showToast(`Could not open ${name}`, 'warn')
  }
}
