import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DemoApi } from '../api/demo'
import type { Me, SupportGrant, SupportGrantRequest } from '../api/types'
import { ApiProvider } from '../app/context'
import { createAppRouter } from '../app/router'

/** The demo has no staff; this one is staff, with grants kept in memory. */
class StaffApi extends DemoApi {
  grants: SupportGrant[] = []
  supportFor: string | null = null

  async me(): Promise<Me> {
    const me = await super.me()
    const active = this.grants.find((g) => g.status === 'active')
    return {
      ...me,
      staff: true,
      memberships: me.memberships.map((m) =>
        m.tenantId === this.supportFor && active
          ? { ...m, role: 'viewer', support: { grantId: active.id, expiresAt: active.expiresAt } }
          : m,
      ),
    }
  }

  async listSupportGrants(): Promise<SupportGrant[]> {
    return structuredClone(this.grants)
  }

  async requestSupportGrant(body: SupportGrantRequest): Promise<SupportGrant> {
    const now = Date.now()
    const grant: SupportGrant = {
      id: `g-${this.grants.length + 1}`,
      tenantId: body.tenantId,
      tenantName: 'Lagos Ledger',
      staffEmail: 'support@cloudoptics.example',
      staffName: null,
      justification: body.justification,
      ticketRef: body.ticketRef,
      createdAt: new Date(now).toISOString(),
      expiresAt: new Date(now + body.durationMinutes * 60_000).toISOString(),
      endedAt: null,
      endedByEmail: null,
      status: 'active',
      requests: 0,
    }
    this.grants.unshift(grant)
    return grant
  }

  async endSupportGrant(grantId: string): Promise<SupportGrant> {
    const grant = this.grants.find((g) => g.id === grantId) as SupportGrant
    grant.status = 'ended'
    grant.endedAt = new Date().toISOString()
    return grant
  }
}

function renderAt(api: DemoApi, path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createAppRouter({ api, queryClient }, createMemoryHistory({ initialEntries: [path] }))
  render(
    <ApiProvider api={api}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ApiProvider>,
  )
  return router
}

describe('staff support access', () => {
  beforeEach(() => localStorage.clear())

  it('is hidden from everyone but staff, including in the demo', async () => {
    const api = new DemoApi(0)
    await api.signIn('ada@example.com')
    await api.createTenant({ name: 'Lagos Ledger', countryCode: 'NG' })
    const router = renderAt(api, '/staff')
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeTruthy()
    expect(router.state.location.pathname).toBe('/overview')
    expect(screen.queryByRole('link', { name: 'Staff' })).toBeNull()
    await expect(api.requestSupportGrant({ tenantId: 't', justification: 'x', ticketRef: 'SUP-1', durationMinutes: 60 })).rejects.toThrow('not available in the demo')
  })

  it('opens read-only access with a reason, shows the banner, and ends it', async () => {
    const user = userEvent.setup()
    const api = new StaffApi(0)
    await api.signIn('support@cloudoptics.example')
    const tenant = await api.createTenant({ name: 'Lagos Ledger', countryCode: 'NG' })
    const request = vi.spyOn(api, 'requestSupportGrant')
    api.supportFor = tenant.id
    renderAt(api, '/staff')

    expect(await screen.findByRole('heading', { name: 'Support access', level: 1 })).toBeTruthy()
    expect(await screen.findByText('No support access yet.')).toBeTruthy()
    const submit = screen.getByRole('button', { name: 'Open read-only access' })
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    await user.type(screen.getByLabelText('Workspace ID'), tenant.id)
    await user.type(screen.getByLabelText('Justification'), 'Customer reports missing October costs')
    // A ticket reference is required, and access lasts 2 hours at most (docs/11b §7.4).
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    const durations = within(screen.getByLabelText('Duration')).getAllByRole('option')
    expect(durations.map((o) => (o as HTMLOptionElement).value)).toEqual(['15', '30', '60', '120'])
    await user.type(screen.getByLabelText('Ticket reference'), 'SUP-1042')
    await user.selectOptions(screen.getByLabelText('Duration'), '120')
    await user.click(submit)
    expect(request).toHaveBeenCalledWith({
      tenantId: tenant.id,
      justification: 'Customer reports missing October costs',
      ticketRef: 'SUP-1042',
      durationMinutes: 120,
    })
    const item = (await screen.findByText(/Customer reports missing October costs/)).closest('li') as HTMLElement
    expect(within(item).getByText(/Active/)).toBeTruthy()

    await user.click(await within(item).findByRole('button', { name: 'View workspace' }))
    expect(await screen.findByText(/you are viewing Lagos Ledger as Cloudoptics staff/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'End access now' }))
    expect(await screen.findByRole('heading', { name: 'Support access', level: 1 })).toBeTruthy()
    await waitFor(() => expect(screen.getByText(/Ended/)).toBeTruthy())
    expect(api.grants[0]?.status).toBe('ended')
  })
})
