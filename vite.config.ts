import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  root: 'src/renderer',
  base: './',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src/renderer'),
      '@shared': path.resolve(__dirname, 'src/shared'),
    },
  },
  build: {
    outDir: path.resolve(__dirname, 'dist-renderer'),
    emptyOutDir: true,
    target: 'esnext',
    sourcemap: true,
    // Two pages: the app and the island (the notch-hugging mini view).
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, 'src/renderer/index.html'),
        island: path.resolve(__dirname, 'src/renderer/island.html'),
      },
    },
  },
  server: {
    port: 5179,
    strictPort: true,
  },
})
