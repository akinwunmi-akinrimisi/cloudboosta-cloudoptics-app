import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  // GitHub Pages serves the demo from /<repo>/; everything else from /.
  base: process.env.VITE_BASE ?? '/',
  plugins: [react(), tailwindcss()],
  // Development only: VITE_API_URL=/api (Codespaces, see scripts/dev-start.sh) sends API
  // calls through this server, so the API port can stay private. App Platform routes /api
  // the same way in staging.
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8000', rewrite: (path) => path.replace(/^\/api/, '') },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
  },
})
