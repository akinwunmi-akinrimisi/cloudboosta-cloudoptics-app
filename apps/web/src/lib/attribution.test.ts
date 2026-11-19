import { afterEach, describe, expect, it, vi } from 'vitest'

import { HttpApi } from '../api/http'
import { write } from './storage'
import { pickAttribution, readAttribution, rememberAttribution } from './attribution'

afterEach(() => {
  write('session', 'co.attribution', null)
  vi.unstubAllGlobals()
})

describe('attribution (SITE-15)', () => {
  it('keeps the campaign the site passed on, and nothing else', () => {
    const search = new URLSearchParams('?intent=signup&utm_source=linkedin&utm_campaign=beta&landing=%2Fsecurity&code=x')
    rememberAttribution(pickAttribution(Object.fromEntries(search)))
    expect(readAttribution()).toEqual({ utm_source: 'linkedin', utm_campaign: 'beta', landing: '/security' })
  })

  it('is sent once with the first workspace, then forgotten', async () => {
    rememberAttribution({ utm_source: 'newsletter', ref: 'www.google.com', landing: '/' })
    const bodies: unknown[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        bodies.push(JSON.parse(String(init.body)))
        return new Response(JSON.stringify({ id: 't1' }), { status: 201, headers: { 'content-type': 'application/json' } })
      }),
    )
    const api = new HttpApi('http://api.test')
    await api.createTenant({ name: 'Kola Pay', countryCode: 'NG' })
    await api.createTenant({ name: 'Second', countryCode: 'NG' })
    expect(bodies[0]).toEqual({
      name: 'Kola Pay',
      countryCode: 'NG',
      attribution: { source: 'newsletter', ref: 'www.google.com', landingPath: '/' },
    })
    expect(bodies[1]).toEqual({ name: 'Second', countryCode: 'NG' })
    expect(readAttribution()).toEqual({})
  })
})
