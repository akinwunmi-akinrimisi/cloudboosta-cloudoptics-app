// Copies the customer-facing CloudFormation templates into the web build. Only the demo uses
// them (its downloads are relative to the build, e.g. /demo/cfn/...); staging and production
// serve /cfn/* from the public site (apps/site, SITE-13). infra/cloudformation is the source.
import { cpSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

const src = join(import.meta.dirname, '..', '..', '..', 'infra', 'cloudformation')
const dest = join(import.meta.dirname, '..', 'public', 'cfn')
mkdirSync(dest, { recursive: true })
for (const name of ['cloudoptics-access.yaml', 'billing-role.yaml', 'resource-role.yaml', 'resource-stackset.yaml'])
  cpSync(join(src, name), join(dest, name))
