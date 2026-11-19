import { Ban, ExternalLink, Receipt, Server, ShieldCheck, Wrench } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import permissions from '../data/permissions.json'
import { HOW_IT_WORKS_STACK, SECURITY_PERMISSIONS, siteUrl } from '../lib/site'

// "What Cloudoptics can read" on the Connect AWS step (docs/14 §6.1, FR-CON-13). The data is
// generated from the CloudFormation template by apps/site/scripts/build-permissions.mjs, the
// same source as the public /security page, and the site's tests fail when this copy is stale.

type Role = (typeof permissions.roles)[number]

const ICONS: Record<string, LucideIcon> = { BillingRole: Receipt, ResourceRole: Server, SetupRole: Wrench }
const noStop = (s: string) => s.replace(/\.$/, '')

const readRoles = permissions.roles.filter((r) => r.readOnly)
const otherRoles = permissions.roles.filter((r) => !r.readOnly)

/** Every action the template grants, for tests and the summary count. */
export const ALL_ACTIONS = [...new Set(permissions.roles.flatMap((r) => r.groups.flatMap((g) => g.actions)))]

function Actions({ actions }: { actions: string[] }) {
  return (
    <ul className="mt-1 flex flex-wrap gap-1" aria-label="AWS actions">
      {actions.map((a) => (
        <li key={a}>
          <code className="block rounded-sm bg-surface-2 px-1.5 py-0.5 font-mono text-xs break-all">{a}</code>
        </li>
      ))}
    </ul>
  )
}

function RoleSection({ role, note }: { role: Role; note: string }) {
  const Icon = ICONS[role.logicalId] ?? ShieldCheck
  return (
    <section>
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        <Icon size={16} aria-hidden="true" /> {role.title}
        <span className="font-normal text-muted">· {note}</span>
      </h2>
      <ul className="mt-2 flex flex-col gap-3">
        {role.groups.map((g) => (
          <li key={g.usedFor}>
            <p className="text-sm text-secondary">{noStop(g.usedFor)}</p>
            <Actions actions={g.actions} />
          </li>
        ))}
        {role.managedPolicies.length > 0 && (
          <li>
            <p className="text-sm text-secondary">Its own logs (AWS managed policy)</p>
            <Actions actions={role.managedPolicies} />
          </li>
        )}
      </ul>
    </section>
  )
}

/** Links to the public pages, opened in a new tab so the wizard stays put (docs/14 §6.3). */
export function SiteLinks() {
  const link = 'inline-flex min-h-6 items-center gap-1 text-sm text-accent underline'
  return (
    <p className="flex flex-wrap gap-x-4 gap-y-1">
      <a className={link} href={siteUrl(HOW_IT_WORKS_STACK)} target="_blank" rel="noreferrer">
        What will this create? <ExternalLink size={14} aria-hidden="true" />
        <span className="sr-only">(opens in a new tab)</span>
      </a>
      <a className={link} href={siteUrl(SECURITY_PERMISSIONS)} target="_blank" rel="noreferrer">
        Read every permission <ExternalLink size={14} aria-hidden="true" />
        <span className="sr-only">(opens in a new tab)</span>
      </a>
    </p>
  )
}

/** Collapsed by default: one line until opened, then every action grouped by what it is for. */
export function PermissionPanel() {
  return (
    <details className="rounded-sm border border-border px-3 py-2">
      <summary className="cursor-pointer text-sm font-medium">
        <ShieldCheck size={16} aria-hidden="true" className="mr-1 inline align-text-bottom" />
        What Cloudoptics can read
      </summary>
      <div className="mt-3 flex flex-col gap-5">
        {readRoles.map((r) => (
          <RoleSection key={r.logicalId} role={r} note="read-only" />
        ))}
        <section>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Ban size={16} aria-hidden="true" /> Never
          </h2>
          <ul className="mt-2 flex flex-col gap-1 text-sm text-secondary">
            {permissions.cannot.map((c) => (
              <li key={c}>{noStop(c)}</li>
            ))}
          </ul>
        </section>
        {otherRoles.map((r) => (
          <RoleSection key={r.logicalId} role={r} note="runs once in your account, Cloudoptics cannot use it" />
        ))}
      </div>
    </details>
  )
}
