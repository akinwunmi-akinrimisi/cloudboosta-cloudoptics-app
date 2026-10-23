// Copies the customer-facing CloudFormation templates into the web build, so the app can
// offer them as same-origin downloads (infra/cloudformation is the single source of truth).
import { cpSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

const src = join(import.meta.dirname, '..', '..', '..', 'infra', 'cloudformation')
const dest = join(import.meta.dirname, '..', 'public', 'cfn')
mkdirSync(dest, { recursive: true })
for (const name of ['billing-role.yaml', 'resource-role.yaml', 'resource-stackset.yaml'])
  cpSync(join(src, name), join(dest, name))
