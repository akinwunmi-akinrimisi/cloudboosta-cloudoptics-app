import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useRef, useState, type FormEvent } from 'react'

import { useApi } from '../app/context'
import type { CostDimension, CostMonth, CreditsRunway } from '../api/types'
import { DailySpendChart } from '../components/charts'
import { ExportButton } from '../components/exports'
import { useEscapeToClose } from '../components/overlay'
import { Alert, Button, Card, Field, cx } from '../components/ui'
import { ForecastPanel, WhatChanged } from './Forecast'
import { DIMENSIONS, useMoney, useWorkspace } from './Workspace'

const RANGES = [30, 90] as const

function Toggle<T extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: { id: T; label: string }[]
  value: T
  onChange: (value: T) => void
}) {
  return (
    <div role="group" aria-label={label} className="flex rounded-sm border border-border text-xs">
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          aria-pressed={value === o.id}
          onClick={() => onChange(o.id)}
          className={cx(
            'px-2 py-1',
            value === o.id ? 'bg-accent-soft font-medium text-primary' : 'text-secondary hover:bg-surface-2',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** The last day of a month ("2026-02" -> "2026-02-28"). */
export function monthEnd(month: string): string {
  return new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).toISOString().slice(0, 10)
}

/** The day `n` days before today (UTC), as YYYY-MM-DD. */
function daysAgo(n: number): string {
  return new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 10)
}

/** "2026-10" as "October 2026". */
export function monthLabel(month: string): string {
  return new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' })
}

/** Cost Explorer (PRD US-2.x): daily trend and a month's spend by service, account or region; this month by default. */
export function Costs() {
  const api = useApi()
  const { tenantId } = useWorkspace()
  const { money } = useMoney()
  const [days, setDays] = useState<(typeof RANGES)[number]>(30)
  const [dimension, setDimension] = useState<CostDimension>('service')
  // null: the current month.
  const [chosen, setChosen] = useState<string | null>(null)
  const months = useQuery({
    queryKey: ['cost-months', tenantId],
    queryFn: () => api.costMonths(tenantId as string),
    enabled: tenantId !== null,
  })
  const ended = (months.data?.months ?? []).filter((m) => m.state !== 'current')
  const past = ended.find((m) => m.month === chosen) ?? null
  const month = past?.month ?? null
  const daily = useQuery({
    queryKey: ['cost-daily', tenantId, month ?? days],
    queryFn: () => api.costDaily(tenantId as string, days, month),
    enabled: tenantId !== null,
  })
  const breakdown = useQuery({
    queryKey: ['cost-breakdown', tenantId, dimension, 25, month],
    queryFn: () => api.costBreakdown(tenantId as string, dimension, 25, month),
    enabled: tenantId !== null,
  })
  const points = daily.data?.points ?? []
  const items = breakdown.data?.items ?? []
  const total = points.reduce((sum, p) => sum + p.amount, 0)
  const provisional = daily.data?.source === 'cost_explorer'
  const breakdownMonth = breakdown.data ? monthLabel(breakdown.data.month) : ''

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Cost Explorer</h1>
          <p className="text-sm text-secondary">Amortised cost after credits, from your AWS Cost and Usage Report.</p>
        </div>
        {ended.length > 0 && (
          <label className="flex items-center gap-2 text-sm">
            <span className="text-secondary">Month</span>
            <select
              className="h-8 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm"
              value={month ?? ''}
              onChange={(e) => setChosen(e.target.value || null)}
            >
              <option value="">This month</option>
              {ended.map((m) => (
                <option key={m.month} value={m.month}>
                  {monthLabel(m.month)}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {past && <PastMonth month={past} money={money} />}
      {daily.data && daily.data.source === null && (
        <Alert tone="info">Costs appear here once the first report from AWS is loaded.</Alert>
      )}
      {provisional && (
        <Alert tone="info">
          Provisional figures from AWS Cost Explorer until the first Cost and Usage Report is loaded.
        </Alert>
      )}
      <Card className="p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">Daily spend{past && ` · ${monthLabel(past.month)}`}</h2>
            <p className="text-xs text-muted">
              {points.length > 0 ? `${money(total)} over ${points.length} days` : 'No daily data yet'}
            </p>
          </div>
          <div className="flex flex-wrap items-start gap-2">
            {!provisional && !past && (
              <Toggle
                label="Range"
                options={RANGES.map((r) => ({ id: r, label: `${r} days` }))}
                value={days}
                onChange={setDays}
              />
            )}
            {tenantId !== null && points.length > 0 && !provisional && (
              <ExportButton
                tenantId={tenantId}
                label="Export daily CSV"
                request={{
                  kind: 'costs',
                  start: past ? `${past.month}-01` : daysAgo(days - 1),
                  end: past ? monthEnd(past.month) : daysAgo(0),
                  granularity: 'day',
                  groupBy: ['service'],
                }}
              />
            )}
          </div>
        </div>
        {daily.isLoading && <p className="text-sm text-secondary">Loading…</p>}
        {points.length > 0 && <DailySpendChart points={points} currency="USD" />}
      </Card>
      <Card className="p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">
              Spend by {dimension} · {breakdownMonth}
            </h2>
            <p className="text-xs text-muted">
              {breakdown.data && breakdown.data.total > 0 ? `${money(breakdown.data.total)} excluding tax` : ''}
            </p>
          </div>
          <div className="flex flex-wrap items-start gap-2">
            <Toggle label="Group by" options={DIMENSIONS} value={dimension} onChange={setDimension} />
            {tenantId !== null && breakdown.data?.source === 'cur' && items.length > 0 && (
              <ExportButton
                tenantId={tenantId}
                request={{
                  kind: 'costs',
                  start: `${breakdown.data.month}-01`,
                  end: monthEnd(breakdown.data.month),
                  granularity: 'month',
                  groupBy: [dimension],
                }}
              />
            )}
          </div>
        </div>
        {breakdown.isLoading && <p className="text-sm text-secondary">Loading…</p>}
        {breakdown.data && items.length === 0 && (
          <p className="text-sm text-secondary">
            {breakdown.data.source === 'cost_explorer'
              ? 'Available once the first Cost and Usage Report is loaded.'
              : past
                ? 'No spend in this month.'
                : 'No spend this month yet.'}
          </p>
        )}
        {items.length > 0 && (
          <div className="overflow-x-auto" role="region" aria-label="Spend breakdown" tabIndex={0}>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted">
                <th className="py-2 font-medium">{DIMENSIONS.find((d) => d.id === dimension)?.label}</th>
                <th className="py-2 text-right font-medium">Spend</th>
                <th className="hidden w-1/3 py-2 font-medium sm:table-cell">
                  <span className="sr-only">Share</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.key} className="border-b border-border last:border-0">
                  <td className="max-w-0 truncate py-2 pr-3" title={item.label}>
                    {item.label}
                    {item.label !== item.key && item.key !== 'other' && (
                      <span className="ml-2 text-xs text-muted">{item.key}</span>
                    )}
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    {money(item.amount)} <span className="text-xs text-muted">{Math.round(item.share * 100)}%</span>
                  </td>
                  <td className="hidden py-2 pl-4 sm:table-cell">
                    <div className="h-1.5 rounded-full bg-surface-2">
                      <div
                        className="h-1.5 rounded-full bg-accent"
                        style={{ width: `${Math.max(item.share * 100, 1)}%` }}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </Card>
      {/* The forecast and the credits runway are about the current month only. */}
      {tenantId !== null && !past && <ForecastPanel tenantId={tenantId} />}
      {tenantId !== null && <WhatChanged tenantId={tenantId} month={month} />}
      {tenantId !== null && !past && <CreditsCard tenantId={tenantId} />}
    </div>
  )
}

/** Which ended month is shown, whether its figures are final, and its monthly report. */
function PastMonth({ month: m, money }: { month: CostMonth; money: (usd: number) => string }) {
  const name = monthLabel(m.month)
  return (
    <Alert tone={m.state === 'final' ? 'info' : 'warning'}>
      <p>
        <strong>{name}</strong>: {money(m.total)} after credits,{' '}
        {m.source === 'cur' ? 'from the AWS bills' : 'provisional figures from AWS Cost Explorer'}.{' '}
        {m.state === 'final'
          ? 'The bill for this month is final.'
          : 'Still changing: AWS may restate this month until its bill is final, usually by the 6th of the next month.'}{' '}
        The forecast and AWS credits cover the current month only.
      </p>
      {m.report && (
        <p className="mt-1">
          <Link to="/reports" search={{ month: m.month }} className="font-medium underline">
            Open the {name} report
          </Link>
          {m.report === 'draft' && ' (a draft from the latest data)'}
        </p>
      )}
    </Alert>
  )
}

function dateLabel(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })
}

/** AWS credits runway (PRD US-2.4): burn from the bills, countdown from the balance an admin enters. */
export function CreditsCard({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const { me } = useWorkspace()
  const { money } = useMoney()
  const isAdmin = me?.memberships.find((m) => m.tenantId === tenantId)?.role === 'admin'
  const [editing, setEditing] = useState(false)
  const editButton = useRef<HTMLButtonElement>(null)
  const editPanel = useRef<HTMLDivElement>(null)
  useEscapeToClose(editing, () => setEditing(false), editButton, editPanel)
  const credits = useQuery({ queryKey: ['credits', tenantId], queryFn: () => api.getCredits(tenantId) })
  const c = credits.data
  if (!c || (c.status === 'no_credits' && !isAdmin)) return null
  return (
    <Card className="p-5">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold">AWS credits</h2>
          <p className="text-xs text-muted">
            {c.burnDays ? `Average over the last ${c.burnDays} days of bills` : 'No bills yet'}
            {c.dataThrough && `, to ${dateLabel(c.dataThrough)}`}
          </p>
        </div>
        {isAdmin && !editing && (
          <Button ref={editButton} variant="secondary" onClick={() => setEditing(true)}>
            {c.balance ? 'Update the balance' : 'Enter the balance'}
          </Button>
        )}
      </div>
      {c.status === 'no_credits' && !editing && (
        <p className="text-sm text-secondary">No credits applied to your bills recently. If you have credits, enter the balance.</p>
      )}
      {c.status === 'no_balance' && (
        <Alert tone="info">
          Credits are being applied, but AWS does not report the balance left. {isAdmin ? 'Enter it' : 'An admin can enter it'} from
          the AWS Billing console (Credits page) to see when they run out.
        </Alert>
      )}
      {c.status === 'ending_soon' && c.depletionDate && (
        <Alert tone="warning">
          Credits {c.expiresFirst ? 'expire' : 'run out'} around {dateLabel(c.depletionDate)}, in about {c.daysLeft} days. After that
          the bill is about {money(c.spendBeforeCreditsUsdMonth)} a month.
        </Alert>
      )}
      {c.status === 'used_up' && (
        <Alert tone="warning">
          The entered balance is used up: the bill is now about {money(c.spendBeforeCreditsUsdMonth)} a month. Update the balance
          if you have more credits.
        </Alert>
      )}
      {c.status !== 'no_credits' && <RunwayFigures c={c} money={money} />}
      {c.daily.some((d) => d.amount > 0) && (
        <div className="mt-4">
          <p className="mb-1 text-xs text-muted">Credits applied per day</p>
          <DailySpendChart points={c.daily} currency="USD" label="Credits applied per day" />
        </div>
      )}
      {c.balance && (
        <p className="mt-3 text-xs text-muted">
          Balance {money(c.balance.amountUsd)} on {dateLabel(c.balance.asOf)}
          {c.balance.expiresOn && `, expiring ${dateLabel(c.balance.expiresOn)}`}
          {c.balance.enteredBy && `, entered by ${c.balance.enteredBy}`}
          {c.balance.note && ` (${c.balance.note})`}. Credits applied since: {money(c.usedSinceBalanceUsd ?? 0)}.
        </p>
      )}
      {editing && (
        <div ref={editPanel}>
          <BalanceForm tenantId={tenantId} current={c} onDone={() => setEditing(false)} />
        </div>
      )}
    </Card>
  )
}

function RunwayFigures({ c, money }: { c: CreditsRunway; money: (usd: number) => string }) {
  const figures: [string, string][] = [
    ['Applied this month', money(c.appliedMonthUsd)],
    ['Average a day', money(c.burnUsdDay)],
    ['Left', c.remainingUsd === null ? '—' : money(c.remainingUsd)],
    [
      c.expiresFirst ? 'Expire on' : 'Run out around',
      c.depletionDate ? dateLabel(c.depletionDate) : c.remainingUsd ? 'Not at this rate' : '—',
    ],
    ['Bill without credits', `${money(c.spendBeforeCreditsUsdMonth)} a month`],
  ]
  return (
    <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {figures.map(([label, value]) => (
        <div key={label} className="rounded-sm border border-border p-3">
          <dt className="text-xs text-muted">{label}</dt>
          <dd className="text-sm font-semibold">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function BalanceForm({ tenantId, current, onDone }: { tenantId: string; current: CreditsRunway; onDone: () => void }) {
  const api = useApi()
  const qc = useQueryClient()
  const [today] = useState(() => new Date().toISOString().slice(0, 10))
  const [amount, setAmount] = useState(current.balance ? String(current.remainingUsd ?? '') : '')
  const [asOf, setAsOf] = useState(today)
  const [expiresOn, setExpiresOn] = useState(current.balance?.expiresOn ?? '')
  const [note, setNote] = useState('')
  const save = useMutation({
    mutationFn: () =>
      api.setCreditBalance(tenantId, { amountUsd: Number(amount), asOf, expiresOn: expiresOn || null, note: note || null }),
    onSuccess: (updated) => {
      qc.setQueryData(['credits', tenantId], updated)
      onDone()
    },
  })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate()
  }
  return (
    <form onSubmit={submit} className="mt-4 flex flex-col gap-3 border-t border-border pt-4">
      <p className="text-sm text-secondary">
        In the AWS Billing console, open Credits and copy the remaining amount (all credits together, in USD).
      </p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
        <Field label="Remaining balance (USD)" type="number" min={0} step="0.01" required value={amount} onChange={(e) => setAmount(e.target.value)} />
        <Field label="As shown on" type="date" max={today} required value={asOf} onChange={(e) => setAsOf(e.target.value)} />
        <Field label="Expires on (optional)" type="date" value={expiresOn} onChange={(e) => setExpiresOn(e.target.value)} />
        <Field label="Note (optional)" maxLength={200} value={note} placeholder="AWS Activate" onChange={(e) => setNote(e.target.value)} />
      </div>
      {save.error && <Alert tone="error">{save.error.message}</Alert>}
      <div className="flex gap-2">
        <Button type="submit" disabled={save.isPending || amount === ''}>
          {save.isPending ? 'Saving…' : 'Save balance'}
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
