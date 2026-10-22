import React, { useEffect, useMemo, useState } from 'react'
import { useHarness } from '../../../state/store'
import type { Skill } from '../../../../shared/events'
import { Btn, Frame, Note, Pending, Row, Slot, Well, type WidgetProps } from '../chrome'

const FILLER = /\b(?:how|do|can|should|would|i|we|you|to|a|an|the|is|there|for|on|about|with|skill|playbook|guide|find|me|please|what|way)\b/g

function tokens(s: string): string[] {
  return s.toLowerCase().replace(FILLER, ' ').split(/[^a-z0-9+#._-]+/).filter((t) => t.length > 1)
}

function score(skill: Skill, q: string[]): number {
  if (q.length === 0) return skill.loadCount ?? 0
  const name = skill.name.toLowerCase()
  const desc = skill.description.toLowerCase()
  const trig = skill.triggers.join(' ').toLowerCase()
  const tags = skill.tags.join(' ').toLowerCase()
  let s = 0
  for (const t of q) {
    if (name.includes(t)) s += 4
    if (trig.includes(t)) s += 3
    if (tags.includes(t)) s += 2
    if (desc.includes(t)) s += 1
  }
  return s
}

export function SkillLookup({ query, slots, crumbs, onClose, onToast }: WidgetProps) {
  const skills = useHarness((s) => s.skills)
  const sendMessage = useHarness((s) => s.sendMessage)
  const ask = slots.skillQuery ?? query
  const q = useMemo(() => tokens(ask), [ask])
  const ranked = useMemo(
    () =>
      Object.values(skills)
        .map((s) => ({ s, score: score(s, q) }))
        .filter((r) => q.length === 0 || r.score > 0)
        .sort((a, b) => b.score - a.score || (b.s.vScore ?? 0) - (a.s.vScore ?? 0))
        .slice(0, 6),
    [skills, q],
  )
  const [picked, setPicked] = useState<string | null>(null)
  useEffect(() => setPicked(null), [ranked[0]?.s.name])
  const sel = ranked.find((r) => r.s.name === picked)?.s ?? ranked[0]?.s

  const [body, setBody] = useState<{ name: string; text: string } | null>(null)
  useEffect(() => {
    if (!sel) return
    const api = (window as any).harness
    if (!api?.skillReadFile) return
    let alive = true
    api.skillReadFile(sel.name).then((r: { ok: boolean; body?: string }) => {
      if (alive && r?.ok && r.body) setBody({ name: sel.name, text: r.body })
    })
    return () => {
      alive = false
    }
  }, [sel?.name])

  const excerpt = useMemo(() => {
    if (!body || body.name !== sel?.name) return null
    const lines = body.text.split('\n')
    // skip front matter
    let i = 0
    if (lines[0]?.trim() === '---') {
      i = lines.findIndex((l, idx) => idx > 0 && l.trim() === '---') + 1
    }
    return lines.slice(i).filter((l) => l.trim()).slice(0, 14).join('\n')
  }, [body, sel?.name])

  return (
    <Frame
      kicker="Skills"
      crumbs={crumbs}
      title={ask ? `“${ask}”` : 'Skill library'}
      badge={`${ranked.length} of ${Object.keys(skills).length}`}
      footer={
        <>
          {sel && (
            <Btn
              onClick={() => {
                const api = (window as any).harness
                if (api?.skillOpen) void api.skillOpen(sel.name)
                else onToast('Skill files are only openable inside the app')
              }}
            >
              Open SKILL.md
            </Btn>
          )}
          <Btn
            primary
            disabled={!sel}
            onClick={() => {
              if (!sel) return
              void sendMessage(slots.skillQuery ? `Load the skill \`${sel.name}\`, then help me ${slots.skillQuery}.` : `Load the skill \`${sel.name}\` and apply it to: ${query}`)
              onToast(`Loading ${sel.name}`)
              onClose()
            }}
          >
            {sel ? `Use ${sel.name}` : 'Use skill'}
          </Btn>
        </>
      }
    >
      {ranked.length === 0 && (
        <Note>
          No skill matches{ask ? ` “${ask}”` : ''}. Press ↵ on “Ask Freyja” in the list, or <span className="kbd text-[8.5px]">/learn-this</span> after you do it once.
        </Note>
      )}
      {ranked.length > 0 && (
        <div className="space-y-1">
          {ranked.map(({ s, score: sc }) => (
            <Row
              key={s.name}
              active={s.name === sel?.name}
              onClick={() => setPicked(s.name)}
              title={s.name}
              flag={
                <span className={`kbd text-[8.5px] ${s.vScore != null && s.vScore < 0 ? 'text-warn' : ''}`}>
                  {s.skillType}
                  {s.vScore != null ? ` · V${s.vScore >= 0 ? '+' : ''}${s.vScore.toFixed(2)}` : ''}
                </span>
              }
              meta={s.description}
              right={q.length ? `${sc}` : s.loadCount ? `${s.loadCount} loads` : undefined}
            />
          ))}
        </div>
      )}
      {sel && (
        <Well className="space-y-1.5">
          <Slot label="Triggers" value={sel.triggers.length ? sel.triggers.slice(0, 4).join(' · ') : undefined} hint="none recorded" />
          {excerpt ? (
            <pre className="max-h-[150px] overflow-y-auto whitespace-pre-wrap text-[10.5px] leading-relaxed text-fg-2">{excerpt}</pre>
          ) : (
            <Pending label="Reading SKILL.md…" />
          )}
        </Well>
      )}
    </Frame>
  )
}
