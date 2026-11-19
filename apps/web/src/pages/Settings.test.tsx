import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { DemoApi } from '../api/demo'
import { ApiProvider } from '../app/context'
import { createAppRouter } from '../app/router'

const ACCOUNT = '565656565656'

async function signedIn(name = 'Kampala Kloud') {
  const api = new DemoApi(0)
  await api.signIn('grace@example.com')
  const tenant = await api.createTenant({ name, countryCode: 'UG' })
  const setup = await api.createConnection(tenant.id, {
    awsAccountId: ACCOUNT,
    accountType: 'standalone',
    createCurExport: true,
  })
  await api.validateConnection(tenant.id, setup.connection.id)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createAppRouter({ api, queryClient }, createMemoryHistory({ initialEntries: ['/overview'] }))
  render(
    <ApiProvider api={api}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ApiProvider>,
  )
  await screen.findByRole('heading', { name: 'Overview' })
  return { api, tenant, connectionId: setup.connection.id, user: userEvent.setup(), router }
}

describe('Settings', () => {
  beforeEach(() => localStorage.clear())

  it('has a sub-nav for every section, and Connections on its own page', async () => {
    const { user, router } = await signedIn()
    await user.click(screen.getByRole('link', { name: 'Settings' }))
    const nav = await screen.findByRole('navigation', { name: 'Settings' })
    expect(within(nav).getAllByRole('link').map((l) => l.textContent)).toEqual([
      'AWS connections',
      'Users & roles',
      'Notifications',
      'Preferences',
      'Security',
      'Workspace',
    ])
    for (const id of ['users', 'notifications', 'preferences', 'security', 'workspace']) {
      expect(document.getElementById(`settings-${id}`)).toBeTruthy()
    }
    await user.click(within(nav).getByRole('link', { name: 'Workspace' }))
    await waitFor(() => expect(router.state.location.search).toEqual({ section: 'workspace' }))
    await user.click(within(nav).getByRole('link', { name: 'AWS connections' }))
    expect(await screen.findByText(`AWS account ${ACCOUNT}`)).toBeTruthy()
    expect(screen.getByRole('navigation', { name: 'Settings' })).toBeTruthy()
  })

  it('renames the workspace', async () => {
    const { api, tenant, user } = await signedIn()
    await user.click(screen.getByRole('link', { name: 'Settings' }))
    const name = await screen.findByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Kampala Cloud')
    await user.click(screen.getByRole('button', { name: 'Save name' }))
    await waitFor(async () => expect((await api.getTenant(tenant.id)).name).toBe('Kampala Cloud'))
    expect(await screen.findByText('Saved.')).toBeTruthy()
  })

  it('deletes a disconnected account’s data after the typed account ID, and keeps the record', async () => {
    const { api, tenant, connectionId, user } = await signedIn()
    await api.disconnectConnection(tenant.id, connectionId)
    await user.click(screen.getByRole('link', { name: 'Connections' }))
    await user.click(await screen.findByRole('button', { name: 'Delete data…' }))
    const confirm = screen.getByLabelText(`Type the account ID ${ACCOUNT} to confirm`)
    const button = screen.getByRole('button', { name: 'Delete data' }) as HTMLButtonElement
    await user.type(confirm, '565656')
    expect(button.disabled).toBe(true)
    await user.type(confirm, '565656')
    expect(button.disabled).toBe(false)
    await user.click(button)

    expect(await screen.findByText(/Deleted the data for AWS account 565656565656: 12,566 records across 17 tables/)).toBeTruthy()
    expect(screen.queryByText(`AWS account ${ACCOUNT}`)).toBeNull()
    expect(await api.listConnections(tenant.id)).toEqual([])

    await user.click(screen.getByRole('link', { name: 'Settings' }))
    expect(await screen.findByText(/Deleted an AWS account’s data/)).toBeTruthy()
    const history = screen.getByRole('heading', { name: 'Data deletions' }).closest('section') as HTMLElement
    expect(within(history).getByText(`AWS account ${ACCOUNT}`)).toBeTruthy()
    expect(within(history).getByText(/12,566 records across 17 tables/)).toBeTruthy()
    expect(within(history).getByText('cost_daily')).toBeTruthy()
  })

  it('only offers deleting data once the account is disconnected', async () => {
    const { user } = await signedIn()
    await user.click(screen.getByRole('link', { name: 'Connections' }))
    await screen.findByText(`AWS account ${ACCOUNT}`)
    expect(screen.queryByRole('button', { name: 'Delete data…' })).toBeNull()
  })

  it('schedules the workspace for deletion after the typed name, shows the AWS steps, and cancels it', async () => {
    const { api, tenant, user } = await signedIn()
    await user.click(screen.getByRole('link', { name: 'Settings' }))
    const zone = (await screen.findByRole('heading', { name: 'Danger zone' })).closest('section') as HTMLElement
    expect(within(zone).getByText(/It happens 7 days after you ask/)).toBeTruthy()
    expect(within(zone).getByText(`AWS account ${ACCOUNT}`)).toBeTruthy()
    expect(within(zone).getByText('cloudoptics-billing-access, cloudoptics-resource-access')).toBeTruthy()

    await user.click(within(zone).getByRole('button', { name: 'Delete workspace…' }))
    const schedule = within(zone).getByRole('button', { name: 'Delete in 7 days' }) as HTMLButtonElement
    await user.type(within(zone).getByLabelText('Type the workspace name (Kampala Kloud) to confirm'), 'kampala kloud')
    expect(schedule.disabled).toBe(true)
    await user.clear(within(zone).getByLabelText(/Type the workspace name/))
    await user.type(within(zone).getByLabelText(/Type the workspace name/), 'Kampala Kloud')
    await user.click(schedule)

    expect(await within(zone).findByText(/This workspace will be deleted on/)).toBeTruthy()
    expect((await api.getTenant(tenant.id)).status).toBe('deleting')
    // Every page says so while it is scheduled.
    expect(await screen.findByText(/This workspace is scheduled for deletion/)).toBeTruthy()
    expect(await screen.findByText(/Scheduled the workspace for deletion/)).toBeTruthy()

    await user.click(within(zone).getByRole('button', { name: 'Cancel deletion' }))
    expect(await within(zone).findByRole('button', { name: 'Delete workspace…' })).toBeTruthy()
    await waitFor(() => expect(screen.queryByText(/This workspace is scheduled for deletion/)).toBeNull())
    expect((await api.getTenant(tenant.id)).status).toBe('active')
    expect((await api.getTenantDeletion(tenant.id)).deletion).toBeNull()
    expect(await screen.findByText(/Cancelled the workspace deletion/)).toBeTruthy()
  })
})
