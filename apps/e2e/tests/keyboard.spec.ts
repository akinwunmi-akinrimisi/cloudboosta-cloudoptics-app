import type { Locator, Page } from '@playwright/test'
import { expect, test } from '@playwright/test'

import type { Workspace } from './support'
import { Api, appPath, linkTabKey, openSignedIn, seedWorkspace } from './support'

// What axe cannot check (docs/08 accessibility table, PRD FR-UI-06, WCAG 2.2 AA): keyboard
// order and focus, Escape on everything that opens, reduced motion, chart alternatives and
// focus on route changes. In the real browser against the real API.

let ws: Workspace
test.beforeAll(async ({ request }) => {
  ws = await seedWorkspace(request)
})

async function focused(page: Page) {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null
    if (!el || el === document.body) return { tag: 'body', text: '', inMain: false, inNav: false }
    return {
      tag: el.tagName.toLowerCase(),
      text: (el.getAttribute('aria-label') ?? el.textContent ?? '').trim(),
      inMain: Boolean(el.closest('main')),
      inNav: Boolean(el.closest('nav')),
    }
  })
}

async function open(page: Page, path: string, heading: string | RegExp) {
  await openSignedIn(page, ws.api.token, ws.tenantId, path)
  await expect(page.getByRole('heading', { level: 1, name: heading }).first()).toBeVisible()
  await page.waitForLoadState('networkidle')
}

/** Focus ring and :focus-visible on what has focus now; null when nothing does. */
async function ring(page: Page) {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement
    if (el === document.body) return null
    const s = getComputedStyle(el)
    return {
      name: `${el.tagName.toLowerCase()} "${(el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 30)}"`,
      outline: s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 2,
      shadow: s.boxShadow !== 'none',
      visible: el.matches(':focus-visible'),
      hidden: el.getBoundingClientRect().width === 0,
    }
  })
}

/** Note in the report that this browser cannot Tab to links (see linkTabKey in support.ts). */
function noLinkTab(what: string) {
  test.info().annotations.push({
    type: 'webkit-tab',
    description: `This browser build does not Tab to links (Safari's default); ${what}`,
  })
}

test('skip to content is the first Tab stop and lands on the main content', async ({ page }) => {
  await open(page, '/overview', 'Overview')
  const skip = page.getByRole('link', { name: 'Skip to main content' })
  const key = await linkTabKey(page)
  if (key) {
    await page.keyboard.press(key)
    await expect(skip).toBeFocused()
  } else {
    // Where links are not Tab stops, the skip link cannot be one either; it must still show
    // and work once it has focus (Safari with Option+Tab or "Press Tab to highlight").
    noLinkTab('the skip link is focused from the keyboard state directly and checked from there.')
    await page.keyboard.press('Shift')
    await skip.focus()
  }
  await expect(skip).toBeInViewport()
  await page.keyboard.press('Enter')
  await expect(page.locator('main#main')).toBeFocused()
  // The next Tab is inside the page, past the navigation and the top bar.
  await page.keyboard.press('Tab')
  const next = await focused(page)
  expect(next.inMain, `after the skip link Tab went to ${next.tag} "${next.text}"`).toBe(true)
})

test('every control shows a visible focus ring from the keyboard', async ({ page }) => {
  await open(page, '/overview', 'Overview')
  const key = await linkTabKey(page)
  const seen: string[] = []
  const check = (r: NonNullable<Awaited<ReturnType<typeof ring>>>) => {
    expect(r.visible, `${r.name} matches :focus-visible`).toBe(true)
    expect(r.outline || r.shadow, `${r.name} has a focus ring`).toBe(true)
    seen.push(r.name)
  }
  for (let i = 0; i < 40; i++) {
    await page.keyboard.press(key ?? 'Tab')
    const r = await ring(page)
    if (r === null) break // past the last control
    if (r.hidden) continue
    check(r)
  }
  if (!key) {
    // Tab reached the buttons and fields; each link is focused after a key press (the browser
    // then treats focus as keyboard focus) and must show the same ring.
    noLinkTab('links are focused one by one after a key press and checked for the ring.')
    for (const link of await page.locator('a[href]:visible').all()) {
      await page.keyboard.press('Shift')
      await link.focus()
      const r = await ring(page)
      if (r && !r.hidden) check(r)
    }
  }
  // Skip link, the nine pages, the currency buttons, sign out and the page's own controls.
  expect(seen.length).toBeGreaterThan(14)
})

test('the phone menu is a modal: focus moves in, Tab stays in, Escape closes it and focus returns', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
  const page = await context.newPage()
  await open(page, '/overview', 'Overview')
  const button = page.getByRole('button', { name: 'Open menu' })
  await button.focus()
  await page.keyboard.press('Enter')
  const drawer = page.getByRole('dialog', { name: 'Cloudoptics' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toHaveAttribute('aria-modal', 'true')
  await expect(button).toHaveAttribute('aria-expanded', 'true')
  await expect(drawer.getByRole('button', { name: 'Close menu' })).toBeFocused()
  // Plain Tab, which in Safari skips the menu's links by default, and the key that reaches links
  // where it differs: focus stays in the menu with either.
  const linkKey = (await linkTabKey(page)) ?? 'Tab'
  for (const key of new Set(['Tab', 'Shift+Tab', linkKey, linkKey.replace('Tab', 'Shift+Tab')])) {
    for (let i = 0; i < 14; i++) {
      await page.keyboard.press(key)
      const inside = await drawer.evaluate((d) => d.contains(document.activeElement))
      expect(inside, `${key} #${i + 1} stayed in the menu`).toBe(true)
    }
  }
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect(button).toBeFocused()
  // The close button does the same.
  await page.keyboard.press('Enter')
  await drawer.getByRole('button', { name: 'Close menu' }).click()
  await expect(drawer).toBeHidden()
  await expect(button).toBeFocused()
  await context.close()
})

/** Opens an inline panel from `trigger`, checks focus moved in, Escape closes it, focus is back. */
async function escapeCloses(page: Page, trigger: Locator, inside: Locator) {
  await trigger.focus()
  await page.keyboard.press('Enter')
  await expect(inside).toBeVisible()
  await expect(inside).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(inside).toBeHidden()
  await expect(trigger).toBeFocused()
}

test('Escape closes every inline panel and focus returns to the button that opened it', async ({ page, request }) => {
  await open(page, '/settings/connections', 'Connections')
  await escapeCloses(page, page.getByRole('button', { name: 'Reload bills…' }), page.getByRole('combobox', { name: 'Months' }))
  await escapeCloses(page, page.getByRole('button', { name: 'Disconnect…' }), page.getByRole('button', { name: 'Disconnect', exact: true }))

  await page.goto(appPath('/budgets'))
  await expect(page.getByRole('heading', { level: 1, name: 'Budgets' })).toBeVisible()
  await escapeCloses(page, page.getByRole('button', { name: 'New budget' }), page.getByLabel('Name'))

  await page.goto(appPath('/settings'))
  await expect(page.getByRole('heading', { level: 1, name: 'Settings' })).toBeVisible()
  await escapeCloses(page, page.getByRole('button', { name: 'Delete workspace…' }), page.getByLabel(/Type the workspace name/))

  await page.goto(appPath('/recommendations'))
  await page.getByRole('checkbox', { name: /^Select / }).first().check()
  await escapeCloses(page, page.getByRole('button', { name: 'Dismiss…' }), page.getByLabel(/Why are you dismissing/))

  const recs = await new Api(request, ws.api.token).get<{ id: string }[]>(`/v1/tenants/${ws.tenantId}/recommendations`)
  await page.goto(appPath(`/recommendations/${recs[0].id}`))
  await escapeCloses(page, page.getByRole('button', { name: 'Dismiss…' }), page.getByLabel('Why are you dismissing it?'))
})

test('reduced motion turns transitions and animations off', async ({ page }) => {
  await open(page, '/settings/connections', 'Connections')
  // Buttons fade their colour on hover (Button in components/ui.tsx).
  const button = page.locator('main button.transition-colors').first()
  const duration = () => button.evaluate((el) => parseFloat(getComputedStyle(el).transitionDuration))
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  expect(await duration()).toBeGreaterThan(0.05)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  expect(await duration()).toBeLessThan(0.001)
  // A spinning icon or pulsing bar, if one is running, is cut to (almost) nothing.
  const animations = await page.evaluate(() =>
    document.getAnimations().map((a) => Number(a.effect?.getComputedTiming().duration ?? 0)),
  )
  for (const d of animations) expect(d).toBeLessThan(1)
})

test('every chart has a table with its figures', async ({ page, request }) => {
  const recs = await new Api(request, ws.api.token).get<{ id: string; ruleId: string }[]>(`/v1/tenants/${ws.tenantId}/recommendations`)
  const idle = recs.find((r) => r.ruleId.startsWith('R1')) ?? recs[0]
  const pages: [string, string | RegExp][] = [
    ['/overview', 'Overview'],
    ['/costs', 'Cost Explorer'],
    ['/commitments', 'Commitments'],
    [`/recommendations/${idle.id}`, /./],
    ['/anomalies', 'Anomalies'],
  ]
  let charts = 0
  await openSignedIn(page, ws.api.token, ws.tenantId, '/overview')
  for (const [url, heading] of pages) {
    await page.goto(appPath(url))
    await expect(page.getByRole('heading', { level: 1, name: heading }).first()).toBeVisible()
    await page.waitForLoadState('networkidle')
    // An anomaly's chart is in its detail panel.
    const expand = page.locator('main button[aria-expanded="false"]')
    if (url === '/anomalies' && (await expand.count()) > 0) {
      await expand.first().click()
      await page.waitForLoadState('networkidle')
    }
    for (const figure of await page.locator('figure:has(svg[role="img"])').all()) {
      const name = await figure.locator('svg[role="img"]').getAttribute('aria-label')
      expect(name, `${url}: the chart has a text summary`).toBeTruthy()
      await figure.locator('summary').click()
      const table = figure.getByRole('table')
      await expect(table, `${url}: "${name}" has a table`).toBeVisible()
      expect(await table.locator('tbody tr').count(), `${url}: "${name}" table has rows`).toBeGreaterThan(0)
      charts++
    }
  }
  // Overview, Cost Explorer and Commitments each draw daily spend at least.
  expect(charts).toBeGreaterThanOrEqual(3)
})

test('a route change puts the page name in the title and moves focus to its heading', async ({ page }) => {
  await open(page, '/overview', 'Overview')
  await expect(page).toHaveTitle('Overview · Cloudoptics')
  // The first page leaves focus where the browser put it.
  expect((await focused(page)).tag).toBe('body')
  const nav = page.getByRole('navigation', { name: 'Main' })
  await nav.getByRole('link', { name: 'Cost Explorer' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { level: 1, name: 'Cost Explorer' })).toBeFocused()
  await expect(page).toHaveTitle('Cost Explorer · Cloudoptics')
  await nav.getByRole('link', { name: 'Commitments' }).click()
  await expect(page.getByRole('heading', { level: 1, name: /Commitments/ })).toBeFocused()
  await expect(page).toHaveTitle('Commitments · Cloudoptics')
  // From there, Tab goes on into the page.
  await page.keyboard.press('Tab')
  expect((await focused(page)).inMain).toBe(true)
})
