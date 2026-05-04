import { useCallback, useEffect, useRef } from 'react'
import { unstable_batchedUpdates } from 'react-dom'
import { useHarness } from './state/store'
import { TitleBar } from './components/TitleBar'
import { Sidebar } from './components/Sidebar'
import { Conversation } from './components/Conversation'
import { InputDock } from './components/InputDock'
import { ActivityPanel } from './components/ActivityPanel'
import { CommandPalette } from './components/CommandPalette'
import { SubagentDetail } from './components/SubagentDetail'
import { Toast } from './components/Toast'
import { DebugDrawer } from './components/DebugDrawer'
import { ModelPicker } from './components/ModelPicker'
import { PermissionPrompt } from './components/PermissionPrompt'
import { SettingsModal } from './components/SettingsModal'
import { EmergencyPanic } from './components/EmergencyPanic'
import { ComputerPermissionWizard } from './components/ComputerPermissionWizard'
import { ComputerHotkeyOverlay } from './components/ComputerHotkeyOverlay'
import { startInRendererDemo } from './lib/inRendererDemo'
import type { Message, ToolCallRecord } from '@shared/events'

/**
 * Extract a plain-text conversation summary from UI messages for legacy
 * sessions that predate transcript persistence. Keeps it concise — tool
 * results are truncated, thinking blocks omitted.
 */
function extractConversationSummary(
  messages: Message[],
  toolCalls: Record<string, ToolCallRecord>,
): string {
  const MAX_SUMMARY_CHARS = 80_000
  const lines: string[] = []
  for (const msg of messages) {
    const role = msg.role.toUpperCase()
    for (const part of msg.parts) {
      if (part.type === 'text' && part.text) {
        const text = part.text.length > 2000 ? part.text.slice(0, 2000) + '...' : part.text
        lines.push(`[${role}] ${text}`)
      } else if (part.type === 'tool_call' && part.toolCallId) {
        const tc = toolCalls[part.toolCallId]
        if (tc) {
          const args = JSON.stringify(tc.arguments ?? {}).slice(0, 200)
          const result =
            typeof tc.result === 'string'
              ? tc.result.slice(0, 300)
              : JSON.stringify(tc.result ?? '').slice(0, 300)
          lines.push(`[TOOL ${tc.name}] args=${args}`)
          if (result) lines.push(`  → ${result}`)
        }
      }
    }
  }
  const summary = lines.join('\n')
  return summary.length > MAX_SUMMARY_CHARS
    ? summary.slice(0, MAX_SUMMARY_CHARS) + '\n...(truncated)'
    : summary
}

function runPostEventEffects(event: any, api: any) {
  if (event?.type === 'session_spawned') {
    const state = useHarness.getState()
    const sid = event.sessionId as string | undefined
    const parentId = event.parentSessionId as string | undefined
    if (parentId) state.persistSession(parentId).catch(() => {})
    if (sid) state.persistSession(sid).catch(() => {})
    state.persistSessionIndex().catch(() => {})
  }
  if (event?.type === 'turn_complete' || event?.type === 'session_completed') {
    const state = useHarness.getState()
    const sid = (event.sessionId as string | undefined) || state.activeSessionId
    state.persistSession(sid).catch(() => {})
    state.persistSessionIndex().catch(() => {})
  }
  // Legacy fallback: bridge couldn't find a transcript file for a
  // persisted session. If we have UI messages for it, extract a text
  // summary and send it so the model has context.
  if (
    event?.type === 'system_event' &&
    event?.subtype === 'transcript_not_found'
  ) {
    const sid = event.sessionId as string | undefined
    const state = useHarness.getState()
    if (sid && sid === state.activeSessionId && state.messages.length > 0) {
      const summary = extractConversationSummary(state.messages, state.toolCalls)
      if (summary) {
        api?.sendCommand?.({
          type: 'restore_context',
          sessionId: sid,
          summary,
        })
      }
    }
  }
}

export function App() {
  const toggleCommandPalette = useHarness((s) => s.toggleCommandPalette)
  const commandPaletteOpen = useHarness((s) => s.commandPaletteOpen)
  const activeSubagentId = useHarness((s) => s.activeSubagentId)
  const openSubagent = useHarness((s) => s.openSubagent)
  const isStreaming = useHarness((s) => s.isStreaming)
  const cancelTurn = useHarness((s) => s.cancelTurn)
  const newSession = useHarness((s) => s.newSession)
  const toggleDebug = useHarness((s) => s.toggleDebug)
  const debugOpen = useHarness((s) => s.debugOpen)
  const modelPickerOpen = useHarness((s) => s.modelPickerOpen)
  const toggleModelPicker = useHarness((s) => s.toggleModelPicker)
  const settingsOpen = useHarness((s) => s.settingsOpen)
  const toggleSettings = useHarness((s) => s.toggleSettings)
  const hydrateSettings = useHarness((s) => s.hydrateSettings)
  const sidebarCollapsed = useHarness((s) => s.sidebarCollapsed)
  const activityPanelCollapsed = useHarness((s) => s.activityPanelCollapsed)
  const toggleSidebar = useHarness((s) => s.toggleSidebar)
  const toggleActivityPanel = useHarness((s) => s.toggleActivityPanel)
  const bridgeApiRef = useRef<any>(null)
  const eventQueueRef = useRef<any[]>([])
  const rafRef = useRef<number | null>(null)

  const flushBridgeEvents = useCallback(() => {
    rafRef.current = null
    const events = eventQueueRef.current
    if (events.length === 0) return
    eventQueueRef.current = []
    unstable_batchedUpdates(() => {
      for (const event of events) {
        useHarness.getState().handleEvent(event)
      }
    })
    for (const event of events) {
      runPostEventEffects(event, bridgeApiRef.current)
    }
  }, [])

  const enqueueBridgeEvent = useCallback(
    (event: any) => {
      eventQueueRef.current.push(event)
      const flushNow =
        event?.type === 'turn_complete' ||
        event?.type === 'session_completed' ||
        event?.type === 'permission_request' ||
        event?.type === 'emergency_stop'
      if (flushNow) {
        if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
        flushBridgeEvents()
        return
      }
      if (rafRef.current != null) return
      rafRef.current = requestAnimationFrame(flushBridgeEvents)
    },
    [flushBridgeEvents],
  )

  useEffect(() => {
    const api = (window as any).harness
    bridgeApiRef.current = api
    if (api) {
      const unsub = api.onEvent((event: any) => {
        enqueueBridgeEvent(event)
      })
      api.getMode().then((mode: string) => {
        useHarness.getState().handleEvent({
          type: 'ready',
          sessionId: 'session-local',
          mode: mode as any,
          capabilities: {},
        })
      })
      // Hydrate persisted sessions into the sidebar.
      useHarness
        .getState()
        .hydrateFromDisk()
        .then(() => useHarness.getState().persistSessionIndex())
        .catch(() => {})
      // Load settings from disk and push the permission policy to the bridge.
      hydrateSettings().catch(() => {})
      return () => {
        unsub()
        if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
        flushBridgeEvents()
        bridgeApiRef.current = null
      }
    }
    // No Electron bridge — run a pure-renderer demo so the UI can be reviewed
    // in a regular browser or when loaded as a plain HTML file.
    const driver = startInRendererDemo((ev) => enqueueBridgeEvent(ev))
    ;(window as any).__harnessDemo = driver
    return () => {
      driver.stop()
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
      flushBridgeEvents()
    }
  }, [enqueueBridgeEvent, flushBridgeEvents, hydrateSettings])

  // Triple-Esc detection state. Resets after 1 second of inactivity.
  const escTimesRef = useRef<number[]>([])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const isMac = navigator.platform.toLowerCase().includes('mac')
      const mod = isMac ? e.metaKey : e.ctrlKey

      // ⌘⇧Esc — instant emergency stop (no modifier-less escape conflict).
      if (mod && e.shiftKey && e.key === 'Escape') {
        e.preventDefault()
        useHarness.getState().emergencyStopComputer('cmd-shift-esc')
        return
      }

      // Triple-Esc inside 1s — also emergency stop. This is the
      // "muscle memory" version; the user mashes Esc when they want
      // the agent to stop touching their computer.
      if (e.key === 'Escape') {
        const now = Date.now()
        const recent = escTimesRef.current.filter((t) => now - t < 1000)
        recent.push(now)
        escTimesRef.current = recent
        if (recent.length >= 3) {
          escTimesRef.current = []
          const hasActive = Object.values(
            useHarness.getState().computerSessions,
          ).some((s) => s.status === 'running')
          if (hasActive) {
            e.preventDefault()
            useHarness.getState().emergencyStopComputer('triple-esc')
            return
          }
        }
      }

      if (mod && e.key === 'k') {
        e.preventDefault()
        toggleCommandPalette()
        return
      }
      if (mod && e.key === 'b') {
        // ⌘B: child session → back to parent. At the top-level session
        // this is a no-op (we used to fire a demo "burst" message here,
        // but that auto-sent a full prompt to the live model which
        // conflicted with the core "go to parent" semantics).
        e.preventDefault()
        const state = useHarness.getState()
        const active = state.sessions.find((s) => s.id === state.activeSessionId)
        if (active?.parentSessionId) {
          state.switchToParent()
        }
        return
      }
      if (mod && e.key === 'n') {
        e.preventDefault()
        newSession()
        return
      }
      if (mod && e.key === 'd') {
        e.preventDefault()
        toggleDebug()
        return
      }
      if (mod && e.key === ',') {
        e.preventDefault()
        toggleSettings()
        return
      }
      if (mod && e.key === 'o') {
        e.preventDefault()
        const first = Object.values(useHarness.getState().subagents)[0]
        openSubagent(activeSubagentId ? null : first?.id ?? null)
        return
      }
      if (mod && e.key === '[') {
        e.preventDefault()
        useHarness.getState().toggleSidebar()
        return
      }
      if (mod && e.key === ']') {
        e.preventDefault()
        useHarness.getState().toggleActivityPanel()
        return
      }
      if (mod && e.key === '\\') {
        // ⌘\ toggles FOCUS MODE — hide BOTH side panels for maximum
        // conversation real estate. Second press restores both.
        // Delegated to the store so the "one panel open + one
        // closed" state is handled deterministically (previously
        // this was a no-op in that mixed state).
        e.preventDefault()
        useHarness.getState().toggleFocusMode()
        return
      }
      // ⌘Esc cancels the current turn. We intentionally do NOT bind
      // BARE Escape to cancel anymore: when the agent uses
      // `press_key("escape")` as a diagnostic keystroke, macOS
      // routes the injected Esc back into this very window, which
      // would call cancelTurn() here — a self-cancel loop that
      // can deadlock the in-flight tool call. Requiring ⌘ means the
      // agent's plain Escape presses don't accidentally kill its own
      // turn. Users still have ⌘Esc, ⌘⇧Esc (global), triple-Esc
      // (still handled above), and the floating panic button.
      if (mod && e.key === 'Escape') {
        if (isStreaming) {
          e.preventDefault()
          cancelTurn()
          return
        }
      }
      if (e.key === 'Escape') {
        // Dialog dismissal — these are fine to handle on bare Esc
        // because they're no-ops when no dialog is open, and the
        // agent's injected Esc won't match any of these conditions.
        if (settingsOpen) toggleSettings(false)
        else if (modelPickerOpen) toggleModelPicker(false)
        else if (commandPaletteOpen) toggleCommandPalette(false)
        else if (debugOpen) toggleDebug(false)
        else if (activeSubagentId) openSubagent(null)
        return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [
    toggleCommandPalette,
    commandPaletteOpen,
    activeSubagentId,
    openSubagent,
    isStreaming,
    cancelTurn,
    newSession,
    toggleDebug,
    debugOpen,
    modelPickerOpen,
    toggleModelPicker,
    settingsOpen,
    toggleSettings,
    hydrateSettings,
  ])

  // Opt-in faux backdrop so headless screenshots can showcase the glass
  // effect. The real app stays fully transparent so vibrancy works against
  // whatever the user has behind it.
  const showBackdrop =
    typeof window !== 'undefined' &&
    (new URLSearchParams(window.location.search).get('backdrop') === '1' ||
      (window as any).__SCREENSHOT_BACKDROP__ === true)

  return (
    <div className="relative flex h-full w-full flex-col text-fg-0">
      {showBackdrop && <FauxBackdrop />}
      <div className="app-tint relative flex h-full w-full flex-col">
        <TitleBar />
        <div className="flex min-h-0 flex-1 gap-2 px-2 pt-4 pb-3">
          {sidebarCollapsed ? (
            <CollapsedRail
              side="left"
              onExpand={() => toggleSidebar(false)}
              title="Show sidebar (⌘[)"
            />
          ) : (
            <Sidebar />
          )}
          <main className="flex min-w-0 flex-1 flex-col overflow-hidden">

            <Conversation />
            <InputDock />
          </main>
          {activityPanelCollapsed ? (
            <CollapsedRail
              side="right"
              onExpand={() => toggleActivityPanel(false)}
              title="Show activity panel (⌘])"
            />
          ) : (
            <ActivityPanel />
          )}
        </div>
        {commandPaletteOpen && <CommandPalette />}
        {activeSubagentId && <SubagentDetail id={activeSubagentId} />}
        {modelPickerOpen && <ModelPicker />}
        <SettingsModal />
        <PermissionPrompt />
        <ComputerPermissionWizard />
        <ComputerHotkeyOverlay />
        <EmergencyPanic />
        <Toast />
        <DebugDrawer />
      </div>
    </div>
  )
}

/**
 * Narrow edge rail shown when a side panel is collapsed. Gives the user
 * a way to reveal the panel again without memorising the keyboard shortcut.
 */
function CollapsedRail({
  side,
  onExpand,
  title,
}: {
  side: 'left' | 'right'
  onExpand: () => void
  title: string
}) {
  const arrow = side === 'left' ? '›' : '‹'
  return (
    <div
      className="glass flex w-[14px] shrink-0 flex-col items-center justify-center rounded-[18px]"
    >
      <button
        onClick={onExpand}
        title={title}
        className="flex h-full w-full items-center justify-center text-[12px] text-fg-3 hover:bg-white/[0.04] hover:text-fg-1"
      >
        {arrow}
      </button>
    </div>
  )
}

/** Simulated desktop visible only in screenshot mode. */
function FauxBackdrop() {
  return (
    <div
      aria-hidden
      className="absolute inset-0"
      style={{
        background:
          'radial-gradient(1200px 800px at 20% 20%, #1e2a3a 0%, #0f1626 35%, #05070e 70%, #02030a 100%), linear-gradient(180deg, #151d2e 0%, #080a14 100%)',
      }}
    >
      <div
        className="absolute inset-0"
        style={{
          backgroundImage:
            'radial-gradient(circle at 70% 30%, rgba(168,212,252,0.18), transparent 45%), radial-gradient(circle at 30% 80%, rgba(255,180,120,0.12), transparent 40%)',
        }}
      />
    </div>
  )
}
