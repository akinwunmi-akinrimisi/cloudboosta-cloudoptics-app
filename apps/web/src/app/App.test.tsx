import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DemoApi } from '../api/demo'
import { ApiError, MFA_REQUIRED_MESSAGE } from '../api/types'
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

    // No account ID to type: one link, and the stack reports which account it is in.
    expect(await screen.findByRole('heading', { name: 'Connect your AWS account' })).toBeTruthy()
    expect(screen.queryByLabelText('AWS account ID')).toBeNull()
    // One click is the way in; deploying the template by hand is the alternative underneath.
    const oneClick = await screen.findByRole('link', { name: /Open AWS and connect/ })
    expect(screen.getByText('Press the button below. AWS opens with everything filled in.')).toBeTruthy()
    const alternative = screen.getByText('Prefer to deploy the template yourself?')
    await user.click(alternative)
    const download = screen.getByRole('link', { name: /Download template/ })
    expect(download.getAttribute('href')).toMatch(/cfn\/cloudoptics-access\.yaml$/)
    expect(screen.getByLabelText('Copy ConnectionRef')).toBeTruthy()

    await user.click(oneClick)
    expect(await screen.findByRole('heading', { name: 'Connected' })).toBeTruthy()
    expect(screen.getByText('Healthy')).toBeTruthy()
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

  it('explains how to fix it when the workspace requires MFA', async () => {
    const user = userEvent.setup()
    const { api } = renderApp('/sign-in')
    await api.signIn('nia@example.com')
    const tenant = await api.createTenant({ name: 'Kigali Cloud', countryCode: 'RW' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '123123123123',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    vi.spyOn(api, 'getTenant').mockRejectedValue(new ApiError(401, MFA_REQUIRED_MESSAGE, [], 'mfa_required'))
    await user.type(await screen.findByLabelText('Work email'), 'nia@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByText(MFA_REQUIRED_MESSAGE)).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Overview' })).toBeNull()
    const [, signOut] = screen.getAllByRole('button', { name: 'Sign out' })
    expect(signOut).toBeTruthy()
    await user.click(signOut as HTMLElement)
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
  })

  it('creates, edits and deletes a budget', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('zola@example.com')
    const tenant = await api.createTenant({ name: 'Joburg Data', countryCode: 'ZA' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '210987654322',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'zola@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: /Budgets/ }))
    expect(await screen.findByText(/No budgets yet/)).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'New budget' }))
    await user.type(screen.getByLabelText('Name'), 'Company total')
    await user.type(screen.getByLabelText('Amount a month'), '100000')
    await user.selectOptions(screen.getByLabelText('Budget currency'), 'USD')
    expect(screen.getByLabelText('Threshold 4 measure')).toHaveProperty('value', 'forecast')
    await user.type(screen.getByLabelText('Other addresses'), 'finance@example.com')
    await user.click(screen.getByRole('button', { name: 'Create budget' }))
    expect(await screen.findByText('Company total')).toBeTruthy()
    expect(screen.getByText('On track')).toBeTruthy()
    expect(screen.getByRole('progressbar', { name: 'Company total: spent so far' })).toBeTruthy()
    const stored = (await api.listBudgets(tenant.id))[0]!
    expect(stored.recipients.emails).toEqual(['finance@example.com'])
    expect(stored.thresholds.map((t) => `${t.kind}:${t.percent}`)).toEqual([
      'actual:50',
      'actual:80',
      'actual:100',
      'forecast:100',
    ])

    // A budget far below this month's spend is over it.
    await user.click(screen.getByRole('button', { name: 'Edit' }))
    const amountField = screen.getByLabelText('Amount a month')
    await user.clear(amountField)
    await user.type(amountField, '1')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText('Over budget')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Delete…' }))
    await user.click(screen.getByRole('button', { name: 'Delete Company total' }))
    expect(await screen.findByText(/No budgets yet/)).toBeTruthy()
  })

  it('triages an anomaly and changes the alert thresholds', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('ada@example.com')
    const tenant = await api.createTenant({ name: 'Lagos Labs', countryCode: 'NG' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '210987654323',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: /Anomalies/ }))
    expect(await screen.findByRole('tab', { name: 'Open (1)' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: /Amazon Elastic Compute Cloud in demo-dev/ }))
    expect(await screen.findByText('EU-BoxUsage:c5.4xlarge')).toBeTruthy()
    expect(await screen.findByRole('img', { name: /against the expected level/ })).toBeTruthy()
    await user.type(screen.getByLabelText('Note (optional)'), 'Planned load test')
    await user.click(screen.getByRole('button', { name: 'Mark expected' }))
    await waitFor(async () => expect((await api.listAnomalies(tenant.id, 'expected')).length).toBe(1))
    expect(await screen.findByRole('tab', { name: 'Open (0)' })).toBeTruthy()

    const minimum = screen.getByLabelText('Minimum above expected, per day')
    await user.clear(minimum)
    await user.type(minimum, '100')
    await user.selectOptions(screen.getByLabelText('And at least'), '50')
    await user.type(screen.getByLabelText('Also email'), 'Ops@Example.com')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () =>
      expect((await api.getAlertSettings(tenant.id)).settings).toEqual({
        anomalyMinImpact: 100,
        anomalyMinImpactCurrency: 'USD',
        anomalyMinPercent: 50,
        anomalyEmails: ['ops@example.com'],
      }),
    )
  })

  it('lets members choose their emails and preview the weekly digest', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('ada@example.com')
    const tenant = await api.createTenant({ name: 'Lagos Labs', countryCode: 'NG' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '210987654324',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: /Settings/ }))
    const digest = await screen.findByRole('checkbox', { name: /Monday from 07:00/ })
    expect(digest).toHaveProperty('checked', true)
    await user.click(digest)
    await waitFor(async () => expect((await api.getNotificationPreferences(tenant.id)).mine.weeklyDigest).toBe(false))
    await user.click(screen.getByLabelText('Anomaly alerts by default'))
    await waitFor(async () =>
      expect((await api.getNotificationPreferences(tenant.id)).defaults).toEqual({
        weeklyDigest: true,
        anomalyAlert: false,
        monthlyReport: true,
        commitmentExpiry: true,
      }),
    )
    await user.click(screen.getByRole('button', { name: 'Preview last week’s digest' }))
    expect(await screen.findByText(/^Your AWS week: USD/)).toBeTruthy()
    expect(screen.getByTitle('Weekly digest preview')).toBeTruthy()
  })

  it('shows last month’s report as a draft, and sends it with extra recipients', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('ada@example.com')
    const tenant = await api.createTenant({ name: 'Lagos Labs', countryCode: 'NG' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '210987654325',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: /Reports/ }))
    const doc = await screen.findByRole('article', { name: /Report for/ })
    expect(doc.textContent).toContain('Total spend')
    expect(doc.textContent).toContain('What changed most')
    expect(screen.getByText(/Draft from today/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Print or save as PDF/ })).toBeTruthy()

    await user.type(screen.getByLabelText('Also send to'), 'Board@Example.com')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => expect((await api.getReportSettings(tenant.id)).emails).toEqual(['board@example.com']))
    await user.click(screen.getByRole('button', { name: /Make and send now/ }))
    expect(await screen.findByText('Sent to 2 recipients.')).toBeTruthy()
    expect(await screen.findByText(/the figures stay as they were then/)).toBeTruthy()
    expect((await api.listReports(tenant.id)).reports).toHaveLength(1)
  })

  it('shows the credits runway and warns on the overview when they end soon', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('ada@example.com')
    const tenant = await api.createTenant({ name: 'Lagos Labs', countryCode: 'NG' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '210987654326',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: /Cost Explorer/ }))
    expect(await screen.findByText(/AWS does not report the balance left/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Enter the balance' }))
    await user.type(screen.getByLabelText('Remaining balance (USD)'), '1000')
    await user.click(screen.getByRole('button', { name: 'Save balance' }))
    expect(await screen.findByText(/in about \d+ days/)).toBeTruthy()
    const runway = await api.getCredits(tenant.id)
    expect(runway.status).toBe('ending_soon')
    expect(runway.remainingUsd).toBe(1000)

    await user.click(screen.getByRole('link', { name: /Overview/ }))
    expect(await screen.findByText(/Your AWS credits run out in about/)).toBeTruthy()
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
    expect(screen.getByText(/days' run rate \(\d+ of \d+ days\)/)).toBeTruthy()
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

  it('shows the Savings Plan advisory, the commitments held and R8 on the list', async () => {
    const { api, tenant, user } = await connectedWorkspace('kofi@example.com')
    await user.click(screen.getByRole('link', { name: 'Commitments' }))
    expect(await screen.findByRole('heading', { name: /Our option: Compute Savings Plan, 1 year, No Upfront/ })).toBeTruthy()
    expect(screen.getByText('Advisory only')).toBeTruthy()
    expect(screen.getByRole('meter', { name: 'Savings Plan coverage' })).toBeTruthy()
    expect(screen.getAllByText(/AWS recommends: 1 year/)).toHaveLength(4)
    expect(screen.getByRole('heading', { name: 'Buy it in steps' })).toBeTruthy()
    expect(screen.getAllByText(/within 7 days of buying it/).length).toBeGreaterThan(0)
    // GBP workspace: the commitment in pounds now and if sterling weakens 20% / 40%.
    expect(screen.getByRole('heading', { name: 'In GBP, if the currency weakens' })).toBeTruthy()
    expect(screen.getByText('GBP weakens 40%')).toBeTruthy()
    expect(screen.getByText('2 x m5.large Linux/UNIX')).toBeTruthy()
    expect(screen.getByText('28 days left')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Refresh now/ })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: /Refresh now/ }))
    expect(await screen.findByText(/Read again from AWS just now/)).toBeTruthy()

    const advisory = await api.savingsPlanAdvisory(tenant.id)
    const [advice] = advisory.advice
    expect(advice?.status).toBe('ok')
    expect(advice?.default?.discountFromAws).toBe(true)
    expect(advice?.ladder[0]?.sharePercent).toBe(25)
    expect(advice?.options.filter((o) => o.source === 'ours')).toHaveLength(12)

    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    await user.click(await screen.findByRole('link', { name: /1-year Compute Savings Plan/ }))
    expect(await screen.findByText('Commits to paying for a year')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Commitments page' })).toBeTruthy()
  })

  it('lists recommendations best first and walks one to done', async () => {
    const { user } = await connectedWorkspace('wanjiru@example.com')
    expect(await screen.findByText(/^10 open recommendations · .+\/mo realised$/)).toBeTruthy()

    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    // Most saved first: the Extended Support upgrade, then R8 (the Savings Plan advisory).
    const first = await screen.findByRole('link', { name: /Upgrade database reports-db off PostgreSQL 12/ })
    const links = screen.getAllByRole('link', { name: /\/mo$/ })
    expect(links[0]).toBe(first)
    expect(links[1]).toBe(screen.getByRole('link', { name: /1-year Compute Savings Plan/ }))
    expect(links[2]).toBe(screen.getByRole('link', { name: /Delete unattached gp2 volume build-cache/ }))

    await user.selectOptions(screen.getByLabelText('Type'), 'R2_idle_eip')
    expect(screen.getAllByRole('link', { name: /\/mo$/ })).toHaveLength(1)
    await user.selectOptions(screen.getByLabelText('Type'), 'all')

    await user.click(screen.getByRole('link', { name: /build-cache/ }))
    expect(await screen.findByRole('heading', { name: 'How to fix it' })).toBeTruthy()
    expect(screen.getByRole('link', { name: /Open in AWS console/ }).getAttribute('href')).toContain(
      '#VolumeDetails:volumeId=vol-0a1b2c3d4e5f60718',
    )
    expect(screen.getByText('Choose Actions, then Delete volume, and confirm.')).toBeTruthy()
    expect(screen.getByText(/Show the AWS CLI commands/)).toBeTruthy()
    expect(screen.getByText(/aws ec2 delete-volume --volume-id vol-0a1b2c3d4e5f60718/)).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'What could go wrong' })).toBeTruthy()
    expect(screen.getByText('A backup snapshot is kept, so it can be restored from that.')).toBeTruthy()
    expect(screen.getByText('No snapshot in the last 30 days')).toBeTruthy()
    expect(screen.getByText('Reversible from the snapshot')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Dismiss…' }))
    expect((screen.getByRole('button', { name: 'Dismiss' }) as HTMLButtonElement).disabled).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Mark as done' }))
    expect(await screen.findByText(/Done, waiting for verification/)).toBeTruthy()
    expect(screen.getByText(/marks it verified when it sees the change/)).toBeTruthy()

    await user.click(screen.getByRole('link', { name: /All recommendations/ }))
    await screen.findByRole('link', { name: /Change volume db-data/ })
    expect(screen.queryByRole('link', { name: /build-cache/ })).toBeNull()
    expect(screen.getByText('9')).toBeTruthy()
  })

  it('shows a non-production schedule: the tag to copy, the template and the database caveat', async () => {
    const { user } = await connectedWorkspace('ngozi@example.com')
    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    await user.selectOptions(await screen.findByLabelText('Type'), 'R7_schedule_ec2')
    await user.click(await screen.findByRole('link', { name: /Run non-production server staging-api/ }))
    expect(await screen.findByRole('heading', { name: /Run it 07:00 to 20:00, Monday to Friday/ })).toBeTruthy()
    // The schedule is in the workspace's time zone (this one is in the UK).
    expect(screen.getByText('07:00-20:00;MO-FR;Europe/London')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Copy tag value' })).toBeTruthy()
    expect(screen.getByRole('link', { name: /Download the scheduler template/ }).getAttribute('href')).toContain(
      'infra/cloudformation/scheduler/cloudoptics-scheduler.yaml',
    )
    expect(screen.getByText('Stopped outside office hours, so not there for anyone working late')).toBeTruthy()
    expect(screen.getByText('tagged Environment=staging')).toBeTruthy()
    expect(screen.queryByText(/AWS starts a stopped database again by itself/)).toBeNull()

    await user.click(screen.getByRole('link', { name: /All recommendations/ }))
    await user.selectOptions(await screen.findByLabelText('Type'), 'R7_schedule_rds')
    await user.click(await screen.findByRole('link', { name: /Run non-production database qa-orders-db/ }))
    expect(await screen.findByText('07:00-20:00 MO-FR Europe/London')).toBeTruthy()
    expect(screen.getByText(/AWS starts a stopped database again by itself after 7 days/)).toBeTruthy()

    // Office hours come from the rule settings.
    await user.click(screen.getByRole('link', { name: 'Settings' }))
    const end = await screen.findByLabelText('Office hours end')
    await user.clear(end)
    await user.type(end, '18:00')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true))
    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    expect(await screen.findAllByRole('link', { name: /07:00-18:00 weekdays, London time/ })).toHaveLength(2)
  })

  it('shows the billing-only findings: Extended Support and idle NAT gateways', async () => {
    const { user } = await connectedWorkspace('thandi@example.com')
    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    await user.click(await screen.findByRole('link', { name: /reports-db off PostgreSQL 12/ }))
    expect(await screen.findByRole('heading', { name: 'How to fix it' })).toBeTruthy()
    expect(screen.getByText(/Extended Support charge · account 111111111111/)).toBeTruthy()
    expect(screen.getByRole('link', { name: /Open in AWS console/ }).getAttribute('href')).toContain(
      '#database:id=reports-db;is-cluster=false',
    )
    expect(screen.getByText('A version upgrade, so test it on a copy first')).toBeTruthy()
    expect(screen.getByText('Reversible from the snapshot · needs a restart')).toBeTruthy()
    expect(screen.getByText('30 of the last 30')).toBeTruthy()
    expect(screen.getByText(/aws rds create-db-snapshot --db-instance-identifier reports-db/)).toBeTruthy()

    await user.click(screen.getByRole('link', { name: /All recommendations/ }))
    await user.selectOptions(await screen.findByLabelText('Type'), 'R10_idle_nat')
    expect(screen.getAllByRole('link', { name: /\/mo$/ })).toHaveLength(1)
    await user.click(screen.getByRole('link', { name: /Delete idle NAT gateway nat-0d1e2f3a4b5c6d7e8/ }))
    // Scanned with access 2.0.4: no route table sends traffic to it, so no routes risk.
    expect(await screen.findByText('No route tables send traffic to it.')).toBeTruthy()
    expect(screen.queryByText('Route tables may still send traffic to it')).toBeNull()
    expect(screen.getByText('Route tables sending traffic to it')).toBeTruthy()
    expect(screen.getByRole('link', { name: /Open in AWS console/ }).getAttribute('href')).toContain(
      '#NatGatewayDetails:natGatewayId=nat-0d1e2f3a4b5c6d7e8',
    )
  })

  it('shows verified recommendations and their realised savings', async () => {
    const { user } = await connectedWorkspace('ngozi@example.com')
    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    expect(await screen.findByText('Realised savings')).toBeTruthy()
    expect(await screen.findByText(/^2 verified · .+ estimated$/)).toBeTruthy()

    await user.click(screen.getByLabelText('Show done and dismissed'))
    await user.click(await screen.findByRole('link', { name: /analytics-scratch/ }))
    expect(await screen.findByRole('heading', { name: 'Change verified' })).toBeTruthy()
    expect(screen.getAllByText('Verified').length).toBeGreaterThan(0)
    expect(screen.getByText(/The volume is gp3 now\./)).toBeTruthy()
    expect(screen.getByText(/From your bills: .+ a day over 30 days before the change/)).toBeTruthy()

    await user.click(screen.getByRole('link', { name: /All recommendations/ }))
    await user.click(await screen.findByLabelText('Show done and dismissed'))
    await user.click(await screen.findByRole('link', { name: /198\.51\.100\.7/ }))
    expect(await screen.findByText('Estimated realised savings')).toBeTruthy()
  })

  it('assigns, comments, acts on several at once and changes the rules', async () => {
    const { user } = await connectedWorkspace('kofi@example.com')
    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    await user.click(await screen.findByRole('link', { name: /build-cache/ }))
    await user.selectOptions(await screen.findByLabelText('Assigned to'), screen.getByRole('option', { name: /^Me/ }))
    expect(await screen.findByText(/assigned it to/)).toBeTruthy()
    await user.type(screen.getByLabelText('Add a comment'), 'Checking with the data team')
    await user.click(screen.getByRole('button', { name: 'Comment' }))
    expect(await screen.findByText('Checking with the data team')).toBeTruthy()

    await user.click(screen.getByRole('link', { name: /All recommendations/ }))
    await user.selectOptions(await screen.findByLabelText('Assigned to'), 'me')
    expect(screen.getAllByRole('link', { name: /\/mo$/ })).toHaveLength(1)
    await user.selectOptions(screen.getByLabelText('Assigned to'), 'anyone')

    const boxes = screen.getAllByRole('checkbox', { name: /^Select / })
    await user.click(boxes[0] as HTMLElement)
    await user.click(boxes[1] as HTMLElement)
    expect(screen.getByText('2 selected')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Dismiss…' }))
    await user.type(screen.getByLabelText(/Why are you dismissing them/), 'Kept on purpose')
    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.getAllByRole('link', { name: /\/mo$/ })).toHaveLength(8))
    expect(screen.getByRole('link', { name: /Release idle Elastic IP/ })).toBeTruthy()

    await user.click(screen.getByRole('link', { name: 'Settings' }))
    const eip = await screen.findByRole('checkbox', { name: 'Idle Elastic IP' })
    await user.click(eip)
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true))
    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    await screen.findAllByRole('link', { name: /\/mo$/ })
    expect(screen.queryByRole('link', { name: /Release idle Elastic IP/ })).toBeNull()
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

  it('exports the cost table and the recommendation list, and lists the exports on Reports', async () => {
    const saved: string[] = []
    URL.createObjectURL = vi.fn(() => 'blob:csv')
    URL.revokeObjectURL = vi.fn()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download)
    })
    const { user } = await connectedWorkspace('amara@example.com')
    await user.click(screen.getByRole('link', { name: 'Cost Explorer' }))
    expect(await screen.findByRole('cell', { name: /Amazon Relational Database Service/ })).toBeTruthy()
    await user.click(await screen.findByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(saved[0]).toMatch(/^cloudoptics-costs-\d{8}-\d{8}\.csv$/)

    await user.click(screen.getByRole('link', { name: 'Recommendations' }))
    await user.click(await screen.findByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(saved).toHaveLength(2))
    expect(saved[1]).toMatch(/^cloudoptics-recommendations-\d{8}\.csv$/)

    await user.click(screen.getByRole('link', { name: 'Reports' }))
    const list = await screen.findByRole('region', { name: 'Your exports' })
    await waitFor(() => expect(list.querySelectorAll('tbody tr')).toHaveLength(2))
    expect(screen.getAllByText('Ready')).toHaveLength(2)
    click.mockRestore()
  })

  it('reviews a previous month on the Cost Explorer', async () => {
    const { api, tenant, user } = await connectedWorkspace('wanjiru@example.com')
    await user.click(screen.getByRole('link', { name: 'Cost Explorer' }))
    expect(await screen.findByRole('heading', { name: 'Forecast' })).toBeTruthy()
    const [, last] = (await api.costMonths(tenant.id)).months
    if (!last) throw new Error('the demo has ended months')
    const name = new Date(`${last.month}-01T00:00:00Z`).toLocaleDateString('en-GB', {
      month: 'long',
      year: 'numeric',
      timeZone: 'UTC',
    })

    await user.selectOptions(await screen.findByLabelText('Month'), last.month)
    expect(await screen.findByText(name, { selector: 'strong' })).toBeTruthy()
    expect(screen.getByText(/The forecast and AWS credits cover the current month only/)).toBeTruthy()
    expect(screen.getByRole('heading', { name: `Daily spend · ${name}` })).toBeTruthy()
    expect(await screen.findByRole('heading', { name: new RegExp(`Spend by service · ${name}`) })).toBeTruthy()
    expect(screen.queryByRole('button', { name: '90 days' })).toBeNull()
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Forecast' })).toBeNull())
    const report = screen.getByRole('link', { name: `Open the ${name} report` })
    expect(report.getAttribute('href')).toBe(`/reports?month=${last.month}`)
    // Back to this month: the forecast returns.
    await user.selectOptions(screen.getByLabelText('Month'), '')
    expect(await screen.findByRole('heading', { name: 'Forecast' })).toBeTruthy()
  })

  it('lets admins reload a connection’s bills, once an hour', async () => {
    const { user } = await connectedWorkspace('baraka@example.com')
    await user.click(screen.getByRole('link', { name: 'Connections' }))
    await user.click(await screen.findByRole('button', { name: 'Reload bills…' }))
    expect(screen.getByText(/open a support case/)).toBeTruthy()
    expect(screen.getByRole('option', { name: 'All months in the export' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Reload' }))
    expect(await screen.findByText(/Bills reloaded: all months in the export/)).toBeTruthy()
    expect(screen.getAllByText(/: Reloaded$/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/: Unchanged/).length).toBeGreaterThan(0)
    expect(screen.getByText(/You can reload again after/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Reload' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('forecasts with a currency scenario and says what changed', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('kemi@example.com')
    const tenant = await api.createTenant({ name: 'Abuja Apps', countryCode: 'NG' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '565656565656',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'kemi@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    // The Overview says what changed most this month.
    expect(await screen.findByRole('heading', { name: 'What changed' })).toBeTruthy()
    expect(
      await screen.findByText(/^Amazon Elastic Compute Cloud in eu-west-1 rose \$[\d,.]+ · ₦[\d,.]+ \(\+\d+%\) mainly due to EU-BoxUsage:m5\.xlarge\.$/),
    ).toBeTruthy()
    expect(screen.getByText(/^Amazon Simple Storage Service in eu-west-1 fell/)).toBeTruthy()

    await user.click(screen.getByRole('link', { name: /Cost Explorer/ }))
    expect(await screen.findByRole('heading', { name: 'Forecast' })).toBeTruthy()
    const forecast = await api.costForecast(tenant.id)
    expect(forecast.months).toHaveLength(3)
    const nextMonth = forecast.months[0] ?? { amount: 0 }
    // Today's rate, then 10% weaker: 1,530.25 x 1.1.
    expect(screen.getByText('Currency scenario: NGN unchanged')).toBeTruthy()
    const amountToday = `₦${(nextMonth.amount * 1530.25).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    expect(screen.getAllByText(amountToday)).toHaveLength(2)
    const percent = screen.getByLabelText('Movement in percent')
    await user.clear(percent)
    await user.type(percent, '10')
    expect(await screen.findByText('Currency scenario: NGN 10% weaker')).toBeTruthy()
    expect(screen.getByText(/scenario 1 USD = ₦1,683\.28/)).toBeTruthy()
    const amountScenario = `₦${(nextMonth.amount * 1530.25 * 1.1).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    expect(screen.getByText(amountScenario)).toBeTruthy()
    expect(screen.getByText(/from usage change and .* from FX change/)).toBeTruthy()
    expect(screen.getAllByRole('heading', { name: 'What changed' })).toHaveLength(1)
  })

  it('lets admins invite people and cancel invites in settings', async () => {
    const { user } = await connectedWorkspace('chidi@example.com')
    await user.click(screen.getByRole('link', { name: 'Settings' }))
    expect(await screen.findByRole('heading', { name: 'Members' })).toBeTruthy()
    expect(screen.getByRole('combobox', { name: 'Role for chidi@example.com' })).toBeTruthy()
    await user.type(screen.getByLabelText('Invite by email'), 'Claude-Review@Example.com')
    await user.click(screen.getByRole('button', { name: 'Invite' }))
    expect(await screen.findByText(/Invited claude-review@example.com/)).toBeTruthy()
    expect(await screen.findByText(/claude-review@example.com · viewer/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Cancel invite' }))
    await waitFor(() => expect(screen.queryByText(/claude-review@example.com · viewer/)).toBeNull())
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
    expect(await screen.findByRole('heading', { name: 'Move to the single CloudOptics stack' })).toBeTruthy()
    expect(screen.getByText(/sign in to AWS account 343434343434/)).toBeTruthy()
    await user.click(screen.getByRole('link', { name: 'Back to Connections' }))
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    await user.click(await screen.findByRole('button', { name: 'Disconnect…' }))
    expect(screen.getByText(/Disconnect AWS account 343434343434\?/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText(/Disconnected\. Cloudoptics no longer reads this account/)).toBeTruthy()
    const [gone] = await api.listConnections(tenant.id)
    expect(gone?.status).toBe('disconnected')

    const before = (await api.getSetup(tenant.id, gone!.id)).externalId
    await user.click(screen.getByRole('button', { name: 'Reconnect' }))
    expect(await screen.findByRole('heading', { name: 'Move to the single CloudOptics stack' })).toBeTruthy()
    const [conn] = await api.listConnections(tenant.id)
    expect(conn?.status).toBe('pending')
    expect((await api.getSetup(tenant.id, conn!.id)).externalId).not.toBe(before)
  })

  it('offers connections on the older stacks a move to the single stack', async () => {
    const { user } = await connectedWorkspace('amara@example.com')
    await user.click(screen.getByRole('link', { name: 'Connections' }))
    expect(await screen.findByText('Move to the single CloudOptics stack')).toBeTruthy()
    expect(screen.getByText(/covers your bill, recommendations and this account/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Move to the single stack' }))
    expect(await screen.findByRole('heading', { name: 'Move to the single CloudOptics stack' })).toBeTruthy()
    expect(screen.getByText(/Cloudoptics removes the old stacks for you/i)).toBeTruthy()
    expect(screen.getByRole('link', { name: /Open AWS and connect/ })).toBeTruthy()
    await user.click(screen.getByText('Prefer to deploy the template yourself?'))
    expect(screen.getByRole('link', { name: /Download template/ }).getAttribute('href')).toMatch(
      /cloudoptics-access\.yaml$/,
    )
  })

  it('offers payer accounts on the older stacks the whole organization in one move', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('kofi@example.com')
    const tenant = await api.createTenant({ name: 'Accra Apps', countryCode: 'GH' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '565656565656',
      accountType: 'management',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'kofi@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    expect(await screen.findByText(/every account in your organization/)).toBeTruthy()
  })

  it('shows resource access once on the single stack', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('efua@example.com')
    const tenant = await api.createTenant({ name: 'Tema Tech', countryCode: 'GH' })
    const link = await api.createIntent(tenant.id)
    const intent = await api.getIntent(tenant.id, link.intent.id)
    expect(intent.status).toBe('connected')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'efua@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    expect(await screen.findByText(/Resource access in this account/)).toBeTruthy()
    expect(screen.queryByText('Move to the single CloudOptics stack')).toBeNull()

    // With access everywhere, admins can still scan straight away and see what it found.
    await user.click(screen.getByRole('button', { name: 'Scan now' }))
    expect(await screen.findByText(/Last scan .*: 12 resources in 17 regions across 1 of 1 accounts/)).toBeTruthy()

    // A scan still running shows its progress, also after leaving the page and coming back.
    const real = api.resourceSummary.bind(api)
    const running = vi.spyOn(api, 'resourceSummary').mockImplementation(async (t) => {
      const summary = await real(t)
      return {
        ...summary,
        scans: summary.scans.map((s) => ({ ...s, status: 'running' as const, progress: { regionsDone: 10, regionsTotal: 34 } })),
      }
    })
    await user.click(screen.getByRole('link', { name: 'Overview' }))
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    expect(await screen.findByText('Scanning resources: 10 of 34 regions')).toBeTruthy()
    expect(screen.getByRole('progressbar', { name: 'Resource scan' }).getAttribute('aria-valuenow')).toBe('29')
    expect((screen.getByRole('button', { name: 'Scanning…' }) as HTMLButtonElement).disabled).toBe(true)
    await user.click(screen.getByRole('link', { name: 'Overview' }))
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    expect(await screen.findByText('Scanning resources: 10 of 34 regions')).toBeTruthy()

    // When it finishes, the page shows the result without a reload.
    running.mockRestore()
    expect(
      await screen.findByText(/Last scan .*: 12 resources/, undefined, { timeout: 5_000 }),
    ).toBeTruthy()
    expect(screen.queryByRole('progressbar')).toBeNull()
  })

  it('notices a stack update without reloading the page, and refreshes on request', async () => {
    const { api } = renderApp('/sign-in')
    await api.signIn('efua@example.com')
    const tenant = await api.createTenant({ name: 'Tema Tech', countryCode: 'GH' })
    const link = await api.createIntent(tenant.id)
    const intent = await api.getIntent(tenant.id, link.intent.id)
    expect(intent.status).toBe('connected')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Work email'), 'efua@example.com')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(await screen.findByRole('link', { name: 'Connections' }))
    expect(await screen.findByText(/Resource access in this account/)).toBeTruthy()
    expect(screen.queryByText('Move to the single CloudOptics stack')).toBeNull()

    // An update is offered while AWS is still updating the stack ...
    const real = api.getSetup.bind(api)
    let updated = false
    vi.spyOn(api, 'getSetup').mockImplementation(async (t, c) => {
      const s = await real(t, c)
      return updated ? s : { ...s, updateAvailable: true, latestStackVersion: '9.9.9' }
    })
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Update Cloudoptics access to version 9.9.9')).toBeTruthy()
    // ... and once it is done, Refresh (or the page's own check) shows it without a reload.
    updated = true
    const calls = vi.spyOn(api, 'listConnections')
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(/Cloudoptics access is up to date/)).toBeTruthy()
    expect(calls).toHaveBeenCalled()
    expect(screen.getByText(/^Updated /)).toBeTruthy()
  })

})
