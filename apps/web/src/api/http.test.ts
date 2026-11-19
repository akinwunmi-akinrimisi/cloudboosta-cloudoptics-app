import { afterEach, describe, expect, it, vi } from 'vitest'

import { HttpApi, parseRetryAfter } from './http'
import { ApiError, MFA_REQUIRED_MESSAGE, isRateLimited, rateLimitedMessage } from './types'

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

describe('HttpApi and the API rate limits and caching headers', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('turns a 429 into a clear rate-limited error with the wait', async () => {
    const problem = { title: 'Too many requests. Try again in 12 seconds.', status: 429, code: 'rate_limited' }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(problem), { status: 429, headers: { 'retry-after': '12' } })))
    const api = new HttpApi('/api')
    api.attachProvider({ signedIn: true, getToken: async () => 't', signIn: async () => {}, signOut: () => {} })
    const error = await api.me().catch((e: unknown) => e)
    expect(isRateLimited(error)).toBe(true)
    expect((error as ApiError).retryAfter).toBe(12)
    expect((error as ApiError).code).toBe('rate_limited')
    expect((error as ApiError).message).toBe(rateLimitedMessage(12))
    expect(api.isSignedIn()).toBe(true)
  })

  it('leaves conditional requests to the browser cache (default cache mode, no manual validators)', async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({ id: 'u1', memberships: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    const api = new HttpApi('/api')
    api.attachProvider({ signedIn: true, getToken: async () => 't', signIn: async () => {}, signOut: () => {} })
    await expect(api.me()).resolves.toEqual({ id: 'u1', memberships: [] })
    const init = fetch.mock.calls[0]?.[1]
    expect(init?.cache).toBeUndefined()
    expect(Object.keys(init?.headers ?? {})).not.toContain('if-none-match')
  })

  it('reads Retry-After as seconds or an HTTP date', () => {
    expect(parseRetryAfter('7')).toBe(7)
    expect(parseRetryAfter(null)).toBeUndefined()
    expect(parseRetryAfter('soon')).toBeUndefined()
    const now = Date.parse('2026-11-19T10:00:00Z')
    expect(parseRetryAfter('Thu, 19 Nov 2026 10:00:30 GMT', now)).toBe(30)
  })
})
