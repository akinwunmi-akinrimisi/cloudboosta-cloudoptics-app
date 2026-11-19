import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

import AxeBuilder from '@axe-core/playwright'
import type { Page } from '@playwright/test'
import { expect, test } from '@playwright/test'

import { openSignedIn, seedWorkspace, signInApi, uniqueEmail } from './support'

// axe-core on each main page with real layout (docs/11f §1.1 "Accessibility"). It reports
// and does not fail: the accessibility pass comes next. E2E_A11Y_STRICT=1 makes any
// violation fail, for when that pass is done. Output: reports/a11y/summary.md and one JSON
// file per page, uploaded with the HTML report in CI.

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']
const OUT = path.join(import.meta.dirname, '..', 'reports', 'a11y')

interface PageResult {
  name: string
  path: string
  violations: { id: string; impact: string | null; help: string; helpUrl: string; nodes: number; targets: string[] }[]
}

async function audit(page: Page, name: string, url: string): Promise<PageResult> {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
  return {
    name,
    path: url,
    violations: results.violations.map((v) => ({
      id: v.id,
      impact: v.impact ?? null,
      help: v.help,
      helpUrl: v.helpUrl,
      nodes: v.nodes.length,
      targets: v.nodes.slice(0, 5).map((n) => n.target.join(' ')),
    })),
  }
}

function summary(results: PageResult[]): string {
  const lines = [
    '# Accessibility report (axe-core, E2E stack)',
    '',
    `Rules: ${TAGS.join(', ')}. Generated ${new Date().toISOString()}.`,
    '',
    '| Page | Path | Violations | Elements |',
    '|---|---|---|---|',
    ...results.map(
      (r) => `| ${r.name} | \`${r.path}\` | ${r.violations.length} | ${r.violations.reduce((n, v) => n + v.nodes, 0)} |`,
    ),
    '',
  ]
  for (const r of results.filter((x) => x.violations.length > 0)) {
    lines.push(`## ${r.name} (\`${r.path}\`)`, '')
    for (const v of r.violations) {
      lines.push(`- **${v.id}** (${v.impact ?? 'n/a'}, ${v.nodes} element(s)): ${v.help}. ${v.helpUrl}`)
      for (const t of v.targets) lines.push(`  - \`${t}\``)
    }
    lines.push('')
  }
  return lines.join('\n')
}

test('axe on each main page', async ({ page, browser, request }) => {
  test.setTimeout(180_000)
  const ws = await seedWorkspace(request)
  const recs = await ws.api.get<{ id: string }[]>(`/v1/tenants/${ws.tenantId}/recommendations`)
  const results: PageResult[] = []

  // Signed out, and a new user's first step.
  const fresh = await browser.newContext()
  const anon = await fresh.newPage()
  await anon.goto('/sign-in')
  await expect(anon.getByRole('heading', { name: 'Sign in' })).toBeVisible()
  results.push(await audit(anon, 'Sign in', '/sign-in'))
  const newcomer = await signInApi(request, uniqueEmail('newcomer'))
  await openSignedIn(anon, newcomer.token, undefined, '/onboarding')
  await expect(anon.getByRole('heading', { name: 'Create your workspace' })).toBeVisible()
  results.push(await audit(anon, 'Onboarding: create workspace', '/onboarding'))
  await fresh.close()

  // [name, path, path as reported, h1]
  const pages: [string, string, string, RegExp | string][] = [
    ['Overview', '/overview', '/overview', 'Overview'],
    ['Cost Explorer', '/costs', '/costs', 'Cost Explorer'],
    ['Recommendations', '/recommendations', '/recommendations', 'Recommendations'],
    ['Recommendation detail', `/recommendations/${recs[0].id}`, '/recommendations/:id', /./],
    ['Anomalies', '/anomalies', '/anomalies', 'Anomalies'],
    ['Budgets', '/budgets', '/budgets', 'Budgets'],
    ['Commitments', '/commitments', '/commitments', /Commitments|Savings Plans/],
    ['Reports', '/reports', '/reports', /Report/],
    ['Connections', '/settings/connections', '/settings/connections', 'Connections'],
    ['Settings', '/settings', '/settings', 'Settings'],
  ]
  await openSignedIn(page, ws.api.token, ws.tenantId, '/overview')
  for (const [name, url, shown, heading] of pages) {
    await test.step(name, async () => {
      await page.goto(url)
      await expect(page.getByRole('heading', { level: 1, name: heading }).first()).toBeVisible()
      await page.waitForLoadState('networkidle')
      results.push(await audit(page, name, shown))
    })
  }

  await mkdir(OUT, { recursive: true })
  for (const r of results) {
    await writeFile(path.join(OUT, `${r.name.toLowerCase().replace(/\W+/g, '-')}.json`), JSON.stringify(r, null, 2))
  }
  const report = summary(results)
  await writeFile(path.join(OUT, 'summary.md'), report)
  await test.info().attach('a11y-summary.md', { body: report, contentType: 'text/markdown' })
  const total = results.reduce((n, r) => n + r.violations.length, 0)
  console.log(`axe: ${total} violation(s) across ${results.length} pages; see reports/a11y/summary.md`)
  if (process.env.E2E_A11Y_STRICT === '1') expect(total, report).toBe(0)
})
