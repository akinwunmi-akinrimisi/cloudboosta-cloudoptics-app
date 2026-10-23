import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { useState } from 'react'

import { useApi, useCurrentTenantId, useMe } from '../app/context'
import type { CostDimension, CostSummary, CurrentRate } from '../api/types'
import { DailySpendChart, ShareList } from '../components/charts'
import { Alert, Card, StatusPill, cx } from '../components/ui'
import type { FxRate } from '../lib/currency'
import { formatDual, formatMoney } from '../lib/currency'

function useWorkspace() {
  const api = useApi()
  const { data: me } = useMe()
  const [tenantId] = useCurrentTenantId(me)
  const tenant = useQuery({
    queryKey: ['tenant', tenantId],
    queryFn: () => api.getTenant(tenantId as string),
    enabled: tenantId !== null,
  })
  const connections = useQuery({
    queryKey: ['connections', tenantId],
    queryFn: () => api.listConnections(tenantId as string),
    enabled: tenantId !== null,
  })
  return { me, tenantId, tenant, connections }
}

function Kpi({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <Card className="p-5">
      <p className="text-sm text-secondary">{label}</p>
      <p className="mt-2 text-2xl font-semibold tabular-nums">{value}</p>
      <p className="mt-1 text-xs text-muted">{note}</p>
    </Card>
  )
}

function shortDate(isoDate: string): string {
  return new Date(`${isoDate}T00:00:00Z`).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  })
}

/** The spend and forecast tiles; "—" until the first Cost and Usage Report is loaded. */
function costTiles(
  summary: CostSummary | undefined,
  money: (usd: number) => string,
): { label: string; value: string; note: string }[] {
  const waiting =
    summary?.lastLoadStatus === 'failed'
      ? 'The latest report failed our checks; we will retry with the next delivery'
      : 'Waiting for first data'
  const mtd = summary?.monthToDate
  const forecast = summary?.forecast
  const hasData = summary?.status === 'ready' || summary?.status === 'provisional'
  const source = summary?.source === 'cost_explorer' ? 'Provisional (Cost Explorer)' : 'Amortised, after credits'
  return [
    {
      label: 'Month-to-date spend',
      value: mtd ? money(mtd.effective) : '—',
      note:
        mtd && summary?.dataThrough
          ? `${source} · data to ${shortDate(summary.dataThrough)}`
          : hasData
            ? 'No data for this month yet'
            : waiting,
    },
    {
      label: 'Forecast (month end)',
      value: forecast ? money(forecast.amount) : '—',
      note: forecast
        ? `Estimate from the daily run rate (${forecast.daysCovered} of ${forecast.daysInMonth} days)`
        : hasData
          ? 'Needs data for this month'
          : waiting,
    },
  ]
}

export function Overview() {
  const api = useApi()
  const { me, tenantId, tenant, connections } = useWorkspace()
  const summary = useQuery({
    queryKey: ['cost-summary', tenantId],
    queryFn: () => api.costSummary(tenantId as string),
    enabled: tenantId !== null,
  })
  const rate = useQuery({
    queryKey: ['fx-current', tenantId],
    queryFn: () => api.fxCurrent(tenantId as string),
    enabled: tenantId !== null,
  })
  const waiting = summary.data?.status === 'waiting'
  const view = me?.preferredCurrencyView ?? 'both'
  const currency = tenant.data?.displayCurrency ?? 'USD'
  const quote = rate.data?.quote
  const fx: FxRate | null =
    quote && quote.currency !== 'USD'
      ? { currency: quote.currency, rate: quote.rate, estimated: quote.estimated }
      : null
  const money = (usd: number) => formatDual(usd, view, fx)

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Overview</h1>
        <p className="text-sm text-secondary">
          {tenant.data ? `${tenant.data.name} · ${currency} · ${tenant.data.timeZone}` : 'Loading…'}
        </p>
      </div>
      {connections.data && connections.data.length === 0 && (
        <Alert tone="info">
          No AWS account is connected yet.{' '}
          <Link to="/onboarding" search={{ step: 1 }} className="font-medium text-accent underline">
            Connect one
          </Link>{' '}
          to see your costs.
        </Alert>
      )}
      {waiting && connections.data?.some((c) => c.status === 'healthy' || c.status === 'degraded') && (
        <Alert tone="info">
          Connected. AWS delivers the first Cost and Usage Report within 24 hours; your costs and
          recommendations appear here as soon as it lands.
        </Alert>
      )}
      {summary.data?.reconciliation?.status === 'mismatch' && (
        <Alert tone="warning">
          {reconciliationMessage(summary.data.reconciliation.month, summary.data.reconciliation.differencePct)}
        </Alert>
      )}
      {summary.data?.status === 'provisional' && (
        <Alert tone="info">
          Provisional figures from AWS Cost Explorer. Full detail arrives with the first Cost and Usage
          Report, within 24 hours of connecting.
        </Alert>
      )}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {costTiles(summary.data, money).map((tile) => (
          <Kpi key={tile.label} {...tile} />
        ))}
        <Kpi label="Savings identified" value="—" note="Recommendations start after first data" />
        <Kpi {...rateTile(currency, rate.data)} />
      </div>
      {tenantId !== null && (summary.data?.status === 'ready' || summary.data?.status === 'provisional') && (
        <div className="grid gap-4 lg:grid-cols-3">
          <SpendTrend tenantId={tenantId} />
          <TopSpend tenantId={tenantId} />
        </div>
      )}
    </div>
  )
}

function SpendTrend({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const daily = useQuery({ queryKey: ['cost-daily', tenantId], queryFn: () => api.costDaily(tenantId) })
  const points = daily.data?.points ?? []
  return (
    <Card className="p-5 lg:col-span-2">
      <h2 className="text-base font-semibold">Daily spend</h2>
      <p className="mb-4 text-xs text-muted">
        {daily.data?.source === 'cost_explorer' ? 'This month so far, provisional' : `Last ${points.length} days`} ·
        amortised, after credits
      </p>
      {daily.isLoading && <p className="text-sm text-secondary">Loading…</p>}
      {daily.data && points.length === 0 && <p className="text-sm text-secondary">No daily data yet.</p>}
      {points.length > 0 && <DailySpendChart points={points} currency="USD" />}
    </Card>
  )
}

const DIMENSIONS: { id: CostDimension; label: string }[] = [
  { id: 'service', label: 'Service' },
  { id: 'account', label: 'Account' },
  { id: 'region', label: 'Region' },
]

function TopSpend({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const [dimension, setDimension] = useState<CostDimension>('service')
  const breakdown = useQuery({
    queryKey: ['cost-breakdown', tenantId, dimension],
    queryFn: () => api.costBreakdown(tenantId, dimension),
  })
  const items = breakdown.data?.items ?? []
  return (
    <Card className="p-5">
      <div className="mb-4 flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold">Top spend this month</h2>
        <div role="group" aria-label="Group by" className="flex rounded-sm border border-border text-xs">
          {DIMENSIONS.map((d) => (
            <button
              key={d.id}
              type="button"
              aria-pressed={dimension === d.id}
              onClick={() => setDimension(d.id)}
              className={cx(
                'px-2 py-1',
                dimension === d.id ? 'bg-accent-soft font-medium text-primary' : 'text-secondary hover:bg-surface-2',
              )}
            >
              {d.label}
            </button>
          ))}
        </div>
      </div>
      {breakdown.isLoading && <p className="text-sm text-secondary">Loading…</p>}
      {breakdown.data && items.length === 0 && (
        <p className="text-sm text-secondary">
          {breakdown.data.source === 'cost_explorer'
            ? 'Available once the first Cost and Usage Report is loaded.'
            : 'No spend this month yet.'}
        </p>
      )}
      {items.length > 0 && <ShareList items={items} currency="USD" />}
      {items.length > 0 && <p className="mt-4 text-xs text-muted">Excludes tax</p>}
    </Card>
  )
}

function reconciliationMessage(month: string, differencePct: number | null): string {
  const name = new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', { month: 'long', timeZone: 'UTC' })
  const by = differencePct === null ? '' : ` by ${Math.abs(differencePct).toFixed(1)}%`
  return `${name} totals differ from AWS Cost Explorer${by}. We're reconciling; figures may change.`
}

/** The exchange-rate tile: today's rate for the workspace currency, and where it came from. */
function rateTile(currency: string, current: CurrentRate | undefined): { label: string; value: string; note: string } {
  const label = 'Exchange rate'
  if (currency === 'USD') return { label, value: 'USD', note: 'Amounts are shown in US dollars' }
  const quote = current?.quote
  if (!current) return { label, value: '—', note: 'Loading…' }
  if (!quote) return { label, value: '—', note: `No ${currency} rate set yet; amounts show in USD` }
  const value = `1 USD = ${quote.estimated ? '≈' : ''}${formatMoney(quote.rate, currency)}`
  const note =
    quote.source === 'feed'
      ? 'Estimated from a reference feed until Cloudoptics staff set a rate'
      : `${quote.basis === 'official' ? 'Official' : 'Market'} rate set by Cloudoptics staff · ${shortDate(quote.rateDate)}${quote.estimated ? ' (carried forward)' : ''}`
  return { label, value, note }
}

export function Connections() {
  const { tenantId, connections } = useWorkspace()
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Connections</h1>
          <p className="text-sm text-secondary">AWS accounts Cloudoptics can read. Read-only, always.</p>
        </div>
        <Link
          to="/onboarding"
          search={{ step: 1 }}
          className="inline-flex h-9 items-center gap-2 rounded-sm bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover"
        >
          <Plus size={16} /> Connect an account
        </Link>
      </div>
      {connections.isLoading && <p className="text-sm text-secondary">Loading…</p>}
      {connections.data?.length === 0 && <Alert tone="info">No accounts connected yet.</Alert>}
      {connections.data?.map((c) => (
        <Card key={c.id}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="font-medium">AWS account {c.awsAccountId}</p>
              <p className="text-xs text-muted">
                {c.accountType === 'management' ? 'Management (payer) account' : 'Standalone account'}
                {c.lastValidatedAt && ` · checked ${new Date(c.lastValidatedAt).toLocaleString()}`}
              </p>
            </div>
            <StatusPill status={c.status} />
          </div>
          {c.permissionChecks.length > 0 && (
            <ul className="mt-4 divide-y divide-border">
              {c.permissionChecks.map((check) => (
                <li key={check.id} className="flex items-start justify-between gap-4 py-2">
                  <div>
                    <p className="text-sm">{check.label}</p>
                    <p className="text-xs text-muted">{check.hint ?? check.message}</p>
                  </div>
                  <StatusPill status={check.status} />
                </li>
              ))}
            </ul>
          )}
        </Card>
      ))}
      {tenantId !== null && connections.data && connections.data.length > 0 && <Accounts tenantId={tenantId} />}
    </div>
  )
}

function orgStatusLabel(status: string | null): string | null {
  if (!status || status === 'ACTIVE') return null
  if (status === 'NOT_LISTED') return 'No longer in the organization'
  return status.charAt(0) + status.slice(1).toLowerCase().replace(/_/g, ' ')
}

/** Accounts from the AWS Organization and the bills, with coverage (PRD US-1.3). */
function Accounts({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const list = useQuery({ queryKey: ['accounts', tenantId], queryFn: () => api.listAccounts(tenantId) })
  if (!list.data || list.data.accounts.length === 0) return null
  const { coverage, accounts } = list.data
  return (
    <Card>
      <h2 className="text-base font-semibold">AWS accounts</h2>
      <p className="mt-1 text-sm text-secondary">
        {coverage.withCostData} of {coverage.accounts} accounts have cost data ·{' '}
        {coverage.withResourceAccess} of {coverage.accounts} have resource access for recommendations
      </p>
      <ul className="mt-4 divide-y divide-border">
        {accounts.map((a) => {
          const status = orgStatusLabel(a.orgStatus)
          return (
            <li key={a.awsAccountId} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <div>
                <p className="text-sm">
                  {a.name ?? 'Unnamed account'} <span className="text-muted tabular-nums">{a.awsAccountId}</span>
                </p>
                <p className="text-xs text-muted">
                  {a.accountType === 'management'
                    ? 'Management (payer)'
                    : a.accountType === 'member'
                      ? 'Member'
                      : 'Standalone'}
                  {status && ` · ${status}`}
                </p>
              </div>
              <div className="flex gap-2 text-xs">
                <span className={a.hasCostData ? 'text-good-ink' : 'text-muted'}>
                  {a.hasCostData ? 'Costs' : 'No costs yet'}
                </span>
                <span className={a.resourceAccess ? 'text-good-ink' : 'text-muted'}>
                  {a.resourceAccess ? 'Resources' : 'No resource access'}
                </span>
              </div>
            </li>
          )
        })}
      </ul>
    </Card>
  )
}

export function ComingSoon({ title, sprint, what }: { title: string; sprint: string; what: string }) {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold">{title}</h1>
      <Card>
        <p className="text-sm text-secondary">{what}</p>
        <p className="mt-2 text-xs text-muted">Arrives in {sprint}.</p>
      </Card>
    </div>
  )
}
