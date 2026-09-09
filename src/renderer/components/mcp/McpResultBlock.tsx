import { useState } from 'react'
import { mcpMessageLooksTabular } from '../../lib/mcp'
import { copyText } from './mcpUi'

/**
 * Conversation block for an `mcp_command_result` reply. Multi-line output
 * (status tables, tool lists, catalog rows, test reports) is unreadable
 * as a 2.6 s toast, so the store drops a system part with
 * `systemSubtype: 'mcp_result' | 'mcp_error'` into the transcript and
 * Conversation renders it through here.
 */
export function McpResultBlock({
  text,
  ok,
  action,
}: {
  text: string
  ok: boolean
  action?: string
}) {
  const [copied, setCopied] = useState(false)
  const monospace = mcpMessageLooksTabular(text) || text.includes('\n')
  const tone = ok ? 'text-accent' : 'text-danger'
  const ring = ok ? 'ring-hairline' : 'ring-1 ring-danger/30'
  const bg = ok ? 'bg-white/[0.025]' : 'bg-danger/[0.06]'
  return (
    <div className={`rounded-md ${bg} ${ring} px-3 py-2`} data-testid="mcp-result-block">
      <div className="mb-1 flex items-center gap-2">
        <span className={`font-mono text-[10.5px] uppercase tracking-[0.08em] ${tone}`}>
          mcp{action ? ` · ${action}` : ''}
        </span>
        <span className={`font-mono text-[10px] uppercase ${ok ? 'text-fg-3' : 'text-danger/80'}`}>
          {ok ? 'ok' : 'error'}
        </span>
        <button
          type="button"
          onClick={() => {
            void copyText(text).then((done) => {
              setCopied(done)
              setTimeout(() => setCopied(false), 1200)
            })
          }}
          className="ml-auto font-mono text-[10px] uppercase tracking-[0.08em] text-fg-3 hover:text-fg-1"
        >
          {copied ? '✓ copied' : 'copy'}
        </button>
      </div>
      <div
        className={`selectable whitespace-pre-wrap break-words text-[11.5px] leading-[1.55] ${
          monospace ? 'font-mono text-[11px]' : ''
        } ${ok ? 'text-fg-0' : 'text-danger'}`}
      >
        {text}
      </div>
    </div>
  )
}
