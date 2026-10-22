import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { createContext, useCallback, useContext, useState } from 'react'

import type { Api, Me } from '../api/types'
import { read, write } from '../lib/storage'

const ApiContext = createContext<Api | null>(null)

export function ApiProvider({ api, children }: { api: Api; children: ReactNode }) {
  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>
}

export function useApi(): Api {
  const api = useContext(ApiContext)
  if (!api) throw new Error('useApi outside ApiProvider')
  return api
}

export function useMe() {
  const api = useApi()
  return useQuery({ queryKey: ['me'], queryFn: () => api.me(), enabled: api.isSignedIn() })
}

const TENANT_KEY = 'co.tenant'

/** The workspace in view: the remembered one if the user still belongs to it, else the first. */
export function useCurrentTenantId(me: Me | undefined): [string | null, (id: string) => void] {
  const [stored, setStored] = useState(() => read('local', TENANT_KEY))
  const qc = useQueryClient()
  const set = useCallback(
    (id: string) => {
      write('local', TENANT_KEY, id)
      setStored(id)
      void qc.invalidateQueries()
    },
    [qc],
  )
  const ids = me?.memberships.map((m) => m.tenantId) ?? []
  const current = stored && ids.includes(stored) ? stored : (ids[0] ?? null)
  return [current, set]
}

export function rememberTenant(id: string): void {
  write('local', TENANT_KEY, id)
}
