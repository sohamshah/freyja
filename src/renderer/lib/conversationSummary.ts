import type { Message, ToolCallRecord } from '@shared/events'

/**
 * Extract a plain-text conversation summary from UI messages for legacy
 * sessions that predate engine transcript persistence. Tool results are
 * intentionally capped; the goal is continuity, not full replay.
 */
export function extractConversationSummary(
  messages: Message[],
  toolCalls: Record<string, ToolCallRecord>,
): string {
  const MAX_SUMMARY_CHARS = 80_000
  const lines: string[] = []
  for (const msg of messages) {
    const role = msg.role.toUpperCase()
    for (const part of msg.parts) {
      if (part.type === 'text' && part.text) {
        const text =
          part.text.length > 2000 ? `${part.text.slice(0, 2000)}...` : part.text
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
          if (result) lines.push(`  -> ${result}`)
        }
      }
    }
  }
  const summary = lines.join('\n')
  return summary.length > MAX_SUMMARY_CHARS
    ? `${summary.slice(0, MAX_SUMMARY_CHARS)}\n...(truncated)`
    : summary
}
