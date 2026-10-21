import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import { AppErrorBoundary } from './components/AppErrorBoundary'
import { useHarness } from './state/store'
import { initTheme } from './lib/theme'
import 'katex/dist/katex.min.css'
import './styles/globals.css'
import './styles/work.css'

// Resolve dark/light before the first paint — the tokens in globals.css
// key off <html data-theme>, so this must land before React renders.
initTheme()

// Expose store for dev tooling / screenshots
;(window as any).__harnessStore = useHarness

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <App />
    </AppErrorBoundary>
  </React.StrictMode>,
)
