// Build-time settings. Defaults follow hosting layout H1 (ADR-016, built in SITE-13): this site
// at /, the app under /app and the demo under /demo, on one origin.
import claimsJson from './data/claims.json'
import permissionsJson from './generated/permissions.json'

const env = import.meta.env as Record<string, string | undefined>

export const APP_PATH = (env.SITE_APP_PATH ?? '/app').replace(/\/$/, '')
export const SIGN_IN_URL = `${APP_PATH}/sign-in`
export const START_URL = `${APP_PATH}/sign-in?intent=signup`
// The demo's sample workspace (sample data, no analytics, noindex; SITE-14), on this origin.
export const DEMO_URL = env.SITE_DEMO_URL ?? '/demo/'
export const TEMPLATE_URL = '/cfn/cloudoptics-access.yaml'
// The API on this origin (layout H1: /api). The site's first-party beacon posts there
// (SITE-15); SITE_ANALYTICS=off builds the site without it.
export const API_URL = (env.SITE_API_URL ?? '/api').replace(/\/$/, '')
export const BEACON_URL = env.SITE_ANALYTICS === 'off' ? '' : `${API_URL}/v1/site/visits`
// Search Console and Bing Webmaster Tools verification (owner action, docs/analytics.md):
// set these at build time to the tokens each tool gives; empty leaves the tag out.
export const GOOGLE_SITE_VERIFICATION = env.SITE_GOOGLE_VERIFICATION ?? ''
export const BING_SITE_VERIFICATION = env.SITE_BING_VERIFICATION ?? ''

type Claims = typeof claimsJson
export const claims = Object.fromEntries(
  Object.entries(claimsJson).filter(([k]) => k !== '$comment').map(([k, v]) => [k, (v as { value: unknown }).value]),
) as { [K in Exclude<keyof Claims, '$comment'>]: Claims[K] extends { value: infer V } ? V : never }

export const permissions = permissionsJson
export const facts = permissionsJson.facts
export const readRoles = permissionsJson.roles.filter((r) => r.readOnly)
export const billingRole = readRoles.find((r) => r.logicalId === 'BillingRole')!
export const resourceRole = readRoles.find((r) => r.logicalId === 'ResourceRole')!
export const setupRole = permissionsJson.roles.find((r) => r.logicalId === 'SetupRole')!
export const sessionHours = Math.max(...readRoles.map((r) => r.maxSessionSeconds ?? 0)) / 3600
