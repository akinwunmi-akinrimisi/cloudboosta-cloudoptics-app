// First-party site analytics (SITE-15, docs/14 §8.4; founder decision: no ads or pixels).
//
// Sends anonymous page views and call-to-action clicks to our own API. No cookies, no
// localStorage or sessionStorage, no identifier and no fingerprint: each request carries only
// the path, the referring host, the campaign (utm_*, ref) and, for a click, the CTA name.
// Nothing is sent when the browser asks not to be tracked (Do Not Track, Global Privacy
// Control), under /demo/, or when the page or an element on it has data-no-analytics.
//
// Attribution through sign-up: on an entry page that has a campaign (or an outside
// referrer), the campaign and the landing path are copied onto this site's links, so they
// travel in the URL to the app's sign-in. The app sends them once with the new workspace.

export const CAMPAIGN_KEYS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'ref']
const CARRY_KEYS = [...CAMPAIGN_KEYS, 'lp']
const DEMO_PATH = /^\/demo(\/|$)/
const camel = { utm_source: 'utmSource', utm_medium: 'utmMedium', utm_campaign: 'utmCampaign', utm_content: 'utmContent', utm_term: 'utmTerm', ref: 'ref' }

/** True when this page may send beacons. */
export function allowed(win) {
  const nav = win.navigator
  const doc = win.document
  return !(
    !doc.querySelector('meta[name="cloudoptics:beacon"]') ||
    DEMO_PATH.test(win.location.pathname) ||
    doc.querySelector('[data-no-analytics]') ||
    nav.doNotTrack === '1' ||
    win.doNotTrack === '1' ||
    nav.globalPrivacyControl === true
  )
}

/** The referring host, or '' for none, or the same host for a visit from this site. */
export function referrerHost(referrer) {
  try {
    return referrer ? new URL(referrer).hostname : ''
  } catch {
    return ''
  }
}

/** The CTA a link stands for: data-cta, else start, sign_in, demo or template; '' if none. */
export function ctaFor(link, app, demo) {
  if (link.dataset.cta) return link.dataset.cta
  const u = new URL(link.href)
  if ((demo && u.href.startsWith(demo)) || (u.origin === link.ownerDocument.location.origin && DEMO_PATH.test(u.pathname))) return 'demo'
  if (u.pathname === `${app}/sign-in` || u.pathname.startsWith(`${app}/sign-in/`)) return u.searchParams.get('intent') === 'signup' ? 'start' : 'sign_in'
  if (u.pathname.startsWith('/cfn/')) return 'template'
  return ''
}

/** Params to carry onto this site's links: the campaign and landing path of the first touch. */
export function carried(search, refHost, ownHost, path) {
  const now = new URLSearchParams(search)
  const out = new URLSearchParams()
  for (const k of CARRY_KEYS) if (now.get(k)) out.set(k, now.get(k).slice(0, k === 'lp' ? 200 : 100))
  if (refHost !== ownHost && !out.has('lp')) {
    if (!CAMPAIGN_KEYS.some((k) => out.has(k))) {
      if (!refHost) return out
      out.set('ref', refHost)
    }
    out.set('lp', path)
  }
  return out
}

/** Adds the carried params to links on this origin (not downloads, the demo or same-page anchors). */
export function decorate(doc, params) {
  if (!params.toString()) return
  const loc = doc.location
  for (const a of doc.querySelectorAll('a[href]')) {
    const u = new URL(a.href)
    if (u.origin !== loc.origin || u.pathname.startsWith('/cfn/') || DEMO_PATH.test(u.pathname)) continue
    if (u.hash && u.pathname === loc.pathname) continue
    for (const [k, v] of params) if (!u.searchParams.has(k)) u.searchParams.set(k, v)
    a.href = u.href
  }
}

/** The JSON body of one beacon. */
export function payload(kind, loc, refHost, extra = {}) {
  const params = new URLSearchParams(loc.search)
  const body = { kind, path: loc.pathname.slice(0, 200), ...extra }
  if (kind === 'page_view') {
    body.landing = refHost !== loc.hostname
    if (body.landing && refHost) body.referrerHost = refHost
  }
  for (const k of CAMPAIGN_KEYS) {
    const v = params.get(k)
    if (v) body[camel[k]] = v.slice(0, 100)
  }
  return JSON.stringify(body)
}

export function start(win) {
  if (!allowed(win)) return
  const doc = win.document
  const meta = doc.querySelector('meta[name="cloudoptics:beacon"]')
  const url = meta.content
  const app = meta.dataset.app ?? ''
  const demo = meta.dataset.demo ?? ''
  const loc = win.location
  const ref = referrerHost(doc.referrer)
  // text/plain keeps it a simple request (no preflight); no credentials are ever sent.
  const send = (kind, extra) =>
    win
      .fetch(url, { method: 'POST', body: payload(kind, loc, ref, extra), keepalive: true, credentials: 'omit', headers: { 'content-type': 'text/plain' } })
      .catch(() => {})
  decorate(doc, carried(loc.search, ref, loc.hostname, loc.pathname))
  send('page_view')
  doc.addEventListener(
    'click',
    (event) => {
      const link = event.target instanceof win.Element ? event.target.closest('a[href]') : null
      const cta = link && ctaFor(link, app, demo)
      if (cta) send('cta_click', { cta })
    },
    true,
  )
}
