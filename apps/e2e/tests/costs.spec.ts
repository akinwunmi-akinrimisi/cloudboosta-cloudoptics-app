import { expect, test } from '@playwright/test'

import { ngn, openSignedIn, seedWorkspace, usd } from './support'

// J2 and the currency toggle (docs/11f §1.4): the totals the API computed from the fixture
// bills, shown in USD, in naira at the staff rate, and both.

test('Overview and Cost Explorer show the bills in USD and in naira', async ({ page, request }) => {
  const ws = await seedWorkspace(request, { scan: false })
  const summary = await ws.api.get<{ monthToDate: { effective: number }; previousMonth: number }>(
    `/v1/tenants/${ws.tenantId}/costs/summary`,
  )
  const mtd = summary.monthToDate.effective
  expect(mtd).toBeGreaterThan(0)

  await openSignedIn(page, ws.api.token, ws.tenantId)
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
  await expect(page.getByText(`${ws.name} · NGN · Africa/Lagos`)).toBeVisible()
  const tile = page.getByText('Month-to-date spend', { exact: true }).locator('xpath=following-sibling::p[1]')
  await expect(tile).toHaveText(`${usd(mtd)} · ${ngn(mtd)}`)
  await expect(page.getByText(/Official rate set by Cloudoptics staff/)).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Daily spend' })).toBeVisible()

  const currency = page.getByRole('group', { name: 'Currency' })
  await currency.getByRole('button', { name: 'USD' }).click()
  await expect(tile).toHaveText(usd(mtd))
  await currency.getByRole('button', { name: 'Local' }).click()
  await expect(tile).toHaveText(ngn(mtd))
  await currency.getByRole('button', { name: 'Both' }).click()
  await expect(tile).toHaveText(`${usd(mtd)} · ${ngn(mtd)}`)

  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Cost Explorer' }).click()
  await expect(page.getByRole('heading', { name: 'Cost Explorer' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /^Spend by service/ })).toBeVisible()
  const ec2 = page.getByRole('row', { name: /^AmazonEC2 / })
  await expect(ec2).toHaveText(/\$[\d,]+\.\d{2} · ₦[\d,]+\.\d{2}/)
  await expect(page.getByText('1 USD = ₦1,550.00 today', { exact: false })).toBeVisible()
  await expect(page.getByRole('img', { name: /^Daily spend, 30 days, \$[\d,]+\.\d{2} in total$/ })).toBeVisible()
  const daily = await ws.api.get<{ points: { amount?: number; effective?: number }[] }>(
    `/v1/tenants/${ws.tenantId}/costs/daily`,
  )
  expect(daily.points.length).toBeGreaterThan(0)
})
