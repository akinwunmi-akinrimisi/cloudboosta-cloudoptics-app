// sitemap.xml with lastmod from git (docs/14 §8.5): each URL's date is the last commit that
// touched its page or the data it is built from. Without git history the lastmod is left out.
import { execFileSync } from 'node:child_process'
import type { APIRoute } from 'astro'

const PAGES: Record<string, string[]> = {
  '/': ['src/pages/index.astro', 'src/data'],
  '/how-it-works': ['src/pages/how-it-works.astro', 'src/data', '../../infra/cloudformation/cloudoptics-access.yaml'],
  '/security': ['src/pages/security.astro', 'src/data', '../../infra/cloudformation/cloudoptics-access.yaml'],
}

function lastmod(paths: string[]): string | null {
  try {
    const out = execFileSync('git', ['log', '-1', '--format=%cI', '--', ...paths], { encoding: 'utf8' }).trim()
    return out ? out.slice(0, 10) : null
  } catch {
    return null
  }
}

export const GET: APIRoute = ({ site }) => {
  const urls = Object.entries(PAGES).map(([path, sources]) => {
    const date = lastmod(sources)
    const loc = new URL(path, site).href
    return `  <url><loc>${loc}</loc>${date ? `<lastmod>${date}</lastmod>` : ''}</url>`
  })
  const xml = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls.join('\n')}\n</urlset>\n`
  return new Response(xml, { headers: { 'Content-Type': 'application/xml' } })
}
