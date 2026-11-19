import { afterEach, describe, expect, it, vi } from 'vitest'

import { HttpApi } from './http'
import { ApiError, MFA_REQUIRED_MESSAGE } from './types'

describe('HttpApi with an identity provider', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('sends the provider token and delegates sign-in and sign-out', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ id: 'u1', memberships: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    const signIn = vi.fn(async () => {})
    const signOut = vi.fn()
    const api = new HttpApi('/api')
    api.attachProvider({ signedIn: true, getToken: async () => 'workos-token', signIn, signOut })

    expect(api.isSignedIn()).toBe(true)
    await api.me()
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/me',
      expect.objectContaining({ headers: expect.objectContaining({ authorization: 'Bearer workos-token' }) }),
    )
    await api.signIn('ignored@example.com')
    expect(signIn).toHaveBeenCalled()
    api.signOut()
    expect(signOut).toHaveBeenCalled()
  })

  it('keeps the session on a 401 when a provider owns it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"title":"Invalid or expired token"}', { status: 401 })))
    const signOut = vi.fn()
    const api = new HttpApi('/api')
    api.attachProvider({ signedIn: true, getToken: async () => 't', signIn: async () => {}, signOut })
    await expect(api.me()).rejects.toThrow('Invalid or expired token')
    expect(signOut).not.toHaveBeenCalled()
  })
})

describe('HttpApi when a workspace requires MFA', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    sessionStorage.clear()
  })

  it('reports mfa_required with a clear message and keeps the dev session', async () => {
    const problem = { title: 'This workspace requires multi-factor authentication.', status: 401, code: 'mfa_required' }
    const fetch = vi.fn(async (url: string) =>
      url.endsWith('/v1/dev/token')
        ? new Response(JSON.stringify({ accessToken: 'dev-token' }), { status: 200 })
        : new Response(JSON.stringify(problem), { status: 401 }),
    )
    vi.stubGlobal('fetch', fetch)
    const api = new HttpApi('/api')
    await api.signIn('ada@example.com')

    const error = await api.getTenant('t1').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('mfa_required')
    expect((error as ApiError).message).toBe(MFA_REQUIRED_MESSAGE)
    expect(api.isSignedIn()).toBe(true)
  })

  it('still signs out a dev session on other 401s', async () => {
    const fetch = vi.fn(async (url: string) =>
      url.endsWith('/v1/dev/token')
        ? new Response(JSON.stringify({ accessToken: 'dev-token' }), { status: 200 })
        : new Response(JSON.stringify({ title: 'Invalid or expired token' }), { status: 401 }),
    )
    vi.stubGlobal('fetch', fetch)
    const api = new HttpApi('/api')
    await api.signIn('ada@example.com')
    await expect(api.me()).rejects.toThrow('Invalid or expired token')
    expect(api.isSignedIn()).toBe(false)
  })
})
