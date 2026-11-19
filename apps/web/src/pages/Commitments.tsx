import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { useState } from 'react'

import { useApi } from '../app/context'
import type { Commitment, CommitmentKind, CommitmentOption, CommitmentPayment, SavingsPlanAdvice, SavingsPlanAdvisory } from '../api/types'
import { DailySpendChart } from '../components/charts'
import { Alert, Button, Card, cx } from '../components/ui'
import { formatMoney } from '../lib/currency'
import { Kpi, useMoney, useWorkspace } from './Workspace'

const KIND: Record<CommitmentKind, string> = {
  savings_plan: 'Savings Plan',
  ec2_reserved: 'EC2 Reserved Instance',
  rds_reserved: 'RDS Reserved Instance',
}

const PAYMENT: Record<CommitmentPayment, string> = {
  no_upfront: 'No Upfront',
  partial_upfront: 'Partial Upfront',
  all_upfront: 'All Upfront',
}

const INSTRUMENT: Record<CommitmentOption['instrument'], string> = {
  savings_plan: 'Compute Savings Plan',
  reserved_instance: 'EC2 Reserved Instance',
}

const STATUS_NOTE: Record<Exclude<SavingsPlanAdvice['status'], 'ok'>, string> = {
  no_data: 'There is no on-demand compute spend a Savings Plan could cover in the bills yet.',
  not_enough_data:
    'Fewer than 14 days of bills so far. The advisory needs at least two weeks to see the quiet days; the figures below are a first look only.',
  no_purchase:
    'The usage that is always there, after the other recommendations, is too small for a commitment to be worth it now. Nothing to buy.',
}

const hourly = (usd: number) => `USD ${usd.toFixed(3)}/h`
const percent = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${v.toFixed(1)}%`)

function months(v: number): string {
  if (v <= 0) return 'from the first month'
  return `${v.toFixed(1)} months`
}

function optionName(o: CommitmentOption): string {
  return `${INSTRUMENT[o.instrument]}, ${o.termYears} year${o.termYears > 1 ? 's' : ''}, ${PAYMENT[o.payment]}`
}

function day(iso: string): string {
  return new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

/** Savings Plans and Reserved Instances, and the Savings Plan advisory (PRD US-4.5, FR-ANA-10). */
export function Commitments() {
  const api = useApi()
  const qc = useQueryClient()
  const { me, tenantId } = useWorkspace()
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  const held = useQuery({
    queryKey: ['commitments', tenantId],
    queryFn: () => api.listCommitments(tenantId as string),
    enabled: tenantId !== null,
  })
  const advisory = useQuery({
    queryKey: ['savings-plan-advisory', tenantId],
    queryFn: () => api.savingsPlanAdvisory(tenantId as string),
    enabled: tenantId !== null,
  })
  const refresh = useMutation({
    mutationFn: () => api.refreshCommitments(tenantId as string),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['commitments', tenantId] })
      void qc.invalidateQueries({ queryKey: ['savings-plan-advisory', tenantId] })
      void qc.invalidateQueries({ queryKey: ['recommendations', tenantId] })
    },
  })
  if (tenantId === null) return null
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            Commitments
            <span className="rounded-full bg-surface-2 px-2.5 py-0.5 text-xs font-medium text-secondary">Advisory only</span>
          </h1>
          <p className="text-sm text-secondary">
            Savings Plans and Reserved Instances: what you hold, when it ends, and whether to commit to more. Cloudoptics
            never buys anything for you.
          </p>
        </div>
        {role === 'admin' && (
          <Button variant="secondary" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            <RefreshCw size={16} /> {refresh.isPending ? 'Refreshing…' : 'Refresh now'}
          </Button>
        )}
      </div>
      {refresh.data && (
        <Alert tone="info">
          {refresh.data.status === 'queued'
            ? 'Refreshing in the background; this page updates in a minute or two.'
            : refresh.data.status === 'skipped'
              ? 'No working AWS connection to read.'
              : 'Read again from AWS just now (about USD 0.08 of Cost Explorer calls a connection, billed to your AWS account).'}
        </Alert>
      )}
      {refresh.error && <Alert tone="error">{refresh.error.message}</Alert>}
      {advisory.isLoading && <p className="text-sm text-secondary">Loading…</p>}
      {advisory.error && <Alert tone="error">{advisory.error.message}</Alert>}
      {advisory.data?.advice.length === 0 && (
        <Alert tone="info">No bills yet. The Savings Plan advisory appears once Cloudoptics has your AWS bills.</Alert>
      )}
      {advisory.data?.advice.map((a) => (
        <Advice key={a.connectionId} advice={a} advisory={advisory.data} several={advisory.data.advice.length > 1} />
      ))}
      <Held commitments={held.data?.commitments ?? []} alertDays={held.data?.expiryAlertDays ?? [60, 30, 15]} loading={held.isLoading} />
      {held.data?.coverage.map((c) => (
        <Card key={c.connectionId} className="p-5">
          <h2 className="mb-3 text-base font-semibold">Coverage and use, last 30 days (AWS Cost Explorer)</h2>
          <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-4">
            <Stat label="Savings Plans cover" value={percent(c.savingsPlansCoveragePercent)} />
            <Stat label="Savings Plans used" value={percent(c.savingsPlansUtilisationPercent)} />
            <Stat label="Reservations cover (hours)" value={percent(c.reservationsCoveragePercent)} />
            <Stat label="Reservations used" value={percent(c.reservationsUtilisationPercent)} />
          </dl>
          <p className="mt-3 text-xs text-muted">Read from AWS {day(c.fetchedAt)}.</p>
          {c.errors.length > 0 && (
            <p className="mt-1 text-xs text-warning-ink">Some figures could not be read: {c.errors.join('; ')}</p>
          )}
        </Card>
      ))}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums">{value}</dd>
    </div>
  )
}

function Advice({ advice: a, advisory, several }: { advice: SavingsPlanAdvice; advisory: SavingsPlanAdvisory; several: boolean }) {
  const { money } = useMoney()
  const o = a.default
  return (
    <section className="flex flex-col gap-4" aria-label={`Savings Plan advisory for ${a.payerAccountId}`}>
      {/* The cards below use h3, so each advisory has its h2 (heading order), shown when there are several. */}
      <h2 className={several ? 'text-lg font-semibold' : 'sr-only'}>
        {several ? `Payer account ${a.payerAccountId}` : 'Savings Plan advisory'}
      </h2>
      {a.status !== 'ok' && <Alert tone={a.status === 'not_enough_data' ? 'warning' : 'info'}>{STATUS_NOTE[a.status]}</Alert>}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Coverage current={a.currentCoveragePercent} target={a.status === 'ok' ? a.targetCoveragePercent : null} />
        <Kpi
          label="Savings Plans used"
          value={percent(a.utilisationPercent)}
          note="Of what you already committed to, last 30 days"
        />
        <Kpi
          label="Coverable on-demand spend"
          value={hourly(a.baseline.averageHourlyUsd)}
          note={`Average over ${a.baseline.days} days, after other recommendations`}
        />
        {o && a.status === 'ok' ? (
          <Kpi label="Saving with our option" value={`${money(o.monthlySavingsUsd)}/mo`} note={`${hourly(o.hourlyCommitmentUsd)} committed`} />
        ) : (
          <Kpi label="Saving with our option" value="—" note="Nothing to buy now" />
        )}
      </div>
      {o && a.status === 'ok' && <Recommended advice={a} option={o} returnWindowDays={advisory.returnWindowDays} />}
      <Card className="p-5">
        <h3 className="mb-1 text-base font-semibold">Spend a Savings Plan could cover</h3>
        <p className="mb-3 text-xs text-muted">
          On-demand EC2, Fargate and Lambda each day (not Spot, not already covered), before taking off other
          recommendations.
        </p>
        <DailySpendChart points={a.dailyEligible} currency="USD" label="Daily coverable on-demand spend" />
        <Sizing advice={a} />
      </Card>
      {o && <Matrix options={a.options} best={o} />}
      {o && a.status === 'ok' && a.ladder.length > 0 && <Ladder advice={a} advisory={advisory} />}
      {o && a.status === 'ok' && a.fx && <FxStress advice={a} option={o} />}
    </section>
  )
}

function Coverage({ current, target }: { current: number | null; target: number | null }) {
  const now = Math.min(100, current ?? 0)
  const next = target === null ? null : Math.min(100, target)
  return (
    <Card className="p-5">
      <p className="text-sm text-secondary">Savings Plan coverage</p>
      <p className="mt-2 text-2xl font-semibold tabular-nums">
        {percent(current)}
        {next !== null && <span className="text-base font-normal text-secondary"> → {percent(next)}</span>}
      </p>
      <div
        className="relative mt-2 h-2 overflow-hidden rounded-full bg-surface-2"
        role="meter"
        aria-label="Savings Plan coverage"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(now)}
      >
        {next !== null && <div className="absolute inset-y-0 left-0 bg-accent-soft" style={{ width: `${next}%` }} />}
        <div className="absolute inset-y-0 left-0 bg-accent" style={{ width: `${now}%` }} />
      </div>
      <p className="mt-1 text-xs text-muted">{next !== null ? 'Now → after our option' : 'Of coverable spend, last 30 days'}</p>
    </Card>
  )
}

function Recommended({ advice: a, option: o, returnWindowDays }: { advice: SavingsPlanAdvice; option: CommitmentOption; returnWindowDays: number }) {
  const { money } = useMoney()
  const fxNow = a.fx?.scenarios.find((s) => s.weakeningPercent === 0)
  return (
    <Card className="flex flex-col gap-3 p-5">
      <div>
        <h3 className="text-base font-semibold">Our option: {optionName(o)}</h3>
        <p className="text-sm text-secondary">
          {a.fxExposed
            ? 'For a workspace that earns in another currency we suggest 1 year with nothing up front: no large dollar payment now, and the shortest time tied to the exchange rate.'
            : 'The shortest term with nothing up front: it covers any instance family, size or region, so it keeps working as your servers change.'}
        </p>
      </div>
      <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-4">
        <Stat label="Hourly commitment" value={hourly(o.hourlyCommitmentUsd)} />
        <Stat label="Monthly saving" value={money(o.monthlySavingsUsd)} />
        <Stat label="Pays back" value={months(o.breakEvenMonths)} />
        <Stat
          label="Total over the term"
          value={`${formatMoney(o.totalCommitmentUsd, 'USD', { compact: true })}${
            fxNow && a.fx ? ` · ${formatMoney(fxNow.totalLocal, a.fx.currency, { compact: true })}` : ''
          }`}
        />
      </dl>
      {a.declineTolerancePercent !== null && (
        <p className="text-sm text-secondary">
          <strong className="font-medium text-primary">Risk if usage falls:</strong> coverable usage can drop{' '}
          {a.declineTolerancePercent.toFixed(0)}% below its 60-day average before any of this commitment goes unused. You pay
          for the commitment every hour of the year whether you use it or not.
        </p>
      )}
      <p className="text-xs text-muted">
        AWS lets you return a Savings Plan within {returnWindowDays} days of buying it, if the hourly commitment is USD 100
        or less. Check the purchase during that week.
      </p>
    </Card>
  )
}

function Sizing({ advice: a }: { advice: SavingsPlanAdvice }) {
  const b = a.baseline
  return (
    <details className="mt-4 text-sm">
      <summary className="cursor-pointer font-medium">How we size it</summary>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-secondary">
        <li>
          {b.nettedUsdMonth > 0
            ? `Open recommendations on servers (idle, rightsizing, newer types, schedules) would save USD ${b.nettedUsdMonth.toFixed(2)} a month; we take that off first so nothing is counted twice.`
            : 'No open recommendations on servers to take off first.'}
        </li>
        <li>
          Over the last {b.days} days the quietest hours run at about {hourly(b.p10HourlyUsd)} (10th percentile) to{' '}
          {hourly(b.p20HourlyUsd)} (20th).
        </li>
        <li>
          A safe commitment covers 60–80% of that: {hourly(b.bandLowHourlyUsd)} to {hourly(b.bandHighHourlyUsd)} of on-demand
          spend. We keep the bills by day, not by hour, so each day counts as its average hour; that hides night-time dips,
          so we take the cautious end ({hourly(b.targetHourlyUsd)}).
        </li>
        {a.dataThrough && <li>Bills up to {day(a.dataThrough)}.</li>}
      </ul>
    </details>
  )
}

function Matrix({ options, best }: { options: CommitmentOption[]; best: CommitmentOption }) {
  const { money } = useMoney()
  const ours = options.filter((o) => o.source === 'ours')
  const aws = options.filter((o) => o.source === 'aws')
  const row = (o: CommitmentOption, name: string) => (
    <tr key={`${o.source}-${o.instrument}-${o.termYears}-${o.payment}-${o.lookbackDays}`} className={cx(o === best && 'bg-accent-soft')}>
      <td className="py-2 pr-3">
        {name}
        {o === best && <span className="ml-2 text-xs font-medium text-primary">Suggested</span>}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">{hourly(o.hourlyCommitmentUsd)}</td>
      <td className="py-2 pr-3 text-right tabular-nums">
        {o.discountPercent.toFixed(0)}%{o.discountFromAws && <span title="AWS's estimate for your usage">*</span>}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">{money(o.monthlySavingsUsd)}</td>
      <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(o.upfrontUsd, 'USD', { compact: true })}</td>
      <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(o.totalCommitmentUsd, 'USD', { compact: true })}</td>
      <td className="py-2 text-right tabular-nums">{o.breakEvenMonths <= 0 ? '—' : `${o.breakEvenMonths.toFixed(1)} mo`}</td>
    </tr>
  )
  return (
    <Card className="p-5">
      <h3 className="mb-1 text-base font-semibold">Options</h3>
      <p className="mb-3 text-xs text-muted">
        Our sizing priced each way, then AWS&apos;s own recommendation (1-year Compute Savings Plan, from 30 and 60 days of
        usage). Reserved Instances fix the instance family and region. Break-even: when the on-demand cost avoided has paid
        back the upfront payment. * discount AWS estimated for your usage; the others are typical rates.
      </p>
      {/* Scrolls sideways on a phone; focusable so the keyboard can scroll it too. */}
      <div className="overflow-x-auto" role="region" aria-label="Savings Plan options" tabIndex={0}>
        <table className="w-full min-w-[680px] text-sm">
          <thead className="text-left text-xs text-muted">
            <tr>
              <th className="pb-2 pr-3 font-medium">Option</th>
              <th className="pb-2 pr-3 text-right font-medium">Commitment</th>
              <th className="pb-2 pr-3 text-right font-medium">Discount</th>
              <th className="pb-2 pr-3 text-right font-medium">Saving a month</th>
              <th className="pb-2 pr-3 text-right font-medium">Upfront</th>
              <th className="pb-2 pr-3 text-right font-medium">Total</th>
              <th className="pb-2 text-right font-medium">Break-even</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {ours.map((o) => row(o, optionName(o)))}
            {aws.map((o) => row(o, `AWS recommends: 1 year, ${PAYMENT[o.payment]} (${o.lookbackDays ?? '?'} days of usage)`))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function Ladder({ advice: a, advisory }: { advice: SavingsPlanAdvice; advisory: SavingsPlanAdvisory }) {
  return (
    <Card className="p-5">
      <h3 className="mb-1 text-base font-semibold">Buy it in steps</h3>
      <p className="mb-3 text-sm text-secondary">
        Start with {advisory.ladderFirstPercent}% of the target, then add about {advisory.ladderStepPercent}% each month while
        usage holds. Each step is a separate 1-year Savings Plan, so they also end in steps.
      </p>
      <ol className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-3 lg:grid-cols-5">
        {a.ladder.map((s) => (
          <li key={s.month} className="rounded-sm border border-border p-3">
            <p className="text-xs text-muted">{s.month === 0 ? 'Now' : day(s.buyOn)}</p>
            <p className="font-semibold tabular-nums">+ {hourly(s.buyHourlyUsd)}</p>
            <p className="text-xs text-muted">
              {hourly(s.totalHourlyUsd)} in all ({s.sharePercent}%)
            </p>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-xs text-muted">
        Return window: AWS lets you return a Savings Plan within {advisory.returnWindowDays} days of buying it (hourly
        commitment of USD 100 or less), so check each step that week.
      </p>
    </Card>
  )
}

function FxStress({ advice: a, option: o }: { advice: SavingsPlanAdvice; option: CommitmentOption }) {
  const [shift, setShift] = useState(20)
  const fx = a.fx
  if (!fx) return null
  const monthlyUsd = o.totalCommitmentUsd / (12 * o.termYears)
  const local = (rate: number) => formatMoney(monthlyUsd * rate, fx.currency)
  return (
    <Card className="p-5">
      <h3 className="mb-1 text-base font-semibold">In {fx.currency}, if the currency weakens</h3>
      <p className="mb-3 text-sm text-secondary">
        The commitment is billed in US dollars. At {fx.estimated ? '≈' : ''}
        {fx.rate.toLocaleString()} {fx.currency} per USD ({day(fx.rateDate)}) it costs {local(fx.rate)} a month.
      </p>
      <div className="overflow-x-auto" role="region" aria-label="Cost if the currency weakens" tabIndex={0}>
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-muted">
          <tr>
            <th className="pb-2 pr-3 font-medium">Scenario</th>
            <th className="pb-2 pr-3 text-right font-medium">Rate</th>
            <th className="pb-2 pr-3 text-right font-medium">A month</th>
            <th className="pb-2 text-right font-medium">Over the term</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {fx.scenarios.map((s) => (
            <tr key={s.weakeningPercent}>
              <td className="py-2 pr-3">{s.weakeningPercent === 0 ? 'Today’s rate' : `${fx.currency} weakens ${s.weakeningPercent}%`}</td>
              <td className="py-2 pr-3 text-right tabular-nums">{s.rate.toLocaleString()}</td>
              <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(s.monthlyLocal, fx.currency)}</td>
              <td className="py-2 text-right tabular-nums">{formatMoney(s.totalLocal, fx.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <label className="mt-4 flex flex-col gap-1 text-sm">
        <span>
          Try a move of <strong>{shift}%</strong>: {local(fx.rate * (1 + shift / 100))} a month
        </span>
        <input
          type="range"
          min={-30}
          max={80}
          step={5}
          value={shift}
          onChange={(e) => setShift(Number(e.target.value))}
          aria-label="Exchange-rate move in percent"
        />
      </label>
      <p className="mt-2 text-xs text-muted">A weakening of 20% means each US dollar costs 20% more {fx.currency}.</p>
    </Card>
  )
}

function Held({ commitments, alertDays, loading }: { commitments: Commitment[]; alertDays: number[]; loading: boolean }) {
  const current = commitments.filter((c) => c.current)
  const soonest = Math.max(...alertDays)
  return (
    <Card className="p-5">
      <h2 className="mb-1 text-base font-semibold">What you hold</h2>
      <p className="mb-3 text-xs text-muted">
        Read daily from AWS. Admins and members get an email {alertDays.join(', ')} days before each one ends (Settings,
        Notifications).
      </p>
      {loading && <p className="text-sm text-secondary">Loading…</p>}
      {!loading && current.length === 0 && (
        <p className="text-sm text-secondary">No Savings Plans or Reserved Instances found yet.</p>
      )}
      {/* Scrolls sideways on a phone; focusable so the keyboard can scroll it too. */}
      {current.length > 0 && (
        <div className="overflow-x-auto" role="region" aria-label="Commitments you hold" tabIndex={0}>
          <table className="w-full min-w-[640px] text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                <th className="pb-2 pr-3 font-medium">Commitment</th>
                <th className="pb-2 pr-3 font-medium">Account</th>
                <th className="pb-2 pr-3 text-right font-medium">Per hour</th>
                <th className="pb-2 pr-3 text-right font-medium">Used</th>
                <th className="pb-2 text-right font-medium">Ends</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {current.map((c) => (
                <tr key={c.id}>
                  <td className="py-2 pr-3">
                    <p className="font-medium">{c.description}</p>
                    <p className="text-xs text-muted">
                      {KIND[c.kind]}
                      {c.paymentOption ? ` · ${c.paymentOption}` : ''}
                      {c.termMonths ? ` · ${c.termMonths / 12} yr` : ''}
                    </p>
                  </td>
                  <td className="py-2 pr-3 text-xs">
                    {c.accountId ?? '—'}
                    {c.regionId ? ` · ${c.regionId}` : ''}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {c.hourlyCommitmentUsd === null ? '—' : hourly(c.hourlyCommitmentUsd)}
                  </td>
                  <td
                    className={cx(
                      'py-2 pr-3 text-right tabular-nums',
                      c.utilisationPct !== null && c.utilisationPct < 80 && 'text-warning-ink',
                    )}
                  >
                    {percent(c.utilisationPct)}
                  </td>
                  <td className="py-2 text-right">
                    {c.endsAt ? day(c.endsAt) : '—'}
                    {c.daysLeft !== null && c.daysLeft <= soonest && (
                      <span className="ml-2 inline-flex rounded-full bg-warning-soft px-2 py-0.5 text-xs font-medium text-warning-ink">
                        {c.daysLeft} days left
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
