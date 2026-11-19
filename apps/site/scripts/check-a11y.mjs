// Accessibility checks for the built site (docs/14 §8.2), with tools already in the repo:
//   1. axe-core on every page in jsdom (WCAG 2.0-2.2 A/AA rules and best practices). jsdom does
//      no layout, so axe's colour-contrast rule cannot run here; check 2 covers colour instead.
//   2. Every foreground/background pair in packages/design-tokens/contrast-pairs.json, in the
//      light and the dark theme, read from tokens.css (docs/08 §2.2.1).
// A browser-based axe pass (Playwright) with real layout belongs to the E2E work (SITE-18).
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { JSDOM, VirtualConsole } from 'jsdom'
import { htmlFiles } from './strip-html-comments.mjs'

const require = createRequire(import.meta.url)
const axeSource = readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8')
const dist = join(import.meta.dirname, '..', 'dist')
const problems = []
// jsdom has no canvas; axe probes for one and the 'not implemented' notice is noise.
const virtualConsole = new VirtualConsole()

// 1. axe
for (const file of htmlFiles(dist)) {
  const dom = new JSDOM(readFileSync(file, 'utf8'), { runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole })
  dom.window.eval(axeSource)
  const result = await dom.window.axe.run(dom.window.document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] },
    rules: { 'color-contrast': { enabled: false } },
  })
  const name = file.slice(dist.length).replaceAll('\\', '/')
  for (const v of result.violations) {
    problems.push(`${name}: [${v.impact}] ${v.id}: ${v.help} (${v.nodes.length}): ${v.nodes[0]?.target.join(' ')}`)
  }
  console.log(`axe ${name}: ${result.violations.length} violation(s), ${result.passes.length} rules passed`)
  dom.window.close()
}

// 2. Contrast of the token pairs
const tokensDir = join(import.meta.dirname, '..', 'node_modules', '@cloudoptics', 'design-tokens')
const css = readFileSync(join(tokensDir, 'tokens.css'), 'utf8')
const pairs = JSON.parse(readFileSync(join(tokensDir, 'contrast-pairs.json'), 'utf8'))
const block = (re) => Object.fromEntries([...(css.match(re)?.[1] ?? '').matchAll(/(--[\w-]+):\s*(#[0-9a-fA-F]{6})/g)].map((m) => [m[1], m[2]]))
const light = block(/:root\s*\{([^}]*)\}/)
const dark = { ...light, ...block(/:root:not\(\[data-theme='light'\]\)\s*\{([^}]*)\}/) }
if (Object.keys(light).length < 10 || Object.keys(dark).length === Object.keys(light).length && dark['--bg'] === light['--bg']) {
  problems.push('could not read the light and dark tokens from tokens.css')
}
const lum = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
const ratio = (a, b) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}
for (const [theme, vars] of [['light', light], ['dark', dark]]) {
  const colour = (name) => (name.startsWith('--') ? vars[name] : pairs.fixed[name])
  for (const p of pairs.pairs) {
    const fg = colour(p.fg)
    const bg = colour(p.bg)
    if (!fg || !bg) {
      problems.push(`contrast: unknown colour in pair ${p.fg} on ${p.bg}`)
      continue
    }
    const r = ratio(fg, bg)
    if (r < p.min) problems.push(`contrast (${theme}): ${p.fg} ${fg} on ${p.bg} ${bg} is ${r.toFixed(2)}:1, needs ${p.min}:1 (${p.use})`)
  }
}
console.log(`contrast: ${pairs.pairs.length} pairs checked in light and dark themes`)

if (problems.length) {
  console.error(`\n${problems.length} accessibility problem(s):`)
  for (const p of problems) console.error(`  - ${p}`)
  process.exit(1)
}
console.log('Accessibility checks passed.')
