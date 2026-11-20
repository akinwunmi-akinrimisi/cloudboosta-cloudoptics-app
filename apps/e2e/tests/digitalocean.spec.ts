import { randomBytes } from 'node:crypto'

import { expect, test } from '@playwright/test'

import { appPath, uniqueEmail } from './support'

// Connect DigitalOcean -> costs appear (ADR-021). The API's DigitalOcean is the fake in
// apps/api/tests/fake_digitalocean.py: any well-formed token reads a team of its own with two
// closed invoices, this month's spend and a few idle resources. A token whose hex starts with
// "ffff" can also write, and is refused.

function token(prefix = ''): string {
  // Never "dead" / "ffff" / "b111" by chance: those prefixes mean something to the fake.
  return `dop_v1_${(prefix + '1' + randomBytes(32).toString('hex')).slice(0, 64)}`
}

test('an admin connects DigitalOcean with a read-only token and sees its costs', async ({ page }) => {
  await test.step('sign in and create a workspace', async () => {
    await page.goto(appPath('/sign-in'))
    await page.getByLabel('Work email').fill(uniqueEmail('ocean'))
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByLabel('Company or team name').fill('Ocean Apps')
    await page.getByRole('button', { name: 'Create workspace' }).click()
    await expect(page.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
  })

  await test.step('choose DigitalOcean: the steps name the read scopes', async () => {
    await page.getByText('DigitalOcean', { exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Connect DigitalOcean' })).toBeVisible()
    await expect(page.getByRole('link', { name: /Generate New Token/ })).toHaveAttribute(
      'href',
      'https://cloud.digitalocean.com/account/api/tokens/new',
    )
    await expect(page.getByText('billing:read', { exact: true })).toBeVisible()
  })

  await test.step('a token that can write is refused and not kept', async () => {
    await page.getByLabel('DigitalOcean token').fill(token('ffff'))
    await page.getByRole('button', { name: 'Connect' }).click()
    await expect(page.getByRole('alert')).toContainText('This token can change your DigitalOcean resources')
  })

  const good = token()
  await test.step('a read-only token connects and every check passes', async () => {
    await page.getByLabel('DigitalOcean token').fill(good)
    await page.getByRole('button', { name: 'Connect' }).click()
    await expect(page.getByRole('heading', { name: 'Connected' })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/^DigitalOcean account [0-9a-f-]{36}\.$/)).toBeVisible()
    await expect(page.getByText('Token is read-only')).toBeVisible()
    await expect(page.getByText(good)).toHaveCount(0)
    await page.getByRole('button', { name: 'Go to my workspace' }).click()
  })

  await test.step('Overview shows month-to-date spend', async () => {
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
    const tile = page.getByText('Month-to-date spend', { exact: true }).locator('xpath=following-sibling::p[1]')
    await expect(tile).toHaveText(/^\$[\d,]+\.\d{2}/)
    const amount = Number((await tile.innerText()).split(' · ')[0].replace(/[$,]/g, ''))
    expect(amount).toBeGreaterThan(0)
  })

  await test.step('last month is the invoice, by service', async () => {
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Cost Explorer' }).click()
    await expect(page.getByRole('heading', { name: 'Cost Explorer' })).toBeVisible()
    const month = page.getByRole('combobox', { name: /month/i })
    if (await month.count()) {
      await month.selectOption({ index: 1 })
      await expect(page.getByRole('row', { name: /^Droplets / }).first()).toBeVisible()
    }
  })

  await test.step('the Connections page offers Replace token, not the AWS stack', async () => {
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Connections' }).click()
    await expect(page.getByText('DigitalOcean team, read-only token')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Replace token…' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Set up again' })).toHaveCount(0)
  })
})
