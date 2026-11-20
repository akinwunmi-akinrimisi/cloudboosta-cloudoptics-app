import { expect, test } from '@playwright/test'

import { API_URL, appPath, ngn, uniqueEmail, usd } from './support'

// Phase 2a (ADR-019): connect Google Cloud, then costs appear. All in the browser except what
// the customer does in Google Cloud itself (label the dataset), which the test-only route on
// the E2E API does for the fake Google Cloud (apps/api/tests/fake_gcp.py).

test('an admin connects Google Cloud and sees its costs', async ({ page, request }) => {
  const fake = await request.post(`${API_URL}/__e2e/gcp/billing-account`)
  expect(fake.ok()).toBeTruthy()
  const { billingAccountId, project, dataset } = (await fake.json()) as {
    billingAccountId: string
    project: string
    dataset: string
  }

  await test.step('sign in and create the workspace', async () => {
    await page.goto(appPath('/sign-in'))
    await page.getByLabel('Work email').fill(uniqueEmail('gcp'))
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByLabel('Company or team name').fill('Lagos Cloud Kitchens')
    await page.getByLabel('Country').selectOption('NG')
    await page.getByRole('button', { name: 'Create workspace' }).click()
    await expect(page.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
  })

  await test.step('choose Google Cloud and say where the export is', async () => {
    await page.getByRole('radio', { name: /Google Cloud/ }).check()
    await expect(page.getByRole('heading', { name: 'Connect Google Cloud' })).toBeVisible()
    await page.getByLabel(/Billing account ID/).fill(billingAccountId)
    await page.getByLabel(/Project that holds the export/).fill(project)
    await page.getByLabel(/^Dataset/).first().fill(dataset)
    await page.getByRole('button', { name: 'Continue' }).click()
    await expect(page.getByRole('heading', { name: 'Give Cloudoptics read access' })).toBeVisible()
  })

  await test.step('run the commands in Google Cloud', async () => {
    const command = await page.getByText(/^bq update --set_label cloudoptics:/).innerText()
    const value = /cloudoptics:([0-9a-f]{32})/.exec(command)?.[1]
    expect(value).toBeTruthy()
    const labelled = await request.post(`${API_URL}/__e2e/gcp/label`, { data: { key: 'cloudoptics', value } })
    expect(labelled.ok()).toBeTruthy()
    await page.getByRole('button', { name: 'I have run the commands' }).click()
  })

  await test.step('the access check passes', async () => {
    await expect(page.getByRole('heading', { name: 'Connected' })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText(`Google Cloud billing account ${billingAccountId}.`)).toBeVisible()
    await expect(page.getByText('Prove the dataset is yours')).toBeVisible()
    await page.getByRole('button', { name: 'Go to my workspace' }).click()
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
  })

  await test.step('the bills are read from BigQuery', async () => {
    // Production's worker loads the export right after the check; inline, Reload bills does.
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Connections' }).click()
    await expect(page.getByText(`Google Cloud billing account ${billingAccountId}`)).toBeVisible()
    await page.getByRole('button', { name: 'Reload bills…' }).click()
    await page.getByRole('button', { name: 'Reload', exact: true }).click()
    await expect(page.getByText(/Bills reloaded/)).toBeVisible({ timeout: 30_000 })
  })

  await test.step('Overview shows month-to-date spend in USD and naira', async () => {
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Overview' }).click()
    const tile = page.getByText('Month-to-date spend', { exact: true }).locator('xpath=following-sibling::p[1]')
    await expect(tile).toHaveText(/^\$[\d,]+\.\d{2} · ₦[\d,]+\.\d{2}$/)
    const [usdText] = (await tile.innerText()).split(' · ')
    const amount = Number(usdText.replace(/[$,]/g, ''))
    expect(amount).toBeGreaterThan(0)
    await expect(tile).toHaveText(`${usd(amount)} · ${ngn(amount)}`)
  })
})
