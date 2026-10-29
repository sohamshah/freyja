import React from 'react'
import ReactDOM from 'react-dom/client'
import { applyEffectiveTheme } from '../lib/theme'
import { Island } from './Island'
import '../styles/globals.css'
import '../styles/island.css'

// The island is pitch black in both shells, so it fuses with the notch: it
// pins the dark tokens whatever the app's appearance is, and never follows
// the media query the app follows.
applyEffectiveTheme('dark')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Island />
  </React.StrictMode>,
)
