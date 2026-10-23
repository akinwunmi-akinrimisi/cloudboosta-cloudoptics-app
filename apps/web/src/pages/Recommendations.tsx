import { Link } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Check, Copy } from 'lucide-react'
import { useState } from 'react'

import { useApi } from '../app/context'
import type { Recommendation, RecommendationStatus, StatusChange } from '../api/types'
import { ApiError } from '../api/types'
import { Alert, Button, Card, cx } from '../components/ui'
import { Kpi, useMoney, useWorkspace } from './Workspace'

const RULES: Record<string, string> = {
  R1_unattached_ebs: 'Unattached volume',
  R1_old_snapshot: 'Old snapshot',
  R2_idle_eip: 'Idle Elastic IP',
  R2_idle_lb: 'Idle load balancer',
  R3_gp2_gp3: 'gp2 to gp3',
}

const FACTORS: Record<string, string> = {
  production: 'Tagged as production',
  environment_unknown: 'No environment tag',
  no_recent_snapshot: 'No snapshot in the last 30 days',
  data_loss: 'Deleting loses the data',
  recently_created: 'Created less than 30 days ago',
  large_volume: 'Larger than 1 TiB',
}

const NOTES: Record<string, string> = {
  detach_time_unknown: 'It was already unattached when we first scanned it, so we do not know for how long.',
  idle_since_unknown: 'It was already idle when we first scanned it, so we do not know for how long.',
  savings_upper_bound_incremental:
    'Snapshots are incremental, so the saving may be smaller if other snapshots share its data.',
  hourly_charge_only_lcu_excluded: 'Counts the hourly charge only; capacity-unit charges are not included.',
}

const REVERSIBILITY: Record<Recommendation['reversibility'], string> = {
  reversible: 'Reversible',
  with_snapshot: 'Reversible from the snapshot',
  irreversible: 'Cannot be undone',
}

const STATUS: Record<RecommendationStatus, string> = {
  new: 'New',
  assigned: 'Assigned',
  in_progress: 'In progress',
  done: 'Done, waiting for the next scan',
  verified: 'Verified',
  dismissed: 'Dismissed',
  snoozed: 'Snoozed',
  resolved_externally: 'Resolved outside Cloudoptics',
  expired: 'Expired',
}

const LEVEL_STYLE = {
  low: 'bg-good-soft text-good-ink',
  medium: 'bg-warning-soft text-warning-ink',
  high: 'bg-critical-soft text-critical-ink',
} as const

function Pill({ tone, children }: { tone: keyof typeof LEVEL_STYLE | 'neutral'; children: string }) {
  return (
    <span
      className={cx(
        'inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium',
        tone === 'neutral' ? 'bg-surface-2 text-secondary' : LEVEL_STYLE[tone],
      )}
    >
      {children}
    </span>
  )
}

function capitalise(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Recommendations, best first (PRD US-3.1): savings, risk and confidence at a glance. */
export function Recommendations() {
  const api = useApi()
  const { tenantId } = useWorkspace()
  const { money } = useMoney()
  const [rule, setRule] = useState<string>('all')
  const [showClosed, setShowClosed] = useState(false)
  const summary = useQuery({
    queryKey: ['recommendation-summary', tenantId],
    queryFn: () => api.recommendationSummary(tenantId as string),
    enabled: tenantId !== null,
  })
  const list = useQuery({
    queryKey: ['recommendations', tenantId, showClosed],
    queryFn: () =>
      api.listRecommendations(tenantId as string, {
        status: showClosed ? 'all' : 'open',
      }),
    enabled: tenantId !== null,
  })
  const recs = (list.data ?? []).filter((r) => rule === 'all' || r.ruleId === rule)
  const evaluated = summary.data?.lastEvaluatedAt

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Recommendations</h1>
        <p className="text-sm text-secondary">
          Savings ranked by impact, risk and confidence. Each one has the evidence, the commands to fix it and how to
          roll back.
        </p>
      </div>
      {summary.data && evaluated === null && (
        <Alert tone="info">
          Recommendations need resource access in your AWS accounts. The single Cloudoptics stack adds it; if{' '}
          <Link to="/settings/connections" className="font-medium text-accent underline">
            Connections
          </Link>{' '}
          offers to move your account to it, do that first. The first results appear within a few minutes of the next
          scan.
        </Alert>
      )}
      <div className="grid gap-4 sm:grid-cols-3">
        <Kpi
          label="Monthly savings identified"
          value={summary.data ? money(summary.data.savingsUsdMonth) : '—'}
          note="Open recommendations, alternatives not counted twice"
        />
        <Kpi
          label="Open recommendations"
          value={summary.data ? String(summary.data.openCount) : '—'}
          note={summary.data?.byRule.map((r) => `${r.count} ${RULES[r.ruleId] ?? r.ruleId}`).join(' · ') || ' '}
        />
        <Kpi
          label="Last analysed"
          value={
            evaluated
              ? new Date(evaluated).toLocaleDateString('en-GB', {
                  day: 'numeric',
                  month: 'short',
                })
              : '—'
          }
          note={
            evaluated
              ? new Date(evaluated).toLocaleTimeString('en-GB', {
                  timeStyle: 'short',
                })
              : 'Not yet'
          }
        />
      </div>
      <Card className="p-0">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3">
          <label className="flex items-center gap-2 text-sm">
            <span className="text-secondary">Type</span>
            <select
              className="h-8 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm"
              value={rule}
              onChange={(e) => setRule(e.target.value)}
            >
              <option value="all">All</option>
              {Object.entries(RULES).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-secondary">
            <input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} />
            Show done and dismissed
          </label>
        </div>
        {list.isLoading && <p className="px-5 py-4 text-sm text-secondary">Loading…</p>}
        {list.data && recs.length === 0 && (
          <p className="px-5 py-4 text-sm text-secondary">
            {evaluated ? 'Nothing to do here right now.' : 'No recommendations yet.'}
          </p>
        )}
        <ul className="divide-y divide-border">
          {recs.map((r) => (
            <li key={r.id}>
              <Link
                to="/recommendations/$recommendationId"
                params={{ recommendationId: r.id }}
                className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 hover:bg-surface-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{r.title}</p>
                  <p className="text-xs text-muted">
                    {RULES[r.ruleId] ?? r.ruleId} · {r.accountId} · {r.regionId}
                    {r.status !== 'new' && ` · ${STATUS[r.status]}`}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Pill tone={r.riskLevel}>{`${capitalise(r.riskLevel)} risk`}</Pill>
                  <Pill tone="neutral">{`${capitalise(r.confidence)} confidence`}</Pill>
                  <span className="w-28 text-right text-sm font-semibold tabular-nums">
                    {money(r.savingsUsdMonth)}/mo
                  </span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      aria-label="Copy command"
      className="shrink-0 rounded-sm p-1.5 text-secondary hover:bg-surface-2"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        })
      }}
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
    </button>
  )
}

function Commands({ steps }: { steps: string[] }) {
  return (
    <ol className="flex flex-col gap-2">
      {steps.map((step, i) => (
        <li key={step} className="flex items-start gap-2 rounded-sm bg-surface-2 px-3 py-2">
          <span className="pt-0.5 text-xs text-muted">{i + 1}</span>
          <code className="min-w-0 flex-1 break-all font-mono text-xs">{step}</code>
          <CopyButton text={step} />
        </li>
      ))}
    </ol>
  )
}

function Actions({ rec, tenantId }: { rec: Recommendation; tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const [dismissing, setDismissing] = useState(false)
  const [reason, setReason] = useState('')
  const change = useMutation({
    mutationFn: (body: StatusChange) => api.updateRecommendation(tenantId, rec.id, body),
    onSuccess: (updated) => {
      qc.setQueryData(['recommendation', tenantId, rec.id], updated)
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
      void qc.invalidateQueries({
        queryKey: ['recommendation-summary', tenantId],
      })
      setDismissing(false)
    },
  })
  const closed = !['new', 'assigned', 'in_progress'].includes(rec.status)
  return (
    <div className="flex flex-col gap-3">
      {change.error && (
        <Alert tone="error">{change.error instanceof ApiError ? change.error.message : 'Could not update.'}</Alert>
      )}
      {dismissing ? (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            change.mutate({ status: 'dismissed', reason })
          }}
        >
          <label className="text-sm font-medium" htmlFor="dismiss-reason">
            Why are you dismissing it?
          </label>
          <input
            id="dismiss-reason"
            className="h-9 rounded-sm border border-border-strong bg-surface-1 px-3 text-sm"
            value={reason}
            maxLength={500}
            required
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="flex gap-2">
            <Button type="submit" disabled={!reason.trim() || change.isPending}>
              Dismiss
            </Button>
            <Button variant="ghost" onClick={() => setDismissing(false)}>
              Cancel
            </Button>
          </div>
        </form>
      ) : (
        <div className="flex flex-wrap gap-2">
          {closed ? (
            <Button variant="secondary" disabled={change.isPending} onClick={() => change.mutate({ status: 'new' })}>
              Reopen
            </Button>
          ) : (
            <>
              <Button disabled={change.isPending} onClick={() => change.mutate({ status: 'done' })}>
                Mark as done
              </Button>
              {rec.status !== 'in_progress' && (
                <Button
                  variant="secondary"
                  disabled={change.isPending}
                  onClick={() => change.mutate({ status: 'in_progress' })}
                >
                  Start
                </Button>
              )}
              <Button variant="ghost" onClick={() => setDismissing(true)}>
                Dismiss…
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

/** One recommendation (PRD US-3.2): evidence, risk, the fix and how to roll it back. */
export function RecommendationDetail({ recommendationId }: { recommendationId: string }) {
  const api = useApi()
  const { me, tenantId } = useWorkspace()
  const { money } = useMoney()
  const rec = useQuery({
    queryKey: ['recommendation', tenantId, recommendationId],
    queryFn: () => api.getRecommendation(tenantId as string, recommendationId),
    enabled: tenantId !== null,
  })
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  const back = (
    <Link to="/recommendations" className="inline-flex items-center gap-1 text-sm text-secondary hover:text-primary">
      <ArrowLeft size={14} /> All recommendations
    </Link>
  )
  if (rec.isLoading) return <p className="text-sm text-secondary">Loading…</p>
  if (!rec.data || tenantId === null)
    return (
      <div className="flex flex-col gap-4">
        {back}
        <Alert tone="error">This recommendation was not found.</Alert>
      </div>
    )
  const r = rec.data
  return (
    <div className="flex flex-col gap-6">
      {back}
      <div>
        <h1 className="text-2xl font-semibold">{r.title}</h1>
        <p className="text-sm text-secondary">
          {RULES[r.ruleId] ?? r.ruleId} · account {r.accountId} · {r.regionId} · <code>{r.resourceId}</code> ·{' '}
          {STATUS[r.status]}
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <Kpi
          label="Monthly saving"
          value={money(r.savingsUsdMonth)}
          note={`${money(r.costNowUsdMonth)} now → ${money(r.costAfterUsdMonth)} · ${
            r.savingsBasis === 'effective_rate' ? 'at your own AWS rate' : 'at AWS list price'
          }`}
        />
        <Kpi
          label="Risk"
          value={`${capitalise(r.riskLevel)} (${r.riskScore})`}
          note={`${REVERSIBILITY[r.reversibility]}${r.restartRequired ? ' · needs a restart' : ''}`}
        />
        <Kpi label="Confidence" value={capitalise(r.confidence)} note={`Effort ${r.effort}`} />
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="flex flex-col gap-5 p-5 lg:col-span-2">
          <div>
            <h2 className="mb-2 text-base font-semibold">How to fix it</h2>
            <Commands steps={r.fix.steps} />
            {r.fix.notes && <p className="mt-2 text-xs text-muted">{r.fix.notes}</p>}
          </div>
          {r.fix.rollback.length > 0 && (
            <div>
              <h3 className="mb-2 text-sm font-semibold">Roll back</h3>
              <Commands steps={r.fix.rollback} />
            </div>
          )}
          {r.fix.alternative && (
            <div>
              <h3 className="text-sm font-semibold">{r.fix.alternative.title}</h3>
              <p className="mb-2 text-xs text-muted">Costs about {money(r.fix.alternative.monthlyCostUsd)} a month.</p>
              <Commands steps={r.fix.alternative.steps} />
            </div>
          )}
          {role && role !== 'viewer' && <Actions rec={r} tenantId={tenantId} />}
        </Card>
        <Card className="flex flex-col gap-4 p-5">
          <div>
            <h2 className="mb-2 text-base font-semibold">Why we suggest it</h2>
            <ul className="flex flex-col gap-1 text-sm">
              {r.evidence.map((e) => (
                <li key={e.kind} className="flex justify-between gap-3">
                  <span className="text-secondary">{e.kind}</span>
                  <span className="truncate tabular-nums">
                    {e.value === null || e.value === undefined ? '—' : String(e.value)}
                    {typeof e.sinceDays === 'number' && ` for ${Math.round(e.sinceDays)} days`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
          {r.riskFactors.length > 0 && (
            <div>
              <h3 className="mb-1 text-sm font-semibold">Risk factors</h3>
              <ul className="flex flex-col gap-1 text-sm">
                {r.riskFactors.map((f) => (
                  <li key={f.factor} className="flex justify-between gap-3">
                    <span>{FACTORS[f.factor] ?? f.factor}</span>
                    <span className="text-muted tabular-nums">+{f.points}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {r.confidenceNotes.length > 0 && (
            <ul className="flex flex-col gap-1 text-xs text-muted">
              {r.confidenceNotes.map((n) => (
                <li key={n}>{NOTES[n] ?? n}</li>
              ))}
            </ul>
          )}
          <p className="text-xs text-muted">
            First found {new Date(r.firstDetectedAt).toLocaleDateString('en-GB')} · checked{' '}
            {new Date(r.lastEvaluatedAt).toLocaleString('en-GB')}
          </p>
        </Card>
      </div>
    </div>
  )
}
