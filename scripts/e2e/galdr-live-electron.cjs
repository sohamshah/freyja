// Hidden Electron window for `scripts/e2e_galdr_live.py --electron`: runs
// the harness page on the app's own Chromium instead of system Chrome.
// No capture device is touched (the harness feeds a WebAudio "mic"), so
// macOS never asks for microphone access.
const { app, BrowserWindow } = require('electron')

app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required')
if (app.dock) app.dock.hide()

app.whenReady().then(() => {
  const win = new BrowserWindow({ show: false, webPreferences: { backgroundThrottling: false } })
  win.loadURL(process.env.HARNESS_URL)
})
