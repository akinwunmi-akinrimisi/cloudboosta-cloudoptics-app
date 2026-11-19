// The public Cloudoptics site (ADR-016, docs/14 §7.1 option C): static HTML per route, no
// JavaScript by default, Tailwind v4 reading the tokens shared with apps/web.
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'astro/config'
import stripHtmlComments from './scripts/strip-html-comments.mjs'

export default defineConfig({
  // Absolute URLs (canonical, sitemap, Open Graph) come from SITE_URL, so a domain change is
  // configuration (docs/14 §7.2).
  site: process.env.SITE_URL || 'https://cloudoptics.cloudboosta.com',
  output: 'static',
  trailingSlash: 'ignore',
  build: { format: 'directory', inlineStylesheets: 'never' },
  compressHTML: true,
  devToolbar: { enabled: false },
  integrations: [stripHtmlComments()],
  vite: {
    plugins: [tailwindcss()],
    // Scripts and styles ship as files, never inline, so the page CSP can stay at 'self'.
    build: { assetsInlineLimit: 0 },
  },
})
