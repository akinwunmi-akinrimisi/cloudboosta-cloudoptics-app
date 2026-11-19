// Checks the built site in dist/ (docs/14 §7.3, §8.3, §8.5; SITE-03). Fails on:
//   - a broken internal link or #anchor (the app under /app and the demo under /demo are
//     other builds)
//   - a page over its budget: HTML + CSS ≤ 60 KB and JS ≤ 10 KB gzipped on the landing and 404
//     pages, ≤ 1.5 KB elsewhere (the first-party beacon only, SITE-15), any AVIF image ≤ 100 KB,
//     landing page total ≤ 500 KB
//   - an HTML comment left in a page
//   - missing SEO basics: one <h1>, lang, title ≤ 60, description ≤ 155, absolute canonical,
//     Open Graph and Twitter tags with an absolute image, JSON-LD that parses (Organization on
//     every page, FAQPage wherever the page shows an FAQ); a 404.html marked noindex; every
//     indexable page in sitemap.xml; robots.txt keeping out /app/, /demo/ and /api/
//   - analytics that is not first-party: a script, image or beacon URL on another host
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'
import { JSDOM } from 'jsdom'
import { COMMENT, htmlFiles } from './strip-html-comments.mjs'

const dist = join(import.meta.dirname, '..', 'dist')
const APP_PATH = (process.env.SITE_APP_PATH ?? '/app').replace(/\/$/, '')
const KB = 1024
const BUDGET = { critical: 60 * KB, js: 10 * KB, beacon: 1.5 * KB, image: 100 * KB, landing: 500 * KB }
// The 404 page carries the small redirect for old app paths (public/legacy-redirect.js).
const JS_ALLOWED = new Set(['/', '/404.html'])
// Served by other components on the same origin (infra/digitalocean/app.yaml), not this build.
const OTHER_BUILDS = [APP_PATH, '/demo'].filter(Boolean)

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

const noindexed = (doc) => Boolean(doc.querySelector('meta[name="robots"][content*="noindex"]'))
const sitemap = existsSync(join(dist, 'sitemap.xml')) ? readFileSync(join(dist, 'sitemap.xml'), 'utf8') : ''
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
    if (OTHER_BUILDS.some((p) => bare === p || bare.startsWith(`${p}/`))) continue
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
  const meta = (sel) => doc.querySelector(`meta[${sel}]`)?.getAttribute('content') ?? ''
  for (const tag of ['property="og:title"', 'property="og:description"', 'property="og:url"', 'name="twitter:card"', 'name="twitter:title"']) {
    if (!meta(tag)) problems.push(`${url}: missing <meta ${tag}>`)
  }
  for (const tag of ['property="og:image"', 'name="twitter:image"']) {
    const image = meta(tag)
    if (!image.startsWith('https://')) problems.push(`${url}: <meta ${tag}> must be an absolute https URL`)
    else if (!fileFor(new URL(image).pathname)) problems.push(`${url}: <meta ${tag}> points at a missing file`)
  }
  const types = [...doc.querySelectorAll('script[type="application/ld+json"]')].map((s) => {
    try {
      return JSON.parse(s.textContent)['@type']
    } catch {
      return null
    }
  })
  if (!types.includes('Organization')) problems.push(`${url}: no Organization JSON-LD`)
  if (doc.querySelector('details[id] > summary') && !types.includes('FAQPage') && !url.startsWith('/404')) problems.push(`${url}: shows an FAQ without FAQPage JSON-LD`)
  for (const s of doc.querySelectorAll('script[src], img[src], iframe[src]')) {
    if (/^(https?:)?\/\//.test(s.getAttribute('src'))) problems.push(`${url}: loads ${s.getAttribute('src')} from another host`)
  }
  const beacon = meta('name="cloudoptics:beacon"')
  if (beacon && /^(https?:)?\/\//.test(beacon) && !beacon.startsWith('https://')) problems.push(`${url}: beacon URL must be https or same-origin`)
  if (!noindexed(doc) && pages.size && sitemap && !sitemap.includes(`<loc>${canonical}</loc>`)) problems.push(`${url}: not in sitemap.xml`)
  if (doc.querySelector('script:not([src]):not([type="application/ld+json"])')) problems.push(`${url}: inline script (the CSP allows 'self' only)`)
  if (doc.querySelector('[style]') || doc.querySelector('style')) problems.push(`${url}: inline style (the CSP allows 'self' only)`)

  // Budgets
  const css = [...doc.querySelectorAll('link[rel="stylesheet"]')].map((l) => fileFor(l.getAttribute('href'))).filter(Boolean)
  const js = [...doc.querySelectorAll('script[src]')].map((s) => fileFor(s.getAttribute('src'))).filter(Boolean)
  const critical = gz(file) + css.reduce((n, f) => n + gz(f), 0)
  const jsBytes = js.reduce((n, f) => n + gz(f), 0)
  const jsBudget = JS_ALLOWED.has(url) ? BUDGET.js : BUDGET.beacon
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
// /cfn/* for the whole origin comes from this build (scripts/copy-templates.mjs, SITE-13).
const templates = ['cloudoptics-access.yaml', 'billing-role.yaml', 'resource-role.yaml', 'resource-stackset.yaml', 'scheduler/cloudoptics-scheduler.yaml']
for (const required of ['404.html', 'robots.txt', 'sitemap.xml', 'permissions.json', 'legacy-redirect.js', 'og/cloudoptics.png', ...templates.map((t) => `cfn/${t}`)]) {
  if (!existsSync(join(dist, required))) problems.push(`missing ${required}`)
}

const robots = existsSync(join(dist, 'robots.txt')) ? readFileSync(join(dist, 'robots.txt'), 'utf8') : ''
for (const path of [`${APP_PATH || '/app'}/`, '/demo/', '/api/']) {
  if (!robots.includes(`Disallow: ${path}`)) problems.push(`robots.txt does not disallow ${path}`)
}
if (!/^Sitemap: https:\/\//m.test(robots)) problems.push('robots.txt has no absolute Sitemap line')

if (problems.length) {
  console.error(`\n${problems.length} problem(s):`)
  for (const p of problems) console.error(`  - ${p}`)
  process.exit(1)
}
console.log(`\nSite checks passed: ${pages.size} pages, links, budgets, no HTML comments, SEO basics.`)
