import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { useState } from 'react'

import { useApi, useCurrentTenantId, useMe } from '../app/context'
import type { CostDimension, CostSummary } from '../api/types'
import { DailySpendChart, ShareList } from '../components/charts'
import { Alert, Card, StatusPill, cx } from '../components/ui'
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
function costTiles(summary: CostSummary | undefined): { label: string; value: string; note: string }[] {
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
      value: mtd ? formatMoney(mtd.effective, 'USD') : '—',
      note:
        mtd && summary?.dataThrough
          ? `${source} · data to ${shortDate(summary.dataThrough)}`
          : hasData
            ? 'No data for this month yet'
            : waiting,
    },
    {
      label: 'Forecast (month end)',
      value: forecast ? formatMoney(forecast.amount, 'USD') : '—',
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
  const waiting = summary.data?.status === 'waiting'
  const view = me?.preferredCurrencyView ?? 'both'
  const currency = tenant.data?.displayCurrency ?? 'USD'

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
      {summary.data?.status === 'provisional' && (
        <Alert tone="info">
          Provisional figures from AWS Cost Explorer. Full detail arrives with the first Cost and Usage
          Report, within 24 hours of connecting.
        </Alert>
      )}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {costTiles(summary.data).map((tile) => (
          <Kpi key={tile.label} {...tile} />
        ))}
        <Kpi label="Savings identified" value="—" note="Recommendations start after first data" />
        <Kpi
          label="Example: USD 1,000 shows as"
          value={formatDual(1000, view, currency === 'USD' ? null : { currency, rate: sampleRate(currency) })}
          note="Illustrative rate until staff set today's rate"
        />
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

// Placeholder rates for the example tile only; real rates are set by staff (FR-FX-06).
function sampleRate(currency: string): number {
  const rates: Record<string, number> = {
    NGN: 1500,
    KES: 129,
    GHS: 15.5,
    ZAR: 18,
    EGP: 48,
    RWF: 1400,
    UGX: 3700,
    TZS: 2600,
    GBP: 0.79,
    EUR: 0.92,
  }
  return rates[currency] ?? 1
}

export function Connections() {
  const { connections } = useWorkspace()
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
    </div>
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
