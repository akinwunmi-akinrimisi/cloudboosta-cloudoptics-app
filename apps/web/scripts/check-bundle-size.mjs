// Fails the build when the JS exceeds the budget (PRD FR-UI-10 / NFR-PERF-03: app shell
// ≤ 300 KB gzipped, for 3G-class networks). Pages are split by route (src/app/router.tsx),
// so it reports what the first page load fetches (the entry script and the chunks
// index.html preloads) and, as the budget, every chunk together: the stricter of the two.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'

const BUDGET_KB = 300
const dist = join(import.meta.dirname, '..', 'dist')
const dir = join(dist, 'assets')
const gz = (f) => gzipSync(readFileSync(join(dir, f))).length / 1024
const chunks = readdirSync(dir).filter((f) => f.endsWith('.js'))
const html = readFileSync(join(dist, 'index.html'), 'utf8')
const initial = [...html.matchAll(/(?:src|href)="[^"]*\/assets\/([^"/]+\.js)"/g)].map((m) => m[1])
const initialKb = initial.reduce((sum, f) => sum + gz(f), 0)
const totalKb = chunks.reduce((sum, f) => sum + gz(f), 0)
console.log(`First load JS: ${initialKb.toFixed(1)} KB gzipped (${initial.length} files)`)
console.log(`All JS: ${totalKb.toFixed(1)} KB gzipped in ${chunks.length} chunks (budget ${BUDGET_KB} KB)`)
if (initial.length === 0) {
  console.error('No script found in dist/index.html')
  process.exit(1)
}
if (totalKb > BUDGET_KB) {
  console.error('Bundle-size budget exceeded')
  process.exit(1)
}
