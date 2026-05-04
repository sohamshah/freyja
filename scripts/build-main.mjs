#!/usr/bin/env node
// Build the Electron main process and preload script with esbuild.
// - main -> dist-main/main.cjs (ES modules flattened to CommonJS for Electron's main)
// - preload -> dist-preload/preload.cjs
//
// We use CJS for both so Electron's simple `require` path works regardless of
// how the package.json "type" field is set. The renderer is built separately
// by Vite.

import esbuild from 'esbuild'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const ROOT = path.resolve(__dirname, '..')

const watch = process.argv.includes('--watch')

/** @type {esbuild.BuildOptions} */
const mainCommon = {
  bundle: true,
  platform: 'node',
  target: 'node20',
  format: 'cjs',
  external: ['electron'],
  sourcemap: true,
  logLevel: 'info',
  define: {
    'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV || 'production'),
  },
}

async function build() {
  const mainCtx = await esbuild.context({
    ...mainCommon,
    entryPoints: [path.join(ROOT, 'src/main/main.ts')],
    outfile: path.join(ROOT, 'dist-main/main.cjs'),
  })

  const preloadCtx = await esbuild.context({
    ...mainCommon,
    entryPoints: [path.join(ROOT, 'src/preload/preload.ts')],
    outfile: path.join(ROOT, 'dist-preload/preload.cjs'),
  })

  if (watch) {
    await mainCtx.watch()
    await preloadCtx.watch()
    console.log('[build-main] watching for changes...')
  } else {
    await mainCtx.rebuild()
    await preloadCtx.rebuild()
    await mainCtx.dispose()
    await preloadCtx.dispose()
    console.log('[build-main] built main + preload')
  }
}

build().catch((err) => {
  console.error(err)
  process.exit(1)
})
