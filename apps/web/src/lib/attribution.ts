// First-touch attribution through sign-up (SITE-15, docs/14 §8.4). The public site passes
// the campaign (utm_*, ref) and its landing path (lp) in the URL to the app's sign-in. The app
// keeps them in sessionStorage for this tab only, so they survive the WorkOS sign-in round
// trip, and sends them once with the new workspace (HttpApi.createTenant). Nothing is kept
// when the browser asks not to be tracked, and nothing identifies the visitor.
import { read, write } from './storage'

export interface Attribution {
  source?: string
  medium?: string
  campaign?: string
  content?: string
  term?: string
  ref?: string
  landingPath?: string
}

const KEY = 'co.attribution'
const PARAMS: [string, keyof Attribution, number][] = [
  ['utm_source', 'source', 100],
  ['utm_medium', 'medium', 100],
  ['utm_campaign', 'campaign', 100],
  ['utm_content', 'content', 100],
  ['utm_term', 'term', 100],
  ['ref', 'ref', 100],
  ['lp', 'landingPath', 200],
]

function notTracked(): boolean {
  const nav = navigator as Navigator & { globalPrivacyControl?: boolean }
  return nav.doNotTrack === '1' || nav.globalPrivacyControl === true
}

/** Keeps the campaign in this URL, unless one is already kept (first touch wins). */
export function captureAttribution(search: string = window.location.search): void {
  if (notTracked() || read('session', KEY) !== null) return
  const params = new URLSearchParams(search)
  const found: Attribution = {}
  for (const [param, field, max] of PARAMS) {
    const value = params.get(param)?.trim()
    if (value) found[field] = value.slice(0, max)
  }
  if (found.source || found.ref || found.campaign || found.medium) write('session', KEY, JSON.stringify(found))
}

/** The kept campaign, if any. */
export function readAttribution(): Attribution | undefined {
  const raw = read('session', KEY)
  if (!raw) return undefined
  try {
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object') return undefined
    const out: Attribution = {}
    for (const [, field] of PARAMS) {
      const v = (value as Record<string, unknown>)[field]
      if (typeof v === 'string') out[field] = v
    }
    return out
  } catch {
    return undefined
  }
}

/** Forgets the campaign once a workspace has been created with it. */
export function clearAttribution(): void {
  write('session', KEY, null)
}
