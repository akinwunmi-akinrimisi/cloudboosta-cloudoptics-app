import AxeBuilder from '@axe-core/playwright'
import type { Page } from '@playwright/test'
import { expect, test } from '@playwright/test'

import { API_URL, accountId, appPath, sendStackCallback, stackCallback, uniqueEmail } from './support'

// "Send this to my cloud admin" (ADR-022): the owner sends the AWS step to their admin, the
// admin opens the link from the email (read from the E2E server's outbox) in a browser that is
// not signed in, connects the fake AWS (the stack's callback is sent with the values the page
// shows, as onboarding.spec.ts does), and the owner sees it connected. AWS rather than Google
// Cloud: the E2E server's fake Google Cloud has one billing account, which gcp.spec.ts owns.

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']

async function noViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([])
}

test('the owner sends the connect step and their cloud admin connects it', async ({ page, browser, request }) => {
  const account = accountId()
  const admin = uniqueEmail('cloud-admin')

  await test.step('the owner creates the workspace and sees who can connect AWS', async () => {
    await page.goto(appPath('/sign-in'))
    await page.getByLabel('Work email').fill(uniqueEmail('founder'))
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByLabel('Company or team name').fill('Ikeja Freight')
    await page.getByRole('button', { name: 'Create workspace' }).click()
    await expect(page.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
    await expect(page.getByText(/can create IAM roles and CloudFormation stacks/)).toBeVisible()
  })

  await test.step('they send it to their admin', async () => {
    await page.getByRole('button', { name: 'Not you? Send this to your admin' }).click()
    await page.getByLabel("Your admin's email").fill(admin)
    await noViolations(page)
    await page.getByRole('button', { name: 'Send' }).click()
    const status = page.getByRole('status').filter({ hasText: admin })
    await expect(status).toContainText('Sent')
  })

  const link = await test.step('the admin gets an email with the link', async () => {
    let text = ''
    await expect(async () => {
      const r = await request.get(`${API_URL}/__e2e/emails`, { params: { to: admin } })
      const emails = (await r.json()) as { subject: string; text: string; tag: string }[]
      const invite = emails.find((e) => e.tag === 'connect-invite')
      expect(invite?.subject).toContain('Ikeja Freight')
      text = invite?.text ?? ''
    }).toPass()
    expect(text).toContain('Read-only')
    const match = /(https?:\/\/\S+\/connect-invite\/[A-Za-z0-9_-]+)/.exec(text)
    expect(match).toBeTruthy()
    return (match as RegExpExecArray)[1]
  })

  const adminContext = await browser.newContext()
  const adminPage = await adminContext.newPage()
  try {
    await test.step('the admin opens it, not signed in, and sees only the connect step', async () => {
      await adminPage.goto(link)
      await expect(adminPage.getByText('For Ikeja Freight')).toBeVisible()
      await expect(adminPage.getByText(/asked you to connect AWS\. Read-only/)).toBeVisible()
      await expect(adminPage).toHaveURL(/\/connect-invite\/session$/)
      await expect(adminPage.getByRole('navigation', { name: 'Main' })).toHaveCount(0)
      await expect(adminPage.getByRole('heading', { name: 'Connect your AWS account' })).toBeVisible()
      await expect(adminPage.getByRole('button', { name: /Send this to your admin/ })).toHaveCount(0)
      await noViolations(adminPage)
    })

    await test.step('the admin creates the stack in AWS', async () => {
      const value = async (label: string) =>
        (await adminPage.getByRole('button', { name: `Copy ${label}` }).locator('xpath=..').locator('code').innerText()).trim()
      const setup = { externalId: await value('ExternalId'), connectionRef: await value('ConnectionRef') }
      const tenantId = await value('TenantId')
      await adminPage.getByRole('button', { name: 'I have created the stack' }).click()
      await expect(adminPage.getByText('Waiting for AWS…')).toBeVisible()
      await sendStackCallback(request, stackCallback(tenantId, setup, account))
      await expect(adminPage.getByRole('heading', { name: 'Done, you can close this page' })).toBeVisible({ timeout: 20_000 })
      await noViolations(adminPage)
    })

    await test.step('the link does not open again', async () => {
      await adminPage.goto(link)
      await expect(adminPage.getByRole('heading', { name: 'This link does not work' })).toBeVisible()
    })
  } finally {
    await adminContext.close()
  }

  await test.step('the owner sees it connected', async () => {
    await expect(page.getByRole('heading', { name: 'Connected' })).toBeVisible({ timeout: 20_000 })
    await page.getByRole('button', { name: 'Go to my workspace' }).click()
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Connections' }).click()
    const sent = page.getByRole('region', { name: 'Sent to cloud admins' })
    await expect(sent.getByText('Connected', { exact: true })).toBeVisible()
    await expect(sent.getByText(`${admin} connected AWS.`)).toBeVisible()
  })

  await test.step('the sender is emailed', async () => {
    await expect(async () => {
      const mine = await request.get(`${API_URL}/__e2e/emails`, { params: { to: '@example.com' } })
      const emails = (await mine.json()) as { subject: string; tag: string }[]
      expect(emails.some((e) => e.tag === 'connect-invite-connected' && e.subject.includes(admin))).toBe(true)
    }).toPass()
  })
})
