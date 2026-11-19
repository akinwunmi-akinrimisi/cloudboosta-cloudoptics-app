// Renders the 1200×630 social card (public/og/cloudoptics.png) with the installed Chrome or Edge.
// Not part of CI: run by hand when the wording changes (SITE-06 replaces it with designed cards).
import { existsSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { chromium } from 'playwright-core'

const out = join(import.meta.dirname, '..', 'public', 'og')
mkdirSync(out, { recursive: true })
const executablePath = [
  process.env.SCREENSHOT_BROWSER,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].find((p) => p && existsSync(p))

const html = `<!doctype html><html><body style="margin:0">
<div style="width:1200px;height:630px;box-sizing:border-box;padding:72px 80px;background:#0b1f4b;color:#fff;
  font-family:'Segoe UI',system-ui,sans-serif;display:flex;flex-direction:column;justify-content:space-between">
  <div style="display:flex;align-items:center;gap:20px">
    <svg viewBox="0 0 64 64" width="72" height="72"><rect width="64" height="64" rx="14" fill="#0B1F4B" stroke="#ffffff33"/>
      <circle cx="30" cy="34" r="17" fill="none" stroke="#FFF" stroke-width="5"/>
      <path d="M19 34a11 11 0 0 1 11-11" fill="none" stroke="#5B8CFF" stroke-width="5" stroke-linecap="round"/>
      <circle cx="47" cy="17" r="6" fill="#FAC812"/></svg>
    <div><div style="font-size:40px;font-weight:700">Cloudoptics</div><div style="font-size:24px;color:#b8c4de">by Cloudboosta</div></div>
  </div>
  <div style="font-size:64px;font-weight:700;line-height:1.1;max-width:1000px">Your AWS bill in dollars and naira. And what's safe to cut.</div>
  <div style="display:flex;gap:16px;font-size:26px">
    <span style="background:#fac812;color:#0b1f4b;padding:8px 16px;border-radius:6px;font-weight:600">Read-only access</span>
    <span style="color:#b8c4de;padding:8px 0">Set up in about 15 minutes · Free during the beta</span>
  </div>
</div></body></html>`

const browser = await chromium.launch({ executablePath })
const page = await browser.newPage({ viewport: { width: 1200, height: 630 } })
await page.setContent(html)
await page.screenshot({ path: join(out, 'cloudoptics.png') })
await browser.close()
console.log('saved public/og/cloudoptics.png')
