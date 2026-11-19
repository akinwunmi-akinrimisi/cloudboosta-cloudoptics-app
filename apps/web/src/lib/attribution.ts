// Where a visitor came from (docs/14 §4.3, §7.5): "Start free" on the site links to
// /sign-in?intent=signup with utm_*, ref and the landing path. The app keeps them for the tab
// (session storage, no cookies) and sends them through the WorkOS round trip as AuthKit state,
// so they are still known after sign-up, when workspace creation reports them (SITE-15).
import { read, write } from './storage'

const KEY = 'co.attribution'
export const ATTRIBUTION_KEYS = [
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_term',
  'utm_content',
  'ref',
  'landing',
] as const
export type Attribution = Partial<Record<(typeof ATTRIBUTION_KEYS)[number], string>>

export interface SignInSearch extends Attribution {
  intent?: 'signup'
}

/** The sign-in route's search: intent=signup plus attribution, short strings only. */
export function parseSignInSearch(search: Record<string, unknown>): SignInSearch {
  const out: SignInSearch = pickAttribution(search)
  if (search.intent === 'signup') out.intent = 'signup'
  return out
}

export function pickAttribution(source: Record<string, unknown> | null | undefined): Attribution {
  const out: Attribution = {}
  if (!source || typeof source !== 'object') return out
  for (const key of ATTRIBUTION_KEYS) {
    const value = source[key]
    if (typeof value === 'string' && value.trim() && value.length <= 200) out[key] = value.trim()
  }
  return out
}

/** Keeps the latest non-empty attribution for this tab. */
export function rememberAttribution(attribution: Attribution): void {
  if (Object.keys(attribution).length) write('session', KEY, JSON.stringify(attribution))
}

export function readAttribution(): Attribution {
  const raw = read('session', KEY)
  if (!raw) return {}
  try {
    return pickAttribution(JSON.parse(raw) as Record<string, unknown>)
  } catch {
    return {}
  }
}
