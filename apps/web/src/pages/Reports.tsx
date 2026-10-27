import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { Mail, Printer } from 'lucide-react'
import { useState, type ReactNode } from 'react'

import { useApi } from '../app/context'
import { ApiError, type MonthlyReport } from '../api/types'
import { Alert, Button, Card, cx } from '../components/ui'
import { useWorkspace } from './Workspace'

const BUDGET_STATUS: Record<string, string> = {
  on_track: 'On track',
  at_risk: 'Forecast over budget',
  over: 'Over budget',
  no_data: 'No bills',
  no_rate: 'No exchange rate',
}

function monthLabel(month: string): string {
  return new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' })
}

const usd = (n: number) => `USD ${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const local = (n: number, currency: string) => `${currency} ${n.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
const signed = (n: number, fmt: (v: number) => string) => `${n >= 0 ? '+' : '−'}${fmt(Math.abs(n))}`

/** Monthly executive reports (PRD US-7.1): view, print or save as PDF, and send. */
export function Reports({ month }: { month?: string }) {
  const api = useApi()
  const navigate = useNavigate()
  const { me, tenantId } = useWorkspace()
  const role = me?.memberships.find((m) => m.tenantId === tenantId)?.role
  const list = useQuery({
    queryKey: ['reports', tenantId],
    queryFn: () => api.listReports(tenantId as string),
    enabled: tenantId !== null,
  })
  if (tenantId === null) return null
  const months = list.data
    ? [...new Set([list.data.latestMonth, ...list.data.reports.map((r) => r.month)])].sort().reverse()
    : []
  const selected = month ?? list.data?.latestMonth
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3 print:hidden">
        <div>
          <h1 className="text-2xl font-semibold">Reports</h1>
          <p className="text-sm text-secondary">
            A one-page summary of each month, made on the 3rd working day of the next month and emailed to the
            workspace. Print it or save it as PDF to forward it.
          </p>
        </div>
        {months.length > 0 && (
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Month</span>
            <select
              className="h-9 rounded-sm border border-border-strong bg-surface-1 px-3 text-sm"
              value={selected}
              onChange={(e) => void navigate({ to: '/reports', search: { month: e.target.value } })}
            >
              {months.map((m) => (
                <option key={m} value={m}>
                  {monthLabel(m)}
                  {list.data?.reports.some((r) => r.month === m) ? '' : ' (draft)'}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {list.error && <Alert tone="error">{list.error.message}</Alert>}
      {selected && <ReportView tenantId={tenantId} month={selected} isAdmin={role === 'admin'} />}
      {role === 'admin' && <RecipientsCard tenantId={tenantId} />}
    </div>
  )
}

function ReportView({ tenantId, month, isAdmin }: { tenantId: string; month: string; isAdmin: boolean }) {
  const api = useApi()
  const qc = useQueryClient()
  const view = useQuery({
    queryKey: ['report', tenantId, month],
    queryFn: () => api.getReport(tenantId, month),
    retry: false,
  })
  const send = useMutation({
    mutationFn: () => api.sendReport(tenantId, month),
    onSuccess: (result) => {
      qc.setQueryData(['report', tenantId, month], { report: result.report, stored: true })
      void qc.invalidateQueries({ queryKey: ['reports', tenantId] })
    },
  })
  if (view.isLoading) return <p className="text-sm text-secondary">Loading…</p>
  if (view.error)
    return (
      <Alert tone="info">
        {view.error instanceof ApiError && view.error.status === 404
          ? `No bills for ${monthLabel(month)}, so there is no report.`
          : view.error.message}
      </Alert>
    )
  if (!view.data) return null
  const { report, stored } = view.data
  return (
    <>
      <div className="flex flex-wrap items-center gap-3 print:hidden">
        <Button variant="secondary" onClick={() => window.print()}>
          <Printer size={16} /> Print or save as PDF
        </Button>
        {isAdmin && (
          <Button variant="secondary" disabled={send.isPending} onClick={() => send.mutate()}>
            <Mail size={16} /> {send.isPending ? 'Sending…' : stored ? 'Make again and send' : 'Make and send now'}
          </Button>
        )}
        <span className="text-xs text-muted">
          {stored
            ? `Made ${new Date(report.generatedAt).toLocaleString()}; the figures stay as they were then.`
            : 'Draft from today’s data: the report is made and emailed on the 3rd working day of the month after.'}
        </span>
      </div>
      {send.data && (
        <div className="print:hidden">
          <Alert tone="info">
            {send.data.sentTo ? `Sent to ${send.data.sentTo} recipient${send.data.sentTo === 1 ? '' : 's'}.` : 'Saved; nobody gets it by email.'}
          </Alert>
        </div>
      )}
      {send.error && <Alert tone="error">{send.error.message}</Alert>}
      <ReportDocument report={report} />
    </>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2 break-inside-avoid">
      <h3 className="text-sm font-semibold uppercase tracking-wide text-secondary">{title}</h3>
      {children}
    </section>
  )
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-sm border border-border p-3">
      <p className="text-xs text-muted">{label}</p>
      <p className="text-lg font-semibold">{value}</p>
      {sub && <p className="text-xs text-secondary">{sub}</p>}
    </div>
  )
}

/** The report as printed: A4-friendly, both currencies, no app controls. */
export function ReportDocument({ report: r }: { report: MonthlyReport }) {
  const s = r.spend
  const ccy = r.currency
  const hasLocal = s.local !== null && ccy !== 'USD'
  return (
    <article
      aria-label={`Report for ${monthLabel(r.month)}`}
      className="flex flex-col gap-6 rounded-md border border-border bg-surface-1 p-6 print:border-0 print:p-0"
    >
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border pb-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted">Cloudoptics monthly report</p>
          <h2 className="text-xl font-semibold">
            {r.workspace}: {monthLabel(r.month)}
          </h2>
        </div>
        <p className="text-xs text-muted">Made {new Date(r.generatedAt).toLocaleDateString('en-GB')}</p>
      </header>
      <div className="grid gap-3 sm:grid-cols-3">
        <Kpi label="Total spend" value={usd(s.usd)} sub={hasLocal ? local(s.local as number, ccy) : undefined} />
        <Kpi
          label="Against the month before"
          value={s.changePercent === null ? signed(s.changeUsd, usd) : `${s.changePercent >= 0 ? '+' : ''}${s.changePercent}%`}
          sub={`${usd(s.previousUsd)}${s.previousLocal !== null && ccy !== 'USD' ? ` · ${local(s.previousLocal, ccy)}` : ''}`}
        />
        <Kpi
          label="Open savings"
          value={`${usd(r.savings.openUsdMonth)} a month`}
          sub={`${r.savings.openCount} recommendations${r.savings.newCount ? `, ${r.savings.newCount} new` : ''}`}
        />
      </div>
      {s.usageEffect !== null && s.fxEffect !== null && (
        <Section title={`Change in ${ccy}`}>
          <p className="text-sm">
            {signed(s.usageEffect + s.fxEffect, (v) => local(v, ccy))} in total: {signed(s.usageEffect, (v) => local(v, ccy))} from
            using more or less, {signed(s.fxEffect, (v) => local(v, ccy))} from the exchange rate (average{' '}
            {s.previousRate?.toLocaleString('en-US', { maximumFractionDigits: 2 })} then{' '}
            {s.rate?.toLocaleString('en-US', { maximumFractionDigits: 2 })} {ccy} per USD).
          </p>
        </Section>
      )}
      {r.drivers.length > 0 && (
        <Section title="What changed most">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                <th className="pb-1 font-normal">Service</th>
                <th className="pb-1 pl-3 text-right font-normal">This month</th>
                <th className="hidden pb-1 pl-3 text-right font-normal sm:table-cell">Month before</th>
                <th className="pb-1 pl-3 text-right font-normal">Change</th>
              </tr>
            </thead>
            <tbody>
              {r.drivers.map((d) => (
                <tr key={d.serviceName} className="border-t border-border">
                  <td className="py-1 pr-2">{d.serviceName}</td>
                  <td className="py-1 pl-3 text-right whitespace-nowrap">{usd(d.usd)}</td>
                  <td className="hidden py-1 pl-3 text-right whitespace-nowrap sm:table-cell">{usd(d.previousUsd)}</td>
                  <td className={cx('py-1 pl-3 text-right font-medium whitespace-nowrap', d.changeUsd > 0 && 'text-critical-ink')}>
                    {signed(d.changeUsd, usd)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {s.creditsUsd !== 0 && <p className="text-xs text-secondary">AWS credits applied this month: {usd(-s.creditsUsd)}.</p>}
        </Section>
      )}
      <div className="grid gap-6 sm:grid-cols-2">
        <Section title="Budgets">
          {r.budgets.length === 0 ? (
            <p className="text-sm text-secondary">No budgets set.</p>
          ) : (
            <ul className="flex flex-col gap-1 text-sm">
              {r.budgets.map((b) => (
                <li key={b.name}>
                  <span className="font-medium">{b.name}</span> ({b.currency} {b.amount.toLocaleString()}):{' '}
                  {BUDGET_STATUS[b.status] ?? b.status}
                  {b.actualPercent !== null && `, ${Math.round(b.actualPercent)}% spent`}
                </li>
              ))}
            </ul>
          )}
        </Section>
        <Section title="Savings and anomalies">
          <ul className="flex flex-col gap-1 text-sm">
            <li>
              Marked done this month: {r.savings.doneCount}, saving {usd(r.savings.doneUsdMonth)} a month.
            </li>
            <li>
              {r.anomalies.count
                ? `${r.anomalies.count} unusual day${r.anomalies.count === 1 ? '' : 's'}, ${usd(r.anomalies.impactUsd)} above normal.`
                : 'No unusual days.'}
            </li>
          </ul>
        </Section>
      </div>
      {r.topRecommendations.length > 0 && (
        <Section title="Top open recommendations">
          <ol className="flex list-decimal flex-col gap-1 pl-5 text-sm">
            {r.topRecommendations.map((t) => (
              <li key={t.title}>
                {t.title}: <span className="font-medium">{usd(t.savingsUsdMonth)} a month</span>, {t.riskLevel} risk,
                effort {t.effort}
              </li>
            ))}
          </ol>
        </Section>
      )}
      <Section title="About these figures">
        <p className="text-xs text-secondary">{r.accuracy.note}</p>
      </Section>
    </article>
  )
}

function RecipientsCard({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const current = useQuery({ queryKey: ['report-settings', tenantId], queryFn: () => api.getReportSettings(tenantId) })
  const [draft, setDraft] = useState<string | null>(null)
  const value = draft ?? current.data?.emails.join(', ') ?? ''
  const save = useMutation({
    mutationFn: () => api.saveReportSettings(tenantId, value.split(/[\s,;]+/).filter(Boolean)),
    onSuccess: (updated) => {
      qc.setQueryData(['report-settings', tenantId], updated)
      setDraft(null)
    },
  })
  return (
    <Card className="flex flex-col gap-3 print:hidden">
      <div>
        <h2 className="text-base font-semibold">Who gets the report</h2>
        <p className="mt-1 text-sm text-secondary">
          Every member, unless they turn it off under Settings. Add other addresses here, for example your board or
          accountant.
        </p>
      </div>
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Also send to</span>
        <input
          className="h-9 rounded-sm border border-border-strong bg-surface-1 px-3 text-sm"
          value={value}
          placeholder="board@example.com, finance@example.com"
          onChange={(e) => setDraft(e.target.value)}
        />
      </label>
      {save.error && <Alert tone="error">{save.error.message}</Alert>}
      <div>
        <Button disabled={draft === null || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </Card>
  )
}
