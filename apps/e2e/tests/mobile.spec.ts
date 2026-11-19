import type { Page } from '@playwright/test'
import { expect, test } from '@playwright/test'

import { appPath, openSignedIn, seedSupportGrant, seedWorkspace, signInApi, uniqueEmail } from './support'

// Mobile layouts (docs/08 breakpoints 375-414 and 768; FE-12): no sideways page scroll on
// the main pages (a wide table scrolls inside its own box), and the navigation works on a
// phone. Touch-target size (WCAG 2.5.8, 24 px) is checked by axe at 390 px in a11y.spec.ts.

const PAGES: [string, string][] = [
  ['/overview', 'Overview'],
  ['/costs', 'Cost Explorer'],
  ['/recommendations', 'Recommendations'],
  ['/anomalies', 'Anomalies'],
  ['/budgets', 'Budgets'],
  ['/commitments', 'Commitments'],
  ['/reports', 'Report'],
  ['/settings/connections', 'Connections'],
  ['/settings', 'Settings'],
]

async function overflow(page: Page) {
  return page.evaluate(() => {
    const doc = document.documentElement
    const width = doc.clientWidth
    // What sticks out, for the failure message.
    const wide = [...document.querySelectorAll<HTMLElement>('main *, header *')]
      .filter((el) => {
        const r = el.getBoundingClientRect()
        if (r.width === 0 || r.right <= width + 1) return false
        // Inside a box that scrolls sideways itself: fine.
        for (let p = el.parentElement; p && p !== doc; p = p.parentElement) {
          const o = getComputedStyle(p).overflowX
          if (o === 'auto' || o === 'scroll' || o === 'hidden' || o === 'clip') return false
        }
        return true
      })
      .slice(0, 5)
      .map((el) => `${el.tagName.toLowerCase()}.${String(el.className).split(' ').slice(0, 3).join('.')} (right ${Math.round(el.getBoundingClientRect().right)})`)
    return { scrollWidth: doc.scrollWidth, width, wide }
  })
}

for (const width of [375, 390, 768]) {
  test(`no sideways page scroll at ${width} px`, async ({ browser, request }) => {
    test.setTimeout(120_000)
    const ws = await seedWorkspace(request)
    const recs = await ws.api.get<{ id: string }[]>(`/v1/tenants/${ws.tenantId}/recommendations`)
    const context = await browser.newContext({ viewport: { width, height: 844 }, isMobile: width < 768, hasTouch: width < 768 })
    const page = await context.newPage()
    await openSignedIn(page, ws.api.token, ws.tenantId, '/overview')
    for (const [url, heading] of [...PAGES, [`/recommendations/${recs[0].id}`, /./] as [string, RegExp]]) {
      await test.step(url, async () => {
        await page.goto(appPath(url))
        await expect(page.getByRole('heading', { level: 1, name: heading }).first()).toBeVisible()
        await page.waitForLoadState('networkidle')
        const o = await overflow(page)
        expect(o.scrollWidth, `${url} at ${width} px is ${o.scrollWidth} px wide: ${o.wide.join(', ')}`).toBeLessThanOrEqual(o.width)
      })
    }
    await context.close()
  })
}

test('the staff console has no sideways scroll at 320, 375, 390 and 768 px', async ({ browser, request }) => {
  const { staff } = await seedSupportGrant(request)
  // 320 px too: an internal tool, but the smallest phones should still fit it.
  for (const width of [320, 375, 390, 768]) {
    await test.step(`${width} px`, async () => {
      const context = await browser.newContext({ viewport: { width, height: 844 }, isMobile: width < 768, hasTouch: width < 768 })
      const page = await context.newPage()
      await openSignedIn(page, staff.token, undefined, '/staff')
      await expect(page.getByRole('heading', { level: 1, name: 'Support access' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'End access' }).first()).toBeVisible()
      const o = await overflow(page)
      expect(o.scrollWidth, `/staff at ${width} px is ${o.scrollWidth} px wide: ${o.wide.join(', ')}`).toBeLessThanOrEqual(o.width)
      await context.close()
    })
  }
})

test('on a phone the menu opens, navigates and closes', async ({ browser, request }) => {
  const ws = await seedWorkspace(request, { bills: false, scan: false })
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
  const page = await context.newPage()
  await openSignedIn(page, ws.api.token, ws.tenantId, '/overview')
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
  // The sidebar is not shown at this width; the menu button is.
  await expect(page.getByRole('navigation', { name: 'Main' })).toBeHidden()
  const menu = page.getByRole('button', { name: 'Open menu' })
  await expect(menu).toHaveAttribute('aria-expanded', 'false')
  await menu.tap()
  const drawer = page.getByRole('dialog', { name: 'Cloudoptics' })
  await expect(drawer).toBeVisible()
  await drawer.getByRole('link', { name: 'Budgets' }).tap()
  await expect(drawer).toBeHidden()
  await expect(page.getByRole('heading', { level: 1, name: 'Budgets' })).toBeVisible()
  await expect(page).toHaveTitle('Budgets · Cloudoptics')
  // The backdrop closes it too.
  await menu.tap()
  await expect(drawer).toBeVisible()
  await page.mouse.click(380, 400)
  await expect(drawer).toBeHidden()
  await context.close()
})

test('a member opens New workspace from the phone menu, and it fits 320 px', async ({ browser, request }) => {
  const ws = await seedWorkspace(request, { bills: false, scan: false })
  const context = await browser.newContext({ viewport: { width: 320, height: 640 }, isMobile: true, hasTouch: true })
  const page = await context.newPage()
  await openSignedIn(page, ws.api.token, ws.tenantId, '/overview')
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
  await page.getByRole('button', { name: 'Open menu' }).tap()
  await page.getByRole('dialog', { name: 'Cloudoptics' }).getByRole('link', { name: 'New workspace' }).tap()
  await expect(page.getByRole('heading', { level: 1, name: 'Create another workspace' })).toBeVisible()
  const o = await overflow(page)
  expect(o.scrollWidth, `New workspace at 320 px is ${o.scrollWidth} px wide: ${o.wide.join(', ')}`).toBeLessThanOrEqual(o.width)
  // Back to the workspace without creating anything.
  await page.getByRole('link', { name: /^Back to / }).tap()
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
  await context.close()
})

test('onboarding fits a phone: create the workspace, then connect AWS', async ({ browser, request }) => {
  const newcomer = await signInApi(request, uniqueEmail('phone'))
  const context = await browser.newContext({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true })
  const page = await context.newPage()
  await openSignedIn(page, newcomer.token, undefined, '/onboarding')
  await expect(page.getByRole('heading', { name: 'Create your workspace' })).toBeVisible()
  let o = await overflow(page)
  expect(o.scrollWidth, `create workspace: ${o.wide.join(', ')}`).toBeLessThanOrEqual(o.width)
  await page.getByLabel('Company or team name').fill('Phone Co')
  await page.getByRole('button', { name: 'Create workspace' }).tap()
  await expect(page.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
  await page.waitForLoadState('networkidle')
  o = await overflow(page)
  expect(o.scrollWidth, `connect AWS: ${o.wide.join(', ')}`).toBeLessThanOrEqual(o.width)
  // The permission list (long action names) wraps, down to 320 px.
  await page.getByText('What Cloudoptics can read').tap()
  await expect(page.getByText('cloudformation:ListStackSetOperationResults')).toBeVisible()
  await page.setViewportSize({ width: 320, height: 640 })
  o = await overflow(page)
  expect(o.scrollWidth, `permissions at 320 px: ${o.wide.join(', ')}`).toBeLessThanOrEqual(o.width)
  await context.close()
})
