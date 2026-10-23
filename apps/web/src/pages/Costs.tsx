import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { useApi } from '../app/context'
import type { CostDimension } from '../api/types'
import { DailySpendChart } from '../components/charts'
import { Alert, Card, cx } from '../components/ui'
import { DIMENSIONS, useMoney, useWorkspace } from './Workspace'

const RANGES = [30, 90] as const

function Toggle<T extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: { id: T; label: string }[]
  value: T
  onChange: (value: T) => void
}) {
  return (
    <div role="group" aria-label={label} className="flex rounded-sm border border-border text-xs">
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          aria-pressed={value === o.id}
          onClick={() => onChange(o.id)}
          className={cx(
            'px-2 py-1',
            value === o.id ? 'bg-accent-soft font-medium text-primary' : 'text-secondary hover:bg-surface-2',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** Cost Explorer (PRD US-2.x): daily trend and this month's spend by service, account or region. */
export function Costs() {
  const api = useApi()
  const { tenantId } = useWorkspace()
  const { money } = useMoney()
  const [days, setDays] = useState<(typeof RANGES)[number]>(30)
  const [dimension, setDimension] = useState<CostDimension>('service')
  const daily = useQuery({
    queryKey: ['cost-daily', tenantId, days],
    queryFn: () => api.costDaily(tenantId as string, days),
    enabled: tenantId !== null,
  })
  const breakdown = useQuery({
    queryKey: ['cost-breakdown', tenantId, dimension, 25],
    queryFn: () => api.costBreakdown(tenantId as string, dimension, 25),
    enabled: tenantId !== null,
  })
  const points = daily.data?.points ?? []
  const items = breakdown.data?.items ?? []
  const total = points.reduce((sum, p) => sum + p.amount, 0)
  const provisional = daily.data?.source === 'cost_explorer'
  const month = breakdown.data
    ? new Date(`${breakdown.data.month}-01T00:00:00Z`).toLocaleDateString('en-GB', {
        month: 'long',
        year: 'numeric',
        timeZone: 'UTC',
      })
    : ''

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Cost Explorer</h1>
        <p className="text-sm text-secondary">Amortised cost after credits, from your AWS Cost and Usage Report.</p>
      </div>
      {daily.data && daily.data.source === null && (
        <Alert tone="info">Costs appear here once the first report from AWS is loaded.</Alert>
      )}
      {provisional && (
        <Alert tone="info">
          Provisional figures from AWS Cost Explorer until the first Cost and Usage Report is loaded.
        </Alert>
      )}
      <Card className="p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">Daily spend</h2>
            <p className="text-xs text-muted">
              {points.length > 0 ? `${money(total)} over ${points.length} days` : 'No daily data yet'}
            </p>
          </div>
          {!provisional && (
            <Toggle
              label="Range"
              options={RANGES.map((r) => ({ id: r, label: `${r} days` }))}
              value={days}
              onChange={setDays}
            />
          )}
        </div>
        {daily.isLoading && <p className="text-sm text-secondary">Loading…</p>}
        {points.length > 0 && <DailySpendChart points={points} currency="USD" />}
      </Card>
      <Card className="p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">
              Spend by {dimension} · {month}
            </h2>
            <p className="text-xs text-muted">
              {breakdown.data && breakdown.data.total > 0 ? `${money(breakdown.data.total)} excluding tax` : ''}
            </p>
          </div>
          <Toggle label="Group by" options={DIMENSIONS} value={dimension} onChange={setDimension} />
        </div>
        {breakdown.isLoading && <p className="text-sm text-secondary">Loading…</p>}
        {breakdown.data && items.length === 0 && (
          <p className="text-sm text-secondary">
            {breakdown.data.source === 'cost_explorer'
              ? 'Available once the first Cost and Usage Report is loaded.'
              : 'No spend this month yet.'}
          </p>
        )}
        {items.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted">
                <th className="py-2 font-medium">{DIMENSIONS.find((d) => d.id === dimension)?.label}</th>
                <th className="py-2 text-right font-medium">Spend</th>
                <th className="hidden w-1/3 py-2 font-medium sm:table-cell">
                  <span className="sr-only">Share</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.key} className="border-b border-border last:border-0">
                  <td className="max-w-0 truncate py-2 pr-3" title={item.label}>
                    {item.label}
                    {item.label !== item.key && item.key !== 'other' && (
                      <span className="ml-2 text-xs text-muted">{item.key}</span>
                    )}
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    {money(item.amount)} <span className="text-xs text-muted">{Math.round(item.share * 100)}%</span>
                  </td>
                  <td className="hidden py-2 pl-4 sm:table-cell">
                    <div className="h-1.5 rounded-full bg-surface-2">
                      <div
                        className="h-1.5 rounded-full bg-accent"
                        style={{ width: `${Math.max(item.share * 100, 1)}%` }}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
