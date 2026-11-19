// k6 load test for the main read endpoints and the bill (CUR) ingest path (docs/11f §1.5).
//
//   Local smoke (CI):  k6 run apps/e2e/load/api.js                         (PROFILE=smoke)
//   Staging, beta:     see apps/e2e/load/README.md. Needs the owner's approval first.
//
// Thresholds come from the PRD's NFRs (docs/06 §5.1):
//   NFR-PERF-01  dashboard and cost-explorer queries: p95 <= 2.0 s, p99 <= 4 s
//   NFR-PERF-02  other (non-analytical) endpoints:    p95 <= 500 ms
// NFR-PERF-04 (5M CUR rows in <= 30 min per tenant-month) is a throughput target for the
// worker; the `ingest` scenario here loads the small fixture bill through the API and only
// guards against regressions in that path (see README.md for the full-size run).
//
// Environment:
//   BASE_URL         API address (default http://127.0.0.1:8000, the E2E stack)
//   PROFILE          smoke (default) | beta
//   WORKSPACES_FILE  JSON [{ "token": "...", "tenantId": "..." }, ...]: use existing
//                    workspaces (staging, OIDC) instead of creating them with dev sign-in
//   INGEST           "local" (default when WORKSPACES_FILE is unset) runs the ingest
//                    scenario against the E2E stack's fake AWS; anything else skips it
//   VUS, DURATION    ad-hoc constant load for the reads (e.g. VUS=12 DURATION=2m)
//   BATCH            requests each user sends at once (batchPerHost; smoke 3, beta 6)

import { check, fail, group, sleep } from 'k6'
import exec from 'k6/execution'
import http from 'k6/http'
import { Trend } from 'k6/metrics'

const BASE = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')
const PROFILE = __ENV.PROFILE || 'smoke'
const FILE = __ENV.WORKSPACES_FILE
const INGEST = (__ENV.INGEST || (FILE ? 'off' : 'local')) === 'local'
const supplied = FILE ? JSON.parse(open(FILE)) : null

const ingestSeconds = new Trend('ingest_seconds')

// smoke: a few users for a minute, small enough for every CI run on one runner.
// beta: 8-12 design partners (docs/06 Increment 2), a few people each, at 3x the expected
// peak as docs/11f §1.5 asks: 36 concurrent users for 10 minutes.
const PROFILES = {
  smoke: { workspaces: 3, reads: { executor: 'constant-vus', vus: 3, duration: '1m' }, ingests: 3, batch: 3 },
  beta: {
    workspaces: 12,
    batch: 6,
    reads: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '2m', target: 36 },
        { duration: '6m', target: 36 },
        { duration: '2m', target: 0 },
      ],
    },
    ingests: 12,
  },
}
const plan = PROFILES[PROFILE]
if (!plan) throw new Error(`unknown PROFILE ${PROFILE}`)
// Ad-hoc runs: VUS and DURATION turn the reads into a constant load; BATCH sets batchPerHost.
if (__ENV.VUS) plan.reads = { executor: 'constant-vus', vus: Number(__ENV.VUS), duration: __ENV.DURATION || '1m' }
if (__ENV.BATCH) plan.batch = Number(__ENV.BATCH)

const scenarios = { reads: { ...plan.reads, exec: 'reads', gracefulStop: '30s' } }
if (INGEST) {
  scenarios.ingest = {
    executor: 'per-vu-iterations',
    vus: 1,
    iterations: plan.ingests,
    exec: 'ingest',
    maxDuration: '5m',
  }
}

export const options = {
  scenarios,
  // Requests a page sends at once (browsers send about 6 per host).
  batchPerHost: plan.batch,
  thresholds: {
    'http_req_duration{kind:cost}': ['p(95)<2000', 'p(99)<4000'],
    'http_req_duration{kind:api}': ['p(95)<500'],
    'http_req_failed{kind:cost}': ['rate<0.01'],
    'http_req_failed{kind:api}': ['rate<0.01'],
    // The fixture bill is small: if loading it through the API takes this long, something
    // regressed. Not NFR-PERF-04 itself (see the header).
    ingest_seconds: ['p(95)<30'],
    checks: ['rate>0.99'],
    // Stop early rather than keep loading a struggling environment.
    http_req_failed: [{ threshold: 'rate<0.05', abortOnFail: true, delayAbortEval: '1m' }],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
}

function json(res) {
  try {
    return res.json()
  } catch {
    return null
  }
}

function post(path, body, token, tags) {
  const headers = { 'content-type': 'application/json' }
  if (token) headers.authorization = `Bearer ${token}`
  return http.post(`${BASE}${path}`, body === undefined ? null : JSON.stringify(body), { headers, tags })
}

function digits(n) {
  let s = ''
  for (let i = 0; i < n; i++) s += Math.floor(Math.random() * 10)
  return s
}

/** E2E stack only: dev sign-in, a workspace, the stack's callback (fake AWS validates it). */
function connectedWorkspace(label) {
  const setupTags = { kind: 'setup' }
  const email = `load-${label}-${digits(8)}@example.com`
  const token = json(post('/v1/dev/token', { email, name: 'Load Test' }, null, setupTags))?.accessToken
  if (!token) fail(`dev sign-in failed at ${BASE}: is this the E2E stack?`)
  const tenant = json(post('/v1/tenants', { name: `Load ${label}`, countryCode: 'NG' }, token, setupTags))
  const intent = json(post(`/v1/tenants/${tenant.id}/connections/intents`, undefined, token, setupTags))
  const account = digits(12)
  const callback = json(
    post(
      '/v1/cfn/callback',
      {
        requestType: 'Create',
        ref: intent.connectionRef,
        tenantId: tenant.id,
        externalId: intent.externalId,
        accountId: account,
        management: true,
        curBucket: `cloudoptics-cur-${account}`,
        curPrefix: 'cloudoptics',
        stackName: 'cloudoptics-access',
        templateVersion: '2.0.4',
      },
      null,
      setupTags,
    ),
  )
  if (!callback?.connectionId) fail('the stack callback was refused')
  return { token, tenantId: tenant.id, connectionId: callback.connectionId }
}

export function setup() {
  const ready = json(http.get(`${BASE}/readyz`))
  if (!ready || ready.status !== 'ready') fail(`${BASE} is not ready`)
  if (supplied) return { workspaces: supplied }
  if (ready.environment !== 'test') fail('without WORKSPACES_FILE this test only runs against the E2E stack')
  const workspaces = []
  for (let i = 0; i < plan.workspaces; i++) {
    const ws = connectedWorkspace(`read${i}`)
    const base = `/v1/tenants/${ws.tenantId}/connections/${ws.connectionId}`
    check(post(`${base}/ingest`, undefined, ws.token, { kind: 'setup' }), { 'seed: bills loaded': (r) => r.status === 200 })
    check(post(`${base}/scan`, undefined, ws.token, { kind: 'setup' }), { 'seed: resources scanned': (r) => r.status === 200 })
    workspaces.push({ token: ws.token, tenantId: ws.tenantId })
  }
  return { workspaces }
}

const COST_READS = [
  ['summary', '/costs/summary'],
  ['daily', '/costs/daily'],
  ['breakdown', '/costs/breakdown?dimension=service'],
  ['breakdown-account', '/costs/breakdown?dimension=account'],
  ['forecast', '/costs/forecast'],
  ['changes', '/costs/changes'],
]
const OTHER_READS = [
  ['tenant', ''],
  ['fx', '/fx/current'],
  ['recommendations', '/recommendations'],
  ['recommendation-summary', '/recommendations/summary'],
  ['budgets', '/budgets'],
  ['connections', '/connections'],
  ['anomalies', '/anomalies'],
]

/** One person opening Overview, then Cost Explorer, then Recommendations. */
export function reads(data) {
  const ws = data.workspaces[exec.vu.idInTest % data.workspaces.length]
  const params = (kind, name) => ({ headers: { authorization: `Bearer ${ws.token}` }, tags: { kind, name } })
  const t = `${BASE}/v1/tenants/${ws.tenantId}`
  group('api', () => {
    const me = http.get(`${BASE}/v1/me`, params('api', 'me'))
    check(me, { 'me 200': (r) => r.status === 200 })
    const responses = http.batch(OTHER_READS.map(([name, path]) => ['GET', `${t}${path}`, null, params('api', name)]))
    responses.forEach((r, i) => check(r, { [`${OTHER_READS[i][0]} 200`]: (x) => x.status === 200 }))
  })
  group('cost', () => {
    const responses = http.batch(COST_READS.map(([name, path]) => ['GET', `${t}${path}`, null, params('cost', name)]))
    responses.forEach((r, i) => check(r, { [`${COST_READS[i][0]} 200`]: (x) => x.status === 200 }))
  })
  sleep(PROFILE === 'smoke' ? 1 : 3 + Math.random() * 4)
}

/** A connection's bills loaded through the API: the fixture's two months, read and stored. */
export function ingest() {
  const ws = connectedWorkspace(`ingest${exec.scenario.iterationInTest}`)
  const started = Date.now()
  const r = post(`/v1/tenants/${ws.tenantId}/connections/${ws.connectionId}/ingest`, undefined, ws.token, {
    kind: 'ingest',
    name: 'ingest',
  })
  ingestSeconds.add((Date.now() - started) / 1000)
  check(r, {
    'ingest 200': (x) => x.status === 200,
    'ingest succeeded': (x) => json(x)?.status === 'succeeded',
  })
}
