import { expect, test } from '@playwright/test'

import { openSignedIn, seedWorkspace } from './support'

// ADM-7 (PRD US-8.3): disconnect an account and delete its data; schedule the workspace's
// deletion and cancel it within the grace period.

test('disconnect an AWS account, then delete its data', async ({ page, request }) => {
  const ws = await seedWorkspace(request)
  await openSignedIn(page, ws.api.token, ws.tenantId, '/settings/connections')
  await expect(page.getByText(`AWS account ${ws.account}`, { exact: true })).toBeVisible()

  await test.step('disconnect', async () => {
    await page.getByRole('button', { name: 'Disconnect…' }).click()
    await expect(page.getByText(`Disconnect AWS account ${ws.account}?`)).toBeVisible()
    await page.getByRole('button', { name: 'Disconnect', exact: true }).click()
    await expect(page.getByText(/^Disconnected\. Cloudoptics no longer reads this account/)).toBeVisible()
    const [conn] = await ws.api.get<{ status: string }[]>(`/v1/tenants/${ws.tenantId}/connections`)
    expect(conn.status).toBe('disconnected')
  })

  await test.step('delete the data, typing the account ID to confirm', async () => {
    await page.getByRole('button', { name: 'Delete data…' }).click()
    const submit = page.getByRole('button', { name: 'Delete data', exact: true })
    await expect(submit).toBeDisabled()
    await page.getByLabel(`Type the account ID ${ws.account} to confirm`).fill(ws.account)
    await submit.click()
    await expect(page.getByText(`Deleted the data for AWS account ${ws.account}`, { exact: false })).toBeVisible()
    const summary = await ws.api.get<{ status: string }>(`/v1/tenants/${ws.tenantId}/costs/summary`)
    expect(summary.status).not.toBe('ready')
    const recs = await ws.api.get<unknown[]>(`/v1/tenants/${ws.tenantId}/recommendations`)
    expect(recs).toEqual([])
  })
})

test('schedule the workspace for deletion, then cancel it', async ({ page, request }) => {
  const ws = await seedWorkspace(request, { bills: false, scan: false })
  await openSignedIn(page, ws.api.token, ws.tenantId, '/settings?section=workspace')
  await expect(page.getByRole('heading', { name: 'Danger zone' })).toBeVisible()

  await page.getByRole('button', { name: 'Delete workspace…' }).click()
  const schedule = page.getByRole('button', { name: /^Delete in \d+ days$/ })
  await expect(schedule).toBeDisabled()
  await page.getByLabel(`Type the workspace name (${ws.name}) to confirm`).fill(ws.name)
  await schedule.click()
  await expect(page.getByText(/This workspace will be deleted on/).first()).toBeVisible()
  const scheduled = await ws.api.get<{ deletion: unknown }>(`/v1/tenants/${ws.tenantId}/deletion`)
  expect(scheduled.deletion).not.toBeNull()

  await page.getByRole('button', { name: 'Cancel deletion' }).first().click()
  await expect(page.getByRole('button', { name: 'Delete workspace…' })).toBeVisible()
  await expect(page.getByText(/This workspace will be deleted on/)).toHaveCount(0)
  const cancelled = await ws.api.get<{ deletion: unknown }>(`/v1/tenants/${ws.tenantId}/deletion`)
  expect(cancelled.deletion).toBeNull()
})
