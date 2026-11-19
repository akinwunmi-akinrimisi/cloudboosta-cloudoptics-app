import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { DemoApi } from '../api/demo'
import { ApiProvider } from './context'
import { headingTitle } from './routeFocus'
import { createAppRouter } from './router'

// FE-12 keyboard and screen-reader behaviour of the app shell, in demo mode (the static
// GitHub Pages build runs the same code). The real-browser versions are in
// apps/e2e/tests/keyboard.spec.ts and mobile.spec.ts.

async function signedIn(path = '/overview') {
  const api = new DemoApi(0)
  await api.signIn('ada@example.com')
  const tenant = await api.createTenant({ name: 'Lagos Ledger', countryCode: 'NG' })
  const setup = await api.createConnection(tenant.id, {
    awsAccountId: '123123123123',
    accountType: 'standalone',
    createCurExport: true,
  })
  await api.validateConnection(tenant.id, setup.connection.id)
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

describe('app shell accessibility', () => {
  beforeEach(() => {
    localStorage.clear()
    document.title = 'Cloudoptics'
  })

  it('starts with a skip link that moves focus to the main content', async () => {
    const user = userEvent.setup()
    await signedIn()
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeTruthy()
    await user.tab()
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(document.activeElement).toBe(skip)
    await user.keyboard('{Enter}')
    expect(document.activeElement).toBe(screen.getByRole('main'))
  })

  it('names the page in the title and moves focus to its heading on navigation', async () => {
    const user = userEvent.setup()
    await signedIn()
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeTruthy()
    await waitFor(() => expect(document.title).toBe('Overview · Cloudoptics'))
    // The first page does not steal focus.
    expect(document.activeElement).toBe(document.body)
    await user.click(screen.getByRole('link', { name: 'Budgets' }))
    const heading = await screen.findByRole('heading', { level: 1, name: 'Budgets' })
    await waitFor(() => expect(document.activeElement).toBe(heading))
    expect(document.title).toBe('Budgets · Cloudoptics')
    await user.click(screen.getByRole('link', { name: 'Commitments' }))
    await waitFor(() => expect(document.title).toBe('Commitments · Cloudoptics'))
  })

  it('opens the navigation as a modal drawer and closes it with Escape', async () => {
    const user = userEvent.setup()
    await signedIn()
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    const button = screen.getByRole('button', { name: 'Open menu' })
    expect(button.getAttribute('aria-expanded')).toBe('false')
    await user.click(button)
    const drawer = screen.getByRole('dialog', { name: 'Cloudoptics' })
    expect(button.getAttribute('aria-expanded')).toBe('true')
    // The rest of the page is inert while it is open.
    expect(screen.getByRole('main').closest('[inert]')).not.toBeNull()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(button))
    // A link in the drawer navigates and closes it.
    await user.click(button)
    await user.click(within(screen.getByRole('dialog')).getByRole('link', { name: 'Anomalies' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Anomalies' })).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(drawer.isConnected).toBe(false)
  })

  it('gives every chart a table of its figures (FR-UI-06)', async () => {
    const user = userEvent.setup()
    await signedIn('/costs')
    const chart = await screen.findByRole('img', { name: /^Daily spend, \d+ days/ })
    const figure = chart.closest('figure') as HTMLElement
    await user.click(within(figure).getByText('Show daily spend as a table'))
    const table = within(figure).getByRole('table', { name: 'Daily spend' })
    const rows = within(table).getAllByRole('row')
    // A header row and one row a day.
    expect(rows.length).toBe(Number(/, (\d+) days/.exec(chart.getAttribute('aria-label') ?? '')?.[1]) + 1)
    expect(within(table).getByRole('columnheader', { name: 'USD' })).toBeTruthy()
  })
})

describe('headingTitle', () => {
  it('uses the heading’s own words, not badges inside it', () => {
    const h1 = document.createElement('h1')
    h1.innerHTML = 'Commitments <span>Advisory only</span>'
    expect(headingTitle(h1)).toBe('Commitments')
    const plain = document.createElement('h1')
    plain.innerHTML = '<span>Only a span</span>'
    expect(headingTitle(plain)).toBe('Only a span')
  })
})
