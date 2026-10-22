import { clipboard } from 'electron'
import { execFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import type { PaletteClipboard, PalettePr, PalettePrResult } from '../shared/events.js'

/**
 * Data sources the ⌘K cards read directly, without an agent turn:
 * pull requests through `gh`, and the clipboard for the empty-query
 * suggestions. Both are read-only.
 */

const GH = ['/opt/homebrew/bin/gh', '/usr/local/bin/gh'].find((p) => existsSync(p)) ?? 'gh'

function gh(args: string[], timeoutMs = 12_000): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      GH,
      args,
      { timeout: timeoutMs, maxBuffer: 8 * 1024 * 1024, env: { ...process.env, GH_PAGER: '', NO_COLOR: '1' } },
      (err, stdout, stderr) => {
        if (err) reject(new Error((stderr || err.message || '').trim().split('\n')[0] || 'gh failed'))
        else resolve(stdout)
      },
    )
  })
}

// Paths whose text a session model reads as instructions. These get a
// flag in the triage card so prompt/skill changes are not skimmed.
const PROMPT_FILE = /(^|\/)(AGENTS|CLAUDE)\.md$|(^|\/)SKILL\.md$|(^|\/)prompts?\//i
const PROMPT_FILE_LOOSE = /system_?prompt|tool_?desc|\/knowledge\/|mcpgateway.*(tool|desc)|\.prompt\.|prompt_templates?/i

function searchArgs(scope: string, repo?: string): string[] {
  const base = ['search', 'prs', '--state=open', '--limit', '12', '--json', 'number,title,repository,author,updatedAt,url,isDraft,labels']
  if (repo) base.push('--repo', repo)
  switch (scope) {
    case 'mine':
      return [...base, '--author=@me']
    case 'involves':
      return [...base, '--involves=@me']
    case 'repo':
      return base
    default:
      return [...base, '--review-requested=@me']
  }
}

function summarizeChecks(rollup: unknown): string {
  if (!Array.isArray(rollup) || rollup.length === 0) return 'none'
  let pending = false
  for (const c of rollup as Array<Record<string, unknown>>) {
    const conclusion = String(c.conclusion ?? c.state ?? '').toUpperCase()
    const status = String(c.status ?? '').toUpperCase()
    if (['FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE'].includes(conclusion)) return 'failure'
    if (!conclusion || ['PENDING', 'EXPECTED', 'QUEUED', 'IN_PROGRESS', 'WAITING', 'REQUESTED'].includes(conclusion) || (status && status !== 'COMPLETED')) pending = true
  }
  return pending ? 'pending' : 'success'
}

const cache = new Map<string, { at: number; result: PalettePrResult }>()
const CACHE_MS = 60_000

export async function listPalettePrs(scope: string, repo?: string): Promise<PalettePrResult> {
  const key = `${scope}|${repo ?? ''}`
  const hit = cache.get(key)
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.result
  const t0 = Date.now()
  try {
    const raw = JSON.parse(await gh(searchArgs(scope, repo))) as Array<Record<string, any>>
    const heads: PalettePr[] = raw.map((r) => ({
      repo: String(r.repository?.nameWithOwner ?? ''),
      number: Number(r.number),
      title: String(r.title ?? ''),
      url: String(r.url ?? ''),
      author: String(r.author?.login ?? ''),
      isDraft: !!r.isDraft,
      updatedAt: String(r.updatedAt ?? ''),
      checks: 'none',
      promptFiles: [],
      labels: Array.isArray(r.labels) ? r.labels.map((l: any) => String(l.name ?? l)).filter(Boolean) : [],
    }))
    // Detail only the first eight: each `pr view` is a full GraphQL round trip.
    await Promise.all(
      heads.slice(0, 8).map(async (pr) => {
        try {
          const d = JSON.parse(
            await gh(['pr', 'view', String(pr.number), '--repo', pr.repo, '--json', 'additions,deletions,changedFiles,files,reviewDecision,statusCheckRollup,mergeable']),
          )
          pr.additions = d.additions
          pr.deletions = d.deletions
          pr.changedFiles = d.changedFiles
          pr.reviewDecision = d.reviewDecision || undefined
          pr.mergeable = d.mergeable || undefined
          pr.checks = summarizeChecks(d.statusCheckRollup)
          const files: string[] = Array.isArray(d.files) ? d.files.map((f: any) => String(f.path ?? '')) : []
          pr.promptFiles = files.filter((f) => PROMPT_FILE.test(f) || PROMPT_FILE_LOOSE.test(f)).slice(0, 6)
        } catch {
          // keep the head row; detail is best effort
        }
      }),
    )
    const result = { ok: true, scope, prs: heads, latencyMs: Date.now() - t0 }
    cache.set(key, { at: Date.now(), result })
    return result
  } catch (err) {
    return { ok: false, scope, prs: [], error: (err as Error).message, latencyMs: Date.now() - t0 }
  }
}

export async function readPaletteClipboard(): Promise<PaletteClipboard> {
  const hasImage = await clipboard.has('image/png').catch(() => false)
  const full = (await clipboard.readText().catch(() => '')) ?? ''
  const text = full.length > 12_000 ? full.slice(0, 12_000) : full
  const t = text.trim()
  let kind = 'text'
  if (!t) kind = hasImage ? 'image' : 'empty'
  else if (/^https?:\/\/\S+$/i.test(t)) kind = 'url'
  else if (/^[\[{][\s\S]*[\]}]$/.test(t) && isJson(t)) kind = 'json'
  else if (/(Traceback \(most recent call last\)|^\s+at .+\(.+:\d+:\d+\)|Error: .+\n\s+at |panic: |Exception in thread|^\w+Error: )/m.test(t)) kind = 'stack'
  else if (/^(~|\/|\.\.?\/)[^\n]{1,400}$/.test(t)) kind = 'path'
  else if (/^(diff --git|---\s|\+\+\+\s|@@ )/m.test(t)) kind = 'diff'
  else if (/[{};]\s*$|^\s*(def |class |function |const |import |export |#include|package |fn |pub |use )/m.test(t) && t.split('\n').length > 2) kind = 'code'
  return { text, kind, chars: full.length, hasImage }
}

function isJson(s: string): boolean {
  try {
    JSON.parse(s)
    return true
  } catch {
    return false
  }
}
