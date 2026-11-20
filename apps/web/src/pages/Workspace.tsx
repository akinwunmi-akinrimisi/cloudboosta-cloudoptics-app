import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Plus, RefreshCw } from 'lucide-react'
import { Suspense, lazy, useEffect, useId, useRef, useState } from 'react'

import { useApi, useCurrentTenantId, useMe } from '../app/context'
import type {
  Connection,
  CostDimension,
  DataDeletion,
  CostSummary,
  CurrentRate,
  LastScan,
  RecommendationSummary,
} from '../api/types'
import { DailySpendChart, ShareList } from '../components/charts'
import { useEscapeToClose } from '../components/overlay'
import { Alert, Button, Card, StatusPill, cx } from '../components/ui'
import { ACCESS_STACK_NAME } from '../api/types'
import { CopyValue } from './Onboarding'
import { DeleteConnectionData, verificationSummary } from './DataDeletion'
import { SettingsLayout } from './SettingsNav'
import { WhatChanged } from './Forecast'
import { ReloadBills } from './ReloadBills'
import type { FxRate } from '../lib/currency'
import { formatDual, formatMoney } from '../lib/currency'
import type { CloudWords, Provider } from '../lib/providers'
import {
  PROVIDER_LABEL,
  accountLabel,
  accountNoun,
  cloudWords,
  connectionKind,
  connectionLabel,
  providerOf,
  supports,
  workspaceProviders,
} from '../lib/providers'
import { ReconnectDigitalOcean, ReplaceDigitalOceanToken } from './connect/DigitalOceanConnect'
import { ProviderBadge } from '../components/providers'

// Fetched only when an admin opens Set up again on an Azure connection.
const AzureSetupAgain = lazy(() => import('./AzureSetupAgain'))

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
    // A check runs on the server after a stack is created or updated: follow it to its result.
    refetchInterval: (q) =>
      q.state.data?.some((c) => c.status === 'pending' || c.status === 'validating') ? 3_000 : false,
  })
  return { me, tenantId, tenant, connections }
}

/** Wording from the workspace's connected providers (AWS alone reads as it always has). */
export function useCloudWords(): CloudWords {
  const { connections } = useWorkspace()
  return cloudWords(workspaceProviders(connections.data ?? []))
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
  words: CloudWords,
): { label: string; value: string; note: string }[] {
  const waiting =
    summary?.lastLoadStatus === 'failed'
      ? 'The latest report failed our checks; we will retry with the next delivery'
      : 'Waiting for first data'
  const mtd = summary?.monthToDate
  const forecast = summary?.forecast
  const hasData = summary?.status === 'ready' || summary?.status === 'provisional'
  const source =
    summary?.source === 'cost_explorer'
      ? words.awsOnly
        ? 'Provisional (Cost Explorer)'
        : 'Provisional'
      : 'Amortised, after credits'
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
        ? `Last ${forecast.runRateDays} days' run rate${forecast.monthlyCharges ? ' + monthly charges' : ''} (${forecast.daysCovered} of ${forecast.daysInMonth} days)`
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
  const credits = useQuery({
    queryKey: ['credits', tenantId],
    queryFn: () => api.getCredits(tenantId as string),
    enabled: tenantId !== null,
  })
  const waiting = summary.data?.status === 'waiting'
  const words = cloudWords(workspaceProviders(connections.data ?? []))

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
          No cloud account is connected yet.{' '}
          <Link to="/onboarding" search={{ step: 1 }} className="font-medium text-accent underline">
            Connect one
          </Link>{' '}
          to see your costs.
        </Alert>
      )}
      {waiting && connections.data?.some((c) => c.status === 'healthy' || c.status === 'degraded') && (
        <Alert tone="info">
          {words.awsOnly
            ? 'Connected. AWS delivers the first Cost and Usage Report within 24 hours; your costs and recommendations appear here as soon as it lands.'
            : 'Connected. Your costs and recommendations appear here as soon as the first bill is loaded.'}
        </Alert>
      )}
      {summary.data?.reconciliation?.status === 'mismatch' && (
        <Alert tone="warning">
          {reconciliationMessage(summary.data.reconciliation.month, summary.data.reconciliation.differencePct, words)}
        </Alert>
      )}
      {credits.data && (credits.data.status === 'ending_soon' || credits.data.status === 'used_up') && (
        <Alert tone="warning">
          {credits.data.status === 'used_up'
            ? `Your ${words.single ? words.credits : 'credits'} are used up. `
            : `Your ${words.single ? words.credits : 'credits'} ${credits.data.expiresFirst ? 'expire' : 'run out'} in about ${credits.data.daysLeft} days. `}
          <Link to="/costs" className="font-medium underline">
            See the credits runway
          </Link>
        </Alert>
      )}
      {summary.data?.status === 'provisional' && (
        <Alert tone="info">
          {words.awsOnly
            ? 'Provisional figures from AWS Cost Explorer. Full detail arrives with the first Cost and Usage Report, within 24 hours of connecting.'
            : `Provisional figures from ${words.provisional}. Full detail arrives with the first bill.`}
        </Alert>
      )}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {costTiles(summary.data, money, words).map((tile) => (
          <Kpi key={tile.label} {...tile} />
        ))}
        <Kpi {...savingsTile(savings.data, money)} />
        <Kpi {...rateTile(currency, rate)} />
      </div>
      {tenantId !== null && (summary.data?.status === 'ready' || summary.data?.status === 'provisional') && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <SpendTrend tenantId={tenantId} />
          <TopSpend tenantId={tenantId} words={words} />
        </div>
      )}
      {tenantId !== null && summary.data?.status === 'ready' && <WhatChanged tenantId={tenantId} />}
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

function TopSpend({ tenantId, words }: { tenantId: string; words: CloudWords }) {
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
            ? `Available once the first ${words.firstBill} is loaded.`
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
  const realised = (summary.realisedSavingsUsdMonth ?? 0) + (summary.estimatedRealisedUsdMonth ?? 0)
  const saved = realised > 0 ? ` · ${money(realised)}/mo realised` : ''
  if (summary.openCount === 0) return { label, value: money(0), note: `No open recommendations${saved}` }
  const n = summary.openCount
  return {
    label,
    value: `${money(summary.savingsUsdMonth)}/mo`,
    note: `${n} open recommendation${n === 1 ? '' : 's'}${saved}`,
  }
}

function reconciliationMessage(month: string, differencePct: number | null, words: CloudWords): string {
  const name = new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', {
    month: 'long',
    timeZone: 'UTC',
  })
  const by = differencePct === null ? '' : ` by ${Math.abs(differencePct).toFixed(1)}%`
  return `${name} totals differ from ${words.total}${by}. We're reconciling; figures may change.`
}

/** The exchange-rate tile: today's rate for the workspace currency, and where it came from. */
export function rateTile(currency: string, current: CurrentRate | undefined): { label: string; value: string; note: string } {
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
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  const isAdmin = role === 'admin'
  // The connection's label is kept with the result: its card may be gone once the data is.
  const [deleted, setDeleted] = useState<{ deletion: DataDeletion; label: string } | null>(null)
  const providers = workspaceProviders(connections.data ?? [])
  const multiProvider = providers.length > 1
  return (
    <SettingsLayout current="connections" role={role}>
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Connections</h1>
          <p className="text-sm text-secondary">Cloud accounts Cloudoptics can read. Read-only, always.</p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {tenantId !== null && <RefreshConnections tenantId={tenantId} updatedAt={connections.dataUpdatedAt} />}
          <Link
          to="/onboarding"
          search={{ step: 1 }}
          className="inline-flex h-9 items-center gap-2 rounded-sm bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover"
        >
            <Plus size={16} /> Connect an account
          </Link>
        </div>
      </div>
      {deleted && (
        <Alert tone="info">
          Deleted the data for {deleted.label}: {verificationSummary(deleted.deletion)} The record is in Settings,
          Workspace.
        </Alert>
      )}
      {connections.isLoading && <p className="text-sm text-secondary">Loading…</p>}
      {connections.data?.length === 0 && <Alert tone="info">No accounts connected yet.</Alert>}
      {connections.data?.map((c) => (
        <Card key={c.id}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="font-medium">
                {connectionLabel(c)}
                {multiProvider && ' '}
                <ProviderBadge provider={c.provider} show={multiProvider} />
              </p>
              <p className="text-xs text-muted">
                {connectionKindNote(c)}
                {c.lastValidatedAt && ` · checked ${new Date(c.lastValidatedAt).toLocaleString()}`}
              </p>
            </div>
            <StatusPill status={c.status} />
          </div>
          {isAdmin && tenantId !== null && (
            <ConnectionActions
              connection={c}
              tenantId={tenantId}
              onDeleted={(deletion) => setDeleted({ deletion, label: connectionLabel(c) })}
            />
          )}
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
          {isAdmin &&
            tenantId !== null &&
            providerOf(c.provider) === 'aws' &&
            c.status !== 'disconnected' &&
            c.stackName !== ACCESS_STACK_NAME && (
            <MoveToSingleStack connection={c} />
          )}
          {isAdmin && tenantId !== null && c.stackName === ACCESS_STACK_NAME && c.status !== 'disconnected' && (
            <StackUpdate connection={c} tenantId={tenantId} />
          )}
          {isAdmin &&
            tenantId !== null &&
            providerOf(c.provider) === 'aws' &&
            c.stackName === ACCESS_STACK_NAME &&
            (c.status === 'healthy' || c.status === 'degraded') && <ResourceAccess connection={c} tenantId={tenantId} />}
          {isAdmin &&
            tenantId !== null &&
            providerOf(c.provider) !== 'aws' &&
            supports(c.provider, 'scan') &&
            (c.status === 'healthy' || c.status === 'degraded') && <ProviderScan connection={c} tenantId={tenantId} />}
          {isAdmin &&
            tenantId !== null &&
            supports(c.provider, 'reload') &&
            // AWS needs its CUR bucket; other clouds keep their export settings server-side.
            (c.curBucket || providerOf(c.provider) !== 'aws') &&
            (c.status === 'healthy' || c.status === 'degraded') && (
            <ReloadBills connection={c} tenantId={tenantId} />
          )}
        </Card>
      ))}
      {tenantId !== null && connections.data && connections.data.length > 0 && (
        <Accounts tenantId={tenantId} showProvider={multiProvider} providers={providers} />
      )}
    </div>
    </SettingsLayout>
  )
}

/** Everything on the Connections page, fetched again now (the page also follows changes itself). */
function RefreshConnections({ tenantId, updatedAt }: { tenantId: string; updatedAt: number }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const refresh = async () => {
    setBusy(true)
    try {
      await Promise.all(
        ['connections', 'setup', 'accounts', 'resource-summary'].map((key) =>
          qc.refetchQueries({ queryKey: [key, tenantId] }),
        ),
      )
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      {updatedAt > 0 && (
        <span className="text-xs text-muted">Updated {new Date(updatedAt).toLocaleTimeString()}</span>
      )}
      <Button variant="secondary" disabled={busy} onClick={() => void refresh()}>
        <RefreshCw size={16} className={cx(busy && 'animate-spin motion-reduce:animate-none')} />
        {busy ? 'Refreshing…' : 'Refresh'}
      </Button>
    </>
  )
}

/** Check again, set the stack up again, disconnect, reconnect or delete its data (admins). */
function ConnectionActions({
  connection: c,
  tenantId,
  onDeleted,
}: {
  connection: Connection
  tenantId: string
  onDeleted: (deletion: DataDeletion) => void
}) {
  const api = useApi()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [confirming, setConfirming] = useState(false)
  const confirmButton = useRef<HTMLButtonElement>(null)
  const confirmPanel = useRef<HTMLDivElement>(null)
  useEscapeToClose(confirming, () => setConfirming(false), confirmButton, confirmPanel)
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
  const provider = providerOf(c.provider)
  const gcp = provider === 'gcp'
  const azure = provider === 'azure'
  // Azure's steps open on the card ("Set up again"); after Reconnect they show the new code.
  const [azureSteps, setAzureSteps] = useState<'closed' | 'open' | 'reconnected'>('closed')
  const stepsId = useId()
  const reconnect = useMutation({
    mutationFn: async () => {
      if (azure) {
        const setup = await api.reconnectAzureConnection(tenantId, c.id)
        qc.setQueryData(['azure-setup', tenantId, c.id], setup)
        return null
      }
      return gcp
        ? api.createGcpConnection(tenantId, (await api.getGcpSetup(tenantId, c.id)).settings)
        : api.createConnection(tenantId, {
            awsAccountId: c.awsAccountId,
            accountType: c.accountType,
            createCurExport: c.createCurExport,
          })
    },
    onSuccess: (setup) => {
      refresh()
      if (setup === null) setAzureSteps('reconnected')
      else void navigate({ to: '/onboarding', search: { connection: setup.connection.id } })
    },
  })
  const error = check.error ?? disconnect.error ?? reconnect.error
  const busy = check.isPending || disconnect.isPending || reconnect.isPending

  if (providerOf(c.provider) === 'digitalocean') {
    if (c.status === 'disconnected') {
      return (
        <div className="mt-4 flex flex-col gap-2">
          <p className="text-sm text-secondary">
            {c.credentialType === 'oauth'
              ? 'Disconnected, access revoked in DigitalOcean. Past costs stay until you delete them. To connect again, choose Connect an account.'
              : 'Disconnected and its token deleted from Cloudoptics. Past costs stay until you delete them. To connect again, choose Connect an account and paste a new token.'}
          </p>
          <div className="flex flex-wrap gap-2">
            <DeleteConnectionData connection={c} tenantId={tenantId} onDeleted={onDeleted} />
          </div>
        </div>
      )
    }
    return (
      <div className="mt-4 flex flex-col gap-3">
        {error && <Alert tone="error">{error.message}</Alert>}
        {confirming ? (
          <div ref={confirmPanel} className="flex flex-col gap-2 rounded-sm bg-surface-2 p-3 text-sm">
            <p>
              Disconnect {accountLabel(c.provider, c.awsAccountId)}?{' '}
              {c.credentialType === 'oauth'
                ? 'Cloudoptics revokes its access in DigitalOcean and stops reading the team. Past costs stay.'
                : 'Cloudoptics deletes its copy of the token and stops reading the team. Past costs stay. Delete the token in DigitalOcean too.'}
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
          <div className="flex flex-wrap items-start gap-2">
            <Button variant="secondary" disabled={busy || c.status === 'validating'} onClick={() => check.mutate()}>
              {check.isPending || c.status === 'validating' ? 'Checking…' : 'Check again'}
            </Button>
            {c.credentialType === 'oauth' ? (
              <ReconnectDigitalOcean tenantId={tenantId} />
            ) : (
              <ReplaceDigitalOceanToken tenantId={tenantId} connectionId={c.id} />
            )}
            <Button ref={confirmButton} variant="ghost" disabled={busy} onClick={() => setConfirming(true)}>
              Disconnect…
            </Button>
          </div>
        )}
      </div>
    )
  }

  if (c.status === 'disconnected') {
    return (
      <div className="mt-4 flex flex-col gap-2">
        <p className="text-sm text-secondary">
          Disconnected. Cloudoptics no longer reads this {connectionKind(c)}; past costs stay until you delete them.
          {azure && ' Reconnect gives it a new ownership code, so the steps in Azure are done again.'}
        </p>
        {error && <Alert tone="error">{error.message}</Alert>}
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" disabled={busy} onClick={() => reconnect.mutate()}>
            Reconnect
          </Button>
          <DeleteConnectionData connection={c} tenantId={tenantId} onDeleted={onDeleted} />
        </div>
      </div>
    )
  }
  return (
    <div className="mt-4 flex flex-col gap-3">
      {error && <Alert tone="error">{error.message}</Alert>}
      {confirming ? (
        <div ref={confirmPanel} className="flex flex-col gap-2 rounded-sm bg-surface-2 p-3 text-sm">
          {gcp ? (
            <p>
              Disconnect {connectionLabel(c)}? Cloudoptics stops reading it, deletes its Google Cloud account for
              this connection (so the access you gave it stops working), and its open recommendations close. Past
              costs stay.
            </p>
          ) : azure ? (
            <p>
              Disconnect {connectionLabel(c)}? Cloudoptics stops reading it, deletes its ownership code, and its open
              recommendations close. Past costs stay. To remove our access completely, also remove the Cloudoptics
              role assignments in Azure.
            </p>
          ) : (
          <p>
            Disconnect {accountLabel(c.provider, c.awsAccountId)}? Cloudoptics stops reading it and its open recommendations close.
            Past costs stay. To remove our access completely, also delete the{' '}
            <code className="font-mono text-xs">{c.stackName}</code> stack in{' '}
            <a className="text-accent underline" href={STACKS_URL} target="_blank" rel="noreferrer">
              CloudFormation
            </a>
            . AWS keeps the S3 bucket with your bills, and Cloudoptics reuses it if you connect again.
          </p>
          )}
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
          {azure ? (
            <Button
              variant="secondary"
              disabled={busy}
              aria-expanded={azureSteps !== 'closed'}
              aria-controls={stepsId}
              onClick={() => setAzureSteps((s) => (s === 'closed' ? 'open' : 'closed'))}
            >
              {azureSteps === 'closed' ? 'Set up again' : 'Hide the steps'}
            </Button>
          ) : (
            // AWS opens its stack, Google Cloud its commands and dataset label (Onboarding).
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => void navigate({ to: '/onboarding', search: { connection: c.id } })}
            >
              Set up again
            </Button>
          )}
          <Button ref={confirmButton} variant="ghost" disabled={busy} onClick={() => setConfirming(true)}>
            Disconnect…
          </Button>
        </div>
      )}
      {azure && azureSteps !== 'closed' && (
        <Suspense fallback={<p className="text-sm text-secondary">Loading…</p>}>
          <AzureSetupAgain
            tenantId={tenantId}
            connection={c}
            newCode={azureSteps === 'reconnected'}
            id={stepsId}
          />
        </Suspense>
      )}
    </div>
  )
}

/** The line under a connection's name: what it reads, in the provider's words. */
function connectionKindNote(c: Connection): string {
  switch (providerOf(c.provider)) {
    case 'digitalocean':
      return c.credentialType === 'oauth' ? 'DigitalOcean team, read-only sign-in' : 'DigitalOcean team, read-only token'
    case 'gcp':
      return 'Billing account, with every project billed to it'
    case 'azure':
      return c.accountType === 'management'
        ? 'Billing account, with every subscription billed to it'
        : 'Subscription, with its FOCUS cost export'
    default:
      return c.accountType === 'management' ? 'Management (payer) account' : 'Standalone account'
  }
}

/** Plural of what a provider's scan covers: accounts, projects, subscriptions, teams. */
function scopeNouns(provider: Provider | null | undefined): string {
  return `${accountNoun(provider)}s`
}

/**
 * Scan now for a Google Cloud, Azure or DigitalOcean connection (admins). The shared scan route
 * finds the connection's plug-in by its cloud; there is no AWS role to look for first.
 */
function ProviderScan({ connection: c, tenantId }: { connection: Connection; tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const summary = useQuery({
    queryKey: ['resource-summary', tenantId],
    queryFn: () => api.resourceSummary(tenantId),
    refetchInterval: (q) =>
      q.state.data?.scans.some((s) => s.connectionId === c.id && s.status === 'running') ? 2_000 : false,
  })
  const scan = useMutation({
    mutationFn: () => api.scanResources(tenantId, c.id),
    onSuccess: () => {
      for (const key of ['accounts', 'recommendations', 'resource-summary']) {
        void qc.invalidateQueries({ queryKey: [key, tenantId] })
      }
    },
  })
  const last = summary.data?.scans.find((s) => s.connectionId === c.id)
  const running = last?.status === 'running'
  const wasRunning = useRef(running)
  useEffect(() => {
    if (wasRunning.current && !running) {
      void qc.invalidateQueries({ queryKey: ['accounts', tenantId] })
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
    }
    wasRunning.current = running
  }, [running, qc, tenantId])
  return (
    <div className="mt-4 flex flex-col gap-3 rounded-sm bg-surface-2 p-3 text-sm">
      <div>
        <p className="font-medium">Resources</p>
        <p className="text-secondary">
          Cloudoptics scans the resources in this {connectionKind(c)} every 12 hours for recommendations. Scan now to
          look straight away.
        </p>
      </div>
      {running ? <ScanRunning scan={last} /> : last && <LastScanResult scan={last} provider={c.provider} />}
      {scan.error && <Alert tone="error">{scan.error.message}</Alert>}
      {scan.data?.status === 'skipped' && (
        <Alert tone="warning">
          {PROVIDER_LABEL[providerOf(c.provider)]} returned nothing to scan yet. Check the connection, then scan again.
        </Alert>
      )}
      <div>
        <Button variant="secondary" disabled={scan.isPending || running} onClick={() => scan.mutate()}>
          {scan.isPending ? 'Starting…' : running ? 'Scanning…' : 'Scan now'}
        </Button>
      </div>
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
  const qc = useQueryClient()
  // Set once the admin has opened AWS: the update takes a few minutes, so look more often.
  const [opened, setOpened] = useState(false)
  const setup = useQuery({
    queryKey: ['setup', tenantId, c.id],
    queryFn: () => api.getSetup(tenantId, c.id),
    // While an update is offered, notice by itself when the stack has been updated in AWS.
    refetchInterval: (q) => (q.state.data?.updateAvailable ? (opened ? 5_000 : 30_000) : false),
  })
  const s = setup.data
  // When the update lands, the connection (its version and checks) has changed too.
  const updateAvailable = s?.updateAvailable
  const offered = useRef(updateAvailable)
  useEffect(() => {
    if (offered.current && updateAvailable === false) {
      void qc.invalidateQueries({ queryKey: ['connections', tenantId] })
      void qc.invalidateQueries({ queryKey: ['accounts', tenantId] })
    }
    offered.current = updateAvailable
  }, [updateAvailable, qc, tenantId])
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
          onClick={() => setOpened(true)}
          className="inline-flex h-9 items-center justify-center gap-2 rounded-sm bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover"
        >
          Update Cloudoptics access <ExternalLink size={16} />
        </a>
      </div>
      <p className="text-secondary">
        AWS opens with the new version already chosen, signed in to account {c.awsAccountId}. Choose Next until the last
        page, tick the box that starts “I acknowledge” and choose Submit. Cloudoptics notices when it is done.
      </p>
      {opened && (
        <p aria-live="polite" className="font-medium">
          Waiting for the update in AWS. This page changes by itself when it finishes, usually within a few minutes.
        </p>
      )}
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

/** Resource access on the single stack, the last resource scan and Scan now (admins). */
function ResourceAccess({ connection: c, tenantId }: { connection: Connection; tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: ['accounts', tenantId],
    queryFn: () => api.listAccounts(tenantId),
  })
  const summary = useQuery({
    queryKey: ['resource-summary', tenantId],
    queryFn: () => api.resourceSummary(tenantId),
    // The server says whether a scan is running, so leaving the page or reloading it does not
    // lose track of one: follow its progress until it finishes.
    refetchInterval: (q) =>
      q.state.data?.scans.some((s) => s.connectionId === c.id && s.status === 'running') ? 2_000 : false,
  })
  const accounts = (list.data?.accounts ?? []).filter((a) => a.connectionId === c.id && a.orgStatus !== 'NOT_LISTED')
  const missing = accounts.filter((a) => !a.resourceAccess)
  const scan = useMutation({
    mutationFn: () => api.scanResources(tenantId, c.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['accounts', tenantId] })
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
      void qc.invalidateQueries({ queryKey: ['resource-summary', tenantId] })
    },
  })
  const last = summary.data?.scans.find((s) => s.connectionId === c.id)
  const running = last?.status === 'running'
  // When a running scan finishes, show what it changed.
  const wasRunning = useRef(running)
  useEffect(() => {
    if (wasRunning.current && !running) {
      void qc.invalidateQueries({ queryKey: ['accounts', tenantId] })
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
    }
    wasRunning.current = running
  }, [running, qc, tenantId])
  if (accounts.length === 0) return null
  const refused = scan.data?.accounts.filter((a) => !a.access) ?? []
  return (
    <div className="mt-4 flex flex-col gap-3 rounded-sm bg-surface-2 p-3 text-sm">
      <div>
        <p className="font-medium">
          {missing.length === 0
            ? `Resource access in ${accounts.length === 1 ? 'this account' : `all ${accounts.length} accounts`}`
            : `Resource access in ${accounts.length - missing.length} of ${accounts.length} accounts`}
        </p>
        <p className="text-secondary">
          {missing.length === 0
            ? 'Cloudoptics scans resources every 12 hours for recommendations. Scan now to look straight away.'
            : c.accountType === 'management'
              ? 'Member accounts get access on their own as AWS reaches them, usually within 10 minutes, and new accounts get it when they join. Choose Check now to see where each one is.'
              : 'Access is added by the CloudOptics stack. Check now to look again.'}
        </p>
      </div>
      {running ? <ScanRunning scan={last} /> : last && <LastScanResult scan={last} />}
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
        <Button variant="secondary" disabled={scan.isPending || running} onClick={() => scan.mutate()}>
          {scan.isPending ? 'Checking…' : running ? 'Scanning…' : missing.length === 0 ? 'Scan now' : 'Check now'}
        </Button>
      </div>
    </div>
  )
}

/** A scan in progress: regions scanned so far, from the server, so it survives leaving the page. */
function ScanRunning({ scan }: { scan: LastScan }) {
  const p = scan.progress
  const percent = p && p.regionsTotal > 0 ? Math.min(100, Math.round((100 * p.regionsDone) / p.regionsTotal)) : null
  return (
    <div className="flex flex-col gap-2">
      <p aria-live="polite">
        {p ? `Scanning resources: ${p.regionsDone} of ${p.regionsTotal} regions` : 'Starting the scan…'}
      </p>
      <div
        role="progressbar"
        aria-label="Resource scan"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent ?? undefined}
        className="h-2 w-full overflow-hidden rounded-full bg-border"
      >
        <div
          className={cx(
            'h-full rounded-full bg-accent transition-[width] duration-700 ease-out motion-reduce:transition-none',
            percent === null && 'w-1/4 animate-pulse',
          )}
          style={percent === null ? undefined : { width: `${Math.max(percent, 2)}%` }}
        />
      </div>
      <p className="text-secondary">
        Started {new Date(scan.startedAt).toLocaleTimeString()}. You can leave this page: the scan carries on, and
        recommendations follow a few minutes after it finishes.
      </p>
    </div>
  )
}

/** What the latest scan found, and what it could not read, in plain words. */
function LastScanResult({ scan, provider }: { scan: LastScan; provider?: Provider | null }) {
  const when = new Date(scan.finishedAt ?? scan.startedAt).toLocaleString()
  if (scan.status === 'failed') {
    return (
      <Alert tone="error">
        The last scan ({when}) did not finish{scan.errors.length ? `: ${scan.errors.join(', ')}` : ''}. Scan now to try
        again.
      </Alert>
    )
  }
  const resources = `${scan.resources} ${scan.resources === 1 ? 'resource' : 'resources'} in ${scan.regions} regions`
  const found =
    providerOf(provider) !== 'aws' && scan.accounts <= 1
      ? resources
      : `${resources} across ${scan.accountsWithAccess} of ${scan.accounts} ${scopeNouns(provider)}`
  return (
    <div className="flex flex-col gap-2">
      <p>
        Last scan {when}: {found}.
      </p>
      {scan.errors.length > 0 && (
        <Alert tone="warning">
          <p>Some parts could not be read, so they were left as they were:</p>
          <ul className="mt-1 flex flex-col gap-1 font-mono text-xs">
            {scan.errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </Alert>
      )}
    </div>
  )
}

function orgStatusLabel(status: string | null): string | null {
  if (!status || status === 'ACTIVE') return null
  if (status === 'NOT_LISTED') return 'No longer in the organization'
  return status.charAt(0) + status.slice(1).toLowerCase().replace(/_/g, ' ')
}

/** The accounts, projects, subscriptions and teams from the connections and the bills, with
 * coverage (PRD US-1.3); AWS's come from its Organization. */
function Accounts({
  tenantId,
  showProvider,
  providers,
}: {
  tenantId: string
  showProvider: boolean
  providers: Provider[]
}) {
  const api = useApi()
  const list = useQuery({
    queryKey: ['accounts', tenantId],
    queryFn: () => api.listAccounts(tenantId),
  })
  if (!list.data || list.data.accounts.length === 0) return null
  const { coverage, accounts } = list.data
  return (
    <Card>
      <h2 className="text-base font-semibold">{accountsHeading(providers)}</h2>
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
                  {showProvider && ' '}
                  <ProviderBadge provider={a.provider} show={showProvider} />
                </p>
                <p className="text-xs text-muted">
                  {accountTypeLabel(a.provider, a.accountType)}
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

/** "AWS accounts", "Google Cloud projects", or "Accounts" for several providers. */
function accountsHeading(providers: Provider[]): string {
  const p = providers.length === 1 ? providers[0] : undefined
  if (!p) return 'Accounts'
  return `${PROVIDER_LABEL[p]} ${scopeNouns(p)}`
}

/** An account's role in its connection, in its provider's words. */
function accountTypeLabel(provider: Provider | null | undefined, type: 'management' | 'member' | 'standalone'): string {
  const p = providerOf(provider)
  if (p === 'aws') return type === 'management' ? 'Management (payer)' : type === 'member' ? 'Member' : 'Standalone'
  if (type === 'management') return 'Billing account'
  const noun = accountNoun(p)
  return noun.charAt(0).toUpperCase() + noun.slice(1)
}
