import type { QueryClient } from '@tanstack/react-query'
import {
  createHashHistory,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Outlet,
  redirect,
  type RouterHistory,
} from '@tanstack/react-router'

import type { Api, Me } from '../api/types'
import { Onboarding } from '../pages/Onboarding'
import { SignIn } from '../pages/SignIn'
import { Costs } from '../pages/Costs'
import { RecommendationDetail, Recommendations } from '../pages/Recommendations'
import { Settings } from '../pages/Settings'
import { Anomalies } from '../pages/Anomalies'
import { Budgets } from '../pages/Budgets'
import { ComingSoon, Connections, Overview } from '../pages/Workspace'
import { AppShell } from './AppShell'

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
  component: SignIn,
})

const index = createRoute({
  getParentRoute: () => root,
  path: '/',
  beforeLoad: async ({ context }) => {
    const me = await loadMe(context)
    throw redirect(me.memberships.length ? { to: '/overview' } : { to: '/onboarding' })
  },
})

const onboarding = createRoute({
  getParentRoute: () => root,
  path: '/onboarding',
  validateSearch: (search: Record<string, unknown>): { step?: number; connection?: string } => ({
    ...(search.step === undefined ? {} : { step: Number(search.step) === 1 ? 1 : 0 }),
    ...(typeof search.connection === 'string' ? { connection: search.connection } : {}),
  }),
  beforeLoad: async ({ context }) => {
    await loadMe(context)
  },
  component: function OnboardingRoute() {
    const { step, connection } = onboarding.useSearch()
    return <Onboarding startAt={step ?? 0} connectionId={connection} />
  },
})

const app = createRoute({
  getParentRoute: () => root,
  id: 'app',
  beforeLoad: async ({ context }) => {
    const me = await loadMe(context)
    if (!me.memberships.length) throw redirect({ to: '/onboarding' })
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
const reports = createRoute({
  getParentRoute: () => app,
  path: '/reports',
  component: () => <ComingSoon title="Reports" sprint="Sprint 6" what="Monthly executive summary as PDF and email." />,
})
const settings = createRoute({ getParentRoute: () => app, path: '/settings', component: Settings })

const routeTree = root.addChildren([
  signIn,
  index,
  onboarding,
  app.addChildren([
    overview,
    connections,
    costs,
    recommendations,
    recommendation,
    anomalies,
    budgets,
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
    defaultPreload: 'intent',
  })
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
}
