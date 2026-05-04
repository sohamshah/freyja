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
        ok: '#88d67f',
        warn: '#ffcc66',
        danger: '#ff6b6b',
        mention: '#79b3fa',
      },
      fontFamily: {
        // Every font class resolves to Departure Mono. We keep separate
        // aliases so future surfaces (e.g. long-form prose) can swap in a
        // different companion face without touching every component.
        sans: [
          '"Departure Mono"',
          '"SF Mono"',
          '"JetBrains Mono"',
          'Menlo',
          'Monaco',
          'monospace',
        ],
        mono: [
          '"Departure Mono"',
          '"SF Mono"',
          '"JetBrains Mono"',
          'Menlo',
          'Monaco',
          'monospace',
        ],
        display: [
          '"Departure Mono"',
          '"SF Mono"',
          '"JetBrains Mono"',
          'Menlo',
          'Monaco',
          'monospace',
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
