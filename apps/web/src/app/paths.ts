// Where this build is served (docs/14 §7.2, SITE-13). Staging and production serve the app
// under /app/ (VITE_BASE=/app/) next to the public site at / and the demo at /demo/; local
// development and tests serve it from /.

/** The router's base path: the app's base without its trailing slash, or '/'. */
export function routerBasepath(mode: string | undefined, base: string): string {
  // The demo routes in the URL hash (static hosts cannot rewrite deep links), so its routes
  // never include the base path.
  if (mode === 'demo') return '/'
  return base.replace(/\/+$/, '') || '/'
}

/** Where WorkOS sends people back after sign-in: the app's root, e.g. https://host/app/. */
export function authRedirectUri(origin: string, base: string): string {
  return `${origin}${base.endsWith('/') ? base : `${base}/`}`
}

/**
 * "Start free" from the demo's sample-data banner (SITE-14). Same origin by default; the
 * GitHub Pages mirror sets VITE_SIGNUP_URL to the absolute address.
 */
export const SIGNUP_URL: string = import.meta.env.VITE_SIGNUP_URL ?? '/app/sign-in?intent=signup'

/** The public demo, linked from sign-in while sign-in is being set up. */
export const DEMO_URL: string = import.meta.env.VITE_DEMO_URL ?? '/demo/'
