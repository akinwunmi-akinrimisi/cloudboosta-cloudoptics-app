import { expect, test } from '@playwright/test'

import { addTeammate, openSignedIn, seedWorkspace } from './support'

// J3 (docs/11f §1.4): list -> detail -> assign -> dismiss with a reason. The scan found the
// fake account's resources (server/fake_aws.py) and the engine turned them into these.

interface Rec {
  id: string
  title: string
  ruleId: string
  status: string
  assigneeId: string | null
}

const VOLUME = 'Delete unattached gp2 volume vol-0e2eloose001 (500 GiB)'

test('recommendations: list, detail, assign to a teammate, dismiss with a reason', async ({ page, request }) => {
  const ws = await seedWorkspace(request)
  const sam = await addTeammate(request, ws, 'member')
  const base = `/v1/tenants/${ws.tenantId}/recommendations`
  const recs = await ws.api.get<Rec[]>(base)
  expect(recs.map((r) => r.ruleId)).toEqual(expect.arrayContaining(['R1_unattached_ebs', 'R4_idle_ec2']))
  const other = recs.find((r) => r.ruleId === 'R4_idle_ec2') as Rec

  await openSignedIn(page, ws.api.token, ws.tenantId, '/recommendations')
  await expect(page.getByRole('heading', { name: 'Recommendations' })).toBeVisible()
  await expect(page.getByText('Monthly savings identified')).toBeVisible()
  await expect(page.getByRole('link', { name: new RegExp(other.title.replace(/[()]/g, '\\$&')) })).toBeVisible()

  await test.step('open the detail', async () => {
    await page.getByRole('link', { name: new RegExp(VOLUME.replace(/[()]/g, '\\$&')) }).click()
    await expect(page.getByRole('heading', { name: VOLUME, level: 1 })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'How to fix it' })).toBeVisible()
    await expect(page.getByText('Monthly saving', { exact: true })).toBeVisible()
  })

  await test.step('assign it to a teammate', async () => {
    await page.getByLabel('Assigned to').selectOption({ label: 'Tess member' })
    await expect(page.getByLabel('Assigned to')).toHaveValue(sam.userId)
    const volume = (await ws.api.get<Rec[]>(base)).find((r) => r.title === VOLUME) as Rec
    expect([volume.status, volume.assigneeId]).toEqual(['assigned', sam.userId])
    await page.getByRole('link', { name: 'Recommendations' }).first().click()
    await expect(page.getByText(/· Assigned · Tess member/)).toBeVisible()
  })

  await test.step('dismiss another, with a reason', async () => {
    await page.getByRole('link', { name: new RegExp(other.title.replace(/[()]/g, '\\$&')) }).first().click()
    await page.getByRole('button', { name: 'Dismiss…' }).click()
    await page.getByLabel('Why are you dismissing it?').fill('Batch runner is needed at month end')
    await page.getByRole('button', { name: 'Dismiss', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Reopen' })).toBeVisible()
    const after = (await ws.api.get<Rec[]>(`${base}?status=all`)).find((r) => r.id === other.id)
    expect(after?.status).toBe('dismissed')
  })
})
