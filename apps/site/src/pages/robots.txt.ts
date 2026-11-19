// robots.txt (docs/14 §8.5): the app, the demo and the API are not for search engines.
import type { APIRoute } from 'astro'
import { APP_PATH } from '../config'

export const GET: APIRoute = ({ site }) => {
  const disallow = [`${APP_PATH || '/app'}/`, '/demo/', '/api/']
  const body = ['User-agent: *', ...disallow.map((p) => `Disallow: ${p}`), '', `Sitemap: ${new URL('/sitemap.xml', site).href}`, '']
  return new Response(body.join('\n'), { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
}
