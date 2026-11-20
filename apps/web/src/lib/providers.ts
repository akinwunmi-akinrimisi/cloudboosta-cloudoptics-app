// Provider-neutral naming. Only AWS is live today; Google Cloud, Azure and DigitalOcean come
// later. A missing provider always means AWS, so older payloads and fixtures read as before.

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
  digitalocean: 'account',
}

/** The provider, defaulting to AWS when the payload has none. */
export function providerOf(provider: Provider | null | undefined): Provider {
  return provider ?? 'aws'
}

/** What the provider calls the unit a connection reads: account, project or subscription. */
export function accountNoun(provider?: Provider | null): string {
  return ACCOUNT_NOUN[providerOf(provider)]
}

/** "AWS account 123456789012", "Google Cloud project my-proj", "Azure subscription …". */
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

/**
 * What a connection reads, for its card and messages: "AWS account 123456789012", or
 * "Google Cloud billing account 012345-6789AB-CDEF01" (a Google Cloud connection is one billing
 * account; its ID is in awsAccountId).
 */
export function connectionLabel(c: { provider?: Provider | null; awsAccountId: string }): string {
  return providerOf(c.provider) === 'gcp' ? `Google Cloud billing account ${c.awsAccountId}` : accountLabel(c.provider, c.awsAccountId)
}
