export interface SlashCommand {
  name: string
  description: string
  keys?: string
  hidden?: boolean
}

export const SLASH_COMMANDS: SlashCommand[] = [
  { name: '/help', description: 'Open the command palette' },
  { name: '/dashboard', description: 'Open mission dashboard', keys: 'Cmd+Shift+M' },
  { name: '/mission', description: 'Alias for /dashboard', hidden: true },
  { name: '/profiles', description: 'Browse sub-agent profiles and model policies' },
  { name: '/agents', description: 'Alias for /profiles', hidden: true },
  { name: '/telemetry', description: 'Open screenshot, compaction, and media-pruning metrics' },
  { name: '/metrics', description: 'Alias for /telemetry', hidden: true },
  { name: '/settings', description: 'Open the settings modal', keys: 'Cmd+,' },
  { name: '/new', description: 'Start a fresh session', keys: 'Cmd+N' },
  { name: '/clear', description: 'Alias for /new', hidden: true },
  { name: '/tools', description: 'Load the full tool catalog' },
  { name: '/usage', description: 'Show token and cost usage' },
  { name: '/model', description: 'Switch model (usage: /model claude-opus-4-6)' },
  { name: '/permissions', description: 'Jump to the permission policy' },
  { name: '/subagents', description: 'Open the swarm dashboard', keys: 'Cmd+O' },
  { name: '/skills', description: 'Browse the skills index' },
  { name: '/learn-this', description: 'Force the skill drafter to run on this conversation' },
  { name: '/memory', description: 'Show persistent memory notes' },
  { name: '/sessions', description: 'List recent sessions' },
  { name: '/export', description: 'Export transcript (markdown / jsonl)' },
  { name: '/compact', description: 'Force a context compaction pass' },
  { name: '/compaction', description: 'Alias for /compact', hidden: true },
  { name: '/goal', description: 'Set, inspect, pause, resume, or clear an active goal loop' },
  { name: '/debug', description: 'Toggle the debug drawer', keys: 'Cmd+D', hidden: true },
  { name: '/docs', description: 'Open the Freyja documentation' },
  { name: '/burst', description: '(demo) trigger a scripted demo burst', keys: 'Cmd+B', hidden: true },
  { name: '/computer', description: 'Delegate a task to a computer-use sub-agent' },
  { name: '/screen', description: 'Alias for /computer', hidden: true },
  { name: '/diagnose', description: 'Dump asyncio task states from the bridge (for debugging stuck sessions)', hidden: true },
  { name: '/restart-bridge', description: 'Kill + respawn the Python bridge (use after editing bridge/ code)', hidden: true },
]

export function matchSlash(query: string): SlashCommand[] {
  if (!query.startsWith('/')) return []
  const q = query.slice(1).toLowerCase()
  if (!q) return SLASH_COMMANDS.filter((c) => !c.hidden)
  return SLASH_COMMANDS.filter((c) => {
    const name = c.name.slice(1).toLowerCase()
    return name.includes(q) && (!c.hidden || name.startsWith(q))
  })
}
