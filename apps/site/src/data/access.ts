// The short "Reads" list on the landing and security pages, derived from the generated permission
// data (template → permissions.yaml → permissions.json), not written by hand. Each permission
// group of the two read roles is filed under one heading by the AWS service of its actions; a
// group that fits no heading fails the build, so a new permission cannot go unmentioned. The
// "Never" list is permissions.cannot, unchanged.
import { permissions, readRoles } from '../config'
import type { IconName } from '../components/icons'

interface Heading {
  id: string
  label: string
  icon: IconName
  /** AWS service prefixes; `role:service` limits a service to one role. */
  services: string[]
}

const HEADINGS: Heading[] = [
  { id: 'costs', label: 'Your bill, costs and AWS prices', icon: 'receipt', services: ['ce', 'cur', 'bcm-data-exports', 'pricing', 'BillingRole:s3'] },
  { id: 'commitments', label: 'Savings Plans and budgets', icon: 'wallet', services: ['savingsplans', 'budgets'] },
  { id: 'settings', label: 'Server, disk, database and bucket settings', icon: 'server', services: ['ec2', 'rds', 'elasticloadbalancing', 'eks', 'logs', 'ResourceRole:s3'] },
  { id: 'metrics', label: 'Usage metrics like CPU', icon: 'activity', services: ['cloudwatch'] },
  { id: 'advice', label: "AWS's own savings recommendations", icon: 'layers', services: ['compute-optimizer', 'cost-optimization-hub'] },
  { id: 'accounts', label: 'Tags, account names and coverage', icon: 'tag', services: ['tag', 'organizations', 'cloudformation'] },
]

const service = (action: string) => action.split(':')[0]!

export interface ReadLine {
  id: string
  label: string
  icon: IconName
  actions: number
}

export const reads: ReadLine[] = HEADINGS.map((h) => ({ id: h.id, label: h.label, icon: h.icon, actions: 0 }))

for (const role of readRoles) {
  for (const group of role.groups) {
    const svc = service(group.actions[0]!)
    const i = HEADINGS.findIndex((h) => h.services.includes(svc) || h.services.includes(`${role.logicalId}:${svc}`))
    if (i < 0) throw new Error(`access.ts: no "Reads" heading for ${role.logicalId} group "${group.plain}" (${svc}). Add one.`)
    reads[i]!.actions += group.actions.length
  }
}
for (const r of reads) if (r.actions === 0) throw new Error(`access.ts: "Reads" heading ${r.id} matches no permission. Remove it.`)

export const readActionCount = readRoles.reduce((n, r) => n + r.actionCount, 0)
if (reads.reduce((n, r) => n + r.actions, 0) !== readActionCount) throw new Error('access.ts: "Reads" counts do not add up to the read roles')

export const never: string[] = permissions.cannot
