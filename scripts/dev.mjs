#!/usr/bin/env node
// Dev orchestrator:
// 1. Build main + preload in watch mode.
// 2. Start the Vite dev server for the renderer.
// 3. Wait until Vite is ready, then spawn Electron pointing at the Vite URL.
// 4. On exit, clean up all children.

import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import http from 'node:http'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const ROOT = path.resolve(__dirname, '..')

const children = []
function spawnChild(cmd, args, opts) {
  const c = spawn(cmd, args, {
    cwd: ROOT,
    stdio: 'inherit',
    env: { ...process.env, ...opts?.env },
  })
  children.push(c)
  return c
}

function killAll(signal = 'SIGTERM') {
  for (const c of children) {
    try {
      c.kill(signal)
    } catch {}
  }
}

process.on('SIGINT', () => {
  killAll('SIGINT')
  process.exit(0)
})
process.on('SIGTERM', () => {
  killAll('SIGTERM')
  process.exit(0)
})

const DEV_URL = 'http://localhost:5179'

async function waitFor(url, timeoutMs = 20000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    try {
      await new Promise((resolve, reject) => {
        const req = http.get(url, (res) => {
          res.resume()
          resolve()
        })
        req.on('error', reject)
      })
      return true
    } catch {
      await new Promise((r) => setTimeout(r, 400))
    }
  }
  return false
}

async function main() {
  // 1. Watch main/preload
  const mainBuild = spawnChild('node', ['scripts/build-main.mjs', '--watch'])
  mainBuild.on('exit', (code) => {
    if (code !== 0) killAll()
  })

  // 2. Vite dev server
  const vite = spawnChild('npx', ['vite', '--port', '5179', '--strictPort'])
  vite.on('exit', (code) => {
    if (code !== 0) killAll()
  })

  // 3. Wait for Vite + first main build
  console.log('[dev] waiting for Vite...')
  const viteReady = await waitFor(DEV_URL)
  if (!viteReady) {
    console.error('[dev] Vite never came up')
    killAll()
    process.exit(1)
  }

  // Give esbuild a moment to finish
  await new Promise((r) => setTimeout(r, 1200))

  // 4. Electron
  const electron = spawnChild('npx', ['electron', '.'], {
    env: {
      VITE_DEV_SERVER_URL: DEV_URL,
      NODE_ENV: 'development',
    },
  })
  electron.on('exit', () => {
    killAll()
    process.exit(0)
  })
}

main().catch((err) => {
  console.error(err)
  killAll()
  process.exit(1)
})
