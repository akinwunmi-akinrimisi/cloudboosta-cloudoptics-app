import { expect, test } from '@playwright/test'

import { API_URL, openSignedIn, seedWorkspace, signInApi, uniqueEmail } from './support'

// J5 (docs/11f §1.4): invite a Viewer -> they join on sign-in -> Viewer cannot manage the
// workspace (UI and API) -> an admin promotes them -> removes them -> access is gone.

interface Member {
  userId: string
  email: string
  role: string
}

test('admins invite a viewer, change their role and remove them', async ({ page, browser, request }) => {
  const ws = await seedWorkspace(request, { bills: false, scan: false })
  const email = uniqueEmail('viewer')
  await openSignedIn(page, ws.api.token, ws.tenantId, '/settings?section=users')
  await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible()

  await test.step('invite by email as a viewer', async () => {
    await page.getByLabel('Invite by email').fill(email)
    const form = page.locator('form').filter({ has: page.getByLabel('Invite by email') })
    await form.locator('select').selectOption('viewer')
    await page.getByRole('button', { name: 'Invite', exact: true }).click()
    await expect(page.getByText(`${email} · viewer`)).toBeVisible()
  })

  const viewer = await signInApi(request, email, { name: 'Vic Viewer' })

  await test.step('the viewer joins on sign-in and cannot manage the workspace', async () => {
    const context = await browser.newContext()
    const theirs = await context.newPage()
    await openSignedIn(theirs, viewer.token, ws.tenantId, '/settings')
    const nav = theirs.getByRole('navigation', { name: 'Settings' })
    await expect(nav.getByRole('link', { name: 'Users & roles' })).toBeVisible()
    await expect(nav.getByRole('link', { name: 'Security' })).toHaveCount(0)
    await expect(nav.getByRole('link', { name: 'Workspace' })).toHaveCount(0)
    await expect(theirs.getByLabel('Invite by email')).toHaveCount(0)
    await context.close()
    // The API refuses too, not only the page.
    const r = await request.post(`${API_URL}/v1/tenants/${ws.tenantId}/invites`, {
      headers: { authorization: `Bearer ${viewer.token}` },
      data: { email: uniqueEmail('nope'), role: 'admin' },
    })
    expect(r.status()).toBe(403)
  })

  await test.step('promote the viewer to member', async () => {
    await page.reload()
    await page.getByLabel(`Role for ${email}`).selectOption('member')
    await expect
      .poll(async () => (await ws.api.get<Member[]>(`/v1/tenants/${ws.tenantId}/members`)).find((m) => m.email === email)?.role)
      .toBe('member')
  })

  await test.step('remove them; their access ends', async () => {
    page.once('dialog', (dialog) => void dialog.accept())
    await page.getByRole('listitem').filter({ hasText: 'Vic Viewer' }).getByRole('button', { name: 'Remove' }).click()
    await expect(page.getByLabel(`Role for ${email}`)).toHaveCount(0)
    const r = await request.get(`${API_URL}/v1/tenants/${ws.tenantId}`, {
      headers: { authorization: `Bearer ${viewer.token}` },
    })
    expect([403, 404]).toContain(r.status())
  })
})
