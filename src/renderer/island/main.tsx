import React from 'react'
import ReactDOM from 'react-dom/client'
import { initTheme } from '../lib/theme'
import { Island } from './Island'
import '../styles/globals.css'
import '../styles/island.css'

// The island follows the app's theme through `prefers-color-scheme`, which
// mirrors nativeTheme.themeSource, so the same init as the app gives the
// right `data-theme` before the first paint.
initTheme()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Island />
  </React.StrictMode>,
)
