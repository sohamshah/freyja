import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import { useHarness } from './state/store'
import './styles/globals.css'

// Expose store for dev tooling / screenshots
;(window as any).__harnessStore = useHarness

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
