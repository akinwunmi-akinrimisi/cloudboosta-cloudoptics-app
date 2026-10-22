import { afterEach, describe, expect, it, vi } from 'vitest'

import { HttpApi } from './http'

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
