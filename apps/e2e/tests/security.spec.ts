import { expect, test } from '@playwright/test'

import { API_URL, addTeammate, openSignedIn, seedWorkspace } from './support'

// MFA-required workspaces (docs/09 §3.4 ``mfa_required``) and the API's rate limits (WBS
// API-8): what a person sees when either refuses them.

test('a workspace that requires MFA explains it to a member who signed in without it', async ({ page, request }) => {
  const ws = await seedWorkspace(request, { bills: false, scan: false, mfa: true })
  const member = await addTeammate(request, ws, 'member')
  await ws.api.send('PATCH', `/v1/tenants/${ws.tenantId}`, { mfaRequired: true })

  await openSignedIn(page, member.api.token, ws.tenantId)
  await expect(
    page.getByText('This workspace requires multi-factor authentication. Turn on MFA (an authenticator app)'),
  ).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sign out' }).last()).toBeVisible()
  const r = await request.get(`${API_URL}/v1/tenants/${ws.tenantId}`, {
    headers: { authorization: `Bearer ${member.api.token}` },
  })
  expect(r.status()).toBe(401)
  expect((await r.json()).code).toBe('mfa_required')
})

test('an admin who signed in with MFA keeps working in an MFA-required workspace', async ({ page, request }) => {
  const ws = await seedWorkspace(request, { bills: false, scan: false, mfa: true })
  await ws.api.send('PATCH', `/v1/tenants/${ws.tenantId}`, { mfaRequired: true })
  await openSignedIn(page, ws.api.token, ws.tenantId)
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
  await expect(page.getByText(/requires multi-factor authentication/)).toHaveCount(0)
})

test('when the rate limit is reached the page says how long to wait', async ({ page, request }) => {
  // The E2E API allows each user 240 requests a minute (server/serve.py).
  const ws = await seedWorkspace(request, { bills: false, scan: false })
  await openSignedIn(page, ws.api.token, ws.tenantId, '/budgets')
  await page.getByRole('button', { name: 'New budget' }).click()
  await page.getByLabel('Name', { exact: true }).fill('Rate limited')
  await page.getByLabel('Amount a month').fill('1000')

  const headers = { authorization: `Bearer ${ws.api.token}` }
  const burn = async (): Promise<number> => {
    for (let i = 0; i < 400; i++) {
      const r = await request.get(`${API_URL}/v1/me`, { headers })
      if (r.status() === 429) return Number(r.headers()['retry-after'])
    }
    throw new Error('no 429 after 400 requests')
  }
  let retryAfter = await burn()
  // Limits are per minute. Near the end of a window, wait for the next one and fill it again,
  // so the page's request is refused too.
  if (retryAfter < 15) {
    await page.waitForTimeout((retryAfter + 1) * 1000)
    retryAfter = await burn()
  }
  await page.getByRole('button', { name: 'Create budget' }).click()
  await expect(page.getByText(/^Too many requests right now\. Wait a moment and try again in \d+ seconds?\.$/)).toBeVisible()
})
