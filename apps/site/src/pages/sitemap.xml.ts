// sitemap.xml with lastmod from git (docs/14 §8.5): each URL's date is the last commit that
// touched its page or the data it is built from. Without git history the lastmod is left out.
// Every .astro page is listed except 404 and pages marked noindex, so a new page cannot be
// forgotten here.
import { execFileSync } from 'node:child_process'
import type { APIRoute } from 'astro'

// Each page's source text, to read its noindex flag.
const pageSources = import.meta.glob<string>('./**/*.astro', { query: '?raw', import: 'default', eager: true })
// Pages built from more than their own file and src/data.
const EXTRA_SOURCES: Record<string, string[]> = {
  '/how-it-works': ['../../infra/cloudformation/cloudoptics-access.yaml'],
  '/security': ['../../infra/cloudformation/cloudoptics-access.yaml'],
}

function urlPath(file: string): string {
  const p = file.replace(/^\.\//, '/').replace(/\.astro$/, '').replace(/\/index$/, '')
  return p === '' || p === '/index' ? '/' : p
}

function indexable([file, source]: [string, string]): boolean {
  if (/(^|\/)(404|500)\.astro$/.test(file) || file.includes('[')) return false
  return !/<Base\b[^>]*\bnoindex\b/.test(source)
}

function lastmod(paths: string[]): string | null {
  try {
    const out = execFileSync('git', ['log', '-1', '--format=%cI', '--', ...paths], { encoding: 'utf8' }).trim()
    return out ? out.slice(0, 10) : null
  } catch {
    return null
  }
}

const PAGES: Record<string, string[]> = Object.fromEntries(
  Object.entries(pageSources)
    .filter(indexable)
    .map(([file]): [string, string[]] => {
      const path = urlPath(file)
      return [path, [`src/pages/${file.slice(2)}`, 'src/data', ...(EXTRA_SOURCES[path] ?? [])]]
    })
    .sort(([a], [b]) => (a === '/' ? -1 : b === '/' ? 1 : a.localeCompare(b))),
)

export const GET: APIRoute = ({ site }) => {
  const urls = Object.entries(PAGES).map(([path, sources]) => {
    const date = lastmod(sources)
    const loc = new URL(path, site).href
    return `  <url><loc>${loc}</loc>${date ? `<lastmod>${date}</lastmod>` : ''}</url>`
  })
  const xml = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls.join('\n')}\n</urlset>\n`
  return new Response(xml, { headers: { 'Content-Type': 'application/xml' } })
}
