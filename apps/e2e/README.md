# E2E and load tests

Playwright journeys and an axe pass (`tests/`), and a k6 load test (`load/`), against a local
stack: PostgreSQL, the real API (dev sign-in, inline jobs), the production web build served by
`vite preview`, and fake AWS. Nothing contacts AWS, staging or production (docs/11f §1.4-1.5).

## The stack

`server/serve.py` is a test-only launcher for the API. It refuses to start unless
`CLOUDOPTICS_E2E=1`, the environment is `test`, sign-in is the dev token and jobs are inline;
it is not in the API image. It then:

- drops and recreates the database `cloudoptics_e2e` (the name must end in `_e2e`) and migrates it;
- writes last month's and this month's synthetic CUR (tools/cur-fixture) into `.bills/`, laid out
  like the export bucket;
- replaces the AWS session factory with `server/fake_aws.py`: every role can be assumed, the
  billing checks pass, Cost Explorer agrees with the bills, and one account (the fixture's payer,
  111111111111) has an idle instance, an unattached gp2 volume, an old snapshot, a free Elastic IP
  and an idle load balancer in eu-west-1;
- raises the per-address limits (every test signs in from 127.0.0.1) and lowers the per-user
  limit to 240 a minute, so the rate-limit journey reaches it quickly.

Each test makes its own workspace, through the API for set-up (`tests/support.ts`) and through the
page for what the test is about. The stack's CloudFormation callback is sent by the test with the
values the onboarding page shows. Cloudoptics staff set an NGN rate for every day since the start
of last month (`tests/global.setup.ts`). Staff are `fx-staff@example.com`
(`CLOUDOPTICS_PLATFORM_ADMIN_EMAILS` in `server/serve.py`); the staff console needs a multi-factor
session too, which `signInStaff()` asks for, and `seedSupportGrant()` opens a read-only grant so
the console has a row to show.

## Run it

From the repository root, with PostgreSQL on localhost:5432 (postgres/postgres; set
`CLOUDOPTICS_E2E_ADMIN_URL` otherwise):

```sh
uv sync --all-packages --locked
pnpm install
pnpm --filter @cloudoptics/e2e exec playwright install chromium webkit
pnpm --filter @cloudoptics/e2e test          # starts the API and the web app itself
pnpm --filter @cloudoptics/e2e test --project=chromium   # one browser
pnpm --filter @cloudoptics/e2e report        # the HTML report
```

Playwright reuses servers already running on :8000 and :4173 (except in CI). The API resets its
database each time it starts. To run a second stack beside one already on those ports, set
`E2E_API_URL`, `E2E_WEB_URL` and `CLOUDOPTICS_E2E_DATABASE` (a name ending in `_e2e`), e.g.
`http://127.0.0.1:8130`, `http://127.0.0.1:4290` and `cloudoptics_mine_e2e`: the config starts the
API and `vite preview` on those ports and allows that origin. Reports go to `reports/`: `html/`,
`junit.xml`, `a11y/<project>/` (`summary.md` for desktop, `phone.md`, one JSON file per page), and
`performance/overview.json`.

## Browsers

| Project | Device | Specs |
|---|---|---|
| `chromium` | Desktop Chrome | all |
| `webkit` | Desktop Safari | `onboarding`, `a11y`, `keyboard` |
| `mobile-webkit` | iPhone 13 (user agent, touch, pixel ratio; each test sets its widths) | `mobile` |

The other journeys are API-heavy set-up around ordinary forms and run on Chromium only to keep
the job short; `performance` uses Chrome DevTools throttling, so it is Chromium only.

Safari Tabs only between form fields and buttons by default; links need Option+Tab, or Tab with
"Press Tab to highlight each item on a webpage" turned on. Playwright's WebKit builds differ by
platform (on Windows neither Tab nor Alt+Tab reaches a link), so `linkTabKey()` in
`tests/support.ts` asks the browser which key reaches a link. Where none does, the keyboard spec
says so in the report (annotation `webkit-tab`) and checks links another way: the skip link and
each link get focus after a key press and must show the ring and work; buttons and fields are
still walked with Tab. The phone menu is always checked with plain Tab too: it must keep focus
inside even when the browser would skip its links (it did not, before this suite ran on WebKit).

## Journeys

| Spec | Covers |
|---|---|
| `onboarding` | J1: dev sign-in, create workspace (NG), connect AWS (stack callback), access check, first bills (Reload bills), resource scan, Overview in USD and NGN, recommendations listed |
| `costs` | Overview tiles in USD, naira and both (currency toggle), Cost Explorer by service in both currencies |
| `recommendations` | J3: list, detail, assign to a teammate, dismiss with a reason |
| `budgets` | J4: NGN budget, bills arrive, thresholds alerted once |
| `exports` | Cost Explorer CSV download, formula guard, listed in "Your exports" |
| `settings` | J5: invite a Viewer, they join on sign-in, Viewer refused in UI and API, promote, remove |
| `privacy` | Disconnect an account and delete its data; schedule workspace deletion and cancel it |
| `security` | MFA-required workspace message for a member without MFA; admin with MFA unaffected; rate-limit 429 message |
| `a11y` | axe-core (WCAG 2.0-2.2 A/AA and best practices) on 32 views: the main pages, onboarding (create workspace, connect AWS), the staff console (`/staff`, with a grant listed), opened panels (dismiss form, reload bills, delete confirmation, a chart's table), every main page and the staff console at 390 px, and the phone menu. Any violation fails; `E2E_A11Y_STRICT=0` only reports |
| `keyboard` | What axe cannot see: skip link first and landing on `main`, a focus ring on every control, the phone menu as a modal (focus in, Tab trapped, Escape closes, focus back), Escape on every inline panel, reduced motion, a table behind every chart, page title and focus on route changes |
| `mobile` | No sideways page scroll at 375, 390 and 768 px on the main pages and onboarding, and at 320-768 px on the staff console; the phone menu navigates and closes |
| `performance` | Overview LCP at most 4 s with Chrome's "Fast 3G" (1.6 Mbit/s, 562.5 ms RTT) and the CPU 4x slower, cold cache, production build; writes `reports/performance/overview.json` |

Not covered yet: J6 (monthly report PDF), Firefox, a real iOS device (WebKit's iPhone is
emulated), Slack.

## Load

See [load/README.md](load/README.md).
