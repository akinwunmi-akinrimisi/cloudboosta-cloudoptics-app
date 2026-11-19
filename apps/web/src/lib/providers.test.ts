import { describe, expect, it } from 'vitest'

import { PROVIDER_LABEL, accountLabel, accountNoun, asProvider, providerOf, workspaceProviders } from './providers'

describe('PROVIDER_LABEL', () => {
  it('names every provider', () => {
    expect(PROVIDER_LABEL).toEqual({ aws: 'AWS', gcp: 'Google Cloud', azure: 'Azure', digitalocean: 'DigitalOcean' })
  })
})

describe('providerOf', () => {
  it('defaults a missing provider to AWS', () => {
    expect(providerOf(undefined)).toBe('aws')
    expect(providerOf(null)).toBe('aws')
    expect(providerOf('gcp')).toBe('gcp')
  })
})

describe('accountNoun', () => {
  it.each([
    ['aws', 'account'],
    ['gcp', 'project'],
    ['azure', 'subscription'],
    ['digitalocean', 'account'],
  ] as const)('%s calls it a(n) %s', (provider, noun) => {
    expect(accountNoun(provider)).toBe(noun)
  })

  it('treats a missing provider as AWS', () => {
    expect(accountNoun()).toBe('account')
  })
})

describe('accountLabel', () => {
  it('reads exactly as before for AWS', () => {
    expect(accountLabel('aws', '123456789012')).toBe('AWS account 123456789012')
    expect(accountLabel(undefined, '123456789012')).toBe('AWS account 123456789012')
  })

  it('uses each provider’s own noun', () => {
    expect(accountLabel('gcp', 'my-proj')).toBe('Google Cloud project my-proj')
    expect(accountLabel('azure', 'sub-1')).toBe('Azure subscription sub-1')
    expect(accountLabel('digitalocean', 'team-1')).toBe('DigitalOcean account team-1')
  })
})

describe('workspaceProviders', () => {
  it('is empty for no items', () => {
    expect(workspaceProviders([])).toEqual([])
  })

  it('counts a missing provider as AWS and drops duplicates', () => {
    expect(workspaceProviders([{}, { provider: 'aws' }, { provider: null }])).toEqual(['aws'])
  })

  it('lists distinct providers in a stable order', () => {
    expect(workspaceProviders([{ provider: 'azure' }, {}, { provider: 'gcp' }, { provider: 'azure' }])).toEqual([
      'aws',
      'gcp',
      'azure',
    ])
  })
})

describe('asProvider', () => {
  it('accepts known providers only', () => {
    expect(asProvider('gcp')).toBe('gcp')
    expect(asProvider('oracle')).toBeUndefined()
    expect(asProvider(3)).toBeUndefined()
    expect(asProvider(undefined)).toBeUndefined()
  })
})
