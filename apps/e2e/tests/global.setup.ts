import { expect, test as setup } from '@playwright/test'

import { API_URL, NGN_RATE, STAFF_EMAIL, signInApi } from './support'

setup('the stack is the E2E stack, with an NGN reference rate', async ({ request }) => {
  // Never run this suite against anything but the local test-only API.
  const ready = await request.get(`${API_URL}/readyz`)
  expect(await ready.json()).toEqual({ status: 'ready', environment: 'test' })

  // Cloudoptics staff set the rate the workspace's local currency uses. A rate is carried
  // forward for five days at most, so there is one for every day since the start of last
  // month: every day of the fixture bills up to today has one.
  const staff = await signInApi(request, STAFF_EMAIL, { name: 'FX Staff' })
  const now = new Date()
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  for (let day = Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1); day <= today; day += 86_400_000) {
    await staff.send('POST', '/v1/admin/fx/rates', {
      currency: 'NGN',
      rate: String(NGN_RATE),
      rateDate: new Date(day).toISOString().slice(0, 10),
      basis: 'official',
      reason: 'E2E reference rate',
      confirm: true,
    })
  }
})
