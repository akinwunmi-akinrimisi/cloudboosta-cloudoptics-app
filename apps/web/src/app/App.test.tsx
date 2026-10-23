import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

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
})
