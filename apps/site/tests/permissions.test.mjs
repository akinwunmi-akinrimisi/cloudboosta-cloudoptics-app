// SITE-08 acceptance: the plain-language permission entries and the template must agree both ways.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildExport, compare, loadData, loadTemplate, templateFacts, templateRoles } from '../scripts/permissions-lib.mjs'

const data = loadData()
const fresh = () => loadTemplate(data.template)
const member = () => ({ 'infra/cloudformation/resource-role.yaml': loadTemplate('infra/cloudformation/resource-role.yaml') })
const statement = (template, roleId, sid) =>
  template.Resources[roleId].Properties.Policies[0].PolicyDocument.Statement.find((s) => s.Sid === sid)

test('the template and permissions.yaml agree today', () => {
  assert.deepEqual(compare(templateRoles(fresh()), data, member()), [])
})

test('an action added to the template without an entry fails', () => {
  const t = fresh()
  statement(t, 'ResourceRole', 'Metrics').Action.push('cloudwatch:GetMetricStatistics')
  const problems = compare(templateRoles(t), data, member())
  assert.ok(problems.some((p) => p.includes('cloudwatch:GetMetricStatistics') && p.includes('no plain-language entry')), problems.join('\n'))
})

test('an action removed from the template while still listed fails', () => {
  const t = fresh()
  const st = statement(t, 'BillingRole', 'CostExplorerAndCommitments')
  st.Action = st.Action.filter((a) => a !== 'budgets:ViewBudget')
  const problems = compare(templateRoles(t), data, member())
  assert.ok(problems.some((p) => p.includes('budgets:ViewBudget') && p.includes('no longer grants')), problems.join('\n'))
})

test('a write action in a read-only role fails even with an entry', () => {
  const t = fresh()
  statement(t, 'BillingRole', 'ReadExportBucketOnly').Action.push('s3:PutObject')
  const withEntry = structuredClone(data)
  withEntry.roles[0].groups.push({ actions: ['s3:PutObject'], plain: 'x', usedFor: 'y' })
  const problems = compare(templateRoles(t), withEntry, member())
  assert.ok(problems.some((p) => p.includes('readOnly') && p.includes('s3:PutObject')), problems.join('\n'))
})

test('a new role in the template fails until it is described', () => {
  const t = fresh()
  t.Resources.ExtraRole = structuredClone(t.Resources.ResourceRole)
  const problems = compare(templateRoles(t), data, member())
  assert.ok(problems.some((p) => p.includes('ExtraRole')), problems.join('\n'))
})

test('the member-account role must match the main resource role', () => {
  const m = member()
  const doc = m['infra/cloudformation/resource-role.yaml']
  doc.Resources.ResourceRole.Properties.Policies[0].PolicyDocument.Statement[0].Action.push('ec2:DescribeLaunchTemplateVersions')
  const problems = compare(templateRoles(fresh()), data, m)
  assert.ok(problems.some((p) => p.includes('resource-role.yaml') && p.includes('ec2:DescribeLaunchTemplateVersions')), problems.join('\n'))
})

test('a read role without the ExternalId condition fails', () => {
  const t = fresh()
  delete t.Resources.BillingRole.Properties.AssumeRolePolicyDocument.Statement[0].Condition
  const problems = compare(templateRoles(t), data, member())
  assert.ok(problems.some((p) => p.includes('ExternalId')), problems.join('\n'))
})

test('facts quoted on the pages come from the template', () => {
  const facts = templateFacts(fresh())
  assert.equal(facts.region, 'us-east-1')
  assert.equal(facts.bucketName, 'cloudoptics-cur-<your account id>')
  assert.equal(facts.exportName, 'cloudoptics-cur2')
  assert.equal(facts.stackSetName, 'cloudoptics-resource-access')
  assert.equal(facts.exportExpiryDays, 400)
  assert.equal(facts.bucketKeptOnDelete, true)
  const out = buildExport(fresh(), data)
  const read = out.roles.filter((r) => r.readOnly)
  assert.deepEqual(read.map((r) => r.roleName), ['CloudopticsBillingReader', 'CloudopticsResourceReader'])
  for (const r of read) assert.equal(r.maxSessionSeconds, 3600)
})
