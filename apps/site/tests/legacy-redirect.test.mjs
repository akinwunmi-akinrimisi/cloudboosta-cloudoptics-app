// SITE-13: old app addresses from sent emails and bookmarks land on the site's 404 page, which
// forwards them into the app under /app (public/legacy-redirect.js).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import vm from 'node:vm'

const code = readFileSync(join(import.meta.dirname, '..', 'public', 'legacy-redirect.js'), 'utf8')

function forward(url) {
  const u = new URL(url, 'https://cloudoptics.example')
  let target = null
  const location = { pathname: u.pathname, search: u.search, hash: u.hash, replace: (to) => (target = to) }
  vm.runInNewContext(code, { window: { location } })
  return target
}

test('old app paths go to the same page under /app, with query and hash', () => {
  assert.equal(forward('/overview'), '/app/overview')
  assert.equal(forward('/sign-in?intent=signup'), '/app/sign-in?intent=signup')
  assert.equal(forward('/onboarding'), '/app/onboarding')
  assert.equal(forward('/recommendations/3f2a'), '/app/recommendations/3f2a')
  assert.equal(forward('/settings/connections'), '/app/settings/connections')
  assert.equal(forward('/settings?section=workspace'), '/app/settings?section=workspace')
  assert.equal(forward('/reports?month=2026-10'), '/app/reports?month=2026-10')
  assert.equal(forward('/overview#main'), '/app/overview#main')
  for (const p of ['/costs', '/anomalies', '/budgets', '/commitments', '/staff']) assert.equal(forward(p), `/app${p}`)
})

test('anything else stays on the 404 page', () => {
  for (const p of ['/nope', '/overviews', '/app/missing', '/demo/x', '/how-it-works/old', '/cfn/missing.yaml', '/']) {
    assert.equal(forward(p), null, p)
  }
})

test('the 404 page loads the script from this origin (the CSP allows self only)', () => {
  const page = readFileSync(join(import.meta.dirname, '..', 'src', 'pages', '404.astro'), 'utf8')
  assert.match(page, /<script is:inline slot="head" src="\/legacy-redirect\.js"><\/script>/)
})
