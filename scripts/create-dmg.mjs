#!/usr/bin/env node
/**
 * Create the distributable DMG from the packed .app.
 *
 * electron-builder's built-in DMG step fails on arm64 macOS Tahoe
 * because it creates an APFS temp image and then tries to convert it
 * to UDZO (zlib-compressed HFS+), which hdiutil refuses. Using
 * `hdiutil create -srcfolder` directly bypasses the conversion and
 * works on all macOS versions.
 */

import { execSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'))

const appName = pkg.build?.productName || pkg.name || 'App'
const version = pkg.version || '0.0.0'
const outDir = path.join(root, pkg.build?.directories?.output || 'out')

// Find the .app — electron-builder puts it in out/mac-arm64/ (or out/mac/)
let appDir
for (const sub of ['mac-arm64', 'mac', 'mac-x64', 'mac-universal']) {
  const candidate = path.join(outDir, sub, `${appName}.app`)
  if (fs.existsSync(candidate)) {
    appDir = candidate
    break
  }
}

if (!appDir) {
  console.error(`Could not find ${appName}.app in ${outDir}/`)
  console.error('Run "npm run package" first.')
  process.exit(1)
}

const arch = path.basename(path.dirname(appDir)).replace('mac-', '') || 'universal'
const dmgName = `${appName}-${version}-${arch}.dmg`
const dmgPath = path.join(outDir, dmgName)

// Remove old DMG if present
if (fs.existsSync(dmgPath)) fs.unlinkSync(dmgPath)

console.log(`Creating ${dmgName} from ${path.relative(root, appDir)}...`)
// hdiutil sometimes fails with "Resource temporarily unavailable" (EAGAIN)
// and then succeeds on an immediate retry. rebuild.sh runs this before it
// installs, so one transient failure used to abort the whole rebuild with
// the app already quit and the new build never installed.
const ATTEMPTS = 3
for (let attempt = 1; ; attempt += 1) {
  try {
    execSync(
      `hdiutil create -volname "${appName}" -srcfolder "${appDir}" -ov -format UDZO "${dmgPath}"`,
      { stdio: 'inherit' },
    )
    break
  } catch (err) {
    if (attempt >= ATTEMPTS) throw err
    console.warn(`hdiutil create failed (attempt ${attempt}/${ATTEMPTS}); retrying in 3s…`)
    execSync('sleep 3')
  }
}

const size = (fs.statSync(dmgPath).size / 1024 / 1024).toFixed(1)
console.log(`\nDMG ready: ${path.relative(root, dmgPath)} (${size} MB)`)
