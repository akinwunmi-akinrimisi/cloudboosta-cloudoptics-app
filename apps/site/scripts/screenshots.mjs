// Regenerates the product screenshots in src/assets/screens from the demo build (sample data,
// docs/14 §8.1, SITE-06). Not part of CI: run it by hand after UI changes and commit the PNGs.
//
//   pnpm --filter @cloudoptics/site screenshots
//
// Needs Chrome or Edge installed (playwright-core drives the installed browser; set
// SCREENSHOT_BROWSER to an executable path to use another). Builds apps/web with
// VITE_API_MODE=demo into .demo-build/, serves it locally and walks the demo: sign in, create a
// workspace, connect the simulated AWS account, then capture the screens.
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, normalize } from 'node:path'
import { chromium } from 'playwright-core'

const site = join(import.meta.dirname, '..')
const out = join(site, 'src', 'assets', 'screens')
const build = join(site, '.demo-build')
mkdirSync(out, { recursive: true })

if (!process.env.SKIP_DEMO_BUILD) {
  execFileSync('pnpm', ['--filter', '@cloudoptics/web', 'exec', 'vite', 'build', '--outDir', build, '--emptyOutDir'], {
    cwd: site,
    stdio: 'inherit',
    shell: process.platform === 'win32',
    env: { ...process.env, VITE_API_MODE: 'demo', VITE_BASE: '/' },
  })
}

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.yaml': 'text/yaml' }
const server = createServer((req, res) => {
  const path = normalize(decodeURIComponent(new URL(req.url, 'http://x').pathname)).replace(/^([/\\])+/, '')
  let file = join(build, path)
  if (!file.startsWith(build) || !existsSync(file) || !extname(file)) file = join(build, 'index.html')
  res.writeHead(200, { 'content-type': TYPES[extname(file)] ?? 'application/octet-stream' })
  res.end(readFileSync(file))
}).listen(0)
const base = `http://localhost:${server.address().port}`

const candidates = [
  process.env.SCREENSHOT_BROWSER,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean)
const executablePath = candidates.find((p) => existsSync(p))
if (!executablePath) throw new Error('No Chrome or Edge found; set SCREENSHOT_BROWSER.')

const browser = await chromium.launch({ executablePath })
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, colorScheme: 'light', locale: 'en-GB' })
const page = await context.newPage()
page.setDefaultTimeout(30_000)

try {
  await page.goto(`${base}/#/sign-in`)
  await page.getByLabel('Work email').fill('aisha@kolapay.example')
  await page.getByRole('button', { name: /continue/i }).click()
  await page.getByLabel(/company or team name/i).fill('Kola Pay')
  await page.getByLabel('Country').selectOption('NG')
  await page.getByRole('button', { name: /create workspace/i }).click()
  // The demo's link opens the real AWS console in a new tab; block it, the demo connects anyway.
  await context.route(/amazon\.com|aws\.amazon/, (route) => route.abort())
  // Without an https template URL the demo shows the "by hand" path instead of the one-click link.
  const quick = page.getByRole('link', { name: /open aws and connect/i })
  const byHand = page.getByRole('button', { name: /i have created the stack/i })
  await quick.or(byHand).first().waitFor()
  await ((await quick.count()) ? quick.first() : byHand).click()
  await page.getByRole('button', { name: /go to my workspace/i }).waitFor({ timeout: 120_000 })
  for (const p of context.pages()) if (p !== page) await p.close()
  await page.getByRole('button', { name: /go to my workspace/i }).click()
  await page.waitForURL(/overview/)
  await page.waitForLoadState('networkidle')
  await page.waitForTimeout(1500)

  const shoot = async (name, url, clip) => {
    if (url) {
      await page.goto(`${base}/#${url}`)
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(1500)
    }
    await page.screenshot({ path: join(out, `${name}.png`), clip })
    console.log(`saved ${name}.png`)
  }
  await shoot('overview')
  await shoot('recommendations', '/recommendations')
  const first = page.getByRole('link').filter({ hasText: /unattached/i }).first()
  if (await first.count()) {
    await first.click()
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(1500)
    await shoot('recommendation')
  }
  await shoot('costs', '/costs')
} catch (err) {
  await page.screenshot({ path: join(site, '.demo-build', 'failure.png') })
  console.error(`Failed on ${page.url()}; see .demo-build/failure.png`)
  throw err
} finally {
  await browser.close()
  server.close()
}
