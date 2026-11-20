import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useRef, useState } from 'react'

import { useApi } from '../app/context'
import type { Connection, DataDeletion, StackSteps, Tenant } from '../api/types'
import { ApiError } from '../api/types'
import { useEscapeToClose } from '../components/overlay'
import { Alert, Button, Card } from '../components/ui'
import { accountLabel, asProvider, connectionKind, connectionLabel, providerOf } from '../lib/providers'

// Deleting data (PRD US-8.3): a disconnected connection's data straight away, the whole
// workspace after a grace period. Cloudoptics cannot touch the customer's AWS accounts, so
// both flows show which CloudFormation stacks to delete there.

const inputClass = 'h-9 rounded-sm border border-border-strong bg-surface-1 px-3 text-sm'

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong. Try again.'
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString('en-GB', { dateStyle: 'long', timeStyle: 'short' })
}

/** "2,940 records across 17 tables, each checked to be empty afterwards." */
export function verificationSummary(deletion: DataDeletion): string {
  const v = deletion.verification
  if (!v) return ''
  const tables = Object.keys(v.tables).length
  return `${v.rowsDeleted.toLocaleString('en')} records across ${tables} tables, each checked to be empty afterwards.`
}

/** For a disconnected connection (admins): delete everything Cloudoptics holds for the account. */
export function DeleteConnectionData({
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
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLFormElement>(null)
  useEscapeToClose(open, () => setOpen(false), trigger, panel)
  const remove = useMutation({
    mutationFn: () => api.deleteConnectionData(tenantId, c.id, typed.trim()),
    onSuccess: (deletion) => {
      onDeleted(deletion)
      for (const key of ['connections', 'accounts', 'deletions', 'audit-log', 'resource-summary']) {
        void qc.invalidateQueries({ queryKey: [key, tenantId] })
      }
    },
  })
  if (!open) {
    return (
      <Button ref={trigger} variant="ghost" onClick={() => setOpen(true)}>
        Delete data…
      </Button>
    )
  }
  return (
    <form
      ref={panel}
      className="flex w-full flex-col gap-2 rounded-sm border border-critical-ink/40 bg-critical-soft/40 p-3 text-sm"
      onSubmit={(e) => {
        e.preventDefault()
        remove.mutate()
      }}
    >
      <p>
        Delete everything Cloudoptics holds for {connectionLabel(c)}: its bills, costs, resources, usage metrics,
        recommendations and anomalies. This cannot be undone. The audit log keeps a record of the deletion, and you can
        connect the {connectionKind(c)} again later.
      </p>
      <label className="flex flex-col gap-1">
        <span className="font-medium">
          Type the {connectionKind(c)} ID <span className="break-all">{c.awsAccountId}</span> to confirm
        </span>
        <input
          className={inputClass}
          value={typed}
          inputMode={providerOf(c.provider) === 'aws' ? 'numeric' : 'text'}
          autoComplete="off"
          onChange={(e) => setTyped(e.target.value)}
        />
      </label>
      {remove.error && <Alert tone="error">{errorText(remove.error)}</Alert>}
      <div className="flex gap-2">
        <Button type="submit" disabled={typed.trim() !== c.awsAccountId || remove.isPending}>
          {remove.isPending ? 'Deleting…' : 'Delete data'}
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

/** Where to delete our stacks in each AWS account (we have read-only access, so we cannot). */
export function StackStepsList({ steps, consoleUrl }: { steps: StackSteps[]; consoleUrl: string }) {
  if (steps.length === 0) return null
  return (
    <div className="flex flex-col gap-2 text-sm">
      <h3 className="font-medium">Remove Cloudoptics from your AWS accounts</h3>
      <p className="text-secondary">
        Cloudoptics can only read your accounts, so it cannot delete anything there. Sign in to each account, open{' '}
        <a className="text-accent underline" href={consoleUrl} target="_blank" rel="noreferrer">
          CloudFormation in us-east-1
        </a>{' '}
        and delete these stacks:
      </p>
      <ul className="flex flex-col divide-y divide-border rounded-sm border border-border">
        {steps.map((s) => (
          <li key={s.awsAccountId} className="flex flex-wrap justify-between gap-2 px-3 py-2">
            <span>{accountLabel(s.provider, s.awsAccountId)}</span>
            <span className="font-mono text-xs">{s.stacks.join(', ')}</span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted">
        AWS keeps the S3 bucket with your billing export when the stack is deleted. Empty and delete it in the S3
        console if you no longer need the files.
      </p>
    </div>
  )
}

/** Settings, Workspace: delete the whole workspace after a grace period, or cancel it (admins). */
export function DangerZone({ tenant }: { tenant: Tenant }) {
  const api = useApi()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLFormElement>(null)
  useEscapeToClose(open, () => setOpen(false), trigger, panel)
  const view = useQuery({ queryKey: ['tenant-deletion', tenant.id], queryFn: () => api.getTenantDeletion(tenant.id) })
  const refresh = () => {
    for (const key of ['tenant-deletion', 'tenant', 'deletions', 'audit-log']) {
      void qc.invalidateQueries({ queryKey: [key, tenant.id] })
    }
  }
  const schedule = useMutation({
    mutationFn: () => api.scheduleTenantDeletion(tenant.id, typed),
    onSuccess: (updated) => {
      qc.setQueryData(['tenant-deletion', tenant.id], updated)
      setOpen(false)
      setTyped('')
      refresh()
    },
  })
  const cancel = useMutation({ mutationFn: () => api.cancelTenantDeletion(tenant.id), onSuccess: refresh })
  const scheduled = view.data?.deletion ?? null
  const graceDays = view.data?.graceDays ?? 7
  const error = schedule.error ?? cancel.error
  return (
    <Card className="flex flex-col gap-4 border-critical-ink/40">
      <div>
        <h2 className="text-base font-semibold text-critical-ink">Danger zone</h2>
        <p className="mt-1 text-sm text-secondary">
          Deleting the workspace removes all of its data from Cloudoptics: connections, bills and costs, resources,
          recommendations, budgets, anomalies, reports, settings and members. It happens {graceDays} days after you ask,
          and any admin can cancel it until then. Every admin gets an email when it is scheduled and when it is done.
          The audit log is kept for its retention period as a security record.
        </p>
      </div>
      {scheduled ? (
        <div className="flex flex-col gap-3">
          <Alert tone="warning">
            This workspace will be deleted on {formatDate(scheduled.scheduledFor)}
            {scheduled.requestedByEmail ? ` (asked by ${scheduled.requestedByEmail})` : ''}.
          </Alert>
          <div>
            <Button variant="secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
              {cancel.isPending ? 'Cancelling…' : 'Cancel deletion'}
            </Button>
          </div>
        </div>
      ) : open ? (
        <form
          ref={panel}
          className="flex flex-col gap-2 text-sm"
          onSubmit={(e) => {
            e.preventDefault()
            schedule.mutate()
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="font-medium">Type the workspace name ({tenant.name}) to confirm</span>
            <input className={inputClass} value={typed} autoComplete="off" onChange={(e) => setTyped(e.target.value)} />
          </label>
          <div className="flex gap-2">
            <Button type="submit" disabled={typed.trim() !== tenant.name.trim() || schedule.isPending}>
              {schedule.isPending ? 'Scheduling…' : `Delete in ${graceDays} days`}
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Keep the workspace
            </Button>
          </div>
        </form>
      ) : (
        <div>
          <Button ref={trigger} variant="secondary" onClick={() => setOpen(true)}>
            Delete workspace…
          </Button>
        </div>
      )}
      {error && <Alert tone="error">{errorText(error)}</Alert>}
      {view.data && <StackStepsList steps={view.data.stacks} consoleUrl={view.data.stacksConsoleUrl} />}
    </Card>
  )
}

const STATUS: Record<DataDeletion['status'], string> = {
  scheduled: 'Scheduled',
  cancelled: 'Cancelled',
  completed: 'Deleted',
  failed: 'Failed',
}

/** Every deletion request with its verification record (row counts before and after). */
export function DeletionHistory({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const list = useQuery({ queryKey: ['deletions', tenantId], queryFn: () => api.listDeletions(tenantId) })
  // A connection whose data was deleted is gone; its audit entry says which provider it was.
  const audit = useQuery({
    queryKey: ['audit-log', tenantId, 'deletions'],
    queryFn: () => api.auditLog(tenantId),
    enabled: (list.data ?? []).some((d) => d.kind === 'connection'),
  })
  const connections = useQuery({ queryKey: ['connections', tenantId], queryFn: () => api.listConnections(tenantId) })
  const label = (d: DataDeletion): string => {
    const conn = connections.data?.find((c) => c.id === d.connectionId)
    if (conn) return connectionLabel(conn)
    const entry = audit.data?.entries.find((e) => e.targetId === d.connectionId && e.action.startsWith('connection.'))
    // AWS entries from before ADR-018 carry only the account ID.
    const provider =
      asProvider(entry?.after?.provider) ?? (typeof entry?.after?.awsAccountId === 'string' ? 'aws' : undefined)
    return provider ? accountLabel(provider, d.targetLabel) : `Account ${d.targetLabel}`
  }
  return (
    <Card className="flex flex-col gap-3">
      <div>
        <h2 className="text-base font-semibold">Data deletions</h2>
        <p className="mt-1 text-sm text-secondary">
          Each deletion counts the rows in every table before and after, and is recorded in the audit log.
        </p>
      </div>
      {list.data?.length === 0 && <p className="text-sm text-muted">Nothing has been deleted.</p>}
      <ul className="flex flex-col divide-y divide-border">
        {(list.data ?? []).map((d) => (
          <li key={d.id} className="flex flex-col gap-1 py-2 text-sm">
            <div className="flex flex-wrap justify-between gap-2">
              <span className="font-medium">
                {d.kind === 'connection' ? label(d) : `Workspace ${d.targetLabel}`}
              </span>
              <span className="text-secondary">
                {STATUS[d.status]} ·{' '}
                {formatDate(d.completedAt ?? d.cancelledAt ?? (d.status === 'scheduled' ? d.scheduledFor : d.requestedAt))}
              </span>
            </div>
            {d.verification && (
              <details>
                <summary className="cursor-pointer text-xs text-secondary">{verificationSummary(d)}</summary>
                <div className="mt-2 overflow-x-auto" role="region" aria-label="Verification details" tabIndex={0}>
                <table className="w-full text-xs tabular-nums">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="font-medium">Table</th>
                      <th className="text-right font-medium">Before</th>
                      <th className="text-right font-medium">After</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(d.verification.tables).map(([table, n]) => (
                      <tr key={table}>
                        <td className="font-mono">{table}</td>
                        <td className="text-right">{n.before.toLocaleString('en')}</td>
                        <td className="text-right">{n.after.toLocaleString('en')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              </details>
            )}
          </li>
        ))}
      </ul>
    </Card>
  )
}

/** Across the app while a deletion is scheduled, so nobody misses it. */
export function DeletionBanner({ tenant, isAdmin }: { tenant: Tenant; isAdmin: boolean }) {
  if (tenant.status !== 'deleting') return null
  return (
    <div className="mb-4">
      <Alert tone="warning">
        This workspace is scheduled for deletion.{' '}
        {isAdmin ? (
          <Link to="/settings" search={{ section: 'workspace' }} className="font-medium underline">
            Review or cancel it in Settings
          </Link>
        ) : (
          'Ask a workspace admin if this is a mistake.'
        )}
      </Alert>
    </div>
  )
}
