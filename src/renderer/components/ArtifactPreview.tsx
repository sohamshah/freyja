import { useEffect, useMemo, useState } from 'react'
import { renderMarkdown } from '../lib/markdown'
import type { ArtifactReadResult } from '@shared/events'

/**
 * Artifact preview renderers. One dispatcher + specialized renderers per
 * file type. Inspired by Zed/Glass's per-type preview crates
 * (markdown_preview, svg_preview, csv_preview, image_viewer).
 *
 * Each renderer is responsible for its own loading/error state and tries
 * to make the content beautiful in-app instead of punting to "open
 * externally". Plain text fallback catches everything else.
 */

export function ArtifactPreview({
  path,
  fileType,
}: {
  path: string
  fileType: string
}) {
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'error'; message: string }
    | { kind: 'ready'; result: ArtifactReadResult }
  >({ kind: 'loading' })

  useEffect(() => {
    let cancelled = false
    const api = (window as any).harness
    if (!api?.artifactRead) {
      setState({ kind: 'error', message: 'Artifact IPC unavailable' })
      return
    }
    setState({ kind: 'loading' })
    api.artifactRead(path).then((result: ArtifactReadResult) => {
      if (cancelled) return
      if (!result.ok) {
        setState({ kind: 'error', message: result.error ?? 'Unknown error' })
      } else {
        setState({ kind: 'ready', result })
      }
    })
    return () => {
      cancelled = true
    }
  }, [path])

  if (state.kind === 'loading') {
    return <LoadingPane />
  }
  if (state.kind === 'error') {
    return <ErrorPane message={state.message} path={path} />
  }

  const { result } = state
  const ft = fileType.toLowerCase()

  // Binary types — images
  if (result.binary) {
    if (result.mimeType.startsWith('image/')) {
      return (
        <ImageRenderer
          base64={result.binary}
          mimeType={result.mimeType}
          path={path}
          size={result.size}
        />
      )
    }
    return <BinaryFallback path={path} size={result.size} mimeType={result.mimeType} />
  }

  const content = result.content ?? ''

  // Route to specialized renderer by file type
  if (ft === 'md' || ft === 'markdown') return <MarkdownRenderer content={content} />
  if (ft === 'svg') return <SvgRenderer content={content} />
  if (ft === 'json') return <JsonRenderer content={content} />
  if (ft === 'csv' || ft === 'tsv') return <CsvRenderer content={content} separator={ft === 'tsv' ? '\t' : ','} />
  if (ft === 'html' || ft === 'htm') return <HtmlRenderer path={path} />
  // Code files
  const CODE_TYPES = new Set([
    'ts', 'tsx', 'js', 'jsx', 'py', 'rs', 'go', 'java', 'c', 'h', 'cpp',
    'css', 'scss', 'sh', 'bash', 'zsh', 'sql', 'yaml', 'yml', 'toml', 'xml',
  ])
  if (CODE_TYPES.has(ft)) return <CodeRenderer content={content} language={ft} />
  // Default — plain text
  return <TextRenderer content={content} />
}

// ─── States ─────────────────────────────────────────────────────────

function LoadingPane() {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="font-mono text-[11px] text-fg-3">Loading artifact…</div>
    </div>
  )
}

function ErrorPane({ message, path }: { message: string; path: string }) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="max-w-[520px] rounded-xl bg-danger/[0.06] p-5 ring-1 ring-danger/30">
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.1em] text-danger">
          Couldn't load artifact
        </div>
        <div className="mb-3 font-mono text-[11.5px] text-fg-1">{message}</div>
        <div className="break-all font-mono text-[10px] text-fg-3">{path}</div>
      </div>
    </div>
  )
}

function BinaryFallback({
  path,
  size,
  mimeType,
}: {
  path: string
  size: number
  mimeType: string
}) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="rounded-xl bg-black/40 p-6 text-center ring-hairline">
        <div className="mb-3 text-[42px] text-fg-3">⊞</div>
        <div className="mb-1 font-mono text-[12px] text-fg-1">
          Binary file — no inline preview
        </div>
        <div className="font-mono text-[10px] text-fg-3">
          {mimeType} · {formatBytes(size)}
        </div>
        <div className="mt-2 break-all font-mono text-[9px] text-fg-3">{path}</div>
      </div>
    </div>
  )
}

// ─── Markdown ───────────────────────────────────────────────────────

function MarkdownRenderer({ content }: { content: string }) {
  const html = useMemo(() => renderMarkdown(content), [content])
  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-[820px]">
        <div
          className="md selectable"
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </div>
    </div>
  )
}

// ─── SVG ────────────────────────────────────────────────────────────

function SvgRenderer({ content }: { content: string }) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-auto bg-[#fafafa] p-8">
        <div className="mx-auto max-w-[960px]">
          <div
            className="flex items-center justify-center rounded-xl bg-white p-8 shadow-sm ring-hairline"
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: content }}
          />
        </div>
      </div>
    </div>
  )
}

// ─── JSON ───────────────────────────────────────────────────────────

function JsonRenderer({ content }: { content: string }) {
  const { formatted, error } = useMemo(() => {
    try {
      const parsed = JSON.parse(content)
      return { formatted: JSON.stringify(parsed, null, 2), error: null }
    } catch (e) {
      return { formatted: content, error: String(e) }
    }
  }, [content])

  return (
    <div className="h-full overflow-y-auto bg-[#0a0a0e] p-6">
      {error && (
        <div className="mb-3 rounded-md bg-warn/[0.08] p-2 font-mono text-[10px] text-warn ring-1 ring-warn/30">
          Parse error: {error}
        </div>
      )}
      <pre className="font-mono text-[11.5px] leading-[1.6] text-fg-1">
        <code>{highlightJson(formatted)}</code>
      </pre>
    </div>
  )
}

function highlightJson(src: string): JSX.Element[] {
  // Lightweight token-level highlighter — no regex catastrophes, just
  // walk character by character. Color strings, numbers, booleans, keys,
  // and punctuation differently.
  const out: JSX.Element[] = []
  let i = 0
  let key = 0
  const push = (text: string, cls: string) => {
    out.push(<span key={key++} className={cls}>{text}</span>)
  }
  while (i < src.length) {
    const ch = src[i]
    if (ch === '"') {
      let j = i + 1
      while (j < src.length && src[j] !== '"') {
        if (src[j] === '\\') j++
        j++
      }
      const str = src.slice(i, j + 1)
      // Look ahead: is this a key (followed by :)?
      let k = j + 1
      while (k < src.length && /\s/.test(src[k])) k++
      const isKey = src[k] === ':'
      push(str, isKey ? 'text-[#4a7c9b]' : 'text-[#5b7a5b]')
      i = j + 1
    } else if (/[-\d]/.test(ch) && (i === 0 || !/[a-zA-Z_]/.test(src[i - 1]))) {
      let j = i
      while (j < src.length && /[\d.eE+\-]/.test(src[j])) j++
      push(src.slice(i, j), 'text-[#b08040]')
      i = j
    } else if (src.slice(i, i + 4) === 'true' || src.slice(i, i + 5) === 'false') {
      const len = src.slice(i, i + 4) === 'true' ? 4 : 5
      push(src.slice(i, i + len), 'text-[#b08040]')
      i += len
    } else if (src.slice(i, i + 4) === 'null') {
      push('null', 'text-fg-3')
      i += 4
    } else {
      push(ch, 'text-fg-2')
      i++
    }
  }
  return out
}

// ─── CSV ────────────────────────────────────────────────────────────

function CsvRenderer({ content, separator }: { content: string; separator: string }) {
  const rows = useMemo(() => parseCsv(content, separator), [content, separator])
  const header = rows[0] ?? []
  const body = rows.slice(1)

  return (
    <div className="h-full overflow-auto p-6">
      <div className="mx-auto max-w-[1200px] overflow-hidden rounded-xl bg-black/40 ring-hairline">
        <div className="border-b border-white/[0.06] px-4 py-2 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-3">
          {body.length} row{body.length !== 1 ? 's' : ''} · {header.length} col{header.length !== 1 ? 's' : ''}
        </div>
        <div className="overflow-auto">
          <table className="min-w-full border-collapse text-[11.5px]">
            <thead>
              <tr>
                {header.map((cell, i) => (
                  <th
                    key={i}
                    className="sticky top-0 z-10 whitespace-nowrap border-b border-white/[0.08] bg-[#17171b] px-3 py-2 text-left font-mono text-[10px] font-bold uppercase tracking-[0.04em] text-fg-2"
                  >
                    {cell}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, r) => (
                <tr key={r} className="odd:bg-white/[0.02]">
                  {row.map((cell, c) => (
                    <td
                      key={c}
                      className="max-w-[380px] truncate border-b border-white/[0.04] px-3 py-1.5 font-mono text-fg-1"
                      title={cell}
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function parseCsv(src: string, sep: string): string[][] {
  // Minimal CSV parser — handles quoted cells with commas.
  const rows: string[][] = []
  let row: string[] = []
  let cell = ''
  let inQuotes = false
  for (let i = 0; i < src.length; i++) {
    const ch = src[i]
    if (inQuotes) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          cell += '"'
          i++
        } else {
          inQuotes = false
        }
      } else {
        cell += ch
      }
    } else {
      if (ch === '"') inQuotes = true
      else if (ch === sep) {
        row.push(cell)
        cell = ''
      } else if (ch === '\n') {
        row.push(cell)
        rows.push(row)
        row = []
        cell = ''
      } else if (ch !== '\r') {
        cell += ch
      }
    }
  }
  if (cell || row.length) {
    row.push(cell)
    rows.push(row)
  }
  return rows.filter((r) => r.length > 1 || (r.length === 1 && r[0].length > 0))
}

// ─── HTML ───────────────────────────────────────────────────────────

function HtmlRenderer({ path }: { path: string }) {
  return (
    <iframe
      src={`file://${path}`}
      className="h-full w-full border-0 bg-white"
      sandbox="allow-same-origin"
      title="HTML preview"
    />
  )
}

// ─── Image ──────────────────────────────────────────────────────────

function ImageRenderer({
  base64,
  mimeType,
  path,
  size,
}: {
  base64: string
  mimeType: string
  path: string
  size: number
}) {
  const src = `data:${mimeType};base64,${base64}`
  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-auto bg-[url('data:image/svg+xml;utf8,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20width=%2216%22%20height=%2216%22%3E%3Crect%20width=%228%22%20height=%228%22%20fill=%22%23111%22/%3E%3Crect%20x=%228%22%20y=%228%22%20width=%228%22%20height=%228%22%20fill=%22%23111%22/%3E%3C/svg%3E')] p-6">
        <img
          src={src}
          alt={path}
          className="mx-auto max-w-full rounded-lg shadow-xl ring-1 ring-white/10"
          style={{ imageRendering: 'auto' }}
        />
      </div>
      <div className="border-t border-white/[0.06] px-5 py-1.5 font-mono text-[10px] text-fg-3">
        {mimeType} · {formatBytes(size)}
      </div>
    </div>
  )
}

// ─── Code ───────────────────────────────────────────────────────────

function CodeRenderer({ content, language }: { content: string; language: string }) {
  const lines = content.split('\n')
  return (
    <div className="h-full overflow-auto bg-[#0a0a0e]">
      <div className="flex min-h-full">
        {/* Line numbers */}
        <div className="sticky left-0 select-none bg-[#0a0a0e] px-3 py-4 text-right font-mono text-[10.5px] leading-[1.6] text-fg-3">
          {lines.map((_, i) => (
            <div key={i}>{i + 1}</div>
          ))}
        </div>
        {/* Code */}
        <div className="flex-1 py-4 pr-6">
          <pre className="font-mono text-[11.5px] leading-[1.6] text-fg-1">
            <code>{highlightCode(content, language)}</code>
          </pre>
        </div>
      </div>
    </div>
  )
}

// Lightweight syntax highlighter — token-based, not perfect but good
// enough for a preview. Handles strings, numbers, keywords, comments,
// and punctuation. Covers the common languages we ship in artifacts.
function highlightCode(src: string, language: string): JSX.Element[] {
  const out: JSX.Element[] = []
  let key = 0
  const push = (text: string, cls: string) => {
    out.push(<span key={key++} className={cls}>{text}</span>)
  }

  // Language-specific keyword sets
  const KEYWORDS: Record<string, Set<string>> = {
    ts: new Set([
      'const', 'let', 'var', 'function', 'class', 'interface', 'type', 'enum',
      'export', 'import', 'from', 'as', 'default', 'return', 'if', 'else',
      'for', 'while', 'do', 'switch', 'case', 'break', 'continue', 'new',
      'async', 'await', 'try', 'catch', 'finally', 'throw', 'extends',
      'implements', 'public', 'private', 'protected', 'static', 'readonly',
      'void', 'null', 'undefined', 'true', 'false', 'this', 'super',
    ]),
    py: new Set([
      'def', 'class', 'if', 'elif', 'else', 'for', 'while', 'return',
      'import', 'from', 'as', 'try', 'except', 'finally', 'raise', 'with',
      'async', 'await', 'lambda', 'yield', 'global', 'nonlocal', 'pass',
      'break', 'continue', 'True', 'False', 'None', 'and', 'or', 'not', 'in', 'is',
    ]),
    rs: new Set([
      'fn', 'let', 'mut', 'const', 'static', 'struct', 'enum', 'trait',
      'impl', 'pub', 'use', 'mod', 'return', 'if', 'else', 'for', 'while',
      'loop', 'match', 'break', 'continue', 'async', 'await', 'true', 'false',
      'self', 'Self', 'Option', 'Result', 'Some', 'None', 'Ok', 'Err',
    ]),
    sh: new Set([
      'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'do', 'done',
      'case', 'esac', 'function', 'return', 'exit', 'echo', 'cd', 'export',
      'local', 'readonly', 'true', 'false',
    ]),
    css: new Set([
      '@media', '@keyframes', '@import', '@font-face', '@supports',
      '!important', 'inherit', 'initial', 'unset',
    ]),
  }
  KEYWORDS.tsx = KEYWORDS.ts
  KEYWORDS.js = KEYWORDS.ts
  KEYWORDS.jsx = KEYWORDS.ts
  KEYWORDS.go = KEYWORDS.ts
  KEYWORDS.java = KEYWORDS.ts
  KEYWORDS.c = KEYWORDS.ts
  KEYWORDS.cpp = KEYWORDS.ts
  KEYWORDS.bash = KEYWORDS.sh
  KEYWORDS.zsh = KEYWORDS.sh
  KEYWORDS.scss = KEYWORDS.css

  const keywords = KEYWORDS[language] ?? new Set<string>()
  const lineComment = language === 'py' || language === 'sh' || language === 'bash' || language === 'zsh' ? '#' : '//'

  let i = 0
  while (i < src.length) {
    const ch = src[i]
    // Line comment
    if (src.slice(i, i + lineComment.length) === lineComment) {
      let j = i
      while (j < src.length && src[j] !== '\n') j++
      push(src.slice(i, j), 'text-fg-3 italic')
      i = j
      continue
    }
    // Block comment for C-style languages
    if ((language === 'ts' || language === 'tsx' || language === 'js' || language === 'jsx' || language === 'rs' || language === 'go' || language === 'c' || language === 'cpp' || language === 'java' || language === 'css' || language === 'scss') && src.slice(i, i + 2) === '/*') {
      let j = i + 2
      while (j < src.length - 1 && src.slice(j, j + 2) !== '*/') j++
      push(src.slice(i, j + 2), 'text-fg-3 italic')
      i = j + 2
      continue
    }
    // String (single or double quote, or backtick)
    if (ch === '"' || ch === "'" || ch === '`') {
      const quote = ch
      let j = i + 1
      while (j < src.length && src[j] !== quote) {
        if (src[j] === '\\') j++
        j++
      }
      push(src.slice(i, j + 1), 'text-[#5b7a5b]')
      i = j + 1
      continue
    }
    // Number
    if (/\d/.test(ch) && (i === 0 || !/[a-zA-Z_]/.test(src[i - 1]))) {
      let j = i
      while (j < src.length && /[\d.xXbBoOeE_a-fA-F]/.test(src[j])) j++
      push(src.slice(i, j), 'text-[#b08040]')
      i = j
      continue
    }
    // Identifier / keyword
    if (/[a-zA-Z_$]/.test(ch)) {
      let j = i
      while (j < src.length && /[a-zA-Z0-9_$]/.test(src[j])) j++
      const word = src.slice(i, j)
      if (keywords.has(word)) {
        push(word, 'text-[#7a6b8a] font-bold')
      } else {
        push(word, 'text-fg-1')
      }
      i = j
      continue
    }
    // Everything else
    push(ch, 'text-fg-2')
    i++
  }
  return out
}

// ─── Plain text ─────────────────────────────────────────────────────

function TextRenderer({ content }: { content: string }) {
  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-[820px]">
        <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-[1.7] text-fg-1">
          {content}
        </pre>
      </div>
    </div>
  )
}

// ─── Utilities ──────────────────────────────────────────────────────

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}
