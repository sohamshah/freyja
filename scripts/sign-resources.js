/**
 * electron-builder afterPack hook — codesign the entire app.
 *
 * electron-builder's built-in signing requires a Developer ID certificate
 * in the keychain. We don't have one, so we set identity=null to skip
 * its signing and handle everything here.
 *
 * Signing identity is controlled by FREYJA_SIGN_IDENTITY:
 *   · unset / empty  → ad-hoc signing (`codesign --sign -`). Works
 *     for first-time use but TCC permissions DO NOT persist across
 *     rebuilds because ad-hoc signatures have no stable identity —
 *     macOS keys grants by per-build cdhash, which changes every
 *     time you rebuild.
 *   · set to a keychain identity (e.g. "Freyja Dev") → stable
 *     signing. TCC keys grants by Designated Requirement, which
 *     encodes the signing identity, so Screen Recording /
 *     Accessibility / etc. persist across rebuilds as long as the
 *     same identity is used.
 *
 * Recommended dev setup: create a self-signed Code Signing
 * certificate in Keychain Access named "Freyja Dev", trust it for
 * code signing, then export FREYJA_SIGN_IDENTITY="Freyja Dev"
 * before running `npm run dist`. The provided `scripts/rebuild.sh`
 * wraps this whole flow.
 *
 * Either way, signing enables macOS TCC responsibility inheritance
 * — child processes (the bundled Python) inherit Accessibility and
 * Screen Recording grants from the parent .app.
 *
 * Signing order matters: innermost binaries first, outer .app last.
 * Signing the .app creates a seal over all inner signatures, so they
 * must be finalized before the outer signing.
 */

const { execSync } = require('child_process')
const path = require('path')
const fs = require('fs')

const SIGN_IDENTITY = (process.env.FREYJA_SIGN_IDENTITY || '').trim() || '-'

function sign(target, entitlements, label) {
  try {
    execSync(`codesign --remove-signature "${target}"`, { stdio: 'pipe' })
  } catch {}
  const ent = entitlements ? `--entitlements "${entitlements}"` : ''
  // Quote the identity in case it has spaces (e.g. "Freyja Dev").
  const idArg = `"${SIGN_IDENTITY.replace(/"/g, '\\"')}"`
  try {
    execSync(
      `codesign --force --sign ${idArg} ${ent} --timestamp=none "${target}"`,
      { stdio: 'pipe' },
    )
    if (label) console.log(`  signed: ${label}`)
  } catch (err) {
    if (label) console.warn(`  WARN: ${label}: ${err.message.slice(0, 200)}`)
  }
}

function findFiles(dir, pattern) {
  const results = []
  let entries
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true })
  } catch {
    return results
  }
  for (const ent of entries) {
    const full = path.join(dir, ent.name)
    if (ent.isDirectory()) {
      results.push(...findFiles(full, pattern))
    } else if (pattern.test(ent.name)) {
      results.push(full)
    }
  }
  return results
}

/** @param {import('electron-builder').AfterPackContext} context */
exports.default = async function afterPack(context) {
  if (process.platform !== 'darwin') return

  const appName = context.packager.appInfo.productFilename
  const appPath = path.join(context.appOutDir, `${appName}.app`)
  const contentsDir = path.join(appPath, 'Contents')
  const frameworksDir = path.join(contentsDir, 'Frameworks')
  const resourcesDir = path.join(contentsDir, 'Resources')
  const entInherit = path.resolve(__dirname, '..', 'build', 'entitlements.mac.inherit.plist')
  const entMain = path.resolve(__dirname, '..', 'build', 'entitlements.mac.plist')

  // ── 1. Sign our bundled binaries (python3, .so, .dylib) ───────────
  const innerBinaries = []
  const pythonBin = path.join(resourcesDir, 'python-bundle', 'bin', 'python3')
  if (fs.existsSync(pythonBin)) innerBinaries.push(pythonBin)
  innerBinaries.push(...findFiles(path.join(resourcesDir, 'python-bundle'), /\.(so|dylib)$/))

  console.log(`[sign-resources] signing ${innerBinaries.length} bundled binaries...`)
  for (const bin of innerBinaries) {
    sign(bin, entInherit, path.relative(context.appOutDir, bin))
  }

  // ── 2. Sign Electron frameworks and helper apps ───────────────────
  //    These are already signed by the Electron project, but their
  //    signatures may be invalid after electron-builder modifies them
  //    (arch stripping, etc). Re-sign ad-hoc so the outer .app seal
  //    is consistent.
  if (fs.existsSync(frameworksDir)) {
    const entries = fs.readdirSync(frameworksDir)
    // Sign frameworks first
    for (const name of entries) {
      if (name.endsWith('.framework')) {
        const fw = path.join(frameworksDir, name)
        sign(fw, null, name)
      }
    }
    // Then helper apps
    for (const name of entries) {
      if (name.endsWith('.app')) {
        const helper = path.join(frameworksDir, name)
        sign(helper, entInherit, name)
      }
    }
  }

  // ── 3. Sign the outer .app bundle (must be last) ──────────────────
  console.log(`[sign-resources] signing ${appName}.app...`)
  sign(appPath, entMain, `${appName}.app (ad-hoc)`)

  // Verify
  try {
    const out = execSync(
      `codesign --verify --deep --strict "${appPath}" 2>&1`,
      { encoding: 'utf8' },
    )
    console.log('[sign-resources] verification passed')
    if (out.trim()) console.log(`  ${out.trim()}`)
  } catch (err) {
    // Non-fatal — ad-hoc signed apps may warn but still work
    const msg = err.stdout || err.stderr || err.message || ''
    console.warn(`[sign-resources] verification: ${msg.slice(0, 300)}`)
  }

  console.log('[sign-resources] done')
}
