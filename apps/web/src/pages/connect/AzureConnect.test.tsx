import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DemoApi } from '../../api/demo'
import { DEMO_AZURE_CHECKS } from '../../api/demoAzure'
import { ApiProvider } from '../../app/context'
import { createAppRouter } from '../../app/router'
import { accountLabel } from '../../lib/providers'
import { validateAzureForm } from './AzureConnect'
import { CONNECT_PROVIDERS } from './providers'

const DIRECTORY = '11111111-2222-4333-8444-555555555555'
const SUB = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'

/** Signs in, creates a workspace and lands on the Connect step. */
async function reachConnectStep() {
  const api = new DemoApi(0)
  const user = userEvent.setup()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createAppRouter({ api, queryClient }, createMemoryHistory({ initialEntries: ['/'] }))
  render(
    <ApiProvider api={api}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ApiProvider>,
  )
  await user.type(await screen.findByLabelText('Work email'), 'ada@example.com')
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  await user.type(await screen.findByLabelText('Company or team name'), 'Nairobi Nimbus')
  await user.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'Connect your AWS account' })
  return { api, user, router }
}

async function openAzure() {
  const ctx = await reachConnectStep()
  await ctx.user.click(screen.getByRole('radio', { name: /^Azure/ }))
  await screen.findByRole('heading', { name: 'Connect your Azure subscription' })
  return ctx
}

async function fillValid(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Directory (tenant) ID'), DIRECTORY)
  await user.type(screen.getByLabelText('Subscription ID'), SUB)
  await user.type(screen.getByLabelText('Storage account name'), 'nimbuscosts')
  await user.type(screen.getByLabelText('Container'), 'focus-exports')
  await user.type(screen.getByLabelText('Export folder path (optional)'), '/cloudoptics/focus/')
}

describe('Connect step: provider picker', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  it('lists AWS first, and Azure', () => {
    const ids = CONNECT_PROVIDERS.map((p) => p.id)
    expect(ids[0]).toBe('aws')
    expect(ids).toContain('azure')
  })

  it('shows AWS by default and switches to Azure and back', async () => {
    const { user } = await reachConnectStep()
    const group = screen.getByRole('group', { name: 'Which cloud?' })
    const aws = within(group).getByRole('radio', { name: /^AWS/ }) as HTMLInputElement
    expect(aws.checked).toBe(true)
    expect(screen.getByRole('link', { name: /Open AWS and connect/ })).toBeTruthy()

    await user.click(within(group).getByRole('radio', { name: /^Azure/ }))
    expect(await screen.findByRole('heading', { name: 'Connect your Azure subscription' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Connect your AWS account' })).toBeNull()

    await user.click(within(group).getByRole('radio', { name: /^AWS/ }))
    expect(await screen.findByRole('heading', { name: 'Connect your AWS account' })).toBeTruthy()
  })
})

describe('validateAzureForm', () => {
  const base = {
    directoryId: DIRECTORY,
    scopeType: 'subscription' as const,
    subscriptionIds: `${SUB.toUpperCase()},\n${SUB}`,
    billingAccountId: '',
    storageAccount: 'nimbuscosts',
    container: 'focus-exports',
    exportPath: '/a/b/',
  }

  it('builds the body: GUIDs lowercased and de-duplicated, slashes trimmed from the path', () => {
    expect(validateAzureForm(base).body).toEqual({
      directoryId: DIRECTORY,
      scopeType: 'subscription',
      subscriptionIds: [SUB],
      billingAccountId: null,
      storageAccount: 'nimbuscosts',
      container: 'focus-exports',
      exportPath: 'a/b',
    })
  })

  it('refuses bad GUIDs, storage names and containers', () => {
    const { errors, body } = validateAzureForm({
      ...base,
      directoryId: 'contoso.onmicrosoft.com',
      subscriptionIds: 'not-a-guid',
      storageAccount: 'Nimbus-Costs',
      container: 'focus--exports',
    })
    expect(body).toBeNull()
    expect(Object.keys(errors).sort()).toEqual(['container', 'directoryId', 'storageAccount', 'subscriptionIds'])
    expect(validateAzureForm({ ...base, storageAccount: 'ab' }).errors.storageAccount).toBeTruthy()
    expect(validateAzureForm({ ...base, container: '-abc' }).errors.container).toBeTruthy()
  })

  it('needs a billing account ID for a billing account, and no subscriptions', () => {
    const billing = { ...base, scopeType: 'billing_account' as const, subscriptionIds: '' }
    expect(validateAzureForm(billing).errors.billingAccountId).toBe('Enter the billing account ID.')
    expect(validateAzureForm({ ...billing, billingAccountId: '1234:5678_2019-05-31' }).body).toMatchObject({
      scopeType: 'billing_account',
      billingAccountId: '1234:5678_2019-05-31',
      subscriptionIds: [],
    })
  })

  it('takes one subscription, or up to 50 to scan under a billing account', () => {
    const two = [SUB, '00000000-0000-4000-8000-000000000002'].join(',')
    expect(validateAzureForm({ ...base, subscriptionIds: two }).errors.subscriptionIds).toMatch(/one subscription at a time/)
    const many = Array.from({ length: 51 }, (_, i) => `${String(i).padStart(8, '0')}-0000-4000-8000-000000000000`)
    const billing = { ...base, scopeType: 'billing_account' as const, billingAccountId: '12345678' }
    expect(validateAzureForm({ ...billing, subscriptionIds: many.join(',') }).errors.subscriptionIds).toBe(
      'List up to 50 subscriptions.',
    )
    expect(validateAzureForm({ ...billing, subscriptionIds: two }).body?.subscriptionIds).toHaveLength(2)
  })
})

// Whole journeys typed key by key: more than the 5 s default when the suite runs in parallel.
describe('Connect Azure wizard', { timeout: 20_000 }, () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  it('ties validation messages to their fields and focuses the first one', async () => {
    const { user } = await openAzure()
    await user.type(screen.getByLabelText('Directory (tenant) ID'), 'not-a-guid')
    await user.type(screen.getByLabelText('Storage account name'), 'Bad_Name')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    const directory = screen.getByLabelText('Directory (tenant) ID')
    expect(directory.getAttribute('aria-invalid')).toBe('true')
    await waitFor(() => expect(document.activeElement).toBe(directory))
    expect(screen.getByText(/Enter the directory ID as a GUID/).id).toBeTruthy()
    expect(directory.getAttribute('aria-describedby')).toContain(screen.getByText(/Enter the directory ID as a GUID/).id)
    expect(screen.getByText('Enter the subscription ID.')).toBeTruthy()
    expect(screen.getByText('Use 3 to 24 lowercase letters and numbers, with no hyphens.')).toBeTruthy()

    await user.click(screen.getByRole('radio', { name: 'Billing account' }))
    expect(screen.getByLabelText('Billing account ID')).toBeTruthy()
    expect(screen.getByLabelText('Subscription IDs to scan (optional)')).toBeTruthy()
  })

  it('shows the setup, copies the command, checks access and goes to the workspace', async () => {
    const { api, user } = await openAzure()
    const create = vi.spyOn(api, 'createAzureConnection')
    await fillValid(user)
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByRole('heading', { name: 'Set up access in Azure' })).toBeTruthy()
    expect(create.mock.calls[0]?.[1]).toMatchObject({ subscriptionIds: [SUB], exportPath: 'cloudoptics/focus' })
    expect(screen.getByText(accountLabel('azure', SUB))).toBeTruthy()

    const consent = screen.getByRole('link', { name: 'Grant admin consent (opens in a new tab)' })
    expect(consent.getAttribute('href')).toMatch(/adminconsent/)
    expect(consent.getAttribute('target')).toBe('_blank')
    expect(screen.getByRole('link', { name: 'Open cost exports in Azure (opens in a new tab)' })).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Copy Azure CLI command' }))
    expect(await screen.findByText('Copied')).toBeTruthy()
    expect(await navigator.clipboard.readText()).toMatch(/az role assignment create/)

    const portal = screen.getByText('Prefer the Azure portal?').closest('details') as HTMLDetailsElement
    expect(portal.open).toBe(false)
    await user.click(screen.getByText('Prefer the Azure portal?'))
    expect(portal.open).toBe(true)
    expect(screen.getByText('nimbuscosts')).toBeTruthy()
    expect(screen.getByText('cloudoptics/focus')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Check access' }))
    expect(await screen.findByRole('heading', { name: 'Connected' })).toBeTruthy()
    for (const c of DEMO_AZURE_CHECKS) expect(screen.getByText(c.label)).toBeTruthy()
    expect(screen.getByText('Access checked. Connected.')).toBeTruthy()
    expect(screen.getByText('First Azure costs')).toBeTruthy()
    expect(screen.getByText('within a day')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Go to my workspace' }))
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeTruthy()
  })

  it('says "Almost there" and offers to check again when access is missing', async () => {
    const { api, user } = await openAzure()
    await fillValid(user)
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByRole('heading', { name: 'Set up access in Azure' })
    vi.spyOn(api, 'validateAzureConnection').mockImplementation(async (_t, id) => ({
      ...(await api.getAzureConnection(_t, id)).connection,
      status: 'failed',
      lastValidatedAt: new Date().toISOString(),
      permissionChecks: [
        { id: 'consent', label: 'Admin consent', status: 'error', message: 'Consent not granted.', hint: 'Open the consent link.' },
      ],
    }))
    await user.click(screen.getByRole('button', { name: 'Check access' }))
    expect(await screen.findByRole('heading', { name: 'Almost there' })).toBeTruthy()
    expect(screen.getByText('Consent not granted.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Go to my workspace' })).toBeNull()
  })

  it('shows the API refusal when the subscription is already connected', async () => {
    const { api, user, router } = await openAzure()
    const tenantId = (await api.me()).memberships[0]?.tenantId as string
    await api.createAzureConnection(tenantId, {
      directoryId: DIRECTORY,
      scopeType: 'subscription',
      subscriptionIds: [SUB],
      billingAccountId: null,
      storageAccount: 'nimbuscosts',
      container: 'focus-exports',
      exportPath: '',
    })
    await fillValid(user)
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/already connected/)
    expect(router.state.location.pathname).not.toBe('/overview')
  })
})
