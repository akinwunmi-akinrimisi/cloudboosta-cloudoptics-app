import { expect, test } from '@playwright/test'

import { API_URL, appPath, uniqueEmail } from './support'

// Connect Azure -> costs appear (ADR-020). The API talks to the fake Azure
// (apps/api/tests/fake_azure.py): the customer has consented, given the roles and created the
// FOCUS export. What the customer's Cloud Shell script would do last, tagging the storage
// account with the connection's code, is done here through the test-only /e2e/azure/tag.

test('a new admin connects an Azure subscription and sees its costs', async ({ page, request }) => {
  const customer = (await (await request.get(`${API_URL}/e2e/azure`)).json()) as {
    directoryId: string
    subscriptionId: string
    storageAccount: string
    container: string
    exportPath: string
  }

  await test.step('sign in and create a workspace', async () => {
    await page.goto(appPath('/sign-in'))
    await page.getByLabel('Work email').fill(uniqueEmail('azure'))
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByLabel('Company or team name').fill('Nairobi Analytics')
    await page.getByLabel('Country').selectOption('KE')
    await page.getByRole('button', { name: 'Create workspace' }).click()
    await expect(page.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
  })

  await test.step('choose Azure and say where the export is', async () => {
    await page.getByRole('radio', { name: 'Azure' }).check()
    await expect(page.getByRole('heading', { name: 'Connect your Azure subscription' })).toBeVisible()
    await page.getByLabel('Directory (tenant) ID').fill(customer.directoryId)
    await page.getByLabel('Subscription ID').fill(customer.subscriptionId)
    await page.getByLabel('Storage account name').fill(customer.storageAccount)
    await page.getByLabel('Container', { exact: true }).fill(customer.container)
    await page.getByLabel('Export folder path (optional)').fill(customer.exportPath)
    await page.getByRole('button', { name: 'Continue' }).click()
    await expect(page.getByRole('heading', { name: 'Set up access in Azure' })).toBeVisible()
    await expect(page.getByRole('link', { name: /Grant admin consent/ })).toHaveAttribute(
      'href',
      new RegExp(`^https://login\\.microsoftonline\\.com/${customer.directoryId}/adminconsent\\?`),
    )
  })

  await test.step('the customer runs the script, then access is checked', async () => {
    const script = await page.locator('pre').first().innerText()
    const code = /cloudoptics-verify=(co-[0-9a-f]+)/.exec(script)?.[1]
    expect(code).toBeTruthy()
    const tagged = await request.post(`${API_URL}/e2e/azure/tag`, { data: { code } })
    expect(tagged.ok()).toBeTruthy()
    await page.getByRole('button', { name: 'Check access' }).click()
    await expect(page.getByRole('heading', { name: 'Connected' })).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(`Azure subscription ${customer.subscriptionId}`)).toBeVisible()
    await expect(page.getByText('Prove the storage account is yours')).toBeVisible()
  })

  await test.step('the costs are on the Overview', async () => {
    await page.getByRole('button', { name: 'Go to my workspace' }).click()
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
    const tile = page.getByText('Month-to-date spend', { exact: true }).locator('xpath=following-sibling::p[1]')
    await expect(tile).toHaveText(/^\$[\d,]+\.\d{2}/)
    const amount = Number((await tile.innerText()).split(' · ')[0].replace(/[$,]/g, ''))
    expect(amount).toBeGreaterThan(0)
  })
})
