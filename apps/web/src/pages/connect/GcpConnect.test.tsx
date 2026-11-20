import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { DemoApi } from '../../api/demo'
import { ApiProvider } from '../../app/context'
import { createAppRouter } from '../../app/router'
import { focusTableName, validateGcp } from './GcpConnect'
import { CONNECT_PROVIDERS } from './providers'

const BILLING = '0A1B2C-3D4E5F-6A7B8C'

async function onboarding() {
  const api = new DemoApi(0)
  await api.signIn('ngozi@example.com')
  await api.createTenant({ name: 'Abuja Analytics', countryCode: 'NG' })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createAppRouter(
    { api, queryClient },
    createMemoryHistory({ initialEntries: ['/onboarding?step=1'] }),
  )
  render(
    <ApiProvider api={api}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ApiProvider>,
  )
  return { api, user: userEvent.setup() }
}

describe('Connect Google Cloud', () => {
  beforeEach(() => localStorage.clear())

  it('lists the registered clouds with AWS first and selected', async () => {
    expect(CONNECT_PROVIDERS.map((p) => p.id)).toEqual(['aws', 'gcp', 'digitalocean', 'azure'])
    await onboarding()
    expect(await screen.findByRole('heading', { name: 'Connect your AWS account' })).toBeTruthy()
    const picker = screen.getByRole('group', { name: 'Which cloud?' })
    expect((within(picker).getByRole('radio', { name: /AWS/ }) as HTMLInputElement).checked).toBe(true)
  })

  it('takes the export, shows the sharing steps with no commands, then checks access', async () => {
    const { user } = await onboarding()
    await user.click(await screen.findByRole('radio', { name: /Google Cloud/ }))
    expect(await screen.findByRole('heading', { name: 'Connect Google Cloud' })).toBeTruthy()
    const exportLink = screen.getByRole('link', { name: /Open billing export/ })
    expect(exportLink.getAttribute('href')).toBe('https://console.cloud.google.com/billing/export')

    // Mistakes are named before anything is sent.
    await user.type(screen.getByLabelText(/Billing account ID/), 'nope')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Use the format 012345-6789AB-CDEF01.')).toBeTruthy()
    expect(screen.getByText('Enter the project ID.')).toBeTruthy()

    await user.clear(screen.getByLabelText(/Billing account ID/))
    await user.type(screen.getByLabelText(/Billing account ID/), BILLING.toLowerCase())
    await user.type(screen.getByLabelText(/Project that holds the export/), 'acme-billing')
    await user.type(screen.getAllByLabelText(/^Dataset/)[0] as HTMLElement, 'billing_export')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByRole('heading', { name: 'Give Cloudoptics read access' })).toBeTruthy()
    // The connection's own account, to copy, and the dataset's Share page.
    const account = screen.getByText(/^co-[0-9a-f]{12}@cloudoptics-demo\.iam\.gserviceaccount\.com$/)
    expect(account).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Copy Your Cloudoptics account' })).toBeTruthy()
    const share = screen.getByRole('link', { name: /Open billing_export/ })
    expect(share.getAttribute('href')).toBe(
      'https://console.cloud.google.com/bigquery?project=acme-billing&p=acme-billing&d=billing_export&page=dataset',
    )
    // No Job User: Cloudoptics runs the queries. The viewer roles are optional.
    const roles = screen.getByRole('list', { name: 'Roles to grant your account' })
    expect(within(roles).queryByText('BigQuery Job User')).toBeNull()
    expect(within(roles).getByText('Compute Viewer')).toBeTruthy()
    expect(screen.getByText('Optional')).toBeTruthy()
    // Commands only on request.
    expect(screen.getByText('Prefer commands?')).toBeTruthy()
    expect(screen.queryByText(/set_label/)).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Check access' }))
    expect(await screen.findByRole('heading', { name: 'Connected' })).toBeTruthy()
    expect(screen.getByText(`Google Cloud billing account ${BILLING}.`)).toBeTruthy()
    expect(screen.getByText('Read the billing export dataset')).toBeTruthy()
  })

  it('says which step is missing when the check fails', async () => {
    const { firstProblem } = await import('./GcpConnect')
    expect(
      firstProblem([
        { id: 'federation', label: 'Sign in', status: 'ok', message: 'Signed in.', hint: null },
        {
          id: 'dataset',
          label: 'Read the billing export dataset',
          status: 'error',
          message: "We can't see dataset p.d yet: finish step 2.",
          hint: 'Share it with co-1@x as BigQuery Data Viewer.',
        },
      ]),
    ).toBe("We can't see dataset p.d yet: finish step 2. Share it with co-1@x as BigQuery Data Viewer.")
    expect(firstProblem([])).toBeNull()
  })

  it('checks fields like the API does', () => {
    const ok = {
      billingAccountId: BILLING,
      exportProjectId: 'acme-billing',
      exportDatasetId: 'billing_export',
      exportTableId: 'focus',
      exportKind: 'focus_export' as const,
    }
    expect(validateGcp(ok)).toEqual({})
    expect(focusTableName('01a2b3-c4d5e6-f7a8b9')).toBe('gcp_billing_export_focus_01A2B3_C4D5E6_F7A8B9')
    expect(focusTableName('nope')).toBe('')
    expect(Object.keys(validateGcp({ ...ok, queryProjectId: 'X', datasetLocation: 'a b' }))).toEqual([
      'queryProjectId',
      'datasetLocation',
    ])
  })
})
