import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { api } from './api'
import { retryDelay, retryQuery } from './api/retry'
import { AuthGate } from './app/AuthGate'
import { ApiProvider } from './app/context'
import { createAppRouter } from './app/router'
import { pickAttribution, rememberAttribution } from './lib/attribution'
import './styles.css'

// Before the router or sign-in rewrites the URL: the campaign the site passed on (SITE-15),
// whatever app page it landed on. The sign-in page and the WorkOS round trip keep it too.
rememberAttribution(pickAttribution(Object.fromEntries(new URLSearchParams(window.location.search))))

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: retryQuery, retryDelay } },
})
const router = createAppRouter({ api, queryClient })
// A returning user: ask for /v1/me now, alongside AuthGate's /v1/auth/config, not after it.
// One round trip less before the first page (FR-UI-10: Overview within 4 s on 3G). Under
// WorkOS there is no stored token, so this waits for the provider as before.
if (api.isSignedIn()) void queryClient.prefetchQuery({ queryKey: ['me'], queryFn: () => api.me() })

const root = document.getElementById('root')
if (!root) throw new Error('Missing #root element')

createRoot(root).render(
  <StrictMode>
    <ApiProvider api={api}>
      <QueryClientProvider client={queryClient}>
        <AuthGate api={api}>
          <RouterProvider router={router} />
        </AuthGate>
      </QueryClientProvider>
    </ApiProvider>
  </StrictMode>,
)
