import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Api, Connection } from '../../api/types'
import { ApiError } from '../../api/types'
import { ApiProvider } from '../../app/context'
import { DemoApi } from '../../api/demo'
import {
  DO_REQUIRED_SCOPES,
  DO_RESOURCE_SCOPES,
  DigitalOceanConnect,
  ReconnectDigitalOcean,
  ReplaceDigitalOceanToken,
  SIGN_IN_KEY,
  browser,
} from './DigitalOceanConnect'
import { CONNECT_PROVIDERS } from './providers'

const TOKEN = `dop_v1_${'a1'.repeat(32)}`

function wrap(api: Partial<Api>, children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <ApiProvider api={api as Api}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </ApiProvider>,
  )
}

const OFF = { digitalOceanOAuth: vi.fn(async () => ({ enabled: false })) }

describe('Connect DigitalOcean', () => {
  it('lists the read scopes and links to the token page', async () => {
    wrap(OFF, <DigitalOceanConnect tenantId="t1" onConnected={() => {}} />)
    expect(await screen.findByLabelText('DigitalOcean token')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Connect DigitalOcean' })).toBeNull()
    for (const scope of [...DO_REQUIRED_SCOPES, ...DO_RESOURCE_SCOPES]) {
      expect(screen.getByText(scope)).toBeTruthy()
    }
    expect(screen.getByRole('link', { name: 'Account → API' }).getAttribute('href')).toBe(
      'https://cloud.digitalocean.com/account/api/tokens',
    )
    expect(screen.getByLabelText('DigitalOcean token').getAttribute('type')).toBe('password')
  })

  it('checks the shape before sending, then connects', async () => {
    const connectDigitalOcean = vi.fn(async () => ({ id: 'c1' }) as Connection)
    const onConnected = vi.fn()
    wrap({ ...OFF, connectDigitalOcean }, <DigitalOceanConnect tenantId="t1" onConnected={onConnected} />)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('DigitalOcean token'), 'nope')
    await user.click(screen.getByRole('button', { name: 'Connect' }))
    expect(screen.getByText('Paste the whole token: it starts with dop_v1_.')).toBeTruthy()
    expect(connectDigitalOcean).not.toHaveBeenCalled()
    await user.clear(screen.getByLabelText('DigitalOcean token'))
    await user.type(screen.getByLabelText('DigitalOcean token'), TOKEN)
    await user.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith('c1'))
    expect(connectDigitalOcean).toHaveBeenCalledWith('t1', TOKEN)
    expect((screen.getByLabelText('DigitalOcean token') as HTMLInputElement).value).toBe('')
  })

  it('shows why a token was refused', async () => {
    const connectDigitalOcean = vi.fn(async () => {
      throw new ApiError(422, 'This token can change your DigitalOcean resources.')
    })
    wrap({ ...OFF, connectDigitalOcean }, <DigitalOceanConnect tenantId="t1" onConnected={() => {}} />)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('DigitalOcean token'), TOKEN)
    await user.click(screen.getByRole('button', { name: 'Connect' }))
    expect((await screen.findByRole('alert')).textContent).toContain('can change your DigitalOcean resources')
  })

  it('replaces a token', async () => {
    const replaceDigitalOceanToken = vi.fn(async () => ({ id: 'c1' }) as Connection)
    wrap({ replaceDigitalOceanToken }, <ReplaceDigitalOceanToken tenantId="t1" connectionId="c1" />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Replace token…' }))
    await user.type(screen.getByLabelText('New DigitalOcean token'), TOKEN)
    await user.click(screen.getByRole('button', { name: 'Replace token' }))
    expect(await screen.findByText(/Token replaced/)).toBeTruthy()
    expect(replaceDigitalOceanToken).toHaveBeenCalledWith('t1', 'c1', TOKEN)
  })

  it('falls back to the token when the sign-in setting cannot be read', async () => {
    wrap({}, <DigitalOceanConnect tenantId="t1" onConnected={() => {}} />)
    expect(await screen.findByLabelText('DigitalOcean token')).toBeTruthy()
  })

  it('is offered on the Connect step', () => {
    expect(CONNECT_PROVIDERS.map((p) => p.id)).toContain('digitalocean')
  })

  it('works in the demo', async () => {
    const demo = new DemoApi()
    demo.openSample()
    const me = await demo.me()
    const tenant = me.memberships[0]?.tenantId ?? ''
    await expect(demo.connectDigitalOcean(tenant, 'bad')).rejects.toThrow(/DigitalOcean token/)
    const conn = await demo.connectDigitalOcean(tenant, TOKEN)
    expect(conn.provider).toBe('digitalocean')
    expect(JSON.stringify(await demo.listConnections(tenant))).not.toContain(TOKEN)
    expect((await demo.validateConnection(tenant, conn.id)).status).toBe('healthy')
    expect((await demo.replaceDigitalOceanToken(tenant, conn.id, TOKEN)).id).toBe(conn.id)
  })
})

describe('Connect DigitalOcean by signing in', () => {
  const go = vi.spyOn(browser, 'go').mockImplementation(() => {})
  afterEach(() => {
    go.mockClear()
    sessionStorage.clear()
  })

  it('makes signing in the main way and keeps the token as an alternative', async () => {
    const startDigitalOceanOAuth = vi.fn(async () => ({ authorizeUrl: 'https://cloud.digitalocean.com/v1/oauth/authorize?x=1' }))
    const digitalOceanOAuth = vi.fn(async () => ({ enabled: true }))
    wrap({ digitalOceanOAuth, startDigitalOceanOAuth }, <DigitalOceanConnect tenantId="t1" onConnected={() => {}} />)
    const button = await screen.findByRole('button', { name: 'Connect DigitalOcean' })
    expect(screen.getByText('Sign in, pick the billing team')).toBeTruthy()
    expect(screen.getByText('Approve read-only access')).toBeTruthy()
    // The token paste is folded away under its own heading.
    const alternative = screen.getByText('Use an API token instead').closest('details')
    expect(alternative?.open).toBe(false)
    expect(alternative?.contains(screen.getByLabelText('DigitalOcean token'))).toBe(true)
    await userEvent.setup().click(button)
    await waitFor(() => expect(go).toHaveBeenCalledWith('https://cloud.digitalocean.com/v1/oauth/authorize?x=1'))
    expect(startDigitalOceanOAuth).toHaveBeenCalledWith('t1')
    expect(sessionStorage.getItem(SIGN_IN_KEY)).toBe('t1')
    expect(screen.getByRole('button', { name: 'Opening DigitalOcean…' })).toBeTruthy()
  })

  it('says why a sign-in could not start', async () => {
    const startDigitalOceanOAuth = vi.fn(async () => {
      throw new ApiError(404, 'Signing in with DigitalOcean is not set up here. Use an API token instead.')
    })
    wrap(
      { digitalOceanOAuth: async () => ({ enabled: true }), startDigitalOceanOAuth },
      <DigitalOceanConnect tenantId="t1" onConnected={() => {}} />,
    )
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Connect DigitalOcean' }))
    expect((await screen.findByRole('alert')).textContent).toContain('not set up here')
    expect(go).not.toHaveBeenCalled()
  })

  it('reconnects a signed-in team from the Connections page', async () => {
    const startDigitalOceanOAuth = vi.fn(async () => ({ authorizeUrl: 'https://do.example/authorize' }))
    wrap({ startDigitalOceanOAuth }, <ReconnectDigitalOcean tenantId="t1" />)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Reconnect' }))
    await waitFor(() => expect(go).toHaveBeenCalledWith('https://do.example/authorize'))
  })

  it('works in the demo: one use per state, in its own workspace', async () => {
    const demo = new DemoApi(0)
    await demo.signIn('ngozi@example.com')
    const tenant = await demo.createTenant({ name: 'Abuja Analytics', countryCode: 'NG' })
    expect(await demo.digitalOceanOAuth(tenant.id)).toEqual({ enabled: true })
    const { authorizeUrl } = await demo.startDigitalOceanOAuth(tenant.id)
    expect(authorizeUrl).toMatch(/^#\/connect\/digitalocean\/callback\?code=demo&state=demo/)
    const state = new URLSearchParams(authorizeUrl.split('?')[1]).get('state') ?? ''
    const other = await demo.createTenant({ name: 'Other', countryCode: 'NG' })
    await expect(demo.finishDigitalOceanOAuth(other.id, 'demo', state)).rejects.toThrow(/expired or was already used/)
    const conn = await demo.finishDigitalOceanOAuth(tenant.id, 'demo', state)
    expect(conn.credentialType).toBe('oauth')
    expect(conn.status).toBe('healthy')
    await expect(demo.finishDigitalOceanOAuth(tenant.id, 'demo', state)).rejects.toThrow(/expired/)
    // Signing in again reconnects the same team.
    const again = await demo.startDigitalOceanOAuth(tenant.id)
    const second = new URLSearchParams(again.authorizeUrl.split('?')[1]).get('state') ?? ''
    expect((await demo.finishDigitalOceanOAuth(tenant.id, 'demo', second)).id).toBe(conn.id)
  })
})
