import { toCsv, type CsvValue } from '../lib/csv'
import { workspaceProviders } from '../lib/providers'
import type { Api, CostDimension, ExportRequest } from './types'

const lastDay = (month: string) =>
  new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).toISOString().slice(0, 10)

/** A small CSV made from what the demo shows, in the API's columns (the demo has no bills to read). */
export async function demoCsv(api: Api, tenantId: string, request: ExportRequest): Promise<{ csv: string; rows: number }> {
  let header: string[]
  let rows: CsvValue[][]
  // The demo's cost figures are not split by cloud: rows are the workspace's one provider, or
  // AWS. The API adds provider as the last column of every export (budgets: providers).
  const providers = workspaceProviders(await api.listConnections(tenantId))
  const costProvider = providers.length === 1 ? (providers[0] ?? 'aws') : 'aws'
  if (request.kind === 'costs') {
    const month = request.start.slice(0, 7)
    const current = new Date().toISOString().slice(0, 7)
    const ended = month < current ? month : null
    if (request.granularity === 'day') {
      const daily = await api.costDaily(tenantId, 92, ended)
      header = ['period_start', 'period_end', 'amortised_cost_usd', 'provider']
      rows = daily.points
        .filter((p) => p.date >= request.start && p.date <= request.end)
        .map((p) => [p.date, p.date, p.amount, costProvider])
    } else {
      const group = (request.groupBy ?? ['service']).find((g): g is CostDimension =>
        ['service', 'account', 'region'].includes(g),
      )
      const quote = (await api.fxCurrent(tenantId)).quote
      const currency = (quote?.currency ?? 'USD').toLowerCase()
      const key = group === 'service' ? ['service_code', 'service_name'] : group === 'account' ? ['account_id', 'account_name'] : group ? ['region'] : []
      header = ['period_start', 'period_end', ...key, 'amortised_cost_usd', `amortised_cost_${currency}`, 'fx_rate_avg', 'provider']
      const end = request.end < lastDay(month) ? request.end : lastDay(month)
      const local = (usd: number) => (quote ? usd * quote.rate : null)
      if (group) {
        const breakdown = await api.costBreakdown(tenantId, group, 50, ended)
        rows = breakdown.items
          .filter((i) => i.key !== 'other')
          .map((i) => [request.start, end, ...(key.length === 2 ? [i.key, i.label] : [i.key]), i.amount, local(i.amount), quote?.rate, costProvider])
      } else {
        const summary = await api.costSummary(tenantId, ended)
        const usd = summary.monthToDate?.effective ?? 0
        rows = [[request.start, end, usd, local(usd), quote?.rate, costProvider]]
      }
    }
  } else if (request.kind === 'recommendations') {
    const recs = await api.listRecommendations(tenantId, { status: request.status ?? 'open' })
    header = ['id', 'title', 'rule_id', 'status', 'account_id', 'region', 'resource_id', 'savings_usd_month', 'risk_level', 'confidence', 'evidence_summary', 'provider']
    rows = recs
      .filter((r) => (!request.ruleId || r.ruleId === request.ruleId) && (!request.risk || r.riskLevel === request.risk))
      .map((r) => [
        r.id,
        r.title,
        r.ruleId,
        r.status,
        r.accountId,
        r.regionId,
        r.resourceId,
        r.savingsUsdMonth,
        r.riskLevel,
        r.confidence,
        r.evidence.map((e) => `${String(e.kind)}: ${String(e.value ?? '')}`).join('; '),
        r.provider ?? 'aws',
      ])
  } else if (request.kind === 'anomalies') {
    const anomalies = await api.listAnomalies(tenantId, request.status ?? undefined)
    header = ['date', 'account_id', 'account_name', 'service_name', 'actual_usd', 'expected_usd', 'impact_usd', 'status', 'note', 'provider']
    rows = anomalies.map((a) => [a.date, a.accountId, a.accountName, a.serviceName, a.actualUsd, a.expectedUsd, a.impactUsd, a.status, a.note, a.provider ?? 'aws'])
  } else {
    const budgets = await api.listBudgets(tenantId)
    header = ['name', 'currency', 'amount', 'status', 'actual', 'actual_usd', 'forecast', 'providers']
    rows = budgets.map((b) => [b.name, b.currency, b.amount, b.status, b.actual, b.actualUsd, b.forecast, providers.join(' ')])
  }
  return { csv: toCsv(header, rows), rows: rows.length }
}
