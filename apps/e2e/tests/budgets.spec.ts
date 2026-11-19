import { expect, test } from '@playwright/test'

import { openSignedIn, seedWorkspace } from './support'

// J4 (docs/11f §1.4): a budget in naira, then bills that overspend it, then the alert, once.
// Without a Postmark token the API logs the email instead of sending it; the thresholds are
// still recorded as alerted, which is what the page shows.

test('a naira budget alerts when new bills cross its thresholds', async ({ page, request }) => {
  // Connected, but AWS has not delivered a bill yet.
  const ws = await seedWorkspace(request, { bills: false, scan: false })
  await openSignedIn(page, ws.api.token, ws.tenantId, '/budgets')
  await expect(page.getByRole('heading', { name: 'Budgets' })).toBeVisible()
  const thresholds = page.getByRole('list', { name: 'Thresholds' })

  await test.step('create the budget', async () => {
    await page.getByRole('button', { name: 'New budget' }).click()
    await page.getByLabel('Name', { exact: true }).fill('Platform team')
    await page.getByLabel('Amount a month').fill('500000')
    // The workspace's local currency (Nigeria: NGN) is offered next to USD.
    await page.getByLabel('Budget currency').selectOption('NGN')
    await page.getByLabel('Threshold 1 percent').fill('50')
    await page.getByLabel('Threshold 1 measure').selectOption('actual')
    await page.getByRole('checkbox', { name: 'Ada Admin' }).check()
    await page.getByRole('button', { name: 'Create budget' }).click()
    await expect(page.getByText('Platform team', { exact: true })).toBeVisible()
    // 500,000 NGN at the staff rate of 1,550 per USD.
    await expect(page.getByText('NGN 500,000 a month (USD 322.58) · whole workspace')).toBeVisible()
    await expect(thresholds.getByText(/^50% of spend$/)).toBeVisible()
  })

  await test.step('the first bills arrive and overspend it', async () => {
    // New bills trigger the budget check; inline here, so the load runs it straight away.
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Connections' }).click()
    await page.getByRole('button', { name: 'Reload bills…' }).click()
    await page.getByRole('button', { name: 'Reload', exact: true }).click()
    await expect(page.getByText(/Bills reloaded/)).toBeVisible({ timeout: 30_000 })
    // A fresh load: the page may still hold the budgets it read before the bills came.
    await page.goto('/budgets')
    await expect(page.getByText('Over budget')).toBeVisible()
    await expect(page.getByText(/^Spent NGN [\d,.]+ \([\d.]+%\) · USD [\d,.]+/)).toBeVisible()
    await expect(thresholds.getByText(/^50% of spend · alerted$/)).toBeVisible()
    await expect(thresholds.getByText(/^100% of spend · alerted$/)).toBeVisible()
  })
})
