import { expect, test } from '@playwright/test'

import { accountId, appPath, ngn, sendStackCallback, stackCallback, uniqueEmail, usd } from './support'

// J1 (docs/11f §1.4): sign in -> create workspace -> connect AWS -> access checked -> first
// bills -> recommendations. All in the browser except what AWS itself does: the stack's
// callback, sent here with the values the page shows.

test('a new admin signs in, creates a workspace, connects AWS and sees costs and recommendations', async ({
  page,
  request,
}) => {
  const email = uniqueEmail('founder')
  const account = accountId()

  await test.step('sign in with the development form', async () => {
    await page.goto(appPath('/sign-in'))
    await page.getByLabel('Work email').fill(email)
    await page.getByLabel('Your name (optional)').fill('Funmi Founder')
    await page.getByRole('button', { name: 'Continue' }).click()
    await expect(page.getByRole('heading', { name: 'Create your workspace' })).toBeVisible()
  })

  await test.step('create the workspace in Nigeria (local currency NGN)', async () => {
    await page.getByLabel('Company or team name').fill('Lagos Logistics')
    await page.getByLabel('Country').selectOption('NG')
    await page.getByRole('button', { name: 'Create workspace' }).click()
    await expect(page.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
  })

  await test.step('the stack is created in AWS and calls back', async () => {
    const value = async (label: string) =>
      (await page.getByRole('button', { name: `Copy ${label}` }).locator('xpath=..').locator('code').innerText()).trim()
    const setup = { externalId: await value('ExternalId'), connectionRef: await value('ConnectionRef') }
    const tenantId = await value('TenantId')
    expect(setup.externalId.length).toBeGreaterThanOrEqual(32)
    expect(setup.connectionRef).toMatch(/^i:/)
    await page.getByRole('button', { name: 'I have created the stack' }).click()
    await expect(page.getByText('Waiting for AWS…')).toBeVisible()
    const result = await sendStackCallback(request, stackCallback(tenantId, setup, account))
    expect(result.status).toBe('accepted')
  })

  await test.step('the access check passes', async () => {
    await expect(page.getByRole('heading', { name: 'Connected' })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText(`AWS account ${account}`)).toBeVisible()
    await expect(page.getByText('Assume the Cloudoptics role')).toBeVisible()
    await page.getByRole('button', { name: 'Go to my workspace' }).click()
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
    await expect(page.getByText('Connected. AWS delivers the first Cost and Usage Report')).toBeVisible()
  })

  await test.step('the first bills are loaded and the resources scanned', async () => {
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Connections' }).click()
    await expect(page.getByRole('heading', { name: 'Connections' })).toBeVisible()
    await page.getByRole('button', { name: 'Reload bills…' }).click()
    await page.getByRole('button', { name: 'Reload', exact: true }).click()
    await expect(page.getByText(/Bills reloaded: all months in the export/)).toBeVisible({ timeout: 30_000 })
    // The bills name the accounts; the resource scan covers each of them.
    await page.reload()
    const scan = page.getByRole('button', { name: /^(Scan now|Check now)$/ })
    await scan.click()
    await expect(page.getByRole('button', { name: 'Scan now' })).toBeEnabled({ timeout: 30_000 })
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

  await test.step('recommendations are listed', async () => {
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Recommendations' }).click()
    await expect(page.getByText('Delete unattached gp2 volume vol-0e2eloose001 (500 GiB)')).toBeVisible()
  })
})
