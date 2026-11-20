import type { QueryClient } from '@tanstack/react-query'
import {
  createHashHistory,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  lazyRouteComponent,
  Outlet,
  redirect,
  type RouterHistory,
} from '@tanstack/react-router'

import type { Api, Me } from '../api/types'
import { parseSignInSearch } from '../lib/attribution'
import { SignIn } from '../pages/SignIn'
import { isSection, type SettingsSection } from '../pages/SettingsNav'
import { Connections, Overview } from '../pages/Workspace'
import { AppShell } from './AppShell'
import { routerBasepath } from './paths'

// Route-level code splitting (FR-UI-10 / NFR-PERF-03): sign-in, Overview and Connections are
// in the first download; every other page is its own chunk, fetched when its route loads (or
// on hover/focus of a link to it, defaultPreload 'intent').
const Onboarding = lazyRouteComponent(() => import('../pages/Onboarding'), 'Onboarding')
const Costs = lazyRouteComponent(() => import('../pages/Costs'), 'Costs')
const Recommendations = lazyRouteComponent(() => import('../pages/Recommendations'), 'Recommendations')
const RecommendationDetail = lazyRouteComponent(() => import('../pages/Recommendations'), 'RecommendationDetail')
const Reports = lazyRouteComponent(() => import('../pages/Reports'), 'Reports')
const Settings = lazyRouteComponent(() => import('../pages/Settings'), 'Settings')
const Staff = lazyRouteComponent(() => import('../pages/Staff'), 'Staff')
const Anomalies = lazyRouteComponent(() => import('../pages/Anomalies'), 'Anomalies')
const Budgets = lazyRouteComponent(() => import('../pages/Budgets'), 'Budgets')
const Commitments = lazyRouteComponent(() => import('../pages/Commitments'), 'Commitments')

/** For routes whose component wraps a lazy page: load the page's chunk with the route. */
const preload = (page: { preload?: () => Promise<void> | undefined }) => async () => {
  await page.preload?.()
}

export interface RouterContext {
  api: Api
  queryClient: QueryClient
}

const root = createRootRouteWithContext<RouterContext>()({ component: Outlet })

async function loadMe({ api, queryClient }: RouterContext): Promise<Me> {
  if (!api.isSignedIn()) throw redirect({ to: '/sign-in' })
  try {
    return await queryClient.ensureQueryData({
      queryKey: ['me'],
      queryFn: () => api.me(),
    })
  } catch {
    api.signOut()
    throw redirect({ to: '/sign-in' })
  }
}

const signIn = createRoute({
  getParentRoute: () => root,
  path: '/sign-in',
  // "Start free" on the site: ?intent=signup, plus where the visitor came from (docs/14 §4.3).
  validateSearch: parseSignInSearch,
  component: SignIn,
})

const index = createRoute({
  getParentRoute: () => root,
  path: '/',
  beforeLoad: async ({ context }) => {
    const me = await loadMe(context)
    if (me.memberships.length) throw redirect({ to: '/overview' })
    throw redirect(me.staff ? { to: '/staff' } : { to: '/onboarding' })
  },
})

// Internal break-glass tool (ADM-6): Cloudoptics staff only, not in any navigation.
const staff = createRoute({
  getParentRoute: () => root,
  path: '/staff',
  beforeLoad: async ({ context }) => {
    const me = await loadMe(context)
    if (!me.staff) throw redirect({ to: '/' })
  },
  component: Staff,
})

const onboarding = createRoute({
  getParentRoute: () => root,
  path: '/onboarding',
  validateSearch: (
    search: Record<string, unknown>,
  ): { step?: number; connection?: string; check?: string; new?: true } => ({
    ...(search.step === undefined ? {} : { step: Number(search.step) === 1 ? 1 : 0 }),
    ...(typeof search.connection === 'string' ? { connection: search.connection } : {}),
    ...(typeof search.check === 'string' ? { check: search.check } : {}),
    ...(search.new === true || search.new === 'true' || search.new === 1 ? { new: true as const } : {}),
  }),
  beforeLoad: async ({ context, search }) => {
    const me = await loadMe(context)
    const creating = !search.step && !search.connection && !search.check
    // With a workspace already, the create step opens only from "New workspace" (?new=true);
    // without one, there is nothing to connect yet.
    if (me.memberships.length && creating && !search.new) throw redirect({ to: '/overview' })
    if (!me.memberships.length && !creating) throw redirect({ to: '/onboarding' })
  },
  loader: preload(Onboarding),
  component: function OnboardingRoute() {
    const { step, connection, check } = onboarding.useSearch()
    return <Onboarding startAt={step ?? 0} connectionId={connection} checkId={check} />
  },
})

// Where DigitalOcean sends the browser back after "Connect DigitalOcean" (ADR-021). Must match
// the OAuth app's registered callback URL: <app URL>/connect/digitalocean/callback.
const doCallback = createRoute({
  getParentRoute: () => root,
  path: '/connect/digitalocean/callback',
  validateSearch: (search: Record<string, unknown>): { code?: string; state?: string; error?: string } => {
    // The address bar's own text first: the router reads an all-digit value as a number.
    const raw = new URLSearchParams(window.location.search)
    const pick = (name: string) => {
      const v = raw.get(name) ?? search[name]
      return typeof v === 'string' && v ? v : undefined
    }
    const out: { code?: string; state?: string; error?: string } = {}
    const code = pick('code')
    const state = pick('state')
    const error = pick('error')
    if (code) out.code = code
    if (state) out.state = state
    if (error) out.error = error
    return out
  },
  beforeLoad: async ({ context }) => {
    const me = await loadMe(context)
    if (!me.memberships.length) throw redirect({ to: '/onboarding' })
  },
  loader: preload(Onboarding),
  component: function DigitalOceanCallbackRoute() {
    const search = doCallback.useSearch()
    return <Onboarding signIn={search} />
  },
})

const app = createRoute({
  getParentRoute: () => root,
  id: 'app',
  beforeLoad: async ({ context }) => {
    const me = await loadMe(context)
    if (!me.memberships.length) throw redirect(me.staff ? { to: '/staff' } : { to: '/onboarding' })
  },
  component: AppShell,
})

const overview = createRoute({
  getParentRoute: () => app,
  path: '/overview',
  component: Overview,
})
const connections = createRoute({
  getParentRoute: () => app,
  path: '/settings/connections',
  component: Connections,
})
const costs = createRoute({
  getParentRoute: () => app,
  path: '/costs',
  component: Costs,
})
const recommendations = createRoute({
  getParentRoute: () => app,
  path: '/recommendations',
  component: Recommendations,
})
const recommendation = createRoute({
  getParentRoute: () => app,
  path: '/recommendations/$recommendationId',
  loader: preload(RecommendationDetail),
  component: function RecommendationRoute() {
    const { recommendationId } = recommendation.useParams()
    return <RecommendationDetail recommendationId={recommendationId} />
  },
})
const anomalies = createRoute({
  getParentRoute: () => app,
  path: '/anomalies',
  component: Anomalies,
})
const budgets = createRoute({ getParentRoute: () => app, path: '/budgets', component: Budgets })
const commitments = createRoute({ getParentRoute: () => app, path: '/commitments', component: Commitments })
const reports = createRoute({
  getParentRoute: () => app,
  path: '/reports',
  validateSearch: (search: Record<string, unknown>): { month?: string } =>
    typeof search.month === 'string' && /^\d{4}-(0[1-9]|1[0-2])$/.test(search.month) ? { month: search.month } : {},
  loader: preload(Reports),
  component: function ReportsRoute() {
    const { month } = reports.useSearch()
    return <Reports month={month} />
  },
})
const settings = createRoute({
  getParentRoute: () => app,
  path: '/settings',
  validateSearch: (search: Record<string, unknown>): { section?: SettingsSection } =>
    isSection(search.section) ? { section: search.section } : {},
  loader: preload(Settings),
  component: function SettingsRoute() {
    const { section } = settings.useSearch()
    return <Settings section={section} />
  },
})

const routeTree = root.addChildren([
  signIn,
  index,
  staff,
  onboarding,
  doCallback,
  app.addChildren([
    overview,
    connections,
    costs,
    recommendations,
    recommendation,
    anomalies,
    budgets,
    commitments,
    reports,
    settings,
  ]),
])

export function createAppRouter(context: RouterContext, history?: RouterHistory) {
  return createRouter({
    routeTree,
    context,
    // GitHub Pages cannot rewrite deep links, so the static demo uses hash URLs.
    history: history ?? (import.meta.env.VITE_API_MODE === 'demo' ? createHashHistory() : undefined),
    // Staging serves the app under /app/ (VITE_BASE), next to the public site at / (SITE-13).
    basepath: routerBasepath(import.meta.env.VITE_API_MODE, import.meta.env.BASE_URL),
    defaultPreload: 'intent',
  })
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
}
