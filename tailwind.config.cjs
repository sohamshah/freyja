/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/renderer/**/*.{html,ts,tsx,js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg: {
          0: '#0a0a0a',
          1: '#121212',
          2: '#181818',
          3: '#1e1e1e',
          4: '#262626',
        },
        fg: {
          0: '#e8e8e8',
          1: '#a8a8a8',
          2: '#6e6e6e',
          3: '#4a4a4a',
          4: '#303030',
        },
        accent: {
          DEFAULT: '#a8d4fc',
          hi: '#c4e0fc',
          lo: '#7aafea',
        },
        // Status colors are deliberately muted toward greyscale.
        // Steel-blue accent is the only saturated color in the palette.
        ok: '#a8b0a8',
        warn: '#b8a078',
        danger: '#b48282',
        mention: '#a8d4fc',
      },
      fontFamily: {
        // Geist Mono is the single primary face. Departure Mono is kept
        // only as a deeper fallback for legacy surfaces; nothing new
        // should opt into it.
        sans: [
          '"Geist Mono"',
          'ui-monospace',
          '"SF Mono"',
          '"JetBrains Mono"',
          'Menlo',
          'Monaco',
          'monospace',
        ],
        mono: [
          '"Geist Mono"',
          'ui-monospace',
          '"SF Mono"',
          '"JetBrains Mono"',
          'Menlo',
          'Monaco',
          'monospace',
        ],
        display: [
          '"Geist Mono"',
          'ui-monospace',
          '"SF Mono"',
          '"JetBrains Mono"',
          'Menlo',
          'Monaco',
          'monospace',
        ],
        // Editorial serif — only for the mission objective h1 in each
        // view header and the drawer / brief memo title. Used sparingly.
        serif: [
          'Fraunces',
          'Georgia',
          'serif',
        ],
      },
      fontSize: {
        '2xs': ['10px', '14px'],
      },
      boxShadow: {
        'inset-hairline': 'inset 0 0 0 1px rgba(255,255,255,0.06)',
        'inset-hairline-strong': 'inset 0 0 0 1px rgba(255,255,255,0.12)',
        'glow-accent': '0 0 0 1px rgba(168,212,252,0.35), 0 8px 24px rgba(168,212,252,0.08)',
      },
      animation: {
        'pulse-soft': 'pulse-soft 2s ease-in-out infinite',
        'shimmer': 'shimmer 2.6s linear infinite',
        'caret-blink': 'caret-blink 1.1s steps(2) infinite',
        'fade-in': 'fade-in 220ms ease-out',
      },
      keyframes: {
        'pulse-soft': {
          '0%, 100%': { opacity: '0.55' },
          '50%': { opacity: '1' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        'caret-blink': {
          '0%, 50%': { opacity: '1' },
          '51%, 100%': { opacity: '0' },
        },
        'fade-in': {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
}
