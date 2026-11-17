import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'

import { useApi } from '../app/context'
import type { CostDimension, CreditsRunway } from '../api/types'
import { DailySpendChart } from '../components/charts'
import { Alert, Button, Card, Field, cx } from '../components/ui'
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

/** Cost Explorer (PRD US-2.x): daily trend and this month's spend by service, account or region. */
export function Costs() {
  const api = useApi()
  const { tenantId } = useWorkspace()
  const { money } = useMoney()
  const [days, setDays] = useState<(typeof RANGES)[number]>(30)
  const [dimension, setDimension] = useState<CostDimension>('service')
  const daily = useQuery({
    queryKey: ['cost-daily', tenantId, days],
    queryFn: () => api.costDaily(tenantId as string, days),
    enabled: tenantId !== null,
  })
  const breakdown = useQuery({
    queryKey: ['cost-breakdown', tenantId, dimension, 25],
    queryFn: () => api.costBreakdown(tenantId as string, dimension, 25),
    enabled: tenantId !== null,
  })
  const points = daily.data?.points ?? []
  const items = breakdown.data?.items ?? []
  const total = points.reduce((sum, p) => sum + p.amount, 0)
  const provisional = daily.data?.source === 'cost_explorer'
  const month = breakdown.data
    ? new Date(`${breakdown.data.month}-01T00:00:00Z`).toLocaleDateString('en-GB', {
        month: 'long',
        year: 'numeric',
        timeZone: 'UTC',
      })
    : ''

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Cost Explorer</h1>
        <p className="text-sm text-secondary">Amortised cost after credits, from your AWS Cost and Usage Report.</p>
      </div>
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
            <h2 className="text-base font-semibold">Daily spend</h2>
            <p className="text-xs text-muted">
              {points.length > 0 ? `${money(total)} over ${points.length} days` : 'No daily data yet'}
            </p>
          </div>
          {!provisional && (
            <Toggle
              label="Range"
              options={RANGES.map((r) => ({ id: r, label: `${r} days` }))}
              value={days}
              onChange={setDays}
            />
          )}
        </div>
        {daily.isLoading && <p className="text-sm text-secondary">Loading…</p>}
        {points.length > 0 && <DailySpendChart points={points} currency="USD" />}
      </Card>
      <Card className="p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">
              Spend by {dimension} · {month}
            </h2>
            <p className="text-xs text-muted">
              {breakdown.data && breakdown.data.total > 0 ? `${money(breakdown.data.total)} excluding tax` : ''}
            </p>
          </div>
          <Toggle label="Group by" options={DIMENSIONS} value={dimension} onChange={setDimension} />
        </div>
        {breakdown.isLoading && <p className="text-sm text-secondary">Loading…</p>}
        {breakdown.data && items.length === 0 && (
          <p className="text-sm text-secondary">
            {breakdown.data.source === 'cost_explorer'
              ? 'Available once the first Cost and Usage Report is loaded.'
              : 'No spend this month yet.'}
          </p>
        )}
        {items.length > 0 && (
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
        )}
      </Card>
      {tenantId !== null && <CreditsCard tenantId={tenantId} />}
    </div>
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
          <Button variant="secondary" onClick={() => setEditing(true)}>
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
      {editing && <BalanceForm tenantId={tenantId} current={c} onDone={() => setEditing(false)} />}
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
    <dl className="mt-3 grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
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
      <div className="grid gap-3 sm:grid-cols-4">
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
