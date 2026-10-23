import { useInfiniteQuery } from '@tanstack/react-query'

import { useApi } from '../app/context'
import type { AuditEntry } from '../api/types'
import { Alert, Button, Card } from '../components/ui'
import { useWorkspace } from './Workspace'

const ACTIONS: Record<string, string> = {
  'tenant.created': 'Created the workspace',
  'tenant.updated': 'Changed workspace settings',
  'connection.created': 'Connected an AWS account',
  'connection.disconnected': 'Disconnected an AWS account',
  'connection.status_changed': 'Connection status changed',
  'recommendation.status_changed': 'Recommendation status changed',
  'fx_rate.set': 'Exchange rate set',
}

function who(entry: AuditEntry): string {
  if (entry.actor.type === 'system') return 'Cloudoptics (automatic)'
  if (entry.actor.type === 'staff') return 'Cloudoptics staff'
  return entry.actor.name ?? entry.actor.email ?? 'A former member'
}

function change(entry: AuditEntry): string {
  const after = entry.after ?? {}
  const before = entry.before ?? {}
  if (typeof after.status === 'string') {
    const from = typeof before.status === 'string' ? `${before.status} → ` : ''
    const reason = typeof after.reason === 'string' && after.reason ? ` (${after.reason})` : ''
    return `${from}${after.status}${reason}`
  }
  if (typeof after.awsAccountId === 'string') return `AWS account ${after.awsAccountId}`
  const keys = Object.keys(after)
  return keys.length ? keys.map((k) => `${k}: ${String(after[k])}`).join(', ') : ''
}

/** The workspace audit trail (PRD FR-SEC-05): who did what, and when. Admins only. */
function AuditLog({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const log = useInfiniteQuery({
    queryKey: ['audit-log', tenantId],
    queryFn: ({ pageParam }) => api.auditLog(tenantId, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.nextBefore ?? undefined,
  })
  const entries = log.data?.pages.flatMap((p) => p.entries) ?? []
  const verified = log.data?.pages[0]?.verified
  return (
    <Card className="p-0">
      <div className="border-b border-border px-5 py-4">
        <h2 className="text-base font-semibold">Audit log</h2>
        <p className="text-sm text-secondary">
          Every change to this workspace, newest first. Entries cannot be edited or removed.
        </p>
      </div>
      {verified === false && (
        <div className="px-5 pt-4">
          <Alert tone="error">The audit log failed its integrity check. Contact Cloudoptics support.</Alert>
        </div>
      )}
      {log.isLoading && <p className="px-5 py-4 text-sm text-secondary">Loading…</p>}
      {log.error && <p className="px-5 py-4 text-sm text-secondary">Only workspace admins can see the audit log.</p>}
      <ul className="divide-y divide-border">
        {entries.map((e) => (
          <li key={e.id} className="flex flex-wrap items-baseline justify-between gap-2 px-5 py-3">
            <div className="min-w-0">
              <p className="text-sm">
                <span className="font-medium">{who(e)}</span> · {ACTIONS[e.action] ?? e.action}
              </p>
              <p className="truncate text-xs text-muted">{change(e)}</p>
            </div>
            <time className="shrink-0 text-xs text-muted tabular-nums" dateTime={e.occurredAt}>
              {new Date(e.occurredAt).toLocaleString('en-GB')}
            </time>
          </li>
        ))}
      </ul>
      {log.hasNextPage && (
        <div className="border-t border-border px-5 py-3">
          <Button variant="secondary" disabled={log.isFetchingNextPage} onClick={() => void log.fetchNextPage()}>
            Load older entries
          </Button>
        </div>
      )}
    </Card>
  )
}

export function Settings() {
  const { me, tenantId } = useWorkspace()
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="text-sm text-secondary">Members, notifications and preferences arrive in Sprints 5–6.</p>
      </div>
      <Card>
        <h2 className="text-base font-semibold">Email alerts</h2>
        <p className="mt-1 text-sm text-secondary">
          Workspace admins get an email when a connected AWS account stops working. Connections are checked every 6
          hours.
        </p>
      </Card>
      {tenantId !== null && role === 'admin' && <AuditLog tenantId={tenantId} />}
    </div>
  )
}
