// Provider-neutral naming for AWS, Google Cloud, Azure and DigitalOcean (ADR-018 to ADR-021).
// A missing provider always means AWS, so older payloads and fixtures read as before.

import type { Provider } from '../api/types'

export type { Provider }

export const PROVIDERS: readonly Provider[] = ['aws', 'gcp', 'azure', 'digitalocean']

export const PROVIDER_LABEL: Record<Provider, string> = {
  aws: 'AWS',
  gcp: 'Google Cloud',
  azure: 'Azure',
  digitalocean: 'DigitalOcean',
}

const ACCOUNT_NOUN: Record<Provider, string> = {
  aws: 'account',
  gcp: 'project',
  azure: 'subscription',
  // A DigitalOcean token reads one team, and its costs and recommendations are the team's.
  digitalocean: 'team',
}

/** The provider, defaulting to AWS when the payload has none. */
export function providerOf(provider: Provider | null | undefined): Provider {
  return provider ?? 'aws'
}

/** What the provider calls the unit a connection reads: account, project or subscription. */
export function accountNoun(provider?: Provider | null): string {
  return ACCOUNT_NOUN[providerOf(provider)]
}

/** "AWS account 123456789012", "Google Cloud project my-proj", "Azure subscription …", "DigitalOcean team …". */
export function accountLabel(provider: Provider | null | undefined, id: string): string {
  const p = providerOf(provider)
  return `${PROVIDER_LABEL[p]} ${ACCOUNT_NOUN[p]} ${id}`
}

/** The distinct providers among the items, in a stable order; a missing provider counts as AWS. */
export function workspaceProviders(items: readonly { provider?: Provider | null }[]): Provider[] {
  const seen = new Set(items.map((i) => providerOf(i.provider)))
  return PROVIDERS.filter((p) => seen.has(p))
}

/** A known provider from an untyped value (audit log JSON), or undefined. */
export function asProvider(value: unknown): Provider | undefined {
  return typeof value === 'string' && (PROVIDERS as readonly string[]).includes(value) ? (value as Provider) : undefined
}

/** What one connection reads: an AWS account, a Google Cloud billing account (every project billed
 * to it), an Azure subscription or billing account (stored as a management connection), or a
 * DigitalOcean team. */
export function connectionKind(c: { provider?: Provider | null; accountType?: string | null }): string {
  const p = providerOf(c.provider)
  if (p === 'gcp') return 'billing account'
  if (p === 'azure') return c.accountType === 'management' ? 'billing account' : 'subscription'
  return ACCOUNT_NOUN[p]
}

/**
 * What a connection reads, for its card and messages: "AWS account 123456789012",
 * "Google Cloud billing account 012345-6789AB-CDEF01", "Azure subscription …" or "Azure billing
 * account …", "DigitalOcean team …". The ID is in awsAccountId for every provider.
 */
export function connectionLabel(c: {
  provider?: Provider | null
  accountType?: string | null
  awsAccountId: string
}): string {
  return `${PROVIDER_LABEL[providerOf(c.provider)]} ${connectionKind(c)} ${c.awsAccountId}`
}

/** What the Connections page offers per provider. Every registered plug-in has a billing
 * source (Reload bills) and an inventory (Scan now); the shared routes find it by cloud. */
export const CAPABILITIES: Record<Provider, { reload: boolean; scan: boolean }> = {
  aws: { reload: true, scan: true },
  gcp: { reload: true, scan: true },
  azure: { reload: true, scan: true },
  digitalocean: { reload: true, scan: true },
}

export function supports(provider: Provider | null | undefined, what: 'reload' | 'scan'): boolean {
  return CAPABILITIES[providerOf(provider)][what]
}

/**
 * Workspace-wide wording from the providers it has connected. With none connected yet, or AWS
 * alone, everything reads as it always has (AWS, Cost Explorer, Cost and Usage Report).
 */
export interface CloudWords {
  /** AWS alone (or nothing connected yet). */
  awsOnly: boolean
  /** Has an AWS connection (commitments, the price catalog and CUR are AWS-only). */
  hasAws: boolean
  /** "AWS", "Google Cloud": the one provider, or null with several. */
  single: string | null
  /** "AWS credits", "Azure credits" or "Credits". */
  credits: string
  /** Where provisional month-to-date figures come from: "AWS Cost Explorer", "Azure". */
  provisional: string
  /** The first full bill: "Cost and Usage Report" for AWS, "bill" otherwise. */
  firstBill: string
  /** What loaded bills are checked against: "AWS Cost Explorer", "Azure's own total". */
  total: string
}

export function cloudWords(providers: readonly Provider[]): CloudWords {
  const awsOnly = providers.length === 0 || (providers.length === 1 && providers[0] === 'aws')
  const only = providers.length === 1 ? providers[0] : undefined
  const single = awsOnly ? 'AWS' : only ? PROVIDER_LABEL[only] : null
  return {
    awsOnly,
    hasAws: providers.length === 0 || providers.includes('aws'),
    single,
    credits: single ? `${single} credits` : 'Credits',
    provisional: awsOnly ? 'AWS Cost Explorer' : (single ?? 'each provider'),
    firstBill: awsOnly ? 'Cost and Usage Report' : 'bill',
    total: awsOnly ? 'AWS Cost Explorer' : single ? `${single}'s own total` : 'each provider’s own total',
  }
}
