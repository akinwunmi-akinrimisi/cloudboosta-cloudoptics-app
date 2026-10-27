import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { useState } from 'react'

import { useApi } from '../app/context'
import { ApiError, type AlertSettings, type AlertSettingsView, type Anomaly, type AnomalyStatus } from '../api/types'
import { AnomalyChart } from '../components/charts'
import { Alert, Button, Card, cx } from '../components/ui'
import { useMoney, useWorkspace } from './Workspace'

const STATUS: Record<AnomalyStatus, [string, string]> = {
  open: ['bg-critical-soft text-critical-ink', 'Open'],
  investigating: ['bg-warning-soft text-warning-ink', 'Investigating'],
  expected: ['bg-surface-2 text-secondary', 'Expected'],
  resolved: ['bg-good-soft text-good-ink', 'Resolved'],
}
const FILTERS: (AnomalyStatus | 'all')[] = ['open', 'investigating', 'expected', 'resolved', 'all']
const selectClass = 'h-9 rounded-sm border border-border-strong bg-surface-1 px-3 text-sm'

function dayLabel(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  })
}

/** Unusual daily spend per account and service, with triage (PRD US-6.3). */
export function Anomalies() {
  const api = useApi()
  const { me, tenantId } = useWorkspace()
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  const [filter, setFilter] = useState<AnomalyStatus | 'all'>('open')
  const anomalies = useQuery({
    queryKey: ['anomalies', tenantId],
    queryFn: () => api.listAnomalies(tenantId as string),
    enabled: tenantId !== null,
  })
  if (tenantId === null) return null
  const all = anomalies.data ?? []
  const shown = filter === 'all' ? all : all.filter((a) => a.status === filter)
  const count = (s: AnomalyStatus | 'all') => (s === 'all' ? all.length : all.filter((a) => a.status === s).length)
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Anomalies</h1>
        <p className="text-sm text-secondary">
          Days when an account&apos;s spend on a service is well above the same weekday over the four weeks before.
          Checked each time new bills arrive; new anomalies are emailed to admins and members.
        </p>
      </div>
      <div role="tablist" aria-label="Status" className="flex flex-wrap gap-2">
        {FILTERS.map((s) => (
          <button
            key={s}
            role="tab"
            aria-selected={filter === s}
            onClick={() => setFilter(s)}
            className={cx(
              'rounded-full border px-3 py-1 text-sm',
              filter === s ? 'border-accent bg-accent-soft text-primary' : 'border-border text-secondary hover:bg-surface-2',
            )}
          >
            {s === 'all' ? 'All' : STATUS[s][1]} ({count(s)})
          </button>
        ))}
      </div>
      {anomalies.isLoading && <p className="text-sm text-secondary">Loading…</p>}
      {anomalies.error && <Alert tone="error">{anomalies.error.message}</Alert>}
      {anomalies.data && shown.length === 0 && (
        <Alert tone="info">
          {all.length === 0
            ? 'No anomalies in the last 90 days. Detection needs four weeks of bills, so a new workspace starts finding them after its first month.'
            : 'None with this status.'}
        </Alert>
      )}
      {shown.map((a) => (
        <AnomalyCard key={a.id} anomaly={a} tenantId={tenantId} canEdit={role === 'admin' || role === 'member'} />
      ))}
      {role !== undefined && <AlertSettingsCard tenantId={tenantId} canEdit={role === 'admin'} />}
    </div>
  )
}

function AnomalyCard({ anomaly: a, tenantId, canEdit }: { anomaly: Anomaly; tenantId: string; canEdit: boolean }) {
  const { money } = useMoney()
  const [open, setOpen] = useState(false)
  const [style, label] = STATUS[a.status]
  return (
    <section className="rounded-md border border-border bg-surface-1">
      <button
        className="flex w-full flex-wrap items-start justify-between gap-3 p-6 text-left"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <div className="flex items-start gap-2">
          {open ? (
            <ChevronDown size={18} className="mt-0.5 shrink-0" />
          ) : (
            <ChevronRight size={18} className="mt-0.5 shrink-0" />
          )}
          <div>
            <p className="font-medium">
              {a.serviceName} in {a.accountName}
              {a.accountName !== a.accountId && <span className="text-muted"> ({a.accountId})</span>}
            </p>
            <p className="text-sm text-secondary">
              {dayLabel(a.date)}: {money(a.actualUsd)} against {money(a.expectedUsd)} expected ·{' '}
              <span className="font-medium text-primary">{money(a.impactUsd)} above</span>
              {a.deviationPercent === null ? ' (new spend)' : ` (+${Math.round(a.deviationPercent)}%)`}
            </p>
          </div>
        </div>
        <span className={cx('inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium', style)}>
          {label}
          {a.suppressed && ' (suppressed)'}
        </span>
      </button>
      {open && <AnomalyDetailPanel anomaly={a} tenantId={tenantId} canEdit={canEdit} />}
    </section>
  )
}

function AnomalyDetailPanel({ anomaly: a, tenantId, canEdit }: { anomaly: Anomaly; tenantId: string; canEdit: boolean }) {
  const api = useApi()
  const qc = useQueryClient()
  const { money } = useMoney()
  const [note, setNote] = useState(a.note ?? '')
  const detail = useQuery({ queryKey: ['anomaly', tenantId, a.id], queryFn: () => api.getAnomaly(tenantId, a.id) })
  const change = useMutation({
    mutationFn: (status: AnomalyStatus) => api.updateAnomaly(tenantId, a.id, { status, note: note.trim() || null }),
    onSuccess: (updated) => {
      qc.setQueryData(['anomaly', tenantId, a.id], updated)
      void qc.invalidateQueries({ queryKey: ['anomalies', tenantId] })
    },
  })
  return (
    <div className="flex flex-col gap-4 border-t border-border p-6">
      {detail.data && <AnomalyChart points={detail.data.series} highlight={a.date} currency="USD" />}
      {detail.error && <Alert tone="error">{detail.error.message}</Alert>}
      {a.contributors.length > 0 && (
        <div>
          <h3 className="mb-2 text-sm font-medium">What rose most</h3>
          <div className="overflow-x-auto">
          <table className="w-full whitespace-nowrap text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                <th className="pb-1 font-normal">Usage type</th>
                <th className="pb-1 font-normal">Region</th>
                <th className="pb-1 text-right font-normal">That day</th>
                <th className="pb-1 text-right font-normal">Expected</th>
                <th className="pb-1 text-right font-normal">Above</th>
              </tr>
            </thead>
            <tbody>
              {a.contributors.map((c) => (
                <tr key={`${c.usageType}|${c.region}`} className="border-t border-border">
                  <td className="py-1 pr-3 font-mono text-xs">{c.usageType}</td>
                  <td className="py-1 pr-3">{c.region ?? 'Global'}</td>
                  <td className="py-1 pl-3 text-right">{money(c.actualUsd)}</td>
                  <td className="py-1 pl-3 text-right">{money(c.expectedUsd)}</td>
                  <td className="py-1 pl-3 text-right font-medium">{money(c.impactUsd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      )}
      {a.suppressed && (
        <p className="text-sm text-secondary">
          Stored as expected without an alert: the same account and service was marked expected in the week before.
        </p>
      )}
      {a.statusChangedAt && (
        <p className="text-xs text-muted">
          Marked {STATUS[a.status][1].toLowerCase()}
          {a.statusChangedBy && ` by ${a.statusChangedBy}`} on {new Date(a.statusChangedAt).toLocaleString()}
          {a.note && ` · “${a.note}”`}
        </p>
      )}
      {canEdit ? (
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Note (optional)</span>
            <input
              className={selectClass}
              value={note}
              maxLength={500}
              placeholder="For example: planned load test"
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          <div className="flex flex-wrap gap-2">
            {(['investigating', 'expected', 'resolved', 'open'] as AnomalyStatus[])
              .filter((s) => s !== a.status || note.trim() !== (a.note ?? ''))
              .map((s) => (
                <Button key={s} variant={s === 'expected' ? 'primary' : 'secondary'} disabled={change.isPending} onClick={() => change.mutate(s)}>
                  {s === 'open' ? 'Reopen' : `Mark ${STATUS[s][1].toLowerCase()}`}
                </Button>
              ))}
          </div>
          <p className="text-xs text-muted">Expected also stops alerts for this account and service for the next 7 days.</p>
          {change.error && <Alert tone="error">{change.error.message}</Alert>}
        </div>
      ) : (
        <p className="text-xs text-muted">Members and admins can mark anomalies.</p>
      )}
    </div>
  )
}

function AlertSettingsCard({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const api = useApi()
  const view = useQuery({ queryKey: ['alert-settings', tenantId], queryFn: () => api.getAlertSettings(tenantId) })
  if (!view.data) return null
  return <AlertSettingsForm key={JSON.stringify(view.data.settings)} tenantId={tenantId} canEdit={canEdit} view={view.data} />
}

function AlertSettingsForm({ tenantId, canEdit, view }: { tenantId: string; canEdit: boolean; view: AlertSettingsView }) {
  const api = useApi()
  const qc = useQueryClient()
  const { tenant } = useWorkspace()
  const local = tenant.data?.displayCurrency ?? 'USD'
  const [draft, setDraft] = useState<AlertSettings>(view.settings)
  const [emails, setEmails] = useState(view.settings.anomalyEmails.join(', '))
  const next = { ...draft, anomalyEmails: emails.split(/[\s,;]+/).filter(Boolean) }
  const save = useMutation({
    mutationFn: () => api.saveAlertSettings(tenantId, next),
    onSuccess: (updated) => qc.setQueryData(['alert-settings', tenantId], updated),
  })
  const changed = JSON.stringify(next) !== JSON.stringify(view.settings)
  const s = view.settings
  return (
    <Card className="flex flex-col gap-4">
      <div>
        <h2 className="text-base font-semibold">Alert thresholds</h2>
        <p className="mt-1 text-sm text-secondary">
          A day counts as an anomaly only when it is at least {s.anomalyMinPercent}% above the expected level (or three
          times the usual day-to-day variation, if that is larger) and at least {s.anomalyMinImpactCurrency}{' '}
          {s.anomalyMinImpact.toLocaleString()} above it.
          {view.minImpactUsd === null &&
            ` There is no ${s.anomalyMinImpactCurrency} exchange rate yet, so USD ${view.defaults.anomalyMinImpact} applies for now.`}
        </p>
      </div>
      <fieldset className="grid gap-4 sm:grid-cols-3" disabled={!canEdit}>
        <div className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Minimum above expected, per day</span>
          <div className="flex gap-2">
            <select
              className={selectClass}
              aria-label="Currency"
              value={draft.anomalyMinImpactCurrency}
              onChange={(e) => setDraft({ ...draft, anomalyMinImpactCurrency: e.target.value })}
            >
              {[...new Set(['USD', local])].map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
            <input
              type="number"
              min={1}
              aria-label="Minimum above expected, per day"
              className={cx(selectClass, 'w-full')}
              value={draft.anomalyMinImpact}
              onChange={(e) => setDraft({ ...draft, anomalyMinImpact: Number(e.target.value) })}
            />
          </div>
        </div>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">And at least</span>
          <select
            className={selectClass}
            value={draft.anomalyMinPercent}
            onChange={(e) => setDraft({ ...draft, anomalyMinPercent: Number(e.target.value) })}
          >
            {[10, 20, 30, 50, 75, 100, 200].map((n) => (
              <option key={n} value={n}>
                {n}% above expected
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Also email</span>
          <input
            className={selectClass}
            value={emails}
            placeholder="ops@example.com"
            onChange={(e) => setEmails(e.target.value)}
          />
        </label>
      </fieldset>
      {save.error && (
        <Alert tone="error">{save.error instanceof ApiError ? save.error.message : 'Could not save. Try again.'}</Alert>
      )}
      {canEdit ? (
        <div className="flex items-center gap-3">
          <Button disabled={!changed || !(draft.anomalyMinImpact >= 1) || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : 'Save'}
          </Button>
          <p className="text-xs text-muted">New thresholds apply from the next check (within an hour).</p>
        </div>
      ) : (
        <p className="text-xs text-muted">Admins can change these.</p>
      )}
    </Card>
  )
}
