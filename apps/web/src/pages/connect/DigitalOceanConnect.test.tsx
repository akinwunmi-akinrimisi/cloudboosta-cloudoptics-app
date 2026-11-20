import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { Api, Connection } from '../../api/types'
import { ApiError } from '../../api/types'
import { ApiProvider } from '../../app/context'
import { DemoApi } from '../../api/demo'
import { DO_REQUIRED_SCOPES, DO_RESOURCE_SCOPES, DigitalOceanConnect, ReplaceDigitalOceanToken } from './DigitalOceanConnect'
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

describe('Connect DigitalOcean', () => {
  it('lists the read scopes and links to the token page', () => {
    wrap({}, <DigitalOceanConnect tenantId="t1" onConnected={() => {}} />)
    for (const scope of [...DO_REQUIRED_SCOPES, ...DO_RESOURCE_SCOPES]) {
      expect(screen.getByText(scope)).toBeTruthy()
    }
    expect(screen.getByRole('link', { name: /Generate New Token/ }).getAttribute('href')).toBe(
      'https://cloud.digitalocean.com/account/api/tokens/new',
    )
    expect(screen.getByLabelText('DigitalOcean token').getAttribute('type')).toBe('password')
  })

  it('checks the shape before sending, then connects', async () => {
    const connectDigitalOcean = vi.fn(async () => ({ id: 'c1' }) as Connection)
    const onConnected = vi.fn()
    wrap({ connectDigitalOcean }, <DigitalOceanConnect tenantId="t1" onConnected={onConnected} />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('DigitalOcean token'), 'nope')
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
    wrap({ connectDigitalOcean }, <DigitalOceanConnect tenantId="t1" onConnected={() => {}} />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('DigitalOcean token'), TOKEN)
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
