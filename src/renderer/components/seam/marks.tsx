import type { ToolCallRecord } from '@shared/events'
import { resolveToolCall } from '../../lib/toolCall'
import { EDIT_TOOLS } from './stepWords'

/**
 * The mark at the head of a line: a small drawing of what the step does,
 * on an 11 × 11 grid, one hairline stroke, no fill. A step of a kind we do
 * not know keeps the plain ring. While a step runs, every mark is the same
 * turning ring; when it lands, the ring settles into its drawing.
 */

export type MarkKind =
  | 'think'
  | 'step'
  | 'run'
  | 'read'
  | 'edit'
  | 'look'
  | 'web'
  | 'page'
  | 'talk'
  | 'task'
  | 'note'
  | 'image'
  | 'pointer'
  | 'letter'

const KINDS: Array<[MarkKind, string[]]> = [
  ['run', ['bash', 'code']],
  ['read', ['read_file', 'read', 'read_ax_tree']],
  ['look', ['glob', 'grep', 'list_directory', 'list_windows', 'list_displays', 'find_element']],
  ['web', ['web_search', 'twitter_search']],
  ['page', ['web_fetch', 'web_research', 'browser_navigate', 'browser_execute_js']],
  ['talk', ['talk', 'send_attachment']],
  ['task', ['tasks', 'kanban', 'schedule']],
  ['note', ['memory', 'session_memory', 'working_memory', 'publish_finding', 'read_findings', 'record_user_preference', 'recall']],
  ['image', ['view_image', 'screenshot', 'browser_screenshot', 'appshot', 'inspect_region', 'generate_image', 'generate_svg', 'analyze_video']],
  ['pointer', ['computer_use', 'jev_computer_use', 'click', 'browser_click', 'move_mouse', 'type_text', 'press_key', 'key_down', 'key_up', 'scroll', 'focus_window']],
]
const BY_NAME = new Map<string, MarkKind>(KINDS.flatMap(([kind, names]) => names.map((n) => [n, kind] as [string, MarkKind])))

/** Which drawing a call's line carries. */
export function markKind(call: ToolCallRecord): MarkKind {
  const shown = resolveToolCall(call)
  const name = shown.name
  if (EDIT_TOOLS.has(name) || (call.fileChangeSet?.totals.files ?? 0) > 0) return 'edit'
  if (name === 'artifacts') {
    const action = (shown.arguments as Record<string, unknown> | undefined)?.action
    return action === 'write' || action === 'save' ? 'edit' : 'read'
  }
  return BY_NAME.get(name) ?? 'step'
}

/** The drawings. Each keeps inside 1.5–9.5 on both axes, so the gaps to a
 *  joining line read the same whatever the mark. */
const PATHS: Record<Exclude<MarkKind, 'step'>, string> = {
  // A coil of thread, wound: the work before the work.
  think: 'M4.75 6.25a1.25 1.25 0 0 1 2.5 0a2.5 2.5 0 0 1-5 0a3.75 3.75 0 0 1 7.5 0',
  run: 'M2 2.5l3.5 3-3.5 3M6.5 8.5h3',
  read: 'M5.5 3.1C4.4 2.3 3 2 1.5 2.1v6.6c1.5-.1 2.9.2 4 1 1.1-.8 2.5-1.1 4-1V2.1C8 2 6.6 2.3 5.5 3.1zM5.5 3.1v6.6',
  edit: 'M1.8 9.2l.5-2.3 5-5a1 1 0 0 1 1.4 0l.4.4a1 1 0 0 1 0 1.4l-5 5zM6.5 2.9l1.6 1.6',
  look: 'M7.75 4.75a3 3 0 1 1-6 0a3 3 0 1 1 6 0M7 7l2.5 2.5',
  web: 'M9.5 5.5a4 4 0 1 1-8 0a4 4 0 1 1 8 0M7.2 5.5a1.7 4 0 1 1-3.4 0a1.7 4 0 1 1 3.4 0M1.5 5.5h8',
  page: 'M2.5 2h6a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-6a1 1 0 0 1-1-1v-5a1 1 0 0 1 1-1zM1.5 4.3h8',
  talk: 'M2.5 2h6a1 1 0 0 1 1 1v3.5a1 1 0 0 1-1 1H5.2L3.2 9.2V7.5h-.7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z',
  task: 'M3.2 2h4.6a1.2 1.2 0 0 1 1.2 1.2v4.6a1.2 1.2 0 0 1-1.2 1.2H3.2A1.2 1.2 0 0 1 2 7.8V3.2A1.2 1.2 0 0 1 3.2 2zM3.8 5.6l1.3 1.3 2.3-2.6',
  note: 'M3 1.8h5v7.7L5.5 7.6 3 9.5z',
  image: 'M2.5 2h6a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-6a1 1 0 0 1-1-1v-5a1 1 0 0 1 1-1zM2 8.5l2.6-2.7 1.8 1.7 1-1 1.6 1.6',
  pointer: 'M2.6 1.8l6.2 3.9-2.7.7-1.3 2.9z',
  // A letter: what a helper sends back.
  letter: 'M2.3 2.5h6.4a.8.8 0 0 1 .8.8v4.4a.8.8 0 0 1-.8.8H2.3a.8.8 0 0 1-.8-.8V3.3a.8.8 0 0 1 .8-.8zM1.9 3l3.6 2.9L9.1 3',
}

/** The drawing alone. The plain ring is drawn by CSS, so `step` has none. */
export function Glyph({ kind }: { kind: MarkKind }) {
  if (kind === 'step') return null
  return (
    <svg className="wk-glyph" viewBox="0 0 11 11" width="11" height="11" aria-hidden>
      <path d={PATHS[kind]} />
    </svg>
  )
}
