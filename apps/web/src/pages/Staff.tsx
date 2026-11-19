import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from '@tanstack/react-router'
import { useState } from 'react'

import { rememberTenant, useApi, useMe } from '../app/context'
import type { SupportGrant, SupportSession } from '../api/types'
import { ApiError } from '../api/types'
import { Alert, Button, Card, Field, Logo } from '../components/ui'

// Staff break-glass tool (WBS ADM-6, docs/11b §7.4): internal, for Cloudoptics staff only
// (the API refuses everyone else). Access is read-only, time-boxed, and the workspace's
// admins are emailed and see it in their audit log.

const DURATIONS: [number, string][] = [
  [15, '15 minutes'],
  [30, '30 minutes'],
  [60, '1 hour'],
  [120, '2 hours'],
  [240, '4 hours'],
]

const STATUS: Record<SupportGrant['status'], string> = { active: 'Active', ended: 'Ended', expired: 'Expired' }

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong. Try again.'
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })
}

function RequestForm() {
  const api = useApi()
  const qc = useQueryClient()
  const [tenantId, setTenantId] = useState('')
  const [justification, setJustification] = useState('')
  const [ticketRef, setTicketRef] = useState('')
  const [minutes, setMinutes] = useState(60)
  const request = useMutation({
    mutationFn: () =>
      api.requestSupportGrant({
        tenantId: tenantId.trim(),
        justification: justification.trim(),
        ticketRef: ticketRef.trim() || null,
        durationMinutes: minutes,
      }),
    onSuccess: () => {
      setTenantId('')
      setJustification('')
      setTicketRef('')
      void qc.invalidateQueries({ queryKey: ['support-grants'] })
      void qc.invalidateQueries({ queryKey: ['me'] })
    },
  })
  const ready = tenantId.trim().length > 0 && justification.trim().length >= 10
  return (
    <Card>
      <h2 className="text-base font-semibold">Open support access</h2>
      <p className="mb-4 text-sm text-secondary">
        Read-only access to one workspace. It starts now and ends on its own. The workspace’s admins get an email and
        see who, why and for how long in their audit log.
      </p>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault()
          if (ready) request.mutate()
        }}
      >
        <Field label="Workspace ID" value={tenantId} onChange={(e) => setTenantId(e.target.value)} required />
        <div className="flex flex-col gap-1">
          <label htmlFor="support-justification" className="text-sm font-medium text-primary">
            Justification
          </label>
          <textarea
            id="support-justification"
            className="min-h-20 rounded-sm border border-border-strong bg-surface-1 px-3 py-2 text-sm"
            value={justification}
            maxLength={2000}
            onChange={(e) => setJustification(e.target.value)}
            required
          />
          <p className="text-xs text-muted">The workspace’s admins read this. At least 10 characters.</p>
        </div>
        <Field
          label="Ticket reference (optional)"
          value={ticketRef}
          maxLength={100}
          onChange={(e) => setTicketRef(e.target.value)}
        />
        <div className="flex flex-col gap-1">
          <label htmlFor="support-duration" className="text-sm font-medium text-primary">
            Duration
          </label>
          <select
            id="support-duration"
            className="h-9 w-48 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm"
            value={minutes}
            onChange={(e) => setMinutes(Number(e.target.value))}
          >
            {DURATIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        {request.error && <Alert tone="error">{errorText(request.error)}</Alert>}
        <Button type="submit" disabled={!ready || request.isPending}>
          Open read-only access
        </Button>
      </form>
    </Card>
  )
}

function GrantList() {
  const api = useApi()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { data: me } = useMe()
  const grants = useQuery({ queryKey: ['support-grants'], queryFn: () => api.listSupportGrants() })
  const end = useMutation({
    mutationFn: (id: string) => api.endSupportGrant(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['support-grants'] })
      void qc.invalidateQueries({ queryKey: ['me'] })
    },
  })
  const mine = new Set((me?.memberships ?? []).flatMap((m) => (m.support ? [m.support.grantId] : [])))
  const open = (tenantId: string) => {
    rememberTenant(tenantId)
    void qc.invalidateQueries()
    void navigate({ to: '/overview' })
  }
  return (
    <Card className="p-0">
      <div className="border-b border-border px-5 py-4">
        <h2 className="text-base font-semibold">Support access</h2>
        <p className="text-sm text-secondary">Every staff member’s grants, newest first.</p>
      </div>
      {grants.isLoading && <p className="px-5 py-4 text-sm text-secondary">Loading…</p>}
      {grants.error && (
        <div className="px-5 py-4">
          <Alert tone="error">{errorText(grants.error)}</Alert>
        </div>
      )}
      {grants.data?.length === 0 && <p className="px-5 py-4 text-sm text-secondary">No support access yet.</p>}
      {end.error && (
        <div className="px-5 pt-4">
          <Alert tone="error">{errorText(end.error)}</Alert>
        </div>
      )}
      <ul className="divide-y divide-border">
        {grants.data?.map((g) => (
          <li key={g.id} className="flex flex-wrap items-start justify-between gap-3 px-5 py-3">
            <div className="min-w-0 space-y-0.5">
              <p className="text-sm">
                <span className="font-medium">{g.tenantName}</span> · {STATUS[g.status]} · {g.staffName ?? g.staffEmail}
              </p>
              <p className="text-sm text-secondary">
                {g.justification}
                {g.ticketRef ? ` (${g.ticketRef})` : ''}
              </p>
              <p className="text-xs text-muted tabular-nums">
                {formatTime(g.createdAt)} to {formatTime(g.endedAt ?? g.expiresAt)} · {g.requests} requests
                {g.endedByEmail ? ` · ended by ${g.endedByEmail}` : ''}
              </p>
            </div>
            {g.status === 'active' && (
              <div className="flex gap-2">
                {mine.has(g.id) && (
                  <Button variant="secondary" onClick={() => open(g.tenantId)}>
                    View workspace
                  </Button>
                )}
                <Button variant="ghost" disabled={end.isPending} onClick={() => end.mutate(g.id)}>
                  End access
                </Button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </Card>
  )
}

/** /staff: hidden route for Cloudoptics staff (the router sends everyone else away). */
export function Staff() {
  const { data: me } = useMe()
  return (
    <div className="min-h-screen bg-surface-2">
      <header className="flex h-14 items-center justify-between gap-3 bg-brand-navy px-4 text-white">
        <div className="flex items-center gap-2 font-semibold">
          <Logo size={26} /> Cloudoptics staff
        </div>
        {me?.memberships.length ? (
          <Link to="/overview" className="text-sm underline">
            Back to the app
          </Link>
        ) : null}
      </header>
      <main className="mx-auto max-w-4xl space-y-6 px-4 py-6">
        <h1 className="text-xl font-semibold">Support access</h1>
        <RequestForm />
        <GrantList />
      </main>
    </div>
  )
}

/** Shown on every page while staff view a workspace under support access. */
export function SupportBanner({ tenantName, support }: { tenantName: string; support: SupportSession }) {
  const api = useApi()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const end = useMutation({
    mutationFn: () => api.endSupportGrant(support.grantId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['me'] })
      void qc.invalidateQueries({ queryKey: ['support-grants'] })
      void navigate({ to: '/staff' })
    },
  })
  return (
    <div className="mb-4">
      <Alert tone="warning">
        <span className="font-semibold">Support access:</span> you are viewing {tenantName} as Cloudoptics staff,
        read-only. It ends at {formatTime(support.expiresAt)}. Every request is
        recorded in the workspace’s audit trail.{' '}
        <button type="button" className="font-medium underline" disabled={end.isPending} onClick={() => end.mutate()}>
          End access now
        </button>
      </Alert>
    </div>
  )
}
