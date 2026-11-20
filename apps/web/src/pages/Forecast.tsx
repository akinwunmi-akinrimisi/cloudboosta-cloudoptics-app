import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { useApi } from '../app/context'
import type { CostChanges, CostContributor, CostForecast } from '../api/types'
import { Card, cx } from '../components/ui'
import { formatMoney } from '../lib/currency'
import { rateTile, useCloudWords, useMoney } from './Workspace'

export const MAX_MOVE = 50

function monthLabel(month: string): string {
  return new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' })
}

function dayLabel(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })
}

function signed(value: number, format: (v: number) => string): string {
  return `${value >= 0 ? '+' : '−'}${format(Math.abs(value))}`
}

/** The scenario rate: ``movement`` percent more local currency per USD (negative: the currency strengthens). */
export function scenarioRate(rate: number, movement: number): number {
  return rate * (1 + movement / 100)
}

/**
 * Forecast with an exchange-rate scenario (PRD US-3.2, FR-ANA-09): this month and the next three in USD, in the
 * workspace currency at today's rate and at a rate moved by up to ±50%, and last month's change split into usage
 * and exchange-rate effects.
 */
export function ForecastPanel({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const { money } = useMoney()
  const [movement, setMovement] = useState(0)
  const words = useCloudWords()
  const forecast = useQuery({ queryKey: ['cost-forecast', tenantId], queryFn: () => api.costForecast(tenantId) })
  const f = forecast.data
  if (!f || f.status === 'no_data' || !f.monthEnd) return null
  const ccy = f.currency
  const quote = f.quote && ccy !== 'USD' ? f.quote : null
  const local = (usd: number, rate: number) => `${quote?.estimated ? '≈' : ''}${formatMoney(usd * rate, ccy)}`
  const rows = [
    { month: f.monthEnd.month, amount: f.monthEnd.forecast.amount, low: f.monthEnd.low, high: f.monthEnd.high, note: 'month end' },
    ...f.months.map((m) => ({ ...m, note: m.method === 'trend' ? 'trend' : 'run rate' })),
  ]
  const moved = quote ? scenarioRate(quote.rate, movement) : null
  const rate = rateTile(ccy, { currency: ccy, basis: f.basis, quote: f.quote })
  const fm = f.monthEnd.forecast
  return (
    <Card className="p-5">
      <div className="mb-4">
        <h2 className="text-base font-semibold">Forecast</h2>
        <p className="text-xs text-muted">
          This month: month to date + the last {fm.runRateDays} days&apos; average ({money(fm.dailyRunRate)} a day) for the{' '}
          {fm.daysInMonth - fm.daysCovered} days left
          {fm.monthlyCharges > 0 && ` + ${money(fm.monthlyCharges)} of monthly charges still to be billed`}. Next months: the
          trend over the last {f.trendDays} days
          {f.monthlyChargesUsd > 0 && `, with ${money(f.monthlyChargesUsd)} a month of once-a-month charges such as ${words.awsOnly ? 'AWS Support' : 'support plans'}`}
          {f.taxShare > 0 && ` and tax at ${(f.taxShare * 100).toFixed(1)}%`}. Amortised, after credits.
        </p>
      </div>
      {quote && moved !== null ? (
        <div className="mb-4 flex flex-col gap-2 rounded-sm bg-surface-2 p-3 text-sm">
          <label htmlFor="fx-move" className="font-medium">
            Currency scenario: {ccy} {movement === 0 ? 'unchanged' : `${Math.abs(movement)}% ${movement > 0 ? 'weaker' : 'stronger'}`}
          </label>
          <div className="flex items-center gap-3">
            <input
              id="fx-move"
              type="range"
              min={-MAX_MOVE}
              max={MAX_MOVE}
              step={1}
              value={movement}
              onChange={(e) => setMovement(Number(e.target.value))}
              className="w-full accent-accent"
            />
            <input
              type="number"
              aria-label="Movement in percent"
              min={-MAX_MOVE}
              max={MAX_MOVE}
              value={movement}
              onChange={(e) => setMovement(Math.max(-MAX_MOVE, Math.min(MAX_MOVE, Number(e.target.value) || 0)))}
              className="h-8 w-20 rounded-sm border border-border-strong bg-surface-1 px-2 text-right tabular-nums"
            />
            <span className="text-secondary">%</span>
          </div>
          <p className="text-xs text-secondary">
            {rate.value} today ({rate.note}); scenario 1 USD = {formatMoney(moved, ccy)}. Positive: {ccy} buys fewer dollars,
            so the bill costs more in {ccy}.
          </p>
        </div>
      ) : (
        <p className="mb-4 text-xs text-secondary">{ccy === 'USD' ? 'Amounts in US dollars.' : rate.note}</p>
      )}
      <div className="overflow-x-auto" role="region" aria-label="Forecast by month" tabIndex={0}>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted">
            <th className="py-2 font-medium">Month</th>
            <th className="py-2 text-right font-medium">USD</th>
            <th className="hidden py-2 text-right font-medium sm:table-cell">Range</th>
            {quote && <th className="py-2 text-right font-medium">{ccy} today</th>}
            {quote && <th className="py-2 text-right font-medium">{ccy} scenario</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.month} className="border-b border-border last:border-0">
              <td className="py-2">
                {monthLabel(r.month)} <span className="text-xs text-muted">{r.note}</span>
              </td>
              <td className="py-2 text-right tabular-nums">{formatMoney(r.amount, 'USD')}</td>
              <td className="hidden py-2 text-right text-xs text-muted tabular-nums sm:table-cell">
                {formatMoney(r.low, 'USD', { compact: true })}–{formatMoney(r.high, 'USD', { compact: true })}
              </td>
              {quote && <td className="py-2 text-right tabular-nums">{local(r.amount, quote.rate)}</td>}
              {quote && moved !== null && (
                <td className={cx('py-2 text-right tabular-nums', movement !== 0 && 'font-medium')}>{local(r.amount, moved)}</td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      {f.decomposition && <FxSplit d={f.decomposition} currency={ccy} />}
    </Card>
  )
}

function FxSplit({ d, currency }: { d: NonNullable<CostForecast['decomposition']>; currency: string }) {
  const usd = (v: number) => formatMoney(v, 'USD')
  const loc = (v: number) => formatMoney(v, currency)
  const change = d.usd - d.previousUsd
  return (
    <div className="mt-4 border-t border-border pt-3 text-sm">
      <p className="font-medium">
        {monthLabel(d.month)} against {monthLabel(d.previousMonth)}
      </p>
      <p className="text-secondary">
        {usd(d.usd)} against {usd(d.previousUsd)} ({signed(change, usd)}).
        {currency !== 'USD' && d.usageEffect !== null && d.fxEffect !== null && d.local !== null && d.previousLocal !== null
          ? ` In ${currency}: ${loc(d.local)} against ${loc(d.previousLocal)}, ${signed(d.usageEffect, loc)} from usage change and ${signed(d.fxEffect, loc)} from FX change (each day at its own rate; average ${d.previousRate?.toLocaleString('en-US', { maximumFractionDigits: 4 })} then ${d.rate?.toLocaleString('en-US', { maximumFractionDigits: 4 })} ${currency} per USD).`
          : currency !== 'USD'
            ? ` No ${currency} rate for every day of both months, so no ${currency} split.`
            : ''}
      </p>
    </div>
  )
}

/** FR-ANA-12's sentence in the viewer's currency. */
export function changeSentence(c: CostContributor, money: (usd: number) => string): string {
  const where = c.region ? `${c.serviceName} in ${c.region}` : c.serviceName
  const pct = c.changePercent === null ? 'new this month' : `${c.changePercent > 0 ? '+' : ''}${c.changePercent.toFixed(0)}%`
  const why = c.usageType ? ` mainly due to ${c.usageType}` : ''
  return `${where} ${c.changeUsd > 0 ? 'rose' : 'fell'} ${money(Math.abs(c.changeUsd))} (${pct})${why}.`
}

/** What changed (PRD FR-ANA-12): month to date against the same days last month, biggest rises and falls. */
/** Month to date against the same days last month; for an ended `month`, the whole month against the month before. */
export function WhatChanged({ tenantId, month = null }: { tenantId: string; month?: string | null }) {
  const api = useApi()
  const { money } = useMoney()
  const changes = useQuery({
    queryKey: ['cost-changes', tenantId, month],
    queryFn: () => api.costChanges(tenantId, month),
  })
  const w: CostChanges | undefined = changes.data
  if (!w || w.status === 'no_data' || !w.current || !w.previous) return null
  const pct = w.previousUsd > 0 ? ` (${w.changeUsd >= 0 ? '+' : ''}${Math.round((100 * w.changeUsd) / w.previousUsd)}%)` : ''
  return (
    <Card className="p-5">
      <h2 className="text-base font-semibold">What changed</h2>
      <p className="mb-3 text-xs text-muted">
        {dayLabel(w.current.from)}–{dayLabel(w.current.to)} against {dayLabel(w.previous.from)}–{dayLabel(w.previous.to)} ·
        usage and purchases by service and region: {money(w.usd)}, {signed(w.changeUsd, money)}
        {pct}
      </p>
      {w.rises.length === 0 && w.falls.length === 0 ? (
        <p className="text-sm text-secondary">No service changed by more than USD 1.</p>
      ) : (
        <ul className="flex flex-col gap-2 text-sm">
          {[...w.rises, ...w.falls].map((c) => (
            <li key={`${c.serviceCode}:${c.region ?? ''}`} className="flex gap-2">
              <span aria-hidden className={cx('font-semibold', c.changeUsd > 0 ? 'text-critical-ink' : 'text-good-ink')}>
                {c.changeUsd > 0 ? '▲' : '▼'}
              </span>
              <span>{changeSentence(c, money)}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
