// Fails the build when the initial JS exceeds the budget (PRD FR-UI-10 / NFR-PERF-03:
// app shell ≤ 300 KB gzipped, for 3G-class networks).
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'

const BUDGET_KB = 300
const dir = join(import.meta.dirname, '..', 'dist', 'assets')
const total = readdirSync(dir)
  .filter((f) => f.endsWith('.js'))
  .reduce((sum, f) => sum + gzipSync(readFileSync(join(dir, f))).length, 0)
const kb = total / 1024
console.log(`Initial JS: ${kb.toFixed(1)} KB gzipped (budget ${BUDGET_KB} KB)`)
if (kb > BUDGET_KB) {
  console.error('Bundle-size budget exceeded')
  process.exit(1)
}
