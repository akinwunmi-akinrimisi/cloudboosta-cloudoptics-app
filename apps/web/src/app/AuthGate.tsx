import { AuthKitProvider, useAuth } from '@workos-inc/authkit-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useEffect } from 'react'

import { HttpApi } from '../api/http'
import type { Api } from '../api/types'
import { pickAttribution, rememberAttribution } from '../lib/attribution'
import { authRedirectUri } from './paths'

// Decides how people sign in, from the API's /v1/auth/config: WorkOS AuthKit on hosted
// environments, the development form elsewhere. Children render once the session is known.
export function AuthGate({ api, children }: { api: Api; children: ReactNode }) {
  const config = useQuery({ queryKey: ['auth-config'], queryFn: () => api.authConfig() })
  if (!(api instanceof HttpApi)) return <>{children}</>
  if (config.isLoading) return <Splash />
  if (config.data?.provider !== 'workos' || !config.data.clientId) return <>{children}</>
  return (
    <AuthKitProvider
      clientId={config.data.clientId}
      // The app's root under its base path (https://host/app/ on staging): this exact URL must be
      // a redirect URI in the WorkOS dashboard (docs/handoff/staging-next-steps.md).
      redirectUri={authRedirectUri(window.location.origin, import.meta.env.BASE_URL)}
      // No custom WorkOS auth domain yet, so the refresh token lives in local storage.
      devMode
      onRedirectCallback={({ state }) => {
        // Where the visitor came from, round-tripped through WorkOS (docs/14 §6.5, §7.5).
        rememberAttribution(pickAttribution((state as { attribution?: Record<string, unknown> } | null)?.attribution))
        window.history.replaceState(null, '', import.meta.env.BASE_URL)
      }}
    >
      <WorkOSBridge api={api}>{children}</WorkOSBridge>
    </AuthKitProvider>
  )
}

function WorkOSBridge({ api, children }: { api: HttpApi; children: ReactNode }) {
  const auth = useAuth()
  const qc = useQueryClient()
  const userId = auth.user?.id ?? null
  useEffect(() => {
    void qc.invalidateQueries({ queryKey: ['me'] })
  }, [qc, userId])
  if (auth.isLoading) return <Splash />
  api.attachProvider({
    signedIn: auth.user !== null,
    getToken: () => auth.getAccessToken(),
    signIn: (o) => (o?.signUp ? auth.signUp({ state: o.state }) : auth.signIn({ state: o?.state })),
    signOut: () => auth.signOut(),
  })
  // Re-mount the app when the user changes so routes re-check the session.
  return <div key={auth.user?.id ?? 'signed-out'}>{children}</div>
}

function Splash() {
  return (
    <div className="flex min-h-screen items-center justify-center text-sm text-secondary" role="status">
      Loading…
    </div>
  )
}
