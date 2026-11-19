// Reads the customer-facing CloudFormation template and the plain-language permission entries
// (src/data/permissions.yaml) and checks them against each other both ways (docs/14 §5.5, SITE-08).
// Used by build-permissions.mjs (build and typecheck) and by tests/permissions.test.mjs.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { parse } from 'yaml'

export const REPO_ROOT = join(import.meta.dirname, '..', '..', '..')

// CloudFormation short-form intrinsics (!Ref, !Sub, ...) as plain objects, in every YAML shape.
const INTRINSICS = ['Ref', 'Sub', 'GetAtt', 'Equals', 'If', 'Join', 'Select', 'Split', 'FindInMap', 'Not', 'And', 'Or', 'Condition', 'Base64', 'Cidr', 'GetAZs', 'ImportValue']
const customTags = INTRINSICS.flatMap((name) => {
  const key = name === 'Ref' || name === 'Condition' ? name : `Fn::${name}`
  return [
    { tag: `!${name}`, resolve: (value) => ({ [key]: value }) },
    { tag: `!${name}`, collection: 'seq', resolve: (seq) => ({ [key]: seq.toJSON() }) },
    { tag: `!${name}`, collection: 'map', resolve: (map) => ({ [key]: map.toJSON() }) },
  ]
})

export function parseTemplate(text) {
  return parse(text, { customTags, maxAliasCount: -1 })
}

export function loadTemplate(relPath) {
  return parseTemplate(readFileSync(join(REPO_ROOT, relPath), 'utf8'))
}

export function loadData(path = join(import.meta.dirname, '..', 'src', 'data', 'permissions.yaml')) {
  return parse(readFileSync(path, 'utf8'))
}

const asList = (v) => (v === undefined ? [] : Array.isArray(v) ? v : [v])

/** A resource ARN as a reader sees it: ${AWS::AccountId} becomes "<your account id>", and so on. */
export function readableResource(resource) {
  if (resource === '*') return '*'
  const text = typeof resource === 'string' ? resource : (resource?.['Fn::Sub'] ?? JSON.stringify(resource))
  return String(text)
    .replaceAll('${AWS::Partition}', 'aws')
    .replaceAll('${AWS::AccountId}', '<your account id>')
    .replaceAll('${AWS::Region}', 'us-east-1')
}

/** Every IAM role in a template: its statements (Allow only) and managed policy names. */
export function templateRoles(template) {
  const roles = {}
  for (const [logicalId, res] of Object.entries(template.Resources ?? {})) {
    if (res.Type !== 'AWS::IAM::Role') continue
    const props = res.Properties ?? {}
    const statements = []
    for (const policy of asList(props.Policies)) {
      for (const st of asList(policy.PolicyDocument?.Statement)) {
        statements.push({
          sid: st.Sid ?? null,
          effect: st.Effect,
          actions: asList(st.Action),
          notAction: st.NotAction !== undefined,
          resources: asList(st.Resource).map(readableResource),
        })
      }
    }
    const managed = asList(props.ManagedPolicyArns).map((arn) => readableResource(arn).split('/').pop())
    const trust = asList(props.AssumeRolePolicyDocument?.Statement)
    roles[logicalId] = {
      roleName: props.RoleName ?? null,
      maxSessionSeconds: props.MaxSessionDuration ?? null,
      requiresExternalId: trust.length > 0 && trust.every((t) => t.Condition?.StringEquals?.['sts:ExternalId'] !== undefined),
      trustedService: trust.map((t) => t.Principal?.Service).find(Boolean) ?? null,
      statements,
      managed,
    }
  }
  return roles
}

const READ_VERB = /^[a-z0-9-]+:(Get|Describe|List|View)[A-Za-z0-9*]*$/

/** Compares the template's roles with the plain-language entries. Returns a list of problems. */
export function compare(roles, data, extraTemplates = {}) {
  const problems = []
  const listed = new Set()
  for (const entry of data.roles ?? []) {
    const id = entry.logicalId
    listed.add(id)
    const role = roles[id]
    if (!role) {
      problems.push(`permissions.yaml describes role ${id}, which the template no longer has.`)
      continue
    }
    for (const st of role.statements) {
      if (st.effect !== 'Allow') problems.push(`${id}: statement ${st.sid ?? '(no Sid)'} is not an Allow; describe it by hand first.`)
      if (st.notAction) problems.push(`${id}: statement ${st.sid ?? '(no Sid)'} uses NotAction, which the page cannot describe.`)
    }
    const inTemplate = new Set(role.statements.flatMap((s) => s.actions))
    const inData = new Set((entry.groups ?? []).flatMap((g) => g.actions ?? []))
    for (const a of inTemplate) if (!inData.has(a)) problems.push(`${id}: the template grants ${a}, which has no plain-language entry in permissions.yaml.`)
    for (const a of inData) if (!inTemplate.has(a)) problems.push(`${id}: permissions.yaml lists ${a}, which the template no longer grants.`)
    for (const g of entry.groups ?? []) {
      if (!g.plain || !g.usedFor) problems.push(`${id}: the entry for ${(g.actions ?? []).join(', ')} needs both "plain" and "usedFor".`)
    }
    if (entry.readOnly) {
      for (const a of inTemplate) if (!READ_VERB.test(a)) problems.push(`${id} is marked readOnly but the template grants ${a}.`)
      if (!role.requiresExternalId) problems.push(`${id} is marked readOnly but its trust policy does not require the ExternalId.`)
      if (role.managed.length) problems.push(`${id} is marked readOnly but has managed policies: ${role.managed.join(', ')}.`)
    }
    const managedData = new Set((entry.managedPolicies ?? []).map((m) => m.policy))
    for (const m of role.managed) if (!managedData.has(m)) problems.push(`${id}: the template attaches ${m}, which has no entry in permissions.yaml.`)
    for (const m of managedData) if (!role.managed.includes(m)) problems.push(`${id}: permissions.yaml lists managed policy ${m}, which the template no longer attaches.`)
    if (entry.sameAs) {
      const other = extraTemplates[entry.sameAs]
      const otherRole = other && templateRoles(other)[id]
      if (!otherRole) {
        problems.push(`${id}: ${entry.sameAs} has no role ${id} to compare with.`)
      } else {
        const otherActions = new Set(otherRole.statements.flatMap((s) => s.actions))
        for (const a of inTemplate) if (!otherActions.has(a)) problems.push(`${id}: ${entry.sameAs} is missing ${a}; member accounts would get a different role.`)
        for (const a of otherActions) if (!inTemplate.has(a)) problems.push(`${id}: ${entry.sameAs} grants ${a}, which the main template does not.`)
      }
    }
  }
  for (const id of Object.keys(roles)) {
    if (!listed.has(id)) problems.push(`The template has role ${id}, which permissions.yaml does not describe.`)
  }
  return problems
}

/** Facts the pages quote, read from the template so the copy cannot drift from it. */
export function templateFacts(template) {
  const res = template.Resources ?? {}
  const code = String(res.SetupFunction?.Properties?.Code?.ZipFile ?? '')
  const regionRule = JSON.stringify(template.Rules ?? {})
  const bucket = readableResource(res.CurBucket?.Properties?.BucketName)
  const exp = res.CurExport?.Properties?.Export ?? {}
  return {
    templateVersion: String(template.Metadata?.cloudoptics?.templateVersion ?? ''),
    region: regionRule.includes('"us-east-1"') ? 'us-east-1' : null,
    bucketName: bucket,
    exportName: exp.Name ?? null,
    exportGranularity: exp.DataQuery?.TableConfigurations?.COST_AND_USAGE_REPORT?.TIME_GRANULARITY ?? null,
    exportFormat: exp.DestinationConfigurations?.S3Destination?.S3OutputConfigurations?.Format ?? null,
    exportExpiryDays: Number(code.match(/"Expiration":\s*\{\s*"Days":\s*(\d+)/)?.[1] ?? NaN) || null,
    stackSetName: code.match(/STACKSET = "([^"]+)"/)?.[1] ?? null,
    setupRuntime: res.SetupFunction?.Properties?.Runtime ?? null,
    bucketKeptOnDelete: /if kind == "Delete":\s*\n\s*return None\s*#\s*kept on purpose/.test(code),
  }
}

/** The JSON the site (and later the app's onboarding panel) reads. */
export function buildExport(template, data) {
  const roles = templateRoles(template)
  return {
    $comment: 'Generated by apps/site/scripts/build-permissions.mjs from infra/cloudformation/cloudoptics-access.yaml and apps/site/src/data/permissions.yaml. Do not edit.',
    template: data.template,
    facts: templateFacts(template),
    roles: data.roles.map((entry) => {
      const role = roles[entry.logicalId]
      return {
        logicalId: entry.logicalId,
        roleName: role.roleName,
        title: entry.title,
        readOnly: Boolean(entry.readOnly),
        usedBy: entry.usedBy,
        summary: entry.summary,
        maxSessionSeconds: role.maxSessionSeconds,
        requiresExternalId: role.requiresExternalId,
        managedPolicies: entry.managedPolicies ?? [],
        groups: entry.groups.map((g) => {
          const scopes = new Set(role.statements.filter((s) => s.actions.some((a) => g.actions.includes(a))).flatMap((s) => s.resources))
          return { actions: g.actions, plain: g.plain, usedFor: g.usedFor, resources: [...scopes] }
        }),
        actionCount: new Set(role.statements.flatMap((s) => s.actions)).size,
      }
    }),
    cannot: data.cannot,
  }
}
