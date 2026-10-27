// The island: a small always-on-top window that hugs the notch (or the top
// edge of the screen) and shows what Freyja is doing while the app is
// elsewhere. Main holds no session state, so the main renderer reduces its
// store into this digest, main relays it to the island, and the island's
// actions travel back the same way onto ordinary store actions.
// Keep this file dependency-free: main, preload and both renderers import it.

export type IslandLiveKind = 'root' | 'sub' | 'voice'

export interface IslandLive {
  id: string
  title: string
  kind: IslandLiveKind
  parentId?: string
  /** When the turn (or the helper) started, ms. */
  startedAt: number
  /** The tool call in flight, if any. */
  tool: { name: string; summary: string; startedAt: number } | null
  thinking: boolean
  /** Tail of the reply being written (≤ 160 chars), the sneak peek. */
  text: string
}

export type IslandAttention =
  | {
      kind: 'permission'
      id: string
      sessionId?: string
      sessionTitle: string
      level: 'info' | 'low' | 'medium' | 'high' | 'dangerous'
      prompt: string
      reason?: string
    }
  | { kind: 'elicitation'; id: string; server: string; message: string; mode: 'form' | 'url' }
  | { kind: 'voice-confirm'; id: string; summary: string }

export interface IslandDone {
  sessionId: string
  title: string
  /** The opening of the reply, ≤ 220 chars, markdown stripped. */
  text: string
  at: number
  success: boolean
  durationMs: number
}

export type IslandVoiceState =
  | 'idle'
  | 'minting'
  | 'connecting'
  | 'listening'
  | 'thinking'
  | 'acting'
  | 'speaking'
  | 'error'
  | 'closing'

export interface IslandVoice {
  state: IslandVoiceState
  active: boolean
  /** Mic level 0..1. */
  level: number
  userLine: string
  assistantLine: string
  hotkeyLabel: string | null
  /** False when voice is switched off or has no key; the key still shows and says so. */
  available: boolean
}

export type IslandBucket = 'now' | 'next' | 'blocked' | 'done'

export interface IslandTaskRow {
  id: string
  title: string
  bucket: IslandBucket
  at: number
}

export interface IslandTasks {
  now: number
  next: number
  blocked: number
  done: number
  /** The most recently touched tasks, newest first (≤ 4). */
  latest: IslandTaskRow[]
  /** A task that just moved to done; the island stamps it. */
  justDone: { id: string; title: string; at: number } | null
}

export interface IslandSessionRow {
  id: string
  title: string
  updatedAt: number
  streaming: boolean
}

export interface IslandDigest {
  at: number
  active: { id: string; title: string; streaming: boolean } | null
  live: IslandLive[]
  attention: IslandAttention[]
  done: IslandDone | null
  voice: IslandVoice
  tasks: IslandTasks
  /** Recent root sessions for the reply picker, most recent first (≤ 6). */
  sessions: IslandSessionRow[]
  appshotLabel: string | null
  /** Spend across the sessions touched today, dollars. */
  costToday: number
  /** The next scheduled job, if the scheduler knows one. */
  schedule: { name: string; at: number } | null
  /** Computer use is capturing the screen: the island hides so it is not in the shot. */
  veil: boolean
}

/** Island → main → main renderer. */
export type IslandAction =
  | { type: 'open'; sessionId: string; mode?: 'replace' | 'split' }
  | { type: 'send'; sessionId: string; text: string; cutIn?: boolean }
  | { type: 'permission'; requestId: string; approved: boolean }
  | { type: 'elicitation'; requestId: string; action: 'accept' | 'decline' | 'cancel' }
  | { type: 'voice'; on: boolean }
  | { type: 'voice-confirm'; approve: boolean }
  | { type: 'capture' }
  | { type: 'focus-app' }
  | { type: 'dashboard'; tab: 'overview' | 'tasks' | 'activity' | 'scheduler' }
  | { type: 'cancel'; sessionId: string }
  /** Files dropped on the island go to the composer's tray (images and videos). */
  | { type: 'attach'; files: Array<{ name: string; mimeType: string; dataBase64: string }> }

/** Island → main, about the window itself. */
export type IslandControl =
  | { type: 'ready' }
  /** The pointer is over the drawn island (true) or over the transparent rest of the window (false). */
  | { type: 'hover'; over: boolean }
  /** Take the keyboard (the composer wants to be typed into). */
  | { type: 'focus' }
  /** Give keyboard focus back to whatever had it. */
  | { type: 'release' }

export interface IslandGeometry {
  displayId: number
  notched: boolean
  /** Width of the hardware notch in points; 0 when the display has none. */
  notchWidth: number
  /** Height of the strip the island fuses with: the notch on a notched display, otherwise the menu bar. */
  stripHeight: number
  /** Window size in points, so the page knows where the top centre is. */
  width: number
  height: number
  scale: number
}
