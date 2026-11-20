import type { Api, InviteSessionApi } from './types'
import { ApiError } from './types'

/**
 * The connect steps (ConnectNew, GcpConnect, DigitalOceanConnect, AzureConnect) call the
 * workspace API with a tenant ID. Inside a connect-invite session they get this instead: the
 * same method names, sent to the session's own routes (the tenant ID is ignored; the session
 * knows its workspace). Every other method is refused here, as the server refuses it too.
 */
export function scopedApi(session: InviteSessionApi, base: Api): Api {
  const allowed: Partial<Record<keyof Api, unknown>> = {
    mode: base.mode,
    isSignedIn: () => false,
    authConfig: () => base.authConfig(),
    createIntent: () => session.createIntent(),
    getIntent: (_t: string, id: string) => session.getIntent(id),
    listConnections: async () => {
      const view = await session.view()
      return view.connection ? [view.connection] : []
    },
    validateConnection: () => session.check(),
    createGcpConnection: (_t: string, body: Parameters<InviteSessionApi['createGcpConnection']>[0]) =>
      session.createGcpConnection(body),
    getGcpSetup: () => session.getGcpSetup(),
    createAzureConnection: (_t: string, body: Parameters<InviteSessionApi['createAzureConnection']>[0]) =>
      session.createAzureConnection(body),
    getAzureConnection: () => session.getAzureConnection(),
    validateAzureConnection: async () => {
      await session.check()
      return (await session.getAzureConnection()).connection
    },
    connectDigitalOcean: (_t: string, token: string) => session.connectDigitalOcean(token),
    digitalOceanOAuth: () => session.digitalOceanOAuth(),
    startDigitalOceanOAuth: () => session.startDigitalOceanOAuth(),
    finishDigitalOceanOAuth: (_t: string, code: string, state: string) => session.finishDigitalOceanOAuth(code, state),
  }
  return new Proxy({} as Api, {
    get(_target, name: string) {
      if (name in allowed) return allowed[name as keyof Api]
      if (name === 'then') return undefined
      return () => Promise.reject(new ApiError(403, 'This page can only connect the cloud it was sent for.'))
    },
  })
}
