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
  {
    name: '/mcp',
    description:
      'MCP servers — /mcp <status|enable|disable|reload|login|logout|reauth|add|remove|test|tools|approve|catalog> [server] [args…] · approve <server> <tool> registers a quarantined tool · add <url|cmd> [--name n] [--transport t] [--header K=V] [--enable] · catalog [list|search <q>|install <id> [--enable]]',
  },
  { name: '/plugin', description: 'Plugins (usage: /plugin [list|install <src>|remove <name>])' },
  { name: '/plugins', description: 'Alias for /plugin', hidden: true },
  { name: '/usage', description: 'Show token and cost usage' },
  { name: '/model', description: 'Switch model (usage: /model claude-opus-4-6)' },
  { name: '/permissions', description: 'Jump to the permission policy' },
  { name: '/subagents', description: 'Open the swarm dashboard', keys: 'Cmd+O' },
  { name: '/schedule', description: 'Open scheduled jobs + past runs', keys: 'Cmd+Shift+S' },
  { name: '/schedules', description: 'Alias for /schedule', hidden: true },
  { name: '/jobs', description: 'Alias for /schedule', hidden: true },
  { name: '/cron', description: 'Alias for /schedule', hidden: true },
  { name: '/artifacts', description: 'Browse every artifact from every session', keys: 'Cmd+Shift+A' },
  { name: '/files', description: 'Alias for /artifacts', hidden: true },
  { name: '/skills', description: 'Browse the skills index' },
  { name: '/learn-this', description: 'Force the skill drafter to run on this conversation' },
  { name: '/memory', description: 'Show persistent memory notes' },
  { name: '/sessions', description: 'List recent sessions' },
  { name: '/export', description: 'Export transcript (markdown / jsonl)' },
  { name: '/compact', description: 'Force a context compaction pass' },
  { name: '/compaction', description: 'Alias for /compact', hidden: true },
  {
    name: '/repair',
    description:
      "Fix history problems that make the API reject this session (/repair images also removes images from the model's copy)",
  },
  { name: '/goal', description: 'Set, inspect, pause, resume, or clear an active goal loop' },
  { name: '/debug', description: 'Toggle the debug drawer', keys: 'Cmd+D', hidden: true },
  { name: '/docs', description: 'Open the Freyja documentation' },
  { name: '/burst', description: '(demo) trigger a scripted demo burst', keys: 'Cmd+B', hidden: true },
  { name: '/computer', description: 'Delegate a task to a computer-use sub-agent' },
  { name: '/screen', description: 'Alias for /computer', hidden: true },
  { name: '/diagnose', description: 'Dump asyncio task states from the bridge (for debugging stuck sessions)', hidden: true },
  { name: '/restart-bridge', description: 'Kill + respawn the Python bridge (use after editing bridge/ code)', hidden: true },
]

/** The command name and arguments a sent message would run, when its first
 *  word has the shape of a command name (`/compact`, `/model gpt-6.2-sol`).
 *  Null when the message only starts with a slash, as a pasted path does
 *  (`/Users/me/app/log.txt shows a crash, why?`): that goes to the agent.
 *  Whether the name is a known command is runSlashCommand's to say. */
export function splitSlashCommand(content: string): { name: string; args: string } | null {
  const match = /^(\/[\w-]+)(?:\s+([\s\S]*))?$/.exec(content.trim())
  return match ? { name: match[1], args: match[2] ?? '' } : null
}

/** The command closest to a mistyped name (`/compcat` → `/compact`), by
 *  edit distance, when it is close enough to be the likely intent. */
export function nearestCommand(name: string): string | null {
  const typed = name.toLowerCase()
  let best: string | null = null
  let bestDistance = Infinity
  for (const command of SLASH_COMMANDS) {
    const d = editDistance(typed, command.name)
    if (d < bestDistance) {
      best = command.name
      bestDistance = d
    }
  }
  const limit = Math.min(2, Math.floor((typed.length - 1) / 2))
  return best && bestDistance <= limit ? best : null
}

function editDistance(a: string, b: string): number {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    const cur = [i]
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
    }
    prev = cur
  }
  return prev[b.length]
}

export type SubmitAction =
  | { kind: 'command' }
  | { kind: 'hold'; name: string; suggestion: string | null }
  | { kind: 'send' }

/** What Enter does with the composer text. A known command runs. A first
 *  word shaped like a command that names none (`/compcat`) is held once,
 *  with a suggestion: sent straight to the agent, a typo cost a turn.
 *  Enter again on the same text sends it, for a message that really starts
 *  that way. Anything else, a pasted path included, is sent. */
export function submitAction(
  content: string,
  run: (name: string, args: string) => boolean,
  held: string | null,
): SubmitAction {
  const command = splitSlashCommand(content)
  if (!command) return { kind: 'send' }
  if (run(command.name, command.args)) return { kind: 'command' }
  if (held === content.trim()) return { kind: 'send' }
  return { kind: 'hold', name: command.name, suggestion: nearestCommand(command.name) }
}

export function matchSlash(query: string): SlashCommand[] {
  if (!query.startsWith('/')) return []
  const q = query.slice(1).toLowerCase()
  if (!q) return SLASH_COMMANDS.filter((c) => !c.hidden)
  return SLASH_COMMANDS.filter((c) => {
    const name = c.name.slice(1).toLowerCase()
    return name.includes(q) && (!c.hidden || name.startsWith(q))
  })
}
