import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { useApi } from '../app/context'
import type { Connection, PeriodReload, ReloadStatus } from '../api/types'
import { Alert, Button } from '../components/ui'

const OUTCOME: Record<PeriodReload['outcome'], string> = {
  reloaded: 'Reloaded',
  unchanged: 'Unchanged (the bills read the same)',
  failed: 'Failed',
  not_in_bucket: 'Not in the bucket',
}

function monthName(month: string): string {
  return new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' })
}

function timeOf(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** This month and the 12 before it, newest first (YYYY-MM). */
function recentMonths(): string[] {
  const now = new Date()
  return Array.from({ length: 13 }, (_, i) =>
    new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - i, 1)).toISOString().slice(0, 7),
  )
}

/** What a reload changes: the cost views and the recommendations. */
const REFRESH_AFTER = [
  'cost-months',
  'cost-daily',
  'cost-breakdown',
  'cost-changes',
  'cost-summary',
  'cost-forecast',
  'recommendations',
  'recommendation-summary',
]

const running = (s: ReloadStatus | undefined) => s?.status === 'queued' || s?.status === 'running'

/** Admins: read a connection's bills again from its S3 bucket (all months, or one), with progress and the result per month. */
export function ReloadBills({ connection: c, tenantId }: { connection: Connection; tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [period, setPeriod] = useState('')
  const status = useQuery({
    queryKey: ['reload', tenantId, c.id],
    queryFn: () => api.reloadStatus(tenantId, c.id),
    // Follow a reload until the worker finishes it, also after leaving the page.
    refetchInterval: (q) => (running(q.state.data) ? 2_000 : false),
  })
  const reload = useMutation({
    mutationFn: () => api.reloadBills(tenantId, c.id, period || null),
    onSuccess: (s) => qc.setQueryData(['reload', tenantId, c.id], s),
  })
  const s = status.data
  const busy = running(s)
  // When a reload finishes, show the new figures everywhere.
  const wasBusy = useRef(busy)
  useEffect(() => {
    if (wasBusy.current && !busy) {
      for (const key of REFRESH_AFTER) void qc.invalidateQueries({ queryKey: [key, tenantId] })
    }
    wasBusy.current = busy
  }, [busy, qc, tenantId])
  const waitUntil = s?.nextAllowedAt && !busy ? s.nextAllowedAt : null

  return (
    <div className="mt-4 flex flex-col gap-3 rounded-sm bg-surface-2 p-3 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-medium">Bills</p>
          <p className="text-secondary">
            Cloudoptics loads new bills from your S3 bucket every 6 hours. Reload them to read months again.
          </p>
        </div>
        {!open && (
          <Button variant="secondary" onClick={() => setOpen(true)}>
            Reload bills…
          </Button>
        )}
      </div>
      {open && (
        <div className="flex flex-col gap-3 border-t border-border pt-3">
          <p className="text-secondary">
            Reloading reads the bills for this account again from your S3 bucket ({c.curBucket}), the latest version AWS
            delivered for each month, and replaces what Cloudoptics holds for those months. Use it when a month looks
            incomplete or out of date, or after Cloudoptics starts reading more from the bills (such as per-resource
            costs). Recommendations, budgets and anomalies are checked again afterwards. One reload an hour.
          </p>
          <p className="text-secondary">
            Months before the export was created are not in the bucket. AWS can fill them in: open a support case
            (Billing) and ask for a backfill of the Data Export into the same bucket, then reload.
          </p>
          <div className="flex flex-wrap items-end gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted">Months</span>
              <select
                className="h-9 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm"
                value={period}
                onChange={(e) => setPeriod(e.target.value)}
                disabled={busy || reload.isPending}
              >
                <option value="">All months in the export</option>
                {recentMonths().map((m) => (
                  <option key={m} value={m}>
                    {monthName(m)}
                  </option>
                ))}
              </select>
            </label>
            <Button disabled={busy || reload.isPending || waitUntil !== null} onClick={() => reload.mutate()}>
              {busy || reload.isPending ? 'Reloading…' : 'Reload'}
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Close
            </Button>
          </div>
          {waitUntil && <p className="text-xs text-muted">You can reload again after {timeOf(waitUntil)}.</p>}
        </div>
      )}
      {reload.error && <Alert tone="error">{reload.error.message}</Alert>}
      {s && s.status !== 'none' && <ReloadResult status={s} />}
    </div>
  )
}

function ReloadResult({ status: s }: { status: ReloadStatus }) {
  const what = s.period ? monthName(s.period) : 'all months in the export'
  if (running(s)) {
    return (
      <p role="status" className="text-secondary">
        Reloading {what}
        {s.requestedAt && `, started ${timeOf(s.requestedAt)}`}… This usually takes a few minutes.
      </p>
    )
  }
  const tone = s.status === 'succeeded' ? 'info' : s.status === 'partial' ? 'warning' : 'error'
  const title =
    s.status === 'succeeded'
      ? 'Bills reloaded'
      : s.status === 'partial'
        ? 'Bills partly reloaded'
        : 'The reload did not finish'
  return (
    <Alert tone={tone}>
      <p className="font-medium">
        {title}: {what}
        {s.finishedAt && `, ${new Date(s.finishedAt).toLocaleString()}`}.
      </p>
      {s.error && <p>{s.error}</p>}
      {s.periods.length > 0 && (
        <ul className="mt-1 flex flex-col gap-0.5">
          {s.periods.map((p) => (
            <li key={p.period}>
              {monthName(p.period)}: {OUTCOME[p.outcome]}
              {p.detail && ` (${p.detail})`}
            </li>
          ))}
        </ul>
      )}
      {s.periods.some((p) => p.outcome === 'not_in_bucket') && (
        <p className="mt-1">Months not in the bucket need AWS to backfill the export (a support case).</p>
      )}
    </Alert>
  )
}
