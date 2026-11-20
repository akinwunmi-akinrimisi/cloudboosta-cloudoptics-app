import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { DemoApi } from '../../api/demo'
import { ApiProvider } from '../../app/context'
import { createAppRouter } from '../../app/router'
import { validateGcp } from './GcpConnect'
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

  it('takes the export, shows the commands, then checks access', async () => {
    const { user } = await onboarding()
    await user.click(await screen.findByRole('radio', { name: /Google Cloud/ }))
    expect(await screen.findByRole('heading', { name: 'Connect Google Cloud' })).toBeTruthy()

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
    const label = screen.getByText(/bq update --set_label cloudoptics:[0-9a-f]{32} acme-billing:billing_export/)
    expect(label).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Copy Let Cloudoptics read the dataset' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Copy all commands' })).toBeTruthy()
    expect(screen.getByText('Prefer the Google Cloud console?')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'I have run the commands' }))
    expect(await screen.findByRole('heading', { name: 'Connected' })).toBeTruthy()
    expect(screen.getByText(`Google Cloud billing account ${BILLING}.`)).toBeTruthy()
    expect(screen.getByText('Prove the dataset is yours')).toBeTruthy()
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
    expect(Object.keys(validateGcp({ ...ok, queryProjectId: 'X', datasetLocation: 'a b' }))).toEqual([
      'queryProjectId',
      'datasetLocation',
    ])
  })
})
