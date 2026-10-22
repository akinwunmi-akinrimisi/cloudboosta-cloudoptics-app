import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { api } from './api'
import { AuthGate } from './app/AuthGate'
import { ApiProvider } from './app/context'
import { createAppRouter } from './app/router'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
})
const router = createAppRouter({ api, queryClient })

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
