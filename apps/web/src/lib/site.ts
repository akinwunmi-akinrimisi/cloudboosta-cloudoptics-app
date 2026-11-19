// Links from the app to the public site and the demo (docs/14 §4.3, §6.3, §6.6). The site is
// served at / on the same origin as the app (/app/, SITE-13), so the default site base is '/'
// and links are root-relative: /how-it-works, never /app/how-it-works. They are plain <a>
// links, so the router's basepath is not added. Set VITE_SITE_URL (an absolute address) where
// the build is not on the Cloudoptics origin, such as the GitHub Pages mirror.
const base = (import.meta.env.VITE_SITE_URL ?? '/').replace(/\/+$/, '')

/** A page on the public site, e.g. siteUrl('/security#permissions'). */
export function siteUrl(path: string): string {
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

/** The same page as an absolute URL, for text that leaves the app (a message to a colleague). */
export function absoluteSiteUrl(path: string): string {
  return new URL(siteUrl(path), window.location.origin).href
}

/** The sample workspace (SITE-14 serves it at /demo/); defined with the other build paths. */
export { DEMO_URL } from '../app/paths'

export const HOW_IT_WORKS_STACK = '/how-it-works#what-the-stack-creates'
export const SECURITY_PERMISSIONS = '/security#permissions'
