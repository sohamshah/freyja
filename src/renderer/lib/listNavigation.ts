/**
 * Keyboard navigation and scroll-reveal math for the flat, keyboard-driven
 * lists in the app (the ⌘K palette, the quick switcher, the artifact index).
 *
 * Kept free of React and the DOM so the behaviour can be driven directly from
 * a test script — see `test-list-navigation.mjs`.
 */

export interface NavKey {
  key: string
  metaKey?: boolean
  ctrlKey?: boolean
  altKey?: boolean
  shiftKey?: boolean
}

export interface NavOptions {
  /** Currently highlighted index. */
  selected: number
  /** Number of rows in the list. */
  count: number
  /** Rows a PageUp/PageDown moves by. Defaults to 8. */
  pageStep?: number
  /**
   * Whether single-step moves wrap past the ends. Page jumps never wrap.
   * Defaults to true.
   */
  wrap?: boolean
}

/**
 * Resolve a keypress to the next highlighted index.
 *
 * Returns `null` when the key is not a navigation key, so the caller can let
 * it fall through to the input (typing, caret movement, Enter, Escape). Home
 * and End are deliberately NOT handled: in a search field they belong to the
 * caret. ⌘↑/⌘↓ jump to the ends instead, which is the macOS idiom.
 */
export function stepSelection(e: NavKey, opts: NavOptions): number | null {
  const { selected, count } = opts
  const pageStep = opts.pageStep ?? 8
  const wrap = opts.wrap ?? true
  const last = count - 1
  if (last < 0) return null

  const clamp = (n: number) => Math.max(0, Math.min(last, n))
  const single = (delta: number) => {
    const next = selected + delta
    if (!wrap) return clamp(next)
    if (next < 0) return last
    if (next > last) return 0
    return next
  }

  if (e.altKey) return null

  if (e.key === 'ArrowDown') {
    if (e.metaKey) return last
    return single(1)
  }
  if (e.key === 'ArrowUp') {
    if (e.metaKey) return 0
    return single(-1)
  }
  // Emacs-style bindings, which several terminal-shaped surfaces in the app
  // already honour.
  if (e.ctrlKey && !e.metaKey && (e.key === 'n' || e.key === 'N')) return single(1)
  if (e.ctrlKey && !e.metaKey && (e.key === 'p' || e.key === 'P')) return single(-1)

  if (e.key === 'PageDown') return clamp(selected + pageStep)
  if (e.key === 'PageUp') return clamp(selected - pageStep)

  return null
}

export interface RevealBox {
  /** Current scroll offset of the viewport. */
  scrollTop: number
  /** Visible height of the viewport (clientHeight — excludes borders). */
  viewportHeight: number
  /** Offset of the selected row's top edge within the scrolled content. */
  rowTop: number
  /** Height of the selected row. */
  rowHeight: number
  /**
   * Offset of the topmost element that should come into view along with the
   * row — a group label above the first row of a section. Defaults to rowTop.
   */
  anchorTop?: number
  /** Breathing room kept between the row and the viewport edge. Defaults to 8. */
  pad?: number
  /** Total scrollable content height, used to clamp the result. */
  contentHeight?: number
}

/**
 * Smallest scroll adjustment that brings the selected row fully into view.
 *
 * Returns the new `scrollTop`, or `null` when the row is already visible — so
 * callers can skip the write and avoid fighting a user-driven scroll.
 */
export function scrollToReveal(box: RevealBox): number | null {
  const pad = box.pad ?? 8
  const anchorTop = box.anchorTop ?? box.rowTop
  const rowBottom = box.rowTop + box.rowHeight
  const viewTop = box.scrollTop
  const viewBottom = viewTop + box.viewportHeight

  const maxScroll =
    box.contentHeight != null
      ? Math.max(0, box.contentHeight - box.viewportHeight)
      : Number.POSITIVE_INFINITY
  const clamp = (n: number) => Math.max(0, Math.min(maxScroll, n))

  // Scrolled off the top (or the group label above it is clipped).
  if (anchorTop - pad < viewTop) {
    const next = clamp(anchorTop - pad)
    return next === box.scrollTop ? null : next
  }
  // Scrolled off the bottom.
  if (rowBottom + pad > viewBottom) {
    const next = clamp(rowBottom + pad - box.viewportHeight)
    return next === box.scrollTop ? null : next
  }
  return null
}
