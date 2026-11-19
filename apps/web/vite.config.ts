import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import type { Plugin } from 'vite'
import { defineConfig } from 'vitest/config'

// The demo (VITE_API_MODE=demo, served at /demo/ and mirrored on GitHub Pages) is sample data:
// search engines must not index it (SITE-14).
const demoNoindex = (): Plugin => ({
  name: 'cloudoptics-demo-noindex',
  transformIndexHtml: () =>
    process.env.VITE_API_MODE === 'demo'
      ? [{ tag: 'meta', attrs: { name: 'robots', content: 'noindex, nofollow' }, injectTo: 'head' }]
      : [],
})

export default defineConfig({
  // Where the build is served (docs/14 §7.2): /app/ on staging, /demo/ for the demo, the
  // repository path on GitHub Pages, / in development and tests.
  base: process.env.VITE_BASE ?? '/',
  plugins: [react(), tailwindcss(), demoNoindex()],
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
