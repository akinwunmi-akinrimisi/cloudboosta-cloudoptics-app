# Load tests (k6)

`api.js` loads the main read endpoints and the bill (CUR) ingest path, with thresholds from the
PRD (docs/06 §5.1, docs/11f §1.5):

| Metric | Threshold | Source |
|---|---|---|
| Cost queries (`/costs/summary`, `daily`, `breakdown`, `forecast`, `changes`) | p95 < 2 s, p99 < 4 s, errors < 1% | NFR-PERF-01 |
| Other reads (`/me`, tenant, FX, recommendations, budgets, connections, anomalies) | p95 < 500 ms, errors < 1% | NFR-PERF-02 |
| Fixture bill loaded through `POST .../ingest` | p95 < 30 s | regression guard only; NFR-PERF-04 (5M rows in 30 min) needs the full-size harness below |
| All checks | > 99% pass | |
| Any request | aborts the run above 5% errors (after 1 minute) | safety |

Each read iteration is one person opening Overview, Cost Explorer and Recommendations: the
requests a page sends at once go out together (`batchPerHost`).

## Smoke run (CI and local)

Against the E2E stack (see ../README.md), with the per-user limit raised because one machine
sends every request:

```sh
CLOUDOPTICS_E2E=1 CLOUDOPTICS_RATE_LIMIT_USER_PER_MINUTE=1000000 uv run python apps/e2e/server/serve.py &
k6 run apps/e2e/load/api.js          # PROFILE=smoke: 3 workspaces, 3 users for 1 minute, 3 ingests
```

The `load-smoke` job in `.github/workflows/e2e.yml` does this and uploads `load-summary.json`.
Local result (Windows laptop, 19 Nov 2026): cost p95 217 ms, other p95 279 ms, 0 errors, fixture
ingest p95 3.2 s.

## Finding: more than 15 concurrent requests stall for 30 seconds

At 3 users sending 6 requests at once (18 in flight) every request took 30 s; at 2 users (12 in
flight) p95 was 286 ms:

```sh
INGEST=off VUS=3 BATCH=6 DURATION=40s k6 run apps/e2e/load/api.js   # p95 30.3 s
INGEST=off VUS=2 BATCH=6 DURATION=40s k6 run apps/e2e/load/api.js   # p95 286 ms
```

Cause: each request holds a pooled connection for its transaction, then the rate limiter
(`platform/ratelimit.py`, `hit`) checks out a second connection from the same pool (SQLAlchemy
default: 5 + 10 overflow, 30 s timeout, `platform/db.py`). With 15 requests in flight every
connection is held by a request waiting for one more; each waits 30 s, the limiter gives up
("rate limit not checked") and the request carries on. One browser tab sends up to 6 requests at
once, so three people loading a page together on one API instance can trigger it. The E2E suite
runs two workers for this reason.

This is outside this change (it is product code); options are a separate small pool for the
limiter, counting the hit before the request's session takes a connection, or a larger pool
sized to the instance. Until it is fixed the beta profile below will fail its thresholds.

## Staging run at beta load: plan (needs the owner's approval; not run)

Beta is 8 to 12 design partners (docs/06, Increment 2). `PROFILE=beta` uses 12 workspaces and
ramps to 36 concurrent users (3 per partner, about 3x the expected peak as docs/11f §1.5 asks),
holds for 6 minutes and ramps down: 10 minutes, roughly 4-8 requests per user per 5-10 s.

1. Approval: the owner agrees the time window, and that staging may be slow or unavailable
   during it. Nothing runs before that.
2. Fix the pool finding above first, and redeploy.
3. Test workspaces: 12 synthetic workspaces on staging, each with a test user in WorkOS (no real
   customer, no design partner data) and a bill loaded (the Cloudoptics test AWS account's CUR,
   or the fixture bill loaded by staff). Put `[{ "token": "...", "tenantId": "..." }]` in a
   local `staging-workspaces.json` (never committed). WorkOS access tokens are short-lived:
   mint them just before the run, or split it into runs shorter than the token lifetime; a
   longer run needs a token refresh step, which the script does not have yet.
4. Rate limits: staging allows 300 requests a minute per user. At 36 users over 12 tokens, each
   token sends about 3 x 8 requests per 5-10 s, near the limit; give each virtual user its own
   test user (36 entries) or expect some 429s, which count as errors.
5. Watch during the run: App Platform CPU and memory for the API, PostgreSQL connections and
   CPU, Sentry. Stop (Ctrl-C) if the API's error rate passes 5% for a minute (the script also
   aborts) or PostgreSQL CPU stays above 80%.
6. Run from a machine outside DigitalOcean, close to the users (Lagos or Nairobi if possible):

   ```sh
   BASE_URL=https://<staging host>/api PROFILE=beta WORKSPACES_FILE=staging-workspaces.json \
     INGEST=off k6 run --summary-export beta-summary.json apps/e2e/load/api.js
   ```

7. Ingestion at scale is measured on the worker, not through the API: in staging a reload is
   queued (202) and the worker loads it. NFR-PERF-04 needs a 5M-line synthetic bill
   (docs/11f §1.5 "Python harness with synthetic CUR"; tools/cur-fixture generates the shape,
   not yet that size) in the test account's bucket, one reload, and the `cur.reload` row in
   `sync_run` for the duration. That harness is not built here.
8. Afterwards: schedule the 12 workspaces for deletion, remove the WorkOS test users, and
   record the summary and the dashboards' peaks in the release notes.
