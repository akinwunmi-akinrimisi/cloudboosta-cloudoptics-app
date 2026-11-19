// Checks the plain-language permission entries against the CloudFormation template, both ways,
// and writes src/generated/permissions.json (docs/14 §5.5, SITE-08). Runs before every build and
// typecheck, so CI fails when the template gains or loses an action without a matching entry.
import { cpSync, mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { REPO_ROOT, buildExport, compare, loadData, loadTemplate, templateRoles } from './permissions-lib.mjs'

const data = loadData()
const template = loadTemplate(data.template)
const extra = Object.fromEntries(
  data.roles.filter((r) => r.sameAs).map((r) => [r.sameAs, loadTemplate(r.sameAs)]),
)
const problems = compare(templateRoles(template), data, extra)
if (problems.length) {
  console.error(`permissions.yaml and ${data.template} disagree:`)
  for (const p of problems) console.error(`  - ${p}`)
  console.error('Add, change or remove the entry in apps/site/src/data/permissions.yaml (reviewed by the Tech Lead).')
  process.exit(1)
}
const out = buildExport(template, data)
for (const [key, value] of Object.entries(out.facts)) {
  if (value === null || value === false) {
    console.error(`Could not read "${key}" from ${data.template}; update templateFacts() in permissions-lib.mjs.`)
    process.exit(1)
  }
}
const dir = join(import.meta.dirname, '..', 'src', 'generated')
mkdirSync(dir, { recursive: true })
writeFileSync(join(dir, 'permissions.json'), JSON.stringify(out, null, 2) + '\n')
const counts = out.roles.map((r) => `${r.roleName ?? r.logicalId} ${r.actionCount}`).join(', ')
console.log(`Permissions: template v${out.facts.templateVersion} matches permissions.yaml (${counts} actions).`)

// The security page links to the exact template file (docs/14 §5.6). The site serves it at
// /cfn/cloudoptics-access.yaml, the path the app's downloads use (SITE-13 moves /cfn here).
const cfn = join(import.meta.dirname, '..', 'public', 'cfn')
mkdirSync(cfn, { recursive: true })
cpSync(join(REPO_ROOT, data.template), join(cfn, 'cloudoptics-access.yaml'))
