/**
 * Extract human-readable metadata from artifact files.
 *
 * Subagent artifacts follow the format written by sub_agent_tool.py:
 *
 *   # {agent label}
 *
 *   **Agent type:** explore
 *   **Task:** ...
 *   **Model:** ...
 *   **Tokens:** ...
 *   **Tools called:** ...
 *
 *   ---
 *
 *   {actual content body}
 *
 * We pull the title from the first `# heading`, parse the metadata
 * fields, and grab the first paragraph of the content body as an
 * excerpt for the workspace cards.
 *
 * Non-subagent artifacts (raw write_file outputs) get title=filename
 * and the raw first-paragraph excerpt.
 */

export interface ArtifactMeta {
  /** Human-readable title — first `# heading` or filename. */
  title: string
  /** First paragraph of body content, ~200 chars. */
  excerpt: string
  /** Parsed metadata fields when available. */
  agentType?: string
  task?: string
  model?: string
  tokensIn?: number
  tokensOut?: number
  toolsCalled?: number
}

/** Extract metadata from raw file content. */
export function extractArtifactMeta(
  content: string,
  fileType: string,
  fallbackTitle: string,
): ArtifactMeta {
  // Markdown — full structured parsing
  if (fileType === 'md' || fileType === 'markdown') {
    return parseMarkdownArtifact(content, fallbackTitle)
  }
  // Code / JSON / CSV / others — no title extraction, just excerpt
  return {
    title: cleanFilename(fallbackTitle),
    excerpt: firstParagraph(content, 220),
  }
}

function parseMarkdownArtifact(content: string, fallback: string): ArtifactMeta {
  const lines = content.split('\n')
  let title = ''
  const meta: Partial<ArtifactMeta> = {}
  let bodyStart = 0
  let sawSeparator = false

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const trimmed = line.trim()

    // First H1 = title
    if (!title && trimmed.startsWith('# ')) {
      title = trimmed.slice(2).trim()
      continue
    }

    // Bold metadata lines: **Key:** value
    const metaMatch = trimmed.match(/^\*\*([\w\s]+):\*\*\s*(.+)$/)
    if (metaMatch) {
      const key = metaMatch[1].toLowerCase().trim()
      const value = metaMatch[2].trim()
      if (key === 'agent type') meta.agentType = value
      else if (key === 'task') meta.task = value
      else if (key === 'model') meta.model = value
      else if (key === 'tokens') {
        const m = value.match(/(\d+)\s*in\s*\/\s*(\d+)\s*out/i)
        if (m) {
          meta.tokensIn = parseInt(m[1], 10)
          meta.tokensOut = parseInt(m[2], 10)
        }
      } else if (key === 'tools called') {
        const n = parseInt(value, 10)
        if (!isNaN(n)) meta.toolsCalled = n
      }
      continue
    }

    // The `---` separator marks the end of the header block
    if (trimmed === '---') {
      sawSeparator = true
      bodyStart = i + 1
      break
    }

    // No separator, no metadata, no title — start of body
    if (title && trimmed && !sawSeparator) {
      bodyStart = i
      break
    }
  }

  const body = lines.slice(bodyStart).join('\n').trim()
  return {
    title: title || cleanFilename(fallback),
    excerpt: firstParagraph(body, 220),
    ...meta,
  }
}

/** Strip markdown noise and return the first ~N chars of meaningful prose. */
function firstParagraph(body: string, maxChars: number): string {
  // Walk lines, accumulating prose until we hit ~maxChars or a code block.
  const lines = body.split('\n')
  const collected: string[] = []
  let chars = 0
  for (const raw of lines) {
    const line = raw.trim()
    if (!line) {
      if (collected.length > 0) break // double newline = paragraph end
      continue
    }
    // Skip code fences and tables
    if (line.startsWith('```') || line.startsWith('|')) break
    // Strip markdown emphasis/links for plain readable preview
    const cleaned = line
      .replace(/^#+\s+/, '') // headings
      .replace(/^\s*[-*+]\s+/, '') // list bullets
      .replace(/^\s*\d+\.\s+/, '') // numbered lists
      .replace(/\*\*([^*]+)\*\*/g, '$1') // bold
      .replace(/\*([^*]+)\*/g, '$1') // italic
      .replace(/`([^`]+)`/g, '$1') // inline code
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // links
      .trim()
    if (!cleaned) continue
    collected.push(cleaned)
    chars += cleaned.length + 1
    if (chars >= maxChars) break
  }
  let result = collected.join(' ')
  if (result.length > maxChars) result = result.slice(0, maxChars - 1) + '…'
  return result
}

/** Convert "sub_19ddd04f5b8_13.md" → "13" or
 *  "ai-agent-memory-blog.html" → "ai-agent-memory-blog". */
function cleanFilename(name: string): string {
  const base = name.replace(/\.[^.]+$/, '')
  // For sub_ ids, use the trailing index
  const subMatch = base.match(/^sub_[a-z0-9]+_(\d+)$/)
  if (subMatch) return `Subagent #${subMatch[1]}`
  // Replace separators with spaces
  return base.replace(/[-_]/g, ' ')
}
