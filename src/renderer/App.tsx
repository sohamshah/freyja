import { useCallback, useEffect, useRef, useState } from 'react'
import { unstable_batchedUpdates } from 'react-dom'
import { useHarness } from './state/store'
import { useSchedulerStore } from './state/scheduler-store'
import { TitleBar } from './components/TitleBar'
import { Sidebar } from './components/Sidebar'
import { SessionPanes } from './components/SessionPanes'
import { InputDock } from './components/InputDock'
import { ActivityPanel } from './components/ActivityPanel'
import { CommandPalette } from './components/CommandPalette'
import { SubagentDetail } from './components/SubagentDetail'
import { Toast } from './components/Toast'
import { DebugDrawer } from './components/DebugDrawer'
import { ModelPicker } from './components/ModelPicker'
import { PermissionPrompt } from './components/PermissionPrompt'
import { SkillToast } from './components/SkillToast'
import { SettingsModal } from './components/SettingsModal'
import { EmergencyPanic } from './components/EmergencyPanic'
import { ComputerPermissionWizard } from './components/ComputerPermissionWizard'
import { ComputerHotkeyOverlay } from './components/ComputerHotkeyOverlay'
import { MissionDashboard } from './components/MissionDashboard'
import { ScheduledJobsDashboard } from './components/ScheduledJobsDashboard'
import { SlackSetupWizard } from './components/SlackSetupWizard'
import { MetricsDashboard } from './components/MetricsDashboard'
import { MorningRoom } from './components/MorningRoom'
import { RecallPanel } from './components/RecallPanel'
import { SplashScreen } from './components/SplashScreen'
import { IdleSleep } from './components/IdleSleep'
import { QuickSwitcher } from './components/QuickSwitcher'
import { startInRendererDemo } from './lib/inRendererDemo'
import { extractConversationSummary } from './lib/conversationSummary'

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
  // Auto-rename from the bridge needs to land on disk — without
  // persistence the new title vanishes on next app start. The reducer
  // already updated the in-memory list; mirror that to disk here.
  if (event?.type === 'session_renamed') {
    const state = useHarness.getState()
    const sid = event.sessionId as string | undefined
    if (sid) {
      state.persistSession(sid).catch(() => {})
      state.persistSessionIndex().catch(() => {})
    }
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
  const missionDashboardOpen = useHarness((s) => s.missionDashboardOpen)
  const morningRoomOpen = useHarness((s) => s.morningRoomOpen)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const recallDrawer = useHarness((s) => s.recallDrawer)
  const closeRecallDrawer = useHarness((s) => s.closeRecallDrawer)
  const recallSessionId = useHarness((s) => s.activeSessionId)
  const slackSetupOpen = useHarness((s) => s.slackSetupOpen)
  const toggleSlackSetup = useHarness((s) => s.toggleSlackSetup)
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
  const schedulerOpen = useSchedulerStore((s) => s.showDashboard)
  const openScheduler = useSchedulerStore((s) => s.openDashboard)
  const closeScheduler = useSchedulerStore((s) => s.closeDashboard)
  const sidebarCollapsed = useHarness((s) => s.sidebarCollapsed)
  const activityPanelCollapsed = useHarness((s) => s.activityPanelCollapsed)
  const focusMode = useHarness((s) => s.focusMode)
  const splitView = useHarness((s) => {
    const panes = s.sessionPanes
    if (panes.length > 1) return true
    if (panes.length === 1 && panes[0].sessionId !== s.activeSessionId) return true
    return false
  })
  const bridgeApiRef = useRef<any>(null)
  const eventQueueRef = useRef<any[]>([])
  const rafRef = useRef<number | null>(null)
  // Boot splash plays once per process lifetime — App only mounts once,
  // so this state survives reloads of the underlying session but is
  // re-evaluated on every cold launch.
  const [splashShowing, setSplashShowing] = useState(true)

  const flushBridgeEvents = useCallback(() => {
    rafRef.current = null
    const events = eventQueueRef.current
    if (events.length === 0) return
    eventQueueRef.current = []
    unstable_batchedUpdates(() => {
      for (const event of events) {
        useHarness.getState().handleEvent(event)
        // Mirror scheduler-targeted events into the scheduler store
        // so the dashboard updates without going through the main
        // harness store (which has no scheduler awareness).
        if (event?.type === 'scheduler_response'
            || (typeof event?.type === 'string'
                && event.type.startsWith('scheduler_'))) {
          // Lazy import — scheduler-store is small but the App.tsx
          // import graph already has a lot of weight; this defers
          // the cost to the first scheduler event. Includes both the
          // response envelope and the push lifecycle events
          // (scheduler_run_* / scheduler_job_*) so the dashboard
          // stays live without polling.
          import('./state/scheduler-store').then(({ useSchedulerStore }) => {
            useSchedulerStore.getState().handleEvent(event)
          }).catch(() => {})
        }
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

  // Keep header bars a constant distance from the macOS traffic lights at any
  // zoom. The native buttons sit at a fixed device-point position and DON'T
  // zoom, but our CSS left-inset does — so on ⌘-/⌘+ the logo (and the swarm /
  // artifact / brief headers) drift toward/away from them. Counter-scale the
  // inset by the zoom factor (exposed as the `--titlebar-inset` CSS var that
  // every header bar consumes) so its rendered size stays put. Lives here at
  // the root so it tracks zoom regardless of which top-level view is mounted.
  // A `resize` fires on every zoom change (zoom alters the CSS-px viewport).
  useEffect(() => {
    const BASE_INSET = 82
    const apply = () => {
      const api = (window as { harness?: { getZoomFactor?: () => number } }).harness
      const z = typeof api?.getZoomFactor === 'function' ? api.getZoomFactor() : 1
      const zoom = Number.isFinite(z) && z > 0 ? z : 1
      document.documentElement.style.setProperty(
        '--titlebar-inset',
        `${BASE_INSET / zoom}px`,
      )
    }
    apply()
    window.addEventListener('resize', apply)
    return () => window.removeEventListener('resize', apply)
  }, [])

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

      // Bind the scheduler IPC client to the bridge so the dashboard
      // can call listJobs / createJob / etc. The preload exposes
      // `sendCommand` (not `send`), and we await it so failures
      // propagate cleanly. Then do a one-time hydration of jobs,
      // recent runs, metrics, and daemon status so the dashboard
      // opens with content already populated.
      import('./state/scheduler-store').then(({ bindSchedulerBridge, schedulerApi }) => {
        bindSchedulerBridge({
          send: (cmd: any) => {
            // Best-effort fire-and-forget — sendCommand returns a
            // promise we don't await here; the bridge's response
            // arrives via onEvent as scheduler_response.
            try { api.sendCommand?.(cmd) } catch { /* ignore */ }
          },
        })
        schedulerApi.listJobs().catch(() => {})
        schedulerApi.recentRuns(50).catch(() => {})
        schedulerApi.metrics().catch(() => {})
        schedulerApi.daemonStatus().catch(() => {})
      }).catch(() => {})

      // Morning Room landing policy: open automatically on the FIRST
      // launch of a day. If today's edition exists we land on it; if it's
      // MISSING (e.g. the 6am fire slept through), we still open so the
      // room's own auto-recovery can generate today's edition — that's
      // what makes "ready when you sit down" hold after an overnight
      // sleep. localStorage tracks the last auto-open date so subsequent
      // launches the same day land in the normal shell (⌘⇧B reopens it).
      ;(async () => {
        try {
          const res = await api.getBriefing?.()
          // Local calendar date — the briefer writes its date dir with
          // `date +%F` (local), and toISOString() is UTC, which is the
          // wrong day every morning east of Greenwich / evening west.
          const { localToday } = await import('./components/MorningRoom')
          const today = localToday()
          const seenKey = 'freyja.morningRoom.lastAutoOpen'
          const firstLaunchToday = localStorage.getItem(seenKey) !== today
          const hasTodayEdition = res?.json && res?.date === today
          // A briefer job means recovery is possible even with no edition.
          const canRecover = !!res?.brieferJobId
          if (firstLaunchToday && (hasTodayEdition || canRecover)) {
            localStorage.setItem(seenKey, today)
            useHarness.getState().toggleMorningRoom(true)
          }
        } catch {
          /* no briefing surface available — land in the shell as usual */
        }
      })()

      // Auto-refresh the session list so gateway-created sessions
      // (Slack DMs, etc.) appear in the sidebar without requiring an
      // app restart. The gateway daemon is a separate process — when
      // it writes a new transcript to ~/.freyja/sessions/, the
      // renderer has no way to know without re-querying. Two
      // triggers:
      //
      //   · window focus — instant refresh when the operator clicks
      //     back into Freyja (most likely moment to look at the
      //     sidebar; refresh cost is one IPC call)
      //   · 30s background poll — covers the "I'm staring at the
      //     sidebar live, waiting for a new Slack session to land"
      //     case. hydrateFromDisk is idempotent + dedupes by id, so
      //     the cost is mostly the IPC round-trip on a quiet system.
      const refresh = () => {
        useHarness.getState().hydrateFromDisk().catch(() => {})
      }
      const focusHandler = () => refresh()
      window.addEventListener('focus', focusHandler)
      const pollTimer = setInterval(refresh, 30_000)

      return () => {
        unsub()
        window.removeEventListener('focus', focusHandler)
        clearInterval(pollTimer)
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

  // Ctrl+Tab quick switcher state. `null` means closed; otherwise we
  // hold a frozen candidate list (computed at open time so the order
  // doesn't shift mid-cycle as background sessions finish streaming)
  // plus the current selection index. Local state (not store) so the
  // keydown capture handler can mutate it cheaply without bouncing
  // through a reducer for every Tab press.
  const [quickSwitcher, setQuickSwitcher] = useState<{
    candidates: string[]
    selected: number
  } | null>(null)

  const buildSwitcherCandidates = useCallback((): string[] => {
    const state = useHarness.getState()
    const active = state.activeSessionId
    const sessionById = new Map(state.sessions.map((s) => [s.id, s]))

    // Bucket 1: currently-streaming sessions (excluding active), most
    // recently updated first. These are "wants your attention now."
    const streaming = state.sessions
      .filter(
        (s) =>
          s.id !== active &&
          !!state.sessionArchive[s.id]?.isStreaming,
      )
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .map((s) => s.id)

    // Bucket 2: MRU sessions the operator actually visited, preserving
    // recency order. Filter out anything already in bucket 1 or that
    // no longer exists in the sessions list (deleted / unloaded).
    const seen = new Set<string>([active, ...streaming])
    const mru = state.sessionMRU.filter(
      (id) => !seen.has(id) && sessionById.has(id),
    )
    for (const id of mru) seen.add(id)

    // Bucket 3: fill out remaining slots with the most-recently-active
    // sessions overall so a fresh-launch operator (empty MRU) still
    // gets a useful switcher rather than an empty one.
    const others = state.sessions
      .filter((s) => !seen.has(s.id))
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .map((s) => s.id)

    return [...streaming, ...mru, ...others].slice(0, 10)
  }, [])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // Ctrl+Tab opens / advances; Ctrl+Shift+Tab reverses. We keep
      // Ctrl as the modifier on macOS too — Cmd+Tab is the OS app
      // switcher and never reaches the renderer anyway.
      if (e.ctrlKey && e.key === 'Tab') {
        e.preventDefault()
        e.stopPropagation()
        setQuickSwitcher((prev) => {
          if (!prev) {
            const candidates = buildSwitcherCandidates()
            if (candidates.length === 0) return null
            // First Tab while holding Ctrl lands on index 0 — the
            // previously-active session if MRU has any entries, else
            // the most-recently-streaming session. Mirrors browser /
            // IDE Tab-switch muscle memory.
            return { candidates, selected: 0 }
          }
          const step = e.shiftKey ? -1 : 1
          const n = prev.candidates.length
          const next = ((prev.selected + step) % n + n) % n
          return { ...prev, selected: next }
        })
        return
      }
      // Esc while the switcher is open → cancel without switching.
      // Must run before the existing Esc handler so panel-dismiss
      // logic doesn't fire underneath the overlay.
      if (e.key === 'Escape' && quickSwitcher) {
        e.preventDefault()
        e.stopPropagation()
        setQuickSwitcher(null)
        return
      }
    }
    const onKeyUp = (e: KeyboardEvent) => {
      // Releasing Ctrl commits the selection. We tolerate the user
      // releasing Ctrl mid-flight (e.g. cramped-hand reshuffle) by
      // only committing when the switcher is actually open.
      if (e.key !== 'Control') return
      setQuickSwitcher((prev) => {
        if (!prev) return null
        const target = prev.candidates[prev.selected]
        if (target) {
          useHarness
            .getState()
            .openSessionPane(target, 'replace')
            .catch(() => {})
        }
        return null
      })
    }
    // Capture phase so we intercept Tab before any focused input
    // consumes it as a focus-shift key.
    window.addEventListener('keydown', onKeyDown, true)
    window.addEventListener('keyup', onKeyUp, true)
    // If the user alt-tabs the OS while holding Ctrl, the keyup
    // never fires for the renderer. Treat a blur as "Ctrl released" so
    // the overlay doesn't strand open.
    const onBlur = () => setQuickSwitcher(null)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('keydown', onKeyDown, true)
      window.removeEventListener('keyup', onKeyUp, true)
      window.removeEventListener('blur', onBlur)
    }
  }, [buildSwitcherCandidates, quickSwitcher])

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
      if (mod && e.shiftKey && (e.key === 'm' || e.key === 'M')) {
        e.preventDefault()
        toggleMissionDashboard()
        return
      }
      // ⌘⇧S — open the scheduled jobs modal (idempotent — second press
      // is a no-op so users can mash it without flicker; Esc closes).
      if (mod && e.shiftKey && (e.key === 's' || e.key === 'S')) {
        e.preventDefault()
        if (schedulerOpen) closeScheduler()
        else openScheduler()
        return
      }
      // ⌘⇧B — Morning Room (the daily briefing landing view).
      if (mod && e.shiftKey && (e.key === 'b' || e.key === 'B')) {
        e.preventDefault()
        useHarness.getState().toggleMorningRoom()
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
        toggleMissionDashboard(true, 'overview')
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
        else if (schedulerOpen) closeScheduler()
        else if (missionDashboardOpen) toggleMissionDashboard(false)
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
    missionDashboardOpen,
    toggleMissionDashboard,
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
    schedulerOpen,
    openScheduler,
    closeScheduler,
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
      {splashShowing && <SplashScreen onComplete={() => setSplashShowing(false)} />}
      <div className="app-tint relative flex h-full w-full flex-col">
        <TitleBar />
        <div className={`flex min-h-0 flex-1 gap-2 px-2 pb-3 ${focusMode ? 'pt-2' : 'pt-4'}`}>
          {!sidebarCollapsed && <Sidebar />}
          <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
            <SessionPanes />
            {!splitView && <InputDock />}
          </main>
          {!activityPanelCollapsed && <ActivityPanel />}
        </div>
        {quickSwitcher && (
          <QuickSwitcher
            candidateIds={quickSwitcher.candidates}
            selectedIndex={quickSwitcher.selected}
          />
        )}
        {commandPaletteOpen && <CommandPalette />}
        {missionDashboardOpen && <MissionDashboard />}
        {schedulerOpen && <ScheduledJobsDashboard />}
        {morningRoomOpen && <MorningRoom />}
        <SlackSetupWizard
          open={slackSetupOpen}
          onClose={() => toggleSlackSetup(false)}
        />
        <MetricsDashboard />
        <RecallPanel
          open={recallDrawer.open}
          onClose={closeRecallDrawer}
          sessionId={recallSessionId}
          initialQuery={recallDrawer.query}
        />

        {activeSubagentId && <SubagentDetail id={activeSubagentId} />}
        {modelPickerOpen && <ModelPicker />}
        <SettingsModal />
        <PermissionPrompt />
        <SkillToast />
        <ComputerPermissionWizard />
        <ComputerHotkeyOverlay />
        <EmergencyPanic />
        <Toast />
        <DebugDrawer />
        {!splashShowing && <IdleSleep />}
      </div>
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
