import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

import AxeBuilder from '@axe-core/playwright'
import type { Page } from '@playwright/test'
import { expect, test } from '@playwright/test'

import { API_URL, appPath, openSignedIn, seedSupportGrant, seedWorkspace, signInApi, uniqueEmail } from './support'

// axe-core on each main page with real layout (docs/11f §1.1 "Accessibility"), on a few
// opened states (inline confirmations, the mobile menu) and at a phone width. Any violation
// fails the test (FE-12, WCAG 2.2 AA); E2E_A11Y_STRICT=0 only reports, for investigating.
// Output, per browser project: reports/a11y/<project>/summary.md (desktop), phone.md and one
// JSON file per page, uploaded with the HTML report in CI. The staff console (/staff) is
// included at both widths. What axe cannot check (keyboard, focus, motion, layout) is in keyboard.spec.ts and
// mobile.spec.ts.

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']
const REPORTS = path.join(import.meta.dirname, '..', 'reports', 'a11y')
const STRICT = process.env.E2E_A11Y_STRICT !== '0'
const PHONE = { width: 390, height: 844 }

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
    `# Accessibility report (axe-core, E2E stack, ${test.info().project.name})`,
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

// [name, path, path as reported, h1]
function mainPages(recommendationId: string): [string, string, string, RegExp | string][] {
  return [
    ['Overview', '/overview', '/overview', 'Overview'],
    ['Cost Explorer', '/costs', '/costs', 'Cost Explorer'],
    ['Recommendations', '/recommendations', '/recommendations', 'Recommendations'],
    ['Recommendation detail', `/recommendations/${recommendationId}`, '/recommendations/:id', /./],
    ['Anomalies', '/anomalies', '/anomalies', 'Anomalies'],
    ['Budgets', '/budgets', '/budgets', 'Budgets'],
    ['Commitments', '/commitments', '/commitments', /Commitments|Savings Plans/],
    ['Reports', '/reports', '/reports', /Report/],
    ['Connections', '/settings/connections', '/settings/connections', 'Connections'],
    ['Settings', '/settings', '/settings', 'Settings'],
    ['New workspace', '/onboarding?new=true', '/onboarding?new=true', 'Create another workspace'],
  ]
}

/** Writes reports/a11y/<project>/<part>.md and a JSON file per page, and fails on any violation. */
async function report(part: string, results: PageResult[]) {
  // One folder per browser project (reports/a11y/chromium, reports/a11y/webkit).
  const out = path.join(REPORTS, test.info().project.name)
  await mkdir(out, { recursive: true })
  for (const r of results) {
    await writeFile(path.join(out, `${r.name.toLowerCase().replace(/\W+/g, '-')}.json`), JSON.stringify(r, null, 2))
  }
  const text = summary(results)
  await writeFile(path.join(out, `${part}.md`), text)
  await test.info().attach(`a11y-${part}.md`, { body: text, contentType: 'text/markdown' })
  const total = results.reduce((n, r) => n + r.violations.length, 0)
  console.log(`axe (${part}): ${total} violation(s) across ${results.length} pages; see reports/a11y/${test.info().project.name}/${part}.md`)
  if (STRICT) expect(total, text).toBe(0)
}

test('axe on each main page', async ({ page, browser, request }) => {
  test.setTimeout(300_000)
  const ws = await seedWorkspace(request)
  const recs = await ws.api.get<{ id: string }[]>(`/v1/tenants/${ws.tenantId}/recommendations`)
  const results: PageResult[] = []

  // Signed out, and a new user's first step.
  const fresh = await browser.newContext()
  const anon = await fresh.newPage()
  await anon.goto(appPath('/sign-in'))
  await expect(anon.getByRole('heading', { name: 'Sign in' })).toBeVisible()
  results.push(await audit(anon, 'Sign in', '/sign-in'))
  await anon.goto(appPath('/sign-in?intent=signup&utm_source=e2e'))
  await expect(anon.getByRole('heading', { name: 'Create your account' })).toBeVisible()
  results.push(await audit(anon, 'Create your account', '/sign-in?intent=signup'))
  const newcomer = await signInApi(request, uniqueEmail('newcomer'))
  await openSignedIn(anon, newcomer.token, undefined, '/onboarding')
  await expect(anon.getByRole('heading', { name: 'Create your workspace' })).toBeVisible()
  results.push(await audit(anon, 'Onboarding: create workspace', '/onboarding'))
  await anon.getByLabel('Company or team name').fill('A11y Apps')
  await anon.getByRole('button', { name: 'Create workspace' }).click()
  await expect(anon.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
  await anon.waitForLoadState('networkidle')
  results.push(await audit(anon, 'Onboarding: connect AWS', '/onboarding (step 2)'))
  await anon.getByText('What Cloudoptics can read').click()
  await expect(anon.getByText('Connection health')).toBeVisible()
  results.push(await audit(anon, 'Onboarding: permissions open', '/onboarding (step 2, permissions)'))
  // The public pages sit at the origin's root, next to the app under /app/ (SITE-13, SITE-16).
  await expect(anon.getByRole('link', { name: /Read every permission/ })).toHaveAttribute('href', '/security#permissions')
  await expect(anon.getByRole('link', { name: /What will this create\?/ })).toHaveAttribute(
    'href',
    '/how-it-works#what-the-stack-creates',
  )
  await fresh.close()

  // The internal staff console (break-glass access), with a grant listed.
  const { staff } = await seedSupportGrant(request)
  const staffContext = await browser.newContext()
  const console_ = await staffContext.newPage()
  await openSignedIn(console_, staff.token, undefined, '/staff')
  await expect(console_.getByRole('heading', { level: 1, name: 'Support access' })).toBeVisible()
  await expect(console_.getByRole('button', { name: 'End access' }).first()).toBeVisible()
  results.push(await audit(console_, 'Staff console', '/staff'))
  await staffContext.close()

  const pages = mainPages(recs[0].id)
  await openSignedIn(page, ws.api.token, ws.tenantId, '/overview')
  for (const [name, url, shown, heading] of pages) {
    await test.step(name, async () => {
      await page.goto(appPath(url))
      await expect(page.getByRole('heading', { level: 1, name: heading }).first()).toBeVisible()
      await page.waitForLoadState('networkidle')
      results.push(await audit(page, name, shown))
    })
  }

  // Opened states: inline panels that take focus and close with Escape.
  await test.step('Recommendations: bulk dismiss form', async () => {
    await page.goto(appPath('/recommendations'))
    await page.getByRole('checkbox', { name: /^Select / }).first().check()
    await page.getByRole('button', { name: 'Dismiss…' }).click()
    await expect(page.getByLabel(/Why are you dismissing/)).toBeFocused()
    results.push(await audit(page, 'Recommendations: dismiss form open', '/recommendations (dismissing)'))
  })
  await test.step('Connections: reload bills panel', async () => {
    await page.goto(appPath('/settings/connections'))
    await page.getByRole('button', { name: 'Reload bills…' }).click()
    await expect(page.getByRole('button', { name: 'Reload', exact: true })).toBeVisible()
    results.push(await audit(page, 'Connections: reload panel open', '/settings/connections (reload)'))
  })
  await test.step('Settings: delete workspace confirmation', async () => {
    await page.goto(appPath('/settings'))
    await page.getByRole('button', { name: 'Delete workspace…' }).click()
    await expect(page.getByLabel(/Type the workspace name/)).toBeFocused()
    results.push(await audit(page, 'Settings: delete confirmation open', '/settings (danger zone)'))
  })
  await test.step('Charts as tables', async () => {
    await page.goto(appPath('/overview'))
    await page.getByText('Show daily spend as a table').click()
    await expect(page.getByRole('table', { name: 'Daily spend' })).toBeVisible()
    results.push(await audit(page, 'Overview: chart table open', '/overview (table)'))
  })

  await report('summary', results)
})

// The other clouds' connect steps, in a test of their own so it runs beside the one above
// (axe is slow on WebKit, and that test is close to its time limit there).
test('axe on the Google Cloud, Azure and DigitalOcean connect steps', async ({ page, request }) => {
  test.setTimeout(180_000)
  const results: PageResult[] = []
  const newcomer = await signInApi(request, uniqueEmail('clouds'))
  await openSignedIn(page, newcomer.token, undefined, '/onboarding')
  await page.getByLabel('Company or team name').fill('A11y Clouds')
  await page.getByRole('button', { name: 'Create workspace' }).click()
  await expect(page.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
  // The other clouds' connect steps, from the same picker.
  for (const [cloud, heading] of [
    ['Google Cloud', 'Connect Google Cloud'],
    ['Azure', 'Connect your Azure subscription'],
    ['DigitalOcean', 'Connect DigitalOcean'],
  ]) {
    await page.getByRole('radio', { name: new RegExp(`^${cloud}`) }).check()
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible()
    await page.waitForLoadState('networkidle')
    results.push(await audit(page, `Onboarding: connect ${cloud}`, `/onboarding (step 2, ${cloud})`))
  }
  // Google Cloud's second page: the commands to run (a fake billing account of its own).
  const fake = await request.post(`${API_URL}/__e2e/gcp/billing-account`)
  expect(fake.ok()).toBeTruthy()
  const gcp = (await fake.json()) as { billingAccountId: string; project: string; dataset: string }
  await page.getByRole('radio', { name: /^Google Cloud/ }).check()
  await page.getByLabel(/Billing account ID/).fill(gcp.billingAccountId)
  await page.getByLabel(/Project that holds the export/).fill(gcp.project)
  await page.getByLabel(/^Dataset/).first().fill(gcp.dataset)
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Give Cloudoptics read access' })).toBeVisible()
  results.push(await audit(page, 'Onboarding: Google Cloud commands', '/onboarding (step 2, Google Cloud commands)'))

  await report('connect', results)
})

// The same pages on a phone, and the navigation drawer open: a test of its own so the two run
// in parallel (axe is slower on WebKit).
test('axe on each main page at 390 px', async ({ browser, request }) => {
  test.setTimeout(300_000)
  const ws = await seedWorkspace(request)
  const recs = await ws.api.get<{ id: string }[]>(`/v1/tenants/${ws.tenantId}/recommendations`)
  const pages = mainPages(recs[0].id)
  const results: PageResult[] = []
  const phone = await browser.newContext({ viewport: PHONE, isMobile: true, hasTouch: true })
  const mobile = await phone.newPage()
  await openSignedIn(mobile, ws.api.token, ws.tenantId, '/overview')
  for (const [name, url, shown, heading] of pages) {
    await test.step(`${name} (phone)`, async () => {
      await mobile.goto(appPath(url))
      await expect(mobile.getByRole('heading', { level: 1, name: heading }).first()).toBeVisible()
      await mobile.waitForLoadState('networkidle')
      results.push(await audit(mobile, `${name} (390 px)`, shown))
    })
  }
  await test.step('Menu drawer (phone)', async () => {
    await mobile.goto(appPath('/overview'))
    await mobile.getByRole('button', { name: 'Open menu' }).click()
    await expect(mobile.getByRole('dialog', { name: 'Cloudoptics' })).toBeVisible()
    results.push(await audit(mobile, 'Menu open (390 px)', '/overview (menu)'))
  })
  await phone.close()
  const { staff } = await seedSupportGrant(request)
  await test.step('Staff console (phone)', async () => {
    const context = await browser.newContext({ viewport: PHONE, isMobile: true, hasTouch: true })
    const p = await context.newPage()
    await openSignedIn(p, staff.token, undefined, '/staff')
    await expect(p.getByRole('heading', { level: 1, name: 'Support access' })).toBeVisible()
    await expect(p.getByRole('button', { name: 'End access' }).first()).toBeVisible()
    results.push(await audit(p, 'Staff console (390 px)', '/staff'))
    await context.close()
  })

  await report('phone', results)
})
