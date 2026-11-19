import { Link } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ExternalLink, ShieldCheck } from 'lucide-react'
import { useRef, useState } from 'react'

import { useApi } from '../app/context'
import type {
  Member,
  Recommendation,
  RecommendationChange,
  RecommendationEvent,
  RecommendationStatus,
  ScheduleFix,
  RecommendationSummary,
} from '../api/types'
import { ApiError } from '../api/types'
import { UsageChart } from '../components/charts'
import { Commands, CopyButton } from '../components/commands'
import { ExportButton } from '../components/exports'
import { NatRoutes } from '../components/natRoutes'
import { useEscapeToClose } from '../components/overlay'
import { Alert, Button, Card, cx } from '../components/ui'
import { RULES } from '../lib/rules'
import { Kpi, useMoney, useWorkspace } from './Workspace'

const FACTORS: Record<string, string> = {
  production: 'Tagged as production',
  environment_unknown: 'No environment tag',
  no_recent_snapshot: 'No snapshot in the last 30 days',
  data_loss: 'Deleting loses the data',
  recently_created: 'Created less than 30 days ago',
  recently_detached: 'Detached less than 7 days ago',
  large_volume: 'Larger than 1 TiB',
  stops_running_server: 'Stops a running server',
  deletes_database: 'Removes a database (a final snapshot is kept)',
  restart_needed: 'The server restarts',
  architecture_change: 'Needs software built for Arm',
  less_headroom: 'Less spare capacity after the change',
  stops_out_of_hours: 'Stopped outside office hours, so not there for anyone working late',
  environment_from_name: 'Judged non-production from its name only, not a tag',
  database_stops_daily: 'The database stops every evening',
  version_upgrade: 'A version upgrade, so test it on a copy first',
  no_downgrade: 'A Kubernetes upgrade cannot be rolled back in place',
  routes_unchecked: 'Route tables may still send traffic to it',
  routes_point_here: 'Route tables still send traffic to it',
  one_year_commitment: 'Commits to paying for a year',
  paid_in_dollars: 'Paid in US dollars, so it costs more if your currency weakens',
  usage_may_fall: 'Little room if usage falls',
}

const NOTES: Record<string, string> = {
  detach_time_unknown: 'It was already unattached when we first scanned it, so we do not know for how long.',
  idle_since_unknown: 'It was already idle when we first scanned it, so we do not know for how long.',
  savings_upper_bound_incremental:
    'Snapshots are incremental, so the saving may be smaller if other snapshots share its data.',
  hourly_charge_only_lcu_excluded: 'Counts the hourly charge only; capacity-unit charges are not included.',
  stop_saves_compute_only:
    'Stopping saves the server charge only. The disks are counted too, because they are removed in the last step.',
  priced_as_linux: 'Priced as a Linux server, so the real saving is likely higher (licences are not counted).',
  list_price_us_east_1: 'Estimated from US East prices; prices in this region may be a little higher.',
  storage_not_included: 'Counts the database server only; its storage costs extra.',
  check_type_in_region: 'Check that the new type is offered in this region before you change it.',
  needs_nitro_drivers: 'Newer types need the ENA and NVMe drivers; most current images already have them.',
  t3_unlimited_by_default:
    't3 servers can burst without limit by default, which can cost extra; set credits to "standard" to match t2.',
  no_memory_data:
    'AWS does not report memory use unless the CloudWatch agent is installed, so check memory before you resize.',
  short_history: 'Based on less than a month of usage; month-end peaks may not be included yet.',
  compute_optimizer_agrees: 'AWS Compute Optimizer also says this server is bigger than it needs to be.',
  arm64_build_needed: 'Your software needs an Arm version, so plan time to build and test it.',
  schedule_saves_compute_only:
    'Saves the server charge for the hours it is off; its disks and any Elastic IP are still charged.',
  rds_restarts_after_7_days:
    'AWS starts a stopped database again after 7 days; a weekday schedule never stops it that long.',
  short_billing_history: 'Based on less than 30 days of bills, so the monthly figure may change.',
  route_tables_not_checked:
    'Found from the bill alone: we cannot see your route tables, so check them before you delete it.',
  route_tables_need_access_update:
    'We could not read your route tables: update CloudOptics access to 2.0.4 (Connections) so we can check them. Until then, check them yourself before you delete it.',
  no_routes_to_it: 'No route tables send traffic to it.',
  routes_still_point_here: 'Route tables still send traffic to it: change those routes before you delete it.',
  hourly_from_daily_cur:
    'Sized from daily bills (each day as its average hour), at the cautious end of the range, so quiet hours stay covered.',
  advisory_only: 'Advisory only: Cloudoptics never buys anything for you.',
  other_savings_netted: 'Savings from other open recommendations on servers are taken off first, so nothing is counted twice.',
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
  done: 'Done, waiting for verification',
  verified: 'Verified',
  not_verified: 'Not verified',
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

type Assigned = 'anyone' | 'me' | 'nobody' | string

function memberName(m: Member): string {
  return m.displayName ?? m.email
}

/** Who can be assigned: the workspace's members and admins (viewers only read). */
function useAssignees(tenantId: string | null) {
  const api = useApi()
  const members = useQuery({
    queryKey: ['members', tenantId],
    queryFn: () => api.listMembers(tenantId as string),
    enabled: tenantId !== null,
  })
  const all = members.data ?? []
  return { all, assignable: all.filter((m) => m.role !== 'viewer') }
}

/** Recommendations, best first (PRD US-3.1): savings, risk and confidence at a glance. */
export function Recommendations() {
  const api = useApi()
  const qc = useQueryClient()
  const { me, tenantId } = useWorkspace()
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  const canChange = role !== undefined && role !== 'viewer'
  const { money } = useMoney()
  const { all: members, assignable } = useAssignees(tenantId)
  const [rule, setRule] = useState<string>('all')
  const [risk, setRisk] = useState<'all' | Recommendation['riskLevel']>('all')
  const [assigned, setAssigned] = useState<Assigned>('anyone')
  const [showClosed, setShowClosed] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [dismissing, setDismissing] = useState(false)
  const [reason, setReason] = useState('')
  const dismissButton = useRef<HTMLButtonElement>(null)
  const dismissForm = useRef<HTMLFormElement>(null)
  useEscapeToClose(dismissing, () => setDismissing(false), dismissButton, dismissForm)
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
  const bulk = useMutation({
    mutationFn: (change: RecommendationChange) => api.updateRecommendations(tenantId as string, [...selected], change),
    onSuccess: () => {
      setSelected(new Set())
      setDismissing(false)
      setReason('')
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
      void qc.invalidateQueries({ queryKey: ['recommendation-summary', tenantId] })
    },
  })
  const recs = (list.data ?? []).filter(
    (r) =>
      (rule === 'all' || r.ruleId === rule) &&
      (risk === 'all' || r.riskLevel === risk) &&
      (assigned === 'anyone' ||
        (assigned === 'me' && r.assigneeId === me?.id) ||
        (assigned === 'nobody' && r.assigneeId === null) ||
        r.assigneeId === assigned),
  )
  const evaluated = summary.data?.lastEvaluatedAt
  const names = new Map(members.map((m) => [m.userId, memberName(m)]))
  const shown = recs.filter((r) => selected.has(r.id))
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const select = 'h-8 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm'

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
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
        <Kpi {...realisedTile(summary.data, money)} />
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
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm">
              <span className="text-secondary">Type</span>
              <select className={select} value={rule} onChange={(e) => setRule(e.target.value)}>
                <option value="all">All</option>
                {Object.entries(RULES).map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-sm">
              <span className="text-secondary">Risk</span>
              <select
                className={select}
                value={risk}
                onChange={(e) => setRisk(e.target.value as 'all' | Recommendation['riskLevel'])}
              >
                <option value="all">Any</option>
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
              </select>
            </label>
            <label className="flex items-center gap-2 text-sm">
              <span className="text-secondary">Assigned to</span>
              <select className={select} value={assigned} onChange={(e) => setAssigned(e.target.value)}>
                <option value="anyone">Anyone</option>
                <option value="me">Me</option>
                <option value="nobody">Nobody yet</option>
                {members
                  .filter((m) => m.userId !== me?.id)
                  .map((m) => (
                    <option key={m.userId} value={m.userId}>
                      {memberName(m)}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          <div className="flex flex-wrap items-start gap-3">
            <label className="flex h-8 items-center gap-2 text-sm text-secondary">
              <input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} />
              Show done and dismissed
            </label>
            {tenantId !== null && (
              <ExportButton
                tenantId={tenantId}
                request={{
                  kind: 'recommendations',
                  status: showClosed ? 'all' : 'open',
                  ruleId: rule === 'all' ? null : rule,
                  risk: risk === 'all' ? null : risk,
                }}
              />
            )}
          </div>
        </div>
        {canChange && shown.length > 0 && (
          <div className="flex flex-col gap-2 border-b border-border bg-surface-2 px-5 py-3">
            {bulk.error && (
              <Alert tone="error">{bulk.error instanceof ApiError ? bulk.error.message : 'Could not update.'}</Alert>
            )}
            {dismissing ? (
              <form
                ref={dismissForm}
                className="flex flex-wrap items-center gap-2"
                onSubmit={(e) => {
                  e.preventDefault()
                  bulk.mutate({ status: 'dismissed', reason })
                }}
              >
                <label className="text-sm font-medium" htmlFor="bulk-reason">
                  Why are you dismissing {shown.length === 1 ? 'it' : 'them'}?
                </label>
                <input
                  id="bulk-reason"
                  className="h-8 min-w-48 flex-1 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm"
                  value={reason}
                  maxLength={500}
                  required
                  onChange={(e) => setReason(e.target.value)}
                />
                <Button type="submit" disabled={!reason.trim() || bulk.isPending}>
                  Dismiss
                </Button>
                <Button variant="ghost" onClick={() => setDismissing(false)}>
                  Cancel
                </Button>
              </form>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{shown.length} selected</span>
                <Button variant="secondary" disabled={bulk.isPending} onClick={() => bulk.mutate({ status: 'in_progress' })}>
                  Start
                </Button>
                <Button variant="secondary" disabled={bulk.isPending} onClick={() => bulk.mutate({ status: 'done' })}>
                  Mark as done
                </Button>
                <Button ref={dismissButton} variant="ghost" onClick={() => setDismissing(true)}>
                  Dismiss…
                </Button>
                <select
                  aria-label="Assign selected to"
                  className={select}
                  value=""
                  disabled={bulk.isPending}
                  onChange={(e) => bulk.mutate({ assigneeId: e.target.value === 'nobody' ? null : e.target.value })}
                >
                  <option value="" disabled>
                    Assign to…
                  </option>
                  {assignable.map((m) => (
                    <option key={m.userId} value={m.userId}>
                      {m.userId === me?.id ? 'Me' : memberName(m)}
                    </option>
                  ))}
                  <option value="nobody">Nobody</option>
                </select>
                <Button variant="ghost" onClick={() => setSelected(new Set())}>
                  Clear
                </Button>
              </div>
            )}
          </div>
        )}
        {list.isLoading && <p className="px-5 py-4 text-sm text-secondary">Loading…</p>}
        {list.data && recs.length === 0 && (
          <p className="px-5 py-4 text-sm text-secondary">
            {evaluated ? 'Nothing to do here right now.' : 'No recommendations yet.'}
          </p>
        )}
        <ul className="divide-y divide-border">
          {recs.map((r) => (
            <li key={r.id} className="flex items-center hover:bg-surface-2">
              {canChange && (
                // 24 px: WCAG 2.2 target size (2.5.8); the row's link starts right beside it.
                <input
                  type="checkbox"
                  className="ml-4 size-6 shrink-0 cursor-pointer accent-accent"
                  aria-label={`Select ${r.title}`}
                  checked={selected.has(r.id)}
                  onChange={() => toggle(r.id)}
                />
              )}
              <Link
                to="/recommendations/$recommendationId"
                params={{ recommendationId: r.id }}
                className="flex min-w-0 flex-1 flex-wrap items-center justify-between gap-3 px-5 py-3"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{r.title}</p>
                  <p className="text-xs text-muted">
                    {RULES[r.ruleId] ?? r.ruleId} · {r.accountId} · {r.regionId}
                    {r.status !== 'new' && r.status !== 'verified' && ` · ${STATUS[r.status]}`}
                    {r.assigneeId && ` · ${r.assigneeId === me?.id ? 'You' : (names.get(r.assigneeId) ?? 'A teammate')}`}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
                  {r.status === 'verified' && <VerifiedBadge />}
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

/** Realised savings (PRD US-5.2): measured from the bills, or estimated until they are. */
function realisedTile(
  summary: RecommendationSummary | undefined,
  money: (usd: number) => string,
): { label: string; value: string; note: string } {
  const label = 'Realised savings'
  if (!summary) return { label, value: '—', note: ' ' }
  const measured = summary.realisedSavingsUsdMonth ?? 0
  const estimated = summary.estimatedRealisedUsdMonth ?? 0
  const verified = summary.verifiedCount ?? 0
  const waiting = summary.awaitingVerificationCount ?? 0
  const notVerified = summary.notVerifiedCount ?? 0
  const parts = [
    verified > 0 ? `${verified} verified` : 'None verified yet',
    estimated > 0 ? `${money(estimated)} estimated` : '',
    waiting > 0 ? `${waiting} waiting for a scan` : '',
    notVerified > 0 ? `${notVerified} not verified` : '',
  ].filter(Boolean)
  return { label, value: `${money(measured + estimated)}/mo`, note: parts.join(' · ') }
}

function VerifiedBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-good-soft px-2.5 py-0.5 text-xs font-medium text-good-ink">
      <ShieldCheck size={12} aria-hidden /> Verified
    </span>
  )
}

const CHECKS: Record<string, string> = {
  volume_deleted: 'The volume is deleted.',
  snapshot_deleted: 'The snapshot is deleted.',
  snapshot_archived: 'The snapshot is in the archive tier.',
  address_released: 'The address is released.',
  load_balancer_deleted: 'The load balancer is deleted.',
  volume_type_gp3: 'The volume is gp3 now.',
  instance_stopped: 'The server is stopped.',
  instance_terminated: 'The server is terminated.',
  database_stopped: 'The database is stopped.',
  database_deleted: 'The database is deleted.',
  instance_type_changed: 'The server has the recommended type now.',
  resource_gone: 'The resource no longer exists.',
  rule_no_longer_applies: 'The rule no longer finds anything to change.',
  unchanged: 'The latest scan shows no change yet.',
}

function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
}

function realisedNote(rec: Recommendation, money: (usd: number) => string): { label: string; value: string; note: string } {
  const v = rec.verification
  const m = v?.measurement
  const realised = rec.realisedSavingsUsdMonth
  if (realised !== null && realised !== undefined) {
    return {
      label: 'Realised savings',
      value: `${money(realised)}/mo`,
      note:
        rec.realisedBasis === 'cur' && m
          ? `From your bills: ${money(m.beforeUsdDay ?? 0)} a day over ${m.beforeDays} days before the change, ${money(m.afterUsdDay ?? 0)} a day over ${m.afterDays} days after, normalised to 30 days.`
          : 'Estimated: your bills do not show this resource on its own, so the estimate is counted.',
    }
  }
  if (rec.status === 'verified') {
    return {
      label: 'Estimated realised savings',
      value: `${money(v?.estimateUsdMonth ?? rec.savingsUsdMonth)}/mo`,
      note: 'Measured from your bills once they cover 7 days after the change.',
    }
  }
  return {
    label: 'Realised savings',
    value: '—',
    note: m
      ? 'Your bills do not show a cost drop for this resource, so no saving is counted.'
      : 'Counted only if your bills show the cost drop, once they cover 7 days after the change.',
  }
}

/** After the fix (PRD US-5.1/5.2): what the scans saw and the saving it made. */
function Outcome({ rec, money }: { rec: Recommendation; money: (usd: number) => string }) {
  const v = rec.verification
  const seen = v ? (CHECKS[v.check] ?? v.check) : null
  if (rec.status === 'done') {
    return (
      <Alert tone="info">
        Marked done{rec.doneAt ? ` on ${shortDate(rec.doneAt)}` : ''}. Cloudoptics checks the resource after each scan
        and marks it verified when it sees the change, within 72 hours.
        {seen && v?.outcome === 'not_implemented' ? ` ${seen}` : ''}
      </Alert>
    )
  }
  if (rec.status === 'not_verified') {
    return (
      <Alert tone="warning">
        Marked done, but 72 hours later the scans still do not show the change. Check it was made in AWS account{' '}
        {rec.accountId}; it is verified as soon as a scan sees it.
      </Alert>
    )
  }
  if (rec.status !== 'verified' && rec.status !== 'resolved_externally') return null
  const tile = realisedNote(rec, money)
  return (
    <Card className="flex flex-col gap-3 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">
          {rec.status === 'verified' ? 'Change verified' : 'Resolved outside Cloudoptics'}
        </h2>
        {rec.status === 'verified' && <VerifiedBadge />}
      </div>
      <p className="text-sm text-secondary">
        {seen}
        {rec.verifiedAt && ` Verified ${new Date(rec.verifiedAt).toLocaleString('en-GB')}.`}
        {rec.changedAt && ` Changed around ${shortDate(rec.changedAt)}.`}
      </p>
      <div>
        <p className="text-sm text-secondary">{tile.label}</p>
        <p className="mt-1 text-2xl font-semibold tabular-nums">{tile.value}</p>
        <p className="mt-1 text-xs text-muted">{tile.note}</p>
      </div>
    </Card>
  )
}

const DAY_NAMES: Record<string, string> = { 'MO-FR': 'Monday to Friday' }

/** R7's fix: one tag to copy, and the scheduler stack (deployed by the customer) that acts on it. */
function ScheduleSteps({ schedule: s }: { schedule: ScheduleFix }) {
  return (
    <div className="flex flex-col gap-3 rounded-sm border border-border p-4">
      <div>
        <h3 className="text-sm font-semibold">
          Run it {s.start} to {s.end}, {DAY_NAMES[s.days] ?? s.days} ({s.timeZone})
        </h3>
        <p className="mt-1 text-xs text-muted">
          Add one tag. A scheduler stack you deploy in your own AWS account stops it at {s.end} and starts it at{' '}
          {s.start}; Cloudoptics itself never changes your resources.
        </p>
      </div>
      <dl className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2 text-sm">
        <dt className="text-secondary">Tag key</dt>
        <dd className="flex min-w-0 items-center gap-1">
          <code className="break-all rounded-sm bg-surface-2 px-2 py-1 font-mono text-xs">{s.tagKey}</code>
          <CopyButton text={s.tagKey} label="Copy tag key" />
        </dd>
        <dt className="text-secondary">Tag value</dt>
        <dd className="flex min-w-0 items-center gap-1">
          <code className="break-all rounded-sm bg-surface-2 px-2 py-1 font-mono text-xs">{s.tagValue}</code>
          <CopyButton text={s.tagValue} label="Copy tag value" />
        </dd>
      </dl>
      <div className="flex flex-wrap items-center gap-3">
        {s.quickCreateUrl && (
          <a
            href={s.quickCreateUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex h-9 items-center gap-1.5 rounded-sm bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover"
          >
            Create the scheduler stack <ExternalLink size={14} />
          </a>
        )}
        <a
          href={s.templateUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-sm font-medium text-accent hover:underline"
        >
          Download the scheduler template <ExternalLink size={14} />
        </a>
      </div>
      <p className="text-xs text-muted">
        Once per AWS account and region: in CloudFormation choose Create stack, then Upload a template file, and name
        it {s.stackName}. Every server and database with the tag in that region then follows its schedule.
      </p>
      {s.rdsAutoStart && (
        <Alert tone="warning">
          AWS starts a stopped database again by itself after 7 days. A weekday schedule never keeps it stopped that
          long, but if you stop it by hand for longer, AWS starts it and the scheduler stops it again at the end of the
          next working day.
        </Alert>
      )}
    </div>
  )
}

function Actions({ rec, tenantId }: { rec: Recommendation; tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const [dismissing, setDismissing] = useState(false)
  const [reason, setReason] = useState('')
  const dismissButton = useRef<HTMLButtonElement>(null)
  const dismissForm = useRef<HTMLFormElement>(null)
  useEscapeToClose(dismissing, () => setDismissing(false), dismissButton, dismissForm)
  const change = useMutation({
    mutationFn: (body: RecommendationChange) => api.updateRecommendation(tenantId, rec.id, body),
    onSuccess: (updated) => {
      qc.setQueryData(['recommendation', tenantId, rec.id], updated)
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
      void qc.invalidateQueries({
        queryKey: ['recommendation-summary', tenantId],
      })
      setDismissing(false)
      void qc.invalidateQueries({ queryKey: ['recommendation-events', tenantId, rec.id] })
    },
  })
  const { me } = useWorkspace()
  const { assignable } = useAssignees(tenantId)
  const closed = !['new', 'assigned', 'in_progress'].includes(rec.status)
  return (
    <div className="flex flex-col gap-3">
      <label className="flex items-center gap-2 text-sm">
        <span className="font-medium">Assigned to</span>
        <select
          className="h-8 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm"
          value={rec.assigneeId ?? ''}
          disabled={change.isPending}
          onChange={(e) => change.mutate({ assigneeId: e.target.value || null })}
        >
          <option value="">Nobody yet</option>
          {assignable.map((m) => (
            <option key={m.userId} value={m.userId}>
              {m.userId === me?.id ? `Me (${memberName(m)})` : memberName(m)}
            </option>
          ))}
        </select>
      </label>
      {change.error && (
        <Alert tone="error">{change.error instanceof ApiError ? change.error.message : 'Could not update.'}</Alert>
      )}
      {dismissing ? (
        <form
          ref={dismissForm}
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
              <Button ref={dismissButton} variant="ghost" onClick={() => setDismissing(true)}>
                Dismiss…
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

const UNDO: Record<Recommendation['reversibility'], string> = {
  reversible: 'You can undo this at any time; the steps to undo it are below.',
  with_snapshot: 'A backup snapshot is kept, so it can be restored from that.',
  irreversible: 'This cannot be undone, so check nothing still needs it first.',
}

const EVIDENCE: Record<string, string> = {
  state: 'State',
  created: 'Created',
  lastSnapshot: 'Last snapshot',
  started: 'Snapshot taken',
  sourceVolume: 'Volume it came from',
  healthyTargets: 'Healthy targets',
  idleDays: 'Idle for (days)',
  volumeType: 'Volume type',
  peakCpuPercent: 'Highest CPU use',
  busiestDayNetworkMb: "Busiest day's network",
  stopSavesUsdMonth: 'Stopping saves',
  volumesUsdMonth: 'Its disks cost',
  maxConnections: 'Most connections at once',
  cpuP95Percent: 'CPU use, 95% of the time under',
  iopsP95: 'Disk operations a second, 95% of the time under',
  cpuPercentile: 'CPU use, nearly always under',
  projectedCpuPercent: 'Expected CPU use after the change',
  computeOptimizer: 'AWS Compute Optimizer says',
  previousGeneration: 'Server family',
  architecture: 'Processor type',
  nonProduction: 'Non-production because it is',
  runningHoursPercent: 'Hours it was running',
  scheduledHoursPerWeek: 'Running on the schedule',
  hoursOffPerMonth: 'Off on the schedule',
  hourlyRateUsd: 'Costs per hour (USD)',
  extendedSupportUsd: 'Extended Support charge',
  billedDays: 'Days billed',
  billedThrough: 'Bill read up to',
  usageType: 'Billed as',
  natHours: 'Hours billed',
  natBytes: 'Data processed (GB)',
  routeTables: 'Route tables sending traffic to it',
  eligibleOnDemandUsdHour: 'On-demand spend a Savings Plan could cover',
  baselineP10UsdHour: 'Quiet-hour level (10th percentile)',
  nettedSavingsUsdMonth: 'Other savings taken off first',
  awsHourlyCommitmentUsd: 'AWS recommends (an hour)',
  coveragePercent: 'Covered by Savings Plans now',
  usageDeclineTolerancePercent: 'Usage can fall before any goes unused',
}

const PERCENT = new Set([
  'peakCpuPercent',
  'cpuP95Percent',
  'cpuPercentile',
  'projectedCpuPercent',
  'runningHoursPercent',
  'coveragePercent',
  'usageDeclineTolerancePercent',
])
const MONEY = new Set(['stopSavesUsdMonth', 'volumesUsdMonth', 'nettedSavingsUsdMonth'])
const MONEY_HOUR = new Set(['eligibleOnDemandUsdHour', 'baselineP10UsdHour', 'awsHourlyCommitmentUsd'])
const DATES = new Set(['created', 'lastSnapshot', 'started', 'billedThrough'])

function evidenceValue(e: Recommendation['evidence'][number], money: (usd: number) => string): string {
  const v = e.value
  if (v === null || v === undefined) return '—'
  const days = typeof e.days === 'number' ? ` over ${e.days} days` : ''
  if (PERCENT.has(e.kind) && typeof v === 'number') return `${v}%${days}`
  if (MONEY.has(e.kind) && typeof v === 'number') return `${money(v)}/mo`
  if (e.kind === 'extendedSupportUsd' && typeof v === 'number') return `${money(v)}${days}`
  if (e.kind === 'billedDays' && typeof e.days === 'number') return `${String(v)} of the last ${e.days}`
  if (MONEY_HOUR.has(e.kind) && typeof v === 'number') return `USD ${v.toFixed(3)}/h${days}`
  if (e.kind === 'busiestDayNetworkMb' && typeof v === 'number') return `${v} MB${days}`
  if (e.kind === 'scheduledHoursPerWeek' && typeof v === 'number') return `${v} hours a week`
  if (e.kind === 'hoursOffPerMonth' && typeof v === 'number') return `${Math.round(v)} hours a month`
  if (e.kind === 'hourlyRateUsd' && typeof v === 'number') return `$${v.toFixed(3)}`
  if (e.kind === 'nonProduction' && typeof v === 'string')
    return v.replace(/^tag /, 'tagged ').replace(/^name /, 'named ')
  if (DATES.has(e.kind) && typeof v === 'string') return new Date(v).toLocaleDateString('en-GB')
  if (e.kind === 'computeOptimizer') return String(v).replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()
  const since = typeof e.sinceDays === 'number' ? ` for ${Math.round(e.sinceDays)} days` : ''
  return `${String(v)}${days}${since}`
}

const USAGE_CHARTS: Record<string, { title: string; unit: string; pick: 'maximum' | 'average'; max?: number }> = {
  cpu_max: { title: 'Highest CPU use each day', unit: '%', pick: 'maximum', max: 100 },
  connections: { title: 'Most database connections each day', unit: '', pick: 'maximum' },
}

/** Daily usage behind the recommendation (PRD US-3.2 evidence charts). */
function Usage({ tenantId, rec }: { tenantId: string; rec: Recommendation }) {
  const api = useApi()
  const usage = useQuery({
    queryKey: ['recommendation-usage', tenantId, rec.id],
    queryFn: () => api.recommendationUsage(tenantId, rec.id),
  })
  const charts = (usage.data ?? []).flatMap((s) => {
    const chart = USAGE_CHARTS[s.metric]
    return chart && s.days.length > 0 ? [{ series: s, chart }] : []
  })
  if (charts.length === 0) return null
  return (
    <Card className="flex flex-col gap-4 p-5">
      <h2 className="text-base font-semibold">Usage</h2>
      <div className="grid gap-6 lg:grid-cols-2">
        {charts.map(({ series: s, chart: c }) => {
          return (
            <div key={s.metric} className="flex flex-col gap-2">
              <h3 className="text-sm font-medium">{c.title}</h3>
              <UsageChart
                label={c.title}
                unit={c.unit}
                max={c.max}
                points={s.days.map((d) => ({ date: d.day, value: d[c.pick] }))}
              />
            </div>
          )
        })}
      </div>
    </Card>
  )
}

const DID: Partial<Record<RecommendationStatus, string>> = {
  new: 'reopened it',
  assigned: 'moved it to assigned',
  in_progress: 'started work on it',
  done: 'marked it as fixed',
  dismissed: 'dismissed it',
  verified: 'verified the change in AWS',
  not_verified: 'could not see the change in AWS yet',
  resolved_externally: 'closed it as resolved outside Cloudoptics',
}

function describe(e: RecommendationEvent): string {
  if (e.kind === 'savings_measured') return 'measured the realised savings from your bills'
  if (e.kind === 'assigned') return e.assigneeId ? `assigned it to ${e.assigneeName ?? 'a teammate'}` : 'unassigned it'
  if (e.kind === 'status_changed') return e.toStatus ? (DID[e.toStatus] ?? 'changed its status') : 'changed its status'
  return 'commented'
}

/** Who did what, oldest first, and a box to add a comment (members and admins). */
function History({ rec, tenantId, canComment }: { rec: Recommendation; tenantId: string; canComment: boolean }) {
  const api = useApi()
  const qc = useQueryClient()
  const [body, setBody] = useState('')
  const key = ['recommendation-events', tenantId, rec.id]
  const events = useQuery({ queryKey: key, queryFn: () => api.recommendationEvents(tenantId, rec.id) })
  const comment = useMutation({
    mutationFn: () => api.addComment(tenantId, rec.id, body),
    onSuccess: (updated) => {
      qc.setQueryData(key, updated)
      setBody('')
    },
  })
  return (
    <Card className="flex flex-col gap-4 p-5">
      <h2 className="text-base font-semibold">Activity</h2>
      {events.data && events.data.length === 0 && (
        <p className="text-sm text-secondary">Nobody has worked on this yet.</p>
      )}
      <ol className="flex flex-col gap-3">
        {(events.data ?? []).map((e) => (
          <li key={e.id} className="text-sm">
            <p>
              <span className="font-medium">{e.actorName ?? (e.actorId ? 'Someone' : 'Cloudoptics')}</span> {describe(e)}
              <span className="text-xs text-muted"> · {new Date(e.createdAt).toLocaleString('en-GB')}</span>
            </p>
            {e.body && <p className="mt-1 whitespace-pre-wrap rounded-sm bg-surface-2 px-3 py-2">{e.body}</p>}
          </li>
        ))}
      </ol>
      {canComment && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            comment.mutate()
          }}
        >
          <label className="text-sm font-medium" htmlFor="comment">
            Add a comment
          </label>
          <textarea
            id="comment"
            className="min-h-20 rounded-sm border border-border-strong bg-surface-1 px-3 py-2 text-sm"
            value={body}
            maxLength={2000}
            onChange={(e) => setBody(e.target.value)}
          />
          {comment.error && <Alert tone="error">Could not add the comment. Try again.</Alert>}
          <div>
            <Button type="submit" disabled={!body.trim() || comment.isPending}>
              Comment
            </Button>
          </div>
        </form>
      )}
    </Card>
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
        {r.status === 'verified' && (
          <div className="mt-2">
            <VerifiedBadge />
          </div>
        )}
      </div>
      <Outcome rec={r} money={money} />
      {r.ruleId === 'R8_savings_plan' && (
        <Alert tone="info">
          Advisory only. The{' '}
          <Link to="/commitments" className="font-medium underline">
            Commitments page
          </Link>{' '}
          compares our option with AWS&apos;s (Savings Plans and Reserved Instances, 1 or 3 years, each payment option),
          with the buying ladder and the cost in your currency if it weakens.
        </Alert>
      )}
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
          <div className="flex flex-col gap-3">
            <h2 className="text-base font-semibold">How to fix it</h2>
            {r.fix.schedule && <ScheduleSteps schedule={r.fix.schedule} />}
            {r.fix.console && (
              <div className="flex flex-wrap items-center gap-3">
                <a
                  href={r.fix.console.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex h-9 items-center gap-1.5 rounded-sm bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover"
                >
                  Open in AWS console <ExternalLink size={14} />
                </a>
                <span className="text-xs text-muted">Sign in to AWS account {r.accountId} first.</span>
              </div>
            )}
            {(r.fix.checklist?.length ?? 0) > 0 || (r.fix.console?.steps.length ?? 0) > 0 ? (
              <ol className="list-decimal space-y-1 pl-5 text-sm">
                {(r.fix.checklist?.length ? r.fix.checklist : (r.fix.console?.steps ?? [])).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ol>
            ) : (
              r.fix.steps.length > 0 && <Commands steps={r.fix.steps} />
            )}
            {r.fix.routes && r.fix.routes.tables.length > 0 && <NatRoutes routes={r.fix.routes} />}
            {r.fix.notes && <p className="text-xs text-muted">{r.fix.notes}</p>}
          </div>
          {r.fix.alternative && (
            <div>
              <h3 className="text-sm font-semibold">{r.fix.alternative.title}</h3>
              {r.fix.alternative.monthlyCostUsd !== undefined && (
                <p className="mb-2 text-xs text-muted">Costs about {money(r.fix.alternative.monthlyCostUsd)} a month.</p>
              )}
              <Commands steps={r.fix.alternative.steps} />
            </div>
          )}
          {(r.fix.steps.length > 0 || r.fix.rollback.length > 0) && (r.fix.console || r.fix.checklist?.length) && (
            <details className="rounded-sm border border-border px-3 py-2">
              <summary className="cursor-pointer text-sm font-medium">Prefer the command line? Show the AWS CLI commands</summary>
              <div className="mt-3 flex flex-col gap-3">
                {r.fix.steps.length > 0 && <Commands steps={r.fix.steps} />}
                {r.fix.rollback.length > 0 && (
                  <div>
                    <h3 className="mb-2 text-sm font-semibold">To undo it</h3>
                    <Commands steps={r.fix.rollback} />
                  </div>
                )}
              </div>
            </details>
          )}
          {!r.fix.console && !r.fix.checklist?.length && r.fix.rollback.length > 0 && (
            <div>
              <h3 className="mb-2 text-sm font-semibold">To undo it</h3>
              <Commands steps={r.fix.rollback} />
            </div>
          )}
          {role && role !== 'viewer' && <Actions rec={r} tenantId={tenantId} />}
        </Card>
        <div className="flex flex-col gap-4">
          <Card className="flex flex-col gap-3 p-5">
            <h2 className="text-base font-semibold">What could go wrong</h2>
            <div>
              <Pill tone={r.riskLevel}>{`${capitalise(r.riskLevel)} risk`}</Pill>
            </div>
            <ul className="flex list-disc flex-col gap-1 pl-5 text-sm">
              <li>{UNDO[r.reversibility]}</li>
              {r.restartRequired && <li>The server is unavailable for a few minutes while it restarts.</li>}
              {r.riskFactors.map((f) => (
                <li key={f.factor}>{FACTORS[f.factor] ?? f.factor}</li>
              ))}
            </ul>
          </Card>
          <Card className="flex flex-col gap-4 p-5">
            <div>
              <h2 className="mb-2 text-base font-semibold">Why we suggest it</h2>
              <ul className="flex flex-col gap-1 text-sm">
                {r.evidence.map((e) => (
                  <li key={e.kind} className="flex justify-between gap-3">
                    <span className="text-secondary">{EVIDENCE[e.kind] ?? e.kind}</span>
                    <span className="truncate text-right tabular-nums">{evidenceValue(e, money)}</span>
                  </li>
                ))}
              </ul>
            </div>
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
      <Usage tenantId={tenantId} rec={r} />
      <History rec={r} tenantId={tenantId} canComment={role !== undefined && role !== 'viewer'} />
    </div>
  )
}
