import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { useApi } from '../app/context'
import type { Budget, BudgetInput, BudgetStatus, BudgetThresholdInput } from '../api/types'
import { Alert, Button, Card, Field, cx } from '../components/ui'
import { useWorkspace } from './Workspace'

const STATUS: Record<BudgetStatus, [string, string]> = {
  on_track: ['bg-good-soft text-good-ink', 'On track'],
  at_risk: ['bg-warning-soft text-warning-ink', 'Forecast over budget'],
  over: ['bg-critical-soft text-critical-ink', 'Over budget'],
  no_data: ['bg-surface-2 text-secondary', 'No bills yet this month'],
  no_rate: ['bg-surface-2 text-secondary', 'No exchange rate'],
}

const DEFAULT_THRESHOLDS: BudgetThresholdInput[] = [
  { kind: 'actual', percent: 50 },
  { kind: 'actual', percent: 80 },
  { kind: 'actual', percent: 100 },
  { kind: 'forecast', percent: 100 },
]

function amount(value: number | null, currency: string): string {
  if (value === null) return '—'
  return `${currency} ${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

/** Monthly budgets with forecast alerts by email (PRD US-6.1/6.2). */
export function Budgets() {
  const api = useApi()
  const { me, tenantId, tenant } = useWorkspace()
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  const canEdit = role === 'admin' || role === 'member'
  const [creating, setCreating] = useState(false)
  const budgets = useQuery({
    queryKey: ['budgets', tenantId],
    queryFn: () => api.listBudgets(tenantId as string),
    enabled: tenantId !== null,
  })
  if (tenantId === null) return null
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Budgets</h1>
          <p className="text-sm text-secondary">
            Monthly budgets in USD or {tenant.data?.displayCurrency ?? 'your currency'}, with an email when spend or the
            month-end forecast crosses a threshold. Each threshold alerts once a month.
          </p>
        </div>
        {canEdit && !creating && (
          <Button onClick={() => setCreating(true)}>
            <Plus size={16} /> New budget
          </Button>
        )}
      </div>
      {creating && (
        <Card>
          <h2 className="mb-4 text-lg font-semibold">New budget</h2>
          <BudgetForm tenantId={tenantId} onDone={() => setCreating(false)} />
        </Card>
      )}
      {budgets.isLoading && <p className="text-sm text-secondary">Loading…</p>}
      {budgets.error && <Alert tone="error">{budgets.error.message}</Alert>}
      {budgets.data?.length === 0 && !creating && (
        <Alert tone="info">
          No budgets yet.{' '}
          {canEdit ? 'Create one to get an email before the month ends over budget.' : 'Members and admins can create them.'}
        </Alert>
      )}
      {budgets.data?.map((b) => <BudgetCard key={b.id} budget={b} tenantId={tenantId} canEdit={canEdit} />)}
    </div>
  )
}

function BudgetCard({ budget: b, tenantId, canEdit }: { budget: Budget; tenantId: string; canEdit: boolean }) {
  const api = useApi()
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const refresh = () => void qc.invalidateQueries({ queryKey: ['budgets', tenantId] })
  const toggle = useMutation({
    mutationFn: () => api.updateBudget(tenantId, b.id, { isActive: !b.isActive }),
    onSuccess: refresh,
  })
  const remove = useMutation({ mutationFn: () => api.deleteBudget(tenantId, b.id), onSuccess: refresh })
  const [style, label] = STATUS[b.status]
  const spent = Math.min(100, b.actualPercent ?? 0)
  const forecast = b.forecastPercent === null ? null : Math.min(100, b.forecastPercent)
  const usd = b.currency !== 'USD'
  if (editing) {
    return (
      <Card>
        <h2 className="mb-4 text-lg font-semibold">Edit {b.name}</h2>
        <BudgetForm tenantId={tenantId} budget={b} onDone={() => setEditing(false)} />
      </Card>
    )
  }
  return (
    <Card className={cx(!b.isActive && 'opacity-70')}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-medium">{b.name}</p>
          <p className="text-xs text-muted">
            {amount(b.amount, b.currency)} a month
            {usd && b.amountUsd !== null && ` (USD ${b.amountUsd.toLocaleString()})`} ·{' '}
            {b.scope.type === 'tenant' ? 'whole workspace' : `accounts ${b.scope.accounts.join(', ')}`}
            {!b.isActive && ' · paused'}
          </p>
        </div>
        <span className={cx('inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium', style)}>{label}</span>
      </div>
      {b.status === 'no_rate' ? (
        <div className="mt-4">
          <Alert tone="warning">
            There is no exchange rate for {b.currency} yet, so this budget is not checked. Cloudoptics staff set the
            rate; spend so far is USD {b.actualUsd.toLocaleString()}.
          </Alert>
        </div>
      ) : (
        <div className="mt-4 flex flex-col gap-2">
          <div
            role="progressbar"
            aria-label={`${b.name}: spent so far`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(spent)}
            className="relative h-3 w-full overflow-hidden rounded-full bg-border"
          >
            <div
              className={cx(
                'h-full rounded-full',
                b.status === 'over' ? 'bg-critical-ink' : b.status === 'at_risk' ? 'bg-warning-ink' : 'bg-accent',
              )}
              style={{ width: `${spent}%` }}
            />
            {forecast !== null && (
              <div
                title="Forecast for the month"
                className="absolute top-0 h-full w-0.5 bg-primary"
                style={{ left: `calc(${forecast}% - 1px)` }}
              />
            )}
          </div>
          <p className="text-sm">
            Spent {amount(b.actual, b.currency)} ({b.actualPercent ?? 0}%)
            {usd && ` · USD ${b.actualUsd.toLocaleString()}`}
            {b.forecast !== null && ` · forecast ${amount(b.forecast, b.currency)} (${b.forecastPercent}%)`}
            {b.dataThrough && <span className="text-muted"> · bills to {b.dataThrough}</span>}
          </p>
        </div>
      )}
      <ul className="mt-3 flex flex-wrap gap-2 text-xs" aria-label="Thresholds">
        {b.thresholds.map((t) => (
          <li
            key={`${t.kind}-${t.percent}`}
            className={cx('rounded-full px-2.5 py-0.5', t.crossed ? 'bg-warning-soft text-warning-ink' : 'bg-surface-2 text-secondary')}
          >
            {t.percent}% of {t.kind === 'actual' ? 'spend' : 'forecast'}
            {t.alerted ? ' · alerted' : t.crossed ? ' · crossed' : ''}
          </li>
        ))}
      </ul>
      {canEdit && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="secondary" onClick={() => setEditing(true)}>
            Edit
          </Button>
          <Button variant="secondary" disabled={toggle.isPending} onClick={() => toggle.mutate()}>
            {b.isActive ? 'Pause alerts' : 'Resume alerts'}
          </Button>
          {confirming ? (
            <>
              <Button disabled={remove.isPending} onClick={() => remove.mutate()}>
                Delete {b.name}
              </Button>
              <Button variant="ghost" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <Button variant="ghost" onClick={() => setConfirming(true)}>
              <Trash2 size={16} /> Delete…
            </Button>
          )}
        </div>
      )}
      {(toggle.error ?? remove.error) && <Alert tone="error">{(toggle.error ?? remove.error)?.message}</Alert>}
    </Card>
  )
}

function BudgetForm({ tenantId, budget, onDone }: { tenantId: string; budget?: Budget; onDone: () => void }) {
  const api = useApi()
  const qc = useQueryClient()
  const { tenant } = useWorkspace()
  const local = tenant.data?.displayCurrency ?? 'USD'
  const accounts = useQuery({ queryKey: ['accounts', tenantId], queryFn: () => api.listAccounts(tenantId) })
  const members = useQuery({ queryKey: ['members', tenantId], queryFn: () => api.listMembers(tenantId) })
  const [name, setName] = useState(budget?.name ?? '')
  const [value, setValue] = useState(budget ? String(budget.amount) : '')
  const [currency, setCurrency] = useState(budget?.currency ?? local)
  const [scopeType, setScopeType] = useState<'tenant' | 'accounts'>(budget?.scope.type ?? 'tenant')
  const [chosen, setChosen] = useState<string[]>(budget?.scope.accounts ?? [])
  const [thresholds, setThresholds] = useState<BudgetThresholdInput[]>(
    budget?.thresholds.map(({ kind, percent }) => ({ kind, percent })) ?? DEFAULT_THRESHOLDS,
  )
  const [users, setUsers] = useState<string[]>(budget?.recipients.users ?? [])
  const [emails, setEmails] = useState((budget?.recipients.emails ?? []).join(', '))
  const save = useMutation({
    mutationFn: (body: BudgetInput) =>
      budget ? api.updateBudget(tenantId, budget.id, body) : api.createBudget(tenantId, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['budgets', tenantId] })
      onDone()
    },
  })
  const toggle = (list: string[], item: string) => (list.includes(item) ? list.filter((x) => x !== item) : [...list, item])
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate({
      name,
      amount: Number(value),
      currency,
      scope: { type: scopeType, accounts: scopeType === 'accounts' ? chosen : [] },
      thresholds,
      recipients: {
        users,
        emails: emails
          .split(/[\s,;]+/)
          .map((x) => x.trim())
          .filter(Boolean),
      },
    })
  }
  const currencies = local === 'USD' ? ['USD'] : [local, 'USD']
  const choosable = (accounts.data?.accounts ?? []).filter((a) => a.orgStatus !== 'NOT_LISTED')
  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} />
        <Field
          label="Amount a month"
          type="number"
          min="0.01"
          step="0.01"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          required
        />
        <div className="flex flex-col gap-1">
          <label htmlFor="budget-currency" className="text-sm font-medium">
            Budget currency
          </label>
          <select
            id="budget-currency"
            value={currency}
            onChange={(e) => setCurrency(e.target.value)}
            className="h-9 rounded-sm border border-border-strong bg-surface-1 px-3 text-sm"
          >
            {currencies.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </div>
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-sm font-medium">Spend that counts</legend>
        <label className="flex items-center gap-2 text-sm">
          <input type="radio" checked={scopeType === 'tenant'} onChange={() => setScopeType('tenant')} /> The whole
          workspace
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="radio" checked={scopeType === 'accounts'} onChange={() => setScopeType('accounts')} /> Only
          these accounts
        </label>
        {scopeType === 'accounts' && (
          <div className="ml-6 flex flex-wrap gap-3">
            {choosable.map((a) => (
              <label key={a.awsAccountId} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={chosen.includes(a.awsAccountId)}
                  onChange={() => setChosen(toggle(chosen, a.awsAccountId))}
                />
                <span className="font-mono">{a.awsAccountId}</span>
                {a.name && <span className="text-muted">{a.name}</span>}
              </label>
            ))}
          </div>
        )}
      </fieldset>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-sm font-medium">Alert when</legend>
        {thresholds.map((t, i) => (
          <div key={i} className="flex flex-wrap items-center gap-2 text-sm">
            <input
              type="number"
              aria-label={`Threshold ${i + 1} percent`}
              min={1}
              max={1000}
              value={t.percent}
              onChange={(e) => setThresholds(thresholds.map((x, j) => (j === i ? { ...x, percent: Number(e.target.value) } : x)))}
              className="h-9 w-20 rounded-sm border border-border-strong bg-surface-1 px-2"
            />
            <span>% of</span>
            <select
              aria-label={`Threshold ${i + 1} measure`}
              value={t.kind}
              onChange={(e) =>
                setThresholds(
                  thresholds.map((x, j) => (j === i ? { ...x, kind: e.target.value as BudgetThresholdInput['kind'] } : x)),
                )
              }
              className="h-9 rounded-sm border border-border-strong bg-surface-1 px-2"
            >
              <option value="actual">spend so far</option>
              <option value="forecast">the month-end forecast</option>
            </select>
            {thresholds.length > 1 && (
              <Button variant="ghost" onClick={() => setThresholds(thresholds.filter((_, j) => j !== i))}>
                Remove
              </Button>
            )}
          </div>
        ))}
        {thresholds.length < 10 && (
          <div>
            <Button variant="ghost" onClick={() => setThresholds([...thresholds, { kind: 'actual', percent: 90 }])}>
              <Plus size={16} /> Add a threshold
            </Button>
          </div>
        )}
      </fieldset>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-sm font-medium">Email</legend>
        <div className="flex flex-wrap gap-3">
          {(members.data ?? []).map((m) => (
            <label key={m.userId} className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={users.includes(m.userId)} onChange={() => setUsers(toggle(users, m.userId))} />
              {m.displayName ?? m.email}
            </label>
          ))}
        </div>
        <Field
          label="Other addresses"
          hint="Separate with commas, for example finance@example.com"
          value={emails}
          onChange={(e) => setEmails(e.target.value)}
        />
      </fieldset>
      {save.error && <Alert tone="error">{save.error.message}</Alert>}
      <div className="flex gap-2">
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? 'Saving…' : budget ? 'Save changes' : 'Create budget'}
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
