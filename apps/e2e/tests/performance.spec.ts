import { writeFile, mkdir } from 'node:fs/promises'
import path from 'node:path'

import { expect, test } from '@playwright/test'

import { openSignedIn, seedWorkspace } from './support'

// Overview load budget (docs/11f DoD, PRD FR-UI-10 / NFR-PERF-03): Largest Contentful Paint
// on /overview at most 4 s on a throttled "Fast 3G" connection with a 4x slower CPU, from a
// cold cache, against the production build (`vite preview`) and the real API.
//
// The network figures are Chrome DevTools' "Fast 3G" preset: 1.6 Mbit/s down and 750 kbit/s
// up at 90%, 562.5 ms round trip. Chromium applies them to every request of the page, the
// API's included. In the E2E stack the API is on another origin, so each API call also pays
// for a CORS preflight; in production it is same-origin (/api), so this is the harder case.

const BUDGET_MS = 4000
const FAST_3G = {
  offline: false,
  downloadThroughput: (1.6 * 1000 * 1000) / 8 * 0.9,
  uploadThroughput: (750 * 1000) / 8 * 0.9,
  latency: 150 * 3.75,
}
const OUT = path.join(import.meta.dirname, '..', 'reports', 'performance')

test('Overview LCP is within budget on Fast 3G with a 4x slower CPU', async ({ browserName, page, request }) => {
  test.skip(browserName !== 'chromium', 'network and CPU throttling need the Chrome DevTools protocol')
  test.setTimeout(90_000)
  const ws = await seedWorkspace(request)

  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Network.enable')
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true })
  await cdp.send('Network.emulateNetworkConditions', FAST_3G)
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 })

  // LCP can be a tile's placeholder ("—"); also note when the first dollar figure is on screen.
  await page.addInitScript(() => {
    const w = window as unknown as { __figureAt?: number }
    new MutationObserver((_, observer) => {
      const tile = [...document.querySelectorAll('main p')].find((p) => /^\$\d/.test(p.textContent ?? ''))
      if (tile) {
        w.__figureAt = performance.now()
        observer.disconnect()
      }
    }).observe(document, { subtree: true, childList: true, characterData: true })
  })
  await openSignedIn(page, ws.api.token, ws.tenantId, '/overview')
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible({ timeout: 30_000 })
  // The tiles and the chart arrive with the API's answers; LCP is final once they have rendered.
  await expect(page.getByRole('img', { name: /^Daily spend/ })).toBeVisible({ timeout: 30_000 })
  await page.waitForLoadState('networkidle')

  const lcp = await page.evaluate(
    () =>
      new Promise<{ startTime: number; element: string }>((resolve) => {
        new PerformanceObserver((list) => {
          const entries = list.getEntries() as (PerformanceEntry & { element?: Element | null })[]
          const last = entries[entries.length - 1]
          const el = last.element
          resolve({
            startTime: last.startTime,
            element: el
              ? `${el.tagName.toLowerCase()}${el.className ? `.${String(el.className).split(' ')[0]}` : ''} "${(el.textContent ?? '').trim().slice(0, 40)}"`
              : '',
          })
        }).observe({ type: 'largest-contentful-paint', buffered: true })
      }),
  )
  const nav = await page.evaluate(() => {
    const n = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming
    return {
      ttfbMs: Math.round(n.responseStart),
      domContentLoadedMs: Math.round(n.domContentLoadedEventEnd),
      loadMs: Math.round(n.loadEventEnd),
      firstFigureMs: Math.round((window as unknown as { __figureAt?: number }).__figureAt ?? -1),
    }
  })
  const result = { budgetMs: BUDGET_MS, lcpMs: Math.round(lcp.startTime), lcpElement: lcp.element, ...nav }
  const line =
    `Overview LCP ${result.lcpMs} ms (budget ${BUDGET_MS} ms; Fast 3G, CPU 4x; element ${lcp.element || 'n/a'}); ` +
    `first figure ${result.firstFigureMs} ms`
  console.log(line)
  await mkdir(OUT, { recursive: true })
  await writeFile(path.join(OUT, 'overview.json'), JSON.stringify(result, null, 2))
  await test.info().attach('overview-lcp.json', { body: JSON.stringify(result, null, 2), contentType: 'application/json' })
  expect(result.lcpMs, line).toBeLessThanOrEqual(BUDGET_MS)
})
