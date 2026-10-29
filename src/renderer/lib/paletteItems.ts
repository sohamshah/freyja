import { useHarness } from '../state/store'
import { useSchedulerStore } from '../state/scheduler-store'
import { getEffectiveTheme, oppositeTheme } from './theme'
import { SLASH_COMMANDS } from './slash'

/**
 * The rows of the ⌘K palette, built from the store: commands, the slash
 * commands, skills, sub-agents and sessions. The palette lists them and the
 * island's field searches the same rows (docs/ISLAND.md), so one builder
 * serves both: the palette keeps the action closures, the island gets the
 * titles and runs a row by id through `runPaletteItem`.
 */
export interface PaletteItem {
  id: string
  title: string
  subtitle?: string
  group: 'Clipboard' | 'Command' | 'Skill' | 'Subagent' | 'Session'
  action: () => void
}

export function buildPaletteItems(close: () => void): PaletteItem[] {
  const st = useHarness.getState()
  const scheduler = useSchedulerStore.getState()
  const schedulerJobCount = scheduler.jobs.length
  const out: PaletteItem[] = []
  out.push(
    {
      id: 'morning-room',
      title: 'Morning Room',
      subtitle: "Today's briefing — projects, decisions, and a staged plan (⌘⇧B)",
      group: 'Command',
      action: () => {
        useHarness.getState().toggleMorningRoom(true)
        close()
      },
    },
    {
      id: 'mission:overview',
      title: 'Mission Dashboard',
      subtitle: 'Overview of session health, active agents, findings, changes, and artifacts',
      group: 'Command',
      action: () => {
        useHarness.getState().toggleMissionDashboard(true, 'overview')
        close()
      },
    },
    {
      id: 'mission:swarm',
      title: 'Swarm Monitor',
      subtitle: 'Agent lanes, collaboration state, and live multi-agent activity',
      group: 'Command',
      action: () => {
        useHarness.getState().toggleMissionDashboard(true, 'overview')
        close()
      },
    },
    {
      id: 'mission:findings',
      title: 'Findings Board',
      subtitle: 'Message-bus findings grouped by source, topic, and reuse potential',
      group: 'Command',
      action: () => {
        useHarness.getState().toggleMissionDashboard(true, 'findings')
        close()
      },
    },
    {
      id: 'mission:telemetry',
      title: 'Session Telemetry',
      subtitle: 'Screenshots, media pruning, compaction, and context pressure events',
      group: 'Command',
      action: () => {
        useHarness.getState().toggleMissionDashboard(true, 'telemetry')
        close()
      },
    },
    {
      id: 'mission:profiles',
      title: 'Agent Profiles',
      subtitle: 'Browse built-in sub-agent profiles, tools, models, and iteration caps',
      group: 'Command',
      action: () => {
        useHarness.getState().toggleMissionDashboard(true, 'profiles')
        close()
      },
    },
    {
      id: 'artifacts:browse',
      title: 'Artifacts',
      subtitle: 'Every file every session ever produced — search, read, edit, comment (⌘⇧A)',
      group: 'Command',
      action: () => {
        useHarness.getState().toggleArtifactsBrowser(true)
        close()
      },
    },
    {
      id: 'scheduler:open',
      title: 'Scheduled Jobs',
      subtitle:
        schedulerJobCount > 0
          ? `${schedulerJobCount} schedule${schedulerJobCount === 1 ? '' : 's'} · past runs · daemon status (⌘⇧S)`
          : 'Create a schedule, browse past runs, daemon status (⌘⇧S)',
      group: 'Command',
      action: () => {
        useSchedulerStore.getState().openDashboard()
        close()
      },
    },
    {
      id: 'appshot:capture',
      title: 'Appshot',
      subtitle: 'Capture the window behind Freyja, picture and text, into the composer',
      group: 'Command',
      action: () => {
        close()
        void useHarness.getState().captureAppshot('palette')
      },
    },
    {
      id: 'island:toggle',
      title: `Island: ${(st.settings.island?.enabled ?? true) ? 'hide' : 'show'}`,
      subtitle: 'The notch mini view: live sessions, what needs you, a field that replies, searches and asks (⌃⌥⌘I opens it)',
      group: 'Command',
      action: () => {
        close()
        void useHarness.getState().setIslandEnabled(!(useHarness.getState().settings.island?.enabled ?? true))
      },
    },
    {
      id: 'appearance:toggle',
      title: `Appearance: switch to ${oppositeTheme(getEffectiveTheme())} mode`,
      subtitle: 'Flip between the dark and light shells (⌘⇧L)',
      group: 'Command',
      action: () => {
        close()
        void useHarness.getState().setAppearanceTheme(oppositeTheme(getEffectiveTheme()))
      },
    },
    {
      id: 'appearance:system',
      title: 'Appearance: follow system',
      subtitle: 'Let macOS decide, including its sunset schedule',
      group: 'Command',
      action: () => {
        close()
        void useHarness.getState().setAppearanceTheme('system')
      },
    },
  )
  {
    const current = st.sessions.find((sess) => sess.id === st.activeSessionId)
    const runtime = st.runtime ?? current?.runtime ?? 'native'
    if (current && runtime === 'native') {
      out.push({
        id: 'session:fork',
        title: 'Fork session',
        subtitle: 'Copy this session as it stands — context, sub-agents, artifacts, task board',
        group: 'Command',
        action: () => {
          void useHarness.getState().branchSessionFrom(null, `${current.title} (fork)`)
          close()
        },
      })
    }
  }
  for (const c of SLASH_COMMANDS.filter((command) => !command.hidden)) {
    out.push({
      id: `cmd:${c.name}`,
      title: c.name,
      subtitle: c.description,
      group: 'Command',
      action: () => {
        const state = useHarness.getState()
        if (c.name === '/burst') {
          state.requestDemoBurst()
        } else if (c.name === '/schedule' || c.name === '/schedules' || c.name === '/jobs' || c.name === '/cron') {
          // Skip the slash-prefill round-trip — these all just open
          // the modal directly.
          useSchedulerStore.getState().openDashboard()
        } else {
          state.setInputDraft(c.name + ' ')
        }
        close()
      },
    })
  }
  for (const s of Object.values(st.skills)) {
    out.push({
      id: `skill:${s.id}`,
      title: s.name,
      subtitle: `${s.skillType} · ${s.confidence} · ${s.retrievalCount}↑ · ${s.description}`,
      group: 'Skill',
      action: () => {
        useHarness.getState().setInputDraft(`/skills ${s.name}`)
        close()
      },
    })
  }
  for (const sub of Object.values(st.subagents)) {
    out.push({
      id: `sub:${sub.id}`,
      title: sub.label,
      subtitle: `${sub.state} · ${sub.mode} · ${sub.task}`,
      group: 'Subagent',
      action: () => {
        useHarness.getState().openSubagent(sub.id)
        close()
      },
    })
  }
  for (const s of st.sessions) {
    out.push({
      id: `session:${s.id}`,
      title: s.title,
      subtitle: `${s.model} · ${s.workspace}`,
      group: 'Session',
      action: () => {
        useHarness.getState().openSessionPane(s.id, 'replace').catch(() => {})
        close()
      },
    })
  }
  return out
}

/** Run one row by id against the store as it is now. False when the id is gone. */
export function runPaletteItem(id: string): boolean {
  const item = buildPaletteItems(() => {}).find((row) => row.id === id)
  if (!item) return false
  item.action()
  return true
}
