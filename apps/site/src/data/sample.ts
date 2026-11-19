// Sample figures shown on the landing page. They match the demo workspace (demo tenant "Kola Pay",
// apps/web/src/api/demo.ts DEMO_RECS: title, saving = costNow - costAfter, riskScore) and are
// always labelled "Sample data". Update them when the demo data changes.
export const sample = {
  tenant: 'Kola Pay',
  hero: [
    { label: 'Forecast for the month', usd: 1237.8 },
    { label: 'Savings found, a month', usd: 663.75, highlight: true },
  ],
  /** Five savings from the demo, then the rest, adding up to the hero's savings. */
  ledger: [
    { title: 'Upgrade reports-db off PostgreSQL 12 Extended Support', risk: 60, usd: 288 },
    { title: 'Delete unattached volume build-cache, 500 GiB', risk: 30, usd: 55 },
    { title: 'Delete idle NAT gateway, no traffic in 30 days', risk: 0, usd: 43.8, highlight: true },
    { title: 'Delete idle load balancer staging-old-alb', risk: 0, usd: 21.39 },
    { title: 'Change db-data from gp2 to gp3', risk: 25, usd: 11.2 },
  ],
  /** The saving followed through to the bill in "Verified savings": the build-cache volume. */
  rec: { usd: 55, check: 'volume deleted' },
}

const total = sample.hero.find((f) => f.highlight)!.usd
export const ledgerOther = Math.round((total - sample.ledger.reduce((n, l) => n + l.usd, 0)) * 100) / 100
export const ledgerTotal = total
if (ledgerOther < 0) throw new Error('sample.ts: the ledger lines add up to more than the savings total')
