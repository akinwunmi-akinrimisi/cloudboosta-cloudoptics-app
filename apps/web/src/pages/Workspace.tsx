import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Plus } from 'lucide-react'
import { useState } from 'react'

import { useApi, useCurrentTenantId, useMe } from '../app/context'
import type { Connection, CostDimension, CostSummary, CurrentRate, RecommendationSummary } from '../api/types'
import { DailySpendChart, ShareList } from '../components/charts'
import { Alert, Button, Card, StatusPill, cx } from '../components/ui'
import { ACCESS_STACK_NAME } from '../api/types'
import { CopyValue } from './Onboarding'
import type { FxRate } from '../lib/currency'
import { formatDual, formatMoney } from '../lib/currency'

export function useWorkspace() {
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

/** Formats USD amounts in the viewer's currency mode, with today's rate for the workspace. */
export function useMoney(): {
  money: (usd: number) => string
  rate: CurrentRate | undefined
  currency: string
} {
  const api = useApi()
  const { me, tenantId, tenant } = useWorkspace()
  const rate = useQuery({
    queryKey: ['fx-current', tenantId],
    queryFn: () => api.fxCurrent(tenantId as string),
    enabled: tenantId !== null,
  })
  const view = me?.preferredCurrencyView ?? 'both'
  const quote = rate.data?.quote
  const fx: FxRate | null =
    quote && quote.currency !== 'USD'
      ? {
          currency: quote.currency,
          rate: quote.rate,
          estimated: quote.estimated,
        }
      : null
  return {
    money: (usd: number) => formatDual(usd, view, fx),
    rate: rate.data,
    currency: tenant.data?.displayCurrency ?? 'USD',
  }
}

export function Kpi({ label, value, note }: { label: string; value: string; note: string }) {
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
  const { tenantId, tenant, connections } = useWorkspace()
  const { money, rate, currency } = useMoney()
  const summary = useQuery({
    queryKey: ['cost-summary', tenantId],
    queryFn: () => api.costSummary(tenantId as string),
    enabled: tenantId !== null,
  })
  const savings = useQuery({
    queryKey: ['recommendation-summary', tenantId],
    queryFn: () => api.recommendationSummary(tenantId as string),
    enabled: tenantId !== null,
  })
  const waiting = summary.data?.status === 'waiting'

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
          Connected. AWS delivers the first Cost and Usage Report within 24 hours; your costs and recommendations appear
          here as soon as it lands.
        </Alert>
      )}
      {summary.data?.reconciliation?.status === 'mismatch' && (
        <Alert tone="warning">
          {reconciliationMessage(summary.data.reconciliation.month, summary.data.reconciliation.differencePct)}
        </Alert>
      )}
      {summary.data?.status === 'provisional' && (
        <Alert tone="info">
          Provisional figures from AWS Cost Explorer. Full detail arrives with the first Cost and Usage Report, within
          24 hours of connecting.
        </Alert>
      )}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {costTiles(summary.data, money).map((tile) => (
          <Kpi key={tile.label} {...tile} />
        ))}
        <Kpi {...savingsTile(savings.data, money)} />
        <Kpi {...rateTile(currency, rate)} />
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
  const daily = useQuery({
    queryKey: ['cost-daily', tenantId],
    queryFn: () => api.costDaily(tenantId),
  })
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

export const DIMENSIONS: { id: CostDimension; label: string }[] = [
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

function savingsTile(
  summary: RecommendationSummary | undefined,
  money: (usd: number) => string,
): { label: string; value: string; note: string } {
  const label = 'Savings identified'
  if (!summary || summary.lastEvaluatedAt === null) {
    return {
      label,
      value: '—',
      note: 'Appears after the first resource scan (see Connections)',
    }
  }
  if (summary.openCount === 0) return { label, value: money(0), note: 'No open recommendations' }
  const n = summary.openCount
  return {
    label,
    value: `${money(summary.savingsUsdMonth)}/mo`,
    note: `${n} open recommendation${n === 1 ? '' : 's'}`,
  }
}

function reconciliationMessage(month: string, differencePct: number | null): string {
  const name = new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', {
    month: 'long',
    timeZone: 'UTC',
  })
  const by = differencePct === null ? '' : ` by ${Math.abs(differencePct).toFixed(1)}%`
  return `${name} totals differ from AWS Cost Explorer${by}. We're reconciling; figures may change.`
}

/** The exchange-rate tile: today's rate for the workspace currency, and where it came from. */
function rateTile(currency: string, current: CurrentRate | undefined): { label: string; value: string; note: string } {
  const label = 'Exchange rate'
  if (currency === 'USD') return { label, value: 'USD', note: 'Amounts are shown in US dollars' }
  const quote = current?.quote
  if (!current) return { label, value: '—', note: 'Loading…' }
  if (!quote)
    return {
      label,
      value: '—',
      note: `No ${currency} rate set yet; amounts show in USD`,
    }
  const value = `1 USD = ${quote.estimated ? '≈' : ''}${formatMoney(quote.rate, currency)}`
  const note =
    quote.source === 'feed'
      ? 'Estimated from a reference feed until Cloudoptics staff set a rate'
      : `${quote.basis === 'official' ? 'Official' : 'Market'} rate set by Cloudoptics staff · ${shortDate(quote.rateDate)}${quote.estimated ? ' (carried forward)' : ''}`
  return { label, value, note }
}

export function Connections() {
  const { me, tenantId, connections } = useWorkspace()
  const isAdmin = me?.memberships.find((m) => m.tenantId === tenantId)?.role === 'admin'
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
          {isAdmin && tenantId !== null && <ConnectionActions connection={c} tenantId={tenantId} />}
          {c.status !== 'disconnected' && c.permissionChecks.length > 0 && (
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
          {isAdmin && tenantId !== null && c.status !== 'disconnected' && c.stackName !== ACCESS_STACK_NAME && (
            <MoveToSingleStack connection={c} />
          )}
          {isAdmin && tenantId !== null && c.stackName === ACCESS_STACK_NAME && c.status !== 'disconnected' && (
            <StackUpdate connection={c} tenantId={tenantId} />
          )}
          {isAdmin &&
            tenantId !== null &&
            c.stackName === ACCESS_STACK_NAME &&
            (c.status === 'healthy' || c.status === 'degraded') && <ResourceAccess connection={c} tenantId={tenantId} />}
        </Card>
      ))}
      {tenantId !== null && connections.data && connections.data.length > 0 && <Accounts tenantId={tenantId} />}
    </div>
  )
}

/** Check again, set the stack up again, disconnect, or reconnect (admins). */
function ConnectionActions({ connection: c, tenantId }: { connection: Connection; tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [confirming, setConfirming] = useState(false)
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['connections', tenantId] })
    void qc.invalidateQueries({ queryKey: ['accounts', tenantId] })
  }
  const check = useMutation({ mutationFn: () => api.validateConnection(tenantId, c.id), onSettled: refresh })
  const disconnect = useMutation({
    mutationFn: () => api.disconnectConnection(tenantId, c.id),
    onSuccess: () => setConfirming(false),
    onSettled: refresh,
  })
  const reconnect = useMutation({
    mutationFn: () =>
      api.createConnection(tenantId, {
        awsAccountId: c.awsAccountId,
        accountType: c.accountType,
        createCurExport: c.createCurExport,
      }),
    onSuccess: (setup) => {
      refresh()
      void navigate({ to: '/onboarding', search: { connection: setup.connection.id } })
    },
  })
  const error = check.error ?? disconnect.error ?? reconnect.error
  const busy = check.isPending || disconnect.isPending || reconnect.isPending

  if (c.status === 'disconnected') {
    return (
      <div className="mt-4 flex flex-col gap-2">
        <p className="text-sm text-secondary">
          Disconnected. Cloudoptics no longer reads this account; past costs stay.
        </p>
        {error && <Alert tone="error">{error.message}</Alert>}
        <div>
          <Button variant="secondary" disabled={busy} onClick={() => reconnect.mutate()}>
            Reconnect
          </Button>
        </div>
      </div>
    )
  }
  return (
    <div className="mt-4 flex flex-col gap-3">
      {error && <Alert tone="error">{error.message}</Alert>}
      {confirming ? (
        <div className="flex flex-col gap-2 rounded-sm bg-surface-2 p-3 text-sm">
          <p>
            Disconnect AWS account {c.awsAccountId}? Cloudoptics stops reading it and its open recommendations close.
            Past costs stay. To remove our access completely, also delete the{' '}
            <code className="font-mono text-xs">{c.stackName}</code> stack in{' '}
            <a className="text-accent underline" href={STACKS_URL} target="_blank" rel="noreferrer">
              CloudFormation
            </a>
            . AWS keeps the S3 bucket with your bills, and Cloudoptics reuses it if you connect again.
          </p>
          <div className="flex gap-2">
            <Button disabled={busy} onClick={() => disconnect.mutate()}>
              Disconnect
            </Button>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" disabled={busy || c.status === 'validating'} onClick={() => check.mutate()}>
            {check.isPending || c.status === 'validating' ? 'Checking…' : 'Check again'}
          </Button>
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => void navigate({ to: '/onboarding', search: { connection: c.id } })}
          >
            Set up again
          </Button>
          <Button variant="ghost" disabled={busy} onClick={() => setConfirming(true)}>
            Disconnect…
          </Button>
        </div>
      )}
    </div>
  )
}

const STACKS_URL =
  'https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks?filteringText=cloudoptics'

/** Connections still on the older billing and resource stacks: one stack replaces them. */
function MoveToSingleStack({ connection: c }: { connection: Connection }) {
  const navigate = useNavigate()
  return (
    <div className="mt-4 flex flex-col gap-3 rounded-sm bg-accent-soft p-3 text-sm">
      <div>
        <p className="font-medium">Move to the single CloudOptics stack</p>
        <p className="text-secondary">
          One stack now covers your bill, recommendations and{' '}
          {c.accountType === 'management' ? 'every account in your organization' : 'this account'}, and gets future
          updates in place. It takes one click in AWS, Cloudoptics removes the old stacks for you, and your costs and
          history stay.
        </p>
      </div>
      <div>
        <Button onClick={() => void navigate({ to: '/onboarding', search: { connection: c.id } })}>
          Move to the single stack
        </Button>
      </div>
    </div>
  )
}

/** A newer version of the single stack: update it in place in AWS. */
function StackUpdate({ connection: c, tenantId }: { connection: Connection; tenantId: string }) {
  const api = useApi()
  const setup = useQuery({ queryKey: ['setup', tenantId, c.id], queryFn: () => api.getSetup(tenantId, c.id) })
  const s = setup.data
  if (setup.error) {
    return <Alert tone="error">Could not check for a newer Cloudoptics stack: {setup.error.message}</Alert>
  }
  if (!s) return null
  if (!s.updateAvailable) {
    return (
      <p className="mt-4 text-sm text-secondary">
        Cloudoptics access is up to date (version {c.stackVersion ?? s.latestStackVersion}). Nothing to update in AWS.
      </p>
    )
  }
  return (
    <div className="mt-4 flex flex-col gap-3 rounded-sm bg-accent-soft p-3 text-sm">
      <div>
        <p className="font-medium">Update Cloudoptics access to version {s.latestStackVersion}</p>
        <p className="text-secondary">
          A newer version of the {ACCESS_STACK_NAME} stack is ready. Until you update, everything that works today keeps
          working.
        </p>
      </div>
      <div>
        <a
          href={s.stackUpdateUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex h-9 items-center justify-center gap-2 rounded-sm bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover"
        >
          Update Cloudoptics access <ExternalLink size={16} />
        </a>
      </div>
      <p className="text-secondary">
        AWS opens with the new version already chosen, signed in to account {c.awsAccountId}. Choose Next until the last
        page, tick the box that starts “I acknowledge” and choose Submit. Cloudoptics notices when it is done.
      </p>
      <details className="text-secondary">
        <summary className="cursor-pointer">AWS asks for a template?</summary>
        <div className="mt-2 flex flex-col gap-2">
          <p>
            Choose <strong>Replace existing template</strong> and paste this address:
          </p>
          <CopyValue label="Template address" value={s.accessTemplateUrl} />
        </div>
      </details>
    </div>
  )
}

/** Resource access on the single stack: member accounts get it on their own; Check now. */
function ResourceAccess({ connection: c, tenantId }: { connection: Connection; tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: ['accounts', tenantId],
    queryFn: () => api.listAccounts(tenantId),
  })
  const accounts = (list.data?.accounts ?? []).filter((a) => a.connectionId === c.id && a.orgStatus !== 'NOT_LISTED')
  const missing = accounts.filter((a) => !a.resourceAccess)
  const scan = useMutation({
    mutationFn: () => api.scanResources(tenantId, c.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['accounts', tenantId] })
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
    },
  })
  if (accounts.length === 0) return null
  if (missing.length === 0) {
    return (
      <p className="mt-4 text-sm text-good-ink">
        Resource access in {accounts.length === 1 ? 'this account' : `all ${accounts.length} accounts`}.{' '}
        {scan.data?.status === 'queued'
          ? 'Scanning resources now; recommendations appear in a few minutes.'
          : 'Cloudoptics scans resources every 12 hours for recommendations.'}
      </p>
    )
  }
  const refused = scan.data?.accounts.filter((a) => !a.access) ?? []
  return (
    <div className="mt-4 flex flex-col gap-3 rounded-sm bg-surface-2 p-3 text-sm">
      <div>
        <p className="font-medium">
          Resource access in {accounts.length - missing.length} of {accounts.length} accounts
        </p>
        <p className="text-secondary">
          {c.accountType === 'management'
            ? 'Member accounts get access on their own as AWS reaches them, usually within 10 minutes, and new accounts get it when they join. Choose Check now to see where each one is.'
            : 'Access is added by the CloudOptics stack. Check now to look again.'}
        </p>
      </div>
      {scan.error && <Alert tone="error">{scan.error.message}</Alert>}
      {refused.length > 0 && (
        <Alert tone="warning">
          <ul className="flex flex-col gap-1">
            {refused.map((a) => (
              <li key={a.awsAccountId}>
                <span className="font-mono">{a.awsAccountId}</span>:{' '}
                {a.rollout ?? 'No access yet. Check again in a few minutes.'}
              </li>
            ))}
          </ul>
        </Alert>
      )}
      <div>
        <Button variant="secondary" disabled={scan.isPending} onClick={() => scan.mutate()}>
          {scan.isPending ? 'Checking…' : 'Check now'}
        </Button>
      </div>
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
  const list = useQuery({
    queryKey: ['accounts', tenantId],
    queryFn: () => api.listAccounts(tenantId),
  })
  if (!list.data || list.data.accounts.length === 0) return null
  const { coverage, accounts } = list.data
  return (
    <Card>
      <h2 className="text-base font-semibold">AWS accounts</h2>
      <p className="mt-1 text-sm text-secondary">
        {coverage.withCostData} of {coverage.accounts} accounts have cost data · {coverage.withResourceAccess} of{' '}
        {coverage.accounts} have resource access for recommendations
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
