import { readFile } from 'node:fs/promises'

import { expect, test } from '@playwright/test'

import { openSignedIn, seedWorkspace } from './support'

// J2's last step and FE-10 (docs/11f §1.4): what the page shows, downloaded as CSV.

test('Cost Explorer exports spend by service as a CSV download', async ({ page, request }) => {
  const ws = await seedWorkspace(request, { scan: false })
  await openSignedIn(page, ws.api.token, ws.tenantId, '/costs')
  await expect(page.getByRole('heading', { name: /^Spend by service/ })).toBeVisible()

  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export CSV', exact: true }).click()
  const file = await download
  expect(file.suggestedFilename()).toMatch(/\.csv$/)
  const csv = await readFile(await file.path(), 'utf8')
  const lines = csv.replace(/^﻿/, '').trim().split(/\r?\n/)
  expect(lines.length).toBeGreaterThan(1)
  expect(lines[0].toLowerCase()).toContain('service')
  expect(lines.some((l) => l.includes('AmazonEC2'))).toBeTruthy()
  // The cloud is the last column, so the earlier columns keep their positions (ADR-018).
  expect(lines[0].split(',').at(-1)).toBe('provider')
  expect(lines.filter((l) => l.includes('AmazonEC2')).every((l) => l.endsWith(',aws'))).toBeTruthy()
  // Formula guard (docs/11f J2): no cell starts with a character a spreadsheet would run.
  for (const line of lines) {
    for (const cell of line.split(',')) expect(cell.replace(/^"/, '')).not.toMatch(/^[=+@]/)
  }

  // The export is also listed under "Your exports" for 7 days.
  const listed = await ws.api.get<{ exports: { fileName: string; status: string }[] }>(
    `/v1/tenants/${ws.tenantId}/exports`,
  )
  expect(listed.exports.map((e) => e.fileName)).toContain(file.suggestedFilename())
})
