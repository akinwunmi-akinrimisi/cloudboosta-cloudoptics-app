import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { useApi } from '../app/context'
import type { AuditEntry, RuleSettings, RuleSettingsView, Tenant } from '../api/types'
import { ApiError } from '../api/types'
import { Alert, Button, Card } from '../components/ui'
import { RULES } from '../lib/rules'
import { useWorkspace } from './Workspace'

// The currencies the API accepts for a workspace (identity SUPPORTED_CURRENCIES).
const CURRENCIES: Record<string, string> = {
  NGN: 'Nigerian naira',
  KES: 'Kenyan shilling',
  GHS: 'Ghanaian cedi',
  ZAR: 'South African rand',
  EGP: 'Egyptian pound',
  RWF: 'Rwandan franc',
  UGX: 'Ugandan shilling',
  TZS: 'Tanzanian shilling',
  GBP: 'British pound',
  EUR: 'Euro',
  USD: 'US dollar',
}

function timeZones(current: string): string[] {
  const all = typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : []
  return all.includes(current) ? all : [current, ...all]
}

const selectClass = 'h-9 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm'

/** Currency and time zone for the whole workspace (PRD Epic E3). Admins only. */
function WorkspaceSettings({ tenant }: { tenant: Tenant }) {
  const api = useApi()
  const qc = useQueryClient()
  const [saved, setSaved] = useState(false)
  const save = useMutation({
    mutationFn: (change: { displayCurrency?: string; timeZone?: string }) => api.updateTenant(tenant.id, change),
    onSuccess: (updated) => {
      qc.setQueryData(['tenant', tenant.id], updated)
      void qc.invalidateQueries({ queryKey: ['fx-current', tenant.id] })
      setSaved(true)
    },
  })
  return (
    <Card className="flex flex-col gap-4">
      <div>
        <h2 className="text-base font-semibold">Workspace</h2>
        <p className="mt-1 text-sm text-secondary">
          Costs are shown in US dollars and in this currency. Dates and the daily cut-off use this time zone.
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Local currency</span>
          <select
            className={selectClass}
            value={tenant.displayCurrency}
            disabled={save.isPending}
            onChange={(e) => save.mutate({ displayCurrency: e.target.value })}
          >
            {Object.entries(CURRENCIES).map(([code, name]) => (
              <option key={code} value={code}>
                {name} ({code})
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Time zone</span>
          <select
            className={selectClass}
            value={tenant.timeZone}
            disabled={save.isPending}
            onChange={(e) => save.mutate({ timeZone: e.target.value })}
          >
            {timeZones(tenant.timeZone).map((tz) => (
              <option key={tz} value={tz}>
                {tz.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </label>
      </div>
      {save.error && <Alert tone="error">Could not save. Try again.</Alert>}
      {saved && !save.isPending && !save.error && <p className="text-xs text-good-ink">Saved.</p>}
    </Card>
  )
}

/** Which rules run and how strict they are (PRD FR-REC-08). Saving re-runs them. */
function RuleSettingsCard({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const api = useApi()
  const view = useQuery({ queryKey: ['rule-settings', tenantId], queryFn: () => api.getRuleSettings(tenantId) })
  if (!view.data) return null
  // Keyed so the form starts again from what was saved.
  return (
    <RuleSettingsForm
      key={JSON.stringify(view.data.settings)}
      tenantId={tenantId}
      canEdit={canEdit}
      view={view.data}
    />
  )
}

function RuleSettingsForm({ tenantId, canEdit, view }: { tenantId: string; canEdit: boolean; view: RuleSettingsView }) {
  const api = useApi()
  const qc = useQueryClient()
  const [draft, setDraft] = useState<RuleSettings>(view.settings)
  const save = useMutation({
    mutationFn: (settings: RuleSettings) => api.saveRuleSettings(tenantId, settings),
    onSuccess: (updated) => {
      qc.setQueryData(['rule-settings', tenantId], updated)
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
      void qc.invalidateQueries({ queryKey: ['recommendation-summary', tenantId] })
    },
  })
  const toggle = (rule: string, on: boolean) =>
    setDraft({
      ...draft,
      disabledRules: on ? draft.disabledRules.filter((r) => r !== rule) : [...draft.disabledRules, rule],
    })
  const changed = JSON.stringify(draft) !== JSON.stringify(view.settings)
  return (
    <Card className="flex flex-col gap-4">
      <div>
        <h2 className="text-base font-semibold">Recommendation rules</h2>
        <p className="mt-1 text-sm text-secondary">
          Choose what Cloudoptics looks for. Changes apply straight away to every AWS account in this workspace.
        </p>
      </div>
      <fieldset className="grid gap-2 sm:grid-cols-2" disabled={!canEdit}>
        <legend className="mb-1 text-sm font-medium">Look for</legend>
        {view.rules.map((rule) => (
          <label key={rule} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={!draft.disabledRules.includes(rule)}
              onChange={(e) => toggle(rule, e.target.checked)}
            />
            {RULES[rule] ?? rule}
          </label>
        ))}
      </fieldset>
      <fieldset className="grid gap-4 sm:grid-cols-3" disabled={!canEdit}>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Idle when CPU stays under</span>
          <select
            className={selectClass}
            value={draft.idleCpuPercent}
            onChange={(e) => setDraft({ ...draft, idleCpuPercent: Number(e.target.value) })}
          >
            {[1, 2, 3, 5, 10, 15, 20].map((n) => (
              <option key={n} value={n}>
                {n}%
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">For at least</span>
          <select
            className={selectClass}
            value={draft.idleDays}
            onChange={(e) => setDraft({ ...draft, idleDays: Number(e.target.value) })}
          >
            {[7, 14, 21, 30].map((n) => (
              <option key={n} value={n}>
                {n} days
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Skip resources tagged</span>
          <input
            className="h-9 rounded-sm border border-border-strong bg-surface-1 px-3 text-sm"
            value={draft.ignoreTag}
            maxLength={128}
            onChange={(e) => setDraft({ ...draft, ignoreTag: e.target.value })}
          />
        </label>
      </fieldset>
      <p className="text-xs text-muted">
        Add the tag above to any AWS resource (with any value) and Cloudoptics will never suggest changing it.
      </p>
      {save.error && (
        <Alert tone="error">{save.error instanceof ApiError ? save.error.message : 'Could not save. Try again.'}</Alert>
      )}
      {canEdit ? (
        <div className="flex items-center gap-3">
          <Button disabled={!changed || !draft.ignoreTag.trim() || save.isPending} onClick={() => save.mutate(draft)}>
            {save.isPending ? 'Saving and re-checking…' : 'Save'}
          </Button>
          {changed && (
            <Button variant="ghost" onClick={() => setDraft(view.settings)}>
              Undo changes
            </Button>
          )}
        </div>
      ) : (
        <p className="text-xs text-muted">Only workspace admins can change these.</p>
      )}
    </Card>
  )
}

const ACTIONS: Record<string, string> = {
  'tenant.created': 'Created the workspace',
  'tenant.updated': 'Changed workspace settings',
  'connection.created': 'Connected an AWS account',
  'connection.disconnected': 'Disconnected an AWS account',
  'connection.status_changed': 'Connection status changed',
  'recommendation.status_changed': 'Recommendation status changed',
  'recommendation.assigned': 'Recommendation assigned',
  'rule_settings.updated': 'Changed recommendation rules',
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
  const { me, tenantId, tenant } = useWorkspace()
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="text-sm text-secondary">Your workspace, what Cloudoptics looks for, and who changed what.</p>
      </div>
      {role === 'admin' && tenant.data && <WorkspaceSettings tenant={tenant.data} />}
      {tenantId !== null && role !== undefined && <RuleSettingsCard tenantId={tenantId} canEdit={role === 'admin'} />}
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
