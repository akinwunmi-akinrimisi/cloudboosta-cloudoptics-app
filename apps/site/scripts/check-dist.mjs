// Checks the built site in dist/ (docs/14 §7.3, §8.3, §8.5; SITE-03). Fails on:
//   - a broken internal link or #anchor (links into the app under /app are outside this build)
//   - a page over its budget: HTML + CSS ≤ 60 KB and JS ≤ 10 KB gzipped (0 KB except the landing
//     page), any AVIF image ≤ 100 KB, landing page total ≤ 500 KB
//   - an HTML comment left in a page
//   - missing SEO basics: one <h1>, lang, title ≤ 60, description ≤ 155, absolute canonical,
//     JSON-LD that parses; a 404.html marked noindex
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'
import { JSDOM } from 'jsdom'
import { COMMENT, htmlFiles } from './strip-html-comments.mjs'

const dist = join(import.meta.dirname, '..', 'dist')
const APP_PATH = (process.env.SITE_APP_PATH ?? '/app').replace(/\/$/, '')
const KB = 1024
const BUDGET = { critical: 60 * KB, js: 10 * KB, image: 100 * KB, landing: 500 * KB }
const JS_ALLOWED = new Set(['/'])

const problems = []
const gz = (path) => gzipSync(readFileSync(path)).length
const size = (path) => statSync(path).size
const urlOf = (file) => {
  const rel = file.slice(dist.length).replaceAll('\\', '/')
  return rel.replace(/index\.html$/, '').replace(/\/$/, '') || '/'
}
const fileFor = (path) => {
  const p = decodeURIComponent(path).replace(/\/$/, '')
  for (const c of [p, `${p}/index.html`, `${p}.html`]) {
    const f = join(dist, c)
    if (c && existsSync(f) && statSync(f).isFile()) return f
  }
  return p === '' ? join(dist, 'index.html') : null
}

const pages = new Map()
for (const file of htmlFiles(dist)) {
  const html = readFileSync(file, 'utf8')
  pages.set(urlOf(file), { file, html, doc: new JSDOM(html).window.document })
}
const idsOf = (url) => new Set([...(pages.get(url)?.doc.querySelectorAll('[id]') ?? [])].map((e) => e.id))
const kb = (n) => `${(n / KB).toFixed(1)} KB`

for (const [url, { file, html, doc }] of pages) {
  // Comments
  const comments = html.match(COMMENT)
  if (comments) problems.push(`${url}: ${comments.length} HTML comment(s) left in the page`)

  // Links
  const refs = [
    ...[...doc.querySelectorAll('a[href]')].map((a) => a.getAttribute('href')),
    ...[...doc.querySelectorAll('link[href]')].map((l) => l.getAttribute('href')),
    ...[...doc.querySelectorAll('script[src], img[src]')].map((s) => s.getAttribute('src')),
    ...[...doc.querySelectorAll('[srcset]')].flatMap((s) => s.getAttribute('srcset').split(',').map((c) => c.trim().split(/\s+/)[0])),
  ]
  for (const ref of refs) {
    if (!ref || /^(https?:|mailto:|tel:)/.test(ref)) {
      if (ref?.startsWith('http://')) problems.push(`${url}: insecure link ${ref}`)
      continue
    }
    const [path, hash] = ref.split('#')
    const bare = path.split('?')[0]
    if (APP_PATH && (bare === APP_PATH || bare.startsWith(`${APP_PATH}/`))) continue
    const target = bare === '' ? url : bare.replace(/\/$/, '') || '/'
    if (bare !== '' && !fileFor(bare)) {
      problems.push(`${url}: broken link ${ref}`)
      continue
    }
    if (hash && pages.has(target) && !idsOf(target).has(hash)) problems.push(`${url}: link ${ref} points at a missing #${hash}`)
  }

  if (url === '/404.html' || url === '/404') {
    if (!doc.querySelector('meta[name="robots"][content*="noindex"]')) problems.push('404.html must be noindex')
  }

  // SEO basics
  const title = doc.querySelector('title')?.textContent ?? ''
  const desc = doc.querySelector('meta[name="description"]')?.getAttribute('content') ?? ''
  const canonical = doc.querySelector('link[rel="canonical"]')?.getAttribute('href') ?? ''
  if (!doc.documentElement.getAttribute('lang')) problems.push(`${url}: <html> has no lang`)
  if (doc.querySelectorAll('h1').length !== 1) problems.push(`${url}: needs exactly one <h1>`)
  if (!title || title.length > 60) problems.push(`${url}: title missing or over 60 characters`)
  if (!desc || desc.length > 155) problems.push(`${url}: description missing or over 155 characters`)
  if (!canonical.startsWith('https://')) problems.push(`${url}: canonical must be an absolute https URL`)
  for (const ld of doc.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      JSON.parse(ld.textContent)
    } catch {
      problems.push(`${url}: JSON-LD does not parse`)
    }
  }
  if (doc.querySelector('script:not([src]):not([type="application/ld+json"])')) problems.push(`${url}: inline script (the CSP allows 'self' only)`)
  if (doc.querySelector('[style]') || doc.querySelector('style')) problems.push(`${url}: inline style (the CSP allows 'self' only)`)

  // Budgets
  const css = [...doc.querySelectorAll('link[rel="stylesheet"]')].map((l) => fileFor(l.getAttribute('href'))).filter(Boolean)
  const js = [...doc.querySelectorAll('script[src]')].map((s) => fileFor(s.getAttribute('src'))).filter(Boolean)
  const critical = gz(file) + css.reduce((n, f) => n + gz(f), 0)
  const jsBytes = js.reduce((n, f) => n + gz(f), 0)
  const jsBudget = JS_ALLOWED.has(url) ? BUDGET.js : 0
  if (critical > BUDGET.critical) problems.push(`${url}: HTML + CSS ${kb(critical)} gzipped, budget ${kb(BUDGET.critical)}`)
  if (jsBytes > jsBudget) problems.push(`${url}: JavaScript ${kb(jsBytes)} gzipped, budget ${kb(jsBudget)}`)
  // Worst case for images: the largest candidate of each <picture>/<img>.
  const images = [...doc.querySelectorAll('picture, img:not(picture img)')].map((el) => {
    const cands = [...el.querySelectorAll('source[srcset]'), ...(el.matches('img') ? [el] : el.querySelectorAll('img'))]
      .flatMap((s) => (s.getAttribute('srcset') ?? s.getAttribute('src') ?? '').split(',').map((c) => c.trim().split(/\s+/)[0]))
      .map(fileFor)
      .filter(Boolean)
    return Math.max(0, ...cands.map(size))
  })
  const total = critical + jsBytes + images.reduce((a, b) => a + b, 0)
  if (url === '/' && total > BUDGET.landing) problems.push(`/: total page weight ${kb(total)}, budget ${kb(BUDGET.landing)}`)
  console.log(`${url.padEnd(16)} HTML+CSS ${kb(critical).padStart(8)}  JS ${kb(jsBytes).padStart(7)}  images ${kb(images.reduce((a, b) => a + b, 0)).padStart(8)}  total ${kb(total).padStart(8)}`)
}

if (pages.size === 0) problems.push('no HTML pages in dist; run the build first')
const avif = readdirSync(join(dist, '_astro')).filter((f) => f.endsWith('.avif'))
for (const f of avif) if (size(join(dist, '_astro', f)) > BUDGET.image) problems.push(`image ${f} is ${kb(size(join(dist, '_astro', f)))}, budget ${kb(BUDGET.image)}`)
for (const required of ['404.html', 'robots.txt', 'sitemap.xml', 'permissions.json', 'cfn/cloudoptics-access.yaml', 'og/cloudoptics.png']) {
  if (!existsSync(join(dist, required))) problems.push(`missing ${required}`)
}

if (problems.length) {
  console.error(`\n${problems.length} problem(s):`)
  for (const p of problems) console.error(`  - ${p}`)
  process.exit(1)
}
console.log(`\nSite checks passed: ${pages.size} pages, links, budgets, no HTML comments, SEO basics.`)
