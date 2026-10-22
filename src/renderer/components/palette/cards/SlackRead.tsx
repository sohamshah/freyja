import React, { useMemo } from 'react'
import { useHarness } from '../../../state/store'
import { SLACK_SERVER, mcpServerState, openExternal, slackFindChannel, slackPlain, slackReadChannel, slackSearch, type SlackMessage } from '../../../lib/paletteData'
import { Btn, Frame, Note, Pending, Row, RowAction, Slot, Well, useAsync, type WidgetProps } from '../chrome'

const SLACK_HOST = 'https://ematechnologies.slack.com'

function dayBounds(iso?: string): { oldest?: number; on?: string } {
  if (!iso) return {}
  const d = new Date(`${iso}T00:00:00`)
  return { oldest: Math.floor(d.getTime() / 1000), on: iso }
}

export function SlackRead({ slots, query, crumbs, onClose, onToast }: WidgetProps) {
  const servers = useHarness((s) => s.mcp.servers)
  const active = useMemo(() => mcpServerState(SLACK_SERVER) === 'active', [servers])
  const channel = slots.channel
  const person = slots.person ?? slots.attendees[0]
  const { oldest, on } = dayBounds(slots.day?.iso)
  const topic = slots.topic ?? slots.keyword

  const key = `${channel ?? ''}|${person ?? ''}|${on ?? ''}|${topic ?? ''}`
  const { data, loading, error } = useAsync(
    async () => {
      if (channel) {
        const found = await slackFindChannel(channel)
        const ch = found.channels[0]
        if (!ch) return { messages: [] as SlackMessage[], error: found.error ?? `No channel like #${channel}`, channelId: undefined as string | undefined, channelName: undefined as string | undefined }
        const read = await slackReadChannel(ch.id, 25, oldest)
        return { messages: read.messages, error: read.error, channelId: ch.id, channelName: ch.name }
      }
      if (person || topic) {
        const parts: string[] = []
        if (person) parts.push(`from:@${person.split(/\s+/)[0]}`)
        if (topic) parts.push(topic)
        if (on) parts.push(`on:${on}`)
        const r = await slackSearch(parts.join(' '), 15)
        return { messages: r.messages, error: r.error, channelId: undefined as string | undefined, channelName: undefined as string | undefined }
      }
      return { messages: [] as SlackMessage[], error: undefined, channelId: undefined as string | undefined, channelName: undefined as string | undefined }
    },
    key,
    active && !!(channel || person || topic),
  )

  const messages = useMemo(() => {
    let list = data?.messages ?? []
    if (channel && person) {
      const p = person.toLowerCase()
      list = list.filter((m) => m.author.toLowerCase().includes(p) || (m.email ?? '').toLowerCase().includes(p))
    }
    return list
  }, [data, channel, person])

  const scopeLabel = channel ? `#${data?.channelName ?? channel}` : person ? `from ${person}` : topic ? `“${topic}”` : 'Slack'
  const fallback = `In Slack, ${channel ? `read #${channel}` : 'search'}${person ? ` for what ${person} said` : ''}${topic ? ` about ${topic}` : ''}${slots.day ? ` on ${slots.day.iso}` : ''} and summarize the key points.`
  const channelUrl = data?.channelId ? `${SLACK_HOST}/archives/${data.channelId}` : undefined

  return (
    <Frame
      kicker="Slack"
      crumbs={crumbs}
      title={`${scopeLabel}${person && channel ? ` · ${person}` : ''}${slots.day ? ` · ${slots.day.label}` : ''}`}
      badge={!active ? 'not connected' : loading && !data ? 'reading…' : `${messages.length} message${messages.length === 1 ? '' : 's'}`}
      footer={
        <>
          <Btn
            onClick={() => {
              useHarness.getState().setInputDraft(fallback)
              onClose()
            }}
          >
            Ask Freyja to summarize
          </Btn>
          {channelUrl ? (
            <Btn
              primary
              onClick={() => {
                openExternal(channelUrl)
                onClose()
              }}
            >
              Open {scopeLabel} in Slack
            </Btn>
          ) : (
            <Btn
              primary
              disabled={!active}
              onClick={() => {
                void useHarness.getState().sendMessage(fallback)
                onToast('Reading Slack…')
                onClose()
              }}
            >
              Summarize via agent
            </Btn>
          )}
        </>
      }
    >
      {!channel && !person && !topic && (
        <Well className="space-y-1.5">
          <Slot label="Channel" value={undefined} hint="“#proj-autopilot-orchestrator” or “in the dogfooding channel”" />
          <Slot label="From" value={undefined} hint="“what did nari say”" />
          <Slot label="When" value={undefined} hint="“today”, “yesterday”" />
        </Well>
      )}
      {!active && <Note tone="warn">Slack MCP is not active. `/mcp login slack` to reconnect.</Note>}
      {error && <Note tone="danger">{error}</Note>}
      {active && loading && !data && <Pending label={channel ? `Finding #${channel}…` : 'Searching Slack…'} />}
      {data && (
        <div className="max-h-[300px] space-y-1 overflow-y-auto">
          {messages.map((m, i) => {
            const text = slackPlain(m.text)
            return (
              <Row
                key={i}
                title={
                  <>
                    <span className="text-fg-1">{m.author}</span>
                    {m.channel && !channel && <span className="ml-1.5 text-[10px] text-fg-3">{m.channel}</span>}
                  </>
                }
                meta={<span className="whitespace-pre-wrap text-fg-2">{text.length > 240 ? text.slice(0, 240) + '…' : text}</span>}
                right={m.at ? m.at.replace(/^\d{4}-/, '').replace(/:\d{2} [A-Z]+$/, '') : undefined}
                actions={
                  <RowAction
                    title="Draft a reply in the composer (nothing is sent)"
                    onClick={() => {
                      useHarness.getState().setInputDraft(`Draft a reply (do not send) in Slack ${channel ? `#${channel}` : m.channel ?? ''} to ${m.author}'s message “${text.slice(0, 120)}”: `)
                      onClose()
                    }}
                  >
                    reply
                  </RowAction>
                }
              />
            )
          })}
          {messages.length === 0 && !loading && !error && <div className="py-3 text-center text-[11px] text-fg-3">No messages{person ? ` from ${person}` : ''}{slots.day ? ` ${slots.day.label}` : ''}.</div>}
        </div>
      )}
      {query && !channel && !person && !topic && <Note>Keep typing: a #channel, a name, or a topic.</Note>}
    </Frame>
  )
}
