import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DemoApi } from '../api/demo'
import { ApiProvider } from '../app/context'
import { createAppRouter } from '../app/router'
import { browser } from './connect/DigitalOceanConnect'

// The Connections page, Overview and Commitments for workspaces with other clouds than AWS:
// each provider's own name and account, its badge when there are several, Scan now and
// Reload bills for every provider, and Set up again opening that provider's own steps.

const BILLING = '0A1B2C-3D4E5F-6A7B8C'
const DIRECTORY = '11111111-2222-4333-8444-555555555555'
const SUBSCRIPTION = '0b5e0f1c-0000-4000-8000-000000000001'
const TOKEN = `dop_v1_${'a1'.repeat(32)}`

type Cloud = 'aws' | 'gcp' | 'azure' | 'digitalocean'

/** A demo workspace with the given clouds connected and working. */
async function workspace(clouds: Cloud[]) {
  const api = new DemoApi(0)
  await api.signIn('ngozi@example.com')
  const tenant = await api.createTenant({ name: 'Abuja Analytics', countryCode: 'NG' })
  const ids: Partial<Record<Cloud, string>> = {}
  if (clouds.includes('aws')) {
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '210987654321',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    ids.aws = setup.connection.id
  }
  if (clouds.includes('gcp')) {
    const setup = await api.createGcpConnection(tenant.id, {
      billingAccountId: BILLING,
      exportProjectId: 'abuja-billing',
      exportDatasetId: 'billing_export',
      exportTableId: 'gcp_billing_export_focus_0A1B2C_3D4E5F_6A7B8C',
      exportKind: 'focus_export',
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    ids.gcp = setup.connection.id
  }
  if (clouds.includes('azure')) {
    const setup = await api.createAzureConnection(tenant.id, {
      directoryId: DIRECTORY,
      scopeType: 'subscription',
      subscriptionIds: [SUBSCRIPTION],
      billingAccountId: null,
      storageAccount: 'abujacosts',
      container: 'focus-exports',
      exportPath: 'cloudoptics/focus',
    })
    await api.validateAzureConnection(tenant.id, setup.connection.id)
    ids.azure = setup.connection.id
  }
  if (clouds.includes('digitalocean')) {
    ids.digitalocean = (await api.connectDigitalOcean(tenant.id, TOKEN)).id
  }
  return { api, tenantId: tenant.id, ids }
}

function open(api: DemoApi, path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createAppRouter({ api, queryClient }, createMemoryHistory({ initialEntries: [path] }))
  render(
    <ApiProvider api={api}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ApiProvider>,
  )
  return { router, user: userEvent.setup() }
}

/** The card (a Card is a section) of the connection whose title starts with ``label``. */
async function card(label: string): Promise<HTMLElement> {
  const [title] = await screen.findAllByText(
    (_, el) => el?.tagName === 'P' && el.classList.contains('font-medium') && (el.textContent ?? '').startsWith(label),
  )
  return title?.closest('section') as HTMLElement
}

describe('Connections for every provider', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  it('names each provider and its account, with a badge for all four, and offers Scan now and Reload bills', async () => {
    const { api } = await workspace(['aws', 'gcp', 'azure', 'digitalocean'])
    open(api, '/settings/connections')
    expect(await screen.findByText(/^AWS account 210987654321/)).toBeTruthy()
    expect(screen.getByText(new RegExp(`^Google Cloud billing account ${BILLING}`))).toBeTruthy()
    expect(screen.getByText(new RegExp(`^Azure subscription ${SUBSCRIPTION}`))).toBeTruthy()
    expect(screen.getByText(/^DigitalOcean team /)).toBeTruthy()
    expect(screen.getByText(/^Subscription, with its FOCUS cost export/)).toBeTruthy()

    for (const [label, badge] of [
      ['AWS account', 'AWS'],
      ['Google Cloud billing account', 'Google Cloud'],
      ['Azure subscription', 'Azure'],
      ['DigitalOcean team', 'DigitalOcean'],
    ] as const) {
      const c = await card(label)
      expect(within(c).getAllByText(badge, { selector: 'span' }).length).toBeGreaterThan(0)
      expect(within(c).getByRole('button', { name: 'Reload bills…' })).toBeTruthy()
    }
    // Scan now for the other clouds: through the shared route, no AWS stack needed.
    for (const label of ['Google Cloud billing account', 'Azure subscription', 'DigitalOcean team']) {
      const c = await card(label)
      expect(within(c).getByRole('button', { name: 'Scan now' })).toBeTruthy()
      expect(within(c).queryByText(/CloudFormation|S3 bucket/)).toBeNull()
    }
    // Several providers: the accounts list is not called AWS accounts.
    expect(await screen.findByRole('heading', { name: 'Accounts' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'AWS accounts' })).toBeNull()
  })

  it('shows no badge in a one-provider workspace', async () => {
    const { api } = await workspace(['gcp'])
    open(api, '/settings/connections')
    const c = await card('Google Cloud billing account')
    expect(within(c).queryByText('Google Cloud', { selector: 'span' })).toBeNull()
    expect(await screen.findByRole('heading', { name: 'Google Cloud projects' })).toBeTruthy()
  })

  it('scans a DigitalOcean team now', async () => {
    const { api } = await workspace(['digitalocean'])
    const { user } = open(api, '/settings/connections')
    const c = await card('DigitalOcean team')
    expect(within(c).getByText(/scans the resources in this team every 12 hours/)).toBeTruthy()
    await user.click(within(c).getByRole('button', { name: 'Scan now' }))
    await waitFor(() => expect(within(c).getByText(/^Last scan /)).toBeTruthy())
    // DigitalOcean's reload reads invoices, not a bucket.
    await user.click(within(c).getByRole('button', { name: 'Reload bills…' }))
    expect(within(c).getByText(/invoices again from DigitalOcean/)).toBeTruthy()
    expect(within(c).queryByText(/S3|AWS/)).toBeNull()
    // Set up again for DigitalOcean is replacing the token.
    expect(within(c).getByRole('button', { name: 'Replace token…' })).toBeTruthy()
    expect(within(c).queryByRole('button', { name: 'Set up again' })).toBeNull()
  })

  it('Set up again on Google Cloud opens its commands and dataset label', async () => {
    const { api, ids } = await workspace(['aws', 'gcp'])
    const { user, router } = open(api, '/settings/connections')
    const c = await card('Google Cloud billing account')
    await user.click(within(c).getByRole('button', { name: 'Set up again' }))
    expect(await screen.findByRole('heading', { name: 'Give Cloudoptics read access' })).toBeTruthy()
    expect(router.state.location.search).toMatchObject({ connection: ids.gcp })
    expect(screen.getAllByText(/cloudoptics/).length).toBeGreaterThan(0)
    expect(screen.queryByRole('heading', { name: /AWS/ })).toBeNull()
  })

  it('Set up again on Azure shows the consent link, the Cloud Shell script and the ownership code', async () => {
    const { api, tenantId, ids } = await workspace(['azure'])
    const code = (await api.getAzureConnection(tenantId, ids.azure as string)).ownershipCode
    const { user, router } = open(api, '/settings/connections')
    const c = await card('Azure subscription')
    const button = within(c).getByRole('button', { name: 'Set up again' })
    expect(button.getAttribute('aria-expanded')).toBe('false')
    await user.click(button)
    expect(button.getAttribute('aria-expanded')).toBe('true')
    const steps = await within(c).findByRole('region', { name: 'Set up access in Azure again' })
    expect(button.getAttribute('aria-controls')).toBe(steps.id)
    expect((await within(steps).findByRole('link', { name: /Grant admin consent/ })).getAttribute('href')).toMatch(
      new RegExp(`^https://login\\.microsoftonline\\.com/${DIRECTORY}/adminconsent`),
    )
    expect(within(steps).getByText(/az role assignment create/)).toBeTruthy()
    expect(within(steps).getByText(code)).toBeTruthy()
    expect(within(steps).queryByText(/disconnecting deleted/i)).toBeNull()
    // It stays on the Connections page: no AWS stack.
    expect(router.state.location.pathname).toBe('/settings/connections')
    await user.click(within(c).getByRole('button', { name: 'Hide the steps' }))
    expect(within(c).queryByRole('region', { name: 'Set up access in Azure again' })).toBeNull()
  })

  it('Reconnect on a disconnected Azure connection shows the steps with a new code', async () => {
    const { api, tenantId, ids } = await workspace(['azure'])
    const before = (await api.getAzureConnection(tenantId, ids.azure as string)).ownershipCode
    await api.disconnectConnection(tenantId, ids.azure as string)
    const { user } = open(api, '/settings/connections')
    const c = await card('Azure subscription')
    expect(within(c).getByText(/Reconnect gives it a new ownership code/)).toBeTruthy()
    await user.click(within(c).getByRole('button', { name: 'Reconnect' }))
    const steps = await within(c).findByRole('region', { name: 'Set up access in Azure again' })
    expect(await within(steps).findByText(/Disconnecting deleted the old ownership code/)).toBeTruthy()
    const after = (await api.getAzureConnection(tenantId, ids.azure as string)).ownershipCode
    expect(after).not.toBe(before)
    expect(within(steps).getByText(after)).toBeTruthy()
    expect(within(steps).queryByText(before)).toBeNull()
  })

  it('asks for the right ID before deleting a disconnected Azure connection’s data', async () => {
    const { api, tenantId, ids } = await workspace(['azure'])
    await api.disconnectConnection(tenantId, ids.azure as string)
    const { user } = open(api, '/settings/connections')
    const c = await card('Azure subscription')
    await user.click(within(c).getByRole('button', { name: 'Delete data…' }))
    expect(within(c).getByText(/Type the subscription ID/)).toBeTruthy()
    await user.type(within(c).getByRole('textbox'), SUBSCRIPTION)
    await user.click(within(c).getByRole('button', { name: 'Delete data' }))
    expect(await screen.findByText(new RegExp(`Deleted the data for Azure subscription ${SUBSCRIPTION}`))).toBeTruthy()
  })
})

describe('Workspace wording for other clouds', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  it('a Google Cloud workspace’s Overview does not speak of AWS', async () => {
    const { api } = await workspace(['gcp'])
    open(api, '/overview')
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeTruthy()
    await screen.findAllByText(/Month-to-date spend/)
    await waitFor(() => expect(document.querySelector('main')?.textContent).not.toMatch(/Cost Explorer \(|AWS|Cost and Usage Report/))
  })

  it('an empty workspace asks for a cloud account, not an AWS one', async () => {
    const api = new DemoApi(0)
    await api.signIn('ngozi@example.com')
    await api.createTenant({ name: 'Abuja Analytics', countryCode: 'NG' })
    open(api, '/overview')
    expect(await screen.findByText(/No cloud account is connected yet/)).toBeTruthy()
  })

  it('Commitments say they are AWS-only in a DigitalOcean workspace', async () => {
    const { api } = await workspace(['digitalocean'])
    open(api, '/commitments')
    expect(await screen.findByText(/Commitments are AWS-only for now/)).toBeTruthy()
    expect(screen.queryByText(/No bills yet/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Refresh now/ })).toBeNull()
  })

  it('Commitments in a mixed workspace say the other clouds are not shown', async () => {
    const { api } = await workspace(['aws', 'gcp'])
    open(api, '/commitments')
    expect(await screen.findByText(/Commitments are AWS-only; your other clouds are not shown here/)).toBeTruthy()
  })
})

describe('Deleting a workspace with other clouds', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  it('lists what to remove in each cloud the workspace had, and only those', async () => {
    const { api } = await workspace(['gcp', 'azure', 'digitalocean'])
    open(api, '/settings?section=workspace')
    expect(await screen.findByRole('heading', { name: 'Remove Cloudoptics from your other clouds' })).toBeTruthy()
    // No AWS connection: no CloudFormation stacks to delete.
    expect(screen.queryByRole('heading', { name: 'Remove Cloudoptics from your AWS accounts' })).toBeNull()
    // Google Cloud: Cloudoptics deletes the connection's own account, so there are no commands.
    expect(screen.queryByRole('region', { name: `Commands for Google Cloud billing account ${BILLING}` })).toBeNull()
    expect(screen.getByText(/stops working at once\. Nothing else to do\./)).toBeTruthy()
    const azure = screen.getByRole('region', { name: `Commands for Azure subscription ${SUBSCRIPTION}` })
    expect(azure.textContent).toMatch(/az role assignment delete/)
    expect(azure.textContent).toMatch(/--operation Delete --tags cloudoptics-verify=co-/)
    expect(azure.textContent).toMatch(/az ad sp delete/)
    expect(screen.getByText(/delete the Cloudoptics enterprise application/)).toBeTruthy()
    expect(screen.getByText(/Revoke the token itself in DigitalOcean/)).toBeTruthy()
  })

  it('shows only the AWS stacks for an AWS workspace', async () => {
    const { api } = await workspace(['aws'])
    open(api, '/settings?section=workspace')
    expect(await screen.findByRole('heading', { name: 'Remove Cloudoptics from your AWS accounts' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Remove Cloudoptics from your other clouds' })).toBeNull()
  })
})

describe('Connect DigitalOcean by signing in (demo)', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  it('signs in, comes back, checks access and offers Reconnect', async () => {
    const { api, tenantId } = await workspace([])
    let sent = ''
    const go = vi.spyOn(browser, 'go').mockImplementation((url) => {
      sent = url
    })
    const { user } = open(api, '/onboarding?step=1')
    await user.click(await screen.findByText('DigitalOcean', { exact: true }))
    await user.click(await screen.findByRole('button', { name: 'Connect DigitalOcean' }))
    await waitFor(() => expect(sent).toMatch(/^#\/connect\/digitalocean\/callback\?/))
    go.mockRestore()
    cleanup()
    // DigitalOcean sends the browser back to the callback page.
    const back = open(api, sent.slice(1))
    expect(await screen.findByRole('heading', { name: 'Connected' }, { timeout: 5000 })).toBeTruthy()
    expect(back.router.state.location.pathname).toBe('/onboarding')
    const [conn] = await api.listConnections(tenantId)
    expect(conn?.credentialType).toBe('oauth')
    cleanup()
    open(api, '/settings/connections')
    const c = await card('DigitalOcean team')
    expect(within(c).getByText(/^DigitalOcean team, read-only sign-in/)).toBeTruthy()
    expect(within(c).getByRole('button', { name: 'Reconnect' })).toBeTruthy()
    expect(within(c).queryByRole('button', { name: 'Replace token…' })).toBeNull()
  })

  it('a used or unknown sign-in says so and offers the steps again', async () => {
    const { api } = await workspace([])
    open(api, '/connect/digitalocean/callback?code=demo&state=demo-not-started-here-0000')
    expect((await screen.findByRole('alert')).textContent).toContain('expired or was already used')
    expect(await screen.findByRole('button', { name: 'Connect DigitalOcean' })).toBeTruthy()
  })

  it('a sign-in cancelled at DigitalOcean connects nothing', async () => {
    const { api, tenantId } = await workspace([])
    open(api, '/connect/digitalocean/callback?error=access_denied&state=x')
    expect((await screen.findByRole('alert')).textContent).toContain('Not approved in DigitalOcean')
    expect(await api.listConnections(tenantId)).toEqual([])
  })
})
