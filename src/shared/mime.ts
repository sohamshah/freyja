/**
 * One MIME table for everything that inspects an artifact by extension.
 *
 * It used to live only inside the `artifact:read` IPC handler and was missing
 * every media type the app can actually produce — no `pdf`, `mp4`, `wav`,
 * `mp3`. Those all fell through to `text/plain`, so the preview's
 * `mimeType.startsWith('video/')` style checks could never fire and a
 * generated video or PDF was shown as an opaque "binary file" card. Two
 * copies is how that happens, so there is one.
 */
export const MIME_BY_EXTENSION: Record<string, string> = {
  html: 'text/html; charset=utf-8',
  htm: 'text/html; charset=utf-8',
  css: 'text/css; charset=utf-8',
  js: 'text/javascript; charset=utf-8',
  mjs: 'text/javascript; charset=utf-8',
  json: 'application/json; charset=utf-8',
  map: 'application/json; charset=utf-8',
  svg: 'image/svg+xml',
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  avif: 'image/avif',
  ico: 'image/x-icon',
  bmp: 'image/bmp',
  pdf: 'application/pdf',
  mp4: 'video/mp4',
  m4v: 'video/mp4',
  mov: 'video/quicktime',
  webm: 'video/webm',
  ogv: 'video/ogg',
  mp3: 'audio/mpeg',
  m4a: 'audio/mp4',
  wav: 'audio/wav',
  ogg: 'audio/ogg',
  flac: 'audio/flac',
  aac: 'audio/aac',
  woff: 'font/woff',
  woff2: 'font/woff2',
  ttf: 'font/ttf',
  otf: 'font/otf',
  txt: 'text/plain; charset=utf-8',
  md: 'text/markdown; charset=utf-8',
  xml: 'application/xml; charset=utf-8',
  csv: 'text/csv; charset=utf-8',
  wasm: 'application/wasm',
  zip: 'application/zip',
  gz: 'application/gzip',
  // Text + code. These decide the text/binary split as much as the media
  // types do — anything resolving to text/* is decoded as UTF-8.
  markdown: 'text/markdown; charset=utf-8',
  yaml: 'application/yaml',
  yml: 'application/yaml',
  toml: 'application/toml',
  tsv: 'text/tab-separated-values; charset=utf-8',
  log: 'text/plain; charset=utf-8',
  ts: 'text/typescript; charset=utf-8',
  tsx: 'text/typescript; charset=utf-8',
  jsx: 'text/javascript; charset=utf-8',
  py: 'text/x-python; charset=utf-8',
  rs: 'text/x-rust; charset=utf-8',
  go: 'text/x-go; charset=utf-8',
  java: 'text/x-java; charset=utf-8',
  c: 'text/x-c; charset=utf-8',
  h: 'text/x-c; charset=utf-8',
  cpp: 'text/x-c++; charset=utf-8',
  scss: 'text/scss; charset=utf-8',
  sh: 'text/x-shellscript; charset=utf-8',
  bash: 'text/x-shellscript; charset=utf-8',
  zsh: 'text/x-shellscript; charset=utf-8',
  sql: 'text/x-sql; charset=utf-8',
  swift: 'text/x-swift; charset=utf-8',
  glsl: 'text/plain; charset=utf-8',
  tex: 'text/x-tex; charset=utf-8',
}

/** MIME type for a path, by extension. Unknown extensions are binary. */
export function mimeTypeForPath(filePath: string, fallback = 'application/octet-stream'): string {
  const dot = filePath.lastIndexOf('.')
  if (dot < 0) return fallback
  const ext = filePath.slice(dot + 1).toLowerCase()
  return MIME_BY_EXTENSION[ext] ?? fallback
}

/** True for types the browser streams, and therefore wants HTTP Range on. */
export function isStreamableType(type: string): boolean {
  return type.startsWith('video/') || type.startsWith('audio/')
}

/** True for types that must be carried as bytes rather than decoded as text. */
export function isBinaryType(type: string): boolean {
  return (
    type.startsWith('image/') ||
    type.startsWith('video/') ||
    type.startsWith('audio/') ||
    type.startsWith('font/') ||
    type === 'application/pdf' ||
    type === 'application/zip' ||
    type === 'application/gzip' ||
    type === 'application/wasm' ||
    type === 'application/octet-stream'
  )
}
