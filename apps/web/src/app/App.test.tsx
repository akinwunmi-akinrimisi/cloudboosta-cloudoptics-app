import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DemoApi } from '../api/demo'
import { ApiProvider } from './context'
import { createAppRouter } from './router'

function renderApp(path = '/') {
  const api = new DemoApi(0)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createAppRouter({ api, queryClient }, createMemoryHistory({ initialEntries: [path] }))
  render(
    <ApiProvider api={api}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ApiProvider>,
  )
  return { api, router }
}

describe('Cloudoptics web app', () => {
  beforeEach(() => localStorage.clear())

  it('sends signed-out visitors to sign in', async () => {
    renderApp('/overview')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
  })

  it('walks a new user through onboarding to a healthy connection', async () => {
    const user = userEvent.setup()
    renderApp('/')
    await user.type(await screen.findByLabelText('Work email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    await user.type(await screen.findByLabelText('Company or team name'), 'Lagos Ledger')
    await user.selectOptions(screen.getByLabelText('Country'), 'KE')
    await user.click(screen.getByRole('button', { name: 'Create workspace' }))

    const accountField = await screen.findByLabelText('AWS account ID')
    await user.type(accountField, '12345')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('An AWS account ID has exactly 12 digits')).toBeTruthy()

    await user.clear(accountField)
    await user.type(accountField, '1234-5678-9012')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    const download = await screen.findByRole('link', { name: /Download template/ })
    expect(download.getAttribute('href')).toMatch(/cfn\/billing-role\.yaml$/)
    expect(screen.getByText('Upload a template file')).toBeTruthy()
    expect(screen.getByLabelText('Copy External ID')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'I have deployed the stack' }))
    expect(await screen.findByText('Healthy')).toBeTruthy()
    expect(screen.getByText('Find the CUR 2.0 export')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Go to my workspace' }))
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeTruthy()
    await waitFor(() => expect(screen.getByText(/Lagos Ledger · KES · Africa\/Nairobi/)).toBeTruthy())
  })

  it('switches the currency view from the top bar', async () => {
    const user = userEvent.setup()
    const { api } = renderApp('/sign-in')
    await api.signIn('kofi@example.com')
    const tenant = await api.createTenant({ name: 'Accra Apps', countryCode: 'GH' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '456456456456',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    await user.type(await screen.findByLabelText('Work email'), 'kofi@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    const both = await screen.findByRole('button', { name: 'Both' })
    expect(both.getAttribute('aria-pressed')).toBe('true')
    expect((await screen.findAllByText(/^\$[\d,.]+ · GH₵[\d,.]+$/)).length).toBe(2)
    await user.click(screen.getByRole('button', { name: 'Local' }))
    expect((await screen.findAllByText(/^GH₵[\d,.]+$/)).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('1 USD = GH₵15.50')).toBeTruthy()
  })

  it('lists connections with their checks', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('amina@example.com')
    const tenant = await api.createTenant({ name: 'Cairo Cloud', countryCode: 'EG' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '210987654321',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'amina@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    expect(await screen.findByText('AWS account 210987654321')).toBeTruthy()
    expect(screen.getByText('Read Savings Plans')).toBeTruthy()
    expect(await screen.findByText('1 of 1 accounts have cost data', { exact: false })).toBeTruthy()
  })

  it('lists the accounts discovered in the organization', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('zola@example.com')
    const tenant = await api.createTenant({ name: 'Joburg Group', countryCode: 'ZA' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '999988887777',
      accountType: 'management',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'zola@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    expect(await screen.findByText('3 of 4 accounts have cost data', { exact: false })).toBeTruthy()
    expect(screen.getByText('sandbox')).toBeTruthy()
  })

  it('fills the spend and forecast tiles once cost data exists', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('tunde@example.com')
    const tenant = await api.createTenant({ name: 'Ibadan Infra', countryCode: 'NG' })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'tunde@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeTruthy()
    expect((await screen.findAllByText('Waiting for first data')).length).toBe(2)

    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '123123123123',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    expect(await api.costSummary(tenant.id)).toMatchObject({ status: 'ready' })
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    await user.click(await screen.findByRole('link', { name: 'Overview' }))
    expect(await screen.findByText(/Amortised, after credits · data to/)).toBeTruthy()
    expect(screen.getByText(/Estimate from the daily run rate/)).toBeTruthy()
    expect(await screen.findByRole('img', { name: /Daily spend, 30 days/ })).toBeTruthy()
    expect(await screen.findByText('Amazon Elastic Compute Cloud')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Account' }))
    expect(await screen.findByText('demo-prod')).toBeTruthy()
    // Nigerian workspace: the staff-set NGN rate, and spend in both currencies by default.
    expect(screen.getByText(/^1 USD = ₦1,530.25$/)).toBeTruthy()
    expect(screen.getByText(/Official rate set by Cloudoptics staff/)).toBeTruthy()
    expect(screen.getAllByText(/^\$[\d,.]+ · ₦[\d,.]+$/).length).toBe(2)
  })

  it('warns when totals do not reconcile with AWS', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('nia@example.com')
    const tenant = await api.createTenant({ name: 'Kigali Kloud', countryCode: 'RW' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '121212121212',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const real = await api.costSummary(tenant.id)
    vi.spyOn(api, 'costSummary').mockResolvedValue({
      ...real,
      reconciliation: {
        status: 'mismatch',
        month: '2026-09',
        differencePct: -1.83,
        accountsMismatched: 1,
        checkedAt: '2026-10-02T06:00:00Z',
      },
    })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'nia@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(
      await screen.findByText("September totals differ from AWS Cost Explorer by 1.8%. We're reconciling; figures may change."),
    ).toBeTruthy()
  })

  async function connectedWorkspace(email: string) {
    const { api } = renderApp('/sign-in')
    await api.signIn(email)
    const tenant = await api.createTenant({ name: 'Nairobi Nodes', countryCode: 'GB' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '343434343434',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), email)
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByRole('heading', { name: 'Overview' })
    return { api, tenant, user }
  }

  it('lists recommendations best first and walks one to done', async () => {
    const { user } = await connectedWorkspace('wanjiru@example.com')
    expect(await screen.findByText('5 open recommendations')).toBeTruthy()

    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    const first = await screen.findByRole('link', { name: /Delete unattached gp2 volume build-cache/ })
    const links = screen.getAllByRole('link', { name: /\/mo$/ })
    expect(links[0]).toBe(first)

    await user.selectOptions(screen.getByLabelText('Type'), 'R2_idle_eip')
    expect(screen.getAllByRole('link', { name: /\/mo$/ })).toHaveLength(1)
    await user.selectOptions(screen.getByLabelText('Type'), 'all')

    await user.click(screen.getByRole('link', { name: /build-cache/ }))
    expect(await screen.findByRole('heading', { name: 'How to fix it' })).toBeTruthy()
    expect(screen.getByText(/aws ec2 delete-volume --volume-id vol-0a1b2c3d4e5f60718/)).toBeTruthy()
    expect(screen.getByText('No snapshot in the last 30 days')).toBeTruthy()
    expect(screen.getByText('Reversible from the snapshot')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Dismiss…' }))
    expect((screen.getByRole('button', { name: 'Dismiss' }) as HTMLButtonElement).disabled).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Mark as done' }))
    expect(await screen.findByText(/Done, waiting for the next scan/)).toBeTruthy()

    await user.click(screen.getByRole('link', { name: /All recommendations/ }))
    await screen.findByRole('link', { name: /Change volume db-data/ })
    expect(screen.queryByRole('link', { name: /build-cache/ })).toBeNull()
    expect(screen.getByText('4')).toBeTruthy()
  })

  it('explores costs by dimension and range', async () => {
    const { user } = await connectedWorkspace('otieno@example.com')
    await user.click(screen.getByRole('link', { name: 'Cost Explorer' }))
    expect(await screen.findByRole('heading', { name: 'Cost Explorer' })).toBeTruthy()
    expect(await screen.findByRole('img', { name: /Daily spend, 30 days/ })).toBeTruthy()
    expect(await screen.findByRole('cell', { name: /Amazon Relational Database Service/ })).toBeTruthy()

    await user.click(screen.getByRole('button', { name: '90 days' }))
    expect(await screen.findByRole('img', { name: /Daily spend, 90 days/ })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Region' }))
    expect(await screen.findByRole('cell', { name: /af-south-1/ })).toBeTruthy()
  })

  it('shows the audit log to admins in settings', async () => {
    const { user } = await connectedWorkspace('amina@example.com')
    await user.click(screen.getByRole('link', { name: 'Settings' }))
    expect(await screen.findByRole('heading', { name: 'Audit log' })).toBeTruthy()
    expect(await screen.findByText(/Created the workspace/)).toBeTruthy()
    expect(screen.getByText(/Connected an AWS account/)).toBeTruthy()
    expect(screen.getByText('pending → healthy')).toBeTruthy()
  })

  it('lets admins set up a connection again, disconnect it and reconnect it', async () => {
    const { api, tenant, user } = await connectedWorkspace('folake@example.com')
    await user.click(screen.getByRole('link', { name: 'Connections' }))
    expect(await screen.findByText('AWS account 343434343434')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Set up again' }))
    expect(await screen.findByRole('heading', { name: 'Deploy the read-only role' })).toBeTruthy()
    expect(screen.getByText(/Setting up AWS account 343434343434 again/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'I have deployed the stack' }))
    expect(await screen.findByText('Healthy')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Go to my workspace' }))

    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    await user.click(await screen.findByRole('button', { name: 'Disconnect…' }))
    expect(screen.getByText(/Disconnect AWS account 343434343434\?/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText(/Disconnected\. Cloudoptics no longer reads this account/)).toBeTruthy()
    const [gone] = await api.listConnections(tenant.id)
    expect(gone?.status).toBe('disconnected')

    const before = (await api.getSetup(tenant.id, gone!.id)).externalId
    await user.click(screen.getByRole('button', { name: 'Reconnect' }))
    expect(await screen.findByRole('heading', { name: 'Deploy the read-only role' })).toBeTruthy()
    const [conn] = await api.listConnections(tenant.id)
    expect(conn?.status).toBe('pending')
    expect((await api.getSetup(tenant.id, conn!.id)).externalId).not.toBe(before)
  })
})
