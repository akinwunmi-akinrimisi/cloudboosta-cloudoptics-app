// SITE-15: the first-party beacon sends anonymous events, honours DNT/GPC, skips /demo/ and
// data-no-analytics, stores nothing in the browser, and carries the campaign to sign-up.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { JSDOM } from 'jsdom'
import { allowed, carried, ctaFor, payload, start } from '../src/scripts/beacon.mjs'

const BEACON = '<meta name="cloudoptics:beacon" content="/api/v1/site/visits" data-app="/app" data-demo="https://demo.example/">'
const LINKS = `
  <a id="start" href="/app/sign-in?intent=signup">Start free</a>
  <a id="signin" href="/app/sign-in">Sign in</a>
  <a id="demo" href="https://demo.example/">Demo</a>
  <a id="demo-local" href="/demo/">Demo</a>
  <a id="template" href="/cfn/cloudoptics-access.yaml">Template</a>
  <a id="how" href="/how-it-works">How</a>
  <a id="anchor" href="#faq">FAQ</a>
  <a id="away" href="https://aws.amazon.com/">AWS</a>
  <a id="custom" data-cta="pricing" href="/#pricing">Pricing</a>`

function page({ url = 'https://cloudoptics.example/', referrer = '', head = BEACON, body = LINKS, nav = {}, win = {} } = {}) {
  const dom = new JSDOM(`<!doctype html><html><head>${head}</head><body>${body}</body></html>`, { url, ...(referrer && { referrer }) })
  const w = dom.window
  for (const [k, v] of Object.entries(nav)) Object.defineProperty(w.navigator, k, { value: v, configurable: true })
  Object.assign(w, win)
  const sent = []
  w.fetch = (target, init) => {
    sent.push({ target, init, body: JSON.parse(init.body) })
    return Promise.resolve()
  }
  return { w, sent }
}

test('a page view is sent with path, referrer host and campaign, and nothing else', () => {
  const { w, sent } = page({ url: 'https://cloudoptics.example/how-it-works?utm_source=linkedin&utm_campaign=beta&email=x', referrer: 'https://www.linkedin.com/feed/' })
  start(w)
  assert.equal(sent.length, 1)
  const [{ target, init, body }] = sent
  assert.equal(target, '/api/v1/site/visits')
  assert.equal(init.credentials, 'omit')
  assert.equal(init.headers['content-type'], 'text/plain')
  assert.deepEqual(body, { kind: 'page_view', path: '/how-it-works', landing: true, referrerHost: 'www.linkedin.com', utmSource: 'linkedin', utmCampaign: 'beta' })
  // No cookie, no browser storage.
  assert.equal(w.document.cookie, '')
  assert.equal(w.localStorage.length, 0)
  assert.equal(w.sessionStorage.length, 0)
})

test('a visit from this site is not a landing and names no referrer', () => {
  const loc = new URL('https://cloudoptics.example/security')
  assert.deepEqual(JSON.parse(payload('page_view', loc, 'cloudoptics.example')), { kind: 'page_view', path: '/security', landing: false })
})

test('clicks on calls to action are sent with their name', () => {
  const { w, sent } = page()
  start(w)
  for (const id of ['start', 'signin', 'demo', 'demo-local', 'template', 'custom', 'how', 'away']) {
    w.document.getElementById(id).addEventListener('click', (e) => e.preventDefault())
    w.document.getElementById(id).click()
  }
  assert.deepEqual(
    sent.slice(1).map((s) => s.body.cta),
    ['start', 'sign_in', 'demo', 'demo', 'template', 'pricing'],
  )
})

test('ctaFor works with the current layout where the app is at the root', () => {
  const { w } = page({ body: '<a id="s" href="/sign-in?intent=signup">x</a><a id="i" href="/sign-in">y</a>' })
  assert.equal(ctaFor(w.document.getElementById('s'), '', ''), 'start')
  assert.equal(ctaFor(w.document.getElementById('i'), '', ''), 'sign_in')
})

test('Do Not Track, Global Privacy Control, /demo/, data-no-analytics and a missing meta send nothing', () => {
  const cases = [
    { nav: { doNotTrack: '1' } },
    { win: { doNotTrack: '1' } },
    { nav: { globalPrivacyControl: true } },
    { url: 'https://cloudoptics.example/demo/' },
    { url: 'https://cloudoptics.example/demo' },
    { url: 'https://cloudoptics.example/demo/costs' },
    { body: `<div data-no-analytics></div>${LINKS}` },
    { head: '' },
  ]
  for (const c of cases) {
    const { w, sent } = page(c)
    assert.equal(allowed(w), false, JSON.stringify(c))
    start(w)
    w.document.getElementById('start')?.click()
    assert.deepEqual(sent, [], JSON.stringify(c))
  }
  const { w } = page({ url: 'https://cloudoptics.example/demonstration' })
  assert.equal(allowed(w), true)
})

test('the campaign and landing page travel on this site’s links to sign-up', () => {
  const { w } = page({ url: 'https://cloudoptics.example/security?utm_source=newsletter&utm_medium=email&gclid=abc' })
  start(w)
  const href = (id) => w.document.getElementById(id).getAttribute('href')
  assert.equal(href('start'), 'https://cloudoptics.example/app/sign-in?intent=signup&utm_source=newsletter&utm_medium=email&landing=%2Fsecurity')
  assert.equal(href('how'), 'https://cloudoptics.example/how-it-works?utm_source=newsletter&utm_medium=email&landing=%2Fsecurity')
  // Not onto other sites, downloads, the demo or same-page anchors; unknown params never travel.
  assert.equal(href('away'), 'https://aws.amazon.com/')
  assert.equal(href('template'), '/cfn/cloudoptics-access.yaml')
  assert.equal(href('demo-local'), '/demo/')
  assert.equal(href('anchor'), '#faq')
  assert.ok(!href('start').includes('gclid'))
})

test('carried params: first touch wins, an outside referrer becomes ref, direct visits carry nothing', () => {
  assert.equal(carried('', 'www.google.com', 'site.example', '/').toString(), 'ref=www.google.com&landing=%2F')
  assert.equal(carried('', '', 'site.example', '/').toString(), '')
  assert.equal(carried('', 'site.example', 'site.example', '/security').toString(), '')
  // On a later page the first touch is already in the URL and is kept as it is.
  assert.equal(carried('?utm_source=x&landing=%2F', 'site.example', 'site.example', '/security').toString(), 'utm_source=x&landing=%2F')
  assert.equal(carried(`?utm_source=${'a'.repeat(300)}`, '', 'site.example', '/').get('utm_source').length, 100)
})

test('hosting layout H1: the demo at /demo/ on this origin is a CTA, never decorated, never tracked', () => {
  const head = '<meta name="cloudoptics:beacon" content="/api/v1/site/visits" data-app="/app" data-demo="/demo/">'
  const { w, sent } = page({ url: 'https://cloudoptics.example/?utm_source=x', head })
  start(w)
  assert.equal(sent[0].target, '/api/v1/site/visits')
  const demo = w.document.getElementById('demo-local')
  assert.equal(demo.getAttribute('href'), '/demo/')
  demo.addEventListener('click', (e) => e.preventDefault())
  demo.click()
  assert.equal(sent.at(-1).body.cta, 'demo')
  assert.match(w.document.getElementById('start').getAttribute('href'), /^https:\/\/cloudoptics\.example\/app\/sign-in\?intent=signup&utm_source=x&landing=%2F$/)
  assert.equal(allowed(page({ url: 'https://cloudoptics.example/demo/overview?utm_source=x', head }).w), false)
})
