// Copies the customer-facing CloudFormation templates into the site, which serves /cfn/* for
// the whole origin (docs/14 §7.2 item 4, SITE-13): CLOUDOPTICS_CFN_TEMPLATE_DOWNLOAD_BASE points
// at https://<host>/cfn. infra/cloudformation stays the single source of truth.
import { cpSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'

const src = join(import.meta.dirname, '..', '..', '..', 'infra', 'cloudformation')
const dest = join(import.meta.dirname, '..', 'public', 'cfn')
const TEMPLATES = [
  'cloudoptics-access.yaml',
  'billing-role.yaml',
  'resource-role.yaml',
  'resource-stackset.yaml',
  'scheduler/cloudoptics-scheduler.yaml',
]
for (const name of TEMPLATES) {
  mkdirSync(dirname(join(dest, name)), { recursive: true })
  cpSync(join(src, name), join(dest, name))
}
console.log(`Templates: ${TEMPLATES.length} copied to public/cfn.`)
