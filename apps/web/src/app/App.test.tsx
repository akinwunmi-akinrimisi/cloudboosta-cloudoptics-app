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

    const link = await screen.findByRole('link', { name: /Open CloudFormation in AWS/ })
    const url = new URL(link.getAttribute('href') ?? '')
    expect(url.hostname).toBe('us-east-1.console.aws.amazon.com')
    expect(url.hash).toContain('param_ExternalId=')

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
    await api.createTenant({ name: 'Accra Apps', countryCode: 'GH' })
    await user.type(await screen.findByLabelText('Work email'), 'kofi@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    const both = await screen.findByRole('button', { name: 'Both' })
    expect(both.getAttribute('aria-pressed')).toBe('true')
    expect(await screen.findByText('$1,000.00 · GH₵15,500.00')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Local' }))
    expect(await screen.findByText('GH₵15,500.00')).toBeTruthy()
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
})
