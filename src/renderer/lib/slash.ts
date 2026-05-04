export interface SlashCommand {
  name: string
  description: string
  keys?: string
}

export const SLASH_COMMANDS: SlashCommand[] = [
  { name: '/help', description: 'Open the command palette' },
  { name: '/settings', description: 'Open the settings modal', keys: 'Cmd+,' },
  { name: '/new', description: 'Start a fresh session', keys: 'Cmd+N' },
  { name: '/clear', description: 'Alias for /new' },
  { name: '/tools', description: 'Load the full tool catalog' },
  { name: '/usage', description: 'Show token and cost usage' },
  { name: '/model', description: 'Switch model (usage: /model claude-opus-4-6)' },
  { name: '/permissions', description: 'Jump to the permission policy' },
  { name: '/subagents', description: 'Open the subagent panel', keys: 'Cmd+O' },
  { name: '/skills', description: 'Browse the skills index' },
  { name: '/memory', description: 'Show persistent memory notes' },
  { name: '/sessions', description: 'List recent sessions' },
  { name: '/export', description: 'Export transcript (markdown / jsonl)' },
  { name: '/compact', description: 'Force a context compaction pass' },
  { name: '/debug', description: 'Toggle the debug drawer', keys: 'Cmd+D' },
  { name: '/docs', description: 'Open the Freyja documentation' },
  { name: '/burst', description: '(demo) trigger a scripted demo burst', keys: 'Cmd+B' },
  { name: '/computer', description: 'Delegate a task to a computer-use sub-agent' },
  { name: '/screen', description: 'Alias for /computer' },
  { name: '/diagnose', description: 'Dump asyncio task states from the bridge (for debugging stuck sessions)' },
  { name: '/restart-bridge', description: 'Kill + respawn the Python bridge (use after editing bridge/ code)' },
]

export function matchSlash(query: string): SlashCommand[] {
  if (!query.startsWith('/')) return []
  const q = query.slice(1).toLowerCase()
  if (!q) return SLASH_COMMANDS
  return SLASH_COMMANDS.filter((c) => c.name.slice(1).toLowerCase().includes(q))
}
